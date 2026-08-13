import inspect
import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core import tmdb
from core import tvdb as tvdb_client
from models.media import Media, MediaType

logger = logging.getLogger(__name__)


async def create_media_safely(
    db: AsyncSession, tmdb_id: int | None, media_type: MediaType, **fields
) -> tuple[Media, bool]:
    """Add a new Media row, tolerating a concurrent insert of the same
    (tmdb_id, media_type) racing with this one. Returns (media, created) -
    created is False when the row returned is an existing one from a lost
    race, not the one just constructed from **fields.

    Dozens of call sites across the app each do their own "check for an
    existing row, then create if missing" - that check-then-create isn't
    atomic, so two concurrent calls for the same episode (e.g. a webhook
    delivered twice, or two connected media servers reporting the same play
    close together) could both decide it's missing and both insert, creating
    a silent duplicate. A unique index on (tmdb_id, media_type) now makes the
    loser of that race fail with IntegrityError instead - this catches that
    and returns whichever row actually won the race, so callers never see the
    difference between "created" and "lost the race, here's the real one"
    (see #157, whose crash was a downstream symptom of this exact problem).
    """
    media = Media(tmdb_id=tmdb_id, media_type=media_type, **fields)
    if tmdb_id is None:
        # No unique index applies to a null tmdb_id - nothing to race on.
        db.add(media)
        await db.flush()
        return media, True
    try:
        # add() happens *inside* the savepoint, not before it - add()-then-flush
        # must be a single unit the savepoint fully owns, or rolling back on
        # conflict leaves the session's flush-error state stuck even though the
        # SQL-level SAVEPOINT itself rolled back, poisoning every other pending
        # change in the session (verified against a real Postgres instance,
        # not just mocks - see this function's tests).
        async with db.begin_nested():
            db.add(media)
            await db.flush()
    except IntegrityError:
        result = await db.execute(
            select(Media)
            .where(Media.tmdb_id == tmdb_id, Media.media_type == media_type)
            .order_by(Media.id)
        )
        existing = result.scalars().first()
        if not existing:
            raise
        if media_type == MediaType.episode and (
            fields.get("season_number"), fields.get("episode_number")
        ) != (existing.season_number, existing.episode_number):
            # Same tmdb_id but disagreeing season/episode - almost certainly
            # TMDB having re-numbered this episode between two resolutions
            # (or, rarely, a genuine id collision between two unrelated
            # episodes) rather than a same-episode race. There's no safe way
            # to insert a second row for this tmdb_id, so the existing row is
            # still returned, but this is worth surfacing rather than masking.
            logger.warning(
                "Media tmdb_id=%s media_type=episode: existing row's season/episode "
                "(%s, %s) disagrees with the one just attempted (%s, %s) - reusing "
                "the existing row",
                tmdb_id,
                existing.season_number, existing.episode_number,
                fields.get("season_number"), fields.get("episode_number"),
            )
        return existing, False
    return media, True


def tmdb_season_covers(show_tmdb_data: dict | None, season_number: int, episode_number: int) -> bool:
    """True if the show's cached TMDB season list (shape: [{season_number,
    episode_count, name}, ...], as stored on Show.tmdb_data) plausibly has
    this season/episode position. Used to decide whether a TVDB episode with
    no local mapping genuinely has no TMDB counterpart (confidently absent,
    safe to enrich from TVDB directly) versus might still exist on TMDB but
    hasn't been matched yet (ambiguous — don't guess)."""
    if not show_tmdb_data:
        return False
    for season in show_tmdb_data.get("seasons", []):
        if season.get("season_number") == season_number:
            return episode_number <= (season.get("episode_count") or 0)
    return False


def is_unmapped_tvdb_episode(media: Media) -> bool:
    """True if this episode Media row was created from TVDB data because it
    has no TMDB counterpart — its tmdb_id is a TVDB episode id in disguise
    (see enrich_episode_from_tvdb), not a real TMDB id. Must be excluded from
    anything sent to services that expect real TMDB identifiers (Trakt,
    Simkl, MDBList)."""
    return (
        media.media_type == MediaType.episode
        and isinstance(media.tmdb_data, dict)
        and media.tmdb_data.get("source") == "tvdb"
    )


async def enrich_episode_from_tvdb(media: Media, tvdb_episode_data: dict) -> None:
    """Populate a bare episode Media record from TVDB data (shape: core.tvdb's
    format_episode output) for an episode that has no TMDB counterpart.

    Stores the TVDB episode id in tmdb_id — the same convention already used
    by the "resolve a fully-unmatched show to TVDB" flow (routers/sync.py) —
    so every tmdb_id-keyed consumer (ActionBar, ratings, collection, next-up)
    works unchanged, since none of them re-validate tmdb_id against a live
    TMDB call. tmdb_data.source is tagged "tvdb" so is_unmapped_tvdb_episode
    can identify and exclude these rows from outbound TMDB-id-based pushes.
    """
    tvdb_episode_id = tvdb_episode_data.get("tvdb_id")
    if tvdb_episode_id:
        media.tmdb_id = tvdb_episode_id
    media.title = tvdb_episode_data.get("name") or media.title
    media.overview = tvdb_episode_data.get("overview")
    if tvdb_episode_data.get("image_url"):
        media.poster_path = tvdb_episode_data["image_url"]
    media.release_date = tvdb_episode_data.get("air_date")
    media.tmdb_data = {
        "runtime": tvdb_episode_data.get("runtime"),
        "tvdb_episode_id": tvdb_episode_id,
        "source": "tvdb",
    }


def _extract_release_dates(results: list) -> dict:
    us_entry = next((e for e in results if e.get("iso_3166_1") == "US"), None)
    digital = physical = None
    if us_entry:
        for rd in us_entry.get("release_dates", []):
            t = rd.get("type")
            d = (rd.get("release_date") or "")[:10] or None
            if t == 4 and not digital:
                digital = d
            elif t == 5 and not physical:
                physical = d
    return {"digital": digital, "physical": physical}


async def enrich_media(
    media: Media,
    api_key: str = None,
    series_tmdb_id: int = None,
    tvdb_id: int | None = None,
    tvdb_api_key: str | None = None,
    tvdb_lang: str | None = None,
) -> None:
    """Fetch TMDB metadata and update the media record in place.

    For an episode, falls back to TVDB (tvdb_id + tvdb_api_key) when TMDB
    doesn't have this season/episode - some shows have season structures
    that only line up under TVDB's numbering, not TMDB's (#162, #186).
    Callers that don't pass tvdb_id/tvdb_api_key get the old TMDB-only
    behavior unchanged.
    """
    if media.media_type == MediaType.movie and not media.tmdb_id:
        return
    if media.media_type == MediaType.episode and not series_tmdb_id and not (tvdb_id and tvdb_api_key):
        return

    try:
        if media.media_type == MediaType.movie:
            data = await tmdb.get_movie(media.tmdb_id, api_key=api_key)
            media.title = data.get("title") or media.title
            media.original_title = data.get("original_title")
            media.overview = data.get("overview")
            media.poster_path = tmdb.poster_url(data.get("poster_path"))
            media.backdrop_path = tmdb.poster_url(data.get("backdrop_path"), size="w1280")
            media.release_date = data.get("release_date")
            media.tmdb_rating = data.get("vote_average")
            media.tmdb_data = {
                "runtime": data.get("runtime"),
                "genres": [g["name"] for g in data.get("genres", [])],
                "external_ids": data.get("external_ids", {}),
                "cast": [
                    {"name": c["name"], "character": c.get("character", ""), "profile_path": tmdb.poster_url(c.get("profile_path"), size="w185")}
                    for c in data.get("credits", {}).get("cast", [])[:10]
                ],
                "tagline": data.get("tagline"),
                "status": data.get("status"),
                "adult": data.get("adult", False),
                "release_dates": _extract_release_dates((data.get("release_dates") or {}).get("results", [])),
            }
            media.adult = data.get("adult", False)

        elif media.media_type == MediaType.series:
            if not media.tmdb_id:
                return
            data = await tmdb.get_show(media.tmdb_id, api_key=api_key)
            media.title = data.get("name") or media.title
            media.original_title = data.get("original_name")
            media.overview = data.get("overview")
            media.poster_path = tmdb.poster_url(data.get("poster_path"))
            media.backdrop_path = tmdb.poster_url(data.get("backdrop_path"), size="w1280")
            media.release_date = data.get("first_air_date")
            media.tmdb_rating = data.get("vote_average")
            media.tmdb_data = {
                "genres": [g["name"] for g in data.get("genres", [])],
                "cast": [
                    {"name": c["name"], "character": c.get("character", ""), "profile_path": tmdb.poster_url(c.get("profile_path"), size="w185")}
                    for c in data.get("credits", {}).get("cast", [])[:10]
                ],
                "tagline": data.get("tagline"),
                "status": data.get("status"),
                "adult": data.get("adult", False),
            }
            media.adult = data.get("adult", False)

        elif media.media_type == MediaType.episode:
            if media.season_number is None or media.episode_number is None:
                return
            data = None
            tmdb_error: Exception | None = None
            if series_tmdb_id:
                try:
                    data = await tmdb.get_episode(series_tmdb_id, media.season_number, media.episode_number, api_key=api_key)
                except Exception as e:
                    tmdb_error = e

            if data:
                media.tmdb_id = data.get("id") or media.tmdb_id
                media.title = data.get("name") or media.title
                media.overview = data.get("overview")
                media.poster_path = tmdb.poster_url(data.get("still_path"), size="w500")
                media.release_date = data.get("air_date")
                media.tmdb_rating = data.get("vote_average")
                media.tmdb_data = {
                    "runtime": data.get("runtime"),
                    "cast": [
                        {
                            "name": c["name"],
                            "character": c.get("character", ""),
                            "profile_path": tmdb.poster_url(c.get("profile_path"), size="w185")
                        }
                        for c in data.get("credits", {}).get("cast", [])[:10]
                    ],
                }
            elif tvdb_id and tvdb_api_key:
                # TMDB either doesn't have this show at all, or doesn't have
                # this season/episode under the same numbering TVDB uses for
                # it (#162, #186) - fall back to the show's TVDB match.
                raw_eps = await tvdb_client.get_series_episodes(tvdb_id, media.season_number, tvdb_api_key, language=tvdb_lang)
                tvdb_ep = next(
                    (tvdb_client.format_episode(e) for e in raw_eps if e.get("number") == media.episode_number),
                    None,
                )
                if not tvdb_ep:
                    raise tmdb_error or Exception(
                        f"Episode S{media.season_number}E{media.episode_number} not found on TMDB or TVDB"
                    )
                await enrich_episode_from_tvdb(media, tvdb_ep)
            elif tmdb_error:
                raise tmdb_error

    except Exception as e:
        # Don't let TMDB failures break webhook processing.
        # Set tmdb_data to an empty dict (not None) so the sync heal-check knows
        # enrichment was already attempted and won't retry it indefinitely.
        if media.tmdb_data is None:
            media.tmdb_data = {}
        from httpx import HTTPStatusError
        # media.title/tmdb_id are frequently still unset at this point (title is
        # only assigned a few lines up, from the TMDB response that just failed;
        # tmdb_id for episodes is only assigned on a successful fetch) — include
        # the row's own identity (id, if already flushed; type; season/episode;
        # show_id) so a repeat failure can actually be traced back to a row.
        context = (
            f"media_id={media.id!r} type={media.media_type} "
            f"season={media.season_number!r} episode={media.episode_number!r} "
            f"show_id={media.show_id!r} series_tmdb_id={series_tmdb_id!r}"
        )
        if isinstance(e, HTTPStatusError) and e.response.status_code == 404:
            print(f"  TMDB enrich SKIPPED for {media.title!r} (tmdb_id={media.tmdb_id!r}): not found on TMDB [{context}]")
        else:
            import traceback
            print(f"  TMDB enrich FAILED for {media.title!r} (tmdb_id={media.tmdb_id!r}): {e} [{context}]")
            traceback.print_exc()


async def apply_media_change_safely(db: AsyncSession, media: Media, mutate) -> Media:
    """Run `mutate()` against an *already-persisted* Media row and flush the
    result, tolerating a conflict if it just reassigned media.tmdb_id to a
    value some other row (of the same media_type) already claims.

    Several flows mutate an existing row's tmdb_id after the fact - most
    commonly a stub row (created with tmdb_id=None because it couldn't be
    matched yet) finally getting resolved by a later sync, a manual "match
    unmatched movie/episode" action, or an orphan-healing pass. Sharing a
    tmdb_id is possible (TMDB re-numbering, or a genuine coincidence) even
    though it's rare, and it must not crash the whole request/batch when it
    happens - this returns the pre-existing row instead.

    `mutate` may be sync or async, and MUST be the thing that actually changes
    media.tmdb_id - it needs to run *inside* this function's savepoint, not
    before calling this function, or the recovery below can't work (same
    lesson as create_media_safely's add()-inside-savepoint requirement: a
    state change made outside the savepoint's scope leaves the session's
    flush-error state stuck even after the SQL-level SAVEPOINT rolls back,
    poisoning every other pending change in the session - verified against a
    real Postgres instance, not just mocks).

    A media row still being created in the *same* flush cycle (media.id is
    None) doesn't need any of this - that creation's own savepoint already
    covers it, so `mutate` just runs directly.
    """
    if media.id is None:
        result = mutate()
        if inspect.isawaitable(result):
            await result
        return media

    # Captured up front: after a savepoint rolls back, SQLAlchemy expires the
    # touched object's attributes, and reading one back triggers an implicit
    # (sync) reload that isn't safe under AsyncSession outside an awaited
    # context (raises MissingGreenlet) - so nothing on `media` gets read again
    # once the except block below is reached. Everything needed there is
    # captured into plain local variables first instead.
    original_tmdb_id = media.tmdb_id
    original_media_id = media.id
    original_media_type = media.media_type
    resolved_tmdb_id: int | None = None
    try:
        async with db.begin_nested():
            result = mutate()
            if inspect.isawaitable(result):
                await result
            resolved_tmdb_id = media.tmdb_id  # read before the flush, never after
            await db.flush()
    except IntegrityError:
        if resolved_tmdb_id is None or resolved_tmdb_id == original_tmdb_id:
            # Not actually about the id we changed - re-raise rather than mask
            # an unrelated conflict (e.g. on some other dirty row in this flush).
            raise
        result = await db.execute(
            select(Media)
            .where(Media.tmdb_id == resolved_tmdb_id, Media.media_type == original_media_type)
            .order_by(Media.id)
        )
        existing = result.scalars().first()
        if not existing or existing.id == original_media_id:
            raise
        logger.warning(
            "Media.tmdb_id=%s just resolved for media.id=%s but media.id=%s already "
            "has it - discarding the change and using the existing row instead",
            resolved_tmdb_id, original_media_id, existing.id,
        )
        return existing
    return media


async def enrich_media_safely(
    db: AsyncSession,
    media: Media,
    api_key: str = None,
    series_tmdb_id: int = None,
    tvdb_id: int | None = None,
    tvdb_api_key: str | None = None,
    tvdb_lang: str | None = None,
) -> Media:
    """enrich_media(), wrapped so a newly-resolved episode tmdb_id that
    collides with another row returns the pre-existing row instead of
    crashing. Use this instead of a plain enrich_media() call anywhere the
    media passed in already has an id from an earlier create_media_safely()
    call - see apply_media_change_safely for the full explanation."""
    return await apply_media_change_safely(
        db,
        media,
        lambda: enrich_media(
            media,
            api_key=api_key,
            series_tmdb_id=series_tmdb_id,
            tvdb_id=tvdb_id,
            tvdb_api_key=tvdb_api_key,
            tvdb_lang=tvdb_lang,
        ),
    )
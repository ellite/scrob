import asyncio
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from datetime import date

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core import tmdb, tvdb
from models.base import MediaType
from models.collection import Collection, CollectionFile
from models.comments import Comment
from models.episode_order import EpisodeOrderMapping, UserShowEpisodeOrder
from models.events import WatchEvent
from models.lists import ListItem
from models.media import Media
from models.playback_progress import PlaybackProgress
from models.ratings import Rating
from models.rewatch import RewatchProgress

logger = logging.getLogger(__name__)


_VALID_ORDERS = {"tmdb", "tvdb"}


def _normalise_title(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _date_distance(left: str | None, right: str | None) -> int | None:
    if not left or not right:
        return None
    try:
        return abs((date.fromisoformat(left) - date.fromisoformat(right)).days)
    except ValueError:
        return None


async def get_episode_order(
    db: AsyncSession,
    user_id: int,
    series_tmdb_id: int,
) -> UserShowEpisodeOrder | None:
    result = await db.execute(
        select(UserShowEpisodeOrder).where(
            UserShowEpisodeOrder.user_id == user_id,
            UserShowEpisodeOrder.series_tmdb_id == series_tmdb_id,
        )
    )
    return result.scalar_one_or_none()


async def get_mappings_for_tvdb_season(
    db: AsyncSession,
    series_tmdb_id: int,
    tvdb_season_number: int,
) -> list[EpisodeOrderMapping]:
    result = await db.execute(
        select(EpisodeOrderMapping)
        .where(
            EpisodeOrderMapping.series_tmdb_id == series_tmdb_id,
            EpisodeOrderMapping.tvdb_season_number == tvdb_season_number,
        )
        .order_by(EpisodeOrderMapping.tvdb_episode_number)
    )
    return list(result.scalars().all())


async def get_mapping_by_tvdb_position(
    db: AsyncSession,
    series_tmdb_id: int,
    tvdb_season_number: int,
    tvdb_episode_number: int,
) -> EpisodeOrderMapping | None:
    result = await db.execute(
        select(EpisodeOrderMapping).where(
            EpisodeOrderMapping.series_tmdb_id == series_tmdb_id,
            EpisodeOrderMapping.tvdb_season_number == tvdb_season_number,
            EpisodeOrderMapping.tvdb_episode_number == tvdb_episode_number,
        )
    )
    return result.scalar_one_or_none()


async def get_episode_orders_for_series(
    db: AsyncSession,
    user_id: int,
    series_tmdb_ids: list[int],
) -> dict[int, UserShowEpisodeOrder]:
    """Batched form of get_episode_order - one query for many shows at once
    (see enrich_with_state, which needs this for every show on a page at
    once). A series_tmdb_id missing from the returned dict has no row, same
    "tmdb" default meaning as get_episode_order returning None."""
    if not series_tmdb_ids:
        return {}
    result = await db.execute(
        select(UserShowEpisodeOrder).where(
            UserShowEpisodeOrder.user_id == user_id,
            UserShowEpisodeOrder.series_tmdb_id.in_(series_tmdb_ids),
        )
    )
    return {row.series_tmdb_id: row for row in result.scalars().all()}


async def get_tmdb_to_tvdb_positions(
    db: AsyncSession,
    series_tmdb_ids: list[int],
) -> dict[tuple[int, int, int], EpisodeOrderMapping]:
    """Batched forward lookup (TMDB season/episode -> TVDB position) across
    many shows at once - the counterpart to get_mapping_by_tvdb_position's
    reverse lookup. Keyed by (series_tmdb_id, tmdb_season_number,
    tmdb_episode_number), which is unique per this table's own constraint.
    Callers should pass only the shows actually known to have a "tvdb"
    episode_order preference (see get_episode_orders_for_series) - passing
    every show on a page would turn this into a full-table fetch for no
    reason, since shows without that preference never need translated
    positions."""
    if not series_tmdb_ids:
        return {}
    result = await db.execute(
        select(EpisodeOrderMapping).where(
            EpisodeOrderMapping.series_tmdb_id.in_(series_tmdb_ids)
        )
    )
    return {
        (m.series_tmdb_id, m.tmdb_season_number, m.tmdb_episode_number): m
        for m in result.scalars().all()
    }


async def _match_tmdb_to_tvdb_episodes(
    tmdb_episodes: list[dict],
    tvdb_episodes: list[dict],
    series_tmdb_id: int,
    tmdb_api_key: str,
    *,
    used_tvdb_ids: set[int] | None = None,
    on_progress: Callable[[int, int], Awaitable[None]] | None = None,
) -> list[EpisodeOrderMapping]:
    """Matches TMDB episodes to TVDB episodes by external id, falling back to
    normalised title + air-date within 1 day. Returns new EpisodeOrderMapping
    instances (not yet added to any session) - shared by the full-show
    (ensure_episode_order_mapping) and incremental (ensure_episode_order_mapping_for_season)
    computations, which differ only in which episodes they pass in and how
    they persist the result.

    `used_tvdb_ids` seeds the "already spoken for" set so a caller matching
    only a subset of episodes (the incremental path) doesn't re-match a TVDB
    episode that's already mapped to a different TMDB episode elsewhere in
    the show.
    """
    tvdb_by_id = {
        int(episode["id"]): episode
        for episode in tvdb_episodes
        if episode.get("id") is not None
    }

    # Neither side has anything to match against — fail immediately instead of
    # burning API calls on a per-episode loop that's guaranteed to find nothing.
    if not tmdb_episodes or not tvdb_episodes:
        raise ValueError("No TMDB episodes could be matched to TVDB")

    async def _would_match(episode: dict) -> bool:
        ids = await tmdb.get_episode_external_ids(
            series_tmdb_id,
            int(episode["season_number"]),
            int(episode["episode_number"]),
            api_key=tmdb_api_key,
        )
        external_tvdb_id = ids.get("tvdb_id")
        if external_tvdb_id and tvdb_by_id.get(int(external_tvdb_id)):
            return True
        title = _normalise_title(episode.get("name"))
        if not title:
            return False
        return any(
            _normalise_title(candidate.get("name")) == title
            and (_date_distance(episode.get("air_date"), candidate.get("aired")) or 0) <= 1
            for candidate in tvdb_episodes
        )

    # A show-wide numbering/title mismatch (the usual cause of a total match
    # failure) shows up regardless of which episode is checked — probe a small
    # sample spread across the whole series (not just season 1, which is the
    # most likely place for one-off numbering quirks) so a doomed mapping fails
    # in a handful of requests instead of after fetching external IDs for every
    # episode.
    PROBE_SIZE = 10
    if len(tmdb_episodes) > PROBE_SIZE:
        sample_indices = sorted({(i * len(tmdb_episodes)) // PROBE_SIZE for i in range(PROBE_SIZE)})
        probe_matches = await asyncio.gather(*(_would_match(tmdb_episodes[i]) for i in sample_indices))
        if not any(probe_matches):
            raise ValueError("No TMDB episodes could be matched to TVDB")

    total_episodes = len(tmdb_episodes)
    if on_progress:
        await on_progress(0, total_episodes)

    semaphore = asyncio.Semaphore(5)
    progress_lock = asyncio.Lock()
    completed = 0

    async def load_external_ids(episode: dict) -> tuple[dict, dict]:
        nonlocal completed
        async with semaphore:
            ids = await tmdb.get_episode_external_ids(
                series_tmdb_id,
                int(episode["season_number"]),
                int(episode["episode_number"]),
                api_key=tmdb_api_key,
            )
        if on_progress:
            async with progress_lock:
                completed += 1
                snapshot = completed
            if snapshot % 10 == 0 or snapshot == total_episodes:
                await on_progress(snapshot, total_episodes)
        return episode, ids

    external_rows = await asyncio.gather(
        *(load_external_ids(episode) for episode in tmdb_episodes)
    )

    used_tvdb_ids = set(used_tvdb_ids or ())
    mappings: list[EpisodeOrderMapping] = []

    for episode, external_ids in external_rows:
        external_tvdb_id = external_ids.get("tvdb_id")
        match = tvdb_by_id.get(int(external_tvdb_id)) if external_tvdb_id else None
        method = "external_id"
        if match is None:
            title = _normalise_title(episode.get("name"))
            candidates = [
                candidate
                for candidate in tvdb_episodes
                if candidate.get("id") not in used_tvdb_ids
                and title
                and _normalise_title(candidate.get("name")) == title
                and (_date_distance(episode.get("air_date"), candidate.get("aired")) or 0) <= 1
            ]
            if len(candidates) == 1:
                match = candidates[0]
                method = "title_date"

        if match is None or match.get("seasonNumber") is None or match.get("number") is None:
            continue

        mapped_tvdb_id = int(match["id"])
        used_tvdb_ids.add(mapped_tvdb_id)
        mappings.append(
            EpisodeOrderMapping(
                series_tmdb_id=series_tmdb_id,
                tmdb_season_number=int(episode["season_number"]),
                tmdb_episode_number=int(episode["episode_number"]),
                tmdb_episode_id=int(episode["id"]),
                tvdb_id=mapped_tvdb_id,
                tvdb_season_number=int(match["seasonNumber"]),
                tvdb_episode_number=int(match["number"]),
                match_method=method,
            )
        )

    return mappings


async def _get_tvdb_id_for_show(
    series_tmdb_id: int,
    tmdb_api_key: str,
    cache_ttl: float | None,
) -> tuple[int, dict]:
    """(tvdb_id, show_data) - always a fresh-enough (per cache_ttl) TMDB
    lookup, even on the ensure_episode_order_mapping short-circuit path, so
    a corrected/updated TVDB cross-reference on TMDB's side is picked up
    without needing a full mapping recompute."""
    show_data = await tmdb.get_show(series_tmdb_id, api_key=tmdb_api_key, cache_ttl=cache_ttl)
    tvdb_id = (show_data.get("external_ids") or {}).get("tvdb_id")
    if not tvdb_id:
        raise ValueError("TMDB does not expose a TVDB identifier for this show")
    return int(tvdb_id), show_data


async def _fetch_show_episodes(
    series_tmdb_id: int,
    tvdb_id: int,
    show_data: dict,
    tmdb_api_key: str,
    tvdb_api_key: str,
    cache_ttl: float | None,
) -> tuple[list[dict], list[dict]]:
    """(tmdb_episodes, tvdb_episodes) for every season on both sides - the
    shared fetch phase for the full-show and incremental mapping
    computations. Bounded by season count (a handful of calls), not episode
    count - the expensive part (per-episode external-id lookups) happens
    separately in _match_tmdb_to_tvdb_episodes."""
    tmdb_season_numbers = sorted(
        {
            int(season["season_number"])
            for season in show_data.get("seasons") or []
            if season.get("season_number") is not None
        }
    )
    tvdb_series = await tvdb.get_series(tvdb_id, tvdb_api_key, cache_ttl=cache_ttl)
    tvdb_season_numbers = sorted(
        {
            int(season.get("number"))
            for season in tvdb_series.get("seasons") or []
            if season.get("number") is not None
            and (season.get("type") or {}).get("type") in (None, "official")
        }
    )
    if not tvdb_season_numbers:
        tvdb_season_numbers = sorted(
            {
                int(episode.get("seasonNumber"))
                for episode in tvdb_series.get("episodes") or []
                if episode.get("seasonNumber") is not None
            }
        )

    tmdb_seasons, tvdb_seasons = await asyncio.gather(
        asyncio.gather(
            *(
                tmdb.get_season(series_tmdb_id, number, api_key=tmdb_api_key, cache_ttl=cache_ttl)
                for number in tmdb_season_numbers
            )
        ),
        asyncio.gather(
            *(
                tvdb.get_series_episodes(tvdb_id, number, tvdb_api_key, cache_ttl=cache_ttl)
                for number in tvdb_season_numbers
            )
        ),
    )

    tmdb_episodes = [
        episode
        for season in tmdb_seasons
        for episode in season.get("episodes") or []
        if episode.get("season_number") is not None and episode.get("episode_number") is not None
    ]
    tvdb_episodes = [episode for season in tvdb_seasons for episode in season]
    return tmdb_episodes, tvdb_episodes


async def ensure_episode_order_mapping(
    db: AsyncSession,
    series_tmdb_id: int,
    tmdb_api_key: str,
    tvdb_api_key: str,
    *,
    force: bool = False,
    on_progress: Callable[[int, int], Awaitable[None]] | None = None,
) -> dict:
    existing_result = await db.execute(
        select(EpisodeOrderMapping).where(
            EpisodeOrderMapping.series_tmdb_id == series_tmdb_id
        )
    )
    existing = list(existing_result.scalars().all())
    # force=True means the caller explicitly asked to recompute (a "Refresh
    # Metadata" action) - bypass the shared TMDB/TVDB response caches so that
    # isn't a silent no-op if this show was fetched moments earlier. Both
    # caches use the same TTL/None-bypass convention, so this one cache_ttl
    # value is reused for every tmdb.* and tvdb.* call below.
    cache_ttl = None if force else tmdb.DEFAULT_CACHE_TTL
    tvdb_id, show_data = await _get_tvdb_id_for_show(series_tmdb_id, tmdb_api_key, cache_ttl)

    if existing and not force:
        return {
            "tvdb_id": tvdb_id,
            "matched": len(existing),
            "tmdb_episodes": len(existing),
            "unmatched": 0,
        }

    tmdb_episodes, tvdb_episodes = await _fetch_show_episodes(
        series_tmdb_id, tvdb_id, show_data, tmdb_api_key, tvdb_api_key, cache_ttl
    )

    mappings = await _match_tmdb_to_tvdb_episodes(
        tmdb_episodes, tvdb_episodes, series_tmdb_id, tmdb_api_key, on_progress=on_progress
    )
    if not mappings:
        raise ValueError("No TMDB episodes could be matched to TVDB")

    await db.execute(
        delete(EpisodeOrderMapping).where(
            EpisodeOrderMapping.series_tmdb_id == series_tmdb_id
        )
    )
    db.add_all(mappings)
    await db.flush()

    return {
        "tvdb_id": tvdb_id,
        "matched": len(mappings),
        "tmdb_episodes": len(tmdb_episodes),
        "unmatched": len(tmdb_episodes) - len(mappings),
    }


async def ensure_episode_order_mapping_for_season(
    db: AsyncSession,
    show,
    season_number: int,
    tmdb_api_key: str | None,
    tvdb_api_key: str | None,
) -> list[EpisodeOrderMapping]:
    """Incremental, additive counterpart to ensure_episode_order_mapping - for
    on-demand resolution during webhook ingest (#162), where a full
    delete-and-recompute is too heavy to run synchronously on every scrobble.

    `season_number` is the TVDB-native season a webhook just reported -
    resolving it requires searching across every TMDB season (TVDB/TMDB
    season boundaries can differ entirely for the same show, e.g. anime), so
    this still fetches every season's episode list on both sides (cheap -
    bounded by season count, and covered by the same response cache
    ensure_episode_order_mapping uses). What it skips is the expensive part:
    per-episode external-id lookups are only done for episodes that don't
    already have a mapping row, and existing rows for other seasons are left
    untouched (INSERT, not delete-and-reinsert).

    Returns only the newly-inserted mapping rows (possibly empty if nothing
    new could be matched - a real TVDB-only episode, not a bug).
    """
    if not show.tvdb_id or not tmdb_api_key or not tvdb_api_key:
        return []

    existing_result = await db.execute(
        select(EpisodeOrderMapping).where(
            EpisodeOrderMapping.series_tmdb_id == show.tmdb_id
        )
    )
    existing = list(existing_result.scalars().all())
    already_mapped_tmdb = {(m.tmdb_season_number, m.tmdb_episode_number) for m in existing}
    already_used_tvdb_ids = {m.tvdb_id for m in existing}

    if any(m.tvdb_season_number == season_number for m in existing):
        # Already have at least one mapping touching this TVDB season from a
        # prior call - the caller re-checks its specific position after this
        # returns, so there's nothing more to do unless the show's episode
        # count grew (a genuinely new episode), which force-refresh handles.
        return []

    try:
        show_data = await tmdb.get_show(show.tmdb_id, api_key=tmdb_api_key, cache_ttl=tmdb.DEFAULT_CACHE_TTL)
        tmdb_episodes, tvdb_episodes = await _fetch_show_episodes(
            show.tmdb_id, show.tvdb_id, show_data, tmdb_api_key, tvdb_api_key, tmdb.DEFAULT_CACHE_TTL
        )
    except Exception:
        return []

    unmapped_tmdb_episodes = [
        ep for ep in tmdb_episodes
        if (int(ep["season_number"]), int(ep["episode_number"])) not in already_mapped_tmdb
    ]
    if not unmapped_tmdb_episodes:
        return []

    try:
        mappings = await _match_tmdb_to_tvdb_episodes(
            unmapped_tmdb_episodes, tvdb_episodes, show.tmdb_id, tmdb_api_key,
            used_tvdb_ids=already_used_tvdb_ids,
        )
    except ValueError:
        return []
    if not mappings:
        return []

    db.add_all(mappings)
    await db.flush()
    return mappings


async def reconcile_divergent_episode_media(
    db: AsyncSession,
    show,
    season_number: int | None = None,
) -> dict:
    """#162: for a show whose TVDB and TMDB season structures diverge,
    episodes tracked via Jellyfin/Emby before this show's mapping was
    resolved can end up as a separate Media row keyed by the TVDB-native
    (season, episode) position, instead of the canonical TMDB-native row
    Trakt import and every other tracking path use for the same real
    episode. Once a mapping exists, both positions are known - find any such
    pair and merge the divergent row's data into the canonical one.

    `season_number` scopes this to mappings touching one TVDB season (the
    cheap path, called right after ensure_episode_order_mapping_for_season
    resolves a single season during webhook ingest); omitted, it sweeps
    every mapping for the show (called after a full ensure_episode_order_mapping
    recompute - switching a show to TVDB ordering, or "Refresh Metadata").

    Merge failures for one pair are logged and skipped rather than raised -
    this always runs as a side effect of something else (a mapping job, a
    webhook), which must not fail over a reconciliation problem.
    """
    query = select(EpisodeOrderMapping).where(EpisodeOrderMapping.series_tmdb_id == show.tmdb_id)
    if season_number is not None:
        query = query.where(EpisodeOrderMapping.tvdb_season_number == season_number)
    mappings = (await db.execute(query)).scalars().all()

    stats = {"merged": 0, "checked": 0}

    for mapping in mappings:
        if (
            mapping.tmdb_season_number == mapping.tvdb_season_number
            and mapping.tmdb_episode_number == mapping.tvdb_episode_number
        ):
            continue  # same position on both sides - nothing could have diverged
        stats["checked"] += 1

        canonical = (await db.execute(
            select(Media).where(
                Media.show_id == show.id,
                Media.media_type == MediaType.episode,
                Media.season_number == mapping.tmdb_season_number,
                Media.episode_number == mapping.tmdb_episode_number,
            )
        )).scalars().first()
        divergent = (await db.execute(
            select(Media).where(
                Media.show_id == show.id,
                Media.media_type == MediaType.episode,
                Media.season_number == mapping.tvdb_season_number,
                Media.episode_number == mapping.tvdb_episode_number,
            )
        )).scalars().first()

        if not canonical or not divergent or canonical.id == divergent.id:
            continue

        # Safety check: a Media row sitting at the raw TVDB position isn't
        # automatically the mis-tracked artifact of this mapped episode -
        # TVDB and TMDB can assign genuinely different, unrelated episodes to
        # the same numeric slot. Only merge if `divergent` is provably that
        # artifact: enrich_episode_from_tvdb (core/enrichment.py) always
        # stores the raw TVDB episode id in tmdb_id for an episode with no
        # TMDB counterpart, which is the exact same id this mapping's
        # tvdb_id was built from. Without this check, a real, correctly
        # tracked TMDB episode that just happens to share the same raw
        # (season, episode) numbers as this mapping's TVDB position would
        # get its watch history/ratings/etc. silently merged into a
        # completely different episode.
        if divergent.tmdb_id != mapping.tvdb_id:
            continue

        try:
            async with db.begin_nested():
                await _merge_episode_media(db, canonical, divergent)
            stats["merged"] += 1
        except Exception:
            logger.exception(
                "Failed to merge divergent episode media (show=%s canonical=%s divergent=%s)",
                show.id, canonical.id, divergent.id,
            )

    return stats


async def _merge_episode_media(db: AsyncSession, canonical: Media, divergent: Media) -> None:
    """Moves every reference to `divergent` onto `canonical`, deduplicating
    against rows canonical already has where a table's uniqueness would
    otherwise be violated, then deletes the now-empty divergent row."""
    # WatchEvent: no uniqueness on media_id - every play is real and distinct,
    # re-point them all directly.
    await db.execute(update(WatchEvent).where(WatchEvent.media_id == divergent.id).values(media_id=canonical.id))

    # RewatchProgress: unique per (rewatch_id, media_id) - move rows that
    # don't collide, drop the rest (canonical's own progress for that
    # rewatch cycle already covers it).
    for row in (await db.execute(select(RewatchProgress).where(RewatchProgress.media_id == divergent.id))).scalars().all():
        clash = (await db.execute(
            select(RewatchProgress).where(RewatchProgress.rewatch_id == row.rewatch_id, RewatchProgress.media_id == canonical.id)
        )).scalars().first()
        if clash:
            await db.delete(row)
        else:
            row.media_id = canonical.id

    # PlaybackProgress: unique per (user_id, media_id) - keep canonical's
    # progress if it already has one for that user, otherwise adopt divergent's.
    for row in (await db.execute(select(PlaybackProgress).where(PlaybackProgress.media_id == divergent.id))).scalars().all():
        clash = (await db.execute(
            select(PlaybackProgress).where(PlaybackProgress.user_id == row.user_id, PlaybackProgress.media_id == canonical.id)
        )).scalars().first()
        if clash:
            await db.delete(row)
        else:
            row.media_id = canonical.id

    # Rating: no hard DB constraint here, but the app never expects more than
    # one rating per (user, media) for a plain episode - same adopt-if-missing
    # rule as PlaybackProgress.
    for row in (await db.execute(select(Rating).where(Rating.media_id == divergent.id))).scalars().all():
        clash = (await db.execute(
            select(Rating).where(Rating.user_id == row.user_id, Rating.media_id == canonical.id)
        )).scalars().first()
        if clash:
            await db.delete(row)
        else:
            row.media_id = canonical.id

    # ListItem: episode entries never set season_number, so uniqueness is
    # effectively per (list_id, media_id) - dedupe the same way.
    for row in (await db.execute(select(ListItem).where(ListItem.media_id == divergent.id))).scalars().all():
        clash = (await db.execute(
            select(ListItem).where(ListItem.list_id == row.list_id, ListItem.media_id == canonical.id)
        )).scalars().first()
        if clash:
            await db.delete(row)
        else:
            row.media_id = canonical.id

    # Collection (+ its CollectionFile children, moved along with it): unique
    # per (user_id, media_id) - keep canonical's collection entry if the user
    # already has one, otherwise move divergent's whole entry over.
    for row in (await db.execute(select(Collection).where(Collection.media_id == divergent.id))).scalars().all():
        clash = (await db.execute(
            select(Collection).where(Collection.user_id == row.user_id, Collection.media_id == canonical.id)
        )).scalars().first()
        if clash:
            await db.delete(row)  # cascades to its CollectionFile rows
        else:
            row.media_id = canonical.id

    # Comment: not Media-FK'd at all (keyed by media_type/tmdb_id/season/episode
    # directly) - re-key rows matching the divergent identity to the canonical one.
    await db.execute(
        update(Comment).where(
            Comment.media_type == "episode",
            Comment.tmdb_id == divergent.tmdb_id,
            Comment.season_number == divergent.season_number,
            Comment.episode_number == divergent.episode_number,
        ).values(
            tmdb_id=canonical.tmdb_id,
            season_number=canonical.season_number,
            episode_number=canonical.episode_number,
        )
    )

    await db.flush()
    await db.delete(divergent)


def validate_episode_order(value: str) -> str:
    if value not in _VALID_ORDERS:
        raise ValueError(f"Unsupported episode order: {value}")
    return value

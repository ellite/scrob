from core import tmdb
from models.media import Media, MediaType


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


async def enrich_media(media: Media, api_key: str = None, series_tmdb_id: int = None) -> None:
    """Fetch TMDB metadata and update the media record in place."""
    if media.media_type == MediaType.movie and not media.tmdb_id:
        return
    if media.media_type == MediaType.episode and not series_tmdb_id:
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
                    {"name": c["name"], "character": c["character"], "profile_path": tmdb.poster_url(c.get("profile_path"), size="w185")}
                    for c in data.get("credits", {}).get("cast", [])[:10]
                ],
                "tagline": data.get("tagline"),
                "status": data.get("status"),
                "adult": data.get("adult", False),
                "release_dates": _extract_release_dates(data.get("release_dates", {}).get("results", [])),
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
            data = await tmdb.get_episode(series_tmdb_id, media.season_number, media.episode_number, api_key=api_key)
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
                        "character": c["character"],
                        "profile_path": tmdb.poster_url(c.get("profile_path"), size="w185")
                    }
                    for c in data.get("credits", {}).get("cast", [])[:10]
                ],
            }

    except Exception as e:
        # Don't let TMDB failures break webhook processing.
        # Set tmdb_data to an empty dict (not None) so the sync heal-check knows
        # enrichment was already attempted and won't retry it indefinitely.
        if media.tmdb_data is None:
            media.tmdb_data = {}
        from httpx import HTTPStatusError
        if isinstance(e, HTTPStatusError) and e.response.status_code == 404:
            print(f"  TMDB enrich SKIPPED for {media.title}: not found on TMDB (id={media.tmdb_id})")
        else:
            import traceback
            print(f"  TMDB enrich FAILED for {media.title}: {e}")
            traceback.print_exc()
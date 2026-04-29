from core import tmdb
from models.media import Media, MediaType


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
                "cast": [
                    {"name": c["name"], "character": c["character"], "profile_path": tmdb.poster_url(c.get("profile_path"), size="w185")}
                    for c in data.get("credits", {}).get("cast", [])[:10]
                ],
                "tagline": data.get("tagline"),
                "status": data.get("status"),
                "adult": data.get("adult", False),
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
        import traceback
        print(f"  TMDB enrich FAILED for {media.title}: {e}")
        traceback.print_exc()
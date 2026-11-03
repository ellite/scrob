# Emby and Jellyfin share the same REST API — all functions are re-exported.
import functools

from core.jellyfin import (
    EMBY_DATE_PLAYED_FORMAT,
    validate_connection,
    get_libraries,
    get_movies,
    get_shows,
    get_episodes,
    get_item,
    get_items_batch,
    get_items_watched_state,
    extract_quality,
    find_movie_by_tmdb_id,
    find_episode_by_ids,
    find_episode_in_series,
    build_tmdb_index,
    build_tvdb_index,
    mark_watched as _jellyfin_mark_watched,
    mark_unwatched,
    set_rating,
    scan_libraries,
)

# Same call, Emby's DatePlayed format (#530).
mark_watched = functools.partial(_jellyfin_mark_watched, date_format=EMBY_DATE_PLAYED_FORMAT)

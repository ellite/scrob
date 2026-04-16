import asyncio
import httpx
from core.config import settings

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p"

# Errors that are worth retrying (transient). 404/4xx are permanent — don't retry.
_RETRYABLE = (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError)


def get_headers(api_key: str = None) -> dict:
    key = api_key or getattr(settings, 'tmdb_api_key', None)
    if not key:
        return {}
    return {
        "Authorization": f"Bearer {key}",
        "accept": "application/json",
    }


async def _get(url: str, *, headers: dict = None, params: dict = None, max_retries: int = 3) -> dict:
    """Shared GET helper with retry + exponential backoff for transient failures."""
    last_exc: Exception = None
    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                r = await client.get(url, headers=headers or {}, params=params)
                if r.status_code == 429:
                    wait = int(r.headers.get("Retry-After", 2 ** (attempt + 1)))
                    await asyncio.sleep(wait)
                    continue
                r.raise_for_status()
                return r.json()
        except _RETRYABLE as e:
            last_exc = e
            if attempt < max_retries:
                await asyncio.sleep(2 ** (attempt + 1))  # 2s, 4s, 8s
        except httpx.HTTPStatusError:
            raise  # 4xx/5xx — don't retry, surface immediately
    raise last_exc


async def validate_api_key(api_key: str) -> bool:
    if not api_key:
        return False
    try:
        await _get(f"{TMDB_BASE}/authentication", headers=get_headers(api_key))
        return True
    except Exception:
        return False


async def get_movie(tmdb_id: int, api_key: str = None) -> dict:
    return await _get(
        f"{TMDB_BASE}/movie/{tmdb_id}",
        headers=get_headers(api_key),
        params={"append_to_response": "credits,release_dates,recommendations"},
    )


async def get_show(tmdb_id: int, api_key: str = None) -> dict:
    return await _get(
        f"{TMDB_BASE}/tv/{tmdb_id}",
        headers=get_headers(api_key),
        params={"append_to_response": "credits,content_ratings,recommendations,external_ids"},
    )


async def get_season(tmdb_id: int, season_number: int, api_key: str = None) -> dict:
    return await _get(
        f"{TMDB_BASE}/tv/{tmdb_id}/season/{season_number}",
        headers=get_headers(api_key),
    )


async def get_episode(tmdb_id: int, season_number: int, episode_number: int, api_key: str = None) -> dict:
    return await _get(
        f"{TMDB_BASE}/tv/{tmdb_id}/season/{season_number}/episode/{episode_number}",
        headers=get_headers(api_key),
        params={"append_to_response": "credits"},
    )


async def get_trending_movies(time_window: str = "day", page: int = 1, api_key: str = None) -> dict:
    return await _get(f"{TMDB_BASE}/trending/movie/{time_window}", headers=get_headers(api_key), params={"page": page})


async def get_trending_shows(time_window: str = "day", page: int = 1, api_key: str = None) -> dict:
    return await _get(f"{TMDB_BASE}/trending/tv/{time_window}", headers=get_headers(api_key), params={"page": page})


async def get_show_light(tmdb_id: int, api_key: str = None) -> dict:
    """Fetch base show details (includes last_episode_to_air / next_episode_to_air)."""
    return await _get(f"{TMDB_BASE}/tv/{tmdb_id}", headers=get_headers(api_key))


async def get_on_air_today(page: int = 1, api_key: str = None) -> dict:
    return await _get(f"{TMDB_BASE}/tv/airing_today", headers=get_headers(api_key), params={"page": page})


async def get_popular_movies(page: int = 1, api_key: str = None) -> dict:
    return await _get(f"{TMDB_BASE}/movie/popular", headers=get_headers(api_key), params={"page": page})


async def get_top_rated_movies(page: int = 1, api_key: str = None) -> dict:
    return await _get(f"{TMDB_BASE}/movie/top_rated", headers=get_headers(api_key), params={"page": page})


async def get_popular_shows(page: int = 1, api_key: str = None) -> dict:
    return await _get(f"{TMDB_BASE}/tv/popular", headers=get_headers(api_key), params={"page": page})


async def get_top_rated_shows(page: int = 1, api_key: str = None) -> dict:
    return await _get(f"{TMDB_BASE}/tv/top_rated", headers=get_headers(api_key), params={"page": page})


async def search_multi(q: str, page: int = 1, api_key: str = None) -> dict:
    return await _get(f"{TMDB_BASE}/search/multi", headers=get_headers(api_key), params={"query": q, "include_adult": "false", "page": page})


async def search_movies(q: str, page: int = 1, year: int | None = None, api_key: str = None) -> dict:
    params: dict = {"query": q, "include_adult": "false", "page": page}
    if year:
        params["year"] = year
    return await _get(f"{TMDB_BASE}/search/movie", headers=get_headers(api_key), params=params)


async def search_shows(q: str, page: int = 1, year: int | None = None, api_key: str = None) -> dict:
    params: dict = {"query": q, "include_adult": "false", "page": page}
    if year:
        params["first_air_date_year"] = year
    return await _get(f"{TMDB_BASE}/search/tv", headers=get_headers(api_key), params=params)


async def search_collection(q: str, page: int = 1, api_key: str = None) -> dict:
    return await _get(f"{TMDB_BASE}/search/collection", headers=get_headers(api_key), params={"query": q, "include_adult": "false", "page": page})


async def search_people(q: str, page: int = 1, api_key: str = None) -> dict:
    return await _get(f"{TMDB_BASE}/search/person", headers=get_headers(api_key), params={"query": q, "include_adult": "false", "page": page})


def poster_url(path: str, size: str = "w500") -> str | None:
    if not path:
        return None
    return f"{TMDB_IMAGE_BASE}/{size}{path}"


async def get_person(person_id: int, api_key: str = None) -> dict:
    return await _get(f"{TMDB_BASE}/person/{person_id}", headers=get_headers(api_key), params={"append_to_response": "combined_credits"})


async def get_movie_credits(movie_id: int, api_key: str = None) -> dict:
    return await _get(f"{TMDB_BASE}/movie/{movie_id}/credits", headers=get_headers(api_key))


async def get_genre_list(api_key: str = None) -> dict:
    return await _get(f"{TMDB_BASE}/genre/movie/list", headers=get_headers(api_key))


async def get_now_playing(page: int = 1, api_key: str = None) -> dict:
    return await _get(f"{TMDB_BASE}/movie/now_playing", headers=get_headers(api_key), params={"page": page})


async def get_upcoming_movies(page: int = 1, api_key: str = None) -> dict:
    return await _get(f"{TMDB_BASE}/movie/upcoming", headers=get_headers(api_key), params={"page": page})


async def get_on_air_this_week(page: int = 1, api_key: str = None) -> dict:
    return await _get(f"{TMDB_BASE}/tv/on_the_air", headers=get_headers(api_key), params={"page": page})


async def get_movie_recommendations(movie_id: int, api_key: str = None) -> dict:
    return await _get(f"{TMDB_BASE}/movie/{movie_id}/recommendations", headers=get_headers(api_key))


async def get_show_recommendations(show_id: int, api_key: str = None) -> dict:
    return await _get(f"{TMDB_BASE}/tv/{show_id}/recommendations", headers=get_headers(api_key))


async def discover_movies(
    page: int = 1,
    genre_id: int | None = None,
    year: int | None = None,
    min_rating: float | None = None,
    vote_count_min: int | None = None,
    vote_count_max: int | None = None,
    sort_by: str = "popularity.desc",
    watch_provider_id: int | None = None,
    watch_region: str = "US",
    with_original_language: str | None = None,
    api_key: str = None,
) -> dict:
    params: dict = {
        "page": page,
        "sort_by": sort_by,
        "include_adult": "false",
        "vote_count.gte": vote_count_min if vote_count_min is not None else 50,
    }
    if genre_id:
        params["with_genres"] = genre_id
    if year:
        params["primary_release_year"] = year
    if min_rating:
        params["vote_average.gte"] = min_rating
    if vote_count_max is not None:
        params["vote_count.lte"] = vote_count_max
    if watch_provider_id is not None:
        params["with_watch_providers"] = watch_provider_id
        params["watch_region"] = watch_region
    if with_original_language:
        params["with_original_language"] = with_original_language
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{TMDB_BASE}/discover/movie",
            headers=get_headers(api_key),
            params=params,
        )
        r.raise_for_status()
        return r.json()


async def discover_shows(
    page: int = 1,
    genre_id: int | None = None,
    year: int | None = None,
    min_rating: float | None = None,
    vote_count_min: int | None = None,
    vote_count_max: int | None = None,
    sort_by: str = "popularity.desc",
    status: int | None = None,
    watch_provider_id: int | None = None,
    watch_region: str = "US",
    with_original_language: str | None = None,
    api_key: str = None,
) -> dict:
    params: dict = {
        "page": page,
        "sort_by": sort_by,
        "include_adult": "false",
        "vote_count.gte": vote_count_min if vote_count_min is not None else 50,
    }
    if genre_id:
        params["with_genres"] = genre_id
    if year:
        params["first_air_date_year"] = year
    if min_rating:
        params["vote_average.gte"] = min_rating
    if vote_count_max is not None:
        params["vote_count.lte"] = vote_count_max
    if status is not None:
        params["with_status"] = status
    if watch_provider_id is not None:
        params["with_watch_providers"] = watch_provider_id
        params["watch_region"] = watch_region
    if with_original_language:
        params["with_original_language"] = with_original_language
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{TMDB_BASE}/discover/tv",
            headers=get_headers(api_key),
            params=params,
        )
        r.raise_for_status()
        return r.json()


async def get_collection(collection_id: int, api_key: str = None) -> dict:
    return await _get(f"{TMDB_BASE}/collection/{collection_id}", headers=get_headers(api_key))


async def get_movie_videos(tmdb_id: int, api_key: str = None) -> dict:
    return await _get(f"{TMDB_BASE}/movie/{tmdb_id}/videos", headers=get_headers(api_key))


async def find_by_external_id(external_id: str, source: str, api_key: str = None) -> dict:
    """Find a movie or TV show by an external ID (imdb_id, tvdb_id, etc.)."""
    return await _get(f"{TMDB_BASE}/find/{external_id}", headers=get_headers(api_key), params={"external_source": source})


async def get_external_ids(tmdb_id: int, type: str, api_key: str = None) -> dict:
    """Fetch external IDs (IMDB, TVDB, etc.) for a movie or TV show."""
    path = "movie" if type == "movie" else "tv"
    return await _get(f"{TMDB_BASE}/{path}/{tmdb_id}/external_ids", headers=get_headers(api_key))
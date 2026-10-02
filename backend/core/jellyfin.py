import asyncio
import re
import httpx
from datetime import datetime
from typing import Optional, List, Dict

TIMEOUT = httpx.Timeout(120.0)  # 120 second timeout

_TMDB_PROVIDER_PATH_RE = re.compile(r"/(?:movie|tv)/(\d+)")


def get_jellyfin_tmdb_id(provider_ids: dict) -> int | None:
    tid = provider_ids.get("Tmdb") or provider_ids.get("tmdb")
    if not tid:
        return None
    tid = str(tid)
    if tid.isdigit():
        return int(tid)
    # Some Emby TMDB metadata plugins encode an episode's provider id as a
    # relative path to its parent show/movie (e.g. "../tv/203124/season/1/episode/1")
    # instead of a plain numeric id (see GitHub #125) - pull the id out of that
    # instead of crashing the whole sync on int().
    m = _TMDB_PROVIDER_PATH_RE.search(tid)
    if m:
        return int(m.group(1))
    return None


def get_jellyfin_tvdb_id(provider_ids: dict) -> int | None:
    tid = provider_ids.get("Tvdb") or provider_ids.get("tvdb")
    if not tid:
        return None
    tid = str(tid)
    return int(tid) if tid.isdigit() else None


def _auth_headers(token: str) -> Dict[str, str]:
    # Jellyfin 12.0 removed legacy X-Emby-Token support; Authorization: MediaBrowser
    # Token="..." is the primary form and works on all versions (Jellyfin and Emby).
    return {"Authorization": f'MediaBrowser Token="{token}"'}


async def _get(url: str, token: str, path: str, params: Optional[Dict] = None) -> Dict:
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
        headers = _auth_headers(token)
        full_url = f"{url.rstrip('/')}/{path.lstrip('/')}"
        r = await client.get(full_url, headers=headers, params=params)
        r.raise_for_status()
        return r.json()

async def get_item(url: str, token: str, item_id: str, user_id: Optional[str] = None) -> Optional[Dict]:
    """Fetch full metadata for a single item by ID, including MediaStreams."""
    try:
        # Use the user-scoped endpoint when a user_id is available — the admin
        # Items/{id} endpoint may omit MediaStreams for non-admin tokens.
        path = f"Users/{user_id}/Items/{item_id}" if user_id else f"Items/{item_id}"
        data = await _get(url, token, path, params={"Fields": "MediaStreams,Path"})
        return data
    except Exception:
        return None


# Items?Ids= accepts at most this many comma-separated ids per request in
# practice - stay well under any server-side query-length limit.
_ITEMS_BATCH_CHUNK = 100


async def get_items_batch(
    url: str,
    token: str,
    item_ids: List[str],
    user_id: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Dict]:
    """Fetch just UserData for up to _ITEMS_BATCH_CHUNK items in a single
    Items?Ids= request (~0.5ms/item vs. ~55-800ms/item for one get_item call
    each - see GitHub #362). Returns {item_id: item_dict}; an id the caller
    passed in but that's missing from the result no longer exists on the
    server - the same signal get_item's None return used to carry for one id
    at a time. Callers with more than _ITEMS_BATCH_CHUNK ids must chunk
    themselves (see get_items_watched_state below) - this makes exactly one
    request per call, accepts an optional shared client the same way
    mark_watched/set_rating do, and never raises."""
    if not item_ids:
        return {}
    headers = _auth_headers(token)
    path = f"Users/{user_id}/Items" if user_id else "Items"
    full_url = f"{url.rstrip('/')}/{path}"
    params = {"Ids": ",".join(item_ids), "Fields": "UserData"}
    try:
        if client:
            r = await client.get(full_url, headers=headers, params=params)
            r.raise_for_status()
            data = r.json()
        else:
            async with httpx.AsyncClient(timeout=PUSH_TIMEOUT, follow_redirects=False) as c:
                r = await c.get(full_url, headers=headers, params=params)
                r.raise_for_status()
                data = r.json()
        return {item["Id"]: item for item in data.get("Items", []) if item.get("Id")}
    except Exception:
        return {}


async def get_items_watched_state(
    url: str,
    token: str,
    item_ids: List[str],
    user_id: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, bool]:
    """UserData.Played for every id in item_ids that still exists on the
    server, fetched in chunks of _ITEMS_BATCH_CHUNK via get_items_batch
    instead of one get_item call per id (#362). An id absent from the
    returned dict means "not found" - not "unplayed" - matching how callers
    already treat get_item returning None."""
    result: Dict[str, bool] = {}
    if not item_ids:
        return result
    chunks = [item_ids[i : i + _ITEMS_BATCH_CHUNK] for i in range(0, len(item_ids), _ITEMS_BATCH_CHUNK)]
    # Same concurrency cap the full-push loop itself uses elsewhere - plenty
    # for ~0.5ms/item batches, without firing every chunk at the server at once.
    sem = asyncio.Semaphore(10)

    async def _fetch(chunk: List[str]) -> Dict[str, Dict]:
        async with sem:
            return await get_items_batch(url, token, chunk, user_id=user_id, client=client)

    batches = await asyncio.gather(*(_fetch(chunk) for chunk in chunks))
    for items in batches:
        for item_id, item in items.items():
            result[item_id] = bool((item.get("UserData") or {}).get("Played"))
    return result


async def validate_connection(url: str, token: str, user_id: Optional[str] = None) -> bool:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0), follow_redirects=False) as client:
            headers = _auth_headers(token)

            # Basic connectivity check
            r = await client.get(f"{url.rstrip('/')}/System/Info", headers=headers)
            if r.status_code != 200:
                return False

            # Optional user validation
            if user_id:
                r = await client.get(f"{url.rstrip('/')}/Users/{user_id}", headers=headers)
                return r.status_code == 200

            return True
    except Exception:
        return False

async def get_libraries(url: str, token: str, user_id: str) -> list:
    data = await _get(url, token, f"Users/{user_id}/Views")
    return data.get("Items", [])


async def get_movies(library_id: str, url: str, token: str, user_id: str) -> list:
    all_items = []
    start = 0
    page_size = 500

    while True:
        data = await _get(url, token, f"Users/{user_id}/Items", params={
            "ParentId": library_id,
            "IncludeItemTypes": "Movie",
            "Recursive": True,
            "Fields": "ProviderIds,MediaStreams,Overview,Genres,CommunityRating,OfficialRating,RunTimeTicks,PremiereDate,UserData,DateCreated",
            "Limit": page_size,
            "StartIndex": start,
        })
        items = data.get("Items", [])
        all_items.extend(items)

        total = data.get("TotalRecordCount", 0)
        start += page_size
        if start >= total:
            break

    return all_items

async def get_shows(library_id: str, url: str, token: str, user_id: str) -> list:
    all_items = []
    start = 0
    page_size = 500

    while True:
        data = await _get(url, token, f"Users/{user_id}/Items", params={
            "ParentId": library_id,
            "IncludeItemTypes": "Series",
            "Recursive": True,
            "Fields": "ProviderIds",
            "Limit": page_size,
            "StartIndex": start,
        })
        items = data.get("Items", [])
        all_items.extend(items)

        total = data.get("TotalRecordCount", 0)
        start += page_size
        if start >= total:
            break

    return all_items

async def get_episodes(library_id: str, url: str, token: str, user_id: str) -> list:
    all_items = []
    start = 0
    page_size = 500

    while True:
        data = await _get(url, token, f"Users/{user_id}/Items", params={
            "ParentId": library_id,
            "IncludeItemTypes": "Episode",
            "Recursive": True,
            # Jellyfin returns virtual records for missing episodes unless they
            # are explicitly excluded. They have no local media file and must
            # never be imported into a user's collection.
            "ExcludeLocationTypes": "Virtual",
            "IsMissing": False,
            "Fields": "ProviderIds,MediaStreams,Overview,Genres,CommunityRating,RunTimeTicks,PremiereDate,UserData,DateCreated",
            "Limit": page_size,
            "StartIndex": start,
        })
        items = data.get("Items", [])
        all_items.extend(items)

        total = data.get("TotalRecordCount", 0)
        start += page_size
        if start >= total:
            break

    return all_items

def extract_quality(media_streams: list) -> dict:
    quality = {
        "resolution": None,
        "video_codec": None,
        "audio_codec": None,
        "audio_channels": None,
        "audio_languages": [],
        "subtitle_languages": [],
    }

    for stream in media_streams:
        stream_type = stream.get("Type", "")

        if stream_type == "Video" and not quality["video_codec"]:
            height = stream.get("Height", 0)
            width = stream.get("Width", 0)
            if width >= 3200 or height >= 2000:
                quality["resolution"] = "4K"
            elif width >= 1700 or height >= 800:
                quality["resolution"] = "1080p"
            elif width >= 1100 or height >= 540:
                quality["resolution"] = "720p"
            else:
                quality["resolution"] = f"{height}p"
            quality["video_codec"] = stream.get("Codec", "").upper()

        elif stream_type == "Audio":
            if not quality["audio_codec"]:
                quality["audio_codec"] = stream.get("Codec", "").upper()
                channels = stream.get("Channels", 0)
                if channels == 8:
                    quality["audio_channels"] = "7.1"
                elif channels == 6:
                    quality["audio_channels"] = "5.1"
                elif channels == 2:
                    quality["audio_channels"] = "2.0"
                else:
                    quality["audio_channels"] = str(channels)
            lang = stream.get("Language")
            if lang and lang not in quality["audio_languages"]:
                quality["audio_languages"].append(lang)

        elif stream_type == "Subtitle":
            lang = stream.get("Language")
            if lang and lang not in quality["subtitle_languages"]:
                quality["subtitle_languages"].append(lang)

    return quality

async def _scan_for_tmdb_match(url: str, token: str, item_type: str, tmdb_id: int) -> Optional[Dict]:
    """Paged fallback for find_movie_by_tmdb_id/find_episode_by_ids's series lookup.

    On some Jellyfin versions (10.11.11 confirmed) AnyProviderIdEquals is
    silently ignored - the server returns the whole unfiltered library instead
    of narrowing to the requested TMDB id, so a Limit=25 page is effectively
    the first 25 items in default order and almost never contains the real
    match (#300). Paging the whole library once and matching ProviderIds
    client-side is the only way to find it when that happens.
    """
    start = 0
    page_size = 500
    while True:
        data = await _get(url, token, "Items", params={
            "Recursive": True,
            "IncludeItemTypes": item_type,
            "Fields": "ProviderIds",
            "Limit": page_size,
            "StartIndex": start,
        })
        items = data.get("Items", [])
        match = next((i for i in items if get_jellyfin_tmdb_id(i.get("ProviderIds", {})) == tmdb_id), None)
        if match:
            return match
        total = data.get("TotalRecordCount", 0)
        start += page_size
        if start >= total or not items:
            return None


async def find_movie_by_tmdb_id(url: str, token: str, tmdb_id: int, user_id: Optional[str] = None) -> Optional[Dict]:
    """Search all Jellyfin libraries for a movie by TMDB ID. Returns the item with MediaStreams or None."""
    try:
        data = await _get(url, token, "Items", params={
            "Recursive": True,
            "IncludeItemTypes": "Movie",
            "AnyProviderIdEquals": f"Tmdb.{tmdb_id}",
            "Fields": "MediaStreams,Path,ProviderIds",
            "Limit": 25,
        })
        items = data.get("Items", [])
        # AnyProviderIdEquals doesn't reliably bind on every Jellyfin version - it
        # can return an unrelated item as Items[0], which would then get marked
        # watched/unwatched in this movie's place (see GitHub #247). Confirm the
        # match against the item's own ProviderIds instead of trusting the filter.
        match = next((i for i in items if get_jellyfin_tmdb_id(i.get("ProviderIds", {})) == tmdb_id), None)
        if not match:
            # TotalRecordCount exceeding what we already checked means the
            # filter didn't narrow the result set - the 25-item page can't be
            # trusted to be exhaustive, so fall back to a full scan (#300).
            if data.get("TotalRecordCount", 0) > len(items):
                match = await _scan_for_tmdb_match(url, token, "Movie", tmdb_id)
            if not match:
                return None
        # Fetch full detail with MediaStreams - user_id is required here, the
        # admin-only Items/{id} endpoint (no Users/ prefix) throws server-side
        # for a non-admin token (see #153).
        return await get_item(url, token, match["Id"], user_id=user_id)
    except Exception:
        return None


async def find_episode_in_series(url: str, token: str, series_id: str, season: int, episode: int, user_id: Optional[str] = None) -> Optional[Dict]:
    """Look up an episode within an already-known series.

    Split out of find_episode_by_ids so a caller that already has the series'
    Jellyfin item id (e.g. from a pre-built TMDB index - see build_tmdb_index,
    #300) can skip straight to this step instead of repeating the (possibly
    expensive) series resolution for every episode of the same show.

    Uses GET /Shows/{seriesId}/Episodes, not a SeriesId filter on /Items:
    that filter was removed from /Items at some point before Jellyfin 12.1.0
    (confirmed against its own OpenAPI spec - no SeriesId parameter is even
    listed anymore) and the server silently ignores the unknown param instead
    of erroring, so it used to return an arbitrary item from across the whole
    library rather than narrowing to this series. #436's reported "not found"
    warnings on modern Jellyfin were this - every episode lookup was silently
    matching (or failing to match, when the season/episode-index guard below
    happened to catch the wrong series) the wrong show.
    """
    try:
        ep_data = await _get(url, token, f"Shows/{series_id}/Episodes", params={
            "season": season,
            "userId": user_id,
            "Fields": "ProviderIds",
        })
        match = next((i for i in ep_data.get("Items", []) if i.get("IndexNumber") == episode), None)
        if not match:
            return None
        # user_id required - see #153.
        return await get_item(url, token, match["Id"], user_id=user_id)
    except Exception:
        return None


async def find_episode_by_ids(url: str, token: str, series_tmdb_id: int, season: int, episode: int, user_id: Optional[str] = None) -> Optional[Dict]:
    """Search all Jellyfin libraries for an episode by series TMDB ID + season + episode number."""
    try:
        # First find the series by TMDB ID
        series_data = await _get(url, token, "Items", params={
            "Recursive": True,
            "IncludeItemTypes": "Series",
            "AnyProviderIdEquals": f"Tmdb.{series_tmdb_id}",
            "Fields": "ProviderIds",
            "Limit": 25,
        })
        series_items = series_data.get("Items", [])
        # Same AnyProviderIdEquals unreliability as find_movie_by_tmdb_id (#247) -
        # confirm the series match ourselves before searching inside it, since an
        # unrelated series here would misattribute the episode lookup below too.
        series_match = next(
            (s for s in series_items if get_jellyfin_tmdb_id(s.get("ProviderIds", {})) == series_tmdb_id), None
        )
        if not series_match:
            # See the matching comment in find_movie_by_tmdb_id (#300).
            if series_data.get("TotalRecordCount", 0) > len(series_items):
                series_match = await _scan_for_tmdb_match(url, token, "Series", series_tmdb_id)
            if not series_match:
                return None
        series_id = series_match["Id"]
    except Exception:
        return None
    return await find_episode_in_series(url, token, series_id, season, episode, user_id=user_id)


async def build_tmdb_index(url: str, token: str, item_type: str) -> Dict[int, str]:
    """Pages the whole library once, building a tmdb_id -> item_id index.

    For a caller that needs many TMDB lookups in one job (e.g. a full push
    over thousands of items), this replaces one AnyProviderIdEquals request
    per item - which can't be trusted to narrow results on every Jellyfin
    version and, when it doesn't, degrades into a full per-item fallback
    scan (#300) - with exactly one paged scan for the whole job.
    """
    index: Dict[int, str] = {}
    start = 0
    page_size = 500
    while True:
        data = await _get(url, token, "Items", params={
            "Recursive": True,
            "IncludeItemTypes": item_type,
            "Fields": "ProviderIds",
            "Limit": page_size,
            "StartIndex": start,
        })
        items = data.get("Items", [])
        for item in items:
            tid = get_jellyfin_tmdb_id(item.get("ProviderIds", {}))
            if tid is not None and tid not in index:
                index[tid] = item["Id"]
        total = data.get("TotalRecordCount", 0)
        start += page_size
        if start >= total or not items:
            break
    return index


async def build_tvdb_index(url: str, token: str, item_type: str) -> Dict[int, str]:
    """Same as build_tmdb_index, but keyed by TVDB id.

    Used as a fallback for series Scrob only ever matched via TVDB (e.g. a
    legacy-agent Plex library - see plex.get_guids) and so carry no
    Show.tmdb_id at all, which would otherwise make them unmatchable against
    Jellyfin/Emby even though Jellyfin's own TheTVDB plugin tags them with a
    Tvdb provider id (GitHub #436).
    """
    index: Dict[int, str] = {}
    start = 0
    page_size = 500
    while True:
        data = await _get(url, token, "Items", params={
            "Recursive": True,
            "IncludeItemTypes": item_type,
            "Fields": "ProviderIds",
            "Limit": page_size,
            "StartIndex": start,
        })
        items = data.get("Items", [])
        for item in items:
            tid = get_jellyfin_tvdb_id(item.get("ProviderIds", {}))
            if tid is not None and tid not in index:
                index[tid] = item["Id"]
        total = data.get("TotalRecordCount", 0)
        start += page_size
        if start >= total or not items:
            break
    return index


async def scan_libraries(url: str, token: str) -> bool:
    """Trigger a full library scan on the server."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
            headers = _auth_headers(token)
            r = await client.post(
                f"{url.rstrip('/')}/Library/Refresh",
                headers=headers,
            )
            return r.status_code < 400
    except Exception:
        return False


PUSH_TIMEOUT = httpx.Timeout(15.0)  # shorter timeout for bulk push operations

async def mark_watched(url: str, token: str, user_id: str, item_id: str, client: httpx.AsyncClient | None = None, played_at: datetime | None = None) -> bool:
    """Mark a Jellyfin/Emby item as played, optionally stamped with the original watch date (naive UTC)."""
    headers = _auth_headers(token)
    params = {"DatePlayed": played_at.strftime("%Y-%m-%dT%H:%M:%SZ")} if played_at else None
    try:
        if client:
            r = await client.post(f"{url.rstrip('/')}/Users/{user_id}/PlayedItems/{item_id}", headers=headers, params=params)
            return r.status_code < 400
        async with httpx.AsyncClient(timeout=PUSH_TIMEOUT, follow_redirects=False) as c:
            r = await c.post(f"{url.rstrip('/')}/Users/{user_id}/PlayedItems/{item_id}", headers=headers, params=params)
            return r.status_code < 400
    except Exception:
        return False

async def mark_unwatched(url: str, token: str, user_id: str, item_id: str, client: httpx.AsyncClient | None = None) -> bool:
    """Mark a Jellyfin item as unplayed."""
    headers = _auth_headers(token)
    try:
        if client:
            r = await client.delete(f"{url.rstrip('/')}/Users/{user_id}/PlayedItems/{item_id}", headers=headers)
            return r.status_code < 400
        async with httpx.AsyncClient(timeout=PUSH_TIMEOUT, follow_redirects=False) as c:
            r = await c.delete(f"{url.rstrip('/')}/Users/{user_id}/PlayedItems/{item_id}", headers=headers)
            return r.status_code < 400
    except Exception:
        return False

async def set_rating(url: str, token: str, user_id: str, item_id: str, rating: float, client: httpx.AsyncClient | None = None) -> bool:
    """Set a star rating on a Jellyfin item (0–10 scale).

    POST .../UserData replaces the *entire* UserData object rather than patching
    it, so Played/PlayCount/PlaybackPositionTicks/IsFavorite/LastPlayedDate must
    be fetched first and merged in - otherwise a concurrent rating push silently
    resets watched status back to unwatched (#168).
    """
    get_headers = _auth_headers(token)
    post_headers = {**get_headers, "Content-Type": "application/json"}
    item_url = f"{url.rstrip('/')}/Users/{user_id}/Items/{item_id}"
    userdata_url = f"{item_url}/UserData"

    async def _do(c: httpx.AsyncClient) -> bool:
        item_r = await c.get(item_url, headers=get_headers, params={"Fields": "UserData"})
        item_r.raise_for_status()
        body = {**item_r.json().get("UserData", {}), "Rating": rating}
        r = await c.post(userdata_url, headers=post_headers, json=body)
        return r.status_code < 400

    try:
        if client:
            return await _do(client)
        async with httpx.AsyncClient(timeout=PUSH_TIMEOUT, follow_redirects=False) as c:
            return await _do(c)
    except Exception:
        return False

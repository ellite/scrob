import re
import httpx
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
    data = await _get(url, token, f"Users/{user_id}/Items", params={
        "ParentId": library_id,
        "IncludeItemTypes": "Series",
        "Recursive": True,
        "Fields": "ProviderIds",
        "Limit": 2000,
    })
    return data.get("Items", [])

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
            return None
        # Fetch full detail with MediaStreams - user_id is required here, the
        # admin-only Items/{id} endpoint (no Users/ prefix) throws server-side
        # for a non-admin token (see #153).
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
            return None
        series_id = series_match["Id"]

        # Then find the episode within that series
        ep_data = await _get(url, token, "Items", params={
            "SeriesId": series_id,
            "Recursive": True,
            "IncludeItemTypes": "Episode",
            "ParentIndexNumber": season,
            "IndexNumber": episode,
            "Fields": "MediaStreams,Path,ProviderIds",
            "Limit": 1,
        })
        ep_items = ep_data.get("Items", [])
        if not ep_items or ep_items[0].get("SeriesId") != series_id:
            return None
        # user_id required - see #153.
        return await get_item(url, token, ep_items[0]["Id"], user_id=user_id)
    except Exception:
        return None


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

async def mark_watched(url: str, token: str, user_id: str, item_id: str, client: httpx.AsyncClient | None = None) -> bool:
    """Mark a Jellyfin item as played."""
    headers = _auth_headers(token)
    try:
        if client:
            r = await client.post(f"{url.rstrip('/')}/Users/{user_id}/PlayedItems/{item_id}", headers=headers)
            return r.status_code < 400
        async with httpx.AsyncClient(timeout=PUSH_TIMEOUT, follow_redirects=False) as c:
            r = await c.post(f"{url.rstrip('/')}/Users/{user_id}/PlayedItems/{item_id}", headers=headers)
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

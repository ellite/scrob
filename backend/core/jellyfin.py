import httpx
from typing import Optional, List, Dict

TIMEOUT = httpx.Timeout(120.0)  # 120 second timeout

async def _get(url: str, token: str, path: str, params: Optional[Dict] = None) -> Dict:
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
        headers = {"X-Emby-Token": token}
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
            headers = {"X-Emby-Token": token}
            
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
            "Fields": "ProviderIds,MediaStreams,Overview,Genres,CommunityRating,OfficialRating,RunTimeTicks,PremiereDate,UserData",
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
            "Fields": "ProviderIds,MediaStreams,Overview,Genres,CommunityRating,RunTimeTicks,PremiereDate,UserData",
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
            if height >= 2160:
                quality["resolution"] = "4K"
            elif height >= 1080:
                quality["resolution"] = "1080p"
            elif height >= 720:
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

async def find_movie_by_tmdb_id(url: str, token: str, tmdb_id: int) -> Optional[Dict]:
    """Search all Jellyfin libraries for a movie by TMDB ID. Returns the item with MediaStreams or None."""
    try:
        data = await _get(url, token, "Items", params={
            "Recursive": True,
            "IncludeItemTypes": "Movie",
            "AnyProviderIdEquals": f"Tmdb.{tmdb_id}",
            "Fields": "MediaStreams,Path,ProviderIds",
            "Limit": 1,
        })
        items = data.get("Items", [])
        if not items:
            return None
        # Fetch full detail with MediaStreams
        return await get_item(url, token, items[0]["Id"])
    except Exception:
        return None


async def find_episode_by_ids(url: str, token: str, series_tmdb_id: int, season: int, episode: int) -> Optional[Dict]:
    """Search all Jellyfin libraries for an episode by series TMDB ID + season + episode number."""
    try:
        # First find the series by TMDB ID
        series_data = await _get(url, token, "Items", params={
            "Recursive": True,
            "IncludeItemTypes": "Series",
            "AnyProviderIdEquals": f"Tmdb.{series_tmdb_id}",
            "Fields": "ProviderIds",
            "Limit": 1,
        })
        series_items = series_data.get("Items", [])
        if not series_items:
            return None
        series_id = series_items[0]["Id"]

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
        if not ep_items:
            return None
        return await get_item(url, token, ep_items[0]["Id"])
    except Exception:
        return None


async def mark_watched(url: str, token: str, user_id: str, item_id: str) -> bool:
    """Mark a Jellyfin item as played."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
            headers = {"X-Emby-Token": token}
            r = await client.post(
                f"{url.rstrip('/')}/Users/{user_id}/PlayedItems/{item_id}",
                headers=headers,
            )
            return r.status_code < 400
    except Exception:
        return False

async def mark_unwatched(url: str, token: str, user_id: str, item_id: str) -> bool:
    """Mark a Jellyfin item as unplayed."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
            headers = {"X-Emby-Token": token}
            r = await client.delete(
                f"{url.rstrip('/')}/Users/{user_id}/PlayedItems/{item_id}",
                headers=headers,
            )
            return r.status_code < 400
    except Exception:
        return False

async def set_rating(url: str, token: str, user_id: str, item_id: str, rating: float) -> bool:
    """Set a star rating on a Jellyfin item (0–10 scale)."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
            headers = {"X-Emby-Token": token, "Content-Type": "application/json"}
            body = {"PlayedPercentage": None, "UnplayedItemCount": None, "Rating": rating}
            r = await client.post(
                f"{url.rstrip('/')}/Users/{user_id}/Items/{item_id}/UserData",
                headers=headers,
                json=body,
            )
            return r.status_code < 400
    except Exception:
        return False

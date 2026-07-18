import httpx
import xmltodict
from typing import Optional, List, Dict

TIMEOUT = httpx.Timeout(120.0)

async def _get(url: str, token: str, params: Optional[Dict] = None) -> Dict:
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
        headers = {
            "X-Plex-Token": token,
            "Accept": "application/json"
        }
        res = await client.get(url, headers=headers, params=params)
        res.raise_for_status()
        return res.json()

def get_guids(item: Dict) -> List[Dict]:
    """Return a normalised Guid list for a Plex item.

    Modern Plex returns a 'Guid' array: [{"id": "tmdb://123"}, ...].
    Legacy items may have an empty/missing 'Guid' but a single lowercase 'guid'
    string like 'com.plexapp.agents.thetvdb://73762/1/1'.
    """
    guids = item.get("Guid") or []
    if not guids:
        legacy = item.get("guid", "")
        if legacy:
            guids = [{"id": legacy}]
    return guids


def extract_tmdb_id(guids: List[Dict]) -> Optional[int]:
    if not guids:
        return None
    for guid in guids:
        id_str = guid.get("id", "")
        for prefix in ("tmdb://", "com.plexapp.agents.themoviedb://"):
            if id_str.startswith(prefix):
                try:
                    return int(id_str[len(prefix):].split("/")[0])
                except ValueError:
                    break
    return None


def extract_tvdb_id(guids: List[Dict]) -> Optional[str]:
    if not guids:
        return None
    for guid in guids:
        id_str = guid.get("id", "")
        for prefix in ("tvdb://", "com.plexapp.agents.thetvdb://"):
            if id_str.startswith(prefix):
                val = id_str[len(prefix):].split("/")[0].strip()
                return val if val else None
    return None


def extract_imdb_id(guids: List[Dict]) -> Optional[str]:
    if not guids:
        return None
    for guid in guids:
        id_str = guid.get("id", "")
        for prefix in ("imdb://", "com.plexapp.agents.imdb://"):
            if id_str.startswith(prefix):
                val = id_str[len(prefix):].split("/")[0].strip()
                return val if val else None
    return None

def extract_quality(media_list: List[Dict]) -> Dict:
    if not media_list:
        return {}
    
    # Plex usually has multiple 'Media' objects for different versions, we take the first
    m = media_list[0]
    h = m.get("height", 0)
    w = m.get("width", 0)

    # Prefer Plex's own videoResolution label (e.g. "1080", "720", "4k") when available.
    plex_res = str(m.get("videoResolution", "")).lower()
    if plex_res in ("4k", "2160"):
        resolution = "4K"
    elif plex_res == "1080":
        resolution = "1080p"
    elif plex_res == "720":
        resolution = "720p"
    elif plex_res in ("480", "sd"):
        resolution = "480p"
    elif plex_res:
        resolution = f"{plex_res}p"
    else:
        # Fallback using both width and height so cinemascope encodes like
        # 1920x800 (2.40:1) are not misclassified — width is the reliable dimension.
        if w >= 3200 or h >= 2000:
            resolution = "4K"
        elif w >= 1700 or h >= 800:
            resolution = "1080p"
        elif w >= 1100 or h >= 540:
            resolution = "720p"
        else:
            resolution = f"{h}p"

    quality = {
        "resolution": resolution,
        "video_codec": m.get("videoCodec"),
        "audio_codec": m.get("audioCodec"),
        "audio_channels": f"{m.get('audioChannels', 0)}.0" if m.get("audioChannels") else None,
        "audio_languages": [],
        "subtitle_languages": [],
    }
    
    # Plex JSON doesn't always have deep stream info in the list view, 
    # but we can try to extract from the first Part
    parts = m.get("Part", [])
    if parts:
        p = parts[0]
        quality["file_path"] = p.get("file")
        
        # Extract languages from streams if available
        streams = p.get("Stream", [])
        for s in streams:
            stream_type = s.get("streamType")
            # Plex uses language (e.g. "English") or languageCode (e.g. "en") or languageTag (e.g. "en")
            lang = s.get("languageTag") or s.get("languageCode") or s.get("language")
            
            if not lang:
                continue
                
            if stream_type == 2: # Audio
                if lang not in quality["audio_languages"]:
                    quality["audio_languages"].append(lang)
            elif stream_type == 3: # Subtitle
                if lang not in quality["subtitle_languages"]:
                    quality["subtitle_languages"].append(lang)
        
    return quality

async def get_item(url: str, token: str, rating_key: str) -> Optional[Dict]:
    """Fetch full metadata for a single item by ratingKey, including Media/Part/Stream detail."""
    try:
        data = await _get(
            f"{url.rstrip('/')}/library/metadata/{rating_key}",
            token,
            params={"includeGuids": 1},
        )
        items = data.get("MediaContainer", {}).get("Metadata", [])
        return items[0] if items else None
    except Exception:
        return None


async def find_movie_by_tmdb_id(url: str, token: str, tmdb_id: int) -> Optional[Dict]:
    """Search all Plex libraries for a movie by TMDB ID. Returns the item (with Media/Part detail) or None."""
    try:
        data = await _get(
            f"{url.rstrip('/')}/library/all",
            token,
            params={"type": 1, "guid": f"tmdb://{tmdb_id}", "includeGuids": 1},
        )
        items = data.get("MediaContainer", {}).get("Metadata", [])
        if not items:
            return None
        # Fetch the full item with Media/Part/Stream detail
        return await get_item(url, token, str(items[0]["ratingKey"]))
    except Exception:
        return None


async def find_episode_by_ids(url: str, token: str, series_tmdb_id: int, season: int, episode: int) -> Optional[Dict]:
    """Search all Plex libraries for an episode by series TMDB ID + season + episode number."""
    try:
        # Try filtering by grandparent GUID and indexes (supported on modern Plex)
        data = await _get(
            f"{url.rstrip('/')}/library/all",
            token,
            params={
                "type": 4,
                "grandparentGuid": f"tmdb://{series_tmdb_id}",
                "parentIndex": season,
                "index": episode,
                "includeGuids": 1,
            },
        )
        items = data.get("MediaContainer", {}).get("Metadata", [])
        if items:
            return await get_item(url, token, str(items[0]["ratingKey"]))
        return None
    except Exception:
        return None


async def validate_connection(url: str, token: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0), follow_redirects=False) as client:
            headers = {
                "X-Plex-Token": token,
                "Accept": "application/json"
            }
            # Simple endpoint to check connection
            r = await client.get(f"{url.rstrip('/')}/", headers=headers)
            return r.status_code == 200
    except Exception:
        return False

async def get_libraries(url: str, token: str) -> List[Dict]:
    data = await _get(f"{url.rstrip('/')}/library/sections", token)
    return data.get("MediaContainer", {}).get("Directory", [])

async def get_movies(url: str, token: str, section_id: str) -> List[Dict]:
    params = {"includeGuids": 1}
    data = await _get(f"{url.rstrip('/')}/library/sections/{section_id}/all", token, params=params)
    return data.get("MediaContainer", {}).get("Metadata", [])

async def get_shows(url: str, token: str, section_id: str) -> List[Dict]:
    params = {"includeGuids": 1}
    data = await _get(f"{url.rstrip('/')}/library/sections/{section_id}/all", token, params=params)
    return data.get("MediaContainer", {}).get("Metadata", [])

async def get_seasons(url: str, token: str, section_id: str) -> List[Dict]:
    """Fetch season metadata, including user ratings, from a TV library."""
    params = {"type": 3, "includeGuids": 1}
    data = await _get(f"{url.rstrip('/')}/library/sections/{section_id}/all", token, params=params)
    return data.get("MediaContainer", {}).get("Metadata", [])


async def get_episodes(url: str, token: str, section_id: str) -> List[Dict]:
    params = {"type": 4, "includeGuids": 1}
    data = await _get(f"{url.rstrip('/')}/library/sections/{section_id}/all", token, params=params)
    return data.get("MediaContainer", {}).get("Metadata", [])

async def get_recently_added(url: str, token: str, section_id: str, media_type: int, limit: int = 50) -> List[Dict]:
    """Fetch the most recently-added items from a library section.

    media_type: 1 = movie, 4 = episode
    Returns items with full Guid/Media/Part detail.
    """
    try:
        data = await _get(
            f"{url.rstrip('/')}/library/sections/{section_id}/recentlyAdded",
            token,
            params={"type": media_type, "includeGuids": 1, "X-Plex-Container-Size": limit},
        )
        return data.get("MediaContainer", {}).get("Metadata", [])
    except Exception:
        return []


METADATA_BASE = "https://metadata.provider.plex.tv"
DISCOVER_BASE = "https://discover.provider.plex.tv"
PLEX_TV_BASE  = "https://plex.tv"
COMMUNITY_BASE = "https://community.plex.tv"
_CLOUD_HEADERS = {
    "Accept": "application/json",
    "X-Plex-Product": "Scrob",
    "X-Plex-Client-Identifier": "scrob-watchlist",
}


async def _post_graphql(token: str, query: str, variables: Optional[Dict] = None) -> Dict:
    """POST a GraphQL query to the Plex community API."""
    payload: Dict = {"query": query}
    if variables:
        payload["variables"] = variables
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        res = await client.post(
            f"{COMMUNITY_BASE}/api",
            headers={"X-Plex-Token": token, "Content-Type": "application/json", "Accept": "application/json"},
            json=payload,
        )
        res.raise_for_status()
        return res.json()


async def get_all_friends(token: str) -> List[Dict]:
    """Return all Plex friends (server users) visible to this token via the community GraphQL API."""
    try:
        data = await _post_graphql(token, """
            query GetAllFriends {
              allFriendsV2 {
                user { id username displayName }
              }
            }
        """)
        friends = []
        for entry in (data.get("data") or {}).get("allFriendsV2") or []:
            user = entry.get("user", {})
            if user.get("id"):
                friends.append({
                    "watchlist_id": user["id"],
                    "username": user.get("username", ""),
                    "display_name": user.get("displayName", ""),
                })
        return friends
    except Exception:
        return []


async def get_friend_watchlist(token: str, watchlist_id: str) -> List[Dict]:
    """Fetch all watchlist items for a friend via the community GraphQL API.
    Returns list of {id (plex metadata id), title, type} — no GUIDs; enrich separately.
    """
    items = []
    cursor = None
    query = """
        query GetWatchlist($user: UserInput!, $first: PaginationInt!, $after: String) {
          userV2(user: $user) {
            ... on User {
              watchlist(first: $first, after: $after) {
                nodes { id title type }
                pageInfo { hasNextPage endCursor }
              }
            }
          }
        }
    """
    while True:
        try:
            data = await _post_graphql(
                token, query,
                variables={"user": {"id": watchlist_id}, "first": 100, "after": cursor},
            )
        except Exception:
            break
        watchlist = (data.get("data") or {}).get("userV2", {}).get("watchlist", {})
        nodes = watchlist.get("nodes", [])
        items.extend(nodes)
        page_info = watchlist.get("pageInfo", {})
        if not page_info.get("hasNextPage") or not page_info.get("endCursor"):
            break
        cursor = page_info["endCursor"]
    return items


async def enrich_plex_item(token: str, plex_id: str) -> Optional[Dict]:
    """Fetch full metadata for a Plex community item to get GUIDs (TMDB/TVDB/IMDB IDs)."""
    try:
        data = await _get(
            f"{DISCOVER_BASE}/library/metadata/{plex_id}",
            token,
            params={"includeGuids": 1},
        )
        items = data.get("MediaContainer", {}).get("Metadata", [])
        return items[0] if items else None
    except Exception:
        return None


async def resolve_tmdb_ratingkey(token: str, tmdb_id: int, media_type: str) -> str | None:
    """Return the Plex Discover ratingKey for an item identified by TMDB ID.

    media_type must be 'movie' or 'show'.
    Returns None if the item cannot be found.
    """
    plex_type = "1" if media_type == "movie" else "2"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            res = await client.get(
                f"{DISCOVER_BASE}/library/sections/computer/all",
                headers={"X-Plex-Token": token, "Accept": "application/json"},
                params={"X-Plex-Token": token, "guid": f"tmdb://{tmdb_id}", "type": plex_type, "includeGuids": 1},
            )
            if res.status_code >= 400:
                return None
            data = res.json()
        items = data.get("MediaContainer", {}).get("Metadata", [])
        return items[0]["ratingKey"] if items else None
    except Exception:
        return None


async def resolve_season_rating_key(
    url: str,
    token: str,
    show_tmdb_id: int,
    season_number: int,
) -> str | None:
    """Resolve a local Plex season ratingKey from a TMDB show ID and season number."""
    try:
        data = await _get(
            f"{url.rstrip('/')}/library/sections/all",
            token,
            params={"type": 2, "guid": f"tmdb://{show_tmdb_id}", "includeGuids": 1},
        )
        shows = data.get("MediaContainer", {}).get("Metadata", [])
        show = next(
            (
                item
                for item in shows
                if extract_tmdb_id(get_guids(item)) == show_tmdb_id
            ),
            None,
        )
        if not show or not show.get("ratingKey"):
            return None
        children = await _get(
            f"{url.rstrip('/')}/library/metadata/{show['ratingKey']}/children",
            token,
        )
        seasons = children.get("MediaContainer", {}).get("Metadata", [])
        match = next(
            (
                item
                for item in seasons
                if item.get("type") == "season" and item.get("index") == season_number
            ),
            None,
        )
        return str(match["ratingKey"]) if match and match.get("ratingKey") else None
    except Exception:
        return None


async def add_to_watchlist(token: str, rating_key: str) -> bool:
    """Add a Plex item to the user's watchlist by its Discover ratingKey."""
    try:
        async with httpx.AsyncClient(timeout=PUSH_TIMEOUT, follow_redirects=True) as client:
            r = await client.put(
                f"{DISCOVER_BASE}/actions/addToWatchlist",
                headers={"X-Plex-Token": token, "Accept": "application/json"},
                params={"X-Plex-Token": token, "ratingKey": rating_key},
            )
            return r.status_code < 400
    except Exception:
        return False


async def remove_from_watchlist(token: str, rating_key: str) -> bool:
    """Remove a Plex item from the user's watchlist by its Discover ratingKey."""
    try:
        async with httpx.AsyncClient(timeout=PUSH_TIMEOUT, follow_redirects=True) as client:
            r = await client.delete(
                f"{DISCOVER_BASE}/actions/removeFromWatchlist",
                headers={"X-Plex-Token": token, "Accept": "application/json"},
                params={"X-Plex-Token": token, "ratingKey": rating_key},
            )
            return r.status_code < 400
    except Exception:
        return False


async def get_watchlist(token: str) -> List[Dict]:
    """Fetch all items from the user's Plex watchlist via the Plex Discover API."""
    items: List[Dict] = []
    start = 0
    url = f"{DISCOVER_BASE}/library/sections/watchlist/all"
    while True:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            res = await client.get(
                url,
                headers={"X-Plex-Token": token, "Accept": "application/json"},
                params={"X-Plex-Token": token, "X-Plex-Container-Start": start, "includeGuids": 1},
            )
            res.raise_for_status()
            data = res.json()
        container = data.get("MediaContainer", {})
        batch = container.get("Metadata", [])
        items.extend(batch)
        total = container.get("totalSize", 0) or len(items)
        start += len(batch)
        if not batch or start >= total:
            break
    return items


PUSH_TIMEOUT = httpx.Timeout(15.0)

async def mark_watched(url: str, token: str, rating_key: str, client: httpx.AsyncClient | None = None) -> bool:
    """Scrobble a media item as watched on Plex."""
    headers = {"X-Plex-Token": token, "Accept": "application/json"}
    params = {"key": rating_key, "identifier": "com.plexapp.plugins.library"}
    try:
        if client:
            r = await client.get(f"{url.rstrip('/')}/:/scrobble", headers=headers, params=params)
            return r.status_code < 400
        async with httpx.AsyncClient(timeout=PUSH_TIMEOUT, follow_redirects=False) as c:
            r = await c.get(f"{url.rstrip('/')}/:/scrobble", headers=headers, params=params)
            return r.status_code < 400
    except Exception:
        return False

async def mark_unwatched(url: str, token: str, rating_key: str) -> bool:
    """Unscrobble a media item on Plex."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
            headers = {"X-Plex-Token": token, "Accept": "application/json"}
            r = await client.get(
                f"{url.rstrip('/')}/:/unscrobble",
                headers=headers,
                params={"key": rating_key, "identifier": "com.plexapp.plugins.library"},
            )
            return r.status_code < 400
    except Exception:
        return False

async def scan_libraries(url: str, token: str, section_keys: list[str]) -> bool:
    """Trigger a library scan on the given section keys. Scans all sections if list is empty."""
    try:
        if not section_keys:
            libraries = await get_libraries(url, token)
            section_keys = [lib["key"] for lib in libraries]
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
            headers = {"X-Plex-Token": token, "Accept": "application/json"}
            for key in section_keys:
                r = await client.get(
                    f"{url.rstrip('/')}/library/sections/{key}/refresh",
                    headers=headers,
                )
                if r.status_code >= 400:
                    return False
        return True
    except Exception:
        return False


async def set_rating(url: str, token: str, rating_key: str, rating: float, client: httpx.AsyncClient | None = None) -> bool:
    """Set a star rating on a Plex item (0–10 scale)."""
    headers = {"X-Plex-Token": token, "Accept": "application/json"}
    try:
        if client:
            r = await client.put(f"{url.rstrip('/')}/library/metadata/{rating_key}/userRating", headers=headers, params={"rating": rating})
            return r.status_code < 400
        async with httpx.AsyncClient(timeout=PUSH_TIMEOUT, follow_redirects=False) as c:
            r = await c.put(f"{url.rstrip('/')}/library/metadata/{rating_key}/userRating", headers=headers, params={"rating": rating})
            return r.status_code < 400
    except Exception:
        return False

import asyncio
import json
import logging
import re
import httpx
import xmltodict
from datetime import datetime, timezone
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(120.0)

async def _get(url: str, token: str, params: Optional[Dict] = None) -> Dict:
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
        headers = {
            "X-Plex-Token": token,
            "Accept": "application/json"
        }
        res = await client.get(url, headers=headers, params=params)
        res.raise_for_status()
        try:
            return res.json()
        except UnicodeDecodeError:
            # Some Plex Media Server setups emit a genuinely non-UTF-8 byte
            # somewhere in a scraped field (title/summary/etc, likely a
            # mis-transcoded legacy agent result) despite claiming a JSON
            # response - one bad byte anywhere in the payload used to fail
            # decoding of the *entire* response, taking the whole request
            # (e.g. an entire page of watch history) down with it (#388).
            # Decode leniently instead: a mangled character in that one field
            # beats losing every item the response actually carried.
            logger.warning("Plex response from %s was not valid UTF-8 - decoding leniently", url)
            return json.loads(res.content.decode("utf-8", errors="replace"))

def get_guids(item: Dict) -> List[Dict]:
    """Return a normalised Guid list for a Plex item.

    Modern Plex returns a 'Guid' array: [{"id": "tmdb://123"}, ...].
    Legacy items may have an empty/missing 'Guid' but a single lowercase 'guid'
    string like 'com.plexapp.agents.thetvdb://73762/1/1'.
    HAMA items have a guid string like 'com.plexapp.agents.hama://tvdb-73762/1/1'.
    """
    guids = item.get("Guid") or []
    if not guids:
        legacy = item.get("guid", "")
        if legacy:
            guids = [{"id": legacy}]
    return guids


# Plex exposes a source id in a Guid two ways:
#   * the plain scheme - "tmdb://123", or the legacy agent form
#     "com.plexapp.agents.themoviedb://123/1/1"
#   * the HAMA / Absolute Series Scanner agent, which packs the real source
#     behind its own scheme: "com.plexapp.agents.hama://tvdb-73762/1/1"
#     (and "tvdb2-", "tvdb3-" ... for TheTVDB's alternate episode orders).
# Anchoring each alternative on the "://" boundary keeps a stray "tmdb-123"
# elsewhere in the string from being read as an id, and \d+ stops at the
# season/episode path and any "?lang=" suffix on its own.
_TMDB_GUID_RE = re.compile(r"(?:tmdb|themoviedb)://(\d+)|://tmdb-(\d+)")
_TVDB_GUID_RE = re.compile(r"tvdb://(\d+)|://tvdb[2-9]?-(\d+)")
_IMDB_GUID_RE = re.compile(r"imdb://(tt\d+)|://imdb-(tt\d+)")


def _first_guid_match(guids: List[Dict], pattern: "re.Pattern[str]") -> Optional[str]:
    for guid in guids or []:
        match = pattern.search(guid.get("id", "") or "")
        if match:
            return next((g for g in match.groups() if g), None)
    return None


def extract_tmdb_id(guids: List[Dict]) -> Optional[int]:
    raw = _first_guid_match(guids, _TMDB_GUID_RE)
    return int(raw) if raw is not None else None


def extract_tvdb_id(guids: List[Dict]) -> Optional[str]:
    return _first_guid_match(guids, _TVDB_GUID_RE)


def extract_imdb_id(guids: List[Dict]) -> Optional[str]:
    return _first_guid_match(guids, _IMDB_GUID_RE)

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

async def _get_library_items_paginated(url: str, token: str, section_id: str, params: Dict) -> List[Dict]:
    """Fetch every item in a library section, paging with X-Plex-Container-
    Start/-Size the same way get_watchlist/get_plex_history already do.

    A single unpaginated request for a whole section used to time out on a
    large TV library (tens of thousands of episodes in one response, #441) -
    movies libraries are usually small enough that this went unnoticed. A
    fixed page size keeps each request's latency predictable regardless of
    how large the library grows.
    """
    page_size = 1000
    items: List[Dict] = []
    start = 0
    while True:
        data = await _get(
            f"{url.rstrip('/')}/library/sections/{section_id}/all",
            token,
            params={**params, "X-Plex-Container-Start": start, "X-Plex-Container-Size": page_size},
        )
        container = data.get("MediaContainer", {})
        batch = container.get("Metadata", [])
        items.extend(batch)
        total = container.get("totalSize", 0) or len(items)
        start += len(batch)
        if not batch or start >= total:
            break
    return items


async def get_movies(url: str, token: str, section_id: str) -> List[Dict]:
    return await _get_library_items_paginated(url, token, section_id, {"includeGuids": 1})

async def get_shows(url: str, token: str, section_id: str) -> List[Dict]:
    return await _get_library_items_paginated(url, token, section_id, {"includeGuids": 1})

async def get_seasons(url: str, token: str, section_id: str) -> List[Dict]:
    """Fetch season metadata, including user ratings, from a TV library."""
    return await _get_library_items_paginated(url, token, section_id, {"type": 3, "includeGuids": 1})


async def get_episodes(url: str, token: str, section_id: str) -> List[Dict]:
    return await _get_library_items_paginated(url, token, section_id, {"type": 4, "includeGuids": 1})

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


async def get_history(url: str, token: str, since: Optional[datetime] = None) -> List[Dict]:
    """Fetch per-play viewing history from this server (one row per actual play,
    unlike get_movies/get_shows/get_episodes which only expose the aggregate
    viewCount/lastViewedAt fields).

    since: if given, only plays at or after this time are returned (Plex's
    viewedAt> filter). Paginated the same way as get_watchlist.
    Returns [] rather than raising on failure (old server, missing endpoint,
    insufficient token permission) so a sync can just skip the backfill.
    """
    items: List[Dict] = []
    start = 0
    params: Dict = {"sort": "viewedAt:asc"}
    if since is not None:
        # The cursor is stored as naive UTC; .timestamp() on a naive datetime
        # interprets it as *local* time, shifting the window by the host's UTC
        # offset. West of UTC that starts the fetch hours too late, and since
        # the cursor still advances, those plays are then skipped forever (#126).
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        params["viewedAt>"] = int(since.timestamp())
    try:
        while True:
            data = await _get(
                f"{url.rstrip('/')}/status/sessions/history/all",
                token,
                params={**params, "X-Plex-Container-Start": start},
            )
            container = data.get("MediaContainer", {})
            batch = container.get("Metadata", [])
            items.extend(batch)
            total = container.get("totalSize", 0) or len(items)
            start += len(batch)
            if not batch or start >= total:
                break
    except Exception:
        # Whatever pages already succeeded are still worth keeping - losing
        # every prior page over one later failure (#388) meant a single bad
        # page silently discarded an entire account's watch history instead
        # of importing everything up to that point. The next sync's full
        # re-scan (see _backfill_plex_watch_history) still dedupes safely
        # against whatever this run did manage to save.
        logger.warning("Could not fetch all Plex play history from %s (got %d item(s) before failing)", url, len(items))
    return items


async def get_account_id(url: str, token: str, username: str) -> Optional[int]:
    """Resolve a server-local Plex username (MediaServerConnection.server_username)
    to its numeric account id, for scoping history to one user on a shared server."""
    try:
        data = await _get(f"{url.rstrip('/')}/accounts", token)
        accounts = data.get("MediaContainer", {}).get("Account", [])
        for account in accounts:
            name = account.get("name") or account.get("title") or ""
            if name.strip().lower() == username.strip().lower():
                account_id = account.get("id")
                return int(account_id) if account_id is not None else None
    except Exception:
        logger.warning("Could not resolve Plex account id for username=%s", username)
    return None


METADATA_BASE = "https://metadata.provider.plex.tv"
DISCOVER_BASE = "https://discover.provider.plex.tv"
PLEX_TV_BASE  = "https://plex.tv"
APP_AUTH_BASE = "https://app.plex.tv/auth"
COMMUNITY_BASE = "https://community.plex.tv"


# ── plex.tv account auth (PIN / "Login with Plex") ─────────────────────────────

def _plextv_headers(client_id: str, token: Optional[str] = None) -> Dict:
    headers = {
        "Accept": "application/json",
        "X-Plex-Product": "Scrob",
        "X-Plex-Version": "1.0",
        "X-Plex-Client-Identifier": client_id,
        "X-Plex-Device": "Scrob",
        "X-Plex-Device-Name": "Scrob",
        "X-Plex-Platform": "Web",
    }
    if token:
        headers["X-Plex-Token"] = token
    return headers


async def create_auth_pin(client_id: str) -> Dict:
    """Create a plex.tv auth PIN. Returns {id, code}.

    The user then visits build_auth_url(client_id, code) and signs in; once they
    do, poll_auth_pin() returns the account auth token.
    """
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0), follow_redirects=True) as client:
        res = await client.post(
            f"{PLEX_TV_BASE}/api/v2/pins",
            headers=_plextv_headers(client_id),
            params={"strong": "true"},
        )
        res.raise_for_status()
        data = res.json()
        return {"id": data["id"], "code": data["code"]}


async def poll_auth_pin(client_id: str, pin_id: int) -> Optional[str]:
    """Return the account auth token once the PIN has been claimed, else None."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0), follow_redirects=True) as client:
        res = await client.get(
            f"{PLEX_TV_BASE}/api/v2/pins/{pin_id}",
            headers=_plextv_headers(client_id),
        )
        res.raise_for_status()
        return res.json().get("authToken") or None


def build_auth_url(client_id: str, code: str) -> str:
    """The app.plex.tv URL the user opens to authorize the PIN."""
    from urllib.parse import urlencode

    params = {
        "clientID": client_id,
        "code": code,
        "context[device][product]": "Scrob",
        "context[device][device]": "Scrob",
        "context[device][platform]": "Web",
    }
    return f"{APP_AUTH_BASE}#?{urlencode(params)}"


async def get_account(client_id: str, token: str) -> Optional[Dict]:
    """Fetch the signed-in plex.tv account. Returns {id, username, title, email}."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0), follow_redirects=True) as client:
            res = await client.get(
                f"{PLEX_TV_BASE}/api/v2/user",
                headers=_plextv_headers(client_id, token),
            )
            if res.status_code >= 400:
                return None
            d = res.json()
        return {
            "id": str(d.get("id") or ""),
            "username": d.get("username") or d.get("title") or "",
            "title": d.get("title") or "",
            "email": d.get("email") or "",
        }
    except Exception:
        return None


async def get_servers(client_id: str, token: str) -> List[Dict]:
    """List the Plex Media Servers this account can reach.

    Each entry: {name, machine_identifier, owned, access_token, connections:
    [{uri, local, relay, protocol}]}. The per-server access_token is scoped to
    that server and is what belongs in MediaServerConnection.token.
    """
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0), follow_redirects=True) as client:
        res = await client.get(
            f"{PLEX_TV_BASE}/api/v2/resources",
            headers=_plextv_headers(client_id, token),
            params={"includeHttps": 1, "includeRelay": 1},
        )
        res.raise_for_status()
        resources = res.json()

    servers: List[Dict] = []
    for r in resources:
        provides = (r.get("provides") or "").split(",")
        if "server" not in provides:
            continue
        conns = [
            {
                "uri": c.get("uri"),
                "local": bool(c.get("local")),
                "relay": bool(c.get("relay")),
                "protocol": c.get("protocol"),
            }
            for c in (r.get("connections") or [])
            if c.get("uri")
        ]
        servers.append({
            "name": r.get("name") or "Plex Server",
            "machine_identifier": r.get("clientIdentifier") or "",
            "owned": bool(r.get("owned")),
            "access_token": r.get("accessToken") or token,
            "connections": conns,
        })
    return servers


def _connection_rank(conn: Dict) -> int:
    if conn.get("relay"):
        return 2
    if conn.get("local"):
        return 0
    return 1


def connection_label(conn: Dict) -> str:
    """Human label for a Plex connection URI, e.g. 'Local · https://10-0-0-2.plex.direct:32400'."""
    if conn.get("relay"):
        kind = "Relay"
    elif conn.get("local"):
        kind = "Local"
    else:
        kind = "Remote"
    return f"{kind} · {conn.get('uri', '')}"


async def _probe_connection(client: httpx.AsyncClient, uri: str, token: str, want_mid: Optional[str]) -> bool:
    try:
        r = await client.get(
            f"{uri.rstrip('/')}/identity",
            headers={"X-Plex-Token": token, "Accept": "application/json"},
        )
        if r.status_code != 200:
            return False
        got = r.json().get("MediaContainer", {}).get("machineIdentifier")
        return not want_mid or got == want_mid
    except Exception:
        return False


async def resolve_connections(server: Dict) -> Dict:
    """Probe every advertised connection for a server and return them all, ordered
    local → remote → relay, each tagged with a `reachable` flag and a display
    `label`. `recommended` is the first reachable URI, or the best-ranked one if
    none respond (the user can still pick another or edit the field).

    URIs that resolve to a blocked (cloud-metadata) range are dropped entirely -
    a Plex server can advertise arbitrary custom-access URLs and the backend
    fetches these, so they go through the same SSRF filter as manual entry."""
    from core.url_validator import is_safe_service_url

    candidates = sorted(server.get("connections") or [], key=_connection_rank)
    ordered = [
        c for c in candidates
        if c.get("uri") and await is_safe_service_url(c["uri"])
    ]
    if not ordered:
        return {"recommended": None, "connections": []}

    want_mid = server.get("machine_identifier")
    token = server.get("access_token")
    async with httpx.AsyncClient(timeout=httpx.Timeout(4.0), follow_redirects=False) as client:
        flags = await asyncio.gather(
            *(_probe_connection(client, c["uri"], token, want_mid) for c in ordered)
        )

    conns = [
        {
            "uri": c["uri"],
            "local": c["local"],
            "relay": c["relay"],
            "protocol": c.get("protocol"),
            "reachable": bool(ok),
            "label": connection_label(c),
        }
        for c, ok in zip(ordered, flags)
    ]
    recommended = next((c["uri"] for c in conns if c["reachable"]), conns[0]["uri"])
    return {"recommended": recommended, "connections": conns}


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


_RESOLVE_CANDIDATE_CHECK_LIMIT = 10


async def resolve_tmdb_ratingkey(token: str, tmdb_id: int, media_type: str, title: str) -> str | None:
    """Return the Plex Discover ratingKey for an item identified by TMDB ID.

    Discover has no endpoint that filters its global catalog by external
    guid directly - `/library/sections/computer/all?guid=...` (the original
    approach here) isn't a real Discover section and always came back empty.
    The approach actually used by Plex's own clients (and python-plexapi's
    MyPlexAccount.searchDiscover) is a title search against `/library/search`.

    That search's results do NOT include each candidate's external Guid list
    despite `includeMetadata=1` (confirmed live against issue #119/#83 - every
    candidate came back with Guid=None even for an exact title match), so
    title text alone can't be trusted to pick the right candidate on its own
    (remakes, unrelated titles that share a name). Each candidate's ratingKey
    is verified against enrich_plex_item's full metadata (which does carry
    Guid) until the exact tmdb match is found, checking only the closest
    _RESOLVE_CANDIDATE_CHECK_LIMIT results since Discover search is already
    relevance-ranked and the right item is normally at or near the top.

    media_type must be 'movie' or 'show'.
    Returns None if the item cannot be found.
    """
    libtype = "movies" if media_type == "movie" else "tv"
    want_guid = f"tmdb://{tmdb_id}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            res = await client.get(
                f"{DISCOVER_BASE}/library/search",
                headers={"X-Plex-Token": token, "Accept": "application/json"},
                params={
                    "X-Plex-Token": token,
                    "query": title,
                    "limit": 30,
                    "searchTypes": libtype,
                    "searchProviders": "discover",
                    "includeMetadata": 1,
                },
            )
            if res.status_code >= 400:
                logger.warning(
                    "Discover search failed for tmdb_id=%s title=%r: HTTP %s - %s",
                    tmdb_id, title, res.status_code, res.text[:500],
                )
                return None
            data = res.json()
        search_results = data.get("MediaContainer", {}).get("SearchResults", [])
        external = next(
            (s.get("SearchResult", []) for s in search_results if s.get("id") == "external"),
            [],
        )
        candidates = [
            rk for r in external
            if (rk := r.get("Metadata", {}).get("ratingKey"))
        ][:_RESOLVE_CANDIDATE_CHECK_LIMIT]

        checked = []
        for rating_key in candidates:
            full = await enrich_plex_item(token, rating_key)
            guids = (full or {}).get("Guid", []) or []
            checked.append({"ratingKey": rating_key, "guids": guids})
            if any(g.get("id") == want_guid for g in guids):
                return rating_key

        logger.warning(
            "No Discover match for tmdb_id=%s title=%r (want %s): %s section(s), "
            "%s external candidate(s), checked=%s",
            tmdb_id, title, want_guid, len(search_results), len(external), checked,
        )
        return None
    except Exception:
        logger.exception("Discover search errored for tmdb_id=%s title=%r", tmdb_id, title)
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
    params = {"key": rating_key, "identifier": "com.plexapp.plugins.library", "rating": rating}
    try:
        if client:
            r = await client.put(f"{url.rstrip('/')}/:/rate", headers=headers, params=params)
            return r.status_code < 400
        async with httpx.AsyncClient(timeout=PUSH_TIMEOUT, follow_redirects=False) as c:
            r = await c.put(f"{url.rstrip('/')}/:/rate", headers=headers, params=params)
            return r.status_code < 400
    except Exception:
        return False

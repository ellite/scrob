import httpx
import logging
import time
from typing import Optional, List, Dict, Any, Set

logger = logging.getLogger(__name__)

# Brief per-server cache; failures are cached shorter so a down server
# doesn't stall every list request.
_LIBRARY_CACHE: Dict[str, tuple] = {}
_LIBRARY_TTL = 300.0
_LIBRARY_FAILURE_TTL = 60.0


async def get_all_movie_tmdb_ids(url: str, token: str) -> Optional[Set[int]]:
    """TMDB ids of every movie in Radarr, cached per server; None on failure."""
    key = f"{url.rstrip('/')}|{token}"
    cached = _LIBRARY_CACHE.get(key)
    if cached:
        ts, ids = cached
        ttl = _LIBRARY_TTL if ids is not None else _LIBRARY_FAILURE_TTL
        if time.monotonic() - ts < ttl:
            return ids
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.get(
                f"{url.rstrip('/')}/api/v3/movie",
                headers={"X-Api-Key": token}
            )
            response.raise_for_status()
            ids = {m["tmdbId"] for m in response.json() if m.get("tmdbId")}
        _LIBRARY_CACHE[key] = (time.monotonic(), ids)
        return ids
    except Exception as e:
        logger.error(f"Failed to fetch Radarr movie list: {e}")
        _LIBRARY_CACHE[key] = (time.monotonic(), None)
        return None

async def validate_connection(url: str, token: str) -> bool:
    """Check if we can connect to Radarr and if the API key is valid."""
    try:
        url = url.rstrip("/")
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.get(
                f"{url}/api/v3/system/status",
                headers={"X-Api-Key": token}
            )
            return response.status_code == 200
    except Exception as e:
        logger.error(f"Radarr connection validation failed: {e}")
        return False

async def get_root_folders(url: str, token: str) -> List[Dict[str, Any]]:
    """Fetch root folders from Radarr."""
    try:
        url = url.rstrip("/")
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.get(
                f"{url}/api/v3/rootfolder",
                headers={"X-Api-Key": token}
            )
            response.raise_for_status()
            return response.json()
    except Exception as e:
        logger.error(f"Failed to fetch Radarr root folders: {e}")
        return []

async def get_quality_profiles(url: str, token: str) -> List[Dict[str, Any]]:
    """Fetch quality profiles from Radarr."""
    try:
        url = url.rstrip("/")
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.get(
                f"{url}/api/v3/qualityprofile",
                headers={"X-Api-Key": token}
            )
            response.raise_for_status()
            return response.json()
    except Exception as e:
        logger.error(f"Failed to fetch Radarr quality profiles: {e}")
        return []

async def get_tags(url: str, token: str) -> List[Dict[str, Any]]:
    """Fetch tags from Radarr."""
    try:
        url = url.rstrip("/")
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.get(
                f"{url}/api/v3/tag",
                headers={"X-Api-Key": token}
            )
            response.raise_for_status()
            return response.json()
    except Exception as e:
        logger.error(f"Failed to fetch Radarr tags: {e}")
        return []

async def add_movie(
    url: str,
    token: str,
    tmdb_id: int,
    title: str,
    root_folder: str,
    quality_profile_id: int,
    tags: Optional[List[int]] = None,
    monitored: bool = True,
    search_for_movie: bool = True
) -> Dict[str, Any]:
    """Add a movie to Radarr."""
    try:
        url = url.rstrip("/")
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            # First, check if movie already exists or get more details from Radarr's lookup
            lookup_res = await client.get(
                f"{url}/api/v3/movie/lookup",
                headers={"X-Api-Key": token},
                params={"term": f"tmdb:{tmdb_id}"},
            )
            lookup_res.raise_for_status()
            lookup_data = lookup_res.json()
            
            if not lookup_data:
                raise Exception(f"Movie with TMDB ID {tmdb_id} not found on Radarr lookup")
            
            movie_data = lookup_data[0]
            
            # If movie has an 'id', it's already in Radarr
            if movie_data.get("id"):
                return {"status": "already_exists", "movie": movie_data}

            # Prepare payload
            payload = {
                **movie_data,
                "rootFolderPath": root_folder,
                "qualityProfileId": quality_profile_id,
                "tags": tags or [],
                "monitored": monitored,
                "addOptions": {
                    "searchForMovie": search_for_movie
                }
            }

            response = await client.post(
                f"{url}/api/v3/movie",
                headers={"X-Api-Key": token},
                json=payload
            )
            response.raise_for_status()
            return {"status": "added", "movie": response.json()}
            
    except Exception as e:
        logger.error(f"Failed to add movie to Radarr: {e}")
        raise Exception(str(e))

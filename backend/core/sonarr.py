import httpx
import logging
import time
from typing import Optional, List, Dict, Any, Set, Tuple

logger = logging.getLogger(__name__)

# Same brief library cache as core/radarr.py.
_LIBRARY_CACHE: Dict[str, tuple] = {}
_LIBRARY_TTL = 300.0
_LIBRARY_FAILURE_TTL = 60.0


async def get_all_series_ids(url: str, token: str) -> Optional[Tuple[Set[int], Set[int]]]:
    """(tmdb ids, tvdb ids) of every series in Sonarr, cached per server;
    None on failure. tmdbId only exists on Sonarr v4+."""
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
                f"{url.rstrip('/')}/api/v3/series",
                headers={"X-Api-Key": token}
            )
            response.raise_for_status()
            series = response.json()
            ids = (
                {s["tmdbId"] for s in series if s.get("tmdbId")},
                {s["tvdbId"] for s in series if s.get("tvdbId")},
            )
        _LIBRARY_CACHE[key] = (time.monotonic(), ids)
        return ids
    except Exception as e:
        logger.error(f"Failed to fetch Sonarr series list: {e}")
        _LIBRARY_CACHE[key] = (time.monotonic(), None)
        return None

async def validate_connection(url: str, token: str) -> bool:
    """Check if we can connect to Sonarr and if the API key is valid."""
    try:
        url = url.rstrip("/")
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.get(
                f"{url}/api/v3/system/status",
                headers={"X-Api-Key": token}
            )
            return response.status_code == 200
    except Exception as e:
        logger.error(f"Sonarr connection validation failed: {e}")
        return False

async def get_root_folders(url: str, token: str) -> List[Dict[str, Any]]:
    """Fetch root folders from Sonarr."""
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
        logger.error(f"Failed to fetch Sonarr root folders: {e}")
        return []

async def get_quality_profiles(url: str, token: str) -> List[Dict[str, Any]]:
    """Fetch quality profiles from Sonarr."""
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
        logger.error(f"Failed to fetch Sonarr quality profiles: {e}")
        return []

async def get_tags(url: str, token: str) -> List[Dict[str, Any]]:
    """Fetch tags from Sonarr."""
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
        logger.error(f"Failed to fetch Sonarr tags: {e}")
        return []

async def add_series(
    url: str,
    token: str,
    tvdb_id: int,
    root_folder: str,
    quality_profile_id: int,
    tags: Optional[List[int]] = None,
    monitored: bool = True,
    search_for_missing_episodes: bool = True,
    season_folder: bool = True,
) -> Dict[str, Any]:
    """Add a series to Sonarr."""
    try:
        url = url.rstrip("/")
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            # First, lookup series on Sonarr
            lookup_res = await client.get(
                f"{url}/api/v3/series/lookup",
                headers={"X-Api-Key": token},
                params={"term": f"tvdb:{tvdb_id}"},
            )
            lookup_res.raise_for_status()
            lookup_data = lookup_res.json()
            
            if not lookup_data:
                raise Exception(f"Series with TVDB ID {tvdb_id} not found on Sonarr lookup")
            
            series_data = lookup_data[0]
            
            # If series has an 'id', it's already in Sonarr
            if series_data.get("id"):
                return {"status": "already_exists", "series": series_data}

            # Prepare payload
            payload = {
                **series_data,
                "rootFolderPath": root_folder,
                "qualityProfileId": quality_profile_id,
                "seasonFolder": season_folder,
                "tags": tags or [],
                "monitored": monitored,
                "addOptions": {
                    "searchForMissingEpisodes": search_for_missing_episodes
                }
            }

            response = await client.post(
                f"{url}/api/v3/series",
                headers={"X-Api-Key": token},
                json=payload
            )
            response.raise_for_status()
            return {"status": "added", "series": response.json()}
            
    except Exception as e:
        logger.error(f"Failed to add series to Sonarr: {e}")
        raise Exception(str(e))

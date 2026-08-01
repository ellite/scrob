import httpx
import logging
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

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

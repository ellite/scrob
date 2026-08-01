import httpx
import logging
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

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

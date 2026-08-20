import httpx
import logging
import time
from typing import Optional, List, Dict, Any, Set, Tuple

logger = logging.getLogger(__name__)

# Same brief library cache as core/radarr.py.
_LIBRARY_CACHE: Dict[str, tuple] = {}
_LIBRARY_TTL = 300.0
_LIBRARY_FAILURE_TTL = 60.0


class InvalidSeasonSelectionError(ValueError):
    """Raised when a requested season selection no longer matches Sonarr."""


async def lookup_series(url: str, token: str, tvdb_id: int) -> Dict[str, Any]:
    """Fetch the current Sonarr lookup record without exposing its token."""
    url = url.rstrip("/")
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
        return await _lookup_series(client, url, token, tvdb_id)


async def _lookup_series(
    client: httpx.AsyncClient, url: str, token: str, tvdb_id: int
) -> Dict[str, Any]:
    lookup_res = await client.get(
        f"{url}/api/v3/series/lookup",
        headers={"X-Api-Key": token},
        params={"term": f"tvdb:{tvdb_id}"},
    )
    lookup_res.raise_for_status()
    lookup_data = lookup_res.json()
    if not lookup_data:
        raise Exception(f"Series with TVDB ID {tvdb_id} not found on Sonarr lookup")
    return lookup_data[0]


def _apply_season_monitoring(
    series_data: Dict[str, Any], selected_seasons: Optional[List[int]]
) -> List[Dict[str, Any]]:
    """Build a deterministic Sonarr season-monitoring payload.

    All mode intentionally means all *regular* seasons (number > 0); Sonarr's
    own ``MonitorTypes.All`` has the same Specials-excluding meaning.  A
    concrete selection may include season 0 and is checked against the fresh
    lookup response to make stale browser state safe.
    """
    if selected_seasons is not None and any(
        type(number) is not int or number < 0 for number in selected_seasons
    ):
        raise InvalidSeasonSelectionError("Selected seasons must be valid non-negative numbers")

    available_seasons = series_data.get("seasons")
    if not isinstance(available_seasons, list):
        available_seasons = []

    if selected_seasons is None:
        return [
            {
                **season,
                "monitored": season.get("seasonNumber", -1) > 0,
            }
            for season in available_seasons
            if isinstance(season, dict)
        ]

    available_numbers = {
        season.get("seasonNumber")
        for season in available_seasons
        if isinstance(season, dict) and isinstance(season.get("seasonNumber"), int)
    }
    requested_numbers = set(selected_seasons)
    if not requested_numbers:
        raise InvalidSeasonSelectionError("Select at least one season or choose all seasons")

    invalid_numbers = sorted(requested_numbers - available_numbers)
    if invalid_numbers:
        values = ", ".join(str(number) for number in invalid_numbers)
        raise InvalidSeasonSelectionError(f"The selected season(s) are no longer available: {values}")

    return [
        {
            **season,
            "monitored": season.get("seasonNumber") in requested_numbers,
        }
        for season in available_seasons
    ]


async def _queue_selected_season_searches(
    client: httpx.AsyncClient,
    url: str,
    token: str,
    series_id: int | None,
    selected_seasons: List[int],
) -> list[int]:
    """Queue one explicit Sonarr SeasonSearch command per selected season.

    A broad MissingEpisodeSearch has historically ignored per-season flags;
    direct SeasonSearch commands are deliberately restricted to the requested
    season, including season 0 (Specials).
    """
    if not series_id:
        return list(selected_seasons)

    failed_seasons: list[int] = []
    for season_number in selected_seasons:
        try:
            response = await client.post(
                f"{url}/api/v3/command",
                headers={"X-Api-Key": token},
                json={
                    "name": "SeasonSearch",
                    "seriesId": series_id,
                    "seasonNumber": season_number,
                },
            )
            response.raise_for_status()
        except Exception as exc:
            logger.error(
                "Series %s was added but Sonarr SeasonSearch for season %s could not be queued: %s",
                series_id,
                season_number,
                exc,
            )
            failed_seasons.append(season_number)
    return failed_seasons


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
    selected_seasons: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Add a series to Sonarr, optionally monitoring only selected seasons."""
    if selected_seasons is not None and any(
        type(number) is not int or number < 0 for number in selected_seasons
    ):
        raise InvalidSeasonSelectionError("Selected seasons must be valid non-negative numbers")
    try:
        url = url.rstrip("/")
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            series_data = await _lookup_series(client, url, token, tvdb_id)
            
            # If series has an 'id', it's already in Sonarr
            if series_data.get("id"):
                return {"status": "already_exists", "series": series_data}

            # A selected list is validated against this fresh lookup, rather
            # than trusting the seasons displayed in an earlier browser modal.
            # ``skip`` makes Sonarr preserve these manual season flags; we
            # then issue a SeasonSearch for each selected season below.
            seasons = _apply_season_monitoring(series_data, selected_seasons)
            add_options = {
                "monitor": "skip" if selected_seasons is not None else "all",
                "searchForMissingEpisodes": (
                    search_for_missing_episodes if selected_seasons is None else False
                ),
                "searchForCutoffUnmetEpisodes": False,
            }

            # Prepare payload
            payload = {
                **series_data,
                "rootFolderPath": root_folder,
                "qualityProfileId": quality_profile_id,
                "seasonFolder": season_folder,
                "tags": tags or [],
                "monitored": monitored,
                "addOptions": add_options,
                "seasons": seasons,
            }

            response = await client.post(
                f"{url}/api/v3/series",
                headers={"X-Api-Key": token},
                json=payload
            )
            response.raise_for_status()
            added_series = response.json()

            if selected_seasons is not None:
                failed_seasons = await _queue_selected_season_searches(
                    client, url, token, added_series.get("id"), selected_seasons
                )
                if failed_seasons:
                    return {
                        "status": "added_search_failed",
                        "series": added_series,
                        "search_failed_seasons": failed_seasons,
                    }

            return {"status": "added", "series": added_series}
            
    except InvalidSeasonSelectionError:
        raise
    except Exception as e:
        logger.error(f"Failed to add series to Sonarr: {e}")
        raise Exception(str(e))

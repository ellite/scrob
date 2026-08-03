"""Simkl API client.

Uses the PIN (device) authentication flow — only a client_id is needed,
no client_secret. Access tokens are long-lived and do not expire or refresh.

API base: https://api.simkl.com
Rate limits: 1000 requests per 10 minutes per user.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from core.config import settings as app_settings

logger = logging.getLogger(__name__)

SIMKL_BASE = "https://api.simkl.com"
TIMEOUT = 30.0
USER_AGENT = f"scrob/{app_settings.app_version}"


def _scrobble_params(client_id: str) -> dict:
    """Query params every /scrobble/* endpoint requires in addition to auth —
    unlike the rest of the API, these are mandatory here (missing them causes
    a 404 at Simkl's gateway, not a 400 from the app)."""
    return {
        "client_id": client_id,
        "app-name": "scrob",
        "app-version": app_settings.app_version,
    }


def _iso_utc(value: datetime) -> str:
    """Format a datetime as an ISO-8601 UTC string with a Z suffix.

    WatchEvent.watched_at is stored naive but always represents UTC, so a
    naive value is treated as already-UTC rather than the local timezone.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _headers(client_id: str, access_token: Optional[str] = None) -> dict:
    h = {
        "Content-Type": "application/json",
        "simkl-api-key": client_id,
        "User-Agent": USER_AGENT,
    }
    if access_token:
        h["Authorization"] = f"Bearer {access_token}"
    return h


# ── PIN Authentication ────────────────────────────────────────────────────────

async def start_pin_auth(client_id: str) -> dict:
    """Start the PIN authentication flow.

    Returns: {result, device_code, user_code, url, interval, expires_in}
    The user visits `url` and enters `user_code` to authorise.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(
            f"{SIMKL_BASE}/oauth/pin",
            params={"client_id": client_id, "redirect": ""},
        )
        resp.raise_for_status()
        return resp.json()


async def poll_pin_token(client_id: str, user_code: str) -> Optional[str]:
    """Poll for PIN completion.

    Returns the access_token string on success, None while still waiting.
    Raises httpx.HTTPStatusError on permanent failure (expired / denied).
    """
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(
            f"{SIMKL_BASE}/oauth/pin/{user_code}",
            params={"client_id": client_id},
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("result") == "OK":
                return data["access_token"]
            # WAITING — keep polling
            return None
        resp.raise_for_status()
        return None


async def validate_token(client_id: str, access_token: str) -> bool:
    """Return True if the token is valid."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(
                f"{SIMKL_BASE}/users/settings",
                headers=_headers(client_id, access_token),
            )
            return resp.status_code == 200
    except Exception:
        return False


# ── User Data Fetching ────────────────────────────────────────────────────────

async def get_all_items(client_id: str, access_token: str) -> dict:
    """Fetch all watched/plan-to-watch items.

    Returns: {movies: [...], shows: [...], anime: [...]}

    Movie entries include: status, last_watched_at, user_rating, movie.ids.tmdb
    Show entries include: status, last_watched_at, user_rating, show.ids.tmdb,
    and optionally seasons[].episodes[] with watched_at per episode.
    """
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.get(
            f"{SIMKL_BASE}/sync/all-items/",
            params={"extended": "full", "episode_watched_at": "yes"},
            headers=_headers(client_id, access_token),
        )
        resp.raise_for_status()
        return resp.json()


async def get_ratings(client_id: str, access_token: str) -> dict:
    """Fetch all user ratings.

    Returns: {movies: [...], shows: [...]}
    Each entry has: user_rating, user_rated_at, movie/show.ids.tmdb - most
    entries in this response are unrated (this endpoint returns the same
    per-item shape as get_all_items, just with rating fields included), so
    callers must filter on user_rating being present.
    """
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(
            f"{SIMKL_BASE}/sync/ratings/",
            params={"extended": "full"},
            headers=_headers(client_id, access_token),
        )
        resp.raise_for_status()
        data = resp.json()
        # Simkl may return a flat list or a typed dict; normalise to dict
        if isinstance(data, list):
            movies = [e for e in data if e.get("movie")]
            shows  = [e for e in data if e.get("show")]
            return {"movies": movies, "shows": shows}
        return data if isinstance(data, dict) else {}


# ── Outbound Push ─────────────────────────────────────────────────────────────

async def add_movie_to_history(
    client_id: str,
    access_token: str,
    tmdb_id: int,
    watched_at: Optional[datetime] = None,
) -> None:
    """Mark a movie as watched on Simkl. watched_at=None lets Simkl stamp the play as now."""
    movie: dict = {"ids": {"tmdb": tmdb_id}}
    if watched_at:
        movie["watched_at"] = _iso_utc(watched_at)
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{SIMKL_BASE}/sync/history",
            json={"movies": [movie]},
            headers=_headers(client_id, access_token),
        )
        resp.raise_for_status()


async def add_history_batch(
    client_id: str,
    access_token: str,
    movies: list[tuple[int, Optional[datetime]]],
    episodes: list[tuple[int, int, int, Optional[datetime]]],
) -> None:
    """Add multiple movies and/or episodes to Simkl history in a single API call.

    movies: list of (tmdb_id, watched_at)
    episodes: list of (show_tmdb_id, season_number, episode_number, watched_at)
    """
    if not movies and not episodes:
        return
    body: dict = {}
    if movies:
        body["movies"] = []
        for tmdb_id, watched_at in movies:
            m: dict = {"ids": {"tmdb": tmdb_id}}
            if watched_at:
                m["watched_at"] = _iso_utc(watched_at)
            body["movies"].append(m)
    if episodes:
        shows_map: dict[int, dict[int, list[tuple[int, Optional[datetime]]]]] = {}
        for show_tmdb_id, season, ep_num, watched_at in episodes:
            shows_map.setdefault(show_tmdb_id, {}).setdefault(season, []).append((ep_num, watched_at))
        body["shows"] = [
            {
                "ids": {"tmdb": show_tmdb_id},
                "seasons": [
                    {
                        "number": season,
                        "episodes": [
                            ({"number": n, "watched_at": _iso_utc(watched_at)} if watched_at else {"number": n})
                            for n, watched_at in eps
                        ],
                    }
                    for season, eps in seasons.items()
                ],
            }
            for show_tmdb_id, seasons in shows_map.items()
        ]
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{SIMKL_BASE}/sync/history",
            json=body,
            headers=_headers(client_id, access_token),
        )
        resp.raise_for_status()


async def remove_movie_from_history(client_id: str, access_token: str, tmdb_id: int) -> None:
    """Mark a movie as unwatched on Simkl."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{SIMKL_BASE}/sync/history/remove",
            json={"movies": [{"ids": {"tmdb": tmdb_id}}]},
            headers=_headers(client_id, access_token),
        )
        resp.raise_for_status()


async def add_episode_to_history(
    client_id: str,
    access_token: str,
    show_tmdb_id: int,
    season_number: int,
    episode_number: int,
    watched_at: Optional[datetime] = None,
) -> None:
    """Mark an episode as watched on Simkl. watched_at=None lets Simkl stamp the play as now."""
    episode: dict = {"number": episode_number}
    if watched_at:
        episode["watched_at"] = _iso_utc(watched_at)
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{SIMKL_BASE}/sync/history",
            json={
                "shows": [{
                    "ids": {"tmdb": show_tmdb_id},
                    "seasons": [{
                        "number": season_number,
                        "episodes": [episode],
                    }],
                }]
            },
            headers=_headers(client_id, access_token),
        )
        resp.raise_for_status()


async def remove_episode_from_history(
    client_id: str,
    access_token: str,
    show_tmdb_id: int,
    season_number: int,
    episode_number: int,
) -> None:
    """Mark an episode as unwatched on Simkl."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{SIMKL_BASE}/sync/history/remove",
            json={
                "shows": [{
                    "ids": {"tmdb": show_tmdb_id},
                    "seasons": [{
                        "number": season_number,
                        "episodes": [{"number": episode_number}],
                    }],
                }]
            },
            headers=_headers(client_id, access_token),
        )
        resp.raise_for_status()


async def set_movie_rating(client_id: str, access_token: str, tmdb_id: int, rating: float) -> None:
    """Rate a movie on Simkl (1–10 integer scale)."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{SIMKL_BASE}/sync/ratings",
            json={"movies": [{"rating": max(1, min(10, round(rating))), "ids": {"tmdb": tmdb_id}}]},
            headers=_headers(client_id, access_token),
        )
        resp.raise_for_status()


async def remove_movie_rating(client_id: str, access_token: str, tmdb_id: int) -> None:
    """Remove a movie rating on Simkl."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{SIMKL_BASE}/sync/ratings/remove",
            json={"movies": [{"ids": {"tmdb": tmdb_id}}]},
            headers=_headers(client_id, access_token),
        )
        resp.raise_for_status()


async def set_show_rating(client_id: str, access_token: str, tmdb_id: int, rating: float) -> None:
    """Rate a show on Simkl (1–10 integer scale)."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{SIMKL_BASE}/sync/ratings",
            json={"shows": [{"rating": max(1, min(10, round(rating))), "ids": {"tmdb": tmdb_id}}]},
            headers=_headers(client_id, access_token),
        )
        resp.raise_for_status()


async def set_ratings_batch(
    client_id: str,
    access_token: str,
    movie_ratings: list[tuple[int, float]],
    show_ratings: list[tuple[int, float]],
) -> None:
    """Rate multiple movies and/or shows on Simkl in a single API call (1-10 integer scale)."""
    if not movie_ratings and not show_ratings:
        return
    body: dict = {}
    if movie_ratings:
        body["movies"] = [
            {"rating": max(1, min(10, round(rating))), "ids": {"tmdb": tmdb_id}}
            for tmdb_id, rating in movie_ratings
        ]
    if show_ratings:
        body["shows"] = [
            {"rating": max(1, min(10, round(rating))), "ids": {"tmdb": tmdb_id}}
            for tmdb_id, rating in show_ratings
        ]
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{SIMKL_BASE}/sync/ratings",
            json=body,
            headers=_headers(client_id, access_token),
        )
        resp.raise_for_status()


async def remove_show_rating(client_id: str, access_token: str, tmdb_id: int) -> None:
    """Remove a show rating on Simkl."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{SIMKL_BASE}/sync/ratings/remove",
            json={"shows": [{"ids": {"tmdb": tmdb_id}}]},
            headers=_headers(client_id, access_token),
        )
        resp.raise_for_status()


async def checkin_movie(
    client_id: str,
    access_token: str,
    tmdb_id: int,
    title: Optional[str] = None,
    year: Optional[int] = None,
    progress: float = 0.0,
) -> None:
    """Check into a movie on Simkl (now watching) — fire-and-forget; Simkl
    runtime-extrapolates the progress bar itself from here, no further calls
    needed until stop_scrobble_movie. progress must reflect where playback
    actually started (e.g. a resume), or Simkl assumes it started at 0."""
    movie: dict = {"ids": {"tmdb": tmdb_id}}
    if title:
        movie["title"] = title
    if year:
        movie["year"] = year
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{SIMKL_BASE}/scrobble/checkin",
            params=_scrobble_params(client_id),
            json={"movie": movie, "progress": progress},
            headers=_headers(client_id, access_token),
        )
        resp.raise_for_status()


async def checkin_episode(
    client_id: str,
    access_token: str,
    show_tmdb_id: int,
    season_number: int,
    episode_number: int,
    show_title: Optional[str] = None,
    progress: float = 0.0,
) -> None:
    """Check into a TV episode on Simkl (now watching). See checkin_movie for
    why progress must reflect the real starting point, not always 0."""
    show: dict = {"ids": {"tmdb": show_tmdb_id}}
    if show_title:
        show["title"] = show_title
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{SIMKL_BASE}/scrobble/checkin",
            params=_scrobble_params(client_id),
            json={
                "show": show,
                "episode": {"season": season_number, "number": episode_number},
                "progress": progress,
            },
            headers=_headers(client_id, access_token),
        )
        resp.raise_for_status()


async def stop_scrobble_movie(
    client_id: str,
    access_token: str,
    tmdb_id: int,
    progress: float,
) -> None:
    """End a Simkl scrobble session for a movie. Simkl marks it watched at
    progress >= 80, otherwise saves it as a resumable pause — there is no
    separate cancel/delete endpoint; only the progress at the moment of stop
    matters, so this is the sole way to finalize (or walk back) a checkin."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{SIMKL_BASE}/scrobble/stop",
            params=_scrobble_params(client_id),
            json={"movie": {"ids": {"tmdb": tmdb_id}}, "progress": progress},
            headers=_headers(client_id, access_token),
        )
        resp.raise_for_status()


async def stop_scrobble_episode(
    client_id: str,
    access_token: str,
    show_tmdb_id: int,
    season_number: int,
    episode_number: int,
    progress: float,
) -> None:
    """End a Simkl scrobble session for a TV episode. See stop_scrobble_movie."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{SIMKL_BASE}/scrobble/stop",
            params=_scrobble_params(client_id),
            json={
                "show": {"ids": {"tmdb": show_tmdb_id}},
                "episode": {"season": season_number, "number": episode_number},
                "progress": progress,
            },
            headers=_headers(client_id, access_token),
        )
        resp.raise_for_status()

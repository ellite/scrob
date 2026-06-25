"""Simkl API client.

Uses the PIN (device) authentication flow — only a client_id is needed,
no client_secret. Access tokens are long-lived and do not expire or refresh.

API base: https://api.simkl.com
Rate limits: 1000 requests per 10 minutes per user.
"""

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

SIMKL_BASE = "https://api.simkl.com"
TIMEOUT = 30.0


def _headers(client_id: str, access_token: Optional[str] = None) -> dict:
    h = {
        "Content-Type": "application/json",
        "simkl-api-key": client_id,
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
    Each entry has: rating, rated_at, movie/show.ids.tmdb
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
        return data


# ── Outbound Push ─────────────────────────────────────────────────────────────

async def add_movie_to_history(client_id: str, access_token: str, tmdb_id: int) -> None:
    """Mark a movie as watched on Simkl."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{SIMKL_BASE}/sync/history",
            json={"movies": [{"ids": {"tmdb": tmdb_id}}]},
            headers=_headers(client_id, access_token),
        )
        resp.raise_for_status()


async def remove_movie_from_history(client_id: str, access_token: str, tmdb_id: int) -> None:
    """Mark a movie as unwatched on Simkl."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.delete(
            f"{SIMKL_BASE}/sync/history",
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
) -> None:
    """Mark an episode as watched on Simkl."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{SIMKL_BASE}/sync/history",
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


async def remove_episode_from_history(
    client_id: str,
    access_token: str,
    show_tmdb_id: int,
    season_number: int,
    episode_number: int,
) -> None:
    """Mark an episode as unwatched on Simkl."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.delete(
            f"{SIMKL_BASE}/sync/history",
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
            f"{SIMKL_BASE}/ratings",
            json={"movies": [{"rating": max(1, min(10, round(rating))), "ids": {"tmdb": tmdb_id}}]},
            headers=_headers(client_id, access_token),
        )
        resp.raise_for_status()


async def remove_movie_rating(client_id: str, access_token: str, tmdb_id: int) -> None:
    """Remove a movie rating on Simkl."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.delete(
            f"{SIMKL_BASE}/ratings",
            json={"movies": [{"ids": {"tmdb": tmdb_id}}]},
            headers=_headers(client_id, access_token),
        )
        resp.raise_for_status()


async def set_show_rating(client_id: str, access_token: str, tmdb_id: int, rating: float) -> None:
    """Rate a show on Simkl (1–10 integer scale)."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{SIMKL_BASE}/ratings",
            json={"shows": [{"rating": max(1, min(10, round(rating))), "ids": {"tmdb": tmdb_id}}]},
            headers=_headers(client_id, access_token),
        )
        resp.raise_for_status()


async def remove_show_rating(client_id: str, access_token: str, tmdb_id: int) -> None:
    """Remove a show rating on Simkl."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.delete(
            f"{SIMKL_BASE}/ratings",
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
) -> None:
    """Check into a movie on Simkl (now watching)."""
    movie: dict = {"ids": {"tmdb": tmdb_id}}
    if title:
        movie["title"] = title
    if year:
        movie["year"] = year
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{SIMKL_BASE}/checkin",
            json={"movie": movie},
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
) -> None:
    """Check into a TV episode on Simkl (now watching)."""
    show: dict = {"ids": {"tmdb": show_tmdb_id}}
    if show_title:
        show["title"] = show_title
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{SIMKL_BASE}/checkin",
            json={
                "show": show,
                "episode": {"season": season_number, "episode": episode_number},
            },
            headers=_headers(client_id, access_token),
        )
        resp.raise_for_status()


async def delete_checkin(client_id: str, access_token: str) -> None:
    """Delete the current Simkl checkin (stopped watching).
    Simkl returns 400 when there is no active checkin; treat that as a no-op."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.delete(
            f"{SIMKL_BASE}/checkin",
            headers=_headers(client_id, access_token),
        )
        if resp.status_code not in (400, 404):
            resp.raise_for_status()

"""Simkl API client.

Two authentication generations are supported side by side, picked by what the
user's Client ID was registered as:

- AUTH V2 (RFC 8628 device flow): 7-day access token + 180-day refresh token.
  Simkl is retiring V1 around April 2027, and new apps can only be V2.
- AUTH V1 (PIN flow): 5-year access token, never refreshed. Connections made
  this way keep working untouched until Simkl switches V1 off.

Either way only a client_id is needed, no client_secret. New connections use
Scrob's own V2 app (SCROB_CLIENT_ID); the V1 PIN helpers remain only for
finishing a flow started before the upgrade.

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
# Scrob's own Simkl AUTH V2 app ("TV, devices & command line": device flow, no
# secret). A V2 client ID is public by design - every user-data call also needs
# the user's token - so it ships in source, like WETRAKR_CLIENT_ID.
SCROB_CLIENT_ID = "4cf4eb7f537faafed03084ed995705a4f46e8494116a1fb81cce3949745716d8"
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


class SimklHistoryRejected(Exception):
    """Simkl accepted a /sync/history request (HTTP 2xx) but reported the
    item(s) in `not_found` and wrote nothing.

    Happens when the show's TMDB id resolves to a Simkl entry whose season /
    episode layout doesn't contain the number sent - absolute-ordered anime
    past the first cour, or any other TMDB/TVDB season-split divergence. Simkl
    reports this loss *inside* a 201, so raise_for_status() alone treats it as
    success (#328)."""


def _safe_json(resp: httpx.Response) -> object:
    try:
        return resp.json()
    except ValueError:
        return None


def _history_not_found(payload: object) -> list:
    """Flatten a /sync/history response's `not_found` into a list of the items
    Simkl could not resolve. Tolerates both the documented dict-of-lists shape
    ({"episodes": [...], "shows": [...], "movies": [...]}) and a bare list."""
    if isinstance(payload, dict):
        nf = payload.get("not_found")
    else:
        nf = None
    if isinstance(nf, list):
        return nf
    if isinstance(nf, dict):
        out: list = []
        for value in nf.values():
            if isinstance(value, list):
                out.extend(value)
        return out
    return []


def _count_history_items(items: list) -> int:
    """Number of individual movies/episodes in a list of `not_found` entries -
    a show entry stands for every episode nested under its seasons."""
    total = 0
    for item in items:
        seasons = item.get("seasons") if isinstance(item, dict) else None
        if seasons:
            total += sum(len(season.get("episodes") or []) or 1 for season in seasons)
        else:
            total += 1
    return total


def _raise_if_history_rejected(resp: httpx.Response, *, context: str) -> None:
    """For the single-item history helpers: a non-empty `not_found` means the
    one item we sent was rejected, so surface it instead of logging success."""
    not_found = _history_not_found(_safe_json(resp))
    if not_found:
        raise SimklHistoryRejected(f"{context}: Simkl wrote nothing (not_found={not_found})")


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


# ── AUTH V2 (device flow + refresh) ───────────────────────────────────────────

V2_SCOPE = "media:read media:write"
# Refresh once the access token is this close to expiring (it lives 7 days).
V2_REFRESH_SKEW_SECONDS = 2 * 24 * 3600


class SimklNotV2Client(Exception):
    """The Client ID is an AUTH V1 app: /oauth2/* answers 401 invalid_client
    ("This client_id is not enabled for OAuth 2.0"). The caller falls back to
    the V1 PIN flow."""


class SimklAuthError(Exception):
    """A permanent V2 failure (expired/denied/revoked) - polling or retrying
    cannot fix it, the user has to connect again."""


def _v2_headers() -> dict:
    return {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT}


def _error_code(resp: httpx.Response) -> str | None:
    payload = _safe_json(resp)
    return payload.get("error") if isinstance(payload, dict) else None


async def start_device_auth_v2(client_id: str) -> dict:
    """POST /oauth2/device. Returns {device_code, user_code, verification_uri,
    verification_uri_complete, expires_in, interval}. Raises SimklNotV2Client
    for a V1 Client ID."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{SIMKL_BASE}/oauth2/device",
            data={"client_id": client_id, "scope": V2_SCOPE},
            headers=_v2_headers(),
        )
    if resp.status_code == 401 and _error_code(resp) == "invalid_client":
        raise SimklNotV2Client()
    resp.raise_for_status()
    return resp.json()


async def poll_device_token_v2(client_id: str, device_code: str) -> dict | None:
    """Poll POST /oauth2/token. Returns the token response once approved, None
    while still pending (including slow_down - the caller's own interval is
    already >= Simkl's). Raises SimklAuthError when the code expired."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{SIMKL_BASE}/oauth2/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": client_id,
                "device_code": device_code,
            },
            headers=_v2_headers(),
        )
    if resp.status_code == 200:
        return resp.json()
    error = _error_code(resp)
    if error in ("authorization_pending", "slow_down"):
        return None
    if error == "expired_token":
        raise SimklAuthError("The code expired. Start the connection again.")
    resp.raise_for_status()
    return None


async def refresh_access_token_v2(client_id: str, refresh_token: str) -> dict:
    """Refresh grant. Simkl's refresh token is non-rotating - the response
    repeats it - and refreshing invalidates the previous access token. Raises
    SimklAuthError when Simkl rejects the refresh token (revoked / 180 days
    unused), anything else is a transient error left to propagate."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{SIMKL_BASE}/oauth2/token",
            data={"grant_type": "refresh_token", "client_id": client_id, "refresh_token": refresh_token},
            headers=_v2_headers(),
        )
    if resp.status_code == 200:
        return resp.json()
    if resp.status_code in (400, 401) and _error_code(resp) in ("invalid_grant", "invalid_client", "invalid_token"):
        raise SimklAuthError("Simkl rejected the refresh token. Reconnect Simkl in Settings.")
    resp.raise_for_status()
    return {}


async def revoke_token_v2(client_id: str, token: str) -> None:
    """Best-effort RFC 7009 revoke; Simkl always answers 200."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            await client.post(
                f"{SIMKL_BASE}/oauth2/revoke",
                data={"client_id": client_id, "token": token},
                headers=_v2_headers(),
            )
    except httpx.HTTPError as exc:
        logger.info("Simkl token revoke failed (ignored): %s", exc)


def token_expires_at(token_response: dict) -> int:
    return int(datetime.now(timezone.utc).timestamp()) + int(token_response.get("expires_in") or 7 * 24 * 3600)


def needs_refresh(settings, now: int | None = None) -> bool:
    """True for a V2 connection whose access token is expired or about to be.
    A V1 connection (no refresh token) never needs one. A refresh token with no
    known expiry (e.g. restored from a backup) is refreshed straight away."""
    if not getattr(settings, "simkl_refresh_token", None) or not getattr(settings, "simkl_client_id", None):
        return False
    expires_at = getattr(settings, "simkl_token_expires_at", None)
    if not expires_at:
        return True
    now = now if now is not None else int(datetime.now(timezone.utc).timestamp())
    return expires_at - now <= V2_REFRESH_SKEW_SECONDS


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
        _raise_if_history_rejected(resp, context=f"movie tmdb={tmdb_id}")


async def add_history_batch(
    client_id: str,
    access_token: str,
    movies: list[tuple[int, Optional[datetime]]],
    episodes: list[tuple[int, int, int, Optional[datetime]]],
) -> int:
    """Add multiple movies and/or episodes to Simkl history in a single API call.

    movies: list of (tmdb_id, watched_at)
    episodes: list of (show_tmdb_id, season_number, episode_number, watched_at)

    Returns how many of the submitted items Simkl accepted the request for but
    could not resolve (reported in `not_found`), so callers can count them as
    failures instead of successes (#453).
    """
    if not movies and not episodes:
        return 0
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
        # Simkl can accept the request (201) yet silently drop items it can't
        # resolve into its own season layout - don't fail the whole batch over
        # it, but don't let the loss go unrecorded either (#328).
        rejected = _history_not_found(_safe_json(resp))
        if rejected:
            logger.warning(
                "Simkl /sync/history accepted the batch but could not resolve %d item(s): %s",
                len(rejected), rejected,
            )
        return _count_history_items(rejected)


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
        _raise_if_history_rejected(
            resp, context=f"show tmdb={show_tmdb_id} S{season_number}E{episode_number}"
        )


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

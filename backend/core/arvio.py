import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://auth.arvio.tv/.netlify/functions"
DEFAULT_APP_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InpyZHd2b3J0Y2Zub3lrbHR6dXFmIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjY3NDU4NzMsImV4cCI6MjA4MjMyMTg3M30.YfKZbSwxGs6_xMd6jkDtn1PKkfuyOHo9qVhUvFRddGU"

class ArvioAPIError(RuntimeError):
    pass

OnRefresh = Callable[["ArvioSession"], Awaitable[None]] | None

_connection_locks: dict[int, asyncio.Lock] = {}

def connection_lock(connection_id: int) -> asyncio.Lock:
    """Shared per-connection lock guarding ARVIO's rotating refresh token."""
    return _connection_locks.setdefault(connection_id, asyncio.Lock())

@dataclass(frozen=True)
class ArvioSession:
    access_token: str
    refresh_token: str
    expires_in: int
    user_id: str | None = None
    email: str | None = None

def _base_url(url: str) -> str:
    return (url or DEFAULT_URL).rstrip("/")

def _public_headers(api_key: str | None = None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    key = api_key or os.getenv("ARVIO_APP_ANON_KEY") or DEFAULT_APP_ANON_KEY
    if key:
        headers["apikey"] = key
        headers["Authorization"] = f"Bearer {key}"
    return headers

def _auth_headers(access_token: str, api_key: str | None = None) -> dict[str, str]:
    headers = _public_headers(api_key)
    headers["Authorization"] = f"Bearer {access_token}"
    return headers

async def _raise_api_error(response: httpx.Response, operation: str) -> None:
    if response.is_success:
        return
    detail = None
    try:
        payload = response.json()
        detail = (
            payload.get("msg")
            or payload.get("message")
            or payload.get("error_description")
            or payload.get("error_code")
            or payload.get("error")
        )
    except (ValueError, AttributeError):
        detail = None
    if not detail:
        detail = response.text
    suffix = f": {detail}" if detail else ""
    if response.status_code == 401 and ("unauthorized" in str(detail).lower() or not detail):
        suffix += " (ARVIO requires a valid APP_ANON_KEY. Set the ARVIO_APP_ANON_KEY environment variable or supply the App Key.)"
    raise ArvioAPIError(f"ARVIO {operation} failed ({response.status_code}){suffix}")

def _parse_session(payload: dict[str, Any]) -> ArvioSession:
    # Supabase / Netlify auth response may nest session data in 'session' or at root
    data = payload.get("session") if isinstance(payload.get("session"), dict) else payload
    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    expires_in = data.get("expires_in", 3600)
    user = data.get("user") or {}
    user_id = user.get("id") if isinstance(user, dict) else None
    email = user.get("email") if isinstance(user, dict) else None

    if not access_token or not refresh_token:
        raise ArvioAPIError("ARVIO authentication returned an incomplete session")
    return ArvioSession(
        access_token=str(access_token),
        refresh_token=str(refresh_token),
        expires_in=int(expires_in),
        user_id=str(user_id) if user_id else None,
        email=str(email) if email else None,
    )

async def sign_in(url: str, email: str, password: str, api_key: str | None = None) -> ArvioSession:
    base = _base_url(url)
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.post(
                f"{base}/auth-login",
                headers=_public_headers(api_key),
                json={"email": email, "password": password},
            )
        except httpx.RequestError as exc:
            raise ArvioAPIError(f"Could not connect to ARVIO auth backend: {exc}")
    await _raise_api_error(resp, "login")
    return _parse_session(resp.json())

async def refresh_session(url: str, refresh_token: str, api_key: str | None = None) -> ArvioSession:
    base = _base_url(url)
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.post(
                f"{base}/auth-refresh",
                headers=_public_headers(api_key),
                json={"refresh_token": refresh_token},
            )
        except httpx.RequestError as exc:
            raise ArvioAPIError(f"Could not connect to ARVIO auth backend: {exc}")
    await _raise_api_error(resp, "token refresh")
    return _parse_session(resp.json())

async def pull_snapshot(url: str, access_token: str, api_key: str | None = None) -> dict[str, Any]:
    base = _base_url(url)
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.get(
                f"{base}/account-sync-pull",
                headers=_auth_headers(access_token, api_key),
            )
        except httpx.RequestError as exc:
            raise ArvioAPIError(f"Could not pull ARVIO account snapshot: {exc}")
    await _raise_api_error(resp, "pull snapshot")
    body = resp.json()
    payload = body.get("payload", {})
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    return payload if isinstance(payload, dict) else {}

def extract_profiles(payload: dict[str, Any]) -> list[dict[str, str]]:
    raw_profiles = payload.get("profiles", [])
    if not isinstance(raw_profiles, list):
        return []
    profiles = []
    for p in raw_profiles:
        if isinstance(p, dict):
            pid = p.get("id") or p.get("profileId") or p.get("profile_id")
            name = p.get("name") or p.get("displayName") or p.get("profileName") or f"Profile {pid}"
            if pid is not None:
                profiles.append({"id": str(pid), "name": str(name).strip()})
    return profiles

def get_profile_name(profiles: list[dict[str, str]], profile_id: str) -> str:
    for p in profiles:
        if p.get("id") == str(profile_id):
            return p.get("name") or f"Profile {profile_id}"
    return f"Profile {profile_id}"

async def authenticate(url: str, email: str, password: str, api_key: str | None = None) -> tuple[ArvioSession, list[dict[str, str]]]:
    session = await sign_in(url, email, password, api_key=api_key)
    payload = await pull_snapshot(url, session.access_token, api_key=api_key)
    profiles = extract_profiles(payload)
    return session, profiles

async def validate_connection(
    url: str,
    refresh_token: str,
    profile_id: str | None = None,
    *,
    on_refresh: OnRefresh = None,
    api_key: str | None = None,
) -> tuple[ArvioSession, list[dict[str, str]]]:
    session = await refresh_session(url, refresh_token, api_key=api_key)
    if on_refresh:
        await on_refresh(session)
    payload = await pull_snapshot(url, session.access_token, api_key=api_key)
    profiles = extract_profiles(payload)
    if profile_id is not None:
        valid_ids = {p["id"] for p in profiles}
        if valid_ids and str(profile_id) not in valid_ids:
            raise ArvioAPIError(f"ARVIO profile '{profile_id}' not found in account profiles")
    return session, profiles

def _extract_profile_data(raw: Any, profile_id: str) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, dict) or not raw:
        return []

    pid_str = str(profile_id)
    # 1. Exact string match
    if pid_str in raw and isinstance(raw[pid_str], list):
        return raw[pid_str]

    # 2. Integer match
    if pid_str.isdigit():
        pid_int = int(pid_str)
        if pid_int in raw and isinstance(raw[pid_int], list):
            return raw[pid_int]

    # 3. Case-insensitive / suffix match (e.g. "profile_0", "p0", "0")
    for k, v in raw.items():
        if isinstance(v, list):
            k_str = str(k).lower().strip()
            if k_str == pid_str.lower() or k_str.endswith(f"_{pid_str}") or k_str.endswith(f"-{pid_str}"):
                return v

    # 4. If single key in dictionary, return that list
    if len(raw) == 1:
        val = list(raw.values())[0]
        if isinstance(val, list):
            return val

    # 5. Still unmatched - fail safe rather than combine every profile's data
    # together, which would leak another profile's watch history into this
    # one on a multi-profile ARVIO account (cases 1-4 above already cover
    # exact/int/suffix matches and the single-profile shortcut).
    return []


async def pull_sync_data(
    url: str,
    refresh_token: str,
    profile_id: str,
    *,
    on_refresh: OnRefresh = None,
    api_key: str | None = None,
) -> tuple[ArvioSession, dict[str, Any]]:
    session = await refresh_session(url, refresh_token, api_key=api_key)
    if on_refresh:
        await on_refresh(session)
    payload = await pull_snapshot(url, session.access_token, api_key=api_key)

    raw_movies = (
        payload.get("localWatchedMoviesByProfile")
        or payload.get("watchedMoviesByProfile")
        or payload.get("watchedMovies")
        or payload.get("localWatchedMovies")
        or {}
    )
    raw_episodes = (
        payload.get("localWatchedEpisodesByProfile")
        or payload.get("watchedEpisodesByProfile")
        or payload.get("watchedEpisodes")
        or payload.get("localWatchedEpisodes")
        or {}
    )
    raw_cw = (
        payload.get("localContinueWatchingByProfile")
        or payload.get("continueWatchingByProfile")
        or payload.get("continueWatching")
        or payload.get("localContinueWatching")
        or {}
    )

    movies_data = _extract_profile_data(raw_movies, profile_id)
    episodes_data = _extract_profile_data(raw_episodes, profile_id)
    cw_data = _extract_profile_data(raw_cw, profile_id)

    return session, {
        "watched_movies": movies_data,
        "watched_episodes": episodes_data,
        "progress": cw_data,
    }

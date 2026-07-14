from dataclasses import dataclass
from typing import Any

import httpx


DEFAULT_URL = "https://api.nuvio.tv"
PUBLISHABLE_KEY = "sb_publishable_1Clq8rlTVACkdcZuqr6_AD__xUUC_EN"
_PAGE_SIZE = 500


class NuvioAPIError(RuntimeError):
    pass


@dataclass(frozen=True)
class NuvioSession:
    access_token: str
    refresh_token: str
    expires_in: int


def _base_url(url: str) -> str:
    return url.rstrip("/")


def _public_headers() -> dict[str, str]:
    return {"apikey": PUBLISHABLE_KEY, "Content-Type": "application/json"}


def _auth_headers(access_token: str) -> dict[str, str]:
    return {**_public_headers(), "Authorization": f"Bearer {access_token}"}


async def _raise_api_error(response: httpx.Response, operation: str) -> None:
    if response.is_success:
        return
    try:
        payload = response.json()
        detail = payload.get("message") or payload.get("error_description") or payload.get("error")
    except (ValueError, AttributeError):
        detail = None
    suffix = f": {detail}" if detail else ""
    raise NuvioAPIError(f"Nuvio {operation} failed ({response.status_code}){suffix}")


def _parse_session(payload: dict[str, Any]) -> NuvioSession:
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    if not access_token or not refresh_token:
        raise NuvioAPIError("Nuvio authentication returned an incomplete session")
    return NuvioSession(
        access_token=str(access_token),
        refresh_token=str(refresh_token),
        expires_in=int(payload.get("expires_in") or 0),
    )


async def sign_in(url: str, email: str, password: str) -> NuvioSession:
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
        response = await client.post(
            f"{_base_url(url)}/auth/v1/token",
            params={"grant_type": "password"},
            headers=_public_headers(),
            json={"email": email, "password": password},
        )
    await _raise_api_error(response, "sign-in")
    return _parse_session(response.json())


async def refresh_session(
    url: str,
    refresh_token: str,
    client: httpx.AsyncClient | None = None,
) -> NuvioSession:
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=20.0, follow_redirects=False)
    try:
        response = await client.post(
            f"{_base_url(url)}/auth/v1/token",
            params={"grant_type": "refresh_token"},
            headers=_public_headers(),
            json={"refresh_token": refresh_token},
        )
        await _raise_api_error(response, "token refresh")
        return _parse_session(response.json())
    finally:
        if owns_client:
            await client.aclose()


async def _rpc(
    client: httpx.AsyncClient,
    url: str,
    access_token: str,
    function_name: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    response = await client.post(
        f"{_base_url(url)}/rest/v1/rpc/{function_name}",
        headers=_auth_headers(access_token),
        json=payload,
    )
    await _raise_api_error(response, function_name)
    if response.status_code == 204 or not response.content:
        return None
    return response.json()


async def get_profiles(
    url: str,
    access_token: str,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=20.0, follow_redirects=False)
    try:
        profiles = await _rpc(client, url, access_token, "sync_pull_profiles")
        return profiles if isinstance(profiles, list) else []
    finally:
        if owns_client:
            await client.aclose()


async def authenticate(url: str, email: str, password: str) -> tuple[NuvioSession, list[dict[str, Any]]]:
    session = await sign_in(url, email, password)
    profiles = await get_profiles(url, session.access_token)
    return session, profiles


async def validate_connection(
    url: str,
    refresh_token: str,
    profile_id: int | None = None,
) -> tuple[NuvioSession, list[dict[str, Any]]]:
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
        session = await refresh_session(url, refresh_token, client=client)
        profiles = await get_profiles(url, session.access_token, client=client)
    if profile_id is not None and not any(int(profile.get("profile_index", 0)) == profile_id for profile in profiles):
        raise NuvioAPIError(f"Nuvio profile {profile_id} was not found")
    return session, profiles


async def _pull_library(
    client: httpx.AsyncClient,
    url: str,
    access_token: str,
    profile_id: int,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = await _rpc(
            client,
            url,
            access_token,
            "sync_pull_library",
            {"p_profile_id": profile_id, "p_limit": _PAGE_SIZE, "p_offset": offset},
        )
        page = page if isinstance(page, list) else []
        items.extend(page)
        if len(page) < _PAGE_SIZE:
            return items
        offset += _PAGE_SIZE


async def _pull_watched_items(
    client: httpx.AsyncClient,
    url: str,
    access_token: str,
    profile_id: int,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page_number = 1
    while True:
        page = await _rpc(
            client,
            url,
            access_token,
            "sync_pull_watched_items",
            {"p_profile_id": profile_id, "p_page": page_number, "p_page_size": _PAGE_SIZE},
        )
        page = page if isinstance(page, list) else []
        items.extend(page)
        if len(page) < _PAGE_SIZE:
            return items
        page_number += 1


async def _pull_watch_progress(
    client: httpx.AsyncClient,
    url: str,
    access_token: str,
    profile_id: int,
) -> list[dict[str, Any]]:
    rows = await _rpc(
        client,
        url,
        access_token,
        "sync_pull_watch_progress",
        {"p_profile_id": profile_id, "p_limit": 200},
    )
    return rows if isinstance(rows, list) else []


async def pull_sync_data(
    url: str,
    refresh_token: str,
    profile_id: int,
) -> tuple[NuvioSession, dict[str, list[dict[str, Any]]]]:
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
        session = await refresh_session(url, refresh_token, client=client)
        profiles = await get_profiles(url, session.access_token, client=client)
        if not any(int(profile.get("profile_index", 0)) == profile_id for profile in profiles):
            raise NuvioAPIError(f"Nuvio profile {profile_id} was not found")
        library = await _pull_library(client, url, session.access_token, profile_id)
        watched = await _pull_watched_items(client, url, session.access_token, profile_id)
        progress = await _pull_watch_progress(client, url, session.access_token, profile_id)
    return session, {"library": library, "watched": watched, "progress": progress}


async def push_watched_items(
    url: str,
    refresh_token: str,
    profile_id: int,
    items: list[dict[str, Any]],
) -> NuvioSession:
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
        session = await refresh_session(url, refresh_token, client=client)
        for offset in range(0, len(items), _PAGE_SIZE):
            await _rpc(
                client,
                url,
                session.access_token,
                "sync_push_watched_items",
                {"p_profile_id": profile_id, "p_items": items[offset : offset + _PAGE_SIZE]},
            )
    return session


async def delete_watched_items(
    url: str,
    refresh_token: str,
    profile_id: int,
    keys: list[dict[str, Any]],
) -> NuvioSession:
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
        session = await refresh_session(url, refresh_token, client=client)
        for offset in range(0, len(keys), _PAGE_SIZE):
            await _rpc(
                client,
                url,
                session.access_token,
                "sync_delete_watched_items",
                {"p_profile_id": profile_id, "p_keys": keys[offset : offset + _PAGE_SIZE]},
            )
    return session

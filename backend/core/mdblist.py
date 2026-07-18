"""Async client for MDBList's user synchronization API."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import httpx

MDBLIST_BASE = "https://api.mdblist.com"
PAGE_SIZE = 1000
PUSH_BATCH_SIZE = 500


class MDBListAPIError(RuntimeError):
    """Raised when MDBList rejects or cannot complete a request."""


async def _request(
    method: str,
    path: str,
    api_key: str,
    *,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    query = dict(params or {})
    query["apikey"] = api_key
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.request(
                method,
                f"{MDBLIST_BASE}{path}",
                params=query,
                json=payload,
            )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text.strip()[:500]
        suffix = f": {detail}" if detail else ""
        raise MDBListAPIError(
            f"MDBList {method} {path} failed ({exc.response.status_code}){suffix}"
        ) from exc
    except httpx.HTTPError as exc:
        raise MDBListAPIError(f"MDBList {method} {path} failed: {exc}") from exc

    if response.status_code == 204 or not response.content:
        return {}
    data = response.json()
    if not isinstance(data, dict):
        raise MDBListAPIError(f"MDBList {method} {path} returned an invalid response")
    return data


async def validate_api_key(api_key: str) -> bool:
    if not api_key:
        return False
    try:
        await _request("GET", "/sync/last_activities", api_key)
        return True
    except MDBListAPIError:
        return False


async def _get_all(api_key: str, path: str) -> dict[str, Any]:
    merged: dict[str, Any] = {
        "movies": [],
        "shows": [],
        "seasons": [],
        "episodes": [],
    }
    cursor: str | None = None
    total_seen = 0
    seen_cursors: set[str] = set()

    while True:
        params: dict[str, Any] = {"limit": PAGE_SIZE}
        if cursor:
            params["cursor"] = cursor
        elif total_seen:
            params["offset"] = total_seen

        page = await _request("GET", path, api_key, params=params)
        page_count = 0
        for key in ("movies", "shows", "seasons", "episodes"):
            values = page.get(key)
            if isinstance(values, list):
                merged[key].extend(values)
                page_count += len(values)
        total_seen += page_count

        pagination = page.get("pagination")
        pagination = pagination if isinstance(pagination, dict) else {}
        next_cursor = pagination.get("next_cursor")
        if next_cursor:
            next_cursor = str(next_cursor)
            if next_cursor in seen_cursors:
                raise MDBListAPIError(f"MDBList {path} returned a repeated pagination cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
            continue

        if pagination.get("has_more"):
            if page_count == 0:
                raise MDBListAPIError(f"MDBList {path} reported more pages without returning items")
            cursor = None
            continue
        break

    return merged


async def get_watched(api_key: str) -> dict[str, Any]:
    return await _get_all(api_key, "/sync/watched")


async def get_ratings(api_key: str) -> dict[str, Any]:
    return await _get_all(api_key, "/sync/ratings")


async def get_watchlist(api_key: str) -> dict[str, Any]:
    return await _get_all(api_key, "/watchlist/items")


def _batched_payloads(payload: dict[str, list[dict[str, Any]]]) -> Iterable[dict[str, list[dict[str, Any]]]]:
    for key in ("movies", "shows", "seasons", "episodes"):
        values = payload.get(key, [])
        for offset in range(0, len(values), PUSH_BATCH_SIZE):
            yield {key: values[offset : offset + PUSH_BATCH_SIZE]}


async def _push(path: str, api_key: str, payload: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    stats = {"submitted": 0, "batches": 0, "not_found": 0}
    for batch in _batched_payloads(payload):
        stats["batches"] += 1
        result = await _request("POST", path, api_key, payload=batch)
        stats["submitted"] += sum(len(values) for values in batch.values())
        not_found = result.get("not_found")
        if isinstance(not_found, dict):
            stats["not_found"] += sum(
                len(values) for values in not_found.values() if isinstance(values, list)
            )
    return stats


async def push_watched(api_key: str, payload: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    return await _push("/sync/watched", api_key, payload)


async def remove_watched(api_key: str, payload: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    return await _push("/sync/watched/remove", api_key, payload)


async def push_ratings(api_key: str, payload: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    return await _push("/sync/ratings", api_key, payload)


async def remove_ratings(api_key: str, payload: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    return await _push("/sync/ratings/remove", api_key, payload)


async def push_watchlist(api_key: str, payload: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    return await _push("/watchlist/items/add", api_key, payload)


async def remove_watchlist(api_key: str, payload: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    return await _push("/watchlist/items/remove", api_key, payload)

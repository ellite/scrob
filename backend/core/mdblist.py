"""Async client for MDBList's user synchronization API."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
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


def _count_leaf_items(payload: dict[str, list[dict[str, Any]]]) -> int:
    """Count actual movies/shows/seasons/episodes represented in a payload.

    A "shows" entry built by _merge_show_entries() can nest many seasons and
    episodes under a single top-level object — counting len(payload["shows"])
    alone would wildly understate how many real items were sent (e.g. hundreds
    of watched episodes from a handful of shows would look like just a few
    "submitted" items). Count the actual leaves instead.
    """
    count = len(payload.get("movies", [])) + len(payload.get("seasons", [])) + len(payload.get("episodes", []))
    for show in payload.get("shows", []):
        seasons = show.get("seasons")
        if not seasons:
            count += 1
            continue
        for season in seasons:
            episodes = season.get("episodes")
            count += len(episodes) if episodes else 1
    return count


async def _push(
    path: str,
    api_key: str,
    payload: dict[str, list[dict[str, Any]]],
    *,
    on_batch: Callable[[int], Awaitable[None]] | None = None,
) -> dict[str, int]:
    stats = {"submitted": 0, "batches": 0, "not_found": 0}
    for batch in _batched_payloads(payload):
        stats["batches"] += 1
        result = await _request("POST", path, api_key, payload=batch)
        batch_count = _count_leaf_items(batch)
        stats["submitted"] += batch_count
        not_found = result.get("not_found")
        if isinstance(not_found, dict):
            stats["not_found"] += sum(
                len(values) for values in not_found.values() if isinstance(values, list)
            )
        if on_batch is not None:
            await on_batch(batch_count)
    return stats


async def push_watched(
    api_key: str,
    payload: dict[str, list[dict[str, Any]]],
    *,
    on_batch: Callable[[int], Awaitable[None]] | None = None,
) -> dict[str, int]:
    return await _push("/sync/watched", api_key, payload, on_batch=on_batch)


async def remove_watched(api_key: str, payload: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    return await _push("/sync/watched/remove", api_key, payload)


async def push_collection(
    api_key: str,
    payload: dict[str, list[dict[str, Any]]],
    *,
    on_batch: Callable[[int], Awaitable[None]] | None = None,
) -> dict[str, int]:
    return await _push("/sync/collection", api_key, payload, on_batch=on_batch)


async def remove_collection(api_key: str, payload: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    return await _push("/sync/collection/remove", api_key, payload)


async def scrobble_movie(api_key: str, action: str, tmdb_id: int, progress: float | None = None) -> dict[str, Any]:
    """Start/pause/stop/clear a movie scrobble session on MDBList.

    ``progress`` is omitted for ``action="clear"``, which takes no progress value."""
    body: dict[str, Any] = {"movie": {"ids": {"tmdb": tmdb_id}}}
    if progress is not None:
        body["progress"] = round(min(100.0, max(0.0, progress)), 1)
    return await _request("POST", f"/scrobble/{action}", api_key, payload=body)


async def scrobble_episode(
    api_key: str,
    action: str,
    show_tmdb_id: int,
    season_number: int,
    episode_number: int,
    progress: float | None = None,
) -> dict[str, Any]:
    """Start/pause/stop/clear an episode scrobble session on MDBList.

    ``progress`` is omitted for ``action="clear"``, which takes no progress value."""
    body: dict[str, Any] = {
        "show": {"ids": {"tmdb": show_tmdb_id}, "season": season_number, "episode": episode_number},
    }
    if progress is not None:
        body["progress"] = round(min(100.0, max(0.0, progress)), 1)
    return await _request("POST", f"/scrobble/{action}", api_key, payload=body)


async def push_ratings(
    api_key: str,
    payload: dict[str, list[dict[str, Any]]],
    *,
    on_batch: Callable[[int], Awaitable[None]] | None = None,
) -> dict[str, int]:
    return await _push("/sync/ratings", api_key, payload, on_batch=on_batch)


async def remove_ratings(api_key: str, payload: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    return await _push("/sync/ratings/remove", api_key, payload)


async def push_watchlist(
    api_key: str,
    payload: dict[str, list[dict[str, Any]]],
    *,
    on_batch: Callable[[int], Awaitable[None]] | None = None,
) -> dict[str, int]:
    return await _push("/watchlist/items/add", api_key, payload, on_batch=on_batch)


async def remove_watchlist(api_key: str, payload: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    return await _push("/watchlist/items/remove", api_key, payload)

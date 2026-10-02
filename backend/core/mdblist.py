"""Async client for MDBList's user synchronization API."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import httpx

logger = logging.getLogger(__name__)

MDBLIST_BASE = "https://api.mdblist.com"
PAGE_SIZE = 1000
# MDBList rejects a push with more than 200 top-level entries in any of
# movies/shows/seasons/episodes with a 400 ("Too many shows in one request
# (max 200)") - this was set above that limit, so the whole push job aborted
# outright instead of being chunked as _batched_payloads/_push already
# intend it to be (see #176).
PUSH_BATCH_SIZE = 200

# MDBList answers both of its throttles with 429, distinguished only by the body.
# "API rate limit exceeded!" is the short-window throttle and clears on its own, so it
# is worth waiting out; "Daily API limit exceeded!" does not clear until the quota
# resets, so retrying it just burns the rest of the job against a wall.
_RATE_LIMIT_RETRIES = 4
_RATE_LIMIT_BACKOFF = 2.0
# A server-sent Retry-After is honoured only up to this many seconds, so a large
# value can't park a sync job for minutes per request.
_RATE_LIMIT_MAX_WAIT = 30.0
# Live scrobbles are sent from inside a webhook request, which holds a DB
# connection while it waits (see #412 for what that does to the pool) - and a
# scrobble that arrives late is stale anyway. So they get one short retry, not
# the full backoff.
_SCROBBLE_RETRIES = 1
_SCROBBLE_MAX_WAIT = 3.0
_DAILY_LIMIT_MARKER = "daily api limit"


class MDBListAPIError(RuntimeError):
    """Raised when MDBList rejects or cannot complete a request."""


class MDBListDailyLimitError(MDBListAPIError):
    """Raised when MDBList's daily quota is spent - not retryable within the run."""


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds to wait from a Retry-After header, when MDBList sends a usable one."""
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


async def _request_json_with_headers(
    method: str,
    path: str,
    api_key: str,
    *,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    ignore_statuses: set[int] | None = None,
    rate_limit_retries: int = _RATE_LIMIT_RETRIES,
    max_wait: float = _RATE_LIMIT_MAX_WAIT,
) -> tuple[dict[str, Any] | list[Any], httpx.Headers]:
    query = dict(params or {})
    query["apikey"] = api_key
    for attempt in range(rate_limit_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.request(
                    method,
                    f"{MDBLIST_BASE}{path}",
                    params=query,
                    json=payload,
                )
            if ignore_statuses and response.status_code in ignore_statuses:
                return {}, response.headers
            if response.status_code == 429:
                body = response.text.strip()
                if _DAILY_LIMIT_MARKER in body.lower():
                    raise MDBListDailyLimitError(
                        f"MDBList {method} {path} failed (429): {body[:500]}"
                    )
                if attempt < rate_limit_retries:
                    delay = min(max_wait, _retry_after(response) or _RATE_LIMIT_BACKOFF * (2**attempt))
                    logger.info(
                        "MDBList rate-limited on %s %s; waiting %.1fs (attempt %d/%d)",
                        method, path, delay, attempt + 1, rate_limit_retries,
                    )
                    await asyncio.sleep(delay)
                    continue
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text.strip()[:500]
            suffix = f": {detail}" if detail else ""
            raise MDBListAPIError(
                f"MDBList {method} {path} failed ({exc.response.status_code}){suffix}"
            ) from exc
        except httpx.HTTPError as exc:
            raise MDBListAPIError(f"MDBList {method} {path} failed: {exc}") from exc
        break

    if response.status_code == 204 or not response.content:
        return {}, response.headers
    data = response.json()
    if not isinstance(data, (dict, list)):
        raise MDBListAPIError(f"MDBList {method} {path} returned an invalid response")
    return data, response.headers


async def _request_json(
    method: str,
    path: str,
    api_key: str,
    *,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    ignore_statuses: set[int] | None = None,
    rate_limit_retries: int = _RATE_LIMIT_RETRIES,
    max_wait: float = _RATE_LIMIT_MAX_WAIT,
) -> dict[str, Any] | list[Any]:
    data, _ = await _request_json_with_headers(
        method,
        path,
        api_key,
        params=params,
        payload=payload,
        ignore_statuses=ignore_statuses,
        rate_limit_retries=rate_limit_retries,
        max_wait=max_wait,
    )
    return data


async def _request(
    method: str,
    path: str,
    api_key: str,
    *,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    ignore_statuses: set[int] | None = None,
    rate_limit_retries: int = _RATE_LIMIT_RETRIES,
    max_wait: float = _RATE_LIMIT_MAX_WAIT,
) -> dict[str, Any]:
    data = await _request_json(
        method,
        path,
        api_key,
        params=params,
        payload=payload,
        ignore_statuses=ignore_statuses,
        rate_limit_retries=rate_limit_retries,
        max_wait=max_wait,
    )
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


async def get_list(api_key: str, list_ref: str) -> dict[str, Any]:
    """Return metadata for a list addressed by numeric id or username/slug."""
    path = f"/lists/{list_ref}"
    data = await _request_json("GET", path, api_key)
    if isinstance(data, dict):
        return data

    # Username/slug lookups return an array because MDBList may represent a
    # mixed list as separate movie and show records. Both records share the
    # public list name and are fetched together by the unified items endpoint.
    records = [record for record in data if isinstance(record, dict)]
    if not records:
        raise MDBListAPIError(f"MDBList GET {path} returned an invalid response")

    metadata = dict(records[0])
    for field in ("name", "description"):
        if not metadata.get(field):
            value = next((record.get(field) for record in records if record.get(field)), None)
            if value is not None:
                metadata[field] = value
    return metadata


async def get_list_items(
    api_key: str,
    list_ref: str,
    *,
    max_items: int = 500,
) -> dict[str, Any]:
    """Fetch up to ``max_items`` movies/shows from an MDBList list.

    ``unified=true`` is important for dynamic lists that contain both media
    types; without it MDBList may expose the movie and show halves separately.
    """
    merged: dict[str, Any] = {"movies": [], "shows": []}
    cursor: str | None = None
    seen_cursors: set[str] = set()

    while len(merged["movies"]) + len(merged["shows"]) < max_items:
        remaining = max_items - len(merged["movies"]) - len(merged["shows"])
        params: dict[str, Any] = {
            "limit": min(PAGE_SIZE, remaining),
            "unified": "true",
        }
        if cursor:
            params["cursor"] = cursor

        page, headers = await _request_json_with_headers(
            "GET", f"/lists/{list_ref}/items", api_key, params=params
        )
        page_count = 0
        if isinstance(page, list):
            # With unified=true the live API returns one flat array, despite
            # its OpenAPI schema describing an object with media buckets.
            for entry in page:
                if not isinstance(entry, dict):
                    continue
                mediatype = str(entry.get("mediatype") or "").lower()
                if mediatype == "movie":
                    key = "movies"
                elif mediatype in {"show", "series", "tv"}:
                    key = "shows"
                else:
                    continue
                if len(merged["movies"]) + len(merged["shows"]) >= max_items:
                    break
                merged[key].append(entry)
                page_count += 1
            next_cursor = headers.get("X-Next-Cursor")
        else:
            for key in ("movies", "shows"):
                values = page.get(key)
                if isinstance(values, list):
                    room = max_items - len(merged["movies"]) - len(merged["shows"])
                    merged[key].extend(values[:room])
                    page_count += min(len(values), room)

            pagination = page.get("pagination")
            pagination = pagination if isinstance(pagination, dict) else {}
            next_cursor = pagination.get("next_cursor")
        if not next_cursor or page_count == 0:
            break
        cursor = str(next_cursor)
        if cursor in seen_cursors:
            raise MDBListAPIError(
                f"MDBList /lists/{list_ref}/items returned a repeated pagination cursor"
            )
        seen_cursors.add(cursor)

    return merged


async def get_dropped(api_key: str) -> dict[str, Any]:
    return await _get_all(api_key, "/sync/dropped")


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


# How many of MDBList's echoed-back "not found" items to keep for logging /
# the job stats. A healthy push has a handful; capping keeps a pathological
# run from bloating the log line and the SyncJob.stats JSON.
NOT_FOUND_SAMPLE_CAP = 200


def _index_episode_shows(
    batch: dict[str, list[dict[str, Any]]],
) -> tuple[dict[tuple[int, int], list[int]], dict[int, list[int]]]:
    """Maps this outgoing batch's own (season, episode) positions - and,
    separately, bare season numbers - back to the show tmdb id(s) they were
    nested under in the request.

    _payload_item() always nests an episode under its parent show's ids, so
    the request never loses that context - but MDBList's "not_found"
    response echoes a rejected episode back in its own flat "episodes"
    bucket, with just the season (sometimes not even the episode) number and
    no id of any kind (#368). Since the response can't be made to carry more
    than MDBList chooses to send back, recovering the show is only possible
    from what this side already sent, not from the reply.
    """
    exact: dict[tuple[int, int], list[int]] = {}
    by_season: dict[int, list[int]] = {}
    for show in batch.get("shows", []) or []:
        tmdb_id = (show.get("ids") or {}).get("tmdb")
        if tmdb_id is None:
            continue
        for season in show.get("seasons", []) or []:
            season_number = season.get("number")
            if season_number is None:
                continue
            by_season.setdefault(season_number, []).append(tmdb_id)
            for episode in season.get("episodes") or []:
                episode_number = episode.get("number")
                if episode_number is not None:
                    exact.setdefault((season_number, episode_number), []).append(tmdb_id)
    return exact, by_season


async def _push(
    path: str,
    api_key: str,
    payload: dict[str, list[dict[str, Any]]],
    *,
    on_batch: Callable[[int], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    stats: dict[str, Any] = {"submitted": 0, "batches": 0, "not_found": 0, "not_found_items": []}
    for batch in _batched_payloads(payload):
        stats["batches"] += 1
        result = await _request("POST", path, api_key, payload=batch)
        batch_count = _count_leaf_items(batch)
        stats["submitted"] += batch_count
        not_found = result.get("not_found")
        if isinstance(not_found, dict):
            exact_index: dict[tuple[int, int], list[int]] | None = None
            season_index: dict[int, list[int]] | None = None
            for kind, values in not_found.items():
                if not isinstance(values, list):
                    continue
                stats["not_found"] += len(values)
                room = NOT_FOUND_SAMPLE_CAP - len(stats["not_found_items"])
                if room <= 0:
                    continue
                for value in values[:room]:
                    item = value
                    # Only a bare episode entry needs recovering - a movie or
                    # show-level rejection already comes back with its own
                    # tmdb/imdb id, and a season/episode nested under "shows"
                    # keeps its parent's ids too (see the existing tests).
                    if kind == "episodes" and isinstance(value, dict) and not value.get("ids"):
                        if exact_index is None:
                            exact_index, season_index = _index_episode_shows(batch)
                        season_number = value.get("season")
                        episode_number = value.get("episode")
                        candidates = None
                        if season_number is not None and episode_number is not None:
                            candidates = exact_index.get((season_number, episode_number))
                        if not candidates and season_number is not None:
                            # No episode number in the echo either - every show
                            # in this batch with that season number is a
                            # possible match, not a confirmed one.
                            candidates = season_index.get(season_number)
                        if candidates:
                            item = {**value, "_candidate_show_tmdb_ids": sorted(set(candidates))}
                    # singular kind label ("movies" -> "movie"), item body as-is
                    stats["not_found_items"].append({"kind": str(kind).rstrip("s") or kind, "item": item})
        if on_batch is not None:
            await on_batch(batch_count)
    return stats


async def push_watched(
    api_key: str,
    payload: dict[str, list[dict[str, Any]]],
    *,
    on_batch: Callable[[int], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    return await _push("/sync/watched", api_key, payload, on_batch=on_batch)


async def remove_watched(api_key: str, payload: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    return await _push("/sync/watched/remove", api_key, payload)


async def push_collection(
    api_key: str,
    payload: dict[str, list[dict[str, Any]]],
    *,
    on_batch: Callable[[int], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    return await _push("/sync/collection", api_key, payload, on_batch=on_batch)


async def remove_collection(api_key: str, payload: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    return await _push("/sync/collection/remove", api_key, payload)


async def scrobble_movie(api_key: str, action: str, tmdb_id: int, progress: float | None = None) -> dict[str, Any]:
    """Start/pause/stop/clear a movie scrobble session on MDBList.

    ``progress`` is omitted for ``action="clear"``, which takes no progress value.
    A 404 on ``clear`` means there was no session to clear in the first place
    (e.g. a near-zero-progress stop that MDBList never turned into a resumable
    paused session) — that's the outcome we wanted, so it's treated as a no-op
    rather than an error."""
    body: dict[str, Any] = {"movie": {"ids": {"tmdb": tmdb_id}}}
    if progress is not None:
        body["progress"] = round(min(100.0, max(0.0, progress)), 1)
    ignore_statuses = {404} if action == "clear" else None
    return await _request(
        "POST", f"/scrobble/{action}", api_key, payload=body, ignore_statuses=ignore_statuses,
        rate_limit_retries=_SCROBBLE_RETRIES, max_wait=_SCROBBLE_MAX_WAIT,
    )


async def scrobble_episode(
    api_key: str,
    action: str,
    show_tmdb_id: int,
    season_number: int,
    episode_number: int,
    progress: float | None = None,
) -> dict[str, Any]:
    """Start/pause/stop/clear an episode scrobble session on MDBList.

    ``progress`` is omitted for ``action="clear"``, which takes no progress value.
    See scrobble_movie for why a 404 on ``clear`` is treated as a no-op."""
    body: dict[str, Any] = {
        "show": {"ids": {"tmdb": show_tmdb_id}, "season": season_number, "episode": episode_number},
    }
    if progress is not None:
        body["progress"] = round(min(100.0, max(0.0, progress)), 1)
    ignore_statuses = {404} if action == "clear" else None
    return await _request(
        "POST", f"/scrobble/{action}", api_key, payload=body, ignore_statuses=ignore_statuses,
        rate_limit_retries=_SCROBBLE_RETRIES, max_wait=_SCROBBLE_MAX_WAIT,
    )


async def push_ratings(
    api_key: str,
    payload: dict[str, list[dict[str, Any]]],
    *,
    on_batch: Callable[[int], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    return await _push("/sync/ratings", api_key, payload, on_batch=on_batch)


async def remove_ratings(api_key: str, payload: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    return await _push("/sync/ratings/remove", api_key, payload)


async def push_dropped(api_key: str, tmdb_id: int, dropped_at: str) -> dict[str, Any]:
    """Mark a show dropped on MDBList. Shows only - MDBList's /sync/dropped
    has no movie support."""
    return await push_dropped_batch(api_key, [tmdb_id], dropped_at)


async def push_dropped_batch(api_key: str, tmdb_ids: list[int], dropped_at: str) -> dict[str, Any]:
    """Mark multiple shows dropped on MDBList in one request (scheduled-push
    reconcile - see #329)."""
    if not tmdb_ids:
        return {}
    return await _request(
        "POST", "/sync/dropped", api_key,
        payload={"shows": [{"ids": {"tmdb": tid}, "dropped_at": dropped_at} for tid in tmdb_ids]},
    )


async def remove_dropped(api_key: str, tmdb_id: int) -> dict[str, Any]:
    """Undrop a show on MDBList (sets dropped_at back to null)."""
    return await _request(
        "POST", "/sync/dropped/remove", api_key,
        payload={"shows": [{"ids": {"tmdb": tmdb_id}}]},
    )


async def push_watchlist(
    api_key: str,
    payload: dict[str, list[dict[str, Any]]],
    *,
    on_batch: Callable[[int], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    return await _push("/watchlist/items/add", api_key, payload, on_batch=on_batch)


async def remove_watchlist(api_key: str, payload: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    return await _push("/watchlist/items/remove", api_key, payload)

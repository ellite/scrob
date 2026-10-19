"""WeTrakr API client.

Uses the Device Authentication flow — no redirect URI needed. Unlike Trakt/
Simkl, WeTrakr is a single Scrob-owned app: WETRAKR_CLIENT_ID is a public
identifier baked into the source (proven by PKCE/device-code possession, not
secrecy — the WeTrakr dev asked for this instead of every self-hosted
instance registering its own app), never a per-user UserSettings column.

API base: https://api.wetrakr.com
Every request needs wetrakr-api-key + wetrakr-api-version; user-scoped calls
also need Authorization: Bearer <access_token>.
"""

import asyncio
import logging
import time
import weakref
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

import httpx

logger = logging.getLogger(__name__)

WETRAKR_BASE = "https://api.wetrakr.com"
WETRAKR_CLIENT_ID = "e91f5041de433d4e7df4432cd1b04f94"
TIMEOUT = 30.0
# A write of up to 5,000 items can take the server well past TIMEOUT. A read
# timeout there leaves the caller not knowing whether it was applied.
WRITE_TIMEOUT = 120.0
PAGE_SIZE = 100
COMPACT_PAGE_SIZE = 5000
MAX_RETRIES = 4
MAX_RETRY_WAIT = 120.0

# One lock per access token: every call made on a user's behalf is serialised,
# so a user never has two requests in flight at once (the WeTrakr dev asked
# for this after bursts of parallel POST /sync/tracking exhausted their API).
# Weakly referenced so a rotated token's lock goes away with its last user.
_user_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()


def _user_lock(access_token: Optional[str]) -> asyncio.Lock:
    key = access_token or ""
    lock = _user_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[key] = lock
    return lock


class WeTrakrQuotaExceeded(Exception):
    """A daily quota is used up (the user's, or the whole app's). Retrying
    only spends more of it, so requests fail fast until it resets."""


class WeTrakrCommentsDisabled(Exception):
    """WeTrakr answered 403 COMMENTS_NOT_ENABLED: comments are switched on per
    app on request, and every comment route (reading included) is refused
    until then."""


# Quota blocks, as time.monotonic() deadlines. The app one covers every
# request; the per-user one is keyed by access token.
QUOTA_BLOCK_MAX = 3600.0
COMMENTS_BLOCK = 6 * 3600.0
_app_quota_blocked_until = 0.0
_user_quota_blocked_until: dict[str, float] = {}
_comments_blocked_until = 0.0


def _seconds_until_utc_midnight() -> float:
    now = datetime.now(timezone.utc)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - now).total_seconds()


def _raise_if_quota_blocked(access_token: Optional[str]) -> None:
    now = time.monotonic()
    if now < _app_quota_blocked_until:
        raise WeTrakrQuotaExceeded("WeTrakr daily app limit reached, waiting for it to reset")
    key = access_token or ""
    if now < _user_quota_blocked_until.get(key, 0.0):
        raise WeTrakrQuotaExceeded("WeTrakr daily quota reached for this account, waiting for it to reset")


def _block_quota(access_token: Optional[str], app_wide: bool, resp: httpx.Response) -> None:
    global _app_quota_blocked_until
    try:
        reset = float(resp.headers.get("X-Quota-Reset", ""))
    except ValueError:
        reset = _seconds_until_utc_midnight()
    # Probe again within the hour at most, in case the limit was raised.
    until = time.monotonic() + max(60.0, min(reset, QUOTA_BLOCK_MAX))
    if app_wide:
        _app_quota_blocked_until = until
    else:
        now = time.monotonic()
        for key in [k for k, v in _user_quota_blocked_until.items() if v <= now]:
            del _user_quota_blocked_until[key]
        _user_quota_blocked_until[access_token or ""] = until


def comments_blocked() -> bool:
    """True while WeTrakr is known to refuse comments for this app."""
    return time.monotonic() < _comments_blocked_until


def _raise_if_comments_blocked() -> None:
    if comments_blocked():
        raise WeTrakrCommentsDisabled("WeTrakr comments are not enabled for this app")


def _retry_after(resp: httpx.Response) -> float:
    try:
        return max(1.0, min(float(resp.headers.get("Retry-After", "5")), MAX_RETRY_WAIT))
    except ValueError:
        return 5.0


async def _send(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    access_token: Optional[str],
    **kwargs,
) -> httpx.Response:
    """One request, serialised per user, retried after the Retry-After delay on
    a concurrency/per-minute 429 (or 503). A daily-quota 429 is never retried:
    it blocks further requests until the quota resets. Raises on any other
    error status."""
    global _comments_blocked_until
    async with _user_lock(access_token):
        for attempt in range(MAX_RETRIES + 1):
            _raise_if_quota_blocked(access_token)
            resp = await client.request(method, f"{WETRAKR_BASE}{path}", headers=_headers(access_token), **kwargs)
            if resp.status_code == 429:
                # APP_QUOTA_EXCEEDED contains QUOTA_EXCEEDED, so check it first.
                body = resp.text
                if "APP_QUOTA_EXCEEDED" in body or "QUOTA_EXCEEDED" in body:
                    app_wide = "APP_QUOTA_EXCEEDED" in body
                    _block_quota(access_token, app_wide, resp)
                    raise WeTrakrQuotaExceeded(
                        f"WeTrakr {'app' if app_wide else 'account'} daily quota exceeded on {method} {path}"
                    )
            if resp.status_code in (429, 503) and attempt < MAX_RETRIES:
                wait = _retry_after(resp)
                logger.warning("WeTrakr %s %s answered %s, retrying in %.0fs", method, path, resp.status_code, wait)
                await asyncio.sleep(wait)
                continue
            if resp.status_code == 403 and (
                "COMMENTS_NOT_ENABLED" in resp.text or path.startswith("/sync/comments")
            ):
                _comments_blocked_until = time.monotonic() + COMMENTS_BLOCK
                raise WeTrakrCommentsDisabled("WeTrakr comments are not enabled for this app")
            resp.raise_for_status()
            return resp
    raise RuntimeError("unreachable")


def _iso_utc(value: datetime) -> str:
    """Format a datetime as ISO-8601 UTC.

    WatchEvent timestamps are stored as naive UTC values; a naive value is
    treated as already-UTC rather than the local timezone.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def _get_all_pages(
    client: httpx.AsyncClient,
    path: str,
    access_token: str,
    extra_params: dict[str, str] | None = None,
    *,
    compact: bool = False,
) -> list[dict]:
    items: list[dict] = []
    page = 1
    after: str | None = None
    while True:
        params = dict(extra_params or {})
        if compact:
            params.update({"compact": "true", "limit": str(COMPACT_PAGE_SIZE)})
            if after is not None:
                params["after"] = after
        else:
            params.update({"page": str(page), "limit": str(PAGE_SIZE)})
        response = await _send(client, "GET", path, access_token, params=params)
        page_items = response.json()
        if not isinstance(page_items, list):
            raise TypeError(f"WeTrakr {path} returned a non-list response")
        items.extend(page_items)

        if not page_items:
            return items
        if compact:
            next_cursor = response.headers.get("X-Pagination-Next")
            if not next_cursor:
                return items
            if next_cursor == after:
                raise RuntimeError(f"WeTrakr {path} returned the same pagination cursor twice")
            after = next_cursor
            continue
        try:
            page_count = int(response.headers.get("X-Pagination-Page-Count", page))
        except (TypeError, ValueError):
            page_count = page
        if page >= page_count:
            return items
        page += 1


def _log_unresolved(label: str, resp: httpx.Response) -> None:
    """The write endpoints answer 200 even when some items were refused: what
    could not be resolved comes back under notFound, and what was rejected
    (a missing status, "Already added!", ...) under errored. Without a look at
    those, a push can lose data without a trace."""
    try:
        data = resp.json()
    except ValueError:
        return
    if not isinstance(data, dict):
        return
    for key in ("notFound", "errored"):
        section = data.get(key)
        if not isinstance(section, dict):
            continue
        counts = {name: len(items) for name, items in section.items() if isinstance(items, list) and items}
        if counts:
            logger.warning("WeTrakr %s: %s %s", label, key, counts)


def _headers(access_token: Optional[str] = None) -> dict:
    h = {
        "Content-Type": "application/json",
        "wetrakr-api-key": WETRAKR_CLIENT_ID,
        "wetrakr-api-version": "1",
    }
    if access_token:
        h["Authorization"] = f"Bearer {access_token}"
    return h


class WeTrakrAuthError(Exception):
    """The device authorization flow ended in a terminal, non-retryable state
    (invalid code, already used, expired, or denied by the user). The message
    is safe to show — the caller must stop polling and start over."""


# ── Device Authentication ─────────────────────────────────────────────────────

async def start_device_auth() -> dict:
    """Start the device authentication flow.

    Returns: {device_code, user_code, verification_url, expires_in, interval}
    """
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{WETRAKR_BASE}/oauth/device/code",
            json={"client_id": WETRAKR_CLIENT_ID},
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


async def poll_device_token(device_code: str) -> Optional[dict]:
    """Poll for the device token.

    Returns the token dict on success, None while still pending (400) or
    when told to slow down (429) — the caller just keeps polling either way.
    Raises WeTrakrAuthError on a terminal failure (404/409/410/418).
    """
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{WETRAKR_BASE}/oauth/device/token",
            json={"code": device_code, "client_id": WETRAKR_CLIENT_ID},
            headers=_headers(),
        )
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 400:
            # 400 is "pending" while the user has not answered, but an
            # invalid_request 400 is a fault in this request: stop polling.
            try:
                error = (resp.json() or {}).get("error")
            except ValueError:
                error = None
            if error == "invalid_request":
                raise WeTrakrAuthError("WeTrakr rejected the authorization request. Please try connecting again.")
            return None
        if resp.status_code == 429:
            return None
        messages = {
            404: "Invalid device code. Please try connecting again.",
            409: "This code was already used to connect.",
            410: "The code expired before it was approved. Please try again.",
            418: "Access was denied on WeTrakr.",
        }
        raise WeTrakrAuthError(messages.get(resp.status_code, f"WeTrakr returned {resp.status_code}"))


async def refresh_access_token(refresh_token: str) -> dict:
    """Exchange a refresh token for a new access token.

    WeTrakr's own docs flag this: the initial device-token exchange returns
    the new refresh token as ``refresh_token``, but THIS endpoint returns it
    as ``new_refresh_token`` instead. Callers must read the latter.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{WETRAKR_BASE}/oauth/token/refresh",
            json={"refresh_token": refresh_token},
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


async def revoke_token(access_token: str, refresh_token: str) -> None:
    """Revoke a session (disconnect)."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            await client.post(
                f"{WETRAKR_BASE}/oauth/logout",
                json={"refresh_token": refresh_token},
                headers=_headers(access_token),
            )
    except Exception as exc:
        logger.warning("Failed to revoke WeTrakr token: %s", exc)


async def validate_token(access_token: str) -> bool:
    """Return True if the token is valid."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(f"{WETRAKR_BASE}/account/settings", headers=_headers(access_token))
            return resp.status_code == 200
    except Exception:
        return False


# ── User Data Fetching ────────────────────────────────────────────────────────

async def get_watched_history(
    access_token: str,
    target: Literal["movies", "episodes"],
    from_date: str | None = None,
) -> list[dict]:
    """Fetch plays changed after from_date, or every play on first sync.

    Compact cursor pages carry up to 5,000 rows and are the API's intended
    representation for sync clients.
    """
    params = {"from_date": from_date} if from_date else None
    async with httpx.AsyncClient(timeout=120.0) as client:
        return await _get_all_pages(
            client,
            f"/sync/tracking/watched/history/{target}",
            access_token,
            params,
            compact=True,
        )


async def get_ratings(
    access_token: str,
    target: Literal["movies", "shows"],
    from_date: str | None = None,
) -> list[dict]:
    """Fetch ratings changed after from_date, most recent first.

    Compact cursor pages carry up to 5,000 rows. Removed rows are included on
    incremental reads so callers can reconcile deletions.
    """
    params = {"from_date": from_date} if from_date else None
    async with httpx.AsyncClient(timeout=60.0) as client:
        return await _get_all_pages(
            client,
            f"/sync/ratings/{target}",
            access_token,
            params,
            compact=True,
        )


# ── Lists ──────────────────────────────────────────────────────────────────────

async def get_lists(access_token: str, from_date: str | None = None) -> list[dict]:
    """Fetch lists changed after from_date, or every list on first sync."""
    params = {"from_date": from_date} if from_date else None
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await _send(client, "GET", "/sync/lists", access_token, params=params)
        resp.raise_for_status()
        return resp.json()


async def create_list(access_token: str, name: str, description: Optional[str] = None) -> dict:
    """Create an empty list. Returns the new List object (carries its `id`)."""
    body: dict = {"name": name}
    if description:
        body["description"] = description
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await _send(client, "POST", "/sync/lists", access_token, json=body)
        resp.raise_for_status()
        return resp.json()


async def get_list_items(access_token: str, list_id: int) -> list[dict]:
    """Fetch every item in one of the user's lists, as full Media objects."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        return await _get_all_pages(
            client,
            f"/sync/lists/{list_id}/items",
            access_token,
        )


async def add_items_to_list(access_token: str, list_id: int, movies: list[int], shows: list[int]) -> None:
    """Add movies and/or shows to a list, by tmdb id, in one call.

    Idempotent server-side (adding an item already in the list is a no-op),
    so this is safe to call again on every push. Season/episode/person items
    aren't supported here — WeTrakr's list-items endpoint takes those by its
    own WeTrakr id, not by tmdb id.
    """
    if not movies and not shows:
        return
    body: dict = {}
    if movies:
        body["movies"] = [{"ids": {"tmdb": tmdb_id}} for tmdb_id in movies]
    if shows:
        body["shows"] = [{"ids": {"tmdb": tmdb_id}} for tmdb_id in shows]
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await _send(client, "POST", f"/sync/lists/{list_id}/items", access_token, json=body)
        _log_unresolved("list items push", resp)


# ── Comments ───────────────────────────────────────────────────────────────────

async def get_comments(
    access_token: str,
    target: Literal["movies", "shows", "seasons", "episodes"],
    from_date: str | None = None,
) -> list[dict]:
    """Fetch comments changed after from_date, newest first.

    Each entry carries its own id, `target` (movie/show/season/episode/...),
    text, spoiler and comment_added_at/comment_updated_at, plus a compact
    reference to what it's on under a key matching `target` — that reference
    carries only WeTrakr's own numeric id, not external ids (unlike every
    other "Media object" response in this API), so resolving it to a tmdb id
    needs a follow-up lookup via get_movie/get_show/get_season/get_episode.
    """
    _raise_if_comments_blocked()
    params = {"from_date": from_date} if from_date else None
    async with httpx.AsyncClient(timeout=60.0) as client:
        return await _get_all_pages(
            client,
            f"/sync/comments/{target}",
            access_token,
            params,
        )


async def write_comment(
    access_token: str,
    *,
    movie_tmdb_id: Optional[int] = None,
    show_tmdb_id: Optional[int] = None,
    text: str,
    spoiler: bool = False,
) -> dict:
    """Post a new comment on a movie or show, by tmdb id. Send exactly one of
    movie_tmdb_id/show_tmdb_id. Returns the created Comment (carries `id`).

    Season/episode comments aren't supported here — the write endpoint takes
    those by WeTrakr's own season/episode id, which isn't resolved yet (see
    routers/wetrakr.py's comment-target resolver, which only runs pull-side).
    There's no update endpoint in this API — a comment can only be created or
    deleted, never edited in place.
    """
    if not movie_tmdb_id and not show_tmdb_id:
        raise ValueError("write_comment requires movie_tmdb_id or show_tmdb_id")
    _raise_if_comments_blocked()
    body: dict = {"text": text, "spoiler": spoiler}
    if movie_tmdb_id:
        body["movie"] = {"ids": {"tmdb": movie_tmdb_id}}
    else:
        body["show"] = {"ids": {"tmdb": show_tmdb_id}}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await _send(client, "POST", "/sync/comments", access_token, json=body)
        resp.raise_for_status()
        return resp.json()


# ── Metadata lookups (public reads — app key only, no user token) ────────────

async def get_movie(wetrakr_id: int) -> dict:
    """Full metadata for one movie by its WeTrakr id — carries external ids
    under `ids`, unlike a comment's compact nested reference."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await _send(client, "GET", f"/movies/{wetrakr_id}", None)
        return resp.json()


async def get_show(wetrakr_id: int) -> dict:
    """Full metadata for one show by its WeTrakr id."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await _send(client, "GET", f"/shows/{wetrakr_id}", None)
        return resp.json()


async def get_season(wetrakr_id: int) -> dict:
    """One season by its own WeTrakr id. Embeds a compact show object
    (`show.ids.tmdb`) and the season's own `number`. Confirmed against the
    live API — the docs' textual field descriptions here are unreliable."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await _send(client, "GET", f"/seasons/{wetrakr_id}", None)
        return resp.json()


async def get_episode(wetrakr_id: int) -> dict:
    """One episode by its own WeTrakr id. Embeds compact show and season
    objects. Unconfirmed against a live example (unlike get_season) — the
    exact field names for season/episode number are a best-effort guess;
    callers must tolerate a resolution failure gracefully."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await _send(client, "GET", f"/episodes/{wetrakr_id}", None)
        return resp.json()


# ── Outbound Push ─────────────────────────────────────────────────────────────

async def add_to_watched_batch(
    access_token: str,
    movies: list[tuple[int, Optional[datetime]]],
    episodes: list[tuple[int, int, int, Optional[datetime]]],
    allow_rewatch: bool = False,
) -> None:
    """Mark multiple movies and/or episodes as watched on WeTrakr in one call.

    movies: list of (tmdb_id, watched_at)
    episodes: list of (show_tmdb_id, season_number, episode_number, watched_at)
    A missing watched_at is sent as tracked_at_unknown rather than inventing
    a "now" date.
    """
    if not movies and not episodes:
        return
    # allow_rewatch defaults to false: a title the account already has comes back in
    # errored as "Already watched!" instead of becoming another play. Without
    # it every re-send (and every undated item) logged a brand-new play.
    body: dict = {"allow_rewatch": allow_rewatch}
    if movies:
        body["movies"] = []
        for tmdb_id, watched_at in movies:
            item: dict = {"ids": {"tmdb": tmdb_id}, "status": "watched"}
            if watched_at is not None:
                item["tracked_at"] = _iso_utc(watched_at)
            else:
                item["tracked_at_unknown"] = True
            body["movies"].append(item)
    if episodes:
        shows_map: dict[int, dict[int, list[tuple[int, Optional[datetime]]]]] = {}
        for show_tmdb_id, season, ep_num, watched_at in episodes:
            shows_map.setdefault(show_tmdb_id, {}).setdefault(season, []).append((ep_num, watched_at))
        body["shows"] = [
            {
                "ids": {"tmdb": show_tmdb_id},
                "status": "watching",
                "seasons": [
                    {
                        "number": season,
                        "episodes": [
                            # status is required on every item, nested
                            # episodes included (an item without one comes
                            # back in errored and nothing is written).
                            (
                                {"number": n, "status": "watched", "tracked_at": _iso_utc(watched_at)}
                                if watched_at is not None
                                else {"number": n, "status": "watched", "tracked_at_unknown": True}
                            )
                            for n, watched_at in eps
                        ],
                    }
                    for season, eps in seasons.items()
                ],
            }
            for show_tmdb_id, seasons in shows_map.items()
        ]
    async with httpx.AsyncClient(timeout=WRITE_TIMEOUT) as client:
        resp = await _send(client, "POST", "/sync/tracking", access_token, json=body)
        _log_unresolved("watched push", resp)


async def set_ratings_batch(
    access_token: str,
    movie_ratings: list[tuple[int, float]],
    show_ratings: list[tuple[int, float]],
) -> None:
    """Rate multiple movies and/or shows on WeTrakr in one call (1-10 scale).

    Season/episode ratings aren't pushed: WeTrakr's /sync/ratings body takes
    them by its own numeric season/episode id, not by tmdb id, and resolving
    those ids isn't wired up yet — only movie/show ratings push.
    """
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
    async with httpx.AsyncClient(timeout=WRITE_TIMEOUT) as client:
        resp = await _send(client, "POST", "/sync/ratings", access_token, json=body)
        _log_unresolved("ratings push", resp)


# ── Change detection ──────────────────────────────────────────────────────────

async def get_last_activities(access_token: str) -> dict:
    """GET /sync/last_activities: timestamps of the last change per section.
    One cheap call that says whether anything moved since the last sync."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await _send(client, "GET", "/sync/last_activities", access_token)
        data = resp.json()
        return data if isinstance(data, dict) else {}

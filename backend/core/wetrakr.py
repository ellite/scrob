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

import logging
from datetime import datetime, timezone
from typing import Literal, Optional

import httpx

logger = logging.getLogger(__name__)

WETRAKR_BASE = "https://api.wetrakr.com"
WETRAKR_CLIENT_ID = "e91f5041de433d4e7df4432cd1b04f94"
TIMEOUT = 30.0
PAGE_SIZE = 100


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
    headers: dict,
    extra_params: dict[str, str] | None = None,
) -> list[dict]:
    items: list[dict] = []
    page = 1
    while True:
        params = dict(extra_params or {})
        params.update({"page": page, "limit": PAGE_SIZE})
        response = await client.get(f"{WETRAKR_BASE}{path}", headers=headers, params=params)
        response.raise_for_status()
        page_items = response.json()
        if not isinstance(page_items, list):
            raise TypeError(f"WeTrakr {path} returned a non-list response")
        items.extend(page_items)

        if not page_items:
            return items
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

async def get_watched_history(access_token: str, target: Literal["movies", "episodes"]) -> list[dict]:
    """Fetch every play (rewatches included), newest first.

    Each entry has: id (play id), watched_at, watched_at_unknown, and either
    `movie` or `episode` (episode carries season_number, number, and the
    parent show as {id, title, ids}).
    """
    async with httpx.AsyncClient(timeout=120.0) as client:
        return await _get_all_pages(
            client,
            f"/sync/tracking/watched/history/{target}",
            _headers(access_token),
        )


async def get_ratings(access_token: str, target: Literal["movies", "shows"]) -> list[dict]:
    """Fetch every rating for one target, most recent first.

    Each entry is a media object with the user's rating under
    interactions.user.rating (1-10) and the date in rated_at.
    """
    async with httpx.AsyncClient(timeout=60.0) as client:
        return await _get_all_pages(
            client,
            f"/sync/ratings/{target}",
            _headers(access_token),
        )


# ── Lists ──────────────────────────────────────────────────────────────────────

async def get_lists(access_token: str) -> list[dict]:
    """Fetch every list the user owns."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{WETRAKR_BASE}/sync/lists", headers=_headers(access_token))
        resp.raise_for_status()
        return resp.json()


async def create_list(access_token: str, name: str, description: Optional[str] = None) -> dict:
    """Create an empty list. Returns the new List object (carries its `id`)."""
    body: dict = {"name": name}
    if description:
        body["description"] = description
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(f"{WETRAKR_BASE}/sync/lists", json=body, headers=_headers(access_token))
        resp.raise_for_status()
        return resp.json()


async def get_list_items(access_token: str, list_id: int) -> list[dict]:
    """Fetch every item in one of the user's lists, as full Media objects."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        return await _get_all_pages(
            client,
            f"/sync/lists/{list_id}/items",
            _headers(access_token),
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
        resp = await client.post(
            f"{WETRAKR_BASE}/sync/lists/{list_id}/items",
            json=body,
            headers=_headers(access_token),
        )
        resp.raise_for_status()
        _log_unresolved("list items push", resp)


# ── Comments ───────────────────────────────────────────────────────────────────

async def get_comments(access_token: str, target: Literal["movies", "shows", "seasons", "episodes"]) -> list[dict]:
    """Fetch the comments the user wrote for one target, newest first.

    Each entry carries its own id, `target` (movie/show/season/episode/...),
    text, spoiler and comment_added_at/comment_updated_at, plus a compact
    reference to what it's on under a key matching `target` — that reference
    carries only WeTrakr's own numeric id, not external ids (unlike every
    other "Media object" response in this API), so resolving it to a tmdb id
    needs a follow-up lookup via get_movie/get_show/get_season/get_episode.
    """
    async with httpx.AsyncClient(timeout=60.0) as client:
        return await _get_all_pages(
            client,
            f"/sync/comments/{target}",
            _headers(access_token),
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
    body: dict = {"text": text, "spoiler": spoiler}
    if movie_tmdb_id:
        body["movie"] = {"ids": {"tmdb": movie_tmdb_id}}
    else:
        body["show"] = {"ids": {"tmdb": show_tmdb_id}}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(f"{WETRAKR_BASE}/sync/comments", json=body, headers=_headers(access_token))
        resp.raise_for_status()
        return resp.json()


# ── Metadata lookups (public reads — app key only, no user token) ────────────

async def get_movie(wetrakr_id: int) -> dict:
    """Full metadata for one movie by its WeTrakr id — carries external ids
    under `ids`, unlike a comment's compact nested reference."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(f"{WETRAKR_BASE}/movies/{wetrakr_id}", headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def get_show(wetrakr_id: int) -> dict:
    """Full metadata for one show by its WeTrakr id."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(f"{WETRAKR_BASE}/shows/{wetrakr_id}", headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def get_season(wetrakr_id: int) -> dict:
    """One season by its own WeTrakr id. Embeds a compact show object
    (`show.ids.tmdb`) and the season's own `number`. Confirmed against the
    live API — the docs' textual field descriptions here are unreliable."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(f"{WETRAKR_BASE}/seasons/{wetrakr_id}", headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def get_episode(wetrakr_id: int) -> dict:
    """One episode by its own WeTrakr id. Embeds compact show and season
    objects. Unconfirmed against a live example (unlike get_season) — the
    exact field names for season/episode number are a best-effort guess;
    callers must tolerate a resolution failure gracefully."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(f"{WETRAKR_BASE}/episodes/{wetrakr_id}", headers=_headers())
        resp.raise_for_status()
        return resp.json()


# ── Outbound Push ─────────────────────────────────────────────────────────────

async def add_to_watched_batch(
    access_token: str,
    movies: list[tuple[int, Optional[datetime]]],
    episodes: list[tuple[int, int, int, Optional[datetime]]],
) -> None:
    """Mark multiple movies and/or episodes as watched on WeTrakr in one call.

    movies: list of (tmdb_id, watched_at)
    episodes: list of (show_tmdb_id, season_number, episode_number, watched_at)
    A missing watched_at is sent as tracked_at_unknown rather than inventing
    a "now" date.
    """
    if not movies and not episodes:
        return
    body: dict = {}
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
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{WETRAKR_BASE}/sync/tracking",
            json=body,
            headers=_headers(access_token),
        )
        resp.raise_for_status()
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
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{WETRAKR_BASE}/sync/ratings",
            json=body,
            headers=_headers(access_token),
        )
        resp.raise_for_status()
        _log_unresolved("ratings push", resp)

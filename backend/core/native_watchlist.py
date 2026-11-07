"""Scrob's own watchlist, and the reconcile every service runs against it.

Each user has exactly one native watchlist (List.is_watchlist). Plex, Trakt,
MDBList and Simkl all sync their watchlist with it, each against its own
baseline (core/watchlist_reconcile.py), instead of filling a managed list of
their own. That makes Scrob the hub: a title added on Plex lands here and
goes out to Trakt and MDBList on their next reconcile, and a removal on any
side propagates the same way.

Only whole movies and shows take part - watchlists on the services have no
season granularity, so season items on the native list are left alone.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable, Mapping, Optional

from sqlalchemy import delete, func, literal_column, null, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.watchlist_reconcile import (
    compute_new_baseline,
    compute_pull_only_baseline,
    media_key,
    plan_watchlist_reconcile,
)
from models.base import MediaType
from models.lists import List as ListModel, ListItem
from models.media import Media

logger = logging.getLogger(__name__)

WATCHLIST_NAME = "Watchlist"

# Markers of the per-service managed lists that came before the native
# watchlist. The migration folds them into it; nothing creates them any more.
LEGACY_WATCHLIST_TRAKT_SLUGS = (
    "__watchlist__",
    "__watchlist_movies__",
    "__watchlist_shows__",
    "__plex_watchlist__",
    "__simkl_watchlist__",
)
LEGACY_WATCHLIST_MDBLIST_SLUG = "__watchlist__"

# Every reconcile for a user edits the same list, whichever service it is
# for, so they take turns. In-process only, like the Plex connection locks.
_user_locks: dict[int, asyncio.Lock] = {}


def watchlist_lock(user_id: int) -> asyncio.Lock:
    return _user_locks.setdefault(user_id, asyncio.Lock())


def key_for_media(media_type: MediaType, tmdb_id: Optional[int]) -> Optional[str]:
    if tmdb_id is None:
        return None
    if media_type == MediaType.movie:
        return media_key("movie", tmdb_id)
    if media_type == MediaType.series:
        return media_key("show", tmdb_id)
    return None


def split_key(key: str) -> tuple[str, int]:
    """"movie:603" -> ("movie", 603)."""
    kind, _, raw_id = key.partition(":")
    return kind, int(raw_id)


async def get_watchlist(db: AsyncSession, user_id: int) -> Optional[ListModel]:
    result = await db.execute(
        select(ListModel).where(ListModel.user_id == user_id, ListModel.is_watchlist.is_(True))
    )
    return result.scalar_one_or_none()


async def get_or_create_watchlist(db: AsyncSession, user_id: int) -> ListModel:
    watchlist = await get_watchlist(db, user_id)
    if watchlist:
        return watchlist
    try:
        async with db.begin_nested():
            watchlist = ListModel(user_id=user_id, name=WATCHLIST_NAME, is_watchlist=True)
            db.add(watchlist)
            await db.flush()
        return watchlist
    except IntegrityError:
        # A concurrent request created it first (uq_lists_user_watchlist).
        watchlist = await get_watchlist(db, user_id)
        if watchlist is None:
            raise
        return watchlist


async def reset_watchlist_baselines(db: AsyncSession, user_id: int) -> None:
    """Forget every service's watchlist baseline, so the next reconcile of
    each is a purely additive first sync again."""
    from models.connections import MediaServerConnection
    from models.users import UserSettings

    await db.execute(
        update(UserSettings).where(UserSettings.user_id == user_id).values(
            trakt_watchlist_synced_keys=null(),
            mdblist_watchlist_synced_keys=null(),
            simkl_watchlist_synced_keys=null(),
        )
    )
    await db.execute(
        update(MediaServerConnection).where(MediaServerConnection.user_id == user_id).values(
            plex_watchlist_synced_keys=null(),
        )
    )


async def load_watchlist_keys(db: AsyncSession, list_id: int) -> dict[str, tuple[int, str]]:
    """Typed key -> (media_id, title) for the list's whole movies and shows."""
    rows = await db.execute(
        select(Media.id, Media.media_type, Media.tmdb_id, Media.title)
        .join(ListItem, ListItem.media_id == Media.id)
        .where(ListItem.list_id == list_id, ListItem.season_number.is_(None))
    )
    keys: dict[str, tuple[int, str]] = {}
    for media_id, media_type, tmdb_id, title in rows:
        key = key_for_media(media_type, tmdb_id)
        if key:
            keys[key] = (media_id, title)
    return keys


async def add_media_to_watchlist(db: AsyncSession, list_id: int, media_id: int) -> None:
    """Idempotent against a concurrent add from the lists UI or another
    service's reconcile. See the Plex reconcile in routers/sync.py for why
    the -1 is a literal and the conflict target an expression list."""
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
        stmt = insert(ListItem).values(list_id=list_id, media_id=media_id).on_conflict_do_nothing(
            index_elements=[ListItem.list_id, ListItem.media_id, func.coalesce(ListItem.season_number, literal_column("-1"))]
        )
    else:
        from sqlalchemy.dialects.sqlite import insert
        stmt = insert(ListItem).values(list_id=list_id, media_id=media_id).on_conflict_do_nothing()
    await db.execute(stmt)


# key -> coroutine that finds or creates the Media for a remote item. Only
# called for keys that are actually imported, so a sync doesn't hit TMDB for
# titles that are already on the list.
MediaResolver = Callable[[], Awaitable[Optional[Media]]]
# Pushes keys to the service, returns the ones that failed.
RemotePush = Callable[[list[str], dict[str, tuple[int, str]]], Awaitable[set[str]]]


@dataclass
class WatchlistSyncResult:
    added: int = 0
    removed: int = 0
    pushed_added: int = 0
    pushed_removed: int = 0
    suppressed: bool = False
    # What to persist as the service's next baseline. None means "leave it".
    baseline: Optional[list[str]] = None


async def sync_service_watchlist(
    db: AsyncSession,
    user_id: int,
    *,
    service: str,
    remote: Mapping[str, MediaResolver],
    baseline: Optional[Iterable[str]],
    pull_enabled: bool,
    push_enabled: bool,
    push_add: Optional[RemotePush] = None,
    push_remove: Optional[RemotePush] = None,
) -> WatchlistSyncResult:
    """Reconcile one service's watchlist (`remote`, already fetched in full)
    with the native watchlist. Local changes are flushed, not committed - the
    caller's transaction owns them, and it persists result.baseline.

    The caller must hold watchlist_lock(user_id)."""
    result = WatchlistSyncResult()
    if not (pull_enabled or push_enabled):
        return result

    watchlist = await get_watchlist(db, user_id)
    if watchlist is None:
        # Gone with "clear all lists". Without this reset every baseline key
        # would read as a local deletion and wipe the service's watchlist.
        baseline = None
        watchlist = await get_or_create_watchlist(db, user_id)
    local = await load_watchlist_keys(db, watchlist.id)

    plan = plan_watchlist_reconcile(
        local.keys(), remote.keys(), baseline,
        pull_enabled=pull_enabled, push_enabled=push_enabled,
    )
    if plan.suppressed:
        logger.warning(
            "%s watchlist reconcile suppressed for user %s: %s. Not acting on a fetch that may be "
            "truncated. If the removals are real, remove the items from the watchlist in Scrob.",
            service, user_id, plan.suppressed_reason,
        )
        result.suppressed = True
        return result

    failed_add_local: set[str] = set()
    for key in sorted(plan.add_local):
        try:
            # A savepoint per item, so one failure doesn't abort the caller's
            # shared transaction.
            async with db.begin_nested():
                media = await remote[key]()
                if media is None:
                    failed_add_local.add(key)
                    continue
                await add_media_to_watchlist(db, watchlist.id, media.id)
            result.added += 1
        except Exception as exc:
            logger.warning("Failed to import %s watchlist item %s: %s", service, key, exc)
            failed_add_local.add(key)

    if plan.remove_local:
        remove_ids = [local[key][0] for key in plan.remove_local if key in local]
        if remove_ids:
            await db.execute(
                delete(ListItem).where(
                    ListItem.list_id == watchlist.id,
                    ListItem.media_id.in_(remove_ids),
                    ListItem.season_number.is_(None),
                )
            )
        result.removed = len(remove_ids)
    await db.flush()

    failed_push_add: set[str] = set()
    failed_push_remove: set[str] = set()
    if plan.push_add:
        if push_add is None:
            failed_push_add = set(plan.push_add)
        else:
            failed_push_add = await push_add(sorted(plan.push_add), local)
        result.pushed_added = len(plan.push_add) - len(failed_push_add)
    if plan.push_remove:
        if push_remove is None:
            failed_push_remove = set(plan.push_remove)
        else:
            failed_push_remove = await push_remove(sorted(plan.push_remove), local)
        result.pushed_removed = len(plan.push_remove) - len(failed_push_remove)

    final_local = (set(local) | (set(plan.add_local) - failed_add_local)) - set(plan.remove_local)
    if push_enabled:
        result.baseline = compute_new_baseline(
            final_local, failed_push_add=failed_push_add, failed_push_remove=failed_push_remove,
        )
    else:
        result.baseline = compute_pull_only_baseline(remote.keys(), failed_add_local=failed_add_local)
    return result

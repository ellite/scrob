"""Shared duplicate-watch detection for WatchEvent creation.

Every path that can add a WatchEvent (real-time webhooks, Trakt/Simkl/MDBList/
Scrob-format imports, Jellyfin/Emby bulk sync, manual "mark as watched") should
route through find_duplicate_watch_event before inserting, so a single per-user
setting (Settings > duplicate_watch_window_minutes, see #390) governs "is this
the same play as one we already have" everywhere, regardless of which source
produced either row.
"""

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from models.events import WatchEvent
from models.media import Media, MediaType
from models.users import UserSettings
from core.timeutil import utcnow

# Floor under which the per-user setting can't lower duplicate protection -
# this is the minimum needed to stop a single viewing session from producing
# more than one WatchEvent (e.g. Plex sending both media.scrobble and
# media.stop for the same session, see #82/#83). The user's configured value
# only ever widens the window beyond this, never narrows it.
DEFAULT_DEDUP_WINDOW_MINUTES = 5


async def get_dedup_window_minutes(db: AsyncSession, user_id: int) -> int:
    """The effective duplicate-watch window for this user, in minutes. Prefer
    dedup_window_from_settings when the caller already has a loaded
    UserSettings, to avoid a redundant query."""
    result = await db.execute(
        select(UserSettings.duplicate_watch_window_minutes).where(UserSettings.user_id == user_id)
    )
    configured = result.scalar_one_or_none()
    return max(DEFAULT_DEDUP_WINDOW_MINUTES, configured or 0)


def dedup_window_from_settings(settings: Optional[UserSettings]) -> int:
    """Same as get_dedup_window_minutes, from an already-loaded UserSettings -
    for hot paths (e.g. real-time webhooks) that load it anyway. getattr'd
    defensively since some callers pass a lightweight settings stand-in
    (e.g. tests) that may not carry every UserSettings column."""
    configured = getattr(settings, "duplicate_watch_window_minutes", None) if settings else None
    return max(DEFAULT_DEDUP_WINDOW_MINUTES, configured or 0)


async def _acquire_dedup_lock(db: AsyncSession, user_id: int, media_id: int) -> None:
    """Serializes concurrent duplicate-checks for this (user, media) within
    the caller's transaction, so two near-simultaneous events for the same
    play - e.g. a media server double-firing a webhook a few seconds apart -
    can't both pass the check-then-insert race before either commits (#440).
    A transaction-scoped Postgres advisory lock: it blocks a concurrent
    caller until this one's next commit/rollback, at which point that
    caller's own duplicate check will see the row this one just wrote. No-op
    on the sqlite engine some unit tests substitute for Postgres, since a
    single sqlite connection can't race with itself. get_bind is fetched
    defensively since some callers pass a lightweight fake DB double (unit
    tests) that doesn't implement it at all."""
    get_bind = getattr(db, "get_bind", None)
    bind = get_bind() if callable(get_bind) else None
    if bind is None or bind.dialect.name != "postgresql":
        return
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:user_id, :media_id)"),
        {"user_id": user_id, "media_id": media_id},
    )


def _same_episode_media_ids(media_id: int):
    """Subquery of every Media id that is the same episode as media_id: same
    show, season and episode number. The same episode can exist as more than
    one Media row (e.g. a webhook and a Trakt import resolving it through
    different ids, #527), and a play of either is a play of the other, so the
    duplicate check has to look across all of them. Empty for movies and for
    episodes without a show, which only ever match by media_id."""
    anchor = aliased(Media)
    return (
        select(Media.id)
        .join(
            anchor,
            and_(
                anchor.id == media_id,
                anchor.media_type == MediaType.episode,
                Media.show_id == anchor.show_id,
                Media.season_number == anchor.season_number,
                Media.episode_number == anchor.episode_number,
            ),
        )
    )


async def find_duplicate_watch_event(
    db: AsyncSession,
    user_id: int,
    media_id: int,
    candidate_time: Optional[datetime],
    window_minutes: int,
    *,
    exclude_id: Optional[int] = None,
) -> Optional[WatchEvent]:
    """An existing WatchEvent for (user_id, media_id) whose effective time
    (watched_at, or created_at when watched_at is unknown - same convention as
    events.py) falls within window_minutes of candidate_time, regardless of
    which source produced it - or None if there isn't one. candidate_time
    defaults to "now" to match how an unknown-dated play is itself recorded.

    Only completed rows count: every caller is about to insert a completed
    play, and an in-progress row (completed=False, e.g. a Jellyfin/Emby pull
    reporting PlayCount > 0 mid-playback, see #253) is not the same play
    finished, so it must not swallow the real completion (#481).

    Callers that follow a None result by inserting the new WatchEvent get
    race protection for free: see _acquire_dedup_lock."""
    if not window_minutes:
        return None
    await _acquire_dedup_lock(db, user_id, media_id)
    at = candidate_time or utcnow()
    delta = timedelta(minutes=window_minutes)
    effective_time = func.coalesce(WatchEvent.watched_at, WatchEvent.created_at)
    query = select(WatchEvent).where(
        WatchEvent.user_id == user_id,
        or_(WatchEvent.media_id == media_id, WatchEvent.media_id.in_(_same_episode_media_ids(media_id))),
        WatchEvent.completed.is_(True),
        effective_time.between(at - delta, at + delta),
    )
    if exclude_id is not None:
        query = query.where(WatchEvent.id != exclude_id)
    result = await db.execute(query.order_by(WatchEvent.id).limit(1))
    return result.scalar_one_or_none()


async def load_existing_watch_times(db: AsyncSession, user_id: int) -> dict[int, list[datetime]]:
    """Preload every existing WatchEvent's effective time for this user,
    grouped by media_id - for bulk import loops (Trakt/Simkl/etc.) that would
    otherwise run one duplicate-check query per item. Pair with
    is_duplicate_watch_time.

    An episode that exists as several Media rows (same show/season/episode,
    #527) shares one list of times across all of them, so a play recorded
    against one row is seen as a duplicate when the import resolves the same
    episode to another."""
    result = await db.execute(
        select(WatchEvent.media_id, func.coalesce(WatchEvent.watched_at, WatchEvent.created_at))
        .where(WatchEvent.user_id == user_id)
    )
    times: dict[int, list[datetime]] = {}
    for media_id, at in result.all():
        times.setdefault(media_id, []).append(at)
    if not times:
        return times

    # Every Media row sharing an episode position with a watched episode.
    watched = select(Media.show_id, Media.season_number, Media.episode_number).where(
        Media.id.in_(times.keys()),
        Media.media_type == MediaType.episode,
        Media.show_id.isnot(None),
        Media.season_number.isnot(None),
        Media.episode_number.isnot(None),
    ).distinct().subquery()
    siblings = await db.execute(
        select(Media.id, Media.show_id, Media.season_number, Media.episode_number)
        .join(
            watched,
            and_(
                Media.show_id == watched.c.show_id,
                Media.season_number == watched.c.season_number,
                Media.episode_number == watched.c.episode_number,
            ),
        )
        .where(Media.media_type == MediaType.episode)
    )
    groups: dict[tuple, list[int]] = {}
    for media_id, show_id, season, episode in siblings.all():
        groups.setdefault((show_id, season, episode), []).append(media_id)
    for ids in groups.values():
        if len(ids) < 2:
            continue
        merged = [t for i in ids for t in times.get(i, ())]
        for i in ids:
            times[i] = list(merged)
    return times


def is_duplicate_watch_time(
    existing_times: dict[int, list[datetime]],
    media_id: int,
    candidate_time: Optional[datetime],
    window_minutes: int,
) -> bool:
    """In-memory counterpart to find_duplicate_watch_event, for loops that
    preloaded existing times with load_existing_watch_times."""
    if not window_minutes:
        return False
    at = candidate_time or utcnow()
    tolerance = timedelta(minutes=window_minutes).total_seconds()
    return any(
        abs((existing_at - at).total_seconds()) <= tolerance
        for existing_at in existing_times.get(media_id, ())
    )

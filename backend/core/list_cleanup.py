"""Remove newly watched items from opted-in personal lists (#338).

A watch counts when it is newer than the item's cutoff: the moment the
setting was turned on for the list, or - when the list opts into earlier
watches - the moment the item was added. Movies need one such watch; a
series or season needs every released episode watched, the newest watch
past the cutoff. Each list is processed on its own and every IN-list is
chunked, so a large watchlist never exceeds the database parameter limit.

Every removal is recorded in list_item_removals for RETENTION, so it can be
restored; all removals of one run share removed_at and form a batch.
"""

from datetime import date, timedelta

from sqlalchemy import delete, select, func, or_
from sqlalchemy.orm import selectinload

from core.timeutil import utcnow
from models.base import MediaType
from models.events import WatchEvent
from models.lists import List, ListItem, ListItemRemoval
from models.media import Media
from models.show import Show

# Well below asyncpg's 32,767 bind parameters per statement.
CHUNK = 1000
# How long automatically removed items stay restorable.
RETENTION = timedelta(days=7)


def _chunks(values):
    values = list(values)
    for start in range(0, len(values), CHUNK):
        yield values[start:start + CHUNK]


def _required_episode_ids(item, shows, episodes):
    media = item.media
    if media.media_type != MediaType.series:
        return [media.id]
    show = shows.get(("tmdb", media.tmdb_id)) if media.tmdb_id else shows.get(("tvdb", media.tvdb_id))
    if not show:
        return []
    seasons = (show.tmdb_data or {}).get("seasons") or []
    seasons = [s for s in seasons if s.get("season_number") == item.season_number] if item.season_number is not None else [s for s in seasons if (s.get("season_number") or 0) > 0]
    required = set()
    for season in seasons:
        count = season.get("episode_count")
        if not isinstance(count, int) or count < 0:
            return []
        required.update((season["season_number"], n) for n in range(1, count + 1))
    local = [e for e in episodes.get(show.id, [])
             if e.season_number == item.season_number] if item.season_number is not None else [e for e in episodes.get(show.id, []) if (e.season_number or 0) > 0]
    positions = {(e.season_number, e.episode_number) for e in local}
    # Missing, duplicate or mismatched canonical positions must never delete a series.
    if not required or positions != required or len(local) != len(positions):
        return []
    # Announced episodes cannot be watched yet; an unknown date stays required.
    today = date.today().isoformat()
    return [e.id for e in local if not (e.release_date and e.release_date > today)]


def _cutoff(lst, item):
    if lst.auto_remove_include_earlier:
        return item.added_at
    return max(item.added_at, lst.auto_remove_since)


async def _prune_list(db, lst, pending_pushes, removed_at):
    if lst.auto_remove_since is None:
        # Enabled without a start time (e.g. set directly in the database):
        # start counting now instead of judging the whole history.
        lst.auto_remove_since = utcnow()
        return 0
    items = (await db.execute(select(ListItem).where(ListItem.list_id == lst.id)
                              .options(selectinload(ListItem.media)))).scalars().all()
    if not items:
        return 0
    series = [i.media for i in items if i.media.media_type == MediaType.series]
    tmdb_ids = {m.tmdb_id for m in series if m.tmdb_id}
    tvdb_ids = {m.tvdb_id for m in series if not m.tmdb_id and m.tvdb_id}
    shows, episodes = {}, {}
    for ids, column in ((tmdb_ids, Show.tmdb_id), (tvdb_ids, Show.tvdb_id)):
        for chunk in _chunks(ids):
            for show in (await db.execute(select(Show).where(column.in_(chunk)))).scalars().all():
                shows[("tmdb", show.tmdb_id)] = show
                shows[("tvdb", show.tvdb_id)] = show
    show_ids = {show.id for show in shows.values()}
    for chunk in _chunks(show_ids):
        rows = (await db.execute(select(Media).where(
            Media.show_id.in_(chunk), Media.media_type == MediaType.episode))).scalars().all()
        for episode in rows:
            episodes.setdefault(episode.show_id, []).append(episode)

    required = {i.id: _required_episode_ids(i, shows, episodes) for i in items}
    media_ids = {media_id for ids in required.values() for media_id in ids}
    # Per episode/movie: watched at all (an undated watch counts as watched)
    # and the newest dated watch.
    watched, latest = set(), {}
    for chunk in _chunks(media_ids):
        rows = (await db.execute(select(WatchEvent.media_id, func.max(WatchEvent.watched_at)).where(
            WatchEvent.user_id == lst.user_id, WatchEvent.media_id.in_(chunk),
            WatchEvent.completed == True,
            or_(WatchEvent.watched_at.is_(None), WatchEvent.watched_at <= utcnow()),
        ).group_by(WatchEvent.media_id))).all()
        for media_id, last in rows:
            watched.add(media_id)
            if last is not None:
                latest[media_id] = last

    removed = 0
    for item in items:
        ids = required[item.id]
        if not ids or not all(media_id in watched for media_id in ids):
            continue
        newest = max((latest[media_id] for media_id in ids if media_id in latest), default=None)
        if newest is None or newest <= _cutoff(lst, item):
            continue
        if pending_pushes is not None and (lst.is_watchlist or lst.trakt_slug):
            slug = None if lst.is_watchlist else lst.trakt_slug
            pending_pushes.append((lst.user_id, slug, item.media, item.season_number))
        db.add(ListItemRemoval(user_id=lst.user_id, list_id=lst.id, media_id=item.media_id,
                               season_number=item.season_number, added_at=item.added_at,
                               sort_order=item.sort_order, notes=item.notes, removed_at=removed_at))
        await db.delete(item)
        lst.updated_at = utcnow()
        removed += 1
    return removed


async def prune_watched_list_items(db, pending_pushes=None):
    run_at = utcnow()
    await db.execute(delete(ListItemRemoval).where(ListItemRemoval.removed_at < run_at - RETENTION))
    lists = (await db.execute(select(List).where(
        List.auto_remove_watched == True,
    ).with_for_update(skip_locked=True))).scalars().all()
    removed = 0
    for lst in lists:
        removed += await _prune_list(db, lst, pending_pushes, run_at)
    return removed


async def restore_removals(db, entries):
    """Put trashed items back on their lists and drop the trash entries.

    Returns (restored, skipped, pushes); the caller commits and then sends
    each push (user_id, slug or None for the watchlist, media, season) with
    remove=False, like a normal add. An entry whose list is gone or whose item
    is already back on the list is only dropped.
    """
    restored, skipped, pushes = 0, 0, []
    now = utcnow()
    for entry in entries:
        lst = await db.get(List, entry.list_id)
        exists = lst is not None and (await db.execute(select(ListItem.id).where(
            ListItem.list_id == entry.list_id, ListItem.media_id == entry.media_id,
            func.coalesce(ListItem.season_number, -1) == (entry.season_number if entry.season_number is not None else -1),
        ))).first() is not None
        if lst is None or exists:
            skipped += 1
        else:
            # added_at is the restore time, not the original: the cutoff is
            # max(added_at, auto_remove_since), so the watch that removed the
            # item would otherwise remove it again on the next run.
            db.add(ListItem(list_id=lst.id, media_id=entry.media_id, season_number=entry.season_number,
                            added_at=now, sort_order=entry.sort_order, notes=entry.notes))
            lst.updated_at = now
            restored += 1
            if lst.is_watchlist or lst.trakt_slug:
                pushes.append((lst.user_id, None if lst.is_watchlist else lst.trakt_slug,
                               entry.media, entry.season_number))
        await db.delete(entry)
    return restored, skipped, pushes

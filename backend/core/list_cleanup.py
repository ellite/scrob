"""Remove newly watched items from opted-in personal lists (#338)."""

from datetime import date

from sqlalchemy import select, func, or_
from sqlalchemy.orm import selectinload

from core.timeutil import utcnow
from models.base import MediaType
from models.events import WatchEvent
from models.lists import List, ListItem
from models.media import Media
from models.show import Show


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
    return [e.id for e in local]


async def prune_watched_list_items(db, pending_pushes=None):
    lists = (await db.execute(select(List).where(
        List.auto_remove_watched == True,
    ).with_for_update(skip_locked=True))).scalars().all()
    if not lists:
        return 0
    by_id = {lst.id: lst for lst in lists}
    items = (await db.execute(select(ListItem).where(ListItem.list_id.in_(by_id))
                             .options(selectinload(ListItem.media)))).scalars().all()
    if not items:
        return 0
    tmdb_ids = {i.media.tmdb_id for i in items if i.media.media_type == MediaType.series and i.media.tmdb_id}
    tvdb_ids = {i.media.tvdb_id for i in items if i.media.media_type == MediaType.series and not i.media.tmdb_id and i.media.tvdb_id}
    shows = {}
    episodes = {}
    if tmdb_ids or tvdb_ids:
        rows = (await db.execute(select(Show).where(or_(Show.tmdb_id.in_(tmdb_ids), Show.tvdb_id.in_(tvdb_ids))))).scalars().all()
        for show in rows:
            shows[("tmdb", show.tmdb_id)] = show
            shows[("tvdb", show.tvdb_id)] = show
        show_ids = [s.id for s in rows]
        rows = (await db.execute(select(Media).where(Media.show_id.in_(show_ids), Media.media_type == MediaType.episode))).scalars().all()
        for episode in rows:
            episodes.setdefault(episode.show_id, []).append(episode)
    release_dates = {}
    for show_episodes in episodes.values():
        for episode in show_episodes:
            if episode.release_date:
                try:
                    release_dates[episode.id] = date.fromisoformat(episode.release_date)
                except ValueError:
                    pass
    required = {i.id: _required_episode_ids(i, shows, episodes) for i in items}
    media_ids = {media_id for ids in required.values() for media_id in ids}
    watched = (await db.execute(select(
        WatchEvent.user_id, WatchEvent.media_id,
        func.min(func.coalesce(WatchEvent.watched_at, WatchEvent.created_at)),
        func.max(WatchEvent.watched_at),
    ).where(
        WatchEvent.user_id.in_({lst.user_id for lst in lists}),
        WatchEvent.media_id.in_(media_ids), WatchEvent.completed == True,
        or_(WatchEvent.watched_at.is_(None), WatchEvent.watched_at <= utcnow()),
    ).group_by(WatchEvent.user_id, WatchEvent.media_id))).all()
    watch_dates = {(user_id, media_id): (first, last) for user_id, media_id, first, last in watched}
    removed = 0
    for item in items:
        lst = by_id[item.list_id]
        ids = required[item.id]
        dates = [watch_dates.get((lst.user_id, media_id), (None, None)) for media_id in ids]
        if not dates:
            continue
        seen_at_add = [first is not None and first <= item.added_at for first, _ in dates]
        watched_since = [last is not None and last > item.added_at for _, last in dates]
        added_date = item.added_at.date()
        available_at_add = [
            seen for media_id, seen in zip(ids, seen_at_add)
            if release_dates.get(media_id, added_date) <= added_date
        ]
        # Episodes released later do not change whether this was a rewatch.
        if available_at_add and all(available_at_add):
            should_remove = all(watched_since)
        else:
            should_remove = any(watched_since) and all(
                before or after for before, after in zip(seen_at_add, watched_since)
            )
        if should_remove:
            if pending_pushes is not None and (lst.is_watchlist or lst.trakt_slug):
                slug = None if lst.is_watchlist else lst.trakt_slug
                pending_pushes.append((lst.user_id, slug, item.media, item.season_number))
            await db.delete(item)
            lst.updated_at = utcnow()
            removed += 1
    return removed

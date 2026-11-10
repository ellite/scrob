import os
import unittest
from types import SimpleNamespace
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("SECRET_KEY", "synthetic-test-key-32-characters-long")

from core.list_cleanup import prune_watched_list_items, _required_episode_ids
from routers.lists import ListUpdate, update_list
from models.base import Base, MediaType
from models.users import User
from models.media import Media
from models.lists import List, ListItem
from models.events import WatchEvent
from models.show import Show


@compiles(JSONB, "sqlite")
def _jsonb(type_, compiler, **kw):
    return "JSON"


class ListAutoRemoveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import models  # noqa: F401
        self.engine = create_async_engine("sqlite+aiosqlite://")
        self.addAsyncCleanup(self.engine.dispose)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.added = datetime(2026, 1, 2)
        async with self.Session() as db:
            db.add_all([User(id=1, email="a@test.local", username="a", password_hash="x", api_key="test-a"),
                        User(id=2, email="b@test.local", username="b", password_hash="x", api_key="test-b"),
                        Media(id=1, media_type=MediaType.movie, title="Movie"),
                        List(id=1, user_id=1, name="Queue", auto_remove_watched=True,
                             auto_remove_since=self.added)])
            await db.flush()
            db.add(ListItem(id=1, list_id=1, media_id=1, added_at=self.added))
            await db.commit()

    async def _run(self, *, user_id=1, watched_at=None, completed=True, enabled=True,
                   since=None, include_earlier=False):
        async with self.Session() as db:
            lst = await db.get(List, 1)
            lst.auto_remove_watched = enabled
            lst.auto_remove_include_earlier = include_earlier
            if since is not None:
                lst.auto_remove_since = since
            db.add(WatchEvent(user_id=user_id, media_id=1, completed=completed, watched_at=watched_at))
            await db.commit()
            removed = await prune_watched_list_items(db)
            await db.commit()
            return removed, (await db.execute(select(ListItem))).scalars().all()

    async def test_new_completed_watch_removes_item(self):
        removed, remaining = await self._run(watched_at=self.added + timedelta(days=1))
        self.assertEqual(removed, 1)
        self.assertEqual(remaining, [])

    async def test_old_history_keeps_rewatch_entry(self):
        removed, remaining = await self._run(watched_at=self.added - timedelta(days=1))
        self.assertEqual(removed, 0)
        self.assertEqual(len(remaining), 1)

    async def test_unknown_watch_date_is_not_a_new_watch(self):
        removed, remaining = await self._run()
        self.assertEqual(removed, 0)
        self.assertEqual(len(remaining), 1)

    async def test_other_users_watch_does_not_remove_item(self):
        removed, remaining = await self._run(user_id=2, watched_at=self.added + timedelta(days=1))
        self.assertEqual(removed, 0)
        self.assertEqual(len(remaining), 1)

    async def test_incomplete_watch_keeps_item(self):
        removed, remaining = await self._run(completed=False, watched_at=self.added + timedelta(days=1))
        self.assertEqual(removed, 0)
        self.assertEqual(len(remaining), 1)

    async def test_watch_before_turning_the_setting_on_keeps_item(self):
        removed, remaining = await self._run(watched_at=self.added + timedelta(days=1),
                                             since=self.added + timedelta(days=5))
        self.assertEqual(removed, 0)
        self.assertEqual(len(remaining), 1)

    async def test_include_earlier_counts_watches_since_the_item_was_added(self):
        removed, remaining = await self._run(watched_at=self.added + timedelta(days=1),
                                             since=self.added + timedelta(days=5), include_earlier=True)
        self.assertEqual(removed, 1)
        self.assertEqual(remaining, [])

    async def test_include_earlier_still_ignores_watches_before_adding(self):
        removed, remaining = await self._run(watched_at=self.added - timedelta(days=1), include_earlier=True)
        self.assertEqual(removed, 0)
        self.assertEqual(len(remaining), 1)

    async def test_missing_start_time_is_stamped_without_removing(self):
        async with self.Session() as db:
            (await db.get(List, 1)).auto_remove_since = None
            await db.commit()
        removed, remaining = await self._run(watched_at=self.added + timedelta(days=1))
        self.assertEqual(removed, 0)
        self.assertEqual(len(remaining), 1)
        async with self.Session() as db:
            self.assertIsNotNone((await db.get(List, 1)).auto_remove_since)

    async def test_turning_on_stamps_the_start_and_turning_off_clears_it(self):
        user = SimpleNamespace(id=1)
        async with self.Session() as db:
            lst = await db.get(List, 1)
            lst.auto_remove_watched, lst.auto_remove_since = False, None
            await db.commit()
        async with self.Session() as db:
            await update_list(1, ListUpdate(auto_remove_watched=True), db=db, current_user=user)
        async with self.Session() as db:
            first = (await db.get(List, 1)).auto_remove_since
        self.assertIsNotNone(first)
        async with self.Session() as db:
            # Saving the edit dialog again must not move the start forward.
            await update_list(1, ListUpdate(auto_remove_watched=True), db=db, current_user=user)
        async with self.Session() as db:
            self.assertEqual((await db.get(List, 1)).auto_remove_since, first)
            await update_list(1, ListUpdate(auto_remove_watched=False), db=db, current_user=user)
        async with self.Session() as db:
            self.assertIsNone((await db.get(List, 1)).auto_remove_since)

    async def test_disabled_setting_keeps_item(self):
        removed, remaining = await self._run(enabled=False, watched_at=self.added + timedelta(days=1))
        self.assertEqual(removed, 0)
        self.assertEqual(len(remaining), 1)

    async def _series(self, expected=2, *, season=None, tvdb_only=False, old_first=False, previously_seen=False,
                      rewatch_all=False, new_episode=False, announced=False, only_first=False):
        async with self.Session() as db:
            media = await db.get(Media, 1)
            media.media_type = MediaType.series
            media.tmdb_id = None if tvdb_only else 10
            media.tvdb_id = 20
            db.add(Show(id=1, title="Show", tmdb_id=media.tmdb_id, tvdb_id=20,
                        tmdb_data={"seasons": [{"season_number": 1, "episode_count": expected}]}))
            (await db.get(ListItem, 1)).season_number = season
            for number in (1, 2):
                db.add(Media(id=number + 1, media_type=MediaType.episode, title=str(number),
                             show_id=1, season_number=1, episode_number=number,
                             release_date="2999-01-01" if announced and number == 2 else
                             (self.added + timedelta(days=1)).date().isoformat()
                             if new_episode and number == 2 else None))
                if previously_seen or (old_first and number == 1):
                    db.add(WatchEvent(user_id=1, media_id=number + 1, completed=True,
                                      watched_at=self.added - timedelta(days=1)))
                if only_first and number == 2:
                    continue
                if rewatch_all or number == 2 or not (old_first or previously_seen):
                    db.add(WatchEvent(user_id=1, media_id=number + 1, completed=True,
                                      watched_at=self.added + timedelta(days=1)))
            await db.commit()
            result = await prune_watched_list_items(db)
            await db.commit()
            return result

    async def test_complete_series_is_removed(self):
        self.assertEqual(await self._series(), 1)

    async def test_incomplete_metadata_does_not_remove_series(self):
        self.assertEqual(await self._series(expected=3), 0)

    async def test_finishing_partially_watched_series_removes_it(self):
        self.assertEqual(await self._series(old_first=True), 1)

    async def test_previously_completed_series_goes_once_a_new_watch_follows(self):
        # Decision 2026-11-10: every released episode watched and the newest
        # watch after adding - no complete rewatch required.
        self.assertEqual(await self._series(previously_seen=True), 1)

    async def test_complete_rewatch_removes_previously_completed_series(self):
        self.assertEqual(await self._series(previously_seen=True, rewatch_all=True), 1)

    async def test_finishing_a_new_episode_removes_a_series_seen_before(self):
        self.assertEqual(await self._series(old_first=True, new_episode=True), 1)

    async def test_announced_episode_is_not_required(self):
        self.assertEqual(await self._series(announced=True, only_first=True), 1)

    async def test_unwatched_released_episode_keeps_series(self):
        self.assertEqual(await self._series(only_first=True), 0)

    async def test_chunked_queries_give_the_same_result(self):
        with patch("core.list_cleanup.CHUNK", 1):
            self.assertEqual(await self._series(), 1)

    async def test_complete_season_is_removed(self):
        self.assertEqual(await self._series(season=1), 1)

    async def test_tvdb_only_series_is_supported(self):
        self.assertEqual(await self._series(tvdb_only=True), 1)

    async def test_disabled_native_watchlist_is_not_pruned(self):
        async with self.Session() as db:
            (await db.get(List, 1)).is_watchlist = True
            await db.commit()
        removed, remaining = await self._run(enabled=False, watched_at=self.added + timedelta(days=1))
        self.assertEqual(removed, 0)
        self.assertEqual(len(remaining), 1)

    async def test_enabled_native_watchlist_queues_removal(self):
        async with self.Session() as db:
            (await db.get(List, 1)).is_watchlist = True
            db.add(WatchEvent(user_id=1, media_id=1, completed=True, watched_at=self.added + timedelta(days=1)))
            await db.commit()
            pending = []
            self.assertEqual(await prune_watched_list_items(db, pending), 1)
            self.assertEqual([(user, slug, media.id, season) for user, slug, media, season in pending],
                             [(1, None, 1, None)])

    async def test_linked_list_queues_the_existing_removal_push(self):
        async with self.Session() as db:
            (await db.get(List, 1)).trakt_slug = "queue"
            db.add(WatchEvent(user_id=1, media_id=1, completed=True, watched_at=self.added + timedelta(days=1)))
            await db.commit()
            pending = []
            self.assertEqual(await prune_watched_list_items(db, pending), 1)
            self.assertEqual([(user, slug, media.id, season) for user, slug, media, season in pending],
                             [(1, "queue", 1, None)])


class SeriesMetadataSafetyTests(unittest.TestCase):
    def setUp(self):
        self.item = SimpleNamespace(media=SimpleNamespace(media_type=MediaType.series, tmdb_id=10), season_number=None)
        self.shows = {("tmdb", 10): SimpleNamespace(id=1, tmdb_data={"seasons": [
            {"season_number": 1, "episode_count": 1}, {"season_number": 2, "episode_count": 1},
        ]})}

    def test_equal_total_with_missing_season_is_not_complete(self):
        episodes = {1: [SimpleNamespace(id=2, season_number=1, episode_number=1),
                        SimpleNamespace(id=3, season_number=1, episode_number=2)]}
        self.assertEqual(_required_episode_ids(self.item, self.shows, episodes), [])

    def test_duplicate_positions_are_not_complete(self):
        episodes = {1: [SimpleNamespace(id=n, season_number=1, episode_number=1) for n in (2, 3)]}
        self.assertEqual(_required_episode_ids(self.item, self.shows, episodes), [])

    def test_complete_multiple_seasons(self):
        episodes = {1: [SimpleNamespace(id=2, season_number=1, episode_number=1, release_date=None),
                        SimpleNamespace(id=3, season_number=2, episode_number=1, release_date="2020-01-01")]}
        self.assertEqual(_required_episode_ids(self.item, self.shows, episodes), [2, 3])

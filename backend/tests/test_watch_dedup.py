from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from sqlalchemy import select

from core.watch_dedup import find_duplicate_watch_event


@pytest.mark.asyncio
async def test_duplicate_check_only_matches_completed_rows():
    # #481: an in-progress row (completed=False) from a Jellyfin/Emby pull
    # must not swallow the real completion arriving from a webhook.
    db = AsyncMock()
    db.get_bind = MagicMock(return_value=None)
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute.return_value = result

    await find_duplicate_watch_event(db, 1, 2, datetime(2026, 10, 16, 7, 28), 20)

    statement = db.execute.call_args.args[0]
    compiled = str(statement.compile(compile_kwargs={"literal_binds": True})).lower()
    assert "watch_events.completed is true" in compiled or "watch_events.completed is 1" in compiled


# ── Same episode stored as two Media rows (#527) ──────────────────────────────

import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from core.watch_dedup import is_duplicate_watch_time, load_existing_watch_times
from models.events import WatchEvent
from models.media import Media, MediaType


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


class SameEpisodeTwoMediaRowsTests(unittest.IsolatedAsyncioTestCase):
    """A Plex webhook and a Trakt import can resolve one episode to two Media
    rows (different titles/ids); the plays are still the same play."""

    PLEX_PLAY = datetime(2026, 10, 25, 18, 45)
    TRAKT_PLAY = datetime(2026, 10, 25, 18, 47)

    async def asyncSetUp(self):
        from models.base import Base

        self.engine = create_async_engine(
            "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.addAsyncCleanup(self.engine.dispose)
        async with self.Session() as db:
            db.add_all([
                Media(id=1, tmdb_id=111, media_type=MediaType.episode, title="broken [W]ings", show_id=7, season_number=1, episode_number=16),
                Media(id=2, tmdb_id=222, media_type=MediaType.episode, title="Episode 16", show_id=7, season_number=1, episode_number=16),
                Media(id=3, tmdb_id=333, media_type=MediaType.episode, title="Episode 17", show_id=7, season_number=1, episode_number=17),
                Media(id=4, tmdb_id=444, media_type=MediaType.movie, title="A Movie"),
            ])
            db.add(WatchEvent(user_id=1, media_id=1, watched_at=self.PLEX_PLAY, completed=True))
            await db.commit()

    async def test_live_check_finds_the_play_recorded_on_the_other_row(self):
        async with self.Session() as db:
            found = await find_duplicate_watch_event(db, 1, 2, self.TRAKT_PLAY, 10)
            self.assertIsNotNone(found)
            self.assertEqual(found.media_id, 1)

    async def test_a_different_episode_is_not_a_duplicate(self):
        async with self.Session() as db:
            self.assertIsNone(await find_duplicate_watch_event(db, 1, 3, self.TRAKT_PLAY, 10))

    async def test_outside_the_window_is_not_a_duplicate(self):
        async with self.Session() as db:
            self.assertIsNone(await find_duplicate_watch_event(db, 1, 2, self.PLEX_PLAY + timedelta(hours=3), 10))

    async def test_preload_shares_times_between_rows_of_the_same_episode(self):
        async with self.Session() as db:
            times = await load_existing_watch_times(db, 1)
        self.assertTrue(is_duplicate_watch_time(times, 2, self.TRAKT_PLAY, 10))
        self.assertFalse(is_duplicate_watch_time(times, 3, self.TRAKT_PLAY, 10))

    async def test_heal_collapses_a_play_split_across_two_rows(self):
        from routers import sync

        async with self.Session() as db:
            db.add_all([
                WatchEvent(user_id=1, media_id=2, watched_at=self.TRAKT_PLAY, completed=True),
                WatchEvent(user_id=1, media_id=3, watched_at=self.TRAKT_PLAY, completed=True),
            ])
            await db.commit()
            result = await sync.heal_duplicate_history(
                sync.HealDuplicateHistoryBody(window_minutes=10), db=db, current_user=SimpleNamespace(id=1),
            )
            self.assertEqual(result["healed"], 1)
            remaining = (await db.execute(select(WatchEvent.media_id).order_by(WatchEvent.id))).scalars().all()
        # The earlier Plex play on row 1 is kept; episode 17 is untouched.
        self.assertEqual(remaining, [1, 3])

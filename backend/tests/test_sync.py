import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from fastapi import HTTPException

from models.connections import MediaServerConnection
from routers import sync


class _Result:
    def __init__(self, item):
        self.item = item

    def scalar_one_or_none(self):
        return self.item


class _FakeSession:
    def __init__(self, conn):
        self.conn = conn
        self.added = []
        self.commit = AsyncMock()
        self.refresh = AsyncMock()

    async def execute(self, stmt):
        return _Result(self.conn)

    def add(self, obj):
        self.added.append(obj)


class PushUpstreamValidationTests(unittest.IsolatedAsyncioTestCase):
    """Regression test: a connection with only the Plex watchlist push flag
    enabled (no collection/watched/ratings/playback) was rejected outright -
    this validation predated plex_push_watchlist and never learned about it,
    so "Push" always 400'd for anyone using watchlist-only push."""

    async def test_watchlist_only_flag_is_accepted(self):
        conn = MediaServerConnection(
            id=1, user_id=1, type="plex",
            push_collection=False, push_watched=False, push_ratings=False, push_playback=False,
            plex_push_watchlist=True,
        )
        db = _FakeSession(conn)
        background_tasks = SimpleNamespace(add_task=lambda *a, **k: None)

        response = await sync.push_upstream(
            connection_id=1, background_tasks=background_tasks, db=db, current_user=SimpleNamespace(id=1),
        )
        self.assertEqual(response["status"], "started")

    async def test_no_flags_at_all_is_rejected(self):
        conn = MediaServerConnection(
            id=1, user_id=1, type="plex",
            push_collection=False, push_watched=False, push_ratings=False, push_playback=False,
            plex_push_watchlist=False,
        )
        db = _FakeSession(conn)
        background_tasks = SimpleNamespace(add_task=lambda *a, **k: None)

        with self.assertRaises(HTTPException) as ctx:
            await sync.push_upstream(
                connection_id=1, background_tasks=background_tasks, db=db, current_user=SimpleNamespace(id=1),
            )
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_other_flags_still_accepted_without_watchlist(self):
        conn = MediaServerConnection(
            id=1, user_id=1, type="jellyfin",
            push_collection=True, push_watched=False, push_ratings=False, push_playback=False,
            plex_push_watchlist=False,
        )
        db = _FakeSession(conn)
        background_tasks = SimpleNamespace(add_task=lambda *a, **k: None)

        response = await sync.push_upstream(
            connection_id=1, background_tasks=background_tasks, db=db, current_user=SimpleNamespace(id=1),
        )
        self.assertEqual(response["status"], "started")


class PlexSyncNeedsLibraryScanTests(unittest.TestCase):
    """Regression test: a Plex pull with only "Watchlist" selected still
    re-scanned every movie/show/episode in the user's entire library before
    ever reaching the watchlist step, because the scan had no gate of its
    own - only what happened *inside* it was conditional."""

    def test_watchlist_only_does_not_need_a_scan(self):
        conn = SimpleNamespace(sync_collection=False, sync_watched=False, sync_ratings=False, plex_sync_watchlist=True)
        self.assertFalse(sync.plex_sync_needs_library_scan(conn))

    def test_any_single_category_needs_a_scan(self):
        base = dict(sync_collection=False, sync_watched=False, sync_ratings=False)
        for field in ("sync_collection", "sync_watched", "sync_ratings"):
            conn = SimpleNamespace(**{**base, field: True})
            self.assertTrue(sync.plex_sync_needs_library_scan(conn), f"{field} alone should trigger a scan")

    def test_nothing_selected_at_all_does_not_need_a_scan(self):
        conn = SimpleNamespace(sync_collection=False, sync_watched=False, sync_ratings=False, plex_sync_watchlist=False)
        self.assertFalse(sync.plex_sync_needs_library_scan(conn))


if __name__ == "__main__":
    unittest.main()

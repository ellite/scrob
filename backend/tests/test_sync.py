import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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


class _PlexHistoryFakeDB:
    """Minimal async-session double for _backfill_plex_watch_history: just
    enough to serve db.get(MediaServerConnection), the existing-WatchEvent
    select, db.add()/flush()/commit(), and the reconciling UPDATE statement."""

    def __init__(self, conn, existing_watch_rows):
        self.conn = conn
        self.existing_watch_rows = existing_watch_rows
        self.added: list = []
        self.updates: list[dict] = []
        self.commit = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def get(self, model, obj_id):
        return self.conn

    async def execute(self, statement):
        sql = str(statement)
        if sql.startswith("UPDATE watch_events"):
            self.updates.append(dict(statement.compile().params))
            return None
        return list(self.existing_watch_rows)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


class BackfillPlexWatchHistoryDedupTests(unittest.IsolatedAsyncioTestCase):
    """Regression tests for GitHub #135: the real-time Plex webhook already
    records a WatchEvent (marked provisional, since its watched_at is this
    server's receipt time, not Plex's) for a play that the periodic
    _backfill_plex_watch_history() pass will see again via Plex's own history
    endpoint. That must reconcile the provisional row rather than insert a
    duplicate — and, deliberately, only within a tight webhook-latency window
    and only against provisional rows, not a broad time-window match against
    any existing play regardless of source (the original, too-broad fix)."""

    async def _run(self, existing_watch_rows, history_entries):
        # existing_watch_rows: list of (id, media_id, watched_at, provisional)
        conn = SimpleNamespace(plex_history_cursor_at=None)
        db = _PlexHistoryFakeDB(conn, existing_watch_rows)

        with (
            patch.object(sync, "async_sessionmaker", return_value=lambda *a, **k: db),
            patch.object(sync.plex, "get_history", AsyncMock(return_value=history_entries)),
            patch.object(sync, "record_rewatch_progress", AsyncMock()),
        ):
            new_events, reconciled, unmatched = await sync._backfill_plex_watch_history(
                user_id=1,
                connection_id=1,
                p_url="http://plex",
                p_token="token",
                server_username=None,
                ratingkey_to_media={"555": 10},
            )
        return new_events, reconciled, unmatched, db

    @staticmethod
    def _history_entry(viewed_at: datetime, rating_key: str = "555") -> dict:
        return {
            "type": "movie", "ratingKey": rating_key,
            "viewedAt": int(viewed_at.replace(tzinfo=timezone.utc).timestamp()),
        }

    async def test_provisional_webhook_event_within_window_is_reconciled_not_duplicated(self):
        # The webhook already wrote a provisional event from its own receipt
        # time. Plex's authoritative viewedAt for the same play is a few
        # minutes off — must correct that row in place, not add a new one.
        webhook_watched_at = datetime(2026, 8, 1, 20, 0, 0)
        authoritative_watched_at = webhook_watched_at + timedelta(minutes=4)

        new_events, reconciled, unmatched, db = await self._run(
            existing_watch_rows=[(101, 10, webhook_watched_at, True)],
            history_entries=[self._history_entry(authoritative_watched_at)],
        )

        self.assertEqual(new_events, 0)
        self.assertEqual(reconciled, 1)
        self.assertEqual(db.added, [])
        self.assertEqual(len(db.updates), 1)
        self.assertEqual(db.updates[0]["watched_at"], authoritative_watched_at)
        self.assertEqual(db.updates[0]["provisional"], False)
        self.assertEqual(db.updates[0]["id_1"], 101)

    async def test_provisional_event_outside_reconcile_window_is_left_alone(self):
        # A provisional row too far away in time isn't a plausible webhook/
        # backfill pairing for the same play — must not be touched, and the
        # authoritative play still gets its own row.
        webhook_watched_at = datetime(2026, 8, 1, 20, 0, 0)
        authoritative_watched_at = webhook_watched_at + timedelta(hours=3)

        new_events, reconciled, unmatched, db = await self._run(
            existing_watch_rows=[(101, 10, webhook_watched_at, True)],
            history_entries=[self._history_entry(authoritative_watched_at)],
        )

        self.assertEqual(new_events, 1)
        self.assertEqual(reconciled, 0)
        self.assertEqual(db.updates, [])
        self.assertEqual(len(db.added), 1)
        self.assertEqual(db.added[0].watched_at, authoritative_watched_at)

    async def test_confirmed_event_requires_exact_match_not_a_window(self):
        # A non-provisional (already-confirmed) row — e.g. from a prior run of
        # this same backfill, or an unrelated Trakt/Simkl import — must only
        # be treated as the same play on an exact watched_at match. A close
        # but non-identical confirmed row must NOT suppress recording the
        # authoritative play, unlike the old blanket time-window approach.
        confirmed_watched_at = datetime(2026, 8, 1, 20, 0, 0)
        authoritative_watched_at = confirmed_watched_at + timedelta(minutes=2)

        new_events, reconciled, unmatched, db = await self._run(
            existing_watch_rows=[(101, 10, confirmed_watched_at, False)],
            history_entries=[self._history_entry(authoritative_watched_at)],
        )

        self.assertEqual(new_events, 1)
        self.assertEqual(reconciled, 0)
        self.assertEqual(db.updates, [])
        self.assertEqual(len(db.added), 1)

    async def test_confirmed_event_exact_match_is_a_no_op(self):
        # Re-running the backfill (e.g. cursor overlap) must not re-add a play
        # it already recorded itself — deterministic, no tolerance needed.
        watched_at = datetime(2026, 8, 1, 20, 0, 0)

        new_events, reconciled, unmatched, db = await self._run(
            existing_watch_rows=[(101, 10, watched_at, False)],
            history_entries=[self._history_entry(watched_at)],
        )

        self.assertEqual(new_events, 0)
        self.assertEqual(reconciled, 0)
        self.assertEqual(db.updates, [])
        self.assertEqual(db.added, [])

    async def test_distant_replay_is_recorded_as_a_new_play(self):
        # A genuinely distinct rewatch, well outside the dedup window, must
        # still be imported as its own WatchEvent (the original point of #126).
        already_recorded_at = datetime(2026, 1, 1, 20, 0, 0)
        history_viewed_at = datetime(2026, 8, 1, 20, 0, 0)

        new_events, reconciled, unmatched, db = await self._run(
            existing_watch_rows=[(101, 10, already_recorded_at, False)],
            history_entries=[self._history_entry(history_viewed_at)],
        )

        self.assertEqual(new_events, 1)
        self.assertEqual(reconciled, 0)
        self.assertEqual(len(db.added), 1)
        self.assertEqual(db.added[0].watched_at, history_viewed_at)

    async def test_unmapped_ratingkey_counts_as_unmatched(self):
        new_events, reconciled, unmatched, db = await self._run(
            existing_watch_rows=[],
            history_entries=[{"type": "movie", "ratingKey": "999", "viewedAt": 1754074800}],
        )

        self.assertEqual(new_events, 0)
        self.assertEqual(reconciled, 0)
        self.assertEqual(unmatched, 1)
        self.assertEqual(db.added, [])


if __name__ == "__main__":
    unittest.main()

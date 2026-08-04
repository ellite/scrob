import os
import unittest
from datetime import datetime, timedelta

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core.watch_event_cleanup import WatchEventRow, find_duplicate_watch_event_ids


def _webhook_row(id, user_id, media_id, watched_at):
    return WatchEventRow(id=id, user_id=user_id, media_id=media_id, watched_at=watched_at,
                          progress_percent=1.0, progress_seconds=1234)


def _backfill_row(id, user_id, media_id, watched_at):
    return WatchEventRow(id=id, user_id=user_id, media_id=media_id, watched_at=watched_at,
                          progress_percent=None, progress_seconds=None)


class FindDuplicateWatchEventIdsTests(unittest.TestCase):
    def test_webhook_and_backfill_pair_within_window_merges_to_the_webhook_row(self):
        # The exact GitHub #135 shape: webhook fired first, backfill's
        # authoritative viewedAt landed a few minutes later for the same play.
        t = datetime(2026, 8, 4, 12, 54, 16)
        rows = [
            _webhook_row(1, user_id=1, media_id=100, watched_at=t),
            _backfill_row(2, user_id=1, media_id=100, watched_at=t + timedelta(minutes=4)),
        ]

        self.assertEqual(find_duplicate_watch_event_ids(rows), [2])

    def test_pair_outside_reconcile_window_is_left_alone(self):
        t = datetime(2026, 8, 4, 12, 54, 16)
        rows = [
            _webhook_row(1, user_id=1, media_id=100, watched_at=t),
            _backfill_row(2, user_id=1, media_id=100, watched_at=t + timedelta(minutes=30)),
        ]

        self.assertEqual(find_duplicate_watch_event_ids(rows), [])

    def test_different_users_at_the_same_moment_are_never_compared(self):
        # Explicit requirement: two independent users watching the same item
        # at the same time must never be merged into each other.
        t = datetime(2026, 8, 4, 12, 54, 16)
        rows = [
            _webhook_row(1, user_id=1, media_id=100, watched_at=t),
            _backfill_row(2, user_id=2, media_id=100, watched_at=t),
        ]

        self.assertEqual(find_duplicate_watch_event_ids(rows), [])

    def test_different_media_for_the_same_user_are_never_compared(self):
        t = datetime(2026, 8, 4, 12, 54, 16)
        rows = [
            _webhook_row(1, user_id=1, media_id=100, watched_at=t),
            _backfill_row(2, user_id=1, media_id=200, watched_at=t),
        ]

        self.assertEqual(find_duplicate_watch_event_ids(rows), [])

    def test_three_rows_clustered_together_is_ambiguous_and_left_alone(self):
        t = datetime(2026, 8, 4, 12, 0, 0)
        rows = [
            _webhook_row(1, user_id=1, media_id=100, watched_at=t),
            _backfill_row(2, user_id=1, media_id=100, watched_at=t + timedelta(minutes=1)),
            _backfill_row(3, user_id=1, media_id=100, watched_at=t + timedelta(minutes=2)),
        ]

        self.assertEqual(find_duplicate_watch_event_ids(rows), [])

    def test_two_webhook_rows_close_together_are_left_alone(self):
        # Same signature on both sides isn't the known bug shape — could be a
        # genuine fast rewatch, or a duplicate webhook delivery that should
        # already have been caught by _write_watch_event's own guard.
        t = datetime(2026, 8, 4, 12, 0, 0)
        rows = [
            _webhook_row(1, user_id=1, media_id=100, watched_at=t),
            _webhook_row(2, user_id=1, media_id=100, watched_at=t + timedelta(minutes=1)),
        ]

        self.assertEqual(find_duplicate_watch_event_ids(rows), [])

    def test_two_backfill_rows_close_together_are_left_alone(self):
        t = datetime(2026, 8, 4, 12, 0, 0)
        rows = [
            _backfill_row(1, user_id=1, media_id=100, watched_at=t),
            _backfill_row(2, user_id=1, media_id=100, watched_at=t + timedelta(minutes=1)),
        ]

        self.assertEqual(find_duplicate_watch_event_ids(rows), [])

    def test_row_matching_neither_signature_blocks_the_merge(self):
        # e.g. a manual "mark as watched" (progress_percent=1.0, progress_seconds
        # left unset) sitting next to a real backfill row — not the known
        # webhook+backfill shape, so nothing should be deleted.
        t = datetime(2026, 8, 4, 12, 0, 0)
        neither = WatchEventRow(id=1, user_id=1, media_id=100, watched_at=t,
                                 progress_percent=1.0, progress_seconds=None)
        rows = [neither, _backfill_row(2, user_id=1, media_id=100, watched_at=t + timedelta(minutes=1))]

        self.assertEqual(find_duplicate_watch_event_ids(rows), [])

    def test_genuinely_distant_rewatches_are_both_kept(self):
        t = datetime(2026, 1, 1, 20, 0, 0)
        rows = [
            _webhook_row(1, user_id=1, media_id=100, watched_at=t),
            _backfill_row(2, user_id=1, media_id=100, watched_at=t + timedelta(hours=6)),
        ]

        self.assertEqual(find_duplicate_watch_event_ids(rows), [])

    def test_multiple_independent_pairs_across_users_and_media_all_resolve(self):
        t = datetime(2026, 8, 4, 12, 0, 0)
        rows = [
            _webhook_row(1, user_id=1, media_id=100, watched_at=t),
            _backfill_row(2, user_id=1, media_id=100, watched_at=t + timedelta(minutes=1)),
            _webhook_row(3, user_id=2, media_id=100, watched_at=t),
            _backfill_row(4, user_id=2, media_id=100, watched_at=t + timedelta(minutes=2)),
            _webhook_row(5, user_id=1, media_id=200, watched_at=t),
            _backfill_row(6, user_id=1, media_id=200, watched_at=t + timedelta(minutes=3)),
        ]

        self.assertEqual(sorted(find_duplicate_watch_event_ids(rows)), [2, 4, 6])


if __name__ == "__main__":
    unittest.main()

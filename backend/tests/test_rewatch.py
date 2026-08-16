import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from sqlalchemy.sql.dml import Delete

from core.rewatch import (
    capped_season_episode_counts,
    total_aired_episodes,
    record_rewatch_progress,
    get_already_watched_for_bulk_mark,
)
from models.base import MediaType
from models.events import WatchEvent
from models.media import Media
from models.rewatch import ShowRewatch
from models.show import Show
from routers import history
from routers.webhooks import _handle_unwatch_toggle
from routers.sync import is_fresh_rewatch_play


class CappedSeasonEpisodeCountsTests(unittest.TestCase):
    """A rewatch's completion check (core.rewatch._maybe_complete_rewatch) relies
    on this to know the true episode total without a live TMDB call - it must
    match the season_ep_counts logic routers.shows.get_show used to compute
    inline (see AGENTS.md / the get_show refactor these tests guard)."""

    def test_sums_non_special_seasons_when_show_has_ended(self):
        show = SimpleNamespace(tmdb_data={
            "seasons": [
                {"season_number": 0, "episode_count": 5},
                {"season_number": 1, "episode_count": 10},
                {"season_number": 2, "episode_count": 8},
            ],
        })
        self.assertEqual(total_aired_episodes(show), 18)

    def test_caps_current_season_at_last_aired_episode(self):
        show = SimpleNamespace(tmdb_data={
            "seasons": [
                {"season_number": 1, "episode_count": 10},
                {"season_number": 2, "episode_count": 10},
            ],
            "last_episode_to_air": {"season_number": 2, "episode_number": 4},
        })
        counts = capped_season_episode_counts(show)
        self.assertEqual(counts[1], 10)
        self.assertEqual(counts[2], 4)

    def test_zeroes_out_seasons_after_the_currently_airing_one(self):
        show = SimpleNamespace(tmdb_data={
            "seasons": [
                {"season_number": 1, "episode_count": 10},
                {"season_number": 2, "episode_count": 10},
                {"season_number": 3, "episode_count": 10},
            ],
            "last_episode_to_air": {"season_number": 1, "episode_number": 6},
        })
        counts = capped_season_episode_counts(show)
        self.assertEqual(counts[1], 6)
        self.assertEqual(counts[2], 0)
        self.assertEqual(counts[3], 0)

    def test_no_tmdb_data_gives_zero_total(self):
        show = SimpleNamespace(tmdb_data=None)
        self.assertEqual(total_aired_episodes(show), 0)


class _Result:
    def __init__(self, item=None):
        self.item = item

    def scalar_one_or_none(self):
        return self.item

    def scalar(self):
        return self.item

    def all(self):
        if isinstance(self.item, list):
            return self.item
        return [] if self.item is None else [self.item]

    @property
    def rowcount(self):
        return len(self.item) if isinstance(self.item, list) else 0


class _FakeSession:
    """Same shape as the _FakeSession in test_history.py: queued results
    consumed in call order, plus a record of every statement executed so
    tests can assert whether a particular delete happened."""

    def __init__(self, results):
        self._results = list(results)
        self.executed_statements = []
        self.added = []
        self.flush = AsyncMock()
        self.commit = AsyncMock()
        self.refresh = AsyncMock()

    async def execute(self, stmt):
        self.executed_statements.append(stmt)
        item = self._results.pop(0) if self._results else None
        return _Result(item)

    def add(self, obj):
        # Mimics the server_default=func.now() a real commit would populate.
        if isinstance(obj, ShowRewatch) and obj.started_at is None:
            obj.started_at = datetime(2026, 1, 1, 12, 0, 0)
        self.added.append(obj)

    def _deleted_show_rewatch_ids(self) -> set:
        ids = set()
        for stmt in self.executed_statements:
            if isinstance(stmt, Delete) and stmt.table.name == "show_rewatches":
                # The single `ShowRewatch.id == X` where-clause built by
                # start_rewatch/cancel_rewatch - pull the literal id out.
                ids.add(stmt.whereclause.right.value)
        return ids


class RecordRewatchProgressTests(unittest.IsolatedAsyncioTestCase):
    """record_rewatch_progress is called after every completed episode
    WatchEvent across ~15 call sites (manual marks, webhooks, Trakt/Simkl/
    MDBList/Nuvio imports) - it must no-op cheaply and safely whenever a
    rewatch isn't actually in play."""

    async def test_noop_when_media_not_found(self):
        db = _FakeSession([None])
        await record_rewatch_progress(db, user_id=1, media_id=99, watch_event_id=1)
        self.assertEqual(len(db.executed_statements), 1)

    async def test_noop_for_movie_media(self):
        movie = Media(id=10, media_type=MediaType.movie, title="A Movie")
        db = _FakeSession([movie])
        await record_rewatch_progress(db, user_id=1, media_id=10, watch_event_id=1)
        self.assertEqual(len(db.executed_statements), 1)

    async def test_noop_for_episode_with_no_show_id(self):
        episode = Media(id=11, media_type=MediaType.episode, show_id=None, season_number=1, episode_number=1)
        db = _FakeSession([episode])
        await record_rewatch_progress(db, user_id=1, media_id=11, watch_event_id=1)
        self.assertEqual(len(db.executed_statements), 1)

    async def test_noop_when_show_has_no_active_rewatch(self):
        episode = Media(id=12, media_type=MediaType.episode, show_id=55, season_number=1, episode_number=1)
        db = _FakeSession([episode, None])  # media lookup, then get_active_rewatch -> none
        await record_rewatch_progress(db, user_id=1, media_id=12, watch_event_id=1)
        self.assertEqual(len(db.executed_statements), 2)

    async def test_records_progress_without_completing_when_under_total(self):
        episode = Media(id=13, media_type=MediaType.episode, show_id=55, season_number=1, episode_number=1)
        rewatch = ShowRewatch(id=7, user_id=1, show_id=55)
        show = Show(id=55, tmdb_data={"seasons": [{"season_number": 1, "episode_count": 10}]})
        db = _FakeSession([
            episode,   # media lookup
            rewatch,   # get_active_rewatch
            None,      # the upsert itself (result unused)
            show,      # _maybe_complete_rewatch: show lookup
            3,         # progress count so far
        ])
        await record_rewatch_progress(db, user_id=1, media_id=13, watch_event_id=100)
        self.assertEqual(db._deleted_show_rewatch_ids(), set())

    async def test_completes_and_deletes_rewatch_when_progress_reaches_total(self):
        episode = Media(id=14, media_type=MediaType.episode, show_id=55, season_number=1, episode_number=10)
        rewatch = ShowRewatch(id=8, user_id=1, show_id=55)
        show = Show(id=55, tmdb_data={"seasons": [{"season_number": 1, "episode_count": 10}]})
        db = _FakeSession([
            episode,
            rewatch,
            None,
            show,
            10,  # progress count == total
        ])
        await record_rewatch_progress(db, user_id=1, media_id=14, watch_event_id=101)
        self.assertEqual(db._deleted_show_rewatch_ids(), {8})


class StartAndCancelRewatchEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_rewatch_404s_when_show_not_found(self):
        db = _FakeSession([None])
        with self.assertRaises(Exception):
            await history.start_rewatch(series_tmdb_id=999, db=db, current_user=SimpleNamespace(id=1))

    async def test_start_rewatch_creates_new_when_none_active(self):
        show = Show(id=55, tmdb_id=100, title="Test Show")
        db = _FakeSession([show, None])  # show lookup, then get_active_rewatch -> none
        response = await history.start_rewatch(series_tmdb_id=100, db=db, current_user=SimpleNamespace(id=1))
        self.assertEqual(response["status"], "ok")
        created = next(o for o in db.added if isinstance(o, ShowRewatch))
        self.assertEqual((created.user_id, created.show_id), (1, 55))
        self.assertEqual(db._deleted_show_rewatch_ids(), set())

    async def test_start_rewatch_resets_existing_active_rewatch(self):
        show = Show(id=55, tmdb_id=100, title="Test Show")
        existing = ShowRewatch(id=42, user_id=1, show_id=55)
        db = _FakeSession([show, existing])
        await history.start_rewatch(series_tmdb_id=100, db=db, current_user=SimpleNamespace(id=1))
        self.assertEqual(db._deleted_show_rewatch_ids(), {42})
        created = next(o for o in db.added if isinstance(o, ShowRewatch))
        self.assertEqual(created.show_id, 55)

    async def test_cancel_rewatch_deletes_active_and_reports_cancelled(self):
        show = Show(id=55, tmdb_id=100, title="Test Show")
        existing = ShowRewatch(id=42, user_id=1, show_id=55)
        db = _FakeSession([show, existing])
        response = await history.cancel_rewatch(series_tmdb_id=100, db=db, current_user=SimpleNamespace(id=1))
        self.assertEqual(response, {"status": "ok", "cancelled": True})
        self.assertEqual(db._deleted_show_rewatch_ids(), {42})

    async def test_cancel_rewatch_is_a_noop_when_none_active(self):
        show = Show(id=55, tmdb_id=100, title="Test Show")
        db = _FakeSession([show, None])
        response = await history.cancel_rewatch(series_tmdb_id=100, db=db, current_user=SimpleNamespace(id=1))
        self.assertEqual(response, {"status": "ok", "cancelled": False})
        self.assertEqual(db._deleted_show_rewatch_ids(), set())


class _DeletedTablesSession(_FakeSession):
    def _deleted_tables(self) -> list:
        return [s.table.name for s in self.executed_statements if isinstance(s, Delete)]


class ClearHistoryAndUnwatchShowRewatchCleanupTests(unittest.IsolatedAsyncioTestCase):
    """Regression tests for: clearing all watch history (or removing all
    history for a whole show) left any active ShowRewatch behind, orphaned
    at 0 progress forever - it has no direct link to WatchEvent, so nothing
    cascaded it away automatically the way RewatchProgress does."""

    async def test_clear_history_also_deletes_active_rewatches(self):
        db = _DeletedTablesSession([])
        response = await history.clear_history(db=db, current_user=SimpleNamespace(id=1))
        self.assertEqual(response["status"], "ok")
        self.assertEqual(db._deleted_tables(), ["show_rewatches", "watch_events", "playback_progress"])

    async def test_unwatch_show_also_deletes_the_shows_active_rewatch(self):
        show = Show(id=55, tmdb_id=100, title="Test Show")
        db = _DeletedTablesSession([
            show,        # show lookup
            [(11,), (12,)],  # episode id rows
        ])
        with patch("routers.history._push_watch_state", new_callable=AsyncMock):
            response = await history.unwatch_show(series_tmdb_id=100, db=db, current_user=SimpleNamespace(id=1))
        self.assertEqual(response["status"], "ok")
        self.assertEqual(db._deleted_tables(), ["show_rewatches", "watch_events"])


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _EventsFakeSession:
    """Each queued item is a ready-to-use result object (_RowsResult or
    _ScalarResult) rather than a raw value, since get_item_events calls
    both .all() and .scalar_one_or_none() on different queries."""

    def __init__(self, results):
        self._results = list(results)

    async def execute(self, stmt):
        return self._results.pop(0)


class GetItemEventsTests(unittest.IsolatedAsyncioTestCase):
    """Regression tests for: deleting a rewatch-cycle play left an episode
    that also had an older, pre-rewatch play looking "watched" in the modal
    and on the ActionBar, because watched status was being read off raw
    event history (len(events) > 0) instead of the active rewatch's own
    progress."""

    async def test_watched_false_when_no_events(self):
        db = _EventsFakeSession([_RowsResult([])])
        result = await history.get_item_events(
            tmdb_id=1, media_type=MediaType.movie, series_tmdb_id=None,
            db=db, current_user=SimpleNamespace(id=1),
        )
        self.assertEqual(result, {"watched": False, "events": []})

    async def test_watched_true_from_raw_history_when_no_active_rewatch(self):
        event = WatchEvent(id=9, watched_at=None)
        db = _EventsFakeSession([
            _RowsResult([(event, 200)]),  # the join query: (WatchEvent, media_id)
            _ScalarResult(Show(id=55, tmdb_id=100, title="Test Show")),  # show lookup
            _ScalarResult(None),  # get_active_rewatch -> none
        ])
        result = await history.get_item_events(
            tmdb_id=5000, media_type=MediaType.episode, series_tmdb_id=100,
            db=db, current_user=SimpleNamespace(id=1),
        )
        self.assertTrue(result["watched"])
        self.assertEqual([e["id"] for e in result["events"]], [9])

    async def test_watched_false_when_old_play_survives_but_rewatch_progress_was_removed(self):
        """The exact reported bug: an episode has one old (pre-rewatch) play
        left after the user deleted the rewatch-cycle one - it must read as
        unwatched while the rewatch is active, even though history isn't empty."""
        event = WatchEvent(id=9, watched_at=None)
        rewatch = ShowRewatch(id=7, user_id=1, show_id=55)
        db = _EventsFakeSession([
            _RowsResult([(event, 200)]),
            _ScalarResult(Show(id=55, tmdb_id=100, title="Test Show")),
            _ScalarResult(rewatch),
            _ScalarResult(None),  # no RewatchProgress row for this episode
        ])
        result = await history.get_item_events(
            tmdb_id=5000, media_type=MediaType.episode, series_tmdb_id=100,
            db=db, current_user=SimpleNamespace(id=1),
        )
        self.assertFalse(result["watched"])
        self.assertEqual(len(result["events"]), 1)

    async def test_watched_true_when_rewatch_progress_exists(self):
        event = WatchEvent(id=9, watched_at=None)
        rewatch = ShowRewatch(id=7, user_id=1, show_id=55)
        db = _EventsFakeSession([
            _RowsResult([(event, 200)]),
            _ScalarResult(Show(id=55, tmdb_id=100, title="Test Show")),
            _ScalarResult(rewatch),
            _ScalarResult(42),  # RewatchProgress.id found
        ])
        result = await history.get_item_events(
            tmdb_id=5000, media_type=MediaType.episode, series_tmdb_id=100,
            db=db, current_user=SimpleNamespace(id=1),
        )
        self.assertTrue(result["watched"])


class _UnwatchFakeSession:
    def __init__(self, results, rowcount=1):
        self._results = list(results)
        self.executed_statements = []
        self._rowcount = rowcount

    async def execute(self, stmt):
        self.executed_statements.append(stmt)
        if isinstance(stmt, Delete):
            # Mimic a real Result: _handle_unwatch_toggle reads rowcount to
            # report whether state actually changed (#190 follow-up).
            return SimpleNamespace(rowcount=self._rowcount)
        item = self._results.pop(0) if self._results else None
        return _ScalarResult(item)

    def _deleted_tables(self):
        return [stmt.table.name for stmt in self.executed_statements if isinstance(stmt, Delete)]


class HandleUnwatchToggleTests(unittest.IsolatedAsyncioTestCase):
    """Regression tests for: a media server reporting "marked unwatched"
    (currently only Jellyfin's webhook plugin sends this) used to delete ALL
    watch history for the item unconditionally - including plays from before
    an active rewatch started."""

    async def test_movie_deletes_all_watch_events(self):
        movie = Media(id=10, media_type=MediaType.movie, title="A Movie")
        db = _UnwatchFakeSession([])
        await _handle_unwatch_toggle(db, user_id=1, media=movie)
        self.assertEqual(db._deleted_tables(), ["watch_events"])

    async def test_episode_without_active_rewatch_deletes_all_watch_events(self):
        episode = Media(id=11, media_type=MediaType.episode, show_id=55, season_number=1, episode_number=1)
        db = _UnwatchFakeSession([None])  # get_active_rewatch -> none
        await _handle_unwatch_toggle(db, user_id=1, media=episode)
        self.assertEqual(db._deleted_tables(), ["watch_events"])

    async def test_episode_with_active_rewatch_only_removes_progress(self):
        episode = Media(id=12, media_type=MediaType.episode, show_id=55, season_number=1, episode_number=1)
        rewatch = ShowRewatch(id=7, user_id=1, show_id=55)
        db = _UnwatchFakeSession([rewatch])
        await _handle_unwatch_toggle(db, user_id=1, media=episode)
        self.assertEqual(db._deleted_tables(), ["rewatch_progress"])

    async def test_returns_true_when_a_row_was_deleted(self):
        # Regression for #190 follow-up: callers gate the outbound
        # _push_watch_state re-push on this return value to avoid re-pushing
        # (and potentially ping-ponging between two two-way-sync connections)
        # on a no-op delivery.
        movie = Media(id=13, media_type=MediaType.movie, title="A Movie")
        db = _UnwatchFakeSession([], rowcount=1)
        changed = await _handle_unwatch_toggle(db, user_id=1, media=movie)
        self.assertTrue(changed)

    async def test_returns_false_when_already_unwatched(self):
        # Item was already unwatched (e.g. a duplicate/echoed webhook delivery)
        # - nothing to delete, so no row is actually affected.
        movie = Media(id=14, media_type=MediaType.movie, title="A Movie")
        db = _UnwatchFakeSession([], rowcount=0)
        changed = await _handle_unwatch_toggle(db, user_id=1, media=movie)
        self.assertFalse(changed)


class IsFreshRewatchPlayTests(unittest.TestCase):
    """Regression tests for: a full-library sync used to skip creating a new
    WatchEvent for any episode that already had watch history - true of
    almost every rewatch by definition - so rewatch progress never advanced
    from a sync, only from being online for the live playback webhook."""

    def test_false_when_not_already_recorded(self):
        # sync_items already lets a never-before-seen play through on its own -
        # this helper must not also claim it, or it'd double-count.
        rewatch = ShowRewatch(id=1, show_id=55, started_at=datetime(2026, 1, 1))
        self.assertFalse(is_fresh_rewatch_play(False, MediaType.episode, 55, 1, {55: rewatch}, set(), datetime(2026, 1, 2)))

    def test_false_for_movies(self):
        self.assertFalse(is_fresh_rewatch_play(True, MediaType.movie, None, 1, {}, set(), datetime(2026, 1, 2)))

    def test_false_without_show_id(self):
        self.assertFalse(is_fresh_rewatch_play(True, MediaType.episode, None, 1, {}, set(), datetime(2026, 1, 2)))

    def test_false_without_active_rewatch_for_show(self):
        self.assertFalse(is_fresh_rewatch_play(True, MediaType.episode, 55, 1, {}, set(), datetime(2026, 1, 2)))

    def test_false_when_already_progressed_this_cycle(self):
        rewatch = ShowRewatch(id=7, show_id=55, started_at=datetime(2026, 1, 1))
        self.assertFalse(is_fresh_rewatch_play(True, MediaType.episode, 55, 1, {55: rewatch}, {1}, datetime(2026, 1, 2)))

    def test_false_without_a_last_played_date(self):
        rewatch = ShowRewatch(id=7, show_id=55, started_at=datetime(2026, 1, 1))
        self.assertFalse(is_fresh_rewatch_play(True, MediaType.episode, 55, 1, {55: rewatch}, set(), None))

    def test_false_when_last_played_predates_the_rewatch(self):
        # The server's played flag stays true forever - an old play must not
        # be mistaken for a fresh rewatch play just because progress is empty.
        rewatch = ShowRewatch(id=7, show_id=55, started_at=datetime(2026, 1, 1))
        self.assertFalse(is_fresh_rewatch_play(True, MediaType.episode, 55, 1, {55: rewatch}, set(), datetime(2025, 6, 1)))

    def test_true_when_last_played_is_after_the_rewatch_started(self):
        rewatch = ShowRewatch(id=7, show_id=55, started_at=datetime(2026, 1, 1))
        self.assertTrue(is_fresh_rewatch_play(True, MediaType.episode, 55, 1, {55: rewatch}, set(), datetime(2026, 1, 2)))


class GetAlreadyWatchedForBulkMarkTests(unittest.IsolatedAsyncioTestCase):
    """Regression tests for: mark_season_watched/mark_show_watched used to
    skip every episode that already had watch history, so clicking "mark
    season as watched" during an active rewatch silently did nothing for a
    season that had already been watched before the rewatch - which is the
    common case, since that's the point of a rewatch."""

    async def test_empty_media_ids_short_circuits(self):
        db = _EventsFakeSession([])
        result = await get_already_watched_for_bulk_mark(db, user_id=1, show=Show(id=55), media_ids=[])
        self.assertEqual(result, set())

    async def test_without_active_rewatch_uses_full_history(self):
        db = _EventsFakeSession([
            _ScalarResult(None),  # get_active_rewatch -> none
            _RowsResult([(10,), (11,)]),  # raw WatchEvent.media_id query
        ])
        result = await get_already_watched_for_bulk_mark(db, user_id=1, show=Show(id=55), media_ids=[10, 11, 12])
        self.assertEqual(result, {10, 11})

    async def test_with_active_rewatch_uses_only_that_rewatchs_progress(self):
        """The exact reported bug: a season fully watched before the rewatch
        must NOT be treated as "already watched" here - only episodes already
        progressed for the active rewatch itself should be skipped."""
        rewatch = ShowRewatch(id=7, user_id=1, show_id=55)
        db = _EventsFakeSession([
            _ScalarResult(rewatch),  # get_active_rewatch -> active
            _RowsResult([(10,)]),  # only media_id 10 progressed so far this cycle
        ])
        result = await get_already_watched_for_bulk_mark(db, user_id=1, show=Show(id=55), media_ids=[10, 11, 12])
        # 11 and 12 are NOT skipped even though (per the old bug) they'd
        # certainly have raw history from before the rewatch - only 10 has
        # been counted for this specific cycle.
        self.assertEqual(result, {10})


if __name__ == "__main__":
    unittest.main()

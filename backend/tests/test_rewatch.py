import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from sqlalchemy.sql.dml import Delete

from core.rewatch import capped_season_episode_counts, total_aired_episodes, record_rewatch_progress
from models.base import MediaType
from models.events import WatchEvent
from models.media import Media
from models.rewatch import ShowRewatch
from models.show import Show
from routers import history


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


if __name__ == "__main__":
    unittest.main()

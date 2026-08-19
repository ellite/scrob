import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core import credits
from core.credits import CREDITS_TTL, _people, credits_stats, maybe_backfill_credits
from models.base import MediaType


class PeopleHelperTests(unittest.TestCase):
    """_people() reduces a raw TMDB cast/crew/company list to unique {id, name}
    pairs - the single place dedup, job filtering and the cast size cap live."""

    def test_deduplicates_by_id(self):
        raw = [{"id": 1, "name": "A"}, {"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
        self.assertEqual(_people(raw), [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}])

    def test_entries_missing_an_id_are_dropped(self):
        raw = [{"id": None, "name": "Unknown"}, {"id": 1, "name": "A"}]
        self.assertEqual(_people(raw), [{"id": 1, "name": "A"}])

    def test_jobs_filter_keeps_only_matching_crew(self):
        raw = [
            {"id": 1, "name": "Director", "job": "Director"},
            {"id": 2, "name": "Writer", "job": "Screenplay"},
            {"id": 3, "name": "Gaffer", "job": "Gaffer"},
        ]
        self.assertEqual(_people(raw, jobs={"Director"}), [{"id": 1, "name": "Director"}])

    def test_limit_stops_early(self):
        raw = [{"id": i, "name": str(i)} for i in range(20)]
        self.assertEqual(len(_people(raw, limit=10)), 10)

    def test_none_input_is_empty(self):
        self.assertEqual(_people(None), [])


class _AllResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _ScalarOneResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _QueuedSession:
    """Returns canned results from execute() in call order - matches how
    credits_stats/maybe_backfill_credits issue a small, fixed sequence of
    queries, same fake-session style used elsewhere in tests/test_sync.py."""

    def __init__(self, results):
        self._results = list(results)

    async def execute(self, stmt):
        return self._results.pop(0)


class _Credit:
    def __init__(self, media_type, tmdb_id, cast=None, directors=None, writers=None, studios=None):
        self.media_type = media_type
        self.tmdb_id = tmdb_id
        self.cast = cast or []
        self.directors = directors or []
        self.writers = writers or []
        self.studios = studios or []


class CreditsStatsTests(unittest.IsolatedAsyncioTestCase):
    """credits_stats() weights each watched title by its play count, then has
    every title contribute its people once - so a rewatched-a-lot show doesn't
    drown out actors who simply appear in more distinct titles."""

    async def test_no_watched_titles_short_circuits_without_querying_credits(self):
        db = _QueuedSession([_AllResult([])])
        result = await credits_stats(db, user_id=1, date_filters=[])
        self.assertEqual(result, {"actors": [], "directors": [], "writers": [], "studios": []})
        # Only the weight query should have run - the empty-weight path must
        # return before touching title_credits at all.
        self.assertEqual(db._results, [])

    async def test_movie_and_episode_rows_are_weighted_by_their_own_or_show_tmdb_id(self):
        rows = [
            (MediaType.movie, 100, None, 3),
            (MediaType.episode, 555, 200, 5),  # episode's own tmdb_id is irrelevant; show_tmdb matters
        ]
        movie_credit = _Credit("movie", 100, cast=[{"id": 1, "name": "Actor A"}])
        show_credit = _Credit("series", 200, cast=[{"id": 1, "name": "Actor A"}, {"id": 2, "name": "Actor B"}])
        db = _QueuedSession([_AllResult(rows), _ScalarsResult([movie_credit, show_credit])])

        result = await credits_stats(db, user_id=1, date_filters=[])

        # Actor A appears in both titles: 2 titles, 3 + 5 = 8 plays.
        actor_a = next(a for a in result["actors"] if a["id"] == 1)
        self.assertEqual(actor_a["titles"], 2)
        self.assertEqual(actor_a["plays"], 8)
        # Actor B appears only in the show: 1 title, 5 plays.
        actor_b = next(a for a in result["actors"] if a["id"] == 2)
        self.assertEqual(actor_b["titles"], 1)
        self.assertEqual(actor_b["plays"], 5)

    async def test_ranking_is_by_distinct_titles_before_play_count(self):
        # Person 1: fewer titles but far more plays; person 2: more titles.
        # Titles must win the tiebreak, or a single heavily-rewatched show
        # would drown out someone who actually shows up across the library.
        rows = [
            (MediaType.movie, 1, None, 50),
            (MediaType.movie, 2, None, 1),
            (MediaType.movie, 3, None, 1),
        ]
        credits_rows = [
            _Credit("movie", 1, directors=[{"id": 1, "name": "Heavy Rewatch"}]),
            _Credit("movie", 2, directors=[{"id": 2, "name": "Prolific"}]),
            _Credit("movie", 3, directors=[{"id": 2, "name": "Prolific"}]),
        ]
        db = _QueuedSession([_AllResult(rows), _ScalarsResult(credits_rows)])

        result = await credits_stats(db, user_id=1, date_filters=[])

        self.assertEqual(result["directors"][0]["id"], 2)
        self.assertEqual(result["directors"][0]["titles"], 2)

    async def test_titles_missing_credits_are_skipped_not_erroring(self):
        rows = [(MediaType.movie, 999, None, 1)]
        db = _QueuedSession([_AllResult(rows), _ScalarsResult([])])
        result = await credits_stats(db, user_id=1, date_filters=[])
        self.assertEqual(result, {"actors": [], "directors": [], "writers": [], "studios": []})

    async def test_top_list_is_capped_at_fifteen(self):
        rows = [(MediaType.movie, i, None, 1) for i in range(20)]
        credits_rows = [_Credit("movie", i, cast=[{"id": i, "name": str(i)}]) for i in range(20)]
        db = _QueuedSession([_AllResult(rows), _ScalarsResult(credits_rows)])
        result = await credits_stats(db, user_id=1, date_filters=[])
        self.assertEqual(len(result["actors"]), 15)


class MaybeBackfillCreditsTests(unittest.IsolatedAsyncioTestCase):
    """maybe_backfill_credits() decides whether to kick off the background
    TMDB import: skip when fresh, skip without a usable key, and - the
    regression this guards against - never schedule two imports at once when
    two requests race in on the same stale cache."""

    def setUp(self):
        credits._importing = False
        self.addCleanup(setattr, credits, "_importing", False)

    @staticmethod
    def _swallowing_create_task(coro):
        # asyncio.create_task is mocked out in these tests (we're asserting on
        # scheduling decisions, not actually running the background import),
        # so close the coroutine ourselves to avoid a "never awaited" warning.
        coro.close()
        return AsyncMock()

    async def test_fresh_cache_skips_without_checking_the_key(self):
        db = _QueuedSession([_ScalarOneResult(datetime.utcnow())])
        with patch("routers.media.get_user_tmdb_key", new_callable=AsyncMock) as get_key:
            await maybe_backfill_credits(db, user_id=1)
        get_key.assert_not_called()
        self.assertFalse(credits._importing)

    async def test_stale_cache_without_a_usable_key_does_not_import(self):
        db = _QueuedSession([_ScalarOneResult(datetime.utcnow() - CREDITS_TTL - timedelta(days=1))])
        with patch("routers.media.get_user_tmdb_key", new_callable=AsyncMock, return_value=None), \
             patch("routers.media.check_tmdb_key", return_value=False), \
             patch("core.credits.asyncio.create_task") as create_task:
            await maybe_backfill_credits(db, user_id=1)
        create_task.assert_not_called()
        self.assertFalse(credits._importing)

    async def test_stale_cache_with_a_key_schedules_exactly_one_import(self):
        db = _QueuedSession([_ScalarOneResult(None)])
        with patch("routers.media.get_user_tmdb_key", new_callable=AsyncMock, return_value="key"), \
             patch("routers.media.check_tmdb_key", return_value=True), \
             patch("core.credits.asyncio.create_task", side_effect=self._swallowing_create_task) as create_task:
            await maybe_backfill_credits(db, user_id=1)
        create_task.assert_called_once()
        self.assertTrue(credits._importing)

    async def test_two_concurrent_callers_only_schedule_one_import(self):
        # Regression: the claim used to happen inside the scheduled task
        # itself, which runs on the next event-loop tick - leaving a window
        # where a second request, arriving before that tick, would also see
        # _importing == False and schedule its own duplicate import.
        db1 = _QueuedSession([_ScalarOneResult(None)])
        db2 = _QueuedSession([_ScalarOneResult(None)])
        with patch("routers.media.get_user_tmdb_key", new_callable=AsyncMock, return_value="key"), \
             patch("routers.media.check_tmdb_key", return_value=True), \
             patch("core.credits.asyncio.create_task", side_effect=self._swallowing_create_task) as create_task:
            await maybe_backfill_credits(db1, user_id=1)
            await maybe_backfill_credits(db2, user_id=2)
        create_task.assert_called_once()


if __name__ == "__main__":
    unittest.main()

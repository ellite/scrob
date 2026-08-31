import json
import os
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from routers.history import _compute_next_episode, _format_media_item, _group_last_watched, _has_aired, _has_confirmed_air_date, _next_up_needs_live_fetch, _remaining_episode_stats, _stream_next_up_refresh
from core.rewatch import capped_season_episode_counts
from models.base import MediaType


class ComputeNextEpisodeTests(unittest.TestCase):
    """Regression tests for #64: Kodi has no library sync, so the next episode's
    Media row often doesn't exist locally yet. _compute_next_episode is the pure
    logic get_next_up uses to figure out what that next episode is from the
    show's TMDB season metadata, so it can be created/enriched on demand."""

    def test_next_episode_within_same_season(self):
        seasons = [{"season_number": 1, "episode_count": 12}, {"season_number": 2, "episode_count": 10}]
        self.assertEqual(_compute_next_episode(seasons, 1, 11), (1, 12))

    def test_rolls_over_into_next_season(self):
        seasons = [{"season_number": 1, "episode_count": 12}, {"season_number": 2, "episode_count": 10}]
        self.assertEqual(_compute_next_episode(seasons, 1, 12), (2, 1))

    def test_skips_empty_seasons_when_rolling_over(self):
        # A season with 0 known episodes (e.g. announced but not yet aired) must
        # not be returned as "next" — the real next episode is one season further.
        seasons = [
            {"season_number": 1, "episode_count": 12},
            {"season_number": 2, "episode_count": 0},
            {"season_number": 3, "episode_count": 8},
        ]
        self.assertEqual(_compute_next_episode(seasons, 1, 12), (3, 1))

    def test_returns_none_at_series_end(self):
        seasons = [{"season_number": 1, "episode_count": 12}]
        self.assertIsNone(_compute_next_episode(seasons, 1, 12))

    def test_specials_season_zero_is_never_returned_and_never_used_as_current(self):
        seasons = [{"season_number": 0, "episode_count": 5}, {"season_number": 1, "episode_count": 12}]
        self.assertEqual(_compute_next_episode(seasons, 0, 3), (1, 1))


class GroupLastWatchedTests(unittest.TestCase):
    """Regression tests for #108: rows with a NULL watched_at (e.g. imported
    history with no date) must not blow up the datetime comparison that finds
    each show's most recent watch."""

    def test_null_watched_at_row_processed_first_does_not_crash(self):
        rows = [
            (1, 1, 5, None),
            (1, 1, 4, datetime(2026, 1, 1, tzinfo=timezone.utc)),
        ]
        last_per_show, last_watched_at = _group_last_watched(rows)
        self.assertEqual(last_per_show[1], (1, 5))
        self.assertEqual(last_watched_at[1], datetime(2026, 1, 1, tzinfo=timezone.utc))

    def test_show_with_only_null_watched_at_rows_has_no_entry(self):
        rows = [(1, 1, 2, None), (1, 1, 1, None)]
        last_per_show, last_watched_at = _group_last_watched(rows)
        self.assertEqual(last_per_show[1], (1, 2))
        self.assertNotIn(1, last_watched_at)

    def test_keeps_most_recent_watched_at_across_rows(self):
        older = datetime(2025, 1, 1, tzinfo=timezone.utc)
        newer = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = [(1, 1, 2, older), (1, 1, 1, newer)]
        last_per_show, last_watched_at = _group_last_watched(rows)
        self.assertEqual(last_watched_at[1], newer)

    def test_null_season_row_is_skipped_not_used_as_last_watched(self):
        # Regression for #132: a faulty history entry with a NULL season (e.g.
        # a pre-fix Season-0 scrobble) must not become a show's "furthest
        # watched" position — get_next_up would later pass that None straight
        # into an int comparison and crash the whole endpoint.
        rows = [
            (1, None, 3, datetime(2026, 1, 2, tzinfo=timezone.utc)),
            (1, 1, 5, datetime(2026, 1, 1, tzinfo=timezone.utc)),
        ]
        last_per_show, last_watched_at = _group_last_watched(rows)
        self.assertEqual(last_per_show[1], (1, 5))

    def test_show_with_only_null_season_rows_has_no_entry(self):
        rows = [(1, None, 1, None), (1, None, 2, None)]
        last_per_show, last_watched_at = _group_last_watched(rows)
        self.assertNotIn(1, last_per_show)


class NextUpMediaFormattingTests(unittest.TestCase):
    """Next Up exposes the timestamp used to order shows (#237)."""

    def _media(self):
        return SimpleNamespace(
            id=7,
            tmdb_id=42,
            media_type=MediaType.episode,
            title="Episode 3",
            overview="Overview",
            poster_path=None,
            backdrop_path=None,
            release_date="2026-01-01",
            tmdb_rating=8.0,
            season_number=1,
            episode_number=3,
            runtime=45,
            tagline=None,
            tmdb_data={},
            show_id=9,
            show=None,
        )

    def test_last_watched_at_is_serialized_as_iso_datetime(self):
        payload = _format_media_item(
            self._media(),
            last_watched_at=datetime(2026, 8, 31, 9, 30),
        )
        self.assertEqual(payload["last_watched_at"], "2026-08-31T09:30:00")

    def test_missing_last_watched_at_is_null(self):
        payload = _format_media_item(self._media())
        self.assertIsNone(payload["last_watched_at"])


class HasAiredTests(unittest.TestCase):
    """Regression tests for #104: Next Up must not suggest an episode before
    its air date."""

    def test_past_release_date_has_aired(self):
        self.assertTrue(_has_aired("2020-01-01", date(2026, 1, 1)))

    def test_todays_release_date_has_aired(self):
        self.assertTrue(_has_aired("2026-01-01", date(2026, 1, 1)))

    def test_future_release_date_has_not_aired(self):
        self.assertFalse(_has_aired("2026-06-01", date(2026, 1, 1)))

    def test_unknown_release_date_is_treated_as_aired(self):
        # We can't confirm it hasn't aired, so don't hide a show over missing
        # metadata — that would silently empty out someone's Next Up row.
        self.assertTrue(_has_aired(None, date(2026, 1, 1)))
        self.assertTrue(_has_aired("", date(2026, 1, 1)))


class HasConfirmedAirDateTests(unittest.TestCase):
    """Regression tests for #111: Next Up must not suggest an episode with no
    announced air date at all - unlike _has_aired's callers, there's nothing
    else confirming the episode is real yet, so "unknown" must not be treated
    as "safe to suggest"."""

    def test_past_release_date_has_aired(self):
        self.assertTrue(_has_confirmed_air_date("2020-01-01", date(2026, 1, 1)))

    def test_todays_release_date_has_aired(self):
        self.assertTrue(_has_confirmed_air_date("2026-01-01", date(2026, 1, 1)))

    def test_future_release_date_has_not_aired(self):
        self.assertFalse(_has_confirmed_air_date("2026-06-01", date(2026, 1, 1)))

    def test_unknown_release_date_is_not_treated_as_aired(self):
        # The exact bug: an unannounced renewal placeholder (e.g. SNL UK
        # S2E1 in issue #111) must not be suggested just because its air
        # date is missing rather than in the future.
        self.assertFalse(_has_confirmed_air_date(None, date(2026, 1, 1)))
        self.assertFalse(_has_confirmed_air_date("", date(2026, 1, 1)))


if __name__ == "__main__":
    unittest.main()


class RemainingEpisodeStatsTests(unittest.TestCase):
    """Feature #170: episodes-left / remaining-runtime estimate on Next Up."""

    def test_basic_remaining_count_and_runtime(self):
        stats = _remaining_episode_stats(
            {1: 12, 2: 10}, {1: 12, 2: 4}, avg_runtime=30.0
        )
        self.assertEqual(stats["episodes_left"], 6)
        self.assertEqual(stats["remaining_runtime"], 180)

    def test_specials_are_excluded(self):
        stats = _remaining_episode_stats(
            {0: 5, 1: 10}, {0: 5, 1: 3}, avg_runtime=None
        )
        self.assertEqual(stats["episodes_left"], 7)
        self.assertIsNone(stats["remaining_runtime"])

    def test_watched_capped_per_season(self):
        # Provider numbering mismatch: more local watched rows than TMDB says
        # the season has must not push the remainder of other seasons down.
        stats = _remaining_episode_stats(
            {1: 8, 2: 8}, {1: 12, 2: 0}, avg_runtime=45.0
        )
        self.assertEqual(stats["episodes_left"], 8)

    def test_clamped_to_one_when_metadata_is_stale(self):
        # The caller only asks about shows with an aired unwatched episode, so
        # stale TMDB counts saying "all watched" still yield 1, never 0.
        stats = _remaining_episode_stats({1: 10}, {1: 10}, avg_runtime=40.0)
        self.assertEqual(stats["episodes_left"], 1)
        self.assertEqual(stats["remaining_runtime"], 40)

    def test_no_aired_episodes_returns_none(self):
        self.assertIsNone(_remaining_episode_stats({}, {}, avg_runtime=30.0))
        self.assertIsNone(_remaining_episode_stats({0: 3}, {}, avg_runtime=30.0))


class CappedSeasonCountsFromCacheTests(unittest.TestCase):
    """#296: unaired episodes must not be counted on the Next Up card."""

    class _Show:
        def __init__(self, tmdb_data):
            self.tmdb_data = tmdb_data

    def _show(self, last_ep):
        data = {"seasons": [{"season_number": 1, "episode_count": 10},
                            {"season_number": 2, "episode_count": 10}]}
        if last_ep is not None:
            data["last_episode_to_air"] = last_ep
        return self._Show(data)

    def test_current_season_capped_at_last_aired_episode(self):
        # Season 2 is mid-flight: 8 of 10 episodes out.
        counts = capped_season_episode_counts(
            self._show({"season_number": 2, "episode_number": 8})
        )
        self.assertEqual(counts[2], 8)
        # 7 watched of the 8 aired -> exactly 1 left, not 3.
        stats = _remaining_episode_stats(counts, {1: 10, 2: 7}, avg_runtime=51.0)
        self.assertEqual(stats["episodes_left"], 1)

    def test_future_seasons_are_zeroed(self):
        counts = capped_season_episode_counts(
            self._show({"season_number": 1, "episode_number": 4})
        )
        self.assertEqual(counts[1], 4)
        self.assertEqual(counts[2], 0)

    def test_fractional_average_runtime_rounds(self):
        stats = _remaining_episode_stats({1: 4}, {1: 1}, avg_runtime=42.5)
        self.assertEqual(stats["episodes_left"], 3)
        self.assertEqual(stats["remaining_runtime"], 128)

class NeedsLiveFetchTests(unittest.TestCase):
    """Regression tests for #332 (follow-up to #294/#307): the missing-episode
    fallback used to live-fetch every fully-watched show from TMDB on every
    cold load - minutes on a big library. _next_up_needs_live_fetch is the
    gate that decides, from the Show row alone, whether the stored tmdb_data
    snapshot can answer "did a new episode appear?" or TMDB must be asked."""

    TODAY = date(2026, 3, 15)
    NOW = datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)

    class _Show:
        def __init__(self, status="Returning Series", tmdb_id=42, tmdb_data=None):
            self.status = status
            self.tmdb_id = tmdb_id
            self.tmdb_data = tmdb_data

    def _needs(self, show):
        return _next_up_needs_live_fetch(show, self.TODAY, self.NOW)

    def _fresh(self):
        return (self.NOW - timedelta(hours=1)).isoformat()

    def _stale(self):
        return (self.NOW - timedelta(days=3)).isoformat()

    # --- the #332 case: finished shows must not cost a TMDB call ---

    def test_finished_show_with_snapshot_is_not_fetched(self):
        show = self._Show(status="Ended", tmdb_data={"seasons": [{"season_number": 1, "episode_count": 10}]})
        self.assertFalse(self._needs(show))

    def test_canceled_show_with_snapshot_is_not_fetched(self):
        show = self._Show(status="Canceled", tmdb_data={"seasons": []})
        self.assertFalse(self._needs(show))

    def test_finished_show_without_seasons_key_is_fetched_once_to_build_snapshot(self):
        # Status set by some path that never stored a snapshot: fetching once
        # writes a "seasons" key (possibly empty), after which this stops.
        show = self._Show(status="Ended", tmdb_data={})
        self.assertTrue(self._needs(show))
        show_no_data = self._Show(status="Ended", tmdb_data=None)
        self.assertTrue(self._needs(show_no_data))

    def test_revived_show_is_fetched_again(self):
        # The Futurama case (#307): the daily sweep flips status away from
        # Ended - the gate must then treat the show as live again. The sweep
        # also rewrites refreshed_at, but a revival mid-window must not wait
        # out the staleness threshold if the snapshot is old.
        show = self._Show(status="Returning Series", tmdb_data={"seasons": [], "refreshed_at": self._stale()})
        self.assertTrue(self._needs(show))

    # --- returning shows: fetch only when something new can exist ---

    def test_future_next_air_date_with_fresh_snapshot_is_not_fetched(self):
        show = self._Show(tmdb_data={
            "seasons": [],
            "next_episode_to_air": {"air_date": "2026-04-01"},
            "refreshed_at": self._fresh(),
        })
        self.assertFalse(self._needs(show))

    def test_next_air_date_today_is_fetched(self):
        show = self._Show(tmdb_data={
            "seasons": [],
            "next_episode_to_air": {"air_date": "2026-03-15"},
            "refreshed_at": self._fresh(),
        })
        self.assertTrue(self._needs(show))

    def test_past_next_air_date_is_fetched(self):
        show = self._Show(tmdb_data={
            "seasons": [],
            "next_episode_to_air": {"air_date": "2026-03-01"},
            "refreshed_at": self._fresh(),
        })
        self.assertTrue(self._needs(show))

    def test_no_scheduled_episode_with_fresh_snapshot_is_not_fetched(self):
        show = self._Show(tmdb_data={"seasons": [], "refreshed_at": self._fresh()})
        self.assertFalse(self._needs(show))

    def test_stale_snapshot_is_fetched_even_with_future_air_date(self):
        # #287's lesson: never trust a snapshot indefinitely. Even a stored
        # future air date might have been moved up since the snapshot was
        # written, so staleness alone forces a re-fetch.
        show = self._Show(tmdb_data={
            "seasons": [],
            "next_episode_to_air": {"air_date": "2026-04-01"},
            "refreshed_at": self._stale(),
        })
        self.assertTrue(self._needs(show))

    def test_snapshot_without_refreshed_at_is_fetched(self):
        # Pre-upgrade rows have no refreshed_at: they keep today's
        # always-fetch behavior until something writes the stamp.
        show = self._Show(tmdb_data={"seasons": []})
        self.assertTrue(self._needs(show))
        self.assertTrue(self._needs(self._Show(tmdb_data=None)))

    def test_malformed_refreshed_at_is_fetched(self):
        show = self._Show(tmdb_data={"seasons": [], "refreshed_at": "not-a-date"})
        self.assertTrue(self._needs(show))

    def test_naive_refreshed_at_is_treated_as_utc_not_crashed_on(self):
        naive_fresh = (self.NOW - timedelta(hours=1)).replace(tzinfo=None).isoformat()
        show = self._Show(tmdb_data={"seasons": [], "refreshed_at": naive_fresh})
        self.assertFalse(self._needs(show))

    # --- shows this path must never touch ---

    def test_show_without_tmdb_id_is_never_fetched(self):
        self.assertFalse(self._needs(self._Show(tmdb_id=None, tmdb_data=None)))
        self.assertFalse(self._needs(None))

    def test_tvdb_sourced_snapshot_is_never_fetched(self):
        # A TVDB-sourced snapshot's season layout is TVDB-shaped (#335);
        # fetching would later overwrite it with TMDB-shaped data. Applies no
        # matter how stale it is or what status it carries.
        show = self._Show(tmdb_data={"seasons": [], "source": "tvdb", "refreshed_at": self._stale()})
        self.assertFalse(self._needs(show))


class ApplyShowMetadataSnapshotTests(unittest.TestCase):
    """The Next Up gate above only works if apply_show_metadata actually
    writes the fields it reads - these pin that contract (#332)."""

    class _Show:
        def __init__(self):
            self.title = "Old"
            self.status = None
            self.tmdb_data = None

    def test_snapshot_carries_gating_fields(self):
        from routers.shows import apply_show_metadata

        show = self._Show()
        next_ep = {"air_date": "2026-04-01", "season_number": 2, "episode_number": 1}
        apply_show_metadata(show, {"name": "New", "status": "Returning Series",
                                   "next_episode_to_air": next_ep, "seasons": []})
        self.assertEqual(show.tmdb_data["next_episode_to_air"], next_ep)
        refreshed = datetime.fromisoformat(show.tmdb_data["refreshed_at"])
        self.assertIsNotNone(refreshed.tzinfo)
        self.assertLess((datetime.now(timezone.utc) - refreshed).total_seconds(), 60)

    def test_snapshot_always_has_seasons_key(self):
        # The finals branch of _next_up_needs_live_fetch uses "seasons" being
        # absent as "never snapshotted, fetch once" - so a rebuild for a show
        # TMDB reports no seasons for must still write the key, or that show
        # would be re-fetched on every load forever.
        from routers.shows import apply_show_metadata

        show = self._Show()
        apply_show_metadata(show, {"name": "New", "status": "Ended"})
        self.assertIn("seasons", show.tmdb_data)
        self.assertIn("next_episode_to_air", show.tmdb_data)


class _FakeScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    """Minimal async session stand-in for _stream_next_up_refresh: one execute()
    returning the show rows, plus commit() call counting."""

    def __init__(self, rows):
        self._rows = rows
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *a, **k):
        return _FakeScalarResult(self._rows)

    async def commit(self):
        self.commits += 1


def _show(tmdb_id=1, tmdb_data=None):
    return SimpleNamespace(id=tmdb_id, tmdb_id=tmdb_id, status="Returning Series", tmdb_data=tmdb_data)


async def _collect(user_id, api_key):
    return [json.loads(line) async for line in _stream_next_up_refresh(user_id, api_key)]


class StreamNextUpRefreshTests(unittest.IsolatedAsyncioTestCase):
    """The "Refresh from TMDB" button streams NDJSON progress while re-checking
    TMDB for every show the user watches, then rewrites each stored snapshot."""

    def _patches(self, rows, get_show):
        session = _FakeSession(rows)
        apply_meta = AsyncMock()  # patched as a plain callable below
        apply_calls = []
        return session, apply_calls, (
            patch("routers.history.check_tmdb_key", lambda k: bool(k)),
            patch("routers.history.AsyncSessionLocal", lambda: session),
            patch("routers.history.tmdb.get_show", get_show),
            patch("routers.shows.apply_show_metadata", lambda show, data: apply_calls.append(show.tmdb_id)),
        )

    async def test_no_tmdb_key_emits_single_terminal_line(self):
        session, _, patches = self._patches([], AsyncMock())
        with patches[0], patches[1], patches[2], patches[3]:
            msgs = await _collect(1, None)
        self.assertEqual(msgs, [{"total": 0, "complete": True, "error": "no_tmdb_key"}])

    async def test_no_watched_shows_completes_immediately(self):
        session, _, patches = self._patches([], AsyncMock())
        with patches[0], patches[1], patches[2], patches[3]:
            msgs = await _collect(1, "key")
        self.assertEqual(msgs, [{"total": 0}, {"done": 0, "total": 0, "complete": True}])

    async def test_progress_lines_and_snapshot_writes(self):
        rows = [_show(1, {}), _show(2, {}), _show(3, {})]
        get_show = AsyncMock(side_effect=lambda tmdb_id, **k: {"id": tmdb_id, "name": f"S{tmdb_id}"})
        session, apply_calls, patches = self._patches(rows, get_show)
        with patches[0], patches[1], patches[2], patches[3]:
            msgs = await _collect(1, "key")

        self.assertEqual(msgs[0], {"total": 3})
        self.assertEqual([m["done"] for m in msgs[1:4]], [1, 2, 3])
        self.assertEqual(msgs[-1], {"done": 3, "total": 3, "complete": True})
        self.assertEqual(sorted(apply_calls), [1, 2, 3])
        self.assertGreaterEqual(session.commits, 1)  # final commit

    async def test_tvdb_sourced_show_is_skipped(self):
        rows = [_show(1, {"source": "tvdb"}), _show(2, {})]
        get_show = AsyncMock(side_effect=lambda tmdb_id, **k: {"id": tmdb_id})
        session, apply_calls, patches = self._patches(rows, get_show)
        with patches[0], patches[1], patches[2], patches[3]:
            msgs = await _collect(1, "key")
        self.assertEqual(msgs[0], {"total": 1})
        self.assertEqual(apply_calls, [2])

    async def test_one_show_failing_still_completes_progress(self):
        rows = [_show(1, {}), _show(2, {})]

        async def flaky(tmdb_id, **k):
            if tmdb_id == 1:
                raise RuntimeError("TMDB 500")
            return {"id": tmdb_id}

        session, apply_calls, patches = self._patches(rows, AsyncMock(side_effect=flaky))
        with patches[0], patches[1], patches[2], patches[3]:
            msgs = await _collect(1, "key")
        self.assertEqual(msgs[0], {"total": 2})
        self.assertEqual(msgs[-1], {"done": 2, "total": 2, "complete": True})
        self.assertEqual(apply_calls, [2])  # only the one that succeeded

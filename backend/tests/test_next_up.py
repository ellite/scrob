import os
import unittest
from datetime import date, datetime, timezone

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from routers.history import _compute_next_episode, _group_last_watched, _has_aired, _has_confirmed_air_date, _next_up_status, _remaining_episode_stats
from models.media import Media
from models.show import Show


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


class NextUpStatusTests(unittest.TestCase):
    """Feature #195: precise, mutually-exclusive labels for Next Up cards."""

    def _episode(self, *, season=1, episode=2, release_date="2026-01-01", episode_count=None):
        media = Media(season_number=season, episode_number=episode, release_date=release_date)
        if episode_count is not None:
            media.show = Show(tmdb_data={"seasons": [{"season_number": season, "episode_count": episode_count}]})
        return media

    def test_finale_takes_precedence_over_new_today(self):
        self.assertEqual(
            _next_up_status(self._episode(episode=8, episode_count=8), date(2026, 1, 1)),
            "season_finale",
        )

    def test_premiere_takes_precedence_over_new_today(self):
        self.assertEqual(
            _next_up_status(self._episode(episode=1), date(2026, 1, 1)),
            "season_premiere",
        )

    def test_specials_do_not_receive_a_season_premiere_badge(self):
        self.assertEqual(
            _next_up_status(self._episode(season=0, episode=1, release_date="2025-12-31"), date(2026, 1, 1)),
            "next_episode",
        )

    def test_new_today_requires_an_exact_release_date_match(self):
        self.assertEqual(_next_up_status(self._episode(), date(2026, 1, 1)), "new_today")
        self.assertEqual(
            _next_up_status(self._episode(release_date=None), date(2026, 1, 1)),
            "next_episode",
        )
        self.assertEqual(
            _next_up_status(self._episode(release_date="2025-12-31"), date(2026, 1, 1)),
            "next_episode",
        )

    def test_finale_requires_explicit_matching_season_metadata(self):
        self.assertEqual(
            _next_up_status(self._episode(episode=8, episode_count=9), date(2026, 1, 1)),
            "new_today",
        )
        self.assertEqual(
            _next_up_status(self._episode(episode=8), date(2026, 1, 1)),
            "new_today",
        )


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

    def test_fractional_average_runtime_rounds(self):
        stats = _remaining_episode_stats({1: 4}, {1: 1}, avg_runtime=42.5)
        self.assertEqual(stats["episodes_left"], 3)
        self.assertEqual(stats["remaining_runtime"], 128)

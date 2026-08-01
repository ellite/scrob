import os
import unittest
from datetime import date, datetime, timezone

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from routers.history import _compute_next_episode, _group_last_watched, _has_aired


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


if __name__ == "__main__":
    unittest.main()

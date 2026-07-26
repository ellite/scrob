import os
import unittest

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from routers.history import _compute_next_episode


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


if __name__ == "__main__":
    unittest.main()

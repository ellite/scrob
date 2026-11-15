import os
import unittest

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core.episode_order import ORDER_AIRED, season_rating_order_values


class SeasonRatingOrderValuesTests(unittest.TestCase):
    """#563: season ratings are stored under the ordering they were made in, so
    the show page has to read the ratings for the ordering it is displaying."""

    def test_aired_order_is_stored_as_null(self):
        self.assertEqual(season_rating_order_values(ORDER_AIRED), [])
        self.assertEqual(season_rating_order_values(None), [])
        self.assertEqual(season_rating_order_values("tmdb"), [])

    def test_tvdb_official_also_matches_the_legacy_spelling(self):
        self.assertEqual(season_rating_order_values("tvdb:official"), ["tvdb:official", "tvdb"])
        self.assertEqual(season_rating_order_values("tvdb"), ["tvdb:official", "tvdb"])

    def test_other_orders_match_only_themselves(self):
        self.assertEqual(season_rating_order_values("tvdb:dvd"), ["tvdb:dvd"])
        self.assertEqual(season_rating_order_values("tmdb:group:abc"), ["tmdb:group:abc"])

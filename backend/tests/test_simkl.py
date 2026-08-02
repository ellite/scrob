import os
import unittest

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from routers.simkl import _simkl_rating_value


class SimklRatingValueTests(unittest.TestCase):
    """Regression tests for issue #112: Simkl's /sync/ratings response uses
    "user_rating" (most entries None, since it returns every item the user
    has, not just rated ones) - reading the wrong "rating" key made every
    entry look unrated and nothing ever imported."""

    def test_reads_user_rating_field(self):
        item = {"user_rating": 8, "status": "completed", "movie": {"title": "Fight Club"}}
        self.assertEqual(_simkl_rating_value(item), 8.0)

    def test_unrated_item_returns_none(self):
        item = {"user_rating": None, "status": "completed", "movie": {"title": "Fight Club"}}
        self.assertIsNone(_simkl_rating_value(item))

    def test_missing_field_returns_none(self):
        item = {"status": "completed", "movie": {"title": "Fight Club"}}
        self.assertIsNone(_simkl_rating_value(item))

    def test_generic_rating_key_is_ignored(self):
        """The bug itself: a stray "rating" key (used by Simkl's other,
        single-item rate endpoints) must not be mistaken for user_rating."""
        item = {"rating": 9, "user_rating": None}
        self.assertIsNone(_simkl_rating_value(item))

    def test_zero_rating_is_treated_as_unrated(self):
        # Simkl ratings are 1-10; a literal 0 isn't a real rating value.
        item = {"user_rating": 0}
        self.assertIsNone(_simkl_rating_value(item))


if __name__ == "__main__":
    unittest.main()

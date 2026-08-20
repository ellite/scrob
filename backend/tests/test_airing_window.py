import os
import unittest
from datetime import datetime, timezone

from pydantic import TypeAdapter, ValidationError

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from routers.media import _air_date_in_window, _airing_window
from typing import Literal


class AiringWindowTests(unittest.TestCase):
    def test_uses_the_browser_timezone_for_date_boundaries(self):
        start, end = _airing_window(
            "America/Los_Angeles",
            7,
            now=datetime(2026, 1, 8, 2, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(start.isoformat(), "2026-01-07")
        self.assertEqual(end.isoformat(), "2026-01-13")

    def test_window_is_inclusive_and_date_only(self):
        start, end = _airing_window("UTC", 7, now=datetime(2026, 1, 7, 12, tzinfo=timezone.utc))
        self.assertTrue(_air_date_in_window("2026-01-07", start, end))
        self.assertTrue(_air_date_in_window("2026-01-13", start, end))
        self.assertFalse(_air_date_in_window("2026-01-14", start, end))
        self.assertFalse(_air_date_in_window("2026-01-07T20:00:00Z", start, end))
        self.assertFalse(_air_date_in_window("not-a-date", start, end))

    def test_only_the_supported_homepage_window_values_are_valid(self):
        adapter = TypeAdapter(Literal[1, 7])
        self.assertEqual(adapter.validate_python(1), 1)
        self.assertEqual(adapter.validate_python(7), 7)
        with self.assertRaises(ValidationError):
            adapter.validate_python(2)

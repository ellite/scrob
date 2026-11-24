import os
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from core.calendar_ics import render_calendar
from db import get_db
from routers import calendar


def entry(**changes):
    return {"show_id": 7, "show_tmdb_id": 1399, "show_title": "Show", "season_number": 1, "episode_number": 2,
            "episode_name": "Episode", "air_date": "2026-11-10", **changes}


class CalendarIcsTests(unittest.TestCase):
    def render(self, entries):
        return render_calendar(entries, datetime(2026, 11, 1, 12))

    def test_all_day_event_and_exclusive_end(self):
        text = self.render([entry()])
        self.assertIn("DTSTART;VALUE=DATE:20261110\r\n", text)
        self.assertIn("DTEND;VALUE=DATE:20261111\r\n", text)
        self.assertIn("DTSTAMP:20261101T120000Z\r\n", text)
        self.assertTrue(text.endswith("END:VCALENDAR\r\n"))

    def test_uid_survives_rescheduling_and_display_order_changes(self):
        uid = lambda text: next(line for line in text.splitlines() if line.startswith("UID:"))
        self.assertEqual(uid(self.render([entry()])), uid(self.render([entry(
            air_date="2026-11-12", show_title="Renamed", display_season_number=2, display_episode_number=4)])))

    def test_text_is_escaped_and_cannot_inject_properties(self):
        text = self.render([entry(show_title="A,B;C\\D\r\nBEGIN:VEVENT")])
        self.assertIn("A\\,B\\;C\\\\D\\nBEGIN:VEVENT", text)
        self.assertEqual(text.count("\r\nBEGIN:VEVENT\r\n"), 1)

    def test_unicode_lines_are_folded_at_75_octets(self):
        text = self.render([entry(show_title="é漢" * 80)])
        for line in text.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75)
        self.assertIn("é漢" * 80, text.replace("\r\n ", ""))

    def test_invalid_dates_are_omitted_and_duplicates_do_not_repeat(self):
        text = self.render([entry(air_date=None), entry(air_date="invalid"), entry(), entry()])
        self.assertEqual(text.count("BEGIN:VEVENT\r\n"), 1)

    def test_provider_identity_avoids_local_id_collisions_between_instances(self):
        uid = lambda text: next(line for line in text.splitlines() if line.startswith("UID:"))
        self.assertNotEqual(uid(self.render([entry()])), uid(self.render([entry(show_tmdb_id=999)])))
        self.assertEqual(uid(self.render([entry()])), uid(self.render([entry(show_id=99)])))

    def test_tvdb_only_series_has_a_distinct_identity(self):
        text = self.render([entry(show_tmdb_id=None, show_tvdb_id=1399)])
        self.assertIn("UID:show-tvdb-1399-s1-e2@scrob", text)


class CalendarFeedAuthTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(calendar.router, prefix="/calendar")
        self.db = MagicMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = SimpleNamespace(id=42)
        self.db.execute = AsyncMock(return_value=result)
        async def database():
            yield self.db
        self.app.dependency_overrides[get_db] = database
        self.client = TestClient(self.app)

    def test_query_api_key_needs_no_login_header(self):
        with patch.object(calendar, "_load_or_compute", AsyncMock(return_value={"calendar": {"entries": [entry()]}})) as load:
            response = self.client.get("/calendar/feed.ics?api_key=test-key")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/calendar", response.headers["content-type"])
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(load.await_args.args[1], 42)

    def test_missing_and_invalid_credentials_are_rejected(self):
        self.assertEqual(self.client.get("/calendar/feed.ics").status_code, 401)
        self.db.execute.return_value.scalar_one_or_none.return_value = None
        self.assertEqual(self.client.get("/calendar/feed.ics?api_key=wrong").status_code, 401)

import os
import unittest

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from sqlalchemy.dialects import postgresql

from routers.calendar import _candidate_show_query


class CalendarCandidateQueryTests(unittest.TestCase):
    """A show hidden from Next Up is also hidden from Calendar (#258)."""

    def test_excludes_the_users_hidden_shows(self):
        rendered = str(
            _candidate_show_query(12, [3, 7]).compile(dialect=postgresql.dialect())
        )

        self.assertIn("shows.id NOT IN", rendered)

    def test_keeps_the_query_unrestricted_without_hidden_shows(self):
        rendered = str(
            _candidate_show_query(12, []).compile(dialect=postgresql.dialect())
        )

        self.assertNotIn("shows.id NOT IN", rendered)


if __name__ == "__main__":
    unittest.main()

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from models.base import MediaType
from routers import compat


class _Result:
    def __init__(self, items):
        self.items = items

    def scalars(self):
        return self

    def all(self):
        return self.items


class _FakeSession:
    def __init__(self, list_row, rows):
        self.list_row = list_row
        self.execute = AsyncMock(return_value=_Result(rows))

    async def get(self, model, list_id):
        return self.list_row


class MissingYearTests(unittest.IsolatedAsyncioTestCase):
    """Regression test for issue #99: Radarr/Sonarr deserialize `year` into a
    non-nullable int, so a null year on any one item aborts the whole import."""

    async def test_radarr_list_defaults_missing_year_to_zero(self) -> None:
        user = SimpleNamespace(id=1)
        lst = SimpleNamespace(user_id=1)
        media = SimpleNamespace(
            tmdb_id=42, title="No Release Date", original_title=None,
            overview=None, release_date=None, runtime=None, status=None,
        )
        db = _FakeSession(lst, [media])

        result = await compat.radarr_list(list_id=1, user=user, db=db)

        self.assertEqual(result[0]["year"], 0)

    async def test_sonarr_list_defaults_missing_year_to_zero(self) -> None:
        user = SimpleNamespace(id=1)
        lst = SimpleNamespace(user_id=1)
        media = SimpleNamespace(
            tmdb_id=42, title="No Release Date", original_title=None,
            overview=None, release_date=None, runtime=None, status=None,
        )
        db = _FakeSession(lst, [(media, None)])

        result = await compat.sonarr_list(list_id=1, user=user, db=db)

        self.assertEqual(result[0]["year"], 0)


if __name__ == "__main__":
    unittest.main()

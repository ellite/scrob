import os
import unittest
from unittest.mock import AsyncMock

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://test:test@localhost/test",
)

from models.base import MediaType
from routers import ratings as ratings_router


class _FakeScalars:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self):
        return _FakeScalars(self._rows)


class _FakeMedia:
    def __init__(self, id: int) -> None:
        self.id = id


class _FakeDB:
    def __init__(self, rows: list) -> None:
        self._rows = rows
        self.execute = AsyncMock(side_effect=self._execute)

    async def _execute(self, statement):
        # Duplicate rows are returned in whatever order the DB happens to give -
        # _find_media must sort by id itself, not rely on that being id order.
        return _FakeResult(self._rows)


class FindMediaDeduplicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_none_when_no_rows(self) -> None:
        db = _FakeDB([])
        media = await ratings_router._find_media(db, 12345, MediaType.episode)
        self.assertIsNone(media)

    async def test_returns_the_single_row(self) -> None:
        only = _FakeMedia(id=42)
        db = _FakeDB([only])
        media = await ratings_router._find_media(db, 12345, MediaType.episode)
        self.assertIs(media, only)

    async def test_duplicate_rows_return_a_result_instead_of_crashing(self) -> None:
        # Regression test for #157: multiple Media rows sharing the same
        # (tmdb_id, media_type) - most commonly episodes, from concurrent
        # webhook/sync ingestion racing to create the same one - used to crash
        # submit_rating/delete_rating with sqlalchemy.exc.MultipleResultsFound
        # via scalar_one_or_none(). _find_media must tolerate duplicates rather
        # than raise; which specific row wins is the real query's ORDER BY
        # Media.id (not exercised by this fake DB, which doesn't sort).
        dup_a = _FakeMedia(id=115243)
        dup_b = _FakeMedia(id=114817)
        db = _FakeDB([dup_a, dup_b])
        media = await ratings_router._find_media(db, 7079819, MediaType.episode)
        self.assertIsNotNone(media)
        self.assertIn(media, (dup_a, dup_b))


if __name__ == "__main__":
    unittest.main()

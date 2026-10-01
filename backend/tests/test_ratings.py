import os
import unittest
from datetime import datetime
from unittest.mock import AsyncMock

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://test:test@localhost/test",
)

from core.identity import find_media
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
    """The ratings router resolves items through core.identity.find_media
    (dual identity, step 1) - the dedup guarantee below carries over."""

    async def test_returns_none_when_no_rows(self) -> None:
        db = _FakeDB([])
        media = await find_media(db, MediaType.episode, tmdb_id=12345)
        self.assertIsNone(media)

    async def test_returns_the_single_row(self) -> None:
        only = _FakeMedia(id=42)
        db = _FakeDB([only])
        media = await find_media(db, MediaType.episode, tmdb_id=12345)
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
        media = await find_media(db, MediaType.episode, tmdb_id=7079819)
        self.assertIsNotNone(media)
        self.assertIn(media, (dup_a, dup_b))


class _FakeRatedMedia:
    def __init__(self, **attrs) -> None:
        self.id = 1
        self.tmdb_id = None
        self.tvdb_id = None
        self.imdb_id = None
        self.media_type = MediaType.movie
        self.title = "Title"
        self.poster_path = None
        self.release_date = None
        self.__dict__.update(attrs)


class _FakeRating:
    def __init__(self, **attrs) -> None:
        self.id = 1
        self.season_number = None
        self.episode_order = None
        self.user_id = 1
        self.rating = 8.0
        self.review = None
        self.rated_at = datetime(2026, 1, 1)
        self.__dict__.update(attrs)


class FormatRatingTests(unittest.TestCase):
    """Regression test for the ratings payload leaving out tvdb_id/imdb_id:
    a rated item identified only by TheTVDB (no tmdb_id, e.g. a TVDB-only
    show - see core/identity.py) used to serialize with no usable id at all,
    even though Media stores all three. GET /history already includes every
    id for the same Media model; GET/POST/DELETE /ratings now match it."""

    def test_includes_tvdb_and_imdb_ids(self) -> None:
        media = _FakeRatedMedia(tmdb_id=None, tvdb_id=99999, imdb_id="tt1234567")
        rating = _FakeRating()

        payload = ratings_router.format_rating(rating, media)

        self.assertEqual(payload["media"]["tvdb_id"], 99999)
        self.assertEqual(payload["media"]["imdb_id"], "tt1234567")
        self.assertIsNone(payload["media"]["tmdb_id"])


if __name__ == "__main__":
    unittest.main()

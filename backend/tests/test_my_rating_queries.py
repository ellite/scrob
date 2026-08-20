import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.dialects import postgresql

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from models.base import MediaType
from models.media import Media
from models.show import Show
from routers.media import list_media
from routers.shows import list_shows


class _Result:
    def __init__(self, *, count=None, items=None):
        self.count = count
        self.items = items or []

    def scalar_one(self):
        return self.count

    def scalars(self):
        return self

    def all(self):
        return self.items


def _sql(statement) -> str:
    return str(statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    ))


class MyRatingQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_movie_filter_and_sort_are_scoped_to_the_current_users_top_level_rating(self):
        movie = Media(
            id=7, tmdb_id=550, media_type=MediaType.movie, title="Fight Club",
            adult=False, tmdb_data={},
        )
        db = SimpleNamespace(execute=AsyncMock(side_effect=[
            _Result(count=1), _Result(items=[movie]),
        ]))
        user = SimpleNamespace(id=42)

        with (
            patch("routers.media.enrich_with_state", AsyncMock()),
            patch("routers.media.get_user_metadata_language", AsyncMock(return_value=None)),
        ):
            response = await list_media(
                type=MediaType.movie,
                sort="user_rating",
                page=1,
                page_size=30,
                genre=[],
                year=[],
                watched=[],
                my_rating=[8],
                db=db,
                current_user=user,
            )

        self.assertEqual(response["total_results"], 1)
        query = _sql(db.execute.await_args_list[1].args[0])
        self.assertIn("ratings.user_id = 42", query)
        self.assertIn("ratings.season_number IS NULL", query)
        self.assertIn("ratings.rating IN (8)", query)
        self.assertIn("ORDER BY ratings.rating DESC NULLS LAST, media.id DESC", query)

    async def test_show_filter_and_sort_join_rating_media_by_tmdb_id(self):
        show = Show(id=3, tmdb_id=1396, title="Breaking Bad", tmdb_data={})
        db = SimpleNamespace(execute=AsyncMock(side_effect=[
            _Result(count=1), _Result(items=[show]),
        ]))
        user = SimpleNamespace(id=42)

        with (
            patch("routers.shows.enrich_with_state", AsyncMock()),
            patch("routers.shows.get_user_metadata_language", AsyncMock(return_value=None)),
        ):
            response = await list_shows(
                sort="user_rating",
                page=1,
                page_size=30,
                genre=[],
                year=[],
                status=[],
                watched=[],
                my_rating=[9],
                db=db,
                current_user=user,
            )

        self.assertEqual(response["total_results"], 1)
        query = _sql(db.execute.await_args_list[1].args[0])
        self.assertIn("max(ratings.rating) AS user_rating", query)
        self.assertIn("ratings.user_id = 42", query)
        self.assertIn("ratings.season_number IS NULL", query)
        self.assertIn("anon_1.user_rating IN (9)", query)
        self.assertIn("ORDER BY anon_1.user_rating DESC NULLS LAST, shows.id DESC", query)


if __name__ == "__main__":
    unittest.main()

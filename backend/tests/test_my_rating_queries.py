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
    async def test_movie_filter_and_sort_are_user_scoped_and_ignore_season_ratings(self):
        movie = Media(
            id=7, tmdb_id=550, media_type=MediaType.movie, title="Fight Club",
            adult=False, tmdb_data={},
        )
        db = SimpleNamespace(execute=AsyncMock(side_effect=[
            _Result(count=1), _Result(items=[movie]),
        ]))

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
                in_list=None,
                db=db,
                current_user=SimpleNamespace(id=42),
            )

        self.assertEqual(response["total_results"], 1)
        count_query = _sql(db.execute.await_args_list[0].args[0])
        page_query = _sql(db.execute.await_args_list[1].args[0])
        for query in (count_query, page_query):
            self.assertIn("ratings.user_id = 42", query)
            self.assertIn("ratings.season_number IS NULL", query)
        self.assertIn("floor(ratings.rating + 0.5) IN (8)", count_query)
        self.assertIn(
            "ORDER BY ratings.rating DESC NULLS LAST, media.tmdb_rating DESC NULLS LAST, media.id DESC",
            page_query,
        )

    async def test_show_filter_and_sort_match_ratings_by_tmdb_or_tvdb_id(self):
        show = Show(id=3, tmdb_id=1396, tvdb_id=81189, title="Breaking Bad", tmdb_data={})
        db = SimpleNamespace(execute=AsyncMock(side_effect=[
            _Result(count=1), _Result(items=[show]),
        ]))

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
                in_list=None,
                db=db,
                current_user=SimpleNamespace(id=42),
            )

        self.assertEqual(response["total_results"], 1)
        count_query = _sql(db.execute.await_args_list[0].args[0])
        page_query = _sql(db.execute.await_args_list[1].args[0])
        for query in (count_query, page_query):
            self.assertIn("ratings.user_id = 42", query)
            self.assertIn("ratings.season_number IS NULL", query)
            self.assertIn("media_1.tmdb_id = shows.tmdb_id", query)
            self.assertIn("media_1.tvdb_id = shows.tvdb_id", query)
        self.assertIn("floor((SELECT max(ratings.rating)", count_query)
        self.assertIn("+ 0.5) IN (9)", count_query)
        self.assertIn("DESC NULLS LAST", page_query)
        self.assertIn("shows.id DESC", page_query)


if __name__ == "__main__":
    unittest.main()

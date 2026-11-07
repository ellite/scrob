"""The my_rating query parameter, sent through real HTTP requests.

test_my_rating_queries.py calls the endpoint functions directly, which skips
FastAPI's parameter validation. A bound declared on the list instead of its items
only fails here, as a 500 on every collection page.
"""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

import httpx
from fastapi import FastAPI, HTTPException

from db import get_db
from dependencies import get_current_user_or_api_key, get_optional_user_or_api_key
from routers import media, shows


class _Result:
    def scalar_one(self):
        return 0

    def scalars(self):
        return self

    def all(self):
        return []


class MyRatingParamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app = FastAPI()
        app.include_router(media.router, prefix="/media")
        app.include_router(shows.router, prefix="/shows")

        async def _override_db():
            yield SimpleNamespace(execute=AsyncMock(return_value=_Result()))

        user = SimpleNamespace(id=42)
        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[get_current_user_or_api_key] = lambda: user
        app.dependency_overrides[get_optional_user_or_api_key] = lambda: user
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

        for target in (
            "routers.media.enrich_with_state",
            "routers.shows.enrich_with_state",
            "routers.media.get_user_metadata_language",
            "routers.shows.get_user_metadata_language",
        ):
            patcher = patch(target, AsyncMock(return_value=None))
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_collection_lists_accept_no_one_or_several_ratings(self):
        for path in ("/media?type=movie", "/shows"):
            for query in ("", "&my_rating=1", "&my_rating=8&my_rating=10"):
                url = path + (query if "?" in path else query.replace("&", "?", 1))
                with self.subTest(url=url):
                    res = await self.client.get(url)
                    self.assertEqual(res.status_code, 200, res.text)

    async def test_collection_lists_reject_ratings_outside_1_to_10(self):
        for url in ("/media?my_rating=0", "/media?my_rating=11", "/shows?my_rating=0", "/shows?my_rating=11"):
            with self.subTest(url=url):
                res = await self.client.get(url)
                self.assertEqual(res.status_code, 422, res.text)
                self.assertIn("my_rating", res.text)

    async def test_explore_list_validates_each_rating(self):
        # Stop right after parameter validation; the TMDB fetch is not under test.
        stop = AsyncMock(side_effect=HTTPException(status_code=418))
        with patch("routers.media.get_user_tmdb_key", stop):
            accepted = await self.client.get("/media/tmdb/list?type=movie&my_rating=5&my_rating=9")
            rejected = await self.client.get("/media/tmdb/list?type=movie&my_rating=0")
        self.assertEqual(accepted.status_code, 418, accepted.text)
        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertIn("my_rating", rejected.text)


if __name__ == "__main__":
    unittest.main()

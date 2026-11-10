import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("SECRET_KEY", "synthetic-test-key-32-characters-long")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from models.base import MediaType
from routers import media


class _Result:
    def scalars(self):
        return self

    def all(self):
        return []


class ExplorePageSizeTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, page=1, size=50, total=123, kind=MediaType.movie, category="popular",
                    shift=0, **filters):
        async def remote(*, page, **kwargs):
            self.assertLessEqual(page, 500)
            # shift > 0 simulates TMDB reordering between parallel fetches:
            # later source pages repeat the last ids of the page before.
            first = (page - 1) * 20 + 1 - (shift if page > 1 else 0)
            rows = [{"id": n, "title": str(n), "name": str(n),
                     "release_date": "2020-01-01" if n % 2 == 0 else "2019-01-01"}
                    for n in range(first, min(first + 19, total) + 1)]
            return {"page": page, "total_pages": (total + 19) // 20,
                    "total_results": total, "results": rows}

        fetch = AsyncMock(side_effect=remote)
        args = dict(type=kind, category=category, page=page, page_size=size,
                    genre=[], exclude_genre=[], year=[], min_rating=None, status=None,
                    original_language=None, exclude_language=[], network=None, company=None,
                    collection=[], watch=[], arr=[], in_list=[], my_rating=[],
                    db=SimpleNamespace(execute=AsyncMock(return_value=_Result())),
                    current_user=SimpleNamespace(id=7))
        args.update(filters)
        if size is None:
            args.pop("page_size")
        with (
            patch.object(media, "get_user_tmdb_key", AsyncMock(return_value="synthetic")),
            patch.object(media, "check_tmdb_key", lambda value: True),
            patch.object(media, "get_user_metadata_language", AsyncMock(return_value=None)),
            patch.object(media, "enrich_with_state", AsyncMock()),
            patch.object(media, "_get_global_settings", AsyncMock(return_value=SimpleNamespace(
                enable_logged_out_navigation=True, tmdb_api_key="synthetic",
            ))),
            patch.object(media.tmdb, "get_popular_movies", fetch),
            patch.object(media.tmdb, "get_popular_shows", fetch),
            patch.object(media.tmdb, "get_trending_movies", fetch),
            patch.object(media.tmdb, "get_trending_shows", fetch),
            patch.object(media.tmdb, "get_top_rated_movies", fetch),
            patch.object(media.tmdb, "get_top_rated_shows", fetch),
            patch.object(media.tmdb, "discover_movies", fetch),
            patch.object(media.tmdb, "discover_shows", fetch),
        ):
            result = await media.get_tmdb_list(**args)
        return result, [call.kwargs["page"] for call in fetch.await_args_list]

    async def test_fifty_results_combine_three_tmdb_pages(self):
        result, pages = await self._call()
        self.assertEqual([r["tmdb_id"] for r in result["results"]], list(range(1, 51)))
        self.assertEqual(pages, [1, 2, 3])
        self.assertEqual(result["total_pages"], 3)

    async def test_shifted_source_pages_do_not_repeat_a_title(self):
        result, _ = await self._call(shift=2)
        ids = [r["tmdb_id"] for r in result["results"]]
        self.assertEqual(len(ids), len(set(ids)))

    async def test_adjacent_pages_have_no_gaps_or_overlap(self):
        first, _ = await self._call(page=1)
        second, pages = await self._call(page=2)
        self.assertEqual([r["tmdb_id"] for r in first["results"] + second["results"]], list(range(1, 101)))
        self.assertEqual(pages, [3, 4, 5])

    async def test_thirty_results_start_inside_a_source_page(self):
        result, pages = await self._call(page=2, size=30)
        self.assertEqual([r["tmdb_id"] for r in result["results"]], list(range(31, 61)))
        self.assertEqual(pages, [2, 3])

    async def test_partial_final_page_reports_exact_total(self):
        result, pages = await self._call(page=3, total=115)
        self.assertEqual([r["tmdb_id"] for r in result["results"]], list(range(101, 116)))
        self.assertEqual((result["page"], result["total_pages"], result["total_results"]), (3, 3, 115))
        self.assertEqual(pages, [6])

    async def test_tmdb_limit_is_applied_before_display_page_count(self):
        result, pages = await self._call(page=200, total=15000)
        self.assertEqual([r["tmdb_id"] for r in result["results"]], list(range(9951, 10001)))
        self.assertEqual((result["total_pages"], result["total_results"]), (200, 10000))
        self.assertEqual(pages, [498, 499, 500])

    async def test_past_last_accessible_page_does_not_request_tmdb_page_501(self):
        result, pages = await self._call(page=201, total=15000)
        self.assertEqual(result["results"], [])
        self.assertEqual(result["total_pages"], 200)
        self.assertTrue(all(p <= 500 for p in pages))

    async def test_all_movie_and_show_categories_keep_their_results(self):
        for kind in [MediaType.movie, MediaType.series]:
            for category in ["popular", "top_rated", "trending"]:
                with self.subTest(kind=kind, category=category):
                    result, _ = await self._call(kind=kind, category=category, size=40)
                    self.assertEqual([r["tmdb_id"] for r in result["results"]], list(range(1, 41)))

    async def test_discover_filters_also_support_larger_pages(self):
        result, _ = await self._call(genre=["Drama"])
        self.assertEqual(len(result["results"]), 50)

    async def test_local_filters_rebuild_requested_sized_pages(self):
        first, _ = await self._call(year=[2020], total=300)
        second, _ = await self._call(page=2, year=[2020], total=300)
        self.assertEqual([r["tmdb_id"] for r in first["results"]], list(range(2, 101, 2)))
        self.assertEqual([r["tmdb_id"] for r in second["results"]], list(range(102, 201, 2)))

    async def test_missing_parameter_keeps_twenty_results(self):
        result, pages = await self._call(size=None)
        self.assertEqual([r["tmdb_id"] for r in result["results"]], list(range(1, 21)))
        self.assertEqual(pages, [1])

    async def test_anonymous_browsing_supports_the_same_page_size(self):
        result, _ = await self._call(current_user=None)
        self.assertEqual(len(result["results"]), 50)
        self.assertTrue(all(not row["in_library"] for row in result["results"]))

    async def test_local_filter_scan_remains_bounded_to_thirty_source_pages(self):
        result, pages = await self._call(page=7, year=[2020], total=10000)
        self.assertEqual(pages, list(range(1, 31)))
        self.assertEqual(result["results"], [])


class ExplorePageSizeValidationTests(unittest.TestCase):
    def test_api_rejects_more_than_fifty_and_nonpositive_sizes(self):
        app = FastAPI()
        app.add_api_route("/explore", media.get_tmdb_list)
        app.dependency_overrides[media.get_db] = lambda: None
        app.dependency_overrides[media.get_optional_user_or_api_key] = lambda: SimpleNamespace(id=7)
        with TestClient(app) as client:
            for size in (0, -1, 51, 100):
                with self.subTest(size=size):
                    self.assertEqual(client.get(f"/explore?type=movie&page_size={size}").status_code, 422)

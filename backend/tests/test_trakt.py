import unittest
from unittest.mock import patch

import httpx

from core import trakt


_REAL_ASYNC_CLIENT = httpx.AsyncClient


class TraktClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_history_movies_fetches_every_page(self) -> None:
        requested_pages: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/sync/history/movies")
            self.assertEqual(request.url.params["limit"], "250")
            self.assertEqual(request.headers["authorization"], "Bearer access-token")

            page = int(request.url.params["page"])
            requested_pages.append(page)
            page_items = {
                1: [{"id": index, "watched_at": "2026-07-15T20:00:00.000Z", "movie": {"ids": {"tmdb": index}}} for index in range(1, 251)],
                2: [{"id": index, "watched_at": "2026-07-15T20:00:00.000Z", "movie": {"ids": {"tmdb": index}}} for index in range(251, 501)],
                3: [{"id": index, "watched_at": "2026-07-15T20:00:00.000Z", "movie": {"ids": {"tmdb": index}}} for index in range(501, 518)],
            }[page]
            return httpx.Response(
                200,
                json=page_items,
                headers={"X-Pagination-Page-Count": "3"},
            )

        transport = httpx.MockTransport(handler)
        with patch.object(
            trakt.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            plays = await trakt.get_history_movies("client-id", "access-token")

        self.assertEqual(requested_pages, [1, 2, 3])
        self.assertEqual(len(plays), 517)
        self.assertEqual(plays[-1]["movie"]["ids"]["tmdb"], 517)


    async def test_get_history_movies_returns_multiple_plays_of_same_title(self) -> None:
        """Regression test for #61/#77: a title watched more than once must
        appear as multiple distinct history entries, not collapse to one."""

        def handler(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params["page"])
            if page > 1:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[
                    {"id": 1, "watched_at": "2026-01-01T20:00:00.000Z", "movie": {"ids": {"tmdb": 42}}},
                    {"id": 2, "watched_at": "2026-06-01T20:00:00.000Z", "movie": {"ids": {"tmdb": 42}}},
                ],
                headers={"X-Pagination-Page-Count": "1"},
            )

        transport = httpx.MockTransport(handler)
        with patch.object(
            trakt.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            plays = await trakt.get_history_movies("client-id", "access-token")

        self.assertEqual(len(plays), 2)
        self.assertEqual({p["watched_at"] for p in plays}, {"2026-01-01T20:00:00.000Z", "2026-06-01T20:00:00.000Z"})


    async def test_get_history_episodes_fetches_every_page(self) -> None:
        requested_pages: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/sync/history/episodes")
            self.assertEqual(request.url.params["limit"], "250")
            page = int(request.url.params["page"])
            requested_pages.append(page)
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 9001,
                        "watched_at": "2026-07-15T20:00:00.000Z",
                        "episode": {"season": 1, "number": 1},
                        "show": {"title": "Some Show", "ids": {"tmdb": 1396}},
                    }
                ],
                headers={"X-Pagination-Page-Count": "1"},
            )

        transport = httpx.MockTransport(handler)
        with patch.object(
            trakt.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            plays = await trakt.get_history_episodes("client-id", "access-token")

        self.assertEqual(requested_pages, [1])
        self.assertEqual(plays[0]["episode"]["number"], 1)


    async def test_get_history_movies_stops_without_page_count_header(self) -> None:
        """Regression test: a non-paginating response that omits
        X-Pagination-Page-Count must not be re-requested forever."""
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return httpx.Response(
                200,
                json=[{"id": 1, "watched_at": "2026-07-15T20:00:00.000Z", "movie": {"ids": {"tmdb": 1}}}],
            )

        transport = httpx.MockTransport(handler)
        with patch.object(
            trakt.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            plays = await trakt.get_history_movies("client-id", "access-token")

        self.assertEqual(request_count, 1)
        self.assertEqual(len(plays), 1)


if __name__ == "__main__":
    unittest.main()

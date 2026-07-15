import unittest
from unittest.mock import patch

import httpx

from core import trakt


_REAL_ASYNC_CLIENT = httpx.AsyncClient


class TraktClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_watched_movies_fetches_every_page(self) -> None:
        requested_pages: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/sync/watched/movies")
            self.assertEqual(request.url.params["limit"], "250")
            self.assertEqual(request.headers["authorization"], "Bearer access-token")

            page = int(request.url.params["page"])
            requested_pages.append(page)
            page_items = {
                1: [{"movie": {"ids": {"tmdb": index}}} for index in range(1, 251)],
                2: [{"movie": {"ids": {"tmdb": index}}} for index in range(251, 501)],
                3: [{"movie": {"ids": {"tmdb": index}}} for index in range(501, 518)],
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
            movies = await trakt.get_watched_movies("client-id", "access-token")

        self.assertEqual(requested_pages, [1, 2, 3])
        self.assertEqual(len(movies), 517)
        self.assertEqual(movies[-1]["movie"]["ids"]["tmdb"], 517)


    async def test_get_watched_shows_requests_progress_on_every_page(self) -> None:
        requested_pages: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/sync/watched/shows")
            self.assertEqual(request.url.params["limit"], "250")
            self.assertEqual(request.url.params["extended"], "progress")
            page = int(request.url.params["page"])
            requested_pages.append(page)
            if page == 1:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "show": {"ids": {"tmdb": 1396}},
                            "seasons": [
                                {
                                    "number": 1,
                                    "episodes": [
                                        {
                                            "number": 1,
                                            "plays": 1,
                                            "last_watched_at": "2026-07-15T20:00:00.000Z",
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                )
            return httpx.Response(200, json=[])

        transport = httpx.MockTransport(handler)
        with patch.object(
            trakt.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            shows = await trakt.get_watched_shows("client-id", "access-token")

        self.assertEqual(requested_pages, [1, 2])
        self.assertEqual(shows[0]["seasons"][0]["episodes"][0]["number"], 1)


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest.mock import patch

import httpx

from core import plex


_REAL_ASYNC_CLIENT = httpx.AsyncClient


class PlexSeasonRatingTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_season_rating_key_uses_parent_show_tmdb_id(self) -> None:
        requested_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_paths.append(request.url.path)
            if request.url.path == "/library/sections/all":
                self.assertEqual(request.url.params["type"], "2")
                self.assertEqual(request.url.params["guid"], "tmdb://1396")
                return httpx.Response(
                    200,
                    json={
                        "MediaContainer": {
                            "Metadata": [
                                {
                                    "ratingKey": "100",
                                    "Guid": [{"id": "tmdb://1396"}],
                                }
                            ]
                        }
                    },
                )
            if request.url.path == "/library/metadata/100/children":
                return httpx.Response(
                    200,
                    json={
                        "MediaContainer": {
                            "Metadata": [
                                {"ratingKey": "101", "index": 0, "type": "season"},
                                {"ratingKey": "102", "index": 1, "type": "season"},
                                {"ratingKey": "103", "index": 2, "type": "season"},
                            ]
                        }
                    },
                )
            self.fail(f"Unexpected Plex path: {request.url.path}")

        transport = httpx.MockTransport(handler)
        with patch.object(
            plex.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            rating_key = await plex.resolve_season_rating_key(
                "http://plex.local",
                "token",
                1396,
                2,
            )

        self.assertEqual(rating_key, "103")
        self.assertEqual(
            requested_paths,
            ["/library/sections/all", "/library/metadata/100/children"],
        )

    async def test_zero_rating_clears_plex_season_rating(self) -> None:
        request_data: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            request_data["path"] = request.url.path
            request_data["rating"] = request.url.params["rating"]
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handler)
        async with _REAL_ASYNC_CLIENT(transport=transport) as client:
            result = await plex.set_rating(
                "http://plex.local",
                "token",
                "103",
                0,
                client=client,
            )

        self.assertTrue(result)
        self.assertEqual(request_data, {"path": "/library/metadata/103/userRating", "rating": "0"})


if __name__ == "__main__":
    unittest.main()

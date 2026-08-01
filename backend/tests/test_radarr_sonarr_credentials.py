import os
import unittest
from unittest.mock import patch

import httpx

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core import radarr, sonarr


_REAL_ASYNC_CLIENT = httpx.AsyncClient


class RadarrSonarrCredentialTests(unittest.IsolatedAsyncioTestCase):
    async def test_radarr_sends_api_key_as_header_not_query_param(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers.get("x-api-key"), "radarr-secret")
            self.assertNotIn("apiKey", request.url.params)
            self.assertNotIn("radarr-secret", str(request.url))
            return httpx.Response(200, json={"version": "5.0.0"})

        transport = httpx.MockTransport(handler)
        with patch.object(
            radarr.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            success = await radarr.validate_connection("https://radarr.example", "radarr-secret")

        self.assertTrue(success)

    async def test_radarr_movie_lookup_sends_api_key_as_header(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers.get("x-api-key"), "radarr-secret")
            self.assertNotIn("apiKey", request.url.params)
            self.assertEqual(request.url.params["term"], "tmdb:603")
            return httpx.Response(200, json=[{"id": 1, "tmdbId": 603, "title": "The Matrix"}])

        transport = httpx.MockTransport(handler)
        with patch.object(
            radarr.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            result = await radarr.add_movie(
                "https://radarr.example",
                "radarr-secret",
                tmdb_id=603,
                title="The Matrix",
                root_folder="/movies",
                quality_profile_id=1,
            )

        self.assertEqual(result["status"], "already_exists")

    async def test_sonarr_sends_api_key_as_header_not_query_param(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers.get("x-api-key"), "sonarr-secret")
            self.assertNotIn("apiKey", request.url.params)
            self.assertNotIn("sonarr-secret", str(request.url))
            return httpx.Response(200, json={"version": "4.0.0"})

        transport = httpx.MockTransport(handler)
        with patch.object(
            sonarr.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            success = await sonarr.validate_connection("https://sonarr.example", "sonarr-secret")

        self.assertTrue(success)

    async def test_sonarr_series_lookup_sends_api_key_as_header(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers.get("x-api-key"), "sonarr-secret")
            self.assertNotIn("apiKey", request.url.params)
            self.assertEqual(request.url.params["term"], "tvdb:12345")
            return httpx.Response(200, json=[{"id": 1, "tvdbId": 12345}])

        transport = httpx.MockTransport(handler)
        with patch.object(
            sonarr.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            result = await sonarr.add_series(
                "https://sonarr.example",
                "sonarr-secret",
                tvdb_id=12345,
                root_folder="/tv",
                quality_profile_id=1,
            )

        self.assertEqual(result["status"], "already_exists")


if __name__ == "__main__":
    unittest.main()

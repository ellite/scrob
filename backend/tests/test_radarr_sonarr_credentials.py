import os
import unittest
import json
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

    async def test_sonarr_selected_seasons_only_monitor_and_search_the_selection(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            self.assertEqual(request.headers.get("x-api-key"), "sonarr-secret")
            self.assertNotIn("apiKey", request.url.params)
            if request.method == "GET":
                return httpx.Response(200, json=[{
                    "tvdbId": 12345,
                    "title": "Example Show",
                    "seasons": [
                        {"seasonNumber": 0, "monitored": True},
                        {"seasonNumber": 1, "monitored": True},
                        {"seasonNumber": 2, "monitored": True},
                    ],
                }])
            payload = json.loads(request.content)
            if request.url.path == "/api/v3/series":
                self.assertTrue(payload["monitored"])
                self.assertEqual(
                    {season["seasonNumber"] for season in payload["seasons"] if season["monitored"]},
                    {0, 2},
                )
                self.assertEqual(payload["addOptions"], {
                    "monitor": "skip",
                    "searchForMissingEpisodes": False,
                    "searchForCutoffUnmetEpisodes": False,
                })
                return httpx.Response(201, json={"id": 42, **payload})
            self.assertEqual(request.url.path, "/api/v3/command")
            self.assertEqual(payload["name"], "SeasonSearch")
            self.assertEqual(payload["seriesId"], 42)
            self.assertIn(payload["seasonNumber"], {0, 2})
            self.assertNotEqual(payload["seasonNumber"], 1)
            return httpx.Response(201, json={"id": 100 + payload["seasonNumber"]})

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
                selected_seasons=[0, 2],
            )

        self.assertEqual(result["status"], "added")
        self.assertEqual(
            [(request.method, request.url.path) for request in requests],
            [
                ("GET", "/api/v3/series/lookup"),
                ("POST", "/api/v3/series"),
                ("POST", "/api/v3/command"),
                ("POST", "/api/v3/command"),
            ],
        )
        self.assertEqual(
            [json.loads(request.content)["seasonNumber"] for request in requests[2:]],
            [0, 2],
        )

    async def test_sonarr_all_mode_explicitly_searches_regular_seasons_not_specials(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, json=[{
                    "tvdbId": 12345,
                    "seasons": [
                        {"seasonNumber": 0, "monitored": True},
                        {"seasonNumber": 1, "monitored": False},
                        {"seasonNumber": 2, "monitored": False},
                    ],
                }])
            payload = json.loads(request.content)
            self.assertEqual(
                payload["seasons"],
                [
                    {"seasonNumber": 0, "monitored": False},
                    {"seasonNumber": 1, "monitored": True},
                    {"seasonNumber": 2, "monitored": True},
                ],
            )
            self.assertEqual(payload["addOptions"], {
                "monitor": "all",
                "searchForMissingEpisodes": True,
                "searchForCutoffUnmetEpisodes": False,
            })
            return httpx.Response(201, json={"id": 42, **payload})

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

        self.assertEqual(result["status"], "added")
        self.assertEqual(
            [(request.method, request.url.path) for request in requests],
            [("GET", "/api/v3/series/lookup"), ("POST", "/api/v3/series")],
        )

    async def test_sonarr_reports_a_search_queue_failure_without_disguising_the_add(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json=[{
                    "tvdbId": 12345,
                    "seasons": [{"seasonNumber": 1, "monitored": True}],
                }])
            if request.url.path == "/api/v3/series":
                return httpx.Response(201, json={"id": 42})
            self.assertEqual(json.loads(request.content), {
                "name": "SeasonSearch", "seriesId": 42, "seasonNumber": 1,
            })
            return httpx.Response(500, json={"message": "queue unavailable"})

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
                selected_seasons=[1],
            )

        self.assertEqual(result["status"], "added_search_failed")
        self.assertEqual(result["series"], {"id": 42})
        self.assertEqual(result["search_failed_seasons"], [1])

    async def test_sonarr_rejects_stale_selected_seasons_before_posting(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=[{
                "tvdbId": 12345,
                "seasons": [{"seasonNumber": 1, "monitored": True}],
            }])

        transport = httpx.MockTransport(handler)
        with patch.object(
            sonarr.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            with self.assertRaisesRegex(sonarr.InvalidSeasonSelectionError, "no longer available: 2"):
                await sonarr.add_series(
                    "https://sonarr.example",
                    "sonarr-secret",
                    tvdb_id=12345,
                    root_folder="/tv",
                    quality_profile_id=1,
                    selected_seasons=[2],
                )

        self.assertEqual([request.method for request in requests], ["GET"])

    async def test_sonarr_rejects_invalid_season_values_before_lookup(self) -> None:
        with self.assertRaisesRegex(sonarr.InvalidSeasonSelectionError, "non-negative"):
            await sonarr.add_series(
                "https://sonarr.example",
                "sonarr-secret",
                tvdb_id=12345,
                root_folder="/tv",
                quality_profile_id=1,
                selected_seasons=[True],
            )


if __name__ == "__main__":
    unittest.main()

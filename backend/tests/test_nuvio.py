import json
import os
import unittest
from unittest.mock import patch

import httpx

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core import nuvio
from models.base import MediaType
from routers.sync import _normalize_nuvio_item


_REAL_ASYNC_CLIENT = httpx.AsyncClient


class NuvioClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_pull_sync_data_refreshes_session_and_paginates_library(self) -> None:
        library_offsets: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/auth/v1/token":
                self.assertEqual(request.url.params["grant_type"], "refresh_token")
                self.assertEqual(json.loads(request.content), {"refresh_token": "old-refresh"})
                return httpx.Response(
                    200,
                    json={
                        "access_token": "access-token",
                        "refresh_token": "new-refresh",
                        "expires_in": 3600,
                    },
                )
            self.assertEqual(request.headers["authorization"], "Bearer access-token")
            payload = json.loads(request.content or b"{}")
            if request.url.path.endswith("/sync_pull_profiles"):
                return httpx.Response(200, json=[{"profile_index": 2, "name": "Main"}])
            if request.url.path.endswith("/sync_pull_library"):
                library_offsets.append(payload["p_offset"])
                item_count = 500 if payload["p_offset"] == 0 else 1
                return httpx.Response(
                    200,
                    json=[
                        {"content_id": f"tmdb:{index + payload['p_offset']}", "content_type": "movie"}
                        for index in range(item_count)
                    ],
                )
            if request.url.path.endswith("/sync_pull_watched_items"):
                return httpx.Response(
                    200,
                    json=[{"content_id": "tmdb:550", "content_type": "movie", "watched_at": 1711600000000}],
                )
            if request.url.path.endswith("/sync_pull_watch_progress"):
                return httpx.Response(
                    200,
                    json=[{"content_id": "tmdb:550", "content_type": "movie", "position": 1000, "duration": 2000}],
                )
            return httpx.Response(404, json={"message": "unexpected request"})

        transport = httpx.MockTransport(handler)
        with patch.object(
            nuvio.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            session, data = await nuvio.pull_sync_data(
                "https://api.nuvio.tv/",
                "old-refresh",
                2,
            )

        self.assertEqual(session.refresh_token, "new-refresh")
        self.assertEqual(library_offsets, [0, 500])
        self.assertEqual(len(data["library"]), 501)
        self.assertEqual(len(data["watched"]), 1)
        self.assertEqual(len(data["progress"]), 1)

    async def test_push_watched_items_batches_without_full_replace(self) -> None:
        batch_sizes: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/auth/v1/token":
                return httpx.Response(
                    200,
                    json={
                        "access_token": "access-token",
                        "refresh_token": "rotated-refresh",
                        "expires_in": 3600,
                    },
                )
            if request.url.path.endswith("/sync_push_watched_items"):
                payload = json.loads(request.content)
                self.assertEqual(payload["p_profile_id"], 1)
                batch_sizes.append(len(payload["p_items"]))
                return httpx.Response(204)
            return httpx.Response(404, json={"message": "unexpected request"})

        transport = httpx.MockTransport(handler)
        items = [
            {
                "content_id": f"tmdb:{index}",
                "content_type": "movie",
                "watched_at": 1711600000000,
            }
            for index in range(501)
        ]
        with patch.object(
            nuvio.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            session = await nuvio.push_watched_items(
                "https://api.nuvio.tv",
                "old-refresh",
                1,
                items,
            )

        self.assertEqual(session.refresh_token, "rotated-refresh")
        self.assertEqual(batch_sizes, [500, 1])


class NuvioNormalizationTests(unittest.TestCase):
    def test_episode_history_maps_to_tmdb_series_and_watch_state(self) -> None:
        normalized = _normalize_nuvio_item(
            {
                "content_id": "tmdb:1396",
                "content_type": "series",
                "title": "Pilot",
                "season": 1,
                "episode": 1,
                "watched_at": 1711600000000,
            },
            profile_id=3,
            watched=True,
        )

        self.assertIsNotNone(normalized)
        media_type, item = normalized
        self.assertEqual(media_type, MediaType.episode)
        self.assertEqual(item["Id"], "3:tmdb:1396:s1e1")
        self.assertEqual(item["SeriesId"], "tmdb:1396")
        self.assertEqual(item["ProviderIds"], {})
        self.assertEqual(item["UserData"]["Played"], True)
        self.assertEqual(item["UserData"]["PlayCount"], 1)
        self.assertIsNotNone(item["UserData"]["LastPlayedDate"])

    def test_imdb_content_uses_resolved_tmdb_id(self) -> None:
        normalized = _normalize_nuvio_item(
            {
                "content_id": "tt0411008",
                "content_type": "series",
                "name": "Lost",
            },
            profile_id=1,
            tmdb_id=4607,
        )

        self.assertIsNotNone(normalized)
        media_type, item = normalized
        self.assertEqual(media_type, MediaType.series)
        self.assertEqual(item["Id"], "1:tt0411008")
        self.assertEqual(item["ProviderIds"], {"Tmdb": "4607"})

    def test_unsupported_content_identifier_is_skipped(self) -> None:
        self.assertIsNone(
            _normalize_nuvio_item(
                {"content_id": "imdb:tt0137523", "content_type": "movie"},
                profile_id=1,
            )
        )


if __name__ == "__main__":
    unittest.main()

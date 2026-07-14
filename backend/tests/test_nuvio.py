import json
from datetime import datetime, timezone
import os
import unittest
from unittest.mock import patch

import httpx

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core import nuvio
from models.base import MediaType
from models.media import Media
from models.playback_progress import PlaybackProgress
from models.show import Show
from routers.sync import (
    _ensure_nuvio_imdb_ids,
    _normalize_nuvio_item,
    _nuvio_progress_item,
    _nuvio_watched_item,
)


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

    async def test_push_sync_items_uses_watched_and_progress_endpoints(self) -> None:
        calls: list[tuple[str, int]] = []
        refresh_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal refresh_count
            if request.url.path == "/auth/v1/token":
                refresh_count += 1
                return httpx.Response(
                    200,
                    json={
                        "access_token": "access-token",
                        "refresh_token": "rotated-refresh",
                        "expires_in": 3600,
                    },
                )
            payload = json.loads(request.content)
            self.assertEqual(payload["p_profile_id"], 3)
            function_name = request.url.path.rsplit("/", 1)[-1]
            items_key = "p_entries" if function_name == "sync_push_watch_progress" else "p_items"
            self.assertEqual(set(payload), {"p_profile_id", items_key})
            calls.append((function_name, len(payload[items_key])))
            return httpx.Response(204)

        watched_items = [{"content_id": "tmdb:550", "content_type": "movie", "watched_at": 1}]
        progress_items = [
            {
                "content_id": f"tmdb:{index}",
                "content_type": "movie",
                "video_id": f"tmdb:{index}",
                "position": 1000,
                "duration": 2000,
                "last_watched": 1,
            }
            for index in range(501)
        ]
        transport = httpx.MockTransport(handler)
        with patch.object(
            nuvio.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            session = await nuvio.push_sync_items(
                "https://api.nuvio.tv",
                "old-refresh",
                3,
                watched_items,
                progress_items,
            )

        self.assertEqual(session.refresh_token, "rotated-refresh")
        self.assertEqual(refresh_count, 1)
        self.assertEqual(
            calls,
            [
                ("sync_push_watched_items", 1),
                ("sync_push_watch_progress", 500),
                ("sync_push_watch_progress", 1),
            ],
        )

    async def test_missing_imdb_ids_are_resolved_and_cached(self) -> None:
        movie = Media(
            id=10,
            tmdb_id=550,
            media_type=MediaType.movie,
            title="Fight Club",
            tmdb_data={},
        )
        episode = Media(
            id=11,
            media_type=MediaType.episode,
            title="It's All Good",
            show_id=5,
            season_number=3,
            episode_number=2,
        )
        show = Show(id=5, tmdb_id=125988, title="Silo", tmdb_data={})

        async def external_ids(tmdb_id: int, media_type: str, api_key: str | None = None) -> dict:
            self.assertEqual(api_key, "tmdb-token")
            return {
                "imdb_id": {
                    ("movie", 550): "tt0137523",
                    ("tv", 125988): "tt14688458",
                }[(media_type, tmdb_id)]
            }

        with patch("routers.sync.tmdb.get_external_ids", side_effect=external_ids) as get_external_ids:
            await _ensure_nuvio_imdb_ids(
                [movie, episode],
                {show.id: show},
                "tmdb-token",
            )

        self.assertEqual(get_external_ids.await_count, 2)
        self.assertEqual(movie.tmdb_data["external_ids"]["imdb_id"], "tt0137523")
        self.assertEqual(show.tmdb_data["external_ids"]["imdb_id"], "tt14688458")


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

    def test_progress_payload_maps_movies_and_episodes(self) -> None:
        updated_at = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
        progress = PlaybackProgress(
            user_id=1,
            media_id=10,
            progress_seconds=1800,
            progress_percent=0.25,
            updated_at=updated_at,
        )
        movie = Media(
            id=10,
            tmdb_id=550,
            media_type=MediaType.movie,
            title="Fight Club",
            runtime=120,
            tmdb_data={"external_ids": {"imdb_id": "tt0137523"}},
        )
        self.assertEqual(
            _nuvio_progress_item(progress, movie),
            {
                "content_id": "tt0137523",
                "content_type": "movie",
                "video_id": "tt0137523",
                "position": 1800000,
                "duration": 7200000,
                "last_watched": int(updated_at.timestamp() * 1000),
            },
        )

        episode = Media(
            id=11,
            media_type=MediaType.episode,
            title="Pilot",
            show_id=5,
            season_number=1,
            episode_number=1,
        )
        show = Show(
            id=5,
            tmdb_id=1396,
            title="Breaking Bad",
            tmdb_data={"external_ids": {"imdb_id": "tt0903747"}},
        )
        self.assertEqual(
            _nuvio_progress_item(progress, episode, show),
            {
                "content_id": "tt0903747",
                "content_type": "series",
                "video_id": "tt0903747:1:1",
                "season": 1,
                "episode": 1,
                "position": 1800000,
                "duration": 7200000,
                "last_watched": int(updated_at.timestamp() * 1000),
            },
        )

    def test_watched_payload_uses_bare_imdb_ids(self) -> None:
        watched_at = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
        movie = Media(
            id=10,
            tmdb_id=550,
            media_type=MediaType.movie,
            title="Fight Club",
            tmdb_data={"external_ids": {"imdb_id": "tt0137523"}},
        )
        self.assertEqual(
            _nuvio_watched_item(movie, watched_at),
            {
                "content_id": "tt0137523",
                "content_type": "movie",
                "title": "Fight Club",
                "watched_at": int(watched_at.timestamp() * 1000),
            },
        )

        episode = Media(
            id=11,
            media_type=MediaType.episode,
            title="It's All Good",
            show_id=5,
            season_number=3,
            episode_number=2,
        )
        show = Show(
            id=5,
            tmdb_id=125988,
            title="Silo",
            tmdb_data={"external_ids": {"imdb_id": "tt14688458"}},
        )
        self.assertEqual(
            _nuvio_watched_item(episode, watched_at, show),
            {
                "content_id": "tt14688458",
                "content_type": "series",
                "title": "It's All Good",
                "season": 3,
                "episode": 2,
                "watched_at": int(watched_at.timestamp() * 1000),
            },
        )

        tmdb_only_movie = Media(
            id=12,
            tmdb_id=550,
            media_type=MediaType.movie,
            title="Fight Club",
        )
        self.assertIsNone(_nuvio_watched_item(tmdb_only_movie, watched_at))


if __name__ == "__main__":
    unittest.main()

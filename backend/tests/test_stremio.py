import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import httpx

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core import stremio
from models.base import MediaType
from models.media import Media
from schemas import MediaServerConnectionResponse
from routers.history import _push_watch_state
from routers.sync import (
    _apply_nuvio_watch_history,
    _pull_stremio_items,
    _push_stremio_connection,
    _stremio_records,
    _stremio_same_item,
    _stremio_sorted_videos,
)


_REAL_ASYNC_CLIENT = httpx.AsyncClient


class StremioClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_link_poll_treats_code_101_as_pending(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/api/v2/read")
            self.assertEqual(request.url.params["type"], "Read")
            self.assertEqual(request.url.params["code"], "ABCD")
            return httpx.Response(
                200,
                json={"error": {"code": 101, "message": "Invalid or expired token"}},
            )

        transport = httpx.MockTransport(handler)
        with patch.object(
            stremio.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            auth_key = await stremio.read_link_code(" abcd ")

        self.assertIsNone(auth_key)

    async def test_datastore_put_uses_official_envelope(self) -> None:
        requests: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"result": {"success": True}})

        transport = httpx.MockTransport(handler)
        with patch.object(
            stremio.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            await stremio.datastore_put("secret-auth-key", [{"_id": "tt0133093"}])

        self.assertEqual(
            requests,
            [
                {
                    "authKey": "secret-auth-key",
                    "collection": "libraryItem",
                    "changes": [{"_id": "tt0133093"}],
                }
            ],
        )


class StremioBitfieldTests(unittest.TestCase):
    def test_watched_bitfield_round_trip_uses_little_endian_bits(self) -> None:
        video_ids = [f"tt0944947:1:{episode}" for episode in range(1, 12)]
        watched = {video_ids[0], video_ids[7], video_ids[8], video_ids[10]}

        serialized = stremio.encode_watched_bitfield(watched, video_ids)

        self.assertIsNotNone(serialized)
        self.assertEqual(stremio.decode_watched_bitfield(serialized, video_ids), watched)

    def test_watched_bitfield_keeps_alignment_when_episodes_are_prepended(self) -> None:
        old_video_ids = ["tt1234567:1:2", "tt1234567:1:3"]
        serialized = stremio.encode_watched_bitfield({old_video_ids[1]}, old_video_ids)
        current_video_ids = ["tt1234567:1:1", *old_video_ids]

        self.assertEqual(
            stremio.decode_watched_bitfield(serialized, current_video_ids),
            {"tt1234567:1:3"},
        )

    def test_malformed_watched_bitfield_is_ignored(self) -> None:
        self.assertEqual(
            stremio.decode_watched_bitfield("invalid", ["tt1234567:1:1"]),
            set(),
        )


class StremioSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_incremental_pull_consumes_datastore_meta_tuples(self) -> None:
        cursor = datetime.now(timezone.utc).replace(tzinfo=None)
        recent_ms = int((cursor - timedelta(minutes=1)).replace(tzinfo=timezone.utc).timestamp() * 1000)
        old_ms = int((cursor - timedelta(minutes=10)).replace(tzinfo=timezone.utc).timestamp() * 1000)
        connection = SimpleNamespace(
            token="auth-key",
            stremio_full_sync_done=True,
            stremio_pull_cursor_at=cursor,
        )

        with (
            patch.object(
                stremio,
                "datastore_meta",
                AsyncMock(return_value=[["tt-recent", recent_ms], ["tt-old", old_ms], ["broken"]]),
            ),
            patch.object(
                stremio,
                "datastore_get",
                AsyncMock(return_value=[{"_id": "tt-recent"}]),
            ) as datastore_get,
        ):
            items, complete_snapshot, started_at = await _pull_stremio_items(
                connection,
                full_resync=False,
            )

        self.assertEqual(items, [{"_id": "tt-recent"}])
        self.assertFalse(complete_snapshot)
        self.assertGreaterEqual(started_at, cursor)
        datastore_get.assert_awaited_once_with("auth-key", ids=["tt-recent"])

    async def test_series_records_use_cinemeta_episode_order_and_progress(self) -> None:
        videos = [
            {"id": "tt0944947:2:1", "season": 2, "episode": 1, "name": "S2E1"},
            {"id": "tt0944947:1:2", "season": 1, "episode": 2, "name": "S1E2"},
            {"id": "tt0944947:1:1", "season": 1, "episode": 1, "name": "S1E1"},
        ]
        sorted_ids = [video["id"] for video in _stremio_sorted_videos({"videos": videos})]
        watched = stremio.encode_watched_bitfield(
            {"tt0944947:1:1", "tt0944947:2:1"},
            sorted_ids,
        )
        item = {
            "_id": "tt0944947",
            "type": "series",
            "name": "Game of Thrones",
            "state": {
                "watched": watched,
                "video_id": "tt0944947:1:2",
                "timeOffset": 120_000,
                "duration": 3_600_000,
                "lastWatched": "2026-07-26T12:00:00Z",
            },
        }

        with patch.object(
            stremio,
            "get_cinemeta_series",
            AsyncMock(return_value={"videos": videos}),
        ):
            library, watched_records, progress, removed = await _stremio_records([item])

        self.assertEqual([record["content_id"] for record in library], ["tt0944947"])
        self.assertEqual(
            {(record["season"], record["episode"]) for record in watched_records},
            {(1, 1), (2, 1)},
        )
        self.assertEqual((progress[0]["season"], progress[0]["episode"]), (1, 2))
        self.assertEqual(progress[0]["position"], 120_000)
        self.assertEqual(removed, set())

    def test_noop_comparison_ignores_only_mtime(self) -> None:
        left = {"_id": "tt0133093", "_mtime": "old", "state": {"timeOffset": 42}, "custom": True}
        same = {**left, "_mtime": "new"}
        changed = {**same, "custom": False}

        self.assertTrue(_stremio_same_item(left, same))
        self.assertFalse(_stremio_same_item(left, changed))

    async def test_full_push_preserves_unrelated_remote_items(self) -> None:
        connection = SimpleNamespace(
            id=44,
            token="auth-key",
            push_collection=True,
            push_watched=False,
            push_playback=False,
            stremio_pushed_library_ids=None,
        )
        local = {
            "content_id": "tt0133093",
            "content_type": "movie",
            "name": "The Matrix",
            "poster": "https://example.test/matrix.jpg",
            "poster_shape": "poster",
        }
        remote_only = {
            "_id": "tt9999999",
            "name": "Remote only",
            "type": "movie",
            "removed": False,
            "temp": False,
            "_ctime": "2026-01-01T00:00:00Z",
            "_mtime": "2026-01-01T00:00:00Z",
            "state": {"customPlaybackState": True},
            "unknownField": {"preserve": True},
        }
        datastore_put = AsyncMock()

        with (
            patch(
                "routers.sync._build_nuvio_library_items",
                AsyncMock(return_value=[local]),
            ),
            patch.object(
                stremio,
                "datastore_get",
                AsyncMock(return_value=[remote_only]),
            ),
            patch.object(stremio, "datastore_put", datastore_put),
        ):
            changed = await _push_stremio_connection(
                SimpleNamespace(),
                connection,
                7,
                api_key="tmdb-key",
            )

        self.assertEqual(changed, 1)
        pushed = datastore_put.await_args.args[1]
        self.assertEqual([item["_id"] for item in pushed], ["tt0133093"])
        self.assertEqual(remote_only["unknownField"], {"preserve": True})
        self.assertEqual(connection.stremio_pushed_library_ids, ["tt0133093"])

    async def test_remote_removal_requires_prior_scrob_push(self) -> None:
        connection = SimpleNamespace(
            id=45,
            token="auth-key",
            push_collection=True,
            push_watched=False,
            push_playback=False,
            stremio_pushed_library_ids=["tt0133093"],
        )
        remote_items = [
            {
                "_id": "tt0133093",
                "name": "The Matrix",
                "type": "movie",
                "removed": False,
                "temp": False,
                "_ctime": "2026-01-01T00:00:00Z",
                "_mtime": "2026-01-01T00:00:00Z",
                "state": {},
            },
            {
                "_id": "tt9999999",
                "name": "Remote only",
                "type": "movie",
                "removed": False,
                "temp": False,
                "_ctime": "2026-01-01T00:00:00Z",
                "_mtime": "2026-01-01T00:00:00Z",
                "state": {},
            },
        ]
        datastore_put = AsyncMock()

        with (
            patch(
                "routers.sync._build_nuvio_library_items",
                AsyncMock(return_value=[]),
            ),
            patch.object(
                stremio,
                "datastore_get",
                AsyncMock(return_value=remote_items),
            ),
            patch.object(stremio, "datastore_put", datastore_put),
        ):
            changed = await _push_stremio_connection(
                SimpleNamespace(),
                connection,
                7,
                api_key="tmdb-key",
            )

        self.assertEqual(changed, 1)
        pushed = datastore_put.await_args.args[1]
        self.assertEqual([item["_id"] for item in pushed], ["tt0133093"])
        self.assertTrue(pushed[0]["removed"])
        self.assertEqual(connection.stremio_pushed_library_ids, [])

    async def test_unknown_watch_creates_temporary_item_without_fabricating_date(self) -> None:
        connection = SimpleNamespace(
            id=46,
            token="auth-key",
            push_collection=False,
            push_watched=True,
            push_playback=False,
            stremio_pushed_library_ids=None,
        )
        record = {
            "content_id": "tt0133093",
            "content_type": "movie",
            "title": "The Matrix",
            "watched_at": None,
        }
        datastore_put = AsyncMock()
        with (
            patch(
                "routers.sync._build_nuvio_watched_items",
                AsyncMock(return_value=[record]),
            ) as build_watched,
            patch(
                "routers.sync._stremio_media_records",
                AsyncMock(return_value={10: {key: record[key] for key in ("content_id", "content_type", "title")}}),
            ),
            patch(
                "routers.sync._latest_watched_at",
                AsyncMock(return_value={10: None}),
            ),
            patch.object(stremio, "datastore_get", AsyncMock(return_value=[])),
            patch.object(stremio, "datastore_put", datastore_put),
        ):
            changed = await _push_stremio_connection(
                SimpleNamespace(),
                connection,
                7,
                api_key="tmdb-key",
                changed_media_ids={10},
                watch_overrides={10: True},
            )

        self.assertEqual(changed, 1)
        build_watched.assert_awaited_once_with(
            ANY,
            7,
            media_ids={10},
            api_key="tmdb-key",
            include_unknown_dates=True,
        )
        pushed = datastore_put.await_args.args[1][0]
        self.assertTrue(pushed["removed"])
        self.assertTrue(pushed["temp"])
        self.assertEqual(pushed["state"]["timesWatched"], 1)
        self.assertIsNone(pushed["state"]["lastWatched"])

    async def test_unwatch_clears_state_without_erasing_last_known_date(self) -> None:
        connection = SimpleNamespace(
            id=47,
            token="auth-key",
            push_collection=False,
            push_watched=True,
            push_playback=False,
            stremio_pushed_library_ids=None,
        )
        record = {
            "content_id": "tt0133093",
            "content_type": "movie",
            "title": "The Matrix",
        }
        remote = {
            "_id": "tt0133093",
            "name": "The Matrix",
            "type": "movie",
            "removed": True,
            "temp": True,
            "_ctime": "2026-01-01T00:00:00Z",
            "_mtime": "2026-01-01T00:00:00Z",
            "state": {
                "timesWatched": 1,
                "lastWatched": "2026-01-01T00:00:00Z",
            },
        }
        datastore_put = AsyncMock()
        with (
            patch(
                "routers.sync._build_nuvio_watched_items",
                AsyncMock(return_value=[]),
            ),
            patch(
                "routers.sync._stremio_media_records",
                AsyncMock(return_value={10: record}),
            ),
            patch(
                "routers.sync._latest_watched_at",
                AsyncMock(return_value={}),
            ),
            patch.object(stremio, "datastore_get", AsyncMock(return_value=[remote])),
            patch.object(stremio, "datastore_put", datastore_put),
        ):
            await _push_stremio_connection(
                SimpleNamespace(),
                connection,
                7,
                api_key="tmdb-key",
                changed_media_ids={10},
                watch_overrides={10: False},
            )

        pushed = datastore_put.await_args.args[1][0]
        self.assertEqual(pushed["state"]["timesWatched"], 0)
        self.assertEqual(
            pushed["state"]["lastWatched"],
            "2026-01-01T00:00:00Z",
        )

    async def test_progress_without_collection_creates_temporary_item(self) -> None:
        connection = SimpleNamespace(
            id=48,
            token="auth-key",
            push_collection=False,
            push_watched=False,
            push_playback=True,
            stremio_pushed_library_ids=None,
        )
        progress = {
            "content_id": "tt0133093",
            "content_type": "movie",
            "title": "The Matrix",
            "position": 120_000,
            "duration": 600_000,
            "last_watched": None,
        }
        datastore_put = AsyncMock()
        with (
            patch(
                "routers.sync._build_nuvio_progress_items",
                AsyncMock(return_value=[progress]),
            ),
            patch.object(stremio, "datastore_get", AsyncMock(return_value=[])),
            patch.object(stremio, "datastore_put", datastore_put),
        ):
            await _push_stremio_connection(
                SimpleNamespace(),
                connection,
                7,
                api_key="tmdb-key",
            )

        pushed = datastore_put.await_args.args[1][0]
        self.assertTrue(pushed["removed"])
        self.assertTrue(pushed["temp"])
        self.assertEqual(pushed["state"]["timeOffset"], 120_000)
        self.assertEqual(pushed["state"]["duration"], 600_000)


class StremioCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_inbound_watch_is_persisted_without_a_date(self) -> None:
        movie = Media(
            id=10,
            tmdb_id=603,
            media_type=MediaType.movie,
            title="The Matrix",
        )
        media_result = SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [movie]),
        )
        existing_result = SimpleNamespace(all=lambda: [])
        db = SimpleNamespace(
            execute=AsyncMock(side_effect=[media_result, existing_result]),
            add=MagicMock(),
            commit=AsyncMock(),
        )

        added = await _apply_nuvio_watch_history(
            db,
            user_id=7,
            rows=[
                {
                    "content_id": "tt0133093",
                    "content_type": "movie",
                    "watched_at": None,
                }
            ],
            show_map={},
            tmdb_ids={"tt0133093": 603},
            include_unknown_dates=True,
        )

        self.assertEqual(added, {10})
        self.assertIsNone(db.add.call_args.args[0].watched_at)

    async def test_manual_unwatch_is_forwarded_to_stremio(self) -> None:
        connection = SimpleNamespace(id=49, type="stremio")
        settings = SimpleNamespace(
            trakt_push_watched=False,
            mdblist_push_watched=False,
            simkl_push_watched=False,
        )
        connection_result = SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [connection]),
        )
        settings_result = SimpleNamespace(scalar_one_or_none=lambda: settings)
        files_result = SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: []),
        )
        db = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[connection_result, settings_result, files_result],
            ),
            commit=AsyncMock(),
        )
        push = AsyncMock()
        with (
            patch(
                "routers.sync._get_effective_tmdb_key",
                AsyncMock(return_value="tmdb-key"),
            ),
            patch("routers.sync._push_stremio_connection", push),
        ):
            await _push_watch_state(
                db,
                user_id=7,
                media_ids=[10],
                watched=False,
            )

        push.assert_awaited_once_with(
            db,
            connection,
            7,
            api_key="tmdb-key",
            changed_media_ids={10},
            watch_overrides={10: False},
        )
        db.commit.assert_awaited_once()


    def test_connection_response_redacts_auth_key(self) -> None:
        response = MediaServerConnectionResponse.model_validate(
            {
                "id": 1,
                "user_id": 7,
                "type": "stremio",
                "name": "Stremio",
                "url": stremio.DEFAULT_URL,
                "token": "secret-auth-key",
                "created_at": datetime(2026, 7, 31),
            }
        )

        self.assertEqual(response.token, "")

import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core import arvio
from models.base import MediaType
from models.media import Media
from models.playback_progress import PlaybackProgress
from models.show import Show
from models.events import WatchEvent
from routers.sync import (
    _apply_arvio_playback_progress,
    _apply_arvio_watched_episode,
    _apply_arvio_watched_movie,
    _parse_arvio_timestamp,
    _run_arvio_sync,
)

_REAL_ASYNC_CLIENT = httpx.AsyncClient

class _Result:
    def __init__(self, *, scalars=None, rows=None):
        self._scalars = scalars or []
        self._rows = rows or []

    def scalars(self):
        return _Result(rows=self._scalars)

    def all(self):
        return self._rows

    def first(self):
        return self._scalars[0] if self._scalars else None

    def scalar_one_or_none(self):
        return self._scalars[0] if self._scalars else None


class ArvioClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_sign_in_and_parse_session(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertTrue(request.url.path.endswith("/auth-login"))
            body = json.loads(request.content)
            self.assertEqual(body["email"], "test@arvio.tv")
            self.assertEqual(body["password"], "secret")
            return httpx.Response(
                200,
                json={
                    "access_token": "access-123",
                    "refresh_token": "refresh-123",
                    "expires_in": 3600,
                    "user": {"id": "usr-1", "email": "test@arvio.tv"},
                },
            )

        transport = httpx.MockTransport(handler)
        with patch.object(httpx, "AsyncClient", lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs)):
            session = await arvio.sign_in("https://auth.arvio.tv/.netlify/functions", "test@arvio.tv", "secret")
            self.assertEqual(session.access_token, "access-123")
            self.assertEqual(session.refresh_token, "refresh-123")
            self.assertEqual(session.user_id, "usr-1")

    async def test_refresh_session(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertTrue(request.url.path.endswith("/auth-refresh"))
            body = json.loads(request.content)
            self.assertEqual(body["refresh_token"], "old-refresh")
            return httpx.Response(
                200,
                json={
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 3600,
                },
            )

        transport = httpx.MockTransport(handler)
        with patch.object(httpx, "AsyncClient", lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs)):
            session = await arvio.refresh_session("https://auth.arvio.tv/.netlify/functions", "old-refresh")
            self.assertEqual(session.access_token, "new-access")
            self.assertEqual(session.refresh_token, "new-refresh")

    async def test_pull_snapshot_json_string_and_profile_extraction(self) -> None:
        raw_payload_dict = {
            "profiles": [{"id": "prof-1", "name": "Alice"}, {"id": "prof-2", "name": "Bob"}],
            "activeProfileId": "prof-1",
            "localWatchedMoviesByProfile": {
                "prof-1": [{"tmdbId": 550, "watchedAt": "2026-08-10T12:00:00Z"}]
            },
        }

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertTrue(request.url.path.endswith("/account-sync-pull"))
            self.assertEqual(request.headers["authorization"], "Bearer test-access")
            return httpx.Response(
                200,
                json={"payload": json.dumps(raw_payload_dict)},
            )

        transport = httpx.MockTransport(handler)
        with patch.object(httpx, "AsyncClient", lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs)):
            snapshot = await arvio.pull_snapshot("https://auth.arvio.tv/.netlify/functions", "test-access")
            profiles = arvio.extract_profiles(snapshot)
            self.assertEqual(len(profiles), 2)
            self.assertEqual(profiles[0], {"id": "prof-1", "name": "Alice"})
            self.assertEqual(profiles[1], {"id": "prof-2", "name": "Bob"})

    async def test_validate_connection_executes_on_refresh(self) -> None:
        refreshed_sessions: list[arvio.ArvioSession] = []

        async def on_refresh_cb(session: arvio.ArvioSession) -> None:
            refreshed_sessions.append(session)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/auth-refresh"):
                return httpx.Response(200, json={"access_token": "acc-1", "refresh_token": "ref-2"})
            if request.url.path.endswith("/account-sync-pull"):
                return httpx.Response(
                    200,
                    json={"payload": {"profiles": [{"id": "p1", "name": "Main"}]}},
                )
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        with patch.object(httpx, "AsyncClient", lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs)):
            session, profiles = await arvio.validate_connection(
                "https://auth.arvio.tv/.netlify/functions",
                "ref-1",
                "p1",
                on_refresh=on_refresh_cb,
            )
            self.assertEqual(session.refresh_token, "ref-2")
            self.assertEqual(len(refreshed_sessions), 1)
            self.assertEqual(refreshed_sessions[0].refresh_token, "ref-2")
            self.assertEqual(profiles[0]["name"], "Main")

    async def test_validate_connection_rejects_unknown_profile(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/auth-refresh"):
                return httpx.Response(200, json={"access_token": "acc-1", "refresh_token": "ref-2"})
            if request.url.path.endswith("/account-sync-pull"):
                return httpx.Response(
                    200,
                    json={"payload": {"profiles": [{"id": "p1", "name": "Main"}]}},
                )
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        with patch.object(httpx, "AsyncClient", lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs)):
            with self.assertRaises(arvio.ArvioAPIError):
                await arvio.validate_connection(
                    "https://auth.arvio.tv/.netlify/functions",
                    "ref-1",
                    "non-existent-profile",
                )


class ArvioNormalizationTests(unittest.TestCase):
    def test_parse_arvio_timestamp(self) -> None:
        dt1 = _parse_arvio_timestamp(1711600000000)
        self.assertIsNotNone(dt1)
        self.assertEqual(dt1.year, 2024)

        dt2 = _parse_arvio_timestamp("2026-08-14T10:00:00Z")
        self.assertIsNotNone(dt2)
        self.assertEqual(dt2.year, 2026)

        self.assertIsNone(_parse_arvio_timestamp(None))
        self.assertIsNone(_parse_arvio_timestamp("invalid-date"))


class ArvioApplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_apply_arvio_watched_movie(self) -> None:
        db = AsyncMock()
        db.add = MagicMock()
        db.execute = AsyncMock(side_effect=[
            _Result(scalars=[]),  # Media search
            _Result(scalars=[]),  # WatchEvent search
        ])

        added = await _apply_arvio_watched_movie(
            db,
            user_id=1,
            item={"tmdbId": 550, "title": "Fight Club", "watchedAt": "2026-08-10T12:00:00Z"},
            tmdb_api_key=None,
        )
        self.assertTrue(added)
        self.assertEqual(db.add.call_count, 2)  # Media + WatchEvent

    async def test_apply_arvio_watched_episode(self) -> None:
        db = AsyncMock()
        db.add = MagicMock()
        db.execute = AsyncMock(side_effect=[
            _Result(scalars=[]),  # Show search
            _Result(scalars=[]),  # Media episode search
            _Result(scalars=[]),  # WatchEvent search
        ])

        added = await _apply_arvio_watched_episode(
            db,
            user_id=1,
            item={
                "showTmdbId": 1396,
                "season": 1,
                "episode": 1,
                "title": "Breaking Bad",
                "watchedAt": 1711600000000,
            },
            tmdb_api_key=None,
        )
        self.assertTrue(added)

    async def test_apply_arvio_playback_progress(self) -> None:
        db = AsyncMock()
        db.add = MagicMock()
        db.execute = AsyncMock(side_effect=[
            _Result(scalars=[]),  # Movie search
            _Result(scalars=[]),  # PlaybackProgress search
        ])

        added = await _apply_arvio_playback_progress(
            db,
            user_id=1,
            item={
                "tmdbId": 550,
                "mediaType": "MOVIE",
                "progress": 45.0,
                "resumePositionSeconds": 2700,
                "durationSeconds": 6000,
            },
            tmdb_api_key=None,
        )
        self.assertTrue(added)


if __name__ == "__main__":
    unittest.main()

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

from fastapi import HTTPException

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://test:test@localhost/test",
)

import httpx

from core import trakt
from core.trakt_export import TraktExportData
from models.base import MediaType
from models.media import Media
from routers import trakt as trakt_router


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


    async def test_get_ratings_fetches_movies_shows_seasons_and_episodes(self) -> None:
        requested: list[tuple[str, int]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            kind = request.url.path.rsplit("/", 1)[-1]
            page = int(request.url.params["page"])
            requested.append((kind, page))
            payload = {
                "movies": {"movie": {"ids": {"tmdb": 550}}},
                "shows": {"show": {"ids": {"tmdb": 1396}}},
                "seasons": {
                    "show": {"ids": {"tmdb": 1396}},
                    "season": {"number": page, "ids": {"tmdb": 3572 + page - 1}},
                },
                "episodes": {
                    "show": {"ids": {"tmdb": 1396}},
                    "episode": {"season": 1, "number": page, "ids": {"tmdb": 62085 + page - 1}},
                },
            }
            return httpx.Response(
                200,
                json=[payload[kind]],
                headers={"X-Pagination-Page-Count": "2"},
            )

        transport = httpx.MockTransport(handler)
        with patch.object(
            trakt.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            ratings = await trakt.get_ratings("client-id", "access-token")

        self.assertCountEqual(
            requested,
            [
                ("movies", 1),
                ("movies", 2),
                ("shows", 1),
                ("shows", 2),
                ("seasons", 1),
                ("seasons", 2),
                ("episodes", 1),
                ("episodes", 2),
            ],
        )
        self.assertEqual(len(ratings["seasons"]), 2)
        self.assertEqual(ratings["seasons"][1]["season"]["number"], 2)
        self.assertEqual(len(ratings["episodes"]), 2)
        self.assertEqual(ratings["episodes"][1]["episode"]["number"], 2)

    async def test_history_time_window_is_forwarded(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json=[],
                headers={"X-Pagination-Page-Count": "1"},
            )

        transport = httpx.MockTransport(handler)
        with patch.object(
            trakt.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            await trakt.get_history_movies(
                "client-id",
                "access-token",
                start_at=datetime(2026, 7, 20, 10, 0, 0),
                end_at=datetime(2026, 7, 21, 10, 0, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(
            requests[0].url.params["start_at"],
            "2026-07-20T10:00:00.000Z",
        )
        self.assertEqual(
            requests[0].url.params["end_at"],
            "2026-07-21T10:00:00.000Z",
        )

    async def test_history_batch_preserves_each_watched_at(self) -> None:
        payloads: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payloads.append(json.loads(request.content))
            return httpx.Response(201, json={"added": {"movies": 1, "episodes": 1}})

        transport = httpx.MockTransport(handler)
        with patch.object(
            trakt.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            await trakt.add_to_history_batch(
                "client-id",
                "access-token",
                [(550, datetime(2024, 3, 28, 6, 40, 0, 123456))],
                [(1396, 1, 3, datetime(2024, 3, 29, 8, 13, 20))],
            )

        self.assertEqual(
            payloads[0]["movies"],
            [{"ids": {"tmdb": 550}, "watched_at": "2024-03-28T06:40:00.123Z"}],
        )
        self.assertEqual(
            payloads[0]["shows"][0]["seasons"][0]["episodes"],
            [{"number": 3, "watched_at": "2024-03-29T08:13:20.000Z"}],
        )

    async def test_history_batch_serializes_none_as_unknown_sentinel(self) -> None:
        payloads: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payloads.append(json.loads(request.content))
            return httpx.Response(201, json={"added": {"movies": 1, "episodes": 1}})

        transport = httpx.MockTransport(handler)
        with patch.object(
            trakt.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            await trakt.add_to_history_batch(
                "client-id",
                "access-token",
                [(550, None)],
                [(1396, 1, 3, None)],
            )

        self.assertEqual(
            payloads[0]["movies"],
            [{"ids": {"tmdb": 550}, "watched_at": "unknown"}],
        )
        self.assertEqual(
            payloads[0]["shows"][0]["seasons"][0]["episodes"],
            [{"number": 3, "watched_at": "unknown"}],
        )

    async def test_rating_batches_preserve_season_tmdb_ids(self) -> None:
        requests: list[tuple[str, dict]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={"added": {}, "deleted": {}})

        transport = httpx.MockTransport(handler)
        with patch.object(
            trakt.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            await trakt.set_ratings_batch(
                "client-id",
                "access-token",
                [(550, 8.4)],
                [(1396, 9.0)],
                [(3572, 7.6)],
            )
            await trakt.remove_ratings_batch(
                "client-id",
                "access-token",
                [550],
                [1396],
                [3572],
            )

        self.assertEqual(requests[0][0], "/sync/ratings")
        self.assertEqual(
            requests[0][1]["seasons"],
            [{"rating": 8, "ids": {"tmdb": 3572}}],
        )
        self.assertEqual(requests[1][0], "/sync/ratings/remove")
        self.assertEqual(
            requests[1][1]["seasons"],
            [{"ids": {"tmdb": 3572}}],
        )


class _Result:
    def __init__(self, *, scalar=None, scalars=None, rows=None):
        self._scalar = scalar
        self._scalars = scalars or []
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return self._scalars or self._rows

    def first(self):
        items = self._scalars or self._rows
        return items[0] if items else None

    def __iter__(self):
        return iter(self._rows)

class _FakeSession:
    def __init__(self, settings, watch_rows, media, shows, incomplete_watch_rows=()):
        self.settings = settings
        self.watch_rows = watch_rows
        self.incomplete_watch_rows = incomplete_watch_rows
        self.media = media
        self.shows = shows
        self.commit = AsyncMock()
        self.added: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = len(self.added) + 1
        self.added.append(obj)

    async def flush(self):
        pass

    def begin_nested(self):
        return self

    async def execute(self, statement):
        sql = str(statement)
        if "FROM user_settings" in sql:
            return _Result(scalar=self.settings)
        if "FROM watch_events" in sql:
            rows = self.watch_rows
            if "watch_events.completed" not in sql:
                rows = [*rows, *self.incomplete_watch_rows]
            return _Result(rows=rows)
        if "FROM media" in sql:
            return _Result(scalars=self.media)
        if "FROM shows" in sql:
            return _Result(scalars=self.shows)
        return _Result()


class TraktHistorySafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_incremental_window_overlaps_cursor(self) -> None:
        cursor = datetime(2026, 7, 21, 10, 0, 0)
        cutoff = datetime(2026, 7, 21, 11, 0, 0)

        start_at, end_at = trakt_router._history_window(cursor, False, cutoff)
        self.assertEqual(start_at, cursor - timedelta(minutes=5))
        self.assertEqual(end_at, cutoff)
        self.assertEqual(
            trakt_router._history_window(cursor, True, cutoff),
            (None, cutoff),
        )

    async def test_incremental_pull_advances_cursor_only_after_success(self) -> None:
        original_cursor = datetime(2026, 7, 21, 10, 0, 0)
        settings = SimpleNamespace(
            trakt_access_token="access-token",
            trakt_client_id="client-id",
            trakt_client_secret=None,
            trakt_refresh_token=None,
            trakt_history_cursor_at=original_cursor,
            trakt_sync_watched=True,
            trakt_sync_ratings=False,
            trakt_sync_lists=False,
            tmdb_api_key="tmdb-token",
        )
        session = _FakeSession(settings, [], [], [])
        get_movies = AsyncMock(return_value=[])
        get_episodes = AsyncMock(return_value=[])

        with (
            patch.object(
                trakt_router,
                "async_sessionmaker",
                return_value=lambda: session,
            ),
            patch.object(
                trakt_router.trakt_client,
                "validate_token",
                AsyncMock(return_value=True),
            ),
            patch.object(
                trakt_router.trakt_client,
                "get_history_movies",
                get_movies,
            ),
            patch.object(
                trakt_router.trakt_client,
                "get_history_episodes",
                get_episodes,
            ),
            patch(
                "routers.sync._fan_out_changes_to_other_connections",
                AsyncMock(),
            ),
        ):
            await trakt_router.run_trakt_sync(user_id=1, job_id=21)

        self.assertGreater(settings.trakt_history_cursor_at, original_cursor)
        self.assertEqual(
            get_movies.await_args.kwargs["start_at"],
            original_cursor - timedelta(minutes=5),
        )
        self.assertEqual(
            get_movies.await_args.kwargs["end_at"],
            settings.trakt_history_cursor_at,
        )
        self.assertEqual(
            get_episodes.await_args.kwargs,
            get_movies.await_args.kwargs,
        )

    async def test_failed_incremental_pull_does_not_advance_cursor(self) -> None:
        original_cursor = datetime(2026, 7, 21, 10, 0, 0)
        settings = SimpleNamespace(
            trakt_access_token="access-token",
            trakt_client_id="client-id",
            trakt_client_secret=None,
            trakt_refresh_token=None,
            trakt_history_cursor_at=original_cursor,
            trakt_sync_watched=True,
            trakt_sync_ratings=False,
            trakt_sync_lists=False,
            tmdb_api_key="tmdb-token",
        )
        session = _FakeSession(settings, [], [], [])

        with (
            patch.object(
                trakt_router,
                "async_sessionmaker",
                return_value=lambda: session,
            ),
            patch.object(
                trakt_router.trakt_client,
                "validate_token",
                AsyncMock(return_value=True),
            ),
            patch.object(
                trakt_router.trakt_client,
                "get_history_movies",
                AsyncMock(side_effect=RuntimeError("Trakt unavailable")),
            ),
        ):
            await trakt_router.run_trakt_sync(user_id=1, job_id=22)

        self.assertEqual(settings.trakt_history_cursor_at, original_cursor)

    async def test_full_push_skips_remote_event_after_timestamped_push(self) -> None:
        watched_at = datetime(2024, 3, 28, 6, 40, 0)
        settings = SimpleNamespace(
            trakt_access_token="access-token",
            trakt_client_id="client-id",
            trakt_push_watched=True,
            trakt_push_collection=False,
            trakt_push_ratings=False,
        )
        movie = Media(
            id=10,
            tmdb_id=550,
            media_type=MediaType.movie,
            title="Fight Club",
        )
        session = _FakeSession(settings, [(movie.id, watched_at)], [movie], [])
        add_batch = AsyncMock(return_value=None)
        remote_movie = {
            "id": 987,
            "watched_at": "2024-03-28T06:40:00.000Z",
            "movie": {"ids": {"tmdb": 550}},
        }

        with (
            patch.object(
                trakt_router,
                "async_sessionmaker",
                return_value=lambda: session,
            ),
            patch.object(
                trakt_router.trakt_client,
                "get_history_movies",
                AsyncMock(side_effect=[[], [remote_movie]]),
            ),
            patch.object(
                trakt_router.trakt_client,
                "get_history_episodes",
                AsyncMock(side_effect=[[], []]),
            ),
            patch.object(
                trakt_router.trakt_client,
                "add_to_history_batch",
                add_batch,
            ),
        ):
            await trakt_router._run_trakt_push(user_id=1, job_id=11)
            await trakt_router._run_trakt_push(user_id=1, job_id=12)

        add_batch.assert_awaited_once_with(
            "client-id",
            "access-token",
            [(550, watched_at)],
            [],
        )

    async def test_full_push_preserves_multiple_completed_plays(self) -> None:
        earlier = datetime(2024, 3, 28, 6, 40, 0)
        latest = datetime(2024, 4, 2, 20, 0, 0)
        settings = SimpleNamespace(
            trakt_access_token="access-token",
            trakt_client_id="client-id",
            trakt_push_watched=True,
            trakt_push_collection=False,
            trakt_push_ratings=False,
        )
        movie = Media(
            id=10,
            tmdb_id=550,
            media_type=MediaType.movie,
            title="Fight Club",
        )
        session = _FakeSession(
            settings,
            [(movie.id, earlier), (movie.id, latest)],
            [movie],
            [],
        )
        add_batch = AsyncMock(return_value=None)

        with (
            patch.object(
                trakt_router,
                "async_sessionmaker",
                return_value=lambda: session,
            ),
            patch.object(
                trakt_router.trakt_client,
                "get_history_movies",
                AsyncMock(return_value=[]),
            ),
            patch.object(
                trakt_router.trakt_client,
                "get_history_episodes",
                AsyncMock(return_value=[]),
            ),
            patch.object(
                trakt_router.trakt_client,
                "add_to_history_batch",
                add_batch,
            ),
        ):
            await trakt_router._run_trakt_push(user_id=1, job_id=13)

        add_batch.assert_awaited_once_with(
            "client-id",
            "access-token",
            [(550, earlier), (550, latest)],
            [],
        )

    async def test_full_push_ignores_incomplete_watch_events(self) -> None:
        watched_at = datetime(2024, 3, 28, 6, 40, 0)
        settings = SimpleNamespace(
            trakt_access_token="access-token",
            trakt_client_id="client-id",
            trakt_push_watched=True,
            trakt_push_collection=False,
            trakt_push_ratings=False,
        )
        movie = Media(
            id=10,
            tmdb_id=550,
            media_type=MediaType.movie,
            title="Fight Club",
        )
        session = _FakeSession(
            settings,
            [],
            [movie],
            [],
            incomplete_watch_rows=[(movie.id, watched_at)],
        )
        add_batch = AsyncMock(return_value=None)

        with (
            patch.object(
                trakt_router,
                "async_sessionmaker",
                return_value=lambda: session,
            ),
            patch.object(
                trakt_router.trakt_client,
                "add_to_history_batch",
                add_batch,
            ),
        ):
            await trakt_router._run_trakt_push(user_id=1, job_id=14)

        add_batch.assert_not_awaited()

    def test_normalize_history_time_preserves_none(self) -> None:
        self.assertIsNone(trakt_router._normalize_history_time(None))

    def test_parse_trakt_datetime_treats_unknown_sentinel_as_none(self) -> None:
        self.assertIsNone(trakt_router._parse_trakt_datetime("unknown"))
        self.assertIsNone(trakt_router._parse_trakt_datetime(None))
        self.assertIsNone(trakt_router._parse_trakt_datetime(""))

    def test_parse_trakt_datetime_treats_unix_epoch_as_none(self) -> None:
        """Regression test: verified against the live Trakt API — a history
        entry submitted with watched_at="unknown" is not echoed back as the
        literal string on read, it comes back as 1970-01-01T00:00:00.000Z.
        That must be recognized as unknown too, or a pulled/deduped entry
        would silently get a fabricated real (wrong) watch date."""
        self.assertIsNone(trakt_router._parse_trakt_datetime("1970-01-01T00:00:00.000Z"))
        self.assertIsNone(trakt_router._parse_trakt_datetime("1970-01-01T00:00:00Z"))
        # A genuine (if extremely unlikely) play at a non-zero time on that
        # same date is not the sentinel and must still parse normally.
        self.assertIsNotNone(trakt_router._parse_trakt_datetime("1970-01-01T00:00:01.000Z"))

    def test_remote_history_keys_includes_unknown_dated_entries(self) -> None:
        # Covers both shapes: the literal sentinel (documented write value,
        # defensive-only since Trakt doesn't echo it back) and the Unix epoch
        # (what Trakt actually returns on read for the same entry, verified
        # against the live API).
        remote_movies = [
            {"watched_at": "unknown", "movie": {"ids": {"tmdb": 550}}},
            {"watched_at": "1970-01-01T00:00:00.000Z", "movie": {"ids": {"tmdb": 13}}},
        ]
        remote_episodes = [{
            "watched_at": "1970-01-01T00:00:00.000Z",
            "show": {"ids": {"tmdb": 1396}},
            "episode": {"season": 1, "number": 3},
        }]

        keys = trakt_router._remote_history_keys(remote_movies, remote_episodes)

        self.assertIn(("movie", 550, None), keys)
        self.assertIn(("movie", 13, None), keys)
        self.assertIn(("episode", 1396, 1, 3, None), keys)

    async def test_full_push_handles_mix_of_known_and_unknown_dates(self) -> None:
        """Regression test: before the None-guards, a single unknown-dated local
        watch event crashed the entire push job (AttributeError in
        _normalize_history_time), silently dropping every other pending push too."""
        known_at = datetime(2024, 3, 28, 6, 40, 0)
        settings = SimpleNamespace(
            trakt_access_token="access-token",
            trakt_client_id="client-id",
            trakt_push_watched=True,
            trakt_push_collection=False,
            trakt_push_ratings=False,
        )
        movie1 = Media(id=10, tmdb_id=550, media_type=MediaType.movie, title="Fight Club")
        movie2 = Media(id=11, tmdb_id=680, media_type=MediaType.movie, title="Pulp Fiction")
        session = _FakeSession(
            settings,
            [(movie1.id, known_at), (movie2.id, None)],
            [movie1, movie2],
            [],
        )
        add_batch = AsyncMock(return_value=None)
        get_movies = AsyncMock(return_value=[])
        get_episodes = AsyncMock(return_value=[])

        with (
            patch.object(
                trakt_router,
                "async_sessionmaker",
                return_value=lambda: session,
            ),
            patch.object(trakt_router.trakt_client, "get_history_movies", get_movies),
            patch.object(trakt_router.trakt_client, "get_history_episodes", get_episodes),
            patch.object(trakt_router.trakt_client, "add_to_history_batch", add_batch),
        ):
            await trakt_router._run_trakt_push(user_id=1, job_id=15)

        add_batch.assert_awaited_once_with(
            "client-id",
            "access-token",
            [(550, known_at), (680, None)],
            [],
        )
        # A mix of known and unknown local candidates can't be scoped to a time
        # window, so the remote-dedup fetch falls back to unbounded.
        self.assertIsNone(get_movies.await_args.kwargs["start_at"])
        self.assertIsNone(get_movies.await_args.kwargs["end_at"])


class TraktSourceAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_source_delegates_to_trakt_client(self) -> None:
        source = trakt_router.LiveTraktSource("client-id", "access-token")
        start = datetime(2026, 1, 1)
        end = datetime(2026, 1, 2)

        with patch.object(trakt_router.trakt_client, "get_history_movies", AsyncMock(return_value=["m"])) as m:
            self.assertEqual(await source.get_history_movies(start, end), ["m"])
            m.assert_awaited_once_with("client-id", "access-token", start_at=start, end_at=end)

        with patch.object(trakt_router.trakt_client, "get_ratings", AsyncMock(return_value={"movies": []})) as m:
            self.assertEqual(await source.get_ratings(), {"movies": []})
            m.assert_awaited_once_with("client-id", "access-token")

        with patch.object(trakt_router.trakt_client, "get_list_items", AsyncMock(return_value=["item"])) as m:
            self.assertEqual(await source.get_list_items("my-list"), ["item"])
            m.assert_awaited_once_with("client-id", "access-token", "my-list")

    async def test_export_source_serves_parsed_data_without_network(self) -> None:
        data = TraktExportData(
            history_movies=[{"movie": "a"}],
            history_episodes=[{"episode": "b"}],
            ratings={"movies": [], "shows": [], "seasons": [], "episodes": []},
            watchlist=[{"type": "movie"}],
            lists=[{"name": "x"}],
            list_items={"x": [{"type": "show"}]},
        )
        source = trakt_router.ExportTraktSource(data)

        self.assertEqual(await source.get_history_movies(None, datetime(2026, 1, 1)), data.history_movies)
        self.assertEqual(await source.get_history_episodes(None, datetime(2026, 1, 1)), data.history_episodes)
        self.assertEqual(await source.get_ratings(), data.ratings)
        self.assertEqual(await source.get_watchlist(), data.watchlist)
        self.assertEqual(await source.get_user_lists(), data.lists)
        self.assertEqual(await source.get_list_items("x"), [{"type": "show"}])
        # A slug with no matching item file (e.g. an empty list) should not raise.
        self.assertEqual(await source.get_list_items("missing"), [])


class _NestedTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _RatingsFakeDB:
    """Minimal DB double for exercising _apply_trakt_import's ratings loop only.

    Sync watched/lists are left disabled by the test so this never needs to
    support db.add()/db.flush() — only the calls the ratings loop itself makes.
    """

    def __init__(self):
        self.commit = AsyncMock()

    def begin_nested(self):
        return _NestedTxn()

    async def execute(self, statement):
        return _Result(rows=[])


class TraktImportUploadSizeCapTests(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_upload_is_rejected_without_buffering_it_all(self) -> None:
        # Regression: trakt_import_upload used to do a single unbounded
        # `await file.read()`, buffering the entire request body into memory
        # before any size check ran at all. It must now bail out mid-read.
        chunk = b"0" * (2 * 1024 * 1024)  # 2MB per read() call
        read_calls = 0

        class _FakeUploadFile:
            filename = "export.zip"

            async def read(self, size: int = -1) -> bytes:
                nonlocal read_calls
                read_calls += 1
                # If the cap weren't enforced mid-loop, this would run forever.
                return chunk

        with patch.object(trakt_router, "MAX_TOTAL_SIZE", 1024 * 1024):  # 1MB cap
            with self.assertRaises(HTTPException) as ctx:
                await trakt_router.trakt_import_upload(
                    background_tasks=SimpleNamespace(add_task=lambda *a, **k: None),
                    file=_FakeUploadFile(),
                    sync_watched=True,
                    sync_ratings=True,
                    sync_lists=True,
                    db=None,
                    current_user=SimpleNamespace(id=1),
                )

        self.assertEqual(ctx.exception.status_code, 413)
        # Stopped after the first chunk pushed it past the (mocked) 1MB cap —
        # not after buffering gigabytes.
        self.assertEqual(read_calls, 1)


class TraktExportSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_export_sync_runs_without_oauth_credentials_and_skips_cursor(self) -> None:
        # No trakt_access_token/client_id at all — the whole point of export
        # import is that it works without ever having connected via OAuth.
        settings = SimpleNamespace(
            trakt_access_token=None,
            trakt_client_id=None,
            trakt_client_secret=None,
            trakt_refresh_token=None,
            trakt_history_cursor_at=None,
            trakt_sync_watched=True,
            trakt_sync_ratings=False,
            trakt_sync_lists=False,
            trakt_watchlist_split=False,
            tmdb_api_key="tmdb-token",
        )
        session = _FakeSession(settings, [], [], [])
        export_data = TraktExportData(history_movies=[], history_episodes=[])
        fan_out = AsyncMock()

        with (
            patch.object(trakt_router, "async_sessionmaker", return_value=lambda: session),
            patch("routers.sync._fan_out_changes_to_other_connections", fan_out),
        ):
            await trakt_router.run_trakt_export_sync(user_id=1, job_id=30, export_data=export_data)

        # Reaching the fan-out call means the job completed successfully rather
        # than failing early (both except branches return before this point).
        fan_out.assert_awaited_once()
        # Export import is a full snapshot, not an incremental live pull — it
        # must never touch the live-sync cursor.
        self.assertIsNone(settings.trakt_history_cursor_at)

    async def test_export_sync_ignores_trakt_sync_preferences_and_uses_call_args(self) -> None:
        # Regression: what to import is chosen per-upload (the caller's
        # sync_watched/sync_ratings/sync_lists args, picked in the import
        # confirmation modal) — never read from trakt_sync_* preferences,
        # which only gate the continuous OAuth pull and default sync_lists
        # to False.
        settings = SimpleNamespace(
            trakt_history_cursor_at=None,
            trakt_sync_watched=False,
            trakt_sync_ratings=False,
            trakt_sync_lists=False,
            trakt_watchlist_split=False,
            tmdb_api_key="tmdb-token",
        )
        session = _FakeSession(settings, [], [], [])
        export_data = TraktExportData(history_movies=[], history_episodes=[])

        captured: dict = {}

        async def fake_apply(db, job_id, user_id, source, api_key, sync_watched, sync_ratings, sync_lists, split_watchlist, history_start, history_end):
            captured.update(sync_watched=sync_watched, sync_ratings=sync_ratings, sync_lists=sync_lists)
            return (
                {"movies": 0, "episodes": 0, "ratings": 0, "lists": 0, "list_items": 0, "skipped": 0, "errors": 0},
                0,
                False,
                set(),
                {},
            )

        with (
            patch.object(trakt_router, "async_sessionmaker", return_value=lambda: session),
            patch.object(trakt_router, "_apply_trakt_import", fake_apply),
            patch("routers.sync._fan_out_changes_to_other_connections", AsyncMock()),
        ):
            # Defaults (as when the endpoint isn't given explicit form values).
            await trakt_router.run_trakt_export_sync(user_id=1, job_id=31, export_data=export_data)
            self.assertEqual(captured, {"sync_watched": True, "sync_ratings": True, "sync_lists": True})

            # An explicit partial selection (e.g. the user unchecked "Lists" in
            # the import modal) must be honored exactly, not overridden.
            await trakt_router.run_trakt_export_sync(
                user_id=1, job_id=32, export_data=export_data,
                sync_watched=True, sync_ratings=False, sync_lists=False,
            )
            self.assertEqual(captured, {"sync_watched": True, "sync_ratings": False, "sync_lists": False})


class GetOrCreateEpisodeMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_none_and_creates_nothing_when_tmdb_lacks_the_episode(self) -> None:
        # Regression: importing watch history/ratings for an episode number a
        # provider (Plex/Trakt) has but TMDB doesn't (numbering mismatch) used
        # to fabricate a placeholder Media row with tmdb_id=None. That phantom
        # row would then surface in Next Up and 404 every time its page loaded.
        session = _FakeSession(settings=None, watch_rows=[], media=[], shows=[])
        season_data = {"episodes": [{"episode_number": 1, "id": 1, "name": "Ep 1"}]}

        with patch("core.tmdb.get_season", AsyncMock(return_value=season_data)):
            media = await trakt_router._get_or_create_episode_media(
                session, show_id=1, show_tmdb_id=999, season_number=3, episode_number=11, api_key=None,
            )

        self.assertIsNone(media)
        self.assertEqual(session.added, [])

    async def test_returns_media_when_tmdb_has_the_episode(self) -> None:
        session = _FakeSession(settings=None, watch_rows=[], media=[], shows=[])
        season_data = {"episodes": [{"episode_number": 11, "id": 555, "name": "The Real Episode 11"}]}

        with patch("core.tmdb.get_season", AsyncMock(return_value=season_data)):
            media = await trakt_router._get_or_create_episode_media(
                session, show_id=1, show_tmdb_id=999, season_number=3, episode_number=11, api_key=None,
            )

        self.assertIsNotNone(media)
        self.assertEqual(media.tmdb_id, 555)
        self.assertEqual(media.title, "The Real Episode 11")
        self.assertIn(media, session.added)


class ApplyTraktImportEpisodeRatingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_episode_rating_resolves_show_then_episode_media(self) -> None:
        show_stub = SimpleNamespace(id=42)
        media_stub = SimpleNamespace(id=99)
        episode_rating_item = {
            "rated_at": "2025-06-05T14:00:20.000Z",
            "rating": 7,
            "show": {"title": "The Chalet", "ids": {"tmdb": 78309}},
            "episode": {"season": 1, "number": 1, "ids": {"tmdb": 1460583}},
        }

        class _ExportSource:
            async def get_ratings(self):
                return {"movies": [], "shows": [], "seasons": [], "episodes": [episode_rating_item]}

        applied: list[tuple] = []

        def _fake_apply_rating(db, user_id, media, season_number, item, existing, changed):
            applied.append((media, season_number, item))
            return True

        with (
            patch.object(trakt_router, "_get_or_create_show", AsyncMock(return_value=show_stub)) as show_mock,
            patch.object(trakt_router, "_get_or_create_episode_media", AsyncMock(return_value=media_stub)) as media_mock,
            patch.object(trakt_router, "_apply_imported_rating", side_effect=_fake_apply_rating),
        ):
            stats, processed, had_errors, new_watched, new_ratings = await trakt_router._apply_trakt_import(
                db=_RatingsFakeDB(),
                job_id=1,
                user_id=7,
                source=_ExportSource(),
                api_key=None,
                sync_watched=False,
                sync_ratings=True,
                sync_lists=False,
                split_watchlist=False,
                history_start=None,
                history_end=datetime(2026, 1, 1),
            )

        show_mock.assert_awaited_once_with(ANY, 78309, "The Chalet", None)
        media_mock.assert_awaited_once_with(ANY, 42, 78309, 1, 1, None)
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0][0], media_stub)
        self.assertIsNone(applied[0][1])
        self.assertEqual(stats["ratings"], 1)
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(stats["skipped"], 0)


if __name__ == "__main__":
    unittest.main()

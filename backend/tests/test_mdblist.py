import json
import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core import mdblist
from models.base import MediaType
from models.media import Media
from routers.mdblist import (
    _episode_identity,
    _payload_item,
    _rating_removal_item,
    _resolve_external_tmdb_id,
    _season_identity,
)
from routers.lists import _push_list_item_to_mdblist


_REAL_ASYNC_CLIENT = httpx.AsyncClient


class MDBListClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_watched_follows_cursor_pagination(self) -> None:
        cursors: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/sync/watched")
            self.assertEqual(request.url.params["apikey"], "secret-key")
            self.assertEqual(request.url.params["limit"], "1000")
            cursor = request.url.params.get("cursor")
            cursors.append(cursor)
            if cursor is None:
                return httpx.Response(
                    200,
                    json={
                        "movies": [{"movie": {"ids": {"tmdb": 550}}}],
                        "pagination": {"next_cursor": "next-page"},
                    },
                )
            self.assertEqual(cursor, "next-page")
            return httpx.Response(
                200,
                json={
                    "shows": [{"show": {"ids": {"tmdb": 1396}}}],
                    "pagination": {"next_cursor": None},
                },
            )

        transport = httpx.MockTransport(handler)
        with patch.object(
            mdblist.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            result = await mdblist.get_watched("secret-key")

        self.assertEqual(cursors, [None, "next-page"])
        self.assertEqual(len(result["movies"]), 1)
        self.assertEqual(len(result["shows"]), 1)

    async def test_get_watchlist_falls_back_to_offset_pagination(self) -> None:
        offsets: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            offset = int(request.url.params.get("offset", 0))
            offsets.append(offset)
            if offset == 0:
                return httpx.Response(
                    200,
                    json={
                        "movies": [
                            {"movie": {"ids": {"tmdb": 1}}},
                            {"movie": {"ids": {"tmdb": 2}}},
                        ],
                        "pagination": {"has_more": True},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "movies": [{"movie": {"ids": {"tmdb": 3}}}],
                    "pagination": {"has_more": False},
                },
            )

        transport = httpx.MockTransport(handler)
        with patch.object(
            mdblist.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            result = await mdblist.get_watchlist("secret-key")

        self.assertEqual(offsets, [0, 2])
        self.assertEqual(len(result["movies"]), 3)

    async def test_push_watched_batches_each_media_type(self) -> None:
        calls: list[tuple[str, int]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/sync/watched")
            self.assertEqual(request.url.params["apikey"], "secret-key")
            payload = json.loads(request.content)
            self.assertEqual(len(payload), 1)
            key, values = next(iter(payload.items()))
            calls.append((key, len(values)))
            return httpx.Response(200, json={"added": {}, "not_found": {}})

        payload = {
            "movies": [{"ids": {"tmdb": value}} for value in (1, 2, 3)],
            "shows": [],
            "seasons": [],
            "episodes": [{"ids": {"tmdb": 4}}],
        }
        transport = httpx.MockTransport(handler)
        with (
            patch.object(mdblist, "PUSH_BATCH_SIZE", 2),
            patch.object(
                mdblist.httpx,
                "AsyncClient",
                side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
            ),
        ):
            result = await mdblist.push_watched("secret-key", payload)

        self.assertEqual(calls, [("movies", 2), ("movies", 1), ("episodes", 1)])
        self.assertEqual(result, {"submitted": 4, "batches": 3, "not_found": 0})



class MDBListListFanoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_managed_watchlist_edit_pushes_to_mdblist(self) -> None:
        result = MagicMock()
        result.scalar_one_or_none.return_value = SimpleNamespace(
            mdblist_push_watchlist=True,
            mdblist_api_key="secret-key",
        )
        db = AsyncMock()
        db.execute.return_value = result
        media = Media(id=1, tmdb_id=550, media_type=MediaType.movie, title="Fight Club")
        push_watchlist = AsyncMock()

        with patch.object(mdblist, "push_watchlist", push_watchlist):
            await _push_list_item_to_mdblist(
                db,
                user_id=1,
                list_mdblist_slug="__watchlist__",
                media=media,
            )

        push_watchlist.assert_awaited_once_with(
            "secret-key",
            {
                "movies": [{"ids": {"tmdb": 550}}],
                "shows": [],
                "seasons": [],
                "episodes": [],
            },
        )


class MDBListNormalizationTests(unittest.IsolatedAsyncioTestCase):
    def test_episode_identity_accepts_nested_show_shape(self) -> None:
        entry = {
            "episode": {"season": 3, "number": 2, "title": "Caballo sin Nombre"},
            "show": {"ids": {"tmdb": 1396}},
        }
        self.assertEqual(_episode_identity(entry), (1396, 3, 2, "Caballo sin Nombre"))

    async def test_show_imdb_id_resolves_to_tmdb_once(self) -> None:
        find = AsyncMock(return_value={"tv_results": [{"id": 1396}]})
        cache: dict[tuple[str, str], int | None] = {}
        with patch("core.tmdb.find_by_external_id", find):
            first = await _resolve_external_tmdb_id(
                {"ids": {"imdb": "tt0903747"}},
                "tv",
                "tmdb-token",
                cache,
            )
            second = await _resolve_external_tmdb_id(
                {"ids": {"imdb": "tt0903747"}},
                "tv",
                "tmdb-token",
                cache,
            )

        self.assertEqual((first, second), (1396, 1396))
        find.assert_awaited_once_with("tt0903747", "imdb_id", api_key="tmdb-token")

    def test_payload_item_uses_episode_tmdb_identifier(self) -> None:
        media = Media(
            id=1,
            tmdb_id=62085,
            media_type=MediaType.episode,
            title="Caballo sin Nombre",
            season_number=3,
            episode_number=2,
        )
        kind, item = _payload_item(media, watched_at=datetime(2026, 7, 17, 12, 0, 0))
        self.assertEqual(kind, "episodes")
        self.assertEqual(item["ids"], {"tmdb": 62085})
        self.assertEqual(item["watched_at"], "2026-07-17T12:00:00Z")

    def test_payload_item_preserves_rating_timestamp(self) -> None:
        media = Media(id=1, tmdb_id=550, media_type=MediaType.movie, title="Fight Club")
        kind, item = _payload_item(
            media,
            rating=8.0,
            rated_at=datetime(2026, 7, 17, 12, 0, 0),
        )
        self.assertEqual(kind, "movies")
        self.assertEqual(item["rating"], 8.0)
        self.assertEqual(item["rated_at"], "2026-07-17T12:00:00Z")

    def test_season_identity_uses_parent_show_and_season_number(self) -> None:
        entry = {
            "rated_at": "2026-07-18T00:00:00Z",
            "rating": 8,
            "season": {"number": 1, "ids": {"tmdb": 3572}},
            "show": {"title": "Breaking Bad", "ids": {"tmdb": 1396}},
        }

        show, season_number = _season_identity(entry)

        self.assertEqual(show["ids"]["tmdb"], 1396)
        self.assertEqual(season_number, 1)

    def test_payload_item_nests_season_under_parent_show(self) -> None:
        media = Media(
            id=1,
            tmdb_id=1396,
            media_type=MediaType.series,
            title="Breaking Bad",
        )

        kind, item = _payload_item(
            media,
            season_number=1,
            rating=8.0,
            rated_at=datetime(2026, 7, 18, 0, 0, 0, 123456),
        )

        self.assertEqual(kind, "shows")
        self.assertEqual(
            item,
            {
                "ids": {"tmdb": 1396},
                "seasons": [
                    {
                        "number": 1,
                        "rating": 8.0,
                        "rated_at": "2026-07-18T00:00:00Z",
                    }
                ],
            },
        )

    def test_rating_removal_nests_season_under_parent_show(self) -> None:
        media = Media(
            id=1,
            tmdb_id=1396,
            media_type=MediaType.series,
            title="Breaking Bad",
        )

        kind, item = _rating_removal_item(media, season_number=1)

        self.assertEqual(kind, "shows")
        self.assertEqual(
            item,
            {
                "ids": {"tmdb": 1396},
                "seasons": [{"number": 1}],
            },
        )


if __name__ == "__main__":
    unittest.main()

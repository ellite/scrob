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
from models.show import Show
from routers.mdblist import (
    _episode_identity,
    _merge_show_entries,
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

    def test_push_batch_size_does_not_exceed_mdblist_limit(self) -> None:
        # Regression test for #176: MDBList rejects any request with more
        # than 200 top-level entries ("Too many shows in one request (max
        # 200)") - PUSH_BATCH_SIZE was set to 500, so a push job for a
        # watched-list over 200 items failed outright instead of being
        # chunked, even though the batching mechanism itself was correct.
        self.assertLessEqual(mdblist.PUSH_BATCH_SIZE, 200)

    async def test_push_watched_chunks_large_show_list_within_mdblist_limit(self) -> None:
        # End-to-end with the real (unpatched) PUSH_BATCH_SIZE: a watched-list
        # of 450 shows must be split into batches MDBList would actually accept.
        batch_sizes: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            batch_sizes.append(len(payload["shows"]))
            return httpx.Response(200, json={"added": {}, "not_found": {}})

        payload = {
            "movies": [], "seasons": [], "episodes": [],
            "shows": [{"ids": {"tmdb": i}} for i in range(450)],
        }
        transport = httpx.MockTransport(handler)
        with patch.object(
            mdblist.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        ):
            result = await mdblist.push_watched("secret-key", payload)

        self.assertTrue(all(size <= 200 for size in batch_sizes), batch_sizes)
        self.assertEqual(sum(batch_sizes), 450)
        self.assertEqual(result["submitted"], 450)



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

    def test_payload_item_nests_episode_under_parent_show(self) -> None:
        """Regression test: an episode's own TMDB id is a completely different
        ID namespace from shows/movies. Sending it as a standalone "episodes"
        entry (the old behavior) resolves to an unrelated, wrong item on
        MDBList. Episodes must be identified via the parent show's ids plus
        season/episode numbers, nested under "shows"."""
        media = Media(
            id=1,
            tmdb_id=62085,
            media_type=MediaType.episode,
            title="Caballo sin Nombre",
            season_number=3,
            episode_number=2,
        )
        show = Show(id=10, tmdb_id=1396, title="Breaking Bad")
        kind, item = _payload_item(media, show=show, watched_at=datetime(2026, 7, 17, 12, 0, 0))
        self.assertEqual(kind, "shows")
        self.assertEqual(
            item,
            {
                "ids": {"tmdb": 1396},
                "seasons": [
                    {
                        "number": 3,
                        "episodes": [
                            {"number": 2, "watched_at": "2026-07-17T12:00:00Z"},
                        ],
                    }
                ],
            },
        )

    def test_payload_item_drops_episode_without_parent_show(self) -> None:
        media = Media(
            id=1,
            tmdb_id=62085,
            media_type=MediaType.episode,
            title="Caballo sin Nombre",
            season_number=3,
            episode_number=2,
        )
        self.assertIsNone(_payload_item(media, watched_at=datetime(2026, 7, 17, 12, 0, 0)))
        self.assertIsNone(
            _payload_item(media, show=Show(id=10, title="Breaking Bad"), watched_at=datetime(2026, 7, 17, 12, 0, 0))
        )

    def test_merge_show_entries_combines_multiple_episodes_of_same_season(self) -> None:
        show = Show(id=10, tmdb_id=1396, title="Breaking Bad")
        media_ep1 = Media(id=1, media_type=MediaType.episode, season_number=2, episode_number=1)
        media_ep2 = Media(id=2, media_type=MediaType.episode, season_number=2, episode_number=2)

        _, ep1_item = _payload_item(media_ep1, show=show, watched_at=datetime(2026, 7, 17, 12, 0, 0))
        _, ep2_item = _payload_item(media_ep2, show=show, watched_at=datetime(2026, 7, 17, 13, 0, 0))

        merged = _merge_show_entries([ep1_item, ep2_item])

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["ids"], {"tmdb": 1396})
        self.assertEqual(len(merged[0]["seasons"]), 1)
        self.assertEqual(merged[0]["seasons"][0]["number"], 2)
        self.assertEqual(
            merged[0]["seasons"][0]["episodes"],
            [
                {"number": 1, "watched_at": "2026-07-17T12:00:00Z"},
                {"number": 2, "watched_at": "2026-07-17T13:00:00Z"},
            ],
        )

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

    def test_merge_show_entries_combines_multiple_seasons_of_same_show(self) -> None:
        """Regression test: two season ratings for one show must round-trip
        as a single show object with both seasons nested, not two separate
        entries sharing the same ids.tmdb."""
        _, season_one = _payload_item(
            Media(id=1, tmdb_id=1396, media_type=MediaType.series, title="Breaking Bad"),
            season_number=1,
            rating=8.0,
            rated_at=datetime(2026, 7, 18, 0, 0, 0),
        )
        _, season_two = _payload_item(
            Media(id=1, tmdb_id=1396, media_type=MediaType.series, title="Breaking Bad"),
            season_number=2,
            rating=9.0,
            rated_at=datetime(2026, 7, 18, 0, 0, 0),
        )

        merged = _merge_show_entries([season_one, season_two])

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["ids"], {"tmdb": 1396})
        self.assertEqual(
            merged[0]["seasons"],
            [
                {"number": 1, "rating": 8.0, "rated_at": "2026-07-18T00:00:00Z"},
                {"number": 2, "rating": 9.0, "rated_at": "2026-07-18T00:00:00Z"},
            ],
        )

    def test_merge_show_entries_keeps_different_shows_separate(self) -> None:
        _, breaking_bad = _payload_item(
            Media(id=1, tmdb_id=1396, media_type=MediaType.series, title="Breaking Bad"),
            season_number=1,
            rating=8.0,
        )
        _, the_wire = _payload_item(
            Media(id=2, tmdb_id=1438, media_type=MediaType.series, title="The Wire"),
            season_number=1,
            rating=10.0,
        )

        merged = _merge_show_entries([breaking_bad, the_wire])

        self.assertEqual(len(merged), 2)
        self.assertEqual({item["ids"]["tmdb"] for item in merged}, {1396, 1438})

    def test_merge_show_entries_combines_show_rating_with_season_removal(self) -> None:
        """A show-level rating and a season removal for the same show must
        merge into one object rather than clobbering each other."""
        show_item = {"ids": {"tmdb": 1396}, "rating": 9.0, "rated_at": "2026-07-18T00:00:00Z"}
        season_removal = {"ids": {"tmdb": 1396}, "seasons": [{"number": 1}]}

        merged = _merge_show_entries([show_item, season_removal])

        self.assertEqual(len(merged), 1)
        self.assertEqual(
            merged[0],
            {
                "ids": {"tmdb": 1396},
                "rating": 9.0,
                "rated_at": "2026-07-18T00:00:00Z",
                "seasons": [{"number": 1}],
            },
        )


class _WatchedFakeSession:
    """Fakes just enough of AsyncSession for _import_watched: an empty
    existing-watch-events query, plus recording every WatchEvent added.
    Also backs record_rewatch_progress's own lookups (always empty here,
    so it no-ops - this test isn't exercising rewatch behavior)."""

    def __init__(self) -> None:
        self.added: list = []

    async def execute(self, statement):
        return SimpleNamespace(all=lambda: [], scalar_one_or_none=lambda: None)

    def begin_nested(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


class _WatchedFakeSessionWithHistory(_WatchedFakeSession):
    """Like _WatchedFakeSession, but the first execute() (the existing-watch-
    events query) returns pre-seeded (media_id, watched_at) rows instead of
    an empty result, so the dedup-window logic has something to compare
    against. Every later execute() (record_rewatch_progress's own lookups)
    still no-ops."""

    def __init__(self, existing_rows: list) -> None:
        super().__init__()
        self._existing_rows = existing_rows
        self._call_count = 0

    async def execute(self, statement):
        self._call_count += 1
        if self._call_count == 1:
            return SimpleNamespace(all=lambda: self._existing_rows, scalar_one_or_none=lambda: None)
        return SimpleNamespace(all=lambda: [], scalar_one_or_none=lambda: None)


class ImportWatchedDedupWindowTests(unittest.IsolatedAsyncioTestCase):
    async def test_watch_within_window_of_existing_is_skipped(self) -> None:
        """Regression test for #148: MDBList (and a source it round-tripped
        through, e.g. a push to a media server) doesn't always agree on the
        exact watched_at for what's really the same play. A new watch
        reported within WATCH_DEDUP_WINDOW of one we already have for the
        title must not create a second WatchEvent."""
        from routers.mdblist import _import_watched

        async def fake_resolve_media(db, kind, entry, api_key, external_cache):
            return SimpleNamespace(id=1)

        payload = {
            "movies": [{"ids": {"tmdb": 100}, "watched_at": "2026-08-01T12:00:00Z"}],
            "episodes": [],
        }
        stats = {"watched": 0, "skipped": 0, "errors": 0}
        # Existing completed watch for media 1 four minutes before the incoming one.
        db = _WatchedFakeSessionWithHistory([(1, datetime(2026, 8, 1, 11, 56, 0))])

        with patch("routers.mdblist._resolve_media", side_effect=fake_resolve_media):
            changed = await _import_watched(
                db, user_id=35, payload=payload, api_key=None, external_cache={}, stats=stats
            )

        self.assertEqual(db.added, [])
        self.assertEqual(stats["watched"], 0)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(changed, set())

    async def test_watch_outside_window_of_existing_is_recorded(self) -> None:
        """A watch reported more than WATCH_DEDUP_WINDOW away from the last
        known one for the title is a genuine rewatch and must still be
        recorded - including a same-day rewatch a couple hours later (e.g.
        watching a movie twice in a row to make sense of it)."""
        from routers.mdblist import _import_watched

        async def fake_resolve_media(db, kind, entry, api_key, external_cache):
            return SimpleNamespace(id=1)

        payload = {
            "movies": [{"ids": {"tmdb": 100}, "watched_at": "2026-08-01T14:00:00Z"}],
            "episodes": [],
        }
        stats = {"watched": 0, "skipped": 0, "errors": 0}
        # Existing completed watch for media 1 two hours before the incoming one.
        db = _WatchedFakeSessionWithHistory([(1, datetime(2026, 8, 1, 12, 0, 0))])

        with patch("routers.mdblist._resolve_media", side_effect=fake_resolve_media):
            changed = await _import_watched(
                db, user_id=35, payload=payload, api_key=None, external_cache={}, stats=stats
            )

        self.assertEqual({obj.media_id for obj in db.added}, {1})
        self.assertEqual(stats["watched"], 1)
        self.assertEqual(stats["skipped"], 0)
        self.assertEqual(changed, {1})

    async def test_null_dated_existing_watch_does_not_crash_the_window_check(self) -> None:
        """A title can already have a completed WatchEvent with no date at
        all (explicit "mark watched without a date" - see
        UnknownWatchDateTests in test_history.py). The window comparison
        must not blow up comparing a real timestamp against that None, and
        a null-dated existing watch can't be used to dedup against (there's
        no timestamp to compare), so the new watch is still recorded."""
        from routers.mdblist import _import_watched

        async def fake_resolve_media(db, kind, entry, api_key, external_cache):
            return SimpleNamespace(id=1)

        payload = {
            "movies": [{"ids": {"tmdb": 100}, "watched_at": "2026-08-01T14:00:00Z"}],
            "episodes": [],
        }
        stats = {"watched": 0, "skipped": 0, "errors": 0}
        db = _WatchedFakeSessionWithHistory([(1, None)])

        with patch("routers.mdblist._resolve_media", side_effect=fake_resolve_media):
            changed = await _import_watched(
                db, user_id=35, payload=payload, api_key=None, external_cache={}, stats=stats
            )

        self.assertEqual(stats["errors"], 0)
        self.assertEqual({obj.media_id for obj in db.added}, {1})
        self.assertEqual(stats["watched"], 1)
        self.assertEqual(changed, {1})


class ImportWatchedSkipsShowRollupTests(unittest.IsolatedAsyncioTestCase):
    async def test_shows_entries_are_not_imported_as_watch_events(self) -> None:
        """Regression test: MDBList's /sync/watched "shows" entries are rollup
        wrappers whose watched_at just mirrors the show's most recently
        watched episode — they carry no per-episode data of their own.
        Importing them as standalone watch events created a bogus
        series-level WatchEvent for every watched show, alongside the real
        episode-level one, and could collide with an unrelated movie that
        happens to share the same TMDB id (movies and shows are separate
        TMDB id namespaces)."""
        from routers.mdblist import _import_watched

        seen_kinds: list[str] = []

        async def fake_resolve_media(db, kind, entry, api_key, external_cache):
            seen_kinds.append(kind)
            if kind == "movies":
                return SimpleNamespace(id=1)
            if kind == "episodes":
                return SimpleNamespace(id=2)
            return SimpleNamespace(id=999)  # would only happen on regression

        payload = {
            "movies": [{"ids": {"tmdb": 100}, "watched_at": "2026-08-01T00:00:00Z"}],
            "shows": [
                {"ids": {"tmdb": 32726}, "last_watched_at": "2026-08-01T17:37:45Z"}
            ],
            "episodes": [
                {
                    "episode": {"season": 12, "number": 1},
                    "show": {"ids": {"tmdb": 32726}},
                    "last_watched_at": "2026-08-01T17:37:45Z",
                }
            ],
        }
        stats = {"watched": 0, "skipped": 0, "errors": 0}
        db = _WatchedFakeSession()

        with patch("routers.mdblist._resolve_media", side_effect=fake_resolve_media):
            changed = await _import_watched(
                db, user_id=35, payload=payload, api_key=None, external_cache={}, stats=stats
            )

        self.assertEqual(seen_kinds, ["movies", "episodes"])
        self.assertEqual({obj.media_id for obj in db.added}, {1, 2})
        self.assertEqual(stats["watched"], 2)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(changed, {1, 2})


if __name__ == "__main__":
    unittest.main()

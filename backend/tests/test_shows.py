import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from fastapi import HTTPException
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from models.base import MediaType
from routers import shows


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):
    """JSONB is PostgreSQL-only and has no SQLite rendering of its own."""
    return "JSON"


class FindLocalShowForTvdbTests(unittest.IsolatedAsyncioTestCase):
    """Regression tests: `ShowModel.tmdb_id == series_tmdb_id` compiles to
    `tmdb_id IS NULL` whenever series_tmdb_id is None (TVDB's tmdb_id_cross
    is absent for most TVDB-only shows), so OR-ing it in unconditionally
    matched every other TVDB-only show in the library instead of nothing -
    MultipleResultsFound (500) the moment a second one existed. Runs against
    a real SQLite DB since the bug was in the WHERE clause itself."""

    async def asyncSetUp(self):
        import models  # noqa: F401 - registers every table on Base.metadata
        from models.base import Base

        self.engine = create_async_engine(
            "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.addAsyncCleanup(self.engine.dispose)

    async def test_multiple_tmdb_null_shows_does_not_raise(self):
        from models.show import Show

        async with self.Session() as db:
            db.add_all([
                Show(title="Show A", tvdb_id=111, tmdb_id=None),
                Show(title="Show B", tvdb_id=222, tmdb_id=None),
                Show(title="Formula 1", tvdb_id=387219, tmdb_id=None),
            ])
            await db.commit()

            show = await shows._find_local_show_for_tvdb(db, 387219, None)
            self.assertIsNotNone(show)
            self.assertEqual(show.title, "Formula 1")

    async def test_no_match_returns_none(self):
        async with self.Session() as db:
            show = await shows._find_local_show_for_tvdb(db, 999999, None)
            self.assertIsNone(show)

    async def test_matches_by_cross_linked_tmdb_id_when_present(self):
        from models.show import Show

        async with self.Session() as db:
            db.add(Show(title="Cross-linked", tvdb_id=555, tmdb_id=777))
            await db.commit()

            # Looked up by a different tvdb_id, but the same tmdb_id the
            # caller resolved from TVDB's own tmdb_id_cross field.
            show = await shows._find_local_show_for_tvdb(db, 999, 777)
            self.assertIsNotNone(show)
            self.assertEqual(show.title, "Cross-linked")


class ResolveTvdbShowTests(FindLocalShowForTvdbTests):
    """#585: TVDB cross-links a TMDB id whose entry was deleted. TMDB answers
    404, which used to bubble up as a 500 on the TVDB show/season/episode page."""

    SHOW_DATA = {"title": "Gone From TMDB", "overview": "o", "seasons": [], "genres": []}

    @staticmethod
    def _http_error(status):
        import httpx

        req = httpx.Request("GET", "https://api.themoviedb.org/3/tv/1")
        return httpx.HTTPStatusError("err", request=req, response=httpx.Response(status, request=req))

    async def test_dead_tmdb_entry_falls_back_to_tvdb_only_show(self):
        with patch("routers.webhooks._find_or_create_show", AsyncMock(side_effect=self._http_error(404))):
            async with self.Session() as db:
                show, tmdb_id = await shows._resolve_tvdb_show(db, 4242, self.SHOW_DATA, 999, "key")
                self.assertIsNone(tmdb_id)
                self.assertIsNone(show.tmdb_id)
                self.assertEqual(show.tvdb_id, 4242)
                self.assertEqual(show.canonical_source, "tvdb")
                self.assertEqual(show.title, "Gone From TMDB")

    async def test_other_tmdb_http_errors_still_surface(self):
        import httpx

        with patch("routers.webhooks._find_or_create_show", AsyncMock(side_effect=self._http_error(401))):
            async with self.Session() as db:
                with self.assertRaises(httpx.HTTPStatusError):
                    await shows._resolve_tvdb_show(db, 4242, self.SHOW_DATA, 999, "key")

    async def test_live_tmdb_entry_is_used_unchanged(self):
        sentinel = SimpleNamespace(tmdb_id=999)
        with patch("routers.webhooks._find_or_create_show", AsyncMock(return_value=sentinel)):
            async with self.Session() as db:
                show, tmdb_id = await shows._resolve_tvdb_show(db, 4242, self.SHOW_DATA, 999, "key")
                self.assertIs(show, sentinel)
                self.assertEqual(tmdb_id, 999)

    async def test_existing_local_show_is_reused_without_asking_tmdb(self):
        from models.show import Show

        with patch("routers.webhooks._find_or_create_show", AsyncMock(side_effect=AssertionError("no TMDB call"))):
            async with self.Session() as db:
                db.add(Show(title="Local", tvdb_id=4242, tmdb_id=None))
                await db.commit()
                show, tmdb_id = await shows._resolve_tvdb_show(db, 4242, self.SHOW_DATA, 999, "key")
                self.assertEqual(show.title, "Local")
                self.assertIsNone(tmdb_id)

    async def test_existing_local_show_with_tmdb_id_keeps_it(self):
        from models.show import Show

        with patch("routers.webhooks._find_or_create_show", AsyncMock(side_effect=AssertionError("no TMDB call"))):
            async with self.Session() as db:
                db.add(Show(title="Local", tvdb_id=4242, tmdb_id=999))
                await db.commit()
                show, tmdb_id = await shows._resolve_tvdb_show(db, 4242, self.SHOW_DATA, 999, "key")
                self.assertEqual(tmdb_id, 999)


class GetTvdbShowDeadTmdbCrossLinkTests(FindLocalShowForTvdbTests):
    """#585 endpoint level: TVDB 387219 (Formula 1) style payload cross-links a
    deleted TMDB id; TMDB answers 404. The response must carry null tmdb_id and
    tmdb_id_cross (so the frontend neither redirects nor 404s), and a repeat
    visit that finds the TVDB-only local show must not resurrect the dead id."""

    TVDB_ID = 387219
    DEAD_TMDB_ID = 999999

    async def _call(self):
        import httpx

        req = httpx.Request("GET", "https://api.themoviedb.org/3/tv/999999")
        dead = httpx.HTTPStatusError("404", request=req, response=httpx.Response(404, request=req))
        show_data = {
            "title": "Formula 1", "overview": "o", "seasons": [], "genres": [],
            "tmdb_id_cross": self.DEAD_TMDB_ID, "imdb_id": None,
        }
        with patch.object(shows, "get_user_tvdb_key", AsyncMock(return_value="tvdb-key")), \
             patch.object(shows, "get_user_tmdb_key", AsyncMock(return_value="tmdb-key")), \
             patch.object(shows, "get_user_metadata_language", AsyncMock(return_value="en")), \
             patch.object(shows.tvdb_client, "get_series", AsyncMock(return_value={})), \
             patch.object(shows.tvdb_client, "format_series", MagicMock(return_value=dict(show_data))), \
             patch.object(shows.tvdb_client, "format_cast", MagicMock(return_value=[])), \
             patch.object(shows.tvdb_client, "get_series_episodes", AsyncMock(return_value=[])), \
             patch.object(shows, "_enrich_tvdb_seasons", AsyncMock(return_value=([], {}))), \
             patch("routers.webhooks._find_or_create_show", AsyncMock(side_effect=dead)):
            async with self.Session() as db:
                return await shows.get_tvdb_show(self.TVDB_ID, db=db, current_user=SimpleNamespace(id=1))

    async def test_dead_tmdb_cross_link_yields_null_tmdb_fields(self):
        data = await self._call()
        self.assertIsNone(data["tmdb_id"])
        self.assertIsNone(data["tmdb_id_cross"])
        self.assertIsNotNone(data["id"])

    async def test_second_visit_keeps_single_show_and_null_tmdb_fields(self):
        first = await self._call()
        second = await self._call()
        self.assertEqual(first["id"], second["id"])
        self.assertIsNone(second["tmdb_id"])
        self.assertIsNone(second["tmdb_id_cross"])


class GetTvdbShowDeadTmdbHttpRouteTests(FindLocalShowForTvdbTests):
    """#585 over the real HTTP route (GET /shows/tvdb/{id}) with the real
    router, dependency chain, TVDB remoteIds parsing and TMDB client. Only the
    network edge is simulated: TVDB's series payload cross-links TMDB id 999999
    and api.themoviedb.org answers a genuine HTTP 404 for it."""

    TVDB_ID = 387219
    DEAD_TMDB_ID = 999999

    async def test_route_returns_null_tmdb_fields_when_cross_link_is_dead(self):
        import httpx
        from fastapi import FastAPI

        from core import tmdb as tmdb_client
        from db import get_db
        from dependencies import get_optional_user_or_api_key

        tmdb_urls: list[str] = []

        def tmdb_handler(request: httpx.Request) -> httpx.Response:
            tmdb_urls.append(str(request.url))
            return httpx.Response(404, json={"success": False, "status_code": 34}, request=request)

        raw_series = {
            "id": self.TVDB_ID, "name": "Formula 1", "overview": "o", "seasons": [],
            "remoteIds": [
                {"id": "tt0000001", "sourceName": "IMDB"},
                {"id": str(self.DEAD_TMDB_ID), "sourceName": "TheMovieDB.com"},
            ],
        }

        app = FastAPI()
        app.include_router(shows.router, prefix="/shows")

        async def _db():
            async with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[get_optional_user_or_api_key] = lambda: SimpleNamespace(id=1)

        tmdb_http = httpx.AsyncClient(transport=httpx.MockTransport(tmdb_handler))
        self.addAsyncCleanup(tmdb_http.aclose)
        with patch.object(tmdb_client, "_client", tmdb_http), \
             patch.object(shows, "get_user_tvdb_key", AsyncMock(return_value="tvdb-key")), \
             patch.object(shows, "get_user_tmdb_key", AsyncMock(return_value="tmdb-key")), \
             patch.object(shows, "get_user_metadata_language", AsyncMock(return_value="en")), \
             patch.object(shows.tvdb_client, "get_series", AsyncMock(return_value=raw_series)), \
             patch.object(shows.tvdb_client, "get_series_episodes", AsyncMock(return_value=[])):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                ids = []
                for _ in range(2):
                    r = await client.get(f"/shows/tvdb/{self.TVDB_ID}")
                    self.assertEqual(r.status_code, 200, r.text)
                    body = r.json()
                    self.assertIsNone(body["tmdb_id"])
                    self.assertIsNone(body["tmdb_id_cross"])
                    ids.append(body["id"])
                self.assertEqual(ids[0], ids[1])
        self.assertTrue(any(f"/tv/{self.DEAD_TMDB_ID}" in u for u in tmdb_urls), tmdb_urls)


class _Scalars:
    def __init__(self, item=None):
        self.item = item

    def first(self):
        if isinstance(self.item, list):
            return self.item[0] if self.item else None
        return self.item

    def all(self):
        if isinstance(self.item, list):
            return self.item
        return [] if self.item is None else [self.item]


class _Result:
    def __init__(self, item=None):
        self.item = item

    def scalars(self):
        return _Scalars(self.item)

    def scalar_one_or_none(self):
        return self.item


class _FakeSession:
    """Queues results for db.execute() in call order - mirrors the pattern
    already established in tests/test_history.py."""

    def __init__(self, results):
        self.execute = AsyncMock(side_effect=[_Result(item) for item in results])


class _NestedTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSessionWithNesting(_FakeSession):
    """Adds begin_nested()/flush()/commit() support for tests that exercise
    apply_media_change_safely - mirrors tests/test_history.py's _FakeSession."""

    def __init__(self, results):
        super().__init__(results)
        self.flush = AsyncMock()
        self.commit = AsyncMock()

    def begin_nested(self):
        return _NestedTxn()


class GetEpisodeDetailTvdbFallbackMappingTests(unittest.IsolatedAsyncioTestCase):
    """Regression tests for #186's follow-up: when TMDB doesn't have an
    episode and the show has a TVDB match, get_episode_detail must translate
    the URL's TMDB-style season/episode into the show's real TVDB position
    via EpisodeOrderMapping before delegating to get_tvdb_episode - reusing
    the same numbers unchanged returns a DIFFERENT, wrong episode's data
    instead of a clear error (reported as "the fix made it worse")."""

    def _user(self):
        return SimpleNamespace(id=7)

    async def test_mapped_episode_translates_to_real_tvdb_position(self) -> None:
        show = SimpleNamespace(tmdb_id=980001, tvdb_id=980002)
        mapping = SimpleNamespace(tvdb_season_number=4, tvdb_episode_number=12)
        # Query order: 1) show lookup, 2) episode-order preference (none),
        # 3) EpisodeOrderMapping lookup.
        db = _FakeSession([show, None, mapping])

        tvdb_calls = []

        async def fake_get_tvdb_episode(tvdb_id, season_number, episode_number, db_arg, user_arg):
            tvdb_calls.append((tvdb_id, season_number, episode_number))
            return {"title": "the real episode"}

        with patch("routers.shows.get_user_tmdb_key", AsyncMock(return_value="key")), \
             patch("routers.shows.check_tmdb_key", lambda k: True), \
             patch("routers.shows.get_user_metadata_language", AsyncMock(return_value=None)), \
             patch("routers.shows.tmdb.get_episode", AsyncMock(side_effect=Exception("404 Not Found"))), \
             patch("routers.shows.get_tvdb_episode", fake_get_tvdb_episode):
            result = await shows.get_episode_detail(980001, 1, 1, db, self._user())

        self.assertEqual(tvdb_calls, [(980002, 4, 12)])
        self.assertEqual(result["title"], "the real episode")

    async def test_unmapped_episode_raises_instead_of_guessing(self) -> None:
        show = SimpleNamespace(tmdb_id=980001, tvdb_id=980002)
        # Query order: 1) show lookup, 2) episode-order preference (none),
        # 3) EpisodeOrderMapping lookup (none found).
        db = _FakeSession([show, None, None])

        with patch("routers.shows.get_user_tmdb_key", AsyncMock(return_value="key")), \
             patch("routers.shows.check_tmdb_key", lambda k: True), \
             patch("routers.shows.get_user_metadata_language", AsyncMock(return_value=None)), \
             patch("routers.shows.tmdb.get_episode", AsyncMock(side_effect=Exception("404 Not Found"))), \
             patch("routers.shows.get_tvdb_episode", AsyncMock()) as tvdb_mock:
            with self.assertRaises(HTTPException) as ctx:
                await shows.get_episode_detail(980001, 9, 9, db, self._user())

        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("TVDB episode mapping", ctx.exception.detail)
        tvdb_mock.assert_not_awaited()


class RefreshShowMetadataTvdbFallbackCorruptionTests(unittest.IsolatedAsyncioTestCase):
    """Regression tests for #186's second follow-up: refresh_show_metadata's
    TVDB fallback has the same "reuse possibly-TMDB-canonical numbers as TVDB
    query keys" flaw as get_episode_detail's did, except here it PERSISTS the
    wrong episode's data instead of just displaying it. A temporary TMDB
    failure for one season (unrelated to real TVDB/TMDB divergence) must not
    silently overwrite an already-correct, TMDB-sourced episode."""

    def _show(self):
        return SimpleNamespace(id=1, tmdb_id=980001, tvdb_id=980002, title="Show", canonical_source="tmdb")

    def _tmdb_show_data(self):
        return {"name": "Show", "seasons": [{"season_number": 1, "episode_count": 10, "name": "Season 1"}]}

    async def test_tmdb_sourced_episode_is_not_overwritten_by_coincidental_tvdb_match(self) -> None:
        show = self._show()
        good_ep = SimpleNamespace(
            id=101, media_type=MediaType.episode, season_number=1, episode_number=1, show_id=None,
            title="Correct Existing Title", overview="Correct overview",
            tmdb_id=555555, tmdb_data={"runtime": 42, "cast": []},  # NOT tvdb-sourced
        )
        # Query order: 1) show, 2) linked episodes, 3) orphans.
        db = _FakeSessionWithNesting([show, [good_ep], []])

        wrong_tvdb_ep = {"id": 999999, "seasonNumber": 1, "number": 1, "name": "WRONG Episode"}

        with patch("routers.shows.get_user_tmdb_key", AsyncMock(return_value="key")), \
             patch("routers.shows.check_tmdb_key", lambda k: True), \
             patch("routers.shows.tmdb.get_show", AsyncMock(return_value=self._tmdb_show_data())), \
             patch("routers.shows.tmdb.get_season", AsyncMock(side_effect=Exception("temporary TMDB failure"))), \
             patch("routers.shows.get_user_tvdb_key", AsyncMock(return_value="tvdb-key")), \
             patch("routers.shows.get_user_metadata_language", AsyncMock(return_value=None)), \
             patch("routers.shows.tvdb_client.get_series_episodes", AsyncMock(return_value=[wrong_tvdb_ep])), \
             patch("routers.shows.refresh_technical_data", AsyncMock()):
            await shows.refresh_show_metadata(980001, db, SimpleNamespace(id=7))

        self.assertEqual(good_ep.title, "Correct Existing Title")
        self.assertEqual(good_ep.tmdb_id, 555555)

    async def test_tvdb_sourced_episode_still_refreshes_normally(self) -> None:
        show = self._show()
        tvdb_ep = SimpleNamespace(
            id=102, media_type=MediaType.episode, season_number=1, episode_number=2, show_id=show.id,
            title="Stale Title", overview="stale", tmdb_id=None, tvdb_id=888888,
            tmdb_data={"runtime": 20, "tvdb_episode_id": 888888, "source": "tvdb"},
        )
        db = _FakeSessionWithNesting([show, [tvdb_ep], []])

        raw_tvdb_ep = {
            "id": 888888, "seasonNumber": 1, "number": 2, "name": "Refreshed Title",
            "overview": "refreshed", "aired": "2020-01-01", "runtime": 25, "image": None,
        }

        with patch("routers.shows.get_user_tmdb_key", AsyncMock(return_value="key")), \
             patch("routers.shows.check_tmdb_key", lambda k: True), \
             patch("routers.shows.tmdb.get_show", AsyncMock(return_value=self._tmdb_show_data())), \
             patch("routers.shows.tmdb.get_season", AsyncMock(side_effect=Exception("temporary TMDB failure"))), \
             patch("routers.shows.get_user_tvdb_key", AsyncMock(return_value="tvdb-key")), \
             patch("routers.shows.get_user_metadata_language", AsyncMock(return_value=None)), \
             patch("routers.shows.tvdb_client.get_series_episodes", AsyncMock(return_value=[raw_tvdb_ep])), \
             patch("routers.shows.refresh_technical_data", AsyncMock()):
            await shows.refresh_show_metadata(980001, db, SimpleNamespace(id=7))

        self.assertEqual(tvdb_ep.title, "Refreshed Title")


class RemapShowSeasonsTests(unittest.TestCase):
    """#174: _remap_show_seasons rewrites a show payload's season structures
    into a non-aired ordering."""

    def _pos(self, ds, de, cs, ce, eid):
        return SimpleNamespace(
            display_season=ds, display_episode=de,
            tmdb_season_number=cs, tmdb_episode_number=ce, tmdb_episode_id=eid,
        )

    def test_regroups_and_renumbers_episodes_and_meta(self):
        by_canonical = {
            (1, 1): self._pos(1, 1, 1, 1, 10),
            (1, 2): self._pos(1, 3, 1, 2, 11),  # aired S1E2 -> display S1E3
            (2, 1): self._pos(1, 2, 2, 1, 20),  # aired S2E1 -> display S1E2
        }
        payload = {
            "poster_path": "/p.jpg",
            "seasons": {
                "season_1": [
                    {"tmdb_id": 10, "season_number": 1, "episode_number": 1, "title": "A"},
                    {"tmdb_id": 11, "season_number": 1, "episode_number": 2, "title": "B"},
                ],
                "season_2": [
                    {"tmdb_id": 20, "season_number": 2, "episode_number": 1, "title": "C"},
                ],
            },
        }
        shows._remap_show_seasons(payload, by_canonical)

        s1 = payload["seasons"]["season_1"]
        self.assertEqual([(e["episode_number"], e["title"]) for e in s1], [(1, "A"), (2, "C"), (3, "B")])
        self.assertEqual(s1[1]["canonical_season_number"], 2)
        self.assertEqual(s1[1]["canonical_episode_number"], 1)
        self.assertEqual(payload["seasons_meta"], [
            {"season_number": 1, "name": "Season 1", "overview": None,
             "poster_path": "/p.jpg", "episode_count": 3, "air_date": None},
        ])

    def test_drops_episodes_the_order_does_not_place(self):
        by_canonical = {(1, 1): self._pos(1, 1, 1, 1, 10)}
        payload = {"seasons": {"season_1": [
            {"tmdb_id": 10, "season_number": 1, "episode_number": 1},
            {"tmdb_id": 99, "season_number": 1, "episode_number": 9},
        ]}}
        shows._remap_show_seasons(payload, by_canonical)
        self.assertEqual(len(payload["seasons"]["season_1"]), 1)


if __name__ == "__main__":
    unittest.main()


class TvdbSeasonArtTests(unittest.IsolatedAsyncioTestCase):
    """Seasons under a tvdb:* ordering take TheTVDB's own season posters and
    names instead of the show poster (reported on Reacher). Type-specific
    entries (DVD, absolute, ...) fall back per field to the official season
    with the same number, since TVDB rarely fills those in."""

    _RAW = {"seasons": [
        {"type": {"id": 1, "type": "official"}, "number": 1, "image": "/banners/s1.jpg", "name": "Killing Floor", "premiereDate": "2022-02-03"},
        {"type": {"id": 1, "type": "official"}, "number": 2, "image": "/banners/s2.jpg", "name": "Bad Luck and Trouble", "premiereDate": "2023-12-15"},
        {"type": {"id": 2, "type": "dvd"}, "number": 1, "image": "/banners/dvd1.jpg", "name": None, "premiereDate": None},
        {"type": {"id": 2, "type": "dvd"}, "number": 2, "image": None, "name": None, "premiereDate": None},
        {"type": {"id": 77, "type": "alternate"}, "number": 1, "image": None, "name": "Arc One", "premiereDate": None},
    ]}

    async def _art(self, order_key, key="tvdb-key", tvdb_id=366924):
        with patch("routers.shows.get_user_tvdb_key", AsyncMock(return_value=key)), \
             patch("routers.shows.tvdb_client.get_series", AsyncMock(return_value=self._RAW)):
            return await shows._tvdb_season_art(MagicMock(), 7, order_key, tvdb_id)

    async def test_official_order_uses_official_art(self):
        art = await self._art("tvdb:official")
        self.assertEqual(art[1]["poster_path"], "https://artworks.thetvdb.com/banners/s1.jpg")
        self.assertEqual(art[2]["name"], "Bad Luck and Trouble")
        self.assertEqual(art[1]["air_date"], "2022-02-03")

    async def test_typed_order_prefers_its_own_art_then_official(self):
        art = await self._art("tvdb:dvd")
        self.assertEqual(art[1]["poster_path"], "https://artworks.thetvdb.com/banners/dvd1.jpg")
        self.assertEqual(art[1]["name"], "Killing Floor")  # DVD S1 has no name -> official
        self.assertEqual(art[2]["poster_path"], "https://artworks.thetvdb.com/banners/s2.jpg")  # DVD S2 no image -> official

    async def test_custom_type_id_matches_by_id(self):
        art = await self._art("tvdb:type:77")
        self.assertEqual(art[1]["name"], "Arc One")
        self.assertEqual(art[1]["poster_path"], "https://artworks.thetvdb.com/banners/s1.jpg")

    async def test_empty_without_key_or_id_or_for_tmdb_orders(self):
        self.assertEqual(await self._art("tvdb:dvd", key=None), {})
        self.assertEqual(await self._art("tvdb:dvd", tvdb_id=None), {})
        self.assertEqual(await self._art("tmdb:group:abc"), {})

    def test_remap_overlays_season_art_on_meta_and_fallback_posters(self):
        pos = SimpleNamespace(display_season=1, display_episode=1, tmdb_season_number=1, tmdb_episode_number=1, tmdb_episode_id=10)
        payload = {
            "poster_path": "/show.jpg",
            "seasons": {"season_1": [
                {"tmdb_id": 10, "season_number": 1, "episode_number": 1, "title": "A", "poster_path": "/show.jpg"},
            ]},
        }
        art = {1: {"poster_path": "https://artworks.thetvdb.com/banners/s1.jpg", "name": "Killing Floor", "air_date": "2022-02-03"}}
        shows._remap_show_seasons(payload, {(1, 1): pos}, art)
        self.assertEqual(payload["seasons_meta"], [{
            "season_number": 1, "name": "Killing Floor", "overview": None,
            "poster_path": "https://artworks.thetvdb.com/banners/s1.jpg", "episode_count": 1, "air_date": "2022-02-03",
        }])
        # An episode that was only showing the show poster now shows its season's.
        self.assertEqual(payload["seasons"]["season_1"][0]["poster_path"], "https://artworks.thetvdb.com/banners/s1.jpg")


class SeasonNavigationOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_season_detail_uses_display_seasons_for_navigation(self):
        show = SimpleNamespace(id=1, tmdb_id=312415, tvdb_id=369316)
        db = AsyncMock()
        empty = MagicMock()
        empty.scalars.return_value.all.return_value = []
        empty.all.return_value = []
        empty.scalar_one_or_none.return_value = None
        found = MagicMock()
        found.scalar_one_or_none.return_value = show
        db.execute.side_effect = [found] + [empty] * 20
        positions = {
            (1, 1): SimpleNamespace(display_season=6, display_episode=1),
            (2, 1): SimpleNamespace(display_season=7, display_episode=1),
        }
        with (
            patch.object(shows, 'get_episode_order', AsyncMock(return_value=SimpleNamespace(episode_order='tvdb:official', tvdb_id=369316))),
            patch.object(shows, 'get_user_tmdb_key', AsyncMock(return_value='key')),
            patch.object(shows, 'get_user_metadata_language', AsyncMock(return_value='en')),
            patch.object(shows, 'check_tmdb_key', return_value=True),
            patch.object(shows, '_tvdb_season_art', AsyncMock(return_value={6: {'name': 'Series F'}})),
            patch.object(shows, '_resolve_ordered_season', AsyncMock(return_value=({'episodes': []}, set()))),
            patch.object(shows, 'get_position_maps', AsyncMock(return_value=(positions, {}))),
            patch.object(shows, 'format_show', return_value={'seasons_meta': [{'season_number': 1}, {'season_number': 2}]}),
            patch.object(shows, 'get_watch_stats', AsyncMock(return_value=SimpleNamespace(plays=0, watch_time_minutes=0, last_watched_at=None))),
            patch.object(shows, 'enrich_with_state', AsyncMock()),
        ):
            payload = await shows.get_show_season(312415, 6, db, SimpleNamespace(id=7))
        self.assertEqual([s['season_number'] for s in payload['show']['seasons_meta']], [6, 7])
        self.assertEqual(payload['show']['seasons_meta'][0]['name'], 'Series F')

    async def test_nonlocal_season_uses_ordered_payload_and_navigation(self):
        db = AsyncMock()
        empty = MagicMock()
        empty.scalars.return_value.all.return_value = []
        empty.all.return_value = []
        empty.scalar_one_or_none.return_value = None
        found = MagicMock()
        found.scalar_one_or_none.return_value = None
        db.execute.side_effect = [found] + [empty] * 20
        positions = {
            (1, 1): SimpleNamespace(display_season=6, display_episode=1),
            (2, 1): SimpleNamespace(display_season=7, display_episode=1),
        }
        with (
            patch.object(shows, 'get_episode_order', AsyncMock(return_value=SimpleNamespace(episode_order='tvdb:official', tvdb_id=None))),
            patch.object(shows, 'get_user_tmdb_key', AsyncMock(return_value='key')),
            patch.object(shows, 'get_user_metadata_language', AsyncMock(return_value='en')),
            patch.object(shows, 'check_tmdb_key', return_value=True),
            patch.object(shows, '_tvdb_season_art', AsyncMock(return_value={6: {'name': 'Series F'}})) as season_art,
            patch.object(shows, '_resolve_ordered_season', AsyncMock(return_value=({'name': 'Series F', 'episodes': []}, set()))),
            patch.object(shows, 'get_position_maps', AsyncMock(return_value=(positions, {}))),
            patch.object(shows.tmdb, 'get_show', AsyncMock(return_value={'name': 'QI XL', 'external_ids': {'tvdb_id': 369316}})),
            patch.object(shows.tmdb, 'get_season', AsyncMock(side_effect=AssertionError('must not fetch canonical display season'))),
            patch.object(shows, 'get_watch_stats', AsyncMock(return_value=SimpleNamespace(plays=0, watch_time_minutes=0, last_watched_at=None))),
            patch.object(shows, 'enrich_with_state', AsyncMock()),
        ):
            payload = await shows.get_show_season(312415, 6, db, SimpleNamespace(id=7))
        self.assertEqual([s['season_number'] for s in payload['show']['seasons_meta']], [6, 7])
        self.assertEqual(payload['show']['seasons_meta'][0]['name'], 'Series F')
        self.assertIsNone(payload['show']['id'])
        self.assertEqual(payload['name'], 'Series F')
        season_art.assert_awaited_once_with(db, 7, 'tvdb:official', 369316)

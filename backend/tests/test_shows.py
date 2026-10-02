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
        return SimpleNamespace(id=1, tmdb_id=980001, tvdb_id=980002, title="Show")

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

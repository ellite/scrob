"""#453: Simkl indexes episodes by TVDB's season layout. Hunter x Hunter's TMDB
S2E91 is TVDB S2E33, and sending TMDB's numbers made Simkl reject the watch
(confirmed against the live API: TMDB coordinates 404 / not_found even with
use_tvdb_anime_seasons, TVDB coordinates resolve)."""
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

import httpx
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from core import simkl
from core.episode_order import to_simkl_positions
from models.base import Base, MediaType
from models.episode_order import EpisodeOrderMapping
from routers import webhooks

_REAL_ASYNC_CLIENT = httpx.AsyncClient
HXH = 46298


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


class _MappedDb(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import models  # noqa: F401

        self.engine = create_async_engine(
            "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.addAsyncCleanup(self.engine.dispose)
        async with self.Session() as db:
            db.add_all([
                EpisodeOrderMapping(
                    series_tmdb_id=HXH, tmdb_season_number=2, tmdb_episode_number=91, tmdb_episode_id=908683,
                    tvdb_id=1, tvdb_season_number=2, tvdb_episode_number=33,
                ),
                EpisodeOrderMapping(
                    series_tmdb_id=HXH, tmdb_season_number=2, tmdb_episode_number=79, tmdb_episode_id=908671,
                    tvdb_id=2, tvdb_season_number=2, tvdb_episode_number=21,
                ),
            ])
            await db.commit()


class ToSimklPositionsTests(_MappedDb):
    async def test_mapped_episodes_get_their_tvdb_position(self):
        async with self.Session() as db:
            out = await to_simkl_positions(db, [(HXH, 2, 91), (HXH, 2, 79)])
        self.assertEqual(out, [(HXH, 2, 33), (HXH, 2, 21)])

    async def test_unmapped_episode_and_unmapped_show_pass_through(self):
        async with self.Session() as db:
            out = await to_simkl_positions(db, [(HXH, 1, 5), (777, 3, 4)])
        self.assertEqual(out, [(HXH, 1, 5), (777, 3, 4)])

    async def test_order_and_length_are_preserved_for_a_mixed_batch(self):
        async with self.Session() as db:
            out = await to_simkl_positions(db, [(777, 1, 1), (HXH, 2, 91), (HXH, 9, 9), (HXH, 2, 91)])
        self.assertEqual(out, [(777, 1, 1), (HXH, 2, 33), (HXH, 9, 9), (HXH, 2, 33)])

    async def test_empty_input(self):
        async with self.Session() as db:
            self.assertEqual(await to_simkl_positions(db, []), [])


class HistoryPayloadTests(unittest.IsolatedAsyncioTestCase):
    def _capture(self):
        sent: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(json.loads(request.content))
            return httpx.Response(201, json={"added": {"episodes": 1}, "not_found": {"shows": [], "episodes": []}})

        transport = httpx.MockTransport(handler)
        patcher = patch.object(
            simkl.httpx, "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=transport, **kwargs),
        )
        return sent, patcher

    async def test_add_episode_asks_simkl_to_use_tvdb_anime_seasons(self):
        sent, patcher = self._capture()
        with patcher:
            await simkl.add_episode_to_history("cid", "tok", HXH, 2, 33)
        show = sent[0]["shows"][0]
        self.assertTrue(show["use_tvdb_anime_seasons"])
        self.assertEqual(show["ids"], {"tmdb": HXH})
        self.assertEqual(show["seasons"], [{"number": 2, "episodes": [{"number": 33}]}])

    async def test_remove_episode_asks_simkl_to_use_tvdb_anime_seasons(self):
        sent, patcher = self._capture()
        with patcher:
            await simkl.remove_episode_from_history("cid", "tok", HXH, 2, 33)
        self.assertTrue(sent[0]["shows"][0]["use_tvdb_anime_seasons"])

    async def test_batch_sets_the_flag_on_every_show(self):
        sent, patcher = self._capture()
        with patcher:
            await simkl.add_history_batch("cid", "tok", [], [(HXH, 2, 33, None), (95479, 1, 25, None)])
        self.assertTrue(all(s["use_tvdb_anime_seasons"] for s in sent[0]["shows"]))
        self.assertEqual(len(sent[0]["shows"]), 2)


class ScrobbleUsesTvdbPositionTests(_MappedDb):
    def _settings(self):
        return SimpleNamespace(simkl_scrobble=True, simkl_access_token="tok", simkl_client_id="cid")

    def _episode(self, season=2, episode=91):
        return SimpleNamespace(
            media_type=MediaType.episode, season_number=season, episode_number=episode, tmdb_id=908683, show_id=1,
            show=SimpleNamespace(tmdb_id=HXH, title="Hunter x Hunter"),
        )

    async def test_checkin_and_stop_send_the_tvdb_numbers(self):
        with (
            patch.object(webhooks.simkl_client, "checkin_episode", AsyncMock()) as checkin,
            patch.object(webhooks.simkl_client, "stop_scrobble_episode", AsyncMock()) as stop,
        ):
            async with self.Session() as db:
                await webhooks._maybe_simkl_scrobble(self._settings(), self._episode(), "start", 0.0, db)
                await webhooks._maybe_simkl_scrobble(self._settings(), self._episode(), "stop", 1.0, db)
        self.assertEqual((checkin.await_args.kwargs["season_number"], checkin.await_args.kwargs["episode_number"]), (2, 33))
        self.assertEqual((stop.await_args.kwargs["season_number"], stop.await_args.kwargs["episode_number"]), (2, 33))

    async def test_history_fallback_uses_the_tvdb_numbers_too(self):
        with (
            patch.object(webhooks.simkl_client, "stop_scrobble_episode", AsyncMock(side_effect=RuntimeError("404"))),
            patch.object(webhooks.simkl_client, "add_episode_to_history", AsyncMock()) as add_hist,
        ):
            async with self.Session() as db:
                await webhooks._maybe_simkl_scrobble(self._settings(), self._episode(), "stop", 1.0, db)
        add_hist.assert_awaited_once_with("cid", "tok", HXH, 2, 33)

    async def test_unmapped_episode_keeps_its_own_numbers(self):
        with patch.object(webhooks.simkl_client, "stop_scrobble_episode", AsyncMock()) as stop:
            async with self.Session() as db:
                await webhooks._maybe_simkl_scrobble(self._settings(), self._episode(1, 5), "stop", 1.0, db)
        self.assertEqual((stop.await_args.kwargs["season_number"], stop.await_args.kwargs["episode_number"]), (1, 5))

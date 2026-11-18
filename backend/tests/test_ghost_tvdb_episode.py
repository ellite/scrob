"""#581: a TVDB-only "ghost" episode (Lost S6E18) on a TMDB-canonical show must
not be created by a Nuvio sync, must be pruned by Refresh Metadata, must not
surface in Next Up / show detail, and must not 301 to a 404."""
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from core.enrichment import unreferenced_media_ids
from models.base import Base, CollectionSource
from models.collection import Collection
from models.episode_order import EpisodeOrderMapping, UserShowEpisodeOrder
from models.events import WatchEvent
from models.media import Media, MediaType
from models.show import Show
from routers import history, shows, sync

TMDB_ID = 4607
USER = 1


class _DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        compiles(JSONB, "sqlite")(lambda type_, compiler, **kw: "JSON")
        self.engine = create_async_engine(
            "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.addAsyncCleanup(self.engine.dispose)

    async def _lost(self, db, canonical="tmdb"):
        show = Show(id=1, tmdb_id=TMDB_ID, tvdb_id=73739, title="Lost", canonical_source=canonical,
                    tmdb_data={"seasons": [{"season_number": 6, "episode_count": 17}]})
        db.add(show)
        db.add(Media(id=17, tmdb_id=1700, media_type=MediaType.episode, title="The End", show_id=1,
                     season_number=6, episode_number=17, release_date="2010-05-23"))
        db.add(Media(id=18, tvdb_id=999, media_type=MediaType.episode, title="The End (2)", show_id=1,
                     season_number=6, episode_number=18, release_date="2010-05-23"))
        await db.commit()
        return show


class UnreferencedMediaIdsTests(_DbCase):
    async def test_any_user_reference_keeps_the_row(self):
        async with self.Session() as db:
            await self._lost(db)
            self.assertEqual(await unreferenced_media_ids(db, [17, 18]), {17, 18})
            db.add(Collection(user_id=2, media_id=18, added_at=datetime(2026, 1, 1)))
            await db.commit()
            self.assertEqual(await unreferenced_media_ids(db, [17, 18]), {17})
            db.add(WatchEvent(user_id=3, media_id=17, watched_at=datetime(2026, 1, 1), completed=True))
            await db.commit()
            self.assertEqual(await unreferenced_media_ids(db, [17, 18]), set())


class RefreshPrunesGhostTests(_DbCase):
    async def _refresh(self, db, season_side_effect, canonical="tmdb"):
        await self._lost(db, canonical)
        season = {"episodes": [{"episode_number": 17, "id": 1700, "name": "The End"}]}
        get_season = AsyncMock(side_effect=season_side_effect) if season_side_effect else AsyncMock(return_value=season)
        data = {"name": "Lost", "seasons": [{"season_number": 6, "episode_count": 17, "name": "Season 6"}]}
        with patch("routers.shows.get_user_tmdb_key", AsyncMock(return_value="key")), \
             patch("routers.shows.check_tmdb_key", lambda k: True), \
             patch("routers.shows.tmdb.get_show", AsyncMock(return_value=data)), \
             patch("routers.shows.tmdb.get_season", get_season), \
             patch("routers.shows.get_user_tvdb_key", AsyncMock(return_value=None)), \
             patch("routers.shows.refresh_technical_data", AsyncMock()):
            await shows.refresh_show_metadata(TMDB_ID, db, SimpleNamespace(id=USER))
        return set((await db.execute(select(Media.id))).scalars())

    async def test_unreferenced_ghost_is_deleted(self):
        async with self.Session() as db:
            self.assertEqual(await self._refresh(db, None), {17})

    async def test_referenced_ghost_is_kept(self):
        async with self.Session() as db:
            db.add(WatchEvent(user_id=USER, media_id=18, watched_at=datetime(2026, 1, 1), completed=True))
            await db.commit()
            self.assertEqual(await self._refresh(db, None), {17, 18})

    async def test_transient_tmdb_failure_keeps_the_row(self):
        async with self.Session() as db:
            self.assertEqual(await self._refresh(db, Exception("TMDB down")), {17, 18})

    async def test_tvdb_canonical_show_is_untouched(self):
        async with self.Session() as db:
            self.assertEqual(await self._refresh(db, None, canonical="tvdb"), {17, 18})


class NextUpGhostTests(_DbCase):
    async def _candidates(self, db):
        res = await db.execute(select(Media).where(Media.id == 18))
        return res.scalars().all()

    async def test_ghost_after_tmdb_numbered_last_watch_is_dropped(self):
        async with self.Session() as db:
            await self._lost(db)
            db.add(WatchEvent(user_id=USER, media_id=17, watched_at=datetime(2026, 1, 1), completed=True))
            await db.commit()
            out = await history._drop_ghost_tvdb_candidates(db, USER, await self._candidates(db), {1: (6, 17)})
            self.assertEqual(out, [])

    async def test_collected_ghost_is_kept(self):
        async with self.Session() as db:
            await self._lost(db)
            db.add(WatchEvent(user_id=USER, media_id=17, watched_at=datetime(2026, 1, 1), completed=True))
            db.add(Collection(user_id=USER, media_id=18, added_at=datetime(2026, 1, 1)))
            await db.commit()
            out = await history._drop_ghost_tvdb_candidates(db, USER, await self._candidates(db), {1: (6, 17)})
            self.assertEqual([m.id for m in out], [18])

    async def test_one_piece_style_tvdb_sourced_last_watch_is_kept(self):
        async with self.Session() as db:
            await self._lost(db)
            db.add(Media(id=16, tvdb_id=998, media_type=MediaType.episode, title="x", show_id=1,
                         season_number=6, episode_number=17 - 1))
            db.add(WatchEvent(user_id=USER, media_id=16, watched_at=datetime(2026, 1, 1), completed=True))
            await db.commit()
            out = await history._drop_ghost_tvdb_candidates(db, USER, await self._candidates(db), {1: (6, 16)})
            self.assertEqual([m.id for m in out], [18])

    async def test_tvdb_canonical_show_is_kept(self):
        async with self.Session() as db:
            await self._lost(db, canonical="tvdb")
            db.add(WatchEvent(user_id=USER, media_id=17, watched_at=datetime(2026, 1, 1), completed=True))
            await db.commit()
            out = await history._drop_ghost_tvdb_candidates(db, USER, await self._candidates(db), {1: (6, 17)})
            self.assertEqual([m.id for m in out], [18])

    async def test_tvdb_order_is_kept(self):
        async with self.Session() as db:
            await self._lost(db)
            db.add(UserShowEpisodeOrder(user_id=USER, series_tmdb_id=TMDB_ID, episode_order="tvdb:official"))
            db.add(WatchEvent(user_id=USER, media_id=17, watched_at=datetime(2026, 1, 1), completed=True))
            await db.commit()
            out = await history._drop_ghost_tvdb_candidates(db, USER, await self._candidates(db), {1: (6, 17)})
            self.assertEqual([m.id for m in out], [18])


class ShowDetailGhostTests(_DbCase):
    async def _detail(self, db, order=None):
        await self._lost(db)
        if order:
            db.add(UserShowEpisodeOrder(user_id=USER, series_tmdb_id=TMDB_ID, episode_order=order))
            await db.commit()
        with patch("routers.shows.get_user_tmdb_key", AsyncMock(return_value=None)), \
             patch("routers.shows.check_tmdb_key", lambda k: False):
            return await shows.get_show(TMDB_ID, db, SimpleNamespace(id=USER))

    def _numbers(self, payload):
        return sorted(e["episode_number"] for eps in payload["seasons"].values() for e in eps)

    async def test_unreferenced_ghost_is_hidden_under_aired_order(self):
        async with self.Session() as db:
            self.assertEqual(self._numbers(await self._detail(db)), [17])

    async def test_ghost_is_shown_under_tvdb_order(self):
        async with self.Session() as db:
            self.assertIn(18, self._numbers(await self._detail(db, "tvdb:official")))


class NuvioSyncNoGhostTests(_DbCase):
    async def _enrich(self, db, prune, mapping=False):
        await self._lost(db)
        await db.execute(Media.__table__.delete().where(Media.id == 18))
        stub = Media(id=18, media_type=MediaType.episode, title="", show_id=1, season_number=6, episode_number=18)
        db.add(stub)
        if mapping:
            db.add(EpisodeOrderMapping(series_tmdb_id=TMDB_ID, tmdb_season_number=6, tmdb_episode_number=17,
                                       tmdb_episode_id=1700, tvdb_id=999, tvdb_season_number=6, tvdb_episode_number=18))
        await db.commit()
        season = {"episodes": [{"episode_number": 17, "id": 1700, "name": "The End"}]}
        tvdb_ep = {"id": 999, "seasonNumber": 6, "number": 18, "name": "The End (2)", "aired": "2010-05-23"}
        with patch("routers.sync.tmdb.get_season", AsyncMock(return_value=season)), \
             patch("routers.sync.tmdb.poster_url", lambda *a, **k: None), \
             patch("routers.shows.get_user_tvdb_key", AsyncMock(return_value="tvdb")), \
             patch("routers.sync.get_user_metadata_language", AsyncMock(return_value=None)), \
             patch("core.tvdb.get_series_episodes", AsyncMock(return_value=[tvdb_ep])):
            await sync.batch_enrich_items(db, [(stub, TMDB_ID)], api_key="k", user_id=USER, prune_absent=prune)
        await db.commit()
        return set((await db.execute(select(Media.id))).scalars())

    async def test_nuvio_sync_does_not_leave_a_ghost(self):
        async with self.Session() as db:
            self.assertEqual(await self._enrich(db, prune=True), {17})

    async def test_other_syncs_keep_the_tvdb_fallback(self):
        async with self.Session() as db:
            self.assertEqual(await self._enrich(db, prune=False), {17, 18})

    async def test_mapped_position_keeps_the_tvdb_fallback(self):
        async with self.Session() as db:
            self.assertEqual(await self._enrich(db, prune=True, mapping=True), {17, 18})


if __name__ == "__main__":
    unittest.main()

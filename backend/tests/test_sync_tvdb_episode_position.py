"""#447: end-to-end sync_items runs for a Jellyfin episode whose server-reported
numbering is TVDB's while the canonical row sits at the TMDB position.

The item is resolved through its episode-level TVDB id (EpisodeOrderMapping),
never through its raw season/episode numbers, so a sync neither recreates the
divergent twin nor records the watch a second time."""
import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from models.base import CollectionSource, MediaType
from models.collection import Collection, CollectionFile
from models.episode_order import EpisodeOrderMapping
from models.events import WatchEvent
from models.media import Media
from models.show import Show
from models.users import User
from routers import sync


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "JSON"


TVDB_EPISODE_ID = 4562021
SOURCE_ID = "jf-ep-1"


def _item(tvdb_id="4562021"):
    """What a TVDB-numbered Jellyfin reports for the episode (raw S02E21)."""
    return {
        "Id": SOURCE_ID,
        "SeriesId": "jf-show",
        "Name": "No x Good x NGL",
        "ParentIndexNumber": 2,
        "IndexNumber": 21,
        "ProviderIds": {"Tvdb": tvdb_id},
        "UserData": {"Played": True, "PlayCount": 1, "LastPlayedDate": "2026-09-30T20:59:36Z"},
    }


class SyncItemsTvdbEpisodePositionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import models  # noqa: F401
        from models.base import Base

        self.engine = create_async_engine(
            "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.addAsyncCleanup(self.engine.dispose)

        async with self.Session() as db:
            user = User(email="a@example.com", username="agent", api_key="k" * 32)
            show = Show(tmdb_id=46298, tvdb_id=252322, title="Hunter x Hunter")
            db.add_all([user, show])
            await db.flush()
            db.add(EpisodeOrderMapping(
                series_tmdb_id=46298,
                tmdb_season_number=2, tmdb_episode_number=79, tmdb_episode_id=908653,
                tvdb_id=TVDB_EPISODE_ID, tvdb_season_number=2, tvdb_episode_number=21,
            ))
            await db.commit()
            self.user_id, self.show_id = user.id, show.id

    async def _add_episode(self, db, *, season, episode, tmdb_id=None, tvdb_id=None, with_file=False, watched=False):
        media = Media(
            media_type=MediaType.episode, title="ep", show_id=self.show_id,
            season_number=season, episode_number=episode, tmdb_id=tmdb_id, tvdb_id=tvdb_id,
        )
        db.add(media)
        await db.flush()
        if with_file:
            coll = Collection(user_id=self.user_id, media_id=media.id)
            db.add(coll)
            await db.flush()
            db.add(CollectionFile(collection_id=coll.id, source=CollectionSource.jellyfin, source_id=SOURCE_ID))
        if watched:
            db.add(WatchEvent(user_id=self.user_id, media_id=media.id, completed=True, play_count=1))
        await db.flush()
        return media

    async def _run_sync(self, item=None):
        async with self.Session() as db:
            with patch.object(sync, "batch_enrich_items", AsyncMock(return_value=[])) as enrich:
                await sync.sync_items(
                    [item or _item()], MediaType.episode, CollectionSource.jellyfin, db,
                    {"errors": 0, "skipped": 0, "episodes": 0}, self.user_id,
                    show_map={"jf-show": self.show_id}, show_id_to_tmdb={self.show_id: 46298},
                )
        return enrich

    async def _state(self):
        async with self.Session() as db:
            rows = (await db.execute(
                select(Media.id, Media.season_number, Media.episode_number, Media.tmdb_id)
                .where(Media.show_id == self.show_id).order_by(Media.id)
            )).all()
            events = (await db.execute(select(func.count(WatchEvent.id)))).scalar()
            files = (await db.execute(select(func.count(CollectionFile.id)))).scalar()
            colls = (await db.execute(select(func.count(Collection.id)))).scalar()
        return [tuple(r) for r in rows], events, files, colls

    async def test_twin_without_a_canonical_row_is_moved_not_duplicated(self):
        async with self.Session() as db:
            twin = await self._add_episode(
                db, season=2, episode=21, tvdb_id=TVDB_EPISODE_ID, with_file=True, watched=True,
            )
            await db.commit()
            twin_id = twin.id

        enrich = await self._run_sync()

        rows, events, files, colls = await self._state()
        # The same row, now at the canonical TMDB position - nothing created.
        self.assertEqual([(r[0], r[1], r[2]) for r in rows], [(twin_id, 2, 79)])
        self.assertEqual((events, files, colls), (1, 1, 1))
        # ...and it is queued to pick up the TMDB data it never had.
        enrich.assert_awaited_once()
        queued = enrich.await_args.args[1]
        self.assertEqual([(m.id, stid) for m, stid in queued], [(twin_id, 46298)])

        # A second sync has nothing left to do: no twin, no second watch event.
        enrich = await self._run_sync()
        self.assertEqual(await self._state(), (rows, 1, 1, 1))
        # (Enrichment is mocked, so the row still looks unenriched and the
        # existing heal branch may queue it again - but never anything new.)
        if enrich.await_count:
            self.assertEqual([m.id for m, _ in enrich.await_args.args[1]], [twin_id])

    async def test_twin_beside_the_canonical_row_is_merged_into_it(self):
        async with self.Session() as db:
            canonical = await self._add_episode(db, season=2, episode=79, tmdb_id=908653)
            twin = await self._add_episode(
                db, season=2, episode=21, tvdb_id=TVDB_EPISODE_ID, with_file=True, watched=True,
            )
            await db.commit()
            canonical_id, twin_id = canonical.id, twin.id

        await self._run_sync()

        rows, events, files, colls = await self._state()
        self.assertEqual([r[0] for r in rows], [canonical_id])
        self.assertNotIn(twin_id, [r[0] for r in rows])
        # The watch and the file followed the merge; the sync added nothing of its own.
        self.assertEqual((events, files, colls), (1, 1, 1))
        async with self.Session() as db:
            moved = (await db.execute(select(WatchEvent.media_id))).scalars().all()
        self.assertEqual(moved, [canonical_id])

        await self._run_sync()
        self.assertEqual(await self._state(), (rows, 1, 1, 1))

    async def test_a_different_episode_at_the_raw_position_is_left_alone(self):
        # Same raw slot (S02E21), but the row is a genuinely different episode
        # (its own TVDB id): it must not be moved or merged into the mapped one.
        async with self.Session() as db:
            other = await self._add_episode(db, season=2, episode=21, tvdb_id=999, with_file=True, watched=True)
            await db.commit()
            other_id = other.id

        with patch.object(sync, "create_media_safely", AsyncMock(side_effect=RuntimeError("stop"))):
            await self._run_sync()

        rows, events, _, _ = await self._state()
        self.assertEqual([(r[0], r[1], r[2]) for r in rows], [(other_id, 2, 21)])
        self.assertEqual(events, 1)


if __name__ == "__main__":
    unittest.main()

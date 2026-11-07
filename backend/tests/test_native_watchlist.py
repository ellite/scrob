import os
import unittest

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from core import native_watchlist
from core.native_watchlist import (
    get_or_create_watchlist,
    load_watchlist_keys,
    sync_service_watchlist,
)
from models.base import MediaType
from models.lists import List as ListModel, ListItem
from models.media import Media


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


class NativeWatchlistTests(unittest.IsolatedAsyncioTestCase):
    """Real-SQLite coverage: one shared list, several services reconciling
    against it with their own baselines."""

    async def asyncSetUp(self) -> None:
        import models  # noqa: F401 - registers every table on Base.metadata
        from models.base import Base
        from models.users import User

        self.engine = create_async_engine(
            "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.addAsyncCleanup(self.engine.dispose)

        async with self.Session() as db:
            db.add(User(id=1, email="a@test.local", username="a", api_key="key-a", password_hash="x"))
            for media_id, tmdb_id, kind in ((1, 603, MediaType.movie), (2, 1399, MediaType.series), (3, 550, MediaType.movie)):
                db.add(Media(id=media_id, tmdb_id=tmdb_id, media_type=kind, title=f"t{tmdb_id}"))
            await db.commit()

    def _resolver(self, media_id):
        async def _resolve():
            async with self.Session() as db:
                return await db.get(Media, media_id)
        return _resolve

    async def _keys(self) -> set[str]:
        async with self.Session() as db:
            watchlist = await get_or_create_watchlist(db, 1)
            await db.commit()
            return set(await load_watchlist_keys(db, watchlist.id))

    async def _sync(self, remote, baseline, *, pull=True, push=False, pushed=None):
        async def _push_add(keys, _local):
            if pushed is not None:
                pushed.extend(keys)
            return set()

        async with self.Session() as db:
            result = await sync_service_watchlist(
                db, 1, service="test", remote=remote, baseline=baseline,
                pull_enabled=pull, push_enabled=push, push_add=_push_add,
            )
            await db.commit()
            return result

    async def test_get_or_create_is_idempotent(self) -> None:
        async with self.Session() as db:
            first = await get_or_create_watchlist(db, 1)
            second = await get_or_create_watchlist(db, 1)
            await db.commit()
            self.assertEqual(first.id, second.id)
            rows = (await db.execute(select(ListModel).where(ListModel.is_watchlist.is_(True)))).scalars().all()
            self.assertEqual(len(rows), 1)

    async def test_two_services_share_one_list_without_duplicates(self) -> None:
        both = {"movie:603": self._resolver(1)}
        await self._sync(both, None)
        await self._sync({**both, "show:1399": self._resolver(2)}, None)
        self.assertEqual(await self._keys(), {"movie:603", "show:1399"})
        async with self.Session() as db:
            count = len((await db.execute(select(ListItem))).scalars().all())
        self.assertEqual(count, 2)

    async def test_pull_only_service_does_not_remove_another_services_titles(self) -> None:
        # Plex brought movie:603; a pull-only Simkl that never had it must
        # leave it alone, which the old per-service mirror didn't have to.
        await self._sync({"movie:603": self._resolver(1)}, None)
        simkl = await self._sync({"show:1399": self._resolver(2)}, None)
        simkl = await self._sync({"show:1399": self._resolver(2)}, simkl.baseline)
        self.assertEqual(await self._keys(), {"movie:603", "show:1399"})
        self.assertEqual(simkl.baseline, ["show:1399"])

    async def test_pull_only_removal_on_the_service_propagates(self) -> None:
        first = await self._sync({"movie:603": self._resolver(1)}, None)
        result = await self._sync({}, first.baseline)
        self.assertEqual(result.removed, 1)
        self.assertEqual(await self._keys(), set())

    async def test_title_from_one_service_is_pushed_to_another(self) -> None:
        # The hub: Plex adds a title, Trakt (both directions) receives it.
        await self._sync({"movie:603": self._resolver(1)}, None)
        pushed: list[str] = []
        trakt = await self._sync({}, [], push=True, pushed=pushed)
        self.assertEqual(pushed, ["movie:603"])
        self.assertEqual(trakt.baseline, ["movie:603"])

    async def test_failed_import_stays_out_of_the_baseline(self) -> None:
        async def _broken():
            raise RuntimeError("tmdb down")

        result = await self._sync({"movie:550": _broken}, None)
        self.assertEqual(result.added, 0)
        self.assertEqual(result.baseline, [])
        retry = await self._sync({"movie:550": self._resolver(3)}, result.baseline)
        self.assertEqual(retry.added, 1)

    async def test_recreated_watchlist_drops_a_stale_baseline(self) -> None:
        # After "clear all lists" the old baseline must not read every
        # synced title as a local removal and wipe the service.
        async with self.Session() as db:
            result = await sync_service_watchlist(
                db, 1, service="test", remote={"movie:603": self._resolver(1)},
                baseline=["movie:603"], pull_enabled=True, push_enabled=True,
                push_remove=None,
            )
            await db.commit()
        self.assertEqual(result.pushed_removed, 0)
        self.assertEqual(result.added, 1)

    async def test_season_items_are_ignored(self) -> None:
        async with self.Session() as db:
            watchlist = await get_or_create_watchlist(db, 1)
            db.add(ListItem(list_id=watchlist.id, media_id=2, season_number=1))
            await db.commit()
        self.assertEqual(await self._keys(), set())


class KeyHelperTests(unittest.TestCase):
    def test_key_round_trip(self) -> None:
        key = native_watchlist.key_for_media(MediaType.series, 1399)
        self.assertEqual(key, "show:1399")
        self.assertEqual(native_watchlist.split_key(key), ("show", 1399))

    def test_episodes_and_missing_ids_have_no_key(self) -> None:
        self.assertIsNone(native_watchlist.key_for_media(MediaType.episode, 1))
        self.assertIsNone(native_watchlist.key_for_media(MediaType.movie, None))


if __name__ == "__main__":
    unittest.main()

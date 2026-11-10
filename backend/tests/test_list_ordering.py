import unittest
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from datetime import datetime

from fastapi import HTTPException
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from core.list_order import arranged
from models.base import Base
from models.lists import List as ListModel
from routers.lists import ListOrder, reorder_lists, reorder_list_items


@compiles(JSONB, "sqlite")
def _jsonb(type_, compiler, **kw):
    return "JSON"


def _db(rows, owner=None):
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    first = MagicMock()
    first.scalar_one_or_none.return_value = owner
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[first, result] if owner is not None else [result])
    db.commit = AsyncMock()
    return db


class ListOrderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_reorders_owned_lists_without_touching_updated_at(self):
        engine = create_async_engine("sqlite+aiosqlite://")
        self.addAsyncCleanup(engine.dispose)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        stamp = datetime(2020, 1, 1)
        async with Session() as db:
            db.add_all([ListModel(id=n, user_id=7, name=f"L{n}", updated_at=stamp,
                                  created_at=datetime(2020, 1, n)) for n in (1, 2, 3)])
            await db.commit()
        async with Session() as db:
            await reorder_lists(ListOrder(ids=[3, 1, 2]), db, SimpleNamespace(id=7))
        async with Session() as db:
            rows = (await db.execute(select(ListModel))).scalars().all()
        # Moving lists is no content change: public listings sort by updated_at.
        self.assertEqual({r.updated_at for r in rows}, {stamp})
        self.assertEqual([r.id for r in arranged(rows)], [3, 1, 2])

    def test_new_lists_follow_arranged_ones_oldest_first(self):
        rows = [SimpleNamespace(id=1, sort_order=-2, created_at=datetime(2020, 1, 1)),
                SimpleNamespace(id=5, sort_order=None, created_at=datetime(2024, 1, 2)),
                SimpleNamespace(id=4, sort_order=None, created_at=datetime(2024, 1, 1)),
                SimpleNamespace(id=2, sort_order=-1, created_at=datetime(2020, 1, 2))]
        self.assertEqual([r.id for r in arranged(rows)], [1, 2, 4, 5])

    def test_default_order_is_kept_until_the_user_arranges(self):
        rows = [SimpleNamespace(id=n, sort_order=None, created_at=datetime(2020, 1, n)) for n in (3, 1, 2)]
        self.assertEqual([r.id for r in arranged(rows)], [3, 1, 2])

    async def test_foreign_or_missing_lists_are_rejected_atomically(self):
        for ids in ([1, 9], [1], [1, 1]):
            rows = [SimpleNamespace(id=n, sort_order=8) for n in (1, 2)]
            db = _db(rows)
            with self.assertRaises(HTTPException) as cm:
                await reorder_lists(ListOrder(ids=ids), db, SimpleNamespace(id=7))
            self.assertEqual(cm.exception.status_code, 400)
            self.assertEqual([r.sort_order for r in rows], [8, 8])
            db.commit.assert_not_awaited()

    async def test_reorders_items_within_owned_list(self):
        rows = [SimpleNamespace(id=n, sort_order=0) for n in (11, 12)]
        db = _db(rows, SimpleNamespace(id=1, user_id=7))
        await reorder_list_items(1, ListOrder(ids=[12, 11]), db, SimpleNamespace(id=7))
        self.assertEqual([r.id for r in sorted(rows, key=lambda r: r.sort_order)], [12, 11])

    async def test_another_lists_item_cannot_be_injected(self):
        db = _db([SimpleNamespace(id=11, sort_order=0)], SimpleNamespace(id=1, user_id=7))
        with self.assertRaises(HTTPException) as cm:
            await reorder_list_items(1, ListOrder(ids=[99]), db, SimpleNamespace(id=7))
        self.assertEqual(cm.exception.status_code, 400)
        db.commit.assert_not_awaited()

    async def test_foreign_list_is_not_found(self):
        db = _db([])
        db.execute.return_value = MagicMock()
        db.execute.side_effect = None
        db.execute.return_value.scalar_one_or_none.return_value = None
        with self.assertRaises(HTTPException) as cm:
            await reorder_list_items(9, ListOrder(ids=[]), db, SimpleNamespace(id=7))
        self.assertEqual(cm.exception.status_code, 404)

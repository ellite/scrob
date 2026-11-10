import unittest
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import HTTPException
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
from routers.lists import ListOrder, reorder_lists, reorder_list_items


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
    async def test_reorders_all_owned_lists(self):
        rows = [SimpleNamespace(id=n, sort_order=None) for n in (1, 2, 3)]
        db = _db(rows)
        await reorder_lists(ListOrder(ids=[3, 1, 2]), db, SimpleNamespace(id=7))
        self.assertEqual([r.id for r in sorted(rows, key=lambda r: r.sort_order)], [3, 1, 2])
        db.commit.assert_awaited_once()

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

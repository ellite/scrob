import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from models.lists import List as ListModel
from routers import lists


class ClearAllListsTests(unittest.IsolatedAsyncioTestCase):
    async def test_deletes_every_list_owned_by_the_user(self) -> None:
        db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())

        response = await lists.clear_all_lists(db=db, current_user=SimpleNamespace(id=7))

        self.assertEqual(response["status"], "ok")
        db.execute.assert_awaited_once()
        stmt = db.execute.call_args.args[0]
        self.assertEqual(stmt.table.name, ListModel.__tablename__)
        db.commit.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

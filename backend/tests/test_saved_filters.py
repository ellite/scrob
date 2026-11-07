import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("SECRET_KEY", "test")

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from routers import saved_filters as sf


class SavedFilterValidationTests(unittest.TestCase):
    def test_name_is_trimmed_and_collapsed(self) -> None:
        b = sf.SavedFilterBody(scope="/movies", name="  No   horror ", params={"genre": ["Drama"]})
        self.assertEqual(b.name, "No horror")

    def test_blank_name_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            sf.SavedFilterBody(scope="/movies", name="   ", params={"genre": ["Drama"]})

    def test_long_name_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            sf.SavedFilterBody(scope="/movies", name="x" * 65, params={"genre": ["Drama"]})

    def test_empty_params_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            sf.SavedFilterBody(scope="/movies", name="a", params={"genre": []})

    def test_bad_key_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            sf.SavedFilterBody(scope="/movies", name="a", params={"Gen re!": ["x"]})

    def test_exclude_keys_kept_and_blank_values_dropped(self) -> None:
        b = sf.SavedFilterBody(scope="/shows", name="a", params={"genre": ["Drama", " "], "exclude_genre": ["Reality"]})
        self.assertEqual(b.params, {"genre": ["Drama"], "exclude_genre": ["Reality"]})


def _result(value=None, scalars=None, scalar_one=None):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    r.scalar_one.return_value = scalar_one
    r.scalars.return_value.all.return_value = scalars or []
    return r


class SavedFilterRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.user = SimpleNamespace(id=7)

    def _db(self, *results):
        db = MagicMock()
        db.execute = AsyncMock(side_effect=list(results))
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        db.refresh = AsyncMock()
        db.delete = AsyncMock()
        return db

    async def test_create_conflict_maps_to_409(self) -> None:
        db = self._db(_result(scalar_one=0))
        db.commit = AsyncMock(side_effect=IntegrityError("x", {}, Exception()))
        body = sf.SavedFilterBody(scope="/movies", name="a", params={"genre": ["Drama"]})
        with self.assertRaises(HTTPException) as cm:
            await sf.create_saved_filter(body, db, self.user)
        self.assertEqual(cm.exception.status_code, 409)
        db.rollback.assert_awaited()

    async def test_create_limit(self) -> None:
        db = self._db(_result(scalar_one=sf.MAX_PER_SCOPE))
        body = sf.SavedFilterBody(scope="/movies", name="a", params={"genre": ["Drama"]})
        with self.assertRaises(HTTPException) as cm:
            await sf.create_saved_filter(body, db, self.user)
        self.assertEqual(cm.exception.status_code, 400)

    async def test_overwrite_other_users_filter_is_404(self) -> None:
        db = self._db(_result(value=None))
        with self.assertRaises(HTTPException) as cm:
            await sf.overwrite_saved_filter(1, sf.SavedFilterOverwrite(params={"genre": ["Drama"]}), db, self.user)
        self.assertEqual(cm.exception.status_code, 404)

    async def test_overwrite_replaces_params_and_renames(self) -> None:
        row = SimpleNamespace(id=3, scope="/movies", name="old", params={"genre": ["Horror"]})
        db = self._db(_result(value=row))
        out = await sf.overwrite_saved_filter(
            3, sf.SavedFilterOverwrite(params={"exclude_genre": ["Horror"]}, name="new"), db, self.user)
        self.assertEqual(out, {"id": 3, "scope": "/movies", "name": "new", "params": {"exclude_genre": ["Horror"]}})

    async def test_delete_missing_is_404(self) -> None:
        db = self._db(_result(value=None))
        with self.assertRaises(HTTPException) as cm:
            await sf.delete_saved_filter(9, db, self.user)
        self.assertEqual(cm.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()

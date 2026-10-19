from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from routers.auth import _find_user_for_login


def _db(*results):
    """db.execute() returns the given results in order: first the exact-match
    lookup (scalar_one_or_none), then the case-insensitive one (scalars().all())."""
    exact, loose = results
    first = MagicMock()
    first.scalar_one_or_none.return_value = exact
    second = MagicMock()
    second.scalars.return_value.all.return_value = loose
    db = AsyncMock()
    db.execute.side_effect = [first, second]
    return db


@pytest.mark.asyncio
async def test_exact_match_wins_without_a_second_lookup():
    bob = SimpleNamespace(username="Bob")
    db = _db(bob, [])
    assert await _find_user_for_login(db, " Bob ") is bob
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_case_insensitive_fallback_when_unambiguous():
    bob = SimpleNamespace(username="Bob")
    assert await _find_user_for_login(_db(None, [bob]), "bob") is bob


@pytest.mark.asyncio
async def test_ambiguous_case_insensitive_match_finds_nobody():
    a, b = SimpleNamespace(username="Bob"), SimpleNamespace(username="BOB")
    assert await _find_user_for_login(_db(None, [a, b]), "bOb") is None

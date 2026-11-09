"""Accept-Language fallback for the Metadata Language (#538).

When a viewer has no Metadata Language set (anonymous visitors of a public
list/profile, or a logged-in user who never opened Settings), the browser's
Accept-Language header picks one of the supported languages instead of
silently defaulting to English.
"""
import os
from contextvars import copy_context
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.accept_language import (
    AcceptLanguageMiddleware,
    negotiate_metadata_language,
    request_accept_language,
)
from core.translations import get_user_metadata_language




@pytest.mark.parametrize(
    "header, expected",
    [
        (None, None),
        ("", None),
        ("*", None),
        # English is today's default: no fallback language means "no translation".
        ("en", None),
        ("en-US,en;q=0.9", None),
        ("en-GB,fr;q=0.8", None),
        # Exact match on a regional code the dropdown offers.
        ("pt-BR", "pt-BR"),
        ("pt-PT,pt;q=0.9", "pt-PT"),
        ("es-MX,es;q=0.9,en;q=0.8", "es-MX"),
        # Regional variant not offered falls back to its primary subtag.
        ("fr-FR,fr;q=0.9,en;q=0.8", "fr"),
        ("fr-CA", "fr"),
        ("es-AR", "es"),
        # Primary subtag only, but only regional variants are offered: first offered wins.
        ("pt", "pt-BR"),
        ("zh", "zh-CN"),
        # q-values decide the order, not the position in the header.
        ("de;q=0.3,fr;q=0.7", "fr"),
        ("de;q=0.3, fr ; q=0.7", "fr"),
        ("fr;q=0,de", "de"),
        # Case-insensitive.
        ("FR-fr", "fr"),
        ("PT-br", "pt-BR"),
        # Unknown languages are skipped, not treated as a match.
        ("xx-YY,fr;q=0.5", "fr"),
        ("xx-YY", None),
        # Garbage never raises.
        (",,;q=,fr;q=abc", "fr"),
        (";;;", None),
    ],
)
def test_negotiate_metadata_language(header, expected):
    assert negotiate_metadata_language(header) == expected


def _fake_db(profile_language):
    db = MagicMock()
    db.info = {}
    row = (profile_language,) if profile_language is not ... else None
    result = MagicMock()
    result.first.return_value = row
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_explicit_profile_setting_wins_over_the_browser():
    token = request_accept_language.set("fr-FR,fr;q=0.9")
    try:
        assert await get_user_metadata_language(_fake_db("de"), 5) == "de"
    finally:
        request_accept_language.reset(token)


@pytest.mark.asyncio
async def test_unset_profile_setting_falls_back_to_the_browser():
    token = request_accept_language.set("fr-FR,fr;q=0.9")
    try:
        assert await get_user_metadata_language(_fake_db(None), 5) == "fr"
        # A profile row with an empty string (the dropdown's default option) is "unset" too.
        assert await get_user_metadata_language(_fake_db(""), 6) == "fr"
        # No profile row at all (anonymous viewer / ANON_USER_ID).
        assert await get_user_metadata_language(_fake_db(...), -1) == "fr"
    finally:
        request_accept_language.reset(token)


@pytest.mark.asyncio
async def test_no_header_keeps_the_english_default():
    assert request_accept_language.get() is None
    assert await get_user_metadata_language(_fake_db(None), 5) is None


@pytest.mark.asyncio
async def test_profile_lookup_is_still_cached_per_session():
    db = _fake_db(None)
    token = request_accept_language.set("de")
    try:
        assert await get_user_metadata_language(db, 5) == "de"
        assert await get_user_metadata_language(db, 5) == "de"
    finally:
        request_accept_language.reset(token)
    assert db.execute.await_count == 1


def test_middleware_exposes_the_header_for_the_request_only():
    app = FastAPI()
    app.add_middleware(AcceptLanguageMiddleware)

    @app.get("/lang")
    async def lang():
        return {"header": request_accept_language.get()}

    client = TestClient(app)
    assert client.get("/lang", headers={"Accept-Language": "fr-FR,fr;q=0.9"}).json() == {"header": "fr-FR,fr;q=0.9"}
    assert client.get("/lang").json() == {"header": None}
    # Nothing leaks out of the request context.
    assert copy_context().run(request_accept_language.get) is None

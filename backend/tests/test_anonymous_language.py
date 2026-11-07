"""Public lists and profiles translate for anonymous viewers too (#538)."""
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

import pytest

from dependencies import ANON_USER_ID
from routers import profile


@pytest.mark.asyncio
async def test_anonymous_profile_viewer_gets_the_negotiated_language():
    rows = [({"media_type": "movie", "title": "Another Day"}, SimpleNamespace(id=1), None)]
    with patch.object(profile, "get_user_metadata_language", AsyncMock(return_value="fr")) as get_lang, \
         patch.object(profile, "get_media_translations", AsyncMock(return_value={1: {"title": "Garance"}})), \
         patch.object(profile, "get_show_translations", AsyncMock(return_value={})):
        await profile._apply_viewer_language(AsyncMock(), None, rows)

    assert rows[0][0]["title"] == "Garance"
    assert get_lang.await_args.args[1] == ANON_USER_ID

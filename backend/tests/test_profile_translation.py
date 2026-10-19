from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from routers import profile


@pytest.mark.asyncio
async def test_profile_listings_use_the_viewers_metadata_language():
    # #460: recently watched / top rated on a profile showed stored (English)
    # titles regardless of the viewer's Metadata Language.
    movie = SimpleNamespace(id=1)
    episode = SimpleNamespace(id=2)
    show = SimpleNamespace(id=7)
    rows = [
        ({"media_type": "movie", "title": "Bugonia"}, movie, None),
        ({"media_type": "episode", "title": "The Road", "show_title": "The Gentlemen"}, episode, show),
        ({"media_type": "series", "title": "FROM"}, SimpleNamespace(id=3), show),
    ]
    with patch.object(profile, "get_user_metadata_language", AsyncMock(return_value="ru")), \
         patch.object(profile, "get_media_translations", AsyncMock(return_value={1: {"title": "Бугония"}, 2: {"title": "Дорога"}})), \
         patch.object(profile, "get_show_translations", AsyncMock(return_value={7: {"title": "Джентльмены"}})):
        await profile._apply_viewer_language(AsyncMock(), SimpleNamespace(id=5), rows)

    assert rows[0][0]["title"] == "Бугония"
    assert rows[1][0]["title"] == "Дорога"
    assert rows[1][0]["show_title"] == "Джентльмены"
    assert rows[2][0]["title"] == "Джентльмены"


@pytest.mark.asyncio
async def test_anonymous_viewer_sees_stored_titles():
    rows = [({"media_type": "movie", "title": "Bugonia"}, SimpleNamespace(id=1), None)]
    await profile._apply_viewer_language(AsyncMock(), None, rows)
    assert rows[0][0]["title"] == "Bugonia"

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from models.base import MediaType
from models.media import Media
from routers import history
from schemas import WatchEventCreate


class _Scalars:
    def __init__(self, item=None):
        self.item = item

    def first(self):
        return self.item


class _Result:
    def __init__(self, item=None):
        self.item = item

    def scalars(self):
        return _Scalars(self.item)


class _FakeSession:
    def __init__(self, results):
        self.added = []
        self.execute = AsyncMock(side_effect=[_Result(item) for item in results])
        self.flush = AsyncMock()
        self.commit = AsyncMock()

    def add(self, value):
        if isinstance(value, Media) and value.id is None:
            value.id = 101
        self.added.append(value)


class ManualEpisodeWatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user = SimpleNamespace(id=7)
        self.show = SimpleNamespace(id=55)
        self.event = WatchEventCreate(
            tmdb_id=5767197,
            media_type=MediaType.episode,
            series_tmdb_id=277439,
            season_number=1,
            episode_number=1,
        )

    def _patch_dependencies(self):
        get_key = AsyncMock(return_value="tmdb-key")
        find_show = AsyncMock(return_value=self.show)
        get_episode = AsyncMock(return_value={"id": 5767197, "name": "Fingers & Toes"})
        enrich = AsyncMock()
        push_state = AsyncMock()
        patches = (
            patch("routers.media.get_user_tmdb_key", get_key),
            patch("routers.webhooks._find_or_create_show", find_show),
            patch("routers.history.tmdb.get_episode", get_episode),
            patch("routers.history.enrich_media", enrich),
            patch("routers.history._push_watch_state", push_state),
        )
        return patches, get_key, find_show, get_episode, enrich, push_state

    async def test_manual_episode_creates_parent_show_before_media(self):
        db = _FakeSession([None, None, None])
        patches, get_key, find_show, get_episode, enrich, push_state = self._patch_dependencies()

        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            response = await history.mark_as_watched(self.event, db, self.user)

        media = next(value for value in db.added if isinstance(value, Media))
        self.assertEqual(response["status"], "ok")
        self.assertEqual(media.show_id, self.show.id)
        self.assertEqual((media.season_number, media.episode_number), (1, 1))
        find_show.assert_awaited_once_with(db, 277439, "tmdb-key")
        get_episode.assert_awaited_once_with(277439, 1, 1, api_key="tmdb-key")
        enrich.assert_awaited_once_with(media, api_key="tmdb-key", series_tmdb_id=277439)
        push_state.assert_awaited_once_with(db, 7, [media.id], watched=True)
        get_key.assert_awaited_once()
        db.commit.assert_awaited_once()

    async def test_manual_episode_repairs_existing_orphan(self):
        orphan = Media(
            id=202,
            tmdb_id=5767197,
            media_type=MediaType.episode,
            title="Fingers & Toes",
            season_number=1,
            episode_number=1,
            show_id=None,
            poster_path=None,
        )
        db = _FakeSession([None, orphan, None])
        patches, _, _, get_episode, enrich, _ = self._patch_dependencies()

        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            await history.mark_as_watched(self.event, db, self.user)

        self.assertEqual(orphan.show_id, self.show.id)
        self.assertEqual((orphan.season_number, orphan.episode_number), (1, 1))
        get_episode.assert_not_awaited()
        enrich.assert_awaited_once_with(orphan, api_key="tmdb-key", series_tmdb_id=277439)

    async def test_tvdb_mapping_uses_canonical_show_position(self):
        mapped_media = Media(
            id=303,
            tmdb_id=None,
            media_type=MediaType.episode,
            title="TVDB-mapped episode",
            season_number=2,
            episode_number=3,
            show_id=self.show.id,
        )
        event = WatchEventCreate(
            tmdb_id=7654321,
            media_type=MediaType.episode,
            series_tmdb_id=277439,
            season_number=2,
            episode_number=3,
        )
        db = _FakeSession([mapped_media, None])
        patches, _, _, get_episode, enrich, push_state = self._patch_dependencies()

        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            response = await history.mark_as_watched(event, db, self.user)

        self.assertEqual(response["status"], "ok")
        self.assertEqual(mapped_media.id, 303)
        self.assertIsNone(mapped_media.tmdb_id)
        get_episode.assert_not_awaited()
        enrich.assert_not_awaited()
        push_state.assert_awaited_once_with(db, 7, [303], watched=True)


if __name__ == "__main__":
    unittest.main()

import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from models.base import MediaType
from models.events import WatchEvent
from models.media import Media
from models.show import Show
from routers import history
from schemas import WatchEventCreate


class _Scalars:
    def __init__(self, item=None):
        self.item = item

    def first(self):
        if isinstance(self.item, list):
            return self.item[0] if self.item else None
        return self.item

    def all(self):
        if isinstance(self.item, list):
            return self.item
        return [] if self.item is None else [self.item]


class _Result:
    def __init__(self, item=None):
        self.item = item

    def scalars(self):
        return _Scalars(self.item)

    def scalar_one_or_none(self):
        return self.item

    def all(self):
        if isinstance(self.item, list):
            return self.item
        return [] if self.item is None else [self.item]


class _FakeSession:
    def __init__(self, results):
        self.added = []
        self.info = {}
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
        push_state.assert_awaited_once()
        push_call = push_state.await_args
        self.assertEqual(push_call.args, (db, 7, [media.id]))
        self.assertEqual(push_call.kwargs["watched"], True)
        self.assertIn(media.id, push_call.kwargs["watched_at_by_media"])
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
        push_state.assert_awaited_once()
        push_call = push_state.await_args
        self.assertEqual(push_call.args, (db, 7, [303]))
        self.assertEqual(push_call.kwargs["watched"], True)
        self.assertIn(303, push_call.kwargs["watched_at_by_media"])


class UnknownWatchDateTests(unittest.IsolatedAsyncioTestCase):
    async def _mark(self, payload: dict):
        media = Media(id=10, tmdb_id=550, media_type=MediaType.movie, title="Fight Club")
        # Two execute() calls: the media lookup, then the PlaybackProgress delete.
        db = _FakeSession([media, None])
        with patch("routers.history._push_watch_state", new_callable=AsyncMock) as push:
            response = await history.mark_as_watched(
                WatchEventCreate(**payload), db, SimpleNamespace(id=7)
            )
        event = next(value for value in db.added if isinstance(value, WatchEvent))
        return response, event, push

    async def test_explicit_null_marks_watched_without_a_date(self) -> None:
        response, event, push = await self._mark({
            "tmdb_id": 550,
            "media_type": "movie",
            "watched_at": None,
        })

        self.assertEqual(response["status"], "ok")
        self.assertIsNone(event.watched_at)
        push.assert_awaited_once()
        self.assertEqual(push.await_args.kwargs["watched_at_by_media"], {10: None})

    async def test_omitted_watched_at_defaults_to_now(self) -> None:
        response, event, push = await self._mark({
            "tmdb_id": 550,
            "media_type": "movie",
        })

        self.assertEqual(response["status"], "ok")
        self.assertIsNotNone(event.watched_at)
        self.assertEqual(
            push.await_args.kwargs["watched_at_by_media"],
            {10: event.watched_at},
        )


_SEASON_PAYLOAD = {
    "episodes": [
        {"episode_number": 1, "id": 999, "name": "Ep 1", "air_date": "2020-01-01", "vote_average": 8.0, "still_path": None},
    ]
}


class MarkSeasonWatchedDateTests(unittest.IsolatedAsyncioTestCase):
    """Regression test for issue #92: marking a season watched had no way to
    pick a custom date or leave it unknown — every episode got watched_at=now()."""

    async def _mark_season(self, **watched_at_kwargs) -> tuple[dict, WatchEvent]:
        show = Show(id=55, tmdb_id=100, title="Test Show")
        # execute() call order: show lookup, existing-episode lookup,
        # already-watched lookup, PlaybackProgress delete.
        db = _FakeSession([show, [], [], None])
        db.info["tmdb_key_7"] = "test-key"  # pre-cache so get_user_tmdb_key skips its own query
        with (
            patch.object(history.tmdb, "get_season", AsyncMock(return_value=_SEASON_PAYLOAD)),
            patch("routers.history._push_watch_state", new_callable=AsyncMock),
        ):
            body = history.SeasonWatchRequest(series_tmdb_id=100, season_number=1, **watched_at_kwargs)
            response = await history.mark_season_watched(body, db, SimpleNamespace(id=7))
        event = next(v for v in db.added if isinstance(v, WatchEvent))
        return response, event

    async def test_explicit_null_marks_season_watched_without_a_date(self) -> None:
        response, event = await self._mark_season(watched_at=None)
        self.assertEqual(response["count"], 1)
        self.assertIsNone(event.watched_at)

    async def test_omitted_watched_at_defaults_to_now(self) -> None:
        response, event = await self._mark_season()
        self.assertEqual(response["count"], 1)
        self.assertIsNotNone(event.watched_at)

    async def test_explicit_custom_date_is_used(self) -> None:
        custom = datetime(2020, 6, 15, 12, 0, 0)
        response, event = await self._mark_season(watched_at=custom)
        self.assertEqual(response["count"], 1)
        self.assertEqual(event.watched_at, custom)


class MarkShowWatchedDateTests(unittest.IsolatedAsyncioTestCase):
    """Same regression as MarkSeasonWatchedDateTests, but for mark_show_watched."""

    async def _mark_show(self, **watched_at_kwargs) -> tuple[dict, WatchEvent]:
        show = Show(
            id=55,
            tmdb_id=100,
            title="Test Show",
            tmdb_data={"seasons": [{"season_number": 1, "episode_count": 1, "name": "Season 1"}]},
        )
        # execute() call order: show lookup, existing-episode lookup,
        # already-watched lookup, PlaybackProgress delete.
        db = _FakeSession([show, [], [], None])
        db.info["tmdb_key_7"] = "test-key"
        with (
            patch.object(history.tmdb, "get_season", AsyncMock(return_value=_SEASON_PAYLOAD)),
            patch("routers.history._push_watch_state", new_callable=AsyncMock),
        ):
            body = history.ShowWatchRequest(series_tmdb_id=100, **watched_at_kwargs)
            response = await history.mark_show_watched(body, db, SimpleNamespace(id=7))
        event = next(v for v in db.added if isinstance(v, WatchEvent))
        return response, event

    async def test_explicit_null_marks_show_watched_without_a_date(self) -> None:
        response, event = await self._mark_show(watched_at=None)
        self.assertEqual(response["count"], 1)
        self.assertIsNone(event.watched_at)

    async def test_omitted_watched_at_defaults_to_now(self) -> None:
        response, event = await self._mark_show()
        self.assertEqual(response["count"], 1)
        self.assertIsNotNone(event.watched_at)

    async def test_explicit_custom_date_is_used(self) -> None:
        custom = datetime(2020, 6, 15, 12, 0, 0)
        response, event = await self._mark_show(watched_at=custom)
        self.assertEqual(response["count"], 1)
        self.assertEqual(event.watched_at, custom)


if __name__ == "__main__":
    unittest.main()

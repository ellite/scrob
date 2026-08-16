import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from models.base import MediaType, CollectionSource
from models.episode_order import EpisodeOrderMapping, UserShowEpisodeOrder
from models.events import WatchEvent
from models.media import Media
from models.playback_session import PlaybackSession
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


class _NestedTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False  # let exceptions propagate, like a real SAVEPOINT rollback


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

    def begin_nested(self):
        return _NestedTxn()


class ManualEpisodeWatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user = SimpleNamespace(id=7)
        self.show = SimpleNamespace(id=55, tvdb_id=None)
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
            # Some call sites (e.g. the orphan-repair branch) go through
            # enrich_media_safely, which resolves enrich_media via
            # core.enrichment's own namespace rather than history.py's
            # imported alias - patch it there too so every code path is
            # controlled by this same mock.
            patch("core.enrichment.enrich_media", enrich),
            patch("routers.history._push_watch_state", push_state),
        )
        return patches, get_key, find_show, get_episode, enrich, push_state

    async def test_manual_episode_creates_parent_show_before_media(self):
        # Trailing None: record_rewatch_progress's own Media lookup (no
        # active rewatch involved in this test, so it no-ops from there).
        db = _FakeSession([None, None, None, None])
        patches, get_key, find_show, get_episode, enrich, push_state = self._patch_dependencies()

        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
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
        # Called twice: once for the WatchEvent itself, once more after
        # record_rewatch_progress (a no-op here, but still its own commit).
        self.assertEqual(db.commit.await_count, 2)

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
        db = _FakeSession([None, orphan, None, None])  # trailing None: record_rewatch_progress's Media lookup
        patches, _, _, get_episode, enrich, _ = self._patch_dependencies()

        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            await history.mark_as_watched(self.event, db, self.user)

        self.assertEqual(orphan.show_id, self.show.id)
        self.assertEqual((orphan.season_number, orphan.episode_number), (1, 1))
        get_episode.assert_not_awaited()
        enrich.assert_awaited_once_with(
            orphan, api_key="tmdb-key", series_tmdb_id=277439,
            tvdb_id=None, tvdb_api_key=None, tvdb_lang=None,
        )

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
        db = _FakeSession([mapped_media, None, None])  # trailing None: record_rewatch_progress's Media lookup
        patches, _, _, get_episode, enrich, push_state = self._patch_dependencies()

        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
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
        # Three execute() calls: the media lookup, the PlaybackProgress delete,
        # then record_rewatch_progress's own Media lookup (movies always no-op
        # there, but the query still runs before that type check).
        db = _FakeSession([media, None, None])
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
        # already-watched lookup (get_active_rewatch -> none, then the raw
        # WatchEvent query), PlaybackProgress delete, then
        # record_rewatch_progress's own Media lookup for the one new episode.
        db = _FakeSession([show, [], None, [], None, None])
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
        # already-watched lookup (get_active_rewatch -> none, then the raw
        # WatchEvent query), PlaybackProgress delete, then
        # record_rewatch_progress's own Media lookup for the one new episode.
        db = _FakeSession([show, [], None, [], None, None])
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


class PushWatchStateExcludeConnectionTests(unittest.IsolatedAsyncioTestCase):
    """Regression tests for #190: a two-way-sync connection (webhook in +
    push_watched out) can self-trigger an unbounded loop - Scrob pushes
    "unwatched" to Jellyfin, Jellyfin's own UserData change re-fires its
    webhook back into Scrob, which pushes "unwatched" again, forever. The
    fix is exclude_connection_id: the connection whose webhook triggered
    this push must be skipped, while other connections still get it."""

    def _connections(self):
        conn_origin = SimpleNamespace(
            id=1, type="jellyfin", url="http://origin.local", token="tok1", server_user_id="u1",
        )
        conn_other = SimpleNamespace(
            id=2, type="jellyfin", url="http://other.local", token="tok2", server_user_id="u2",
        )
        return conn_origin, conn_other

    def _coll_files(self):
        cf1 = SimpleNamespace(source=CollectionSource.jellyfin, source_id="item-1")
        cf2 = SimpleNamespace(source=CollectionSource.jellyfin, source_id="item-2")
        return cf1, cf2

    async def test_excludes_only_the_originating_connection(self) -> None:
        conn_origin, conn_other = self._connections()
        cf1, cf2 = self._coll_files()
        # Query order in _push_watch_state (settings=None short-circuits every
        # later trakt/mdblist/simkl query, keeping this fixture minimal):
        # 1. connections, 2. settings, 3. collection files.
        db = _FakeSession([[conn_origin, conn_other], None, [cf1, cf2]])

        calls: list[str] = []

        async def fake_mark_unwatched(url, token, user_id, source_id):
            calls.append(url)
            return True

        with patch("routers.history.jellyfin_client.mark_unwatched", fake_mark_unwatched):
            await history._push_watch_state(
                db, user_id=7, media_ids=[10], watched=False,
                exclude_connection_id=conn_origin.id,
            )

        self.assertNotIn("http://origin.local", calls)
        self.assertIn("http://other.local", calls)

    async def test_no_exclusion_pushes_to_every_connection(self) -> None:
        # Baseline: without exclude_connection_id (e.g. a manual UI mark, not
        # webhook-triggered), behavior is unchanged - every push_watched
        # connection still gets it, including what would be conn_origin above.
        conn_origin, conn_other = self._connections()
        cf1, cf2 = self._coll_files()
        db = _FakeSession([[conn_origin, conn_other], None, [cf1, cf2]])

        calls: list[str] = []

        async def fake_mark_unwatched(url, token, user_id, source_id):
            calls.append(url)
            return True

        with patch("routers.history.jellyfin_client.mark_unwatched", fake_mark_unwatched):
            await history._push_watch_state(db, user_id=7, media_ids=[10], watched=False)

        self.assertIn("http://origin.local", calls)
        self.assertIn("http://other.local", calls)


class GetNowPlayingEpisodeOrderTests(unittest.IsolatedAsyncioTestCase):
    """Regression test for #186: get_now_playing builds its media dict
    inline rather than going through enrich_with_state, so it needs its own
    wiring for the episode-order preference / TVDB position translation used
    by the Now Playing bar's link building."""

    async def test_tvdb_preference_session_gets_translated_position(self) -> None:
        media = Media(
            id=10, tmdb_id=550, media_type=MediaType.episode,
            title="Ep", season_number=4, episode_number=12, show_id=1,
        )
        show = Show(id=1, tmdb_id=550, tvdb_id=999, title="Show")
        session = PlaybackSession(
            id=1, user_id=7, media_id=10, session_key="k", source="plex",
            state="playing", progress_percent=0.1, progress_seconds=60,
            started_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1),
        )
        preference = UserShowEpisodeOrder(user_id=7, series_tmdb_id=550, episode_order="tvdb")
        mapping = EpisodeOrderMapping(
            series_tmdb_id=550, tmdb_season_number=4, tmdb_episode_number=12,
            tmdb_episode_id=1, tvdb_id=1, tvdb_season_number=3, tvdb_episode_number=8,
            match_method="external_id",
        )
        db = _FakeSession([
            [(session, media)],  # main PlaybackSession+Media query
            show,                # per-session Show lookup
            [preference],        # get_episode_orders_for_series
            [mapping],           # get_tmdb_to_tvdb_positions
        ])

        result = await history.get_now_playing(db=db, current_user=SimpleNamespace(id=7))

        item = result["now_playing"][0]["media"]
        self.assertEqual(item["show_episode_order"], "tvdb")
        self.assertEqual(item["tvdb_season_number"], 3)
        self.assertEqual(item["tvdb_episode_number"], 8)

    async def test_tmdb_preference_session_is_untouched(self) -> None:
        media = Media(
            id=10, tmdb_id=550, media_type=MediaType.episode,
            title="Ep", season_number=4, episode_number=12, show_id=1,
        )
        show = Show(id=1, tmdb_id=550, tvdb_id=999, title="Show")
        session = PlaybackSession(
            id=1, user_id=7, media_id=10, session_key="k", source="plex",
            state="playing", progress_percent=0.1, progress_seconds=60,
            started_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1),
        )
        db = _FakeSession([
            [(session, media)],  # main PlaybackSession+Media query
            show,                # per-session Show lookup
            [],                  # get_episode_orders_for_series - no preference row
        ])

        result = await history.get_now_playing(db=db, current_user=SimpleNamespace(id=7))

        item = result["now_playing"][0]["media"]
        self.assertNotIn("show_episode_order", item)
        self.assertNotIn("tvdb_season_number", item)


if __name__ == "__main__":
    unittest.main()

import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from fastapi import HTTPException

from models.base import MediaType, CollectionSource
from models.episode_order import EpisodeOrderMapping, UserShowEpisodeOrder, ShowEpisodePosition
from models.events import WatchEvent
from models.media import Media
from models.playback_progress import PlaybackProgress
from models.playback_session import PlaybackSession
from models.rewatch import ShowRewatch
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


class MarkAsWatchedTypeGuardTests(unittest.IsolatedAsyncioTestCase):
    """A watch event is movie/episode only - a whole show or season is a
    derived state, not its own event. #358."""

    async def _mark(self, media_type: str):
        db = _FakeSession([])
        return await history.mark_as_watched(
            WatchEventCreate(tmdb_id=97546, media_type=media_type),
            db,
            SimpleNamespace(id=7),
        )

    async def test_series_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            await self._mark("series")
        self.assertEqual(ctx.exception.status_code, 422)

    async def test_person_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            await self._mark("person")
        self.assertEqual(ctx.exception.status_code, 422)


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
        # (CollectionFile, Collection.media_id) - _push_watch_state selects
        # both columns, joining in the owning collection's media_id.
        cf1 = SimpleNamespace(source=CollectionSource.jellyfin, source_id="item-1")
        cf2 = SimpleNamespace(source=CollectionSource.jellyfin, source_id="item-2")
        return (cf1, 10), (cf2, 10)

    async def test_excludes_only_the_originating_connection(self) -> None:
        conn_origin, conn_other = self._connections()
        row1, row2 = self._coll_files()
        # Query order in _push_watch_state (settings=None short-circuits every
        # later trakt/mdblist/simkl query, keeping this fixture minimal):
        # 1. connections, 2. settings, 3. collection files.
        db = _FakeSession([[conn_origin, conn_other], None, [row1, row2]])

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
        row1, row2 = self._coll_files()
        db = _FakeSession([[conn_origin, conn_other], None, [row1, row2]])

        calls: list[str] = []

        async def fake_mark_unwatched(url, token, user_id, source_id):
            calls.append(url)
            return True

        with patch("routers.history.jellyfin_client.mark_unwatched", fake_mark_unwatched):
            await history._push_watch_state(db, user_id=7, media_ids=[10], watched=False)

        self.assertIn("http://origin.local", calls)
        self.assertIn("http://other.local", calls)


class GetNowPlayingEpisodeOrderTests(unittest.IsolatedAsyncioTestCase):
    """#174: get_now_playing builds its media dict inline rather than through
    enrich_with_state, so it needs its own wiring for the episode-order
    preference / display-position translation the Now Playing bar renders."""

    async def test_non_aired_order_session_gets_display_position(self) -> None:
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
        preference = UserShowEpisodeOrder(user_id=7, series_tmdb_id=550, episode_order="tvdb:dvd")
        pos = ShowEpisodePosition(
            series_tmdb_id=550, order_key="tvdb:dvd",
            display_season=3, display_episode=8,
            tmdb_episode_id=1, tmdb_season_number=4, tmdb_episode_number=12,
        )
        db = _FakeSession([
            [(session, media)],  # main PlaybackSession+Media query
            show,                # per-session Show lookup
            [preference],        # get_order_keys_for_series
            [pos],               # get_positions_for_series
        ])

        result = await history.get_now_playing(db=db, current_user=SimpleNamespace(id=7))

        item = result["now_playing"][0]["media"]
        self.assertEqual(item["show_episode_order"], "tvdb:dvd")
        self.assertEqual(item["display_season_number"], 3)
        self.assertEqual(item["display_episode_number"], 8)

    async def test_aired_preference_session_is_untouched(self) -> None:
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
            [],                  # get_order_keys_for_series - no non-aired pref
        ])

        result = await history.get_now_playing(db=db, current_user=SimpleNamespace(id=7))

        item = result["now_playing"][0]["media"]
        self.assertNotIn("show_episode_order", item)
        self.assertNotIn("display_season_number", item)


class GetNowPlayingCreditsStingerTests(unittest.IsolatedAsyncioTestCase):
    """#319 - the Now Playing bar's credits-scene icon reads these two flags
    straight off the playing movie's cached tmdb_data."""

    async def test_movie_with_stingers_exposes_both_flags(self) -> None:
        media = Media(
            id=10, tmdb_id=550, media_type=MediaType.movie, title="Fight Club",
            tmdb_data={"has_mid_credits_scene": True, "has_post_credits_scene": True},
        )
        session = PlaybackSession(
            id=1, user_id=7, media_id=10, session_key="k", source="plex",
            state="playing", progress_percent=0.1, progress_seconds=60,
            started_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1),
        )
        db = _FakeSession([[(session, media)]])

        result = await history.get_now_playing(db=db, current_user=SimpleNamespace(id=7))

        item = result["now_playing"][0]["media"]
        self.assertTrue(item["has_mid_credits_scene"])
        self.assertTrue(item["has_post_credits_scene"])

    async def test_movie_without_tmdb_data_defaults_to_false(self) -> None:
        media = Media(id=10, tmdb_id=550, media_type=MediaType.movie, title="Fight Club")
        session = PlaybackSession(
            id=1, user_id=7, media_id=10, session_key="k", source="plex",
            state="playing", progress_percent=0.1, progress_seconds=60,
            started_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1),
        )
        db = _FakeSession([[(session, media)]])

        result = await history.get_now_playing(db=db, current_user=SimpleNamespace(id=7))

        item = result["now_playing"][0]["media"]
        self.assertFalse(item["has_mid_credits_scene"])
        self.assertFalse(item["has_post_credits_scene"])


class ClearHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_clears_playback_progress_along_with_watch_events(self) -> None:
        # Continue Watching is sourced from PlaybackProgress, not WatchEvent -
        # a full clear must reset it too, or finished/cleared items keep
        # showing up there as if still in progress.
        db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())

        await history.clear_history(db=db, current_user=SimpleNamespace(id=7))

        tables_deleted = {
            call.args[0].table.name for call in db.execute.call_args_list
        }
        self.assertEqual(tables_deleted, {ShowRewatch.__tablename__, WatchEvent.__tablename__, PlaybackProgress.__tablename__})
        db.commit.assert_awaited_once()


class PushWatchStateEchoSuppressionTests(unittest.IsolatedAsyncioTestCase):
    """#324: a manual mark-watched with a backdated date got duplicated - the
    Jellyfin/Emby push echoes straight back via UserDataSaved, and the
    backdated watched_at slips past _write_watch_event's recent-event guard.
    Fix: register the push with mark_pushed_watched so the echo is caught."""

    def _fixture(self, conn_type):
        conn = SimpleNamespace(id=1, type=conn_type, url="http://srv.local",
                               token="t", server_user_id="u1")
        cf = SimpleNamespace(source=CollectionSource[conn_type], source_id="item-1")
        # query order: 1. connections, 2. settings, 3. collection files
        return _FakeSession([[conn], None, [(cf, 42)]])

    async def test_jellyfin_watched_push_registers_for_echo_suppression(self):
        registered: list[tuple[int, int]] = []

        async def fake_mark_watched(url, token, user_id, source_id):
            return True

        with patch("routers.history.jellyfin_client.mark_watched", fake_mark_watched), \
             patch("routers.webhooks.mark_pushed_watched",
                   side_effect=lambda uid, mid: registered.append((uid, mid))):
            await history._push_watch_state(
                self._fixture("jellyfin"), user_id=7, media_ids=[42], watched=True,
                watched_at_by_media={42: datetime(2020, 1, 1)},
            )

        self.assertEqual(registered, [(7, 42)])

    async def test_emby_watched_push_registers_for_echo_suppression(self):
        registered: list[tuple[int, int]] = []

        async def fake_mark_watched(url, token, user_id, source_id):
            return True

        with patch("routers.history.emby_client.mark_watched", fake_mark_watched), \
             patch("routers.webhooks.mark_pushed_watched",
                   side_effect=lambda uid, mid: registered.append((uid, mid))):
            await history._push_watch_state(
                self._fixture("emby"), user_id=7, media_ids=[42], watched=True,
                watched_at_by_media={42: datetime(2020, 1, 1)},
            )

        self.assertEqual(registered, [(7, 42)])

    async def test_unwatch_push_does_not_register(self):
        async def fake_mark_unwatched(url, token, user_id, source_id):
            return True

        with patch("routers.history.jellyfin_client.mark_unwatched", fake_mark_unwatched), \
             patch("routers.webhooks.mark_pushed_watched") as reg:
            await history._push_watch_state(
                self._fixture("jellyfin"), user_id=7, media_ids=[42], watched=False,
            )

        reg.assert_not_called()


class PushWatchStateTraktTokenTests(unittest.IsolatedAsyncioTestCase):
    """#326: the Trakt history fan-out on a manual mark-watched must go through
    ensure_valid_trakt_token, not use the stored token blindly."""

    def _settings(self, **overrides):
        base = dict(
            trakt_push_watched=True,
            trakt_access_token="tok",
            trakt_client_id="cid",
            trakt_client_secret=None,
            trakt_refresh_token=None,
            trakt_token_expires_at=9_999_999_999,
            trakt_push_collection=False,
            trakt_push_ratings=False,
            simkl_push_watched=False,
            simkl_access_token=None,
            simkl_client_id=None,
            mdblist_push_watched=False,
            mdblist_api_key=None,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def _movie(self):
        return SimpleNamespace(
            id=5, tmdb_id=550, media_type=MediaType.movie,
            show_id=None, season_number=None, episode_number=None, tmdb_data=None,
        )

    async def test_valid_token_is_passed_to_the_trakt_history_push(self):
        db = _FakeSession([[], self._settings(), [self._movie()]])
        add_movie = AsyncMock()
        with patch("routers.history.trakt_client.add_movie_to_history", add_movie):
            await history._push_watch_state(
                db, user_id=1, media_ids=[5], watched=True,
                watched_at_by_media={5: datetime(2024, 1, 1)},
            )
        add_movie.assert_awaited_once()
        self.assertEqual(add_movie.await_args.args[1], "tok")

    async def test_unrefreshable_token_skips_the_trakt_push(self):
        db = _FakeSession([[], self._settings(trakt_token_expires_at=1), [self._movie()]])
        add_movie = AsyncMock()
        with patch("routers.history.trakt_client.add_movie_to_history", add_movie), \
             patch("routers.trakt.trakt_client.validate_token", AsyncMock(return_value=False)):
            await history._push_watch_state(
                db, user_id=1, media_ids=[5], watched=True,
                watched_at_by_media={5: datetime(2024, 1, 1)},
            )
        add_movie.assert_not_awaited()


class DropMovieResolveTests(unittest.IsolatedAsyncioTestCase):
    """#330: dropping a movie opened straight from TMDB (no local Media row)
    must resolve/create one instead of storing a garbage id."""

    def setUp(self):
        # flag_modified needs a real ORM instance; these tests use SimpleNamespace.
        p = patch("routers.history.flag_modified")
        p.start()
        self.addCleanup(p.stop)

    async def test_drop_by_media_id_uses_it_directly(self):
        settings = SimpleNamespace(dropped_movies=[])
        db = _FakeSession([settings])
        await history.drop_movie(
            body=SimpleNamespace(media_id=42, tmdb_id=None),
            db=db, current_user=SimpleNamespace(id=1),
        )
        self.assertEqual(settings.dropped_movies, [42])

    async def test_drop_by_tmdb_id_uses_existing_media_row(self):
        settings = SimpleNamespace(dropped_movies=[])
        db = _FakeSession([settings, SimpleNamespace(id=99)])
        await history.drop_movie(
            body=SimpleNamespace(media_id=None, tmdb_id=550),
            db=db, current_user=SimpleNamespace(id=1),
        )
        self.assertEqual(settings.dropped_movies, [99])

    async def test_drop_by_tmdb_id_creates_media_row_when_missing(self):
        settings = SimpleNamespace(dropped_movies=[])
        db = _FakeSession([settings, None])
        with patch("routers.media.get_user_tmdb_key", new_callable=AsyncMock, return_value="k"), \
             patch("routers.history.tmdb.get_movie", new_callable=AsyncMock, return_value={"title": "Fight Club"}), \
             patch("routers.history.create_media_safely", new_callable=AsyncMock,
                   return_value=(SimpleNamespace(id=123), True)), \
             patch("routers.history.enrich_media", new_callable=AsyncMock):
            await history.drop_movie(
                body=SimpleNamespace(media_id=None, tmdb_id=550),
                db=db, current_user=SimpleNamespace(id=1),
            )
        self.assertEqual(settings.dropped_movies, [123])

    async def test_drop_with_neither_id_is_400(self):
        db = _FakeSession([SimpleNamespace(dropped_movies=[])])
        with self.assertRaises(HTTPException) as ctx:
            await history.drop_movie(
                body=SimpleNamespace(media_id=None, tmdb_id=None),
                db=db, current_user=SimpleNamespace(id=1),
            )
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_undrop_by_tmdb_id_removes_the_resolved_row_without_creating(self):
        settings = SimpleNamespace(dropped_movies=[99, 5])
        db = _FakeSession([settings, SimpleNamespace(id=99)])
        await history.undrop_movie(
            media_id=None, tmdb_id=550, db=db, current_user=SimpleNamespace(id=1),
        )
        self.assertEqual(settings.dropped_movies, [5])


class ListDroppedTests(unittest.IsolatedAsyncioTestCase):
    """#330: GET /history/dropped feeds the dedicated Dropped page."""

    async def test_returns_shows_and_movies_newest_first_skipping_unknown_ids(self):
        settings = SimpleNamespace(dropped_shows=[10, 20, 99], dropped_movies=[5])
        show10 = SimpleNamespace(id=10, tmdb_id=1399, tvdb_id=None, title="GoT",
                                 poster_path="/got.jpg", first_air_date="2011-04-17", status="Ended")
        show20 = SimpleNamespace(id=20, tmdb_id=None, tvdb_id=555, title="Old Show",
                                 poster_path=None, first_air_date=None, status=None)
        movie5 = SimpleNamespace(id=5, tmdb_id=550, title="Fight Club",
                                 poster_path="/fc.jpg", release_date="1999-10-15")
        db = _FakeSession([settings, [show10, show20], [movie5]])

        out = await history.list_dropped(db=db, current_user=SimpleNamespace(id=1))

        # id 99 has no Show row -> skipped; the rest come back newest-drop-first.
        self.assertEqual([s["id"] for s in out["shows"]], [20, 10])
        self.assertEqual(out["shows"][1]["year"], "2011")
        self.assertEqual(out["shows"][0]["tvdb_id"], 555)
        self.assertEqual(out["movies"], [
            {"id": 5, "tmdb_id": 550, "title": "Fight Club", "poster_path": "/fc.jpg", "year": "1999"},
        ])

    async def test_empty_when_nothing_dropped(self):
        db = _FakeSession([SimpleNamespace(dropped_shows=[], dropped_movies=None)])
        out = await history.list_dropped(db=db, current_user=SimpleNamespace(id=1))
        self.assertEqual(out, {"shows": [], "movies": []})


class RatePromptTests(unittest.IsolatedAsyncioTestCase):
    """#177: /history/rate-prompt decides whether the homepage shows a
    "rate it now" popup when a session drops off the Now Playing bar."""

    def _settings(self, *, movies=False, episodes=False):
        return SimpleNamespace(rate_prompt_movies=movies, rate_prompt_episodes=episodes)

    def _movie(self):
        return SimpleNamespace(
            id=10, tmdb_id=550, media_type=MediaType.movie, title="Fight Club",
            season_number=None, episode_number=None, poster_path="/fc.jpg", show=None,
        )

    async def _call(self, results):
        with patch("routers.history.is_unmapped_tvdb_episode", return_value=False):
            return await history.rate_prompt(
                media_id=10, db=_FakeSession(results), current_user=SimpleNamespace(id=7),
            )

    async def test_prompts_for_opted_in_recent_unrated_movie(self):
        out = await self._call([self._movie(), self._settings(movies=True), 99, None])
        self.assertTrue(out["should_prompt"])
        self.assertEqual(out["media"]["tmdb_id"], 550)
        self.assertEqual(out["media"]["type"], "movie")

    async def test_no_prompt_when_opted_out(self):
        out = await self._call([self._movie(), self._settings(movies=False), 99, None])
        self.assertFalse(out["should_prompt"])
        self.assertIsNone(out["media"])

    async def test_no_prompt_without_recent_completion(self):
        out = await self._call([self._movie(), self._settings(movies=True), None, None])
        self.assertFalse(out["should_prompt"])

    async def test_no_prompt_when_already_rated(self):
        out = await self._call([self._movie(), self._settings(movies=True), 99, 5])
        self.assertFalse(out["should_prompt"])

    async def test_episode_uses_show_title_and_still_then_show_poster(self):
        episode = SimpleNamespace(
            id=10, tmdb_id=42, media_type=MediaType.episode, title="Pilot",
            season_number=1, episode_number=1, poster_path="/ep-still.jpg",
            show=SimpleNamespace(title="The Show", poster_path="/show.jpg"),
        )
        out = await self._call([episode, self._settings(episodes=True), 99, None])
        self.assertTrue(out["should_prompt"])
        self.assertEqual(out["media"]["show_title"], "The Show")
        # Episode still first, show poster only as fallback.
        self.assertEqual(out["media"]["poster_path"], "/ep-still.jpg")

    async def test_episode_falls_back_to_show_poster_without_still(self):
        episode = SimpleNamespace(
            id=10, tmdb_id=42, media_type=MediaType.episode, title="Pilot",
            season_number=1, episode_number=1, poster_path=None,
            show=SimpleNamespace(title="The Show", poster_path="/show.jpg"),
        )
        out = await self._call([episode, self._settings(episodes=True), 99, None])
        self.assertEqual(out["media"]["poster_path"], "/show.jpg")


class ManualSessionEpisodeShowLinkTests(unittest.IsolatedAsyncioTestCase):
    """#366: starting a manual session for an episode of a show never added
    to Scrob before used to only ever look an existing local Show up, never
    create one - the episode's Media row landed with show_id=None, and the
    Now Playing bar (which sources an episode's title/poster/link from its
    linked Show) showed a blank poster, the bare episode title, and no link
    at all. Same root cause class as #192's webhook fast-path gap."""

    def _body(self, show_tmdb_id=9999):
        return SimpleNamespace(
            media_id=None, tmdb_id=42, media_type=MediaType.episode,
            title="Episode 1", runtime=46, show_tmdb_id=show_tmdb_id,
            season_number=1, episode_number=1,
        )

    async def test_unknown_show_is_created_and_linked(self):
        # First queued result: the "does a Media row for this tmdb_id already
        # exist" lookup at the top of the function (miss, since body.tmdb_id
        # is set) - then the Show lookup (also a miss).
        db = _FakeSession([None, None])
        new_show = SimpleNamespace(id=99, tmdb_id=9999, title="The Murder Detective", poster_path="/show.jpg")
        media_stub = SimpleNamespace(id=10, show_id=None)
        with patch("routers.history.get_user_tmdb_key", new_callable=AsyncMock, return_value="k"), \
             patch("routers.webhooks._find_or_create_show", new_callable=AsyncMock, return_value=new_show) as find_or_create, \
             patch("routers.history.create_media_safely", new_callable=AsyncMock, return_value=(media_stub, True)) as create_media:
            media = await history._get_or_create_media_for_session(db, self._body(), user_id=1)

        find_or_create.assert_awaited_once_with(db, 9999, "k")
        self.assertEqual(create_media.await_args.kwargs["show_id"], 99)
        self.assertEqual(media.show_id, 99)

    async def test_existing_local_show_is_reused_without_creating(self):
        existing_show = SimpleNamespace(id=5, tmdb_id=9999)
        db = _FakeSession([None, existing_show])
        media_stub = SimpleNamespace(id=10, show_id=None)
        with patch("routers.history.get_user_tmdb_key", new_callable=AsyncMock, return_value="k"), \
             patch("routers.webhooks._find_or_create_show", new_callable=AsyncMock) as find_or_create, \
             patch("routers.history.create_media_safely", new_callable=AsyncMock, return_value=(media_stub, True)) as create_media:
            media = await history._get_or_create_media_for_session(db, self._body(), user_id=1)

        find_or_create.assert_not_awaited()
        self.assertEqual(create_media.await_args.kwargs["show_id"], 5)
        self.assertEqual(media.show_id, 5)

    async def test_no_tmdb_key_skips_show_creation_without_erroring(self):
        db = _FakeSession([None, None])
        media_stub = SimpleNamespace(id=10, show_id=None)
        with patch("routers.history.get_user_tmdb_key", new_callable=AsyncMock, return_value=None), \
             patch("routers.history.check_tmdb_key", return_value=False), \
             patch("routers.webhooks._find_or_create_show", new_callable=AsyncMock) as find_or_create, \
             patch("routers.history.create_media_safely", new_callable=AsyncMock, return_value=(media_stub, True)):
            media = await history._get_or_create_media_for_session(db, self._body(), user_id=1)

        find_or_create.assert_not_awaited()
        self.assertIsNone(media.show_id)

    async def test_no_show_tmdb_id_is_a_noop_for_show_linking(self):
        # Only the top-of-function "existing Media for this tmdb_id" lookup
        # runs - show_tmdb_id is None, so the Show-lookup branch is skipped
        # entirely.
        db = _FakeSession([None])
        media_stub = SimpleNamespace(id=10, show_id=None)
        with patch("routers.history.get_user_tmdb_key", new_callable=AsyncMock, return_value="k"), \
             patch("routers.webhooks._find_or_create_show", new_callable=AsyncMock) as find_or_create, \
             patch("routers.history.create_media_safely", new_callable=AsyncMock, return_value=(media_stub, True)) as create_media:
            media = await history._get_or_create_media_for_session(db, self._body(show_tmdb_id=None), user_id=1)

        find_or_create.assert_not_awaited()
        self.assertIsNone(create_media.await_args.kwargs["show_id"])
        self.assertIsNone(media.show_id)


class EffectiveRuntimeTests(unittest.TestCase):
    """#383: a NULL Media.runtime freezes the Now Playing bar - fall back to
    the cached tmdb_data.runtime for rows enriched before #169."""

    def _media(self, runtime, tmdb_runtime):
        return SimpleNamespace(runtime=runtime, tmdb_data={"runtime": tmdb_runtime} if tmdb_runtime is not None else {})

    def test_column_value_wins(self):
        self.assertEqual(history._effective_runtime(self._media(45, 50)), 45)

    def test_falls_back_to_tmdb_data(self):
        self.assertEqual(history._effective_runtime(self._media(None, 53)), 53)

    def test_tmdb_data_runtime_as_string_is_coerced(self):
        self.assertEqual(history._effective_runtime(self._media(None, "22")), 22)

    def test_zero_or_missing_or_junk_is_none(self):
        self.assertIsNone(history._effective_runtime(self._media(None, 0)))
        self.assertIsNone(history._effective_runtime(self._media(None, None)))
        self.assertIsNone(history._effective_runtime(self._media(None, "n/a")))
        self.assertIsNone(history._effective_runtime(SimpleNamespace(runtime=None, tmdb_data=None)))


if __name__ == "__main__":
    unittest.main()

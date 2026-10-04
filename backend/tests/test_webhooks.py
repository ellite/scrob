import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from sqlalchemy.exc import IntegrityError
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm.exc import StaleDataError

from models.base import MediaType
from routers import webhooks
from routers.webhooks import (
    _backfill_credits_stingers,
    _backfill_jellyfin_runtimes,
    _backfill_kodi_runtime,
    _backfill_plex_runtime,
    _commit_playback_session_update,
    _consume_recently_pushed_watched,
    _ensure_collection_entry,
    _episode_for_progress,
    _get_or_open_session,
    _is_duplicate_webhook_delivery,
    _maybe_bingebase_scrobble,
    _maybe_simkl_scrobble,
    _resolve_plex_progress,
    _resolve_tvdb_episode_to_tmdb_position,
    _translate_plex_tvdb_episode_position,
    _write_completed_events_and_filter_echoes,
    _write_watch_event,
    find_or_create_media_jellyfin,
    find_or_create_media_jellyfin_multi,
    find_or_create_media_kodi,
    find_or_create_media_plex,
    mark_pushed_watched,
    parse_jellyfin_payload,
    parse_kodi_payload,
    parse_plex_payload,
)


class _Scalars:
    def __init__(self, value):
        self._value = value

    def first(self):
        return self._value[0] if isinstance(self._value, list) else self._value

    def all(self):
        if isinstance(self._value, list):
            return self._value
        return [] if self._value is None else [self._value]


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return _Scalars(self._value)


class _FakeDB:
    """Fakes just enough of AsyncSession for _write_watch_event: every
    execute() call returns the next queued scalar_one_or_none() value, and
    add() is recorded so tests can assert whether a WatchEvent was created."""

    def __init__(self, queued_scalars):
        self._queued = list(queued_scalars)
        self.added = []
        self.executed_statements = []
        self.commits = 0

    async def execute(self, stmt):
        self.executed_statements.append(stmt)
        value = self._queued.pop(0) if self._queued else None
        return _ScalarResult(value)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1


class DuplicateWebhookDeliveryTests(unittest.TestCase):
    def setUp(self):
        webhooks._recent_webhook_deliveries.clear()

    def test_second_call_with_same_key_is_flagged_as_duplicate(self):
        key = "plex:1:session-abc:media.stop"
        self.assertFalse(_is_duplicate_webhook_delivery(key))
        self.assertTrue(_is_duplicate_webhook_delivery(key))

    def test_distinct_keys_are_never_duplicates(self):
        self.assertFalse(_is_duplicate_webhook_delivery("plex:1:session-abc:media.stop"))
        self.assertFalse(_is_duplicate_webhook_delivery("plex:1:session-abc:media.scrobble"))
        self.assertFalse(_is_duplicate_webhook_delivery("plex:2:session-xyz:media.stop"))


class WriteWatchEventDedupTests(IsolatedAsyncioTestCase):
    def setUp(self):
        webhooks._recently_pushed_watched.clear()

    async def test_first_completed_event_is_recorded(self):
        db = _FakeDB(queued_scalars=[None])  # no recent WatchEvent found
        result = await _write_watch_event(db, user_id=1, media_id=2, progress_percent=1.0, progress_seconds=120, completed=True)
        self.assertEqual(len(db.added), 1)
        self.assertTrue(result)

    async def test_second_completed_event_for_same_media_within_window_is_skipped(self):
        # Simulates media.scrobble having already written a WatchEvent moments
        # ago, then media.stop firing for the same viewing. Not an echo, so
        # callers scrobbling this onward should still treat it as real (#369).
        db = _FakeDB(queued_scalars=[123])  # a recent WatchEvent id is found
        result = await _write_watch_event(db, user_id=1, media_id=2, progress_percent=1.0, progress_seconds=120, completed=True)
        self.assertEqual(len(db.added), 0)
        self.assertTrue(result)

    async def test_echo_of_a_just_pushed_mark_watched_is_skipped(self):
        # Regression for #247/#251: pushing "mark watched" to Jellyfin/Emby can
        # echo straight back as a UserDataSaved webhook. The item's real watch
        # event is typically old (imported history), so the 5-minute dedup
        # above never catches it and it used to land as a brand new WatchEvent
        # stamped at push time - even though no recent duplicate is queued here.
        mark_pushed_watched(user_id=1, media_id=2)
        db = _FakeDB(queued_scalars=[None])
        result = await _write_watch_event(db, user_id=1, media_id=2, progress_percent=1.0, progress_seconds=120, completed=True)
        self.assertEqual(len(db.added), 0)
        # False here is the signal callers use to also skip scrobbling this
        # row onward to Trakt/MDBList/Simkl/Bingebase (#369).
        self.assertFalse(result)

    async def test_echo_suppression_is_scoped_to_the_pushed_user_and_media(self):
        mark_pushed_watched(user_id=1, media_id=2)
        db = _FakeDB(queued_scalars=[None, None])
        await _write_watch_event(db, user_id=1, media_id=999, progress_percent=1.0, progress_seconds=120, completed=True)
        await _write_watch_event(db, user_id=999, media_id=2, progress_percent=1.0, progress_seconds=120, completed=True)
        self.assertEqual(len(db.added), 2)

    async def test_echo_suppression_is_one_shot(self):
        # A real rewatch shortly after must not be silently swallowed too -
        # only the one echo actually expected back from the push is consumed.
        mark_pushed_watched(user_id=1, media_id=2)
        db = _FakeDB(queued_scalars=[None, None])
        await _write_watch_event(db, user_id=1, media_id=2, progress_percent=1.0, progress_seconds=120, completed=True)
        await _write_watch_event(db, user_id=1, media_id=2, progress_percent=1.0, progress_seconds=120, completed=True)
        self.assertEqual(len(db.added), 1)

    async def test_unknown_dated_dedup_branch_is_bound_by_created_at(self):
        # Regression for #355: the null-watched_at branch of this guard used
        # to have no time bound at all ("NULL >= cutoff" is never true in SQL,
        # so it matched unconditionally instead) - once a title had any
        # unknown-dated watch event, every later real rewatch reported by
        # Jellyfin/Plex/Emby was silently treated as a duplicate of it
        # forever. watched_at can't carry that bound when it's NULL, so the
        # shared dedup query (core.watch_dedup.find_duplicate_watch_event)
        # falls back to created_at via coalesce() - assert that's actually in
        # the query, not just watched_at.
        db = _FakeDB(queued_scalars=[None])
        await _write_watch_event(db, user_id=1, media_id=2, progress_percent=1.0, progress_seconds=120, completed=True)
        compiled = str(db.executed_statements[0])
        self.assertIn("coalesce(watch_events.watched_at, watch_events.created_at)", compiled)


class WriteCompletedEventsAndFilterEchoesTests(IsolatedAsyncioTestCase):
    """#369: a Jellyfin/Emby "mark played" webhook for a multi-episode file
    carries several media rows in one call - only the ones that turn out to
    be push-watched echoes should be dropped before scrobbling onward, not
    the whole batch and not none of it."""

    def setUp(self):
        webhooks._recently_pushed_watched.clear()

    async def test_filters_out_only_the_echoed_media(self):
        # media_id=2 was just pushed (an echo is expected back for it);
        # media_id=3 is a genuine, unrelated completion in the same payload.
        mark_pushed_watched(user_id=1, media_id=2)
        db = _FakeDB(queued_scalars=[None])  # only media_id=3 reaches a real query
        media_list = [SimpleNamespace(id=2), SimpleNamespace(id=3)]

        result = await _write_completed_events_and_filter_echoes(db, 1, media_list, progress_seconds=120)

        self.assertEqual([m.id for m in result], [3])
        self.assertEqual(len(db.added), 1)

    async def test_nothing_echoed_keeps_the_whole_list(self):
        db = _FakeDB(queued_scalars=[None, None])
        media_list = [SimpleNamespace(id=2), SimpleNamespace(id=3)]

        result = await _write_completed_events_and_filter_echoes(db, 1, media_list, progress_seconds=120)

        self.assertEqual([m.id for m in result], [2, 3])
        self.assertEqual(len(db.added), 2)


class _NestedTxn:
    def __init__(self, db: "_OpenSessionFakeDB"):
        self.db = db

    async def __aenter__(self):
        self.db.events.append("begin_nested")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False  # let exceptions propagate, like a real SAVEPOINT rollback


class _OpenSessionFakeDB:
    """Fakes just enough of AsyncSession for _get_or_open_session: each
    execute() call returns the next queued scalar_one_or_none() value, add()
    is recorded (with ordering relative to begin_nested(), same regression
    guard as create_media_safely's own tests), and flush() can be made to
    raise to simulate a lost race on the session_key unique index."""

    def __init__(self, queued_scalars, flush_raises: Exception | None = None):
        self._queued = list(queued_scalars)
        self.added: list = []
        self.events: list[str] = []
        self.flush_raises = flush_raises
        self.flush_calls = 0

    async def execute(self, stmt):
        value = self._queued.pop(0) if self._queued else None
        return _ScalarResult(value)

    def add(self, obj):
        self.events.append("add")
        self.added.append(obj)

    def begin_nested(self):
        return _NestedTxn(self)

    async def flush(self):
        self.flush_calls += 1
        if self.flush_raises is not None:
            raise self.flush_raises


class GetOrOpenSessionTests(IsolatedAsyncioTestCase):
    """Regression tests for #392: two webhook events for a session that
    doesn't exist yet (e.g. Jellyfin's PlaybackStart and its first
    PlaybackProgress, sent back to back) can both SELECT nothing and both
    try to INSERT - the loser must recover instead of 500ing."""

    async def test_existing_session_is_returned_without_inserting(self):
        existing = SimpleNamespace(id=1, session_key="jellyfin:1:abc")
        db = _OpenSessionFakeDB(queued_scalars=[existing])

        session = await _get_or_open_session(db, "jellyfin:1:abc", "jellyfin", 1, 2)

        self.assertIs(session, existing)
        self.assertEqual(db.added, [])
        self.assertEqual(db.events, [])

    async def test_creates_session_when_none_exists(self):
        db = _OpenSessionFakeDB(queued_scalars=[None])

        session = await _get_or_open_session(db, "jellyfin:1:abc", "jellyfin", 1, 2)

        self.assertEqual(session.session_key, "jellyfin:1:abc")
        self.assertIn(session, db.added)
        # add() must happen *after* the savepoint is entered - see
        # _OpenSessionFakeDB and create_media_safely's matching comment.
        self.assertEqual(db.events, ["begin_nested", "add"])

    async def test_lost_race_returns_the_winners_row(self):
        winner = SimpleNamespace(id=7, session_key="jellyfin:1:abc")
        db = _OpenSessionFakeDB(
            queued_scalars=[None, winner],  # first SELECT: none; re-select after conflict: winner
            flush_raises=IntegrityError("stmt", {}, Exception("duplicate key")),
        )

        session = await _get_or_open_session(db, "jellyfin:1:abc", "jellyfin", 1, 2)

        self.assertIs(session, winner)
        self.assertEqual(db.events, ["begin_nested", "add"])

    async def test_integrity_error_with_no_existing_row_reraises(self):
        db = _OpenSessionFakeDB(
            queued_scalars=[None, None],
            flush_raises=IntegrityError("stmt", {}, Exception("duplicate key")),
        )

        with self.assertRaises(IntegrityError):
            await _get_or_open_session(db, "jellyfin:1:abc", "jellyfin", 1, 2)


class ParsePlexPayloadGrandparentGuidTests(unittest.TestCase):
    """Regression coverage for #440's calendar investigation: grandparentGuid
    parsing used its own stale inline regex instead of core.plex's
    extract_tmdb_id/extract_tvdb_id/extract_imdb_id, so it never gained HAMA
    agent support (com.plexapp.agents.hama://tvdb-73762/1/1) the way the
    item's own guid extraction (just above it in parse_plex_payload) did.
    For a HAMA-scanned show this meant the grandparent's real id silently
    failed to resolve, forcing every such episode through a much weaker
    title-only fallback - a real gap that made this class of misattribution
    possible."""

    @staticmethod
    def _payload(grandparent_guid: str) -> dict:
        return {
            "event": "media.scrobble",
            "Metadata": {
                "type": "episode",
                "title": "Episode 1",
                "grandparentTitle": "Vigil",
                "grandparentGuid": grandparent_guid,
                "grandparentRatingKey": "999",
                "parentIndex": 1,
                "index": 1,
                "ratingKey": "123",
                "Guid": [],
            },
        }

    def test_hama_grandparent_tmdb_id_is_extracted(self):
        data = parse_plex_payload(self._payload("com.plexapp.agents.hama://tmdb-126167/1/1"))
        self.assertEqual(data["grandparent_tmdb_id"], "126167")

    def test_hama_grandparent_tvdb_id_is_extracted(self):
        data = parse_plex_payload(self._payload("com.plexapp.agents.hama://tvdb-73762/1/1"))
        self.assertEqual(data["grandparent_tvdb_id"], "73762")
        self.assertIsNone(data["grandparent_tmdb_id"])

    def test_legacy_and_plain_schemes_still_work(self):
        data = parse_plex_payload(self._payload("com.plexapp.agents.themoviedb://126167?lang=en"))
        self.assertEqual(data["grandparent_tmdb_id"], "126167")

        data = parse_plex_payload(self._payload("tmdb://126167"))
        self.assertEqual(data["grandparent_tmdb_id"], "126167")

    def test_unresolvable_grandparent_guid_yields_no_ids(self):
        data = parse_plex_payload(self._payload("plex://show/abc123"))
        self.assertIsNone(data["grandparent_tmdb_id"])
        self.assertIsNone(data["grandparent_tvdb_id"])
        self.assertIsNone(data["grandparent_imdb_id"])


class FindOrCreateMediaPlexRefreshTests(IsolatedAsyncioTestCase):
    """Regression tests for #394: an episode matched by tmdb_id must have its
    metadata refreshed from TMDB on every match, not just returned as-is from
    whatever Plex reported when the row was first created - Plex's own title
    for a brand-new episode can still be a pre-air working title ("TTT
    Anniversary" vs. the aired "125 Years Young"), and unlike the episode/
    season pages this cached row was otherwise never revisited again."""

    def _payload(self):
        return {
            "media_type": "episode",
            "tmdb_id": "123",
            "grandparent_tmdb_id": "999",
            "title": "TTT Anniversary",
        }

    async def test_already_linked_episode_is_refreshed_from_tmdb(self):
        existing = SimpleNamespace(id=10, media_type=MediaType.episode, show_id=55, tmdb_id=123)
        show = SimpleNamespace(id=55, tmdb_id=999, tvdb_id=None)
        refreshed = SimpleNamespace(id=10, media_type=MediaType.episode, show_id=55, tmdb_id=123, title="125 Years Young")
        db = _FakeDB(queued_scalars=[existing, show])

        with patch("routers.webhooks.enrich_media_safely", AsyncMock(return_value=refreshed)) as enrich_safely:
            result = await find_or_create_media_plex(self._payload(), db, api_key="tmdb-key", user_id=7)

        self.assertIs(result, refreshed)
        enrich_safely.assert_awaited_once()
        _, kwargs = enrich_safely.await_args
        self.assertEqual(kwargs.get("series_tmdb_id"), 999)

    async def test_refresh_failure_falls_back_to_the_cached_row(self):
        # enrich_media already tolerates a TMDB failure internally and leaves
        # the row untouched - this covers the (unexpected) case of something
        # else in the refresh path raising, which must still not lose the
        # perfectly good cached row or crash the webhook.
        existing = SimpleNamespace(
            id=10, media_type=MediaType.episode, show_id=55, tmdb_id=123, title="TTT Anniversary",
        )
        show = SimpleNamespace(id=55, tmdb_id=999, tvdb_id=None)
        db = _FakeDB(queued_scalars=[existing, show])

        with patch("routers.webhooks.enrich_media_safely", AsyncMock(side_effect=Exception("TMDB unreachable"))):
            result = await find_or_create_media_plex(self._payload(), db, api_key="tmdb-key", user_id=7)

        self.assertIs(result, existing)
        self.assertEqual(result.title, "TTT Anniversary")

    async def test_orphan_episode_still_gets_backfilled_and_refreshed(self):
        # media.show_id is None: the pre-existing backfill-show-context branch
        # must still run before the new refresh-on-match behavior.
        existing = SimpleNamespace(id=10, media_type=MediaType.episode, show_id=None, tmdb_id=123)
        show = SimpleNamespace(id=55, tmdb_id=999, tvdb_id=None)
        refreshed = SimpleNamespace(id=10, media_type=MediaType.episode, show_id=55, tmdb_id=123, title="125 Years Young")
        db = _FakeDB(queued_scalars=[existing])

        with (
            patch("routers.webhooks._find_or_create_show", AsyncMock(return_value=show)),
            patch("routers.webhooks.enrich_media_safely", AsyncMock(return_value=refreshed)) as enrich_safely,
        ):
            result = await find_or_create_media_plex(self._payload(), db, api_key="tmdb-key", user_id=7)

        self.assertIs(result, refreshed)
        self.assertEqual(existing.show_id, 55)
        enrich_safely.assert_awaited_once()


class _CollectionIdResult:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value


class _FirstResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _EnsureCollectionFakeDB:
    """Routes _ensure_collection_entry's queries by SQL text and captures the
    collection_files insert so tests can inspect its bound connection_id."""

    def __init__(self, *, connection_exists: bool, file_exists: bool = False):
        self._connection_exists = connection_exists
        self._file_exists = file_exists
        self.collection_file_insert = None

    async def execute(self, stmt):
        sql = str(stmt)
        if "media_server_connections" in sql:
            return _ScalarResult(1 if self._connection_exists else None)
        if sql.startswith("SELECT") and "FROM collections" in sql:
            return _CollectionIdResult(7)
        if sql.startswith("SELECT") and "FROM collection_files" in sql:
            return _FirstResult((1,) if self._file_exists else None)
        if "collection_files" in sql:
            self.collection_file_insert = stmt
        return _ScalarResult(None)

    async def flush(self):
        pass


class EnsureCollectionEntryConnectionGuardTests(IsolatedAsyncioTestCase):
    """#339: collection_files.connection_id is FK'd to media_server_connections.
    A scrobble webhook passes a ScrobbleConnection id (different table/sequence)
    - _ensure_collection_entry must store NULL, not let the INSERT FK-crash and
    roll back the whole webhook (losing the watch event + stuck Now Playing)."""

    def _bound_connection_id(self, stmt):
        from sqlalchemy.dialects import postgresql
        return stmt.compile(dialect=postgresql.dialect()).params.get("connection_id")

    async def test_unknown_connection_id_is_stored_as_null(self):
        from models.base import CollectionSource
        db = _EnsureCollectionFakeDB(connection_exists=False)
        await _ensure_collection_entry(
            db, user_id=1, media_id=2, source=CollectionSource.plex,
            source_id="521136", quality=None, connection_id=999,
        )
        self.assertIsNotNone(db.collection_file_insert)
        self.assertIsNone(self._bound_connection_id(db.collection_file_insert))

    async def test_real_connection_id_is_kept(self):
        db = _EnsureCollectionFakeDB(connection_exists=True)
        from models.base import CollectionSource
        await _ensure_collection_entry(
            db, user_id=1, media_id=2, source=CollectionSource.plex,
            source_id="521136", quality=None, connection_id=5,
        )
        self.assertEqual(self._bound_connection_id(db.collection_file_insert), 5)

    async def test_none_connection_id_skips_the_lookup(self):
        db = _EnsureCollectionFakeDB(connection_exists=False)
        from models.base import CollectionSource
        await _ensure_collection_entry(
            db, user_id=1, media_id=2, source=CollectionSource.plex,
            source_id="521136", quality=None, connection_id=None,
        )
        self.assertIsNone(self._bound_connection_id(db.collection_file_insert))


class EnsureCollectionEntryCreatedFlagTests(IsolatedAsyncioTestCase):
    """#420: the return value tells library.new handlers whether the item is new to the source."""

    async def _call(self, *, file_exists: bool) -> bool:
        from models.base import CollectionSource
        db = _EnsureCollectionFakeDB(connection_exists=True, file_exists=file_exists)
        return await _ensure_collection_entry(
            db, user_id=1, media_id=2, source=CollectionSource.plex,
            source_id="521136", quality=None, connection_id=5,
        )

    async def test_new_file_returns_true(self):
        self.assertTrue(await self._call(file_exists=False))

    async def test_existing_file_returns_false(self):
        self.assertFalse(await self._call(file_exists=True))


class PushWatchedForNewItemTests(IsolatedAsyncioTestCase):
    """#420: an item added to a server that Scrob already has watched gets pushed there."""

    def _conn(self, type_="jellyfin", push_watched=True):
        return SimpleNamespace(
            type=type_, push_watched=push_watched, url="http://srv", token="t", server_user_id="u1",
        )

    async def _push(self, conn, *, watched, item, media_ids=(11,)):
        from routers import sync as sync_router
        media_list = [SimpleNamespace(id=i) for i in media_ids]
        with (
            patch.object(sync_router, "_latest_watched_at", AsyncMock(return_value=watched)),
            patch("core.jellyfin.get_item", AsyncMock(return_value=item)) as get_item,
            patch("core.jellyfin.mark_watched", AsyncMock(return_value=True)) as mark,
            patch("core.plex.get_item", AsyncMock(return_value=item)),
            patch.object(sync_router, "_push_plex_watched_and_record", AsyncMock(return_value=True)) as plex_push,
        ):
            result = await webhooks._push_watched_for_new_item(None, 1, conn, "sid", media_list)
        return result, mark, plex_push

    async def test_jellyfin_pushes_with_original_watch_date(self):
        when = datetime(2024, 5, 1, 12, 0, 0)
        result, mark, _ = await self._push(
            self._conn(), watched={11: when}, item={"UserData": {"Played": False}},
        )
        self.assertTrue(result)
        self.assertEqual(mark.await_args.kwargs["played_at"], when)

    async def test_jellyfin_already_played_is_left_alone(self):
        result, mark, _ = await self._push(
            self._conn(), watched={11: None}, item={"UserData": {"Played": True}},
        )
        self.assertFalse(result)
        mark.assert_not_awaited()

    async def test_plex_already_viewed_is_left_alone(self):
        result, _, plex_push = await self._push(
            self._conn("plex"), watched={11: None}, item={"viewCount": 2},
        )
        self.assertFalse(result)
        plex_push.assert_not_awaited()

    async def test_plex_unviewed_is_pushed(self):
        result, _, plex_push = await self._push(
            self._conn("plex"), watched={11: None}, item={"viewCount": 0},
        )
        self.assertTrue(result)
        plex_push.assert_awaited_once()

    async def test_unwatched_in_scrob_is_not_pushed(self):
        result, mark, _ = await self._push(
            self._conn(), watched={}, item={"UserData": {"Played": False}},
        )
        self.assertFalse(result)
        mark.assert_not_awaited()

    async def test_multi_episode_file_needs_every_episode_watched(self):
        result, mark, _ = await self._push(
            self._conn(), watched={11: None}, item={"UserData": {"Played": False}}, media_ids=(11, 12),
        )
        self.assertFalse(result)
        mark.assert_not_awaited()

    async def test_push_watched_off_does_nothing(self):
        result, mark, _ = await self._push(
            self._conn(push_watched=False), watched={11: None}, item={"UserData": {"Played": False}},
        )
        self.assertFalse(result)
        mark.assert_not_awaited()


class ConsumeRecentlyPushedWatchedTests(unittest.TestCase):
    def setUp(self):
        webhooks._recently_pushed_watched.clear()

    def test_unmarked_media_is_not_consumed(self):
        self.assertFalse(_consume_recently_pushed_watched(user_id=1, media_id=2))

    def test_marked_media_is_consumed_once(self):
        mark_pushed_watched(user_id=1, media_id=2)
        self.assertTrue(_consume_recently_pushed_watched(user_id=1, media_id=2))
        self.assertFalse(_consume_recently_pushed_watched(user_id=1, media_id=2))

    def test_expired_marker_is_not_consumed(self):
        import datetime
        mark_pushed_watched(user_id=1, media_id=2)
        webhooks._recently_pushed_watched[(1, 2)][0] = datetime.datetime.utcnow() - webhooks._PUSHED_WATCHED_TTL - datetime.timedelta(seconds=1)
        self.assertFalse(_consume_recently_pushed_watched(user_id=1, media_id=2))

    def test_two_pending_pushes_each_consume_their_own_echo(self):
        # A user pushing the same item to two Jellyfin/Emby connections at
        # once expects two echoes back - the second shouldn't be treated as
        # an unexpected duplicate just because the first already consumed.
        mark_pushed_watched(user_id=1, media_id=2)
        mark_pushed_watched(user_id=1, media_id=2)
        self.assertTrue(_consume_recently_pushed_watched(user_id=1, media_id=2))
        self.assertTrue(_consume_recently_pushed_watched(user_id=1, media_id=2))
        self.assertFalse(_consume_recently_pushed_watched(user_id=1, media_id=2))


class ParseJellyfinPayloadEmbyEventFieldTests(unittest.TestCase):
    """Regression test for #160: Emby doesn't send NotificationType at all -
    its webhooks report the event under "Event" (dotted, lowercase names like
    "playback.stop"), which used to be read from the wrong key entirely,
    silently no-oping every inbound Emby webhook."""

    def test_reads_notification_type_from_emby_event_field(self):
        payload = {
            "Event": "playback.stop",
            "Item": {
                "Id": "test1",
                "Name": "Supergirl",
                "Type": "Movie",
                "ProductionYear": 2026,
                "RunTimeTicks": 64800000000,
                "ProviderIds": {"Tmdb": "1081003"},
            },
            "Session": {
                "Id": "testsession1",
                "UserName": "arne",
                "PlayState": {"PositionTicks": 61560000000, "IsPaused": False},
            },
        }
        data = parse_jellyfin_payload(payload)
        self.assertIsNotNone(data)
        self.assertEqual(data["notification_type"], "playback.stop")
        self.assertEqual(data["title"], "Supergirl")

    def test_notification_type_still_prefers_pascal_case_field(self):
        payload = {
            "NotificationType": "PlaybackStop",
            "Event": "playback.stop",
            "Item": {"Id": "test1", "Name": "Supergirl", "Type": "Movie"},
        }
        data = parse_jellyfin_payload(payload)
        self.assertEqual(data["notification_type"], "PlaybackStop")


class ParseJellyfinPayloadNestedEpisodeSeriesNameTests(unittest.TestCase):
    """Regression test for #192: Emby's native webhook notifications use this
    nested Item/Session shape and don't reliably populate SeriesProviderIds
    for an episode the way Jellyfin's "send all properties" plugin does.
    Without a series_name fallback here (mirroring the flat-format branch,
    which already has one), find_or_create_media_jellyfin can never resolve
    show linkage - Now Playing then shows the bare episode title with no
    poster instead of the series."""

    def test_nested_episode_payload_includes_series_name_fallback(self):
        payload = {
            "Event": "playback.start",
            "Item": {
                "Id": "ep1",
                "Name": "Aquamom",
                "Type": "Episode",
                "SeriesName": "Entourage",
                "ParentIndexNumber": 3,
                "IndexNumber": 1,
                "ProviderIds": {"Tmdb": "1081099"},
                # SeriesProviderIds deliberately absent - this is the exact gap.
            },
            "Session": {"Id": "sess1", "UserName": "arne", "PlayState": {}},
        }
        data = parse_jellyfin_payload(payload)
        self.assertIsNotNone(data)
        self.assertIsNone(data["series_tmdb_id"])
        self.assertEqual(data["series_name"], "Entourage")

    def test_nested_movie_payload_has_no_series_name(self):
        # A movie item has no SeriesName field at all - must not crash or
        # fabricate a value.
        payload = {
            "Event": "playback.start",
            "Item": {"Id": "m1", "Name": "Inception", "Type": "Movie"},
            "Session": {"Id": "sess1", "PlayState": {}},
        }
        data = parse_jellyfin_payload(payload)
        self.assertIsNone(data["series_name"])


class ParseJellyfinPayloadPlayedToCompletionTests(unittest.TestCase):
    """Regression tests for #206: on auto-play, Emby resets Session.PlayState
    to the next episode before firing the "playback.stop" event for the one
    that just finished, so PositionTicks/RunTimeTicks there read 0 - a
    genuinely completed episode looked like a <5% no-op stop and was silently
    dropped. PlaybackInfo carries this event's own authoritative position and
    PlayedToCompletion flag; the flat plugin format exposes the same flag as
    its own top-level property."""

    def test_nested_format_falls_back_to_playback_info_position(self):
        payload = {
            "Event": "playback.stop",
            "Item": {"Id": "ep1", "Name": "Finale", "Type": "Episode", "RunTimeTicks": 10_000_000},
            # Session.PlayState already reset for the auto-playing next episode.
            "Session": {"Id": "sess1", "PlayState": {"PositionTicks": 0}},
            "PlaybackInfo": {"PositionTicks": 10_000_000, "PlayedToCompletion": True},
        }
        data = parse_jellyfin_payload(payload)
        self.assertEqual(data["progress_percent"], 1.0)
        self.assertTrue(data["played_to_completion"])

    def test_nested_format_prefers_session_position_when_present(self):
        # A normal (non-auto-play) stop still has a real Session position -
        # PlaybackInfo must not override a legitimate in-progress stop.
        payload = {
            "Event": "playback.stop",
            "Item": {"Id": "ep1", "Name": "Ep", "Type": "Episode", "RunTimeTicks": 10_000_000},
            "Session": {"Id": "sess1", "PlayState": {"PositionTicks": 3_000_000}},
            "PlaybackInfo": {"PositionTicks": 10_000_000, "PlayedToCompletion": True},
        }
        data = parse_jellyfin_payload(payload)
        self.assertEqual(data["progress_percent"], 0.3)

    def test_nested_format_defaults_played_to_completion_false(self):
        payload = {
            "Event": "playback.stop",
            "Item": {"Id": "ep1", "Name": "Ep", "Type": "Episode"},
            "Session": {"Id": "sess1", "PlayState": {}},
        }
        data = parse_jellyfin_payload(payload)
        self.assertFalse(data["played_to_completion"])

    def test_flat_format_reads_played_to_completion(self):
        payload = {
            "NotificationType": "PlaybackStop",
            "ItemType": "Episode",
            "PlayedToCompletion": True,
        }
        data = parse_jellyfin_payload(payload)
        self.assertTrue(data["played_to_completion"])

    def test_flat_format_defaults_played_to_completion_false(self):
        payload = {"NotificationType": "PlaybackStop", "ItemType": "Episode"}
        data = parse_jellyfin_payload(payload)
        self.assertFalse(data["played_to_completion"])


class ParseJellyfinFlatPayloadSeasonZeroTests(unittest.TestCase):
    """Regression test for #132: a Season 0 (specials) episode has
    SeasonNumber: 0 in the flat webhook payload, which a falsy check like
    `payload.get("SeasonNumber") or None` incorrectly coerces to None."""

    def test_season_zero_is_preserved_not_coerced_to_none(self):
        payload = {
            "NotificationType": "PlaybackStart",
            "ItemType": "Episode",
            "ItemId": "abc123",
            "Name": "Behind the Scenes",
            "SeriesName": "Some Show",
            "SeasonNumber": 0,
            "EpisodeNumber": 1,
            "Provider_tmdb": "999",
        }
        data = parse_jellyfin_payload(payload)
        self.assertIsNotNone(data)
        self.assertEqual(data["season_number"], 0)

    def test_movie_has_no_season_number(self):
        payload = {
            "NotificationType": "PlaybackStart",
            "ItemType": "Movie",
            "ItemId": "xyz",
            "Name": "A Movie",
        }
        data = parse_jellyfin_payload(payload)
        self.assertIsNone(data["season_number"])


class ParseJellyfinUserDataSavedPayloadTests(unittest.TestCase):
    """Regression test for #69: Jellyfin's official Webhook plugin has no
    "MarkPlayed" event — manually toggling watched/unwatched raises
    UserDataSaved with SaveReason=TogglePlayed instead. The parser must
    surface both fields so the handler can tell a real toggle apart from
    the same notification firing on every playback tick/rating/favorite."""

    def test_extracts_played_and_save_reason_on_manual_toggle(self):
        payload = {
            "NotificationType": "UserDataSaved",
            "ItemType": "Episode",
            "ItemId": "abc123",
            "Name": "Pilot",
            "SeriesName": "Some Show",
            "SeasonNumber": 1,
            "EpisodeNumber": 1,
            "Provider_tmdb": "999",
            "SaveReason": "TogglePlayed",
            "Played": True,
        }
        data = parse_jellyfin_payload(payload)
        self.assertIsNotNone(data)
        self.assertEqual(data["save_reason"], "TogglePlayed")
        self.assertIs(data["played"], True)

    def test_extracts_played_false_for_unwatch_toggle(self):
        payload = {
            "NotificationType": "UserDataSaved",
            "ItemType": "Movie",
            "ItemId": "xyz",
            "Name": "A Movie",
            "SaveReason": "TogglePlayed",
            "Played": False,
        }
        data = parse_jellyfin_payload(payload)
        self.assertEqual(data["save_reason"], "TogglePlayed")
        self.assertIs(data["played"], False)

    def test_still_parses_non_toggle_save_reasons(self):
        # UserDataSaved also fires for playback progress, ratings, favorites,
        # etc. — the handler (not the parser) is responsible for ignoring
        # those via save_reason, so parsing itself must not drop them.
        payload = {
            "NotificationType": "UserDataSaved",
            "ItemType": "Movie",
            "ItemId": "xyz",
            "Name": "A Movie",
            "SaveReason": "PlaybackProgress",
            "Played": False,
        }
        data = parse_jellyfin_payload(payload)
        self.assertEqual(data["save_reason"], "PlaybackProgress")


class ParseJellyfinMultiEpisodePayloadTests(unittest.TestCase):
    """Regression tests for #138 follow-up: Jellyfin can mux several episodes
    into one file and fire a single webhook event for it, exposing the span
    via IndexNumber/IndexNumberEnd on the nested-format Item."""

    def test_nested_format_extracts_index_number_end(self):
        payload = {
            "NotificationType": "MarkPlayed",
            "Item": {"Type": "Episode", "Id": "abc", "Name": "Ep 1-2", "IndexNumber": 1, "IndexNumberEnd": 2},
            "Session": {},
        }
        data = parse_jellyfin_payload(payload)
        self.assertEqual(data["episode_number"], 1)
        self.assertEqual(data["episode_number_end"], 2)

    def test_nested_format_single_episode_has_no_end(self):
        payload = {
            "NotificationType": "MarkPlayed",
            "Item": {"Type": "Episode", "Id": "abc", "Name": "Ep 1", "IndexNumber": 1},
            "Session": {},
        }
        data = parse_jellyfin_payload(payload)
        self.assertIsNone(data["episode_number_end"])

    def test_flat_format_extracts_episode_number_end(self):
        # "Send all properties" - the setup this repo's README documents,
        # since custom templates produce invalid JSON - includes
        # EpisodeNumberEnd alongside EpisodeNumber for a combined file.
        # Confirmed against a live payload while diagnosing #138 follow-up.
        payload = {
            "NotificationType": "PlaybackStart",
            "ItemType": "Episode",
            "ItemId": "abc",
            "SeasonNumber": 1,
            "EpisodeNumber": 1,
            "EpisodeNumberEnd": 2,
        }
        data = parse_jellyfin_payload(payload)
        self.assertEqual(data["episode_number_end"], 2)

    def test_flat_format_end_is_none_when_absent(self):
        # A normal single-episode file has no EpisodeNumberEnd key at all.
        payload = {
            "NotificationType": "MarkPlayed",
            "ItemType": "Episode",
            "ItemId": "abc",
            "SeasonNumber": 1,
            "EpisodeNumber": 1,
        }
        data = parse_jellyfin_payload(payload)
        self.assertIsNone(data["episode_number_end"])


class ParseJellyfinSeriesYearTests(unittest.TestCase):
    """#373: the flat plugin sets Year to the series' production year on an
    episode payload - carried as series_year so _resolve_show_for_episode can
    pick between two shows that share a title."""

    def test_flat_episode_carries_the_series_year(self):
        data = parse_jellyfin_payload({
            "NotificationType": "PlaybackStop", "ItemType": "Episode",
            "Name": "The Holy Trinity", "SeriesName": "The Grand Tour",
            "Year": 2026, "SeasonNumber": 1, "EpisodeNumber": 1,
        })
        self.assertEqual(data["series_year"], 2026)

    def test_flat_movie_has_no_series_year(self):
        data = parse_jellyfin_payload({
            "NotificationType": "PlaybackStop", "ItemType": "Movie",
            "Name": "The Matrix", "Year": 1999,
        })
        self.assertIsNone(data["series_year"])

    def test_nested_episode_has_no_series_year(self):
        # item.ProductionYear there is the episode's year, not the series'.
        data = parse_jellyfin_payload({
            "Event": "playback.stop",
            "Item": {"Id": "ep1", "Name": "Ep", "Type": "Episode", "ProductionYear": 2026},
            "Session": {"Id": "s", "PlayState": {}},
        })
        self.assertIsNone(data["series_year"])


class PickShowByYearTests(unittest.TestCase):
    """#373: from same-title Show candidates, pick the one matching the
    payload's series year (within a year), else fall back to the first."""

    def _show(self, tmdb_id, first_air_date):
        return SimpleNamespace(tmdb_id=tmdb_id, first_air_date=first_air_date)

    def test_single_candidate_is_returned_as_is(self):
        s = self._show(1, "2016-01-01")
        self.assertIs(webhooks._pick_show_by_year([s], 2020), s)

    def test_no_year_returns_the_first(self):
        a, b = self._show(1, "2016"), self._show(2, "2026")
        self.assertIs(webhooks._pick_show_by_year([a, b], None), a)

    def test_year_within_one_matches(self):
        a, b = self._show(1, "2016-05-13"), self._show(2, "2025-12-31")
        self.assertIs(webhooks._pick_show_by_year([a, b], 2026), b)

    def test_no_match_falls_back_to_the_first(self):
        a, b = self._show(1, "2016"), self._show(2, "2018")
        self.assertIs(webhooks._pick_show_by_year([a, b], 2030), a)

    def test_empty_is_none(self):
        self.assertIsNone(webhooks._pick_show_by_year([], 2020))


class ResolveShowForEpisodeYearTests(IsolatedAsyncioTestCase):
    """#373: an episode of a show whose title collides with another show's
    (a remake/reboot) was attributed to whichever row came back first."""

    def _db(self, shows):
        async def execute(_stmt):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(shows)))
        return SimpleNamespace(execute=execute)

    def _data(self, **over):
        d = {
            "media_type": "episode", "series_name": "The Grand Tour",
            "series_year": 2026, "series_tmdb_id": None,
        }
        d.update(over)
        return d

    async def test_local_year_match_wins_over_the_first_row(self):
        old = SimpleNamespace(id=1, tmdb_id=67557, first_air_date="2016-05-13")
        new = SimpleNamespace(id=2, tmdb_id=329471, first_air_date="2026-01-01")
        created = SimpleNamespace(id=2, tmdb_id=329471)
        with patch("routers.webhooks._find_or_create_show", AsyncMock(return_value=created)) as find_show:
            show, series_tmdb_id = await webhooks._resolve_show_for_episode(self._data(), self._db([old, new]))
        self.assertEqual(series_tmdb_id, 329471)
        self.assertIs(show, created)
        self.assertEqual(find_show.await_args.args[1], 329471)

    async def test_no_series_year_keeps_the_first_row(self):
        old = SimpleNamespace(id=1, tmdb_id=67557, first_air_date="2016-05-13")
        new = SimpleNamespace(id=2, tmdb_id=329471, first_air_date="2026-01-01")
        with patch("routers.webhooks._find_or_create_show",
                   AsyncMock(return_value=SimpleNamespace(id=1, tmdb_id=67557))):
            _, series_tmdb_id = await webhooks._resolve_show_for_episode(
                self._data(series_year=None), self._db([old, new])
            )
        self.assertEqual(series_tmdb_id, 67557)

    async def test_tmdb_search_fallback_is_year_scoped_first(self):
        search = AsyncMock(return_value={"results": [{"id": 329471}]})
        with patch("routers.webhooks.tmdb.search_shows", search), \
             patch("routers.webhooks._find_or_create_show",
                   AsyncMock(return_value=SimpleNamespace(id=9, tmdb_id=329471))):
            _, series_tmdb_id = await webhooks._resolve_show_for_episode(self._data(), self._db([]))
        self.assertEqual(series_tmdb_id, 329471)
        self.assertEqual(search.await_args_list[0].kwargs.get("year"), 2026)


class FindOrCreateMediaJellyfinMultiTests(IsolatedAsyncioTestCase):
    """Regression tests for #138 follow-up (bittom's comment): scrobbling a
    combined multi-episode file previously only ever marked the first
    episode watched. find_or_create_media_jellyfin_multi is the piece that
    expands one webhook event into a resolve-per-episode call."""

    def _base_data(self, **overrides):
        data = {
            "media_type": "episode",
            "jellyfin_id": "file-1",
            "title": "Ep 1-2",
            "season_number": 1,
            "episode_number": 1,
            "episode_number_end": None,
            "tmdb_id": None,
            "series_tmdb_id": None,
        }
        data.update(overrides)
        return data

    async def test_single_episode_resolves_once(self):
        fake_media = object()
        mock_resolver = AsyncMock(return_value=fake_media)
        with patch("routers.webhooks.find_or_create_media_jellyfin", mock_resolver):
            result = await find_or_create_media_jellyfin_multi(self._base_data(), db=None)

        self.assertEqual(result, [fake_media])
        mock_resolver.assert_awaited_once()

    async def test_combined_span_resolves_one_call_per_episode(self):
        seen_episode_numbers = []

        async def fake_resolver(data, db, api_key=None, user_id=None):
            seen_episode_numbers.append(data["episode_number"])
            return object()

        mock_resolver = AsyncMock(side_effect=fake_resolver)
        data = self._base_data(episode_number=1, episode_number_end=3)
        with patch("routers.webhooks.find_or_create_media_jellyfin", mock_resolver):
            result = await find_or_create_media_jellyfin_multi(data, db=None)

        self.assertEqual(seen_episode_numbers, [1, 2, 3])
        self.assertEqual(len(result), 3)
        # The original event payload must be untouched for any other caller.
        self.assertEqual(data["episode_number"], 1)

    async def test_equal_start_and_end_resolves_once(self):
        mock_resolver = AsyncMock(return_value=object())
        data = self._base_data(episode_number=4, episode_number_end=4)
        with patch("routers.webhooks.find_or_create_media_jellyfin", mock_resolver):
            await find_or_create_media_jellyfin_multi(data, db=None)

        mock_resolver.assert_awaited_once()

    async def test_movie_never_expands_even_with_an_end_value(self):
        mock_resolver = AsyncMock(return_value=object())
        data = self._base_data(media_type="movie", episode_number=None, episode_number_end=2)
        with patch("routers.webhooks.find_or_create_media_jellyfin", mock_resolver):
            await find_or_create_media_jellyfin_multi(data, db=None)

        mock_resolver.assert_awaited_once()

    async def test_unresolvable_sub_episode_is_skipped_not_fatal(self):
        # One episode in the span can't be identified (e.g. TMDB lookup
        # failed for just that one) — the rest of the span must still land.
        async def fake_resolver(data, db, api_key=None, user_id=None):
            return None if data["episode_number"] == 2 else object()

        mock_resolver = AsyncMock(side_effect=fake_resolver)
        data = self._base_data(episode_number=1, episode_number_end=3)
        with patch("routers.webhooks.find_or_create_media_jellyfin", mock_resolver):
            result = await find_or_create_media_jellyfin_multi(data, db=None)

        self.assertEqual(len(result), 2)


class _FastPathScalars:
    def __init__(self, item):
        self._item = item

    def first(self):
        return self._item


class _FastPathResult:
    def __init__(self, item):
        self._item = item

    def scalars(self):
        return _FastPathScalars(self._item)


class _FastPathDB:
    """Fakes just enough of AsyncSession for find_or_create_media_jellyfin's
    CollectionFile fast-path query: a single execute() returning the queued
    Media match (or None)."""

    def __init__(self, media):
        self._media = media

    async def execute(self, stmt):
        return _FastPathResult(self._media)


class FindOrCreateMediaJellyfinBackfillShowLinkageTests(IsolatedAsyncioTestCase):
    """Regression tests for #192 follow-up: the CollectionFile fast-path match
    in find_or_create_media_jellyfin returned an existing episode Media row
    as-is even when it was missing show_id (e.g. synced/created before the
    series_name/CollectionSource.emby fixes existed, or because show
    resolution simply failed the first time). Since this fast path is hit on
    every subsequent webhook for an already-synced item, an unlinked episode
    stayed unlinked forever - Now Playing kept showing the bare episode title
    instead of the series - unless something backfills show_id here too, the
    same way the slower TMDB-ID match path a few lines down already did."""

    def _episode_data(self, **overrides):
        data = {
            "media_type": "episode",
            "jellyfin_id": "file-1",
            "title": "Ep 1",
            "series_name": "Entourage",
            "season_number": 1,
            "episode_number": 1,
            "tmdb_id": None,
            "series_tmdb_id": None,
        }
        data.update(overrides)
        return data

    async def test_fast_path_backfills_missing_show_id(self):
        episode = SimpleNamespace(id=99, media_type=MediaType.episode, show_id=None)
        show = SimpleNamespace(id=42, tvdb_id=None)
        db = _FastPathDB(episode)

        with patch("routers.webhooks._resolve_show_for_episode", AsyncMock(return_value=(show, 555))), \
             patch("routers.webhooks._resolve_tvdb_fallback", AsyncMock(return_value=(None, None, None))), \
             patch("routers.webhooks.enrich_media", AsyncMock()) as enrich_mock:
            result = await find_or_create_media_jellyfin(self._episode_data(), db, api_key="key")

        self.assertIs(result, episode)
        self.assertEqual(episode.show_id, 42)
        enrich_mock.assert_awaited_once()

    async def test_fast_path_leaves_already_linked_episode_untouched(self):
        episode = SimpleNamespace(id=100, media_type=MediaType.episode, show_id=7)
        db = _FastPathDB(episode)

        with patch("routers.webhooks._resolve_show_for_episode", AsyncMock()) as resolve_mock, \
             patch("routers.webhooks.enrich_media", AsyncMock()) as enrich_mock:
            result = await find_or_create_media_jellyfin(self._episode_data(), db, api_key="key")

        self.assertIs(result, episode)
        resolve_mock.assert_not_awaited()
        enrich_mock.assert_not_awaited()

    async def test_fast_path_show_resolution_failure_still_returns_media(self):
        # Show couldn't be resolved this time either (e.g. TMDB down) - must
        # still return the existing media match rather than losing it.
        episode = SimpleNamespace(id=101, media_type=MediaType.episode, show_id=None)
        db = _FastPathDB(episode)

        with patch("routers.webhooks._resolve_show_for_episode", AsyncMock(return_value=(None, None))), \
             patch("routers.webhooks.enrich_media", AsyncMock()) as enrich_mock:
            result = await find_or_create_media_jellyfin(self._episode_data(), db, api_key="key")

        self.assertIs(result, episode)
        self.assertIsNone(episode.show_id)
        enrich_mock.assert_not_awaited()


class _QueuedResultDB:
    """Fakes AsyncSession.execute() as a queue of .scalars().first()-style
    results, popped in call order."""

    def __init__(self, media_results):
        self._queue = list(media_results)

    async def execute(self, stmt):
        return _FastPathResult(self._queue.pop(0) if self._queue else None)


class ResolveTvdbEpisodeToTmdbPositionTests(IsolatedAsyncioTestCase):
    """#162: resolves a Jellyfin-reported TVDB-native (season, episode)
    position to the canonical TMDB one, computing/caching the mapping for
    just that season on demand when it isn't known yet."""

    def _show(self):
        return SimpleNamespace(id=9, tmdb_id=100, tvdb_id=389597)

    async def test_returns_none_without_tvdb_id_or_keys(self):
        db = AsyncMock()
        show_no_tvdb = SimpleNamespace(id=9, tmdb_id=100, tvdb_id=None)
        result = await _resolve_tvdb_episode_to_tmdb_position(db, show_no_tvdb, 2, 1, "tmdb-key", "tvdb-key")
        self.assertIsNone(result)
        db.execute.assert_not_awaited()

        result = await _resolve_tvdb_episode_to_tmdb_position(db, self._show(), 2, 1, "tmdb-key", None)
        self.assertIsNone(result)

    async def test_uses_an_existing_mapping_without_recomputing(self):
        db = AsyncMock()
        mapping = SimpleNamespace(tmdb_season_number=1, tmdb_episode_number=25)
        with (
            patch("routers.webhooks.get_mapping_by_tvdb_position", AsyncMock(return_value=mapping)),
            patch("routers.webhooks.ensure_episode_order_mapping_for_season", AsyncMock()) as compute_mock,
        ):
            result = await _resolve_tvdb_episode_to_tmdb_position(db, self._show(), 2, 1, "tmdb-key", "tvdb-key")
        self.assertEqual(result, (1, 25))
        compute_mock.assert_not_awaited()

    async def test_computes_on_demand_and_reconciles_when_no_mapping_exists_yet(self):
        db = AsyncMock()
        new_mapping = SimpleNamespace(
            tmdb_season_number=1, tmdb_episode_number=25, tvdb_season_number=2, tvdb_episode_number=1,
        )
        show = self._show()
        with (
            patch("routers.webhooks.get_mapping_by_tvdb_position", AsyncMock(return_value=None)),
            patch("routers.webhooks.ensure_episode_order_mapping_for_season", AsyncMock(return_value=[new_mapping])),
            patch("routers.webhooks.reconcile_divergent_episode_media", AsyncMock()) as reconcile_mock,
        ):
            result = await _resolve_tvdb_episode_to_tmdb_position(db, show, 2, 1, "tmdb-key", "tvdb-key")
        self.assertEqual(result, (1, 25))
        reconcile_mock.assert_awaited_once_with(db, show, season_number=2)

    async def test_returns_none_when_genuinely_unmapped(self):
        # Real TVDB-only content (#101) - must fall through cleanly, not force a match.
        db = AsyncMock()
        with (
            patch("routers.webhooks.get_mapping_by_tvdb_position", AsyncMock(return_value=None)),
            patch("routers.webhooks.ensure_episode_order_mapping_for_season", AsyncMock(return_value=[])),
        ):
            result = await _resolve_tvdb_episode_to_tmdb_position(db, self._show(), 2, 1, "tmdb-key", "tvdb-key")
        self.assertIsNone(result)

    async def test_swallows_exceptions_and_returns_none(self):
        db = AsyncMock()
        with patch("routers.webhooks.get_mapping_by_tvdb_position", AsyncMock(side_effect=RuntimeError("boom"))):
            result = await _resolve_tvdb_episode_to_tmdb_position(db, self._show(), 2, 1, "tmdb-key", "tvdb-key")
        self.assertIsNone(result)


class TranslatePlexTvdbEpisodePositionTests(IsolatedAsyncioTestCase):
    """#335: Plex reports whatever numbering the library's episode-ordering
    setting uses. When the user has put the show on TVDB (aired) order,
    _translate_plex_tvdb_episode_position rewrites the reported (season,
    episode) to the canonical TMDB position in place - and does nothing at all
    otherwise, so a show still on TMDB numbering is never disturbed."""

    def _data(self, **overrides):
        data = {
            "media_type": "episode",
            "tmdb_id": None,
            "season_number": 2,
            "episode_number": 1,
        }
        data.update(overrides)
        return data

    async def test_noop_without_tvdb_order_preference(self):
        db = AsyncMock()
        data = self._data()
        with (
            patch("routers.webhooks.get_episode_order", AsyncMock(return_value=None)),
            patch("routers.webhooks._resolve_tvdb_episode_to_tmdb_position", AsyncMock()) as translate,
        ):
            await _translate_plex_tvdb_episode_position(data, db, series_tmdb_id=100, user_id=1, tmdb_api_key="k")
        self.assertEqual((data["season_number"], data["episode_number"]), (2, 1))
        translate.assert_not_awaited()

    async def test_noop_when_preference_is_tmdb(self):
        db = AsyncMock()
        data = self._data()
        pref = SimpleNamespace(episode_order="tmdb")
        with (
            patch("routers.webhooks.get_episode_order", AsyncMock(return_value=pref)),
            patch("routers.webhooks._resolve_tvdb_episode_to_tmdb_position", AsyncMock()) as translate,
        ):
            await _translate_plex_tvdb_episode_position(data, db, 100, 1, "k")
        translate.assert_not_awaited()
        self.assertEqual((data["season_number"], data["episode_number"]), (2, 1))

    async def test_noop_when_episode_has_a_tmdb_id(self):
        db = AsyncMock()
        data = self._data(tmdb_id="555")
        with patch("routers.webhooks.get_episode_order", AsyncMock()) as order:
            await _translate_plex_tvdb_episode_position(data, db, 100, 1, "k")
        order.assert_not_awaited()

    async def test_noop_when_show_has_no_tvdb_id(self):
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_ScalarResult(SimpleNamespace(tmdb_id=100, tvdb_id=None)))
        data = self._data()
        with (
            patch("routers.webhooks.get_episode_order", AsyncMock(return_value=SimpleNamespace(episode_order="tvdb"))),
            patch("routers.webhooks._resolve_tvdb_episode_to_tmdb_position", AsyncMock()) as translate,
        ):
            await _translate_plex_tvdb_episode_position(data, db, 100, 1, "k")
        translate.assert_not_awaited()

    async def test_rewrites_position_when_mapping_resolves(self):
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_ScalarResult(SimpleNamespace(id=9, tmdb_id=100, tvdb_id=42)))
        data = self._data(season_number=2, episode_number=1)
        with (
            patch("routers.webhooks.get_episode_order", AsyncMock(return_value=SimpleNamespace(episode_order="tvdb:official"))),
            patch("routers.webhooks._resolve_tvdb_fallback", AsyncMock(return_value=(42, "tvdb-key", None))),
            patch("routers.webhooks._resolve_tvdb_episode_to_tmdb_position", AsyncMock(return_value=(1, 25))),
        ):
            await _translate_plex_tvdb_episode_position(data, db, 100, 1, "tmdb-key")
        self.assertEqual((data["season_number"], data["episode_number"]), (1, 25))

    async def test_leaves_position_untouched_for_genuinely_tvdb_only_episode(self):
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_ScalarResult(SimpleNamespace(id=9, tmdb_id=100, tvdb_id=42)))
        data = self._data(season_number=0, episode_number=7)
        with (
            patch("routers.webhooks.get_episode_order", AsyncMock(return_value=SimpleNamespace(episode_order="tvdb:official"))),
            patch("routers.webhooks._resolve_tvdb_fallback", AsyncMock(return_value=(42, "tvdb-key", None))),
            patch("routers.webhooks._resolve_tvdb_episode_to_tmdb_position", AsyncMock(return_value=None)),
        ):
            await _translate_plex_tvdb_episode_position(data, db, 100, 1, "tmdb-key")
        self.assertEqual((data["season_number"], data["episode_number"]), (0, 7))

    async def test_never_raises(self):
        db = AsyncMock()
        data = self._data()
        with patch("routers.webhooks.get_episode_order", AsyncMock(side_effect=RuntimeError("boom"))):
            await _translate_plex_tvdb_episode_position(data, db, 100, 1, "k")
        self.assertEqual((data["season_number"], data["episode_number"]), (2, 1))


class FindOrCreateMediaJellyfinTvdbTranslationTests(IsolatedAsyncioTestCase):
    """#162: find_or_create_media_jellyfin must match/create the episode at
    its canonical TMDB position when one can be resolved, not Jellyfin's raw
    (potentially TVDB-native) SeasonNumber/EpisodeNumber."""

    def _episode_data(self, **overrides):
        data = {
            "media_type": "episode",
            "jellyfin_id": None,  # skip the CollectionFile fast path
            "title": "Ep 1",
            "series_name": "Jujutsu Kaisen",
            "season_number": 2,
            "episode_number": 1,
            "tmdb_id": None,
            "series_tmdb_id": None,
        }
        data.update(overrides)
        return data

    async def test_translated_position_is_used_for_the_media_match(self):
        show = SimpleNamespace(id=9, tmdb_id=100, tvdb_id=389597)
        found_media = SimpleNamespace(id=1, media_type=MediaType.episode, show_id=9, season_number=1, episode_number=25)
        data = self._episode_data()
        db = _QueuedResultDB([found_media])  # step 3's match, at the translated position

        with (
            patch("routers.webhooks._resolve_show_for_episode", AsyncMock(return_value=(show, 100))),
            patch("routers.webhooks._resolve_tvdb_fallback", AsyncMock(return_value=(389597, "tvdb-key", "en"))),
            patch("routers.webhooks._resolve_tvdb_episode_to_tmdb_position", AsyncMock(return_value=(1, 25))),
        ):
            result = await find_or_create_media_jellyfin(data, db, api_key="tmdb-key")

        self.assertIs(result, found_media)
        # The raw TVDB-native numbers Jellyfin sent must have been replaced
        # with the canonical TMDB position before the match/create step.
        self.assertEqual(data["season_number"], 1)
        self.assertEqual(data["episode_number"], 25)

    async def test_unresolvable_position_falls_through_to_raw_numbers_unchanged(self):
        show = SimpleNamespace(id=9, tmdb_id=100, tvdb_id=None)
        found_media = SimpleNamespace(id=1, media_type=MediaType.episode, show_id=9, season_number=2, episode_number=1)
        data = self._episode_data()
        db = _QueuedResultDB([found_media])

        with (
            patch("routers.webhooks._resolve_show_for_episode", AsyncMock(return_value=(show, 100))),
            patch("routers.webhooks._resolve_tvdb_fallback", AsyncMock(return_value=(None, None, None))),
            patch("routers.webhooks._resolve_tvdb_episode_to_tmdb_position", AsyncMock(return_value=None)) as resolve_mock,
        ):
            result = await find_or_create_media_jellyfin(data, db, api_key="tmdb-key")

        self.assertIs(result, found_media)
        self.assertEqual(data["season_number"], 2)
        self.assertEqual(data["episode_number"], 1)
        resolve_mock.assert_awaited_once()


class _FakeSessionCommitDB:
    """Fakes just enough of AsyncSession for _commit_playback_session_update:
    commit() either succeeds or raises a queued exception; rollback() is
    recorded so tests can assert it was called to recover the session."""

    def __init__(self, commit_side_effect=None):
        self._commit_side_effect = commit_side_effect
        self.rollback_called = False
        self.refreshed = []

    async def commit(self):
        if self._commit_side_effect:
            raise self._commit_side_effect

    async def rollback(self):
        self.rollback_called = True

    async def refresh(self, obj):
        if isinstance(obj, Exception):
            raise obj
        self.refreshed.append(obj)


class CommitPlaybackSessionUpdateTests(IsolatedAsyncioTestCase):
    """Regression tests for a live crash hit while testing #138: Jellyfin
    sends no dedup protection on webhook deliveries, so an overlapping
    PlaybackProgress tick can race a PlaybackStop for the same session_key -
    the stop's _close_session() deletes the PlaybackSession row, and the
    progress tick's later UPDATE against that now-gone row raises
    sqlalchemy.orm.exc.StaleDataError, crashing the whole request with a 500
    instead of just no-op'ing (the session is closed either way)."""

    async def test_normal_commit_succeeds(self):
        db = _FakeSessionCommitDB()
        result = await _commit_playback_session_update(db)
        self.assertTrue(result)
        self.assertFalse(db.rollback_called)

    async def test_stale_data_error_is_caught_and_rolled_back(self):
        db = _FakeSessionCommitDB(commit_side_effect=StaleDataError("0 were matched"))
        result = await _commit_playback_session_update(db)
        self.assertFalse(result)
        self.assertTrue(db.rollback_called)

    async def test_kept_objects_are_reloaded_after_rollback(self):
        # #410: the rollback expires settings/media, and the scrobble
        # forwarders that run next would lazy-load them (MissingGreenlet).
        db = _FakeSessionCommitDB(commit_side_effect=StaleDataError("0 were matched"))
        settings, media = object(), object()
        await _commit_playback_session_update(db, settings, None, media)
        self.assertEqual(db.refreshed, [settings, media])

    async def test_kept_objects_are_not_touched_on_normal_commit(self):
        db = _FakeSessionCommitDB()
        await _commit_playback_session_update(db, object())
        self.assertEqual(db.refreshed, [])

    async def test_kept_object_whose_row_is_gone_is_skipped(self):
        db = _FakeSessionCommitDB(commit_side_effect=StaleDataError("0 were matched"))
        gone, alive = InvalidRequestError("Could not refresh instance"), object()
        result = await _commit_playback_session_update(db, gone, alive)
        self.assertFalse(result)
        self.assertEqual(db.refreshed, [alive])

    async def test_other_exceptions_still_propagate(self):
        db = _FakeSessionCommitDB(commit_side_effect=RuntimeError("unrelated failure"))
        with self.assertRaises(RuntimeError):
            await _commit_playback_session_update(db)
        self.assertFalse(db.rollback_called)


class EpisodeForProgressTests(unittest.TestCase):
    """Regression tests for the Now Playing bar showing only the first
    episode of a combined file the whole way through, instead of switching
    as the file-wide progress crosses each episode's boundary (e.g. a
    3-episode file: ep1 for the first third, ep2 for the next, ep3 for the
    rest), each with its own 0->100% segment progress."""

    def test_single_episode_is_a_pass_through(self):
        media = [SimpleNamespace(id=1)]
        episode, pct, secs = _episode_for_progress(media, 0.42, 600)
        self.assertIs(episode, media[0])
        self.assertAlmostEqual(pct, 0.42)
        self.assertEqual(secs, 600)

    def test_two_episodes_first_half_stays_on_episode_one(self):
        media = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
        episode, pct, secs = _episode_for_progress(media, 0.25, 300)
        self.assertIs(episode, media[0])
        self.assertAlmostEqual(pct, 0.5)  # 25% of the file = 50% into ep1's half

    def test_two_episodes_right_at_the_boundary_switches_to_episode_two(self):
        media = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
        episode, pct, secs = _episode_for_progress(media, 0.5, 660)
        self.assertIs(episode, media[1])
        self.assertAlmostEqual(pct, 0.0)
        self.assertEqual(secs, 0)

    def test_three_episodes_crossing_the_first_third_boundary(self):
        media = [SimpleNamespace(id=1), SimpleNamespace(id=2), SimpleNamespace(id=3)]
        # Just under a third - still episode 1, nearly done with its segment.
        episode, pct, _ = _episode_for_progress(media, 0.333, 1)
        self.assertIs(episode, media[0])
        self.assertGreater(pct, 0.9)
        # Just past a third - now episode 2, just starting its segment.
        episode, pct, _ = _episode_for_progress(media, 0.34, 1)
        self.assertIs(episode, media[1])
        self.assertLess(pct, 0.1)

    def test_seconds_are_renormalized_to_the_current_episode_segment(self):
        # 44-minute combined file, 22 minutes into episode 2 (75% overall).
        media = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
        episode, pct, secs = _episode_for_progress(media, 0.75, 1980)
        self.assertIs(episode, media[1])
        self.assertAlmostEqual(pct, 0.5)
        self.assertEqual(secs, 660)  # 11 of episode 2's own 22 minutes

    def test_full_progress_clamps_to_the_last_episode(self):
        media = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
        episode, pct, _ = _episode_for_progress(media, 1.0, 1320)
        self.assertIs(episode, media[1])
        self.assertAlmostEqual(pct, 1.0)

    def test_zero_progress_is_safe(self):
        media = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
        episode, pct, secs = _episode_for_progress(media, 0.0, 0)
        self.assertIs(episode, media[0])
        self.assertEqual(pct, 0.0)
        self.assertEqual(secs, 0)

    def test_none_progress_is_safe(self):
        media = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
        episode, pct, secs = _episode_for_progress(media, None, 0)
        self.assertIs(episode, media[0])
        self.assertEqual(pct, 0.0)
        self.assertEqual(secs, 0)


class TestBingebaseScrobble(unittest.IsolatedAsyncioTestCase):
    async def test_maybe_bingebase_scrobble_disabled(self):
        settings = SimpleNamespace(bingebase_scrobble=False, bingebase_webhook_url="https://bingebase.com/api/webhook")
        media = SimpleNamespace(media_type="movie", title="Test", tmdb_id=550, imdb_id=None)
        with patch("httpx.AsyncClient.post") as mock_post:
            await _maybe_bingebase_scrobble(settings, media, "start", 0.5)
            mock_post.assert_not_called()

    async def test_maybe_bingebase_scrobble_enabled(self):
        settings = SimpleNamespace(
            bingebase_scrobble=True,
            bingebase_webhook_url="https://bingebase.com/api/webhook",
            bingebase_api_key="secret-token"
        )
        media = SimpleNamespace(media_type="movie", title="Fight Club", tmdb_id=550, imdb_id="tt0137523")
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            await _maybe_bingebase_scrobble(settings, media, "stop", 0.95)
            mock_post.assert_called_once()
            args, kwargs = mock_post.call_args
            self.assertEqual(args[0], "https://bingebase.com/api/webhook")
            self.assertEqual(kwargs["json"]["Event"], "playback.stop")
            self.assertEqual(kwargs["json"]["Item"]["ProviderIds"]["Tmdb"], "550")
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret-token")


class SimklScrobbleFallbackTests(unittest.IsolatedAsyncioTestCase):
    """#328: a Simkl /scrobble/stop that 404s (absolute-numbered anime past
    the first cour) must not silently lose the watch - fall back to
    /sync/history, which maps TMDB numbering onto Simkl's split entries."""

    def _settings(self):
        return SimpleNamespace(
            simkl_scrobble=True, simkl_access_token="tok", simkl_client_id="cid",
        )

    def _episode(self):
        return SimpleNamespace(
            media_type=MediaType.episode, season_number=1, episode_number=25,
            tmdb_id=4562708, show_id=1,
            show=SimpleNamespace(tmdb_id=95479, title="JUJUTSU KAISEN"),
        )

    async def test_successful_stop_does_not_touch_history(self):
        with (
            patch.object(webhooks.simkl_client, "stop_scrobble_episode", AsyncMock()),
            patch.object(webhooks.simkl_client, "add_episode_to_history", AsyncMock()) as add_hist,
        ):
            await _maybe_simkl_scrobble(self._settings(), self._episode(), "stop", 1.0)
        add_hist.assert_not_awaited()

    async def test_failed_stop_at_watched_progress_falls_back_to_history(self):
        with (
            patch.object(webhooks.simkl_client, "stop_scrobble_episode",
                         AsyncMock(side_effect=RuntimeError("404 Not Found"))),
            patch.object(webhooks.simkl_client, "add_episode_to_history", AsyncMock()) as add_hist,
        ):
            await _maybe_simkl_scrobble(self._settings(), self._episode(), "stop", 1.0)
        add_hist.assert_awaited_once_with("cid", "tok", 95479, 1, 25)

    async def test_failed_stop_below_watched_progress_does_not_fall_back(self):
        with (
            patch.object(webhooks.simkl_client, "stop_scrobble_episode",
                         AsyncMock(side_effect=RuntimeError("404"))),
            patch.object(webhooks.simkl_client, "add_episode_to_history", AsyncMock()) as add_hist,
        ):
            await _maybe_simkl_scrobble(self._settings(), self._episode(), "stop", 0.30)
        add_hist.assert_not_awaited()

    async def test_failed_start_does_not_fall_back(self):
        with (
            patch.object(webhooks.simkl_client, "checkin_episode",
                         AsyncMock(side_effect=RuntimeError("404"))),
            patch.object(webhooks.simkl_client, "add_episode_to_history", AsyncMock()) as add_hist,
        ):
            await _maybe_simkl_scrobble(self._settings(), self._episode(), "start", 0.05)
        add_hist.assert_not_awaited()

    async def test_failed_movie_stop_falls_back_to_movie_history(self):
        movie = SimpleNamespace(
            media_type=MediaType.movie, tmdb_id=550, title="Fight Club", release_date="1999-10-15",
        )
        with (
            patch.object(webhooks.simkl_client, "stop_scrobble_movie",
                         AsyncMock(side_effect=RuntimeError("500"))),
            patch.object(webhooks.simkl_client, "add_movie_to_history", AsyncMock()) as add_hist,
        ):
            await _maybe_simkl_scrobble(self._settings(), movie, "stop", 0.95)
        add_hist.assert_awaited_once_with("cid", "tok", 550)

    async def test_disabled_does_nothing(self):
        s = SimpleNamespace(simkl_scrobble=False, simkl_access_token="tok", simkl_client_id="cid")
        with patch.object(webhooks.simkl_client, "stop_scrobble_episode", AsyncMock()) as stop:
            await _maybe_simkl_scrobble(s, self._episode(), "stop", 1.0)
        stop.assert_not_awaited()

    async def test_history_fallback_that_simkl_also_rejects_is_swallowed(self):
        # #328 follow-up: /sync/history resolves the tmdb id to Simkl's own
        # layout too, so the season-split mismatch comes back as not_found
        # inside a 201. add_episode_to_history now raises on that; the webhook
        # must log it, not crash and not claim success.
        with (
            patch.object(webhooks.simkl_client, "stop_scrobble_episode",
                         AsyncMock(side_effect=RuntimeError("404 Not Found"))),
            patch.object(webhooks.simkl_client, "add_episode_to_history",
                         AsyncMock(side_effect=webhooks.simkl_client.SimklHistoryRejected("not_found"))),
        ):
            with self.assertLogs("routers.webhooks", level="WARNING") as logs:
                await _maybe_simkl_scrobble(self._settings(), self._episode(), "stop", 1.0)
        self.assertTrue(any("fallback also failed" in m for m in logs.output))


class BackfillPlexRuntimeTests(IsolatedAsyncioTestCase):
    """Regression tests for #169: the Now Playing bar's live progress
    interpolation never engages while Media.runtime is unset, freezing the
    bar at a flat percentage. Some Plex clients under-report duration_ms on
    their first play/resume event, so _backfill_plex_runtime tries, in
    order: the current event's own duration_ms, asking Plex directly for the
    item, then TMDB - so any event (not just play/resume) can self-heal it."""

    def _movie(self, **overrides):
        defaults = dict(runtime=None, media_type=MediaType.movie, tmdb_id=550, show_id=None, season_number=None, episode_number=None)
        return SimpleNamespace(**{**defaults, **overrides})

    def _episode(self, **overrides):
        defaults = dict(runtime=None, media_type=MediaType.episode, tmdb_id=None, show_id=1, season_number=1, episode_number=1)
        return SimpleNamespace(**{**defaults, **overrides})

    async def test_noop_when_runtime_already_set(self) -> None:
        media = self._movie(runtime=42)
        db = _FakeDB([])
        with patch("core.plex.get_item", new_callable=AsyncMock) as mock_get_item:
            await _backfill_plex_runtime(db, media, {"duration_ms": 999999}, SimpleNamespace(url="u", token="t"), "tmdb-key")
        self.assertEqual(media.runtime, 42)
        mock_get_item.assert_not_called()

    async def test_uses_current_event_duration_ms_first(self) -> None:
        media = self._movie()
        db = _FakeDB([])
        with patch("core.plex.get_item", new_callable=AsyncMock) as mock_get_item:
            await _backfill_plex_runtime(db, media, {"duration_ms": 5_400_000}, SimpleNamespace(url="u", token="t"), "tmdb-key")
        self.assertEqual(media.runtime, 90)
        mock_get_item.assert_not_called()

    async def test_asks_plex_directly_when_event_duration_missing(self) -> None:
        media = self._movie()
        db = _FakeDB([])
        conn = SimpleNamespace(url="http://plex.local", token="plex-token")
        with patch("core.plex.get_item", new_callable=AsyncMock, return_value={"duration": 3_600_000}) as mock_get_item:
            await _backfill_plex_runtime(db, media, {"duration_ms": 0, "plex_rating_key": "123"}, conn, "tmdb-key")
        mock_get_item.assert_awaited_once_with("http://plex.local", "plex-token", "123")
        self.assertEqual(media.runtime, 60)

    async def test_falls_back_to_tmdb_movie_when_plex_unavailable(self) -> None:
        media = self._movie(tmdb_id=550)
        db = _FakeDB([])
        with patch("core.tmdb.get_movie", new_callable=AsyncMock, return_value={"runtime": 139}) as mock_get_movie:
            await _backfill_plex_runtime(db, media, {"duration_ms": 0}, None, "tmdb-key")
        mock_get_movie.assert_awaited_once_with(550, api_key="tmdb-key")
        self.assertEqual(media.runtime, 139)

    async def test_falls_back_to_tmdb_episode_via_show_lookup(self) -> None:
        media = self._episode(show_id=7, season_number=2, episode_number=3)
        show = SimpleNamespace(id=7, tmdb_id=999)
        db = _FakeDB([show])
        with patch("core.tmdb.get_episode", new_callable=AsyncMock, return_value={"runtime": 45}) as mock_get_episode:
            await _backfill_plex_runtime(db, media, {"duration_ms": 0}, None, "tmdb-key")
        mock_get_episode.assert_awaited_once_with(999, 2, 3, api_key="tmdb-key")
        self.assertEqual(media.runtime, 45)

    async def test_leaves_runtime_none_when_every_source_fails(self) -> None:
        media = self._movie(tmdb_id=None)
        db = _FakeDB([])
        await _backfill_plex_runtime(db, media, {"duration_ms": 0}, None, "tmdb-key")
        self.assertIsNone(media.runtime)

    async def test_no_conn_skips_plex_and_goes_straight_to_tmdb(self) -> None:
        # Scrobble-only Plex connections have no url/token to call Plex with -
        # conn=None must not raise, and should fall through to TMDB.
        media = self._movie(tmdb_id=550)
        db = _FakeDB([])
        with patch("core.plex.get_item", new_callable=AsyncMock) as mock_get_item, \
             patch("core.tmdb.get_movie", new_callable=AsyncMock, return_value={"runtime": 120}):
            await _backfill_plex_runtime(db, media, {"duration_ms": 0, "plex_rating_key": "123"}, None, "tmdb-key")
        mock_get_item.assert_not_called()
        self.assertEqual(media.runtime, 120)

    async def test_connection_with_no_url_or_token_skips_plex_without_crashing(self) -> None:
        # A webhook-only user may have a MediaServerConnection row that was
        # never fully filled in (url/token blank) - must behave the same as
        # conn=None, not attempt the call and definitely not raise.
        media = self._movie(tmdb_id=550)
        db = _FakeDB([])
        conn = SimpleNamespace(url="", token="")
        with patch("core.plex.get_item", new_callable=AsyncMock) as mock_get_item, \
             patch("core.tmdb.get_movie", new_callable=AsyncMock, return_value={"runtime": 120}):
            await _backfill_plex_runtime(db, media, {"duration_ms": 0, "plex_rating_key": "123"}, conn, "tmdb-key")
        mock_get_item.assert_not_called()
        self.assertEqual(media.runtime, 120)

    async def test_plex_lookup_raising_does_not_crash_and_falls_back_to_tmdb(self) -> None:
        # A multi-server user's webhook can arrive from a Plex server other
        # than the one configured in Scrob - that server won't have this
        # item, so the lookup can fail in ways get_item's own try/except
        # might not anticipate. Must not propagate.
        media = self._movie(tmdb_id=550)
        db = _FakeDB([])
        conn = SimpleNamespace(url="http://wrong-server.local", token="tok")
        with patch("core.plex.get_item", new_callable=AsyncMock, side_effect=RuntimeError("boom")), \
             patch("core.tmdb.get_movie", new_callable=AsyncMock, return_value={"runtime": 120}):
            await _backfill_plex_runtime(db, media, {"duration_ms": 0, "plex_rating_key": "123"}, conn, "tmdb-key")
        self.assertEqual(media.runtime, 120)

    async def test_tmdb_failure_does_not_crash_and_leaves_runtime_none(self) -> None:
        media = self._movie(tmdb_id=550)
        db = _FakeDB([])
        with patch("core.tmdb.get_movie", new_callable=AsyncMock, side_effect=Exception("network error")):
            await _backfill_plex_runtime(db, media, {"duration_ms": 0}, None, "tmdb-key")
        self.assertIsNone(media.runtime)

    async def test_malformed_duration_does_not_crash(self) -> None:
        media = self._movie()
        db = _FakeDB([])
        # No tmdb_key, so this isolates the malformed-duration path itself
        # rather than also exercising the TMDB fallback that follows it.
        await _backfill_plex_runtime(db, media, {"duration_ms": "not-a-number"}, None, None)
        self.assertIsNone(media.runtime)


class ParseJellyfinRuntimeTicksTests(unittest.TestCase):
    """#383: the parser now surfaces RunTimeTicks so the handler can backfill
    Media.runtime from it, instead of only using it for the progress ratio."""

    _MIN = 600_000_000  # RunTimeTicks per minute

    def test_nested_payload_carries_runtime_ticks(self):
        data = parse_jellyfin_payload({
            "Event": "playback.progress",
            "Item": {"Id": "m1", "Name": "Heat", "Type": "Movie", "RunTimeTicks": 170 * self._MIN},
            "Session": {"Id": "s", "PlayState": {"PositionTicks": 0}},
        })
        self.assertEqual(data["runtime_ticks"], 170 * self._MIN)

    def test_flat_payload_carries_runtime_ticks(self):
        data = parse_jellyfin_payload({
            "NotificationType": "PlaybackProgress", "ItemType": "Episode", "ItemId": "e1",
            "Name": "Ep", "SeasonNumber": 1, "EpisodeNumber": 1, "RunTimeTicks": 22 * self._MIN,
        })
        self.assertEqual(data["runtime_ticks"], 22 * self._MIN)

    def test_absent_runtime_ticks_is_none(self):
        data = parse_jellyfin_payload({
            "NotificationType": "PlaybackStop", "ItemType": "Movie", "ItemId": "m2", "Name": "x",
        })
        self.assertIsNone(data["runtime_ticks"])


class BackfillJellyfinRuntimesTests(IsolatedAsyncioTestCase):
    """#383: Jellyfin/Emby used RunTimeTicks only for the progress ratio and
    never wrote Media.runtime, so an item enriched before #169 kept the
    column NULL however many times it was replayed - freezing the Now
    Playing bar. Mirrors what Plex already does."""

    _MIN = 600_000_000

    def _movie(self, **overrides):
        defaults = dict(runtime=None, media_type=MediaType.movie, tmdb_id=550,
                        show_id=None, season_number=None, episode_number=None)
        return SimpleNamespace(**{**defaults, **overrides})

    def _episode(self, **overrides):
        defaults = dict(runtime=None, media_type=MediaType.episode, tmdb_id=None,
                        show_id=1, season_number=2, episode_number=3)
        return SimpleNamespace(**{**defaults, **overrides})

    async def test_fills_from_runtime_ticks_and_commits(self):
        media = self._movie()
        db = _FakeDB([])
        await _backfill_jellyfin_runtimes(db, [media], {"runtime_ticks": 53 * self._MIN}, "tmdb-key")
        self.assertEqual(media.runtime, 53)
        self.assertEqual(db.commits, 1)

    async def test_noop_when_runtime_already_set(self):
        media = self._movie(runtime=90)
        db = _FakeDB([])
        with patch("core.tmdb.get_movie", new_callable=AsyncMock) as get_movie:
            await _backfill_jellyfin_runtimes(db, [media], {"runtime_ticks": 10 * self._MIN}, "tmdb-key")
        self.assertEqual(media.runtime, 90)
        self.assertEqual(db.commits, 0)
        get_movie.assert_not_called()

    async def test_falls_back_to_tmdb_when_no_ticks(self):
        media = self._episode(show_id=7)
        show = SimpleNamespace(id=7, tmdb_id=999)
        db = _FakeDB([show])
        with patch("core.tmdb.get_episode", new_callable=AsyncMock, return_value={"runtime": 45}) as get_episode:
            await _backfill_jellyfin_runtimes(db, [media], {"runtime_ticks": None}, "tmdb-key")
        get_episode.assert_awaited_once_with(999, 2, 3, api_key="tmdb-key")
        self.assertEqual(media.runtime, 45)

    async def test_multi_episode_file_fills_every_row(self):
        a, b = self._episode(id=1, episode_number=3), self._episode(id=2, episode_number=4)
        db = _FakeDB([])
        await _backfill_jellyfin_runtimes(db, [a, b], {"runtime_ticks": 44 * self._MIN}, "tmdb-key")
        # Each row of a combined file gets the file duration (the Now Playing
        # bar divides it across episodes at display time), same as Plex.
        self.assertEqual((a.runtime, b.runtime), (44, 44))
        self.assertEqual(db.commits, 1)

    async def test_only_the_missing_rows_are_touched(self):
        got, missing = self._episode(id=1, runtime=30), self._episode(id=2, runtime=None)
        db = _FakeDB([])
        await _backfill_jellyfin_runtimes(db, [got, missing], {"runtime_ticks": 22 * self._MIN}, "tmdb-key")
        self.assertEqual((got.runtime, missing.runtime), (30, 22))

    async def test_nothing_resolvable_leaves_runtime_none_without_committing(self):
        media = self._movie(tmdb_id=None)
        db = _FakeDB([])
        await _backfill_jellyfin_runtimes(db, [media], {"runtime_ticks": None}, "tmdb-key")
        self.assertIsNone(media.runtime)
        self.assertEqual(db.commits, 0)


class ResolvePlexProgressTests(IsolatedAsyncioTestCase):
    """Plex's play/resume/stop webhook can fire with viewOffset stuck at 0
    (see #322's Now Playing bar starting a resume over at 0%, and its
    Continue Watching entry for a mid-episode stop never being written) -
    _resolve_plex_progress should trust a non-zero webhook value as-is, and
    only reach out to Plex directly when that value is suspiciously 0."""

    async def test_trusts_a_nonzero_webhook_value_without_calling_plex(self) -> None:
        data = {"progress_percent": 0.42, "progress_seconds": 500}
        with patch("core.plex.get_item", new_callable=AsyncMock) as mock_get_item:
            percent, seconds = await _resolve_plex_progress(data, SimpleNamespace(url="u", token="t"))
        mock_get_item.assert_not_called()
        self.assertEqual((percent, seconds), (0.42, 500))

    async def test_falls_back_to_plex_item_lookup_when_webhook_reports_zero(self) -> None:
        data = {"progress_percent": 0.0, "progress_seconds": 0, "plex_rating_key": "57214"}
        conn = SimpleNamespace(url="http://plex.local", token="plex-token")
        with patch(
            "core.plex.get_item", new_callable=AsyncMock,
            return_value={"viewOffset": 3_758_054, "duration": 5_567_274},
        ) as mock_get_item:
            percent, seconds = await _resolve_plex_progress(data, conn)
        mock_get_item.assert_awaited_once_with("http://plex.local", "plex-token", "57214")
        self.assertAlmostEqual(percent, 0.6750, places=4)
        self.assertEqual(seconds, 3758)

    async def test_plex_lookup_also_zero_returns_the_webhook_zero(self) -> None:
        data = {"progress_percent": 0.0, "progress_seconds": 0, "plex_rating_key": "57214"}
        conn = SimpleNamespace(url="http://plex.local", token="plex-token")
        with patch(
            "core.plex.get_item", new_callable=AsyncMock,
            return_value={"viewOffset": 0, "duration": 5_567_274},
        ):
            percent, seconds = await _resolve_plex_progress(data, conn)
        self.assertEqual((percent, seconds), (0.0, 0))

    async def test_no_conn_skips_plex_and_returns_the_webhook_zero(self) -> None:
        data = {"progress_percent": 0.0, "progress_seconds": 0, "plex_rating_key": "57214"}
        with patch("core.plex.get_item", new_callable=AsyncMock) as mock_get_item:
            percent, seconds = await _resolve_plex_progress(data, None)
        mock_get_item.assert_not_called()
        self.assertEqual((percent, seconds), (0.0, 0))

    async def test_plex_lookup_raising_does_not_crash(self) -> None:
        data = {"progress_percent": 0.0, "progress_seconds": 0, "plex_rating_key": "57214"}
        conn = SimpleNamespace(url="http://plex.local", token="plex-token")
        with patch("core.plex.get_item", new_callable=AsyncMock, side_effect=RuntimeError("boom")):
            percent, seconds = await _resolve_plex_progress(data, conn)
        self.assertEqual((percent, seconds), (0.0, 0))


class BackfillCreditsStingersTests(IsolatedAsyncioTestCase):
    """#319 - a movie enriched before the credits-stinger badge shipped has
    no has_mid_credits_scene/has_post_credits_scene keys in tmdb_data yet, so
    the Now Playing bar's badge silently never showed for it. Playing it
    again should self-heal those keys in, once, without re-fetching on every
    subsequent event."""

    def _movie(self, **overrides):
        defaults = dict(id=1, media_type=MediaType.movie, tmdb_id=550, tmdb_data={})
        return SimpleNamespace(**{**defaults, **overrides})

    async def test_backfills_missing_flags_from_tmdb(self) -> None:
        media = self._movie(tmdb_data={"runtime": 139})
        db = _FakeDB([])
        tmdb_data = {"keywords": {"keywords": [{"id": 1, "name": "aftercreditsstinger"}]}}
        with patch("core.tmdb.get_movie", AsyncMock(return_value=tmdb_data)) as mock_get_movie:
            await _backfill_credits_stingers(db, media, "tmdb-key")
        mock_get_movie.assert_awaited_once_with(550, api_key="tmdb-key")
        self.assertEqual(media.tmdb_data["runtime"], 139)
        self.assertFalse(media.tmdb_data["has_mid_credits_scene"])
        self.assertTrue(media.tmdb_data["has_post_credits_scene"])

    async def test_already_backfilled_does_not_call_tmdb_again(self) -> None:
        media = self._movie(tmdb_data={"has_mid_credits_scene": False, "has_post_credits_scene": True})
        db = _FakeDB([])
        with patch("core.tmdb.get_movie", new_callable=AsyncMock) as mock_get_movie:
            await _backfill_credits_stingers(db, media, "tmdb-key")
        mock_get_movie.assert_not_called()

    async def test_episode_is_skipped(self) -> None:
        media = SimpleNamespace(id=1, media_type=MediaType.episode, tmdb_id=550, tmdb_data={})
        db = _FakeDB([])
        with patch("core.tmdb.get_movie", new_callable=AsyncMock) as mock_get_movie:
            await _backfill_credits_stingers(db, media, "tmdb-key")
        mock_get_movie.assert_not_called()

    async def test_no_tmdb_key_is_skipped(self) -> None:
        media = self._movie()
        db = _FakeDB([])
        with patch("core.tmdb.get_movie", new_callable=AsyncMock) as mock_get_movie:
            await _backfill_credits_stingers(db, media, None)
        mock_get_movie.assert_not_called()

    async def test_tmdb_failure_does_not_crash(self) -> None:
        media = self._movie()
        db = _FakeDB([])
        with patch("core.tmdb.get_movie", new_callable=AsyncMock, side_effect=RuntimeError("boom")):
            await _backfill_credits_stingers(db, media, "tmdb-key")
        self.assertNotIn("has_mid_credits_scene", media.tmdb_data)


class ParseKodiPayloadTests(unittest.TestCase):
    @staticmethod
    def _hms(seconds: int) -> dict:
        return {"hours": seconds // 3600, "minutes": (seconds % 3600) // 60, "seconds": seconds % 60}

    def _stop_payload(self, *, position: int, total: int, end: bool) -> dict:
        return {
            "method": "Player.OnStop",
            "item": {"type": "movie", "title": "The Matrix", "year": 1999,
                     "uniqueid": {"tmdb": "603"}, "id": 7},
            "player": {"time": self._hms(position), "totaltime": self._hms(total)},
            "params": {"data": {"end": end}},
        }

    def test_unknown_method_is_ignored(self):
        self.assertIsNone(parse_kodi_payload({"method": "System.OnWake"}))

    def test_music_item_is_ignored(self):
        payload = {"method": "Player.OnPlay", "item": {"type": "song", "title": "x"}}
        self.assertIsNone(parse_kodi_payload(payload))

    def test_play_maps_and_reads_ids(self):
        payload = {
            "method": "Player.OnPlay",
            "item": {"type": "movie", "title": "The Matrix", "year": 1999,
                     "uniqueid": {"tmdb": "603", "imdb": "tt0133093"}},
            "player": {"time": self._hms(0), "totaltime": self._hms(8160)},
        }
        data = parse_kodi_payload(payload)
        self.assertEqual(data["notification_type"], "play")
        self.assertEqual(data["media_type"], "movie")
        self.assertEqual(data["tmdb_id"], "603")
        self.assertEqual(data["imdb_id"], "tt0133093")

    def test_episode_carries_series_and_numbers(self):
        payload = {
            "method": "Player.OnPlay",
            "item": {"type": "episode", "title": "Pilot", "showtitle": "Lost",
                     "season": 1, "episode": 1, "uniqueid": {}},
            "player": {"time": self._hms(60), "totaltime": self._hms(2520)},
        }
        data = parse_kodi_payload(payload)
        self.assertEqual(data["media_type"], "episode")
        self.assertEqual(data["series_name"], "Lost")
        self.assertEqual((data["season_number"], data["episode_number"]), (1, 1))

    def test_stop_near_end_reports_high_progress_even_without_end_flag(self):
        # Issue #2: stopping in the credits (end flag false) should still land
        # as a >= 90% watch so the stop handler marks it completed.
        data = parse_kodi_payload(self._stop_payload(position=7350, total=7680, end=False))
        self.assertFalse(data["ended"])
        self.assertGreaterEqual(data["progress_percent"], 0.90)

    def test_stop_with_end_flag_sets_ended(self):
        data = parse_kodi_payload(self._stop_payload(position=0, total=7680, end=True))
        self.assertTrue(data["ended"])

    def test_stop_early_reports_low_progress(self):
        data = parse_kodi_payload(self._stop_payload(position=120, total=7680, end=False))
        self.assertLess(data["progress_percent"], 0.05)

    def test_total_length_is_kept_for_the_runtime_backfill(self):
        data = parse_kodi_payload(self._stop_payload(position=120, total=7680, end=False))
        self.assertEqual(data["total_seconds"], 7680)

    def test_unknown_total_length_is_none(self):
        payload = {"method": "Player.OnPlay", "item": {"type": "movie", "title": "x", "uniqueid": {"tmdb": "603"}}}
        self.assertIsNone(parse_kodi_payload(payload)["total_seconds"])

    def test_synthetic_mark_watched_payload_is_complete(self):
        # Shape the add-on POSTs for a "mark as watched" (time == totaltime).
        data = parse_kodi_payload(self._stop_payload(position=7680, total=7680, end=True))
        self.assertTrue(data["ended"])
        self.assertEqual(data["progress_percent"], 1.0)
        self.assertEqual(data["session_id"], "7")


class BackfillKodiRuntimeTests(IsolatedAsyncioTestCase):
    """The Kodi-style webhook is the one external players use (Scrob's Kodi
    add-on, the Silo watch-provider plugin). Like the Plex and Jellyfin
    paths, it must fill a missing Media.runtime from the event's own total
    length, then TMDB, or the Now Playing bar's live progress never engages
    for a title TMDB has no runtime for yet (#383)."""

    def _movie(self, **overrides):
        defaults = dict(runtime=None, media_type=MediaType.movie, tmdb_id=550, show_id=None, season_number=None, episode_number=None)
        return SimpleNamespace(**{**defaults, **overrides})

    async def test_noop_when_runtime_already_set(self) -> None:
        media = self._movie(runtime=42)
        db = _FakeDB([])
        with patch("core.tmdb.get_movie", new_callable=AsyncMock) as mock_get_movie:
            await _backfill_kodi_runtime(db, media, {"total_seconds": 5400}, "tmdb-key")
        self.assertEqual(media.runtime, 42)
        self.assertEqual(db.commits, 0)
        mock_get_movie.assert_not_called()

    async def test_uses_the_event_total_length_first(self) -> None:
        media = self._movie()
        db = _FakeDB([])
        with patch("core.tmdb.get_movie", new_callable=AsyncMock) as mock_get_movie:
            await _backfill_kodi_runtime(db, media, {"total_seconds": 3187}, "tmdb-key")
        self.assertEqual(media.runtime, 53)
        self.assertEqual(db.commits, 1)
        mock_get_movie.assert_not_called()

    async def test_falls_back_to_tmdb_when_the_event_has_no_length(self) -> None:
        media = self._movie(tmdb_id=550)
        db = _FakeDB([])
        with patch("core.tmdb.get_movie", new_callable=AsyncMock, return_value={"runtime": 139}) as mock_get_movie:
            await _backfill_kodi_runtime(db, media, {"total_seconds": None}, "tmdb-key")
        mock_get_movie.assert_awaited_once_with(550, api_key="tmdb-key")
        self.assertEqual(media.runtime, 139)

    async def test_leaves_runtime_none_when_every_source_fails(self) -> None:
        media = self._movie(tmdb_id=None)
        db = _FakeDB([])
        await _backfill_kodi_runtime(db, media, {"total_seconds": 0}, "tmdb-key")
        self.assertIsNone(media.runtime)
        self.assertEqual(db.commits, 0)


class FindOrCreateShowRaceTests(IsolatedAsyncioTestCase):
    """#446: one media server feeding two Scrob accounts delivers the same
    webhook to both at once, so both decide a brand-new show is missing and
    both insert it. The loser must get the winner's row back, not a failed
    session and a 500 (which dropped the event for that user)."""

    SHOW = {"name": "Deadwood", "external_ids": {}, "seasons": []}

    async def test_creates_the_show_inside_a_savepoint(self) -> None:
        db = _OpenSessionFakeDB([None])
        with patch("routers.webhooks.tmdb.get_show", AsyncMock(return_value=self.SHOW)):
            show = await webhooks._find_or_create_show(db, 1406, "k")
        self.assertEqual(show.tmdb_id, 1406)
        # add() happens inside the savepoint, never before it.
        self.assertEqual(db.events, ["begin_nested", "add"])

    async def test_loser_of_the_insert_race_gets_the_winners_row(self) -> None:
        winner = SimpleNamespace(id=7, tmdb_id=1406)
        db = _OpenSessionFakeDB([None, winner], flush_raises=IntegrityError("insert", {}, Exception("dup tmdb_id")))
        with patch("routers.webhooks.tmdb.get_show", AsyncMock(return_value=self.SHOW)):
            show = await webhooks._find_or_create_show(db, 1406, "k")
        self.assertIs(show, winner)

    async def test_an_integrity_error_with_no_winning_row_still_raises(self) -> None:
        # e.g. a tvdb_id clash: nothing exists for this tmdb_id to fall back to.
        db = _OpenSessionFakeDB([None, None], flush_raises=IntegrityError("insert", {}, Exception("dup tvdb_id")))
        with patch("routers.webhooks.tmdb.get_show", AsyncMock(return_value=self.SHOW)):
            with self.assertRaises(IntegrityError):
                await webhooks._find_or_create_show(db, 1406, "k")


class FindOrCreateMediaKodiShowIdTests(IsolatedAsyncioTestCase):
    """Kodi puts the *show* TMDB id in an episode's uniqueid whenever its
    scraper has no episode-level id, and add-ons forward it as-is. Shows and
    episodes are separate TMDB id spaces that reuse the same numbers, so
    matching that id against Media.tmdb_id can land on a completely unrelated
    episode - show 214546 (Sleepers) collides with episode 214546 (Law &
    Order: SVU S04E10). Worse, that match was tried before the show +
    season/episode lookup, so a payload carrying a perfectly good showtitle
    and S/E still scrobbled the wrong episode."""

    def _episode_data(self, **overrides):
        data = {
            "media_type": "episode",
            "title": "Episode 6",
            "series_name": "Sleepers",
            "season_number": 3,
            "episode_number": 6,
            "tmdb_id": "214546",
        }
        data.update(overrides)
        return data

    async def test_colliding_tmdb_id_falls_through_to_season_episode(self):
        wrong = SimpleNamespace(id=37092, media_type=MediaType.episode, show_id=2,
                                season_number=4, episode_number=10)
        right = SimpleNamespace(id=50001, media_type=MediaType.episode, show_id=1,
                                season_number=3, episode_number=6)
        show = SimpleNamespace(id=1, tmdb_id=214546)
        db = _QueuedResultDB([SimpleNamespace(tmdb_id=214546), wrong, right])

        with patch("routers.webhooks._find_or_create_show", AsyncMock(return_value=show)):
            result = await find_or_create_media_kodi(self._episode_data(), db)

        self.assertIs(result, right)

    async def test_matching_tmdb_id_is_still_accepted(self):
        episode = SimpleNamespace(id=50001, media_type=MediaType.episode, show_id=1,
                                  season_number=3, episode_number=6)
        show = SimpleNamespace(id=1, tmdb_id=999)
        db = _QueuedResultDB([SimpleNamespace(tmdb_id=999), episode])

        with patch("routers.webhooks._find_or_create_show", AsyncMock(return_value=show)):
            result = await find_or_create_media_kodi(self._episode_data(), db)

        self.assertIs(result, episode)

    async def test_show_id_resolves_series_when_payload_has_no_showtitle(self):
        # No showtitle/tvdb/imdb: the only hint is the uniqueid, which is the
        # show's id - use it to resolve the series instead of as an episode id.
        episode = SimpleNamespace(id=50001, media_type=MediaType.episode, show_id=1,
                                  season_number=3, episode_number=6)
        show = SimpleNamespace(id=1, tmdb_id=214546)
        db = _QueuedResultDB([show, None, episode])

        with patch("routers.webhooks._find_or_create_show", AsyncMock()) as find_show:
            result = await find_or_create_media_kodi(self._episode_data(series_name=None), db)

        self.assertIs(result, episode)
        # The local Show row is already in hand from the candidate-id lookup
        # above - calling _find_or_create_show would just repeat that exact
        # query for nothing.
        find_show.assert_not_awaited()

    async def test_unverified_show_id_is_not_stored_on_a_new_episode(self):
        show = SimpleNamespace(id=1, tmdb_id=214546)
        created = SimpleNamespace(id=60001, media_type=MediaType.episode, show_id=1,
                                  season_number=3, episode_number=6)
        db = _QueuedResultDB([SimpleNamespace(tmdb_id=214546), None, None])

        with patch("routers.webhooks._find_or_create_show", AsyncMock(return_value=show)),              patch("routers.webhooks._resolve_tvdb_fallback", AsyncMock(return_value=(None, None, None))),              patch("routers.webhooks.enrich_media_safely", AsyncMock(return_value=created)),              patch("routers.webhooks.create_media_safely",
                   AsyncMock(return_value=(created, True))) as create_mock:
            await find_or_create_media_kodi(self._episode_data(), db)

        self.assertIsNone(create_mock.await_args.args[1])

    async def test_unresolved_show_dedups_by_title_season_episode_before_creating(self):
        # No showtitle/tvdb/imdb and the uniqueid doesn't match any local
        # show - series_tmdb_id/show never resolve, so the row this same
        # episode created on an earlier webhook call has show_id=None and
        # tmdb_id=None (unverified ids are never stored). Without a
        # season/episode dedup lookup here, every later webhook for this
        # episode (pause/resume/stop, a repeat play) would mint a fresh
        # duplicate instead of finding it.
        existing = SimpleNamespace(id=70001, media_type=MediaType.episode, show_id=None,
                                    season_number=3, episode_number=6)
        db = _QueuedResultDB([None, None, existing])

        with patch("routers.webhooks.create_media_safely", AsyncMock()) as create_mock:
            result = await find_or_create_media_kodi(self._episode_data(series_name=None), db)

        self.assertIs(result, existing)
        create_mock.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()


class JellyfinEventUserFilterTests(unittest.TestCase):
    """#405: a Jellyfin/Emby webhook raised by another server user must not be
    attributed to this connection's Scrob account."""

    def test_flat_and_nested_payloads_expose_the_server_user_id(self):
        from routers.webhooks import parse_jellyfin_payload
        flat = parse_jellyfin_payload({"NotificationType": "PlaybackStop", "ItemType": "Movie", "UserId": "abc"})
        nested = parse_jellyfin_payload({
            "NotificationType": "PlaybackStop",
            "Item": {"Type": "Movie", "Id": "1"},
            "Session": {"UserId": "def"},
        })
        self.assertEqual(flat["server_user_id"], "abc")
        self.assertEqual(nested["server_user_id"], "def")

    def test_other_user_is_detected_ignoring_dashes_and_case(self):
        from routers.webhooks import _jellyfin_event_for_other_user
        conn = SimpleNamespace(server_user_id="AAAAAAAA-1111-2222-3333-444444444444")
        self.assertFalse(_jellyfin_event_for_other_user({"server_user_id": "aaaaaaaa111122223333444444444444"}, conn))
        self.assertTrue(_jellyfin_event_for_other_user({"server_user_id": "bbbbbbbb111122223333444444444444"}, conn))

    def test_missing_id_on_either_side_is_accepted(self):
        from routers.webhooks import _jellyfin_event_for_other_user
        self.assertFalse(_jellyfin_event_for_other_user({"server_user_id": None}, SimpleNamespace(server_user_id="x")))
        self.assertFalse(_jellyfin_event_for_other_user({"server_user_id": "x"}, SimpleNamespace(server_user_id=None)))
        self.assertFalse(_jellyfin_event_for_other_user({"server_user_id": "x"}, None))

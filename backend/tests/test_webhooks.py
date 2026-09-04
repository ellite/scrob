import os
import unittest
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from sqlalchemy.orm.exc import StaleDataError

from models.base import MediaType
from routers import webhooks
from routers.webhooks import (
    _backfill_credits_stingers,
    _backfill_plex_runtime,
    _commit_playback_session_update,
    _consume_recently_pushed_watched,
    _ensure_collection_entry,
    _episode_for_progress,
    _is_duplicate_webhook_delivery,
    _maybe_bingebase_scrobble,
    _maybe_simkl_scrobble,
    _resolve_plex_progress,
    _resolve_tvdb_episode_to_tmdb_position,
    _translate_plex_tvdb_episode_position,
    _write_watch_event,
    find_or_create_media_jellyfin,
    find_or_create_media_jellyfin_multi,
    find_or_create_media_kodi,
    mark_pushed_watched,
    parse_jellyfin_payload,
    parse_kodi_payload,
)


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    """Fakes just enough of AsyncSession for _write_watch_event: every
    execute() call returns the next queued scalar_one_or_none() value, and
    add() is recorded so tests can assert whether a WatchEvent was created."""

    def __init__(self, queued_scalars):
        self._queued = list(queued_scalars)
        self.added = []

    async def execute(self, stmt):
        value = self._queued.pop(0) if self._queued else None
        return _ScalarResult(value)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


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
        await _write_watch_event(db, user_id=1, media_id=2, progress_percent=1.0, progress_seconds=120, completed=True)
        self.assertEqual(len(db.added), 1)

    async def test_second_completed_event_for_same_media_within_window_is_skipped(self):
        # Simulates media.scrobble having already written a WatchEvent moments
        # ago, then media.stop firing for the same viewing.
        db = _FakeDB(queued_scalars=[123])  # a recent WatchEvent id is found
        await _write_watch_event(db, user_id=1, media_id=2, progress_percent=1.0, progress_seconds=120, completed=True)
        self.assertEqual(len(db.added), 0)

    async def test_echo_of_a_just_pushed_mark_watched_is_skipped(self):
        # Regression for #247/#251: pushing "mark watched" to Jellyfin/Emby can
        # echo straight back as a UserDataSaved webhook. The item's real watch
        # event is typically old (imported history), so the 5-minute dedup
        # above never catches it and it used to land as a brand new WatchEvent
        # stamped at push time - even though no recent duplicate is queued here.
        mark_pushed_watched(user_id=1, media_id=2)
        db = _FakeDB(queued_scalars=[None])
        await _write_watch_event(db, user_id=1, media_id=2, progress_percent=1.0, progress_seconds=120, completed=True)
        self.assertEqual(len(db.added), 0)

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


class _CollectionIdResult:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value


class _EnsureCollectionFakeDB:
    """Routes _ensure_collection_entry's queries by SQL text and captures the
    collection_files insert so tests can inspect its bound connection_id."""

    def __init__(self, *, connection_exists: bool):
        self._connection_exists = connection_exists
        self.collection_file_insert = None

    async def execute(self, stmt):
        sql = str(stmt)
        if "media_server_connections" in sql:
            return _ScalarResult(1 if self._connection_exists else None)
        if sql.startswith("SELECT") and "FROM collections" in sql:
            return _CollectionIdResult(7)
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
            patch("routers.webhooks.get_episode_order", AsyncMock(return_value=SimpleNamespace(episode_order="tvdb"))),
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
            patch("routers.webhooks.get_episode_order", AsyncMock(return_value=SimpleNamespace(episode_order="tvdb"))),
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

    async def commit(self):
        if self._commit_side_effect:
            raise self._commit_side_effect

    async def rollback(self):
        self.rollback_called = True


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

    def test_synthetic_mark_watched_payload_is_complete(self):
        # Shape the add-on POSTs for a "mark as watched" (time == totaltime).
        data = parse_kodi_payload(self._stop_payload(position=7680, total=7680, end=True))
        self.assertTrue(data["ended"])
        self.assertEqual(data["progress_percent"], 1.0)
        self.assertEqual(data["session_id"], "7")


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

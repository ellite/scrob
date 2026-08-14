import os
import unittest
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from sqlalchemy.orm.exc import StaleDataError

from routers import webhooks
from routers.webhooks import (
    _commit_playback_session_update,
    _episode_for_progress,
    _is_duplicate_webhook_delivery,
    _write_watch_event,
    find_or_create_media_jellyfin_multi,
    parse_jellyfin_payload,
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


if __name__ == "__main__":
    unittest.main()

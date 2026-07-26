import os
import unittest
from unittest import IsolatedAsyncioTestCase

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from routers import webhooks
from routers.webhooks import _is_duplicate_webhook_delivery, _write_watch_event, parse_jellyfin_payload


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


if __name__ == "__main__":
    unittest.main()

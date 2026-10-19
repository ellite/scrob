import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core.scrobble_threshold import DEFAULT_SCROBBLE_PERCENT, reached_threshold, scrobble_threshold_for
from routers.webhooks import _update_playback_progress


class ScrobbleThresholdForTests(unittest.TestCase):
    def test_missing_connection_uses_default_percent(self) -> None:
        # Arrange
        connection = None

        # Act
        threshold = scrobble_threshold_for(connection)

        # Assert
        self.assertEqual(threshold, DEFAULT_SCROBBLE_PERCENT / 100)

    def test_connection_percent_is_converted_to_fraction(self) -> None:
        # Arrange
        connection = SimpleNamespace(scrobble_percent=80)

        # Act
        threshold = scrobble_threshold_for(connection)

        # Assert
        self.assertEqual(threshold, 0.80)


class ReachedThresholdTests(unittest.TestCase):
    def test_position_at_threshold_counts_as_reached(self) -> None:
        # Arrange
        position, duration, threshold = 75_000, 100_000, 0.75

        # Act
        reached = reached_threshold(position, duration, threshold)

        # Assert
        self.assertTrue(reached)

    def test_position_below_threshold_does_not_count(self) -> None:
        # Arrange
        position, duration, threshold = 74_000, 100_000, 0.75

        # Act
        reached = reached_threshold(position, duration, threshold)

        # Assert
        self.assertFalse(reached)

    def test_zero_duration_never_counts(self) -> None:
        # Arrange
        position, duration, threshold = 10_000, 0, 0.75

        # Act
        reached = reached_threshold(position, duration, threshold)

        # Assert
        self.assertFalse(reached)


class UpdatePlaybackProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_progress_above_ninety_percent_is_kept_as_in_progress(self) -> None:
        # Arrange
        db = MagicMock()
        db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: None))
        db.add = MagicMock()

        # Act
        await _update_playback_progress(db, user_id=1, media_id=2, progress_percent=0.92, progress_seconds=920)

        # Assert
        db.add.assert_called_once()


if __name__ == "__main__":
    unittest.main()

import os
import unittest

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from models.sync import SyncStatus
from routers.sync import _MAX_ERROR_MESSAGE, _short_error


class ShortErrorTests(unittest.TestCase):
    """sync_jobs.error_message is varchar(1000): recording a longer failure
    made the UPDATE itself raise, leaving the job stuck on 'running'."""

    def test_short_message_is_unchanged(self) -> None:
        self.assertEqual(_short_error(ValueError("boom")), "boom")

    def test_long_message_fits_the_column(self) -> None:
        text = _short_error(RuntimeError("x" * 50_000))
        self.assertEqual(len(text), _MAX_ERROR_MESSAGE)
        self.assertTrue(text.endswith("…"))

    def test_message_exactly_at_the_limit_is_unchanged(self) -> None:
        self.assertEqual(_short_error("y" * _MAX_ERROR_MESSAGE), "y" * _MAX_ERROR_MESSAGE)

    def test_accepts_a_plain_string(self) -> None:
        self.assertEqual(_short_error("plain"), "plain")


class SyncStatusTests(unittest.TestCase):
    def test_running_exists_and_in_progress_does_not(self) -> None:
        # run_bingebase_push used SyncStatus.in_progress, which never existed,
        # so every Bingebase push died with AttributeError.
        self.assertTrue(hasattr(SyncStatus, "running"))
        self.assertFalse(hasattr(SyncStatus, "in_progress"))

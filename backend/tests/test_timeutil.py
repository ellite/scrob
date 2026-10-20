import unittest
import warnings
from datetime import datetime, timedelta, timezone

from core.timeutil import utcnow


class UtcnowTests(unittest.TestCase):
    def test_returns_a_naive_datetime_in_utc(self) -> None:
        now = utcnow()

        self.assertIsNone(now.tzinfo)
        expected = datetime.now(timezone.utc).replace(tzinfo=None)
        self.assertLess(abs(expected - now), timedelta(seconds=5))

    def test_does_not_emit_a_deprecation_warning(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            utcnow()


if __name__ == "__main__":
    unittest.main()

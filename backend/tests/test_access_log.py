import logging
import os
import unittest

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core.access_log import (
    DEFAULT_ACCESS_LOG_EXCLUDE,
    _ExcludePathsFilter,
    _RedactQuerySecretsFilter,
    parse_exclude_prefixes,
)


def _filtered_path(path_with_query: str) -> str:
    record = logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname=__file__, lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:1234", "GET", path_with_query, "1.1", 200),
        exc_info=None,
    )
    _RedactQuerySecretsFilter().filter(record)
    return record.args[2]


class RedactQuerySecretsFilterTests(unittest.TestCase):
    """Regression: uvicorn's access log records the full request path and
    query string verbatim - api_key (accepted as an alternative to a JWT on
    webhooks, Radarr/Sonarr compat, and the write endpoints in
    get_current_user_or_api_key) would otherwise sit in plaintext log
    files/aggregators."""

    def test_redacts_known_secret_params(self) -> None:
        self.assertEqual(
            _filtered_path("/webhooks/jellyfin?api_key=SECRET123"),
            "/webhooks/jellyfin?api_key=REDACTED",
        )

    def test_redacts_multiple_secret_params_case_insensitively(self) -> None:
        self.assertEqual(
            _filtered_path("/history?API_KEY=abc&Token=xyz"),
            "/history?API_KEY=REDACTED&Token=REDACTED",
        )

    def test_leaves_non_secret_params_untouched(self) -> None:
        self.assertEqual(
            _filtered_path("/media/tmdb/list?type=movie&page=2"),
            "/media/tmdb/list?type=movie&page=2",
        )

    def test_redacts_secret_alongside_non_secret_params(self) -> None:
        self.assertEqual(
            _filtered_path("/history?type=movie&api_key=SECRET123&page=2"),
            "/history?type=movie&api_key=REDACTED&page=2",
        )

    def test_non_uvicorn_access_records_are_untouched(self) -> None:
        # Only uvicorn.access-shaped records (5-tuple args, index 2 = path)
        # should ever be rewritten - anything else passes through as-is.
        record = logging.LogRecord(
            name="uvicorn.error", level=logging.INFO, pathname=__file__, lineno=1,
            msg="some other message with api_key=SECRET123", args=(), exc_info=None,
        )
        result = _RedactQuerySecretsFilter().filter(record)
        self.assertTrue(result)
        self.assertEqual(record.msg, "some other message with api_key=SECRET123")


def _kept(path: str, status: int = 200, prefixes=DEFAULT_ACCESS_LOG_EXCLUDE) -> bool:
    record = logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname=__file__, lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:1234", "POST", path, "1.1", status),
        exc_info=None,
    )
    return _ExcludePathsFilter(prefixes).filter(record)


class ExcludePathsFilterTests(unittest.TestCase):
    def test_drops_successful_webhook_with_query(self) -> None:
        self.assertFalse(_kept("/webhooks/jellyfin/1?api_key=REDACTED"))

    def test_drops_polled_endpoint(self) -> None:
        self.assertFalse(_kept("/auth/me"))

    def test_keeps_failed_requests_on_excluded_paths(self) -> None:
        self.assertTrue(_kept("/webhooks/jellyfin/1", status=500))
        self.assertTrue(_kept("/auth/me", status=401))

    def test_keeps_other_paths(self) -> None:
        self.assertTrue(_kept("/media/recently-added?type=movie"))

    def test_non_access_records_pass(self) -> None:
        record = logging.LogRecord(
            name="uvicorn.access", level=logging.INFO, pathname=__file__, lineno=1,
            msg="hello", args=(), exc_info=None,
        )
        self.assertTrue(_ExcludePathsFilter(DEFAULT_ACCESS_LOG_EXCLUDE).filter(record))

    def test_parse_defaults_custom_and_empty(self) -> None:
        self.assertEqual(parse_exclude_prefixes(None), DEFAULT_ACCESS_LOG_EXCLUDE)
        self.assertEqual(parse_exclude_prefixes(" /a , /b/ ,"), ("/a", "/b/"))
        self.assertEqual(parse_exclude_prefixes(""), ())


if __name__ == "__main__":
    unittest.main()

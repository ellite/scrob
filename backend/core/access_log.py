import logging
import os
import re

# Uvicorn's access log records the full request path + query string verbatim
# (e.g. GET /webhooks/jellyfin?api_key=... or POST /history?api_key=...) -
# api_key is accepted as a query param as an alternative to a JWT Bearer
# token (webhooks, Radarr/Sonarr compat endpoints, and now the write
# endpoints in dependencies.get_current_user_or_api_key), so without this
# every request using it would sit in plaintext in the access log.
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:api_key|token|access_token|refresh_token|password|secret)=)[^&\s\"]+"
)


class _RedactQuerySecretsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn's access logger always logs with args =
        # (client_addr, method, path_with_query_string, http_version, status_code).
        if isinstance(record.args, tuple) and len(record.args) >= 3 and isinstance(record.args[2], str):
            args = list(record.args)
            args[2] = _QUERY_SECRET_RE.sub(r"\1REDACTED", args[2])
            record.args = tuple(args)
        return True


# Polled or high-frequency endpoints whose successful requests are not worth a
# log line each: the Jellyfin/Plex/Emby/Kodi webhooks fire on every playback
# progress tick and the frontend polls the rest. Overridable via
# ACCESS_LOG_EXCLUDE (comma-separated path prefixes, empty = log everything).
DEFAULT_ACCESS_LOG_EXCLUDE = (
    "/webhooks/",
    "/auth/me",
    "/auth/settings",
    "/auth/session/renew",
    "/sync/status",
    "/history/now-playing",
    "/admin/requests/pending-count",
)


def parse_exclude_prefixes(raw: str | None) -> tuple[str, ...]:
    if raw is None:
        return DEFAULT_ACCESS_LOG_EXCLUDE
    return tuple(p.strip() for p in raw.split(",") if p.strip())


class _ExcludePathsFilter(logging.Filter):
    """Drops successful (status < 400) access-log lines for noisy paths.
    Errors are always kept so failing webhooks stay visible."""

    def __init__(self, prefixes: tuple[str, ...]) -> None:
        super().__init__()
        self.prefixes = prefixes

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not (isinstance(args, tuple) and len(args) >= 5 and isinstance(args[2], str)):
            return True
        status = args[4]
        if isinstance(status, int) and status >= 400:
            return True
        path = args[2].split("?", 1)[0]
        return not path.startswith(self.prefixes)


def install() -> None:
    access = logging.getLogger("uvicorn.access")
    access.addFilter(_RedactQuerySecretsFilter())

    prefixes = parse_exclude_prefixes(os.environ.get("ACCESS_LOG_EXCLUDE"))
    if prefixes:
        access.addFilter(_ExcludePathsFilter(prefixes))

    level = logging.getLevelName(os.environ.get("LOG_LEVEL", "").strip().upper())
    if isinstance(level, int):
        access.setLevel(level)
        logging.getLogger("uvicorn.error").setLevel(level)

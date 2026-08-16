import logging
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


def install() -> None:
    logging.getLogger("uvicorn.access").addFilter(_RedactQuerySecretsFilter())

"""Browser-language fallback for the Metadata Language (#538).

A viewer with no Metadata Language set - an anonymous visitor of a public
list or profile, or a logged-in user who never opened Settings - used to get
the English titles unconditionally. Localized titles are often unrelated to
the English ones (the French film *Garance* is *Another Day* in English), so
the browser's Accept-Language header is used to pick one of the supported
languages instead. An explicit profile setting still wins (see
core.translations.get_user_metadata_language).
"""
from contextvars import ContextVar

# Mirror of METADATA_LANGUAGES in frontend/src/pages/user-settings.astro.
# Order matters: a primary-subtag-only request ("pt") maps to the first
# regional variant listed here ("pt-BR").
SUPPORTED_METADATA_LANGUAGES: tuple[str, ...] = (
    "en", "fr", "de", "es", "es-MX", "it", "pt-BR", "pt-PT", "ja", "ko",
    "zh-CN", "zh-TW", "hi", "ar", "ru", "nl", "pl", "tr", "sv", "cs", "hu",
    "hr", "sr",
)

# The raw Accept-Language header of the request being served, set by
# AcceptLanguageMiddleware. None outside a request (background jobs, tests).
request_accept_language: ContextVar[str | None] = ContextVar("request_accept_language", default=None)


def _normalize(tag: str) -> str:
    """'FR-fr' -> 'fr-FR'; extra subtags are dropped ('zh-Hans-CN' -> 'zh-CN')."""
    parts = [p for p in tag.strip().split("-") if p]
    if not parts:
        return ""
    primary = parts[0].lower()
    region = next((p.upper() for p in parts[1:] if len(p) == 2), None)
    return f"{primary}-{region}" if region else primary


def _parse(header: str) -> list[str]:
    """Language tags of an Accept-Language header, best preference first."""
    ranked: list[tuple[float, int, str]] = []
    for position, item in enumerate(header.split(",")):
        tag, _, params = item.partition(";")
        tag = tag.strip()
        if not tag or tag == "*":
            continue
        q = 1.0
        for param in params.split(";"):
            name, _, value = param.partition("=")
            if name.strip().lower() == "q":
                try:
                    q = float(value.strip())
                except ValueError:
                    pass  # unreadable q: keep the default rather than drop the language
        if q <= 0:
            continue
        ranked.append((-q, position, tag))
    return [tag for _q, _pos, tag in sorted(ranked)]


def negotiate_metadata_language(
    accept_language: str | None,
    supported: tuple[str, ...] = SUPPORTED_METADATA_LANGUAGES,
) -> str | None:
    """Pick the best supported metadata language for an Accept-Language header.

    Exact match first ("pt-BR"), then primary subtag ("fr-CA" -> "fr", "pt" ->
    first "pt-*" offered). Returns None when nothing matches or when the best
    match is English, so callers keep today's exact behaviour for English
    browsers (no translation lookups, TMDB's implicit en-US default).
    """
    if not accept_language:
        return None
    by_code = {code.lower(): code for code in supported}
    by_primary: dict[str, str] = {}
    for code in supported:
        by_primary.setdefault(code.split("-")[0].lower(), code)
    for tag in _parse(accept_language):
        wanted = _normalize(tag)
        if not wanted:
            continue
        match = by_code.get(wanted.lower()) or by_primary.get(wanted.split("-")[0])
        if match:
            return None if match == "en" else match
    return None


class AcceptLanguageMiddleware:
    """Pure ASGI middleware: expose the request's Accept-Language header
    through `request_accept_language` for the duration of the request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        header = next(
            (value.decode("latin-1") for name, value in scope.get("headers", ()) if name == b"accept-language"),
            None,
        )
        token = request_accept_language.set(header)
        try:
            await self.app(scope, receive, send)
        finally:
            request_accept_language.reset(token)

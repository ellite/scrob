"""TVDB v4 API client.

Token-based auth: POST /login returns a 30-day Bearer token.
We cache the token in memory (module-level) and refresh it when it expires.
"""
import asyncio
import time
import httpx

TVDB_BASE = "https://api4.thetvdb.com/v4"

# In-memory token cache keyed by api_key
_token_cache: dict[str, tuple[str, float]] = {}  # api_key -> (token, expires_at)
_token_lock = asyncio.Lock()

TVDB_IMAGE_BASE = "https://artworks.thetvdb.com"

DEFAULT_CACHE_TTL = 1800  # 30 minutes - same rationale as core/tmdb.py's response cache


class _TTLCache:
    """Minimal bounded in-process cache: TTL expiry checked lazily on read, oldest
    entry evicted on overflow (dict insertion order). No shared/multi-worker
    guarantees - fine here since scrob runs a single uvicorn process. Mirrors
    core/tmdb.py's _TTLCache exactly."""

    def __init__(self, maxsize: int = 2000):
        self._store: dict[tuple, tuple[float, dict]] = {}
        self._maxsize = maxsize

    def get(self, key: tuple):
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key: tuple, value: dict, ttl: float) -> None:
        if key not in self._store and len(self._store) >= self._maxsize:
            self._store.pop(next(iter(self._store)))
        self._store[key] = (time.monotonic() + ttl, value)


_cache = _TTLCache()

# BCP 47 (metadata_language) → ISO 639-3 used by TVDB
_TVDB_LANG: dict[str, str] = {
    "en":    "eng",
    "fr":    "fra",
    "de":    "deu",
    "es":    "spa",
    "es-MX": "spa",
    "it":    "ita",
    "pt-BR": "por",
    "pt-PT": "por",
    "ja":    "jpn",
    "ko":    "kor",
    "zh-CN": "zho",
    "zh-TW": "zho",
    "hi":    "hin",
    "ar":    "ara",
    "ru":    "rus",
    "nl":    "nld",
    "pl":    "pol",
    "tr":    "tur",
    "sv":    "swe",
    "cs":    "ces",
    "hu":    "hun",
    "hr":    "hrv",
    "sr":    "srp",
}


def tvdb_language(metadata_language: str | None) -> str | None:
    """Convert a BCP 47 metadata_language code to the ISO 639-3 code TVDB expects.

    An unset preference defaults to English: the settings UI shows an empty
    metadata_language as "-- English (default) --", but TVDB's own default
    when no language is requested is the show's original/native language
    (e.g. Japanese for anime) — unlike TMDB, which implicitly defaults to
    en-US. Passing None straight through here would silently break that
    "default is English" promise for every TVDB-sourced call.
    """
    return _TVDB_LANG.get(metadata_language or "en")


def _image_url(path: str | None) -> str | None:
    if not path:
        return None
    if path.startswith("http"):
        return path
    return f"{TVDB_IMAGE_BASE}{path}"


async def _get_token(api_key: str) -> str:
    """Return a valid TVDB Bearer token, refreshing if necessary."""
    async with _token_lock:
        cached = _token_cache.get(api_key)
        if cached:
            token, expires_at = cached
            # Refresh 1 hour before expiry
            if time.time() < expires_at - 3600:
                return token

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            r = await client.post(
                f"{TVDB_BASE}/login",
                json={"apikey": api_key},
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            r.raise_for_status()
            data = r.json()

        token = data["data"]["token"]
        # TVDB tokens last 30 days; cache for 29 days
        expires_at = time.time() + 29 * 86400
        _token_cache[api_key] = (token, expires_at)
        return token


async def _get(
    path: str,
    api_key: str,
    params: dict | None = None,
    cache_ttl: float | None = DEFAULT_CACHE_TTL,
) -> dict:
    """cache_ttl: seconds to cache the response for, keyed by (path, params) -
    api_key is auth-only and doesn't change TVDB's response content, so it's
    deliberately excluded from the cache key (same reasoning as core/tmdb.py's
    _get). Pass cache_ttl=None to bypass caching (e.g. a "Refresh Metadata"
    action, where returning a stale cached response would make it a no-op)."""
    cache_key = None
    if cache_ttl is not None:
        cache_key = (path, tuple(sorted((params or {}).items())))
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

    token = await _get_token(api_key)
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        r = await client.get(
            f"{TVDB_BASE}{path}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params=params or {},
        )
        r.raise_for_status()
        data = r.json()
        if cache_key is not None:
            _cache.set(cache_key, data, cache_ttl)
        return data


async def validate_api_key(api_key: str) -> bool:
    if not api_key:
        return False
    try:
        await _get_token(api_key)
        return True
    except Exception:
        return False


async def search_series(query: str, api_key: str, cache_ttl: float | None = DEFAULT_CACHE_TTL) -> list[dict]:
    """Search for TV series by title. Returns list of simplified series dicts."""
    data = await _get("/search", api_key, params={"query": query, "type": "series"}, cache_ttl=cache_ttl)
    results = []
    for item in data.get("data") or []:
        tvdb_id_str = item.get("tvdb_id") or item.get("id") or ""
        try:
            tvdb_id = int(str(tvdb_id_str).lstrip("series-"))
        except (ValueError, TypeError):
            continue
        results.append({
            "tvdb_id": tvdb_id,
            "title": item.get("name") or item.get("translations", {}).get("eng", ""),
            "overview": item.get("overview") or item.get("overviews", {}).get("eng"),
            "year": item.get("year"),
            "image_url": _image_url(item.get("image_url") or item.get("thumbnail")),
            "status": item.get("status"),
            "network": item.get("network"),
        })
    return results


async def get_series(tvdb_id: int, api_key: str, cache_ttl: float | None = DEFAULT_CACHE_TTL) -> dict:
    """Fetch series extended info including episodes for accurate per-season counts."""
    data = await _get(
        f"/series/{tvdb_id}/extended", api_key, params={"meta": "translations,episodes"}, cache_ttl=cache_ttl,
    )
    return data.get("data") or {}


async def get_season(season_id: int, api_key: str, cache_ttl: float | None = DEFAULT_CACHE_TTL) -> dict:
    """Fetch extended season metadata, including translated names and overviews."""
    data = await _get(
        f"/seasons/{season_id}/extended",
        api_key,
        params={"meta": "translations"},
        cache_ttl=cache_ttl,
    )
    return data.get("data") or {}


def format_season(raw: dict, language: str | None = None) -> dict:
    """Normalise extended TVDB season metadata."""
    translations = raw.get("translations") or {}

    def _pick(key: str, field: str) -> str | None:
        entries = translations.get(key) or []
        fallback = None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if language and entry.get("language") == language:
                return entry.get(field) or None
            if entry.get("language") == "eng":
                fallback = entry.get(field) or None
        return fallback

    return {
        "season_number": raw.get("number"),
        "name": _pick("nameTranslations", "name") or raw.get("name"),
        "overview": _pick("overviewTranslations", "overview") or raw.get("overview"),
        "poster_path": _image_url(raw.get("image")),
        "air_date": raw.get("premiereDate"),
        "id": raw.get("id"),
    }


async def get_series_episodes(
    tvdb_id: int,
    season_number: int | None,
    api_key: str,
    language: str | None = None,
    cache_ttl: float | None = DEFAULT_CACHE_TTL,
) -> list[dict]:
    """Fetch episodes for a specific season (season_type=official), or every
    episode in the series if season_number is None.

    TVDB v4 has no `language` query param on this endpoint — it's silently
    ignored if passed. Translated episode name/overview require the separate
    `.../episodes/{season-type}/{lang}` path variant instead — and that variant
    silently ignores the `season` query param too, always returning the show's
    entire episode list regardless of what season was requested. So when a
    specific season_number is given, the filter is re-applied client-side
    below, unconditionally, rather than trusting the server to have honored it.
    """
    episodes = []
    page = 0
    path = f"/series/{tvdb_id}/episodes/official/{language}" if language else f"/series/{tvdb_id}/episodes/official"
    while True:
        params: dict = {"page": page}
        if season_number is not None:
            params["season"] = season_number
        data = await _get(
            path,
            api_key,
            params=params,
            cache_ttl=cache_ttl,
        )
        batch = (data.get("data") or {}).get("episodes") or []
        if not batch:
            break
        episodes.extend(batch)
        # TVDB paginates at 500; if we got fewer, we're done
        if len(batch) < 500:
            break
        page += 1
    if season_number is None:
        return episodes
    return [e for e in episodes if e.get("seasonNumber") == season_number]


def format_series(raw: dict, language: str | None = None) -> dict:
    """Normalise TVDB extended series data into a frontend-friendly dict."""
    image = raw.get("image") or ""
    poster = _image_url(image) if image else None

    translations = raw.get("translations") or {}

    def _pick(key: str, field: str) -> str | None:
        entries = translations.get(key) or []
        result = None
        for t in entries:
            if not isinstance(t, dict):
                continue
            if language and t.get("language") == language:
                return t.get(field) or None  # preferred language found
            if t.get("language") == "eng":
                result = t.get(field) or None  # English fallback
        return result

    translated_title = _pick("nameTranslations", "name")
    eng_overview = _pick("overviewTranslations", "overview")

    genres = [g.get("name") for g in (raw.get("genres") or []) if g.get("name")]

    # Count episodes per season and derive premiere dates from embedded episodes
    episode_counts: dict[int, int] = {}
    season_premiere_dates: dict[int, str] = {}
    for ep in raw.get("episodes") or []:
        sn = ep.get("seasonNumber")
        if sn is None:
            continue
        episode_counts[sn] = episode_counts.get(sn, 0) + 1
        if ep.get("number") == 1 and ep.get("aired") and sn not in season_premiere_dates:
            season_premiere_dates[sn] = ep["aired"]

    seasons = []
    for s in raw.get("seasons") or []:
        if s.get("type", {}).get("type") == "official":
            sn = s.get("number")
            count = episode_counts.get(sn) if sn in episode_counts else (s.get("episodeCount") or 0)
            seasons.append({
                "season_number": sn,
                "name": s.get("name") or f"Season {sn}",
                "overview": None,
                "poster_path": _image_url(s.get("image")),
                "episode_count": count,
                "air_date": s.get("premiereDate") or season_premiere_dates.get(sn),
                "id": s.get("id"),
            })
    seasons.sort(key=lambda x: x["season_number"] or 0)

    network = None
    for n in raw.get("networks") or []:
        if n.get("primaryLanguage") == "eng" or not network:
            network = n.get("name")

    age_rating = None
    for cr in raw.get("contentRatings") or []:
        if cr.get("country") == "usa" and cr.get("contentType") == "TV":
            age_rating = cr.get("name")
            break
    if not age_rating:
        for cr in raw.get("contentRatings") or []:
            age_rating = cr.get("name")
            break

    imdb_id = None
    tmdb_id_cross = None
    for rid in raw.get("remoteIds") or []:
        source = (rid.get("sourceName") or "").upper()
        if source == "IMDB" and not imdb_id:
            imdb_id = rid.get("id")
        elif "MOVIEDB" in source and not tmdb_id_cross:
            try:
                tmdb_id_cross = int(rid.get("id"))
            except (TypeError, ValueError):
                pass

    return {
        "tvdb_id": raw.get("id"),
        "title": translated_title or raw.get("name"),
        "original_title": raw.get("originalName") or raw.get("name"),
        "overview": eng_overview or raw.get("overview"),
        "poster_path": poster,
        "backdrop_path": _image_url(raw.get("artworks", [{}])[0].get("image") if raw.get("artworks") else None),
        "first_air_date": raw.get("firstAired"),
        "last_air_date": raw.get("lastAired"),
        "status": (raw.get("status") or {}).get("name"),
        "genres": genres,
        "network": network,
        "seasons": seasons,
        "original_language": raw.get("originalLanguage"),
        "age_rating": age_rating,
        "imdb_id": imdb_id,
        "tmdb_id_cross": tmdb_id_cross,
    }


def format_cast(raw: dict) -> list[dict]:
    """Extract actor list from TVDB extended series data."""
    characters = [c for c in (raw.get("characters") or []) if c.get("type") == 3]
    characters.sort(key=lambda x: x.get("sort") or 999)
    return [
        {
            "tmdb_id": None,
            "person_id": c.get("personId"),
            "name": c.get("personName") or "",
            "character": c.get("name") or "",
            "profile_path": _image_url(c.get("image")),
        }
        for c in characters[:12]
        if c.get("personName")
    ]


def format_episode(raw: dict) -> dict:
    return {
        "tvdb_id": raw.get("id"),
        "season_number": raw.get("seasonNumber"),
        "episode_number": raw.get("number"),
        "name": raw.get("name"),
        "overview": raw.get("overview"),
        "air_date": raw.get("aired"),
        "runtime": raw.get("runtime"),
        "image_url": _image_url(raw.get("image")),
    }

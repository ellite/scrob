"""Builds a personal data export zip in the same file/shape layout as a
Trakt.tv data export (Settings -> Data Export on trakt.tv).

This is generate-only — matching Trakt's shapes means the existing Trakt
export *importer* (core/trakt_export.py, used by the "import a Trakt export
zip" flow) can read history/ratings/lists back out of it, so the export
doubles as a restorable backup of that data, on this instance or another.

Secrets (API keys, OAuth tokens, third-party service URLs) are excluded by
default — a zip is far easier to accidentally share or upload somewhere than
an authenticated API response, so user-profile/user-stats/user-settings only
ever contain identity/counts/preferences, never credentials. The four
secret-bearing categories (api_keys, media_connections, scrobble_connections,
connections) are opt-in only and, when requested, are written in plaintext —
callers must surface that plainly before a user opts in.
"""

import io
import json
import re
import zipfile
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.base import MediaType
from models.collection import Collection, CollectionFile
from models.comments import Comment
from models.connections import MediaServerConnection
from models.events import WatchEvent
from models.lists import List as ListModel, ListItem
from models.media import Media
from models.profile import UserProfileData
from models.ratings import Rating
from models.scrobble_connection import ScrobbleConnection
from models.show import Show
from models.users import User, UserSettings

HISTORY_CHUNK_SIZE = 2000
COLLECTION_CHUNK_SIZE = 2000

# Third-party sync-service credentials + preferences (Trakt/Simkl/MDBList/Radarr/Sonarr) — a superset
# of _SAFE_SETTINGS_FIELDS below, since this bundle is opt-in and plaintext-secrets are expected.
_CONNECTIONS_SETTINGS_FIELDS = (
    "radarr_url", "radarr_token", "radarr_root_folder", "radarr_quality_profile", "radarr_tags",
    "sonarr_url", "sonarr_token", "sonarr_root_folder", "sonarr_quality_profile", "sonarr_tags", "sonarr_season_folder",
    "trakt_client_id", "trakt_client_secret", "trakt_access_token", "trakt_refresh_token", "trakt_token_expires_at",
    "trakt_sync_watched", "trakt_sync_ratings", "trakt_sync_lists", "trakt_watchlist_split",
    "trakt_push_watched", "trakt_push_ratings", "trakt_push_collection", "trakt_push_lists", "trakt_scrobble",
    "trakt_auto_sync_interval", "trakt_auto_push_interval",
    "simkl_client_id", "simkl_access_token",
    "simkl_sync_watched", "simkl_sync_ratings", "simkl_sync_lists",
    "simkl_push_watched", "simkl_push_ratings", "simkl_scrobble",
    "simkl_auto_sync_interval", "simkl_auto_push_interval",
    "mdblist_api_key", "mdblist_sync_watched", "mdblist_sync_ratings", "mdblist_sync_watchlist",
    "mdblist_push_watched", "mdblist_push_ratings", "mdblist_push_watchlist", "mdblist_push_collection", "mdblist_scrobble",
    "mdblist_auto_sync_interval", "mdblist_auto_push_interval",
)

_MEDIA_CONNECTION_FIELDS = (
    "type", "name", "url", "token", "server_user_id", "server_username",
    "sync_collection", "sync_watched", "sync_ratings", "sync_playback",
    "push_watched", "push_collection", "push_playback", "push_ratings",
)

_SCROBBLE_CONNECTION_FIELDS = (
    "type", "name", "server_user_id", "server_username",
    "sync_collection", "sync_watched", "sync_playback",
)

_WATCHLIST_SLUGS = {"__watchlist__", "__watchlist_movies__", "__watchlist_shows__", "__plex_watchlist__"}

# Sync preferences only (booleans + auto-sync intervals) — never a URL, token, or API key.
_SAFE_SETTINGS_FIELDS = (
    "trakt_sync_watched", "trakt_sync_ratings", "trakt_sync_lists", "trakt_watchlist_split",
    "trakt_push_watched", "trakt_push_ratings", "trakt_push_collection", "trakt_push_lists", "trakt_scrobble",
    "trakt_auto_sync_interval", "trakt_auto_push_interval",
    "simkl_sync_watched", "simkl_sync_ratings", "simkl_sync_lists",
    "simkl_push_watched", "simkl_push_ratings", "simkl_scrobble",
    "simkl_auto_sync_interval", "simkl_auto_push_interval",
    "mdblist_sync_watched", "mdblist_sync_ratings", "mdblist_sync_watchlist",
    "mdblist_push_watched", "mdblist_push_ratings", "mdblist_push_watchlist", "mdblist_push_collection", "mdblist_scrobble",
    "mdblist_auto_sync_interval", "mdblist_auto_push_interval",
)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _year(release_date: str | None) -> int | None:
    return int(release_date[:4]) if release_date and release_date[:4].isdigit() else None


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "list"


def _movie_dict(media: Media) -> dict:
    return {
        "title": media.title,
        "year": _year(media.release_date),
        "ids": {"tmdb": media.tmdb_id},
    }


def _show_dict(tmdb_id: int | None, title: str, release_date: str | None, tvdb_id: int | None) -> dict:
    ids: dict = {"tmdb": tmdb_id}
    if tvdb_id:
        ids["tvdb"] = tvdb_id
    return {"title": title, "year": _year(release_date), "ids": ids}


async def _shows_by_tmdb_id(db: AsyncSession, tmdb_ids: set[int]) -> dict[int, Show]:
    if not tmdb_ids:
        return {}
    result = await db.execute(select(Show).where(Show.tmdb_id.in_(tmdb_ids)))
    return {s.tmdb_id: s for s in result.scalars().all()}


async def _shows_by_id(db: AsyncSession, show_ids: set[int]) -> dict[int, Show]:
    if not show_ids:
        return {}
    result = await db.execute(select(Show).where(Show.id.in_(show_ids)))
    return {s.id: s for s in result.scalars().all()}


# ── Watch history ──────────────────────────────────────────────────────

async def build_history(db: AsyncSession, user_id: int) -> list[dict]:
    rows = (await db.execute(
        select(WatchEvent, Media)
        .join(Media, Media.id == WatchEvent.media_id)
        .where(WatchEvent.user_id == user_id, WatchEvent.completed == True)  # noqa: E712
        .order_by(WatchEvent.watched_at)
    )).all()

    show_ids = {m.show_id for _, m in rows if m.media_type == MediaType.episode and m.show_id}
    shows_by_id = await _shows_by_id(db, show_ids)

    entries: list[dict] = []
    for event, media in rows:
        if media.media_type == MediaType.movie:
            entries.append({
                "id": event.id,
                "watched_at": _iso(event.watched_at),
                "action": "watch",
                "type": "movie",
                "movie": _movie_dict(media),
            })
        elif media.media_type == MediaType.episode and media.season_number is not None and media.episode_number is not None:
            show = shows_by_id.get(media.show_id)
            entries.append({
                "id": event.id,
                "watched_at": _iso(event.watched_at),
                "action": "watch",
                "type": "episode",
                "episode": {
                    "season": media.season_number,
                    "number": media.episode_number,
                    "title": media.title,
                    "ids": {"tmdb": media.tmdb_id},
                },
                "show": _show_dict(show.tmdb_id if show else None, show.title if show else media.title, show.first_air_date if show else None, show.tvdb_id if show else None),
            })
    return entries


# ── Ratings ─────────────────────────────────────────────────────────────

async def build_ratings(db: AsyncSession, user_id: int) -> dict[str, list[dict]]:
    rows = (await db.execute(
        select(Rating, Media)
        .join(Media, Media.id == Rating.media_id)
        .where(Rating.user_id == user_id)
    )).all()

    series_tmdb_ids = {m.tmdb_id for r, m in rows if m.media_type == MediaType.series and m.tmdb_id}
    episode_show_ids = {m.show_id for r, m in rows if m.media_type == MediaType.episode and m.show_id}
    shows_by_tmdb = await _shows_by_tmdb_id(db, series_tmdb_ids)
    shows_by_id = await _shows_by_id(db, episode_show_ids)

    out: dict[str, list[dict]] = {"movies": [], "shows": [], "seasons": [], "episodes": []}
    for rating, media in rows:
        base = {"rated_at": _iso(rating.rated_at), "rating": round(rating.rating) if rating.rating is not None else None}
        if media.media_type == MediaType.movie:
            out["movies"].append({**base, "type": "movie", "movie": _movie_dict(media)})
        elif media.media_type == MediaType.series and rating.season_number is None:
            show = shows_by_tmdb.get(media.tmdb_id)
            out["shows"].append({**base, "type": "show", "show": _show_dict(media.tmdb_id, media.title, show.first_air_date if show else None, show.tvdb_id if show else None)})
        elif media.media_type == MediaType.series and rating.season_number is not None:
            show = shows_by_tmdb.get(media.tmdb_id)
            out["seasons"].append({
                **base, "type": "season",
                "season": {"number": rating.season_number, "ids": {}},
                "show": _show_dict(media.tmdb_id, media.title, show.first_air_date if show else None, show.tvdb_id if show else None),
            })
        elif media.media_type == MediaType.episode and media.season_number is not None and media.episode_number is not None:
            show = shows_by_id.get(media.show_id)
            out["episodes"].append({
                **base, "type": "episode",
                "episode": {"season": media.season_number, "number": media.episode_number, "title": media.title, "ids": {"tmdb": media.tmdb_id}},
                "show": _show_dict(show.tmdb_id if show else None, show.title if show else media.title, show.first_air_date if show else None, show.tvdb_id if show else None),
            })
    return out


# ── Collection ────────────────────────────────────────────────────────

async def build_collection(db: AsyncSession, user_id: int) -> dict[str, list[dict]]:
    rows = (await db.execute(
        select(Collection, Media)
        .join(Media, Media.id == Collection.media_id)
        .where(Collection.user_id == user_id)
    )).all()

    show_ids = {m.show_id for _, m in rows if m.media_type == MediaType.episode and m.show_id}
    shows_by_id = await _shows_by_id(db, show_ids)

    out: dict[str, list[dict]] = {"movies": [], "episodes": []}
    for coll, media in rows:
        collected_at = _iso(coll.added_at)
        if media.media_type == MediaType.movie:
            out["movies"].append({
                "type": "movie", "collected_at": collected_at, "updated_at": collected_at,
                "movie": _movie_dict(media),
            })
        elif media.media_type == MediaType.episode and media.season_number is not None and media.episode_number is not None:
            show = shows_by_id.get(media.show_id)
            out["episodes"].append({
                "type": "episode", "collected_at": collected_at, "updated_at": collected_at,
                "episode": {"season": media.season_number, "number": media.episode_number, "title": media.title, "ids": {"tmdb": media.tmdb_id}},
                "show": _show_dict(show.tmdb_id if show else None, show.title if show else media.title, show.first_air_date if show else None, show.tvdb_id if show else None),
            })
    return out


# ── Lists (watchlist + custom) ───────────────────────────────────────────

async def build_lists(db: AsyncSession, user_id: int) -> tuple[list[dict], list[dict], dict[str, list[dict]]]:
    """Returns (watchlist_items, lists_meta, {slug: items}) for custom lists."""
    lists = (await db.execute(select(ListModel).where(ListModel.user_id == user_id))).scalars().all()

    watchlist_items: list[dict] = []
    lists_meta: list[dict] = []
    list_items_by_slug: dict[str, list[dict]] = {}

    for lst in lists:
        rows = (await db.execute(
            select(ListItem, Media)
            .join(Media, Media.id == ListItem.media_id)
            .where(ListItem.list_id == lst.id)
            .order_by(ListItem.sort_order)
        )).all()

        series_tmdb_ids = {m.tmdb_id for _, m in rows if m.media_type == MediaType.series and m.tmdb_id}
        shows_by_tmdb = await _shows_by_tmdb_id(db, series_tmdb_ids)

        items: list[dict] = []
        for rank, (item, media) in enumerate(rows, start=1):
            entry = {
                "rank": rank,
                "id": item.id,
                "listed_at": _iso(item.added_at),
                "notes": item.notes,
            }
            if media.media_type == MediaType.movie:
                entry["type"] = "movie"
                entry["movie"] = _movie_dict(media)
            elif media.media_type == MediaType.series:
                show = shows_by_tmdb.get(media.tmdb_id)
                entry["type"] = "show"
                entry["show"] = _show_dict(media.tmdb_id, media.title, show.first_air_date if show else None, show.tvdb_id if show else None)
            else:
                continue
            items.append(entry)

        is_watchlist = lst.trakt_slug in _WATCHLIST_SLUGS or lst.mdblist_slug in _WATCHLIST_SLUGS
        if is_watchlist:
            watchlist_items.extend(items)
            continue

        slug = _slugify(lst.name)
        lists_meta.append({
            "name": lst.name,
            "description": lst.description or "",
            "privacy": lst.privacy_level.value,
            "type": "personal",
            "created_at": _iso(lst.created_at),
            "updated_at": _iso(lst.updated_at),
            "item_count": len(items),
            "ids": {"trakt": lst.id, "slug": slug},
        })
        list_items_by_slug[slug] = items

    return watchlist_items, lists_meta, list_items_by_slug


# ── Comments ──────────────────────────────────────────────────────────

async def build_comments(db: AsyncSession, user_id: int) -> dict[str, list[dict]]:
    comments = (await db.execute(select(Comment).where(Comment.user_id == user_id))).scalars().all()

    tmdb_ids_by_type: dict[str, set[int]] = {"movie": set(), "series": set(), "episode": set()}
    for c in comments:
        if c.media_type in tmdb_ids_by_type:
            tmdb_ids_by_type[c.media_type].add(c.tmdb_id)

    movies_res = await db.execute(select(Media).where(Media.media_type == MediaType.movie, Media.tmdb_id.in_(tmdb_ids_by_type["movie"]))) if tmdb_ids_by_type["movie"] else None
    movies_by_tmdb = {m.tmdb_id: m for m in movies_res.scalars().all()} if movies_res else {}

    shows_res = await db.execute(select(Show).where(Show.tmdb_id.in_(tmdb_ids_by_type["series"]))) if tmdb_ids_by_type["series"] else None
    shows_by_tmdb = {s.tmdb_id: s for s in shows_res.scalars().all()} if shows_res else {}

    out: dict[str, list[dict]] = {"movies": [], "shows": [], "seasons": [], "episodes": []}
    for c in comments:
        base = {"id": c.id, "created_at": _iso(c.created_at), "comment": c.content, "spoiler": c.is_spoiler}
        if c.media_type == "movie":
            media = movies_by_tmdb.get(c.tmdb_id)
            out["movies"].append({**base, "movie": {"title": media.title if media else None, "year": _year(media.release_date) if media else None, "ids": {"tmdb": c.tmdb_id}}})
        elif c.media_type == "series" and c.season_number is None:
            show = shows_by_tmdb.get(c.tmdb_id)
            out["shows"].append({**base, "show": _show_dict(c.tmdb_id, show.title if show else "", show.first_air_date if show else None, show.tvdb_id if show else None)})
        elif c.media_type == "series" and c.season_number is not None:
            show = shows_by_tmdb.get(c.tmdb_id)
            out["seasons"].append({**base, "season": {"number": c.season_number}, "show": _show_dict(c.tmdb_id, show.title if show else "", show.first_air_date if show else None, show.tvdb_id if show else None)})
        elif c.media_type == "episode":
            out["episodes"].append({**base, "episode": {"season": c.season_number, "number": c.episode_number}, "ids": {"tmdb": c.tmdb_id}})
    return out


# ── User metadata (no secrets) ───────────────────────────────────────────

def build_user_profile(user: User, display_name: str) -> dict:
    return {
        "username": user.username,
        "display_name": display_name,
        "joined_at": _iso(user.created_at),
    }


def build_user_settings(settings: UserSettings | None) -> dict:
    if settings is None:
        return {}
    return {field: getattr(settings, field) for field in _SAFE_SETTINGS_FIELDS}


# ── Opt-in, secret-bearing categories (plaintext) ───────────────────────

def build_api_keys(user: User, settings: UserSettings | None) -> dict:
    return {
        "scrob_api_key": user.api_key,
        "tmdb_api_key": settings.tmdb_api_key if settings else None,
        "tvdb_api_key": settings.tvdb_api_key if settings else None,
    }


async def build_media_connections(db: AsyncSession, user_id: int) -> list[dict]:
    rows = (await db.execute(select(MediaServerConnection).where(MediaServerConnection.user_id == user_id))).scalars().all()
    return [{field: getattr(conn, field) for field in _MEDIA_CONNECTION_FIELDS} for conn in rows]


async def build_scrobble_connections(db: AsyncSession, user_id: int) -> list[dict]:
    rows = (await db.execute(select(ScrobbleConnection).where(ScrobbleConnection.user_id == user_id))).scalars().all()
    return [{field: getattr(conn, field) for field in _SCROBBLE_CONNECTION_FIELDS} for conn in rows]


def build_connections(settings: UserSettings | None) -> dict:
    if settings is None:
        return {}
    return {field: getattr(settings, field) for field in _CONNECTIONS_SETTINGS_FIELDS}


def build_user_stats(
    history: list[dict], ratings: dict[str, list[dict]], collection: dict[str, list[dict]],
    *, include_watched: bool, include_ratings: bool, include_collection: bool,
) -> dict:
    """Only reports counts for categories actually included in the export —
    otherwise an excluded category would misleadingly read as "0"."""
    movies: dict = {}
    shows: dict = {}
    seasons: dict = {}
    episodes: dict = {}
    if include_watched:
        movies["plays"] = sum(1 for e in history if e["type"] == "movie")
        episodes["plays"] = sum(1 for e in history if e["type"] == "episode")
    if include_ratings:
        movies["ratings"] = len(ratings["movies"])
        shows["ratings"] = len(ratings["shows"])
        seasons["ratings"] = len(ratings["seasons"])
        episodes["ratings"] = len(ratings["episodes"])
    if include_collection:
        movies["collected"] = len(collection["movies"])
        episodes["collected"] = len(collection["episodes"])
    return {"movies": movies, "shows": shows, "seasons": seasons, "episodes": episodes}


# ── Zip assembly ────────────────────────────────────────────────────────

def _chunked(items: list, size: int):
    for offset in range(0, len(items), size):
        yield items[offset:offset + size]


async def build_export_zip(
    db: AsyncSession,
    user: User,
    settings: UserSettings | None,
    *,
    include_watched: bool = True,
    include_ratings: bool = True,
    include_collection: bool = True,
    include_lists: bool = True,
    include_comments: bool = True,
    include_api_keys: bool = False,
    include_media_connections: bool = False,
    include_scrobble_connections: bool = False,
    include_connections: bool = False,
) -> bytes:
    profile_result = await db.execute(select(UserProfileData).where(UserProfileData.user_id == user.id))
    profile = profile_result.scalar_one_or_none()
    display_name = profile.display_name if profile and profile.display_name else user.username

    history = await build_history(db, user.id) if include_watched else []
    ratings = await build_ratings(db, user.id) if include_ratings else {"movies": [], "shows": [], "seasons": [], "episodes": []}
    collection = await build_collection(db, user.id) if include_collection else {"movies": [], "episodes": []}
    watchlist_items, lists_meta, list_items_by_slug = await build_lists(db, user.id) if include_lists else ([], [], {})
    comments = await build_comments(db, user.id) if include_comments else {"movies": [], "shows": [], "seasons": [], "episodes": []}

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        def write_json(name: str, data) -> None:
            zf.writestr(name, json.dumps(data, indent=2))

        write_json("user-profile.json", build_user_profile(user, display_name))
        write_json("user-settings.json", build_user_settings(settings))
        write_json("user-stats.json", build_user_stats(
            history, ratings, collection,
            include_watched=include_watched, include_ratings=include_ratings, include_collection=include_collection,
        ))

        if include_watched:
            for i, chunk in enumerate(_chunked(history, HISTORY_CHUNK_SIZE), start=1):
                write_json(f"watched-history-{i}.json", chunk)
            if not history:
                write_json("watched-history-1.json", [])

        if include_ratings:
            write_json("ratings-movies.json", ratings["movies"])
            write_json("ratings-shows.json", ratings["shows"])
            write_json("ratings-seasons.json", ratings["seasons"])
            write_json("ratings-episodes.json", ratings["episodes"])

        if include_collection:
            for i, chunk in enumerate(_chunked(collection["movies"], COLLECTION_CHUNK_SIZE), start=1):
                write_json(f"collection-movies-{i}.json", chunk)
            if not collection["movies"]:
                write_json("collection-movies-1.json", [])
            for i, chunk in enumerate(_chunked(collection["episodes"], COLLECTION_CHUNK_SIZE), start=1):
                write_json(f"collection-episodes-{i}.json", chunk)
            if not collection["episodes"]:
                write_json("collection-episodes-1.json", [])

        if include_lists:
            write_json("lists-watchlist.json", watchlist_items)
            write_json("lists-lists.json", lists_meta)
            for slug, items in list_items_by_slug.items():
                lst_id = next((m["ids"]["trakt"] for m in lists_meta if m["ids"]["slug"] == slug), 0)
                write_json(f"lists-list-{lst_id}-{slug}.json", items)

        if include_comments:
            write_json("comments-movies.json", comments["movies"])
            write_json("comments-shows.json", comments["shows"])
            write_json("comments-seasons.json", comments["seasons"])
            write_json("comments-episodes.json", comments["episodes"])

        if include_api_keys:
            write_json("api-keys.json", build_api_keys(user, settings))

        if include_media_connections:
            write_json("media-connections.json", await build_media_connections(db, user.id))

        if include_scrobble_connections:
            write_json("scrobble-connections.json", await build_scrobble_connections(db, user.id))

        if include_connections:
            write_json("connections.json", build_connections(settings))

    return buffer.getvalue()

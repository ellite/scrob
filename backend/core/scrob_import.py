"""Parser for a Scrob personal data export (the zip produced by
GET /export/data — see core/data_export.py for the writer).

Mirrors core/trakt_export.py's structure and size-capping approach, but reads
the full set of categories Scrob's own export can produce — including
collection, comments, and the opt-in secret-bearing files that a Trakt
export never has.
"""

import io
import json
import logging
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.base import CollectionSource, PrivacyLevel
from models.collection import Collection, CollectionFile
from models.comments import Comment
from models.connections import MediaServerConnection
from models.events import WatchEvent
from models.lists import List as ListModel, ListItem
from models.media import Media
from models.ratings import Rating, RatingChanges
from models.scrobble_connection import ScrobbleConnection
from models.show import Show
from models.sync import SyncJob
from models.users import UserSettings

logger = logging.getLogger(__name__)

MAX_ENTRY_SIZE = 100 * 1024 * 1024
MAX_TOTAL_SIZE = 500 * 1024 * 1024
_READ_CHUNK_SIZE = 1024 * 1024

WATCHLIST_SLUG = "__watchlist__"

# Same third-party credential/preference fields as core/data_export.py's
# _CONNECTIONS_SETTINGS_FIELDS — kept in sync manually since importing one
# from the other would be a needless cross-module coupling for a plain tuple.
_CONNECTIONS_SETTINGS_FIELDS = (
    "radarr_url", "radarr_token", "radarr_root_folder", "radarr_quality_profile", "radarr_tags",
    "sonarr_url", "sonarr_token", "sonarr_root_folder", "sonarr_quality_profile", "sonarr_tags", "sonarr_season_folder",
    "trakt_client_id", "trakt_client_secret", "trakt_access_token", "trakt_refresh_token", "trakt_token_expires_at",
    "trakt_sync_watched", "trakt_sync_ratings", "trakt_sync_lists", "trakt_watchlist_split",
    "trakt_push_watched", "trakt_push_ratings", "trakt_push_collection", "trakt_push_lists", "trakt_scrobble",
    "simkl_client_id", "simkl_access_token",
    "simkl_sync_watched", "simkl_sync_ratings", "simkl_sync_lists",
    "simkl_push_watched", "simkl_push_ratings", "simkl_scrobble",
    "mdblist_api_key", "mdblist_sync_watched", "mdblist_sync_ratings", "mdblist_sync_watchlist",
    "mdblist_push_watched", "mdblist_push_ratings", "mdblist_push_watchlist", "mdblist_push_collection", "mdblist_scrobble",
)

_HISTORY_RE = re.compile(r"^watched-history-\d+\.json$")
_COLLECTION_MOVIES_RE = re.compile(r"^collection-movies-\d+\.json$")
_COLLECTION_EPISODES_RE = re.compile(r"^collection-episodes-\d+\.json$")
_LIST_ITEMS_RE = re.compile(r"^lists-list-(\d+)-(.+)\.json$")


@dataclass
class ScrobImportData:
    history_movies: list[dict] = field(default_factory=list)
    history_episodes: list[dict] = field(default_factory=list)
    ratings: dict[str, list[dict]] = field(default_factory=dict)
    collection_movies: list[dict] = field(default_factory=list)
    collection_episodes: list[dict] = field(default_factory=list)
    watchlist: list[dict] = field(default_factory=list)
    lists: list[dict] = field(default_factory=list)
    list_items: dict[str, list[dict]] = field(default_factory=dict)
    comments: dict[str, list[dict]] = field(default_factory=dict)
    api_keys: dict | None = None
    media_connections: list[dict] | None = None
    scrobble_connections: list[dict] | None = None
    connections: dict | None = None


def parse_scrob_export(content: bytes) -> ScrobImportData:
    """Parse a Scrob data export zip into the shapes the import apply-logic expects.

    Raises ValueError with a user-facing message if the file isn't a valid
    or recognizable Scrob export.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        raise ValueError("This file doesn't look like a valid Scrob export (.zip).")

    names = set(zf.namelist())

    if "user-profile.json" not in names:
        raise ValueError("This doesn't look like a Scrob data export — user-profile.json is missing.")

    # Same rationale as core/trakt_export.py: ZipInfo.file_size is attacker-controlled
    # metadata, so caps are enforced against bytes actually produced while streaming.
    total_read = 0

    def _read_limited(name: str) -> bytes:
        nonlocal total_read
        chunks: list[bytes] = []
        entry_read = 0
        try:
            with zf.open(name) as f:
                while True:
                    chunk = f.read(_READ_CHUNK_SIZE)
                    if not chunk:
                        break
                    entry_read += len(chunk)
                    total_read += len(chunk)
                    if entry_read > MAX_ENTRY_SIZE or total_read > MAX_TOTAL_SIZE:
                        raise ValueError("Export file is too large to import.")
                    chunks.append(chunk)
        except zipfile.BadZipFile:
            raise ValueError(f"'{name}' in the export is corrupted or inconsistent.")
        return b"".join(chunks)

    def _load_list(name: str) -> list:
        if name not in names:
            return []
        data = json.loads(_read_limited(name))
        return data if isinstance(data, list) else []

    def _load_dict(name: str) -> dict | None:
        if name not in names:
            return None
        data = json.loads(_read_limited(name))
        return data if isinstance(data, dict) else None

    def _load_chunked(pattern: re.Pattern) -> list[dict]:
        matches = sorted(
            (n for n in names if pattern.match(n)),
            key=lambda n: int(re.search(r"-(\d+)\.json$", n).group(1)),
        )
        items: list[dict] = []
        for n in matches:
            items.extend(_load_list(n))
        return items

    history = _load_chunked(_HISTORY_RE)
    history_movies = [e for e in history if e.get("type") == "movie"]
    history_episodes = [e for e in history if e.get("type") == "episode"]

    ratings = {
        "movies": _load_list("ratings-movies.json"),
        "shows": _load_list("ratings-shows.json"),
        "seasons": _load_list("ratings-seasons.json"),
        "episodes": _load_list("ratings-episodes.json"),
    }

    comments = {
        "movies": _load_list("comments-movies.json"),
        "shows": _load_list("comments-shows.json"),
        "seasons": _load_list("comments-seasons.json"),
        "episodes": _load_list("comments-episodes.json"),
    }

    watchlist = _load_list("lists-watchlist.json")
    lists_meta = _load_list("lists-lists.json")

    id_to_filename: dict[int, str] = {}
    for n in names:
        m = _LIST_ITEMS_RE.match(n)
        if m:
            id_to_filename[int(m.group(1))] = n

    list_items: dict[str, list[dict]] = {}
    for lst in lists_meta:
        list_id = lst.get("ids", {}).get("trakt")
        slug = lst.get("ids", {}).get("slug")
        fname = id_to_filename.get(list_id)
        if slug and fname:
            list_items[slug] = _load_list(fname)

    return ScrobImportData(
        history_movies=history_movies,
        history_episodes=history_episodes,
        ratings=ratings,
        collection_movies=_load_chunked(_COLLECTION_MOVIES_RE),
        collection_episodes=_load_chunked(_COLLECTION_EPISODES_RE),
        watchlist=watchlist,
        lists=lists_meta,
        list_items=list_items,
        comments=comments,
        api_keys=_load_dict("api-keys.json"),
        media_connections=_load_list("media-connections.json") if "media-connections.json" in names else None,
        scrobble_connections=_load_list("scrobble-connections.json") if "scrobble-connections.json" in names else None,
        connections=_load_dict("connections.json"),
    )


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    from dateutil import parser as dt_parser
    parsed = dt_parser.isoparse(value)
    if parsed.tzinfo:
        from datetime import timezone
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


async def apply_scrob_import(
    db: AsyncSession,
    job_id: int,
    user_id: int,
    data: ScrobImportData,
    api_key: str | None,
    *,
    include_watched: bool,
    include_ratings: bool,
    include_collection: bool,
    include_lists: bool,
    include_comments: bool,
    include_api_keys: bool,
    include_media_connections: bool,
    include_scrobble_connections: bool,
    include_connections: bool,
) -> dict:
    """Restores a Scrob data export into the current user's account.

    Reuses the same get-or-create-media / rating-application helpers as the
    Trakt export import (routers/trakt.py) since the history/ratings/watchlist
    shapes are identical by design. Collection, comments, and the four
    secret-bearing categories have no Trakt-import equivalent and are applied
    here directly.

    Secret categories never overwrite a value that's already set locally —
    restoring only fills gaps (e.g. a fresh account, or a connection that was
    since removed), so it can't clobber a currently-working connection with a
    stale exported one.
    """
    from routers.sync import _raise_if_cancelled
    from routers.trakt import (
        _apply_imported_rating,
        _get_or_create_episode_media,
        _get_or_create_movie_media,
        _get_or_create_series_media,
        _get_or_create_show,
    )

    stats = {
        "movies": 0, "episodes": 0, "ratings": 0, "collected": 0,
        "lists": 0, "list_items": 0, "comments": 0, "connections": 0,
        "skipped": 0, "errors": 0,
    }
    processed = 0

    total = 0
    if include_watched:
        total += len(data.history_movies) + len(data.history_episodes)
    if include_ratings:
        total += sum(len(v) for v in data.ratings.values())
    if include_collection:
        total += len(data.collection_movies) + len(data.collection_episodes)
    if include_lists:
        total += len(data.watchlist) + sum(len(v) for v in data.list_items.values())
    if include_comments:
        total += sum(len(v) for v in data.comments.values())
    await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(total_items=total))
    await db.commit()

    async def _tick() -> None:
        nonlocal processed
        processed += 1
        if processed % 50 == 0:
            await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(processed_items=processed))
            await db.commit()
            await _raise_if_cancelled(db, job_id)

    # ── Watch history ──────────────────────────────────────────────────
    if include_watched:
        we_result = await db.execute(select(WatchEvent.media_id, WatchEvent.watched_at).where(WatchEvent.user_id == user_id))
        existing_watched: set[tuple[int, datetime | None]] = {(r[0], r[1]) for r in we_result}

        for entry in data.history_movies:
            tmdb_id = entry.get("movie", {}).get("ids", {}).get("tmdb")
            try:
                if not tmdb_id:
                    stats["skipped"] += 1
                    continue
                try:
                    async with db.begin_nested():
                        media = await _get_or_create_movie_media(db, tmdb_id, entry.get("movie", {}).get("title", ""), api_key)
                        if not media:
                            stats["errors"] += 1
                            continue
                        watched_at = _parse_iso(entry.get("watched_at"))
                        key = (media.id, watched_at)
                        if key not in existing_watched:
                            db.add(WatchEvent(user_id=user_id, media_id=media.id, watched_at=watched_at, completed=True, play_count=1))
                            existing_watched.add(key)
                            stats["movies"] += 1
                        else:
                            stats["skipped"] += 1
                except Exception:
                    logger.exception("Error importing history movie tmdb=%s", tmdb_id)
                    stats["errors"] += 1
            finally:
                # Outside the nested-transaction try/except above (a savepoint
                # must be fully closed before _tick()'s own commit can run).
                await _tick()
        await db.commit()

        shows_by_tmdb: dict[int, Show] = {}
        for entry in data.history_episodes:
            show_tmdb_id = entry.get("show", {}).get("ids", {}).get("tmdb")
            ep_data = entry.get("episode", {})
            season_number, episode_number = ep_data.get("season"), ep_data.get("number")
            try:
                if not show_tmdb_id or season_number is None or episode_number is None:
                    stats["skipped"] += 1
                    continue
                try:
                    async with db.begin_nested():
                        show = shows_by_tmdb.get(show_tmdb_id)
                        if not show:
                            show = await _get_or_create_show(db, show_tmdb_id, entry.get("show", {}).get("title", ""), api_key)
                            if show:
                                shows_by_tmdb[show_tmdb_id] = show
                        if not show:
                            stats["errors"] += 1
                            continue
                        media = await _get_or_create_episode_media(db, show.id, show_tmdb_id, season_number, episode_number, api_key)
                        if not media:
                            stats["errors"] += 1
                            continue
                        watched_at = _parse_iso(entry.get("watched_at"))
                        key = (media.id, watched_at)
                        if key not in existing_watched:
                            db.add(WatchEvent(user_id=user_id, media_id=media.id, watched_at=watched_at, completed=True, play_count=1))
                            existing_watched.add(key)
                            stats["episodes"] += 1
                        else:
                            stats["skipped"] += 1
                except Exception:
                    logger.exception("Error importing history episode show_tmdb=%s S%sE%s", show_tmdb_id, season_number, episode_number)
                    stats["errors"] += 1
            finally:
                await _tick()
        await db.commit()

    # ── Ratings ────────────────────────────────────────────────────────
    if include_ratings:
        existing_ratings_result = await db.execute(select(Rating).where(Rating.user_id == user_id))
        existing_ratings: dict[tuple[int, int | None], Rating] = {(r.media_id, r.season_number): r for r in existing_ratings_result.scalars()}
        _new_ratings: RatingChanges = {}
        shows_by_tmdb2: dict[int, Show] = {}

        for kind, entries in data.ratings.items():
            for entry in entries:
                try:
                    try:
                        async with db.begin_nested():
                            media: Media | None = None
                            season_number: int | None = None
                            if kind == "movies":
                                tmdb_id = entry.get("movie", {}).get("ids", {}).get("tmdb")
                                if tmdb_id:
                                    media = await _get_or_create_movie_media(db, tmdb_id, entry.get("movie", {}).get("title", ""), api_key)
                            elif kind == "shows":
                                tmdb_id = entry.get("show", {}).get("ids", {}).get("tmdb")
                                if tmdb_id:
                                    media = await _get_or_create_series_media(db, tmdb_id, entry.get("show", {}).get("title", ""), api_key)
                            elif kind == "seasons":
                                tmdb_id = entry.get("show", {}).get("ids", {}).get("tmdb")
                                season_number = entry.get("season", {}).get("number")
                                if tmdb_id:
                                    media = await _get_or_create_series_media(db, tmdb_id, entry.get("show", {}).get("title", ""), api_key)
                            elif kind == "episodes":
                                show_tmdb_id = entry.get("show", {}).get("ids", {}).get("tmdb")
                                ep_data = entry.get("episode", {})
                                if show_tmdb_id and ep_data.get("season") is not None and ep_data.get("number") is not None:
                                    show = shows_by_tmdb2.get(show_tmdb_id)
                                    if not show:
                                        show = await _get_or_create_show(db, show_tmdb_id, entry.get("show", {}).get("title", ""), api_key)
                                        if show:
                                            shows_by_tmdb2[show_tmdb_id] = show
                                    if show:
                                        media = await _get_or_create_episode_media(db, show.id, show_tmdb_id, ep_data["season"], ep_data["number"], api_key)

                            if not media:
                                stats["skipped"] += 1
                                continue
                            if _apply_imported_rating(db, user_id, media, season_number, entry, existing_ratings, _new_ratings):
                                stats["ratings"] += 1
                            else:
                                stats["skipped"] += 1
                    except Exception:
                        logger.exception("Error importing %s rating", kind)
                        stats["errors"] += 1
                finally:
                    await _tick()
        await db.commit()

    # ── Collection ─────────────────────────────────────────────────────
    if include_collection:
        async def _add_to_collection(media: Media) -> bool:
            existing = await db.execute(select(Collection).where(Collection.user_id == user_id, Collection.media_id == media.id))
            coll = existing.scalars().first()
            if coll:
                return False
            coll = Collection(user_id=user_id, media_id=media.id)
            db.add(coll)
            await db.flush()
            db.add(CollectionFile(collection_id=coll.id, source=CollectionSource.manual, source_id=str(media.tmdb_id)))
            return True

        for entry in data.collection_movies:
            tmdb_id = entry.get("movie", {}).get("ids", {}).get("tmdb")
            try:
                if not tmdb_id:
                    stats["skipped"] += 1
                    continue
                try:
                    async with db.begin_nested():
                        media = await _get_or_create_movie_media(db, tmdb_id, entry.get("movie", {}).get("title", ""), api_key)
                        if media and await _add_to_collection(media):
                            stats["collected"] += 1
                        else:
                            stats["skipped"] += 1
                except Exception:
                    logger.exception("Error importing collected movie tmdb=%s", tmdb_id)
                    stats["errors"] += 1
            finally:
                await _tick()
        await db.commit()

        shows_by_tmdb3: dict[int, Show] = {}
        for entry in data.collection_episodes:
            show_tmdb_id = entry.get("show", {}).get("ids", {}).get("tmdb")
            ep_data = entry.get("episode", {})
            season_number, episode_number = ep_data.get("season"), ep_data.get("number")
            try:
                if not show_tmdb_id or season_number is None or episode_number is None:
                    stats["skipped"] += 1
                    continue
                try:
                    async with db.begin_nested():
                        show = shows_by_tmdb3.get(show_tmdb_id)
                        if not show:
                            show = await _get_or_create_show(db, show_tmdb_id, entry.get("show", {}).get("title", ""), api_key)
                            if show:
                                shows_by_tmdb3[show_tmdb_id] = show
                        if not show:
                            stats["errors"] += 1
                            continue
                        media = await _get_or_create_episode_media(db, show.id, show_tmdb_id, season_number, episode_number, api_key)
                        if media and await _add_to_collection(media):
                            stats["collected"] += 1
                        else:
                            stats["skipped"] += 1
                except Exception:
                    logger.exception("Error importing collected episode show_tmdb=%s S%sE%s", show_tmdb_id, season_number, episode_number)
                    stats["errors"] += 1
            finally:
                await _tick()
        await db.commit()

    # ── Lists (watchlist + custom, matched by name) ───────────────────
    if include_lists:
        async def _resolve_list_item_media(entry: dict) -> Media | None:
            if entry.get("type") == "movie":
                tmdb_id = entry.get("movie", {}).get("ids", {}).get("tmdb")
                if not tmdb_id:
                    return None
                async with db.begin_nested():
                    return await _get_or_create_movie_media(db, tmdb_id, entry.get("movie", {}).get("title", ""), api_key)
            elif entry.get("type") == "show":
                tmdb_id = entry.get("show", {}).get("ids", {}).get("tmdb")
                if not tmdb_id:
                    return None
                async with db.begin_nested():
                    return await _get_or_create_series_media(db, tmdb_id, entry.get("show", {}).get("title", ""), api_key)
            return None

        wl_result = await db.execute(select(ListModel).where(ListModel.user_id == user_id, ListModel.trakt_slug == WATCHLIST_SLUG))
        watchlist = wl_result.scalar_one_or_none()
        if not watchlist and data.watchlist:
            watchlist = ListModel(user_id=user_id, name="Watchlist", trakt_slug=WATCHLIST_SLUG)
            db.add(watchlist)
            await db.flush()
            stats["lists"] += 1

        if watchlist:
            wl_items_result = await db.execute(select(ListItem.media_id).where(ListItem.list_id == watchlist.id))
            wl_existing_ids = {r[0] for r in wl_items_result}
            for entry in data.watchlist:
                try:
                    media = await _resolve_list_item_media(entry)
                    if media and media.id not in wl_existing_ids:
                        db.add(ListItem(list_id=watchlist.id, media_id=media.id))
                        wl_existing_ids.add(media.id)
                        stats["list_items"] += 1
                    else:
                        stats["skipped"] += 1
                except Exception:
                    logger.exception("Error importing watchlist item")
                    stats["errors"] += 1
                await _tick()
            await db.commit()

        for meta in data.lists:
            slug = meta.get("ids", {}).get("slug")
            items = data.list_items.get(slug, [])
            name = meta.get("name")
            if not name:
                continue

            existing_list_result = await db.execute(select(ListModel).where(ListModel.user_id == user_id, ListModel.name == name))
            local_list = existing_list_result.scalar_one_or_none()
            if not local_list:
                try:
                    privacy = PrivacyLevel(meta.get("privacy", "private"))
                except ValueError:
                    privacy = PrivacyLevel.private
                local_list = ListModel(user_id=user_id, name=name, description=meta.get("description") or None, privacy_level=privacy)
                db.add(local_list)
                await db.flush()
                stats["lists"] += 1

            existing_items_result = await db.execute(select(ListItem.media_id).where(ListItem.list_id == local_list.id))
            existing_item_ids = {r[0] for r in existing_items_result}
            for entry in items:
                try:
                    media = await _resolve_list_item_media(entry)
                    if media and media.id not in existing_item_ids:
                        db.add(ListItem(list_id=local_list.id, media_id=media.id))
                        existing_item_ids.add(media.id)
                        stats["list_items"] += 1
                    else:
                        stats["skipped"] += 1
                except Exception:
                    logger.exception("Error importing item for list %s", name)
                    stats["errors"] += 1
                await _tick()
            await db.commit()

    # ── Comments ───────────────────────────────────────────────────────
    if include_comments:
        def _comment_key(media_type: str, tmdb_id: int | None, season_number: int | None, episode_number: int | None, content: str) -> tuple:
            return (media_type, tmdb_id, season_number, episode_number, content)

        existing_comments_result = await db.execute(select(Comment).where(Comment.user_id == user_id))
        existing_comment_keys = {
            _comment_key(c.media_type, c.tmdb_id, c.season_number, c.episode_number, c.content)
            for c in existing_comments_result.scalars()
        }

        def _add_comment(media_type: str, tmdb_id: int | None, season_number: int | None, episode_number: int | None, entry: dict) -> None:
            if not tmdb_id:
                stats["skipped"] += 1
                return
            content = entry.get("comment") or ""
            key = _comment_key(media_type, tmdb_id, season_number, episode_number, content)
            if key in existing_comment_keys:
                stats["skipped"] += 1
                return
            db.add(Comment(
                user_id=user_id, media_type=media_type, tmdb_id=tmdb_id,
                season_number=season_number, episode_number=episode_number,
                content=content, is_spoiler=bool(entry.get("spoiler")),
                created_at=_parse_iso(entry.get("created_at")) or datetime.utcnow(),
            ))
            existing_comment_keys.add(key)
            stats["comments"] += 1

        for entry in data.comments.get("movies", []):
            _add_comment("movie", entry.get("movie", {}).get("ids", {}).get("tmdb"), None, None, entry)
            await _tick()
        for entry in data.comments.get("shows", []):
            _add_comment("series", entry.get("show", {}).get("ids", {}).get("tmdb"), None, None, entry)
            await _tick()
        for entry in data.comments.get("seasons", []):
            _add_comment("series", entry.get("show", {}).get("ids", {}).get("tmdb"), entry.get("season", {}).get("number"), None, entry)
            await _tick()
        for entry in data.comments.get("episodes", []):
            ep = entry.get("episode", {})
            _add_comment("episode", entry.get("ids", {}).get("tmdb"), ep.get("season"), ep.get("number"), entry)
            await _tick()
        await db.commit()

    # ── Secret categories (fill-gaps-only, never overwrite) ───────────
    if include_api_keys and data.api_keys:
        settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
        settings = settings_result.scalar_one_or_none()
        if settings:
            for field_name in ("tmdb_api_key", "tvdb_api_key"):
                value = data.api_keys.get(field_name)
                if value and not getattr(settings, field_name):
                    setattr(settings, field_name, value)
                    stats["connections"] += 1
            await db.commit()

    if include_media_connections and data.media_connections:
        for entry in data.media_connections:
            existing_result = await db.execute(select(MediaServerConnection).where(
                MediaServerConnection.user_id == user_id,
                MediaServerConnection.type == entry.get("type"),
                MediaServerConnection.url == entry.get("url"),
            ))
            if existing_result.scalars().first():
                stats["skipped"] += 1
                continue
            db.add(MediaServerConnection(user_id=user_id, **{k: v for k, v in entry.items()}))
            stats["connections"] += 1
        await db.commit()

    if include_scrobble_connections and data.scrobble_connections:
        for entry in data.scrobble_connections:
            conditions = [ScrobbleConnection.user_id == user_id, ScrobbleConnection.type == entry.get("type")]
            if entry.get("server_user_id"):
                conditions.append(ScrobbleConnection.server_user_id == entry.get("server_user_id"))
            elif entry.get("server_username"):
                conditions.append(ScrobbleConnection.server_username == entry.get("server_username"))
            existing_result = await db.execute(select(ScrobbleConnection).where(*conditions))
            if existing_result.scalars().first():
                stats["skipped"] += 1
                continue
            db.add(ScrobbleConnection(user_id=user_id, **{k: v for k, v in entry.items()}))
            stats["connections"] += 1
        await db.commit()

    if include_connections and data.connections:
        settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
        settings = settings_result.scalar_one_or_none()
        if settings:
            for field_name in _CONNECTIONS_SETTINGS_FIELDS:
                if field_name not in data.connections:
                    continue
                value = data.connections[field_name]
                if value and not getattr(settings, field_name, None):
                    setattr(settings, field_name, value)
                    stats["connections"] += 1
            await db.commit()

    await db.execute(update(SyncJob).where(SyncJob.id == job_id).values(processed_items=processed))
    await db.commit()

    return stats

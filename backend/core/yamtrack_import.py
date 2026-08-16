"""Parser for a Yamtrack (https://github.com/FuzzyGrim/Yamtrack) or Floppy
(https://github.com/dannyvfilms/Floppy, a Yamtrack fork) CSV export.

Both projects generate their CSV header dynamically from their own Django
models, so column order isn't fixed - rows are read by column name, not
position. Floppy's format is a strict superset of Yamtrack's: an extra
leading `row_type` column (media/list/list_item/collection) plus trailing
list- and collection-specific columns. Floppy's own importer defaults
row_type to "media" when the column is absent, which is exactly a vanilla
Yamtrack file - this parser does the same, so one code path covers both.

Produces the same ScrobImportData shape core/scrob_import.py's own export
parser does, so apply_scrob_import can consume it unmodified.
"""

import csv
import io
import logging

from core.scrob_import import MAX_TOTAL_SIZE, ScrobImportData

logger = logging.getLogger(__name__)

_REQUIRED_COLUMNS = {"media_id", "source", "media_type"}
_STATUS_COMPLETED = "completed"
_STATUS_PLANNING = "planning"


def _norm(value: str | None) -> str:
    return (value or "").strip()


def _norm_lower(value: str | None) -> str:
    return _norm(value).lower()


def _parse_int(value: str | None) -> int | None:
    value = _norm(value)
    if not value:
        return None
    try:
        # Tolerate "1.0"-style values defensively - neither project emits
        # these for season/episode numbers, but a hand-edited CSV might.
        # float() accepts "inf"/"1e400"-style strings without raising, and
        # int() on an infinite float raises OverflowError rather than
        # ValueError - a crafted CSV value must not crash the whole import.
        return int(float(value))
    except (ValueError, OverflowError):
        return None


def _first_nonempty(row: dict, *keys: str) -> str | None:
    for key in keys:
        value = _norm(row.get(key))
        if value:
            return value
    return None


def parse_yamtrack_csv(content: bytes) -> ScrobImportData:
    """Parse a Yamtrack or Floppy CSV export into ScrobImportData.

    Raises ValueError with a user-facing message if the file isn't a
    recognizable Yamtrack/Floppy export.
    """
    if len(content) > MAX_TOTAL_SIZE:
        raise ValueError("Export file is too large to import.")

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("This doesn't look like a valid CSV file (not UTF-8 text).")

    reader = csv.DictReader(io.StringIO(text))
    try:
        fieldnames = set(reader.fieldnames or [])
    except csv.Error as exc:
        # e.g. a single field over Python's 128KB csv field-size limit -
        # well within our overall file-size cap, so a real, reachable case,
        # not just a hypothetical.
        raise ValueError(f"This CSV file is malformed and couldn't be read: {exc}")
    if not _REQUIRED_COLUMNS.issubset(fieldnames):
        raise ValueError(
            "This doesn't look like a Yamtrack or Floppy export - "
            "expected columns like media_id, source, and media_type."
        )
    has_row_type = "row_type" in fieldnames

    data = ScrobImportData()
    skipped_non_tmdb = 0

    try:
        for row in reader:
            row_type = _norm_lower(row.get("row_type")) if has_row_type else "media"

            if row_type == "list":
                _process_list_row(row, data)
                continue

            if row_type not in ("", "media", "list_item", "collection"):
                continue

            # Yamtrack/Floppy support non-TMDB sources (mal for anime/manga,
            # igdb for games, openlibrary for books, musicbrainz for music,
            # etc.) - Scrob is TMDB-only, so these have nothing to match
            # against and are skipped, same as a Trakt entry missing a tmdb id.
            source = _norm_lower(row.get("source"))
            if source != "tmdb":
                if source:
                    skipped_non_tmdb += 1
                continue

            if row_type == "list_item":
                _process_list_item_row(row, data)
            elif row_type == "collection":
                _process_collection_row(row, data)
            else:
                _process_media_row(row, data)
    except csv.Error as exc:
        raise ValueError(f"This CSV file is malformed and couldn't be read: {exc}")

    if skipped_non_tmdb:
        logger.info("Yamtrack/Floppy import: skipped %s non-TMDB row(s)", skipped_non_tmdb)

    return data


def _process_media_row(row: dict, data: ScrobImportData) -> None:
    media_type = _norm_lower(row.get("media_type"))
    tmdb_id = _parse_int(row.get("media_id"))
    if tmdb_id is None:
        return
    title = _norm(row.get("title"))
    status = _norm_lower(row.get("status"))
    score = _norm(row.get("score"))
    notes = _norm(row.get("notes"))
    # created_at is the one date field every media row is guaranteed to have
    # (auto-set on creation in both projects); end_date/progressed_at are
    # preferred when present since they reflect the actual watch date.
    watched_at = _first_nonempty(row, "end_date", "progressed_at", "created_at")
    rated_at = _first_nonempty(row, "created_at", "end_date", "progressed_at")

    if media_type == "movie":
        movie = {"ids": {"tmdb": tmdb_id}, "title": title}
        if status == _STATUS_COMPLETED:
            data.history_movies.append({"movie": movie, "watched_at": watched_at})
        if status == _STATUS_PLANNING:
            data.watchlist.append({"type": "movie", "movie": movie})
        if score:
            data.ratings.setdefault("movies", []).append({"rating": score, "rated_at": rated_at, "movie": movie})
        if notes:
            data.comments.setdefault("movies", []).append({"comment": notes, "created_at": rated_at, "movie": movie})
        return

    if media_type == "tv":
        show = {"ids": {"tmdb": tmdb_id}, "title": title}
        if status == _STATUS_PLANNING:
            data.watchlist.append({"type": "show", "show": show})
        if score:
            data.ratings.setdefault("shows", []).append({"rating": score, "rated_at": rated_at, "show": show})
        if notes:
            data.comments.setdefault("shows", []).append({"comment": notes, "created_at": rated_at, "show": show})
        return

    if media_type == "season":
        season_number = _parse_int(row.get("season_number"))
        if season_number is None:
            return
        show = {"ids": {"tmdb": tmdb_id}, "title": title}
        if score:
            data.ratings.setdefault("seasons", []).append({
                "rating": score, "rated_at": rated_at, "show": show, "season": {"number": season_number},
            })
        if notes:
            data.comments.setdefault("seasons", []).append({
                "comment": notes, "created_at": rated_at, "show": show, "season": {"number": season_number},
            })
        return

    if media_type == "episode":
        season_number = _parse_int(row.get("season_number"))
        episode_number = _parse_int(row.get("episode_number"))
        if season_number is None or episode_number is None:
            return
        # An episode row has no status/score of its own in either project
        # (their Episode model doesn't track those) - its mere presence in
        # the export is the watched signal.
        data.history_episodes.append({
            "show": {"ids": {"tmdb": tmdb_id}, "title": title},
            "episode": {"season": season_number, "number": episode_number},
            "watched_at": watched_at,
        })
        return

    # Any other media_type (anime/manga/game/book/comic/boardgame/podcast/
    # music_artist/music_album) has no TMDB-matchable equivalent in Scrob.
    # In practice these never carry source=tmdb, so this is unreachable
    # given the caller's filter, kept only as a defensive no-op.


def _process_collection_row(row: dict, data: ScrobImportData) -> None:
    """Floppy-only: a `collection` row means "I own this," independent of
    watch status - vanilla Yamtrack has no such concept, so this only ever
    fires for a Floppy export."""
    media_type = _norm_lower(row.get("media_type"))
    tmdb_id = _parse_int(row.get("media_id"))
    if tmdb_id is None:
        return
    title = _norm(row.get("title"))

    if media_type == "movie":
        data.collection_movies.append({"movie": {"ids": {"tmdb": tmdb_id}, "title": title}})
    elif media_type == "episode":
        season_number = _parse_int(row.get("season_number"))
        episode_number = _parse_int(row.get("episode_number"))
        if season_number is None or episode_number is None:
            return
        data.collection_episodes.append({
            "show": {"ids": {"tmdb": tmdb_id}, "title": title},
            "episode": {"season": season_number, "number": episode_number},
        })
    # Scrob's Collection is per-movie/per-episode only (no show/season-level
    # ownership concept) - tv/season collection rows are skipped.


def _process_list_row(row: dict, data: ScrobImportData) -> None:
    """Floppy-only: the one authoritative metadata row per custom list."""
    list_uid = _norm(row.get("list_uid"))
    name = _norm(row.get("list_name"))
    if not list_uid or not name:
        return
    visibility = _norm_lower(row.get("list_visibility"))
    privacy = visibility if visibility in ("public", "private") else "private"
    data.lists.append({
        "name": name,
        "description": _norm(row.get("list_description")) or None,
        "privacy": privacy,
        "ids": {"slug": list_uid},
    })


def _process_list_item_row(row: dict, data: ScrobImportData) -> None:
    """Floppy-only: one row per item on a custom list, referencing its
    parent list by list_uid (the "list" row carries the full metadata)."""
    list_uid = _norm(row.get("list_uid"))
    if not list_uid:
        return
    media_type = _norm_lower(row.get("media_type"))
    tmdb_id = _parse_int(row.get("media_id"))
    if tmdb_id is None:
        return
    title = _norm(row.get("title"))

    if media_type == "movie":
        entry = {"type": "movie", "movie": {"ids": {"tmdb": tmdb_id}, "title": title}}
    elif media_type == "tv":
        entry = {"type": "show", "show": {"ids": {"tmdb": tmdb_id}, "title": title}}
    else:
        # Season/episode-level list items aren't supported by the existing
        # import apply logic (_resolve_list_item_media in
        # core/scrob_import.py only resolves movie/show entries) - not a
        # limitation introduced here.
        return

    data.list_items.setdefault(list_uid, []).append(entry)

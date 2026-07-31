"""Parser for a Trakt.tv personal data export (the zip downloaded from
Settings → Data Export on trakt.tv).

The export's JSON files use (almost) the same shapes as the corresponding
live Trakt API responses, so the result of parse_trakt_export() is designed
to be a drop-in source for the same import logic that consumes the live API
(see ExportTraktSource in routers/trakt.py).
"""

import io
import json
import re
import zipfile
from dataclasses import dataclass, field

MAX_ENTRY_SIZE = 100 * 1024 * 1024
MAX_TOTAL_SIZE = 500 * 1024 * 1024
_READ_CHUNK_SIZE = 1024 * 1024

_HISTORY_RE = re.compile(r"^watched-history-\d+\.json$")
_LIST_ITEMS_RE = re.compile(r"^lists-list-(\d+)-(.+)\.json$")


@dataclass
class TraktExportData:
    history_movies: list[dict] = field(default_factory=list)
    history_episodes: list[dict] = field(default_factory=list)
    ratings: dict[str, list[dict]] = field(default_factory=dict)
    watchlist: list[dict] = field(default_factory=list)
    lists: list[dict] = field(default_factory=list)
    list_items: dict[str, list[dict]] = field(default_factory=dict)


def parse_trakt_export(content: bytes) -> TraktExportData:
    """Parse a Trakt export zip into the shapes _apply_trakt_import expects.

    Raises ValueError with a user-facing message if the file isn't a valid
    or recognizable Trakt export.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        raise ValueError("This file doesn't look like a valid Trakt export (.zip).")

    names = set(zf.namelist())

    if not any(_HISTORY_RE.match(n) for n in names):
        raise ValueError(
            "This doesn't look like a Trakt data export — watched-history files are missing."
        )

    # ZipInfo.file_size is metadata declared inside the zip itself — an attacker
    # can freely lie about it (e.g. declare 100 bytes while the entry actually
    # inflates to hundreds of MB), so it must never be trusted as a decompression
    # cap. Enforce the limits against bytes actually produced while streaming
    # each entry out, aborting mid-read the moment either cap is exceeded.
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

    def _load(name: str) -> list:
        if name not in names:
            return []
        data = json.loads(_read_limited(name))
        return data if isinstance(data, list) else []

    def _load_history() -> list[dict]:
        matches = sorted(
            (n for n in names if _HISTORY_RE.match(n)),
            key=lambda n: int(re.search(r"-(\d+)\.json$", n).group(1)),
        )
        items: list[dict] = []
        for n in matches:
            items.extend(_load(n))
        return items

    history = _load_history()
    history_movies = [e for e in history if e.get("type") == "movie"]
    history_episodes = [e for e in history if e.get("type") == "episode"]

    ratings = {
        "movies": _load("ratings-movies.json"),
        "shows": _load("ratings-shows.json"),
        "seasons": _load("ratings-seasons.json"),
        "episodes": _load("ratings-episodes.json"),
    }

    watchlist = _load("lists-watchlist.json")
    lists_meta = _load("lists-lists.json")

    id_to_filename: dict[int, str] = {}
    for n in names:
        m = _LIST_ITEMS_RE.match(n)
        if m:
            id_to_filename[int(m.group(1))] = n

    list_items: dict[str, list[dict]] = {}
    for lst in lists_meta:
        trakt_id = lst.get("ids", {}).get("trakt")
        slug = lst.get("ids", {}).get("slug")
        fname = id_to_filename.get(trakt_id)
        if slug and fname:
            list_items[slug] = _load(fname)

    return TraktExportData(
        history_movies=history_movies,
        history_episodes=history_episodes,
        ratings=ratings,
        watchlist=watchlist,
        lists=lists_meta,
        list_items=list_items,
    )

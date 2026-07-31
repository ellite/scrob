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

    infos = zf.infolist()
    total_size = sum(i.file_size for i in infos)
    if total_size > MAX_TOTAL_SIZE or any(i.file_size > MAX_ENTRY_SIZE for i in infos):
        raise ValueError("Export file is too large to import.")

    names = set(zf.namelist())

    if not any(_HISTORY_RE.match(n) for n in names):
        raise ValueError(
            "This doesn't look like a Trakt data export — watched-history files are missing."
        )

    def _load(name: str) -> list:
        if name not in names:
            return []
        with zf.open(name) as f:
            data = json.load(f)
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

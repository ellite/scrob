"""Persistent external id -> TMDB id cache.

TMDB's /find endpoint is the only way to turn an IMDb or TVDB id into a TMDB
id, and several providers (Nuvio, Stremio, Plex, Jellyfin, Emby, MDBList, and
every scrobble webhook) speak external ids while our database is TMDB-keyed.
Resolving them on every sync is the single largest cost in a sync run: a
2,000-item watched-only Nuvio library was making 2,000 /find calls per sync.

This module resolves in batch, caches the answers globally and permanently in
`external_id_mappings`, and only calls TMDB for genuinely new ids.

Session ownership
-----------------
Unlike the rest of `core/`, this module opens its own sessions rather than
taking `db`. That is deliberate:

* "tt0903747 is TMDB show 1396" is a fact about the world, not about the
  caller's transaction. Syncs roll back (routers/sync.py sync_jellyfin/
  sync_plex both call db.rollback() on failure) and would otherwise discard
  exactly the mappings that just cost a full TMDB fan-out to obtain.
* It keeps the number of `db.execute` calls at every call site unchanged,
  which matters because the callers are threaded through hand-rolled test
  fakes that replay a fixed, ordered list of results.

Sessions are always closed before any network call, so a connection is never
held across TMDB latency.
"""
import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Iterable, Sequence

from sqlalchemy import and_, case, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core import tmdb
from db import AsyncSessionLocal
from models.external_id_mapping import ExternalIdMapping

logger = logging.getLogger(__name__)

# Sources, matching TMDB's `external_source` query parameter.
IMDB = "imdb_id"
TVDB = "tvdb_id"

# Which /find result bucket the caller wants. For TV_EPISODE_SHOW the value we
# store is the PARENT SHOW's id (tv_episode_results[0].show_id) — named
# explicitly so it cannot quietly be mistaken for an episode id.
MOVIE = "movie"
TV = "tv"
TV_EPISODE_SHOW = "tv_episode_show"

_BUCKETS = {
    MOVIE: ("movie_results", "id"),
    TV: ("tv_results", "id"),
    TV_EPISODE_SHOW: ("tv_episode_results", "show_id"),
}

# (source, external_id, media_kind)
Pair = tuple[str, str, str]

CONCURRENCY = 5

# asyncpg caps a statement at 32767 bind parameters; 500 rows x 7 columns is
# well under, and matches the batch size used elsewhere in the codebase.
BATCH_SIZE = 500

# Negative entries (TMDB answered, no match) are retried on exponential
# backoff: 1d, 2d, 4d ... capped at 30d. A title released yesterday may simply
# not be in TMDB yet and should be retried soon; one that has never existed
# should approach never being retried. Positives never expire.
_NEGATIVE_BASE = timedelta(days=1)
_NEGATIVE_MAX = timedelta(days=30)

# Small in-process memo in front of the table, positives only. Negatives must
# always go to the database so miss_count backoff is not bypassed.
_MEMO_TTL = 900.0
_MEMO_MAXSIZE = 4096
_memo: dict[Pair, tuple[float, int]] = {}


def reset_memo() -> None:
    """Clear the in-process memo. Tests must call this between cases that
    assert on TMDB call counts."""
    _memo.clear()


def _memo_get(pair: Pair) -> int | None:
    entry = _memo.get(pair)
    if entry is None:
        return None
    expires_at, tmdb_id = entry
    if time.monotonic() >= expires_at:
        del _memo[pair]
        return None
    return tmdb_id


def _memo_set(pair: Pair, tmdb_id: int) -> None:
    if pair not in _memo and len(_memo) >= _MEMO_MAXSIZE:
        _memo.pop(next(iter(_memo)))
    _memo[pair] = (time.monotonic() + _MEMO_TTL, tmdb_id)


def make_pair(source: str, external_id: str | int, media_kind: str) -> Pair | None:
    """Normalise a candidate into a cache key, or None if it isn't usable."""
    value = str(external_id or "").strip()
    if not value or media_kind not in _BUCKETS:
        return None
    if source == IMDB:
        value = value.lower()
        if not (value.startswith("tt") and value[2:].isdigit()):
            return None
    elif source == TVDB:
        if not value.isdigit():
            return None
    else:
        return None
    return (source, value, media_kind)


def _negative_is_fresh(row: ExternalIdMapping, now: datetime) -> bool:
    ttl = min(_NEGATIVE_BASE * (2 ** min(row.miss_count, 5)), _NEGATIVE_MAX)
    return (now - row.checked_at) < ttl


async def _load(pairs: Sequence[Pair]) -> dict[Pair, ExternalIdMapping]:
    """One SELECT for the whole batch, grouped by (source, media_kind)."""
    grouped: dict[tuple[str, str], set[str]] = defaultdict(set)
    for source, external_id, media_kind in pairs:
        grouped[(source, media_kind)].add(external_id)

    found: dict[Pair, ExternalIdMapping] = {}
    async with AsyncSessionLocal() as db:
        clauses = []
        for (source, media_kind), id_set in grouped.items():
            ids = sorted(id_set)
            for i in range(0, len(ids), BATCH_SIZE):
                clauses.append(
                    and_(
                        ExternalIdMapping.source == source,
                        ExternalIdMapping.media_kind == media_kind,
                        ExternalIdMapping.external_id.in_(ids[i : i + BATCH_SIZE]),
                    )
                )
        for i in range(0, len(clauses), 50):
            result = await db.execute(
                select(ExternalIdMapping).where(or_(*clauses[i : i + 50]))
            )
            for row in result.scalars():
                found[(row.source, row.external_id, row.media_kind)] = row
    return found


async def _store(rows: list[dict]) -> None:
    """One chunked upsert for the whole batch."""
    if not rows:
        return
    # Deterministic lock ordering so concurrent syncs cannot deadlock.
    rows.sort(key=lambda r: (r["source"], r["media_kind"], r["external_id"]))
    async with AsyncSessionLocal() as db:
        for i in range(0, len(rows), BATCH_SIZE):
            stmt = pg_insert(ExternalIdMapping).values(rows[i : i + BATCH_SIZE])
            await db.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_external_id_mappings_lookup",
                    set_={
                        "tmdb_id": stmt.excluded.tmdb_id,
                        "checked_at": stmt.excluded.checked_at,
                        # Reset the backoff on a hit; deepen it on a repeat miss.
                        # Computed server-side so concurrent syncs cannot lose
                        # an increment.
                        "miss_count": case(
                            (stmt.excluded.tmdb_id.isnot(None), 0),
                            else_=ExternalIdMapping.miss_count + 1,
                        ),
                    },
                )
            )
        await db.commit()


async def _fetch(pairs: list[Pair], api_key: str | None) -> tuple[dict[Pair, int | None], list[dict]]:
    """Resolve pairs against TMDB. Returns (resolutions, rows-to-store).

    One /find call serves every media_kind of the same id. A request that
    FAILS produces no row: a timeout or 5xx must never be persisted as
    "TMDB has no match for this".
    """
    resolutions: dict[Pair, int | None] = {}
    rows: list[dict] = []
    now = datetime.utcnow()
    semaphore = asyncio.Semaphore(CONCURRENCY)

    by_id: dict[tuple[str, str], set[str]] = defaultdict(set)
    for source, external_id, media_kind in pairs:
        by_id[(source, external_id)].add(media_kind)

    async def one(source: str, external_id: str, kinds: set[str]) -> None:
        async with semaphore:
            try:
                payload = await tmdb.find_by_external_id(external_id, source, api_key=api_key)
            except Exception as exc:
                logger.warning("TMDB /find failed for %s=%s: %s", source, external_id, exc)
                for kind in kinds:
                    resolutions[(source, external_id, kind)] = None
                return

        for kind, (bucket, id_field) in _BUCKETS.items():
            matches = payload.get(bucket) or []
            tmdb_id = None
            if matches:
                try:
                    tmdb_id = int(matches[0].get(id_field))
                except (TypeError, ValueError):
                    tmdb_id = None
            if tmdb_id is not None:
                # Record every non-empty bucket, even ones nobody asked about —
                # the response is already paid for and another caller will want it.
                rows.append({
                    "source": source, "external_id": external_id, "media_kind": kind,
                    "tmdb_id": tmdb_id, "miss_count": 0, "checked_at": now, "created_at": now,
                })
            elif kind in kinds:
                # Negative only for kinds actually asked about, so row growth
                # and retry cadence track real demand.
                rows.append({
                    "source": source, "external_id": external_id, "media_kind": kind,
                    "tmdb_id": None, "miss_count": 0, "checked_at": now, "created_at": now,
                })
            if kind in kinds:
                resolutions[(source, external_id, kind)] = tmdb_id

    await asyncio.gather(*(one(s, e, k) for (s, e), k in sorted(by_id.items())))
    return resolutions, rows


async def resolve_many(pairs: Iterable[Pair], api_key: str | None) -> dict[Pair, int | None]:
    """Resolve a batch of external ids to TMDB ids.

    Returns an entry for every distinct input pair; unresolvable ones map to
    None. Never raises for an individual id.
    """
    wanted = {p for p in pairs if p}
    if not wanted:
        return {}

    resolved: dict[Pair, int | None] = {}
    outstanding: set[Pair] = set()
    for pair in wanted:
        hit = _memo_get(pair)
        if hit is not None:
            resolved[pair] = hit
        else:
            outstanding.add(pair)

    if outstanding:
        now = datetime.utcnow()
        cached = await _load(sorted(outstanding))
        for pair, row in cached.items():
            if row.tmdb_id is not None:
                resolved[pair] = row.tmdb_id
                _memo_set(pair, row.tmdb_id)
                outstanding.discard(pair)
            elif _negative_is_fresh(row, now):
                resolved[pair] = None
                outstanding.discard(pair)
            # else: stale negative — fall through and retry it

    if outstanding and api_key:
        fetched, rows = await _fetch(sorted(outstanding), api_key)
        await _store(rows)
        for pair, tmdb_id in fetched.items():
            resolved[pair] = tmdb_id
            if tmdb_id is not None:
                _memo_set(pair, tmdb_id)
        outstanding -= set(fetched)

    for pair in outstanding:
        resolved.setdefault(pair, None)
    return resolved


async def resolve_one(
    source: str, external_id: str | int, media_kind: str, api_key: str | None
) -> int | None:
    """Resolve a single external id. Used by the webhook paths, where items
    arrive one HTTP request at a time and batching is not possible."""
    pair = make_pair(source, external_id, media_kind)
    if pair is None:
        return None
    return (await resolve_many([pair], api_key)).get(pair)


async def resolve_first(
    candidates: Sequence[tuple[str, str | int]], media_kind: str, api_key: str | None
) -> int | None:
    """Try candidates in order, first hit wins — e.g. [(IMDB, ...), (TVDB, ...)]."""
    pairs = [p for p in (make_pair(s, v, media_kind) for s, v in candidates) if p]
    if not pairs:
        return None
    resolved = await resolve_many(pairs, api_key)
    for pair in pairs:
        if (tmdb_id := resolved.get(pair)) is not None:
            return tmdb_id
    return None


async def record(
    source: str, external_id: str | int, media_kind: str, tmdb_id: int
) -> None:
    """Write through a mapping already known to be correct, so paths that
    resolve ids by other means (Plex GUIDs, TMDB external_ids payloads) warm
    the cache for free."""
    pair = make_pair(source, external_id, media_kind)
    if pair is None or not tmdb_id:
        return
    now = datetime.utcnow()
    await _store([{
        "source": pair[0], "external_id": pair[1], "media_kind": pair[2],
        "tmdb_id": int(tmdb_id), "miss_count": 0, "checked_at": now, "created_at": now,
    }])
    _memo_set(pair, int(tmdb_id))


async def tmdb_to_external(
    tmdb_ids: Iterable[int], source: str, media_kind: str
) -> dict[int, str]:
    """Reverse lookup, backed by idx_external_id_mappings_reverse. Cache-only:
    TMDB's forward endpoint for this is /{type}/{id}/external_ids, which
    callers already handle themselves."""
    ids = sorted({int(t) for t in tmdb_ids if t})
    if not ids:
        return {}
    out: dict[int, str] = {}
    async with AsyncSessionLocal() as db:
        for i in range(0, len(ids), BATCH_SIZE):
            result = await db.execute(
                select(ExternalIdMapping.tmdb_id, ExternalIdMapping.external_id).where(
                    ExternalIdMapping.source == source,
                    ExternalIdMapping.media_kind == media_kind,
                    ExternalIdMapping.tmdb_id.in_(ids[i : i + BATCH_SIZE]),
                )
            )
            for tmdb_id, external_id in result.all():
                out.setdefault(int(tmdb_id), external_id)
    return out

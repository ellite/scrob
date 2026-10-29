"""Push Scrob's resume points (Continue Watching) to Plex.

Scrob already pushes watched status and ratings to Plex, but a partly
watched title only ever lived in Scrob: a play stopped half way on another
player (a Kodi add-on, Silo, Jellyfin) never showed up in Plex as in
progress. With a Plex connection's "Playback progress" push enabled, this
writes each PlaybackProgress row to Plex as the item's resume point.

Two callers:

- the connection's Push / Auto Push (_run_full_push), which sends every
  resume point, looking items up in the Plex libraries when Scrob has not
  seen them on that server yet;
- a webhook stop from a player other than Plex, which sends that one resume
  point right away, but only for items whose Plex ratingKey Scrob already
  knows - a library-wide lookup has no place in a webhook request, so
  anything else waits for the next push.

What it never does is move Plex backwards: an item Plex itself has played
more recently than Scrob's resume point was written is left alone, and so
is one whose Plex position is already within a few seconds of Scrob's.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core import plex
from core.push_state import MISS, PROGRESS, PushState
from db import engine
from models.base import CollectionSource, MediaType
from models.collection import Collection, CollectionFile
from models.connections import MediaServerConnection
from models.media import Media
from models.playback_progress import PlaybackProgress
from models.show import Show

logger = logging.getLogger(__name__)

# Positions this close together are the same resume point: Plex stores
# milliseconds, Scrob whole seconds, and players report a stop a second or so
# apart from where the picture actually stopped.
SAME_POSITION_SECONDS = 10

# Plex's lastViewedAt comes from the Plex server's clock and Scrob's
# updated_at from its own. A Plex play only counts as newer than Scrob's
# resume point when it is newer by more than this.
CLOCK_SLACK = timedelta(seconds=60)

_IN_CHUNK = 500


@dataclass
class ProgressPushResult:
    pushed: int = 0
    failed: int = 0
    # Already right on Plex, or Plex has newer activity of its own.
    unchanged: int = 0
    # Not found in this Plex server's libraries.
    missing: int = 0
    failed_media_ids: list[int] = field(default_factory=list)

    @property
    def attempted(self) -> int:
        return self.pushed + self.failed + self.unchanged + self.missing


def _as_utc_naive(epoch: object) -> datetime | None:
    try:
        seconds = int(epoch)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None)


def plex_already_matches(item: dict, progress: PlaybackProgress) -> bool:
    """True when Plex needs no write for this resume point: its own position
    is already the same one, or Plex has seen playback more recent than the
    moment Scrob recorded this position (that play wins, and Scrob will hear
    of it through Plex's own webhook or history)."""
    offset_ms = item.get("viewOffset")
    if offset_ms is not None:
        try:
            if abs(int(offset_ms) / 1000 - progress.progress_seconds) < SAME_POSITION_SECONDS:
                return True
        except (TypeError, ValueError):
            pass
    last_viewed = _as_utc_naive(item.get("lastViewedAt"))
    if last_viewed is not None and progress.updated_at is not None:
        if last_viewed - progress.updated_at > CLOCK_SLACK:
            return True
    return False


async def _known_rating_keys(
    db: AsyncSession, user_id: int, conn: MediaServerConnection, media_ids: list[int]
) -> dict[int, list[str]]:
    """ratingKeys Scrob already holds for these media on this connection. A
    key read from a different Plex server means nothing here, so the match is
    on the connection, not just the source type."""
    keys: dict[int, list[str]] = {}
    for i in range(0, len(media_ids), _IN_CHUNK):
        chunk = media_ids[i : i + _IN_CHUNK]
        rows = await db.execute(
            select(CollectionFile.source_id, Collection.media_id)
            .join(Collection, Collection.id == CollectionFile.collection_id)
            .where(
                Collection.user_id == user_id,
                Collection.media_id.in_(chunk),
                CollectionFile.source == CollectionSource.plex,
                CollectionFile.connection_id == conn.id,
                CollectionFile.source_id.isnot(None),
            )
        )
        for source_id, media_id in rows.all():
            if source_id not in keys.setdefault(media_id, []):
                keys[media_id].append(source_id)
    return keys


async def _lookup_rating_key(conn: MediaServerConnection, media: Media, show: Show | None) -> tuple[str | None, dict | None]:
    """Find an item in the Plex libraries by its TMDB ids. Returns the
    ratingKey and the item, or (None, None) when Plex does not have it."""
    found: dict | None = None
    if media.media_type == MediaType.movie and media.tmdb_id:
        found = await plex.find_movie_by_tmdb_id(conn.url, conn.token, media.tmdb_id)
    elif (
        media.media_type == MediaType.episode
        and show is not None
        and show.tmdb_id
        and media.season_number is not None
        and media.episode_number is not None
    ):
        found = await plex.find_episode_by_ids(
            conn.url, conn.token, show.tmdb_id, media.season_number, media.episode_number
        )
    if not found or not found.get("ratingKey"):
        return None, None
    return str(found["ratingKey"]), found


async def push_progress_to_plex(
    db: AsyncSession,
    conn: MediaServerConnection,
    user_id: int,
    push_state: PushState,
    *,
    media_ids: list[int] | None = None,
    allow_lookup: bool = True,
) -> ProgressPushResult:
    """Write Scrob's resume points to one Plex connection.

    ``media_ids`` limits the push to those titles (a webhook stop); without it
    every resume point the user has is pushed. ``allow_lookup`` False skips
    titles whose ratingKey on this server is not already known.

    Records what it pushed in ``push_state`` under PROGRESS and a lookup that
    found nothing under MISS; the caller saves the state.
    """
    result = ProgressPushResult()
    if conn.type != "plex" or not conn.push_playback:
        return result

    query = select(PlaybackProgress, Media).join(Media, Media.id == PlaybackProgress.media_id).where(
        PlaybackProgress.user_id == user_id,
        Media.media_type.in_([MediaType.movie, MediaType.episode]),
    )
    if media_ids is not None:
        if not media_ids:
            return result
        query = query.where(PlaybackProgress.media_id.in_(media_ids))
    rows = (await db.execute(query)).all()
    if not rows:
        return result

    # An incremental push skips what it already sent at the same position.
    todo = [
        (progress, media)
        for progress, media in rows
        if progress.progress_seconds > 0
        and not push_state.unchanged(media.id, PROGRESS, float(progress.progress_seconds))
    ]
    if not todo:
        return result

    known = await _known_rating_keys(db, user_id, conn, [media.id for _, media in todo])

    shows: dict[int, Show] = {}
    if allow_lookup:
        show_ids = list({media.show_id for _, media in todo if media.show_id and media.id not in known})
        for i in range(0, len(show_ids), _IN_CHUNK):
            show_rows = await db.execute(select(Show).where(Show.id.in_(show_ids[i : i + _IN_CHUNK])))
            shows.update({show.id: show for show in show_rows.scalars().all()})

    sem = asyncio.Semaphore(10)

    async with httpx.AsyncClient(timeout=plex.PUSH_TIMEOUT, follow_redirects=False) as client:

        async def _push_one(progress: PlaybackProgress, media: Media) -> str:
            async with sem:
                targets: list[tuple[str, dict | None]] = [(rk, None) for rk in known.get(media.id, [])]
                if not targets:
                    if not allow_lookup or push_state.recent_miss(media.id):
                        return "missing"
                    rating_key, item = await _lookup_rating_key(conn, media, shows.get(media.show_id or -1))
                    if rating_key is None:
                        push_state.record_miss(media.id)
                        return "missing"
                    push_state.forget(media.id, MISS)
                    targets = [(rating_key, item)]

                outcome = "missing"
                for rating_key, item in targets:
                    item = item or await plex.get_item(conn.url, conn.token, rating_key)
                    if item is None:
                        # A stale key (the file was replaced or the item
                        # deleted): nothing to write to.
                        continue
                    if outcome == "missing":
                        outcome = "unchanged"
                    if plex_already_matches(item, progress):
                        continue
                    ok = await plex.set_progress(
                        conn.url, conn.token, rating_key, progress.progress_seconds * 1000, client=client
                    )
                    if not ok:
                        return "failed"
                    outcome = "pushed"
                return outcome

        outcomes = await asyncio.gather(*[_push_one(progress, media) for progress, media in todo])

    for (progress, media), outcome in zip(todo, outcomes):
        if outcome == "failed":
            result.failed += 1
            result.failed_media_ids.append(media.id)
            push_state.fail(media.id, PROGRESS)
            continue
        if outcome == "missing":
            result.missing += 1
            continue
        # Pushed, or already right on Plex: either way Plex holds it now, so
        # the next incremental push can skip it until the position changes.
        push_state.record(media.id, PROGRESS, float(progress.progress_seconds))
        if outcome == "pushed":
            result.pushed += 1
        else:
            result.unchanged += 1
    return result


async def push_progress_after_stop(user_id: int, media_ids: list[int]) -> int:
    """Send the resume points a webhook stop just wrote to every Plex
    connection that pushes playback progress, without waiting for its next
    Auto Push. Only titles with a known ratingKey on that server go out.

    Runs in a session of its own, after the webhook committed its write, so a
    failure here can never roll back or expire the webhook's own objects.
    Best-effort: a failure is logged and left for the next push, never raised
    into the webhook. Returns how many items were written to Plex.
    """
    if not media_ids:
        return 0
    pushed = 0
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with session_factory() as db:
            conns = (
                await db.execute(
                    select(MediaServerConnection).where(
                        MediaServerConnection.user_id == user_id,
                        MediaServerConnection.type == "plex",
                        MediaServerConnection.push_playback == True,  # noqa: E712
                    )
                )
            ).scalars().all()
            for conn in conns:
                try:
                    # Recorded, not consulted: a stop's position is new, and
                    # recording it lets the next Auto Push skip what this sent.
                    state = PushState(use_state=False)
                    outcome = await push_progress_to_plex(
                        db, conn, user_id, state, media_ids=media_ids, allow_lookup=False
                    )
                    await state.save(db, conn.id)
                    pushed += outcome.pushed
                except Exception:
                    logger.exception("Pushing playback progress to Plex connection %s failed", conn.id)
                    await db.rollback()
    except Exception:
        logger.exception("Pushing playback progress to Plex after a stop failed")
    return pushed


# Strong references to the pushes still running: the event loop only keeps a
# weak one, and a task nobody holds can be garbage collected mid-flight.
_running_pushes: set[asyncio.Task] = set()


def schedule_progress_push_after_stop(user_id: int, media_ids: list[int]) -> asyncio.Task | None:
    """Run push_progress_after_stop in the background, so a webhook answers
    its player straight away instead of waiting on Plex."""
    if not media_ids:
        return None
    task = asyncio.create_task(push_progress_after_stop(user_id, list(media_ids)))
    _running_pushes.add(task)
    task.add_done_callback(_running_pushes.discard)
    return task

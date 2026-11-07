"""Tests for sync progress counter fixes (#494, #497, #503)."""
import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-at-least-32-bytes-long")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles

from models.base import MediaType, CollectionSource
from models.collection import Collection, CollectionFile
from models.events import WatchEvent
from models.media import Media
from models.show import Show
from models.sync import SyncJob, SyncStatus
from routers.sync import sync_items, _apply_nuvio_watch_history, _expand_multi_episode_items


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):
    """JSONB is PostgreSQL-only and has no SQLite rendering of its own."""
    return "JSON"


class _TestDB(unittest.IsolatedAsyncioTestCase):
    """SQLite harness for sync tests."""

    async def asyncSetUp(self):
        import models  # noqa: F401 - registers every table on Base.metadata
        from models.base import Base

        self.engine = create_async_engine(
            "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.addAsyncCleanup(self.engine.dispose)


class StremioWatchHistorySourceTests(_TestDB):
    """Regression test for #503: Stremio watch history was saved with source="nuvio"
    instead of source="stremio" because _apply_nuvio_watch_history hardcoded the source."""

    async def test_nuvio_watch_history_uses_nuvio_source(self):
        """Default source should be 'nuvio' for backward compatibility."""
        async with self.Session() as db:
            media = Media(tmdb_id=603, media_type=MediaType.movie, title="The Matrix")
            db.add(media)
            await db.commit()
            media_id = media.id

            rows = [{
                "content_id": "tmdb:603",
                "content_type": "movie",
                "watched_at": 1705317600000,  # Unix timestamp in milliseconds
            }]
            tmdb_ids = {"tmdb:603": 603}

            added_ids = await _apply_nuvio_watch_history(
                db, user_id=1, rows=rows, show_map={}, tmdb_ids=tmdb_ids
            )

            self.assertEqual(len(added_ids), 1)
            self.assertIn(media_id, added_ids)

            events = (await db.execute(
                select(WatchEvent).where(WatchEvent.user_id == 1, WatchEvent.media_id == media_id)
            )).scalars().all()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].source, "nuvio")

    async def test_stremio_watch_history_uses_stremio_source(self):
        """When source="stremio" is passed, watch events should have that source."""
        async with self.Session() as db:
            media = Media(tmdb_id=603, media_type=MediaType.movie, title="The Matrix")
            db.add(media)
            await db.commit()
            media_id = media.id

            rows = [{
                "content_id": "tmdb:603",
                "content_type": "movie",
                "watched_at": 1705317600000,  # Unix timestamp in milliseconds
            }]
            tmdb_ids = {"tmdb:603": 603}

            added_ids = await _apply_nuvio_watch_history(
                db, user_id=1, rows=rows, show_map={}, tmdb_ids=tmdb_ids,
                source="stremio"
            )

            self.assertEqual(len(added_ids), 1)
            self.assertIn(media_id, added_ids)

            events = (await db.execute(
                select(WatchEvent).where(WatchEvent.user_id == 1, WatchEvent.media_id == media_id)
            )).scalars().all()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].source, "stremio")


class SyncItemsBatchBoundarySkipTests(_TestDB):
    """Regression test for #494: when an unmatched item is skipped at exactly a
    batch boundary, the batch bookkeeping is jumped over by continue, causing
    processed_items to lose that entire batch."""

    async def test_skipped_item_at_batch_boundary_does_not_lose_batch(self):
        """A skipped item at position (BATCH_SIZE - 1) must still count the batch."""
        async with self.Session() as db:
            # Create a sync job
            job = SyncJob(
                user_id=1, source=CollectionSource.jellyfin, status=SyncStatus.running,
                processed_items=0, total_items=3
            )
            db.add(job)
            await db.commit()
            job_id = job.id

            # Patch BATCH_SIZE to 2 for testing (as mentioned in the issue)
            with patch("routers.sync.BATCH_SIZE", 2):
                # Three unmatched movies without a name (will be skipped)
                items = [
                    {"Id": "1", "ProviderIds": {}},  # No TMDB ID, no name
                    {"Id": "2", "ProviderIds": {}},  # No TMDB ID, no name - at boundary (i=1, (i+1) % 2 == 0)
                    {"Id": "3", "ProviderIds": {}},  # No TMDB ID, no name
                ]

                stats = {"movies": 0, "skipped": 0, "errors": 0}
                await sync_items(
                    items=items,
                    media_type=MediaType.movie,
                    source=CollectionSource.jellyfin,
                    db=db,
                    stats=stats,
                    user_id=1,
                    job_id=job_id,
                )

            # Refresh job to get updated values
            await db.refresh(job)

            # All 3 items should be counted as processed (even though they were skipped)
            self.assertEqual(job.processed_items, 3,
                "Skipped items at batch boundaries should still count toward processed_items")
            self.assertEqual(stats["skipped"], 3)


class SyncItemsMultiEpisodeProgressTests(_TestDB):
    """Regression test for #497: multi-episode files expand into one row each, so
    the pull sizes total_items with _sync_row_count (not len(items)) and sync_items
    leaves that total alone. Otherwise processed_items exceeds it above 100%."""

    async def test_multi_episode_expansion_updates_total_items(self):
        """A job sized by _sync_row_count ends at exactly its total."""
        async with self.Session() as db:
            # Create a show
            show = Show(tmdb_id=1396, title="Breaking Bad")
            db.add(show)
            await db.commit()
            show_id = show.id

            # The pull sizes the job by the expanded row count: 3, not 1 file.
            job = SyncJob(
                user_id=1, source=CollectionSource.jellyfin, status=SyncStatus.running,
                processed_items=0, total_items=3
            )
            db.add(job)
            await db.commit()
            job_id = job.id

            # One multi-episode file covering episodes 1-3
            items = [{
                "Id": "jf-multi",
                "SeriesId": "jf-series-1",
                "Name": "Pilot",
                "ParentIndexNumber": 1,
                "IndexNumber": 1,
                "IndexNumberEnd": 3,  # Will expand to 3 episodes
                "ProviderIds": {},
            }]

            # Verify expansion creates 3 items from 1
            expanded = _expand_multi_episode_items(items, MediaType.episode, CollectionSource.jellyfin)
            self.assertEqual(len(expanded), 3, "Multi-episode file should expand to 3 episodes")

            show_map = {"jf-series-1": show_id}
            stats = {"episodes": 0, "skipped": 0, "errors": 0}

            await sync_items(
                items=items,  # Pass unexpanded items (sync_items will expand them)
                media_type=MediaType.episode,
                source=CollectionSource.jellyfin,
                db=db,
                stats=stats,
                user_id=1,
                job_id=job_id,
                show_map=show_map,
            )

            await db.refresh(job)

            # sync_items must leave the total the pull set alone
            self.assertEqual(job.total_items, 3,
                "total_items should stay at the expanded row count")
            # All 3 expanded episodes should be processed
            self.assertEqual(job.processed_items, 3,
                "processed_items should count all expanded episodes")

    async def test_plex_multi_episode_also_updates_total_items(self):
        """Plex multi-episode files should also trigger total_items update (though Plex
        doesn't actually use multi-episode files, this verifies the source check works)."""
        async with self.Session() as db:
            show = Show(tmdb_id=1396, title="Breaking Bad")
            db.add(show)
            await db.commit()

            job = SyncJob(
                user_id=1, source=CollectionSource.plex, status=SyncStatus.running,
                processed_items=0, total_items=1
            )
            db.add(job)
            await db.commit()
            job_id = job.id

            # Plex doesn't expand, so total_items should NOT be updated
            items = [{
                "ratingKey": "123",
                "grandparentRatingKey": "100",
                "title": "Pilot",
                "parentIndex": 1,
                "index": 1,
                "Guid": [],
            }]

            await sync_items(
                items=items,
                media_type=MediaType.episode,
                source=CollectionSource.plex,  # Plex, not Jellyfin/Emby
                db=db,
                stats={"episodes": 0, "skipped": 0, "errors": 0},
                user_id=1,
                job_id=job_id,
                show_map={"100": show.id},
            )

            await db.refresh(job)

            # Plex doesn't expand, so total_items should remain 1
            self.assertEqual(job.total_items, 1,
                "Plex should not trigger total_items update (no multi-episode expansion)")


if __name__ == "__main__":
    unittest.main()

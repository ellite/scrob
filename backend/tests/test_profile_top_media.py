import os
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from models.base import MediaType
from models.events import WatchEvent
from models.media import Media
from models.show import Show as ShowModel
from routers.profile.top_media import _top_media, _watch_date_filters, get_most_played


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


class TopMediaQueryTests(unittest.IsolatedAsyncioTestCase):
    """_top_media counts in the database; these run it on real SQLite rows."""

    async def asyncSetUp(self):
        from models.base import Base

        self.engine = create_async_engine(
            "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.addAsyncCleanup(self.engine.dispose)

    async def _add(self, shows=(), media=(), plays=()):
        async with self.Session() as db:
            db.add_all([ShowModel(id=sid, tmdb_id=tmdb, tvdb_id=tvdb, title=f"Show {sid}", poster_path=f"/{sid}.jpg")
                        for sid, tmdb, tvdb in shows])
            db.add_all(media)
            await db.commit()
            db.add_all([
                WatchEvent(user_id=user, media_id=mid, watched_at=datetime(2026, 1, 1) + timedelta(days=day), completed=completed)
                for user, mid, day, completed in plays
            ])
            await db.commit()

    def _episode(self, mid, show_id, runtime=30, tmdb_runtime=None):
        return Media(
            id=mid, tmdb_id=1000 + mid, media_type=MediaType.episode, title=f"Ep {mid}",
            show_id=show_id, season_number=1, episode_number=mid, runtime=runtime,
            tmdb_data={"runtime": tmdb_runtime} if tmdb_runtime is not None else None,
        )

    def _movie(self, mid, title, runtime=100):
        return Media(id=mid, tmdb_id=mid, media_type=MediaType.movie, title=title, poster_path=f"/{mid}.jpg", runtime=runtime)

    async def _top(self, **kwargs):
        async with self.Session() as db:
            return await _top_media(db, 1, kwargs.pop("date_filters", [WatchEvent.completed == True]), **kwargs)  # noqa: E712

    async def test_shows_are_ranked_by_plays_then_minutes(self):
        await self._add(
            shows=[(10, 10, None), (20, 20, None), (30, 30, None)],
            media=[self._episode(1, 10, 20), self._episode(2, 20, 40), self._episode(3, 20, 40), self._episode(4, 30, 60)],
            plays=[(1, 1, 0, True), (1, 2, 0, True), (1, 3, 0, True), (1, 4, 0, True)],
        )

        shows, _ = await self._top()

        self.assertEqual([s["tmdb_id"] for s in shows], [20, 30, 10])

    async def test_each_watch_event_counts_as_a_play_so_rewatches_add_up(self):
        await self._add(
            shows=[(10, 10, 5)], media=[self._episode(1, 10, 20)],
            plays=[(1, 1, 0, True), (1, 1, 1, True), (1, 1, 2, True)],
        )

        shows, _ = await self._top()

        self.assertEqual((shows[0]["plays"], shows[0]["minutes"], shows[0]["episodes"]), (3, 60, 1))
        self.assertEqual((shows[0]["title"], shows[0]["tmdb_id"], shows[0]["tvdb_id"]), ("Show 10", 10, 5))

    async def test_movies_are_ranked_and_carry_their_poster(self):
        await self._add(
            media=[self._movie(1, "Once"), self._movie(2, "Twice")],
            plays=[(1, 1, 0, True), (1, 2, 0, True), (1, 2, 1, True)],
        )

        _, movies = await self._top()

        self.assertEqual([m["title"] for m in movies], ["Twice", "Once"])
        self.assertEqual(movies[0]["poster_path"], "/2.jpg")
        self.assertEqual((movies[0]["plays"], movies[0]["minutes"]), (2, 200))

    async def test_limit_keeps_the_top_n_of_each_kind(self):
        await self._add(
            shows=[(10, 10, None), (20, 20, None)],
            media=[self._episode(1, 10), self._episode(2, 20), self._movie(5, "A"), self._movie(6, "B")],
            plays=[(1, 1, 0, True), (1, 2, 0, True), (1, 2, 1, True), (1, 5, 0, True), (1, 6, 0, True), (1, 6, 1, True)],
        )

        shows, movies = await self._top(limit=1)

        self.assertEqual([s["tmdb_id"] for s in shows], [20])
        self.assertEqual([m["title"] for m in movies], ["B"])

    async def test_without_a_limit_nothing_is_truncated(self):
        ids = range(1, 31)
        await self._add(
            media=[self._movie(i, f"Movie {i}") for i in ids],
            plays=[(1, i, 0, True) for i in ids],
        )

        _, movies = await self._top()

        self.assertEqual(len(movies), 30)

    async def test_only_the_users_own_completed_plays_inside_the_period_count(self):
        await self._add(
            media=[self._movie(1, "Mine"), self._movie(2, "Not completed"), self._movie(3, "Someone else's"), self._movie(4, "Too old")],
            plays=[(1, 1, 5, True), (1, 2, 5, False), (2, 3, 5, True), (1, 4, 0, True)],
        )
        filters = [WatchEvent.completed == True, WatchEvent.watched_at >= datetime(2026, 1, 3)]  # noqa: E712

        _, movies = await self._top(date_filters=filters)

        self.assertEqual([m["title"] for m in movies], ["Mine"])

    async def test_episode_runtime_falls_back_to_tmdb_data(self):
        await self._add(
            shows=[(10, 10, None)], media=[self._episode(1, 10, runtime=None, tmdb_runtime=42)],
            plays=[(1, 1, 0, True)],
        )

        shows, _ = await self._top()

        self.assertEqual(shows[0]["minutes"], 42)

    async def test_episodes_without_a_show_are_not_counted_as_shows(self):
        await self._add(media=[self._episode(1, None)], plays=[(1, 1, 0, True)])

        shows, movies = await self._top()

        self.assertEqual((shows, movies), ([], []))

    async def test_missing_show_row_leaves_fields_empty(self):
        await self._add(media=[self._episode(1, 99)], plays=[(1, 1, 0, True)])

        shows, _ = await self._top()

        self.assertEqual((shows[0]["title"], shows[0]["tmdb_id"], shows[0]["plays"]), (None, None, 1))


class WatchDateFiltersTests(unittest.TestCase):
    def test_no_bounds_keeps_only_completed_plays(self):
        # Arrange
        since = None
        until = None

        # Act
        filters = _watch_date_filters(since, until)

        # Assert
        self.assertEqual(len(filters), 1)

    def test_since_and_until_add_inclusive_bounds(self):
        # Arrange
        since = date(2026, 1, 1)
        until = date(2026, 12, 31)

        # Act
        filters = _watch_date_filters(since, until)

        # Assert
        self.assertEqual(len(filters), 3)


class MostPlayedEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_the_requested_kind(self):
        # Arrange
        movies = [{"title": "Movie", "plays": 2, "minutes": 200}]
        top_media = AsyncMock(return_value=([{"title": "Show"}], movies))
        with patch("routers.profile.top_media._check_profile_access", AsyncMock()), patch("routers.profile.top_media._top_media", top_media):
            # Act
            result = await get_most_played(user_id=1, kind="movies", db=AsyncMock(), current_user=None)

        # Assert
        self.assertEqual(result, movies)

    async def test_passes_the_date_range_through(self):
        # Arrange
        top_media = AsyncMock(return_value=([], []))
        with patch("routers.profile.top_media._check_profile_access", AsyncMock()), patch("routers.profile.top_media._top_media", top_media):
            # Act
            await get_most_played(
                user_id=1, kind="shows", since=date(2026, 1, 1), until=date(2026, 12, 31),
                db=AsyncMock(), current_user=None,
            )

        # Assert
        self.assertEqual(len(top_media.await_args.args[2]), 3)


if __name__ == "__main__":
    unittest.main()

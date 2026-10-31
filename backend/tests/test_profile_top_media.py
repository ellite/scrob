import os
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from routers.profile.top_media import _rank_top_media, _top_media, _watch_date_filters, get_most_played


def episode_row(media_id, show_id, runtime=30):
    return ("episode", media_id, None, None, None, show_id, runtime)


def movie_row(media_id, title, runtime=100):
    return ("movie", media_id, title, f"/{media_id}.jpg", media_id, None, runtime)


def show_meta(show_id):
    return {
        "title": f"Show {show_id}",
        "poster_path": f"/{show_id}.jpg",
        "tmdb_id": show_id,
        "tvdb_id": None,
    }


class RankTopMediaTests(unittest.TestCase):
    def test_shows_are_ranked_by_plays_then_minutes(self):
        # Arrange
        rows = [
            episode_row(1, show_id=10, runtime=20),
            episode_row(2, show_id=20, runtime=40),
            episode_row(3, show_id=20, runtime=40),
            episode_row(4, show_id=30, runtime=60),
        ]
        meta = {10: show_meta(10), 20: show_meta(20), 30: show_meta(30)}

        # Act
        shows, _ = _rank_top_media(rows, meta)

        # Assert
        self.assertEqual([s["tmdb_id"] for s in shows], [20, 30, 10])

    def test_each_watch_event_counts_as_a_play_so_rewatches_add_up(self):
        # Arrange
        rows = [episode_row(1, show_id=10, runtime=20) for _ in range(3)]

        # Act
        shows, _ = _rank_top_media(rows, {10: show_meta(10)})

        # Assert
        self.assertEqual(shows[0]["plays"], 3)
        self.assertEqual(shows[0]["minutes"], 60)
        self.assertEqual(shows[0]["episodes"], 1)

    def test_shows_are_not_truncated(self):
        # Arrange
        rows = [episode_row(i, show_id=i) for i in range(1, 16)]
        meta = {i: show_meta(i) for i in range(1, 16)}

        # Act
        shows, _ = _rank_top_media(rows, meta)

        # Assert
        self.assertEqual(len(shows), 15)

    def test_movies_are_ranked_by_plays_and_not_truncated(self):
        # Arrange
        rows = [movie_row(i, f"Movie {i}") for i in range(1, 14)]
        rows.append(movie_row(1, "Movie 1"))

        # Act
        _, movies = _rank_top_media(rows, {})

        # Assert
        self.assertEqual(len(movies), 13)
        self.assertEqual(movies[0]["title"], "Movie 1")
        self.assertEqual(movies[0]["plays"], 2)

    def test_episodes_without_a_show_are_not_counted_as_shows(self):
        # Arrange
        rows = [episode_row(1, show_id=None)]

        # Act
        shows, _ = _rank_top_media(rows, {})

        # Assert
        self.assertEqual(shows, [])

    def test_missing_show_metadata_leaves_fields_empty(self):
        # Arrange
        rows = [episode_row(1, show_id=10)]

        # Act
        shows, _ = _rank_top_media(rows, {})

        # Assert
        self.assertIsNone(shows[0]["title"])
        self.assertEqual(shows[0]["plays"], 1)


class TopMediaQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_show_metadata_is_loaded_only_when_episodes_were_played(self):
        # Arrange
        db = AsyncMock()
        first = MagicMock()
        first.all.return_value = [movie_row(5, "Only Movie")]
        db.execute.return_value = first

        # Act
        shows, movies = await _top_media(db, user_id=1, date_filters=[])

        # Assert
        self.assertEqual(db.execute.await_count, 1)
        self.assertEqual(shows, [])
        self.assertEqual(movies[0]["title"], "Only Movie")

    async def test_shows_get_metadata_from_the_show_lookup(self):
        # Arrange
        db = AsyncMock()
        rows_result = MagicMock()
        rows_result.all.return_value = [episode_row(1, show_id=10, runtime=25)]
        shows_result = MagicMock()
        shows_result.scalars.return_value.all.return_value = [
            SimpleNamespace(id=10, title="Show 10", poster_path="/10.jpg", tmdb_id=99, tvdb_id=7),
        ]
        db.execute.side_effect = [rows_result, shows_result]

        # Act
        shows, _ = await _top_media(db, user_id=1, date_filters=[])

        # Assert
        self.assertEqual(shows[0]["title"], "Show 10")
        self.assertEqual(shows[0]["tmdb_id"], 99)
        self.assertEqual(shows[0]["tvdb_id"], 7)
        self.assertEqual(shows[0]["minutes"], 25)


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

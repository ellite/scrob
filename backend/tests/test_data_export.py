import io
import json
import os
import unittest
import zipfile
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from models.base import MediaType, PrivacyLevel
from core import data_export
from core.trakt_export import parse_trakt_export


class _Result:
    def __init__(self, items):
        self.items = items

    def all(self):
        return self.items

    def scalars(self):
        return self

    def one_or_none(self):
        return self.items[0] if self.items else None

    def scalar_one_or_none(self):
        return self.items[0] if self.items else None


class _FakeSession:
    """Returns queued results in the exact order the code under test calls
    db.execute() — mirrors the pattern used by tests/test_history.py."""

    def __init__(self, results: list[list]):
        self.execute = AsyncMock(side_effect=[_Result(r) for r in results])


def _movie_media(id=1, tmdb_id=100, title="A Movie", release_date="2020-05-01"):
    return SimpleNamespace(id=id, tmdb_id=tmdb_id, title=title, release_date=release_date, media_type=MediaType.movie, season_number=None, episode_number=None, show_id=None)


def _episode_media(id=2, tmdb_id=200, title="Pilot", show_id=9, season_number=1, episode_number=1):
    return SimpleNamespace(id=id, tmdb_id=tmdb_id, title=title, release_date=None, media_type=MediaType.episode, season_number=season_number, episode_number=episode_number, show_id=show_id)


def _series_media(id=3, tmdb_id=300, title="A Show"):
    return SimpleNamespace(id=id, tmdb_id=tmdb_id, title=title, release_date=None, media_type=MediaType.series, season_number=None, episode_number=None, show_id=None)


def _show(id=9, tmdb_id=200, title="The Show", tvdb_id=500, first_air_date="2018-01-01"):
    return SimpleNamespace(id=id, tmdb_id=tmdb_id, title=title, tvdb_id=tvdb_id, first_air_date=first_air_date)


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_movie_watch_becomes_trakt_shaped_entry(self) -> None:
        event = SimpleNamespace(id=1, watched_at=datetime(2026, 1, 1, 12, 0, 0), completed=True)
        db = _FakeSession([[(event, _movie_media())]])

        entries = await data_export.build_history(db, user_id=1)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["type"], "movie")
        self.assertEqual(entries[0]["movie"]["ids"]["tmdb"], 100)
        self.assertEqual(entries[0]["movie"]["year"], 2020)
        self.assertEqual(entries[0]["watched_at"], "2026-01-01T12:00:00.000Z")

    async def test_episode_watch_nests_show_with_tvdb_id(self) -> None:
        event = SimpleNamespace(id=2, watched_at=datetime(2026, 2, 2), completed=True)
        db = _FakeSession([
            [(event, _episode_media())],
            [_show()],
        ])

        entries = await data_export.build_history(db, user_id=1)

        self.assertEqual(entries[0]["type"], "episode")
        self.assertEqual(entries[0]["episode"]["season"], 1)
        self.assertEqual(entries[0]["episode"]["number"], 1)
        self.assertEqual(entries[0]["show"]["ids"]["tmdb"], 200)
        self.assertEqual(entries[0]["show"]["ids"]["tvdb"], 500)

    async def test_unknown_watched_at_serializes_as_null(self) -> None:
        event = SimpleNamespace(id=3, watched_at=None, completed=True)
        db = _FakeSession([[(event, _movie_media())]])

        entries = await data_export.build_history(db, user_id=1)

        self.assertIsNone(entries[0]["watched_at"])


class RatingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_movie_rating(self) -> None:
        rating = SimpleNamespace(rated_at=datetime(2026, 1, 1), rating=8.0, season_number=None)
        db = _FakeSession([[(rating, _movie_media())]])

        out = await data_export.build_ratings(db, user_id=1)

        self.assertEqual(len(out["movies"]), 1)
        self.assertEqual(out["movies"][0]["rating"], 8)
        self.assertEqual(out["movies"][0]["movie"]["ids"]["tmdb"], 100)

    async def test_show_rating(self) -> None:
        rating = SimpleNamespace(rated_at=datetime(2026, 1, 1), rating=7.0, season_number=None)
        db = _FakeSession([[(rating, _series_media())], []])

        out = await data_export.build_ratings(db, user_id=1)

        self.assertEqual(len(out["shows"]), 1)
        self.assertEqual(out["shows"][0]["show"]["ids"]["tmdb"], 300)

    async def test_season_rating(self) -> None:
        rating = SimpleNamespace(rated_at=datetime(2026, 1, 1), rating=6.0, season_number=2)
        db = _FakeSession([[(rating, _series_media())], []])

        out = await data_export.build_ratings(db, user_id=1)

        self.assertEqual(len(out["seasons"]), 1)
        self.assertEqual(out["seasons"][0]["season"]["number"], 2)

    async def test_episode_rating(self) -> None:
        rating = SimpleNamespace(rated_at=datetime(2026, 1, 1), rating=9.0, season_number=None)
        db = _FakeSession([[(rating, _episode_media())], [_show()]])

        out = await data_export.build_ratings(db, user_id=1)

        self.assertEqual(len(out["episodes"]), 1)
        self.assertEqual(out["episodes"][0]["episode"]["season"], 1)
        self.assertEqual(out["episodes"][0]["show"]["ids"]["tmdb"], 200)


class CollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_collected_movie(self) -> None:
        coll = SimpleNamespace(added_at=datetime(2026, 1, 1))
        db = _FakeSession([[(coll, _movie_media())]])

        out = await data_export.build_collection(db, user_id=1)

        self.assertEqual(len(out["movies"]), 1)
        self.assertEqual(out["movies"][0]["collected_at"], "2026-01-01T00:00:00.000Z")


class ListsTests(unittest.IsolatedAsyncioTestCase):
    async def test_watchlist_slug_is_separated_from_custom_lists(self) -> None:
        watchlist = SimpleNamespace(id=1, name="Watchlist", trakt_slug="__watchlist__", mdblist_slug=None,
                                     description=None, privacy_level=PrivacyLevel.private,
                                     created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1))
        custom = SimpleNamespace(id=2, name="My Faves", trakt_slug=None, mdblist_slug=None,
                                  description="desc", privacy_level=PrivacyLevel.public,
                                  created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1))
        item1 = SimpleNamespace(id=11, added_at=datetime(2026, 1, 1), notes=None)
        item2 = SimpleNamespace(id=12, added_at=datetime(2026, 1, 2), notes="great")

        db = _FakeSession([
            [watchlist, custom],       # select(ListModel)
            [(item1, _movie_media())],  # items for watchlist
            [(item2, _movie_media(id=5, tmdb_id=101))],  # items for custom list
        ])

        watchlist_items, lists_meta, list_items_by_slug = await data_export.build_lists(db, user_id=1)

        self.assertEqual(len(watchlist_items), 1)
        self.assertEqual(len(lists_meta), 1)
        self.assertEqual(lists_meta[0]["name"], "My Faves")
        self.assertIn("my-faves", list_items_by_slug)
        self.assertEqual(list_items_by_slug["my-faves"][0]["notes"], "great")


class CommentsTests(unittest.IsolatedAsyncioTestCase):
    async def test_movie_comment(self) -> None:
        comment = SimpleNamespace(id=1, created_at=datetime(2026, 1, 1), content="Great movie",
                                   is_spoiler=False, media_type="movie", tmdb_id=100,
                                   season_number=None, episode_number=None)
        db = _FakeSession([[comment], [_movie_media()]])

        out = await data_export.build_comments(db, user_id=1)

        self.assertEqual(len(out["movies"]), 1)
        self.assertEqual(out["movies"][0]["comment"], "Great movie")


class SecretCategoryTests(unittest.IsolatedAsyncioTestCase):
    """These four categories are opt-in and plaintext-secret-bearing —
    verify they're excluded unless explicitly requested, and that the
    shapes carry the actual secret values when they are."""

    async def test_api_keys_includes_scrob_and_third_party_keys(self) -> None:
        user = SimpleNamespace(api_key="scrob-secret")
        settings = SimpleNamespace(tmdb_api_key="tmdb-secret", tvdb_api_key=None)

        out = data_export.build_api_keys(user, settings)

        self.assertEqual(out["scrob_api_key"], "scrob-secret")
        self.assertEqual(out["tmdb_api_key"], "tmdb-secret")
        self.assertIsNone(out["tvdb_api_key"])

    async def test_media_connection_includes_token(self) -> None:
        conn = SimpleNamespace(type="plex", name="Plex", url="http://x", token="plex-token",
                                server_user_id=None, server_username="me",
                                sync_collection=True, sync_watched=True, sync_ratings=True, sync_playback=True,
                                push_watched=False, push_collection=False, push_playback=False, push_ratings=False)
        db = _FakeSession([[conn]])

        out = await data_export.build_media_connections(db, user_id=1)

        self.assertEqual(out[0]["token"], "plex-token")

    async def test_scrobble_connection_has_no_secret_fields(self) -> None:
        conn = SimpleNamespace(type="jellyfin", name="Jellyfin", server_user_id="u1", server_username=None,
                                sync_collection=True, sync_watched=True, sync_playback=True)
        db = _FakeSession([[conn]])

        out = await data_export.build_scrobble_connections(db, user_id=1)

        self.assertNotIn("token", out[0])
        self.assertNotIn("url", out[0])

    async def test_connections_includes_third_party_secrets(self) -> None:
        settings = SimpleNamespace(**{field: None for field in data_export._CONNECTIONS_SETTINGS_FIELDS})
        settings.trakt_access_token = "trakt-secret"
        settings.mdblist_api_key = "mdblist-secret"

        out = data_export.build_connections(settings)

        self.assertEqual(out["trakt_access_token"], "trakt-secret")
        self.assertEqual(out["mdblist_api_key"], "mdblist-secret")

    async def test_export_zip_excludes_secret_categories_by_default(self) -> None:
        user = SimpleNamespace(id=1, username="alice", api_key="secret", created_at=datetime(2026, 1, 1))
        db = _FakeSession([[None]])  # only the UserProfileData lookup in build_export_zip

        payload = await data_export.build_export_zip(
            db, user, None,
            include_watched=False, include_ratings=False, include_collection=False,
            include_lists=False, include_comments=False,
        )

        names = zipfile.ZipFile(io.BytesIO(payload)).namelist()
        for secret_file in ("api-keys.json", "media-connections.json", "scrobble-connections.json", "connections.json"):
            self.assertNotIn(secret_file, names)


class ZipRoundTripTests(unittest.TestCase):
    """The zip assembly itself (naming/chunking), fed straight back into the
    existing Trakt-export importer to confirm the shapes are compatible."""

    def test_written_zip_is_readable_by_the_trakt_export_parser(self) -> None:
        history = [
            {"id": 1, "watched_at": "2026-01-01T00:00:00.000Z", "action": "watch", "type": "movie",
             "movie": {"title": "A Movie", "year": 2020, "ids": {"tmdb": 100}}},
        ]
        ratings = {"movies": [], "shows": [], "seasons": [], "episodes": []}
        collection = {"movies": [], "episodes": []}

        buf_bytes = self._build_minimal_zip(history, ratings, collection)
        parsed = parse_trakt_export(buf_bytes)

        self.assertEqual(len(parsed.history_movies), 1)
        self.assertEqual(parsed.history_movies[0]["movie"]["ids"]["tmdb"], 100)

    @staticmethod
    def _build_minimal_zip(history, ratings, collection) -> bytes:
        import io
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("watched-history-1.json", json.dumps(history))
            zf.writestr("ratings-movies.json", json.dumps(ratings["movies"]))
            zf.writestr("ratings-shows.json", json.dumps(ratings["shows"]))
            zf.writestr("ratings-seasons.json", json.dumps(ratings["seasons"]))
            zf.writestr("ratings-episodes.json", json.dumps(ratings["episodes"]))
            zf.writestr("lists-watchlist.json", json.dumps([]))
            zf.writestr("lists-lists.json", json.dumps([]))
        return buffer.getvalue()


if __name__ == "__main__":
    unittest.main()

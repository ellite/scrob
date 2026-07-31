import io
import json
import os
import struct
import unittest
import zipfile
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core import scrob_import
from core.scrob_import import apply_scrob_import, parse_scrob_export, ScrobImportData
from routers import trakt as trakt_router
from routers import export as export_router


def _build_zip(files: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, json.dumps(data))
    return buffer.getvalue()


class ParseScrobExportTests(unittest.TestCase):
    def test_rejects_non_zip(self) -> None:
        with self.assertRaises(ValueError):
            parse_scrob_export(b"not a zip")

    def test_rejects_zip_without_user_profile(self) -> None:
        payload = _build_zip({"ratings-movies.json": []})
        with self.assertRaises(ValueError):
            parse_scrob_export(payload)

    def test_parses_every_category_including_secrets(self) -> None:
        payload = _build_zip({
            "user-profile.json": {"username": "alice"},
            "watched-history-1.json": [{"type": "movie", "movie": {"ids": {"tmdb": 1}}}],
            "ratings-movies.json": [{"type": "movie", "rating": 8, "movie": {"ids": {"tmdb": 1}}}],
            "ratings-shows.json": [], "ratings-seasons.json": [], "ratings-episodes.json": [],
            "collection-movies-1.json": [{"type": "movie", "movie": {"ids": {"tmdb": 2}}}],
            "collection-episodes-1.json": [{"type": "episode", "episode": {"season": 1, "number": 1}, "show": {"ids": {"tmdb": 3}}}],
            "lists-watchlist.json": [{"type": "movie", "movie": {"ids": {"tmdb": 4}}}],
            "lists-lists.json": [{"name": "Faves", "ids": {"trakt": 9, "slug": "faves"}}],
            "lists-list-9-faves.json": [{"type": "movie", "movie": {"ids": {"tmdb": 5}}}],
            "comments-movies.json": [{"comment": "great", "movie": {"ids": {"tmdb": 1}}}],
            "comments-shows.json": [], "comments-seasons.json": [], "comments-episodes.json": [],
            "api-keys.json": {"scrob_api_key": "s", "tmdb_api_key": "t", "tvdb_api_key": None},
            "media-connections.json": [{"type": "plex", "url": "http://x", "token": "tok"}],
            "scrobble-connections.json": [],
            "connections.json": {"trakt_access_token": "tok2"},
        })

        data = parse_scrob_export(payload)

        self.assertEqual(len(data.history_movies), 1)
        self.assertEqual(len(data.ratings["movies"]), 1)
        self.assertEqual(len(data.collection_movies), 1)
        self.assertEqual(len(data.collection_episodes), 1)
        self.assertEqual(len(data.watchlist), 1)
        self.assertEqual(data.list_items["faves"][0]["movie"]["ids"]["tmdb"], 5)
        self.assertEqual(len(data.comments["movies"]), 1)
        self.assertEqual(data.api_keys["tmdb_api_key"], "t")
        self.assertEqual(data.media_connections[0]["token"], "tok")
        self.assertEqual(data.scrobble_connections, [])
        self.assertEqual(data.connections["trakt_access_token"], "tok2")

    def test_missing_optional_files_are_none_not_empty(self) -> None:
        # Distinguishes "category wasn't in this export" (None) from "was
        # included but empty" ([]) for the three opt-in list/dict files.
        payload = _build_zip({"user-profile.json": {"username": "alice"}})
        data = parse_scrob_export(payload)
        self.assertIsNone(data.api_keys)
        self.assertIsNone(data.media_connections)
        self.assertIsNone(data.scrobble_connections)
        self.assertIsNone(data.connections)


class ParseScrobExportZipBombTests(unittest.TestCase):
    """Same defense-in-depth as core/trakt_export.py — decompression caps must
    be enforced against bytes actually produced while streaming, never trusted
    from the zip's own (attacker-controllable) declared size metadata."""

    def test_rejects_oversized_export(self) -> None:
        # user-profile.json alone is only ever used as a marker (membership
        # check in the zip's namelist) and is never itself decompressed by
        # the parser, so the payload needs a file that actually gets _load-ed
        # for the running byte-count to have anything to exceed the cap with.
        payload = _build_zip({"user-profile.json": {"username": "alice"}, "watched-history-1.json": [{"type": "movie"}]})
        with patch.object(scrob_import, "MAX_TOTAL_SIZE", 10):
            with self.assertRaises(ValueError):
                parse_scrob_export(payload)

    def test_declared_size_lie_cannot_bypass_the_size_cap(self) -> None:
        # Build an entry whose real (compressible) content is much bigger than
        # what its local header / central directory claim, and confirm the
        # cap still catches it from bytes actually read, not the declared value.
        real_payload = b"0" * (2 * 1024 * 1024)  # 2MB, highly compressible
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("user-profile.json", json.dumps({"username": "alice"}))
            zf.writestr("watched-history-1.json", real_payload)
        raw = bytes(buf.getvalue())

        true_size_bytes = struct.pack("<I", len(real_payload))
        fake_size_bytes = struct.pack("<I", 10)
        self.assertEqual(raw.count(true_size_bytes), 2)  # local header + central directory
        tampered = raw.replace(true_size_bytes, fake_size_bytes)

        with patch.object(scrob_import, "MAX_ENTRY_SIZE", 1024):
            with self.assertRaises(ValueError):
                parse_scrob_export(tampered)

    def test_corrupted_entry_raises_a_clean_error(self) -> None:
        payload = _build_zip({"user-profile.json": {"username": "alice"}, "watched-history-1.json": []})
        with patch.object(zipfile.ZipExtFile, "read", side_effect=zipfile.BadZipFile("Bad CRC-32 for file 'x'")):
            with self.assertRaises(ValueError) as ctx:
                parse_scrob_export(payload)
        self.assertIn("corrupted", str(ctx.exception))


class _NestedTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Result:
    def __init__(self, items=None):
        self.items = items if items is not None else []

    def scalars(self):
        return self

    def first(self):
        return self.items[0] if self.items else None

    def scalar_one_or_none(self):
        return self.items[0] if self.items else None

    def all(self):
        return self.items

    def __iter__(self):
        return iter(self.items)


class _FakeSession:
    """Generic double: queued results per db.execute() call, in call order,
    plus id-assigning add()/flush() like the other test files in this repo."""

    def __init__(self, results):
        self.execute = AsyncMock(side_effect=[_Result(r) for r in results])
        self.commit = AsyncMock()
        self.added: list = []
        self._next_id = 1000

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id
                self._next_id += 1

    def begin_nested(self):
        return _NestedTxn()


_EMPTY_INCLUDE = dict(
    include_watched=False, include_ratings=False, include_collection=False,
    include_lists=False, include_comments=False, include_api_keys=False,
    include_media_connections=False, include_scrobble_connections=False, include_connections=False,
)


class CollectionImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_movie_is_added_to_collection(self) -> None:
        media_stub = SimpleNamespace(id=55, tmdb_id=100)
        data = ScrobImportData(collection_movies=[{"movie": {"ids": {"tmdb": 100}, "title": "X"}}])
        # execute() order: total_items update, select(Collection) [none existing], final processed_items update
        db = _FakeSession([[], [], []])

        with patch.object(trakt_router, "_get_or_create_movie_media", AsyncMock(return_value=media_stub)):
            stats = await apply_scrob_import(
                db, job_id=1, user_id=1, data=data, api_key=None,
                **{**_EMPTY_INCLUDE, "include_collection": True},
            )

        self.assertEqual(stats["collected"], 1)
        collection_files = [o for o in db.added if type(o).__name__ == "CollectionFile"]
        self.assertEqual(collection_files[0].source_id, "100")

    async def test_already_collected_movie_is_skipped(self) -> None:
        media_stub = SimpleNamespace(id=55, tmdb_id=100)
        existing_collection = SimpleNamespace(id=1, media_id=55)
        data = ScrobImportData(collection_movies=[{"movie": {"ids": {"tmdb": 100}, "title": "X"}}])
        db = _FakeSession([[], [existing_collection], []])

        with patch.object(trakt_router, "_get_or_create_movie_media", AsyncMock(return_value=media_stub)):
            stats = await apply_scrob_import(
                db, job_id=1, user_id=1, data=data, api_key=None,
                **{**_EMPTY_INCLUDE, "include_collection": True},
            )

        self.assertEqual(stats["collected"], 0)
        self.assertEqual(stats["skipped"], 1)


class CommentsImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_movie_comment_is_created(self) -> None:
        data = ScrobImportData(comments={
            "movies": [{"comment": "Great flick", "spoiler": False, "created_at": "2026-01-01T00:00:00.000Z",
                        "movie": {"ids": {"tmdb": 42}}}],
            "shows": [], "seasons": [], "episodes": [],
        })
        db = _FakeSession([[], [], []])  # total update, select existing comments, final update

        stats = await apply_scrob_import(
            db, job_id=1, user_id=1, data=data, api_key=None,
            **{**_EMPTY_INCLUDE, "include_comments": True},
        )

        self.assertEqual(stats["comments"], 1)
        comments = [o for o in db.added if type(o).__name__ == "Comment"]
        self.assertEqual(comments[0].tmdb_id, 42)
        self.assertEqual(comments[0].content, "Great flick")

    async def test_duplicate_comment_is_skipped(self) -> None:
        existing = SimpleNamespace(media_type="movie", tmdb_id=42, season_number=None, episode_number=None, content="Great flick")
        data = ScrobImportData(comments={
            "movies": [{"comment": "Great flick", "spoiler": False, "movie": {"ids": {"tmdb": 42}}}],
            "shows": [], "seasons": [], "episodes": [],
        })
        db = _FakeSession([[], [existing], []])

        stats = await apply_scrob_import(
            db, job_id=1, user_id=1, data=data, api_key=None,
            **{**_EMPTY_INCLUDE, "include_comments": True},
        )

        self.assertEqual(stats["comments"], 0)
        self.assertEqual(stats["skipped"], 1)


class ApiKeysImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_fills_empty_tmdb_key_but_never_touches_scrob_api_key(self) -> None:
        settings = SimpleNamespace(tmdb_api_key=None, tvdb_api_key="existing-tvdb")
        data = ScrobImportData(api_keys={"scrob_api_key": "leaked-key", "tmdb_api_key": "new-tmdb", "tvdb_api_key": "new-tvdb"})
        db = _FakeSession([[], [settings], []])

        await apply_scrob_import(
            db, job_id=1, user_id=1, data=data, api_key=None,
            **{**_EMPTY_INCLUDE, "include_api_keys": True},
        )

        self.assertEqual(settings.tmdb_api_key, "new-tmdb")
        # Already had a tvdb key — must not be overwritten by the import.
        self.assertEqual(settings.tvdb_api_key, "existing-tvdb")
        # scrob_api_key was never even an attribute the import touches.
        self.assertFalse(hasattr(settings, "scrob_api_key"))


class MediaConnectionsImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_creates_new_connection_when_none_matches(self) -> None:
        data = ScrobImportData(media_connections=[{
            "type": "plex", "name": "Plex", "url": "http://x", "token": "tok",
            "server_user_id": None, "server_username": "me",
            "sync_collection": True, "sync_watched": True, "sync_ratings": True, "sync_playback": True,
            "push_watched": False, "push_collection": False, "push_playback": False, "push_ratings": False,
        }])
        db = _FakeSession([[], [], []])  # total update, select existing (none), final update

        stats = await apply_scrob_import(
            db, job_id=1, user_id=1, data=data, api_key=None,
            **{**_EMPTY_INCLUDE, "include_media_connections": True},
        )

        self.assertEqual(stats["connections"], 1)
        conns = [o for o in db.added if type(o).__name__ == "MediaServerConnection"]
        self.assertEqual(conns[0].token, "tok")

    async def test_skips_when_a_connection_with_the_same_type_and_url_exists(self) -> None:
        existing = SimpleNamespace(type="plex", url="http://x")
        data = ScrobImportData(media_connections=[{"type": "plex", "url": "http://x", "token": "tok"}])
        db = _FakeSession([[], [existing], []])

        stats = await apply_scrob_import(
            db, job_id=1, user_id=1, data=data, api_key=None,
            **{**_EMPTY_INCLUDE, "include_media_connections": True},
        )

        self.assertEqual(stats["connections"], 0)
        self.assertEqual(stats["skipped"], 1)


class ConnectionsImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_fills_currently_unset_fields(self) -> None:
        settings = SimpleNamespace(**{f: None for f in scrob_import._CONNECTIONS_SETTINGS_FIELDS})
        settings.trakt_client_id = "already-set"
        data = ScrobImportData(connections={"trakt_client_id": "imported-value", "mdblist_api_key": "imported-mdblist"})
        db = _FakeSession([[], [settings], []])

        await apply_scrob_import(
            db, job_id=1, user_id=1, data=data, api_key=None,
            **{**_EMPTY_INCLUDE, "include_connections": True},
        )

        self.assertEqual(settings.trakt_client_id, "already-set")
        self.assertEqual(settings.mdblist_api_key, "imported-mdblist")


class ImportEndpointValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_upload_is_rejected_without_buffering_it_all(self) -> None:
        chunk = b"0" * (2 * 1024 * 1024)
        read_calls = 0

        class _FakeUploadFile:
            filename = "export.zip"

            async def read(self, size: int = -1) -> bytes:
                nonlocal read_calls
                read_calls += 1
                return chunk

        with patch.object(export_router, "MAX_TOTAL_SIZE", 1024 * 1024):
            with self.assertRaises(HTTPException) as ctx:
                await export_router.import_data(
                    background_tasks=SimpleNamespace(add_task=lambda *a, **k: None),
                    file=_FakeUploadFile(),
                    watched=True, ratings=True, collection=True, lists=True, comments=True,
                    api_keys=False, media_connections=False, scrobble_connections=False, connections=False,
                    db=None, current_user=SimpleNamespace(id=1),
                )

        self.assertEqual(ctx.exception.status_code, 413)
        self.assertEqual(read_calls, 1)

    async def test_non_zip_filename_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            await export_router.import_data(
                background_tasks=SimpleNamespace(add_task=lambda *a, **k: None),
                file=SimpleNamespace(filename="export.rar"),
                watched=True, ratings=True, collection=True, lists=True, comments=True,
                api_keys=False, media_connections=False, scrobble_connections=False, connections=False,
                db=None, current_user=SimpleNamespace(id=1),
            )
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_no_categories_selected_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            await export_router.import_data(
                background_tasks=SimpleNamespace(add_task=lambda *a, **k: None),
                file=SimpleNamespace(filename="export.zip"),
                watched=False, ratings=False, collection=False, lists=False, comments=False,
                api_keys=False, media_connections=False, scrobble_connections=False, connections=False,
                db=None, current_user=SimpleNamespace(id=1),
            )
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()

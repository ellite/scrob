import io
import json
import os
import struct
import unittest
import unittest.mock
import zipfile

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core.trakt_export import parse_trakt_export


def _make_zip(files: dict[str, object]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, json.dumps(content))
    return buf.getvalue()


_MOVIE_PLAY = {
    "id": 1,
    "watched_at": "2026-01-11T20:00:00.000Z",
    "action": "watch",
    "type": "movie",
    "movie": {"title": "Good Fortune", "year": 2025, "ids": {"tmdb": 1114967}},
}
_EPISODE_PLAY = {
    "id": 2,
    "watched_at": "2026-07-01T11:03:00.000Z",
    "action": "scrobble",
    "type": "episode",
    "episode": {"title": "Pilot", "number": 1, "season": 1, "ids": {"tmdb": 4650466}},
    "show": {"title": "Bob's Burgers", "year": 2011, "ids": {"tmdb": 32726}},
}


class ParseTraktExportTests(unittest.TestCase):
    def test_splits_combined_history_by_type(self) -> None:
        content = _make_zip({
            "watched-history-1.json": [_MOVIE_PLAY, _EPISODE_PLAY],
        })
        data = parse_trakt_export(content)
        self.assertEqual(len(data.history_movies), 1)
        self.assertEqual(len(data.history_episodes), 1)
        self.assertEqual(data.history_movies[0]["movie"]["ids"]["tmdb"], 1114967)
        self.assertEqual(data.history_episodes[0]["show"]["ids"]["tmdb"], 32726)

    def test_concatenates_paginated_history_in_numeric_order(self) -> None:
        content = _make_zip({
            "watched-history-2.json": [{**_MOVIE_PLAY, "id": 2}],
            "watched-history-10.json": [{**_MOVIE_PLAY, "id": 10}],
            "watched-history-1.json": [{**_MOVIE_PLAY, "id": 1}],
        })
        data = parse_trakt_export(content)
        self.assertEqual([m["id"] for m in data.history_movies], [1, 2, 10])

    def test_missing_ratings_files_default_to_empty_lists(self) -> None:
        content = _make_zip({"watched-history-1.json": [_MOVIE_PLAY]})
        data = parse_trakt_export(content)
        self.assertEqual(data.ratings, {"movies": [], "shows": [], "seasons": [], "episodes": []})
        self.assertEqual(data.watchlist, [])
        self.assertEqual(data.lists, [])
        self.assertEqual(data.list_items, {})

    def test_loads_all_four_rating_kinds(self) -> None:
        content = _make_zip({
            "watched-history-1.json": [_MOVIE_PLAY],
            "ratings-movies.json": [{"rating": 8, "movie": {"ids": {"tmdb": 1}}}],
            "ratings-shows.json": [{"rating": 7, "show": {"ids": {"tmdb": 2}}}],
            "ratings-seasons.json": [{"rating": 6, "season": {"number": 1}, "show": {"ids": {"tmdb": 2}}}],
            "ratings-episodes.json": [{"rating": 9, "episode": {"season": 1, "number": 1}, "show": {"ids": {"tmdb": 2}}}],
        })
        data = parse_trakt_export(content)
        self.assertEqual(len(data.ratings["movies"]), 1)
        self.assertEqual(len(data.ratings["shows"]), 1)
        self.assertEqual(len(data.ratings["seasons"]), 1)
        self.assertEqual(len(data.ratings["episodes"]), 1)

    def test_matches_list_items_by_trakt_id_not_filename_slug(self) -> None:
        # The item file's slug segment need not match ids.slug exactly — only
        # the numeric trakt id prefix should be relied on.
        content = _make_zip({
            "watched-history-1.json": [_MOVIE_PLAY],
            "lists-lists.json": [{"name": "Ended Shows", "ids": {"trakt": 2832029, "slug": "ended-shows"}}],
            "lists-list-2832029-something-else.json": [{"type": "show", "show": {"ids": {"tmdb": 1}}}],
        })
        data = parse_trakt_export(content)
        self.assertIn("ended-shows", data.list_items)
        self.assertEqual(len(data.list_items["ended-shows"]), 1)

    def test_rejects_bad_zip(self) -> None:
        with self.assertRaises(ValueError):
            parse_trakt_export(b"not a zip file")

    def test_rejects_zip_without_watched_history(self) -> None:
        content = _make_zip({"user-profile.json": {"username": "someone"}})
        with self.assertRaises(ValueError):
            parse_trakt_export(content)

    def test_rejects_oversized_export(self) -> None:
        content = _make_zip({"watched-history-1.json": [_MOVIE_PLAY]})
        with unittest.mock.patch("core.trakt_export.MAX_TOTAL_SIZE", 10):
            with self.assertRaises(ValueError):
                parse_trakt_export(content)

    def test_declared_size_lie_cannot_bypass_the_size_cap(self) -> None:
        # ZipInfo.file_size is metadata the zip itself declares — an attacker can
        # lie about it. Build an entry whose real (compressible) content is much
        # bigger than what its central directory / local header claim, and
        # confirm the cap still catches it from bytes actually read, not the
        # declared value.
        real_payload = b"0" * (2 * 1024 * 1024)  # 2MB, highly compressible
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("watched-history-1.json", b"[]")
            zf.writestr("watched-history-2.json", real_payload)
        raw = bytes(buf.getvalue())

        true_size_bytes = struct.pack("<I", len(real_payload))
        fake_size_bytes = struct.pack("<I", 10)
        self.assertEqual(raw.count(true_size_bytes), 2)  # local header + central directory
        tampered = raw.replace(true_size_bytes, fake_size_bytes)

        with unittest.mock.patch("core.trakt_export.MAX_ENTRY_SIZE", 1024):
            with self.assertRaises(ValueError):
                parse_trakt_export(tampered)

    def test_corrupted_entry_raises_a_clean_error(self) -> None:
        content = _make_zip({"watched-history-1.json": [_MOVIE_PLAY]})
        with unittest.mock.patch.object(
            zipfile.ZipExtFile, "read", side_effect=zipfile.BadZipFile("Bad CRC-32 for file 'x'")
        ):
            with self.assertRaises(ValueError) as ctx:
                parse_trakt_export(content)
        self.assertIn("corrupted", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

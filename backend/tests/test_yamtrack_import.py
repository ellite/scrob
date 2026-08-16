import csv
import io
import os
import unittest

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core.yamtrack_import import parse_yamtrack_csv

_VANILLA_HEADER = [
    "media_id", "source", "media_type", "title", "image", "season_number", "episode_number",
    "score", "status", "notes", "start_date", "end_date", "progress", "created_at", "progressed_at",
]

_FLOPPY_HEADER = [
    "row_type", *_VANILLA_HEADER[:],
    "list_uid", "list_name", "list_description", "list_tags", "list_visibility",
    "list_allow_recommendations", "list_source", "list_source_id", "list_is_smart",
    "list_smart_media_types", "list_smart_excluded_media_types", "list_smart_filters",
    "list_item_date_added",
    "collection_format", "collection_resolution", "collection_hdr", "collection_is_3d",
    "collection_audio_codec", "collection_audio_channels", "collection_bitrate",
    "collection_collected_at",
]


def _make_csv(header: list[str], rows: list[dict]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL)
    writer.writerow(header)
    for row in rows:
        writer.writerow([row.get(col, "") for col in header])
    return buf.getvalue().encode()


def _movie_row(**overrides) -> dict:
    row = {
        "media_id": "680", "source": "tmdb", "media_type": "movie", "title": "Pulp Fiction",
        "status": "Completed", "created_at": "2026-01-01T00:00:00+00:00", "end_date": "2026-01-02T00:00:00+00:00",
    }
    row.update(overrides)
    return row


def _episode_row(**overrides) -> dict:
    row = {
        "media_id": "1668", "source": "tmdb", "media_type": "episode", "title": "Friends",
        "season_number": "1", "episode_number": "1",
        "created_at": "2026-01-01T00:00:00+00:00", "end_date": "2026-01-02T00:00:00+00:00",
    }
    row.update(overrides)
    return row


class ParseYamtrackCsvVanillaTests(unittest.TestCase):
    def test_completed_movie_becomes_history_entry(self) -> None:
        data = parse_yamtrack_csv(_make_csv(_VANILLA_HEADER, [_movie_row(status="Completed")]))
        self.assertEqual(len(data.history_movies), 1)
        self.assertEqual(data.history_movies[0]["movie"]["ids"]["tmdb"], 680)
        self.assertEqual(data.history_movies[0]["watched_at"], "2026-01-02T00:00:00+00:00")

    def test_non_completed_statuses_are_excluded_from_history(self) -> None:
        for status in ("In progress", "Paused", "Dropped", "Planning"):
            data = parse_yamtrack_csv(_make_csv(_VANILLA_HEADER, [_movie_row(status=status)]))
            self.assertEqual(data.history_movies, [], f"status={status!r} should not produce history")

    def test_episode_row_is_watched_regardless_of_status_field(self) -> None:
        data = parse_yamtrack_csv(_make_csv(_VANILLA_HEADER, [_episode_row()]))
        self.assertEqual(len(data.history_episodes), 1)
        entry = data.history_episodes[0]
        self.assertEqual(entry["show"]["ids"]["tmdb"], 1668)
        self.assertEqual(entry["episode"], {"season": 1, "number": 1})
        self.assertEqual(entry["watched_at"], "2026-01-02T00:00:00+00:00")

    def test_episode_watched_at_falls_back_when_end_date_missing(self) -> None:
        data = parse_yamtrack_csv(_make_csv(_VANILLA_HEADER, [
            _episode_row(end_date="", progressed_at="", created_at="2026-03-01T00:00:00+00:00"),
        ]))
        self.assertEqual(data.history_episodes[0]["watched_at"], "2026-03-01T00:00:00+00:00")

    def test_non_tmdb_source_is_skipped_entirely(self) -> None:
        data = parse_yamtrack_csv(_make_csv(_VANILLA_HEADER, [
            _movie_row(source="mal", media_type="anime", title="Hellsing", media_id="270"),
        ]))
        self.assertEqual(data.history_movies, [])
        self.assertEqual(data.ratings, {})
        self.assertEqual(data.watchlist, [])

    def test_movie_score_becomes_a_movie_rating(self) -> None:
        data = parse_yamtrack_csv(_make_csv(_VANILLA_HEADER, [_movie_row(score="9.0")]))
        self.assertEqual(len(data.ratings.get("movies", [])), 1)
        self.assertEqual(data.ratings["movies"][0]["rating"], "9.0")
        self.assertEqual(data.ratings["movies"][0]["movie"]["ids"]["tmdb"], 680)

    def test_show_score_becomes_a_show_rating(self) -> None:
        data = parse_yamtrack_csv(_make_csv(_VANILLA_HEADER, [
            {"media_id": "1668", "source": "tmdb", "media_type": "tv", "title": "Friends", "score": "8.5"},
        ]))
        self.assertEqual(len(data.ratings.get("shows", [])), 1)
        self.assertEqual(data.ratings["shows"][0]["show"]["ids"]["tmdb"], 1668)

    def test_season_score_becomes_a_season_rating(self) -> None:
        data = parse_yamtrack_csv(_make_csv(_VANILLA_HEADER, [
            {"media_id": "1668", "source": "tmdb", "media_type": "season", "title": "Friends", "season_number": "2", "score": "7.0"},
        ]))
        self.assertEqual(len(data.ratings.get("seasons", [])), 1)
        self.assertEqual(data.ratings["seasons"][0]["season"], {"number": 2})

    def test_episode_rows_never_produce_a_rating(self) -> None:
        # Neither Yamtrack nor Floppy has a score field on Episode at all,
        # so an episode row is never expected to carry one - confirm the
        # parser doesn't invent an "episodes" rating bucket regardless.
        data = parse_yamtrack_csv(_make_csv(_VANILLA_HEADER, [_episode_row()]))
        self.assertNotIn("episodes", data.ratings)

    def test_planning_movie_goes_to_watchlist(self) -> None:
        data = parse_yamtrack_csv(_make_csv(_VANILLA_HEADER, [_movie_row(status="Planning")]))
        self.assertEqual(data.watchlist, [{"type": "movie", "movie": {"ids": {"tmdb": 680}, "title": "Pulp Fiction"}}])

    def test_planning_show_goes_to_watchlist(self) -> None:
        data = parse_yamtrack_csv(_make_csv(_VANILLA_HEADER, [
            {"media_id": "1668", "source": "tmdb", "media_type": "tv", "title": "Friends", "status": "Planning"},
        ]))
        self.assertEqual(data.watchlist, [{"type": "show", "show": {"ids": {"tmdb": 1668}, "title": "Friends"}}])

    def test_planning_season_does_not_produce_a_watchlist_entry(self) -> None:
        # Watchlisting is only meaningful at movie/show granularity -
        # matches _resolve_list_item_media's existing movie/show-only support.
        data = parse_yamtrack_csv(_make_csv(_VANILLA_HEADER, [
            {"media_id": "1668", "source": "tmdb", "media_type": "season", "title": "Friends", "season_number": "1", "status": "Planning"},
        ]))
        self.assertEqual(data.watchlist, [])

    def test_notes_become_a_comment(self) -> None:
        data = parse_yamtrack_csv(_make_csv(_VANILLA_HEADER, [_movie_row(notes="Great rewatch")]))
        self.assertEqual(len(data.comments.get("movies", [])), 1)
        self.assertEqual(data.comments["movies"][0]["comment"], "Great rewatch")

    def test_empty_notes_produce_no_comment(self) -> None:
        data = parse_yamtrack_csv(_make_csv(_VANILLA_HEADER, [_movie_row(notes="")]))
        self.assertEqual(data.comments, {})

    def test_vanilla_file_has_no_collection_or_list_data(self) -> None:
        data = parse_yamtrack_csv(_make_csv(_VANILLA_HEADER, [_movie_row(), _episode_row()]))
        self.assertEqual(data.collection_movies, [])
        self.assertEqual(data.collection_episodes, [])
        self.assertEqual(data.lists, [])
        self.assertEqual(data.list_items, {})

    def test_missing_required_columns_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            parse_yamtrack_csv(b"foo,bar\n1,2\n")

    def test_empty_file_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            parse_yamtrack_csv(b"")

    def test_non_numeric_ids_are_skipped_not_fatal(self) -> None:
        # float("inf")/float("1e400") don't raise, but int() on the result
        # does (OverflowError, not ValueError) - a crafted id value must be
        # dropped like any other unparseable row, not crash the whole import.
        for bad_id in ("inf", "-inf", "1e400", "not-a-number", ""):
            data = parse_yamtrack_csv(_make_csv(_VANILLA_HEADER, [_movie_row(media_id=bad_id)]))
            self.assertEqual(data.history_movies, [], f"media_id={bad_id!r} should be skipped, not crash")

    def test_field_over_csv_size_limit_raises_value_error_not_csv_error(self) -> None:
        # Python's csv module caps a single field at 128KB and raises
        # csv.Error mid-iteration - well within our overall file-size cap,
        # so a real, reachable case. Must surface as a clean ValueError
        # (400 to the caller), not an unhandled 500.
        oversized = "x" * 200_000
        content = ("media_id,source,media_type,title\n" f"1,tmdb,movie,{oversized}\n").encode()
        with self.assertRaises(ValueError):
            parse_yamtrack_csv(content)

    def test_non_numeric_season_or_episode_number_is_skipped(self) -> None:
        for bad_value in ("inf", "1e400", "not-a-number"):
            data = parse_yamtrack_csv(_make_csv(_VANILLA_HEADER, [_episode_row(season_number=bad_value)]))
            self.assertEqual(data.history_episodes, [])
            data = parse_yamtrack_csv(_make_csv(_VANILLA_HEADER, [_episode_row(episode_number=bad_value)]))
            self.assertEqual(data.history_episodes, [])


class ParseYamtrackCsvFloppyTests(unittest.TestCase):
    def _floppy_row(self, row_type: str, **overrides) -> dict:
        row = {"row_type": row_type}
        row.update(overrides)
        return row

    def test_row_type_media_behaves_like_vanilla(self) -> None:
        data = parse_yamtrack_csv(_make_csv(_FLOPPY_HEADER, [
            self._floppy_row("media", **_movie_row(status="Completed")),
        ]))
        self.assertEqual(len(data.history_movies), 1)

    def test_collection_row_becomes_collected_movie(self) -> None:
        data = parse_yamtrack_csv(_make_csv(_FLOPPY_HEADER, [
            self._floppy_row("collection", media_id="680", source="tmdb", media_type="movie", title="Pulp Fiction"),
        ]))
        self.assertEqual(data.collection_movies, [{"movie": {"ids": {"tmdb": 680}, "title": "Pulp Fiction"}}])

    def test_collection_row_becomes_collected_episode(self) -> None:
        data = parse_yamtrack_csv(_make_csv(_FLOPPY_HEADER, [
            self._floppy_row("collection", media_id="1668", source="tmdb", media_type="episode",
                              title="Friends", season_number="1", episode_number="1"),
        ]))
        self.assertEqual(len(data.collection_episodes), 1)
        self.assertEqual(data.collection_episodes[0]["episode"], {"season": 1, "number": 1})

    def test_collection_row_for_show_or_season_is_ignored(self) -> None:
        data = parse_yamtrack_csv(_make_csv(_FLOPPY_HEADER, [
            self._floppy_row("collection", media_id="1668", source="tmdb", media_type="tv", title="Friends"),
        ]))
        self.assertEqual(data.collection_movies, [])
        self.assertEqual(data.collection_episodes, [])

    def test_list_and_list_item_rows_are_linked_by_uid(self) -> None:
        data = parse_yamtrack_csv(_make_csv(_FLOPPY_HEADER, [
            self._floppy_row("list", list_uid="list1", list_name="Favorites", list_description="desc", list_visibility="public"),
            self._floppy_row("list_item", media_id="680", source="tmdb", media_type="movie", title="Pulp Fiction", list_uid="list1"),
        ]))
        self.assertEqual(data.lists, [{"name": "Favorites", "description": "desc", "privacy": "public", "ids": {"slug": "list1"}}])
        self.assertEqual(data.list_items, {"list1": [{"type": "movie", "movie": {"ids": {"tmdb": 680}, "title": "Pulp Fiction"}}]})

    def test_unrecognized_visibility_falls_back_to_private(self) -> None:
        data = parse_yamtrack_csv(_make_csv(_FLOPPY_HEADER, [
            self._floppy_row("list", list_uid="list1", list_name="Mystery", list_visibility="unlisted"),
        ]))
        self.assertEqual(data.lists[0]["privacy"], "private")

    def test_list_item_for_season_or_episode_is_skipped(self) -> None:
        data = parse_yamtrack_csv(_make_csv(_FLOPPY_HEADER, [
            self._floppy_row("list", list_uid="list1", list_name="Favorites"),
            self._floppy_row("list_item", media_id="1668", source="tmdb", media_type="season",
                              title="Friends", season_number="1", list_uid="list1"),
        ]))
        self.assertEqual(data.list_items, {})

    def test_non_tmdb_list_item_is_skipped(self) -> None:
        data = parse_yamtrack_csv(_make_csv(_FLOPPY_HEADER, [
            self._floppy_row("list", list_uid="list1", list_name="Favorites"),
            self._floppy_row("list_item", media_id="270", source="mal", media_type="anime", title="Hellsing", list_uid="list1"),
        ]))
        self.assertEqual(data.list_items, {})

    def test_unknown_row_type_is_ignored(self) -> None:
        data = parse_yamtrack_csv(_make_csv(_FLOPPY_HEADER, [
            self._floppy_row("something_new", media_id="680", source="tmdb", media_type="movie", title="Pulp Fiction", status="Completed"),
        ]))
        self.assertEqual(data.history_movies, [])


if __name__ == "__main__":
    unittest.main()

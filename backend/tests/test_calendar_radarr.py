"""Radarr movie releases in the calendar and the Radarr filter (#501)."""

import os
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from routers.calendar import _radarr_calendar_entries, _resolve_watch_status


class RadarrCalendarEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start = date(2026, 10, 12)
        self.end = date(2026, 11, 1)

    def test_each_release_inside_the_window_becomes_an_entry(self) -> None:
        got = _radarr_calendar_entries([{
            "title": "Film", "tmdbId": 99,
            "inCinemas": "2026-10-01T00:00:00Z",
            "digitalRelease": "2026-10-20T00:00:00Z",
            "physicalRelease": "2026-10-28T00:00:00Z",
            "images": [{"coverType": "poster", "remoteUrl": "https://image.tmdb.org/t/p/original/abc.jpg"}],
        }], self.start, self.end)
        self.assertEqual(
            [(e["air_date"], e["release_type"]) for e in got],
            [("2026-10-20", "digital"), ("2026-10-28", "physical")],
        )
        self.assertEqual(got[0]["entry_type"], "movie")
        self.assertEqual(got[0]["movie_tmdb_id"], 99)
        self.assertEqual(got[0]["show_title"], "Film")
        # TMDB posters are reduced to their path so the frontend proxies a small size.
        self.assertEqual(got[0]["poster_path"], "/abc.jpg")
        self.assertIsNone(got[0]["show_id"])
        self.assertIsNone(got[0]["season_number"])

    def test_invalid_and_missing_dates_are_skipped(self) -> None:
        got = _radarr_calendar_entries([
            {"title": "No dates", "tmdbId": 1},
            {"title": "Bad", "tmdbId": 2, "inCinemas": "soon"},
            {"title": "Later", "tmdbId": 3, "inCinemas": "2026-11-02T00:00:00Z"},
            {"title": "Edge", "tmdbId": 4, "inCinemas": "2026-11-01T00:00:00Z"},
        ], self.start, self.end)
        self.assertEqual([e["movie_tmdb_id"] for e in got], [4])

    def test_non_tmdb_poster_is_kept_and_local_paths_dropped(self) -> None:
        got = _radarr_calendar_entries([
            {"title": "A", "tmdbId": 1, "inCinemas": "2026-10-20",
             "images": [{"coverType": "poster", "remoteUrl": "https://example.org/a.jpg"}]},
            {"title": "B", "tmdbId": 2, "inCinemas": "2026-10-20",
             "images": [{"coverType": "poster", "url": "/MediaCover/2/poster.jpg"}]},
        ], self.start, self.end)
        self.assertEqual([e["poster_path"] for e in got], ["https://example.org/a.jpg", None])


def _rows(*results):
    """A db whose execute() answers each query with the next list of rows."""
    return SimpleNamespace(execute=AsyncMock(side_effect=[
        SimpleNamespace(all=lambda rows=rows: rows) for rows in results
    ]))


class MovieWatchStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_movie_flags_come_from_the_library(self) -> None:
        entries = [
            {"entry_type": "movie", "movie_tmdb_id": 99, "show_id": None},
            {"entry_type": "movie", "movie_tmdb_id": 98, "show_id": None},
            {"entry_type": "movie", "movie_tmdb_id": None, "show_id": None},
        ]
        # Movie Media rows, then collected ids, then watched ids.
        db = _rows([(99, 1), (98, 2)], [(1,)], [(2,)])
        await _resolve_watch_status(db, 7, entries)
        self.assertEqual(
            [(e["collected"], e["watched"]) for e in entries],
            [(True, False), (False, True), (False, False)],
        )

    async def test_no_known_ids_means_no_queries(self) -> None:
        entries = [{"entry_type": "movie", "movie_tmdb_id": None, "show_id": None}]
        db = _rows()
        await _resolve_watch_status(db, 7, entries)
        self.assertEqual((entries[0]["collected"], entries[0]["watched"]), (False, False))
        db.execute.assert_not_awaited()


class ArrCalendarFilterTests(unittest.IsolatedAsyncioTestCase):
    """Each filter pair only narrows its own media type."""

    def setUp(self) -> None:
        self.entries = [
            {"entry_type": "movie", "movie_tmdb_id": 1},
            {"entry_type": "movie", "movie_tmdb_id": 2},
            {"show_tmdb_id": 10, "show_tvdb_id": None},
            {"show_tmdb_id": None, "show_tvdb_id": 20},
            {"show_tmdb_id": 30, "show_tvdb_id": None},
            {"show_tmdb_id": 40, "show_tvdb_id": None},
            {"entry_type": "movie", "movie_tmdb_id": 3},
        ]
        self.radarr = SimpleNamespace(radarr_url="http://radarr.invalid", radarr_token="synthetic")
        self.sonarr = SimpleNamespace(sonarr_url="http://sonarr.invalid", sonarr_token="synthetic")

    async def _apply(self, cfgs, radarr_ids=({1, 2}, {2}), **flags):
        from routers import calendar

        flags = {"sonarr_only": False, "sonarr_monitored": False,
                 "radarr_only": False, "radarr_monitored": False, **flags}
        result = {"calendar": {"entries": list(self.entries)}}
        with patch.object(calendar, "_arr_configs", new=AsyncMock(return_value=cfgs)), \
             patch("core.radarr.get_all_movie_tmdb_ids", new=AsyncMock(return_value=radarr_ids[0])), \
             patch("core.radarr.get_monitored_movie_tmdb_ids", new=AsyncMock(return_value=radarr_ids[1])), \
             patch("core.sonarr.get_all_series_ids", new=AsyncMock(return_value=({10, 30}, {20}))), \
             patch("core.sonarr.get_monitored_series_ids", new=AsyncMock(return_value=({10}, set()))):
            return await calendar._apply_arr(None, 1, result, **flags)

    def _ids(self, result):
        return [e.get("movie_tmdb_id") or e.get("show_tmdb_id") or e.get("show_tvdb_id")
                for e in result["calendar"]["entries"]]

    async def test_no_filter_keeps_everything_and_reports_configuration(self) -> None:
        result = await self._apply((self.radarr, self.sonarr))
        self.assertEqual(len(result["calendar"]["entries"]), 7)
        self.assertTrue(result["radarr_configured"])
        self.assertTrue(result["sonarr_configured"])

    async def test_radarr_monitored_narrows_movies_and_keeps_episodes(self) -> None:
        result = await self._apply((self.radarr, self.sonarr), radarr_monitored=True)
        self.assertEqual(self._ids(result), [2, 10, 20, 30, 40])

    async def test_radarr_only_drops_movies_not_in_radarr(self) -> None:
        result = await self._apply((self.radarr, self.sonarr), radarr_only=True)
        self.assertEqual(self._ids(result), [1, 2, 10, 20, 30, 40])

    async def test_sonarr_filter_keeps_movies(self) -> None:
        result = await self._apply((self.radarr, self.sonarr), sonarr_monitored=True)
        self.assertEqual(self._ids(result), [1, 2, 10, 3])

    async def test_sonarr_only_matches_tmdb_or_tvdb(self) -> None:
        result = await self._apply((self.radarr, self.sonarr), sonarr_only=True)
        self.assertEqual(self._ids(result), [1, 2, 10, 20, 30, 3])

    async def test_both_filters_combine(self) -> None:
        result = await self._apply((self.radarr, self.sonarr), radarr_monitored=True, sonarr_only=True)
        self.assertEqual(self._ids(result), [2, 10, 20, 30])

    async def test_unreachable_radarr_leaves_movies_unfiltered(self) -> None:
        result = await self._apply((self.radarr, None), radarr_ids=(None, None), radarr_only=True)
        self.assertEqual(len(result["calendar"]["entries"]), 7)
        self.assertFalse(result["sonarr_configured"])

    async def test_cached_movies_disappear_once_radarr_is_removed(self) -> None:
        # The cached payload still holds movies from when Radarr was configured.
        result = await self._apply((None, self.sonarr))
        self.assertEqual(self._ids(result), [10, 20, 30, 40])
        self.assertFalse(result["radarr_configured"])

    async def test_radarr_flag_without_radarr_configured_is_ignored(self) -> None:
        result = await self._apply((None, self.sonarr), radarr_only=True, sonarr_only=True)
        self.assertEqual(self._ids(result), [10, 20, 30])
        self.assertFalse(result["radarr_configured"])


class ComputeCalendarRadarrTests(unittest.IsolatedAsyncioTestCase):
    """Movies are opt-in by configuration: no Radarr, no movie entries."""

    RADARR = SimpleNamespace(radarr_url="http://radarr.invalid", radarr_token="synthetic")
    SHOW = SimpleNamespace(id=5, tmdb_id=500, tvdb_id=None, canonical_source="tmdb",
                           title="Show", poster_path="/show.jpg")

    async def _compute(self, radarr_cfg, calendar_rows, shows=(), order_keys=None, positions=None):
        from routers import calendar

        get_calendar = AsyncMock(return_value=calendar_rows)
        detail = {"name": "Show", "next_episode_to_air": {"season_number": 2}}
        season = {"episodes": [
            {"air_date": "2026-10-20", "season_number": 2, "episode_number": 1, "name": "One"},
            {"air_date": "2026-10-19", "season_number": 2, "episode_number": 0, "name": "Zero"},
        ]}
        with patch.object(calendar, "_candidate_shows", new=AsyncMock(return_value=list(shows))), \
             patch.object(calendar, "_arr_configs", new=AsyncMock(return_value=(radarr_cfg, None))), \
             patch.object(calendar, "_server_today", return_value=date(2026, 10, 19)), \
             patch.object(calendar, "get_order_keys_for_series", new=AsyncMock(return_value=order_keys or {})), \
             patch.object(calendar, "get_positions_for_series", new=AsyncMock(return_value=positions or {})), \
             patch("routers.media.get_user_tmdb_key", new=AsyncMock(return_value="key")), \
             patch("routers.media.check_tmdb_key", return_value=True), \
             patch("routers.shows.get_user_tvdb_key", new=AsyncMock(return_value=None)), \
             patch("core.translations.get_user_metadata_language", new=AsyncMock(return_value=None)), \
             patch("core.tmdb.get_show_light", new=AsyncMock(return_value=detail)), \
             patch("core.tmdb.get_season", new=AsyncMock(return_value=season)), \
             patch("core.radarr.get_calendar", new=get_calendar):
            payload = await calendar.compute_calendar(None, 1)
        return payload, get_calendar

    def _rows(self, payload):
        return [(e["air_date"], e.get("entry_type", "episode"), e["show_title"]) for e in payload["entries"]]

    async def test_mixed_shows_and_movies_sort_by_day_and_only_episodes_get_ordering(self) -> None:
        payload, _ = await self._compute(
            self.RADARR,
            [{"title": "Film", "tmdbId": 99, "inCinemas": "2026-10-19T00:00:00Z"}],
            shows=[self.SHOW],
            order_keys={500: "dvd"},
            positions={(500, "dvd"): {(2, 1): SimpleNamespace(display_season=1, display_episode=7)}},
        )
        self.assertEqual(self._rows(payload), [
            ("2026-10-19", "movie", "Film"),
            ("2026-10-19", "episode", "Show"),
            ("2026-10-20", "episode", "Show"),
        ])
        movie, zero, one = payload["entries"]
        self.assertNotIn("show_episode_order", movie)
        self.assertEqual(zero["show_episode_order"], "dvd")
        self.assertEqual((one["display_season_number"], one["display_episode_number"]), (1, 7))

    async def test_broken_radarr_answers_keep_the_episode_calendar(self) -> None:
        from core import radarr

        def client_answering(payload=None, error=None):
            client = AsyncMock()
            if error:
                client.get.side_effect = error
            else:
                response = MagicMock()
                response.json.return_value = payload
                client.get.return_value = response
            return client

        cases = {
            "unreachable": client_answering(error=RuntimeError("down")),
            "dict instead of list": client_answering({"error": "not radarr"}),
            "non-dict rows": client_answering(["junk", 7, None]),
        }
        for name, client in cases.items():
            with self.subTest(name):
                with patch.object(radarr.httpx, "AsyncClient") as factory:
                    factory.return_value.__aenter__.return_value = client
                    rows = await radarr.get_calendar("http://radarr.invalid", "synthetic", "a", "b")
                payload, _ = await self._compute(self.RADARR, rows, shows=[self.SHOW])
                self.assertEqual([r[1] for r in self._rows(payload)], ["episode", "episode"])

    async def test_without_radarr_nothing_is_fetched(self) -> None:
        payload, get_calendar = await self._compute(None, [])
        self.assertEqual(payload["entries"], [])
        get_calendar.assert_not_awaited()

    async def test_with_radarr_movies_are_added_even_without_shows(self) -> None:
        cfg = SimpleNamespace(radarr_url="http://radarr.invalid", radarr_token="synthetic")
        payload, get_calendar = await self._compute(cfg, [
            {"title": "Film", "tmdbId": 99, "digitalRelease": "2026-10-20T00:00:00Z"},
        ])
        self.assertEqual([(e["movie_tmdb_id"], e["air_date"]) for e in payload["entries"]], [(99, "2026-10-20")])
        # Lookback start, and one day past the 90-day window end.
        self.assertEqual(get_calendar.await_args.args[2:], ("2026-10-12", "2027-01-17"))


class AiringTodayTests(unittest.IsolatedAsyncioTestCase):
    async def test_movie_releases_stay_out_of_the_episode_widget(self) -> None:
        from routers import media

        row = SimpleNamespace(payload={"entries": [
            {"air_date": "2026-10-19", "show_tmdb_id": 10, "show_title": "Show",
             "season_number": 1, "episode_number": 2, "episode_name": "Two"},
            {"entry_type": "movie", "air_date": "2026-10-19", "movie_tmdb_id": 99, "show_title": "Film"},
        ]})
        db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
            scalars=lambda: SimpleNamespace(first=lambda: row)
        )))
        with patch("routers.calendar._is_cache_fresh", return_value=True), \
             patch("routers.calendar._server_today", return_value=date(2026, 10, 19)), \
             patch.object(media, "enrich_with_state", new=AsyncMock()):
            got = await media.airing_today_collected(db=db, current_user=SimpleNamespace(id=1))
        self.assertEqual([r["show_title"] for r in got["results"]], ["Show"])


class RadarrClientTests(unittest.IsolatedAsyncioTestCase):
    def _client(self, payload):
        response = MagicMock()
        response.json.return_value = payload
        client = AsyncMock()
        client.get.return_value = response
        return client

    async def test_monitored_ids_come_from_the_same_snapshot(self) -> None:
        from core import radarr

        client = self._client([
            {"tmdbId": 1, "monitored": True}, {"tmdbId": 2, "monitored": False}, {"tmdbId": None, "monitored": True},
        ])
        radarr._LIBRARY_CACHE.clear()
        try:
            with patch.object(radarr.httpx, "AsyncClient") as factory:
                factory.return_value.__aenter__.return_value = client
                self.assertEqual(await radarr.get_all_movie_tmdb_ids("http://radarr.invalid", "synthetic"), {1, 2})
                self.assertEqual(await radarr.get_monitored_movie_tmdb_ids("http://radarr.invalid", "synthetic"), {1})
            self.assertEqual(client.get.await_count, 1)
        finally:
            radarr._LIBRARY_CACHE.clear()

    async def test_calendar_requests_the_window_including_unmonitored(self) -> None:
        from core import radarr

        client = self._client([{"tmdbId": 5}])
        with patch.object(radarr.httpx, "AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            got = await radarr.get_calendar("http://radarr.invalid/", "synthetic", "2026-10-12", "2026-11-02")
        self.assertEqual(got, [{"tmdbId": 5}])
        url = client.get.await_args.args[0]
        params = client.get.await_args.kwargs["params"]
        self.assertEqual(url, "http://radarr.invalid/api/v3/calendar")
        self.assertEqual(params, {"start": "2026-10-12", "end": "2026-11-02", "unmonitored": "true"})

    async def test_calendar_failure_returns_empty(self) -> None:
        from core import radarr

        client = AsyncMock()
        client.get.side_effect = RuntimeError("down")
        with patch.object(radarr.httpx, "AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            self.assertEqual(await radarr.get_calendar("http://radarr.invalid", "synthetic", "a", "b"), [])

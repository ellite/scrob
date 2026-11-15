from datetime import date

from routers.calendar import CALENDAR_WINDOW_DAYS, _seasons_to_scan, _trim_window


def _payload():
    return {
        "calendar": {
            "today": "2026-10-19",
            "entries": [
                {"air_date": "2026-10-12"},
                {"air_date": "2026-10-19"},
                {"air_date": "2026-11-01"},
                {"air_date": "2026-11-18"},
                {"air_date": "2027-01-10"},
            ],
        }
    }


def test_trim_window_keeps_today_plus_days_minus_one():
    # #467: today is day one, so a 14-day window ends on 2026-11-01.
    got = _trim_window(_payload(), 14)["calendar"]["entries"]
    assert [e["air_date"] for e in got] == ["2026-10-12", "2026-10-19", "2026-11-01"]


def test_trim_window_longer_horizon_and_untouched_cache():
    p = _payload()
    assert len(_trim_window(p, 30)["calendar"]["entries"]) == 3
    assert len(_trim_window(p, 31)["calendar"]["entries"]) == 4
    assert len(_trim_window(p, CALENDAR_WINDOW_DAYS)["calendar"]["entries"]) == 5
    assert len(p["calendar"]["entries"]) == 5


_WINDOW_START = date(2026, 10, 19)


def test_seasons_to_scan_includes_recently_aired_season_when_no_next_episode():
    # #518: the episode aired yesterday and TMDB has no dated next episode, so
    # next_episode_to_air is null - the calendar must still scan season 8.
    detail = {
        "next_episode_to_air": None,
        "last_episode_to_air": {"season_number": 8, "air_date": "2026-10-25"},
        "seasons": [{"season_number": 8}],
    }
    assert _seasons_to_scan(detail, _WINDOW_START) == {8}


def test_seasons_to_scan_skips_last_episode_older_than_the_window():
    detail = {
        "next_episode_to_air": None,
        "last_episode_to_air": {"season_number": 3, "air_date": "2026-09-01"},
        "seasons": [{"season_number": 3}],
    }
    assert _seasons_to_scan(detail, _WINDOW_START) == set()


def test_seasons_to_scan_combines_next_last_and_specials():
    detail = {
        "next_episode_to_air": {"season_number": 2, "air_date": "2026-11-02"},
        "last_episode_to_air": {"season_number": 1, "air_date": "2026-10-26"},
        "seasons": [{"season_number": 0}, {"season_number": 1}, {"season_number": 2}],
    }
    assert _seasons_to_scan(detail, _WINDOW_START) == {0, 1, 2}


# --- an incomplete calendar must not stick for the day (airing-today bug) -----

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from routers import calendar as calendar_router
from routers.calendar import CALENDAR_SCHEMA, INCOMPLETE_RETRY, _is_cache_fresh


def _row(age: timedelta, **payload):
    today = calendar_router._server_today().isoformat()
    body = {"schema": CALENDAR_SCHEMA, "today": today, "entries": [], **payload}
    return SimpleNamespace(payload=body, computed_at=calendar_router.utcnow() - age)


def test_complete_cache_stays_fresh_all_day():
    assert _is_cache_fresh(_row(timedelta(hours=3), failed_fetches=0))
    assert _is_cache_fresh(_row(timedelta(hours=3)))  # rows written before the field existed


def test_cache_with_failed_fetches_is_trusted_only_briefly():
    assert _is_cache_fresh(_row(INCOMPLETE_RETRY - timedelta(seconds=30), failed_fetches=5, shows_checked=10))
    assert not _is_cache_fresh(_row(INCOMPLETE_RETRY + timedelta(seconds=30), failed_fetches=5, shows_checked=10))


def test_a_stray_failing_show_does_not_force_recomputes():
    assert _is_cache_fresh(_row(INCOMPLETE_RETRY + timedelta(hours=1), failed_fetches=1, shows_checked=348))


def test_compute_calendar_counts_failed_provider_calls():
    shows = [
        SimpleNamespace(id=1, tmdb_id=10, tvdb_id=None, title="Down", poster_path=None, status="Returning Series"),
        SimpleNamespace(id=2, tmdb_id=20, tvdb_id=None, title="Up", poster_path=None, status="Returning Series"),
    ]

    async def light(tmdb_id, **kw):
        if tmdb_id == 10:
            raise RuntimeError("TMDB circuit breaker open")
        return {"name": "Up", "next_episode_to_air": {"season_number": 1, "air_date": "2099-01-01"}}

    async def season(tmdb_id, sn, **kw):
        return {"episodes": []}

    with patch.object(calendar_router, "_candidate_shows", AsyncMock(return_value=shows)), \
         patch("routers.media.get_user_tmdb_key", AsyncMock(return_value="k")), \
         patch("routers.media.check_tmdb_key", lambda k: True), \
         patch("routers.shows.get_user_tvdb_key", AsyncMock(return_value=None)), \
         patch("core.translations.get_user_metadata_language", AsyncMock(return_value=None)), \
         patch("core.tmdb.get_show_light", light), patch("core.tmdb.get_season", season), \
         patch.object(calendar_router, "get_order_keys_for_series", AsyncMock(return_value={})):
        payload = asyncio.run(calendar_router.compute_calendar(AsyncMock(), 1))

    assert payload["failed_fetches"] == 1
    assert payload["shows_checked"] == 2

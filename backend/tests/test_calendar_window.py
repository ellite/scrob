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

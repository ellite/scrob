from routers.calendar import CALENDAR_WINDOW_DAYS, _trim_window


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

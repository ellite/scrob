"""Serialize the existing episode calendar as RFC 5545 all-day events."""

from datetime import date, timedelta


def _text(value):
    return str(value or "").replace("\\", "\\\\").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")


def _fold(line):
    parts = []
    current = ""
    size = 0
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > 75:
            parts.append(current)
            current, size = " ", 1
        current += char
        size += width
    parts.append(current)
    return "\r\n".join(parts)


def render_calendar(entries, generated_at):
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Scrob//Episode Calendar//EN", "CALSCALE:GREGORIAN"]
    seen = set()
    for entry in entries:
        try:
            day = date.fromisoformat(entry.get("air_date") or "")
        except (ValueError, TypeError):
            continue
        if entry.get("show_tmdb_id"):
            show = f"tmdb-{entry['show_tmdb_id']}"
        elif entry.get("show_tvdb_id"):
            show = f"tvdb-{entry['show_tvdb_id']}"
        else:
            continue
        season, episode = entry.get("season_number"), entry.get("episode_number")
        if season is None or episode is None:
            continue
        uid = f"show-{show}-s{season}-e{episode}@scrob"
        if uid in seen:
            continue
        seen.add(uid)
        display_season = entry.get("display_season_number", season)
        display_episode = entry.get("display_episode_number", episode)
        summary = f"{entry.get('show_title') or ''} - S{display_season}E{display_episode}"
        if entry.get("episode_name"):
            summary += f" - {entry['episode_name']}"
        lines.extend([
            "BEGIN:VEVENT", f"UID:{uid}",
            f"DTSTAMP:{generated_at.strftime('%Y%m%dT%H%M%SZ')}",
            f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{(day + timedelta(days=1)).strftime('%Y%m%d')}",
            f"SUMMARY:{_text(summary)}", "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"

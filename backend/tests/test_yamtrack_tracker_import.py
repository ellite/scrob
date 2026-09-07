import csv
import io
import os
import unittest

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core.yamtrack_tracker_import import parse_yamtrack_tracker_csv


def make_csv(rows: list[dict]) -> bytes:
    fields = ["media_id", "source", "media_type", "title", "image", "score", "status", "notes", "progress", "start_date", "end_date"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode()


class YamtrackTrackerParserTests(unittest.TestCase):
    def test_keeps_non_tmdb_media_with_tracking_state(self):
        records = parse_yamtrack_tracker_csv(make_csv([{
            "media_id": "270", "source": "mal", "media_type": "anime", "title": "Hellsing",
            "status": "In progress", "progress": "5", "score": "82.5", "notes": "Great",
            "image": "https://example.test/cover.jpg", "start_date": "2026-01-02",
        }]))
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual((record.source, record.external_id, record.media_type), ("mal", "270", "anime"))
        self.assertEqual((record.status, record.progress, record.score, record.notes), ("current", 5, 82.5, "Great"))
        self.assertIsNotNone(record.started_at)

    def test_tv_is_normalized_to_series_and_episodes_are_excluded(self):
        records = parse_yamtrack_tracker_csv(make_csv([
            {"media_id": "1", "source": "tmdb", "media_type": "tv", "title": "A Show", "status": "Planning"},
            {"media_id": "1", "source": "tmdb", "media_type": "episode", "title": "A Show", "status": "Completed"},
        ]))
        self.assertEqual([(record.media_type, record.status) for record in records], [("series", "planning")])

    def test_unknown_type_and_invalid_score_are_safely_ignored_or_normalized(self):
        records = parse_yamtrack_tracker_csv(make_csv([
            {"media_id": "1", "source": "manual", "media_type": "podcast", "title": "Skip"},
            {"media_id": "2", "source": "igdb", "media_type": "game", "title": "Keep", "score": "900"},
        ]))
        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0].score)

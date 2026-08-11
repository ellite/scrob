import os
import unittest

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://test:test@localhost/test",
)

from sqlalchemy.exc import IntegrityError

from core.enrichment import create_media_safely, apply_media_change_safely
from models.base import MediaType


class _FakeScalars:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self):
        return _FakeScalars(self._rows)


class _FakeNestedTxn:
    def __init__(self, session: "_FakeSession") -> None:
        self.session = session

    async def __aenter__(self):
        self.session.events.append("begin_nested")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False  # let exceptions propagate, like a real SAVEPOINT rollback


class _FakeSession:
    def __init__(self, flush_raises: Exception | None = None, existing_rows: list | None = None) -> None:
        self.added: list = []
        self.flush_raises = flush_raises
        self.existing_rows = existing_rows or []
        self.flush_calls = 0
        # Ordered event log ("begin_nested", "add") - a real Postgres session
        # only recovers cleanly from a failed flush inside begin_nested() if
        # add() happened *after* entering the savepoint (verified against a
        # live database, see this module's docstring notes) - add()-before-
        # begin_nested() leaves the session's flush-error state stuck even
        # after the SQL-level SAVEPOINT rolls back, which a mock alone can't
        # catch, so the ordering itself is asserted here as a regression guard.
        self.events: list[str] = []

    def add(self, obj) -> None:
        self.events.append("add")
        if getattr(obj, "id", None) is None:
            obj.id = 1000 + len(self.added)
        self.added.append(obj)

    def begin_nested(self):
        return _FakeNestedTxn(self)

    async def flush(self) -> None:
        self.flush_calls += 1
        if self.flush_raises is not None:
            raise self.flush_raises

    async def execute(self, statement):
        return _FakeResult(self.existing_rows)


class CreateMediaSafelyTests(unittest.IsolatedAsyncioTestCase):
    async def test_creates_media_when_no_conflict(self) -> None:
        session = _FakeSession()
        media, created = await create_media_safely(
            session, 555, MediaType.movie, title="A Movie"
        )
        self.assertTrue(created)
        self.assertEqual(media.tmdb_id, 555)
        self.assertEqual(media.title, "A Movie")
        self.assertIn(media, session.added)
        self.assertEqual(session.flush_calls, 1)
        # add() must happen *after* the savepoint is entered - see _FakeSession.
        self.assertEqual(session.events, ["begin_nested", "add"])

    async def test_null_tmdb_id_never_hits_conflict_path(self) -> None:
        # No unique index applies when tmdb_id is null - nothing to race on,
        # so this must not attempt a nested-transaction/re-fetch dance at all.
        session = _FakeSession()
        media, created = await create_media_safely(
            session, None, MediaType.episode, title="Unresolved Episode"
        )
        self.assertTrue(created)
        self.assertIsNone(media.tmdb_id)
        self.assertIn(media, session.added)
        self.assertEqual(session.flush_calls, 1)

    async def test_integrity_error_returns_the_race_winner(self) -> None:
        # Regression test for #157's root cause: a concurrent insert of the
        # same (tmdb_id, media_type) must not crash - it should return
        # whichever row actually won the race instead.
        class _Existing:
            id = 42
            season_number = 3
            episode_number = 7

        existing = _Existing()
        session = _FakeSession(
            flush_raises=IntegrityError("stmt", {}, Exception("duplicate key")),
            existing_rows=[existing],
        )
        media, created = await create_media_safely(
            session, 999, MediaType.episode,
            title="Episode 7", season_number=3, episode_number=7,
        )
        self.assertIs(media, existing)
        self.assertFalse(created)
        # add() must happen *after* the savepoint is entered - see _FakeSession.
        self.assertEqual(session.events, ["begin_nested", "add"])

    async def test_integrity_error_with_no_existing_row_reraises(self) -> None:
        session = _FakeSession(
            flush_raises=IntegrityError("stmt", {}, Exception("duplicate key")),
            existing_rows=[],
        )
        with self.assertRaises(IntegrityError):
            await create_media_safely(session, 999, MediaType.episode, title="Episode 7")

    async def test_season_episode_mismatch_logs_but_still_returns_existing(self) -> None:
        # Regression test: sharing a tmdb_id doesn't always mean the same
        # episode (TMDB can re-number episodes between two resolutions) - this
        # must not silently look identical to a normal race, but it also can't
        # insert a second row for the same tmdb_id, so it still returns the
        # existing row rather than raising.
        class _Existing:
            id = 42
            season_number = 5
            episode_number = 1

        existing = _Existing()
        session = _FakeSession(
            flush_raises=IntegrityError("stmt", {}, Exception("duplicate key")),
            existing_rows=[existing],
        )
        with self.assertLogs("core.enrichment", level="WARNING") as cm:
            media, created = await create_media_safely(
                session, 999, MediaType.episode,
                title="All Clear", season_number=6, episode_number=3,
            )
        self.assertIs(media, existing)
        self.assertTrue(any("disagrees" in msg for msg in cm.output))


class _FakeMedia:
    def __init__(self, id, media_type, tmdb_id) -> None:
        self.id = id
        self.media_type = media_type
        self.tmdb_id = tmdb_id


class ApplyMediaChangeSafelyTests(unittest.IsolatedAsyncioTestCase):
    async def test_unpersisted_media_runs_mutate_directly(self) -> None:
        # media.id is None - this row is still being created in the same flush
        # cycle, whose own savepoint already covers it - no extra ceremony.
        session = _FakeSession()
        media = _FakeMedia(id=None, media_type=MediaType.episode, tmdb_id=None)

        def mutate():
            media.tmdb_id = 42

        result = await apply_media_change_safely(session, media, mutate)
        self.assertIs(result, media)
        self.assertEqual(media.tmdb_id, 42)
        self.assertEqual(session.flush_calls, 0)

    async def test_movie_conflict_returns_existing_row_instead_of_crashing(self) -> None:
        # uq_media_tmdb_type applies to every media_type, not just episodes -
        # e.g. "match unmatched movie" can reassign a stub movie's tmdb_id to
        # one another row already claims (two duplicate unmatched stubs of
        # the same title getting matched in the same request).
        existing = _FakeMedia(id=99, media_type=MediaType.movie, tmdb_id=2)
        session = _FakeSession(
            flush_raises=IntegrityError("stmt", {}, Exception("duplicate key")),
            existing_rows=[existing],
        )
        media = _FakeMedia(id=7, media_type=MediaType.movie, tmdb_id=1)

        def mutate():
            media.tmdb_id = 2

        result = await apply_media_change_safely(session, media, mutate)
        self.assertIs(result, existing)

    async def test_supports_sync_and_async_mutate(self) -> None:
        session = _FakeSession()
        media = _FakeMedia(id=7, media_type=MediaType.episode, tmdb_id=None)

        async def async_mutate():
            media.tmdb_id = 42

        result = await apply_media_change_safely(session, media, async_mutate)
        self.assertIs(result, media)
        self.assertEqual(media.tmdb_id, 42)
        self.assertEqual(session.flush_calls, 1)

    async def test_conflict_returns_existing_row_instead_of_crashing(self) -> None:
        # Regression test: a stub episode (tmdb_id=None) finally resolving to a
        # tmdb_id another row already has (TMDB re-numbering, a healing pass,
        # a manual re-match, etc.) must not crash the caller.
        existing = _FakeMedia(id=99, media_type=MediaType.episode, tmdb_id=42)
        session = _FakeSession(
            flush_raises=IntegrityError("stmt", {}, Exception("duplicate key")),
            existing_rows=[existing],
        )
        media = _FakeMedia(id=7, media_type=MediaType.episode, tmdb_id=None)

        def mutate():
            media.tmdb_id = 42

        result = await apply_media_change_safely(session, media, mutate)
        self.assertIs(result, existing)

    async def test_conflict_unrelated_to_the_change_reraises(self) -> None:
        # If media.tmdb_id ended up unchanged, the flush failure came from
        # something else entirely - must not be silently swallowed as if it
        # were about this change.
        session = _FakeSession(
            flush_raises=IntegrityError("stmt", {}, Exception("duplicate key")),
            existing_rows=[_FakeMedia(id=99, media_type=MediaType.episode, tmdb_id=42)],
        )
        media = _FakeMedia(id=7, media_type=MediaType.episode, tmdb_id=42)

        def mutate():
            pass  # tmdb_id stays 42 - unchanged

        with self.assertRaises(IntegrityError):
            await apply_media_change_safely(session, media, mutate)

    async def test_conflict_with_no_matching_existing_row_reraises(self) -> None:
        session = _FakeSession(
            flush_raises=IntegrityError("stmt", {}, Exception("duplicate key")),
            existing_rows=[],
        )
        media = _FakeMedia(id=7, media_type=MediaType.episode, tmdb_id=None)

        def mutate():
            media.tmdb_id = 42

        with self.assertRaises(IntegrityError):
            await apply_media_change_safely(session, media, mutate)


if __name__ == "__main__":
    unittest.main()

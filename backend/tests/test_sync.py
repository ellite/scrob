import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from fastapi import HTTPException

from models.connections import MediaServerConnection
from routers import sync


class _Result:
    def __init__(self, item):
        self.item = item

    def scalar_one_or_none(self):
        return self.item


class _FakeSession:
    def __init__(self, conn):
        self.conn = conn
        self.added = []
        self.commit = AsyncMock()
        self.refresh = AsyncMock()

    async def execute(self, stmt):
        return _Result(self.conn)

    def add(self, obj):
        self.added.append(obj)


class PushUpstreamValidationTests(unittest.IsolatedAsyncioTestCase):
    """Regression test: a connection with only the Plex watchlist push flag
    enabled (no collection/watched/ratings/playback) was rejected outright -
    this validation predated plex_push_watchlist and never learned about it,
    so "Push" always 400'd for anyone using watchlist-only push."""

    async def test_watchlist_only_flag_is_accepted(self):
        conn = MediaServerConnection(
            id=1, user_id=1, type="plex",
            push_collection=False, push_watched=False, push_ratings=False, push_playback=False,
            plex_push_watchlist=True,
        )
        db = _FakeSession(conn)
        background_tasks = SimpleNamespace(add_task=lambda *a, **k: None)

        response = await sync.push_upstream(
            connection_id=1, background_tasks=background_tasks, db=db, current_user=SimpleNamespace(id=1),
        )
        self.assertEqual(response["status"], "started")

    async def test_no_flags_at_all_is_rejected(self):
        conn = MediaServerConnection(
            id=1, user_id=1, type="plex",
            push_collection=False, push_watched=False, push_ratings=False, push_playback=False,
            plex_push_watchlist=False,
        )
        db = _FakeSession(conn)
        background_tasks = SimpleNamespace(add_task=lambda *a, **k: None)

        with self.assertRaises(HTTPException) as ctx:
            await sync.push_upstream(
                connection_id=1, background_tasks=background_tasks, db=db, current_user=SimpleNamespace(id=1),
            )
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_other_flags_still_accepted_without_watchlist(self):
        conn = MediaServerConnection(
            id=1, user_id=1, type="jellyfin",
            push_collection=True, push_watched=False, push_ratings=False, push_playback=False,
            plex_push_watchlist=False,
        )
        db = _FakeSession(conn)
        background_tasks = SimpleNamespace(add_task=lambda *a, **k: None)

        response = await sync.push_upstream(
            connection_id=1, background_tasks=background_tasks, db=db, current_user=SimpleNamespace(id=1),
        )
        self.assertEqual(response["status"], "started")


class PlexSyncNeedsLibraryScanTests(unittest.TestCase):
    """Regression test: a Plex pull with only "Watchlist" selected still
    re-scanned every movie/show/episode in the user's entire library before
    ever reaching the watchlist step, because the scan had no gate of its
    own - only what happened *inside* it was conditional."""

    def test_watchlist_only_does_not_need_a_scan(self):
        conn = SimpleNamespace(sync_collection=False, sync_watched=False, sync_ratings=False, plex_sync_watchlist=True)
        self.assertFalse(sync.plex_sync_needs_library_scan(conn))

    def test_any_single_category_needs_a_scan(self):
        base = dict(sync_collection=False, sync_watched=False, sync_ratings=False)
        for field in ("sync_collection", "sync_watched", "sync_ratings"):
            conn = SimpleNamespace(**{**base, field: True})
            self.assertTrue(sync.plex_sync_needs_library_scan(conn), f"{field} alone should trigger a scan")

    def test_nothing_selected_at_all_does_not_need_a_scan(self):
        conn = SimpleNamespace(sync_collection=False, sync_watched=False, sync_ratings=False, plex_sync_watchlist=False)
        self.assertFalse(sync.plex_sync_needs_library_scan(conn))


class _PlexHistoryFakeDB:
    """Minimal async-session double for _backfill_plex_watch_history: just
    enough to serve db.get(MediaServerConnection), the existing-WatchEvent
    select, db.add()/flush()/commit(), and the reconciling UPDATE statement."""

    def __init__(self, conn, existing_watch_rows):
        self.conn = conn
        self.existing_watch_rows = existing_watch_rows
        self.added: list = []
        self.updates: list[dict] = []
        self.commit = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def get(self, model, obj_id):
        return self.conn

    async def execute(self, statement):
        sql = str(statement)
        if sql.startswith("UPDATE watch_events"):
            self.updates.append(dict(statement.compile().params))
            return None
        return list(self.existing_watch_rows)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


class BackfillPlexWatchHistoryDedupTests(unittest.IsolatedAsyncioTestCase):
    """Regression tests for GitHub #135: the real-time Plex webhook already
    records a WatchEvent (marked provisional, since its watched_at is this
    server's receipt time, not Plex's) for a play that the periodic
    _backfill_plex_watch_history() pass will see again via Plex's own history
    endpoint. That must reconcile the provisional row rather than insert a
    duplicate — and, deliberately, only within a tight webhook-latency window
    and only against provisional rows, not a broad time-window match against
    any existing play regardless of source (the original, too-broad fix)."""

    async def _run(self, existing_watch_rows, history_entries):
        # existing_watch_rows: list of (id, media_id, watched_at, provisional)
        conn = SimpleNamespace(plex_history_cursor_at=None)
        db = _PlexHistoryFakeDB(conn, existing_watch_rows)

        with (
            patch.object(sync, "async_sessionmaker", return_value=lambda *a, **k: db),
            patch.object(sync.plex, "get_history", AsyncMock(return_value=history_entries)),
            patch.object(sync, "record_rewatch_progress", AsyncMock()),
        ):
            new_events, reconciled, unmatched = await sync._backfill_plex_watch_history(
                user_id=1,
                connection_id=1,
                p_url="http://plex",
                p_token="token",
                server_username=None,
                ratingkey_to_media={"555": 10},
            )
        return new_events, reconciled, unmatched, db

    @staticmethod
    def _history_entry(viewed_at: datetime, rating_key: str = "555") -> dict:
        return {
            "type": "movie", "ratingKey": rating_key,
            "viewedAt": int(viewed_at.replace(tzinfo=timezone.utc).timestamp()),
        }

    async def test_provisional_webhook_event_within_window_is_reconciled_not_duplicated(self):
        # The webhook already wrote a provisional event from its own receipt
        # time. Plex's authoritative viewedAt for the same play is a few
        # minutes off — must correct that row in place, not add a new one.
        webhook_watched_at = datetime(2026, 8, 1, 20, 0, 0)
        authoritative_watched_at = webhook_watched_at + timedelta(minutes=4)

        new_events, reconciled, unmatched, db = await self._run(
            existing_watch_rows=[(101, 10, webhook_watched_at, True)],
            history_entries=[self._history_entry(authoritative_watched_at)],
        )

        self.assertEqual(new_events, 0)
        self.assertEqual(reconciled, 1)
        self.assertEqual(db.added, [])
        self.assertEqual(len(db.updates), 1)
        self.assertEqual(db.updates[0]["watched_at"], authoritative_watched_at)
        self.assertEqual(db.updates[0]["provisional"], False)
        self.assertEqual(db.updates[0]["id_1"], 101)

    async def test_provisional_event_outside_reconcile_window_is_left_alone(self):
        # A provisional row too far away in time isn't a plausible webhook/
        # backfill pairing for the same play — must not be touched, and the
        # authoritative play still gets its own row.
        webhook_watched_at = datetime(2026, 8, 1, 20, 0, 0)
        authoritative_watched_at = webhook_watched_at + timedelta(hours=3)

        new_events, reconciled, unmatched, db = await self._run(
            existing_watch_rows=[(101, 10, webhook_watched_at, True)],
            history_entries=[self._history_entry(authoritative_watched_at)],
        )

        self.assertEqual(new_events, 1)
        self.assertEqual(reconciled, 0)
        self.assertEqual(db.updates, [])
        self.assertEqual(len(db.added), 1)
        self.assertEqual(db.added[0].watched_at, authoritative_watched_at)

    async def test_confirmed_event_requires_exact_match_not_a_window(self):
        # A non-provisional (already-confirmed) row — e.g. from a prior run of
        # this same backfill, or an unrelated Trakt/Simkl import — must only
        # be treated as the same play on an exact watched_at match. A close
        # but non-identical confirmed row must NOT suppress recording the
        # authoritative play, unlike the old blanket time-window approach.
        confirmed_watched_at = datetime(2026, 8, 1, 20, 0, 0)
        authoritative_watched_at = confirmed_watched_at + timedelta(minutes=2)

        new_events, reconciled, unmatched, db = await self._run(
            existing_watch_rows=[(101, 10, confirmed_watched_at, False)],
            history_entries=[self._history_entry(authoritative_watched_at)],
        )

        self.assertEqual(new_events, 1)
        self.assertEqual(reconciled, 0)
        self.assertEqual(db.updates, [])
        self.assertEqual(len(db.added), 1)

    async def test_confirmed_event_exact_match_is_a_no_op(self):
        # Re-running the backfill (e.g. cursor overlap) must not re-add a play
        # it already recorded itself — deterministic, no tolerance needed.
        watched_at = datetime(2026, 8, 1, 20, 0, 0)

        new_events, reconciled, unmatched, db = await self._run(
            existing_watch_rows=[(101, 10, watched_at, False)],
            history_entries=[self._history_entry(watched_at)],
        )

        self.assertEqual(new_events, 0)
        self.assertEqual(reconciled, 0)
        self.assertEqual(db.updates, [])
        self.assertEqual(db.added, [])

    async def test_distant_replay_is_recorded_as_a_new_play(self):
        # A genuinely distinct rewatch, well outside the dedup window, must
        # still be imported as its own WatchEvent (the original point of #126).
        already_recorded_at = datetime(2026, 1, 1, 20, 0, 0)
        history_viewed_at = datetime(2026, 8, 1, 20, 0, 0)

        new_events, reconciled, unmatched, db = await self._run(
            existing_watch_rows=[(101, 10, already_recorded_at, False)],
            history_entries=[self._history_entry(history_viewed_at)],
        )

        self.assertEqual(new_events, 1)
        self.assertEqual(reconciled, 0)
        self.assertEqual(len(db.added), 1)
        self.assertEqual(db.added[0].watched_at, history_viewed_at)

    async def test_unmapped_ratingkey_counts_as_unmatched(self):
        new_events, reconciled, unmatched, db = await self._run(
            existing_watch_rows=[],
            history_entries=[{"type": "movie", "ratingKey": "999", "viewedAt": 1754074800}],
        )

        self.assertEqual(new_events, 0)
        self.assertEqual(reconciled, 0)
        self.assertEqual(unmatched, 1)
        self.assertEqual(db.added, [])


class _StaleCollectionFakeDB:
    """Minimal async-session double for _remove_stale_collection_files: serves
    the initial CollectionFile+Collection.media_id join, then a func.count
    remaining-files query per candidate removal (in the same order the
    function issues them)."""

    def __init__(self, rows, remaining_counts):
        self.rows = rows
        self.remaining_counts = list(remaining_counts)
        self.deleted_files: list = []
        self.deleted_collections: list = []
        self._call = 0

    async def execute(self, stmt):
        self._call += 1
        if self._call == 1:
            return SimpleNamespace(all=lambda: self.rows)
        count = self.remaining_counts.pop(0)
        return SimpleNamespace(scalar_one=lambda: count)

    async def delete(self, obj):
        if hasattr(obj, "source_id"):
            self.deleted_files.append(obj)
        else:
            self.deleted_collections.append(obj)

    async def flush(self):
        pass

    async def get(self, model, obj_id):
        return SimpleNamespace(id=obj_id)


class RemoveStaleCollectionFilesTests(unittest.IsolatedAsyncioTestCase):
    """Regression tests for #139: a title deleted from Plex/Jellyfin/Emby
    never left the user's Scrob collection, since a full sync only ever
    added/updated CollectionFiles for items it still saw - it never noticed
    one had dropped out."""

    async def test_item_missing_from_scan_is_removed(self):
        file = SimpleNamespace(id=1, collection_id=10, source_id="rk-1")
        db = _StaleCollectionFakeDB(rows=[(file, 100)], remaining_counts=[0])

        removed = await sync._remove_stale_collection_files(
            db, user_id=1, source=sync.CollectionSource.plex, connection_id=5,
            seen_source_ids=set(),  # nothing seen this run - rk-1 is gone
        )

        self.assertEqual(removed, {100})
        self.assertEqual(db.deleted_files, [file])
        self.assertEqual(len(db.deleted_collections), 1)

    async def test_item_still_in_scan_is_kept(self):
        file = SimpleNamespace(id=1, collection_id=10, source_id="rk-1")
        db = _StaleCollectionFakeDB(rows=[(file, 100)], remaining_counts=[])

        removed = await sync._remove_stale_collection_files(
            db, user_id=1, source=sync.CollectionSource.plex, connection_id=5,
            seen_source_ids={"rk-1"},
        )

        self.assertEqual(removed, set())
        self.assertEqual(db.deleted_files, [])
        self.assertEqual(db.deleted_collections, [])

    async def test_multi_source_item_keeps_collection_alive(self):
        # Same media collected from both Plex and Jellyfin - losing it from
        # Plex should drop the Plex CollectionFile but not the Collection
        # itself, since Jellyfin still backs it.
        file = SimpleNamespace(id=1, collection_id=10, source_id="rk-1")
        db = _StaleCollectionFakeDB(rows=[(file, 100)], remaining_counts=[1])

        removed = await sync._remove_stale_collection_files(
            db, user_id=1, source=sync.CollectionSource.plex, connection_id=5,
            seen_source_ids=set(),
        )

        self.assertEqual(removed, set())
        self.assertEqual(db.deleted_files, [file])
        self.assertEqual(db.deleted_collections, [])

    async def test_refuses_to_prune_when_majority_of_collection_vanished(self):
        # 12 existing files, only 2 still seen this run. A real deletion this
        # large is vanishingly rare - this shape is what a bad/partial scan
        # (transient API hiccup, stale library-selection filter) looks like,
        # and the circuit breaker must refuse rather than guess (see #139
        # follow-up: don't repeat the #135 duplicate-watch-events blunder,
        # this time in a destructive direction).
        rows = [
            (SimpleNamespace(id=i, collection_id=i, source_id=f"rk-{i}"), 100 + i)
            for i in range(12)
        ]
        seen = {"rk-0", "rk-1"}
        db = _StaleCollectionFakeDB(rows=rows, remaining_counts=[])

        removed = await sync._remove_stale_collection_files(
            db, user_id=1, source=sync.CollectionSource.plex, connection_id=5,
            seen_source_ids=seen,
        )

        self.assertEqual(removed, set())
        self.assertEqual(db.deleted_files, [])
        self.assertEqual(db.deleted_collections, [])

    async def test_prunes_normally_when_only_a_minority_is_gone(self):
        # 12 existing files, 10 still seen - losing 2 real items is exactly
        # what the fix is for, and shouldn't trip the circuit breaker.
        rows = [
            (SimpleNamespace(id=i, collection_id=i, source_id=f"rk-{i}"), 100 + i)
            for i in range(12)
        ]
        seen = {f"rk-{i}" for i in range(2, 12)}
        db = _StaleCollectionFakeDB(rows=rows, remaining_counts=[0, 0])

        removed = await sync._remove_stale_collection_files(
            db, user_id=1, source=sync.CollectionSource.plex, connection_id=5,
            seen_source_ids=seen,
        )

        self.assertEqual(removed, {100, 101})
        self.assertEqual(len(db.deleted_files), 2)

    async def test_small_collections_are_exempt_from_the_circuit_breaker(self):
        # Below the minimum-existing threshold, even losing everything is
        # allowed through - a user with 3 items who deletes all 3 shouldn't
        # be silently ignored just because the sample is small.
        rows = [
            (SimpleNamespace(id=i, collection_id=i, source_id=f"rk-{i}"), 100 + i)
            for i in range(3)
        ]
        db = _StaleCollectionFakeDB(rows=rows, remaining_counts=[0, 0, 0])

        removed = await sync._remove_stale_collection_files(
            db, user_id=1, source=sync.CollectionSource.plex, connection_id=5,
            seen_source_ids=set(),
        )

        self.assertEqual(removed, {100, 101, 102})


class ExpandMultiEpisodeItemsTests(unittest.TestCase):
    """Regression tests for #138: Jellyfin/Emby can mux several episodes into
    one video file (exposed via IndexNumber..IndexNumberEnd) - a combined item
    like that was previously synced as just its first episode."""

    def test_combined_episode_file_is_expanded_per_episode(self):
        item = {"Id": "file-1", "IndexNumber": 1, "IndexNumberEnd": 2, "Name": "Ep 1-2"}

        expanded = sync._expand_multi_episode_items(
            [item], sync.MediaType.episode, sync.CollectionSource.jellyfin,
        )

        self.assertEqual(len(expanded), 2)
        self.assertEqual([e["IndexNumber"] for e in expanded], [1, 2])
        self.assertTrue(all(e["Id"] == "file-1" for e in expanded))
        # Original item must be untouched - other code still iterates the raw list.
        self.assertEqual(item["IndexNumber"], 1)

    def test_three_episode_span_expands_to_three(self):
        item = {"Id": "file-2", "IndexNumber": 5, "IndexNumberEnd": 7}

        expanded = sync._expand_multi_episode_items(
            [item], sync.MediaType.episode, sync.CollectionSource.emby,
        )

        self.assertEqual([e["IndexNumber"] for e in expanded], [5, 6, 7])

    def test_single_episode_item_is_returned_as_is(self):
        item = {"Id": "file-3", "IndexNumber": 4, "IndexNumberEnd": None}

        expanded = sync._expand_multi_episode_items(
            [item], sync.MediaType.episode, sync.CollectionSource.jellyfin,
        )

        self.assertEqual(expanded, [item])

    def test_equal_start_and_end_is_not_expanded(self):
        item = {"Id": "file-4", "IndexNumber": 4, "IndexNumberEnd": 4}

        expanded = sync._expand_multi_episode_items(
            [item], sync.MediaType.episode, sync.CollectionSource.jellyfin,
        )

        self.assertEqual(expanded, [item])

    def test_movies_are_never_expanded(self):
        # Movies don't carry IndexNumber/IndexNumberEnd, but the media_type
        # gate alone must be enough to skip this entirely.
        item = {"Id": "movie-1", "IndexNumber": 1, "IndexNumberEnd": 2}

        expanded = sync._expand_multi_episode_items(
            [item], sync.MediaType.movie, sync.CollectionSource.jellyfin,
        )

        self.assertEqual(expanded, [item])

    def test_plex_source_is_never_expanded(self):
        # Scoped to Jellyfin/Emby only - Plex doesn't expose this field at all.
        item = {"ratingKey": "1", "index": 1, "IndexNumberEnd": 2}

        expanded = sync._expand_multi_episode_items(
            [item], sync.MediaType.episode, sync.CollectionSource.plex,
        )

        self.assertEqual(expanded, [item])


from sqlalchemy.sql import Select as _SASelect
from sqlalchemy.dialects import postgresql as _pg


class _ReconcileFakeDB:
    """Serves only what _reconcile_plex_watchlist itself executes: the
    baseline re-read (SELECT) and the baseline write (UPDATE). Everything
    heavier sits behind the patched load/apply helpers."""

    def __init__(self, baseline):
        self._baseline = baseline
        self.baseline_writes = []
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def execute(self, stmt):
        if isinstance(stmt, _SASelect):
            return _Result(self._baseline)
        params = stmt.compile(dialect=_pg.dialect()).params
        self.baseline_writes.append(params.get("plex_watchlist_synced_keys"))
        return SimpleNamespace()


def _wl_conn(pull=True, push=True):
    return MediaServerConnection(
        id=1, user_id=1, type="plex", token="tok",
        plex_sync_watchlist=pull, plex_push_watchlist=push,
    )


def _remote_item(kind, tmdb_id, rating_key="rk", title="Title"):
    return {"type": kind, "Guid": [{"id": f"tmdb://{tmdb_id}"}], "ratingKey": rating_key, "title": title}


class PlexWatchlistReconcileTests(unittest.IsolatedAsyncioTestCase):
    """The routine wiring around core.watchlist_reconcile: what plan reaches
    the apply helpers, and what baseline gets persisted."""

    async def _run(self, db, conn, remote_items, local_state, applied_local, remote_failures=(set(), set())):
        apply_local = AsyncMock(return_value=applied_local)
        apply_remote = AsyncMock(return_value=remote_failures)
        with patch.object(sync.plex, "get_watchlist", AsyncMock(return_value=remote_items)), \
             patch.object(sync, "_load_local_watchlist_state", AsyncMock(return_value=local_state)), \
             patch.object(sync, "_apply_local_watchlist_changes", apply_local), \
             patch.object(sync, "_apply_remote_watchlist_changes", apply_remote):
            await sync._reconcile_plex_watchlist(db, 1, conn, "tmdb-key")
        local_plan = apply_local.call_args.args[4] if apply_local.call_args else None
        remote_plan = apply_remote.call_args.args[1] if apply_remote.call_args else None
        return local_plan, remote_plan, apply_local, apply_remote

    async def test_remote_removal_is_applied_locally_not_resurrected(self):
        """Regression: Plex auto-removed a watched item; the old additive push
        re-added it. Now the baseline turns it into a local removal."""
        db = _ReconcileFakeDB(["movie:1"])
        local_plan, remote_plan, _, _ = await self._run(
            db, _wl_conn(), [], (SimpleNamespace(id=5), {"movie:1": (11, "Inception")}), applied_local=set(),
        )
        self.assertEqual(local_plan.remove_local, {"movie:1"})
        self.assertEqual(remote_plan.push_add, frozenset())
        self.assertEqual(db.baseline_writes, [[]])
        db.commit.assert_awaited()

    async def test_deleted_managed_list_resets_baseline_instead_of_wiping_plex(self):
        """Regression: with the list deleted in the UI, a stale baseline must
        not turn the entire real Plex watchlist into push_remove calls."""
        db = _ReconcileFakeDB(["movie:1", "show:2"])
        remote = [_remote_item("movie", 1), _remote_item("show", 2)]
        local_plan, remote_plan, _, _ = await self._run(
            db, _wl_conn(), remote, (None, {}), applied_local={"movie:1", "show:2"},
        )
        self.assertEqual(remote_plan.push_remove, frozenset())
        self.assertEqual(local_plan.add_local, {"movie:1", "show:2"})  # bootstrap re-import
        self.assertEqual(db.baseline_writes, [["movie:1", "show:2"]])

    async def test_bootstrap_first_contact_is_purely_additive(self):
        db = _ReconcileFakeDB(None)
        local_plan, remote_plan, _, _ = await self._run(
            db, _wl_conn(), [_remote_item("show", 2)],
            (SimpleNamespace(id=5), {"movie:1": (11, "Inception")}),
            applied_local={"movie:1", "show:2"},
        )
        self.assertEqual(remote_plan.push_add, {"movie:1"})
        self.assertEqual(local_plan.add_local, {"show:2"})
        self.assertEqual(local_plan.remove_local, frozenset())
        self.assertEqual(db.baseline_writes, [["movie:1", "show:2"]])

    async def test_failed_remote_add_stays_out_of_the_baseline(self):
        db = _ReconcileFakeDB(["movie:1"])
        _, remote_plan, _, _ = await self._run(
            db, _wl_conn(), [_remote_item("movie", 1)],
            (SimpleNamespace(id=5), {"movie:1": (11, "A"), "movie:9": (12, "B")}),
            applied_local={"movie:1", "movie:9"},
            remote_failures=({"movie:9"}, set()),
        )
        self.assertEqual(remote_plan.push_add, {"movie:9"})
        self.assertEqual(db.baseline_writes, [["movie:1"]])  # movie:9 retries next run

    async def test_suppressed_run_changes_nothing(self):
        baseline = [f"movie:{i}" for i in range(12)]
        db = _ReconcileFakeDB(baseline)
        local = {key: (i, "T") for i, key in enumerate(baseline)}
        _, _, apply_local, apply_remote = await self._run(
            db, _wl_conn(), [], (SimpleNamespace(id=5), local), applied_local=set(),
        )
        apply_local.assert_not_called()
        apply_remote.assert_not_called()
        self.assertEqual(db.baseline_writes, [])
        db.commit.assert_not_awaited()

    async def test_fetch_failure_is_swallowed_and_rolled_back(self):
        db = _ReconcileFakeDB(["movie:1"])
        with patch.object(sync.plex, "get_watchlist", AsyncMock(side_effect=RuntimeError("plex down"))):
            await sync._reconcile_plex_watchlist(db, 1, _wl_conn(), "tmdb-key")
        db.rollback.assert_awaited()
        self.assertEqual(db.baseline_writes, [])

    async def test_disabled_flags_do_nothing(self):
        db = _ReconcileFakeDB(["movie:1"])
        fetch = AsyncMock()
        with patch.object(sync.plex, "get_watchlist", fetch):
            await sync._reconcile_plex_watchlist(db, 1, _wl_conn(pull=False, push=False), "tmdb-key")
        fetch.assert_not_called()


class PushEnabledPropertyTests(unittest.TestCase):
    """The scheduler's auto-push gate and the manual push endpoint share this
    property, so a watchlist-only connection can't be forgotten by one of
    them again."""

    def test_watchlist_only_connection_is_push_enabled(self):
        conn = MediaServerConnection(
            push_collection=False, push_watched=False, push_ratings=False, push_playback=False,
            plex_push_watchlist=True,
        )
        self.assertTrue(conn.push_enabled)

    def test_no_push_flags_at_all(self):
        conn = MediaServerConnection(
            push_collection=False, push_watched=False, push_ratings=False, push_playback=False,
            plex_push_watchlist=False,
        )
        self.assertFalse(conn.push_enabled)

    def test_classic_push_flags_still_count(self):
        conn = MediaServerConnection(
            push_collection=False, push_watched=True, push_ratings=False, push_playback=False,
            plex_push_watchlist=False,
        )
        self.assertTrue(conn.push_enabled)


class ConnectionUpdateBaselineResetTests(unittest.IsolatedAsyncioTestCase):
    """Flipping a watchlist direction off→on must restart from a clean
    bootstrap - a baseline recorded under the old settings must not drive
    deletions."""

    async def _patch_connection(self, conn, **body_fields):
        import schemas
        from routers import auth as auth_router

        db = _FakeSession(conn)
        body = schemas.MediaServerConnectionUpdate(**body_fields)
        return await auth_router.update_connection(
            connection_id=1, body=body, db=db, current_user=SimpleNamespace(id=1),
        )

    def _conn(self, sync_on=False, push_on=False):
        return MediaServerConnection(
            id=1, user_id=1, type="plex", name="Plex", url="http://p", token="t",
            plex_sync_watchlist=sync_on, plex_push_watchlist=push_on,
            plex_watchlist_synced_keys=["movie:1"],
        )

    async def test_enabling_pull_resets_the_baseline(self):
        conn = self._conn()
        await self._patch_connection(conn, plex_sync_watchlist=True)
        self.assertIsNone(conn.plex_watchlist_synced_keys)

    async def test_enabling_push_resets_the_baseline(self):
        conn = self._conn()
        await self._patch_connection(conn, plex_push_watchlist=True)
        self.assertIsNone(conn.plex_watchlist_synced_keys)

    async def test_updating_while_already_enabled_keeps_the_baseline(self):
        conn = self._conn(sync_on=True)
        await self._patch_connection(conn, plex_sync_watchlist=True)
        self.assertEqual(conn.plex_watchlist_synced_keys, ["movie:1"])

    async def test_disabling_keeps_the_baseline(self):
        conn = self._conn(sync_on=True)
        await self._patch_connection(conn, plex_sync_watchlist=False)
        self.assertEqual(conn.plex_watchlist_synced_keys, ["movie:1"])


class ProviderAddedAtTests(unittest.TestCase):
    """The library add-date each media server reports, normalised to naive UTC
    like every other timestamp Scrob stores."""

    def test_plex_epoch_seconds(self):
        got = sync.provider_added_at({"addedAt": 1754179200}, sync.CollectionSource.plex)
        self.assertEqual(got, datetime(2025, 8, 3, 0, 0, 0))

    def test_plex_accepts_the_value_as_a_string(self):
        got = sync.provider_added_at({"addedAt": "1754179200"}, sync.CollectionSource.plex)
        self.assertEqual(got, datetime(2025, 8, 3, 0, 0, 0))

    def test_jellyfin_iso_with_dotnet_fractional_seconds(self):
        got = sync.provider_added_at(
            {"DateCreated": "2026-08-01T09:30:00.0000000Z"}, sync.CollectionSource.jellyfin
        )
        self.assertEqual(got, datetime(2026, 8, 1, 9, 30, 0))
        self.assertIsNone(got.tzinfo)

    def test_emby_iso_is_converted_to_utc(self):
        got = sync.provider_added_at(
            {"DateCreated": "2026-08-01T11:30:00+02:00"}, sync.CollectionSource.emby
        )
        self.assertEqual(got, datetime(2026, 8, 1, 9, 30, 0))

    def test_sources_without_a_library_date_return_none(self):
        # Nuvio and Stremio items are synthesized with a fixed key set, so they
        # must never be read as if they were a media browser payload.
        for source in (sync.CollectionSource.nuvio, sync.CollectionSource.stremio):
            self.assertIsNone(
                sync.provider_added_at({"DateCreated": "2026-08-01T09:30:00Z"}, source)
            )

    def test_missing_or_unusable_values_return_none(self):
        cases = [
            ({}, sync.CollectionSource.plex),
            ({"addedAt": None}, sync.CollectionSource.plex),
            ({"addedAt": "not-a-number"}, sync.CollectionSource.plex),
            ({}, sync.CollectionSource.jellyfin),
            ({"DateCreated": ""}, sync.CollectionSource.jellyfin),
            ({"DateCreated": "yesterday"}, sync.CollectionSource.jellyfin),
        ]
        for item, source in cases:
            self.assertIsNone(sync.provider_added_at(item, source), (item, source))


class CollectionAddedAtHealTests(unittest.TestCase):
    """The heal has to lower Collection.added_at without ever raising it, which
    is why the comparison happens in SQL rather than in Python."""

    def test_statement_takes_the_lesser_of_stored_and_provider_dates(self):
        rendered = str(sync.collection_added_at_heal_stmt().compile(dialect=_pg.dialect()))
        self.assertIn("UPDATE collections SET added_at=least(collections.added_at,", rendered)
        self.assertIn("WHERE collections.id =", rendered)


if __name__ == "__main__":
    unittest.main()

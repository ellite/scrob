import unittest

from core.watchlist_reconcile import (
    SUPPRESS_MIN_BASELINE,
    compute_new_baseline,
    media_key,
    plan_watchlist_reconcile,
)


def _plan(local, remote, baseline, pull=True, push=True):
    return plan_watchlist_reconcile(local, remote, baseline, pull_enabled=pull, push_enabled=push)


class MediaKeyTests(unittest.TestCase):
    def test_typed_keys_distinguish_movie_and_show_with_same_tmdb_id(self):
        self.assertNotEqual(media_key("movie", 603), media_key("show", 603))
        self.assertEqual(media_key("movie", 603), "movie:603")


class TruthTableBothDirectionsTests(unittest.TestCase):
    """Every (local, remote, baseline) membership state with both flags on."""

    def test_in_sync_key_produces_no_action(self):
        plan = _plan({"movie:1"}, {"movie:1"}, ["movie:1"])
        self.assertFalse(plan.has_changes)
        self.assertFalse(plan.suppressed)

    def test_locally_added_key_is_pushed(self):
        plan = _plan({"movie:1"}, set(), [])
        self.assertEqual(plan.push_add, {"movie:1"})
        self.assertEqual(plan.remove_local, frozenset())

    def test_remotely_added_key_is_imported(self):
        plan = _plan(set(), {"movie:1"}, [])
        self.assertEqual(plan.add_local, {"movie:1"})

    def test_remotely_deleted_key_is_removed_locally_not_resurrected(self):
        # The headline fix: Plex auto-removed a watched item; the old additive
        # push re-added it. The baseline proves it was synced before, so the
        # only correct action is the local removal.
        plan = _plan({"movie:1"}, set(), ["movie:1"])
        self.assertEqual(plan.remove_local, {"movie:1"})
        self.assertEqual(plan.push_add, frozenset())

    def test_locally_deleted_key_is_removed_remotely(self):
        plan = _plan(set(), {"movie:1"}, ["movie:1"])
        self.assertEqual(plan.push_remove, {"movie:1"})
        self.assertEqual(plan.add_local, frozenset())

    def test_key_deleted_on_both_sides_just_leaves_the_baseline(self):
        plan = _plan(set(), set(), ["movie:1"])
        self.assertFalse(plan.has_changes)

    def test_key_added_on_both_sides_is_simply_in_sync(self):
        plan = _plan({"movie:1"}, {"movie:1"}, [])
        self.assertFalse(plan.has_changes)


class PullOnlyModeTests(unittest.TestCase):
    """Pull-only stays a plain mirror of the remote, as it always was."""

    def test_local_extra_is_removed_even_without_baseline_entry(self):
        plan = _plan({"movie:1"}, set(), [], push=False)
        self.assertEqual(plan.remove_local, {"movie:1"})
        self.assertEqual(plan.push_add, frozenset())

    def test_locally_deleted_key_is_readded_immediately(self):
        # No push direction to propagate the local deletion, so the mirror
        # re-adds in the same run rather than flapping over two runs.
        plan = _plan(set(), {"movie:1"}, ["movie:1"], push=False)
        self.assertEqual(plan.add_local, {"movie:1"})
        self.assertEqual(plan.push_remove, frozenset())

    def test_never_pushes(self):
        plan = _plan({"movie:1", "movie:2"}, {"movie:3"}, ["movie:2"], push=False)
        self.assertEqual(plan.push_add, frozenset())
        self.assertEqual(plan.push_remove, frozenset())


class PushOnlyModeTests(unittest.TestCase):
    """Push-only keeps Scrob as the master, as it always was."""

    def test_remote_deletion_of_synced_key_is_readded(self):
        plan = _plan({"movie:1"}, set(), ["movie:1"], pull=False)
        self.assertEqual(plan.push_add, {"movie:1"})
        self.assertEqual(plan.remove_local, frozenset())

    def test_remote_only_key_is_ignored(self):
        plan = _plan(set(), {"movie:1"}, [], pull=False)
        self.assertFalse(plan.has_changes)

    def test_locally_deleted_key_is_removed_remotely(self):
        plan = _plan(set(), {"movie:1"}, ["movie:1"], pull=False)
        self.assertEqual(plan.push_remove, {"movie:1"})

    def test_never_touches_local_state(self):
        plan = _plan({"movie:1"}, {"movie:2", "movie:3"}, ["movie:3"], pull=False)
        self.assertEqual(plan.add_local, frozenset())
        self.assertEqual(plan.remove_local, frozenset())


class BootstrapTests(unittest.TestCase):
    def test_none_baseline_never_infers_deletions(self):
        plan = _plan({"movie:1"}, {"show:2"}, None)
        self.assertEqual(plan.push_add, {"movie:1"})
        self.assertEqual(plan.add_local, {"show:2"})
        self.assertEqual(plan.remove_local, frozenset())
        self.assertEqual(plan.push_remove, frozenset())

    def test_none_baseline_is_not_the_same_as_empty_baseline(self):
        # An empty baseline means a reconcile happened and everything was
        # cleared, so deletions may be inferred. None means no reconcile has
        # ever happened, so they may not.
        with_none = _plan({"movie:1"}, set(), None, push=False)
        with_empty = _plan({"movie:1"}, set(), [], push=False)
        self.assertEqual(with_none.remove_local, frozenset())
        self.assertEqual(with_empty.remove_local, {"movie:1"})

    def test_none_baseline_respects_direction_flags(self):
        pull_only = _plan({"movie:1"}, {"show:2"}, None, push=False)
        self.assertEqual(pull_only.push_add, frozenset())
        push_only = _plan({"movie:1"}, {"show:2"}, None, pull=False)
        self.assertEqual(push_only.add_local, frozenset())


class CircuitBreakerTests(unittest.TestCase):
    def _baseline(self, n):
        return [media_key("movie", i) for i in range(n)]

    def test_small_watchlist_draining_to_empty_reconciles_unconditionally(self):
        # The canonical trigger: the last watched item auto-removed from a
        # small watchlist. Below the floor there is no suppression, or the
        # headline fix would never fire.
        baseline = self._baseline(SUPPRESS_MIN_BASELINE - 1)
        plan = _plan(set(baseline), set(), baseline)
        self.assertFalse(plan.suppressed)
        self.assertEqual(plan.remove_local, frozenset(baseline))

    def test_empty_remote_with_established_baseline_suppresses_everything(self):
        baseline = self._baseline(SUPPRESS_MIN_BASELINE)
        plan = _plan(set(baseline) | {"show:99"}, set(), baseline)
        self.assertTrue(plan.suppressed)
        self.assertIsNotNone(plan.suppressed_reason)
        self.assertFalse(plan.has_changes)  # a suspect fetch drives nothing

    def test_majority_shrink_suppresses(self):
        baseline = self._baseline(12)
        remote = set(baseline[:5])  # 7 of 12 would be removed
        plan = _plan(set(baseline), remote, baseline)
        self.assertTrue(plan.suppressed)

    def test_minority_shrink_reconciles(self):
        baseline = self._baseline(12)
        remote = set(baseline[:7])  # 5 of 12 removed, under half
        plan = _plan(set(baseline), remote, baseline)
        self.assertFalse(plan.suppressed)
        self.assertEqual(plan.remove_local, frozenset(baseline[7:]))

    def test_push_only_mode_never_suppresses(self):
        # No local deletions can happen, so there is nothing to protect;
        # re-adding everything is that mode's documented master semantics.
        baseline = self._baseline(20)
        plan = _plan(set(baseline), set(), baseline, pull=False)
        self.assertFalse(plan.suppressed)
        self.assertEqual(plan.push_add, frozenset(baseline))


class NewBaselineTests(unittest.TestCase):
    def test_reflects_final_local_state(self):
        self.assertEqual(compute_new_baseline({"movie:2", "movie:1"}), ["movie:1", "movie:2"])

    def test_failed_remote_add_is_excluded_so_it_retries(self):
        new = compute_new_baseline({"movie:1", "movie:2"}, failed_push_add={"movie:2"})
        self.assertEqual(new, ["movie:1"])

    def test_failed_remote_remove_is_retained_so_it_retries(self):
        new = compute_new_baseline({"movie:1"}, failed_push_remove={"movie:3"})
        self.assertEqual(new, ["movie:1", "movie:3"])


class ConvergenceTests(unittest.TestCase):
    """Two consecutive runs settle; nothing oscillates."""

    def _apply(self, plan, local, remote):
        local = (set(local) | set(plan.add_local)) - set(plan.remove_local)
        remote = (set(remote) | set(plan.push_add)) - set(plan.push_remove)
        return local, remote

    def test_resurrection_scenario_settles_in_one_run(self):
        local, remote, baseline = {"movie:1"}, set(), ["movie:1"]
        plan = _plan(local, remote, baseline)
        local, remote = self._apply(plan, local, remote)
        baseline = compute_new_baseline(local)
        self.assertEqual((local, remote, baseline), (set(), set(), []))
        second = _plan(local, remote, baseline)
        self.assertFalse(second.has_changes)

    def test_failed_live_push_retries_then_settles(self):
        # A UI add whose fire-and-forget push failed: locally present,
        # remotely absent, not in the baseline.
        local, remote, baseline = {"movie:1"}, set(), []
        plan = _plan(local, remote, baseline)
        self.assertEqual(plan.push_add, {"movie:1"})
        local, remote = self._apply(plan, local, remote)
        baseline = compute_new_baseline(local)
        second = _plan(local, remote, baseline)
        self.assertFalse(second.has_changes)

    def test_failed_remote_remove_retries_then_settles(self):
        local, remote, baseline = set(), {"movie:1"}, ["movie:1"]
        plan = _plan(local, remote, baseline)
        self.assertEqual(plan.push_remove, {"movie:1"})
        # The remove fails: remote unchanged, key retained in the baseline.
        baseline = compute_new_baseline(set(), failed_push_remove={"movie:1"})
        second = _plan(local, remote, baseline)
        self.assertEqual(second.push_remove, {"movie:1"})  # retried
        local, remote = self._apply(second, local, remote)
        baseline = compute_new_baseline(local)
        third = _plan(local, remote, baseline)
        self.assertFalse(third.has_changes)


if __name__ == "__main__":
    unittest.main()

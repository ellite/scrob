"""Three-way reconciliation between Scrob's watchlist and a service's.

Scrob has one watchlist per user (core/native_watchlist.py) and every
service with a watchlist - Plex, Trakt, MDBList, Simkl - reconciles against
that same list, each with its own baseline. The watchlist is synced by pull
and push jobs on independent schedules, so an item missing on one side is
ambiguous: never synced, or deliberately removed there. Guessing wrong is
how Plex's auto-remove-after-watching used to get undone by the next push.

The tiebreaker is a persisted baseline per service (None until a first
reconcile completes). Comparing local L, remote R and baseline B classifies
every key, and the same plan comes out no matter which job runs first:

    L R B   both directions      pull only              push only
    -----   ------------------   --------------------   ------------------
    1 1 *   in sync              in sync                in sync
    1 0 0   push_add             (ignored)              push_add
    0 1 0   add_local            add_local              (ignored)
    1 0 1   remove_local         remove_local           push_add (master)
    0 1 1   push_remove          (ignored)              push_remove
    0 0 1   drops from baseline  drops from baseline    drops from baseline

With both directions on, each side's removals propagate exactly once
((1,0,1) is the resurrection fix), and a local add whose fire-and-forget
live push failed gets retried instead of deleted by the next pull
((1,0,0)). Push-only keeps Scrob as the master.

Pull-only only applies the service's own changes. It used to be a plain
mirror of the remote, which is wrong once the list is shared: mirroring
Simkl would wipe everything Plex or the lists UI added. So in pull-only
mode the baseline is the remote as last seen (compute_pull_only_baseline),
not the agreed state, and only keys that appeared or disappeared on the
remote since then touch the local list. A local removal of a key that is
still on a pull-only remote sticks.

With no baseline yet nothing is ever deleted - the plan is purely additive
in whichever directions are enabled, like a first sync always was.

Remote-driven deletions go through the same circuit breaker idea as the
collection pruner in routers/sync.py: a fetch that succeeds but comes back
empty or majority-shrunk against an established baseline looks exactly like
a broken fetch, so the run is skipped rather than trusted.
"""

from dataclasses import dataclass, field
from typing import Iterable, Optional

# Same thresholds as the collection pruner (kept local - core doesn't import
# from routers). Below the floor there is no suppression at all: small
# watchlists drain to empty legitimately all the time.
SUPPRESS_MIN_BASELINE = 10
SUPPRESS_MAX_FRACTION = 0.5

_EMPTY: frozenset[str] = frozenset()


def media_key(kind: str, tmdb_id: int) -> str:
    """Key like "movie:603" - typed, since a movie and a show can share a
    TMDB id. Same format the watchlist poller in main.py uses."""
    return f"{kind}:{tmdb_id}"


@dataclass(frozen=True)
class ReconcilePlan:
    add_local    : frozenset[str] = _EMPTY
    remove_local : frozenset[str] = _EMPTY
    push_add     : frozenset[str] = _EMPTY
    push_remove  : frozenset[str] = _EMPTY
    suppressed   : bool = False
    suppressed_reason : Optional[str] = field(default=None, compare=False)

    @property
    def has_changes(self) -> bool:
        return bool(self.add_local or self.remove_local or self.push_add or self.push_remove)


def plan_watchlist_reconcile(
    local: Iterable[str],
    remote: Iterable[str],
    baseline: Optional[Iterable[str]],
    *,
    pull_enabled: bool,
    push_enabled: bool,
) -> ReconcilePlan:
    """Classify every key per the table in the module docstring.

    baseline=None means "never reconciled" and is not the same as an empty
    baseline - only the former disables deletion inference."""
    l, r = set(local), set(remote)

    if baseline is None:
        return ReconcilePlan(
            add_local=frozenset(r - l) if pull_enabled else _EMPTY,
            push_add=frozenset(l - r) if push_enabled else _EMPTY,
        )

    b = set(baseline)

    add_local: set[str] = set()
    remove_local: set[str] = set()
    push_add: set[str] = set()
    push_remove: set[str] = set()

    if pull_enabled and not push_enabled:
        # Remote deltas only - see "Pull-only" in the module docstring.
        add_local = (r - b) - l
        remove_local = (b - r) & l
    else:
        for key in l - r:
            if key in b:  # (1,0,1) synced before, gone remotely
                if pull_enabled:
                    remove_local.add(key)
                elif push_enabled:  # Scrob is master: put it back
                    push_add.add(key)
            elif push_enabled:  # (1,0,0) added locally since last reconcile
                push_add.add(key)

        for key in r - l:
            if key in b:  # (0,1,1) synced before, gone locally
                if push_enabled:
                    push_remove.add(key)
            elif pull_enabled:  # (0,1,0) added remotely since last reconcile
                add_local.add(key)

    if remove_local and len(b) >= SUPPRESS_MIN_BASELINE:
        reason = None
        if not r:
            reason = f"remote watchlist came back empty with {len(b)} baseline item(s)"
        elif len(remove_local & b) > SUPPRESS_MAX_FRACTION * len(b):
            reason = (
                f"remote watchlist would remove {len(remove_local & b)} of "
                f"{len(b)} baseline item(s)"
            )
        if reason:
            # Don't act on any of it - a suspect fetch shouldn't drive a mass
            # re-add either. Baseline stays put so a healthy fetch recovers.
            return ReconcilePlan(suppressed=True, suppressed_reason=reason)

    return ReconcilePlan(
        add_local=frozenset(add_local),
        remove_local=frozenset(remove_local),
        push_add=frozenset(push_add),
        push_remove=frozenset(push_remove),
    )


def compute_new_baseline(
    final_local: Iterable[str],
    *,
    failed_push_add: Iterable[str] = (),
    failed_push_remove: Iterable[str] = (),
) -> list[str]:
    """Next baseline from what actually happened, not what was planned.

    final_local must be the real post-apply local state - a local add that
    failed must not land in the baseline, or the key would later read as a
    local deletion and get removed from Plex. Failed remote adds are left
    out so they retry as (1,0,0); failed remote removes are kept in so they
    retry as (0,1,1)."""
    return sorted((set(final_local) | set(failed_push_remove)) - set(failed_push_add))


def compute_pull_only_baseline(
    remote: Iterable[str],
    *,
    failed_add_local: Iterable[str] = (),
) -> list[str]:
    """Next baseline for a pull-only reconcile: the remote as seen this run.

    A remote add that failed to import locally is left out, so it still
    reads as new on the remote next time and is retried."""
    return sorted(set(remote) - set(failed_add_local))

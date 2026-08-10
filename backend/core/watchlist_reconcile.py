"""Three-way reconciliation for the Plex watchlist mirror.

The watchlist is synced by pull and push jobs on independent schedules, so
an item missing on one side is ambiguous: never synced, or deliberately
removed there. Guessing wrong is how Plex's auto-remove-after-watching used
to get undone by the next push.

The tiebreaker is a persisted baseline - the key set both sides agreed on
after the last reconcile (None until a first one completes). Comparing
local L, remote R and baseline B classifies every key, and the same plan
comes out no matter which job runs first:

    L R B   both directions      pull only              push only
    -----   ------------------   --------------------   ------------------
    1 1 *   in sync              in sync                in sync
    1 0 0   push_add             remove_local (mirror)  push_add
    0 1 0   add_local            add_local              (ignored)
    1 0 1   remove_local         remove_local           push_add (master)
    0 1 1   push_remove          add_local (mirror)     push_remove
    0 0 1   drops from baseline  drops from baseline    drops from baseline

Pull-only stays a plain mirror of the remote and push-only keeps Scrob as
the master, same as before. With both directions on, each side's removals
now propagate exactly once ((1,0,1) is the resurrection fix), and a local
add whose fire-and-forget live push failed gets retried instead of deleted
by the next pull ((1,0,0)).

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

    for key in l - r:
        if key in b:  # (1,0,1) synced before, gone remotely
            if pull_enabled:
                remove_local.add(key)
            elif push_enabled:  # Scrob is master: put it back
                push_add.add(key)
        else:  # (1,0,0) added locally since last reconcile
            if push_enabled:
                push_add.add(key)
            elif pull_enabled:  # plain mirror: remote wins
                remove_local.add(key)

    for key in r - l:
        if key in b:  # (0,1,1) synced before, gone locally
            if push_enabled:
                push_remove.add(key)
            elif pull_enabled:  # plain mirror: remote wins
                add_local.add(key)
        else:  # (0,1,0) added remotely since last reconcile
            if pull_enabled:
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

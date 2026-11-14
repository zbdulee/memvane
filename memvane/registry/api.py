"""Public registry operations; implementations load when called."""
from __future__ import annotations

from memvane.registry.types import HOOK_ROWS, HOOK_SECONDS, HOOK_BACKFILL_PAGE, HOOK_BUSY_MS
from memvane.registry.types import RECONCILE_ROWS, RECONCILE_SECONDS, BACKFILL_PAGE, BUSY_MS


def after_setup(action, project_root, state_root, result):
    from memvane.registry.memvane_registry import after_setup as implementation
    return implementation(action, project_root, state_root, result)


def reconcile(*, home=None, now=None, clock=None, max_rows=RECONCILE_ROWS, seconds=RECONCILE_SECONDS,
              backfill_page=BACKFILL_PAGE, seed=None, busy_ms=BUSY_MS, worktrees=True):
    from memvane.registry.memvane_registry import reconcile as implementation
    return implementation(home=home, now=now, clock=clock, max_rows=max_rows, seconds=seconds,
                          backfill_page=backfill_page, seed=seed, busy_ms=busy_ms, worktrees=worktrees)


def routing_rows(*, home=None, busy_ms=HOOK_BUSY_MS):
    from memvane.registry.memvane_registry import routing_rows as implementation
    return implementation(home=home, busy_ms=busy_ms)


def is_home_install(state, home=None):
    from memvane.registry.memvane_registry import is_home_install as implementation
    return implementation(state, home)


def excluded(view, install_id, project_root):
    from memvane.registry.memvane_registry import excluded as implementation
    return implementation(view, install_id, project_root)


def snapshot(*, home=None, reconcile=True, now=None):
    from memvane.registry.memvane_registry import snapshot as implementation
    return implementation(home=home, reconcile=reconcile, now=now)

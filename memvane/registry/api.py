"""Public registry operations; implementations load when called."""
from __future__ import annotations

from memvane.registry.types import HOOK_ROWS, HOOK_SECONDS, HOOK_BACKFILL_PAGE, HOOK_BUSY_MS


def after_setup(*args, **kwargs):
    from memvane.registry.memvane_registry import after_setup as implementation
    return implementation(*args, **kwargs)


def reconcile(*args, **kwargs):
    from memvane.registry.memvane_registry import reconcile as implementation
    return implementation(*args, **kwargs)


def routing_rows(*args, **kwargs):
    from memvane.registry.memvane_registry import routing_rows as implementation
    return implementation(*args, **kwargs)


def is_home_install(*args, **kwargs):
    from memvane.registry.memvane_registry import is_home_install as implementation
    return implementation(*args, **kwargs)


def excluded(*args, **kwargs):
    from memvane.registry.memvane_registry import excluded as implementation
    return implementation(*args, **kwargs)


def snapshot(*args, **kwargs):
    from memvane.registry.memvane_registry import snapshot as implementation
    return implementation(*args, **kwargs)

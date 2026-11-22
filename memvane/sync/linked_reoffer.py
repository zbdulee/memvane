"""Whether a pass that left linked views waiting asks for the same wake again.

A pass that published the main view is acknowledged, so its wake row and the
row's attempt count are gone; a re-offer is a new request. This module keeps
what is needed to bound those requests in one small file of the install state
root (``linked-reoffer.json``): what each waiting view had left to ingest
after the pass before, the lowest total wait seen, and how many re-offers in a
row did not lower it.

The wake is offered again only when a waiting view was not reached by the pass
(``schema3_pass_budget``), is seen for the first time, or has less left to
ingest than after the pass before. Movement elsewhere (the main view, another
view) and work that did not lower what is left are no reason. ``CAP`` offers
in a row without a new low end the free re-offers: the pass then fails as
``schema3_linked_pending`` and the wake queue's backoff and retry cap bound it.

The file is a cache. Unreadable, it reads as no history; when it cannot be
written no re-offer is made (the count could not advance), and the next sync
request reaches the linked views.
"""
from __future__ import annotations

import json
from pathlib import Path

NAME = 'linked-reoffer.json'
CAP = 20
_MAX_BYTES = 256 * 1024
_MAX_VIEWS = 64


def _read(path):
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_BYTES:
            return {}
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    remaining = value.get('remaining')
    floor, streak = value.get('floor'), value.get('streak')
    return {'remaining': {root: left for root, left in (remaining.items() if isinstance(remaining, dict) else ())
                          if isinstance(root, str) and type(left) is int and left >= 0},
            'floor': floor if type(floor) is int and floor >= 0 else None,
            'streak': streak if type(streak) is int and streak >= 0 else 0}


def decide(state_root, backlog) -> str:
    """``offer``, ``capped`` or ``none`` for one pass; ``backlog`` is ``_linked_waiting``'s (None: nothing waits)."""
    from memvane.kernel.atomic_rename import atomic_json
    if not isinstance(state_root, str) or not state_root:
        return 'none'
    path = Path(state_root) / NAME
    if backlog is None:
        try:
            path.unlink()
        except OSError:
            pass  # Unchecked: a stale history only makes the next wait look older than it is.
        return 'none'
    before = _read(path)
    remaining = dict(list(backlog['remaining'].items())[:_MAX_VIEWS])
    known = before.get('remaining', {})
    less = any(root not in known or left < known[root] for root, left in remaining.items())
    eligible = backlog['unreached'] > 0 or less
    total = backlog['views'] + sum(remaining.values())
    floor, streak = before.get('floor'), before.get('streak', 0)
    if eligible:
        if floor is None or total < floor:
            floor, streak = total, 0
        else:
            streak += 1
    try:
        atomic_json(path, {'remaining': remaining, 'floor': floor, 'streak': streak})
    except (OSError, ValueError, TypeError):
        # Checked: without the written count the re-offers would have no bound.
        return 'none'
    if not eligible:
        return 'none'
    return 'capped' if streak > CAP else 'offer'

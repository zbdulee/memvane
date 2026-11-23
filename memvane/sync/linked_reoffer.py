"""Whether a pass that left linked views waiting asks for the same wake again.

A pass that published the main view is acknowledged, so its wake row and the
row's attempt count are gone; a re-offer is a new request. This module keeps
what is needed to bound those requests in one small file of the install state
root (``linked-reoffer.json``): what each waiting view had left (cards to
ingest, vector targets to fill) after the pass before, the lowest each has
been and the fewest views that waited, and how many re-offers in a row set no
new low.

The wake is offered again only when a waiting view was not reached by the pass
(``schema3_pass_budget``), is seen for the first time, or has less left to
than after the pass before. Movement elsewhere (the main view, another view)
and work that did not lower what is left are no reason. A view whose count rises
again (new work after it had finished or shrunk) starts its low again from
that count, so the passes that work it off set new lows. ``CAP`` offers in a
row without a new low (of any view, or of the number that wait) end the free
re-offers: the pass then fails as
``schema3_linked_pending`` and the wake queue's backoff and retry cap bound it.

Apart from that, ``ABSOLUTE_CAP`` offers in a row (a pass that is not offered
ends the row) fail one pass the same way, whatever moved: a count that rises
and falls again sets a new low every other pass.

The file is a cache. Unreadable, it reads as no history; when it cannot be
written no re-offer is made (the count could not advance), and the next sync
request reaches the linked views.
"""
from __future__ import annotations

import json
from pathlib import Path

NAME = 'linked-reoffer.json'
CAP = 20
# Offers in a row whatever moved: a count that goes up and down sets a new low every other pass.
ABSOLUTE_CAP = 200
_MAX_BYTES = 256 * 1024
_MAX_VIEWS = 64
_MAX_FLOORS = 4 * _MAX_VIEWS


def _counts(value):
    return {key: left for key, left in (value.items() if isinstance(value, dict) else ())
            if isinstance(key, str) and type(left) is int and left >= 0}


def _read(path):
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_BYTES:
            return {}
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    views, streak = value.get('views'), value.get('streak')
    offers = value.get('offers')
    return {'remaining': _counts(value.get('remaining')), 'floors': _counts(value.get('floors')),
            'views': views if type(views) is int and views >= 0 else None,
            'streak': streak if type(streak) is int and streak >= 0 else 0,
            'offers': offers if type(offers) is int and offers >= 0 else 0}


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
    # The same views are followed from pass to pass whatever order the pass met them in.
    remaining = dict(sorted(backlog['remaining'].items())[:_MAX_VIEWS])
    known = before.get('remaining', {})
    less = any(key not in known or left < known[key] for key, left in remaining.items())
    eligible = backlog['unreached'] > 0 or less
    # The lows are kept per view: which views a pass counts changes from pass to pass (one is
    # reached, another is not), so a sum over them can rise while every view only shrinks.
    floors = before.get('floors', {})
    views, streak = before.get('views'), before.get('streak', 0)
    lower = views is None or backlog['views'] < views
    for key, left in remaining.items():
        if key not in floors or left < floors[key]:
            floors[key], lower = left, True
        elif left > floors[key] and (key not in known or left > known[key]):
            # New work on a view that had finished or shrunk: its low starts again from here.
            # That is no new low (the streak stays); the passes that work it off set them.
            floors[key] = left
    # A view that waits now keeps its low; lows of views that do not wait fill what is left of the bound.
    kept = sorted(remaining) + sorted(key for key in floors if key not in remaining)
    floors = {key: floors[key] for key in kept[:_MAX_FLOORS]}
    if lower:
        views, streak = backlog['views'] if views is None else min(views, backlog['views']), 0
    elif eligible:
        streak += 1
    # Every offer since the views last stopped waiting, whatever moved; one counted failure starts it over.
    offers = before.get('offers', 0) + 1 if eligible else 0
    answer = 'none' if not eligible else 'capped' if streak > CAP or offers > ABSOLUTE_CAP else 'offer'
    try:
        atomic_json(path, {'remaining': remaining, 'floors': floors, 'views': views, 'streak': streak,
                           'offers': 0 if offers > ABSOLUTE_CAP else offers})
    except (OSError, ValueError, TypeError):
        # Checked: without the written count the re-offers would have no bound.
        return 'none'
    return answer

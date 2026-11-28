"""Dashboard preferences and the auto-open record under ``<state base>/ui`` (0600 files).

Reading never creates anything. A missing, unsafe or malformed preferences
file reads as the defaults with ``status`` naming why (fail-closed: the
auto-open switch stays off). ``update`` validates every key before it writes.
"""
from __future__ import annotations

from memvane.ui import platform_io
from memvane.ui.types import (AUTO_OPEN_FILE, AUTO_OPEN_SCHEMA, AUTO_OPEN_SCOPES, DEFAULT_PREFERENCES,
                              PREFERENCE_BOOLS, PREFERENCE_RANGES, PREFERENCES_FILE)


class PreferenceError(ValueError):
    """A preference change is invalid; the message is a fixed code."""


def preferences_path(home=None, *, create=False):
    return platform_io.ui_dir(home, create=create) / PREFERENCES_FILE


def _valid(key, value):
    if key in PREFERENCE_BOOLS:
        return isinstance(value, bool)
    if key in PREFERENCE_RANGES:
        low, high = PREFERENCE_RANGES[key]
        return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high
    if key == 'autoOpenScope':
        return value in AUTO_OPEN_SCOPES
    return key == 'schema' and value == DEFAULT_PREFERENCES['schema']


def load(home=None):
    """``(preferences, status)``; status is ``ok``, ``absent`` or a refusal code."""
    preferences = dict(DEFAULT_PREFERENCES)
    try:
        stored = platform_io.read_private_json(preferences_path(home))
    except FileNotFoundError:
        return preferences, 'absent'
    except platform_io.UiStateRefused as exc:
        return preferences, str(exc)
    if stored is None:
        return preferences, 'absent'
    if set(stored) - set(DEFAULT_PREFERENCES) or not all(_valid(k, v) for k, v in stored.items()):
        return preferences, 'ui_preferences_invalid'
    preferences.update(stored)
    return preferences, 'ok'


def update(changes, home=None):
    """Validate ``changes`` (known keys only), merge them into the stored file and return the result."""
    if not isinstance(changes, dict) or not changes:
        raise PreferenceError('ui_preferences_empty')
    for key, value in changes.items():
        if key == 'schema' or key not in DEFAULT_PREFERENCES:
            raise PreferenceError('ui_preferences_unknown_key')
        if not _valid(key, value):
            raise PreferenceError('ui_preferences_invalid_value')
    current, status = load(home)
    if status not in ('ok', 'absent', 'ui_preferences_invalid', 'ui_file_malformed'):
        raise PreferenceError(status)
    current.update(changes)
    platform_io.write_private_json(preferences_path(home, create=True), current)
    return current


def auto_open_state(home=None):
    """``{lastAutoOpenAt, lastUserClosedAt}`` (epoch seconds or None); unreadable reads as empty."""
    empty = {'schema': AUTO_OPEN_SCHEMA, 'lastAutoOpenAt': None, 'lastUserClosedAt': None}
    try:
        stored = platform_io.read_private_json(platform_io.ui_dir(home) / AUTO_OPEN_FILE)
    except (FileNotFoundError, platform_io.UiStateRefused):
        return empty
    if not stored or stored.get('schema') != AUTO_OPEN_SCHEMA:
        return empty
    for key in ('lastAutoOpenAt', 'lastUserClosedAt'):
        value = stored.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            empty[key] = float(value)
    return empty


def record_auto_open(home=None, *, opened_at=None, closed_at=None):
    """Store the given times next to the kept ones; the record outlives an idle shutdown."""
    state = auto_open_state(home)
    if opened_at is not None:
        state['lastAutoOpenAt'] = float(opened_at)
    if closed_at is not None:
        state['lastUserClosedAt'] = float(closed_at)
    platform_io.write_private_json(platform_io.ui_dir(home, create=True) / AUTO_OPEN_FILE, state)
    return state

"""MemVane per-user roots.

Every per-user directory the plugin owns hangs off one of these roots. Each
helper takes an optional ``home`` so tests run against a temporary HOME::

    state    ~/.local/state/memvane            installs, embed runtime, service
    support  ~/Library/Application Support/memvane   (macOS)
             ~/.local/share/memvane                  (other platforms)
    config   ~/.config/memvane
    cache    ~/Library/Caches/memvane (macOS), ~/.cache/memvane (other)

``LEGACY_*`` names the roots earlier releases used. Nothing creates them; an
install or per-user state already there keeps working in place
(initial-build pointer lookup, ``auto_context_state``, embed runtime GC, the
retired-backend cleanup).
"""
from __future__ import annotations

from pathlib import Path
import sys

NAME = 'memvane'
DISPLAY_NAME = 'MemVane'
DB_NAME = 'memvane-db'
PLUGIN_ID = 'memvane'
MARKETPLACE = 'memvane-marketplace'
REPOSITORY = 'zbdulee/memvane'
# Wiki cards and LaunchAgents written by earlier releases carry this owner
# name; readers accept it beside ``NAME`` so existing cards stay owned.
LEGACY_NAME = 'qmd-auto-context'
OWNER_NAMES = (NAME, LEGACY_NAME)
LEGACY_PLUGIN_ID = 'qmd-auto-context'
LEGACY_MARKETPLACE = 'qmd-auto-context-marketplace'

STATE_PARTS = ('.local', 'state', NAME)
LEGACY_STATE_PARTS = ('.local', 'state', 'laya')


def _home(home=None):
    return Path(home) if home is not None else Path.home()


def _support_parts(name):
    if sys.platform == 'darwin':
        return ('Library', 'Application Support', name)
    return ('.local', 'share', name)


def state_base(home=None):
    return _home(home).joinpath(*STATE_PARTS)


def legacy_state_base(home=None):
    return _home(home).joinpath(*LEGACY_STATE_PARTS)


def support_root(home=None):
    return _home(home).joinpath(*_support_parts(NAME))


def legacy_support_root(home=None):
    return _home(home).joinpath(*_support_parts(LEGACY_NAME))


def config_root(home=None):
    return _home(home) / '.config' / NAME


def cache_root(home=None):
    if sys.platform == 'darwin':
        return _home(home) / 'Library' / 'Caches' / NAME
    return _home(home) / '.cache' / NAME


def auto_context_state(home=None):
    """Per-user logs and scan state outside any install.

    An account that already keeps them under the pre-rename base continues
    there (lifecycle cursors, pending lifecycle requests and dedup compare
    state stay in effect) until that base is moved.
    """
    current = state_base(home) / 'auto-context'
    legacy = legacy_state_base(home) / 'auto-context'
    # Decided by the scan state alone: other entries (the resolver run-lock)
    # may create the MemVane directory first.
    if not (current / 'scan-state').exists() and (legacy / 'scan-state').is_dir():
        return legacy
    return current

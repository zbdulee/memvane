"""MemVane per-user roots.

Every per-user directory the plugin owns hangs off one of these roots. Each
helper takes an optional ``home`` so tests run against a temporary HOME::

    state    ~/.local/state/memvane            installs, embed runtime, service
    support  ~/Library/Application Support/memvane   (macOS)
             ~/.local/share/memvane                  (other platforms)
    config   ~/.config/memvane
    cache    ~/Library/Caches/memvane (macOS), ~/.cache/memvane (other)

``LEGACY_*`` names the roots earlier releases used. Nothing creates them.
The bundle upgrade worker moves each install found under the pre-rename state
base to the MemVane base (``laya_state_move``) and then retires the shared
pre-rename trees once nothing references them. Until then readers accept the
pre-rename location only where an unmoved install or tree can still be:
install roots (``laya_product_install._state_bases``), the build pointer
lookup, ``auto_context_state``, the embed runtime and retired-backend
reference checks, and the pre-rename service lease.
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


def install_root(install_id, home=None):
    """``~/.local/state/memvane/<install id>``: where every install lives."""
    return state_base(home) / install_id


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

    Read-compat: scan state still under the pre-rename base is used there
    (lifecycle cursors, pending lifecycle requests and dedup compare state
    stay in effect) until the worker moves that tree into the MemVane base.
    """
    current = state_base(home) / 'auto-context'
    legacy = legacy_state_base(home) / 'auto-context'
    # Decided by the scan state alone: other entries (the resolver run-lock)
    # may create the MemVane directory first.
    if not (current / 'scan-state').exists() and (legacy / 'scan-state').is_dir():
        return legacy
    return current

"""MemVane per-user roots.

Every per-user directory the plugin owns hangs off one of these roots. Each
helper takes an optional ``home`` so tests run against a temporary HOME::

    state    ~/.local/state/memvane/v1         installs, embed runtime, service
    support  ~/Library/Application Support/memvane   (macOS)
             ~/.local/share/memvane                  (other platforms)
    config   ~/.config/memvane
    cache    ~/Library/Caches/memvane (macOS), ~/.cache/memvane (other)

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

STATE_PARTS = ('.local', 'state', NAME, 'v1')


def _home(home=None):
    return Path(home) if home is not None else Path.home()


def _support_parts(name):
    if sys.platform == 'darwin':
        return ('Library', 'Application Support', name)
    return ('.local', 'share', name)


def state_base(home=None):
    return _home(home).joinpath(*STATE_PARTS)




def support_root(home=None):
    return _home(home).joinpath(*_support_parts(NAME))


def install_root(install_id, home=None):
    """``~/.local/state/memvane/v1/installs/<install id>``."""
    return installs_base(home) / install_id


def installs_base(home=None):
    return state_base(home) / 'installs'




def config_root(home=None):
    return _home(home) / '.config' / NAME


def cache_root(home=None):
    if sys.platform == 'darwin':
        return _home(home) / 'Library' / 'Caches' / NAME
    return _home(home) / '.cache' / NAME


def auto_context_state(home=None):
    """Per-user logs and scan state outside any install."""
    return state_base(home) / 'auto-context'

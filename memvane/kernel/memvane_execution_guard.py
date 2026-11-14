"""Keep development checkouts from writing the account's live v1 state.

Live state (the account's ``~/.local/state/memvane/v1/``: installs, the wake
queue, the sync owner locks, the resident service) is written only by a
plugin copy that runs from a host install path:

    ~/.claude/plugins/cache/<marketplace>/memvane/<version>    Claude Code
    ~/.codex/plugins/cache/<marketplace>/memvane/<version>     Codex
    ~/.hermes/plugins/memvane                                  Hermes Agent

``MEMVANE_TEST_ALLOW_UNINSTALLED_ROOT=1`` admits any copy, and only while
``MEMVANE_TEST_RUNTIME_ROOT`` names an absolute test runtime root: the wake
queue, sync owner locks, journal coordination and resident service then
resolve there (each refuses a root outside the platform temp roots), so a
test that bypasses the shape check keeps those UID-shared resources out of
the live tree.
"""
import os
from pathlib import Path
import pwd

from memvane.kernel import memvane_paths


def _account_home():
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def _plugin_root():
    return Path(__file__).resolve().parents[2]


def _test_bypass():
    if os.environ.get('MEMVANE_TEST_ALLOW_UNINSTALLED_ROOT') != '1':
        return False
    raw = os.environ.get('MEMVANE_TEST_RUNTIME_ROOT')
    return bool(raw) and os.path.isabs(raw)


def installed_plugin_root(root=None, account=None):
    """Whether ``root`` (default: this copy) is a host install path of the account."""
    account = _account_home() if account is None else Path(account)
    root = _plugin_root() if root is None else Path(root)
    for host in ('.claude', '.codex'):
        try:
            parts = root.relative_to(account / host / 'plugins' / 'cache').parts
        except ValueError:
            continue
        if len(parts) == 3 and parts[0] and parts[1] == 'memvane' and parts[2]:
            return True
    try:
        return root.relative_to(account / '.hermes' / 'plugins').parts == ('memvane',)
    except ValueError:
        return False


def require_cached_plugin_for_live_write(state=None):
    account = _account_home()
    live = memvane_paths.state_base(account)
    target = live if state is None else Path(state).resolve(strict=False)
    if target != live and live not in target.parents:
        return
    if _test_bypass() or installed_plugin_root(account=account):
        return
    raise ValueError('live_write_requires_plugin_cache')

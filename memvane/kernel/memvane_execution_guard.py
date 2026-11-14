"""Keep development checkouts from writing the account's live v1 state.

Live state (the account's ``~/.local/state/memvane/v1/``: installs, the wake
queue, the sync owner locks, the resident service) is written only by a
plugin copy that runs from a host install path:

    ~/.claude/plugins/cache/<marketplace>/memvane/<version>    Claude Code
    ~/.codex/plugins/cache/<marketplace>/memvane/<version>     Codex
    ~/.hermes/plugins/memvane                                  Hermes Agent

Each host root is judged by its real path: ``HOME`` and the account home,
and the hosts' relocation variables (``CLAUDE_CONFIG_DIR``, ``CODEX_HOME``,
``HERMES_HOME``) name where the host keeps its plugins, so a symlinked or
relocated host root admits the same cache shape and nothing else.

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


_HOSTS = (('.claude', 'CLAUDE_CONFIG_DIR', ('plugins', 'cache'), 3),
          ('.codex', 'CODEX_HOME', ('plugins', 'cache'), 3),
          ('.hermes', 'HERMES_HOME', ('plugins',), 1))


def _host_roots(accounts):
    """``(plugin base, depth)`` for every host root, each by its real path."""
    roots = []
    for default, variable, base, depth in _HOSTS:
        homes = [account / default for account in accounts]
        relocated = os.environ.get(variable)
        if relocated and os.path.isabs(relocated):
            homes.append(Path(relocated))
        for home in homes:
            entry = (Path(os.path.realpath(home)).joinpath(*base), depth)
            if entry not in roots:
                roots.append(entry)
    return roots


def _shape(parts, depth):
    if depth == 1:
        return parts == ('memvane',)
    return len(parts) == 3 and bool(parts[0]) and parts[1] == 'memvane' and bool(parts[2])


def installed_plugin_root(root=None, account=None):
    """Whether ``root`` (default: this copy) is a host install path of the account."""
    if account is None:
        accounts = [_account_home()]
        home = os.environ.get('HOME')
        if home and os.path.isabs(home):
            accounts.append(Path(os.path.realpath(home)))
    else:
        accounts = [Path(os.path.realpath(account))]
    root = _plugin_root() if root is None else Path(os.path.realpath(root))
    for base, depth in _host_roots(accounts):
        try:
            parts = root.relative_to(base).parts
        except ValueError:
            continue
        if _shape(parts, depth):
            return True
    return False


def require_cached_plugin_for_live_write(state=None):
    account = _account_home()
    live = memvane_paths.state_base(account)
    target = live if state is None else Path(state).resolve(strict=False)
    if target != live and live not in target.parents:
        return
    if _test_bypass() or installed_plugin_root():
        return
    raise ValueError('live_write_requires_plugin_cache')

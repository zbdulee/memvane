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
import os
import stat
import sys
import tempfile
import hashlib
import re

NAME = 'memvane'
DISPLAY_NAME = 'MemVane'
DB_NAME = 'memvane-db'
PLUGIN_ID = 'memvane'
MARKETPLACE = 'memvane-marketplace'
REPOSITORY = 'zbdulee/memvane'

STATE_PARTS = ('.local', 'state', NAME, 'v1')
STATE_FORMAT = 1


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


def install_socket(state_root, *, create=False):
    """Return the private, stable per-install socket in the v1 run directory."""
    state = Path(state_root)
    if (not state.is_absolute() or state.parent.name != 'installs'
            or not re.fullmatch(r'[0-9a-f]{32}', state.name)
            or state.resolve(strict=False) != state):
        raise ValueError('install_socket_state_invalid')
    run = state.parent.parent / 'run'
    if create:
        try:
            os.mkdir(run, 0o700)
        except FileExistsError:
            pass
    if os.path.lexists(run):
        info = run.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700
                or run.resolve(strict=True) != run):
            raise ValueError('install_socket_directory_invalid')
    elif create:
        raise ValueError('install_socket_directory_invalid')
    result = run / (hashlib.sha256(state.name.encode('ascii')).hexdigest()[:16] + '.sock')
    if len(os.fsencode(result)) > 100:
        raise ValueError('install_socket_path_too_long')
    return result


def ensure_state_layout(home=None):
    """Create or validate the private v1 format marker without replacing it."""
    from memvane.kernel.memvane_version import same_major
    if not same_major(STATE_FORMAT):
        raise ValueError('state_format_mismatch')
    base = state_base(home)
    info = base.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or base.resolve(strict=True) != base):
        raise ValueError('state_layout_root_invalid')
    target = base / 'layout.json'
    expected = b'{"stateFormat":1}\n'
    if not os.path.lexists(target):
        fd, temporary = tempfile.mkstemp(prefix='.layout-', dir=base)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(expected)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, target, follow_symlinks=False)
            except FileExistsError:
                pass
            else:
                directory = os.open(base, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            os.unlink(temporary)
    fd = os.open(target, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1
                or os.read(fd, len(expected) + 1) != expected):
            raise ValueError('state_layout_invalid')
    finally:
        os.close(fd)




def config_root(home=None):
    return _home(home) / '.config' / NAME


def cache_root(home=None):
    if sys.platform == 'darwin':
        return _home(home) / 'Library' / 'Caches' / NAME
    return _home(home) / '.cache' / NAME


def auto_context_state(home=None):
    """Per-user logs and scan state outside any install."""
    return state_base(home) / 'auto-context'

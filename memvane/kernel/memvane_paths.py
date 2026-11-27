"""MemVane per-user roots (~/.local/state/memvane/v2, support, config, cache)."""
from __future__ import annotations

import hashlib, os, pwd, re, stat, sys
from pathlib import Path

NAME = PLUGIN_ID = 'memvane'
MARKETPLACE, REPOSITORY = 'memvane-marketplace', 'zbdulee/memvane'
STATE_PARTS = ('.local', 'state', NAME, 'v2')
STATE_FORMAT = 2
def _home(home=None): return Path(home) if home is not None else Path.home()


def state_base(home=None):
    return _home(home).joinpath(*STATE_PARTS)


def _narrow_state_base(base, expected=None):
    """Validate state base and narrow group/other bits on owned real dirs (never widens)."""
    info = base.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or base.resolve(strict=True) != (base if expected is None else expected)):
        raise ValueError('state_layout_root_invalid')
    if info.st_mode & 0o077:
        flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(base, flags)
        try:
            opened = os.fstat(fd)
            if (not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.getuid()
                    or not os.path.samestat(opened, info)):
                raise ValueError('state_layout_root_invalid')
            os.fchmod(fd, stat.S_IMODE(opened.st_mode) & 0o700)
            if os.fstat(fd).st_mode & 0o077:
                raise ValueError('state_layout_root_invalid')
        finally:
            os.close(fd)


def ensure_state_dir(*parts, home=None, create=True):
    """Create/validate ``state_base(home)/<parts>`` component-by-component at 0o700."""
    root = _home(home)
    if not root.is_absolute(): raise ValueError('state_layout_root_invalid')
    for part in parts:
        if not isinstance(part, str) or not part or part in ('.', '..') or '/' in part or '\x00' in part:
            raise ValueError('state_layout_root_invalid')
    cursor, base_index = root, len(STATE_PARTS) - 1
    for index, name in enumerate((*STATE_PARTS, *parts)):
        cursor = cursor / name
        if not os.path.lexists(cursor):
            if not create: raise FileNotFoundError(cursor)
            try: os.mkdir(cursor, 0o700)
            except FileExistsError: pass
        if index == base_index and create:
            _narrow_state_base(cursor, expected=root.resolve(strict=True).joinpath(*STATE_PARTS))
            continue
        info = cursor.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise ValueError('state_layout_root_invalid')
    return cursor


def ensure_auto_context_dir(directory, home=None):
    """Create an auto-context directory using 0700 components when under state_base."""
    target = Path(directory)
    try: relative = target.relative_to(state_base(home))
    except ValueError:
        target.mkdir(parents=True, exist_ok=True)
        return target
    return ensure_state_dir(*relative.parts, home=home, create=True)


def install_root(install_id, home=None):
    """``~/.local/state/memvane/v2/installs/<install id>``."""
    return installs_base(home) / install_id


def installs_base(home=None):
    return state_base(home) / 'installs'


def install_socket(state_root, *, create=False):
    """Return the private, stable per-install socket in the state base run directory."""
    state = Path(state_root)
    if (not state.is_absolute() or state.parent.name != 'installs'
            or not re.fullmatch(r'[0-9a-f]{32}', state.name) or state.resolve(strict=False) != state):
        raise ValueError('install_socket_state_invalid')
    run = state.parent.parent / 'run'
    if create:
        try: os.mkdir(run, 0o700)
        except FileExistsError: pass
    if os.path.lexists(run):
        info = run.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700 or run.resolve(strict=True) != run):
            raise ValueError('install_socket_directory_invalid')
    elif create:
        raise ValueError('install_socket_directory_invalid')
    result = run / (hashlib.sha256(state.name.encode('ascii')).hexdigest()[:16] + '.sock')
    if len(os.fsencode(result)) > 100: raise ValueError('install_socket_path_too_long')
    return result


def ensure_state_layout(home=None):
    """Create or validate the private v2 format marker without replacing it."""
    from memvane.kernel.memvane_version import same_major
    if not same_major(STATE_FORMAT): raise ValueError('state_format_mismatch')
    base = state_base(home)
    _narrow_state_base(base)
    target, expected = base / 'layout.json', f'{{"stateFormat":{STATE_FORMAT}}}\n'.encode('ascii')
    if not os.path.lexists(target):
        import tempfile
        fd, temporary = tempfile.mkstemp(prefix='.layout-', dir=base)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(expected); output.flush(); os.fsync(output.fileno())
            try: os.link(temporary, target, follow_symlinks=False)
            except FileExistsError: pass
            else:
                directory = os.open(base, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                try: os.fsync(directory)
                finally: os.close(directory)
        finally:
            os.unlink(temporary)
    fd = os.open(target, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1 or os.read(fd, len(expected) + 1) != expected):
            raise ValueError('state_layout_invalid')
    finally:
        os.close(fd)


def auto_context_state(home=None):
    """Per-user logs and scan state outside any install."""
    runtime = os.environ.get('MEMVANE_TEST_RUNTIME_ROOT') if home is None else None
    if runtime is not None:
        candidate, account = Path(runtime), Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        resolved = candidate.resolve()
        if not candidate.is_absolute() or resolved == account or account in resolved.parents:
            raise OSError('fixture_runtime_root_unsafe')
        return resolved / 'auto-context'
    if home is None and os.environ.get('QMD_SETUP_GUARD_FIXTURE') == '1':
        account, resolved = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(), _home().resolve()
        if resolved == account or account in resolved.parents: raise OSError('fixture_home_unsafe')
    return state_base(home) / 'auto-context'


def auto_context_path(relative, override=None):
    """Resolve wiki state without allowing fixture overrides into the account home."""
    default = auto_context_state() / relative
    if override is None: return default
    candidate, home = Path(override), _home().resolve()
    temp_roots = [Path('/tmp').resolve(), Path('/var/tmp').resolve()]
    if sys.platform == 'darwin': temp_roots.append(Path('/private/var/folders'))
    if (os.environ.get('MEMVANE_TEST_RUNTIME_ROOT') is not None
            or os.environ.get('QMD_SETUP_GUARD_FIXTURE') == '1'
            or any(home == root or root in home.parents for root in temp_roots)):
        account, resolved = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(), candidate.resolve()
        if not candidate.is_absolute() or resolved == account or account in resolved.parents:
            return default
    return candidate

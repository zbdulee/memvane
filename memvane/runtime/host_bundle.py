"""Find installed host bundles and detect replacement of this process's copy.

The host's installation records select candidates; a cache directory by itself
is never evidence of installation. Per-install journal and provider proof still
run in the selected worker before it accesses a project.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

from memvane.kernel.memvane_version import version_key

_SEGMENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')


def _identity(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise ValueError('bundle_symlink')
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


class BundleIdentity:
    def __init__(self, root):
        self.root = Path(root)
        self.paths = (self.root, self.root / 'plugin.json', self.root / 'entry' / 'run.py')
        try:
            self.nodes = tuple(_identity(path) for path in self.paths)
            self.version = version_key(_read_json(self.root / 'plugin.json', limit=65536).get('version'))
        except (OSError, ValueError, TypeError, AttributeError, UnicodeError):
            self.nodes = None
            self.version = None

    def intact(self):
        try:
            return self.nodes is not None and self.nodes == tuple(_identity(path) for path in self.paths)
        except (OSError, ValueError):
            return False

    def entry(self, name, *args, python=None, isolated=False):
        return entry_argv(name, *args, python=python, isolated=isolated)

    def preferred(self):
        """Equal-version host copies may share a resident; older ones may not."""
        if not self.intact():
            return False
        if os.environ.get('MEMVANE_TEST_RUNTIME_ROOT'):
            return True
        root = latest_installed()
        if root is None:
            return False
        try:
            return self.version is not None and self.version == version_key(
                _read_json(root / 'plugin.json', limit=65536).get('version'))
        except (OSError, ValueError, AttributeError, TypeError):
            return False


def _safe_node(path, *, directory):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)):
        raise ValueError('bundle_path_unsafe')
    return info


def _read_json(path, limit=1024 * 1024):
    info = _safe_node(path, directory=False)
    if info.st_size > limit:
        raise ValueError('bundle_metadata_too_large')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        chunks, remaining = [], limit + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b''.join(chunks)
        after = os.fstat(fd)
        if (len(raw) > limit or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino)):
            raise ValueError('bundle_metadata_changed')
        return json.loads(raw.decode('utf8'))
    finally:
        os.close(fd)


def _host_listing(name, home):
    original = shutil.which(name)
    safe = []
    try:
        cwd = Path.cwd().resolve(strict=True)
    except OSError:
        cwd = Path('/')
    for raw in os.environ.get('PATH', '').split(os.pathsep):
        if not raw or not Path(raw).is_absolute():
            continue
        try:
            directory = Path(raw).resolve(strict=True)
            info = directory.stat()
        except OSError:
            continue
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid())
                or info.st_mode & 0o022 or (cwd != Path('/') and (directory == cwd or cwd in directory.parents))):
            continue
        if str(directory) not in safe:
            safe.append(str(directory))
    safe_path = os.pathsep.join(safe)
    executable = shutil.which(name, path=safe_path) if safe_path else None
    if executable is None:
        if original is not None:
            raise ValueError('bundle_inventory_unsafe')
        raise FileNotFoundError(name)
    executable = Path(executable).resolve(strict=True)
    info = executable.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
            or info.st_mode & 0o022):
        raise ValueError('bundle_inventory_unsafe')
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'USER', 'LOGNAME', 'TMPDIR',
                                            'CODEX_HOME', 'CLAUDE_CONFIG_DIR')
           if key in os.environ}
    env.update(PATH=safe_path, HOME=str(home), QMD_SANDBOX='1', PYTHONDONTWRITEBYTECODE='1')
    child = subprocess.run([str(executable), 'plugin', 'list', '--json'], cwd='/', env=env,
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False)
    if child.returncode != 0 or len(child.stdout) > 1024 * 1024:
        raise ValueError('bundle_inventory_unavailable')
    return json.loads(child.stdout.decode('utf8'))


def _codex_inventory(home):
    try:
        return _host_listing('codex', home)
    except FileNotFoundError:
        return {'installed': []}


def _claude_listing(home):
    return _host_listing('claude', home)


def _candidate(base, marketplace, version, host):
    if not all(isinstance(part, str) and _SEGMENT.fullmatch(part) for part in (marketplace, version)):
        return None
    key = version_key(version)
    if key is None or key[0] != 1:
        return None
    try:
        base = base.resolve(strict=True)
        root = base / 'plugins' / 'cache' / marketplace / 'memvane' / version
        for path in (base, base / 'plugins', base / 'plugins' / 'cache', root.parent.parent, root.parent, root,
                     root / 'entry'):
            _safe_node(path, directory=True)
        _safe_node(root / 'entry' / 'run.py', directory=False)
        _safe_node(root / ('.' + host + '-plugin'), directory=True)
        for relative in ('plugin.json', 'package.json', '.' + host + '-plugin/plugin.json'):
            manifest = _read_json(root / relative, limit=65536)
            if manifest.get('name') != 'memvane' or manifest.get('version') != version:
                return None
    except (OSError, ValueError, TypeError, AttributeError, UnicodeError):
        return None
    return key, root


def latest_installed(*, home=None):
    home = Path.home() if home is None else Path(home)
    codex = Path(os.environ.get('CODEX_HOME') or home / '.codex')
    claude = Path(os.environ.get('CLAUDE_CONFIG_DIR') or home / '.claude')
    hermes_home = Path(os.environ.get('HERMES_HOME') or home / '.hermes')
    if not all(path.is_absolute() for path in (home, codex, claude, hermes_home)):
        return None
    found = []
    try:
        inventory = _codex_inventory(home)
        rows = inventory['installed']
        if not isinstance(rows, list) or len(rows) > 128:
            raise ValueError('bundle_inventory_invalid')
        for row in rows:
            if (isinstance(row, dict) and row.get('name') == 'memvane'
                    and row.get('installed') is True and row.get('enabled') is True):
                item = _candidate(codex, row.get('marketplaceName'), row.get('version'), 'codex')
                if item:
                    found.append(item)
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, subprocess.TimeoutExpired):
        return None
    try:
        inventory = _read_json(claude / 'plugins' / 'installed_plugins.json')
        plugins = inventory['plugins']
        if not isinstance(plugins, dict) or len(plugins) > 128:
            raise ValueError('bundle_inventory_invalid')
        try:
            listing = _claude_listing(home)
        except FileNotFoundError as exc:
            raise ValueError('bundle_inventory_unavailable') from exc
        if isinstance(listing, dict):
            listing = listing.get('plugins', listing.get('installed'))
        if not isinstance(listing, list) or len(listing) > 128:
            raise ValueError('bundle_inventory_invalid')
        for row in listing:
            name = row.get('id', row.get('pluginId')) if isinstance(row, dict) else None
            if not isinstance(name, str) or not name.startswith('memvane@'):
                continue
            if type(row.get('enabled')) is not bool:
                raise ValueError('bundle_inventory_invalid')
            if not row['enabled']:
                continue
            rows = plugins.get(name)
            if not isinstance(rows, list) or len(rows) > 128:
                raise ValueError('bundle_inventory_invalid')
            market = name[len('memvane@'):]
            matching = [item for item in rows if isinstance(item, dict)
                        and all(item.get(key) == row.get(key) for key in ('version', 'installPath'))
                        and all(key not in row or item.get(key) == row[key] for key in ('scope', 'projectPath'))]
            if len(matching) != 1 or not isinstance(row.get('installPath'), str):
                raise ValueError('bundle_inventory_changed')
            item = _candidate(claude, market, row.get('version'), 'claude')
            if item is None or Path(row['installPath']).resolve() != item[1]:
                raise ValueError('bundle_inventory_changed')
            found.append(item)
    except FileNotFoundError:
        pass  # Claude is not installed.
    except (OSError, ValueError, KeyError, TypeError, AttributeError, UnicodeError):
        return None
    try:
        hermes = hermes_home.resolve(strict=True)
        root = hermes / 'plugins' / 'memvane'
        for path in (hermes, hermes / 'plugins', root, root / 'entry'):
            _safe_node(path, directory=True)
        _safe_node(root / 'entry' / 'run.py', directory=False)
        manifest = _read_json(root / 'plugin.json', limit=65536)
        key = version_key(manifest.get('version'))
        if manifest.get('name') == 'memvane' and key is not None and key[0] == 1:
            found.append((key, root))
    except FileNotFoundError:
        pass  # Hermes is not installed.
    except (OSError, ValueError, TypeError, AttributeError, UnicodeError):
        return None
    return max(found, key=lambda item: (item[0], str(item[1])))[1] if found else None


def entry_argv(name, *args, python=None, isolated=False):
    """Launch the latest installed 1.x copy, or refuse when it is unproved."""
    # Fixture tests run source checkout code under a private runtime root.
    # Tests exercising handoff inject a synthetic installed inventory instead.
    if os.environ.get('MEMVANE_TEST_RUNTIME_ROOT'):
        from memvane.kernel.bundle_paths import plugin_root
        root = plugin_root()
    else:
        root = latest_installed()
    if root is None:
        raise ValueError('installed_bundle_unavailable')
    return [python or sys.executable, *(['-I'] if isolated else []), '-B',
            str(root / 'entry' / 'run.py'), name, *args]

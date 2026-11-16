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
import time

from memvane.kernel.memvane_version import version_key

_RETIRE_CHECKS = {}
_BACKGROUND_INVENTORY = {'until': 0.0, 'root': None}

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

    def entry(self, name, *args, python=None, isolated=False, background=False):
        return entry_argv(name, *args, python=python, isolated=isolated, background=background)

    def process_version_counts(self):
        return process_version_counts()

    def preferred_background(self):
        return self.preferred(background=True)

    def preferred(self, *, background=False):
        """Equal-version host copies may share a resident; older ones may not."""
        if not self.intact():
            return False
        if os.environ.get('MEMVANE_TEST_RUNTIME_ROOT'):
            return True
        root = background_installed() if background else fast_installed()
        if root is None:
            return True  # An unreadable inventory does not revoke an intact resident.
        try:
            other = version_key(_read_json(root / 'plugin.json', limit=65536).get('version'))
            return self.version is not None and (other is None or
                (self.version == other if background else self.version >= other))
        except (OSError, ValueError, AttributeError, TypeError):
            return True  # A transient metadata read cannot revoke this intact bundle.


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


def _hermes_enabled(home):
    """Accept only an explicit, simple enabled entry in the owner config."""
    path = home / 'config.yaml'
    info = _safe_node(path, directory=False)
    if info.st_size > 1024 * 1024:
        raise ValueError('bundle_metadata_too_large')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        raw = os.read(fd, 1024 * 1024 + 1)
        after = os.fstat(fd)
        if (len(raw) != before.st_size or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino)):
            raise ValueError('bundle_metadata_changed')
    finally:
        os.close(fd)
    section, subsection, seen, enabled, disabled = False, None, set(), False, False
    for raw_line in raw.decode('utf8').splitlines():
        line = raw_line.split('#', 1)[0].rstrip()
        if not line:
            continue
        if not line[0].isspace():
            if line == 'plugins:':
                if section or 'plugins' in seen:
                    raise ValueError('bundle_metadata_invalid')
                seen.add('plugins'); section = True; subsection = None
            else:
                section = False; subsection = None
            continue
        if not section:
            continue
        match = re.fullmatch(r'  (enabled|disabled):\s*', line)
        if match:
            subsection = match.group(1)
            if subsection in seen:
                raise ValueError('bundle_metadata_invalid')
            seen.add(subsection)
            continue
        if line.startswith('  ') and not line.startswith('    '):
            subsection = None
            continue
        if subsection:
            item = re.fullmatch(r'    -\s*([A-Za-z0-9_-]+)\s*', line)
            if item is None:
                raise ValueError('bundle_metadata_invalid')
            if item.group(1) == 'memvane':
                if subsection == 'enabled': enabled = True
                else: disabled = True
    return enabled and not disabled


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
        if not _hermes_enabled(hermes):
            raise FileNotFoundError('hermes_plugin_not_enabled')
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




def _codex_enabled_markets(codex):
    """Read the bounded owner-controlled Codex plugin enablement tables."""
    path = codex / 'config.toml'
    info = _safe_node(path, directory=False)
    if info.st_size > 1024 * 1024:
        raise ValueError('bundle_metadata_too_large')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        raw = os.read(fd, 1024 * 1024 + 1)
        after = os.fstat(fd)
        if (len(raw) != before.st_size or len(raw) > 1024 * 1024 or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino)):
            raise ValueError('bundle_metadata_changed')
    finally:
        os.close(fd)
    markets, current, seen, enable_seen = [], None, set(), set()
    for line in raw.decode('utf8').splitlines():
        line = line.strip()
        if line.startswith('['):
            match = re.fullmatch(r'\[plugins\."memvane@([A-Za-z0-9][A-Za-z0-9._-]{0,127})"\]\s*(?:#.*)?', line)
            current = match.group(1) if match else None
            if current in seen:
                raise ValueError('bundle_metadata_invalid')
            if current:
                seen.add(current)
        elif current and re.match(r'enabled\s*=', line):
            if current in enable_seen:
                raise ValueError('bundle_metadata_invalid')
            enable_seen.add(current)
            if re.fullmatch(r'enabled\s*=\s*true\s*(?:#.*)?', line):
                markets.append(current)
            elif not re.fullmatch(r'enabled\s*=\s*false\s*(?:#.*)?', line):
                raise ValueError('bundle_metadata_invalid')
    return markets


def _inventory_unavailable(host):
    from memvane.diagnostics import api as diagnostics
    diagnostics.emit('runtime.bundle', outcome='unavailable', reason='host_inventory_unreadable', host=host)


def fast_installed(*, home=None):
    """Read bounded host records only; never launch a host CLI on a hook."""
    home = Path.home() if home is None else Path(home)
    codex = Path(os.environ.get('CODEX_HOME') or home / '.codex')
    claude = Path(os.environ.get('CLAUDE_CONFIG_DIR') or home / '.claude')
    found = []
    try:
        from memvane.kernel.bundle_paths import plugin_root
        own = plugin_root()
        for market in _codex_enabled_markets(codex):
            parent = codex / 'plugins' / 'cache' / market / 'memvane'
            _safe_node(parent, directory=True)
            if own.parent == parent:
                item = _candidate(codex, market, own.name, 'codex')
                if item is not None and item[1] == own:
                    found.append(item)
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, AttributeError, UnicodeError):
        _inventory_unavailable('codex')
    try:
        plugins = _read_json(claude / 'plugins' / 'installed_plugins.json')['plugins']
        enabled = _read_json(claude / 'settings.json').get('enabledPlugins', {})
        if isinstance(plugins, dict) and isinstance(enabled, dict) and len(plugins) <= 128:
            for name, rows in plugins.items():
                if not isinstance(name, str) or not name.startswith('memvane@') or enabled.get(name) is not True:
                    continue
                if not isinstance(rows, list) or len(rows) > 128:
                    continue
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    item = _candidate(claude, name[len('memvane@'):], row.get('version'), 'claude')
                    if item is not None and row.get('installPath') == str(item[1]):
                        found.append(item)
    except FileNotFoundError:
        pass
    except (OSError, ValueError, KeyError, TypeError, AttributeError, UnicodeError):
        _inventory_unavailable('claude')
    return max(found, key=lambda item: (item[0], str(item[1])))[1] if found else None


def background_installed():
    """Refresh host evidence on file changes or after a one-hour backoff."""
    now = time.monotonic()
    stamp = _inventory_stamp()
    if now >= _BACKGROUND_INVENTORY['until'] or stamp != _BACKGROUND_INVENTORY.get('stamp'):
        try:
            root = latest_installed()
        except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, subprocess.TimeoutExpired):
            root = None
        _BACKGROUND_INVENTORY.update(root=root, stamp=stamp, until=time.monotonic() + 3600)
    return _BACKGROUND_INVENTORY['root']


def _inventory_stamp():
    home = Path.home()
    codex = Path(os.environ.get('CODEX_HOME') or home / '.codex')
    claude = Path(os.environ.get('CLAUDE_CONFIG_DIR') or home / '.claude')
    hermes = Path(os.environ.get('HERMES_HOME') or home / '.hermes')
    paths = [codex / 'config.toml', claude / 'settings.json',
             claude / 'plugins' / 'installed_plugins.json',
             hermes / 'config.yaml', hermes / 'plugins' / 'memvane' / 'plugin.json']
    try:
        paths.extend(codex / 'plugins' / 'cache' / market / 'memvane'
                     for market in _codex_enabled_markets(codex))
    except (OSError, ValueError, TypeError, UnicodeError):
        pass
    result = []
    for path in paths:
        try:
            result.append((str(path), _identity(path)))
        except (OSError, ValueError):
            result.append((str(path), None))
    return tuple(result)


def entry_argv(name, *args, python=None, isolated=False, background=False):
    """Spawn from the newest known bundle, without host CLI calls on hooks."""
    from memvane.kernel.bundle_paths import plugin_root
    own = plugin_root()
    root = (None if os.environ.get('MEMVANE_TEST_RUNTIME_ROOT') else
            background_installed() if background else fast_installed())
    if root is not None:
        own_identity = BundleIdentity(own)
        other = BundleIdentity(root)
        if not background and own_identity.intact() and (other.version is None or
                (own_identity.version is not None and own_identity.version >= other.version)):
            root = own
    else:
        root = own
        try:
            from memvane.diagnostics import api as diagnostics
            diagnostics.emit('runtime.bundle', outcome='fallback', reason='inventory_unavailable')
        except Exception:
            pass
    if not BundleIdentity(root).intact():
        raise ValueError('installed_bundle_unavailable')
    return [python or sys.executable, *(['-I'] if isolated else []), '-B',
            str(root / 'entry' / 'run.py'), name, *args]


def _process_rows(pid=None):
    """Bounded process metadata; the start time guards against PID reuse."""
    args = ['/bin/ps', '-p', str(pid)] if pid is not None else ['/bin/ps', '-ax']
    result = subprocess.run([*args, '-o', 'pid=,uid=,lstart=,command='],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=3, check=False)
    if result.returncode or len(result.stdout) > 2_000_000:
        return []
    rows = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 7)
        if len(parts) != 8:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), ' '.join(parts[2:7]), parts[7]))
        except ValueError:
            continue
    return rows


def process_version_counts():
    """Best-effort same-user process counts; no process state is modified."""
    import shlex
    try:
        result = subprocess.run(['/bin/ps', '-ax', '-o', 'uid=,command='], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=3, check=False)
        if result.returncode or len(result.stdout) > 2_000_000:
            return {'status': 'unavailable', 'versions': {}}
    except (OSError, subprocess.TimeoutExpired):
        return {'status': 'unavailable', 'versions': {}}
    counts = {}
    bases = [Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex'),
             Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home()/'.claude')]
    hermes = Path(os.environ.get('HERMES_HOME') or Path.home()/'.hermes') / 'plugins' / 'memvane'
    for line in result.stdout.splitlines():
        try:
            uid, command = line.strip().split(None, 1)
            if int(uid) != os.getuid():
                continue
            argv = shlex.split(command)
            for index, value in enumerate(argv):
                path = Path(value)
                if path.name == 'run.py' and path.parent.name == 'entry':
                    if index + 1 >= len(argv) or argv[index + 1] not in ('runtime-work', 'runtime-monitor', 'resident-service'):
                        continue
                    root = path.parent.parent
                elif path.name == 'laya_local_embed_worker.mjs' and len(path.parents) >= 4:
                    root = path.parents[3]
                else:
                    continue
                if not path.is_absolute() or not (root == hermes or any(
                        len(root.parts) == len(base.parts) + 5 and root.parts[:len(base.parts)] == base.parts
                        and root.parts[len(base.parts):len(base.parts)+2] == ('plugins', 'cache')
                        and root.parts[-2] == 'memvane' for base in bases)):
                    continue
                _safe_node(root, directory=True)
                manifest = _read_json(root / 'plugin.json', limit=65536)
                version = manifest.get('version')
                if manifest.get('name') == 'memvane' and version_key(version) is not None:
                    counts[version] = counts.get(version, 0) + 1
                break
        except (OSError, ValueError, AttributeError, TypeError, UnicodeError):
            continue
    return {'status': 'inspected', 'versions': counts}


def _holds_path(pid, fd, expected):
    """Prove the process has the named lock file open before signalling it."""
    try:
        target = expected.stat()
        proc_fd = Path('/proc') / str(pid) / 'fd' / str(fd)
        if proc_fd.exists():
            observed = proc_fd.stat()
            return (observed.st_dev, observed.st_ino) == (target.st_dev, target.st_ino)
        child = subprocess.run(['/usr/sbin/lsof', '-n', '-a', '-p', str(pid), '-d', str(fd), '-Fn'],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=3, check=False)
        if child.returncode or len(child.stdout) > 65536:
            return False
        return any(line == 'n' + str(expected) for line in child.stdout.splitlines())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def _old_helper(row, queue_root, current):
    import shlex
    pid, uid, _started, command = row
    if uid != os.getuid() or pid == os.getpid():
        return None
    try:
        args = shlex.split(command)
        if not args or not Path(args.pop(0)).name.lower().startswith('python'):
            return None
        while args and args[0] in ('-B', '-I'):
            args.pop(0)
        entry = Path(args.pop(0))
        if not entry.is_absolute() or entry.name != 'run.py' or entry.parent.name != 'entry':
            return None
        old = entry.parent.parent
        roots = [(Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home()/'.claude'), 'claude'),
                 (Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex'), 'codex')]
        proven = False
        for base, host in roots:
            try:
                relative = old.relative_to(base)
                parts = relative.parts
                if len(parts) != 5 or parts[:2] != ('plugins', 'cache') or parts[3] != 'memvane':
                    continue
                candidate = _candidate(base, parts[2], parts[4], host)
                if candidate is not None and candidate[1] == old and getattr(current, 'version', None) is not None and candidate[0] < current.version:
                    proven = True
            except (ValueError, OSError):
                continue
        if not proven:
            return None
        if len(args) == 3 and args[:2] == ['runtime-work', 'timer'] and args[2].isdigit():
            return int(args[2]), queue_root / 'runtime-wake-timer.lock'
        if (len(args) == 4 and args[0] == 'runtime-monitor' and Path(args[1]) == queue_root
                and args[2].isdigit() and args[3].isdigit()):
            return int(args[2]), queue_root / 'runtime-monitor.lock'
    except (ValueError, IndexError, TypeError):
        pass
    return None


def retire_old_helpers(queue_root, current, *, background=False):
    """Ask only proved older timer/monitor owners to exit; never signal workers."""
    import signal
    queue_root = Path(queue_root)
    preferred = getattr(current, 'preferred', None)
    if not current.intact() or not callable(preferred) or not preferred(background=background):
        return 0
    try:
        chosen = background_installed() if background else fast_installed()
        if chosen is None:
            return 0
        newest = version_key(_read_json(chosen / 'plugin.json', limit=65536).get('version'))
        if newest is None or getattr(current, 'version', None) is None or current.version != newest:
            return 0
    except (OSError, ValueError, TypeError, AttributeError, UnicodeError):
        return 0
    key = (str(queue_root), getattr(current, 'version', None))
    now = time.monotonic()
    if now - _RETIRE_CHECKS.get(key, -60) < 30:
        return 0
    _RETIRE_CHECKS.clear()
    _RETIRE_CHECKS[key] = now
    try:
        rows = _process_rows()
    except (OSError, subprocess.TimeoutExpired):
        return 0
    stopped = 0
    for row in rows:
        target = _old_helper(row, queue_root, current)
        if target is None or not _holds_path(row[0], *target):
            continue
        try:
            if _process_rows(row[0]) != [row] or not _holds_path(row[0], *target):
                continue
            os.kill(row[0], signal.SIGTERM)
            stopped += target[1].name == 'runtime-wake-timer.lock'
        except (OSError, subprocess.TimeoutExpired):
            continue
    return stopped

"""Find the host plugin roots used by learning authorization."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time

_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
HOST_FILES = tuple(sorted({str(path.relative_to(_PLUGIN_ROOT)) for pattern in
                           ('memvane/**/*.py', 'memvane/**/*.mjs', 'entry/*.py',
                            'hooks/run-hook', 'hooks/*.json',
                            '.claude-plugin/*.json', '.codex-plugin/*.json')
                           for path in _PLUGIN_ROOT.glob(pattern) if path.is_file()}))
_SAFE_SEGMENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}')
_CLI_ENV = ('PATH', 'LANG', 'LC_ALL', 'TMPDIR', 'USER', 'LOGNAME')

# A host CLI's ``plugin list --json`` can take about a minute (a bundled Codex
# refreshes its marketplaces first). Each listing is recorded per host, kind
# and project while the host's installation records keep their stat stamp, so
# the CLI runs only when they change. Selector maintenance (``refresh``) is the
# one caller that waits for a slow CLI (INVENTORY_SECONDS); after a failure it
# uses a listing recorded within STALE_SECONDS or defers
# (``host_inventory_unavailable``). Every other caller reads the record, or a
# record within STALE_SECONDS whose records moved, and runs the CLI with its
# earlier short bound only when nothing is recorded; a failure there keeps its
# earlier error. Every listing is still checked against the installation
# records by the selection that reads it.
INVENTORY_SECONDS = 120
PROJECT_LISTING_SECONDS = 10
STALE_SECONDS = 7 * 24 * 3600
CACHE_SCHEMA = 'memvane-host-inventory-v1'
_MAX_CACHE_BYTES = 2 * 1024 * 1024
# A failed run is not repeated for SETTLE_SECONDS while the records keep their
# stamp, so one pass waits for a slow CLI at most once per listing.
SETTLE_SECONDS = 600
_NOTES = []
_SETTLED = {}
# Set by ``refresh``: this process is selector maintenance.
_MODE = {'maintenance': False, 'refreshing': False}

def _owner_json(path, reason):
    path = Path(path)
    info = path.stat()
    if (any(part.is_symlink() for part in (path, *path.parents))
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid() or info.st_nlink != 1):
        raise ValueError(reason)
    return json.loads(path.read_text())

def _same_host_files(left, right):
    for name in HOST_FILES:
        a, b = Path(left) / name, Path(right) / name
        if (not a.is_file() or a.is_symlink() or not b.is_file() or b.is_symlink()
                or hashlib.sha256(a.read_bytes()).digest()
                != hashlib.sha256(b.read_bytes()).digest()):
            return False
    return True

def _claude_load_roots(account):
    """The directories Claude actually executes for each installed qmd plugin.

    Claude loads a plugin from a ``directory``-source marketplace in place:
    the session's plugin path is the marketplace's plugin folder, not the
    cache copy named by ``installPath``. That folder is derived only from
    Claude's own records (installed_plugins -> known_marketplaces ->
    marketplace.json) and must match the installed version exactly, stay
    inside the marketplace, and equal the cache copy byte for byte. Any other
    source loads from ``installPath`` as before.
    """
    plugins = Path(account) / '.claude/plugins'
    claude = _claude_inventory(plugins)
    known = _known_loader(plugins)
    roots = []
    for key, values in claude['plugins'].items():
        if not key.startswith('memvane@'):
            continue
        marketplace = key.split('@', 1)[1]
        for item in values:
            roots.append(_claude_load_root(plugins, marketplace, item, known))
    return roots

def _claude_inventory(plugins):
    claude = _owner_json(plugins / 'installed_plugins.json',
                         'history_claude_inventory_unsafe')
    if not isinstance(claude, dict) or not isinstance(claude.get('plugins'), dict):
        raise ValueError('history_claude_inventory_unsafe')
    return claude

def _known_loader(plugins):
    """Read known_marketplaces.json lazily, at most once per inventory pass."""
    known = []

    def load():
        if not known:
            known.append(_owner_json(plugins / 'known_marketplaces.json',
                                     'history_claude_inventory_unsafe'))
        return known[0]
    return load

def _claude_load_root(plugins, marketplace, item, known):
    """The directory Claude executes for one installed_plugins.json record."""
    install = Path(item['installPath'])
    # Whatever Claude runs, its install record must be a cache copy;
    # only the directory branch below may name a folder outside it.
    if not install.is_absolute() or plugins / 'cache' not in install.parents:
        raise ValueError('history_claude_inventory_unsafe')
    source = known()[marketplace]['source']
    if not isinstance(source, dict):
        raise ValueError('history_claude_marketplace_invalid')
    if source.get('source') != 'directory':
        return install
    base = Path(source['path'])
    if not base.is_absolute():
        raise ValueError('history_claude_marketplace_invalid')
    manifest = _owner_json(base / '.claude-plugin/marketplace.json',
                           'history_claude_marketplace_invalid')
    if not isinstance(manifest, dict) or not isinstance(manifest.get('plugins'), list):
        raise ValueError('history_claude_marketplace_invalid')
    entries = [row for row in manifest['plugins']
               if isinstance(row, dict) and row.get('name') == 'memvane']
    if len(entries) != 1 or entries[0].get('version') != item['version']:
        raise ValueError('history_claude_marketplace_changed')
    relative = entries[0].get('source')
    if (not isinstance(relative, str) or not relative.startswith('./')
            or '..' in Path(relative).parts):
        raise ValueError('history_claude_marketplace_invalid')
    load = base / relative
    if (any(part.is_symlink() for part in (load, *load.parents))
            or not load.is_dir()
            or base.resolve() not in load.resolve().parents
            or load.stat().st_uid != os.getuid()):
        raise ValueError('history_claude_marketplace_invalid')
    load = load.resolve()
    # The cache copy is what Claude would fall back to; it must not
    # differ from the code that actually runs.
    if not _same_host_files(load, install):
        raise ValueError('history_claude_cache_differs')
    return load

def _discover_host_roots(account):
    """Use host installation records, not arbitrary cached plugin folders."""
    try:
        listed = subprocess.run(('codex', 'plugin', 'list', '--json'),
                                capture_output=True, text=True, timeout=10,
                                check=False)
        if listed.returncode != 0:
            raise ValueError('history_codex_inventory_unavailable')
        codex = json.loads(listed.stdout)
        entries = [item for item in codex['installed']
                   if item.get('name') == 'memvane'
                   and item.get('installed') is True
                   and item.get('enabled') is True]
        codex_roots = [account / '.codex/plugins/cache' / item['marketplaceName']
                       / 'memvane' / item['version'] for item in entries]
        claude_roots = _claude_load_roots(account)
    except (OSError, KeyError, TypeError, ValueError, AttributeError,
            subprocess.TimeoutExpired) as exc:
        raise ValueError('history_host_inventory_unavailable') from exc
    return {'codex': codex_roots, 'claude': claude_roots}

def _safe_project_cli(argv, project):
    """Resolve host CLIs outside project-controlled PATH entries.

    The child inherits this same PATH, so an env-based script interpreter
    cannot resolve a project-planted ``node`` or shell either.
    """
    safe = []
    for raw in os.environ.get('PATH', '').split(os.pathsep):
        if not raw or not Path(raw).is_absolute():
            continue
        try:
            directory = Path(raw).resolve(strict=True)
            info = directory.stat()
        except OSError:
            continue
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid())
                or info.st_mode & 0o022 or directory == project
                or project in directory.parents):
            continue
        if str(directory) not in safe:
            safe.append(str(directory))
    path = os.pathsep.join(safe)
    found = shutil.which(argv[0], path=path) if path else None
    if found is None:
        raise ValueError('history_project_cli_unavailable')
    executable = Path(found).resolve(strict=True)
    info = executable.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
            or info.st_mode & 0o022 or executable == project
            or project in executable.parents):
        raise ValueError('history_project_cli_unsafe')
    return (str(executable), *argv[1:]), path

def _record_paths(host, account, project):
    """The installation records a host's listing is read from (``project`` may be None)."""
    if host == 'codex':
        codex = account / '.codex'
        paths = [codex / 'config.toml']
        if project is not None:
            paths.append(project / '.codex' / 'config.toml')
        cache = codex / 'plugins' / 'cache'
        try:
            markets = sorted(cache.iterdir()) if cache.is_dir() and not cache.is_symlink() else []
        except OSError:
            markets = []
        for market in markets:
            plugin = market / 'memvane'
            if plugin.is_dir() and not plugin.is_symlink():
                paths.append(plugin)
                paths.extend(sorted(plugin.iterdir()))
        return paths
    claude = account / '.claude'
    paths = [claude / 'settings.json', claude / 'settings.local.json',
             claude / 'plugins' / 'installed_plugins.json',
             claude / 'plugins' / 'known_marketplaces.json']
    if project is not None:
        paths += [project / '.claude' / 'settings.json', project / '.claude' / 'settings.local.json']
    return paths

def _records_stamp(host, account, project, executable):
    """Stat stamp of the CLI and its records; no device number (it moves per boot)."""
    rows = []
    for path in (Path(executable), *_record_paths(host, account, project)):
        try:
            info = os.lstat(path)
            rows.append([str(path), info.st_ino, info.st_size, info.st_mtime_ns,
                         info.st_ctime_ns, info.st_mode])
        except FileNotFoundError:
            rows.append([str(path), None])
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()

def _cache_path(account, host, kind, project):
    from .runtime_setup import learning_state_base
    key = hashlib.sha256((host + '\0' + kind + '\0' + str(project)).encode()).hexdigest()[:32]
    return learning_state_base(account) / 'host-inventory' / (key + '.json')

def _read_cache(account, host, kind, project):
    """The recorded listing for this host, kind and project, or None (never raises)."""
    path = _cache_path(account, host, kind, project)
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or info.st_mode & 0o077 or info.st_size > _MAX_CACHE_BYTES):
            return None
        row = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if (not isinstance(row, dict) or row.get('schema') != CACHE_SCHEMA or row.get('host') != host
            or row.get('kind') != kind or row.get('project') != str(project) or not isinstance(row.get('stamp'), str)
            or not isinstance(row.get('executable'), str)
            or not isinstance(row.get('listing'), (dict, list))
            or type(row.get('recordedAt')) not in (int, float)):
        return None
    return row

def _write_cache(account, host, kind, project, row):
    """Best effort: a missing record only costs another CLI run later."""
    path = _cache_path(account, host, kind, project)
    try:
        os.makedirs(path.parent, mode=0o700, exist_ok=True)
        info = path.parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            return
        temporary = path.with_name('.%s.%d.tmp' % (path.name, os.getpid()))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'w') as out:
                json.dump(row, out, sort_keys=True)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)
    except (OSError, ValueError, TypeError):
        pass

def take_notes():
    """``(host, outcome)`` of listings refreshed, stale or unavailable since the last call."""
    notes = list(_NOTES)
    del _NOTES[:]
    return notes

def refresh(project, hosts=('claude', 'codex')):
    """Settle each host's listings before bounded preparation steps.

    Selector maintenance calls this first, outside any short preparation
    budget: a CLI whose records changed runs here, at most once per listing
    and process, for up to INVENTORY_SECONDS. From then on this process
    defers a step whose listing is unavailable. Returns the notes.
    """
    account, project = Path.home().resolve(), Path(project).resolve(strict=True)
    _MODE['maintenance'] = _MODE['refreshing'] = True
    try:
        for host in hosts:
            try:
                _project_listing((host, 'plugin', 'list', '--json'), account, project)
            except Exception:
                pass  # Recorded in the notes; the step that needs it reports it.
        if 'codex' in hosts:
            try:
                from .adapter_binding import _codex_installed
                _codex_installed(account, None)
            except Exception:
                pass
    finally:
        _MODE['refreshing'] = False
    return take_notes()

def _unavailable():
    if _MODE['maintenance']:
        from .selector_heal import HealDeferred
        return HealDeferred('host_inventory_unavailable')
    return ValueError('history_project_inventory_unavailable')

def recorded(host, kind, account, project, executable, run, *, short_seconds):
    """A host listing, from its record when the installation records allow.

    ``run(timeout)`` runs the CLI once and returns its parsed JSON (an object
    or a list). Outside ``refresh`` a missing record runs it for
    ``short_seconds`` (the caller's earlier bound) and a failure re-raises
    the CLI's own error; in maintenance a failure defers.
    """
    key = (host, kind, str(account), str(project))
    stamp = _records_stamp(host, account, project, executable)
    settled = _SETTLED.get(key)
    if (settled is not None and settled[1] == stamp
            and 0 <= time.monotonic() - settled[0] <= SETTLE_SECONDS):
        if settled[2] is None:
            raise _unavailable()
        return settled[2]
    cached = _read_cache(account, host, kind, project)
    if cached is not None and cached['executable'] != executable:
        cached = None
    if cached is not None and cached['stamp'] == stamp:
        return cached['listing']
    recent = cached is not None and 0 <= time.time() - cached['recordedAt'] <= STALE_SECONDS
    if recent and not _MODE['refreshing']:
        # Only selector maintenance waits for a CLI whose records moved.
        _NOTES.append((host, 'stale'))
        _SETTLED[key] = (time.monotonic(), stamp, cached['listing'])
        return cached['listing']
    try:
        value = run(INVENTORY_SECONDS if _MODE['refreshing'] else short_seconds)
        # Claude prints a list of installed plugins; Codex an object.
        if not isinstance(value, (dict, list)):
            raise ValueError('history_project_inventory_unavailable')
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        if recent:
            _NOTES.append((host, 'stale'))
            _SETTLED[key] = (time.monotonic(), stamp, cached['listing'])
            return cached['listing']
        _NOTES.append((host, 'unavailable'))
        _SETTLED[key] = (time.monotonic(), stamp, None)
        if _MODE['maintenance']:
            raise _unavailable() from exc
        raise  # Outside maintenance a caller keeps its earlier error.
    if _records_stamp(host, account, project, executable) == stamp:
        _write_cache(account, host, kind, project, {
            'schema': CACHE_SCHEMA, 'host': host, 'kind': kind, 'project': str(project),
            'executable': executable, 'stamp': stamp, 'recordedAt': time.time(), 'listing': value})
    _NOTES.append((host, 'refreshed'))
    _SETTLED.pop(key, None)
    return value

def _project_listing(argv, account, project):
    """One official read-only ``plugin list --json`` as seen from ``project``.

    The CLI must read the same account records that are verified afterwards,
    so a redirected config home fails closed instead of being dropped.
    """
    for name, default in (('CLAUDE_CONFIG_DIR', account / '.claude'),
                          ('CODEX_HOME', account / '.codex')):
        value = os.environ.get(name)
        if value and Path(value) != default:
            raise ValueError('history_project_inventory_redirected')
    command, path = _safe_project_cli(argv, project)
    env = {key: os.environ[key] for key in _CLI_ENV if key in os.environ}
    env['PATH'] = path
    env.update(HOME=str(account), QMD_SANDBOX='1', PYTHONDONTWRITEBYTECODE='1')

    def run(timeout):
        listed = subprocess.run(command, cwd=str(project), env=env,
                                stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=timeout, check=False)
        if listed.returncode != 0 or len(listed.stdout) > 1024 * 1024:
            raise ValueError('history_project_inventory_unavailable')
        return json.loads(listed.stdout)
    return recorded(Path(argv[0]).name, 'project', account, project, command[0], run,
                    short_seconds=PROJECT_LISTING_SECONDS)

def _same_directory(raw, project, *, strict=True):
    """Directory identity, not spelling: APFS folds case and Unicode form, so
    two different strings can name one project. A recorded path that cannot be
    examined is never assumed to be another project (strict); as a mere
    candidate record it simply does not match (strict=False)."""
    try:
        return os.path.samestat(os.stat(raw), os.stat(project))
    except (OSError, TypeError, ValueError) as exc:
        if not strict:
            return False
        raise ValueError('history_project_claude_record_changed') from exc

def _project_claude_selection(account, project):
    value = _project_listing(('claude', 'plugin', 'list', '--json'), account, project)
    if isinstance(value, dict):
        value = value.get('plugins', value.get('installed'))
    if not isinstance(value, list):
        raise ValueError('history_project_claude_invalid')
    enabled = []
    foreign = []
    for row in value:
        ident = row.get('id', row.get('pluginId')) if isinstance(row, dict) else None
        if not isinstance(ident, str):
            raise ValueError('history_project_claude_invalid')
        if not ident.startswith('memvane@'):
            continue
        if type(row.get('enabled')) is not bool:
            raise ValueError('history_project_claude_invalid')
        if not row['enabled']:
            continue
        # The listing is account-wide: a project/local install belongs only to
        # its own projectPath, which the host never loads for this project.
        # A row without a project binding stays a candidate that the account
        # record below must match exactly, as before.
        scope, recorded = row.get('scope'), row.get('projectPath')
        if recorded is None and scope in (None, 'user'):
            enabled.append(row)
        elif scope in ('project', 'local') and isinstance(recorded, str) and recorded:
            (enabled if _same_directory(recorded, project) else foreign).append(row)
        else:
            raise ValueError('history_project_claude_invalid')
    # Never choose among enabled roots: the host would run each of them.
    if len(enabled) != 1:
        raise ValueError('history_project_claude_ambiguous')
    row = enabled[0]
    ident, version, install = row.get('id', row.get('pluginId')), row.get('version'), row.get('installPath')
    marketplace = ident.split('@', 1)[1]
    if (not _SAFE_SEGMENT.fullmatch(marketplace) or not isinstance(version, str)
            or not isinstance(install, str)):
        raise ValueError('history_project_claude_invalid')
    plugins = account / '.claude/plugins'
    inventory = _claude_inventory(plugins)['plugins']
    for other in foreign:
        # A row is skipped only when an account record really binds that same
        # install to its other project; an unrecorded claim fails closed.
        items = inventory.get(other.get('id', other.get('pluginId')))
        if not isinstance(items, list) or not any(
                isinstance(item, dict) and item.get('scope') == other.get('scope')
                and item.get('installPath') == other.get('installPath')
                and isinstance(item.get('projectPath'), str)
                and _same_directory(item['projectPath'], other['projectPath'], strict=False)
                for item in items):
            raise ValueError('history_project_claude_record_changed')
    records = inventory.get(ident)
    if not isinstance(records, list):
        raise ValueError('history_project_claude_record_missing')
    matches = []
    for item in records:
        if (not isinstance(item, dict) or item.get('version') != version
                or item.get('installPath') != install
                or any(key in row and item.get(key) != row[key]
                       for key in ('scope', 'projectPath'))):
            continue
        # A project-bound record must name this project, by resolved path.
        recorded = item.get('projectPath')
        if recorded is not None and (not isinstance(recorded, str)
                                     or not _same_directory(recorded, project)):
            raise ValueError('history_project_claude_record_changed')
        matches.append(item)
    if len(matches) != 1:
        raise ValueError('history_project_claude_record_missing')
    # Only the effective record is resolved; other marketplaces stay unread.
    known = _known_loader(plugins)
    root = _claude_load_root(plugins, marketplace, matches[0], known)
    return root, marketplace, matches[0], known()[marketplace]

def _project_claude_root(account, project):
    return _project_claude_selection(account, project)[0]

def _project_codex_selection(account, project):
    value = _project_listing(('codex', 'plugin', 'list', '--json'), account, project)
    rows = value.get('installed') if isinstance(value, dict) else None
    if not isinstance(rows, list):
        raise ValueError('history_project_codex_invalid')
    enabled = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('history_project_codex_invalid')
        if row.get('name') != 'memvane' or row.get('installed') is not True:
            continue
        if type(row.get('enabled')) is not bool:
            raise ValueError('history_project_codex_invalid')
        if row['enabled']:
            enabled.append(row)
    if len(enabled) != 1:
        raise ValueError('history_project_codex_ambiguous')
    marketplace, version = enabled[0].get('marketplaceName'), enabled[0].get('version')
    if not all(isinstance(part, str) and _SAFE_SEGMENT.fullmatch(part)
               for part in (marketplace, version)):
        raise ValueError('history_project_codex_invalid')
    root = account / '.codex/plugins/cache' / marketplace / 'memvane' / version
    if not root.is_dir():
        raise ValueError('history_project_codex_record_missing')
    return root, marketplace, enabled[0], enabled[0].get('marketplaceSource')

def _project_codex_root(account, project):
    return _project_codex_selection(account, project)[0]

def _project_host_roots(account, project):
    """Exactly one enabled qmd root per host, as each CLI resolves it in
    ``project``. Unlike ``_discover_host_roots`` this ignores disabled
    historical installs, but every enabled entry still counts and is never
    narrowed by matching against expected code."""
    account, project = Path(account), Path(project)
    try:
        return {'claude': [_project_claude_root(account, project)],
                'codex': [_project_codex_root(account, project)]}
    except (OSError, KeyError, TypeError, AttributeError,
            subprocess.TimeoutExpired) as exc:
        raise ValueError('history_project_inventory_unavailable') from exc

"""Find the host plugin roots used by learning authorization."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess

_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
HOST_FILES = tuple(sorted({str(path.relative_to(_PLUGIN_ROOT)) for pattern in
                           ('memvane/**/*.py', 'memvane/**/*.mjs', 'entry/*.py',
                            'hooks/run-hook', 'hooks/*.json',
                            '.claude-plugin/*.json', '.codex-plugin/*.json')
                           for path in _PLUGIN_ROOT.glob(pattern) if path.is_file()}))
_SAFE_SEGMENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}')
_CLI_ENV = ('PATH', 'LANG', 'LC_ALL', 'TMPDIR', 'USER', 'LOGNAME')

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
    listed = subprocess.run(command, cwd=str(project), env=env,
                            stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=10, check=False)
    if listed.returncode != 0 or len(listed.stdout) > 1024 * 1024:
        raise ValueError('history_project_inventory_unavailable')
    return json.loads(listed.stdout)

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

def _project_claude_root(account, project):
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
    return _claude_load_root(plugins, marketplace, matches[0], _known_loader(plugins))

def _project_codex_root(account, project):
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
    return root

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

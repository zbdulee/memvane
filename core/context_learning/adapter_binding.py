"""Read-only compatibility for the same adapter in official host/version caches.

The policy's old adapter is an identity anchor, never an execution target.
Only the current package adapter is returned, and its normal trainer/runtime
checks still apply. No policy, installation record or authorization is written.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time

MARKETPLACE = 'qmd-auto-context-marketplace'
PLUGIN_ID = 'qmd-auto-context@' + MARKETPLACE
REPOSITORY = 'zbdulee/qmd-auto-context'
REPOSITORY_URLS = frozenset({'https://github.com/' + REPOSITORY,
                            'https://github.com/' + REPOSITORY + '.git'})


class BindingTimeout(TimeoutError):
    """The caller's selector budget cannot accommodate another operation."""


def _inventory_timeout(deadline):
    remaining = deadline - time.monotonic() if deadline is not None else 2
    # choose() already requires at least a second for the prediction itself.
    if remaining < 1:
        raise BindingTimeout('trainer_timeout')
    return min(2, remaining)


# Each admission checkpoint closes/reopens the shared compute gate and may
# sample the OS. A per-read checkpoint over a whole core/hooks inventory
# (four trees per project_scope binding check) spent the 1s foreground
# preparation budget on admission alone on Linux CI. Inventories instead pace
# checkpoints: one at start and end, and between them at most _PACE_STEPS
# entries/reads or _PACE_SECONDS, whichever comes first, so a resource stop or
# expired deadline is still observed within a bounded gap.
_PACE_STEPS = 64
_PACE_SECONDS = .05


class _Pacer:
    def __init__(self):
        from runtime_admission import preparation_checkpoint
        self._checkpoint = preparation_checkpoint
        self.checkpoint()

    def checkpoint(self):
        self._checkpoint()
        self.steps = 0
        self.since = time.monotonic()

    def __call__(self):
        self.steps += 1
        if self.steps >= _PACE_STEPS or time.monotonic() - self.since >= _PACE_SECONDS:
            self.checkpoint()


def _owned_bytes(path, home, limit, *, pace=None):
    path = Path(path)
    if not path.is_absolute() or home not in path.parents:
        raise ValueError('unsupported_adapter')
    for part in (path, *path.parents):
        info = part.lstat()
        if (stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022):
            raise ValueError('unsupported_adapter')
        if part != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError('unsupported_adapter')
        if part == home:
            break
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        before = os.fstat(source.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.getuid() or before.st_mode & 0o022
                or before.st_size > limit):
            raise ValueError('unsupported_adapter')
        if pace is None:
            from runtime_admission import preparation_checkpoint as pace
        parts=[];remaining=limit+1
        while remaining:
            pace()
            block=source.read(min(1024*1024,remaining))
            if not block:break
            parts.append(block);remaining-=len(block)
        content=b''.join(parts)
        after = os.fstat(source.fileno())
    identity = lambda item: (item.st_dev, item.st_ino, item.st_size,
                              item.st_mtime_ns, item.st_ctime_ns)
    if len(content) > limit or identity(before) != identity(after):
        raise ValueError('unsupported_adapter')
    return content


def _owner_json(path, home):
    return json.loads(_owned_bytes(path, home, 1024 * 1024))


def _official_cache(path, home):
    parts = path.relative_to(home).parts
    if (len(parts) != 9 or parts[0] not in ('.claude', '.codex')
            or parts[1:5] != ('plugins', 'cache', MARKETPLACE, 'qmd-auto-context')
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', parts[5])
            or parts[6:] != ('core', 'context_learning', 'laya_adapter.py')):
        raise ValueError('unsupported_adapter')
    host, version = parts[0][1:], parts[5]
    root = home.joinpath(*parts[:6])
    content = _owned_bytes(path, home, 256 * 1024)
    for relative in ('.'+host+'-plugin/plugin.json', 'package.json'):
        metadata = _owner_json(root / relative, home)
        if (not isinstance(metadata, dict) or metadata.get('name') != 'qmd-auto-context'
                or metadata.get('version') != version):
            raise ValueError('unsupported_adapter')
    return host, version, root, hashlib.sha256(content).digest()


def _official_source(source):
    return isinstance(source, dict) and (
        (source.get('source') == 'github' and source.get('repo') == REPOSITORY)
        or (source.get('source') == 'git' and source.get('url') in REPOSITORY_URLS))


def _installed_current(host, version, root, home, deadline):
    if host == 'claude':
        base = home / '.claude/plugins'
        known = _owner_json(base / 'known_marketplaces.json', home)
        inventory = _owner_json(base / 'installed_plugins.json', home)
        if not _official_source(known[MARKETPLACE]['source']):
            raise ValueError('unsupported_adapter')
        entries = inventory['plugins'].get(PLUGIN_ID, [])
        if (not isinstance(entries, list) or not entries
                or not all(isinstance(item, dict) and item.get('version') == version
                           and item.get('installPath') == str(root) for item in entries)):
            raise ValueError('unsupported_adapter')
        return
    item = _codex_installed(home, deadline)
    if not (item.get('name') == 'qmd-auto-context'
               and item.get('marketplaceName') == MARKETPLACE
               and item.get('version') == version
               and item.get('installed') is True and item.get('enabled') is True
               and _official_source(item.get('source'))):
        raise ValueError('unsupported_adapter')


def _codex_installed(home, deadline):
    codex_home = os.environ.get('CODEX_HOME')
    if codex_home and Path(codex_home) != home / '.codex':
        raise ValueError('unsupported_adapter')
    # Existing CLI, inventory command only. Do not inherit provider API keys or
    # invoke an exec/model session. The nested hook guard prevents recursion.
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'TMPDIR')
           if key in os.environ}
    env.update(HOME=str(home), CODEX_HOME=str(home / '.codex'),
               QMD_SANDBOX='1', PYTHONDONTWRITEBYTECODE='1')
    listed = subprocess.run(['codex', 'plugin', 'list', '--marketplace',
                             MARKETPLACE, '--json'], env=env,
                            stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=_inventory_timeout(deadline), check=False)
    if listed.returncode or len(listed.stdout) > 1024 * 1024:
        raise ValueError('unsupported_adapter')
    entries = json.loads(listed.stdout)['installed']
    matches = [item for item in entries if isinstance(item, dict)
               and item.get('pluginId') == PLUGIN_ID]
    if len(matches) != 1:
        raise ValueError('unsupported_adapter')
    return matches[0]


def _local_catalog(market, host, home):
    """Accept the one explicit local plugin entry, never a remote URL fallback."""
    name = '.claude-plugin/marketplace.json' if host == 'claude' else '.agents/plugins/marketplace.json'
    catalog = _owner_json(market / name, home)
    if not isinstance(catalog, dict) or catalog.get('name') != MARKETPLACE:
        raise ValueError('unsupported_adapter')
    rows = catalog.get('plugins')
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError('unsupported_adapter')
    rows = [row for row in rows if row.get('name') == 'qmd-auto-context']
    if len(rows) != 1:
        raise ValueError('unsupported_adapter')
    row = rows[0]
    source = row.get('source')
    expected = './plugins/qmd-auto-context'
    if host == 'codex':
        if not isinstance(source, dict) or source != {'source': 'local', 'path': expected}:
            raise ValueError('unsupported_adapter')
    elif source != expected:
        raise ValueError('unsupported_adapter')
    root = market / 'plugins/qmd-auto-context'
    version = row.get('version')
    if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', version):
        raise ValueError('unsupported_adapter')
    for relative in ('package.json', '.claude-plugin/plugin.json', '.codex-plugin/plugin.json'):
        metadata = _owner_json(root / relative, home)
        if not isinstance(metadata, dict) or metadata.get('name') != 'qmd-auto-context' or metadata.get('version') != version:
            raise ValueError('unsupported_adapter')
    return root, version


def _local_code(root, home):
    """Compare executable core/hooks, with the same private path/file guards."""
    rows = []
    total = 0
    pace = _Pacer()
    for directory in ('core', 'hooks'):
        base = root / directory
        info = base.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022):
            raise ValueError('unsupported_adapter')
        for path in sorted(base.rglob('*')):
            pace()
            if '__pycache__' in path.relative_to(root).parts or path.suffix == '.pyc':
                continue
            info = path.lstat()
            if (stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o022):
                raise ValueError('unsupported_adapter')
            if stat.S_ISDIR(info.st_mode):
                continue
            data = _owned_bytes(path, home, 4 * 1024 * 1024, pace=pace)
            total += len(data)
            rows.append((str(path.relative_to(root)), hashlib.sha256(data).hexdigest()))
            if len(rows) > 4096 or total > 32 * 1024 * 1024:
                raise ValueError('unsupported_adapter')
    pace.checkpoint()
    if not any(name == 'core/context_learning/laya_adapter.py' for name, _ in rows):
        raise ValueError('unsupported_adapter')
    return tuple(rows)


def _local_binding(path, home, deadline, *, execution):
    """Attest an officially registered directory source and its installed copy.

    Claude executes the registered source directory. Codex executes its cache.
    An old Claude cache may be a policy anchor, but is never an execution target.
    """
    try:
        host, version, root, adapter_hash = _official_cache(path, home)
    except ValueError:
        if (not path.is_absolute() or home not in path.parents
                or path.parts[-3:] != ('core', 'context_learning', 'laya_adapter.py')):
            raise ValueError('unsupported_adapter')
        host = 'claude'
        root = path.parents[2]
        version = None
        adapter_hash = hashlib.sha256(_owned_bytes(path, home, 256 * 1024)).digest()
        if path != root / 'core/context_learning/laya_adapter.py':
            raise ValueError('unsupported_adapter')
    if host == 'claude':
        base = home / '.claude/plugins'
        known = _owner_json(base / 'known_marketplaces.json', home)[MARKETPLACE]
        origin = known['source']
        if not isinstance(origin, dict) or origin.get('source') != 'directory':
            raise ValueError('unsupported_adapter')
        market = Path(origin['path'])
        if known.get('installLocation') != str(market):
            raise ValueError('unsupported_adapter')
        source, current_version = _local_catalog(market, host, home)
        cache = home / '.claude/plugins/cache' / MARKETPLACE / 'qmd-auto-context' / current_version
        inventory = _owner_json(base / 'installed_plugins.json', home)
        entries = inventory['plugins'].get(PLUGIN_ID, [])
        if not isinstance(entries, list) or not entries or not all(
                isinstance(item, dict) and item.get('version') == current_version
                and item.get('installPath') == str(cache) for item in entries):
            raise ValueError('unsupported_adapter')
        if root not in (source, cache) or (execution and root != source):
            raise ValueError('unsupported_adapter')
    else:
        item = _codex_installed(home, deadline)
        origin = item.get('marketplaceSource')
        if not isinstance(origin, dict) or origin.get('sourceType') != 'local':
            raise ValueError('unsupported_adapter')
        market = Path(origin['source'])
        source, current_version = _local_catalog(market, host, home)
        cache = home / '.codex/plugins/cache' / MARKETPLACE / 'qmd-auto-context' / current_version
        if (root != cache or item.get('name') != 'qmd-auto-context'
                or item.get('marketplaceName') != MARKETPLACE
                or item.get('version') != current_version
                or item.get('installed') is not True or item.get('enabled') is not True
                or item.get('source') != {'source': 'local', 'path': str(source)}):
            raise ValueError('unsupported_adapter')
    if not market.is_absolute() or market != market.resolve() or (version is not None and version != current_version):
        raise ValueError('unsupported_adapter')
    # Both copies' manifests and every executable core/hook byte must agree.
    for relative in ('package.json', '.claude-plugin/plugin.json', '.codex-plugin/plugin.json'):
        if _owned_bytes(source / relative, home, 1024 * 1024) != _owned_bytes(cache / relative, home, 1024 * 1024):
            raise ValueError('unsupported_adapter')
    fingerprint = _local_code(source, home)
    if fingerprint != _local_code(cache, home):
        raise ValueError('unsupported_adapter')
    return adapter_hash, fingerprint


def inference_argv(argv, adapter, *, deadline=None):
    """Keep the exact adapter case, or prove an official identical-code move."""
    _inventory_timeout(deadline)
    current, configured = Path(adapter), Path(argv[1])
    if configured == current and not configured.is_symlink():
        return list(argv)
    home = Path.home()
    try:
        try:
            source = _official_cache(configured, home)
            target = _official_cache(current, home)
            _installed_current(*target[:3], home, deadline)
            if source[3] != target[3]:
                raise ValueError('unsupported_adapter')
        except ValueError:
            source = _local_binding(configured, home, deadline, execution=False)
            target = _local_binding(current, home, deadline, execution=True)
            if source != target:
                raise ValueError('unsupported_adapter')
    except BindingTimeout:
        raise
    except subprocess.TimeoutExpired as exc:
        if deadline is not None and time.monotonic() >= deadline:
            raise BindingTimeout('trainer_timeout') from exc
        raise ValueError('unsupported_adapter') from exc
    except (OSError, ValueError, KeyError, TypeError, AttributeError,
            UnicodeError, subprocess.SubprocessError) as exc:
        raise ValueError('unsupported_adapter') from exc
    return [argv[0], str(current), argv[2]]

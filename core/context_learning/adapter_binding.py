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


def _owned_bytes(path, home, limit):
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
        content = source.read(limit + 1)
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
    item = matches[0]
    if not (item.get('name') == 'qmd-auto-context'
               and item.get('marketplaceName') == MARKETPLACE
               and item.get('version') == version
               and item.get('installed') is True and item.get('enabled') is True
               and _official_source(item.get('source'))):
        raise ValueError('unsupported_adapter')


def inference_argv(argv, adapter, *, deadline=None):
    """Keep the exact adapter case, or prove an official identical-code move."""
    _inventory_timeout(deadline)
    current, configured = Path(adapter), Path(argv[1])
    if configured == current and not configured.is_symlink():
        return list(argv)
    home = Path.home()
    try:
        source = _official_cache(configured, home)
        target = _official_cache(current, home)
        _installed_current(*target[:3], home, deadline)
        if source[3] != target[3]:
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

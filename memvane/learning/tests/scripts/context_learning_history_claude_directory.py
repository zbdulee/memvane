"""Claude runs a directory-marketplace plugin in place, so the cutover
protocol must verify and accept acknowledgements from that folder, and only
from the folder Claude's own install records name.

HOME is a disposable directory, so the real-account code path runs without
touching the developer's actual plugin records.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from unittest.mock import patch

account = Path(tempfile.mkdtemp(prefix='qmd-claude-directory-', dir='/tmp')).resolve()
os.environ['HOME'] = str(account)
for name in ('QMD_SANDBOX', 'GEMINI_SANDBOX', 'CLAUDE_SANDBOX', 'CODEX_SANDBOX',
             'HERMES_SANDBOX', 'CLAUDE_HEADLESS', 'HERMES_HEADLESS'):
    os.environ.pop(name, None)

from memvane.learning.context_learning.host_inventory import (
    HOST_FILES, _claude_load_roots, _discover_host_roots)
import memvane.learning.context_learning.host_inventory as activation_module
from memvane.learning.tests.support import REPO


def rejects(action, reason):
    try:
        action()
    except ValueError as error:
        assert reason in str(error), str(error)
    else:
        raise AssertionError(f'expected {reason}')


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


try:
    source = REPO
    version = '9.9.9-dev-directory'
    marketplace = account / 'work/local-marketplace'
    loaded = marketplace / 'plugins/memvane-v9'
    cache = account / '.claude/plugins/cache/local-mkt/memvane' / version
    codex = account / '.codex/plugins/cache/local-mkt/memvane' / version
    for root in (loaded, cache, codex):
        for relative in HOST_FILES:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / relative, target)
    installed = account / '.claude/plugins/installed_plugins.json'
    known = account / '.claude/plugins/known_marketplaces.json'
    manifest = marketplace / '.claude-plugin/marketplace.json'

    def records(*, source_kind='directory', entry_version=version,
                relative='./plugins/memvane-v9', install_path=None):
        write_json(installed, {'version': 2, 'plugins': {'memvane@local-mkt': [
            {'scope': 'user', 'installPath': str(install_path or cache), 'version': version}]}})
        write_json(known, {'local-mkt': {'source': (
            {'source': 'directory', 'path': str(marketplace)} if source_kind == 'directory'
            else 'not-a-mapping' if source_kind == 'string'
            else {'source': 'github', 'repo': 'example/qmd'})}})
        write_json(manifest, {'name': 'local', 'plugins': [
            {'name': 'memvane', 'version': entry_version, 'source': relative}]})

    codex_listing = subprocess.CompletedProcess((), 0, json.dumps({'installed': [
        {'name': 'memvane', 'marketplaceName': 'local-mkt',
         'version': version, 'installed': True, 'enabled': True}]}), '')
    real_run = subprocess.run

    def fake_run(args, *rest, **kwargs):
        if tuple(args[:3]) == ('codex', 'plugin', 'list'):
            return codex_listing
        return real_run(args, *rest, **kwargs)

    patch.object(activation_module.subprocess, 'run', side_effect=fake_run).start()

    # 1. A directory marketplace resolves to the folder Claude actually runs.
    records()
    assert _claude_load_roots(account) == [loaded.resolve()]
    assert _discover_host_roots(account) == {'codex': [codex], 'claude': [loaded.resolve()]}
    # Other marketplace kinds still run from the cache install path.
    records(source_kind='github')
    assert _claude_load_roots(account) == [cache]
    records()

    # 2. Anything not exactly derived from Claude's records fails closed.
    # The escape targets exist and hold identical code, so only the path
    # rules (not a missing directory) can refuse them.
    outside = marketplace.parent / 'outside'
    shutil.copytree(loaded, outside)
    for kwargs, reason in [
            ({'entry_version': '9.9.8-other'}, 'history_claude_marketplace_changed'),
            ({'relative': '../outside'}, 'history_claude_marketplace_invalid'),
            ({'relative': './plugins/../../outside'}, 'history_claude_marketplace_invalid'),
            ({'relative': './../outside'}, 'history_claude_marketplace_invalid'),
            ({'relative': str(loaded)}, 'history_claude_marketplace_invalid'),
            ({'relative': './plugins/missing'}, 'history_claude_marketplace_invalid')]:
        records(**kwargs)
        rejects(lambda: _claude_load_roots(account), reason)
    records()
    alias = marketplace / 'plugins/alias'
    alias.symlink_to(loaded)
    records(relative='./plugins/alias')
    rejects(lambda: _claude_load_roots(account), 'history_claude_marketplace_invalid')
    alias.unlink()
    # A trailing slash names the same folder.
    records(relative='./plugins/memvane-v9/')
    assert _claude_load_roots(account) == [loaded.resolve()]
    # Malformed records keep the expected error codes.
    records(source_kind='string')
    rejects(lambda: _claude_load_roots(account), 'history_claude_marketplace_invalid')
    write_json(installed, {'version': 2, 'plugins': []})
    # An install record outside the cache is never trusted, whatever the source.
    for kind in ('github', 'directory'):
        records(source_kind=kind, install_path=outside)
        rejects(lambda: _claude_load_roots(account), 'history_claude_inventory_unsafe')
    records()
    # The cache copy may not drift from the code that runs.
    drift = cache / HOST_FILES[0]
    original = drift.read_bytes()
    drift.write_bytes(original + b'\n# drift\n')
    rejects(lambda: _claude_load_roots(account), 'history_claude_cache_differs')
    drift.write_bytes(original)

finally:
    patch.stopall()
    shutil.rmtree(account)
print('ok')

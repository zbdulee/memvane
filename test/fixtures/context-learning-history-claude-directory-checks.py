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

from context_learning.history_activation import (
    HOST_FILES, _claude_load_roots, _discover_host_roots, _installed_hosts_compatible)
import context_learning.history_activation as activation_module
import context_learning.history_lifecycle as lifecycle_module
from context_learning.history_lifecycle import acknowledge_host, runtime_fingerprint
from context_learning.storage_layout import root as learning_root


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
    source = Path(__file__).resolve().parents[2]
    version = '9.9.9-dev-directory'
    marketplace = account / 'work/local-marketplace'
    loaded = marketplace / 'plugins/qmd-auto-context-v9'
    cache = account / '.claude/plugins/cache/local-mkt/qmd-auto-context' / version
    codex = account / '.codex/plugins/cache/local-mkt/qmd-auto-context' / version
    for root in (loaded, cache, codex):
        for relative in HOST_FILES:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / relative, target)
    installed = account / '.claude/plugins/installed_plugins.json'
    known = account / '.claude/plugins/known_marketplaces.json'
    manifest = marketplace / '.claude-plugin/marketplace.json'

    def records(*, source_kind='directory', entry_version=version,
                relative='./plugins/qmd-auto-context-v9', install_path=None):
        write_json(installed, {'version': 2, 'plugins': {'qmd-auto-context@local-mkt': [
            {'scope': 'user', 'installPath': str(install_path or cache), 'version': version}]}})
        write_json(known, {'local-mkt': {'source': (
            {'source': 'directory', 'path': str(marketplace)} if source_kind == 'directory'
            else 'not-a-mapping' if source_kind == 'string'
            else {'source': 'github', 'repo': 'example/qmd'})}})
        write_json(manifest, {'name': 'local', 'plugins': [
            {'name': 'qmd-auto-context', 'version': entry_version, 'source': relative}]})

    codex_listing = subprocess.CompletedProcess((), 0, json.dumps({'installed': [
        {'name': 'qmd-auto-context', 'marketplaceName': 'local-mkt',
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
    roots = _installed_hosts_compatible(None)
    assert roots['claude'] == [loaded.resolve()], roots
    # Other marketplace kinds still run from the cache install path.
    records(source_kind='github')
    assert _claude_load_roots(account) == [cache]
    assert _installed_hosts_compatible(None)['claude'] == [cache]
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
        rejects(lambda: _installed_hosts_compatible(None), 'history_host_inventory_unavailable')
    records()
    alias = marketplace / 'plugins/alias'
    alias.symlink_to(loaded)
    records(relative='./plugins/alias')
    rejects(lambda: _claude_load_roots(account), 'history_claude_marketplace_invalid')
    alias.unlink()
    # A trailing slash names the same folder.
    records(relative='./plugins/qmd-auto-context-v9/')
    assert _claude_load_roots(account) == [loaded.resolve()]
    # Malformed records keep the expected error codes.
    records(source_kind='string')
    rejects(lambda: _claude_load_roots(account), 'history_claude_marketplace_invalid')
    rejects(lambda: _installed_hosts_compatible(None), 'history_host_inventory_unavailable')
    write_json(installed, {'version': 2, 'plugins': []})
    rejects(lambda: _installed_hosts_compatible(None), 'history_host_inventory_unavailable')
    # An install record outside the cache is never trusted, whatever the source.
    for kind in ('github', 'directory'):
        records(source_kind=kind, install_path=outside)
        rejects(lambda: _claude_load_roots(account), 'history_claude_inventory_unsafe')
        rejects(lambda: _installed_hosts_compatible(None), 'history_host_inventory_unavailable')
    records()
    # The cache copy may not drift from the code that runs.
    drift = cache / HOST_FILES[0]
    original = drift.read_bytes()
    drift.write_bytes(original + b'\n# drift\n')
    rejects(lambda: _claude_load_roots(account), 'history_claude_cache_differs')
    drift.write_bytes(original)
    # The executed folder must match this checkout.
    executed = loaded / HOST_FILES[0]
    executed.write_bytes(original + b'\n# drift\n')
    shutil.copy2(executed, drift)
    rejects(lambda: _installed_hosts_compatible(None), 'history_host_not_coordinated:claude')
    shutil.copy2(source / HOST_FILES[0], executed)
    shutil.copy2(source / HOST_FILES[0], drift)
    assert _installed_hosts_compatible(None)['claude'] == [loaded.resolve()]
    # A caller cannot name an arbitrary out-of-cache folder for the real account.
    stray = account / 'work/stray-plugin'
    shutil.copytree(loaded, stray)
    rejects(lambda: _installed_hosts_compatible({'claude': [stray], 'codex': [codex]}),
            'history_host_inventory_changed')

    # 3. Acknowledgements through the real SessionStart entrypoint.
    import hashlib
    learning_root().mkdir(parents=True, mode=0o700)
    generation = 'g-' + '1'*32
    prepared = learning_root() / 'migrations' / generation / 'prepared.json'
    prepared.parent.mkdir(parents=True, mode=0o700)
    prepared.write_text('{}')
    prepared.chmod(0o600)
    now = time.time()
    pending = {'schema': lifecycle_module.SCHEMA, 'generation': generation,
               'nonce': '2'*32,
               'manifest_sha256': hashlib.sha256(prepared.read_bytes()).hexdigest(),
               'runtime_sha256': runtime_fingerprint(source),
               'created_at': now, 'expires_at': now + lifecycle_module.MAX_AGE}
    lifecycle_module._atomic(lifecycle_module._request_path(), pending)
    assert lifecycle_module.current() == pending

    def hook_ack(engine, root):
        # Exactly what hooks/run-hook does: the executing tree's own script.
        result = real_run(['python3', str(root / 'core/context_learning_host_ack.py'), engine],
                          env={**os.environ, 'HOME': str(account),
                               'PYTHONPATH': str(root / 'core'),
                               'PYTHONDONTWRITEBYTECODE': '1'},
                          capture_output=True, text=True, timeout=30, check=False)
        return result

    result = hook_ack('claude', loaded)
    assert result.returncode == 0, result.stderr
    ack = lifecycle_module._ack_path('claude', loaded.resolve())
    assert json.loads(ack.read_text())['plugin_root'] == str(loaded.resolve())
    assert hook_ack('codex', codex).returncode == 0
    hosts = {'claude': [loaded.resolve()], 'codex': [codex]}
    # Unpatched: the runtime fingerprint is recomputed from this checkout.
    assert lifecycle_module.verify_acks(generation, hosts) == pending
    # A stray copy of identical code is not an installed root.
    stray_result = hook_ack('claude', stray)
    assert stray_result.returncode != 0 and 'history_host_ack_root_invalid' in stray_result.stderr
    assert not lifecycle_module._ack_path('claude', stray.resolve()).exists()
    rejects(lambda: acknowledge_host('codex', loaded.resolve()), 'history_host_ack_root_invalid')
    # Records that no longer name the folder refuse its ack.
    records(entry_version='9.9.8-other')
    rejects(lambda: acknowledge_host('claude', loaded.resolve()), 'history_host_ack_root_invalid')
    records()
    # The cache copy's ack does not stand in for the executed folder.
    assert hook_ack('claude', cache).returncode == 0
    ack.unlink()
    rejects(lambda: lifecycle_module.verify_acks(generation, hosts),
            'history_host_ack_missing:claude')
finally:
    patch.stopall()
    shutil.rmtree(account)
print('ok')

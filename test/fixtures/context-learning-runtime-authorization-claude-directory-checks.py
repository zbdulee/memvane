"""The execution grant accepts the folder Claude actually runs, and nothing else.

Claude runs a directory-marketplace plugin in place, so the adapter asking for
a grant is <marketplace>/plugins/<name>/..., not the cache copy the grant names.
"""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))

from context_learning import runtime_authorization as auth
from context_learning.history_activation import HOST_FILES

ADAPTER = 'core/context_learning/laya_adapter.py'


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def rejects(action, reason):
    try:
        action()
    except ValueError as error:
        assert str(error) == reason, str(error)
    else:
        raise AssertionError(f'expected {reason}')


source = Path(__file__).resolve().parents[2]
with tempfile.TemporaryDirectory(prefix='qmd-runtime-auth-claude-dir-', dir='/tmp') as tmp:
    home = Path(tmp).resolve()
    version = auth.REVIEWED_PLUGIN_VERSION
    marketplace = home / 'work/local-marketplace'
    loaded = marketplace / 'plugins/qmd-auto-context-local'
    cache = home / '.claude/plugins/cache/qmd-shared-v44-local/qmd-auto-context' / version
    codex = home / '.codex/plugins/cache/qmd-shared-v21-local/qmd-auto-context' / version
    for root in (loaded, cache, codex):
        for relative in HOST_FILES:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / relative, target)
        # The copied manifests carry the repo version; the grant reviews this one.
        for name in ('.claude-plugin/plugin.json', '.codex-plugin/plugin.json'):
            write_json(root / name, {'name': 'qmd-auto-context', 'version': version})
    sha = hashlib.sha256((source / ADAPTER).read_bytes()).hexdigest()
    installed = home / '.claude/plugins/installed_plugins.json'
    known = home / '.claude/plugins/known_marketplaces.json'
    manifest = marketplace / '.claude-plugin/marketplace.json'

    def records(*, kind='directory'):
        write_json(installed, {'version': 2, 'plugins': {
            'qmd-auto-context@qmd-shared-v44-local': [
                {'scope': 'user', 'installPath': str(cache), 'version': version}]}})
        write_json(known, {'qmd-shared-v44-local': {'source': (
            {'source': 'directory', 'path': str(marketplace)} if kind == 'directory'
            else {'source': 'github', 'repo': 'example/qmd'})}})
        write_json(manifest, {'name': 'local', 'plugins': [
            {'name': 'qmd-auto-context', 'version': version,
             'source': './plugins/qmd-auto-context-local'}]})

    with patch.object(Path, 'home', return_value=home):
        records()
        expected = [{'host': 'claude', 'path': str(cache / ADAPTER), 'sha256': sha},
                    {'host': 'codex', 'path': str(codex / ADAPTER), 'sha256': sha}]
        # Both cache copies keep working, and the grant still names only them.
        assert auth._installed_adapters(cache / ADAPTER, sha) == expected
        assert auth._installed_adapters(codex / ADAPTER, sha) == expected
        # The folder Claude runs is accepted without changing the recorded scope.
        assert auth._installed_adapters(loaded / ADAPTER, sha) == expected

        # A byte-identical copy that Claude's records do not name is refused.
        stray = home / 'elsewhere/qmd-auto-context'
        for relative in HOST_FILES:
            target = stray / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(loaded / relative, target)
        rejects(lambda: auth._installed_adapters(stray / ADAPTER, sha),
                'runtime_authorization_adapter_invalid')

        # A non-directory marketplace runs from the cache; its folder is not trusted.
        records(kind='github')
        rejects(lambda: auth._installed_adapters(loaded / ADAPTER, sha),
                'runtime_authorization_adapter_invalid')
        records()

        # The executed folder must carry the reviewed version.
        write_json(manifest, {'name': 'local', 'plugins': [
            {'name': 'qmd-auto-context', 'version': 'other',
             'source': './plugins/qmd-auto-context-local'}]})
        rejects(lambda: auth._installed_adapters(loaded / ADAPTER, sha),
                'runtime_authorization_adapter_invalid')
        records()
        original = (loaded / '.claude-plugin/plugin.json').read_bytes()
        write_json(loaded / '.claude-plugin/plugin.json',
                   {'name': 'qmd-auto-context', 'version': 'other'})
        write_json(cache / '.claude-plugin/plugin.json',
                   {'name': 'qmd-auto-context', 'version': 'other'})
        assert auth._claude_executed_adapters() == set()
        (loaded / '.claude-plugin/plugin.json').write_bytes(original)
        (cache / '.claude-plugin/plugin.json').write_bytes(original)

        # Executed code that drifted from the cache copy is refused.
        drift = loaded / HOST_FILES[0]
        kept = drift.read_bytes()
        drift.write_bytes(kept + b'\n# drift\n')
        rejects(lambda: auth._installed_adapters(loaded / ADAPTER, sha),
                'runtime_authorization_adapter_invalid')
        drift.write_bytes(kept)
        assert auth._installed_adapters(loaded / ADAPTER, sha) == expected

        # Unreadable Claude records close the extra path, not the cache copies.
        installed.write_text('{')
        assert auth._claude_executed_adapters() == set()
        rejects(lambda: auth._installed_adapters(loaded / ADAPTER, sha),
                'runtime_authorization_adapter_invalid')
        assert auth._installed_adapters(codex / ADAPTER, sha) == expected
    print(json.dumps({'claude_executed_folder': True}))

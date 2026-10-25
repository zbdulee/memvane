"""Production registry CLI blocks a different owner's global index update."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path.cwd() / 'core'))
import qmd_shared_store

repository = Path.cwd()
with tempfile.TemporaryDirectory(prefix='qmd-shared-enrollment-', dir=repository) as name:
    home = Path(name).resolve()
    stories = [home / 'novel/story-a', home / 'novel/story-b']
    for i, story in enumerate(stories):
        (story / 'wiki').mkdir(parents=True)
        (story / 'wiki/card.md').write_text(f'synthetic card {i}\n')
        (story / '.auto-context').mkdir()
        (story / '.auto-context/settings.json').write_text(json.dumps({
            'indexing': True, 'collections': [f'story-{i}-wiki'],
            'collectionPaths': {f'story-{i}-wiki': 'wiki'},
            'collectionRoles': {f'story-{i}-wiki': 'wiki'}}))
    env = {**os.environ, 'HOME': str(home), 'QMD_SETUP_GUARD_FIXTURE': ''}
    prepared = subprocess.run([sys.executable,
        str(repository / 'test/fixtures/prepare-shared-qmd-store.py'), str(home)],
        env=env, capture_output=True, text=True)
    assert prepared.returncode == 0, prepared.stderr
    # These are existing legacy owners retrying registration. New owners
    # cannot become approved by placing settings on disk.
    for story in stories:
        config = json.loads((story / '.auto-context/settings.json').read_text())
        qmd_shared_store._reserve_owners(qmd_shared_store.root(home),
            qmd_shared_store._collection_owners(story, config))
    route = repository / 'core/qmd_route.py'
    def call(*args):
        return subprocess.run([sys.executable, str(route), *args], env=env,
                              capture_output=True, text=True).returncode
    for story in stories:
        assert call('register-config', str(story)) == 0
    assert call('verify-shared-enrollment') == 0
    index = home / 'Library/Application Support/qmd-auto-context/shared-qmd' / \
        'generations' / ('g-' + 'a' * 32) / 'index.sqlite'
    before = hashlib.sha256(index.read_bytes()).hexdigest()
    settings = stories[1] / '.auto-context/settings.json'
    value = json.loads(settings.read_text())
    value['indexing'] = False
    settings.write_text(json.dumps(value))
    assert call('verify-shared-enrollment') == 1
    value['indexing'] = True
    value['collectionRoles']['story-1-wiki'] = 'source'
    settings.write_text(json.dumps(value))
    assert call('verify-shared-enrollment') == 1
    value['collectionRoles']['story-1-wiki'] = 'wiki'
    settings.write_text(json.dumps(value))
    (stories[1] / 'wiki/card.md').unlink()
    (stories[1] / 'wiki').rmdir()
    assert call('verify-shared-enrollment') == 1
    assert hashlib.sha256(index.read_bytes()).hexdigest() == before
    print('shared-enrollment-cli-ok')

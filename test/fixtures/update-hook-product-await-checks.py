"""The queue worker awaits its already detached updater before ACKing."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

repository = Path.cwd()
with tempfile.TemporaryDirectory(prefix='qmd-update-await-', dir=repository) as name:
    home = Path(name).resolve()
    story = home / 'story'
    (story / 'wiki').mkdir(parents=True)
    (story / 'wiki/card.md').write_text('synthetic only\n')
    (story / '.auto-context').mkdir()
    (story / '.auto-context/settings.json').write_text(json.dumps({
        'indexing': True, 'collections': ['story-wiki'],
        'collectionPaths': {'story-wiki': 'wiki'},
        'collectionRoles': {'story-wiki': 'wiki'}}))
    calls = home / 'calls.txt'
    qmd = home / 'qmd-stub.sh'
    qmd.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "' + str(calls) + '"\nexit 0\n')
    qmd.chmod(0o700)
    manager = home / 'manager.sh'
    manager.write_text('#!/bin/sh\nexit 0\n')
    manager.chmod(0o700)
    environment = {**os.environ, 'HOME': str(home), 'QMD_SETUP_GUARD_FIXTURE': '1',
        'QMD_BIN': str(qmd), 'QMD_BACKEND_MANAGER': str(manager),
        'QMD_UPDATE_HOOK_QUEUE_DIR': str(home / 'queue'),
        'QMD_CACHE_DIR': str(home / 'cache'), 'QMD_LOCK_BASE': str(home / 'locks'),
        'QMD_SKIP_BACKGROUND_EMBED': '', 'QMD_SKIP_BACKGROUND_WORKER': '',
        'QMD_SUPPRESS_NOTICE': '1', 'QMD_RECALL_LOG': ''}
    environment.pop('QMD_CORE_UPDATE_SCRIPT', None)
    payload = json.dumps({'cwd': str(story), 'hook_event_name': 'SessionStart'})
    for engine in ('codex', 'claude'):
        result = subprocess.run(['bash', str(repository / 'hooks/run-hook'),
                                 'update-queued', engine], cwd=story,
            env=environment, input=payload, capture_output=True, text=True, timeout=5)
        assert result.returncode == 0, result.stderr
    key = hashlib.sha256(str(story).encode()).hexdigest()[:32]
    status = home / 'queue' / (key + '.status.json')
    job = home / 'queue' / (key + '.job.json')
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if status.is_file() and json.loads(status.read_text()).get('status') == 'completed' and not job.exists():
            break
        time.sleep(.025)
    assert status.is_file(), 'no_status'
    assert json.loads(status.read_text())['status'] == 'completed', (status.read_text(),
        (home / 'queue' / (key + '.log')).read_text(),
        (home / 'cache/hook.log').read_text() if (home / 'cache/hook.log').exists() else 'no_hook_log',
        calls.read_text() if calls.exists() else 'no_qmd_calls')
    assert not job.exists()
    assert calls.read_text().splitlines().count('update') == 1, calls.read_text()
    assert calls.read_text().splitlines().count('embed') == 1, calls.read_text()
    print('awaited-update-ok')

"""Actual SessionStart entrypoint with isolated fake maintenance commands."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

with tempfile.TemporaryDirectory(prefix='qmd-update-hook-') as name:
    base = Path(name)
    project = base/'project'; project.mkdir(); project = project.resolve()
    queue = base/'queue'
    manager = base/'manager.sh'; manager.write_text('#!/bin/sh\nexit 0\n')
    update = base/'update.sh'
    update.write_text('#!/bin/sh\nif [ "$1" = "--resolve-only" ]; then echo "{}"; exit 0; fi\ncat >/dev/null\necho run >> "$QMD_TEST_COUNT"\nsleep 0.25\n')
    count = base/'count'
    env = {**os.environ, 'CLAUDE_PLUGIN_ROOT': str(Path.cwd()),
           'QMD_SETUP_GUARD_FIXTURE': '1',
           'QMD_BACKEND_MANAGER': str(manager), 'QMD_CORE_UPDATE_SCRIPT': str(update),
           'QMD_UPDATE_HOOK_QUEUE_DIR': str(queue), 'QMD_TEST_COUNT': str(count),
           'QMD_RECALL_LOG': ''}
    payload = json.dumps({'cwd': str(project), 'hook_event_name': 'SessionStart'})
    start = time.monotonic()
    notices = []
    for _ in range(2):
        hook = subprocess.run(['bash','hooks/run-hook','update-queued','codex'],
            input=payload,text=True,capture_output=True,env=env,timeout=3,check=True)
        notices.append(hook.stdout)
    first = json.loads(notices[0])
    assert list(first) == ['hookSpecificOutput']
    assert first['hookSpecificOutput']['hookEventName'] == 'SessionStart'
    assert 'setup skill' in first['hookSpecificOutput']['additionalContext']
    assert 'settings_not_ready' in first['hookSpecificOutput']['additionalContext']
    assert not notices[1], notices
    marker = list(queue.glob('setup-notice-*.json'))
    assert len(marker) == 1
    assert json.loads(marker[0].read_text())['pluginVersion']
    assert time.monotonic()-start < 1.0
    key = hashlib.sha256(str(project).encode()).hexdigest()[:32]
    status = queue/(key+'.status.json')
    deadline = time.monotonic()+5
    while time.monotonic()<deadline and not status.is_file(): time.sleep(.03)
    assert json.loads(status.read_text())['status']=='completed', (status.read_text(), (queue/(key+'.log')).read_text())
    assert count.read_text().splitlines()==['run']
    assert not (queue/(key+'.job.json')).exists()
    assert (queue/(key+'.log')).stat().st_mode & 0o077 == 0
    notice_project = base/'notice'; notice_project.mkdir(); notice_project = notice_project.resolve()
    update.write_text('#!/bin/sh\nif [ "$1" = "--resolve-only" ]; then echo \'{"reason":"pending"}\'; exit 0; fi\ncat >/dev/null\n')
    notice = subprocess.run(['bash','hooks/run-hook','update-queued','codex'],
        input=json.dumps({'cwd': str(notice_project)}), text=True,capture_output=True,
        env=env,timeout=3,check=True)
    second = json.loads(notice.stdout)
    assert len(notice.stdout.splitlines()) == 1
    assert second['hookSpecificOutput']['hookEventName'] == 'SessionStart'
    assert 'opt-in 또는 opt-out' in second['hookSpecificOutput']['additionalContext']
    assert 'setup skill' in second['hookSpecificOutput']['additionalContext']
    assert len(second['hookSpecificOutput']['additionalContext'].splitlines()) == 2
    claude_project = base/'claude-notice'; claude_project.mkdir()
    claude_notice = subprocess.run(['bash','hooks/run-hook','update-queued','claude'],
        input=json.dumps({'cwd': str(claude_project)}), text=True,capture_output=True,
        env=env,timeout=3,check=True)
    assert claude_notice.stdout.startswith('[qmd] ')
    assert 'setup skill' in claude_notice.stdout
    notice_key = hashlib.sha256(str(notice_project).encode()).hexdigest()[:32]
    deadline = time.monotonic()+5
    while time.monotonic()<deadline and not (queue/(notice_key+'.status.json')).exists(): time.sleep(.03)
    retry_project = base/'retry'; retry_project.mkdir(); retry_project = retry_project.resolve()
    retry_key = hashlib.sha256(str(retry_project).encode()).hexdigest()[:32]
    retry_payload = json.dumps({'cwd': str(retry_project), 'hook_event_name': 'SessionStart'})
    update.write_text('#!/bin/sh\ncat >/dev/null\nexit 7\n')
    subprocess.run(['bash','hooks/run-hook','update-queued','codex'], input=retry_payload,
                   text=True,capture_output=True,env=env,timeout=3,check=True)
    retry_status = queue/(retry_key+'.status.json')
    deadline = time.monotonic()+5
    while time.monotonic()<deadline and not retry_status.is_file(): time.sleep(.03)
    assert json.loads(retry_status.read_text())['status']=='failed'
    assert (queue/(retry_key+'.job.json')).is_file()
    assert 'update failed' in (queue/(retry_key+'.log')).read_text()
    update.write_text('#!/bin/sh\ncat >/dev/null\nexit 0\n')
    subprocess.run(['bash','hooks/run-hook','update-queued','codex'], input=retry_payload,
                   text=True,capture_output=True,env=env,timeout=3,check=True)
    deadline = time.monotonic()+5
    while time.monotonic()<deadline:
        if json.loads(retry_status.read_text())['status']=='completed': break
        time.sleep(.03)
    assert json.loads(retry_status.read_text())['status']=='completed'
    assert not (queue/(retry_key+'.job.json')).exists()
    # A malformed payload falls back to the process cwd: keep it neutral, since a
    # checkout of this repository may itself be a selected Laya project.
    subprocess.run(['bash',str(Path.cwd()/'hooks/run-hook'),'update-queued','codex'],
                   input='{invalid',cwd=str(project),
                   text=True,capture_output=True,env=env,timeout=3,check=True)
    assert json.loads((queue/'enqueue.status.json').read_text())['status']=='enqueue_failed'
    assert 'enqueue failed:JSONDecodeError' in (queue/'enqueue.log').read_text()
    print(json.dumps({'fastEnqueue':True,'oneTimeSetupNotice':True,'duplicateCoalesced':True,
        'durableStatus':True,'failureRetry':True,'enqueueErrorVisible':True,'externalCalls':0}))

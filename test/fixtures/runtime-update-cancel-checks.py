"""Revision-scoped cancel of an owned synthetic-QA update wake (no real account state).

A fake HOME, an explicit private update-job dir and an explicit private queue
root are used; the official per-UID queue root is patched to raise if anything
asks for it. Every identifier is generated here; no live project or job key.
"""
from contextlib import ExitStack, redirect_stdout
import fcntl
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path.cwd() / 'core'))
import runtime_work
import runtime_update_cancel as ucancel
import runtime_wake_cancel
import runtime_wake_policy as wake_policy
import update_hook_queue


def forbidden(*_a, **_k):
    raise AssertionError('official per-UID queue root must not be used in tests')


results = {}
with tempfile.TemporaryDirectory(prefix='update-cancel-') as name, ExitStack() as outer:
    base = Path(name).resolve()
    home = base / 'home'; home.mkdir(mode=0o700)
    ujobs = base / 'update-jobs'; ujobs.mkdir(mode=0o700)
    outer.enter_context(patch.object(runtime_work, 'default_root', forbidden))
    outer.enter_context(patch.dict(os.environ, {'HOME': str(home), 'QMD_UPDATE_HOOK_QUEUE_DIR': str(ujobs)}))
    fixture = base / 'qa-root'; fixture.mkdir(mode=0o700)
    project = fixture / 'project'; project.mkdir(mode=0o700)
    (fixture / 'MANIFEST.json').write_text(json.dumps({'schema': runtime_wake_cancel.FIXTURE_SCHEMA,
        'root': str(fixture), 'fixtureId': 'f1', 'paths': {'project': str(project)}}))
    (fixture / '.qa-fixture-owner').write_text(json.dumps({'fixtureId': 'f1', 'uid': os.getuid()}))
    target = ucancel.job_key(project)
    key = wake_policy.wake_key('update', target)
    job = ujobs / (target + '.job.json')
    status_file = ujobs / (target + '.status.json')

    def write_job(value=None):
        # New file then replace: the old inode is alive meanwhile, so the new one always differs.
        tmp = ujobs / '.job-write'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, (json.dumps(value if value is not None else
                                 {'schema': 'qmd-update-hook-job-v1', 'cwd': str(project)}) + '\n').encode())
        os.close(fd)
        os.replace(tmp, job)

    def write_status(**over):
        update_hook_queue._write(status_file, {'schema': 'qmd-update-hook-status-v1', 'cwdHash': target,
                                               'status': 'worker_failed', 'at': 1.0, **over})

    write_job(); write_status()
    qroot = base / 'queue'; qroot.mkdir(mode=0o700)
    queue = runtime_work.WakeQueue(qroot)
    backups = base / 'backups'; backups.mkdir(mode=0o700)
    other_target = ucancel.job_key(base / 'elsewhere')

    def run(rev, *, backup_dir=backups, path=project):
        return ucancel.cancel(str(path), rev, str(backup_dir), queue=queue)

    def revision():
        with queue.connect() as db:
            row = db.execute('SELECT revision FROM wakes WHERE id=?', (key,)).fetchone()
        return row[0] if row else None

    def row_now():
        with queue.connect() as db:
            found = db.execute('SELECT revision,status,attempts,due,reason FROM wakes WHERE id=?', (key,)).fetchone()
        return tuple(found) if found else None

    def snapshot(exclude=None):
        with queue.connect() as db:
            wakes = {tuple(r) for r in db.execute('SELECT * FROM wakes') if r['id'] != exclude}
            over = {tuple(r) for r in db.execute('SELECT * FROM wake_overflow') if r['id'] != exclude}
        return wakes, over

    def audits():
        return ucancel.status(str(project), queue=queue)['cancellations']

    def files():
        return job.read_bytes(), status_file.read_bytes()

    queue.offer('update', target); queue.offer('update', other_target); queue.offer('laya-sync', str(project))
    rev = revision()
    before_rows, before_files = snapshot(), files()

    # Fail closed: revision, row state, backup dir, project/job/status/fixture proof gaps.
    assert run(rev + 1) == {'status': 'refused', 'reason': 'revision_changed'}
    for bad in (0, -1, True, '1', None, 1.0):
        assert run(bad) == {'status': 'refused', 'reason': 'invalid_revision'}, bad
    with queue.connect() as db:
        db.execute("UPDATE wakes SET status='running' WHERE id=?", (key,))
    assert run(rev)['reason'] == 'wake_not_pending'
    with queue.connect() as db:
        db.execute("UPDATE wakes SET status='pending' WHERE id=?", (key,))
    loose = base / 'loose'; loose.mkdir(mode=0o755); loose.chmod(0o755)
    assert run(rev, backup_dir=loose)['reason'] == 'unsafe_backup_dir'
    project.chmod(0o755)
    assert run(rev)['reason'] == 'project_not_private'
    project.chmod(0o700)
    job.chmod(0o644)
    assert run(rev)['reason'] == 'unsafe_proof_path'
    job.chmod(0o600)
    os.link(job, ujobs / 'job-hardlink')
    assert run(rev)['reason'] == 'unsafe_proof_path'
    (ujobs / 'job-hardlink').unlink()
    saved_job = job.read_bytes()
    for value, reason in (({'schema': 'qmd-update-hook-job-v1', 'cwd': str(fixture)}, 'update_job_invalid'),
                          ({'schema': 'other', 'cwd': str(project)}, 'update_job_invalid'),
                          ({'schema': 'qmd-update-hook-job-v1', 'cwd': str(project), 'x': 1}, 'update_job_invalid'),
                          ([str(project)], 'update_state_invalid')):
        write_job(value)
        assert run(rev) == {'status': 'refused', 'reason': reason}, (value, reason)
    job.unlink()
    assert run(rev)['reason'] == 'update_job_missing'
    write_job(); assert job.read_bytes() == saved_job
    saved_status = status_file.read_bytes()
    write_status(cwdHash=other_target)
    assert run(rev)['reason'] == 'update_status_invalid'
    write_status(schema='other')
    assert run(rev)['reason'] == 'update_status_invalid'
    status_file.write_bytes(saved_status); status_file.chmod(0o644)
    assert run(rev)['reason'] == 'unsafe_proof_path'
    status_file.chmod(0o600)
    marker = fixture / '.qa-fixture-owner'; saved_marker = marker.read_text()
    marker.write_text(json.dumps({'fixtureId': 'other', 'uid': os.getuid()}))
    assert run(rev)['reason'] == 'fixture_marker_invalid'
    marker.write_text(saved_marker)
    sibling = fixture / 'sibling'; sibling.mkdir(mode=0o700)
    assert run(rev, path=sibling)['reason'] == 'fixture_marker_invalid'
    stray = base / 'stray'; stray.mkdir(mode=0o700)
    assert run(rev, path=stray)['reason'] == 'proof_unreadable'
    link = base / 'project-link'; link.symlink_to(project)
    assert run(rev, path=link)['reason'] == 'invalid_project'
    assert snapshot() == before_rows and files() == before_files and not list(backups.iterdir())
    results['failClosedNoMutation'] = True

    # A running per-job worker or a busy UID dispatcher is refused, never waited for.
    def hold(path):
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    fd = hold(ujobs / (target + '.lock'))
    assert run(rev) == {'status': 'refused', 'reason': 'update_worker_running'}
    os.close(fd)
    fd = hold(qroot / 'runtime-worker.lock')
    assert run(rev) == {'status': 'refused', 'reason': 'dispatch_busy'}
    os.close(fd)
    assert snapshot() == before_rows and files() == before_files and not list(backups.iterdir())
    results['runningOrBusyRefused'] = True

    real_backup = runtime_wake_cancel._backup
    def retry(q):
        with q.connect() as db:
            db.execute("UPDATE wakes SET attempts=attempts+1,due=due+300,reason='ValueError' WHERE id=?", (key,))
    # Attempt drift before the snapshot: the backup no longer holds the captured row.
    def retry_then_backup(q, d, k, r):
        retry(q); return real_backup(q, d, k, r)
    with patch.object(ucancel, '_backup', retry_then_backup):
        assert run(rev) == {'status': 'refused', 'reason': 'backup_unverified'}
    # Attempt drift or a claim after a verified backup: full-row CAS refuses.
    drifted = row_now()
    def backup_then_retry(q, d, k, r):
        out = real_backup(q, d, k, r); retry(q); return out
    with patch.object(ucancel, '_backup', backup_then_retry):
        assert run(rev) == {'status': 'refused', 'reason': 'wake_changed_before_delete'}
    assert row_now()[2] == drifted[2] + 1 and audits() == []
    drifted = row_now()
    def backup_then_claim(q, d, k, r):
        out = real_backup(q, d, k, r)
        with q.connect() as db:
            db.execute("UPDATE wakes SET status='running' WHERE id=?", (key,))
        return out
    with patch.object(ucancel, '_backup', backup_then_claim):
        assert run(rev) == {'status': 'refused', 'reason': 'wake_changed_before_delete'}
    with queue.connect() as db:
        db.execute("UPDATE wakes SET status='pending' WHERE id=?", (key,))
    # Job or status changing during the backup is caught by the in-transaction re-proof.
    for change in (lambda: write_status(status='indexing', runId='0' * 32), lambda: write_job()):
        def transition(q, d, k, r, change=change):
            out = real_backup(q, d, k, r); change(); return out
        with patch.object(ucancel, '_backup', transition):
            assert run(rev) == {'status': 'refused', 'reason': 'proof_changed'}
        assert row_now() == drifted and audits() == []
    # Re-enqueue after backup: newer revision survives.
    def racing(q, d, k, r):
        out = real_backup(q, d, k, r); q.offer('update', target); return out
    with patch.object(ucancel, '_backup', racing):
        assert run(rev) == {'status': 'refused', 'reason': 'wake_changed_before_delete'}
    newer = revision(); assert newer > rev and audits() == []
    results['driftAndRaceKeepRow'] = True

    # Backup failure: nothing deleted, nothing audited.
    with patch.object(ucancel, '_backup', side_effect=sqlite3.DatabaseError('synthetic')):
        assert run(newer) == {'status': 'refused', 'reason': 'backup_failed'}
    if os.getuid() != 0:
        sealed = base / 'sealed'; sealed.mkdir(mode=0o700); sealed.chmod(0o500)
        assert run(newer, backup_dir=sealed) == {'status': 'refused', 'reason': 'backup_failed'}
        sealed.chmod(0o700)
    holder = sqlite3.connect(queue.path); holder.execute('BEGIN IMMEDIATE')
    busy = run(newer)
    holder.rollback(); holder.close()
    assert busy['status'] == 'refused' and busy['reason'] in ('queue_busy', 'backup_failed'), busy
    assert revision() == newer and audits() == []
    results['backupFailureNoMutation'] = True

    # Applied: exact row and its overflow row go; every other row and the job/status files stay.
    with queue.connect() as db:
        db.execute('INSERT INTO wake_overflow VALUES (?,?,?,?)', (key, 'update', target, 1.0))
        db.execute('INSERT INTO wake_overflow VALUES (?,?,?,?)',
                   (wake_policy.wake_key('cleanup', str(project)), 'cleanup', str(project), 2.0))
    others, kept_files = snapshot(exclude=key), files()
    applied = run(newer)
    assert applied['status'] == 'applied' and applied['overflowRemoved'] == 1, applied
    assert snapshot() == others and files() == kept_files
    after = ucancel.status(str(project), queue=queue)
    assert after['wake'] is None and after['overflow'] is False and len(after['cancellations']) == 1
    audit = after['cancellations'][0]
    assert (audit['revision'], audit['kind'], audit['target'], audit['wake_id']) == (newer, 'update', target, key)
    assert audit['backup_sha256'] == applied['backup']['sha256'] and audit['proof_sha256'] == applied['proof']['sha256']
    removed = applied['removedRow']
    assert (audit['attempts'], audit['due'], audit['reason']) == (removed['attempts'], removed['due'], removed['reason'])
    copy = Path(applied['backup']['path'])
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600 and applied['backup']['quickCheck'] == 'ok'
    with sqlite3.connect(copy.as_uri() + '?mode=ro', uri=True) as db:
        assert db.execute('SELECT revision,status,attempts,due,reason FROM wakes WHERE id=?', (key,)).fetchone() == (
            newer, 'pending', removed['attempts'], removed['due'], removed['reason'])
    results['appliedAuditedOthersKept'] = True

    # Replay: idempotent only for the audited target/revision; any other absence fails closed.
    replay = run(newer)
    assert replay['status'] == 'unchanged' and replay['reason'] == 'already_cancelled'
    assert replay['audit']['seq'] == audit['seq']
    assert run(newer + 7) == {'status': 'refused', 'reason': 'wake_absent_unaudited'}
    assert len(audits()) == 1 and snapshot() == others
    # Recovery is an explicit new request: a fresh, higher revision that is not auto-cancelled.
    queue.offer('update', target); fresh = revision(); assert fresh > newer
    assert run(newer) == {'status': 'refused', 'reason': 'revision_changed'} and revision() == fresh
    assert ucancel.status('relative/path') == {'status': 'refused', 'reason': 'invalid_project'}
    # A read-only status race after COMMIT cannot turn an applied cancellation
    # into a false refusal (and prompt the operator to repeat a real mutation).
    with patch.object(ucancel, 'status', side_effect=sqlite3.OperationalError('synthetic busy')):
        applied_after_busy = run(fresh)
    assert applied_after_busy['status'] == 'applied' and applied_after_busy['after'] is None
    assert len(audits()) == 2 and revision() is None
    results['replayAuditedOnly'] = True

    # CLI: official queue (no root argument), parsed revision, exit codes.
    seen = []
    printed = io.StringIO()
    with patch.dict(os.environ, {}, clear=True), \
            patch.object(ucancel, 'cancel', side_effect=lambda *a, **k: seen.append((a, k)) or {'status': 'refused'}), \
            redirect_stdout(printed):
        assert runtime_work.main(['cancel-update-wake', '--project', str(project), '--revision', 'x',
                                  '--backup-dir', str(backups)]) == 3
    assert json.loads(printed.getvalue()) == {'status': 'refused'}
    assert seen == [((str(project), None, str(backups)), {})], seen
    printed = io.StringIO()
    with patch.dict(os.environ, {'QMD_UPDATE_HOOK_QUEUE_DIR': str(ujobs)}), redirect_stdout(printed):
        assert runtime_work.main(['cancel-update-wake', '--project', str(project), '--revision', '1',
                                  '--backup-dir', str(backups)]) == 3
    assert json.loads(printed.getvalue()) == {'status': 'refused', 'reason': 'non_official_update_queue'}
    printed = io.StringIO()
    with patch.object(ucancel, 'status', side_effect=lambda *a, **k: seen.append((a, k)) or {'wake': None}), \
            redirect_stdout(printed):
        assert runtime_work.main(['update-wake-status', '--project', str(project)]) == 0
    assert seen[-1] == ((str(project),), {})
    assert runtime_work.main(['cancel-update-wake', '--project', str(project)]) == 2
    assert runtime_work.main(['cancel-update-wake', '--revision', '1', '--project', str(project),
                              '--backup-dir', str(backups)]) == 2
    results['cliOfficialQueueOnly'] = True

print(json.dumps(results, sort_keys=True))

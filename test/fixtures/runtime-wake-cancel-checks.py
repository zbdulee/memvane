"""Revision-scoped cancel of an owned synthetic-QA laya-sync wake (no real account state).

A fake HOME and an explicit private queue root are used; the official per-UID
queue root is patched to raise if anything asks for it. Ownership inputs are
real private files plus patched official readers (journal, route, build).
"""
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
import runtime_wake_cancel as cancel
import runtime_wake_policy as wake_policy
import laya_initial_build as initial_build
import laya_install_journal
import laya_index_host_route


def forbidden(*_a, **_k):
    raise AssertionError('official per-UID queue root must not be used in tests')


results = {}
# Platform temp dir (Linux /tmp, macOS /var/folders); `base` below is canonicalized so the
# fixture's no-symlink path checks run on the real, link-free location.
with tempfile.TemporaryDirectory(prefix='wake-cancel-') as name, \
        patch.object(runtime_work, 'default_root', forbidden):
    base = Path(name).resolve()
    home = base / 'home'; home.mkdir(mode=0o700)
    fixture = base / 'qa-root'; fixture.mkdir(mode=0o700)
    project = fixture / 'project'; project.mkdir(mode=0o700)
    (fixture / 'MANIFEST.json').write_text(json.dumps({'schema': cancel.FIXTURE_SCHEMA, 'root': str(fixture),
                                                        'fixtureId': 'f1', 'paths': {'project': str(project)}}))
    (fixture / '.qa-fixture-owner').write_text(json.dumps({'fixtureId': 'f1', 'uid': os.getuid()}))
    state = home / '.local/state/laya/' / ('a' * 32)
    state.mkdir(parents=True, mode=0o700)
    for d in (home / '.local', home / '.local/state', home / '.local/state/laya'):
        d.chmod(0o700)
    pointers = home / '.local/state/laya/initial-builds'; pointers.mkdir(mode=0o700)
    journal = state / 'install-journal.sqlite'
    for f in (state / 'initial-build.json', journal):
        f.write_text('{}'); f.chmod(0o600)
    identity = {'projectRoot': str(project), 'stateRoot': str(state), 'installId': 'a' * 32,
                'journalPath': str(journal)}
    record = {'phase': 'registered', 'identity': identity}
    journal_view = {'state': 'candidate_ready', 'selected': None, 'epoch': 3,
                    'pending': {'installId': 'a' * 32}}
    route = {'state': 'legacy', 'reason': 'no_route_record'}
    qroot = base / 'queue'; qroot.mkdir(mode=0o700)
    queue = runtime_work.WakeQueue(qroot)
    backups = base / 'backups'; backups.mkdir(mode=0o700)
    key = wake_policy.wake_key('laya-sync', str(project))

    live = {}

    def env(**over):
        live.clear(); live.update({'record': record, 'journal': journal_view, 'route': route, **over})
        return [patch.dict(os.environ, {'HOME': str(home)}),
                patch.object(initial_build, 'lookup', side_effect=lambda *_a: live['record']),
                patch.object(laya_install_journal, 'inspect_journal', side_effect=lambda *_a: live['journal']),
                patch.object(laya_index_host_route, 'select_route', side_effect=lambda *_a, **_k: live['route'])]

    def run(revision, *, backup_dir=backups, **over):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in env(**over):
                stack.enter_context(p)
            (pointers / initial_build.pointer_path(project, pointers).name).touch()
            initial_build.pointer_path(project, pointers).chmod(0o600)
            return cancel.cancel(str(project), revision, str(backup_dir), queue=queue)

    def revision():
        with queue.connect() as db:
            row = db.execute('SELECT revision FROM wakes WHERE id=?', (key,)).fetchone()
        return row[0] if row else None

    queue.offer('laya-sync', str(project)); queue.offer('laya-compile', str(project))
    rev = revision()
    # Fail closed: wrong revision, non-pending row, unsafe backup dir, every proof gap.
    assert run(rev + 1) == {'status': 'refused', 'reason': 'revision_changed'}
    with queue.connect() as db:
        db.execute("UPDATE wakes SET status='running' WHERE id=?", (key,))
    assert run(rev)['reason'] == 'wake_not_pending'
    with queue.connect() as db:
        db.execute("UPDATE wakes SET status='pending' WHERE id=?", (key,))
    loose = base / 'loose'; loose.mkdir(mode=0o755)
    assert run(rev, backup_dir=loose)['reason'] == 'unsafe_backup_dir'
    for over, reason in (({'record': None}, 'initial_build_not_registered'),
                         ({'record': {**record, 'phase': 'bound'}}, 'initial_build_not_inactive'),
                         ({'journal': {**journal_view, 'selected': {'x': 1}}}, 'journal_not_pending_candidate'),
                         ({'journal': {**journal_view, 'state': 'active'}}, 'journal_not_pending_candidate'),
                         ({'journal': {**journal_view, 'pending': {'installId': 'b' * 32}}}, 'journal_not_pending_candidate'),
                         ({'route': {'state': 'candidate'}}, 'project_route_not_original_qmd'),
                         ({'record': {**record, 'identity': {**identity, 'projectRoot': str(base)}}}, 'initial_build_not_inactive')):
        assert run(rev, **over) == {'status': 'refused', 'reason': reason}, (over, reason)
    (state / 'initial-build.json').chmod(0o644)
    assert run(rev)['reason'] == 'unsafe_proof_path'
    (state / 'initial-build.json').chmod(0o600)
    os.link(journal, state / 'journal-hardlink')
    assert run(rev)['reason'] == 'unsafe_proof_path'
    (state / 'journal-hardlink').unlink()
    marker = fixture / '.qa-fixture-owner'; saved = marker.read_text()
    marker.write_text(json.dumps({'fixtureId': 'other', 'uid': os.getuid()}))
    assert run(rev)['reason'] == 'fixture_marker_invalid'
    marker.write_text(saved)
    link = base / 'project-link'; link.symlink_to(project)
    with patch.dict(os.environ, {'HOME': str(home)}):
        assert cancel.cancel(str(link), rev, str(backups), queue=queue)['reason'] == 'invalid_project'
    assert revision() == rev and not list(backups.iterdir())
    results['failClosedNoMutation'] = True

    real_backup = cancel._backup
    def row_now():
        with queue.connect() as db:
            found = db.execute('SELECT revision,attempts,due,reason FROM wakes WHERE id=?', (key,)).fetchone()
        return tuple(found) if found else None
    def audits():
        return cancel.status(str(project), queue=queue)['cancellations']
    def retry(q):
        with q.connect() as db:
            db.execute("UPDATE wakes SET attempts=attempts+1,due=due+300,reason='IndexContractError' WHERE id=?", (key,))
    # Same-revision dispatcher retry before the snapshot: backup no longer matches the captured row.
    before = row_now()
    def retry_then_backup(q, d, k, r):
        retry(q); return real_backup(q, d, k, r)
    with patch.object(cancel, '_backup', retry_then_backup):
        assert run(rev) == {'status': 'refused', 'reason': 'backup_unverified'}
    assert row_now()[0] == rev and row_now() != before and audits() == []
    # Same-revision retry after a verified backup: full-row CAS refuses, retried row survives.
    before = row_now()
    def backup_then_retry(q, d, k, r):
        out = real_backup(q, d, k, r); retry(q); return out
    with patch.object(cancel, '_backup', backup_then_retry):
        assert run(rev) == {'status': 'refused', 'reason': 'wake_changed_before_delete'}
    after_retry = row_now()
    assert after_retry[0] == rev and after_retry[1] == before[1] + 1 and audits() == []
    # A journal or route transition during the backup is caught by the in-transaction re-proof.
    for change, reason in (({'journal': {**journal_view, 'selected': {'installId': 'a' * 32}}},
                            'journal_not_pending_candidate'),
                           ({'journal': {**journal_view, 'epoch': 4}}, 'proof_changed'),
                           ({'route': {'state': 'candidate', 'reason': 'selected'}},
                            'project_route_not_original_qmd')):
        def transition(q, d, k, r, change=change):
            out = real_backup(q, d, k, r); live.update(change); return out
        with patch.object(cancel, '_backup', transition):
            assert run(rev) == {'status': 'refused', 'reason': reason}, (change, reason)
        # The CAS DELETE inside that transaction was rolled back: row and audit unchanged.
        assert row_now() == after_retry and audits() == []
    # The established writer locks (install, then build owner) held elsewhere: refuse, never wait.
    from laya_product_install import _install_lock
    with _install_lock(state):
        assert run(rev) == {'status': 'refused', 'reason': 'state_busy'}
    with initial_build.owner(str(state)) as owned:
        assert owned and run(rev) == {'status': 'refused', 'reason': 'state_busy'}
    assert row_now() == after_retry and audits() == []
    results['fullRowAndLockedProof'] = True

    # Race: a re-enqueue after backup bumps the revision; the newer wake survives.
    def racing(q, d, k, r):
        out = real_backup(q, d, k, r); q.offer('laya-sync', str(project)); return out
    with patch.object(cancel, '_backup', racing):
        assert run(rev) == {'status': 'refused', 'reason': 'wake_changed_before_delete'}
    newer = revision(); assert newer > rev
    assert cancel.status(str(project), queue=queue)['cancellations'] == []
    results['raceKeepsNewer'] = True

    # Busy queue: nothing committed, explicit refusal.
    holder = sqlite3.connect(queue.path); holder.execute('BEGIN IMMEDIATE')
    busy = run(newer)
    holder.rollback(); holder.close()
    assert busy['status'] == 'refused' and busy['reason'] in ('queue_busy', 'backup_unverified'), busy
    assert revision() == newer
    results['busyRefused'] = True

    # Applied: exact row and its coalesced overflow row go; other kinds stay; audit + backup recorded.
    with queue.connect() as db:
        db.execute('INSERT INTO wake_overflow VALUES (?,?,?,?)', (key, 'laya-sync', str(project), 1.0))
    applied = run(newer)
    assert applied['status'] == 'applied' and applied['overflowRemoved'] == 1, applied
    after = cancel.status(str(project), queue=queue)
    assert after['wake'] is None and after['overflow'] is False and len(after['cancellations']) == 1
    audit = after['cancellations'][0]
    assert (audit['revision'], audit['kind'], audit['target']) == (newer, 'laya-sync', str(project))
    assert audit['backup_sha256'] == applied['backup']['sha256'] and audit['proof_sha256'] == applied['proof']['sha256']
    removed = applied['removedRow']
    assert (audit['attempts'], audit['due'], audit['reason']) == (removed['attempts'], removed['due'], removed['reason'])
    copy = Path(applied['backup']['path'])
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600
    with sqlite3.connect(copy.as_uri() + '?mode=ro', uri=True) as db:
        assert db.execute('SELECT revision,status,attempts,due,reason FROM wakes WHERE id=?', (key,)).fetchone() == (
            newer, 'pending', removed['attempts'], removed['due'], removed['reason'])
    with queue.connect() as db:
        kinds = {r[0] for r in db.execute('SELECT kind FROM wakes')}
    assert kinds == {'laya-compile'}, kinds
    results['appliedAudited'] = True

    # Repeat is unambiguous: nothing left to cancel.
    assert run(newer)['status'] == 'unchanged'
    # Recovery is a fresh request: a new, higher revision, never the cancelled one.
    queue.offer('laya-sync', str(project)); assert revision() > newer
    results['recoveryNewRevision'] = True

    # The CLI uses the official queue (no root argument) and refuses a bad revision.
    seen = []
    import contextlib, io
    printed = io.StringIO()
    with patch.object(cancel, 'cancel', side_effect=lambda *a, **k: seen.append((a, k)) or {'status': 'refused'}), \
            contextlib.redirect_stdout(printed):
        assert runtime_work.main(['cancel-build-wake', '--project', str(project), '--revision', 'x',
                                  '--backup-dir', str(backups)]) == 3
    assert json.loads(printed.getvalue()) == {'status': 'refused'}
    assert seen == [((str(project), None, str(backups)), {})], seen
    assert cancel.cancel(str(project), None, str(backups), queue=queue)['reason'] == 'invalid_revision'
    assert runtime_work.main(['cancel-build-wake', '--project', str(project)]) == 2
    results['cliOfficialQueueOnly'] = True

print(json.dumps(results, sort_keys=True))

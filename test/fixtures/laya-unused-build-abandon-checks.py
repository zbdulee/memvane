#!/usr/bin/env python3
"""Abandon of an unused initial build: synthetic temp state only.

HOME, the fixture project, every build store, the journal, the wake queue and
the QMD source file are temp files. The official per-UID queue root is patched
to raise. The journal view and project route are patched at their official
reader names; the stores are the real ones ``_register_initial_build`` makes.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import laya_index_host_route
import laya_initial_build as initial_build
import laya_install_journal
from laya_index_a import IndexContractError, LayaIndexA
import laya_product_install as product
import laya_product_sync_worker as worker
import runtime_wake_cancel as wake_cancel
import runtime_wake_policy as wake_policy
import runtime_work

SCOPE = 'c' * 32
INSTALL = '4' * 32
COMMIT = '5' * 40


def forbidden(*_a, **_k):
    raise AssertionError('official per-UID queue root must not be used in tests')


class AbandonCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='laya-abandon-')
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.base = base
        self.home = base / 'home'
        self.home.mkdir(mode=0o700)
        for patcher in (mock.patch.dict(os.environ, {'HOME': str(self.home), 'QMD_SANDBOX': '1'}),
                        mock.patch.object(runtime_work, 'default_root', forbidden)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.fixture = base / 'qa-root'
        self.fixture.mkdir(mode=0o700)
        self.project = self.fixture / 'project'
        self.wiki = self.project / 'wiki'
        self.wiki.mkdir(parents=True)
        (self.fixture / 'MANIFEST.json').write_text(json.dumps({
            'schema': wake_cancel.FIXTURE_SCHEMA, 'root': str(self.fixture), 'fixtureId': 'f1',
            'paths': {'project': str(self.project)}}))
        (self.fixture / '.qa-fixture-owner').write_text(json.dumps(
            {'fixtureId': 'f1', 'uid': os.getuid()}))
        cursor = self.home
        for name in ('.local', 'state', 'laya', INSTALL):
            cursor = cursor / name
            cursor.mkdir(mode=0o700)
        self.state = cursor
        with closing(sqlite3.connect(self.state / 'install-journal.sqlite')) as db:
            db.execute('CREATE TABLE selection(x)')
            db.commit()
        os.chmod(self.state / 'install-journal.sqlite', 0o600)
        self.route = b'{"synthetic":"route"}'
        route_file = self.state / 'reviewed-route.json'
        route_file.write_bytes(self.route)
        route_file.chmod(0o600)
        self.collections = [{'name': 'wiki', 'role': 'wiki', 'sourceRoot': str(self.wiki)}]
        candidate = self.state / product.BUILD_CANDIDATE
        LayaIndexA(candidate, create=True).close()
        incarnation = product._empty_or_bound_candidate(candidate, SCOPE)
        product._publish_claim_queue(self.state / product.BUILD_QUEUE, incarnation)
        product.prepare_capacity_stores(candidate, {'wiki': str(self.wiki)}, SCOPE, incarnation)
        self.identity = {
            'projectRoot': str(self.project), 'stateRoot': str(self.state), 'installId': INSTALL,
            'scopeId': SCOPE, 'collections': self.collections, 'candidateDb': str(candidate),
            'queueDb': str(self.state / product.BUILD_QUEUE),
            'catalogDb': str(candidate) + '.catalog.sqlite',
            'bindingStore': str(candidate) + '.bindings.sqlite', 'dbIncarnation': incarnation,
            'journalPath': str(self.state / 'install-journal.sqlite'),
            'routeFile': str(route_file), 'routeSha256': hashlib.sha256(self.route).hexdigest(),
            'attestationSha256': 'b' * 64, 'vectorProfileId': 'a' * 64,
            'sourceDb': str(self.project / 'qmd.sqlite'), 'sourceFormat': 'qmd-2.5.3',
            'proposalSha256': 'd' * 64, 'providerProposalSha256': 'e' * 64}
        initial_build.create_or_verify(self.identity)
        initial_build.register_project(self.identity)
        self.journal = {
            'state': 'candidate_ready', 'selected': None, 'epoch': 3, 'newWriteSeen': False,
            'pending': {'installId': INSTALL, 'sourceCommit': COMMIT},
            'projectRoot': str(self.project), 'scopeId': SCOPE,
            'pendingRouteSha256': self.identity['routeSha256'],
            'providerAttestationSha256': None, 'providerFileIdentitySha256': None}
        self.route_view = {'state': 'legacy', 'reason': 'no_route_record'}
        for patcher in (
                mock.patch.object(laya_install_journal, 'inspect_journal',
                                  side_effect=lambda *_a: dict(self.journal)),
                mock.patch.object(laya_index_host_route, 'select_route',
                                  side_effect=lambda *_a, **_k: dict(self.route_view))):
            patcher.start()
            self.addCleanup(patcher.stop)
        qroot = base / 'queue'
        qroot.mkdir(mode=0o700)
        self.queue = runtime_work.WakeQueue(qroot)
        self.key = wake_policy.wake_key('laya-sync', str(self.project))
        self.update_target = hashlib.sha256(str(self.project).encode()).hexdigest()[:32]
        self.queue.offer('laya-sync', str(self.project))
        self.queue.offer('update', self.update_target)
        self.other = base / 'other-project'
        self.other.mkdir()
        self.queue.offer('laya-sync', str(self.other))

    # -- helpers ---------------------------------------------------------------

    def revision(self, kind='laya-sync', target=None):
        owner = self.update_target if kind == 'update' else str(target or self.project)
        key = wake_policy.wake_key(kind, owner)
        with self.queue.connect() as db:
            row = db.execute('SELECT revision FROM wakes WHERE id=?', (key,)).fetchone()
        return row[0] if row else None

    def abandon(self, **over):
        args = {'project_root': str(self.project), 'state_root': str(self.state),
                'install_id': INSTALL, 'commit_id': COMMIT,
                'wake_revision': self.revision(), 'queue': self.queue}
        args.update(over)
        return product.abandon_unused_initial_build(**args)

    def abandon_inactive(self, **over):
        args = {'project_root': str(self.project), 'state_root': str(self.state),
                'install_id': INSTALL, 'commit_id': COMMIT,
                'wake_revision': self.revision(), 'queue': self.queue}
        args.update(over)
        return product.abandon_inactive_initial_build(**args)

    def partly_built(self):
        initial_build.advance(initial_build.read(self.state), phase='building',
                              progress={'state': 'pending'},
                              freshness={'requestedNs': 123})
        self.journal['providerAttestationSha256'] = 'a' * 64
        self.journal['providerFileIdentitySha256'] = 'b' * 64
        with closing(sqlite3.connect(self.identity['candidateDb'])) as db:
            db.execute("INSERT INTO content(hash, body) VALUES('wiki-only-hash','wiki card')")
            db.commit()

    def test_partly_built_inactive_candidate_is_archived_without_deleting_content(self):
        self.partly_built()
        result = self.abandon_inactive()
        self.assertEqual((result['status'], result['buildPhase']),
                         ('inactive_build_retired', 'retired'))
        self.assertIsNone(initial_build.lookup(self.project))
        with closing(sqlite3.connect(self.identity['candidateDb'])) as db:
            self.assertEqual(db.execute('SELECT body FROM content').fetchone()[0], 'wiki card')
        receipt = self.state / product.INACTIVE_RETIREMENT
        self.assertEqual(json.loads((receipt / 'receipt.json').read_text())['schema'],
                         product.INACTIVE_RECEIPT_SCHEMA)
        with closing(sqlite3.connect(receipt / 'candidate.sqlite')) as db:
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('SELECT body FROM content').fetchone()[0], 'wiki card')
        self.assertEqual(self.abandon_inactive(wake_revision=None)['replayed'], True)
        self.assertIsNotNone(self.revision('laya-sync', self.other))
        self.assertIsNotNone(self.revision('update'))

    def test_partly_built_generation_fts_is_backed_up_without_deleting_content(self):
        self.partly_built()
        index = LayaIndexA(self.identity['candidateDb'])
        try:
            self.assertEqual(index.stage('g-synthetic', SCOPE,
                                         [('wiki/topic.md', 'verified wiki card', True)]),
                             'prepared')
        finally:
            index.close()
        result = self.abandon_inactive()
        self.assertEqual(result['buildPhase'], 'retired')
        for path in (Path(self.identity['candidateDb']),
                     self.state / product.INACTIVE_RETIREMENT / 'candidate.sqlite'):
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM gen_index').fetchone()[0], 1)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM gen_fts_1').fetchone()[0], 1)

    def test_partly_built_generation_still_refuses_unrecognized_table(self):
        self.partly_built()
        index = LayaIndexA(self.identity['candidateDb'])
        try:
            index.stage('g-synthetic', SCOPE, [('wiki/topic.md', 'wiki card', True)])
        finally:
            index.close()
        with closing(sqlite3.connect(self.identity['candidateDb'])) as db:
            db.execute('CREATE TABLE unexpected_work (body TEXT)')
            db.commit()
        with self.assertRaisesRegex(product.InstallBlocked,
                                    'unused_build_store_schema_unrecognized'):
            self.abandon_inactive()
        self.assertIsNotNone(initial_build.lookup(self.project))

    def test_original_unused_path_still_refuses_partial(self):
        self.partly_built()
        with self.assertRaisesRegex(product.InstallBlocked, 'unused_build_not_registered'):
            self.abandon()

    def test_original_unused_receipt_replays_without_new_proof_fields(self):
        # A receipt written by the older registered-only command must still
        # replay after an upgrade; the new proof fields belong only to the
        # separate inactive-candidate recovery command.
        with mock.patch.object(initial_build, 'advance', side_effect=RuntimeError('interrupted')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                self.abandon()
        receipt = self.state / product.UNUSED_RETIREMENT / 'receipt.json'
        stored = json.loads(receipt.read_text())
        self.assertNotIn('sourceIdentity', stored['stores']['candidateDb'])
        self.assertEqual(self.abandon()['status'], 'unused_build_retired')

    def test_inactive_recovery_refuses_sealed_candidate_and_other_receipt(self):
        self.partly_built()
        initial_build.advance(initial_build.read(self.state), phase='sealed',
                              sealed={key: 'a' * 64 for key in initial_build.SEALED_FIELDS})
        with self.assertRaisesRegex(product.InstallBlocked, 'unused_build_not_registered'):
            self.abandon_inactive()
        initial_build.advance(initial_build.read(self.state), phase='building', sealed=None)
        other = self.state / product.UNUSED_RETIREMENT
        other.mkdir(mode=0o700)
        with self.assertRaisesRegex(product.InstallBlocked, 'unused_build_conflicting_receipt'):
            self.abandon_inactive()

    def test_partly_built_refuses_selection_first_write_and_bound_ledger(self):
        import laya_qmd_cutover
        self.partly_built()
        for key, value in [('selected', {'installId': INSTALL}), ('newWriteSeen', True)]:
            old = self.journal[key]
            self.journal[key] = value
            with self.assertRaises(product.InstallBlocked):
                self.abandon_inactive()
            self.journal[key] = old
        with mock.patch.object(laya_qmd_cutover, 'inspect_bound_cutover', return_value={'phase':'prepared'}):
            with self.assertRaisesRegex(product.InstallBlocked, 'inactive_build_cutover_started'):
                self.abandon_inactive()
        self.assertEqual(initial_build.read(self.state)['phase'], 'building')
        self.assertFalse((self.state / product.INACTIVE_RETIREMENT).exists())

    def test_partly_built_refuses_active_claim_and_wake_race(self):
        self.partly_built()
        with closing(sqlite3.connect(self.identity['queueDb'])) as db:
            db.execute("INSERT INTO jobs VALUES('c','s','g','m','d',1,NULL,'pending',NULL,NULL)")
            db.commit()
        with self.assertRaisesRegex(product.InstallBlocked, 'inactive_build_claim_active'):
            self.abandon_inactive()
        with closing(sqlite3.connect(self.identity['queueDb'])) as db:
            db.execute("UPDATE jobs SET state='acked' WHERE claim='c'")
            db.commit()
        with self.queue.connect() as db:
            db.execute("UPDATE wakes SET status='running' WHERE id=?", (self.key,))
        with self.assertRaises(product.InstallBlocked):
            self.abandon_inactive()
        self.assertEqual(initial_build.read(self.state)['phase'], 'building')

    def test_partly_built_wal_backup_and_same_count_drift_refusal(self):
        self.partly_built()
        db = sqlite3.connect(self.identity['candidateDb'])
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA wal_autocheckpoint=0')
            db.execute('PRAGMA user_version=2')
            db.commit()
            self.assertTrue(Path(self.identity['candidateDb'] + '-wal').exists())
            with self.assertRaises(product.InstallBlocked):
                self.abandon_inactive(wake_revision=self.revision() + 1)
            receipt = self.state / product.INACTIVE_RETIREMENT
            with closing(sqlite3.connect(receipt / 'candidate.sqlite')) as backed:
                self.assertEqual(backed.execute('PRAGMA quick_check').fetchone()[0], 'ok')
                self.assertEqual(backed.execute('SELECT body FROM content').fetchone()[0],
                                 'wiki card')
                self.assertEqual(backed.execute('PRAGMA user_version').fetchone()[0], 2)
            db.execute('PRAGMA user_version=3')
            db.commit()
            with self.assertRaisesRegex(product.InstallBlocked, 'unused_build_store_changed'):
                self.abandon_inactive()
            self.assertEqual(initial_build.read(self.state)['phase'], 'building')
            self.assertIsNotNone(initial_build.lookup(self.project))
        finally:
            db.close()

    def untouched(self, reason=None):
        """Registered record, pointer and the other wakes intact; no receipt move."""
        record = initial_build.read(self.state)
        self.assertEqual(record['phase'], 'registered')
        self.assertIsNotNone(initial_build.lookup(self.project))
        self.assertIsNotNone(self.revision('laya-sync', self.other))
        self.assertIsNotNone(self.revision('update'))

    def refuse(self, reason, **over):
        with self.assertRaises(product.InstallBlocked) as caught:
            self.abandon(**over)
        self.assertEqual(str(caught.exception), reason)
        self.untouched()

    def listing(self):
        return sorted(path.name for path in self.state.iterdir())

    # -- success and idempotent replay ----------------------------------------------

    def test_success_backs_up_cancels_wake_retires_and_replays(self):
        before = self.listing()
        revision = self.revision()
        result = self.abandon()
        self.assertEqual((result['status'], result['buildPhase'], result['buildPointer'],
                          result['wake']), ('unused_build_retired', 'retired', 'retired',
                                            {'status': 'applied', 'revision': revision}))
        record = initial_build.read(self.state)
        self.assertEqual(record['phase'], 'retired')
        self.assertIsNone(initial_build.lookup(self.project))
        # Old state files all remain; only the receipt dir and pointer archive are new.
        self.assertEqual(sorted(set(self.listing()) - set(before)),
                         ['initial-build.lock', 'laya-install.lock',
                          initial_build.RETIRED_POINTER, product.UNUSED_RETIREMENT])
        receipt_dir = self.state / product.UNUSED_RETIREMENT
        receipt = json.loads((receipt_dir / 'receipt.json').read_text())
        self.assertEqual(receipt['recordEpoch'], 1)
        for name, digest in receipt['files'].items():
            self.assertEqual(product._sha256_file(receipt_dir / name), digest, name)
        with closing(sqlite3.connect(receipt_dir / 'journal.sqlite')) as db:
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0], 'ok')
        # Only this project's laya-sync wake went; other project and update stay.
        self.assertIsNone(self.revision())
        self.assertIsNotNone(self.revision('laya-sync', self.other))
        self.assertIsNotNone(self.revision('update'))
        self.assertEqual(len(wake_cancel.status(str(self.project), queue=self.queue)['cancellations']), 1)
        # A worker that knows the phase ACKs; the record is terminal.
        self.assertEqual(worker.run_initial_build(self.project, record)['state'], 'unchanged')
        with self.assertRaises(IndexContractError):
            initial_build.advance(record, phase='building')
        with self.assertRaises(IndexContractError):
            initial_build.request_freshness(record, None)
        with self.assertRaises(product.InstallBlocked):
            product.finalize_initial_build(project_root=str(self.project), state_root=self.state)
        # Replay is a no-op that reports the same retirement; a fresh install can register.
        again = self.abandon()
        self.assertEqual((again['replayed'], again['changesApplied'], again['buildPointer']),
                         (True, 0, 'absent'))
        next_state = self.home / '.local' / 'state' / 'laya' / ('6' * 32)
        next_state.mkdir(mode=0o700)
        fresh = {**self.identity, 'stateRoot': str(next_state), 'installId': '6' * 32}
        for key in ('candidateDb', 'queueDb', 'catalogDb', 'bindingStore',
                    'journalPath', 'routeFile'):
            fresh[key] = str(next_state) + self.identity[key][len(str(self.state)):]
        self.assertEqual(initial_build.lookup(self.project), None)
        initial_build.register_project(fresh)

    def test_unsupported_old_worker_fails_closed_on_retired_phase(self):
        self.abandon()
        with mock.patch.object(initial_build, 'PHASES',
                               ('registered', 'building', 'sealed', 'blocked', 'bound')):
            with self.assertRaises(IndexContractError) as caught:
                initial_build.read(self.state)
        self.assertEqual(str(caught.exception), 'initial_build_record_invalid')

    def test_success_without_pending_wake(self):
        with self.queue.connect() as db:
            db.execute('DELETE FROM wakes WHERE id=?', (self.key,))
        result = self.abandon(wake_revision=None)
        self.assertIsNone(result['wake'])
        self.assertEqual(initial_build.read(self.state)['phase'], 'retired')

    def test_orphaned_overflow_refuses_without_auditable_wake(self):
        with self.queue.connect() as db:
            db.execute('DELETE FROM wakes WHERE id=?', (self.key,))
            db.execute('INSERT INTO wake_overflow VALUES (?,?,?,?)',
                       (self.key, 'laya-sync', str(self.project), 1.0))
        with self.assertRaises(product.InstallBlocked) as caught:
            self.abandon(wake_revision=None)
        self.assertEqual(str(caught.exception), 'unused_build_wake_overflow_present')
        self.assertEqual(initial_build.read(self.state)['phase'], 'registered')
        self.assertFalse((self.state / product.UNUSED_RETIREMENT).exists())

    def test_empty_wal_store_has_verified_standalone_backup(self):
        db = sqlite3.connect(self.identity['candidateDb'])
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA wal_autocheckpoint=0')
            db.execute('PRAGMA user_version=3')
            db.commit()
            self.assertTrue(Path(self.identity['candidateDb'] + '-wal').exists())
            self.assertEqual(self.abandon()['buildPhase'], 'retired')
        finally:
            db.close()
        target = self.state / product.UNUSED_RETIREMENT / 'candidate.sqlite'
        self.assertTrue(target.exists())
        self.assertFalse(Path(str(target) + '-wal').exists())
        self.assertFalse(Path(str(target) + '-shm').exists())

    def test_unknown_store_table_refuses(self):
        with closing(sqlite3.connect(self.identity['candidateDb'])) as db:
            db.execute('CREATE TABLE unknown_work (body TEXT)')
            db.commit()
        self.refuse('unused_build_store_schema_unrecognized')

    # -- refusals: nothing mutated ---------------------------------------------------

    def test_progress_selected_first_write_and_building_refuse(self):
        record = initial_build.read(self.state)
        for changes in ({'phase': 'building', 'progress': {'state': 'pending'}},
                        {'freshness': {'requestedNs': 1}}):
            current = initial_build.read(self.state)
            initial_build.advance(current, **changes)
            with self.assertRaises(product.InstallBlocked) as caught:
                self.abandon()
            self.assertIn(str(caught.exception),
                          ('unused_build_not_registered', 'unused_build_has_progress'))
            self.assertIsNotNone(initial_build.lookup(self.project))
            self.assertFalse((self.state / product.UNUSED_RETIREMENT).exists())
            # restore a pristine registered record for the next case
            initial_build.advance(initial_build.read(self.state), phase='registered',
                                  progress=None, freshness=None)
        self.assertEqual(initial_build.read(self.state)['identity'], record['identity'])
        for field, value, reason in (
                ('selected', {'installId': INSTALL}, 'unused_build_journal_changed'),
                ('newWriteSeen', True, 'unused_build_journal_changed'),
                ('state', 'active', 'unused_build_journal_not_pending_candidate'),
                ('providerAttestationSha256', 'f' * 64, 'unused_build_journal_changed')):
            self.journal[field] = value
            with self.assertRaises(product.InstallBlocked) as caught:
                self.abandon()
            self.assertIn(str(caught.exception),
                          (reason, 'unused_build_journal_changed',
                           'unused_build_journal_not_pending_candidate'))
            self.assertFalse((self.state / product.UNUSED_RETIREMENT).exists())
            self.journal[field] = {'selected': None, 'newWriteSeen': False,
                                   'state': 'candidate_ready',
                                   'providerAttestationSha256': None}[field]
        self.untouched()

    def test_wrong_identity_inputs_refuse(self):
        with self.assertRaises(product.InstallBlocked) as caught:
            self.abandon(install_id='7' * 32)
        self.assertEqual(str(caught.exception), 'unused_build_state_not_canonical')
        self.refuse('unused_build_journal_changed', commit_id='6' * 40)
        self.refuse('unused_build_commit_invalid', commit_id='short')
        self.refuse('unused_build_state_not_canonical', state_root=str(self.base))
        self.route_view = {'state': 'candidate'}
        self.refuse('unused_build_project_route_not_original_qmd')
        self.route_view = {'state': 'legacy', 'reason': 'no_route_record'}
        marker = self.fixture / '.qa-fixture-owner'
        saved = marker.read_text()
        marker.write_text(json.dumps({'fixtureId': 'other', 'uid': os.getuid()}))
        self.refuse('unused_build_fixture_marker_invalid')
        marker.write_text(saved)
        # A pointer naming a different install or state root is never retired.
        pointer = initial_build.pointer_path(self.project, initial_build._pointer_dir(create=False))
        original = pointer.read_bytes()
        pointer.write_bytes(original.replace(INSTALL.encode(), b'8' * 32))
        with self.assertRaises(product.InstallBlocked):
            self.abandon()
        pointer.write_bytes(original)
        self.untouched()
        route = self.state / 'reviewed-route.json'
        route.write_bytes(b'{"changed":1}')
        self.refuse('initial_build_route_changed')
        route.write_bytes(self.route)
        self.assertFalse((self.state / product.UNUSED_RETIREMENT).exists())

    def test_wal_rows_and_unreadable_or_corrupt_stores_refuse(self):
        # An uncheckpointed WAL row in the candidate DB must count (WAL-aware proof).
        db = sqlite3.connect(self.identity['candidateDb'])
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA wal_autocheckpoint=0')
            db.execute("INSERT INTO content(hash, body) VALUES('h','body')")
            db.commit()
            self.assertTrue(Path(self.identity['candidateDb'] + '-wal').exists())
            self.refuse('unused_build_has_work')
        finally:
            db.close()
        # A claim in the queue is progress too.
        queue_db = sqlite3.connect(self.identity['queueDb'])
        try:
            queue_db.execute("INSERT INTO jobs VALUES('c','s','g','m','d',1,NULL,'pending',NULL,NULL)")
            queue_db.commit()
        finally:
            queue_db.close()
        self.refuse('unused_build_has_work')

    def test_corrupt_catalog_refuses(self):
        Path(self.identity['catalogDb']).write_bytes(b'not sqlite' * 20)
        with self.assertRaises(product.InstallBlocked) as caught:
            self.abandon()
        self.assertEqual(str(caught.exception), 'unused_build_store_unreadable')
        self.assertFalse((self.state / product.UNUSED_RETIREMENT).exists())

    # -- locks, races and interrupted replay -----------------------------------------

    def test_lock_order_and_busy_refusals(self):
        with product._install_lock(self.state):
            with self.assertRaises(product.InstallBlocked) as caught:
                self.abandon()
            self.assertEqual(str(caught.exception), 'install_busy')
        with initial_build.owner(self.state) as owned:
            self.assertTrue(owned)
            self.refuse('initial_build_busy')
        self.untouched()

    def test_wake_refusals_then_replay_reuses_receipt(self):
        revision = self.revision()
        self.refuse('unused_build_wake_revision_required', wake_revision=None)
        self.assertFalse((self.state / product.UNUSED_RETIREMENT).exists())
        self.refuse('unused_build_wake_revision_changed', wake_revision=revision + 5)
        receipt = (self.state / product.UNUSED_RETIREMENT / 'receipt.json').read_bytes()
        with self.queue.connect() as db:
            db.execute("UPDATE wakes SET status='running' WHERE id=?", (self.key,))
        self.refuse('unused_build_wake_wake_not_pending')
        with self.queue.connect() as db:
            db.execute("UPDATE wakes SET status='pending' WHERE id=?", (self.key,))
        self.assertEqual(self.revision(), revision)
        self.assertEqual(self.abandon()['buildPhase'], 'retired')
        self.assertEqual((self.state / product.UNUSED_RETIREMENT / 'receipt.json').read_bytes(),
                         receipt)

    def test_record_changed_after_proof_refuses_before_retire(self):
        real = product._unused_store_proof
        calls = []

        def racing(identity):
            out = real(identity)
            calls.append(1)
            if len(calls) == 2:
                initial_build.advance(initial_build.read(self.state), phase='building',
                                      progress={'state': 'pending'})
            return out

        with mock.patch.object(product, '_unused_store_proof', racing):
            with self.assertRaises(product.InstallBlocked) as caught:
                self.abandon()
        self.assertEqual(str(caught.exception), 'unused_build_changed')
        self.assertEqual(initial_build.read(self.state)['phase'], 'building')
        self.assertIsNotNone(initial_build.lookup(self.project))

    def test_tampered_receipt_refuses_replay(self):
        revision = self.revision()
        self.refuse('unused_build_wake_revision_changed', wake_revision=revision + 5)
        backup = self.state / 'unused-build-retirement' / 'initial-build.json'
        backup.chmod(0o600)
        backup.write_bytes(b'{}')
        self.refuse('unused_build_receipt_mismatch')

    def test_crash_after_wake_cancel_and_after_record_retire_replay(self):
        with mock.patch.object(initial_build, 'advance', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                self.abandon()
        self.untouched = None
        self.assertIsNone(self.revision())  # wake already cancelled
        self.assertEqual(initial_build.read(self.state)['phase'], 'registered')
        with mock.patch.object(initial_build, 'retire_project_pointer',
                               side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                self.abandon(wake_revision=None)
        self.assertEqual(initial_build.read(self.state)['phase'], 'retired')
        self.assertIsNotNone(initial_build.lookup(self.project))
        hint = product.retired_build_pointer(self.project)
        self.assertEqual(hint['nextAction'], 'abandon-index-build')
        # A wake that raced in meanwhile ACKs on the retired record, never steps.
        self.assertEqual(worker.run_initial_build(self.project, initial_build.lookup(self.project)
                                                  )['reason'], 'initial_build_retired')
        done = self.abandon(wake_revision=None)
        self.assertEqual((done['replayed'], done['buildPointer']), (True, 'retired'))
        self.assertIsNone(initial_build.lookup(self.project))
        self.assertIsNone(product.retired_build_pointer(self.project))

    def test_other_pointer_and_other_project_never_touched(self):
        other_pointer = initial_build.pointer_path(self.other, initial_build._pointer_dir(create=False))
        other_pointer.write_text('{"other":1}')
        other_pointer.chmod(0o600)
        self.abandon()
        self.assertEqual(other_pointer.read_text(), '{"other":1}')


if __name__ == '__main__':
    unittest.main()

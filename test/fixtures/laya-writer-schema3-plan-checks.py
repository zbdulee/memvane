#!/usr/bin/env python3
"""Selected schema3 writer plan: store identity, v6 parity and refusals.

The schema3 record is issued by the U10 ``seal_schema3_endpoint`` over a real
view store head and read back by the real ``_stored_endpoint``; everything else
is the real v5 install. Selections are written with ``mutate_image`` (no
loaded-bridge CAS), so no parity receipt is needed.
"""
from contextlib import ExitStack, closing, contextmanager
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import unittest
from unittest import mock
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('schema3_writer_seed', ROOT/'test/fixtures/laya-writer-scope-checks.py')
existing = importlib.util.module_from_spec(spec); spec.loader.exec_module(existing)

INSTALL = ('scope', 'install_id', 'project', 'project_node', 'state', 'state_node',
           'attestation_sha256', 'profile', 'settings', 'roots')
STORE = ('store_format', 'capability', 'checkout_policy', 'incarnation', 'db_path', 'db_node',
         'repository_digest', 'worktree_id', 'worktree_binding', 'settings_policy_digest',
         'vector_profile', 'vector_recipe', 'head_floor', 'parity', 'creation_epoch')
PREFIX = b'memvane-reader-endpoint-v1\0'


def emit(label, **values):
    print(json.dumps(dict(case=label, **values), sort_keys=True), flush=True)


class Schema3WriterPlanChecks(unittest.TestCase):
    def setUp(self):
        self.fixture = existing.WriterScopeChecks('runTest')
        self.addCleanup(self.fixture.doCleanups); self.fixture.setUp()
        for name in ('F', 'seed', 'case', 'P', 'W', 'J', 'sync', 'queue', 'plan'):
            setattr(self, name, getattr(self.fixture, name))
        self.N = importlib.import_module('laya_writer_scope')
        self.R = importlib.import_module('laya_journal_reader')
        project = self.case.project
        git = ['git', '-C', str(project), '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=' + os.devnull,
               '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid']
        subprocess.run(['git', '-c', 'init.defaultBranch=main', 'init', '-q', str(project)],
                       check=True, capture_output=True, timeout=10)
        subprocess.run(git + ['commit', '--allow-empty', '-qm', 'fixture'],
                       check=True, capture_output=True, timeout=10)
        time.sleep(1.1)  # Git racy-index window before the checkout capture.
        checkout = importlib.import_module('memvane.store.checkout_view')
        views = importlib.import_module('memvane.store.view_store')
        self.repository = checkout.register_repository(str(project))
        self.worktree = checkout.register_worktree(str(project), self.repository)
        self.store_path = self.case.state / 'writer-schema3-store.sqlite'
        scope = self.case.route['scopeId']
        views.ViewStore.create(self.store_path, project_root=str(project), repository=self.repository,
                               worktree=self.worktree, scope_id=scope).close()
        store = views.ViewStore.open(self.store_path, repository=self.repository, scope_id=scope)
        try:
            self.store_incarnation, self.store_policy = store._incarnation, store._policy
            # One published (empty) generation: the seal pins the view head.
            handle = store.capture_view(self.worktree['worktreeId'])
            head = store.head(handle)
            generation = store.prepare_generation(handle, dict(
                scan_id='6' * 32, policy_digest=handle.policy_digest, revocation_epoch=0,
                complete=True, sources=[], members=[]), expected_head=head)
            store.publish_generation(handle, generation, expected_head=head)
        finally:
            store.close()
        with closing(sqlite3.connect(self.store_path.as_uri() + '?mode=ro', uri=True)) as db:
            self.binding = db.execute('SELECT binding_digest FROM worktrees WHERE worktree_id=?',
                                      (self.worktree['worktreeId'],)).fetchone()[0]
            repository_json = db.execute('SELECT repository_json FROM store_meta').fetchone()[0]
        self.repository_digest = self.R._repository_digest(repository_json)
        self.v6_content = {Path(self.case.db), Path(self.case.catalog), Path(self.case.store),
                           Path(self.seed.sidecar), Path(self.queue)}
        _selection, self.v6_record, self.v6_attestation = self.current()
        self.assertEqual(self.v6_record['store_format'], 'v6')
        with self.seed.authority():
            installer = self.R.seal_current_reader(self.case.state, project, executing_root=self.case.plugin)
            self.sealed = self.R.seal_schema3_endpoint(installer, store_path=self.store_path, parity=dict(
                migration_id='7' * 32, generation=generation, digest='8' * 64))
        sealed = self.sealed.record
        self.assertEqual(set(sealed), set(INSTALL + STORE))
        self.assertEqual((sealed['capability'], sealed['checkout_policy']),
                         ('schema3-ro-v1', 'selected-main-schema3-v1'))
        self.assertEqual((sealed['incarnation'], sealed['worktree_id'], sealed['worktree_binding'],
                          sealed['settings_policy_digest'], sealed['repository_digest']),
                         (self.store_incarnation, self.worktree['worktreeId'], self.binding,
                          self.store_policy, self.repository_digest))
        self.assertEqual(sealed['attestation_sha256'], self.R._sha(self.v6_attestation))

    # -- journal fixture ------------------------------------------------------
    def current(self):
        # _selection also validates the previous endpoint, which may be schema3.
        with self.R._readonly(self.case.journal, self.case.state) as db:
            selection, record = self.R._selection(db)
            attestation = db.execute('SELECT attestation FROM read_endpoints WHERE endpoint_id=?',
                                     (selection.preferred_endpoint,)).fetchone()[0]
        return selection, record, attestation

    def schema3_record(self, **override):
        """The sealed U10 record, optionally with store fields changed after sealing."""
        packed = self.R._issued[self.sealed][4]  # exact sealed record bytes
        self.assertEqual(self.R._sha(PREFIX + packed), self.sealed.endpoint_id)
        record = json.loads(packed)
        record.update(override)
        return record, self.v6_attestation

    def select(self, endpoint_id, transition, record=None, attestation=None):
        def operation(db):
            current, _ = self.R._selection(db)
            if record is not None:
                db.execute('INSERT OR IGNORE INTO read_endpoints VALUES(?,?,?)',
                           (endpoint_id, self.R._raw(record).decode('utf8'), attestation))
            db.execute('INSERT INTO reader_transitions VALUES(?,?,?,?)',
                       (transition, current.epoch + 1, endpoint_id, current.preferred_endpoint))
            db.execute('UPDATE reader_selection SET epoch=?,preferred_endpoint=?,previous_endpoint=?,'
                       'transition_id=? WHERE id=1',
                       (current.epoch + 1, endpoint_id, current.preferred_endpoint, transition))
            return True
        self.R.mutate_image(self.case.journal, operation)
        return self.current()[0]

    def select_schema3(self, transition='5' * 32, **override):
        record, attestation = self.schema3_record(**override)
        endpoint_id = self.R._sha(PREFIX + self.R._raw(record))
        previous = self.current()[0].preferred_endpoint
        self.select(endpoint_id, transition, record, attestation)
        return endpoint_id, previous, record

    @contextmanager
    def traced(self):
        calls = []
        actual = sqlite3.connect
        watched = self.v6_content | {self.store_path}
        def connect(database, *args, **kwargs):
            raw = str(database)
            path = Path(unquote(urlparse(raw).path)) if raw.startswith('file:') else Path(raw)
            if path in watched:
                calls.append(str(path))
            return actual(database, *args, **kwargs)
        with mock.patch.object(sqlite3, 'connect', side_effect=connect):
            yield calls

    def capture(self):
        return self.P.capture_writer_plan(self.case.project, executing_root=self.case.plugin)

    def expected_store(self):
        return dict(path=str(self.store_path), node=self.R._file(self.store_path, self.case.state),
                    incarnation=self.store_incarnation, worktree_id=self.worktree['worktreeId'],
                    repository=self.repository, repository_digest=self.repository_digest,
                    worktree_binding=self.binding, policy_digest=self.store_policy)

    # -- checks ---------------------------------------------------------------
    def test_schema3_selection_issues_store_plan_and_v6_plan_bytes_return_after_rollback(self):
        v6_plan = self.capture()
        v6_packed, v6_snapshot = self.P._plan_record(v6_plan)
        self.assertNotIn('store', v6_snapshot)
        self.assertIsNone(self.P.plan_store(v6_plan))
        schema3_id, v6_id, record = self.select_schema3()
        with self.traced() as calls:
            plan = self.capture()
            self.P.validate_writer_plan(plan)
        _packed, snapshot = self.P._plan_record(plan)
        self.assertEqual(snapshot['endpoint']['store_format'], 'schema3')
        self.assertEqual(snapshot['endpoint_id'], schema3_id)
        self.assertEqual(snapshot['store'], self.expected_store())
        self.assertEqual(self.P.plan_store(plan), self.expected_store())
        self.assertEqual(snapshot['components'], v6_snapshot['components'])
        self.assertEqual(plan.queue_path, v6_plan.queue_path)
        v6_connects = [path for path in calls if Path(path) in self.v6_content]
        store_connects = [path for path in calls if Path(path) == self.store_path]
        self.assertEqual(v6_connects, [])
        self.assertEqual(len(store_connects), 2)  # one metadata read per _snapshot
        with self.assertRaisesRegex(ValueError, 'writer_plan_changed'):
            self.P.validate_writer_plan(v6_plan)
        self.select(v6_id, '6' * 32)
        with self.assertRaisesRegex(ValueError, 'writer_plan_changed'):
            self.P.validate_writer_plan(plan)
        # v6 preferred again; the reader still validates the schema3 previous.
        again = self.capture()
        again_packed, again_snapshot = self.P._plan_record(again)
        normalized = json.loads(again_packed)
        normalized['reader_epoch'] = v6_snapshot['reader_epoch']
        self.assertEqual(self.R._raw(normalized), v6_packed)
        emit('schema3-writer-plan', storeConnects=len(store_connects), v6Connects=len(v6_connects),
             rollbackPlanInvalid=True, v6BytesAfterRollback=True)

    def test_store_identity_change_and_unknown_format_refuse_plan(self):
        self.select_schema3()
        plan = self.capture()
        saved = self.store_path.with_name('writer-schema3-saved.sqlite')
        os.rename(self.store_path, saved)
        try:
            shutil.copy2(saved, self.store_path); os.chmod(self.store_path, 0o600)
            with self.assertRaisesRegex(ValueError, 'writer_plan_store_changed'):
                self.P.validate_writer_plan(plan)
            with self.assertRaisesRegex(ValueError, 'writer_plan_store_changed'):
                self.capture()
        finally:
            self.store_path.unlink(); os.rename(saved, self.store_path)
        self.P.validate_writer_plan(plan)
        for label, override in (('incarnation', dict(incarnation='9' * 32)),
                                ('repository', dict(repository_digest='9' * 64)),
                                ('binding', dict(worktree_binding='9' * 64)),
                                ('policy', dict(settings_policy_digest='9' * 64)),
                                ('worktree', dict(worktree_id='9' * 32)),
                                ('v6_db', dict(db_path=str(self.case.db),
                                               db_node=self.R._file(Path(self.case.db), self.case.state)))):
            with self.subTest(field=label):
                self.select_schema3(transition=hashlib.sha256(label.encode()).hexdigest()[:32], **override)
                with self.assertRaisesRegex(ValueError, 'writer_plan_store_changed'):
                    self.capture()
        # An unknown checkout policy fails the U10 record shape: the journal
        # image refuses the selection, so no writer plan can ever see it.
        before = Path(self.case.journal).read_bytes()
        with self.assertRaisesRegex(ValueError, 'reader_endpoint_invalid'):
            self.select_schema3(transition='4' * 32, checkout_policy='selected-main-v6')
        self.assertEqual(Path(self.case.journal).read_bytes(), before)
        emit('schema3-writer-store-identity', refusals=7)

    def v6_files(self):
        result = {}
        for path in sorted(self.v6_content):
            for suffix in ('', '-wal', '-journal'):
                side = Path(str(path) + suffix)
                result[str(side)] = side.read_bytes() if side.exists() else None
        return result

    def test_v6_consumers_refuse_schema3_selection(self):
        worker = importlib.import_module('laya_product_sync_worker')
        decision = self.J.select_route(str(self.case.project), action='index')
        self.select_schema3()
        epoch = self.J.inspect_journal(self.case.journal)['epoch']
        with self.F.warm(), self.seed.authority(), ExitStack() as owners, \
                mock.patch.object(worker, '_writer_executing_root', return_value=self.case.plugin):
            owner = owners.enter_context(self.sync.sync_owner(self.queue))
            proof = worker._selected_v5_proof(self.case.project, decision, owner)
            files = self.v6_files()
            with self.assertRaisesRegex(ValueError, 'writer_selection_unavailable'):
                with worker._full_worker_runtime(decision, owner, proof,
                                                 {self.case.WIKI: str(self.case.auto / 'wiki')}):
                    self.fail('v6 runtime opened under a schema3 selection')
            plan = self.capture()
            writer = owners.enter_context(self.N.worker_writer_scope(
                plan, queue_owner=owner, operation_timeout=2))
            for role in ('base', 'queue', 'catalog', 'binding', 'sidecar'):
                with self.subTest(role=role):
                    with self.assertRaisesRegex(ValueError, 'writer_selection_unavailable'):
                        self.W.component_for(writer, role)
                    with self.assertRaisesRegex(ValueError, 'writer_selection_unavailable'):
                        self.N._connection_check(writer, role, plan_path(self.P, plan, role))
            # Route proof reads v6 identity read-only; no v6 byte may change.
            self.assertEqual(self.v6_files(), files)
        self.assertEqual(self.J.inspect_journal(self.case.journal)['epoch'], epoch)
        emit('schema3-v6-consumers-refuse', v6Runtime='writer_selection_unavailable', v6BytesUnchanged=True)

    def test_upgrading_fence_refuses_schema3_plan(self):
        fresh = importlib.import_module('laya_fresh_wiki_origin')
        self.select_schema3()
        ledger = self.case.state / fresh.LEDGER
        plan = self.capture()
        row = fresh._load(ledger)
        fenced = fresh._change(ledger, row['epoch'], 'active', 'upgrading')
        errors = []
        for call in (lambda: self.P.validate_writer_plan(plan), self.capture):
            with self.assertRaises(ValueError) as caught:
                call()
            errors.append(str(caught.exception))
        fresh._change(ledger, fenced['epoch'], 'upgrading', 'active')
        with self.assertRaisesRegex(ValueError, 'writer_plan_changed'):
            self.P.validate_writer_plan(plan)
        self.assertEqual(self.P.plan_store(self.capture()), self.expected_store())
        self.assertTrue(all(error in ('writer_route_unavailable', 'writer_plan_binding_changed')
                            for error in errors), errors)
        emit('schema3-writer-upgrading-fence', reasons=errors)

    def test_schema3_writer_marks_first_write_before_store_use(self):
        self.select_schema3()
        with self.F.warm(), self.seed.authority(), ExitStack() as owners:
            owner = owners.enter_context(self.sync.sync_owner(self.queue))
            plan = self.capture()
            writer = owners.enter_context(self.N.worker_writer_scope(
                plan, queue_owner=owner, operation_timeout=2))
            self.assertEqual(self.N.writer_store(writer), self.expected_store())
            before = self.J.inspect_journal(self.case.journal)
            files = self.v6_files()
            proof = self.N.mark_new_write(writer)
            after = self.J.inspect_journal(self.case.journal)
            self.assertEqual(after['epoch'], before['epoch'] + 1)
            self.assertTrue(after['newWriteSeen'])
            self.assertEqual(proof['selectionEpoch'], after['epoch'])
            self.assertEqual(self.N.writer_store(writer), self.expected_store())
            self.assertEqual(self.v6_files(), files)
            self.assertEqual(self.N.mark_new_write(writer), proof)
            self.select(self.current()[0].previous_endpoint, '3' * 32)
            with self.assertRaisesRegex(ValueError, 'writer_plan_changed'):
                self.N.validate_normal_writer(writer)
        emit('schema3-writer-first-write-mark', epochBefore=before['epoch'], epochAfter=after['epoch'],
             v6BytesUnchanged=True, rollbackRevokes=True)


def plan_path(P, plan, role):
    return P._plan_record(plan)[1]['components'][role]['path']


if __name__ == '__main__':
    unittest.main()

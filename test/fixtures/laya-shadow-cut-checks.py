"""Publication cut identity: sidecar seal states and per-field comparison."""
import copy
import importlib
import importlib.util
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / 'core'))
spec = importlib.util.spec_from_file_location('shadow_cut_sidecar_seed', HERE / 'laya-reader-sidecar-write-checks.py')
sidecar_seed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sidecar_seed)

# Every key _cut_identity compares. A field missing from the comparison lets
# a writer change that only that field records slip through the cut.
IDENTITY_FIELDS = (('format',), ('incarnation',), ('head',), ('exclusions',), ('schema_meta',),
                   ('sidecar', 'format'), ('sidecar', 'meta'), ('sidecar', 'revision'))


def _other(value):
    if type(value) is int:
        return value + 1
    if isinstance(value, list):
        return [*value, 'mutated']
    return 'mutated'


class CutIdentityChecks(unittest.TestCase):
    def setUp(self):
        self.fixture = sidecar_seed.SidecarWriteChecks('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.seed, self.case = self.fixture.seed, self.fixture.case
        subprocess.run(['git', '-c', 'init.defaultBranch=main', 'init', '-q', str(self.case.project)],
                       check=True, capture_output=True, timeout=10)
        subprocess.run(['git', '-C', str(self.case.project), '-c', 'commit.gpgsign=false',
                        '-c', 'core.hooksPath=' + os.devnull, '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty',
                        '-qm', 'fixture'], check=True, capture_output=True, timeout=10)
        checkout = importlib.import_module('laya_checkout_view')
        self.blobs = importlib.import_module('laya_blob_store')
        self.views = importlib.import_module('laya_view_store')
        self.migration = importlib.import_module('laya_shadow_migration')
        self.readers = importlib.import_module('laya_reader_open')
        self.repository = checkout.register_repository(str(self.case.project))
        self.worktree = checkout.register_worktree(str(self.case.project), self.repository)

    def revision_state(self):
        with sqlite3.connect('file:' + str(self.fixture.fixture.sidecar) + '?mode=ro', uri=True) as db:
            table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                               "AND name='publication_revisions'").fetchone()
            return ('table', db.execute('SELECT max(revision) FROM publication_revisions').fetchone()[0]
                    ) if table else 'unsealed'

    def seal_only(self):
        """Coordinated setup alone: revision 0, no document change."""
        I = self.fixture.I
        with self.fixture.opened() as (_runtime, _catalog, _row, component):
            db = I._sidecar_open(self.fixture.identity, component)
            try:
                I._sidecar_setup(db, self.fixture.identity, component)
            finally:
                I._close(db)
        # opened() prepares an unfenced project dir move intent; drop it so a
        # later coordinated write can prepare its own.
        mover = self.seed.mover
        mover._abort(mover._load_intent(self.case.state, self.case.project))

    def coordinated_publish(self):
        with self.fixture.opened() as (runtime, catalog, row, component):
            return self.fixture.publish(runtime, catalog, row, component)['status']

    def prepare(self):
        """Copy the selected v5 source and prepare one private candidate."""
        migration = self.migration
        store = self.blobs.BlobStore.create(self.case.state / 'shadow-cut.sqlite',
            project_root=str(self.case.project), repository=self.repository, worktree=self.worktree,
            scope_id=self.case.route['scopeId'])
        self.addCleanup(store.close)
        time.sleep(1.1)
        migration.begin_migration(store, worktree_id=self.worktree['worktreeId'],
            project=self.case.project, executing_root=self.case.plugin, deadline=time.monotonic() + 30)
        for _ in range(8):
            copied = migration.advance_migration(store, project=self.case.project,
                executing_root=self.case.plugin, deadline=time.monotonic() + 30)
            if copied['phase'] == 'copied':
                break
        self.assertEqual(copied['phase'], 'copied')
        view = self.views.ViewStore.open(self.case.state / 'shadow-cut.sqlite',
            repository=self.repository, scope_id=self.case.route['scopeId'])
        self.addCleanup(view.close)
        with self.seed.lease.resolve_endpoint(self.case.project, action='recall',
                                              host_root=self.case.plugin) as endpoint:
            decision = self.seed.reader.journal.select_route(str(self.case.project), action='recall')
            with self.readers.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 10) as snapshot:
                handle = view.capture_view(self.worktree['worktreeId'])
                proof = {'snapshot': snapshot, 'endpoint': endpoint, 'decision': decision,
                         'provenance_scan': copied['provenance_scan']}
                selected = migration.verify_generation_members(view, worktree_id=self.worktree['worktreeId'],
                    **proof, deadline=time.monotonic() + 30)
                manifest = {'scan_id': copied['provenance_scan']['id'],
                    'policy_digest': handle.policy_digest, 'revocation_epoch': 0,
                    'complete': True, 'sources': [], 'members': selected['members']}
                base = view.head(handle)
                generation = view.prepare_generation(handle, manifest, expected_head=base,
                                                     legacy_snapshot=proof)
        self.assertIsNone(base['gen_id'])
        return view, handle, base, generation, copied['provenance_scan']

    def publish(self, view, handle, base, generation, scan, *, before_cut=None, mutate=None):
        """One legacy publish; records cut entries, in-cut rechecks and identities."""
        readers, migration = self.readers, self.migration
        original_cut = readers.reader_publish_cut
        original_verify = view._verify_generation
        original_pinned = migration.publication_cut_identity
        original_current = migration.current_cut_identity
        trace = {'verified': 0, 'cuts': [], 'pinned': [], 'current': []}

        def verify(*args, **kwargs):
            result = original_verify(*args, **kwargs)
            trace['verified'] += 1
            return result

        @contextmanager
        def cut(*args, **kwargs):
            # Only the cut after the full proof and candidate verification.
            if trace['verified'] and not trace['cuts'] and before_cut is not None:
                before_cut()
            trace['cuts'].append(trace['verified'])
            with original_cut(*args, **kwargs):
                yield

        def pinned(*args, **kwargs):
            identity = original_pinned(*args, **kwargs)
            trace['pinned'].append(copy.deepcopy(identity))
            return identity

        def current(*args, **kwargs):
            identity = original_current(*args, **kwargs)
            trace['current'].append(copy.deepcopy(identity))
            if mutate is not None:
                identity = copy.deepcopy(identity)
                *parents, leaf = mutate
                target = identity
                for key in parents:
                    target = target[key]
                target[leaf] = _other(target[leaf])
            return identity

        error = None
        with self.seed.lease.resolve_endpoint(self.case.project, action='recall',
                                              host_root=self.case.plugin) as endpoint:
            decision = self.seed.reader.journal.select_route(str(self.case.project), action='recall')
            with mock.patch.object(readers, 'reader_publish_cut', cut), \
                 mock.patch.object(view, '_verify_generation', side_effect=verify), \
                 mock.patch.object(migration, 'publication_cut_identity', side_effect=pinned), \
                 mock.patch.object(migration, 'current_cut_identity', side_effect=current):
                try:
                    view.publish_generation(handle, generation, expected_head=base, legacy_snapshot={
                        'endpoint': endpoint, 'decision': decision, 'provenance_scan': scan},
                        deadline=time.monotonic() + 30)
                except Exception as exc:  # noqa: BLE001 - reason code is asserted by the caller
                    error = str(exc)
        return error, trace

    def assert_refused_before_commit(self, view, handle, generation, error, trace, reason):
        self.assertEqual(error, reason)
        # The refusal comes from the single in-cut recheck after one proof.
        self.assertEqual((trace['verified'], trace['cuts'], len(trace['pinned']), len(trace['current'])),
                         (1, [1], 1, 1))
        self.assertIsNone(view.head(handle)['gen_id'])
        self.assertEqual(view._db.execute('SELECT state FROM generations WHERE gen_id=?',
                                          (generation,)).fetchone()[0], 'prepared')

    def test_revision_table_without_rows_publishes_as_legacy_baseline(self):
        # Uncoordinated _setup (every pre-v5 sidecar, and v5 creation before
        # its coordinated seal) leaves the revision table without revision 0.
        # Readers treat it as the legacy baseline like a sidecar without the
        # table, and the cut compares its None revision as is: publication
        # proceeds, and only a later coordinated seal moves the value.
        self.assertEqual(self.revision_state(), ('table', None))
        with self.seed.authority(), mock.patch.object(self.migration, 'MAX_BATCH', 1):
            view, handle, base, generation, scan = self.prepare()
            error, trace = self.publish(view, handle, base, generation, scan)
        self.assertIsNone(error)
        self.assertEqual((trace['verified'], trace['cuts'], len(trace['pinned']), len(trace['current'])),
                         (1, [1], 1, 1))
        self.assertEqual(trace['pinned'][0], trace['current'][0])
        self.assertIsNone(trace['current'][0]['sidecar']['revision'])
        self.assertEqual(view.head(handle)['gen_id'], generation)
        self.assertEqual(self.revision_state(), ('table', None))

    def test_sealed_sidecar_each_identity_field_refuses_then_publish_succeeds(self):
        with self.seed.authority(), mock.patch.object(self.migration, 'MAX_BATCH', 1):
            self.assertEqual(self.coordinated_publish(), 'published')
            sealed = self.revision_state()
            self.assertEqual(sealed[0], 'table')
            self.assertGreaterEqual(sealed[1], 1)
            view, handle, base, generation, scan = self.prepare()
            for field in IDENTITY_FIELDS:
                with self.subTest(field='.'.join(field)):
                    error, trace = self.publish(view, handle, base, generation, scan, mutate=field)
                    self.assertEqual(trace['pinned'][0], trace['current'][0])
                    self.assert_refused_before_commit(view, handle, generation, error, trace,
                                                      'migration_snapshot_changed')
            error, trace = self.publish(view, handle, base, generation, scan)
        self.assertIsNone(error)
        self.assertEqual((trace['verified'], trace['cuts'], len(trace['current'])), (1, [1], 1))
        self.assertEqual(trace['pinned'][0], trace['current'][0])
        self.assertEqual(trace['current'][0]['sidecar']['revision'], sealed[1])
        head = view.head(handle)
        self.assertEqual((head['gen_id'], head['epoch']), (generation, base['epoch'] + 1))
        self.assertEqual(view._db.execute('SELECT state FROM generations WHERE gen_id=?',
                                          (generation,)).fetchone()[0], 'published')

    def test_sealed_sidecar_coordinated_write_before_cut_is_refused(self):
        with self.seed.authority(), mock.patch.object(self.migration, 'MAX_BATCH', 1):
            self.seal_only()
            self.assertEqual(self.revision_state(), ('table', 0))
            view, handle, base, generation, scan = self.prepare()
            injected = []
            error, trace = self.publish(view, handle, base, generation, scan,
                                        before_cut=lambda: injected.append(self.coordinated_publish()))
        self.assertEqual(injected, ['published'], (error, trace))
        self.assertEqual(self.revision_state(), ('table', 1))
        self.assertEqual(trace['pinned'][0]['sidecar']['revision'], 0)
        self.assertEqual(trace['current'][0]['sidecar']['revision'], 1)
        self.assert_refused_before_commit(view, handle, generation, error, trace,
                                          'migration_snapshot_changed')


if __name__ == '__main__':
    unittest.main()

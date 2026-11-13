#!/usr/bin/env python3
"""H8: one resident service, installs bound to two host copies of one commit.

Install A is bound to host copy X (the journal's plugin root), the per-user
resident service runs from host copy Y. Both copies carry the same sealed
bundle (same commit, manifest and profile), as after ``companion_sealed``.
The v5 install, journal, route, ledger, build record and stores are actual
(the published-v5 fixture of the reader suites). Synthetic authority is the
same as there: bundle inspection and the executing-root comparison. ``_verified_executing_root`` accepts Y only where the companion comparison
would and refuses Y as an unequal copy otherwise.
"""
from __future__ import annotations

from contextlib import ExitStack, closing, contextmanager
import fcntl
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('h8_companion_seed', HERE / 'laya-reader-move-seal-checks.py')
seed_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed_module)
J = seed_module.base.J
# The per-user service suite's fake embedding worker (no model) and manifest.
rs_spec = importlib.util.spec_from_file_location('h8_resident_suite', HERE / 'laya-resident-service-checks.py')
rs = importlib.util.module_from_spec(rs_spec)
rs_spec.loader.exec_module(rs)


class CompanionResidentChecks(unittest.TestCase):
    def setUp(self):
        self.seed = seed_module.MoveSealChecks('runTest')
        self.seed.setUp()
        self.addCleanup(self.seed.doCleanups)
        self.case = self.seed.case
        self.companion = (self.case.home / 'codex-cache-copy').resolve()
        self.companion.mkdir(mode=0o700, exist_ok=True)
        self.W = importlib.import_module('memvane.sync.product_sync_worker')
        self.E = importlib.import_module('memvane.resident.embed_resident')
        self.S = importlib.import_module('memvane.resident.resident_service')
        self.assertEqual(J.journal_version(self.case.journal), 5)

    @contextmanager
    def executing(self, root, *, equal_copy=True):
        """This process runs from ``root``; Y is an equal copy unless told otherwise."""
        root = Path(root)

        def verified(selected, owner_proof):
            if root == Path(selected['pluginRoot']):
                return str(root)
            if not equal_copy:
                raise J.JournalEvidenceError('executing_bundle_not_selected')
            return str(root)
        with ExitStack() as stack:
            stack.enter_context(self.seed.authority())
            stack.enter_context(mock.patch.object(J, '_verified_executing_root', side_effect=verified))
            stack.enter_context(mock.patch.object(J, '_executing_root', return_value=root))
            stack.enter_context(mock.patch('memvane.kernel.bundle_paths.plugin_root', return_value=root))
            yield

    def test_01_owner_copy_resident_proves_selected(self):
        with self.executing(self.case.plugin):
            decision, proof, mode = self.S.default_prove(str(self.case.project))
        self.assertEqual(mode, 'selected')
        self.assertEqual(proof['executingPluginRoot'], str(self.case.plugin))
        self.assertIsInstance(proof['providerManifest'], dict)






    @contextmanager
    def held_elsewhere(self, name):
        """Another process holds one of this install's reader locks EX."""
        lock = self.seed.reader._lock_dir(self.seed.install, create=False) / name
        holder = subprocess.Popen(
            [sys.executable, '-I', '-c', 'import fcntl,os,sys\n'
             'fd=os.open(sys.argv[1],os.O_RDONLY|os.O_NOFOLLOW)\n'
             'fcntl.flock(fd,fcntl.LOCK_EX)\nprint("HELD",flush=True)\nsys.stdin.read()\n', str(lock)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline(), 'HELD\n')
            yield
        finally:
            holder.stdin.close()
            holder.wait(timeout=10)

    def test_06_worker_attestation_waits_for_the_v5_windows(self):
        # The attestation re-proves the journal before and after hashing the
        # runtime; those re-proofs open the base and queue only inside the
        # journal-bound root's v5 windows, on the companion and owner copy.
        from memvane.kernel.resource_deferred import ResourceDeferred
        opened, connect = [], sqlite3.connect

        def observe(database, *args, **kwargs):
            if str(database).startswith((self.case.db.as_uri(), str(self.case.db),
                                         self.case.queue.as_uri(), str(self.case.queue))):
                opened.append(str(database))
            return connect(database, *args, **kwargs)
        outcomes = {}
        for label, root in (('companion', self.companion), ('owner', self.case.plugin)):
            for name in ('base-physical.lock', 'reader-lifetime.lock'):
                with self.executing(root):
                    decision, proof, mode = self.S.default_prove(str(self.case.project))
                    self.assertEqual(mode, 'selected')
                    del opened[:]
                    with self.held_elsewhere(name), \
                            mock.patch.object(sqlite3, 'connect', side_effect=observe):
                        try:
                            self.S.default_attest(decision, proof, mode)
                            outcome = 'attested'
                        except ResourceDeferred as exc:
                            outcome = 'deferred:' + str(exc)
                        except Exception as exc:
                            outcome = 'error:' + type(exc).__name__ + ':' + str(exc)
                outcomes[label, name] = (outcome, list(opened))
        print('attestation_window_outcomes', json.dumps({'%s/%s' % key: value[0]
                                                         for key, value in outcomes.items()}),
              flush=True)
        for key, (outcome, connections) in outcomes.items():
            self.assertTrue(outcome.startswith('deferred:'), (key, outcome))
            self.assertEqual(connections, [], key)
        # With the windows free the same attestation completes.
        with self.executing(self.companion):
            decision, proof, mode = self.S.default_prove(str(self.case.project))
            with mock.patch.object(J, 'validate_production_provider_attestation',
                                   side_effect=lambda _profile, _proof, record: record['providerManifest']), \
                    mock.patch.object(J, '_verify_runtime_files'):
                self.assertIsNone(self.S.default_attest(decision, proof, mode))


    def stubbed_record(self):
        """The fixture's provider record is synthetic: stub record and runtime-file checks only."""
        stack = ExitStack()
        stack.enter_context(mock.patch.object(J, 'validate_production_provider_attestation',
            side_effect=lambda _profile, _proof, record: record['providerManifest']))
        stack.enter_context(mock.patch.object(J, '_verify_runtime_files'))
        return stack

    def bump_epoch(self):
        """What a sync's first write does to the bundle journal: one epoch."""
        row = J.inspect_journal(self.case.journal)
        return J.cas_mark_new_write(self.case.journal, row['epoch'])['epoch']

    def test_07_epoch_moved_before_attestation_is_not_bad_evidence(self):
        # A sync's first write between the service's proof and the worker
        # attestation bumps the journal epoch. The attestation proves the
        # new selection; it never reports it as invalid evidence.
        with self.executing(self.companion):
            decision, proof, mode = self.S.default_prove(str(self.case.project))
            epoch = self.bump_epoch()
            self.assertEqual(epoch, proof['selectionEpoch'] + 1)
            with self.stubbed_record():
                self.assertIsNone(self.S.default_attest(decision, proof, mode))

    def test_07b_epoch_moved_with_a_damaged_runtime_stays_an_error(self):
        # A sync write moves the epoch, then the runtime hash finds damage
        # on the unmoved bundle: that is bad evidence, not a moved selection.
        with self.executing(self.companion):
            decision, proof, mode = self.S.default_prove(str(self.case.project))
            self.bump_epoch()

            def damaged(_record):
                raise J.JournalEvidenceError('worker_runtime_files_invalid')
            with mock.patch.object(J, 'validate_production_provider_attestation',
                                   side_effect=lambda _profile, _proof, record: record['providerManifest']), \
                    mock.patch.object(J, '_verify_runtime_files', side_effect=damaged):
                with self.assertRaisesRegex(J.JournalEvidenceError, 'worker_runtime_files_invalid'):
                    self.S.default_attest(decision, proof, mode)

    def test_07c_an_unreadable_route_or_journal_is_not_a_moved_selection(self):
        with self.executing(self.companion):
            decision, _proof, _mode = self.S.default_prove(str(self.case.project))
            from memvane.journal.application import service_proof
            view = service_proof._journal_view(decision)
            self.assertIsNotNone(view)
            self.assertFalse(service_proof._selection_moved(decision, view))
            with mock.patch.object(service_proof, 'select_route', side_effect=OSError('unreadable')):
                self.assertFalse(service_proof._selection_moved(decision, view))
            with mock.patch.object(J, 'inspect_journal',
                                   side_effect=J.JournalEvidenceError('journal_unavailable')):
                self.assertFalse(service_proof._selection_moved(decision, view))
            # An epoch alone is not a move; another route root is.
            self.bump_epoch()
            self.assertFalse(service_proof._selection_moved(decision, view))
            self.assertTrue(service_proof._selection_moved({**decision, 'pluginRoot': '/elsewhere'}, view))

    def test_08_windows_are_taken_again_after_the_runtime_hash(self):
        # The re-proof after the hash takes the windows again: a lock taken
        # while the runtime files are hashed defers before any connection.
        from memvane.kernel.resource_deferred import ResourceDeferred
        identity_calls, real_identity = [], J._identity_connection
        real_manifest = J._worker_attestation_manifest
        holders = []

        def observe(path, *args, **kwargs):
            identity_calls.append((holders != [], J._writer_identity_reads.get() is not None))
            return real_identity(path, *args, **kwargs)

        def hashing(selected):
            result = real_manifest(selected)
            holder = self.held_elsewhere('base-physical.lock')
            holder.__enter__()
            holders.append(holder)
            return result
        try:
            with self.executing(self.companion):
                decision, proof, mode = self.S.default_prove(str(self.case.project))
                with self.stubbed_record(), \
                        mock.patch.object(J, '_identity_connection', side_effect=observe), \
                        mock.patch.object(J, '_worker_attestation_manifest', side_effect=hashing):
                    with self.assertRaisesRegex(ResourceDeferred, 'writer_proof_physical_busy'):
                        self.S.default_attest(decision, proof, mode)
        finally:
            for holder in holders:
                holder.__exit__(None, None, None)
        self.assertEqual(len(holders), 1)
        # Before the hash every identity connection was window-bound; after
        # it (lock held) none was opened at all.
        self.assertTrue(identity_calls)
        self.assertTrue(all(bound for after, bound in identity_calls if not after), identity_calls)
        self.assertEqual([call for call in identity_calls if call[0]], [])

    def test_09_epoch_moved_during_the_hash_is_accepted(self):
        # A sync write during the hash moves only the epoch: the hash was of
        # the unmoved bundle, so the attestation stands. (A real move during
        # the hash, another plugin root, defers: test_12.)
        real_manifest = J._worker_attestation_manifest
        bumped = []

        def hashing(selected):
            result = real_manifest(selected)
            bumped.append(self.bump_epoch())
            return result
        with self.executing(self.companion):
            decision, proof, mode = self.S.default_prove(str(self.case.project))
            with self.stubbed_record(), \
                    mock.patch.object(J, '_worker_attestation_manifest', side_effect=hashing):
                self.assertIsNone(self.S.default_attest(decision, proof, mode))
        self.assertEqual(bumped, [proof['selectionEpoch'] + 1])









class SplitInstallsRecoveryChecks(unittest.TestCase):
    """Installs split across both host copies; the service runs from one of them.

    Install A is bound to copy X, install B to copy Y, and the service runs
    from Y with A's resident state stuck at ``starting``. Actual: both v5
    installs and their stores, ``default_prove`` with the writer plan,
    queue owner and v5 windows, the service's adopt loop, socket binding,
    worker start through ``start_worker``, the owner state and the probe a
    sync wake uses. Replaced, and why:
    - bundle inspection and ``_verified_executing_root``: the fixture copies
      are not sealed plugins; the replacement compares the two proofs on the
      same keys as the real check;
    - this process's executing root is set to Y;
    - provider record validation and runtime-file rehash in the attestation
      (the fixture has no model or node runtime); the journal re-proofs
      around them stay real;
    - the embedding worker is the service suite's fake worker (no model);
    - the per-user runtime root: each fixture install is seeded in its own
      temporary HOME, so the UID journal directories (reader locks) are
      routed by install id to the root that seed prepared them in. One user
      has one root; the directories are keyed by install id either way.
    """

    def seed(self, install_id):
        manifest = rs.manifest_for('{state}/runtime/laya-embed.sock')
        del manifest['providerPath']
        test = self

        class Install(seed_module.MoveSealChecks):
            INSTALL_ID = install_id
            STATE_PARTS = ('.local', 'state', 'memvane', install_id)
            PROVIDER_EXTRA = manifest
        seed = Install('runTest')
        seed.setUp()
        test.addCleanup(seed.doCleanups)
        return seed

    def setUp(self):
        self.a = self.seed('a' * 32)
        root_a = os.environ['MEMVANE_TEST_RUNTIME_ROOT']
        self.b = self.seed('b' * 32)
        self.runtime_roots = {'a' * 32: root_a, 'b' * 32: os.environ['MEMVANE_TEST_RUNTIME_ROOT']}
        self.assertNotEqual(self.a.case.plugin, self.b.case.plugin)
        self.W = importlib.import_module('memvane.sync.product_sync_worker')
        self.E = importlib.import_module('memvane.resident.embed_resident')
        self.S = importlib.import_module('memvane.resident.resident_service')
        self.bundle = importlib.import_module('memvane.install.product_bundle')
        temp = tempfile.TemporaryDirectory(prefix='h8s-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root = self.base / 'svc'
        self.root.mkdir(mode=0o700)
        self.loads, self.services = [], []
        self.addCleanup(self.stop_services)

    def stop_services(self):
        for service in self.services:
            if not service.stopped.is_set():
                service.begin_drain()
                service.stopped.wait(10)
            service.close()

    @contextmanager
    def authority(self, executing):
        seeds = {str(seed.case.plugin): seed for seed in (self.a, self.b)}
        proofs = {root: seed.case.proof for root, seed in seeds.items()}
        same = ('sourceCommit', 'bundleManifestSha256', 'vectorProfile')

        def inspect(root):
            if str(root) not in proofs:
                raise self.bundle.BundleEvidenceError('fixture_bundle_not_owned')
            return proofs[str(root)]

        def verified(selected, owner):
            local = proofs[str(executing)]
            if any(local[key] != owner[key] for key in same):
                raise J.JournalEvidenceError('executing_bundle_not_selected')
            return str(executing)

        def identity_rows(attestation):
            return [[attestation['providerManifest']['providerPath'], 1, 2, 3, 4, 5]]
        paths = importlib.import_module('memvane.journal.journal_paths')
        real_directory, env_lock = paths.journal_directory, threading.Lock()

        def directory(*parts, create=False):
            root = self.runtime_roots.get(parts[1]) if len(parts) > 1 else None
            if root is None:
                return real_directory(*parts, create=create)
            with env_lock, mock.patch.dict(os.environ, {'MEMVANE_TEST_RUNTIME_ROOT': root}):
                return real_directory(*parts, create=create)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(paths, 'journal_directory', side_effect=directory))
            stack.enter_context(mock.patch.object(J, '_inspect_identity',
                side_effect=lambda identity: inspect(identity['pluginRoot'])))
            stack.enter_context(mock.patch.object(J, '_verified_executing_root', side_effect=verified))
            stack.enter_context(mock.patch.object(J, '_provider_file_identity', side_effect=identity_rows))
            stack.enter_context(mock.patch.object(J, '_executing_root', return_value=Path(executing)))
            stack.enter_context(mock.patch('memvane.kernel.bundle_paths.plugin_root',
                                                  return_value=Path(executing)))
            seed = self.a
            for module in (J, self.bundle, seed.reader, seed.bridge, seed.lease, seed.mover):
                if callable(getattr(module, 'inspect_full_plugin_candidate', None)):
                    stack.enter_context(mock.patch.object(module, 'inspect_full_plugin_candidate',
                                                          side_effect=inspect))
            stack.enter_context(mock.patch.object(J, 'validate_production_provider_attestation',
                side_effect=lambda _profile, _proof, record: record['providerManifest']))
            stack.enter_context(mock.patch.object(J, '_verify_runtime_files'))
            yield

    def start_worker(self, manifest, admit):
        if admit() is not True:
            raise rs.IndexContractError('provider_admission_deferred')
        process = subprocess.Popen(
            [sys.executable, '-B', rs.__file__, '--fake-worker', manifest['serviceSocketPath'],
             json.dumps({key: manifest[key] for key in self.E._IDENTITY}), '0.01',
             str(self.base / 'max-pending'), str(self.base / 'slow'), str(manifest['dimension'])],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if process.stdout.readline() != b'READY\n':
            raise rs.IndexContractError('provider_start_failed')
        self.loads.append(process.pid)
        return rs.Handle(process, manifest['serviceSocketPath'])

    def manifest(self, seed):
        decision = J.select_route(str(seed.case.project), action='recall')
        return json.loads((Path(decision['journalPath']).parent /
                           J.PRODUCTION_ATTESTATION).read_bytes())['providerManifest']

    def ready(self, manifest):
        try:
            return self.E.probe(manifest) == 'ready'
        except Exception:
            return False

    def run_service(self, manifests, *, attest=None):
        service = self.S.Service(self.root, bundle={'version': '1.0.0', 'digest': 'a'},
                                 start_worker=self.start_worker, admit=lambda: True,
                                 retire=lambda *_a: None, tick=.05,
                                 **({} if attest is None else {'attest': attest}))
        self.services.append(service)
        self.assertTrue(service.acquire(wait=5))
        service.start()
        for seed in (self.a, self.b):
            self.S.register(str(seed.case.project), root=self.root)
        deadline = time.monotonic() + 40
        while not all(self.ready(m) for m in manifests.values()):
            self.assertLess(time.monotonic(), deadline,
                            {'ready': {name: self.ready(m) for name, m in manifests.items()},
                             'waiting': dict(service.waiting),
                             'unproved': dict(getattr(service, 'unproved', {})),
                             'states': {name: self.E.read_owner_state(m) for name, m in manifests.items()}})
            time.sleep(.05)
        return service

    def test_epoch_moved_by_a_sync_before_attestation_still_binds(self):
        # A sync's first write lands between the service's proof and the
        # cold worker's attestation: the start is retried with the new
        # selection, never recorded as a terminal attestation failure.
        executing = self.b.case.plugin
        manifests = {name: self.manifest(seed) for name, seed in (('a', self.a), ('b', self.b))}
        for manifest in manifests.values():
            Path(manifest['serviceSocketPath']).parent.mkdir(mode=0o700, exist_ok=True)
        bumped = {}

        def attest(decision, proof, mode):
            # Each install's first attestation follows a sync write (both
            # installs, so no other install's worker can heal a refusal).
            if decision['projectRoot'] not in bumped:
                journal = Path(decision['journalPath'])
                row = J.inspect_journal(journal)
                bumped[decision['projectRoot']] = J.cas_mark_new_write(journal, row['epoch'])['epoch']
            return self.S.default_attest(decision, proof, mode)
        with self.authority(executing):
            service = self.run_service(manifests, attest=attest)
            states = {name: self.E.read_owner_state(m) for name, m in manifests.items()}
        self.assertEqual(len(bumped), 2)
        self.assertTrue(all(state['reason'] == 'ready' and not state['terminal']
                            for state in states.values()), states)
        self.assertEqual(len(self.loads), 1)
        self.assertEqual(getattr(service, 'unproved', {}), {})







if __name__ == '__main__':
    unittest.main()

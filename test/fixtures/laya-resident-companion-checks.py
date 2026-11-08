#!/usr/bin/env python3
"""H8: one resident service, installs bound to two host copies of one commit.

Install A is bound to host copy X (the journal's plugin root), the per-user
resident service runs from host copy Y. Both copies carry the same sealed
bundle (same commit, manifest and profile), as after ``companion_sealed``.
The v5 install, journal, route, ledger, build record and stores are actual
(the published-v5 fixture of the reader suites). Synthetic authority is the
same as there: bundle inspection and the executing-root comparison. The real
companion comparison is proven on real sealed copies by
``laya-l1-qa-checks.py``; here ``_verified_executing_root`` accepts Y only
where that comparison would, and refuses Y as an unequal copy otherwise.
"""
from __future__ import annotations

from contextlib import ExitStack, closing, contextmanager
import fcntl
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
        self.W = importlib.import_module('laya_product_sync_worker')
        self.E = importlib.import_module('laya_embed_resident')
        self.S = importlib.import_module('laya_resident_service')
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
            stack.enter_context(mock.patch.object(self.W, '_writer_executing_root', return_value=root))
            yield

    def test_01_owner_copy_resident_proves_selected(self):
        with self.executing(self.case.plugin):
            decision, proof, mode = self.S.default_prove(str(self.case.project))
        self.assertEqual(mode, 'selected')
        self.assertEqual(proof['executingPluginRoot'], str(self.case.plugin))
        self.assertIsInstance(proof['providerManifest'], dict)

    def test_02_other_copy_resident_proves_the_same_install_selected(self):
        with self.executing(self.companion):
            decision, proof, mode = self.S.default_prove(str(self.case.project))
            # The fixture's provider record is synthetic: only the record and
            # runtime-file checks are stubbed, the journal re-proofs are real.
            with mock.patch.object(J, 'validate_production_provider_attestation',
                                   side_effect=lambda _profile, _proof, record: record['providerManifest']), \
                    mock.patch.object(J, '_verify_runtime_files'):
                attested = self.S.default_attest(decision, proof, mode)
        self.assertEqual(mode, 'selected')
        self.assertIsNone(attested)
        self.assertEqual(decision['pluginRoot'], str(self.case.plugin))
        self.assertEqual(proof['pluginRoot'], str(self.case.plugin))
        self.assertEqual(proof['executingPluginRoot'], str(self.companion))
        self.assertTrue(proof['productRouteAllowed'])
        self.assertIsInstance(proof['providerManifest'], dict)
        # The binding identity, provider manifest included (so the worker key
        # and socket), is the one a service on the owner copy binds.
        with self.executing(self.case.plugin):
            owner_decision, owner_proof, _mode = self.S.default_prove(str(self.case.project))
        self.assertEqual(self.E._owner_identity(decision, proof),
                         self.E._owner_identity(owner_decision, owner_proof))

    def test_03_unequal_copy_never_proves_selected(self):
        with self.executing(self.companion, equal_copy=False):
            try:
                _decision, _proof, mode = self.S.default_prove(str(self.case.project))
            except Exception as exc:  # refused outright is also acceptable
                mode = 'refused:' + str(exc)
        self.assertNotEqual(mode, 'selected')

    def test_04_writers_keep_the_exact_root_plan(self):
        with self.executing(self.companion):
            with self.assertRaisesRegex(J.JournalEvidenceError, 'writer_bundle_unavailable'):
                self.W._selected(self.case.project)

    def test_05_companion_proof_waits_for_the_v5_physical_window(self):
        # The companion proof opens the base and queue only through the same
        # lifetime and physical windows as the owner's: while another process
        # holds base-physical EX it defers before any content connection.
        from resource_deferred import ResourceDeferred
        lock = self.seed.reader._lock_dir(self.seed.install, create=False) / 'base-physical.lock'
        opened, connect = [], sqlite3.connect

        def observe(database, *args, **kwargs):
            if str(database).startswith((self.case.db.as_uri(), str(self.case.db))):
                opened.append(str(database))
            return connect(database, *args, **kwargs)
        holder = subprocess.Popen(
            [sys.executable, '-I', '-c', 'import fcntl,os,sys\n'
             'fd=os.open(sys.argv[1],os.O_RDONLY|os.O_NOFOLLOW)\n'
             'fcntl.flock(fd,fcntl.LOCK_EX)\nprint("HELD",flush=True)\nsys.stdin.read()\n', str(lock)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline(), 'HELD\n')
            with self.executing(self.companion), mock.patch.object(sqlite3, 'connect', side_effect=observe):
                with self.assertRaises((ResourceDeferred, ValueError)) as caught:
                    self.S.default_prove(str(self.case.project))
        finally:
            holder.stdin.close()
            holder.wait(timeout=10)
        self.assertRegex(str(caught.exception), 'busy|deadline')
        self.assertEqual(opened, [], 'content SQLite opened outside the physical window')


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
      same keys as the real check (real copies: ``laya-l1-qa-checks.py``);
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
        self.W = importlib.import_module('laya_product_sync_worker')
        self.E = importlib.import_module('laya_embed_resident')
        self.S = importlib.import_module('laya_resident_service')
        self.bundle = importlib.import_module('laya_product_bundle')
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
        paths = importlib.import_module('laya_journal_paths')
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
            stack.enter_context(mock.patch.object(self.W, '_writer_executing_root',
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

    def test_service_on_one_copy_binds_installs_of_both_copies(self):
        executing = self.b.case.plugin
        manifests = {name: self.manifest(seed) for name, seed in (('a', self.a), ('b', self.b))}
        for manifest in manifests.values():
            # An installed state root has its private runtime directory.
            runtime = Path(manifest['serviceSocketPath']).parent
            runtime.mkdir(mode=0o700, exist_ok=True)
        with self.authority(executing):
            # A's owner record from the stalled episode: "starting", an hour old.
            with mock.patch.object(self.E.time, 'time', return_value=time.time() - 3600):
                self.E._write_state(manifests['a'], 'starting')
            self.assertEqual(self.E.read_owner_state(manifests['a'])['reason'], 'starting')
            self.assertFalse(self.ready(manifests['a']))
            service = self.S.Service(self.root, bundle={'version': '1.0.0', 'digest': 'a'},
                                     start_worker=self.start_worker, admit=lambda: True,
                                     retire=lambda *_a: None, tick=.05)
            self.services.append(service)
            self.assertIs(service.prove, self.S.default_prove)
            self.assertIs(service.attest, self.S.default_attest)
            self.assertTrue(service.acquire(wait=5))
            service.start()
            for seed in (self.a, self.b):
                self.S.register(str(seed.case.project), root=self.root)
            deadline = time.monotonic() + 30
            while not all(self.ready(m) for m in manifests.values()):
                self.assertLess(time.monotonic(), deadline,
                                {'ready': {name: self.ready(m) for name, m in manifests.items()},
                                 'waiting': dict(service.waiting),
                                 'unproved': dict(getattr(service, 'unproved', {}))})
                time.sleep(.05)
            modes = {name: service.bindings[str(seed.case.project)].mode
                     for name, seed in (('a', self.a), ('b', self.b))}
            state = self.E.read_owner_state(manifests['a'])
        self.assertEqual(modes, {'a': 'selected', 'b': 'selected'})
        self.assertEqual(state['reason'], 'ready')
        # One provider identity: both installs share one worker.
        self.assertEqual(len(self.loads), 1)
        self.assertEqual(getattr(service, 'unproved', {}), {})


if __name__ == '__main__':
    unittest.main()

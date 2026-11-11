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
        from resource_deferred import ResourceDeferred
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
            view = self.W._journal_view(decision)
            self.assertIsNotNone(view)
            self.assertFalse(self.W._selection_moved(decision, view))
            with mock.patch.object(self.W, 'select_route', side_effect=OSError('unreadable')):
                self.assertFalse(self.W._selection_moved(decision, view))
            with mock.patch.object(J, 'inspect_journal',
                                   side_effect=J.JournalEvidenceError('journal_unavailable')):
                self.assertFalse(self.W._selection_moved(decision, view))
            # An epoch alone is not a move; another route root is.
            self.bump_epoch()
            self.assertFalse(self.W._selection_moved(decision, view))
            self.assertTrue(self.W._selection_moved({**decision, 'pluginRoot': '/elsewhere'}, view))

    def test_08_windows_are_taken_again_after_the_runtime_hash(self):
        # The re-proof after the hash takes the windows again: a lock taken
        # while the runtime files are hashed defers before any connection.
        from resource_deferred import ResourceDeferred
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

    def test_10_read_only_fallback_takes_the_v5_windows(self):
        # An unequal copy proves the install as bundle drift; the read-only
        # fallback that follows opens the base and queue only through the
        # journal-bound root's windows, and defers while one is held.
        from resource_deferred import ResourceDeferred
        identity_calls, real_identity = [], J._identity_connection

        def observe(path, *args, **kwargs):
            identity_calls.append(J._writer_identity_reads.get() is not None)
            return real_identity(path, *args, **kwargs)
        with self.executing(self.companion, equal_copy=False), \
                mock.patch.object(J, '_identity_connection', side_effect=observe):
            decision, proof, mode = self.S.default_prove(str(self.case.project))
        self.assertEqual(mode, 'read_only')
        self.assertTrue(proof['readOnlyAllowed'])
        self.assertTrue(identity_calls)
        self.assertTrue(all(identity_calls), identity_calls)
        real_owned, holders = self.E._owned_route, []

        def owned_then_locked(project):
            try:
                return real_owned(project)
            finally:
                # The full proof's windows are closed: a writer takes the lock.
                holder = self.held_elsewhere('base-physical.lock')
                holder.__enter__()
                holders.append(holder)
        del identity_calls[:]
        try:
            with self.executing(self.companion, equal_copy=False), \
                    mock.patch.object(J, '_identity_connection', side_effect=observe), \
                    mock.patch.object(self.E, '_owned_route', side_effect=owned_then_locked):
                with self.assertRaisesRegex(ResourceDeferred, 'writer_proof_physical_busy'):
                    self.S.default_prove(str(self.case.project))
        finally:
            for holder in holders:
                holder.__exit__(None, None, None)
        self.assertEqual(len(holders), 1)
        self.assertTrue(all(identity_calls), identity_calls)


    @contextmanager
    def upgrade_authority(self, new):
        """A real active bundle upgrade of the install from copy X to copy B.

        Same synthetic authority as the schema3 lifecycle suite's
        ``upgrade_authority``: bundle inspection, provider review, adoption
        and attestation record are synthetic; intent, fence, route, ledger
        and journal switch are the real ``laya_active_upgrade`` writes.
        """
        case, reader = self.case, self.seed.reader
        upgrade = importlib.import_module('laya_active_upgrade')
        target = {**case.proof, 'pluginRoot': str(new), 'sourceCommit': 'a' * 40,
                  'bundleManifestSha256': 'b' * 64, 'ownershipManifestSha256': 'f' * 64}
        old_att = json.loads((case.state / J.PRODUCTION_ATTESTATION).read_bytes())
        new_att = {**old_att, 'sourceCommit': target['sourceCommit'],
                   'bundleManifestSha256': target['bundleManifestSha256'],
                   'ownershipManifestSha256': target['ownershipManifestSha256'],
                   'providerManifest': {**old_att['providerManifest'], 'providerPath': str(new / 'p.py')}}

        def lookup(root):
            return case.proof if str(root) == str(case.plugin) else target

        def identify(att):
            return [[att['providerManifest']['providerPath'], 1, 2, 3, 4, 5]]
        review = {'proposalSha256': '9' * 64, 'reviewedProvider': {'vectorProfile': target['vectorProfile']}}
        with ExitStack() as stack:
            for patch in (self.seed.authority(),
                          mock.patch.object(upgrade, 'review_local_provider', return_value=review),
                          mock.patch.object(upgrade, 'reattest', return_value={'vectorProfile': target['vectorProfile']}),
                          mock.patch.object(upgrade, 'adopt_host_loaded_plugin', return_value=target),
                          mock.patch.object(upgrade, 'inspect_full_plugin_candidate', side_effect=lookup),
                          mock.patch.object(upgrade, 'attestation_record', return_value=new_att),
                          mock.patch.object(upgrade, 'validate_production_provider_attestation'),
                          mock.patch.object(upgrade, '_provider_file_identity', side_effect=identify),
                          mock.patch.object(upgrade, '_wait_for_sockets'),
                          mock.patch.object(reader.journal, 'inspect_full_plugin_candidate', side_effect=lookup),
                          mock.patch.object(reader.journal, '_executing_from_candidate'),
                          mock.patch.object(reader.journal, '_provider_file_identity', side_effect=identify),
                          mock.patch.object(reader.journal, '_inspect_identity',
                                            side_effect=lambda identity: lookup(identity['pluginRoot'])),
                          mock.patch.object(reader.journal, '_bind_attestation', side_effect=lambda path, identity, proof: (
                              hashlib.sha256(J._canon(new_att if proof == target else old_att).encode()).hexdigest(),
                              identify(new_att if proof == target else old_att)))):
                stack.enter_context(patch)

            def switch():
                intent = upgrade._prepare(case.project, case.state, case.journal, case.home, new,
                                          target['sourceCommit'], review['proposalSha256'],
                                          '/synthetic/model', 3)
                self.assertEqual(upgrade._switch(intent)['status'], 'active_upgrade_complete')
            yield switch

    @contextmanager
    def running_from(self, root):
        """This process executes ``root``, an equal sealed copy of the selected bundle."""
        root = Path(root)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(J, '_verified_executing_root',
                                                  side_effect=lambda _selected, _owner: str(root)))
            stack.enter_context(mock.patch.object(J, '_executing_root', return_value=root))
            stack.enter_context(mock.patch.object(self.W, '_writer_executing_root', return_value=root))
            yield

    def upgrade_moves_selection(self, when):
        """X -> B upgrade ``before`` the attestation or ``after`` its hash; then B attests."""
        from resource_deferred import ResourceDeferred
        new = (self.case.home / 'claude-cache-next').resolve()
        new.mkdir(mode=0o700, exist_ok=True)
        real_manifest = J._worker_attestation_manifest
        with self.upgrade_authority(new) as switch:
            with self.running_from(self.case.plugin):
                decision, proof, mode = self.S.default_prove(str(self.case.project))
                self.assertEqual((mode, decision['pluginRoot']), ('selected', str(self.case.plugin)))
                if when == 'before':
                    switch()
                    hashing = real_manifest
                else:
                    def hashing(selected):
                        result = real_manifest(selected)
                        switch()
                        return result
                with self.stubbed_record(), \
                        mock.patch.object(J, '_worker_attestation_manifest', side_effect=hashing):
                    with self.assertRaisesRegex(ResourceDeferred, 'worker_selection_changed'):
                        self.S.default_attest(decision, proof, mode)
            # The next attempt (the host's new copy) proves and attests B.
            with self.running_from(new):
                decision, proof, mode = self.S.default_prove(str(self.case.project))
                self.assertEqual((mode, decision['pluginRoot']), ('selected', str(new)))
                with self.stubbed_record():
                    self.assertIsNone(self.S.default_attest(decision, proof, mode))
            # A failure on a selection that did not move stays an error.
            with self.running_from(new):
                decision, proof, mode = self.S.default_prove(str(self.case.project))

                def broken(_selected):
                    raise J.JournalEvidenceError('worker_attestation_changed')
                with mock.patch.object(J, '_worker_attestation_manifest', side_effect=broken):
                    with self.assertRaisesRegex(J.JournalEvidenceError, 'worker_attestation_changed'):
                        self.S.default_attest(decision, proof, mode)

    def test_11_bundle_upgrade_before_attestation_defers(self):
        self.upgrade_moves_selection('before')

    def test_12_bundle_upgrade_during_the_hash_defers(self):
        self.upgrade_moves_selection('after')


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

    def test_bundle_upgrade_during_a_cold_start_binds_the_new_selection(self):
        # A's cold start proved copy X; before its attestation the bundle
        # upgrade moves A to copy Y (the service's own copy, same provider).
        # The start is retried on the new selection: A binds from Y with no
        # terminal record.
        upgrade = importlib.import_module('laya_active_upgrade')
        executing, case = self.b.case.plugin, self.a.case
        manifest = self.manifest(self.a)
        Path(manifest['serviceSocketPath']).parent.mkdir(mode=0o700, exist_ok=True)
        target = self.b.case.proof  # copy Y's own sealed proof (same commit and provider)
        old_att = json.loads((case.state / J.PRODUCTION_ATTESTATION).read_bytes())
        new_att = {**old_att, 'ownershipManifestSha256': target['ownershipManifestSha256'],
                   'providerManifest': {**old_att['providerManifest'],
                                        'providerPath': str(executing / 'p.py')}}

        def identify(att):
            return [[att['providerManifest']['providerPath'], 1, 2, 3, 4, 5]]
        review = {'proposalSha256': '9' * 64, 'reviewedProvider': {'vectorProfile': target['vectorProfile']}}
        switched, results = [], []

        def attest(decision, proof, mode):
            if not switched:
                try:
                    intent = upgrade._prepare(case.project, case.state, case.journal, case.home,
                                              executing, target['sourceCommit'],
                                              review['proposalSha256'], '/synthetic/model', 3)
                    switched.append(upgrade._switch(intent)['status'])
                except Exception as exc:
                    import traceback
                    switched.append('upgrade_failed:' + type(exc).__name__ + ':' + str(exc) + ':'
                                    + traceback.format_exc()[-1500:])
                    raise
            try:
                return self.S.default_attest(decision, proof, mode)
            except Exception as exc:
                results.append((decision['pluginRoot'], type(exc).__name__, str(exc)))
                raise
        with self.authority(executing), ExitStack() as stack:
            # The upgrade's synthetic authority (provider review, adoption,
            # attestation record), as in the schema3 lifecycle suite.
            proofs = {str(self.a.case.plugin): self.a.case.proof, str(executing): target}
            for patch in (
                    mock.patch.object(upgrade, 'review_local_provider', return_value=review),
                    mock.patch.object(upgrade, 'reattest', return_value={'vectorProfile': target['vectorProfile']}),
                    mock.patch.object(upgrade, 'adopt_host_loaded_plugin', return_value=target),
                    mock.patch.object(upgrade, 'inspect_full_plugin_candidate',
                                      side_effect=lambda root: proofs[str(root)]),
                    mock.patch.object(upgrade, 'attestation_record', return_value=new_att),
                    mock.patch.object(upgrade, 'validate_production_provider_attestation'),
                    mock.patch.object(upgrade, '_provider_file_identity', side_effect=identify),
                    mock.patch.object(upgrade, '_wait_for_sockets'),
                    mock.patch.object(upgrade, '_executing_from_candidate', create=True),
                    mock.patch.object(J, '_executing_from_candidate'),
                    mock.patch.object(J, '_inspect_identity',
                                      side_effect=lambda identity: proofs[str(identity['pluginRoot'])]),
                    mock.patch.object(J, '_bind_attestation', side_effect=lambda path, identity, proof: (
                        hashlib.sha256(J._canon(new_att if proof == target else old_att).encode()).hexdigest(),
                        identify(new_att if proof == target else old_att)))):
                stack.enter_context(patch)
            for module in (self.bundle,):
                stack.enter_context(mock.patch.object(module, 'inspect_full_plugin_candidate',
                                                      side_effect=lambda root: proofs[str(root)]))
            service = self.S.Service(self.root, bundle={'version': '1.0.0', 'digest': 'a'},
                                     start_worker=self.start_worker, admit=lambda: True,
                                     retire=lambda *_a: None, tick=.05, attest=attest)
            self.services.append(service)
            self.assertTrue(service.acquire(wait=5))
            service.start()
            self.S.register(str(case.project), root=self.root)
            deadline = time.monotonic() + 40
            while not self.ready(manifest):
                self.assertLess(time.monotonic(), deadline,
                                {'switched': switched, 'results': results,
                                 'unproved': dict(service.unproved),
                                 'state': self.E.read_owner_state(manifest)})
                time.sleep(.05)
            state = self.E.read_owner_state(manifest)
            selected = J.inspect_journal(case.journal)['selected']['pluginRoot']
            binding = service.bindings[str(case.project)]
        self.assertEqual(switched, ['active_upgrade_complete'])
        self.assertEqual(selected, str(executing))
        self.assertTrue(results)
        self.assertEqual(results[0][:2], (str(self.a.case.plugin), 'ResourceDeferred'))
        self.assertIn(results[0][2], ('worker_selection_changed', 'owner_busy'))
        self.assertEqual((state['reason'], state['terminal']), ('ready', False))
        self.assertEqual(binding.mode, 'selected')
        self.assertEqual(binding.identity[1], str(executing))

    @contextmanager
    def _serial_real_anchor(self):
        """Serialize this fixture's actual anchor windows, preserving flock."""
        sync = importlib.import_module('laya_index_sync')
        original, serial = sync._sync_anchor, threading.RLock()

        @contextmanager
        def serialized():
            with serial:
                with original() as value:
                    yield value

        with mock.patch.object(sync, '_sync_anchor', serialized):
            yield sync

    def test_cold_upgrade_lock_free_selection_changed_and_ready(self):
        # All fixture-owned anchor calls use the real lock but cannot overlap.
        original, observations = self.S.default_attest, []
        def capture(decision, proof, mode):
            try:
                return original(decision, proof, mode)
            except Exception as exc:
                observations.append((type(exc).__name__, str(exc)))
                raise
        with self._serial_real_anchor():
            with mock.patch.object(self.S, 'default_attest', side_effect=capture):
                self.test_bundle_upgrade_during_a_cold_start_binds_the_new_selection()
        self.assertEqual(observations[:1], [('ResourceDeferred', 'worker_selection_changed')])

    def test_cold_upgrade_busy_defers_before_worker_and_retries_to_ready(self):
        """An actually held UID anchor defers, then the selected Y binds."""
        original = self.S.default_attest
        observations = []
        with self._serial_real_anchor() as sync:
            def compete(decision, proof, mode):
                if observations:
                    return original(decision, proof, mode)
                before = list(self.loads)
                with sync._sync_anchor() as anchor:
                    self.assertIsNotNone(anchor, 'fixture must really own anchor')
                    try:
                        return original(decision, proof, mode)
                    except Exception as exc:
                        observations.append((type(exc).__name__, str(exc), before,
                                             list(self.loads)))
                        raise

            with mock.patch.object(self.S, 'default_attest', side_effect=compete):
                self.test_bundle_upgrade_during_a_cold_start_binds_the_new_selection()
        self.assertEqual(observations, [('ResourceDeferred', 'owner_busy', [], [])])

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

#!/usr/bin/env python3
"""Disposable replacement evidence checks; no installed QMD or model is used."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / 'core'))
SHARED = runpy.run_path(str(HERE / 'shared-qmd-setup-checks.py'))
BOUND = runpy.run_path(str(HERE / 'laya-qmd-cutover-bound-checks.py'))
BUNDLE = runpy.run_path(str(HERE / 'laya-product-bundle-checks.py'))

import laya_product_install as product  # noqa: E402
import laya_initial_build as initial_build  # noqa: E402
import laya_qmd_cutover as cutover  # noqa: E402
import qmd_shared_store  # noqa: E402
import shared_qmd_setup  # noqa: E402


class ReplacementEvidenceTests(unittest.TestCase):
    def setUp(self):
        # Reuse the existing fake native runner and strictly private HOME.
        # The helper is a TestCase, but none of its inherited tests run here.
        self.fixture = SHARED['SharedSetup']('runTest')
        self.fixture.setUp()
        self.addCleanup(lambda: (self.fixture.doCleanups(), self.fixture.tearDown()))

    def ready(self):
        old = self.fixture.legacy_selected_store()
        accepted = self.fixture.accepted_replacement_options(
            self.fixture.replacement_options())
        code, prepared = self.fixture.cli('prepare-replacement-store', accepted)
        self.assertEqual(code, 0, prepared)
        self.assertEqual(prepared['status'], 'prepared_inactive_replacement')
        return old, prepared

    def assert_old_selected(self, old):
        self.assertEqual(qmd_shared_store.select()['generation'], str(old['generation']))
        self.assertEqual(old['pointer'].read_bytes(), old['pointerBytes'])
        self.assertEqual((old['generation'] / 'index.sqlite').read_bytes(), old['indexBytes'])
        self.assertEqual((old['generation'] / 'config/index.yml').read_bytes(), old['configBytes'])
        self.assertEqual(self.fixture.original.read_bytes(),
                         b'existing global database - preserve')

    def test_inactive_proof_yields_bound_evidence_without_selecting_it(self):
        old, prepared = self.ready()
        before = old['pointer'].stat()
        evidence = shared_qmd_setup.replacement_model_evidence()
        self.assertEqual(evidence['schema'],
                         'qmd-inactive-replacement-model-evidence-v1')
        self.assertEqual(evidence['generation'], prepared['generation'])
        self.assertNotEqual(evidence['generation'], str(old['generation']))
        self.assertEqual(evidence['legacyGeneration'], str(old['generation']))
        self.assertEqual(evidence['legacyIndexPath'],
                         str(old['generation'] / 'index.sqlite'))
        self.assertEqual(evidence['modelPath'], str(self.fixture.model))
        self.assertEqual(evidence['modelSha256'],
                         hashlib.sha256(self.fixture.model.read_bytes()).hexdigest())
        self.assertEqual(evidence['modelBytes'], self.fixture.model.stat().st_size)
        self.assertEqual(evidence['dimension'], 768)
        self.assertEqual(evidence['evidenceSha256'],
                         shared_qmd_setup.coordinator.digest({
                             key: value for key, value in evidence.items()
                             if key != 'evidenceSha256'}))
        self.assertEqual((old['pointer'].stat().st_dev, old['pointer'].stat().st_ino),
                         (before.st_dev, before.st_ino))
        self.assert_old_selected(old)

    def test_same_bytes_new_pointer_inode_invalidates_evidence(self):
        old, _prepared = self.ready()
        pointer = old['pointer']
        temp = pointer.with_name('synthetic-pointer-replacement')
        temp.write_bytes(old['pointerBytes'])
        temp.chmod(0o600)
        os.replace(temp, pointer)
        self.assertEqual(pointer.read_bytes(), old['pointerBytes'])
        with self.assertRaisesRegex(ValueError, 'replacement_selected_store_changed'):
            shared_qmd_setup.replacement_model_evidence()
        self.assertEqual(qmd_shared_store.select()['generation'], str(old['generation']))

    def test_pointer_inode_swap_after_final_journal_read_invalidates_evidence(self):
        old, _prepared = self.ready()
        pointer = old['pointer']
        old_inode = pointer.stat().st_ino
        journal = qmd_shared_store.root() / shared_qmd_setup.REPLACEMENT_JOURNAL
        actual_snapshot = shared_qmd_setup._private_snapshot
        journal_reads = {'count': 0}

        def swap_after_last_read(path):
            result = actual_snapshot(path)
            if Path(path) == journal:
                journal_reads['count'] += 1
                if journal_reads['count'] == 2:
                    staged = pointer.with_name('synthetic-racing-pointer')
                    staged.write_bytes(old['pointerBytes'])
                    staged.chmod(0o600)
                    os.replace(staged, pointer)
            return result

        with mock.patch.object(shared_qmd_setup, '_private_snapshot',
                               swap_after_last_read):
            with self.assertRaisesRegex(ValueError,
                                        'replacement_selected_store_changed|replacement_store_pointer_changed'):
                shared_qmd_setup.replacement_model_evidence()
        self.assertGreaterEqual(journal_reads['count'], 2)
        self.assertEqual(pointer.read_bytes(), old['pointerBytes'])
        self.assertNotEqual(pointer.stat().st_ino, old_inode)
        self.assertEqual(qmd_shared_store.select()['generation'], str(old['generation']))
        self.assertEqual((old['generation'] / 'index.sqlite').read_bytes(), old['indexBytes'])

    def test_changed_model_bytes_fail_without_switching_store(self):
        old, _prepared = self.ready()
        self.fixture.model.write_bytes(b'GGUF' + b'y' * 64)
        with self.assertRaisesRegex(ValueError,
                                    'store_embedding_model_changed|replacement_embedding_model_changed'):
            shared_qmd_setup.replacement_model_evidence()
        self.assert_old_selected(old)

    def test_changed_model_size_fails_without_switching_store(self):
        old, _prepared = self.ready()
        self.fixture.model.write_bytes(b'GGUF' + b'z' * 65)
        with self.assertRaisesRegex(ValueError,
                                    'store_embedding_model_changed|replacement_embedding_model_changed'):
            shared_qmd_setup.replacement_model_evidence()
        self.assert_old_selected(old)

    def test_changed_inactive_proof_fails_without_switching_store(self):
        old, prepared = self.ready()
        proof = Path(prepared['generation']) / 'bootstrap-proof.json'
        value = json.loads(proof.read_text())
        value['searchProved'] = False
        proof.write_text(json.dumps(value, sort_keys=True))
        with self.assertRaisesRegex(ValueError, 'store_preparation_changed'):
            shared_qmd_setup.replacement_model_evidence()
        self.assert_old_selected(old)

    def test_scope_policy_drift_fails_without_switching_store(self):
        old, _prepared = self.ready()
        (self.fixture.project / '.auto-context/settings.json').write_text(
            '{"indexing": false,"collections": []}')
        with self.assertRaisesRegex(ValueError,
                                    'store_project_scope_invalid|replacement_project_scope_changed'):
            shared_qmd_setup.replacement_model_evidence()
        self.assertEqual(qmd_shared_store.select()['generation'], str(old['generation']))
        self.assertEqual(old['pointer'].read_bytes(), old['pointerBytes'])

    def test_prepare_binding_rechecks_original_db_scope_and_reviewed_model(self):
        old, _prepared = self.ready()
        evidence = shared_qmd_setup.replacement_model_evidence()
        review = {'replacementEvidenceSha256': evidence['evidenceSha256'],
                  'reviewedProvider': {'providerManifest': {
                      'modelSha256': evidence['modelSha256'],
                      'dimension': evidence['dimension']}}}
        plan = {'sourceDb': evidence['legacyIndexPath'],
                'qmdStoreGeneration': evidence['legacyGeneration'],
                'projectRoot': str(self.fixture.project),
                'settingsSha256': hashlib.sha256(
                    self.fixture.original_settings).hexdigest()}
        self.assertEqual(product._replacement_origin_binding(review, plan),
                         evidence['evidenceSha256'])
        self.assertEqual(product._replacement_origin_binding({}, plan), None)
        variants = (
            ({**plan, 'sourceDb': str(self.fixture.original)}, review),
            ({**plan, 'qmdStoreGeneration': 'g-' + '0' * 32}, review),
            ({**plan, 'settingsSha256': '0' * 64}, review),
            (plan, {**review, 'replacementEvidenceSha256': '0' * 64}),
            (plan, {**review, 'reviewedProvider': {'providerManifest': {
                **review['reviewedProvider']['providerManifest'],
                'modelSha256': '0' * 64}}}),
        )
        for changed_plan, changed_review in variants:
            with self.subTest(plan=changed_plan, review=changed_review):
                with self.assertRaisesRegex(product.InstallBlocked,
                                            'replacement_model_origin_mismatch'):
                    product._replacement_origin_binding(changed_review, changed_plan)
        self.assert_old_selected(old)


class ProviderFallbackReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = BUNDLE['ProductBundleChecks']('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_missing_selected_bootstrap_uses_inactive_evidence_in_proposal(self):
        fixture = self.fixture
        installed = fixture.candidate('replacement-review', fixture.commit1)
        package = fixture.root / 'synthetic-qmd-package'
        (package / 'bin').mkdir(parents=True)
        (package / 'bin/qmd').write_text('synthetic executable')
        (package / 'package.json').write_text('{"version":"2.5.3"}')
        module_pkg = package / 'node_modules/node-llama-cpp'
        (module_pkg / 'dist').mkdir(parents=True)
        (module_pkg / 'package.json').write_text('{"version":"3.18.1"}')
        (module_pkg / 'dist/index.js').write_text('export const synthetic = true;')
        native_pkg = package / 'node_modules/@node-llama-cpp/mac-arm64-metal'
        (native_pkg / 'bins').mkdir(parents=True)
        (native_pkg / 'package.json').write_text('{"version":"3.18.1"}')
        (native_pkg / 'bins/llama-addon.node').write_bytes(b'synthetic native')
        (native_pkg / 'bins/libllama.dylib').write_bytes(b'synthetic dylib')
        model = fixture.root / 'embeddinggemma-300M-Q8_0.gguf'
        model.write_bytes(b'GGUFsynthetic reviewed model')
        selected = fixture.root / ('g-' + '1' * 32)
        selected.mkdir(mode=0o700)
        evidence = {'legacyGeneration': str(selected), 'modelPath': str(model),
                    'modelSha256': hashlib.sha256(model.read_bytes()).hexdigest(),
                    'dimension': 3, 'evidenceSha256': 'a' * 64}
        runtime = {'proof': {'version': '2.5.3',
                             'qmd': str(package / 'bin/qmd'),
                             'node': str(fixture.node)},
                   'generation': 'synthetic-runtime'}
        with (mock.patch('qmd_runtime.select', return_value=runtime),
              mock.patch('qmd_shared_store.select',
                         return_value={'generation': str(selected)}),
              mock.patch.object(shared_qmd_setup, 'replacement_model_evidence',
                                return_value=evidence),
              mock.patch('platform.system', return_value='Darwin'),
              mock.patch('platform.machine', return_value='arm64')):
            first = product.review_local_provider(
                source_root=fixture.source, plugin_root=installed['pluginRoot'],
                state_root=fixture.install, commit_id=fixture.commit1)
            self.assertEqual(first['replacementEvidenceSha256'], 'a' * 64)
            self.assertEqual(first['reviewedProvider']['providerManifest']['modelSha256'],
                             evidence['modelSha256'])
            evidence['evidenceSha256'] = 'b' * 64
            changed = product.review_local_provider(
                source_root=fixture.source, plugin_root=installed['pluginRoot'],
                state_root=fixture.install, commit_id=fixture.commit1)
            self.assertEqual(changed['replacementEvidenceSha256'], 'b' * 64)
            self.assertNotEqual(changed['proposalSha256'], first['proposalSha256'])


class ReplacementBoundCutoverTests(BOUND['BoundCase']):
    """The real v3 candidate/backup path with a synthetic proof/attestation.

    The evidence reader itself is exercised above using the private QMD native
    fixture. Here it is patched only to place a replacement proof beside the
    existing full raw+wiki v3 bound-build fixture without loading a model.
    """

    def attach_replacement(self, *, attested_model='b' * 64):
        self.bound(docs=3, wiki=1)
        existing = self.row()
        source_before = self.qmd.read_bytes()
        # Recreate only this disposable ledger/marker with the new optional
        # evidence field. The already sealed candidate and backup stay intact.
        self.marker.unlink()
        self.ledger.unlink()
        self.evidence = {'evidenceSha256': 'a' * 64,
                         'sourceDb': str(self.qmd),
                         'modelSha256': 'b' * 64, 'dimension': 2}
        self.attested_provider = {'providerManifest': {
            'modelSha256': attested_model, 'dimension': 2}}
        for target, name, replacement in (
                (shared_qmd_setup, 'replacement_model_evidence', lambda: dict(self.evidence)),
                ('laya_install_journal', '_attestation',
                 lambda _journal: (dict(self.attested_provider), self.fj.attestation))):
            patcher = (mock.patch.object(target, name, replacement)
                       if not isinstance(target, str) else
                       mock.patch(target + '.' + name, replacement))
            patcher.start()
            self.addCleanup(patcher.stop)
        return existing, source_before

    def bind_replacement(self, previous):
        return cutover.bind_qmd_origin_bound(
            **self.bind_kwargs(previous,
                replacement_evidence_sha256=self.evidence['evidenceSha256']))

    def retirement_journal(self, row):
        # The normal bound fixture stubs only fields used by activation. The
        # explicit retire API also binds the inactive journal's owner/scope.
        original = self.fj.inspect
        def owner_journal(path=None):
            return {**original(path), 'projectRoot': str(self.project),
                    'scopeId': row['scope']}
        for target in (cutover, product):
            patcher = mock.patch.object(target, 'inspect_journal', owner_journal)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_bind_rejects_attested_model_mismatch_before_marker_or_route(self):
        previous, source_before = self.attach_replacement(attested_model='0' * 64)
        with self.assertRaisesRegex(cutover.CutoverBlocked,
                                    'cutover_replacement_evidence_changed'):
            self.bind_replacement(previous)
        self.assertFalse(self.ledger.exists())
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.route.exists())
        self.assertEqual(self.qmd.read_bytes(), source_before)
        self.assertTrue(Path(previous['backupDb']).is_file())
        self.attested_provider['providerManifest']['modelSha256'] = 'b' * 64
        row = self.bind_replacement(previous)
        self.assertEqual(row['replacementEvidenceSha256'], 'a' * 64)
        self.assertEqual(row['sourceDb'], str(self.qmd))
        self.assertFalse(self.route.exists())

    def test_activation_refuses_changed_proof_then_prewrite_restore_keeps_qmd(self):
        previous, source_before = self.attach_replacement()
        self.bind_replacement(previous)
        self.evidence['evidenceSha256'] = 'f' * 64
        with self.assertRaisesRegex(cutover.CutoverBlocked,
                                    'cutover_replacement_evidence_changed'):
            self.activate()
        self.assertEqual(self.row()['phase'], 'prepared')
        self.assertFalse(self.route.exists())
        self.assertEqual(self.fj.state, 'candidate_ready')
        self.evidence['evidenceSha256'] = 'a' * 64
        self.assertEqual(self.activate()['phase'], 'active')
        self.assertEqual(self.restore()['phase'], 'restored')
        self.assertFalse(self.route.exists())
        self.assertEqual(self.qmd.read_bytes(), source_before)
        self.assertTrue(Path(previous['backupDb']).is_file())

    def test_proof_drift_after_route_write_withdraws_uncommitted_route(self):
        previous, _source_before = self.attach_replacement()
        self.bind_replacement(previous)
        reads = {'count': 0}

        def changed_after_route():
            reads['count'] += 1
            result = dict(self.evidence)
            if reads['count'] >= 3:
                result['evidenceSha256'] = 'f' * 64
            return result

        with mock.patch.object(shared_qmd_setup, 'replacement_model_evidence',
                               changed_after_route):
            with self.assertRaisesRegex(cutover.CutoverBlocked,
                                        'cutover_replacement_evidence_changed'):
                self.activate()
        self.assertGreaterEqual(reads['count'], 3)
        self.assertEqual(self.row()['phase'], 'prepared')
        self.assertFalse(self.route.exists())
        self.assertEqual(self.fj.state, 'candidate_ready')

    def assert_unreadable_proof_after_route_withdraws(self, unreadable):
        previous, source_before = self.attach_replacement()
        self.bind_replacement(previous)
        reads = {'count': 0}

        def unreadable_after_route():
            reads['count'] += 1
            return unreadable if reads['count'] == 3 else dict(self.evidence)

        with mock.patch.object(shared_qmd_setup, 'replacement_model_evidence',
                               unreadable_after_route):
            with self.assertRaisesRegex(cutover.CutoverBlocked,
                                        'cutover_replacement_evidence_changed'):
                self.activate()
        self.assertGreaterEqual(reads['count'], 3)
        self.assertEqual(self.row()['phase'], 'prepared')
        self.assertFalse(self.route.exists())
        self.assertEqual(self.fj.state, 'candidate_ready')
        self.assertEqual(self.qmd.read_bytes(), source_before)

    def test_none_proof_after_route_write_is_withdrawn(self):
        self.assert_unreadable_proof_after_route_withdraws(None)

    def test_malformed_proof_after_route_write_is_withdrawn(self):
        self.assert_unreadable_proof_after_route_withdraws({'malformed': True})

    def test_after_first_write_rollback_refuses_but_forward_recovery_works(self):
        previous, source_before = self.attach_replacement()
        self.bind_replacement(previous)
        self.assertEqual(self.activate()['phase'], 'active')
        self.fj.new_write = True
        with self.assertRaisesRegex(cutover.CutoverBlocked,
                                    'cutover_rollback_not_authorized'):
            self.restore()
        self.route.unlink()  # interrupted route withdrawal after first write
        row = self.row()
        row['phase'] = 'restoring'
        cutover._write(self.ledger, row, replace=True)
        self.evidence['evidenceSha256'] = 'f' * 64  # old proof no longer available
        recovered = self.recover()
        self.assertEqual(recovered['phase'], 'active')
        self.assertEqual(self.route.read_bytes(), self.route_bytes)
        self.assertEqual(self.qmd.read_bytes(), source_before)
        self.assertTrue(Path(previous['backupDb']).is_file())

    def test_valid_replacement_proof_cannot_retire_prepared_binding(self):
        previous, source_before = self.attach_replacement()
        row = self.bind_replacement(previous)
        self.retirement_journal(row)
        with self.assertRaises(cutover.CutoverBlocked):
            cutover.retire_unusable_replacement_binding(
                self.project, self.journal_path, self.route_bytes)
        self.assertEqual(self.row()['phase'], 'prepared')
        self.assertTrue(self.marker.is_file())
        self.assertFalse(self.route.exists())
        self.assertEqual(self.fj.state, 'candidate_ready')
        self.assertEqual(self.qmd.read_bytes(), source_before)

    def test_lost_prepared_proof_retires_with_resumable_marker_cleanup(self):
        previous, source_before = self.attach_replacement()
        row = self.bind_replacement(previous)
        self.retirement_journal(row)
        old_shared_pointer = self.home / 'synthetic-shared-active.json'
        old_shared_pointer.write_bytes(b'{"generation":"old-selected"}')
        old_shared_pointer.chmod(0o600)
        old_pointer = old_shared_pointer.read_bytes()
        backup = Path(row['backupDb'])
        backup_bytes = backup.read_bytes()
        candidate = Path(row['candidateDb'])
        self.evidence['evidenceSha256'] = 'f' * 64
        with self.assertRaisesRegex(cutover.CutoverBlocked,
                                    'cutover_replacement_evidence_changed'):
            self.activate()
        self.assertFalse(self.route.exists())
        retirement_path = self.state / cutover.RETIREMENT
        real_write = cutover._write

        def interrupt_record(path, value, *, replace):
            if Path(path) == retirement_path:
                raise RuntimeError('synthetic_retirement_interruption')
            return real_write(path, value, replace=replace)

        with mock.patch.object(cutover, '_write', interrupt_record):
            with self.assertRaisesRegex(RuntimeError,
                                        'synthetic_retirement_interruption'):
                cutover.retire_unusable_replacement_binding(
                    self.project, self.journal_path, self.route_bytes)
        self.assertEqual(self.row()['phase'], cutover.RETIRED)
        self.assertTrue(self.marker.is_file())
        self.assertFalse(retirement_path.exists())
        retired = cutover.retire_unusable_replacement_binding(
            self.project, self.journal_path, self.route_bytes)
        self.assertEqual(retired['phase'], cutover.RETIRED)
        self.assertEqual(retired['retirementReason'], 'replacement_evidence_invalid')
        self.assertEqual(retired['retirement']['reason'], 'replacement_evidence_invalid')
        self.assertTrue(retirement_path.is_file())
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.route.exists())
        self.assertEqual(self.fj.state, 'candidate_ready')
        self.assertEqual(old_shared_pointer.read_bytes(), old_pointer)
        self.assertEqual(self.qmd.read_bytes(), source_before)
        self.assertEqual(backup.read_bytes(), backup_bytes)
        self.assertTrue(candidate.is_file())
        self.assertEqual(cutover.retire_unusable_replacement_binding(
            self.project, self.journal_path, self.route_bytes)['epoch'],
                         retired['epoch'])

    def test_product_retirement_dispatches_lost_proof_and_releases_build_pointer(self):
        previous, source_before = self.attach_replacement()
        row = self.bind_replacement(previous)
        self.retirement_journal(row)
        self.assertIsNotNone(initial_build.lookup(str(self.project)))
        self.evidence['evidenceSha256'] = 'f' * 64
        first = product.retire_drifted_origin(project_root=self.project,
                                               state_root=self.state)
        self.assertEqual(first['status'], 'origin_binding_retired')
        self.assertEqual(first['cutoverPhase'], cutover.RETIRED)
        self.assertEqual(first['retirementReason'], 'replacement_evidence_invalid')
        self.assertEqual(first['buildPointer'], 'retired')
        self.assertIsNone(initial_build.lookup(str(self.project)))
        self.assertTrue((self.state / initial_build.RETIRED_POINTER).is_file())
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.route.exists())
        self.assertEqual(self.qmd.read_bytes(), source_before)
        self.assertTrue(Path(row['backupDb']).is_file())
        again = product.retire_drifted_origin(project_root=self.project,
                                               state_root=self.state)
        self.assertEqual(again['retirementReason'], 'replacement_evidence_invalid')
        self.assertEqual(again['buildPointer'], 'absent')
        self.assertEqual(again['retiredGeneration'], first['retiredGeneration'])
        self.assertEqual(self.row()['phase'], cutover.RETIRED)

    def test_product_retries_direct_retirement_with_sealed_build_record(self):
        previous, source_before = self.attach_replacement()
        row = self.bind_replacement(previous)
        self.retirement_journal(row)
        # Reproduce a process death after the direct cutover owner committed
        # retirement while the product build record still says sealed.
        record = initial_build.read(self.state)
        initial_build._write_json(initial_build.record_path(self.state),
                                  {**record, 'phase': 'sealed'}, replace=True)
        self.evidence['evidenceSha256'] = 'f' * 64
        direct = cutover.retire_unusable_replacement_binding(
            self.project, self.journal_path, self.route_bytes)
        self.assertEqual(direct['phase'], cutover.RETIRED)
        self.assertEqual(initial_build.read(self.state)['phase'], 'sealed')
        self.assertIsNotNone(initial_build.lookup(str(self.project)))
        resumed = product.retire_drifted_origin(project_root=self.project,
                                                 state_root=self.state)
        self.assertEqual(resumed['retirementReason'], 'replacement_evidence_invalid')
        self.assertEqual(resumed['buildPhase'], 'bound')
        self.assertEqual(resumed['buildPointer'], 'retired')
        self.assertEqual(initial_build.read(self.state)['phase'], 'bound')
        self.assertIsNone(initial_build.lookup(str(self.project)))
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.route.exists())
        self.assertEqual(self.qmd.read_bytes(), source_before)
        self.assertTrue(Path(row['backupDb']).is_file())

    def test_drift_retirement_replay_settles_without_reopening_unavailable_qmd(self):
        self.bound(docs=3, wiki=1)
        prepared = self.row()
        backup = Path(prepared['backupDb'])
        backup_bytes = backup.read_bytes()
        self.qmd_indexes('later.md', '# Later\nQMD advanced before retirement\n')
        retirement_path = self.state / cutover.RETIREMENT
        real_write = cutover._write

        def interrupt_record(path, value, *, replace):
            if Path(path) == retirement_path:
                raise RuntimeError('synthetic_drift_retirement_interruption')
            return real_write(path, value, replace=replace)

        with mock.patch.object(cutover, '_write', interrupt_record):
            with self.assertRaisesRegex(RuntimeError,
                                        'synthetic_drift_retirement_interruption'):
                cutover.retire_drifted_binding(
                    self.project, self.journal_path, self.route_bytes)
        committed = self.row()
        self.assertEqual(committed['phase'], cutover.RETIRED)
        pinned = committed['retirementObservedSourceDigest']
        self.assertNotEqual(pinned, prepared['sourceDigest'])
        self.assertFalse(retirement_path.exists())
        self.assertTrue(self.marker.is_file())
        offline = self.qmd.with_name('temporarily-unavailable-qmd.sqlite')
        self.qmd.rename(offline)  # preserve the disposable source bytes
        self.assertFalse(self.qmd.exists())
        self.fj.state = 'active'  # later journal state cannot undo terminal CAS
        self.fj.new_write = True
        settled = product.retire_drifted_origin(project_root=self.project,
                                                 state_root=self.state)
        self.assertEqual(settled['cutoverPhase'], cutover.RETIRED)
        self.assertEqual(settled['observedSourceDigest'], pinned)
        self.assertEqual(settled['buildPointer'], 'retired')
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.route.exists())
        self.assertEqual(backup.read_bytes(), backup_bytes)
        self.assertTrue(Path(prepared['candidateDb']).is_file())
        self.assertTrue(offline.is_file())
        self.assertIsNone(initial_build.lookup(str(self.project)))

    def test_same_digest_new_source_inode_can_retire_but_cannot_activate(self):
        self.bound(docs=3, wiki=1)
        prepared = self.row()
        source_bytes = self.qmd.read_bytes()
        backup = Path(prepared['backupDb'])
        backup_bytes = backup.read_bytes()
        replacement = self.qmd.with_name('same-digest-new-inode.sqlite')
        shutil.copy2(self.qmd, replacement)
        original_copy = self.qmd.with_name('preserved-original-inode.sqlite')
        os.link(self.qmd, original_copy)
        os.replace(replacement, self.qmd)
        current = [self.qmd.stat().st_dev, self.qmd.stat().st_ino]
        self.assertNotEqual(current, prepared['sourceIdentity'])
        self.assertEqual(self.qmd.read_bytes(), source_bytes)
        with self.assertRaisesRegex(cutover.CutoverBlocked,
                                    'cutover_bound_file_replaced'):
            self.activate()
        self.assertEqual(self.row()['phase'], 'prepared')
        self.assertFalse(self.route.exists())
        retired = cutover.retire_drifted_binding(
            self.project, self.journal_path, self.route_bytes)
        self.assertEqual(retired['phase'], cutover.RETIRED)
        self.assertEqual(retired['retirementReason'], 'source_identity_changed')
        self.assertEqual(retired['retirementObservedSourceIdentity'], current)
        self.assertEqual(retired['retirementObservedSourceDigest'], prepared['sourceDigest'])
        self.assertEqual(retired['retirement']['reason'], 'source_identity_changed')
        self.assertEqual(retired['retirement']['observedSourceIdentity'], current)
        self.assertFalse(self.marker.exists())
        self.assertEqual(backup.read_bytes(), backup_bytes)
        self.assertEqual(original_copy.read_bytes(), source_bytes)

    def test_retired_replay_refuses_old_route_and_preserves_later_route(self):
        self.bound(docs=3, wiki=1)
        prepared = self.row()
        self.qmd_indexes('later.md', '# Later\nsource drift\n')
        retirement_path = self.state / cutover.RETIREMENT
        real_write = cutover._write

        def interrupt_record(path, value, *, replace):
            if Path(path) == retirement_path:
                raise RuntimeError('synthetic_retired_before_record')
            return real_write(path, value, replace=replace)

        with mock.patch.object(cutover, '_write', interrupt_record):
            with self.assertRaisesRegex(RuntimeError,
                                        'synthetic_retired_before_record'):
                cutover.retire_drifted_binding(
                    self.project, self.journal_path, self.route_bytes)
        self.assertEqual(self.row()['phase'], cutover.RETIRED)
        self.route.write_bytes(self.route_bytes)
        self.route.chmod(0o600)
        with self.assertRaisesRegex(cutover.CutoverBlocked,
                                    'cutover_route_uncertain'):
            cutover.retire_drifted_binding(
                self.project, self.journal_path, self.route_bytes)
        self.assertFalse(retirement_path.exists())
        self.route.unlink()
        later_route = b'{"synthetic":"later-install"}'
        later_marker = {'schema': 'later-install', 'journalPath':
                        str(self.state / 'later-install-journal.sqlite')}
        self.marker.write_bytes(cutover._canon(later_marker))
        self.route.write_bytes(later_route)
        self.route.chmod(0o600)
        self.fj.state = 'active'
        self.fj.new_write = True
        resumed = product.retire_drifted_origin(project_root=self.project,
                                                 state_root=self.state)
        self.assertEqual(resumed['cutoverPhase'], cutover.RETIRED)
        self.assertEqual(resumed['buildPointer'], 'retired')
        self.assertTrue(retirement_path.is_file())
        self.assertEqual(self.marker.read_bytes(), cutover._canon(later_marker))
        self.assertEqual(self.route.read_bytes(), later_route)
        self.assertTrue(Path(prepared['backupDb']).is_file())
        self.assertIsNone(initial_build.lookup(str(self.project)))


if __name__ == '__main__':
    unittest.main()

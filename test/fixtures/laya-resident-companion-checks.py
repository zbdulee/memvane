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

from contextlib import ExitStack, contextmanager
import importlib
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('h8_companion_seed', HERE / 'laya-reader-move-seal-checks.py')
seed_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed_module)
J = seed_module.base.J


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


if __name__ == '__main__':
    unittest.main()

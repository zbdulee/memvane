#!/usr/bin/env python3
"""Fenced recovery crash contracts on temp stores; one real-journal integration."""
from contextlib import closing, contextmanager
import copy
import json
from pathlib import Path
import runpy
import shutil
import sqlite3
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / 'core'))
import laya_install_journal as J
REAL_INSPECT, REAL_ROUTE = J.inspect_journal, J._route
BASE = runpy.run_path(str(HERE / 'laya-fresh-bundle-upgrade-checks.py'))
active, fresh = BASE['active'], BASE['fresh']
REAL_PROJECT_VERIFY = active._verify_recovery_project
Kill, proof, manifest = BASE['Kill'], BASE['proof'], BASE['manifest']


@contextmanager
def fixture():
    case = BASE['UpgradeCase']('runTest')
    case.setUp()
    try:
        case.bound()
        case.activate()
        yield case
    finally:
        case.doCleanups()


def fence(case):
    real = active.inactive._atomic_expected
    def stop(path, old, new):
        if Path(path).name == fresh.ROUTE:
            raise Kill()
        return real(path, old, new)
    with mock.patch.object(active.inactive, '_atomic_expected', stop):
        with case.assertRaises(Kill):
            case.run_upgrade(active)
    case.assertEqual(case.fj.state, 'pending')
    return json.loads((case.state / active.INTENT).read_bytes())


def target(case, number):
    root, commit = str(case.home / ('host-' + str(number))), str(number) * 40
    case.bundles[root] = proof(root, commit)
    case.review['reviewedProvider']['providerManifest'] = manifest(root)
    return root, commit


class RecoveryTests(unittest.TestCase):
    def test_pending_lost_and_reextracted_targets_keep_original_snapshot(self):
        for replaced in (False, True):
            with self.subTest(replaced=replaced), fixture() as case:
                original = fence(case)
                if replaced:
                    for root in (case.old_root, case.new_root):
                        case.bundles[root] = {**case.bundles[root],
                                             'ownershipManifestSha256': 'f' * 64}
                        BASE['REEXTRACTED'].add(root)
                else:
                    case.removed.update((case.old_root, case.new_root))
                result = case.run_upgrade(active, *target(case, 3))
                self.assertEqual(result['status'], 'active_upgrade_complete')
                final = json.loads((case.state / active.INTENT).read_bytes())
                for key in ('backup', 'oldRecord', 'oldLedger', 'oldRoute',
                            'oldAttestation', 'oldMarker', 'counts'):
                    self.assertEqual(final[key], original[key], key)
                self.assertEqual(case.selection(), 'candidate')

    def test_every_durable_step_resumes_without_early_selection(self):
        steps = ('normalize-descriptor', 'switch-descriptor', 'cas',
                 'metadata-0', 'metadata-1', 'metadata-2', 'metadata-3',
                 'metadata-4', 'activate')
        for step in steps:
            with self.subTest(step=step), fixture() as case:
                fence(case)
                case.removed.update((case.old_root, case.new_root))
                args = target(case, 3)
                def stop(name):
                    if name == step:
                        raise Kill()
                with mock.patch.object(active, '_recovery_step', stop):
                    with self.assertRaises(Kill):
                        case.run_upgrade(active, *args)
                self.assertEqual(case.row()['phase'], 'upgrading')
                self.assertEqual(case.selection(), 'blocked')
                self.assertEqual(case.run_upgrade(active, *args)['status'],
                                 'active_upgrade_complete')

    def test_lost_recovery_target_after_each_step_accepts_another_target(self):
        steps = ('normalize-descriptor', 'switch-descriptor', 'cas',
                 'metadata-0', 'metadata-1', 'metadata-2', 'metadata-3',
                 'metadata-4', 'activate')
        for step in steps:
            with self.subTest(step=step), fixture() as case:
                original = fence(case)
                case.removed.update((case.old_root, case.new_root))
                second = target(case, 3)
                def stop(name):
                    if name == step:
                        raise Kill()
                with mock.patch.object(active, '_recovery_step', stop):
                    with self.assertRaises(Kill):
                        case.run_upgrade(active, *second)
                case.removed.add(second[0])
                third = target(case, 4)
                # Re-enter the newly durable normalization intent as well.
                def stop_normalize(name):
                    if name == 'normalize-descriptor':
                        raise Kill()
                with mock.patch.object(active, '_recovery_step', stop_normalize):
                    with self.assertRaises(Kill):
                        case.run_upgrade(active, *third)
                self.assertEqual(case.row()['phase'], 'upgrading')
                self.assertEqual(case.selection(), 'blocked')
                self.assertEqual(case.run_upgrade(active, *third)['status'],
                                 'active_upgrade_complete')
                final = json.loads((case.state / active.INTENT).read_bytes())
                for key in ('backup', 'oldRecord', 'oldLedger', 'oldRoute',
                            'oldAttestation', 'oldMarker', 'counts'):
                    self.assertEqual(final[key], original[key], key)

    def test_cas_loss_normalize_crash_then_third_target_never_uses_missing_fid(self):
        with fixture() as case:
            with mock.patch.object(active, '_replace_ledger', side_effect=Kill()):
                with self.assertRaises(Kill):
                    case.run_upgrade(active)
            case.removed.update((case.old_root, case.new_root))
            second = target(case, 3)
            def kill_cas(name):
                if name == 'cas':
                    raise Kill()
            with mock.patch.object(active, '_recovery_step', kill_cas):
                with self.assertRaises(Kill):
                    case.run_upgrade(active, *second)
            case.removed.add(second[0])
            third = target(case, 4)
            observed = []
            original_fid = BASE['file_identity']
            def live_fid(att):
                path = att['providerManifest']['workerPath']
                observed.append(path)
                if any(path.startswith(root + '/') for root in case.removed):
                    raise AssertionError('missing target FID must never be measured')
                return original_fid(att)
            def kill_normalize(name):
                if name == 'normalize-route':
                    raise Kill()
            before_activations = case.fj.calls.count('activate')
            with case.patches(active), \
                    mock.patch.object(active, '_provider_file_identity', live_fid), \
                    mock.patch.object(active, '_recovery_step', kill_normalize):
                with self.assertRaises(Kill):
                    active.upgrade(project_root=str(case.project), state_root=str(case.state),
                                   source_root=str(case.home / 'marketplace'),
                                   plugin_root=third[0], commit_id=third[1],
                                   accepted_provider_sha256='e' * 64,
                                   model_path='/models/m.gguf', dimension=3)
            intent = json.loads((case.state / active.INTENT).read_bytes())
            self.assertEqual(intent['schema'], 'laya-active-upgrade-intent-v2')
            self.assertEqual(intent['recovery']['mode'], 'normalize')
            self.assertNotIn('recovery', intent['recovery'])
            self.assertEqual(case.fj.calls.count('activate'), before_activations)
            self.assertEqual(case.row()['phase'], 'upgrading')
            self.assertEqual(case.run_upgrade(active, *third)['status'], 'active_upgrade_complete')

    def test_active_target_fence_advances_without_original_restore_authority(self):
        with fixture() as case:
            original_change = active._Ledger.change
            def stop(ledger, path, epoch, before, after):
                if (before, after) == ('upgrading', 'active'):
                    raise Kill()
                return original_change(ledger, path, epoch, before, after)
            with mock.patch.object(active._Ledger, 'change', stop):
                with self.assertRaises(Kill):
                    case.run_upgrade(active)
            self.assertEqual(case.fj.state, 'active')
            case.removed.update((case.old_root, case.new_root))
            self.assertEqual(case.run_upgrade(active, *target(case, 3))['status'],
                             'active_upgrade_complete')
            self.assertEqual(case.fj.previous['pluginRoot'], case.new_root)
            with case.patches(active):
                with self.assertRaisesRegex(Exception, 'forward_only'):
                    active.upgrade(project_root=str(case.project), state_root=str(case.state),
                                   source_root='', plugin_root='', commit_id='',
                                   accepted_provider_sha256='', model_path=None,
                                   dimension=3, restore=True)

    def test_tampered_metadata_epoch_write_provider_and_counts_refuse(self):
        for corruption in ('nonprefix', 'epoch', 'write', 'provider', 'settings', 'counts'):
            with self.subTest(corruption=corruption), fixture() as case:
                intent = fence(case)
                args = target(case, 3)
                if corruption == 'nonprefix':
                    (case.state / J.PRODUCTION_ATTESTATION).write_text(intent['oldAttestation'])
                elif corruption == 'epoch':
                    case.fj.epoch += 7
                elif corruption == 'write':
                    case.fj.new_write = True
                elif corruption == 'provider':
                    case.review['reviewedProvider']['providerManifest']['modelSha256'] = 'a' * 64
                elif corruption == 'settings':
                    case.settings.write_text('{"name":"other","skipPaths":[]}')
                else:
                    intent['counts']['vectorChunkSets'] += 1
                    active.build._write_json(case.state / active.INTENT, intent,
                                             replace=True, max_bytes=active.LIMIT)
                before = case.route.read_bytes()
                with self.assertRaises(Exception):
                    case.run_upgrade(active, *args)
                self.assertEqual(case.row()['phase'], 'upgrading')
                self.assertEqual(case.route.read_bytes(), before)

    def test_normalize_descriptor_fences_legacy_loader_and_malformed_records(self):
        with fixture() as case:
            fence(case)
            before = case.route.read_bytes()
            args = target(case, 3)
            def stop(name):
                if name == 'normalize-descriptor':
                    raise Kill()
            with mock.patch.object(active, '_recovery_step', stop):
                with self.assertRaises(Kill):
                    case.run_upgrade(active, *args)
            saved = json.loads((case.state / active.INTENT).read_bytes())
            # The frozen v1 loader's schema gate rejects this before any switch.
            def legacy_load():
                value = active.build._read_json(case.state / active.INTENT,
                                               max_bytes=active.LIMIT)
                if value.get('schema') != 'laya-active-upgrade-intent-v1':
                    active._fail('active_upgrade_intent_invalid')
                return value
            with self.assertRaisesRegex(Exception, 'active_upgrade_intent_invalid'):
                legacy_load()
            self.assertEqual(case.route.read_bytes(), before)
            for malformed in ([], {**saved['recovery'], 'extra': True},
                              {**saved['recovery'], 'before': []},
                              {**saved['recovery'], 'before': ['x' * (1024 * 1024 + 1)] * 5}):
                with self.subTest(kind=type(malformed).__name__):
                    altered = {**saved, 'recovery': malformed}
                    # Oversize records may be rejected by the writer itself.
                    if len(active._bytes(altered)) > active.LIMIT:
                        with self.assertRaises(Exception):
                            active.build._write_json(case.state / active.INTENT, altered,
                                                     replace=True, max_bytes=active.LIMIT)
                    else:
                        active.build._write_json(case.state / active.INTENT, altered,
                                                 replace=True, max_bytes=active.LIMIT)
                        with self.assertRaisesRegex(Exception, 'active_upgrade_recovery_invalid'):
                            active._load_intent(case.state, case.project)
            active.build._write_json(case.state / active.INTENT, saved,
                                     replace=True, max_bytes=active.LIMIT)
            self.assertEqual(case.run_upgrade(active, *args)['status'], 'active_upgrade_complete')

    def test_real_journal_pending_recovery_uses_real_cas_and_observation(self):
        with fixture() as case, case.patches(active), \
                mock.patch.object(J, 'inspect_journal', REAL_INSPECT), \
                mock.patch.object(J, '_route', REAL_ROUTE), \
                mock.patch.object(fresh, 'inspect_journal', REAL_INSPECT), \
                mock.patch.object(J, 'inspect_full_plugin_candidate', case.inspect_bundle), \
                mock.patch.object(J, '_provider_file_identity', BASE['file_identity']), \
                mock.patch.object(J, 'validate_production_provider_attestation', lambda *_a: None), \
                mock.patch.object(active, 'inspect_journal', REAL_INSPECT), \
                mock.patch.object(active, '_verify_recovery_project', REAL_PROJECT_VERIFY), \
                mock.patch.object(active, 'cas_stage_update', J.cas_stage_update), \
                mock.patch.object(active, 'cas_retarget_pending', J.cas_retarget_pending), \
                mock.patch.object(active, 'cas_activate', J.cas_activate):
            case.journal_path.unlink()
            created = J.create_journal(case.journal_path, case.old, case.route.read_bytes())
            J.cas_activate(case.journal_path, created['epoch'], case.route.read_bytes())
            def run(args):
                return active.upgrade(project_root=str(case.project), state_root=str(case.state),
                                      source_root=str(case.home / 'marketplace'),
                                      plugin_root=args[0], commit_id=args[1],
                                      accepted_provider_sha256='e' * 64,
                                      model_path='/models/m.gguf', dimension=3)
            atomic = active.inactive._atomic_expected
            def stop(path, old, new):
                if Path(path).name == fresh.ROUTE:
                    raise Kill()
                return atomic(path, old, new)
            with mock.patch.object(active.inactive, '_atomic_expected', stop):
                with self.assertRaises(Kill):
                    run((case.new_root, BASE['NEW_COMMIT']))
            self.assertEqual(REAL_INSPECT(case.journal_path)['state'], 'pending')
            legacy = json.loads((case.state / active.INTENT).read_bytes())
            legacy.pop('journalBefore')
            active.build._write_json(case.state / active.INTENT, legacy,
                                     replace=True, max_bytes=active.LIMIT)
            case.removed.update((case.old_root, case.new_root))
            next_args = target(case, 3)
            def change_scope(value):
                with closing(sqlite3.connect(case.journal_path)) as db:
                    db.execute('UPDATE selection SET scope_id=?', (value,))
                    db.commit()
            original_scope = json.loads(legacy['oldRoute'])['scopeId']
            before_scope = (case.state / active.INTENT).read_bytes()
            change_scope('f' * 32)
            try:
                with self.assertRaises(J.JournalEvidenceError):
                    run(next_args)
                self.assertEqual((case.state / active.INTENT).read_bytes(), before_scope,
                                 'a changed journal scope must refuse before any intent write')
            finally:
                change_scope(original_scope)
            original_project = case.project.with_name('original-project')
            replacement_project = case.project.with_name('replacement-project')
            case.project.rename(original_project)
            shutil.copytree(original_project, case.project)
            before_intent = (case.state / active.INTENT).read_bytes()
            try:
                with self.assertRaises(J.JournalEvidenceError):
                    run(next_args)
                self.assertEqual((case.state / active.INTENT).read_bytes(), before_intent,
                                 'a replaced project must refuse before the first intent write')
            finally:
                case.project.rename(replacement_project)
                original_project.rename(case.project)
            original_during_cas = case.project.with_name('original-during-cas')
            replacement_during_cas = case.project.with_name('replacement-during-cas')
            replaced = []
            def replace_after_cas(name):
                if name == 'cas':
                    case.project.rename(original_during_cas)
                    shutil.copytree(original_during_cas, case.project)
                    replaced.append(True)
            before_route = case.route.read_bytes()
            try:
                with mock.patch.object(active, '_recovery_step', replace_after_cas):
                    with self.assertRaises(Exception):
                        run(next_args)
                self.assertEqual(replaced, [True])
                self.assertEqual(case.route.read_bytes(), before_route,
                                 'a replacement after CAS must not receive route writes')
            finally:
                if replaced:
                    case.project.rename(replacement_during_cas)
                    original_during_cas.rename(case.project)
            # Changing settings after the next retarget CAS must refuse before
            # any metadata write, even though project inode/scope stay fixed.
            next_args = target(case, 4)
            settings_before = case.settings.read_bytes()
            before_route = case.route.read_bytes()
            before_metadata = [path.read_bytes() for path in active._metadata_paths(legacy)]
            changed = []
            def change_settings_after_cas(name):
                if name == 'cas':
                    case.settings.write_text('{"name":"other","skipPaths":[]}')
                    changed.append(True)
            try:
                with mock.patch.object(active, '_recovery_step', change_settings_after_cas):
                    with self.assertRaisesRegex(Exception, 'active_upgrade_settings_changed'):
                        run(next_args)
                self.assertEqual(changed, [True])
                self.assertEqual(case.route.read_bytes(), before_route)
                self.assertEqual([path.read_bytes() for path in active._metadata_paths(legacy)],
                                 before_metadata)
                self.assertEqual(REAL_INSPECT(case.journal_path)['state'], 'pending')
                self.assertEqual(case.row()['phase'], 'upgrading')
            finally:
                case.settings.write_bytes(settings_before)
            self.assertEqual(run(next_args)['status'], 'active_upgrade_complete')
            final = REAL_INSPECT(case.journal_path)
            self.assertEqual(final['selected']['pluginRoot'], next_args[0])
            self.assertFalse(final['newWriteSeen'])
            self.assertEqual(case.row()['phase'], 'active')

    def test_worker_does_not_return_current_before_fenced_recovery(self):
        import laya_bundle_upgrade as auto
        with fixture() as case:
            fence(case)
            case.bundles[case.old_root] = proof(case.old_root, '3' * 40)
            case.review['reviewedProvider']['providerManifest'] = manifest(case.old_root)
            with case.patches(active), \
                    mock.patch.object(auto, 'installed_commit', return_value='3' * 40), \
                    mock.patch.object(auto, 'select_source', return_value=(case.home, '3' * 40)), \
                    mock.patch.object(auto, '_not_older'), \
                    mock.patch.object(auto, '_require_pinned_runtime'), \
                    mock.patch.object(auto, '_ensure_embed_runtime',
                                      return_value={'generation': str(case.home / 'synthetic-runtime')}), \
                    mock.patch.object(BASE['product'], '_request_build_wake',
                                      return_value={'status': 'fixture'}):
                result = auto.upgrade_install(case.project, case.state,
                                              Path(case.old_root), home=case.home)
            self.assertEqual(result['status'], 'active_upgrade_complete')
            self.assertEqual(case.fj.selected['sourceCommit'], '3' * 40)
            self.assertEqual(case.row()['phase'], 'active')


if __name__ == '__main__':
    unittest.main()

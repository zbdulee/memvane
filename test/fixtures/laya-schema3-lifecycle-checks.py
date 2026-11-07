#!/usr/bin/env python3
"""U14b: install lifecycle paths on a schema3-selected (switched) v5 install.

Builds on the U10 fixture: the v5 install, v6 index, schema3 view store,
journal images, state directory rename and Git checkout are actual.
Synthetic authority is the same as the existing reader/upgrade fixtures:
bundle inspection, host observation, provider review/attestation records and
the U5 parity receipt resolver. No actual host, provider or operational
setup entry point runs.
"""
from contextlib import ExitStack, closing, contextmanager
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('schema3_lifecycle_base', HERE / 'laya-journal-schema3-checks.py')
schema3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(schema3)
J = schema3.J
CTX = schema3.CTX
EXIT_AT_CHECKPOINT = schema3.EXIT_AT_CHECKPOINT
thaw, emit = schema3.thaw, schema3.emit


class Unwound(BaseException):
    """Controlled unwinding at a mover phase; not a process crash claim."""


class Lifecycle(schema3.Schema3EndpointChecks):
    def stored(self, endpoint_id):
        with closing(sqlite3.connect(self.case.journal)) as db:
            row = db.execute('SELECT record FROM read_endpoints WHERE endpoint_id=?', (endpoint_id,)).fetchone()
        return json.loads(row[0])

    def recall(self, host_root=None, *, authority=True):
        """Resolve the persisted preferred endpoint and query it once."""
        with ExitStack() as stack:
            if authority:
                stack.enter_context(self.seed.authority())
            stack.enter_context(J.read_only_journals())
            endpoint = stack.enter_context(self.lease.resolve_endpoint(
                self.case.project, action='recall', host_root=host_root or self.case.plugin))
            result = self.query(endpoint)
            return endpoint.endpoint_id, thaw(endpoint.record), result


# Reuse the U10 setup and helpers, not its scenarios.
for _name in [n for n in dir(schema3.Schema3EndpointChecks) if n.startswith('test_')]:
    setattr(Lifecycle, _name, None)


class ActiveUpgradeChecks(Lifecycle):
    @contextmanager
    def upgrade_authority(self, new):
        case, reader = self.case, self.reader
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
                          mock.patch.object(reader.journal, '_inspect_identity',
                                            side_effect=lambda identity: lookup(identity['pluginRoot'])),
                          mock.patch.object(reader.journal, '_bind_attestation', side_effect=lambda path, identity, proof: (
                              hashlib.sha256(J._canon(new_att if proof == target else old_att).encode()).hexdigest(),
                              identify(new_att if proof == target else old_att)))):
                stack.enter_context(patch)
            yield upgrade, target, review, new_att

    def test_01_active_upgrade_fence_activation_and_restore_keep_schema3_reader(self):
        _initial, switched_target, _selected = self.switched()
        expected = switched_target.endpoint_id
        case = self.case
        new = case.home / 'new-plugin'
        new.mkdir()
        observed = []
        with self.upgrade_authority(new) as (upgrade, target, review, new_att):
            intent = upgrade._prepare(case.project, case.state, case.journal, case.home, new,
                                      target['sourceCommit'], review['proposalSha256'], '/synthetic/model', 3)
            fresh = importlib.import_module('laya_fresh_wiki_origin')
            actual_change = fresh._change

            def change(*args, **kwargs):
                result = actual_change(*args, **kwargs)
                if result['phase'] == 'upgrading':
                    for host_root in (case.plugin, new):
                        endpoint_id, record, rows = self.recall(host_root, authority=False)
                        observed.append(('fence', str(host_root) == str(new), endpoint_id,
                                         record['store_format'], rows.store_format,
                                         'concepts/heron-route.md' in [r['path'] for r in rows.rows]))
                return result
            with mock.patch.object(fresh, '_change', side_effect=change):
                self.assertEqual(upgrade._switch(intent)['status'], 'active_upgrade_complete')
            with mock.patch.object(self.reader.journal, '_verified_executing_root', return_value=str(new)), \
                    mock.patch.object(self.reader.journal, '_provider_file_identity',
                                      return_value=[[str(new / 'p.py'), 1, 2, 3, 4, 5]]):
                endpoint_id, record, rows = self.recall(new, authority=False)
                observed.append(('activated', True, endpoint_id, record['store_format'], rows.store_format,
                                 'concepts/heron-route.md' in [r['path'] for r in rows.rows]))
            self.assertEqual(upgrade._restore(intent)['status'], 'active_upgrade_restored_laya')
        endpoint_id, record, rows = self.recall()
        observed.append(('restored', False, endpoint_id, record['store_format'], rows.store_format,
                         'concepts/heron-route.md' in [r['path'] for r in rows.rows]))
        emit('schema3-active-upgrade', observed=observed)
        self.assertEqual([o[0] for o in observed], ['fence', 'fence', 'activated', 'restored'])
        for stage in observed:
            self.assertEqual(stage[2:], (expected, 'schema3', 'schema3', True), stage)
        self.assertEqual(self.reader.read_selection(case.journal).preferred_endpoint, expected)

    def test_02_companion_seal_changes_no_journal_and_companion_recall_reads_schema3(self):
        self.switched()
        case = self.case
        bundle = importlib.import_module('laya_bundle_upgrade')
        product = importlib.import_module('laya_product_bundle')
        expected = self.reader.read_selection(case.journal).preferred_endpoint
        kind, identity = bundle.classify(case.state)
        # Bound upgrades take only a candidate_ready journal without a selection.
        self.assertEqual(kind, 'active')
        companion = case.home / 'companion-plugin'
        companion.mkdir()
        before = case.journal.read_bytes()
        sealed = []
        with self.seed.authority(), \
                mock.patch.object(product, 'seal_companion_host_copy',
                                  side_effect=lambda owner, root: sealed.append((owner, str(root)))):
            self.assertEqual(bundle._seal_companion(identity, companion), {'status': 'companion_sealed'})
        self.assertEqual(sealed, [(str(case.plugin), str(companion))])
        self.assertEqual(case.journal.read_bytes(), before)
        with self.seed.authority(), \
                mock.patch.object(J, '_verified_executing_root', return_value=str(companion)):
            endpoint_id, record, rows = self.recall(companion, authority=False)
        emit('schema3-companion', kind=kind, endpoint=endpoint_id, store_format=rows.store_format)
        self.assertEqual((endpoint_id, record['store_format'], rows.store_format), (expected, 'schema3', 'schema3'))
        self.assertIn('concepts/heron-route.md', [r['path'] for r in rows.rows])


class StateMoveChecks(Lifecycle):
    def setUp(self):
        # The pre-rename state base: the only root a state move starts from.
        seed = type('LegacyStateSeed', (schema3.seed_module.MoveSealChecks,),
                    {'STATE_PARTS': ('.local', 'state', 'laya', '3' * 32)})
        with mock.patch.object(schema3.seed_module, 'MoveSealChecks', seed):
            super().setUp()
        build = importlib.import_module('laya_initial_build')
        self.assertIn('/.local/state/laya/', str(self.case.state))
        self.assertTrue(Path(build.register_project(build.read(self.case.state)['identity'])).is_file())
        self.state_mover = importlib.import_module('laya_state_move')

    def move_authority(self):
        return self.seed.helper.full_authority(self.case, self.reader, self.bridge, self.lease,
                                               self.seed.mover, self.state_mover)

    def moved_case(self, target):
        """Point the fixture case at the moved state root (paths only)."""
        case = self.case
        old = case.state
        for name in ('state', 'journal', 'db', 'catalog', 'store'):
            value = getattr(case, name, None)
            if isinstance(value, Path) and (value == old or old in value.parents):
                setattr(case, name, target / value.relative_to(old))
        self.store_path = target / self.store_path.name

    def test_01_state_move_reseals_schema3_endpoint_and_rollback_still_selects_v6(self):
        initial, switched_target, selected = self.switched()
        old = thaw(switched_target.record)
        case = self.case
        target = self.state_mover.target_root(case.state, case.home)

        def unwind(name):
            if name == 'journal':
                raise Unwound()
        with self.move_authority():
            with self.assertRaises(Unwound):
                self.state_mover.move(case.project, case.state, home=case.home, step=unwind)
            chosen, result = self.state_mover.move(case.project, target, home=case.home)
        self.assertEqual((chosen, result['status']), (target, 'state_move_complete'))
        self.moved_case(target)
        current = self.reader.read_selection(case.journal)
        moved = self.stored(current.preferred_endpoint)
        emit('schema3-state-move', selection=list(self.selection()), state=moved['state'], db=moved['db_path'])
        self.assertEqual((current.epoch, current.previous_endpoint), (selected.epoch + 1, switched_target.endpoint_id))
        self.assertEqual({k for k in old if old[k] != moved[k]}, {'state', 'db_path', 'attestation_sha256'})
        self.assertEqual((moved['state'], moved['db_path']), (str(target), str(target / self.store_path.name)))
        self.assertTrue(self.reader.relocated_pair(old, moved))
        for field, value in (('incarnation', '0' * 32), ('state_node', [0, 0]), ('creation_epoch', 99),
                             ('db_path', str(target / 'other.sqlite')), ('roots', ['other'])):
            with self.subTest(forged=field):
                self.assertFalse(self.reader.relocated_pair(old, dict(moved, **{field: value})))
        endpoint_id, record, rows = self.recall()
        self.assertEqual((endpoint_id, record, rows.store_format), (current.preferred_endpoint, moved, 'schema3'))
        self.assertEqual(rows.data_generation, self.gens[-1])
        self.assertIn('concepts/heron-route.md', [r['path'] for r in rows.rows])
        # Rollback (U16) from the re-sealed endpoint keeps the original v6 sidecar baseline.
        original = self.stored(initial.preferred_endpoint)
        with self.seed.authority():
            installer = self.reader.seal_current_reader(case.state, case.project, executing_root=case.plugin)
            back = self.reader.cas_reader(case.journal, expected_epoch=current.epoch,
                                          expected_endpoint=current.preferred_endpoint,
                                          target=installer.endpoint, transition_id='2' * 32)
        self.assertEqual(dict(installer.endpoint.record).get('legacy_sidecar_baseline'),
                         original.get('legacy_sidecar_baseline'))
        self.assertEqual(installer.endpoint.record['state'], str(target))
        with self.resolved() as endpoint:
            self.assertEqual((endpoint.endpoint_id, endpoint.record['store_format']),
                             (back.preferred_endpoint, 'v6'))

    def test_02_process_crash_inside_relocation_cas_resumes_to_one_reseal(self):
        _initial, switched_target, selected = self.switched()
        case = self.case
        target = self.state_mover.target_root(case.state, case.home)
        parent, remote = CTX.Pipe()

        def run():
            parent.close()

            def checkpoint(name):
                if name == 'v5-image-committed':
                    remote.send(name)
                    os._exit(EXIT_AT_CHECKPOINT)
            try:
                with self.move_authority(), mock.patch.object(J, '_conversion_step', checkpoint):
                    self.state_mover.move(case.project, case.state, home=case.home)
                remote.send('completed-without-checkpoint')
            except BaseException as exc:
                remote.send('child-error:%s:%s' % (type(exc).__name__, exc))
            finally:
                os._exit(3)
        process = CTX.Process(target=run)
        process.start()
        remote.close()
        try:
            reached = parent.recv() if parent.poll(120) else 'timeout'
        except EOFError:
            reached = 'eof'
        process.join(30)
        self.assertEqual(reached, 'v5-image-committed', 'child exitcode=%r' % (process.exitcode,))
        self.assertEqual(process.exitcode, EXIT_AT_CHECKPOINT)
        self.assertFalse(case.state.exists())
        crashed = self.reader.read_selection(target / 'install-journal.sqlite')
        self.assertEqual((crashed.epoch, crashed.preferred_endpoint), (selected.epoch, switched_target.endpoint_id))
        with self.move_authority():
            chosen, result = self.state_mover.move(case.project, target, home=case.home)
        self.assertEqual((chosen, result['status']), (target, 'state_move_complete'))
        self.moved_case(target)
        self.assertFalse((target / '.reader-next.sqlite').exists())
        current = self.reader.read_selection(case.journal)
        with closing(sqlite3.connect(case.journal)) as db:
            transitions = db.execute('SELECT epoch FROM reader_transitions ORDER BY epoch').fetchall()
        emit('schema3-state-move-crash', crashedEpoch=crashed.epoch, resumed=list(self.selection()),
             transitions=len(transitions))
        self.assertEqual((current.epoch, current.previous_endpoint), (selected.epoch + 1, switched_target.endpoint_id))
        self.assertEqual(len(transitions), selected.epoch + 1)
        endpoint_id, record, rows = self.recall()
        self.assertEqual((endpoint_id, record['state'], rows.store_format),
                         (current.preferred_endpoint, str(target), 'schema3'))


if __name__ == '__main__':
    unittest.main()

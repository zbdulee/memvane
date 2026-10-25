#!/usr/bin/env python3
"""QMD drift after a v3 bind: retire the stale binding, rebuild as a fresh install.

Synthetic only: temp HOME and stores, the fake provider of
``laya-initial-build-checks.py``, a synthetic QMD 2.5.3 file. Faked: the
installer journals (one per state root, answering inspect, approval and CAS
in memory), the provider/bundle preflight, and the origin plan (a fixed
proposal hash that only models the project-marker refusal). Real modules
write the ledgers, markers, retirement records, pointers, routes,
candidates, catalogs, binding stores, claim queues and QMD backups to temp
files. A passing two-install run here is not installed-host proof, and it
says nothing about how the real plan hash reacts to QMD changes (the
background plan deliberately excludes the volatile QMD digest).
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import runpy
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / 'core'))
BOUND = runpy.run_path(str(HERE / 'laya-qmd-cutover-bound-checks.py'))

import laya_initial_build as initial_build  # noqa: E402
import laya_qmd_cutover as cutover  # noqa: E402
import laya_product_install as product  # noqa: E402
import laya_product_sync_worker as worker  # noqa: E402
from laya_index_a import LayaIndexA  # noqa: E402
from laya_qmd_migration import QmdMigrationBlocked  # noqa: E402

Kill, FakeJournal = BOUND['Kill'], BOUND['FakeJournal']
SCOPE = BOUND['SCOPE']
SECOND = '5' * 32


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


class RebuildCase(BOUND['BoundCase']):
    def setUp(self):
        super().setUp()
        self.journals = {str(self.journal_path): self.fj}

        def pick(path):
            return self.journals[str(path)]

        def inspect(path=None):
            # The real journal also reports its project and scope; ``tampered``
            # overrides model a journal changed behind the install's back.
            journal = pick(path)
            answer = {'projectRoot': str(self.project), 'scopeId': SCOPE,
                      **journal.inspect(path)}
            answer.update(getattr(journal, 'tampered', {}))
            return answer

        def approval(path=None, **kwargs):
            return pick(path).approval(path, **kwargs)

        def activate(path, epoch, route_bytes):
            return pick(path).activate(path, epoch, route_bytes)

        def restore(path, epoch, route_sha):
            return pick(path).restore(path, epoch, route_sha)

        def plan(project_root, **kw):
            # As the real plan: a project marker refuses a new review.
            self.plans.append(kw)
            if self.marker.exists() and not kw.get('_bound_marker'):
                raise product.InstallBlocked('project_selection_already_exists')
            return {'proposalSha256': 'd' * 64}
        for target, name, value in (
                (worker, 'inspect_journal', inspect),
                (worker, 'preflight_product_candidate', approval),
                (cutover, 'inspect_journal', inspect),
                (cutover, 'preflight_product_candidate', approval),
                (cutover, 'cas_activate', activate),
                (cutover, 'cas_restore_qmd', restore),
                (product, 'preflight_product_candidate', approval),
                (product, 'inspect_journal', inspect),
                (product, 'plan_qmd_origin', plan)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch('laya_install_journal.inspect_journal', inspect)
        patcher.start()
        self.addCleanup(patcher.stop)

    def identity(self, **changes):
        return super().identity(**{'installId': self.state.name, **changes})

    def refused(self, reason):
        with self.assertRaisesRegex((cutover.CutoverBlocked, QmdMigrationBlocked), reason):
            self.activate()
        self.assert_prepared_unselected()
        self.assertEqual(self.fj.calls, [])

    # -- the stale install and its retirement ----------------------------------

    def drifted(self):
        """A bound, prepared install whose QMD then indexed one more file."""
        self.bound()
        self.first = {'state': self.state, 'journal': self.journal_path,
                      'ledger': self.ledger, 'route': self.route_bytes, 'fj': self.fj,
                      'row': self.row(), 'record': self.record(),
                      'marker': self.marker.read_bytes()}
        self.first['backup'] = Path(self.first['row']['backupDb']).read_bytes()
        self.qmd_indexes('late.md', '# Late\nindexed after the bind\n')
        self.assertNotEqual(self.qmd_digest(), self.first['row']['sourceDigest'])
        return self.first

    def retire(self):
        return product.retire_drifted_origin(project_root=self.project,
                                             state_root=self.first['state'])

    def pointer(self):
        return initial_build.pointer_path(
            self.project, self.home / '.local' / 'state' / 'laya' / initial_build.POINTERS)

    def old_ledger(self):
        return cutover._ledger(self.first['ledger'])

    def old_head(self):
        with closing(LayaIndexA(self.first['state'] / product.BUILD_CANDIDATE)) as index:
            row = index.db.execute('SELECT gen FROM head WHERE scope=?', (SCOPE,)).fetchone()
            return row[0] if row else None

    def assert_retired(self):
        """Retired, unselected, and nothing of the stale install deleted."""
        first = self.first
        row = self.old_ledger()
        self.assertEqual(row['phase'], cutover.RETIRED)
        self.assertEqual({k: v for k, v in row.items()
                          if k not in ('epoch', 'phase',
                                       'retirementObservedSourceDigest')},
                         {k: v for k, v in first['row'].items()
                          if k not in ('epoch', 'phase')})
        retirement = cutover._read_json(first['state'] / cutover.RETIREMENT)
        self.assertEqual(retirement['marker'], json.loads(first['marker']))
        self.assertEqual(retirement['marker'], cutover._marker(first['row']))
        self.assertEqual(retirement['boundSourceDigest'], first['row']['sourceDigest'])
        self.assertNotEqual(retirement['observedSourceDigest'], first['row']['sourceDigest'])
        self.assertEqual(row['retirementObservedSourceDigest'],
                         retirement['observedSourceDigest'])
        self.assertEqual(Path(first['row']['backupDb']).read_bytes(), first['backup'])
        record = initial_build.read(first['state'])
        self.assertEqual((record['phase'], record['sealed']),
                         ('bound', first['record']['sealed']))
        self.assertEqual(self.old_head(), first['row']['indexGeneration'])
        for name in (product.BUILD_CANDIDATE, product.BUILD_QUEUE,
                     product.BUILD_CANDIDATE + '.catalog.sqlite',
                     product.BUILD_CANDIDATE + '.bindings.sqlite',
                     'reviewed-route.json', initial_build.RETIRED_POINTER):
            self.assertTrue((first['state'] / name).is_file(), name)
        self.assertEqual(first['fj'].state, 'candidate_ready')
        self.assertEqual(first['fj'].calls, [])
        self.assertFalse(self.route.exists())
        self.assertTrue(self.qmd.is_file())

    def assert_retired_kept_after_rebuild(self):
        first = self.first
        self.assertEqual(self.old_ledger()['phase'], cutover.RETIRED)
        self.assertEqual(Path(first['row']['backupDb']).read_bytes(), first['backup'])
        self.assertEqual(self.old_head(), first['row']['indexGeneration'])
        self.assertEqual((first['fj'].state, first['fj'].calls), ('candidate_ready', []))

    # -- the fresh install -------------------------------------------------------

    def second_install(self):
        """A new install id, state root, journal, attestation and candidate."""
        cursor = self.home / '.local' / 'state' / 'laya' / SECOND
        cursor.mkdir(mode=0o700)
        self.state, self.journal_path = cursor, cursor / 'install-journal.sqlite'
        self.ledger = cursor / cutover.LEDGER
        self.fj = FakeJournal()
        self.fj.attestation, self.fj.file_identity = 'c' * 64, '2' * 64
        self.journals[str(self.journal_path)] = self.fj
        identity = self.register()
        self.assertEqual(self.until_settled()[-1]['initialBuild'], 'sealed')
        self.assertEqual(self.finalize()['status'], 'freshness_pending')
        self.assertEqual(self.until_settled()[-1]['initialBuild'], 'sealed')
        result = self.finalize()
        self.assertEqual(result['status'], 'cutover_prepared', result)
        return identity, result


class RetireAndRebuildFlowTests(RebuildCase):
    def test_drift_retire_then_a_fresh_install_binds_and_activates(self):
        first = self.drifted()
        self.refused(cutover.SOURCE_DRIFT)
        qmd_before = _sha(self.qmd.read_bytes())
        result = self.retire()
        self.assertEqual((result['status'], result['cutoverPhase'], result['buildPointer']),
                         ('origin_binding_retired', cutover.RETIRED, 'retired'))
        self.assertEqual(result['nextRequires'], 'new_install_id_and_state_root')
        self.assertEqual(_sha(self.qmd.read_bytes()), qmd_before, 'QMD is read-only')
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.pointer().exists())
        self.assertEqual(cutover._read_json(first['state'] / initial_build.RETIRED_POINTER)
                         ['stateRoot'], str(first['state']))
        self.assert_retired()
        # The old build never steps again and the project selects plain QMD.
        calls = list(BOUND['BUILD']['FakeProvider'].calls)
        self.assertEqual(worker.run_initial_build(self.project, initial_build.read(first['state']),
                                                  coordinator=self.coordinator, report={},
                                                  start_service=self.start)['reason'],
                         'initial_build_bound')
        self.assertEqual(BOUND['BUILD']['FakeProvider'].calls, calls)
        self.assertTrue(worker._still_unselected(self.project))

        identity, bound = self.second_install()
        row = self.row()
        self.assertEqual(row['sourceDigest'], self.qmd_digest(), 'bound to the new digest')
        self.assertNotEqual(row['candidateIncarnation'], first['row']['candidateIncarnation'])
        self.assertNotEqual(row['routeSha256'], first['row']['routeSha256'])
        self.assertEqual(row['attestationSha256'], 'c' * 64)
        self.assertEqual(Path(bound['sourceBackup']).parent, self.state)
        self.assertEqual(cutover._read_json(self.marker), cutover._marker(row))
        self.assertEqual(self.selection(), 'legacy', 'QMD stays selected until activation')
        self.assertEqual(self.fj.calls, [])
        with mock.patch.object(product, '_request_build_wake',
                               return_value={'status': 'queued'}):
            active = product.activate_origin_selection(self.project, self.journal_path,
                                                       self.route_bytes)
        self.assertEqual((active['phase'], active['qmdChangedAfterActivation']),
                         ('active', False))
        self.assertEqual(self.selection(), 'candidate')
        self.assertEqual(self.route.read_bytes(), self.route_bytes)
        self.assert_retired_kept_after_rebuild()

    def test_the_stale_binding_and_its_route_stay_denied(self):
        first = self.drifted()
        self.retire()
        self.second_install()
        row = first['row']
        with self.assertRaisesRegex(cutover.CutoverBlocked, 'cutover_binding_retired'):
            cutover.activate_qmd_origin(self.project, first['journal'], first['route'])
        with self.assertRaisesRegex(product.InstallBlocked, 'cutover_binding_retired'):
            product.activate_origin_selection(self.project, first['journal'], first['route'])
        kwargs = self.bind_kwargs(row, journal_path=first['journal'],
                                  route_bytes=first['route'],
                                  source_digest=self.qmd_digest())
        with self.assertRaisesRegex(cutover.CutoverBlocked, 'cutover_binding_retired'):
            cutover.bind_qmd_origin_bound(**kwargs)
        marker = self.marker.read_bytes()
        self.assertEqual(cutover.recover_qmd_origin(self.project, first['journal'],
                                                    first['route'])['phase'],
                         cutover.RETIRED)
        self.assertEqual(self.marker.read_bytes(), marker, 'the new marker is untouched')
        restored = product.restore_origin_selection(project_root=self.project,
                                                    state_root=first['state'])
        self.assertEqual((restored['status'], restored['changesApplied']),
                         ('binding_retired', 0))
        finalized = product.finalize_initial_build(project_root=self.project,
                                                   state_root=first['state'])
        self.assertEqual(finalized['status'], 'cutover_retired')
        self.assertFalse(self.route.exists())
        self.assertEqual(self.selection(), 'legacy')
        self.assertEqual(first['fj'].calls, [])
        self.assert_retired_kept_after_rebuild()

    def test_retirement_is_idempotent_and_never_touches_a_later_install(self):
        first = self.drifted()
        once = self.retire()
        retirement = cutover._read_json(first['state'] / cutover.RETIREMENT)
        self.qmd_indexes('later.md', '# Later\nanother index after retirement\n')
        again = self.retire()
        self.assertEqual((again['observedSourceDigest'], again['buildPointer']),
                         (once['observedSourceDigest'], 'absent'))
        self.assertEqual(cutover._read_json(first['state'] / cutover.RETIREMENT), retirement)
        self.assertEqual(self.old_ledger()['epoch'], first['row']['epoch'] + 1)
        self.second_install()
        marker, pointer = self.marker.read_bytes(), self.pointer().read_bytes()
        third = self.retire()
        self.assertEqual(third['buildPointer'], 'other')
        self.assertEqual((self.marker.read_bytes(), self.pointer().read_bytes()),
                         (marker, pointer))
        self.assertEqual(self.row()['phase'], 'prepared')


class RetirementRefusalTests(RebuildCase):
    def test_no_drift_or_a_moved_selection_refuses_and_changes_nothing(self):
        self.bound()
        row, marker = self.row(), self.marker.read_bytes()
        self.first = {'state': self.state}
        with self.assertRaisesRegex(cutover.CutoverBlocked, 'cutover_source_not_drifted'):
            self.retire()
        self.assertEqual((self.row(), self.marker.read_bytes()), (row, marker))
        self.assertTrue(self.pointer().exists())
        self.assertFalse((self.state / cutover.RETIREMENT).exists())
        self.activate()
        self.qmd_indexes('late.md', '# Late\nafter activation\n')
        with self.assertRaisesRegex(product.InstallBlocked, 'cutover_not_prepared'):
            self.retire()
        self.assertEqual((self.row()['phase'], self.selection()), ('active', 'candidate'))

    def test_a_fresh_install_is_refused_until_the_stale_one_is_retired(self):
        self.drifted()
        with self.assertRaisesRegex(product.InstallBlocked, 'project_selection_already_exists'):
            product.plan_qmd_origin(self.project, background=True)
        other = self.home / 'elsewhere'
        identity = {**initial_build.read(self.state)['identity'],
                    'stateRoot': str(other), 'installId': SECOND}
        for key in ('candidateDb', 'queueDb', 'catalogDb', 'bindingStore', 'journalPath'):
            identity[key] = str(other / Path(identity[key]).name)
        with self.assertRaisesRegex(Exception, 'initial_build_already_registered'):
            initial_build.register_project(identity)
        self.retire()
        self.assertEqual(product.plan_qmd_origin(self.project, background=True)
                         ['proposalSha256'], 'd' * 64)

    def test_a_changed_plan_or_provider_does_not_strand_withdrawal(self):
        first = self.drifted()
        first['fj'].attestation, first['fj'].file_identity = 'f' * 64, '9' * 64

        def refuse(*_args, **_kwargs):
            raise product.InstallBlocked('must_not_be_consulted')
        with mock.patch.object(product, 'plan_qmd_origin', refuse), \
                mock.patch.object(product, 'preflight_product_candidate', refuse):
            result = self.retire()
        self.assertEqual((result['status'], result['buildPointer']),
                         ('origin_binding_retired', 'retired'))
        self.assertFalse(self.marker.exists())
        self.assert_retired()

    def test_a_tampered_journal_refuses_retirement_and_changes_nothing(self):
        first = self.drifted()
        pinned = first['row']
        for tampered in ({'projectRoot': str(self.home / 'elsewhere')},
                         {'scopeId': '6' * 32},
                         {'pendingRouteSha256': '7' * 64},
                         {'newWriteSeen': True},
                         {'state': 'pending'},
                         {'pending': None},
                         {'providerAttestationSha256': pinned['attestationSha256']},
                         {'providerAttestationSha256': 'e' * 64},
                         {'providerFileIdentitySha256': pinned['providerFileIdentitySha256']},
                         {'providerFileIdentitySha256': 'f' * 64}):
            first['fj'].tampered = tampered
            with self.subTest(tampered=tampered):
                with self.assertRaisesRegex(product.InstallBlocked,
                                            'initial_build_journal_changed'):
                    self.retire()
                self.assertEqual(self.old_ledger(), pinned)
                self.assertEqual(self.marker.read_bytes(), first['marker'])
                self.assertTrue(self.pointer().exists())
                self.assertFalse((first['state'] / initial_build.RETIRED_POINTER).exists())
        first['fj'].tampered = {}
        self.assertEqual(self.retire()['status'], 'origin_binding_retired')
        self.assert_retired()


class RetirementCrashTests(RebuildCase):
    def test_crash_after_bind_ledger_before_marker_keeps_sealed_build_still(self):
        self.corpus(3)
        self.write('wiki/w0.md', '# W\nwiki\n')
        self.qmd_source()
        self.register()
        self.until_settled()
        self.assertEqual(self.finalize()['status'], 'freshness_pending')
        self.until_settled()
        real = cutover._write

        def write(path, value, *, replace):
            real(path, value, replace=replace)
            if Path(path) == self.ledger and not replace:
                raise Kill()
        with mock.patch.object(cutover, '_write', write):
            with self.assertRaises(Kill):
                self.finalize()
        self.assertEqual(self.record()['phase'], 'sealed')
        self.assertTrue(self.ledger.exists())
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.selection(), 'blocked')
        result = worker.run_initial_build(self.project, self.record(),
                                          coordinator=self.coordinator, report={},
                                          start_service=self.start)
        self.assertEqual(result['reason'], 'initial_build_bind_unsettled')
        self.assertEqual(self.record()['phase'], 'sealed')
        self.assertEqual(self.finalize()['status'], 'cutover_prepared')
        self.assertEqual(self.record()['phase'], 'bound')

    def resumed(self):
        result = self.retire()
        self.assertEqual(result['status'], 'origin_binding_retired')
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.pointer().exists())
        self.assertFalse(any(self.pointer().parent.glob('.retiring-*')))
        self.assert_retired()
        self.second_install()
        self.assertEqual(self.row()['sourceDigest'], self.qmd_digest())

    def test_crash_before_the_ledger_retires(self):
        first = self.drifted()
        real = cutover._change

        def change(path, epoch, expected, following, updates=None):
            if following == cutover.RETIRED:
                raise Kill()
            return real(path, epoch, expected, following, updates)
        with mock.patch.object(cutover, '_change', change):
            with self.assertRaises(Kill):
                self.retire()
        self.assertEqual(self.old_ledger(), first['row'])
        self.assertEqual(self.selection(), 'legacy')
        self.resumed()

    def test_crash_after_the_ledger_retires_before_its_record(self):
        first = self.drifted()
        real = cutover._write

        def write(path, value, *, replace):
            if Path(path).name == cutover.RETIREMENT:
                raise Kill()
            return real(path, value, replace=replace)
        with mock.patch.object(cutover, '_write', write):
            with self.assertRaises(Kill):
                self.retire()
        self.assertEqual(self.old_ledger()['phase'], cutover.RETIRED)
        self.assertEqual(self.marker.read_bytes(), first['marker'])
        self.assertEqual(self.selection(), 'legacy', 'QMD is still selected')
        with self.assertRaisesRegex(cutover.CutoverBlocked, 'cutover_binding_retired'):
            self.activate()
        self.assertEqual(self.recover()['phase'], cutover.RETIRED)
        self.assertEqual(worker.run_initial_build(self.project, self.record(),
                                                  coordinator=self.coordinator, report={},
                                                  start_service=self.start)['reason'],
                         'initial_build_bound')
        self.resumed()

    def test_crash_after_the_record_before_the_marker_withdraws(self):
        self.drifted()
        real = cutover._write

        def write(path, value, *, replace):
            real(path, value, replace=replace)
            if Path(path).name == cutover.RETIREMENT:
                raise Kill()
        with mock.patch.object(cutover, '_write', write):
            with self.assertRaises(Kill):
                self.retire()
        self.assertTrue(self.marker.exists())
        self.assertEqual(self.selection(), 'legacy')
        self.resumed()

    def test_crash_while_releasing_the_pointer(self):
        self.drifted()
        real = initial_build._fsync_dir
        calls = {'n': 0}

        def fsync(path):
            if Path(path) == self.pointer().parent:
                calls['n'] += 1
                if calls['n'] == 1:
                    raise Kill()
            return real(path)
        with mock.patch.object(initial_build, '_fsync_dir', fsync):
            with self.assertRaises(Kill):
                self.retire()
        self.assertFalse(self.pointer().exists())
        self.assertTrue(any(self.pointer().parent.glob('.retiring-*')))
        self.resumed()

    def crash_releasing_the_pointer(self):
        real = initial_build._fsync_dir

        def fsync(path):
            if Path(path) == self.pointer().parent:
                raise Kill()
            return real(path)
        with mock.patch.object(initial_build, '_fsync_dir', fsync):
            with self.assertRaises(Kill):
                self.retire()

    def test_a_crashed_retirement_never_strands_a_later_install_retirement(self):
        first = self.drifted()
        self.crash_releasing_the_pointer()
        held = list(self.pointer().parent.glob('.retiring-*'))
        self.assertEqual(len(held), 1)
        a_held = held[0].read_bytes()
        self.assertEqual(json.loads(a_held),
                         cutover._read_json(first['state'] / initial_build.RETIRED_POINTER))
        # A new install registers at the freed pointer path, binds, then drifts.
        self.second_install()
        b_state, b_pointer = self.state, self.pointer().read_bytes()
        self.assertEqual(json.loads(b_pointer)['stateRoot'], str(b_state))
        self.qmd_indexes('second-late.md', '# Late\nindexed after the second bind\n')
        result = product.retire_drifted_origin(project_root=self.project, state_root=b_state)
        self.assertEqual((result['status'], result['buildPointer']),
                         ('origin_binding_retired', 'retired'))
        self.assertFalse(self.pointer().exists())
        self.assertEqual(cutover._read_json(b_state / initial_build.RETIRED_POINTER),
                         json.loads(b_pointer))
        # A's held pointer is A's alone: B neither removed nor replaced it.
        self.assertEqual([p.read_bytes() for p in self.pointer().parent.glob('.retiring-*')],
                         [a_held])
        # A's own repeat clears only its held file.
        self.assertEqual(self.retire()['buildPointer'], 'retired')
        self.assertFalse(any(self.pointer().parent.glob('.retiring-*')))
        self.assertFalse(self.pointer().exists())
        self.assertEqual(cutover._read_json(b_state / cutover.LEDGER)['phase'], cutover.RETIRED)
        self.assert_retired_kept_after_rebuild()

    def test_an_interrupted_retirement_names_only_its_own_pointer(self):
        first = self.drifted()
        self.assertIsNone(product.retired_build_pointer(self.project), 'prepared: no hint')
        with mock.patch.object(initial_build, 'retire_project_pointer', side_effect=Kill()):
            with self.assertRaises(Kill):
                self.retire()
        self.assertFalse(self.marker.exists())
        self.assertTrue(self.pointer().exists())
        self.assertEqual(product.retired_build_pointer(self.project),
                         {'nextAction': 'retire-index-origin', 'stateRoot': str(first['state']),
                          'reason': 'retired_build_pointer_registered'})
        self.retire()
        self.assertIsNone(product.retired_build_pointer(self.project))
        self.second_install()
        self.assertIsNone(product.retired_build_pointer(self.project),
                          'a prepared later install is never named')

    def test_an_interrupted_bind_is_recorded_bound_before_retiring(self):
        self.corpus(3)
        self.write('wiki/w0.md', '# W\nwiki\n')
        self.qmd_source()
        self.register()
        self.until_settled()
        self.assertEqual(self.finalize()['status'], 'freshness_pending')
        self.until_settled()
        real = initial_build.advance

        def advance(record, **changes):
            if changes.get('phase') == 'bound':
                raise Kill()
            return real(record, **changes)
        with mock.patch.object(initial_build, 'advance', advance):
            with self.assertRaises(Kill):
                self.finalize()
        self.assertEqual(self.record()['phase'], 'sealed')
        self.first = {'state': self.state, 'journal': self.journal_path,
                      'ledger': self.ledger, 'route': self.route_bytes, 'fj': self.fj,
                      'row': self.row(), 'marker': self.marker.read_bytes()}
        self.first['backup'] = Path(self.first['row']['backupDb']).read_bytes()
        self.qmd_indexes('late.md', '# Late\nindexed after the bind\n')
        self.retire()
        self.first['record'] = self.record()
        self.assertEqual(self.record()['phase'], 'bound')
        self.resumed()


class ActivationLateWriteTests(RebuildCase):
    def test_a_qmd_write_while_the_route_is_written_withdraws_it(self):
        self.bound()
        real = cutover._write

        def write(path, value, *, replace):
            real(path, value, replace=replace)
            if Path(path) == self.route:
                self.qmd_indexes('race.md', '# Race\nindexed while the route landed\n')
        with mock.patch.object(cutover, '_write', write):
            with self.assertRaisesRegex(cutover.CutoverBlocked, cutover.SOURCE_DRIFT):
                self.activate()
        self.assert_prepared_unselected()
        self.assertEqual(self.fj.calls, [])
        # The final fence keeps failing closed; retirement is the way on.
        self.refused(cutover.SOURCE_DRIFT)

    def test_a_qmd_write_after_the_journal_commit_is_reported_for_sync(self):
        self.bound()
        real = self.fj.activate

        def committed(path, epoch, route_bytes):
            answer = real(path, epoch, route_bytes)
            self.qmd_indexes('after.md', '# After\nindexed after the commit\n')
            return answer
        with mock.patch.object(self.fj, 'activate', committed), \
                mock.patch.object(product, '_request_build_wake',
                                  return_value={'status': 'queued'}):
            active = product.activate_origin_selection(self.project, self.journal_path,
                                                       self.route_bytes)
        self.assertEqual((active['phase'], active['qmdChangedAfterActivation'],
                          active['syncWake']), ('active', True, {'status': 'queued'}))
        self.assertEqual(self.selection(), 'candidate')


if __name__ == '__main__':
    unittest.main()

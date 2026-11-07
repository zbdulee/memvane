#!/usr/bin/env python3
"""U16 reader rollback: a switched (schema3-selected) install back to its v6 reader.

Builds on the U15 switch fixture (itself U11 -> U10): a real v5 install, v6
index, journal images, Git checkout, actual shadow migration driver, switch
CAS and main recall. Synthetic authority is the same as there (bundle
inspection, host observation). The v6 catch-up after the rollback publishes
with the fixture's v6 writer core (``sync_step`` with a fill provider); the
worker's provider-backed v6 path itself is not started. The candidate-loss
and moved-intent checks run the actual driver on the same install; the moved
intent is the byte-identical intent with the absolute paths a state move
leaves behind (synthetic, no actual state move).
"""
from contextlib import ExitStack, contextmanager
import importlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'core'))
spec = importlib.util.spec_from_file_location('rollback_switch_checks', HERE / 'laya-shadow-switch-checks.py')
switch_checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(switch_checks)
J = switch_checks.J

# One rollback in a separate process, optionally killed at a journal checkpoint.
CHILD = r'''
import importlib.util, json, os, signal, sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
root, payload = Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text(encoding='utf8'))
sys.path.insert(0, str(root / 'core'))
signal.alarm(120)
spec = importlib.util.spec_from_file_location('rollback_crash_authority', root / 'test/fixtures/laya-journal-reader-checks.py')
fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)
import laya_bridge_readiness as bridge, laya_install_journal as J, laya_journal_reader as reader
import laya_project_dir_move as mover, laya_reader_lease as lease, laya_shadow_migration as migration
helper = fixture.JournalReaderChecks('runTest')
case = SimpleNamespace(plugin=Path(payload['plugin']), proof=payload['proof'])
project, kill = Path(payload['project']), payload['kill']
real_step = J._conversion_step
def step(name):
    if kill == name:
        print('killed-at-' + name, flush=True)
        os.kill(os.getpid(), signal.SIGKILL)
    if real_step is not None:
        real_step(name)
def inspect(root):
    return {**case.proof, 'pluginRoot': str(root)}
with helper.full_authority(case, reader, bridge, lease, mover), \
        mock.patch.object(bridge, 'inspect_full_plugin_candidate', side_effect=inspect), \
        mock.patch.object(J, '_conversion_step', side_effect=step):
    print(json.dumps(migration.rollback_reader(project, executing_root=case.plugin)), flush=True)
raise SystemExit('checkpoint not reached')
'''


def emit(case, **evidence):
    print(json.dumps(dict(case=case, **evidence), sort_keys=True, default=str), flush=True)


class ShadowRollbackChecks(unittest.TestCase):
    def setUp(self):
        self.u15 = switch_checks.ShadowSwitchChecks('runTest')
        self.u15.setUp()
        self.addCleanup(self.u15.doCleanups)
        self.u11, self.u10, self.case = self.u15.u11, self.u15.u10, self.u15.case
        self.W, self.M, self.reader = self.u15.W, self.u15.M, self.u15.reader
        self.work = importlib.import_module('runtime_work')

    # ---- helpers -----------------------------------------------------------
    def switched(self):
        """Drive the actual driver to ``switched``; returns (v6 selection, switched selection)."""
        u15 = self.u15
        with u15.driver():
            u15.drive_to('shadow_verified')
            initial = u15.selection()
            u15.loaded()
            u15.run_pass()
            u15.run_pass()
        self.assertEqual(u15.intent()['migration_state'], 'switched')
        self.assertEqual(u15.store_format(), 'schema3')
        return initial, u15.selection()

    def rollback(self):
        wakes = []
        with self.u10.bridge_authority(), \
                mock.patch.object(self.work, 'request', side_effect=lambda *a, **k: wakes.append((a, k))):
            result = self.M.rollback_reader(self.case.project, executing_root=self.case.plugin)
        return result, wakes

    def setup(self, *argv):
        """The operator entry point (``core/setup_entry.py``); returns (exit code, JSON result)."""
        import io
        from contextlib import redirect_stdout
        entry = importlib.import_module('setup_entry')
        out = io.StringIO()
        with redirect_stdout(out):
            code = entry.main([argv[0], '--project', str(self.case.project), *argv[1:]])
        return code, json.loads(out.getvalue())

    def files(self):
        """Every regular file under the state root and the project's index folder (name -> size)."""
        found = {}
        for base in (self.case.state, self.case.auto):
            for path in base.rglob('*'):
                if path.is_file() and not path.name.startswith('.laya-cutover-'):
                    found[str(path)] = path.stat().st_size
        return found

    def crash(self, kill):
        case = self.case
        payload = case.home / 'rollback-crash.json'
        payload.write_text(json.dumps({'plugin': str(case.plugin), 'proof': case.proof,
                                       'project': str(case.project), 'kill': kill}), encoding='utf8')
        return subprocess.run([sys.executable, '-B', '-c', CHILD, str(ROOT), str(payload)],
                              capture_output=True, text=True, timeout=180, env=dict(os.environ))

    # ---- completion: rollback, driver hold, v6 writer and catch-up ----------
    def test_01_rollback_selects_v6_holds_the_driver_and_v6_catches_up(self):
        case, u15 = self.case, self.u15
        initial, switched = self.switched()
        schema3_gen, v6_gen = u15.schema3_generation(), self.u11.v6_generation()
        before_files = self.files()
        result, wakes = self.rollback()
        after = u15.selection()
        self.assertEqual(result['status'], 'rolled_back', result)
        self.assertEqual(after[0], switched[0] + 1)
        self.assertEqual(after[2], switched[1])
        self.assertEqual(after[3], result['transition_id'])
        self.assertEqual(result['endpoint_id'], after[1])
        self.assertEqual(u15.store_format(), 'v6')
        # The same bundle re-issues the same v6 endpoint the switch left.
        self.assertEqual(after[1], initial[1])
        self.assertEqual([a[:1] for a, _k in wakes], [('laya-sync',)])
        # Nothing was deleted: v6 DB, sidecar, candidate store, intent, activation.
        gone = sorted(set(before_files) - set(self.files()))
        self.assertEqual(gone, [])
        answer = u15.recall()
        self.assertIn('generation ' + v6_gen, answer['context'])
        self.assertNotIn('generation ' + schema3_gen, answer['context'])
        # The driver is held: no pass reads or writes the migration again.
        intent = (case.state / self.M._INTENT).read_bytes()
        with u15.driver():
            status = self.W.shadow_pass(case.project)
        self.assertEqual((status['state'], status['reason'], status['steps']),
                         ('skipped', 'migration_rollback_retained', 0))
        self.assertEqual((case.state / self.M._INTENT).read_bytes(), intent)
        code, status = self.setup('index-reader-status')
        self.assertEqual((code, status['reader']['store_format'], status['held'], status['migration_state']),
                         (0, 'v6', True, 'switched'))
        self.assertEqual(status['rollback']['transition_id'], result['transition_id'])
        # A second request is idempotent: no CAS, one more wake.
        again, wakes = self.rollback()
        self.assertEqual((again['status'], again['transition_id']), ('rollback_retained', result['transition_id']))
        self.assertEqual(u15.selection(), after)
        self.assertEqual(len(wakes), 1)
        # The v6 writer is the selected writer again; the schema3 one is not.
        plans = importlib.import_module('laya_writer_open_plan')
        with self.u10.seed.authority():
            plan = plans.capture_writer_plan(case.project, executing_root=case.plugin)
            self.assertEqual(plans._issued[plan][3]['endpoint']['store_format'], 'v6')
        decision = J.select_route(str(case.project), action='update')
        self.assertEqual(self.W._selected_store_format(decision), 'v6')
        with mock.patch.object(self.W, '_run_schema3', side_effect=AssertionError('schema3 writer ran')), \
                mock.patch.object(self.W, '_capacity_paths', side_effect=RuntimeError('v6 path reached')), \
                self.u10.seed.authority(), \
                mock.patch.object(self.W, '_writer_executing_root', return_value=case.plugin):
            with self.assertRaisesRegex(RuntimeError, 'v6 path reached'):
                self.W._run_sync(case.project, start_service=lambda *a, **k: None)
        # Catch-up: a card written while schema3 was selected reaches v6 recall
        # after the v6 writer core publishes (what the woken sync does).
        card = case.auto / 'wiki' / 'concepts' / 'heron-route.md'
        card.write_text(card.read_text(encoding='utf8').replace('\n---\n', '\n---\nPlover rollback marker.\n', 1),
                        encoding='utf8')
        roots = {case.WIKI: str(case.auto / 'wiki')}
        with self.u10.seed.authority():
            with case.runtime(roots) as runtime:
                runtime.catalog.request_scan()
            case.publish_all(roots)
            case.rebind_fixture_ledger(roots)
        caught = self.u11.v6_generation()
        self.assertNotEqual(caught, v6_gen)
        answer = self.u11.recall(prompt='plover rollback marker')
        self.assertIn('generation ' + caught, answer['context'])
        self.assertIn('Plover rollback marker', answer['context'])
        emit('rollback', initial=list(initial), switched=list(switched), after=list(after), caught=caught)

    def test_02_refusals_write_nothing(self):
        case, u15 = self.case, self.u15
        bytes_before = case.journal.read_bytes()
        with self.assertRaisesRegex(ValueError, 'rollback_not_switched'):
            self.rollback()
        self.assertEqual(case.journal.read_bytes(), bytes_before)
        self.switched()
        bytes_before = case.journal.read_bytes()
        # The operator entry point needs its approval flags and writes nothing without them.
        for action, reason in (('rollback-index-reader', 'index_reader_rollback_approval_required'),
                               ('resume-index-reader-migration',
                                'index_reader_migration_resume_approval_required')):
            self.assertEqual(self.setup(action), (1, {'status': 'rejected', 'reason': reason, 'changesApplied': 0}))
        code, status = self.setup('index-reader-status')
        self.assertEqual((code, status['reader']['store_format'], status['held']), (0, 'schema3', False))
        self.assertEqual(case.journal.read_bytes(), bytes_before)
        # A v6 store that is not the one the switch left is not a rollback target.
        real = self.reader._recheck

        def other_db(endpoint):
            saved, raw, proof = real(endpoint)
            if saved.get('store_format') == 'v6':
                saved = {**saved, 'db_node': [0, 1]}
            return saved, raw, proof
        with mock.patch.object(self.reader, '_recheck', side_effect=other_db), \
                self.assertRaisesRegex(ValueError, 'rollback_v6_changed'):
            self.rollback()
        self.assertEqual(case.journal.read_bytes(), bytes_before)
        # A held sync owner defers; nothing changes.
        sync = importlib.import_module('laya_index_sync')
        decision = J.select_route(str(case.project), action='recall')
        with sync.sync_owner(Path(decision['queuePath'])) as owner:
            self.assertIsNotNone(owner)
            with self.assertRaisesRegex(ValueError, 'sync_owner_busy'):
                self.rollback()
        self.assertEqual(case.journal.read_bytes(), bytes_before)
        # A held installer lock (an install operation in progress) is busy, not a wait.
        product = importlib.import_module('laya_product_install')
        with product._install_lock(case.state):
            with self.assertRaisesRegex(RuntimeError, 'install_busy'):
                self.rollback()
        # The entry point reports a busy code as deferred (exit 75), never as a refusal.
        with mock.patch.object(self.M, 'rollback_reader', side_effect=product.InstallBlocked('install_busy')):
            code, deferred = self.setup('rollback-index-reader', '--approve-index-reader-rollback')
        self.assertEqual((code, deferred['status'], deferred['reason']), (75, 'deferred', 'install_busy'))
        self.assertEqual(case.journal.read_bytes(), bytes_before)
        self.assertEqual(u15.store_format(), 'schema3')

    # ---- crash points: SIGKILL inside and right after the rollback CAS ------
    def test_03_sigkill_inside_and_after_the_rollback_cas_converges(self):
        case, u15 = self.case, self.u15
        initial, switched = self.switched()
        killed = self.crash('v5-image-committed')
        self.assertEqual(killed.returncode, -signal.SIGKILL, killed.stdout + killed.stderr)
        self.assertEqual(u15.selection(), switched)
        self.assertEqual(u15.store_format(), 'schema3')
        killed = self.crash('v5-image-replaced')
        self.assertEqual(killed.returncode, -signal.SIGKILL, killed.stdout + killed.stderr)
        landed = u15.selection()
        self.assertEqual((landed[0], landed[2]), (switched[0] + 1, switched[1]))
        self.assertEqual(u15.store_format(), 'v6')
        # The wake was lost with the process; the next request re-offers it, no CAS.
        result, wakes = self.rollback()
        self.assertEqual((result['status'], result['transition_id']), ('rollback_retained', landed[3]))
        self.assertEqual(len(wakes), 1)
        self.assertEqual(u15.selection(), landed)
        self.assertFalse((case.state / '.reader-next.sqlite').exists())
        with u15.driver():
            status = self.W.shadow_pass(case.project)
        self.assertEqual(status['reason'], 'migration_rollback_retained')
        emit('rollback-crash', switched=list(switched), landed=list(landed))

    # ---- recalls during the rollback CAS each complete in old or new --------
    def test_04_recalls_during_the_rollback_complete_in_old_or_new(self):
        case, u15 = self.case, self.u15
        self.switched()
        old_gen, new_gen = u15.schema3_generation(), self.u11.v6_generation()
        child = subprocess.Popen([sys.executable, '-B', '-c', CHILD, str(ROOT), str(self.payload('none'))],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ))
        served = []
        try:
            for _ in range(50):
                answer = u15.recall()
                found = [gen for gen in (old_gen, new_gen) if 'generation ' + gen in answer['context']]
                served.append(found)
                if child.poll() is not None and found == [new_gen]:
                    break
            out, err = child.communicate(timeout=180)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
        emit('rollback-concurrent-recall', recalls=len(served), childExit=child.returncode,
             old=sum(found == [old_gen] for found in served), new=sum(found == [new_gen] for found in served))
        self.assertEqual(child.returncode, 1, out + err)
        self.assertEqual(json.loads(out.strip().splitlines()[-1])['status'], 'rolled_back', out + err)
        self.assertTrue(all(found in ([old_gen], [new_gen]) for found in served), served)
        self.assertIn('generation ' + new_gen, u15.recall()['context'])

    def payload(self, kill):
        case = self.case
        payload = case.home / ('rollback-%s.json' % kill)
        payload.write_text(json.dumps({'plugin': str(case.plugin), 'proof': case.proof,
                                       'project': str(case.project), 'kill': kill}), encoding='utf8')
        return payload

    # ---- explicit resume: a fresh migration on a new candidate --------------
    def test_05_resume_starts_a_fresh_migration_on_a_new_candidate(self):
        case, u15 = self.case, self.u15
        self.switched()
        self.rollback()
        old_candidate = case.state / self.W.SHADOW_CANDIDATE
        old_intent = (case.state / self.M._INTENT).read_bytes()
        with self.u10.bridge_authority():
            resumed = self.W.resume_shadow_migration(case.project)
        self.assertEqual(resumed['status'], 'resumed', resumed)
        activation = json.loads((case.state / self.W.SHADOW_ACTIVATION).read_text(encoding='utf8'))
        self.assertEqual(activation['resumed_after'], u15.selection()[3])
        self.assertNotEqual(activation['candidate'], str(old_candidate))
        self.assertTrue(old_candidate.exists())
        retired = case.state / resumed['retiredIntent']
        self.assertEqual(retired.read_bytes(), old_intent)
        self.assertFalse((case.state / self.M._INTENT).exists())
        with u15.driver():
            status = self.W.shadow_pass(case.project)
        self.assertEqual(status['stages'][:2], ['create', 'begin'], status)
        self.assertEqual(u15.intent()['candidate'], activation['candidate'])
        self.assertEqual(u15.intent()['migration_state'], 'copying')
        # Resuming again needs another rollback first.
        with self.u10.bridge_authority(), self.assertRaisesRegex(ValueError, 'migration_not_held'):
            self.W.resume_shadow_migration(case.project)

    # ---- U6 recovery: a lost candidate or a moved intent restarts once ------
    def test_06_lost_candidate_restarts_on_a_new_candidate(self):
        case, u15 = self.case, self.u15
        with u15.driver():
            u15.drive_to('caught_up', limit=60, steps=1)
        candidate = case.state / self.W.SHADOW_CANDIDATE
        stale = (case.state / self.M._INTENT).read_bytes()
        for suffix in ('', '-wal', '-shm'):
            if os.path.lexists(str(candidate) + suffix):
                os.unlink(str(candidate) + suffix)
        with u15.driver():
            first = self.W.shadow_pass(case.project)
            self.assertEqual(first['stages'][:3], ['restart', 'create', 'begin'], first)
            self.assertNotEqual(first['state'], 'error', first)
            retired = sorted(p.name for p in case.state.glob('shadow-migration-intent.retired-*.json'))
            self.assertEqual(len(retired), 1)
            self.assertEqual((case.state / retired[0]).read_bytes(), stale)
            u15.drive_to('shadow_verified')
        activation = json.loads((case.state / self.W.SHADOW_ACTIVATION).read_text(encoding='utf8'))
        self.assertNotEqual(activation['candidate'], str(candidate))
        self.assertEqual(u15.intent()['candidate'], activation['candidate'])
        errors = [e for e in u15.events() if e['event'] == 'migration.driver' and e['outcome'] == 'error']
        self.assertEqual(errors, [])

    def test_07_moved_intent_and_activation_restart_once(self):
        case, u15 = self.case, self.u15
        with u15.driver():
            u15.drive_to('caught_up', limit=60, steps=1)
        ledger = importlib.import_module('laya_ledger_common')
        old_state = str(case.state.parent / 'moved-away' / case.state.name)
        intent = u15.intent()
        moved = {**intent, 'state': old_state,
                 'candidate': str(Path(old_state) / self.W.SHADOW_CANDIDATE)}
        ledger._write(case.state / self.M._INTENT, moved, replace=True)
        activation = json.loads((case.state / self.W.SHADOW_ACTIVATION).read_text(encoding='utf8'))
        ledger._write(case.state / self.W.SHADOW_ACTIVATION,
                      {**activation, 'state': old_state, 'candidate': moved['candidate']}, replace=True)
        with u15.driver():
            first = self.W.shadow_pass(case.project)
        self.assertEqual(first['stages'][:3], ['restart', 'create', 'begin'], first)
        self.assertNotEqual(first['state'], 'error', first)
        # The old candidate stays where it is; nothing is deleted.
        self.assertTrue((case.state / self.W.SHADOW_CANDIDATE).exists())
        self.assertEqual(u15.intent()['state'], str(case.state))
        self.assertNotEqual(u15.intent()['candidate'], moved['candidate'])
        # A corrupt intent naming the current candidate is not a move: it still fails.
        ledger._write(case.state / self.M._INTENT, {**u15.intent(), 'phase': 'bogus'}, replace=True)
        with u15.driver():
            third = self.W.shadow_pass(case.project)
        self.assertEqual((third['state'], third['reason']), ('error', 'migration_intent_invalid'))
        self.assertEqual(len(list(case.state.glob('shadow-migration-intent.retired-*.json'))), 1)


    # ---- U16b: unreadable schema3 store and changed recall settings --------
    def test_08_unreadable_schema3_store_with_changed_settings_still_rolls_back(self):
        case, u15 = self.case, self.u15
        initial, switched = self.switched()
        v6_gen = self.u11.v6_generation()
        _selection, record, _history = self.M._read_history(case.journal)
        self.assertEqual(record['store_format'], 'schema3')
        # The schema3 store is lost (moved aside; nothing is deleted) ...
        lost = Path(record['db_path'])
        for suffix in ('', '-wal', '-shm'):
            if os.path.lexists(str(lost) + suffix):
                os.replace(str(lost) + suffix, str(lost) + suffix + '.lost')
        # ... and the recall settings changed, so the owner cannot re-seal it.
        settings = importlib.import_module('project_dir').settings_path(case.project)
        value = json.loads(settings.read_text(encoding='utf8'))
        settings.write_text(json.dumps(dict(value, skipPaths=['u16b-skip'])), encoding='utf8')
        with self.assertRaises(ValueError):
            self.reader.resettle_selection(case.state, case.project, case.plugin)
        bytes_before = case.journal.read_bytes()
        # A v6 store that is not the one the switch left is still refused.
        real = self.reader._recheck

        def other_db(endpoint):
            saved, raw, proof = real(endpoint)
            if saved.get('store_format') == 'v6':
                saved = {**saved, 'db_node': [0, 1]}
            return saved, raw, proof
        with mock.patch.object(self.reader, '_recheck', side_effect=other_db), \
                self.assertRaisesRegex(ValueError, 'rollback_v6_changed'):
            self.rollback()
        self.assertEqual(case.journal.read_bytes(), bytes_before)
        # The v6 proof alone admits the rollback.
        result, wakes = self.rollback()
        self.assertEqual(result['status'], 'rolled_back', result)
        self.assertEqual([a[:1] for a, _k in wakes], [('laya-sync',)])
        after = u15.selection()
        self.assertEqual((after[0], after[2], after[3]), (switched[0] + 1, switched[1], result['transition_id']))
        _selection, stored, _history = self.M._read_history(case.journal)
        self.assertEqual(stored['store_format'], 'v6')
        # Only the recall settings digest separates the pair (current v6, stale schema3).
        self.assertNotEqual(stored['settings'], record['settings'])
        self.assertTrue(self.reader.rollback_pair(record, stored))
        self.assertFalse(self.reader.rollback_pair(stored, record))
        self.assertFalse(self.reader.rollback_pair(record, {**stored, 'install_id': '0' * 32}))
        answer = u15.recall()
        self.assertIn('generation ' + v6_gen, answer['context'])
        code, status = self.setup('index-reader-status')
        self.assertEqual((code, status['reader']['store_format'], status['held']), (0, 'v6', True))
        again, wakes = self.rollback()
        self.assertEqual((again['status'], again['transition_id']), ('rollback_retained', result['transition_id']))
        # The logs skill shows the rollback, its refusal and its replay.
        done = subprocess.run([sys.executable, '-B', str(ROOT / 'skills/logs/scripts/logs.py'),
                               '--state-root', str(case.state), 'migration', '--limit', '0'],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        lines = [line for line in done.stdout.splitlines() if 'migration.rollback' in line]
        self.assertEqual([line.split('migration.rollback ', 1)[1].split(' ')[0] for line in lines],
                         ['refused', 'rolled_back', 'retained'], done.stdout)
        self.assertIn('reason=rollback_v6_changed', lines[0])
        summary = subprocess.run([sys.executable, '-B', str(ROOT / 'skills/logs/scripts/logs.py'),
                                  '--state-root', str(case.state)],
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(summary.returncode, 0, summary.stderr)
        self.assertIn('rollback=retained', summary.stdout)
        emit('rollback-unreadable-schema3', switched=list(switched), after=list(after))

    # ---- U16b: a stopped lifecycle intent defers the rollback ---------------
    def test_09_stopped_lifecycle_intents_defer_the_rollback(self):
        case, u15 = self.case, self.u15
        fresh = importlib.import_module('laya_fresh_wiki_origin')
        state_move = importlib.import_module('laya_state_move')
        folder_move = importlib.import_module('laya_project_dir_move')
        project_dir = importlib.import_module('project_dir')
        self.switched()
        bytes_before = case.journal.read_bytes()

        def busy():
            with self.assertRaisesRegex(ValueError, 'rollback_lifecycle_busy'):
                self.rollback()
            self.assertEqual(case.journal.read_bytes(), bytes_before)
        # An active upgrade stopped before its fence: the route it names is not selected yet.
        receipt = u15.active_intent(settled=False)
        busy()
        # A stopped state move (intent) and project folder move (pointer).
        u15.active_intent(settled=True)
        for path in (case.state / state_move.INTENT,
                     project_dir.runtime_file(case.project, folder_move.POINTER)):
            path.write_bytes(b'{}')
            path.chmod(0o600)
            try:
                busy()
            finally:
                path.unlink()
        # The upgrade fence alone.
        with mock.patch.object(fresh, 'upgrade_fenced', return_value=True):
            busy()
        # The entry point reports it as deferred (exit 75).
        with mock.patch.object(self.M, 'rollback_reader',
                               side_effect=self.M.IndexContractError('rollback_lifecycle_busy')):
            code, deferred = self.setup('rollback-index-reader', '--approve-index-reader-rollback')
        self.assertEqual((code, deferred['status'], deferred['reason']), (75, 'deferred', 'rollback_lifecycle_busy'))
        # A settled upgrade receipt is not busy; the receipt stays.
        before = receipt.read_bytes()
        result, _wakes = self.rollback()
        self.assertEqual(result['status'], 'rolled_back', result)
        self.assertEqual(receipt.read_bytes(), before)
        emit('rollback-lifecycle-busy', refused=4)


if __name__ == '__main__':
    unittest.main(verbosity=2)

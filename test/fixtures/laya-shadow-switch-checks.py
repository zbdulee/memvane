#!/usr/bin/env python3
"""U15 reader switch: shadow_verified -> reader_ready -> switched by sync passes.

Builds on the U11 fixture (itself the U10 one): a real v5 install, v6 index,
journal images, Git checkout, actual shadow migration driver and main recall.
Synthetic authority, as in U10/U11: bundle inspection and host observation
(the loaded-reader receipts are collected through the existing
``begin_loaded_bridge``/``collect_bridge_preflight`` path with a synthetic
observer). The published fixture project lives on ``.auto-context/``, so the
positive path patches ``project_dir.layout`` to ``current`` for this project
only; the refusal tests use the real layout. No actual host, provider or
operational setup entry point is exercised.
"""
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
import hashlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'core'))
spec = importlib.util.spec_from_file_location('switch_recall_checks', HERE / 'laya-schema3-recall-checks.py')
recall_checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recall_checks)
J = recall_checks.J
HOSTS = frozenset(('claude', 'codex'))
COMMIT = 'c' * 40

# A driver pass in a separate process, killed at one checkpoint (SIGKILL).
CHILD = r'''
import importlib.util, json, os, signal, sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
root, payload = Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text(encoding='utf8'))
sys.path.insert(0, str(root / 'core'))
signal.alarm(120)
spec = importlib.util.spec_from_file_location('switch_crash_authority', root / 'test/fixtures/laya-journal-reader-checks.py')
fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)
import laya_bridge_readiness as bridge, laya_install_journal as J, laya_journal_reader as reader
import laya_ledger_common as ledger, laya_project_dir_move as mover, laya_reader_lease as lease
import laya_product_sync_worker as worker, project_dir
helper = fixture.JournalReaderChecks('runTest')
case = SimpleNamespace(plugin=Path(payload['plugin']), proof=payload['proof'])
project, kill = Path(payload['project']), payload['kill']
real_layout, real_write, real_step = project_dir.layout, ledger._write, J._conversion_step
def die(label):
    print(label, flush=True)
    os.kill(os.getpid(), signal.SIGKILL)
def write(path, value, *args, **kwargs):
    if (kill == 'intent' and Path(path).name == 'shadow-migration-intent.json'
            and isinstance(value, dict) and value.get('migration_state') == 'reader_ready'):
        die('killed-before-reader-ready')
    if (kill == 'after-cas' and Path(path).name == 'shadow-migration-intent.json'
            and isinstance(value, dict) and value.get('migration_state') == 'switched'):
        die('killed-after-cas')
    return real_write(path, value, *args, **kwargs)
def step(name):
    if kill == name:
        die('killed-at-' + name)
    if real_step is not None:
        real_step(name)
def inspect(root):
    return {**case.proof, 'pluginRoot': str(root)}
with helper.full_authority(case, reader, bridge, lease, mover), \
        mock.patch.object(bridge, 'inspect_full_plugin_candidate', side_effect=inspect), \
        mock.patch.object(project_dir, 'layout',
                          side_effect=lambda p: 'current' if Path(p) == project else real_layout(p)), \
        mock.patch.object(worker, '_writer_executing_root', return_value=case.plugin), \
        mock.patch.object(worker, '_run_sync', return_value={'state': 'unchanged'}), \
        mock.patch.object(ledger, '_write', side_effect=write), \
        mock.patch.object(J, '_conversion_step', side_effect=step):
    print(json.dumps(worker.shadow_pass(project)), flush=True)
raise SystemExit('checkpoint not reached')
'''


def emit(case, **evidence):
    print(json.dumps(dict(case=case, **evidence), sort_keys=True, default=str), flush=True)


class ShadowSwitchChecks(unittest.TestCase):
    def setUp(self):
        self.u11 = recall_checks.Schema3RecallChecks('runTest')
        self.u11.setUp()
        self.addCleanup(self.u11.doCleanups)
        self.u10, self.case = self.u11.u10, self.u11.case
        self.W = importlib.import_module('laya_product_sync_worker')
        self.M = self.u10.migration
        self.reader, self.bridge = self.u10.reader, self.u10.bridge
        self.project_dir = importlib.import_module('project_dir')

    # ---- helpers -----------------------------------------------------------
    @contextmanager
    def driver(self, layout='current'):
        """Full synthetic bundle/host authority, the driver's own seams and a ``.memvane`` layout."""
        real = self.project_dir.layout
        with ExitStack() as stack:
            stack.enter_context(self.u10.bridge_authority())
            stack.enter_context(mock.patch.object(self.W, '_writer_executing_root', return_value=self.case.plugin))
            stack.enter_context(mock.patch.object(self.W, '_run_sync', return_value={'state': 'unchanged'}))
            if layout is not None:
                stack.enter_context(mock.patch.object(
                    self.project_dir, 'layout',
                    side_effect=lambda p: layout if Path(p) == self.case.project else real(p)))
            yield

    def intent(self):
        path = self.case.state / self.M._INTENT
        return json.loads(path.read_text(encoding='utf8')) if path.exists() else None

    def events(self):
        path = self.case.state / 'events.jsonl'
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding='utf8').splitlines()]

    def selection(self):
        value = self.reader.read_selection(self.case.journal)
        return (value.epoch, value.preferred_endpoint, value.previous_endpoint, value.transition_id)

    def store_format(self):
        with self.u10.resolved() as endpoint:
            return endpoint.record['store_format']

    def run_pass(self):
        before = len(self.events())
        self.assertEqual(self.W.run(self.case.project), {'state': 'unchanged'})
        driver = [e for e in self.events()[before:] if e['event'] == 'migration.driver']
        return driver[-1] if driver else None

    def drive_to(self, state, limit=30, steps=None):
        passes = []
        for _ in range(limit):
            with mock.patch.object(self.W, 'SHADOW_STEPS_PER_PASS', steps or self.W.SHADOW_STEPS_PER_PASS):
                event = self.run_pass()
            passes.append(None if event is None else (event['steps'], event.get('migration_state'),
                                                      event['outcome'], event.get('reason')))
            if (self.intent() or {}).get('migration_state') == state:
                return passes
        self.fail('%s not reached: %s' % (state, passes))

    def loaded(self, *, hosts=HOSTS, commit=COMMIT):
        """Loaded-reader receipts at the current selection (existing registry format)."""
        current = self.reader.read_selection(self.case.journal)
        if hosts == HOSTS and commit == COMMIT:
            return self.u10.loaded(current)
        case, u10 = self.case, self.u10
        with u10.bridge_authority() as companion:
            def observed(challenge, host):
                value = u10.seed.helper.observer(case, challenge, host)
                root = case.plugin if host == 'claude' else companion
                info = root.stat()
                value.update(hook_stage='loaded-v5-reader', endpoint_id=current.preferred_endpoint,
                             reader_epoch=current.epoch, store_format='v6', generation='g' * 8,
                             sentinel_sha256='5' * 64, owner_role='owner' if host == 'claude' else 'companion',
                             plugin_root=str(root), plugin_node=[info.st_dev, info.st_ino],
                             process_pid=os.getpid() + (1 if host == 'codex' else 0),
                             release_commit=commit)
                return value
            proof = {**case.proof, 'sourceCommit': commit}
            with mock.patch.object(self.bridge, '_observe_registered_hook', side_effect=observed), \
                    mock.patch.object(self.bridge, 'inspect_full_plugin_candidate',
                                      side_effect=lambda root: {**proof, 'pluginRoot': str(root)}):
                challenge = self.bridge.begin_loaded_bridge(case.state, release_commit=commit,
                    expected_hosts=HOSTS, sentinel_path='concepts/otter-ledger.md', sentinel_sha256='5' * 64)
                return [self.bridge.collect_bridge_preflight(challenge, host=host) for host in sorted(hosts)]

    def ready(self, store=None):
        """One direct advance_reader_ready call on the driver's own candidate."""
        opened = store is None
        if opened:
            store = self.W._shadow_open(self.case.state, self.case.project, self.case.route['scopeId'])[0]
        try:
            return self.M.advance_reader_ready(store, project=self.case.project, executing_root=self.case.plugin,
                                               deadline=time.monotonic() + 60)
        finally:
            if opened:
                store.close()

    def recall(self):
        return self.u11.recall()

    def schema3_generation(self):
        return self.intent()['published']['generation']

    # ---- completion: passes alone reach switched ---------------------------
    def test_01_sync_passes_reach_switched_and_main_recall_reads_schema3(self):
        case = self.case
        initial = self.selection()
        v6_gen = self.u11.v6_generation()
        with self.driver():
            passes = self.drive_to('shadow_verified')
            # Without loaded-reader receipts every later pass stays verified.
            event = self.run_pass()
            self.assertEqual((event['outcome'], event['reason'], event['migration_state']),
                             ('waiting', 'switch_bridge_missing', 'shadow_verified'))
            self.assertEqual(self.selection(), initial)
            self.loaded()
            event = self.run_pass()
            self.assertEqual((event['outcome'], event['migration_state']), ('progressed', 'reader_ready'))
            ready = self.intent()['reader_ready']
            self.assertEqual(self.selection(), initial)
            event = self.run_pass()
            self.assertEqual((event['outcome'], event['migration_state']), ('progressed', 'switched'))
            after = self.selection()
            self.assertEqual(after, (initial[0] + 1, ready['endpoint_id'], initial[1], ready['transition_id']))
            switched = self.intent()
            self.assertEqual(switched['migration_state'], 'switched')
            self.assertEqual(switched['switched']['endpoint_id'], ready['endpoint_id'])
            self.assertEqual(self.store_format(), 'schema3')
            # The switched install is past the driver; the intent stays as it is.
            before = self.intent()
            status = self.W.shadow_pass(case.project)
            self.assertEqual((status['state'], status['reason']), ('skipped', 'reader_schema3_selected'))
            self.assertEqual(self.intent(), before)
        answer = self.recall()
        gen = self.schema3_generation()
        emit('switched-by-passes', passes=passes, gen=gen, v6=v6_gen, ready=sorted(ready))
        self.assertEqual(answer['results'][-1].store_format, 'schema3')
        self.assertEqual(answer['results'][-1].data_generation, gen)
        self.assertIn('generation ' + gen, answer['context'])
        self.assertNotIn('generation ' + v6_gen, answer['context'])
        self.assertEqual(answer['v6_runtime'], 0)
        held = [e['ms'] for e in self.events() if e['event'] == 'migration.shadow'
                and e.get('stage') in ('reader_ready', 'switch') and e.get('outcome') != 'attempt']
        emit('switch-step-ms', held=held)
        # After the switch the v6 writer is refused and a schema3 plan is issued (U12b).
        plans = importlib.import_module('laya_writer_open_plan')
        with self.u10.seed.authority():
            plan = plans.capture_writer_plan(case.project, executing_root=case.plugin)
            self.assertEqual(plans._issued[plan][3]['endpoint']['store_format'], 'schema3')

    # ---- gates: each missing piece of evidence refuses and writes nothing ---
    def test_02_each_missing_gate_refuses_without_switching(self):
        case = self.case
        with self.driver():
            self.drive_to('caught_up', limit=60, steps=1)
            result = self.ready()
            self.assertEqual((result['stage'], result['reason']), ('retry', 'switch_parity_missing'))
            self.drive_to('shadow_verified')
        journal_bytes, verified, initial = case.journal.read_bytes(), self.intent(), self.selection()

        def refused(reason, layout='current', state='shadow_verified'):
            before = self.intent()
            with self.driver(layout=layout):
                result = self.ready()
            self.assertEqual((result['stage'], result.get('reason'), result['migration_state']),
                             ('retry', reason, state))
            self.assertEqual(case.journal.read_bytes(), journal_bytes, reason)
            self.assertEqual(self.intent(), before, reason)
            self.assertEqual(self.selection(), initial, reason)
            return reason

        seen = [refused('switch_bridge_missing')]
        # One host only, and a receipt set for another release commit.
        self.loaded(hosts=frozenset(('claude',)))
        seen.append(refused('switch_bridge_missing'))
        self.loaded(commit='a' * 40)
        seen.append(refused('switch_bridge_missing'))
        self.loaded()
        # Real layouts: the published fixture is .auto-context (legacy), and
        # an empty .memvane over it is a split checkout.
        self.assertEqual(self.project_dir.layout(case.project), 'legacy')
        seen.append(refused('switch_layout_not_current', layout=None))
        split = case.project / '.memvane'
        split.mkdir()
        try:
            self.assertEqual(self.project_dir.layout(case.project), 'split')
            # The split checkout already blocks the selected route itself.
            with self.driver(layout=None), self.assertRaisesRegex(ValueError, 'migration_route_invalid'):
                self.ready()
            self.assertEqual(case.journal.read_bytes(), journal_bytes)
            self.assertEqual(self.intent(), verified)
            with self.driver(layout=None):
                status = self.W.shadow_pass(case.project)
            self.assertEqual((status['state'], status['reason']), ('skipped', 'project_dir_split'))
        finally:
            split.rmdir()
        busy = case.state / 'state-move-intent.json'
        busy.write_text('{}', encoding='utf8')
        try:
            seen.append(refused('switch_lifecycle_busy'))
        finally:
            busy.unlink()
        v4 = importlib.import_module('laya_journal_v4')
        with mock.patch.object(v4, 'legacy_hermes_present', return_value=True):
            seen.append(refused('switch_hosts_unsupported'))
        real_inspect = J.inspect_journal

        def other_bundle(path):
            value = real_inspect(path)
            return {**value, 'selected': {**value['selected'], 'sourceCommit': 'b' * 40}}
        with mock.patch.object(J, 'inspect_journal', side_effect=other_bundle):
            seen.append(refused('switch_bundle_mismatch'))
        # A v6 writer commit after the catch-up: the in-lock recheck sees another
        # observation or source revision (synthetic seam over the actual check).
        real_capture, real_revision = self.M.capture_effective, self.M._source_revision

        def moved(*args, **kwargs):
            return {**real_capture(*args, **kwargs), 'observation': '9' * 64}
        with mock.patch.object(self.M, 'capture_effective', side_effect=moved):
            seen.append(refused('switch_catchup_stale'))

        def revised(snapshot):
            value = real_revision(snapshot)
            return {**value, 'generation': value['generation'] + '-later'}
        with mock.patch.object(self.M, '_source_revision', side_effect=revised):
            seen.append(refused('switch_catchup_stale'))
        # Receipts outside their TTL are not evidence.
        real_time = time.time
        with mock.patch.object(self.bridge, 'time', mock.Mock(time=lambda: real_time() + 601,
                                                               monotonic=time.monotonic)):
            seen.append(refused('switch_bridge_missing'))
        self.assertEqual(self.intent(), verified)

        # Every gate holds: reader_ready records the target, nothing switches.
        with self.driver():
            result = self.ready()
        self.assertEqual((result['stage'], result['migration_state']), ('reader_ready', 'reader_ready'))
        self.assertEqual(case.journal.read_bytes(), journal_bytes)
        ready_intent = self.intent()
        # The same gates guard the CAS step.
        seen.append(refused('switch_layout_not_current', layout=None, state='reader_ready'))
        with mock.patch.object(self.M, 'capture_effective', side_effect=moved):
            seen.append(refused('switch_catchup_stale', state='reader_ready'))
        with mock.patch.object(self.bridge, 'time', mock.Mock(time=lambda: real_time() + 601,
                                                               monotonic=time.monotonic)):
            seen.append(refused('switch_bridge_missing', state='reader_ready'))
        self.assertEqual(self.intent(), ready_intent)
        emit('switch-gates', refused=seen)
        with self.driver():
            event = self.run_pass()
        self.assertEqual((event['outcome'], event['migration_state']), ('progressed', 'switched'))
        switched = self.selection()
        self.assertEqual(switched[:3], (initial[0] + 1, ready_intent['reader_ready']['endpoint_id'], initial[1]))
        # A second switch with the same receipts is the recorded one, not a new CAS.
        with self.driver():
            again = self.ready()
        self.assertEqual(again, {'stage': 'switched', 'migration_state': 'switched'})
        self.assertEqual(self.selection(), switched)

    # ---- crash points: SIGKILL before reader_ready, around the CAS ----------
    def crash(self, kill):
        case = self.case
        payload = case.home / 'switch-crash.json'
        payload.write_text(json.dumps({'plugin': str(case.plugin), 'proof': case.proof,
                                       'project': str(case.project), 'kill': kill}), encoding='utf8')
        killed = subprocess.run([sys.executable, '-B', '-c', CHILD, str(ROOT), str(payload)],
                                capture_output=True, text=True, timeout=180, env=dict(os.environ))
        self.assertEqual(killed.returncode, -signal.SIGKILL, killed.stdout + killed.stderr)
        return killed.stdout.strip().splitlines()[-1]

    def test_03_sigkill_before_and_after_the_cas_resumes_to_one_switch(self):
        case = self.case
        with self.driver():
            self.drive_to('shadow_verified')
            self.loaded()
        initial, journal_bytes = self.selection(), case.journal.read_bytes()
        # Killed right before the reader_ready write: nothing changed.
        self.assertEqual(self.crash('intent'), 'killed-before-reader-ready')
        self.assertEqual(self.intent()['migration_state'], 'shadow_verified')
        self.assertEqual(case.journal.read_bytes(), journal_bytes)
        with self.driver():
            event = self.run_pass()
        self.assertEqual(event['migration_state'], 'reader_ready')
        ready = self.intent()['reader_ready']
        # Killed inside the CAS before the journal replace: old selection.
        self.assertEqual(self.crash('v5-image-committed'), 'killed-at-v5-image-committed')
        self.assertEqual(self.selection(), initial)
        self.assertEqual(self.store_format(), 'v6')
        self.assertEqual(self.intent()['reader_ready'], ready)
        # Killed right after the replace: the journal switched, the intent did not.
        self.assertEqual(self.crash('v5-image-replaced'), 'killed-at-v5-image-replaced')
        landed = self.selection()
        self.assertEqual(landed, (initial[0] + 1, ready['endpoint_id'], initial[1], ready['transition_id']))
        self.assertEqual(self.store_format(), 'schema3')
        self.assertEqual(self.intent()['migration_state'], 'reader_ready')
        # The next pass skips the switched install and records the landed CAS once.
        with self.driver():
            status = self.W.shadow_pass(case.project)
            self.assertEqual((status['state'], status['reason']), ('skipped', 'reader_schema3_selected'))
            self.assertEqual(self.intent()['migration_state'], 'switched')
            self.assertEqual(self.intent()['switched']['endpoint_id'], ready['endpoint_id'])
            settled = self.intent()
            self.W.shadow_pass(case.project)
        self.assertEqual(self.intent(), settled)
        self.assertEqual(self.selection(), landed)
        self.assertFalse((case.state / '.reader-next.sqlite').exists())
        emit('switch-crash', initial=list(initial), landed=list(landed))

    def test_04_after_cas_kill_before_the_intent_write_is_recorded_without_a_second_cas(self):
        case = self.case
        with self.driver():
            self.drive_to('shadow_verified')
            self.loaded()
            self.run_pass()
        ready = self.intent()['reader_ready']
        initial = self.selection()
        self.assertEqual(self.crash('after-cas'), 'killed-after-cas')
        landed = self.selection()
        self.assertEqual(landed, (initial[0] + 1, ready['endpoint_id'], initial[1], ready['transition_id']))
        self.assertEqual(self.intent()['migration_state'], 'reader_ready')
        # A direct call (as a later bundle would make) records it with no CAS.
        with self.driver():
            result = self.ready()
        self.assertEqual(result, {'stage': 'switched', 'migration_state': 'switched'})
        self.assertEqual(self.selection(), landed)

    # ---- recalls during the CAS each complete in the old or the new reader --
    def test_05_recalls_during_the_switch_pass_complete_in_old_or_new(self):
        case = self.case
        with self.driver():
            self.drive_to('shadow_verified')
            self.loaded()
            self.run_pass()
        self.assertEqual(self.intent()['migration_state'], 'reader_ready')
        old_gen, new_gen = self.u11.v6_generation(), self.schema3_generation()
        payload = case.home / 'switch-concurrent.json'
        payload.write_text(json.dumps({'plugin': str(case.plugin), 'proof': case.proof,
                                       'project': str(case.project), 'kill': 'none'}), encoding='utf8')
        child = subprocess.Popen([sys.executable, '-B', '-c', CHILD, str(ROOT), str(payload)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ))
        served, retried = [], 0
        try:
            for _ in range(50):
                answer = self.recall()
                found = [gen for gen in (old_gen, new_gen) if 'generation ' + gen in answer['context']]
                served.append(found)
                retried += 'laya_recall_proof_retry' in (answer['event'].get('notes') or [])
                if child.poll() is not None and found == [new_gen]:
                    break
            out, err = child.communicate(timeout=180)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
        emit('switch-concurrent-recall', recalls=len(served), retried=retried, childExit=child.returncode,
             old=sum(found == [old_gen] for found in served), new=sum(found == [new_gen] for found in served))
        # The child pass ends with SystemExit after printing its status.
        self.assertEqual(child.returncode, 1, out + err)
        self.assertEqual(json.loads(out.strip().splitlines()[-1])['migration_state'], 'switched', out + err)
        self.assertTrue(all(found in ([old_gen], [new_gen]) for found in served), served)
        self.assertEqual(self.recall()['results'][-1].data_generation, new_gen)

    # ---- U15b: a finished bundle upgrade's receipt is not a busy lifecycle ---
    def active_intent(self, *, settled):
        """``active-upgrade-intent.json`` as ``laya_active_upgrade`` leaves it.

        settled: the upgrade switched to the current route and released its
        fence; the file stays as the receipt until a later bundle archives it.
        Otherwise the intent was written and the fence not yet taken: the
        current route is still the old one.
        """
        active = importlib.import_module('laya_active_upgrade')
        build = importlib.import_module('laya_initial_build')
        case = self.case
        ledger = active._origin(case.state)
        row = ledger.load(case.state / ledger.name)
        self.assertEqual(row['phase'], 'active')
        other = hashlib.sha256(b'u15b-other-route').hexdigest()
        if settled:
            old, new = {**row, 'routeSha256': other}, {**row, 'phase': 'upgrading'}
        else:
            old, new = dict(row), {**row, 'routeSha256': other, 'phase': 'upgrading'}
        identity = {'projectRoot': str(case.project), 'stateRoot': str(case.state)}
        intent = {'schema': active.V1, 'projectRoot': str(case.project), 'stateRoot': str(case.state),
                  'backup': str(case.state / 'active-upgrade-backup'), 'oldRecord': {'identity': identity},
                  'oldLedger': old, 'newLedger': new,
                  **{name: '{}' for name in ('oldRoute', 'newRoute', 'oldAttestation', 'newAttestation',
                                             'oldMarker', 'newMarker')}}
        path = case.state / active.INTENT
        if path.exists():
            path.unlink()
        build._write_json(path, intent, replace=False, max_bytes=active.LIMIT)
        return path

    def test_06_settled_active_upgrade_receipt_lets_the_switch_proceed(self):
        fresh = importlib.import_module('laya_fresh_wiki_origin')
        with self.driver():
            self.drive_to('shadow_verified')
            self.loaded()
        initial = self.selection()

        def gate(state='shadow_verified'):
            with self.driver():
                result = self.ready()
            return (result['stage'], result.get('reason'), result['migration_state'])
        # In progress: the intent names a route the ledger does not select yet.
        receipt = self.active_intent(settled=False)
        self.assertEqual(gate(), ('retry', 'switch_lifecycle_busy', 'shadow_verified'))
        # The upgrade fence alone is busy too.
        self.active_intent(settled=True)
        with mock.patch.object(fresh, 'upgrade_fenced', return_value=True):
            self.assertEqual(gate(), ('retry', 'switch_lifecycle_busy', 'shadow_verified'))
        self.assertEqual(self.selection(), initial)
        # Settled forward receipt only: passes reach switched, receipt untouched.
        before = receipt.read_bytes()
        with self.driver():
            first, second = self.run_pass(), self.run_pass()
        self.assertEqual([(e['outcome'], e['migration_state']) for e in (first, second)],
                         [('progressed', 'reader_ready'), ('progressed', 'switched')])
        self.assertEqual(self.selection()[:3], (initial[0] + 1, self.intent()['switched']['endpoint_id'], initial[1]))
        self.assertEqual(receipt.read_bytes(), before)
        emit('switch-settled-active-receipt', passes=[first['migration_state'], second['migration_state']])

    # ---- U15b: ordinary recall hooks of both hosts record the receipts ------
    def hook(self, host, *, wakes=None, probes=None, fail=False):
        """One registered recall hook through ``laya_host_response.main`` (serve-stdin).

        Synthetic authority as in ``loaded``: the companion's executing root,
        its host cache layout and the host process identity. Returns
        ``(exit, stdout)``.
        """
        case = self.case
        host_response = self.u11.host
        observer = importlib.import_module('laya_bridge_observer')
        upgrade = importlib.import_module('laya_bundle_upgrade')
        runtime_work = importlib.import_module('runtime_work')
        hook_budget = importlib.import_module('hook_budget')
        companion = case.home / 'schema3-companion'
        companion.mkdir(exist_ok=True)
        root = case.plugin if host == 'claude' else companion
        payload = {'cwd': str(case.project), 'prompt': 'heron route northern marsh',
                   'session_id': 'u15b-session-' + host, 'hook_event_name': 'UserPromptSubmit'}
        identity = dict(pid=os.getpid() + (2 if host == 'codex' else 1), uid=os.getuid(), parent=os.getpid(),
                        start=1000.5 + (host == 'codex'), start_key=(1000, 5), argv=(host,),
                        executable=str(Path(sys.executable).resolve()))
        real_probe = observer._probe_hook_reader

        def probe(*args, **kwargs):
            if probes is not None:
                probes.append(kwargs['deadline'] - time.monotonic())
            if fail:
                raise ValueError('reader_hosts_pending')
            return real_probe(*args, **kwargs)

        def request(kind, target='', **kwargs):
            if wakes is not None:
                wakes.append((kind, target))
        stdin = mock.Mock(buffer=io.BytesIO(json.dumps(payload).encode('utf8')))
        out, err = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(self.driver())
            stack.enter_context(mock.patch.object(J, '_verified_executing_root', return_value=str(root)))
            stack.enter_context(mock.patch.object(host_response, '__file__',
                                                  str(root / 'core' / 'laya_host_response.py')))
            stack.enter_context(mock.patch.object(upgrade, 'host_layout', return_value=(host, root)))
            stack.enter_context(mock.patch.object(observer, '_hook_host_process', return_value=identity))
            stack.enter_context(mock.patch.object(observer, '_probe_hook_reader', side_effect=probe))
            stack.enter_context(mock.patch.object(runtime_work, 'request', side_effect=request))
            stack.enter_context(mock.patch.object(sys, 'stdin', stdin))
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            stack.enter_context(mock.patch.object(hook_budget, '_deadline', None))
            code = host_response.main(['serve-stdin', 'recall', host, str(case.project)])
        self.assertEqual(code, 0, err.getvalue())
        return code, out.getvalue()

    def challenges(self):
        """Challenge directories written after the fixture's own setup."""
        registry = self.bridge._registry()
        names = sorted(p.name for p in registry.iterdir()) if registry.exists() else []
        if not hasattr(self, '_setup_challenges'):
            self._setup_challenges = set(names)
        return [name for name in names if name not in self._setup_challenges]

    def test_07_recall_hooks_of_both_hosts_record_receipts_and_passes_switch(self):
        case = self.case
        initial = self.selection()
        self.challenges()
        # Before shadow_verified the hook reads no reader for a receipt.
        probes, wakes = [], []
        code, out = self.hook('claude', probes=probes)
        self.assertEqual(code, 0)
        self.assertIn('additionalContext', out)
        self.assertEqual((probes, self.challenges()), ([], []))
        with self.driver():
            self.drive_to('shadow_verified')
        # A failing probe never changes the hook answer.
        code, failed = self.hook('claude', probes=probes, fail=True)
        self.assertEqual((code, len(probes), self.challenges()), (0, 1, []))
        self.assertEqual(failed, out)
        # Claude alone: one receipt, one probe bounded by the receipt budget.
        probes[:] = []
        code, answer = self.hook('claude', probes=probes, wakes=wakes)
        self.assertEqual((code, answer), (0, out))
        self.assertEqual(len(probes), 1)
        self.assertLessEqual(probes[0], importlib.import_module('laya_bridge_observer').HOOK_RECEIPT_SECONDS)
        self.assertEqual(len(self.challenges()), 1)
        claude_wakes = len(wakes)
        # The same host again while its receipt is fresh: no probe, no write.
        self.hook('claude', probes=probes)
        self.assertEqual((len(probes), len(self.challenges())), (1, 1))
        event = None
        with self.driver():
            event = self.run_pass()
        self.assertEqual((event['outcome'], event['reason'], event['migration_state']),
                         ('waiting', 'switch_bridge_missing', 'shadow_verified'))
        # Codex joins the same challenge and offers one sync wake.
        wakes[:] = []
        code, codex = self.hook('codex', probes=probes, wakes=wakes)
        self.assertEqual(code, 0)
        self.assertEqual((len(probes), len(self.challenges())), (2, 1))
        self.assertEqual(wakes.count(('laya-sync', str(case.project))) - claude_wakes, 1)
        with self.driver():
            first, second = self.run_pass(), self.run_pass()
        self.assertEqual([(e['outcome'], e['migration_state']) for e in (first, second)],
                         [('progressed', 'reader_ready'), ('progressed', 'switched')])
        self.assertEqual(self.selection()[:3], (initial[0] + 1, self.intent()['switched']['endpoint_id'], initial[1]))
        self.assertEqual(self.store_format(), 'schema3')
        # Past the switch the hook records nothing more.
        self.hook('codex', probes=probes)
        self.assertEqual(len(probes), 2)
        emit('switch-hook-receipts', challenges=len(self.challenges()), probe_budget=probes)


    # ---- U15c: receipts hours apart, epoch-bound, gate waits are not errors --
    def audits(self, name):
        directory = self.bridge._registry() / name
        return {host: json.loads((directory / (host + '.audit')).read_text(encoding='utf8'))
                for host in ('claude', 'codex') if (directory / (host + '.audit')).exists()}

    def logs(self, *args):
        done = subprocess.run([sys.executable, '-B', str(ROOT / 'skills/logs/scripts/logs.py'),
                               '--state-root', str(self.case.state), *args],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def republish(self, card, marker):
        """An actual v6 publish of one edited card; the shadow copy then catches up to it."""
        case = self.case
        before = self.u11.v6_generation()
        card.write_text(card.read_text(encoding='utf8').replace('\n---\n', '\n---\n' + marker + '\n', 1),
                        encoding='utf8')
        roots = {case.WIKI: str(case.auto / 'wiki')}
        with self.u10.seed.authority():
            with case.runtime(roots) as runtime:
                runtime.catalog.request_scan()
            case.publish_all(roots)
            case.rebind_fixture_ledger(roots)
        self.assertNotEqual(self.u11.v6_generation(), before)
        verified, passes = self.intent(), []
        with self.driver():
            for _ in range(30):
                passes.append(self.run_pass())
                now = self.intent()
                if (now['migration_state'] == 'shadow_verified'
                        and now['observation'] != verified['observation']):
                    return passes
        self.fail('catch-up not reached: %s' % passes)

    def described(self):
        """Challenge contents for an assertion message (no card text)."""
        value = {}
        for name in self.challenges():
            loaded = json.loads((self.bridge._registry() / name / 'loaded.json').read_text(encoding='utf8'))
            value[name] = dict(loaded=loaded, **{host: {key: audit.get(key) for key in (
                'endpoint_id', 'reader_epoch', 'generation', 'sentinel_sha256', 'manifest_sha256')}
                for host, audit in self.audits(name).items()})
        return json.dumps(value, sort_keys=True)

    def test_08_hook_receipts_hours_apart_switch_while_the_selection_holds(self):
        case = self.case
        initial = self.selection()
        self.challenges()
        with self.driver():
            self.drive_to('shadow_verified')
        probes, wakes = [], []
        self.hook('claude', probes=probes, wakes=wakes)
        (first,) = self.challenges()
        claude_first = self.audits(first)['claude']
        loaded = json.loads((self.bridge._registry() / first / 'loaded.json').read_text(encoding='utf8'))
        wiki = case.auto / 'wiki'
        sentinel = next(path for path in sorted(wiki.rglob('*.md'))
                        if loaded['sentinel_path'].endswith('/' + path.relative_to(wiki).as_posix()))
        other = next(path for path in sorted(wiki.rglob('*.md')) if path != sentinel)
        # The sentinel card changes: Codex an hour later cannot join Claude's
        # challenge and issues its own; no pair, no wake.
        passes = self.republish(sentinel, 'Sentinel later marker.')
        real_time = time.time

        def clock(hours):
            return mock.Mock(time=lambda: real_time() + hours * 3600, monotonic=time.monotonic)
        wakes[:] = []
        with mock.patch.object(self.bridge, 'time', clock(1)):
            self.hook('codex', probes=probes, wakes=wakes)
        self.assertEqual(len(self.challenges()), 2, self.described())
        (second,) = [name for name in self.challenges() if name != first]
        self.assertEqual((list(self.audits(second)), wakes), (['codex'], []))
        # A switch gate that does not hold yet is a wait, not an error.
        waits = [e for e in self.events() if e['event'] == 'migration.shadow'
                 and e.get('reason') == 'switch_bridge_missing']
        self.assertTrue(waits)
        self.assertEqual({e['outcome'] for e in waits}, {'waiting'})
        line = [row for row in self.logs().splitlines() if row.startswith('migration: ')]
        self.assertEqual(len(line), 1)
        self.assertIn('gate_waits=%d' % len(waits), line[0])
        # Only actual step errors stay errors; no switch gate (nor the gate's
        # catch-up recheck seeing the moved source) is among them.
        errors = [(e.get('op'), e.get('reason')) for e in self.events()
                  if e['event'] == 'migration.shadow' and e['outcome'] == 'error']
        self.assertFalse([e for e in errors if e[0] in ('reader_ready', 'switch')
                          or str(e[1]).startswith('switch_')], errors)
        self.assertIn('errors=%d' % len(errors), line[0])
        self.assertNotIn('switch_bridge_missing', self.logs('errors'))
        # Another card changes (the v6 generation moves again, the sentinel
        # stays). Claude three hours later probes again because Codex's newer
        # challenge waits for it, and joins it although the generations differ.
        passes += self.republish(other, 'Kestrel later marker.')
        wakes[:] = []
        before = len(probes)
        with mock.patch.object(self.bridge, 'time', clock(3)):
            self.hook('claude', probes=probes, wakes=wakes)
            self.assertEqual(len(probes), before + 1)
            self.assertEqual(sorted(self.audits(second)), ['claude', 'codex'], self.described())
            pair = self.audits(second)
            self.assertNotEqual(pair['claude']['generation'], pair['codex']['generation'])
            self.assertEqual((pair['claude']['reader_epoch'], pair['claude']['endpoint_id']),
                             (claude_first['reader_epoch'], claude_first['endpoint_id']))
            self.assertEqual(wakes.count(('laya-sync', str(case.project))), 1)
            # Both hosts now stand: neither probes again.
            self.hook('claude', probes=probes)
            self.hook('codex', probes=probes)
            self.assertEqual(len(probes), before + 1)
            # The journal epoch moves: the pair is not evidence; nothing is written.
            real_selection = self.reader.read_selection

            def epoch_moved(path):
                value = real_selection(path)
                return SimpleNamespace(epoch=value.epoch + 1, preferred_endpoint=value.preferred_endpoint,
                                       previous_endpoint=value.previous_endpoint,
                                       transition_id=value.transition_id)
            held = self.intent()
            with self.driver(), mock.patch.object(self.reader, 'read_selection', side_effect=epoch_moved):
                result = self.ready()
            self.assertEqual((result['stage'], result.get('reason')), ('retry', 'switch_bridge_missing'))
            self.assertEqual((self.intent(), self.selection()), (held, initial))
            with self.driver():
                ready_pass, switch_pass = self.run_pass(), self.run_pass()
        self.assertEqual([(e['outcome'], e['migration_state']) for e in (ready_pass, switch_pass)],
                         [('progressed', 'reader_ready'), ('progressed', 'switched')])
        self.assertEqual(self.store_format(), 'schema3')
        # A day after its issue the challenge is no evidence (hook TTL upper bound).
        with mock.patch.object(self.bridge, 'time', clock(1 + 24)):
            with self.assertRaises(ValueError):
                self.bridge.find_loaded_bridge(case.state, release_commit=claude_first['release_commit'])
        emit('switch-hook-receipts-hours-apart',
             generations=[pair['codex']['generation'], pair['claude']['generation']],
             challenges=len(self.challenges()), passes=len(passes), gate_waits=len(waits), step_errors=errors)

if __name__ == '__main__':
    unittest.main(verbosity=2)

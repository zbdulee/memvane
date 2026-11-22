"""1.0.19 selector budget through the real hook entries (plan §6 items 16-21, 23, 24).

``hooks/run-hook recall|posttool claude|codex`` runs in a child under the
interpreter of this test (a ``python3`` shim on ``PATH``). The child reaches
the real ``verified_rows`` -> ``select_published`` -> ``live_select.choose``
-> ``inference_service.predict`` client. Only the prepared-selector proofs
(selector preparation, private state, adapter binding and attestation,
inference identity admission, bundle signature) are replaced, by a
``sitecustomize`` that calls ``child_patches`` in the ``hook-serve`` process.

This process hosts the real ``inference_service._serve`` socket server and
its real request validation (``_validate_request``, with identity and
adapter binding replaced) and ``GenerationEngine``. The engine's forward is
the real ``inference_service._forward`` -> ``adapter.predict_loaded`` with the
codepoint tokenizer and a table model of the pairing tests: no ML runs. A
gated forward holds a request until the test releases it; assertions use
counts, states and markers, never elapsed time.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from memvane.host.tests.support import CARD_BODY, PROMPT, HostInstallCase
from memvane.host.tests.integration.test_edit_recall import EDIT_TEXT, NEW_FILE, edit_payload
from tests.support.install_builder import ROOT

SIGNATURE = 'mv119-fixture-signature'
_MISSING = object()
SITECUSTOMIZE = '''\
import os, sys
if os.environ.get('MV119_CHILD') and len(sys.argv) > 1 and sys.argv[1] == 'hook-serve':
    try:
        from memvane.host.tests.integration.test_selector_budget_119 import child_patches
        child_patches(os.environ['MV119_CHILD'])
    except BaseException as exc:
        sys.stderr.write('mv119 child patches failed: %r\\n' % (exc,))
        raise
'''


def _append(path, row):
    data = (json.dumps(row, sort_keys=True) + '\n').encode('utf-8')
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def child_patches(raw):
    """Prepared-selector proofs of the ``hook-serve`` child; records markers.

    Everything else (pool, selector input, client, socket, timing, events)
    is the product's own code.
    """
    config = json.loads(raw)
    from memvane.host import capture_dispatch
    from memvane.learning import api as learning_api
    from memvane.learning.context_learning import (
        adapter_binding, candidate_apply, inference_identity, inference_service, live_select, seam)
    state, log = Path(config['state']), config['log']

    def record(**row):
        _append(log, {'pid': os.getpid(), **row})
    patches = [
        mock.patch.object(learning_api, 'selector_prepared', return_value=True),
        mock.patch.object(seam, '_private_state', side_effect=lambda *_a, **_k: state),
        mock.patch.object(adapter_binding, 'inference_argv',
                          side_effect=lambda argv, *_a, **_k: list(argv)),
        mock.patch.object(adapter_binding, 'attested_adapter', return_value='0' * 64),
        mock.patch.object(candidate_apply, 'readable', return_value=None),
        mock.patch.object(inference_identity, 'admit', return_value={'fixture': True}),
        mock.patch.object(inference_service, '_signature', return_value=SIGNATURE),
    ]
    real_choose = live_select.choose

    def choose(state_dir, prompt, rows, **options):
        record(event='choose', lane=options.get('lane'), pair_budget=options.get('pair_budget'),
               skip_busy=bool(options.get('skip_busy')), rows=len(rows), prompt_chars=len(prompt),
               prompt_sha256=hashlib.sha256(prompt.encode('utf-8')).hexdigest())
        if config.get('watchdog'):
            # The hook budget's own watchdog: SIGALRM raises HookDeadlineExceeded.
            from memvane.kernel import hook_budget
            hook_budget.arm(watchdog=True, export=False)
            record(event='armed')
        try:
            ids, reason, resources = real_choose(state_dir, prompt, rows, **options)
        except BaseException as exc:
            record(event='choose_raised', kind=type(exc).__name__)
            raise
        record(event='chosen', reason=reason, selected=None if ids is None else len(ids),
               resources={key: value for key, value in (resources or {}).items()
                          if value is None or type(value) in (int, float, str, bool)})
        return ids, reason, resources
    patches.append(mock.patch.object(live_select, 'choose', choose))
    real_dispatch = capture_dispatch.dispatch

    def dispatch(slot, started):
        for entry in slot:
            record(event='capture', pool=len(entry[4]), selected=len(entry[5]))
        return real_dispatch(slot, started)
    patches.append(mock.patch.object(capture_dispatch, 'dispatch', dispatch))
    for patcher in patches:
        patcher.start()


class _Identity:
    last_timing = {'identity_seconds': 0.0, 'identity_reused': 1}

    def __init__(self):
        self.reuse = []

    def verify(self, _state, _argv, _view, *, reuse=False):
        self.reuse.append(reuse)
        return {'runtime_identity_sha256': 'r' * 64, 'base_model_sha256': 'b' * 64}


class Service:
    """The real inference socket server on the test runtime root, in a thread."""

    def __init__(self, case, *, gate=False, probability=.9):
        from memvane.learning.context_learning import (
            adapter, adapter_binding, candidate_apply, inference_service as service, resident)
        from memvane.learning.tests.unit.test_context_learning_prediction_pairing import (
            TOK, _stub_laya)
        from memvane.learning.tests.unit.test_context_learning_chunked_pairing import (
            EDIT, FAKE_TORCH, TableModel)
        self.service = service
        self.identity = _Identity()
        self.forwards = []
        self.closed_seen = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        if not gate:
            self.release.set()
        self.lock = threading.Lock()
        model = TableModel({}, default=probability)
        real_cancelled = resident._cancelled

        def cancelled(conn, deadline):
            try:
                eof = bool(select.select([conn], [], [], 0)[0]) and not conn.recv(1, socket.MSG_PEEK)
            except OSError:
                eof = True
            if eof:
                with self.lock:
                    self.closed_seen += 1
            return real_cancelled(conn, deadline)

        def forward(loaded, job, *, limits, timing, cancelled):
            request = job['request']
            case_ = request['cases'][0]
            entry = {'lane': request.get('query_lane'), 'pair_budget': request.get('pair_budget'),
                     'candidates': len(case_['candidates']), 'prompt_chars': len(case_['prompt'])}
            with self.lock:
                self.forwards.append(entry)
            self.entered.set()
            if not self.release.wait(120):
                raise ValueError('fixture_gate_not_released')
            entry['cancelled'] = bool(cancelled())
            return service._forward(loaded, job, limits=limits, timing=timing, cancelled=cancelled)

        # Only these module keys are swapped: restoring all of sys.modules
        # would drop modules first imported during the test.
        stubs = {**_stub_laya(), 'torch': FAKE_TORCH}
        saved = {key: sys.modules.get(key, _MISSING) for key in stubs}
        sys.modules.update(stubs)

        def restore_modules():
            for key, value in saved.items():
                if value is _MISSING:
                    sys.modules.pop(key, None)
                else:
                    sys.modules[key] = value
        case.addCleanup(restore_modules)
        # adapter._torch() exports runtime thread variables into os.environ.
        for patcher in (
                mock.patch.dict(os.environ),
                mock.patch.object(service, '_signature', return_value=SIGNATURE),
                mock.patch.object(service, 'has_evidence', return_value=True),
                mock.patch.object(service, '_IDENTITY_CACHE', self.identity),
                mock.patch.object(adapter_binding, 'inference_argv',
                                  side_effect=lambda argv, *_a, **_k: list(argv)),
                mock.patch.object(adapter_binding, 'attested_adapter', return_value='0' * 64),
                mock.patch.object(candidate_apply, 'readable', return_value=None),
                mock.patch.object(adapter, '_batch', side_effect=lambda group, *_: {'items': group}),
                mock.patch.object(resident, '_cancelled', cancelled)):
            patcher.start()
            case.addCleanup(patcher.stop)
        engine = service.GenerationEngine(
            load=lambda _job: (model, TOK, dict(EDIT)), forward=forward,
            admission=lambda *_a, **_k: ({'max_batch_size': 4, 'max_padded_tokens': 8192},
                                         {'available_mib': 4096, 'ready': True}),
            rss=lambda: 100)
        self.root = service.service_root(create=True)
        ready, self.stop = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=service._serve, args=(self.root,),
                                       kwargs={'engine': engine, 'ready': ready, 'stop': self.stop},
                                       daemon=True)
        self.thread.start()
        case.addCleanup(self.close)
        case.assertTrue(ready.wait(30), 'fixture service did not start')

    def close(self):
        self.release.set()
        self.stop.set()
        self.thread.join(30)

    def version(self):
        return self.service._exchange(self.root, {'command': 'version'}, time.monotonic() + 5)

    def lanes(self):
        with self.lock:
            return [entry['lane'] for entry in self.forwards]


class SelectorEntry(HostInstallCase):
    """A prepared selector in a private state directory; hooks run as children."""

    def setUp(self):
        super().setUp()
        from memvane.learning.context_learning.adapter import EDIT_INPUT_VIEW
        self.state = self.tmp / 'sel'
        self.state.mkdir(mode=0o700)
        artifact = self.state / 'head.bin'
        self._private(artifact, b'fixture head')
        self._private(self.state / 'live-selector.json', json.dumps({
            'schema': 'qmd-live-selector-v1',
            'trainerArgv': [sys.executable, str(self.state / 'adapter.py'), str(self.state / 'model')],
            'timeoutSeconds': 30, 'maxRssMib': 4096}).encode())
        self._private(self.state / 'active-checkpoint.json', json.dumps({
            'schema_version': 1, 'artifact_path': str(artifact),
            'artifact_sha256': hashlib.sha256(b'fixture head').hexdigest(),
            'model_input_view': EDIT_INPUT_VIEW, 'cycle_id': 'fixture-cycle'}).encode())
        self.log = self.tmp / 'child-log.jsonl'
        stub = self.tmp / 'stub'
        stub.mkdir(mode=0o700)
        (stub / 'sitecustomize.py').write_text(SITECUSTOMIZE, encoding='utf-8')
        shim = self.tmp / 'bin'
        shim.mkdir(mode=0o700)
        python = shim / 'python3'
        python.write_text('#!/bin/sh\nexec %s "$@"\n' % json.dumps(sys.executable), encoding='utf-8')
        python.chmod(0o700)
        self.child_env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                          'PATH': '%s:%s' % (shim, os.environ.get('PATH', '')),
                          'PYTHONPATH': '%s:%s' % (stub, ROOT)}
        for key in ('QMD_SANDBOX', 'CLAUDE_SANDBOX', 'CLAUDE_HEADLESS', 'CODEX_SANDBOX',
                    'CLAUDE_PLUGIN_ROOT', 'PLUGIN_ROOT', 'QMD_HOOK_DEADLINE_MONO'):
            self.child_env.pop(key, None)
        self.fixture.write_note(NEW_FILE, EDIT_TEXT)

    @staticmethod
    def _private(path, data):
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as target:
            target.write(data)

    def start(self, action, host, payload, *, watchdog=False, env=None):
        config = {'state': str(self.state), 'log': str(self.log), 'watchdog': watchdog}
        child = subprocess.Popen(['bash', str(ROOT / 'hooks' / 'run-hook'), action, host],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, cwd=str(self.project),
                                 env={**self.child_env, 'MV119_CHILD': json.dumps(config),
                                      **(env or {})})
        child.stdin.write(json.dumps({'cwd': str(self.project), **payload}).encode('utf-8'))
        child.stdin.close()
        child.stdin = None
        return child

    def finish(self, child):
        out, err = child.communicate(timeout=120)
        return child.returncode, out.decode('utf-8'), err.decode('utf-8')

    def run_hook(self, action, host, payload, **options):
        return self.finish(self.start(action, host, payload, **options))

    def child_log(self, event=None, pid=None):
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text(encoding='utf-8').splitlines()]
        return [row for row in rows if (event is None or row['event'] == event)
                and (pid is None or row['pid'] == pid)]

    def rows(self, event, session):
        return [row for row in self.install.events()
                if row.get('event') == event and row.get('session') == session]

    def wait_entered(self, service, child):
        self.wait_until(lambda: service.entered.is_set() or child.poll() is not None, timeout=120)
        if not service.entered.is_set():
            code, out, err = self.finish(child)
            self.fail('no request reached the service: %r %r %r %r'
                      % (code, out, err, self.child_log()))

    @staticmethod
    def context(out):
        return json.loads(out)['hookSpecificOutput']['additionalContext'] if out.strip() else ''

    def prompt(self, session, text=PROMPT):
        return {'prompt': text, 'session_id': session, 'hook_event_name': 'UserPromptSubmit'}


class PromptLane(SelectorEntry):
    def test_a_20000_character_prompt_scores_four_query_chunks_with_numeric_fields(self):
        # Items 17 and 23.
        service = Service(self)
        text = (PROMPT + ' ') * (20000 // (len(PROMPT) + 1) + 1)
        text = text[:20000]
        for host in ('claude', 'codex'):
            with self.subTest(host=host):
                session = 'long-' + host
                code, out, err = self.run_hook('recall', host, self.prompt(session, text))
                self.assertEqual(code, 0, err)
                [row] = self.rows('recall', session)
                self.assertIn(row['selection'], ('selected', 'ranked'))
                self.assertEqual(row['selection'], 'selected', row)
                self.assertIn('zqheronx', self.context(out))
                self.assertEqual((row['selectorLane'], row['selectorCandidates']), ('prompt', 1))
                # The worker's reuse mark reaches the recall event.
                self.assertEqual(row['selectorIdentityReused'], 1)
                self.assertLessEqual(row['selectorQueryChunks'], 4)
                self.assertEqual(row['selectorQueryChunks'], 4)
                self.assertGreater(row['selectorDroppedQuery'], 0)
                self.assertNotIn('input_budget_exceeded', json.dumps(row))
                chosen = self.child_log('chosen')[-1]
                self.assertEqual(chosen['reason'], 'selected')
                self.assert_numeric_measures(row, chosen['resources'])
        self.assertEqual([(f['lane'], f['pair_budget'], f['prompt_chars']) for f in service.forwards],
                         [('prompt', 12, 20000)] * 2)
        # Every validation of a recall (lane) request asks for the reuse window.
        self.assertTrue(service.identity.reuse)
        self.assertEqual(set(service.identity.reuse), {True})

    def assert_numeric_measures(self, row, resources):
        from memvane.recall import verified_rows
        names = [name for _key, name in verified_rows._SELECTOR_MEASURES]
        self.assertFalse([name for name in names if 'token' in name.lower()])
        self.assertFalse([key for key in row if 'token' in key.lower()], row)
        delivered = [(key, name) for key, name in verified_rows._SELECTOR_MEASURES
                     if type(resources.get(key)) in (int, float) and resources[key] >= 0]
        # The adapter (M-3), the service (validation, queue, load) and the
        # client (wall, envelope, identity, argv) each delivered measures.
        for key in ('query_chunks', 'pair_len_max', 'padded_total', 'tokenize_seconds',
                    'queue_wait_seconds', 'initial_validation_seconds', 'model_load_seconds',
                    'resident_wall_seconds', 'envelope_seconds', 'client_identity_seconds',
                    'argv_seconds', 'selector_wall_seconds'):
            self.assertIn(key, dict(delivered), resources)
        for key, name in delivered:
            self.assertIn(name, row)
            self.assertIn(type(row[name]), (int, float), name)
            # The event log keeps a float to three decimals.
            expected = resources[key]
            self.assertEqual(row[name], round(expected, 3) if type(expected) is float else expected,
                             name)
        selector = {key: value for key, value in row.items() if key.startswith('selector')}
        strings = {key for key, value in selector.items() if not type(value) in (int, float)}
        self.assertLessEqual(strings, {'selectorLane', 'selectorDevice'}, selector)

    def test_a_stalled_prompt_selector_serves_the_ranked_rows(self):
        # Item 18: the service holds the request past the lane budget.
        service = Service(self, gate=True)
        code, out, err = self.run_hook('recall', 'claude', self.prompt('stalled'))
        self.assertEqual(code, 0, err)
        self.assertIn('zqheronx', self.context(out))
        [row] = self.rows('recall', 'stalled')
        self.assertEqual((row['outcome'], row['selection']), ('served', 'ranked'))
        self.assertEqual(row['selectorLane'], 'prompt')
        self.assertLessEqual(row['selectorBudgetMs'], 3000)
        self.assertTrue([note for note in row['notes'] if note.endswith('deadline_expired')], row)
        self.assertEqual([r['reason'] for r in self.child_log('chosen')],
                         ['resident_service_deadline_expired'])
        # Released after the hook ended: the request is cancelled, not scored.
        service.release.set()
        self.wait_until(lambda: 'cancelled' in service.forwards[0], timeout=60)
        self.assertEqual((service.lanes(), service.forwards[0]['cancelled']), (['prompt'], True))
        self.assertGreaterEqual(service.closed_seen, 1)

    def test_hook_budget_exhaustion_during_the_selector_exits_zero_and_cancels(self):
        # Item 20: HookDeadlineExceeded while the request is in the service.
        service = Service(self, gate=True)
        child = self.start('recall', 'claude', self.prompt('exhausted'), watchdog=True,
                           env={'QMD_HOOK_TOTAL_SECONDS': '20'})
        self.wait_entered(service, child)
        self.assertEqual(len(self.child_log('armed')), 1)
        # The watchdog's own signal, early: the budget ends while waiting.
        os.kill(child.pid, signal.SIGALRM)
        code, out, err = self.finish(child)
        self.assertEqual((code, out), (0, ''), err)
        self.assertIn('hook_budget_exhausted', err)
        self.assertEqual([r['kind'] for r in self.child_log('choose_raised')],
                         ['HookDeadlineExceeded'])
        [row] = self.rows('recall', 'exhausted')
        self.assertEqual((row['outcome'], row['selectorLane']), ('interrupted', 'prompt'))
        service.release.set()
        self.wait_until(lambda: 'cancelled' in service.forwards[0], timeout=60)
        self.assertTrue(service.forwards[0]['cancelled'])
        self.assertGreaterEqual(service.closed_seen, 1)
        # The service serves the next request: nothing of the dead one remains.
        code, out, err = self.run_hook('recall', 'claude', self.prompt('after'))
        self.assertEqual(code, 0, err)
        [row] = self.rows('recall', 'after')
        self.assertEqual(row['selection'], 'selected')
        self.assertEqual(service.lanes(), ['prompt', 'prompt'])

    def test_a_busy_edit_request_never_makes_a_prompt_recall_wait(self):
        # Item 21: an edit recall that finds the service busy queues nothing.
        service = Service(self, gate=True)
        first = self.start('recall', 'claude', self.prompt('busy-first'))
        self.wait_entered(service, first)
        payload = edit_payload('busy-edit')
        code, out, err = self.run_hook('posttool', 'claude', payload)
        # The edit hook ended while the service still holds the first request.
        self.assertFalse(service.release.is_set())
        self.assertEqual(code, 0, err)
        self.assertIn('zqheronx', self.context(out))
        [edit] = self.rows('recall.edit', 'busy-edit')
        self.assertEqual((edit['selection'], len(edit['cards'])), ('default_rank', 1))
        self.assertIn('laya_selector_service_busy', edit['notes'])
        self.assertEqual(service.lanes(), ['prompt'])
        second = self.start('recall', 'codex', self.prompt('busy-second'))
        # Queued behind the first request only: no edit request is ahead of it.
        self.wait_until(lambda: service.version()['queued'] == 1 or second.poll() is not None,
                        timeout=120)
        self.assertEqual(service.version()['queued'], 1, self.child_log())
        service.release.set()
        code, out, err = self.finish(second)
        self.assertEqual(code, 0, err)
        [row] = self.rows('recall', 'busy-second')
        self.assertEqual(row['selection'], 'selected', row)
        self.finish(first)
        self.assertEqual(service.lanes(), ['prompt', 'prompt'])
        self.assertEqual([(r['lane'], r['skip_busy']) for r in self.child_log('choose')],
                         [('prompt', False), ('edit', True), ('prompt', False)])


class EditLane(SelectorEntry):
    def test_an_edit_over_2000_characters_reaches_the_selector_whole(self):
        # Item 19 (first half): the 2,000-character cut is gone.
        Service(self)
        text = EDIT_TEXT * 12
        self.assertGreater(len(text), 2000)
        self.fixture.write_note(NEW_FILE, text)
        for host, tool in (('claude', 'Write'), ('codex', 'apply_patch')):
            with self.subTest(host=host):
                session = 'whole-' + host
                code, out, err = self.run_hook('posttool', host, edit_payload(session, tool, text=text))
                self.assertEqual(code, 0, err)
                [row] = self.rows('recall.edit', session)
                self.assertEqual(row['selection'], 'selector', row)
                [choose] = [r for r in self.child_log('choose') if r['lane'] == 'edit'][-1:]
                self.assertEqual((choose['pair_budget'], choose['skip_busy']), (91, True))
                self.assertGreater(choose['prompt_chars'], 2000)
                self.assertEqual(choose['prompt_chars'], row['queryChars'])
        # Write sends the content as written; apply_patch its added lines.
        query = NEW_FILE + '\n' + text
        self.assertEqual(self.child_log('choose')[0]['prompt_sha256'],
                         hashlib.sha256(query.encode('utf-8')).hexdigest())

    def test_an_edit_selector_past_its_budget_serves_one_default_rank_card(self):
        # Item 19 (second half).
        service = Service(self, gate=True)
        code, out, err = self.run_hook('posttool', 'claude', edit_payload('edit-stall'))
        self.assertEqual(code, 0, err)
        self.assertIn('zqheronx', self.context(out))
        [row] = self.rows('recall.edit', 'edit-stall')
        self.assertEqual((row['outcome'], row['selection'], len(row['cards'])),
                         ('served', 'default_rank', 1))
        self.assertEqual(row['selectorLane'], 'edit')
        self.assertEqual([r['reason'] for r in self.child_log('chosen')],
                         ['resident_service_deadline_expired'])
        service.release.set()
        self.wait_until(lambda: 'cancelled' in service.forwards[0], timeout=60)
        self.assertEqual((service.lanes(), service.forwards[0]['cancelled']), (['edit'], True))


class FifteenCardPool(SelectorEntry):
    def setUp(self):
        super().setUp()
        source = self.project / 'notes' / 'heron.md'
        for index in range(18):
            self.fixture.write_card('survey-%02d.md' % index, title='Heron survey %02d' % index,
                                    sources=[source],
                                    body=CARD_BODY.strip() + ' Survey copy %02d.\n' % index)
        self.fixture.commit('eighteen cards')
        self.install.settle()

    def test_the_selector_scores_ten_and_the_capture_keeps_fifteen(self):
        # Item 16.
        service = Service(self)
        code, out, err = self.run_hook('posttool', 'claude', edit_payload('pool'))
        self.assertEqual(code, 0, err)
        self.assertIn('zqheronx', self.context(out))
        [row] = self.rows('recall.edit', 'pool')
        self.assertEqual((row['pool'], row['selectorCandidates']), (15, 10), row)
        self.assertEqual([r['rows'] for r in self.child_log('choose')], [10])
        self.assertEqual([f['candidates'] for f in service.forwards], [10])
        self.assertEqual([(r['pool'], r['selected']) for r in self.child_log('capture')],
                         [(15, len(row['selected']))])


class SelectorPrompt(unittest.TestCase):
    def test_an_over_limit_prompt_keeps_its_head_and_tail_within_the_byte_limit(self):
        from memvane.recall import verified_rows
        prompt = '머리 ' + '가나다라마바사 ' * 9000 + ' 꼬리 요청'
        self.assertGreater(len(prompt.encode('utf-8')), verified_rows.SELECTOR_PROMPT_BYTES)
        kept = verified_rows._selector_prompt(prompt)
        self.assertLessEqual(len(kept.encode('utf-8')), verified_rows.SELECTOR_PROMPT_BYTES)
        self.assertTrue(kept.startswith(prompt[:1000]))
        self.assertTrue(kept.endswith(prompt[-1000:]))
        self.assertIs(verified_rows._selector_prompt(PROMPT), PROMPT)

    def test_the_prompt_budget_is_at_most_three_seconds(self):
        from memvane.recall import verified_rows
        for timeout, seconds in ((5, 3.0), (1, 1.0), (60, 3.0), ('x', 3.0), (0, .2)):
            self.assertEqual(verified_rows._prompt_selector_seconds({'queryTimeout': timeout}),
                             seconds)


class SystemPythonImports(unittest.TestCase):
    MODULES = ('memvane.learning.context_learning.pairing',
               'memvane.learning.context_learning.adapter',
               'memvane.learning.context_learning.inference_identity',
               'memvane.learning.context_learning.inference_service',
               'memvane.learning.context_learning.inference_timing',
               'memvane.learning.context_learning.live_select',
               'memvane.learning.context_learning.selection_read',
               'memvane.recall.verified_rows',
               'memvane.learning.context_learning.edit_git_migration')

    def test_changed_modules_import_under_the_system_python(self):
        # Item 24: hooks run bare python3; macOS ships 3.9 as /usr/bin/python3.
        python = '/usr/bin/python3'
        if not os.access(python, os.X_OK):
            self.skipTest('no system python3')
        script = ('import importlib, sys\n'
                  'for name in sys.argv[1:]:\n'
                  '    importlib.import_module(name)\n'
                  'print(sys.version_info[0], sys.version_info[1])\n')
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(ROOT)}
        done = subprocess.run([python, '-B', '-c', script, *self.MODULES], capture_output=True,
                              text=True, timeout=120, env=env, cwd=str(ROOT))
        self.assertEqual(done.returncode, 0, done.stderr)
        major, minor = map(int, done.stdout.split())
        self.assertEqual(major, 3)
        self.assertGreaterEqual(minor, 9)


if __name__ == '__main__':
    unittest.main()

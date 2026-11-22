"""1.0.19 selector latency: request keys, cancellation, closed timing and identity cost.

No ML runs. The hosted worker scores with the real ``adapter.predict_loaded``
and edit-view pair plan over a codepoint tokenizer, a stub ``laya.common`` and
a fake model that records every forward; assertions count forwards, sweeps,
hashes and reply fields, never wall-clock time.
"""
import contextlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

from memvane.learning.tests.unit.test_context_learning_prediction_pairing import (
    TOK, _stub_laya, card)
from memvane.learning.tests.unit.test_context_learning_chunked_pairing import FAKE_TORCH, FakeLogits
from memvane.learning.tests.integration.test_context_learning_inference_service import (
    admit, exchange, shutdown_idle)
from memvane.learning.context_learning import (
    adapter, inference_identity as identity, inference_score, inference_service as service,
    inference_timing, live_select, local_cycle)
from memvane.learning.context_learning.contracts import digest
from memvane.learning.context_learning.store import canonical

EDIT = {'max_len': adapter.EDIT_INPUT_TOKENS, 'head_max_len': 256,
        'model_input_view': adapter.EDIT_INPUT_VIEW}
# Keys ``adapter.predict_loaded`` measures that are not part of the closed set.
OPEN_KEYS = ('padded_total', 'query_chunks', 'dropped_pairs', 'dropped_query', 'pair_len_p50',
             'pair_len_p95', 'pair_len_max', 'planned_batches', 'truncated_pairs',
             'total_seconds', 'max_batch_size', 'max_padded_tokens', 'device')


def wait_until(predicate, what, seconds=3):
    until = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= until:
            raise AssertionError('fixture state not reached: ' + what)
        time.sleep(.01)


def edit_request(tag, cards, **extra):
    case = {'request_id': tag, 'prompt': 'ESP32 배선?', 'input_sha256': 'a' * 64,
            'candidates': [card('wiki/%s-%d.md' % (tag, i), '# %s %d\n본문.' % (tag, i))
                           for i in range(cards)]}
    return {'schema_version': 1, 'model_input_view': adapter.EDIT_INPUT_VIEW,
            'cases': [case], **extra}


def wire_envelope(tag, seconds=10):
    now = time.time()
    return {'tag': tag, 'namespace': 'one', 'queued_at_epoch': now, 'deadline_epoch': now + seconds}


class RecordingModel:
    """Records the request of every forward; can hold one request's first batch."""

    def __init__(self):
        self.calls = []
        self.hold = None  # (request_id, entered Event, release Event)

    def parameters(self):
        return iter([types.SimpleNamespace(device='cpu')])

    def __call__(self, items):
        tag = items[0]['request_id']
        self.calls.append(tag)
        hold = self.hold
        if hold is not None and hold[0] == tag and not hold[1].is_set():
            hold[1].set()
            hold[2].wait(5)
        return FakeLogits([.2] * len(items)), None

    def count(self, tag):
        return self.calls.count(tag)


class ServedAdapterBase(unittest.TestCase):
    """The in-process ``_serve`` with an injected engine, scoring through the real adapter."""

    def serve(self, requests):
        temp = tempfile.TemporaryDirectory(prefix='mv-s-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.root.chmod(0o700)
        for key, value in _stub_laya().items():
            # Single-key set/restore: the scoring thread imports laya.common.
            previous = sys.modules.get(key)
            sys.modules[key] = value
            self.addCleanup(lambda k=key, p=previous: sys.modules.__setitem__(k, p)
                            if p is not None else sys.modules.pop(k, None))
        self.model = RecordingModel()
        self.rows = []
        self.loads = []
        real_infer = inference_score.infer

        def infer(engine, coordinator, validate, envelope, *args, **kwargs):
            row = real_infer(engine, coordinator, validate, envelope, *args, **kwargs)
            self.rows.append((envelope.get('tag'), row))
            return row
        for target in (patch.object(adapter, '_torch', return_value=FAKE_TORCH),
                       patch.object(adapter, '_batch', side_effect=lambda group, *_: {'items': group}),
                       patch.object(inference_score, 'infer', infer)):
            target.start()
            self.addCleanup(target.stop)

        def load(job):
            self.loads.append(job['generation'])
            return (self.model, TOK, EDIT)

        def validate(env):
            until, _ = service.resident._request_window(
                env, now_epoch=time.time(), now_monotonic=time.monotonic())
            return {'family': ('runtime', 'base', 'view'),
                    'generation': ('runtime', 'base', 'view', 'a'), 'namespace': 'one',
                    'policy': {'maxRssMib': 1024}, 'request': requests[env['tag']],
                    'deadline': until}
        engine = service.GenerationEngine(load=load, admission=admit, rss=lambda: 100)
        ready = threading.Event()
        thread = threading.Thread(target=service._serve, args=(self.root,), daemon=True,
                                  kwargs={'engine': engine, 'validate': validate, 'ready': ready})
        thread.start()
        self.assertTrue(ready.wait(3))

        def stop():
            if self.model.hold is not None:
                self.model.hold[2].set()
            try:
                shutdown_idle(self.root)
            finally:
                thread.join(3)
            self.assertFalse(thread.is_alive())
        self.addCleanup(stop)

    def connect(self, tag):
        conn = socket.socket(socket.AF_UNIX)
        self.addCleanup(conn.close)
        conn.settimeout(5)
        conn.connect(str(self.root / service.SOCKET_NAME))
        service.resident._write(conn, wire_envelope(tag))
        return conn

    def queued(self):
        return exchange(self.root, {'command': 'version'})['queued']

    def hold(self, tag):
        entered, release = threading.Event(), threading.Event()
        self.model.hold = (tag, entered, release)
        return entered, release

    def row(self, tag):
        found = [row for seen, row in self.rows if seen == tag]
        self.assertEqual(len(found), 1, tag)
        return found[0]


class CancellationTests(ServedAdapterBase):
    def test_closed_client_stops_after_the_running_batch_and_the_next_request_is_served(self):
        # Item 12: one pair per card and one pair per batch, so six batches.
        self.serve({'warm': edit_request('warm', 1), 'A': edit_request('A', 6),
                    'B': edit_request('B', 2)})
        self.assertEqual(exchange(self.root, wire_envelope('warm'))['status'], 'ok')
        entered, release = self.hold('A')
        first = self.connect('A')
        self.assertTrue(entered.wait(3))
        following = self.connect('B')
        wait_until(lambda: self.queued() == 1, 'B queued behind A')
        # The client leaves while A's first batch is in its forward.
        first.close()
        release.set()
        reply = service.resident._read(following)
        self.assertEqual(reply['status'], 'ok')
        # The running batch finished; none of A's five remaining batches ran
        # before (or after) B's two.
        self.assertEqual(self.model.calls, ['warm', 'A', 'B', 'B'])
        cancelled = self.row('A')
        self.assertEqual(cancelled['status'], 'deadline_expired')
        self.assertEqual(cancelled['timing']['cancelled_after_batches'], 1)
        self.assertEqual((cancelled['timing']['batches'], cancelled['timing']['pairs'],
                          cancelled['timing']['planned_pairs']), (1, 1, 6))
        self.assertEqual(len(self.loads), 1)

    def test_a_queued_request_whose_client_left_never_reaches_a_forward(self):
        # Item 13: both a half-closed and a fully closed waiting client.
        self.serve({'warm': edit_request('warm', 1), 'W': edit_request('W', 2),
                    'C1': edit_request('C1', 3), 'C2': edit_request('C2', 3),
                    'D': edit_request('D', 1)})
        self.assertEqual(exchange(self.root, wire_envelope('warm'))['status'], 'ok')
        entered, release = self.hold('W')
        working = self.connect('W')
        self.assertTrue(entered.wait(3))
        half = self.connect('C1')
        half.shutdown(socket.SHUT_WR)
        gone = self.connect('C2')
        wait_until(lambda: self.queued() == 2, 'C1 and C2 queued behind W')
        gone.close()
        release.set()
        self.assertEqual(service.resident._read(working)['status'], 'ok')
        answered = service.resident._read(half)
        self.assertEqual(answered['status'], 'deadline_expired')
        self.assertIsNone(answered['prediction'])
        self.assertEqual(exchange(self.root, wire_envelope('D'))['status'], 'ok')
        self.assertEqual((self.model.count('C1'), self.model.count('C2')), (0, 0))
        self.assertEqual(self.model.calls, ['warm', 'W', 'W', 'D'])


class FailureTimingTests(ServedAdapterBase):
    def test_a_cancelled_reply_carries_only_closed_numeric_timing_with_its_counts(self):
        # Item 15, over the wire: the client half-closes so it can still read.
        self.serve({'warm': edit_request('warm', 1), 'A': edit_request('A', 6)})
        self.assertEqual(exchange(self.root, wire_envelope('warm'))['status'], 'ok')
        entered, release = self.hold('A')
        conn = self.connect('A')
        self.assertTrue(entered.wait(3))
        conn.shutdown(socket.SHUT_WR)
        release.set()
        reply = service.resident._read(conn)
        self.assertEqual((reply['status'], reply['prediction']), ('deadline_expired', None))
        timing = reply['timing']
        self.assertLessEqual(set(timing), inference_timing.KEYS)
        for key, value in timing.items():
            self.assertIn(type(value), (int, float), key)
            self.assertGreaterEqual(value, 0, key)
        for key in inference_timing.COUNTS:
            if key in timing:
                self.assertIs(type(timing[key]), int, key)
        self.assertEqual({key: timing[key] for key in
                          ('cancelled_after_batches', 'batches', 'pairs', 'planned_pairs')},
                         {'cancelled_after_batches': 1, 'batches': 1, 'pairs': 1, 'planned_pairs': 6})
        for key in ('tokenize_seconds', 'batch_prepare_seconds', 'forward_seconds',
                    'queue_wait_seconds', 'initial_validation_seconds', 'model_load_seconds'):
            self.assertIn(key, timing)
        for key in OPEN_KEYS:
            self.assertNotIn(key, timing)
        self.assertEqual(self.model.count('A'), 1)

    def test_clean_keeps_new_counts_and_drops_bools_strings_and_open_keys(self):
        row = inference_timing.clean({
            'tokenize_seconds': .5, 'batch_prepare_seconds': 0, 'envelope_seconds': 1,
            'batches': 2, 'pairs': 3, 'planned_pairs': 4, 'cancelled_after_batches': 0,
            'attempts': 1, 'forward_seconds': True, 'queue_wait_seconds': float('nan'),
            'padded_total': 10, 'device': 'cpu', 'query_chunks': 2, 'pairs_extra': 1})
        self.assertEqual(row, {'tokenize_seconds': .5, 'batch_prepare_seconds': 0.0,
                               'envelope_seconds': 1.0, 'batches': 2, 'pairs': 3,
                               'planned_pairs': 4, 'cancelled_after_batches': 0, 'attempts': 1})
        for bad in (True, -1, 1.0, '2', None):
            self.assertNotIn('batches', inference_timing.clean({'batches': bad}), bad)


class EnvelopeTimingTests(ServedAdapterBase):
    def test_predict_reports_envelope_seconds_on_success_and_failure(self):
        from contextlib import nullcontext
        from memvane.runtime import api as runtime_api

        class NoCoordinator:
            def __init__(self, _root):
                pass

            def inference(self, *_args):
                return nullcontext()
        # pair_budget 0 reaches the adapter only because this fixture's
        # validation is injected; the adapter refuses it as worker_error.
        self.serve({'ok': edit_request('ok', 2), 'bad': edit_request('bad', 1, pair_budget=0)})
        tag = ['ok']
        with patch.object(service, '_make_envelope', side_effect=lambda *a: wire_envelope(tag[0])), \
                patch.object(service, 'service_root', return_value=self.root), \
                patch.object(runtime_api, 'coordinator', NoCoordinator), \
                patch.object(service, '_ensure', return_value=None):
            prediction, timing = service.predict(self.root, {'trainerArgv': [sys.executable]}, {},
                                                 '0' * 64, deadline=time.monotonic() + 5)
            self.assertEqual(prediction['predictions'][0]['request_id'], 'ok')
            self.assertIn(type(timing.get('envelope_seconds')), (int, float))
            self.assertGreaterEqual(timing['envelope_seconds'], 0)
            tag[0] = 'bad'
            with self.assertRaises(service.ServiceFailure) as caught:
                service.predict(self.root, {'trainerArgv': [sys.executable]}, {}, '0' * 64,
                                deadline=time.monotonic() + 5)
        failure = caught.exception
        self.assertEqual(failure.code, 'worker_error')
        self.assertIn(type(failure.timing.get('envelope_seconds')), (int, float))
        self.assertLessEqual(set(failure.timing), inference_timing.KEYS)
        self.assertEqual(self.model.calls, ['ok', 'ok'])


def _private(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


class RequestKeyTests(unittest.TestCase):
    """Item 11: ``_validate_request`` on a real private state with fixed identity."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='mv-v-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name).resolve()
        self.state.chmod(0o700)
        self.artifact = self.state / 'head.artifact'
        self.artifact.write_text('synthetic head')
        self.artifact.chmod(0o600)
        self.sha = service.resident._hash(self.artifact)
        # The experimental pointer skips the profile application gate, which
        # is outside this request-key contract.
        _private(self.state / 'experimental-checkpoint.json', {
            'schema_version': 1, 'artifact_path': str(self.artifact), 'artifact_sha256': self.sha})
        self.raw = {'schema': 'qmd-live-selector-v1', 'trainerArgv': [], 'timeoutSeconds': 5,
                    'maxRssMib': 512}
        policy = {**self.raw, 'trainerArgv': [sys.executable, str(Path(adapter.__file__).resolve()),
                                              str(self.state)]}

        class Trainer:
            def __init__(inner, *_a, **_k):
                pass

            def validate_prediction(inner, *, cwd):
                return {'runtime_identity_sha256': 'r' * 64, 'base_model_sha256': 'm' * 64}
        for target in (patch.object(service, '_policy', return_value=(self.raw, policy)),
                       patch.object(local_cycle, 'VerifiedLayaTrainer', Trainer)):
            target.start()
            self.addCleanup(target.stop)

    def envelope(self, **extra):
        one = {'request_id': 'r', 'prompt': 'synthetic request',
               'candidates': [card('wiki/a.md', '# a\n본문.')]}
        one['input_sha256'] = digest(canonical(one))
        now = time.time()
        return {'schema': service.PROTOCOL, 'signature': service._signature(),
                'state_dir': str(self.state), 'namespace': service.namespace(self.state),
                'pointer_name': 'experimental-checkpoint.json', 'artifact_sha256': self.sha,
                'policy_sha256': service._policy_digest(self.raw),
                'request': {'schema_version': 1, 'artifact_path': str(self.artifact),
                            'cases': [one], **extra},
                'queued_at_epoch': now, 'deadline_epoch': now + 10}

    def test_absent_and_in_range_keys_validate(self):
        for extra in ({}, {'pair_budget': 1}, {'pair_budget': adapter.P_HARD},
                      {'query_lane': 'prompt'}, {'query_lane': 'edit'},
                      {'pair_budget': 24, 'query_lane': 'prompt'}):
            with self.subTest(extra=extra):
                job = service._validate_request(self.envelope(**extra))
                for key in ('pair_budget', 'query_lane'):
                    self.assertEqual(job['request'].get(key), extra.get(key))

    def test_out_of_range_bool_and_unknown_values_are_rejected(self):
        for extra in ({'pair_budget': 0}, {'pair_budget': adapter.P_HARD + 1},
                      {'pair_budget': True}, {'pair_budget': False}, {'pair_budget': 2.0},
                      {'pair_budget': '4'}, {'pair_budget': None}, {'query_lane': 'other'},
                      {'query_lane': None}, {'query_lane': 'Prompt'}, {'lane': 'prompt'},
                      {'pair_budget': 4, 'extra': 1}):
            with self.subTest(extra=extra):
                with self.assertRaises(service.ServiceFailure) as caught:
                    service._validate_request(self.envelope(**extra))
                self.assertEqual(caught.exception.code, 'request_rejected')


class ChooseTests(unittest.TestCase):
    def test_choose_forwards_pair_budget_and_lane_and_reports_argv_seconds(self):
        with tempfile.TemporaryDirectory(prefix='mv-c-', dir='/tmp') as temporary:
            state = Path(temporary).resolve()
            state.chmod(0o700)
            artifact = state / 'head.artifact'
            artifact.write_text('synthetic head')
            artifact.chmod(0o600)
            _private(state / 'active-checkpoint.json', {
                'schema_version': 1, 'artifact_path': str(artifact),
                'artifact_sha256': service.resident._hash(artifact)})
            _private(state / 'live-selector.json', {
                'schema': 'qmd-live-selector-v1',
                'trainerArgv': [sys.executable, str(Path(adapter.__file__).resolve()), str(state)],
                'timeoutSeconds': 5, 'maxRssMib': 512})
            source = '---\ntitle: small\n---\nSmall body.\n'
            rows = [{'id': 'wiki/small.md', 'revision_sha256': digest(source), 'source_text': source}]
            seen = []

            class Trainer:
                def __init__(self, *_a, **_k):
                    pass

                def validate_prediction(self, *, cwd):
                    return {}

            def predict(_state, _policy, request, _sha, **_kwargs):
                seen.append(request)
                one = request['cases'][0]
                return {'schema_version': 1, 'predictions': [{
                    'request_id': one['request_id'], 'input_sha256': one['input_sha256'],
                    'selected_ids': ['wiki/small.md']}]}, {'resident_wall_seconds': .1}
            with patch.object(live_select, 'VerifiedLayaTrainer', Trainer), \
                    patch.object(service, 'predict', predict):
                plain = live_select.choose(state, 'Synthetic request', rows,
                                           corpus_fingerprint='a' * 64)
                lane = live_select.choose(state, 'Synthetic request', rows,
                                          corpus_fingerprint='a' * 64, pair_budget=24, lane='prompt')
        for ids, reason, resources in (plain, lane):
            self.assertEqual((ids, reason), (['wiki/small.md'], 'selected'))
            self.assertIn(type(resources.get('argv_seconds')), (int, float))
            self.assertGreaterEqual(resources['argv_seconds'], 0)
            self.assertEqual(resources['resident_wall_seconds'], .1)
        self.assertNotIn('pair_budget', seen[0])
        self.assertNotIn('query_lane', seen[0])
        self.assertEqual((seen[1]['pair_budget'], seen[1]['query_lane']), (24, 'prompt'))


class FileShaCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='mv-h-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        cache = patch.dict(identity._FILE_SHAS)
        cache.start()
        self.addCleanup(cache.stop)
        self.hashes = Mock(side_effect=identity.file_sha)
        hashing = patch.object(identity, 'file_sha', self.hashes)
        hashing.start()
        self.addCleanup(hashing.stop)

    def test_unchanged_stamp_reuses_the_digest_and_a_same_size_rewrite_rehashes(self):
        path = self.root / 'code.py'
        path.write_bytes(b'value = 1\n')
        first = identity.cached_file_sha(path)
        self.assertEqual(identity.cached_file_sha(path), first)
        self.assertEqual(self.hashes.call_count, 1)
        path.write_bytes(b'value = 2\n')
        rewritten = identity.cached_file_sha(path)
        self.assertNotEqual(rewritten, first)
        self.assertEqual(self.hashes.call_count, 2)
        # Setting mtime back leaves only ctime moved; the digest still follows content.
        before = os.stat(path)
        path.write_bytes(b'value = 3\n')
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = os.stat(path)
        self.assertEqual((after.st_size, after.st_mtime_ns), (before.st_size, before.st_mtime_ns))
        self.assertNotEqual(after.st_ctime_ns, before.st_ctime_ns)
        third = identity.cached_file_sha(path)
        self.assertEqual(self.hashes.call_count, 3)
        self.assertNotIn(third, (first, rewritten))
        self.assertEqual(identity.cached_file_sha(path), third)
        self.assertEqual(self.hashes.call_count, 3)

    def test_code_identity_follows_code_content_and_rehashes_only_changed_files(self):
        package = self.root / 'memvane'
        (package / 'learning' / 'context_learning').mkdir(parents=True)
        one = package / 'a.py'
        two = package / 'learning' / 'b.py'
        one.write_text('A = 1\n')
        two.write_text('B = 1\n')
        fake = package / 'learning' / 'context_learning' / 'inference_identity.py'
        with patch.object(identity, '__file__', str(fake)):
            original = identity.code_identity()
            self.assertEqual(self.hashes.call_count, 2)
            self.assertEqual(identity.code_identity(), original)
            self.assertEqual(self.hashes.call_count, 2)
            two.write_text('B = 2\n')
            changed = identity.code_identity()
            self.assertNotEqual(changed, original)
            self.assertEqual(self.hashes.call_count, 3)
            two.write_text('B = 1\n')
            self.assertEqual(identity.code_identity(), original)
            (package / 'c.py').write_text('')
            self.assertNotIn(identity.code_identity(), (original, changed))


class ModuleScopeTests(unittest.TestCase):
    def test_a_module_from_outside_the_roots_is_rejected_after_an_earlier_pass(self):
        temp = tempfile.TemporaryDirectory(prefix='mv-m-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        outside = Path(temp.name).resolve()
        scoped = patch.dict(identity._SCOPED)
        scoped.start()
        self.addCleanup(scoped.stop)

        def roots():
            # Every directory a loaded module comes from, so the first call passes.
            found = {Path(sys.base_prefix).resolve()}
            for module in tuple(sys.modules.values()):
                origin = getattr(module, '__file__', None)
                if origin:
                    found.add(Path(origin).resolve().parent)
            return sorted(found)
        allowed = roots()
        identity._module_scope(allowed)
        identity._module_scope(allowed)
        name = 'mv119_external_fixture'
        fake = types.ModuleType(name)
        fake.__file__ = str(outside / 'external.py')
        sys.modules[name] = fake
        try:
            with self.assertRaisesRegex(ValueError, 'inference_external_library_scope'):
                identity._module_scope(allowed)
        finally:
            sys.modules.pop(name, None)
        identity._module_scope(allowed)
        # The same name bound to a new module object is checked again.
        inside = types.ModuleType(name)
        inside.__file__ = str(Path(identity.__file__).resolve())
        sys.modules[name] = inside
        try:
            identity._module_scope(allowed)
            replaced = types.ModuleType(name)
            replaced.__file__ = str(outside / 'external.py')
            sys.modules[name] = replaced
            with self.assertRaisesRegex(ValueError, 'inference_external_library_scope'):
                identity._module_scope(allowed)
        finally:
            sys.modules.pop(name, None)


class IdentitySweepTests(unittest.TestCase):
    """Runtime sweeps per request, with the identity fixture of the identity tests."""
    VIEW = 'checkpoint-config-v1'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.root.chmod(0o700)
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir()
        self.library = self.runtime / 'library.py'
        self.library.write_text('version=1\n')
        self.model = self.root / 'model'
        self.model.mkdir()
        (self.model / 'tokenizer.json').write_text('{}')
        (self.model / 'config.json').write_text('{}')
        (self.model / 'model.safetensors').write_bytes(b'synthetic weights')
        self.adapter = Path(identity.__file__).with_name('adapter.py')
        self.argv = [sys.executable, str(self.adapter), str(self.model)]
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.artifact = self.state / 'head'
        self.artifact.write_text('head')
        self.artifact.chmod(0o600)
        self.write('live-selector.json', {'trainerArgv': self.argv})
        self.proof = {'schema': 'qmd-laya-live-proof-v1', 'verifiedAt': time.time(),
                      'attestation': {'old': 'approval'}}
        self.write('laya-synthetic-attestation.json', self.proof)
        self.write('active-checkpoint.json', {'artifact_path': str(self.artifact),
                                              'artifact_sha256': identity.file_sha(self.artifact)})
        self.scope = Mock(return_value=None)
        for target in (patch.object(identity, 'runtime_roots', return_value=[self.runtime]),
                       patch.object(identity, 'device_identity',
                                    return_value={'backend': 'cpu', 'os': 'fixture'}),
                       patch.object(identity, '_module_scope', self.scope),
                       patch.object(identity, 'code_identity', return_value='c' * 64)):
            target.start()
            self.addCleanup(target.stop)
        identity._record(self.state, self.argv, view=self.VIEW, verified={
            'runtime_identity_sha256': 'r' * 64, 'base_model_sha256': 'm' * 64,
            'adapter_sha256': identity.file_sha(self.adapter)})
        self.cache = identity.IdentityCache()
        self.sweeps = Mock(side_effect=identity.snapshot)
        counting = patch.object(identity, 'snapshot', self.sweeps)
        counting.start()
        self.addCleanup(counting.stop)

    def write(self, name, row):
        path = self.state / name
        path.write_text(json.dumps(row))
        path.chmod(0o600)

    def verify(self):
        return self.cache.verify(self.state, self.argv, self.VIEW)

    def request(self, engine):
        """One request as the worker runs it: initial validation, then ``engine.run``."""
        job = {'family': ('runtime', 'base', 'view'), 'generation': ('runtime', 'base', 'view', 'a'),
               'namespace': 'one', 'policy': {'maxRssMib': 1024}, 'request': {},
               'deadline': time.monotonic() + 30}

        def validate():
            self.verify()
            return job
        self.sweeps.reset_mock()
        self.scope.reset_mock()
        engine.run(validate(), cancelled=lambda: False, revalidate=validate)
        return self.sweeps.call_count, self.scope.call_count

    def test_first_load_sweeps_fully_and_a_warm_request_sweeps_once(self):
        loads = []
        engine = service.GenerationEngine(load=lambda job: loads.append(1) or 'loaded',
                                          forward=lambda loaded, job, **kw: {'ok': True},
                                          admission=admit, rss=lambda: 100)
        sweeps, scopes = self.request(engine)
        # Initial validation, after the load and after the forward.
        self.assertGreaterEqual(sweeps, 2)
        self.assertEqual((sweeps, scopes), (3, 3))
        self.assertFalse(self.cache.last_timing['identity_light'])
        for _ in range(2):
            self.assertEqual(self.request(engine), (1, 1))
            self.assertTrue(self.cache.last_timing['identity_light'])
        self.assertEqual(len(loads), 1)
        # The next request's initial validation still sweeps the runtime.
        self.library.write_text('version=2\n')
        with self.assertRaisesRegex(ValueError, 'inference_identity_changed'):
            self.request(engine)
        self.assertEqual(self.sweeps.call_count, 1)

    def test_light_revalidation_is_ignored_until_the_generation_is_cached(self):
        with identity.light_revalidation():
            self.verify()
        self.assertEqual(self.sweeps.call_count, 1)
        self.assertEqual(self.scope.call_count, 1)
        self.assertEqual((self.cache.last_timing['identity_cache_hit'],
                          self.cache.last_timing['identity_light']), (False, False))
        self.sweeps.reset_mock()
        self.scope.reset_mock()
        with identity.light_revalidation():
            self.verify()
        self.assertEqual((self.sweeps.call_count, self.scope.call_count), (0, 0))
        self.assertTrue(self.cache.last_timing['identity_light'])
        # Outside the context the same cache sweeps again.
        self.verify()
        self.assertEqual((self.sweeps.call_count, self.scope.call_count), (1, 1))
        self.assertFalse(self.cache.last_timing['identity_light'])

    def test_light_revalidation_still_checks_the_receipt_and_attestation(self):
        self.verify()
        (self.state / 'laya-synthetic-attestation.json').unlink()
        with identity.light_revalidation():
            with self.assertRaisesRegex(ValueError, 'inference_identity_changed'):
                self.verify()

    # 1.1.0 W6: a recall request's initial validation stands on a recent sweep.
    def clocked(self):
        now = [100.0]
        self.cache.clock = lambda: now[0]
        return now

    def recall(self):
        """(sweeps, module walks, reused) of one recall request's initial validation."""
        self.sweeps.reset_mock()
        self.scope.reset_mock()
        with identity.reuse_window():
            self.cache.verify(self.state, self.argv, self.VIEW, reuse=True)
        return (self.sweeps.call_count, self.scope.call_count,
                self.cache.last_timing['identity_reused'])

    def test_recall_validation_inside_the_window_runs_no_sweep(self):
        now = self.clocked()
        self.assertEqual(self.recall(), (1, 1, 0))
        now[0] += 4
        self.assertEqual(self.recall(), (0, 0, 1))
        # A reused validation does not extend the window (8 s after the sweep).
        now[0] += 4
        self.assertEqual(self.recall(), (1, 1, 0))
        now[0] += identity.IDENTITY_REUSE_SECONDS
        self.assertEqual(self.recall(), (0, 0, 1))
        now[0] += .001
        self.assertEqual(self.recall(), (1, 1, 0))

    def test_apply_warm_and_revalidation_sweep_inside_the_window(self):
        self.clocked()
        self.recall()
        self.sweeps.reset_mock()
        # Apply and the lane-less warm-up request do not ask for reuse.
        with identity.reuse_window():
            self.verify()
        self.assertEqual(self.sweeps.call_count, 1)
        # A revalidation runs outside the initial validation's window.
        self.cache.verify(self.state, self.argv, self.VIEW, reuse=True)
        self.assertEqual(self.sweeps.call_count, 2)
        self.assertEqual(self.cache.last_timing['identity_reused'], 0)

    def test_reused_validation_still_checks_the_receipt_and_then_sweeps(self):
        self.clocked()
        self.recall()
        (self.state / 'laya-synthetic-attestation.json').unlink()
        with self.assertRaisesRegex(ValueError, 'inference_identity_changed'):
            self.recall()
        self.assertEqual(self.sweeps.call_count, 0)
        # A failed validation ends the window: the next one sweeps.
        self.write('laya-synthetic-attestation.json', self.proof)
        self.assertEqual(self.recall(), (1, 1, 0))

    def test_runtime_change_is_found_by_the_first_validation_after_the_window(self):
        now = self.clocked()
        self.recall()
        self.library.write_text('version=2\n')
        now[0] += 1
        self.assertEqual(self.recall(), (0, 0, 1))
        now[0] += identity.IDENTITY_REUSE_SECONDS
        with self.assertRaisesRegex(ValueError, 'inference_identity_changed'):
            self.recall()
        self.assertEqual(self.sweeps.call_count, 1)

    def infer(self, engine, *, lane=True):
        """(sweeps, reused) of one request through the worker's ``infer``."""
        job = {'family': ('runtime', 'base', 'view'), 'generation': ('runtime', 'base', 'view', 'a'),
               'namespace': 'one', 'policy': {'maxRssMib': 1024}, 'request': {},
               'deadline': time.monotonic() + 30}

        def validate(_envelope):
            # As _validate_request: only a lane request asks for reuse.
            self.cache.verify(self.state, self.argv, self.VIEW, reuse=lane)
            return {**job, 'identity_timing': dict(self.cache.last_timing)}

        class Open:
            def inference(self, *_a):
                return contextlib.nullcontext()
            inference_compute = inference
        self.sweeps.reset_mock()
        row = inference_score.infer(engine, Open(), validate, {'namespace': 'one'}, job['deadline'],
                                    0.0, cancelled=lambda: False, failure=service._failure)
        self.assertEqual(row['status'], 'ok', row)
        return self.sweeps.call_count, row['timing']['identity_reused']

    def engine(self):
        return service.GenerationEngine(load=lambda job: 'loaded',
                                        forward=lambda loaded, job, **kw: {'ok': True},
                                        admission=admit, rss=lambda: 100)

    def test_request_inside_the_window_sweeps_only_around_a_model_load(self):
        now = self.clocked()
        engine = self.engine()
        # First request: initial validation, after the load, after the forward.
        self.assertEqual(self.infer(engine), (3, 0))
        now[0] += 1
        # Loaded model, inside the window: no sweep at all.
        self.assertEqual(self.infer(engine), (0, 1))
        # The warm-up request shape (no lane) sweeps at its initial validation.
        self.assertEqual(self.infer(engine, lane=False), (1, 0))
        # A model load inside the window is swept after the load and the forward.
        now[0] += 1
        self.assertEqual(self.infer(self.engine()), (2, 1))
        now[0] += identity.IDENTITY_REUSE_SECONDS + 1
        self.assertEqual(self.infer(engine), (1, 0))


if __name__ == '__main__':
    unittest.main()

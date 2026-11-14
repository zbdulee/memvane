"""The learning client's view of the resident-hosted inference service.

``inference_service._ensure`` asks the resident host through the injected
``ensure_service`` port (the product binds ``resident.api.ensure_service``)
and then decides from the learning service's own answers: wait, accept or
refuse. The host's own replacement policy (hello, drain, spawn) is the
resident domain's; here the port answers with the host verdicts
(``ready``/``starting``/``replacing`` or a protocol refusal).
"""
from contextlib import ExitStack
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from memvane.kernel.resource_deferred import ResourceDeferred
from memvane.learning.context_learning import inference_service as learning
from memvane.learning.context_learning import live_select


class Recorder:
    """An ``ensure_service`` port that records its calls and answers ``verdict``."""

    def __init__(self, verdict):
        self.verdict, self.calls = verdict, []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.verdict, BaseException):
            raise self.verdict
        return self.verdict


def private_root(base, name):
    root = base / name
    root.mkdir(mode=0o700)
    return root


class HostedServiceClient(unittest.TestCase):
    def clock(self, stack, start=100.0):
        now, sleeps = [start], []

        def sleep(seconds):
            self.assertGreater(seconds, 0)
            sleeps.append(seconds)
            now[0] += seconds
        stack.enter_context(mock.patch.object(learning.time, 'monotonic', lambda: now[0]))
        stack.enter_context(mock.patch.object(learning.time, 'sleep', sleep))
        return now, sleeps

    def test_old_client_rejects_ready_hosted_learning_mismatch_without_polling(self):
        with tempfile.TemporaryDirectory(prefix='rt-version-', dir='/tmp') as temp:
            root = private_root(Path(temp).resolve(), 'learning')
            versions, failures = [], []
            newer = {'status': 'ready', 'protocol': learning.PROTOCOL,
                     'signature': 'synthetic-newer-signature', 'hosted': True,
                     'python': sys.executable, 'busy': False, 'queued': 0}
            # The host is newer and ready: it neither drains nor respawns, it answers ready.
            host = Recorder('ready')
            with ExitStack() as stack:
                now, sleeps = self.clock(stack)
                stack.enter_context(mock.patch.object(learning, '_signature', return_value='synthetic-old-signature'))
                stack.enter_context(mock.patch.object(learning, '_version',
                    lambda _root, _deadline: versions.append(now[0]) or dict(newer)))
                started = now[0]
                for _ in range(2):
                    try:
                        learning._ensure(root, sys.executable, now[0] + 2, ensure_service=host)
                    except learning.ServiceFailure as exc:
                        failures.append(exc.code)
                    else:
                        failures.append('returned')
            self.assertEqual(failures, ['version_mismatch', 'version_mismatch'])
            self.assertEqual(len(host.calls), 2)
            self.assertEqual(sleeps, [])
            self.assertEqual(now[0], started)

    def test_hosted_ensure_preserves_normal_startup_replacement_and_matching_paths(self):
        verdicts = {'absent': 'starting', 'learning-starting': 'ready', 'host-replacing': 'replacing',
                    'python-switch': 'replacing', 'matched': None}
        for case, verdict in verdicts.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory(prefix='rt-wait-', dir='/tmp') as temp:
                root = private_root(Path(temp).resolve(), 'learning')
                reads = []
                good = {'status': 'ready', 'hosted': True, 'protocol': learning.PROTOCOL,
                        'signature': 'same-signature', 'python': sys.executable}
                initial = None if case == 'absent' else dict(good)
                if case == 'learning-starting':
                    initial.update(status='starting', signature='previous-signature')
                if case == 'host-replacing':
                    initial['signature'] = 'previous-signature'
                if case == 'python-switch':
                    initial['python'] = '/synthetic/other-python'

                def version(*_args):
                    reads.append(True)
                    return dict(good) if case == 'matched' or len(reads) >= 3 else initial
                host = Recorder(verdict)
                with ExitStack() as stack:
                    now, sleeps = self.clock(stack)
                    stack.enter_context(mock.patch.object(learning, '_signature', return_value='same-signature'))
                    stack.enter_context(mock.patch.object(learning, '_version', version))
                    learning._ensure(root, sys.executable, now[0] + 1, ensure_service=host)
                if case == 'matched':
                    self.assertEqual((host.calls, sleeps), ([], []))
                else:
                    self.assertEqual(host.calls, [{'python': sys.executable, 'learning_root': root}])
                    self.assertTrue(sleeps, 'a genuine transition must still wait')

    def test_host_protocol_mismatch_keeps_existing_wire_code_and_selector_fallback_reason(self):
        with tempfile.TemporaryDirectory(prefix='rt-protocol-', dir='/tmp') as temp:
            root = private_root(Path(temp).resolve(), 'learning')
            host = Recorder(ResourceDeferred('laya_service_protocol_mismatch'))
            with mock.patch.object(learning, '_version', return_value=None):
                with self.assertRaises(learning.ServiceFailure) as observed:
                    learning._ensure(root, sys.executable, learning.time.monotonic() + 1, ensure_service=host)
            failure = observed.exception
            self.assertEqual(failure.code, 'version_mismatch')
            self.assertEqual(failure.timing, {'host_reason': 'laya_service_protocol_mismatch'})
            self.assertEqual(learning._failure(failure), 'version_mismatch')
            # The choose exception-normalization boundary; the model front end is not exercised.
            with mock.patch.object(live_select, 'read_private_json', side_effect=failure):
                ids, reason, resources_out = live_select.choose(root, 'synthetic prompt',
                    [{'id': 'synthetic'}], corpus_fingerprint='a' * 64)
            self.assertIsNone(ids)
            self.assertEqual(reason, 'resident_service_version_mismatch')
            self.assertEqual(resources_out['host_reason'], 'laya_service_protocol_mismatch')

    def test_malformed_and_unsafe_learning_observations_never_become_success(self):
        for kind in ('malformed', 'unsafe'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(prefix='rt-invalid-', dir='/tmp') as temp:
                root = Path(temp).resolve()
                root.chmod(0o700)
                calls = []

                def version(*_args):
                    calls.append(True)
                    if kind == 'unsafe':
                        raise learning.ServiceFailure('unsafe_socket')
                    return {'status': 'ready', 'hosted': True, 'protocol': learning.PROTOCOL,
                            'signature': 123, 'python': sys.executable}
                with ExitStack() as stack:
                    now, _sleeps = self.clock(stack)
                    stack.enter_context(mock.patch.object(learning, '_version', version))
                    stack.enter_context(mock.patch.object(learning, '_signature', return_value='same-signature'))
                    with self.assertRaises(learning.ServiceFailure) as observed:
                        learning._ensure(root, sys.executable, now[0] + .1, ensure_service=Recorder('ready'))
                self.assertEqual(observed.exception.code, 'starting')
                self.assertGreater(len(calls), 1)

    def test_learning_inference_is_reached_through_the_resident_service(self):
        with tempfile.TemporaryDirectory(prefix='lrl-', dir='/tmp') as temp:
            root = Path(temp).resolve()
            root.chmod(0o700)
            stop = threading.Event()
            calls = []

            def host(**kwargs):
                calls.append(kwargs)
                engine = learning.GenerationEngine(
                    load=lambda job: 'loaded',
                    forward=lambda loaded, request, **kw: {'head': loaded},
                    admission=lambda *_a, **_k: ({}, {}), rss=lambda: 100)
                threading.Thread(target=learning._serve, args=(root,),
                                 kwargs={'engine': engine, 'stop': stop, 'hosted': True},
                                 daemon=True).start()
                return 'starting'
            try:
                learning._ensure(root, sys.executable, time.monotonic() + 5, ensure_service=host)
                self.assertEqual(calls, [{'python': sys.executable, 'learning_root': root}])
                answer = learning._version(root, time.monotonic() + 1)
                self.assertEqual((answer['hosted'], answer['python']), (True, sys.executable))
                # Older clients cannot shut a hosted service down.
                reply = learning._exchange(root, {'command': 'shutdown'}, time.monotonic() + 1)
                self.assertEqual(reply['status'], 'busy')
                self.assertTrue(learning._ready(root, time.monotonic() + 1))
            finally:
                stop.set()
                gone = time.monotonic() + 5
                while (root / learning.SOCKET_NAME).exists() and time.monotonic() < gone:
                    time.sleep(.02)


if __name__ == '__main__':
    unittest.main()

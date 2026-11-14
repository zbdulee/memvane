"""The selected sync pass and the per-user sync owner.

One owner per user (``runtime.api.sync_owner``): any install's holder makes
this install's pass defer ``owner_busy`` before content or provider work; the
pass holds the owner through its provider work and the after-sync drain and
releases it when it returns; a brief hold elsewhere is awaited. With the
installed warm service, a cold resident is asked to start under the owner
but outside the provider lane, and the pass defers its vectors.
"""
from contextlib import contextmanager
import json
import os
import select
import subprocess
import sys
import unittest
from unittest import mock

from memvane.provider.api import start_owned_embed_service
from memvane.runtime import api as runtime
from memvane.sync import api as sync
from memvane.sync import product_sync_worker as worker
from memvane.sync.tests.support import Coordinator, SyncCase
from memvane.wiki.intake import api as intake
from tests.support.install_builder import ROOT

HOLDER = r'''
import json, signal, sys
from pathlib import Path
signal.alarm(30)
from memvane.runtime.api import sync_owner
with sync_owner(Path(sys.argv[1])) as owner:
    print(json.dumps({'owned': owner is not None}), flush=True)
    if owner is not None:
        assert sys.stdin.readline() == 'RELEASE\n'
'''


class SyncOwner(SyncCase):
    def setUp(self):
        super().setUp()
        # Another install's queue: its owner is the same per-user owner.
        other = self.tmp / 'other-install'
        other.mkdir(mode=0o700)
        self.other_queue = other / 'store.sqlite'
        self.other_queue.write_bytes(b'')
        os.chmod(self.other_queue, 0o600)

    def available(self):
        with runtime.sync_owner(self.other_queue) as claim:
            return claim is not None

    def run_default(self, coordinator=None):
        """A pass with the installed warm service (the production default starter)."""
        return sync.run(self.project, coordinator=coordinator or Coordinator(), report={},
                        start_service=start_owned_embed_service)

    @contextmanager
    def resident(self, status='absent'):
        """The resident service api: ``ensure_running`` calls observed, ``probe`` answering ``status``."""
        seen = []
        with mock.patch('memvane.resident.api.ensure_running',
                        lambda project, manifest, **kw: seen.append(self.available())), \
                mock.patch('memvane.resident.api.probe', lambda manifest: status):
            yield seen

    def test_a_busy_owner_of_any_install_defers_before_content_and_provider(self):
        start, epoch = self.head(), self.epoch()
        self.card(self.CARDS[0][0], 'falconprobe')
        with runtime.sync_owner(self.other_queue) as held:
            self.assertIsNotNone(held)
            with self.resident() as adoptions, self.provider_calls() as calls:
                result = self.run_default()
        self.assertEqual(result, {'state': 'deferred', 'reason': 'owner_busy'})
        self.assertEqual((adoptions, calls), ([], []))
        self.assertEqual((self.head(), self.epoch()), (start, epoch))
        self.assertTrue(self.available())

    def test_the_owner_is_held_through_provider_work_and_the_drain_then_released(self):
        self.card(self.CARDS[0][0], 'falconprobe')
        seen, waits = {}, []
        real_owner = worker.sync_owner

        @contextmanager
        def recorded(path, **kwargs):
            waits.append(kwargs.get('wait'))
            with real_owner(path, **kwargs) as held:
                yield held
        real_drain = intake.drain

        def drain(decision, **kwargs):
            seen['drain'] = self.available()
            return real_drain(decision, **kwargs)
        coordinator = Coordinator(on_activity=lambda lane: seen.setdefault('provider', self.available()))
        with mock.patch.object(worker, 'sync_owner', recorded), \
                mock.patch('memvane.wiki.intake.api.drain', drain):
            result = self.run_pass(coordinator)
        self.assertEqual((result['state'], result['vector']['state']), ('indexed', 'published'), result)
        self.assertEqual(coordinator.lanes, ['provider'])
        self.assertEqual(seen, {'provider': False, 'drain': False})
        self.assertTrue(waits)
        self.assertTrue(all(wait is True for wait in waits), waits)
        self.assertTrue(self.available())

    def test_a_cold_resident_is_asked_to_start_under_the_owner_outside_the_provider_lane(self):
        start = self.head()
        self.card(self.CARDS[0][0], 'falconprobe')
        coordinator = Coordinator()
        observed = []
        with mock.patch('memvane.resident.api.probe', lambda manifest: 'absent'), \
                mock.patch('memvane.resident.api.ensure_running',
                           lambda project, manifest, **kw: observed.append((self.available(), coordinator.active))):
            result = self.run_default(coordinator)
        self.assertEqual((result['state'], result['reason'], result['vector']['state']),
                         ('deferred', 'laya_service_preparing', 'deferred'), result)
        # Requested once, with the owner held and the provider lane not yet entered.
        self.assertEqual(observed, [(False, False)])
        self.assertEqual(self.head(), (result['generation'], start[1] + 1))
        self.assertEqual(self.coverage(result['generation'])[1], 'lexical')
        # A resident whose identity moves defers with its own reason.
        with self.resident('mismatch'):
            moving = self.run_default()
        self.assertEqual((moving['state'], moving['reason']), ('deferred', 'laya_service_identity_transition'),
                         moving)
        self.assertTrue(self.available())

    def test_an_owner_briefly_held_elsewhere_is_awaited_not_deferred(self):
        self.card(self.CARDS[0][0], 'falconprobe')
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
        child = subprocess.Popen([sys.executable, '-B', '-c', HOLDER, str(self.other_queue)], cwd=str(ROOT),
                                 env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 encoding='utf8')
        released = []
        try:
            self.assertTrue(select.select([child.stdout], [], [], 20)[0], 'holder readiness')
            self.assertEqual(json.loads(child.stdout.readline()), {'owned': True})
            self.assertFalse(self.available())
            real_owner = worker.sync_owner

            @contextmanager
            def releasing(path, **kwargs):
                if not released:
                    # The holder lets go only once the pass asks for the owner.
                    released.append(kwargs.get('wait'))
                    child.stdin.write('RELEASE\n')
                    child.stdin.flush()
                with real_owner(path, **kwargs) as held:
                    yield held
            with mock.patch.object(worker, 'sync_owner', releasing):
                result = self.run_pass()
        finally:
            if not released:
                child.stdin.write('RELEASE\n')
                child.stdin.flush()
            _out, err = child.communicate(timeout=20)
        self.assertEqual(child.returncode, 0, err)
        self.assertEqual(released, [True])
        self.assertEqual((result['state'], result['ingested']), ('indexed', 1), result)


if __name__ == '__main__':
    unittest.main()

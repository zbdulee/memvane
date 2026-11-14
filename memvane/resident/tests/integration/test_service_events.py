"""Resident lifecycle events retain the affected installation identity."""
import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

from memvane.resident import resident_service as service
from tests.support.case import FixtureCase


class ServiceEvents(FixtureCase):
    def test_drain_forced_exit_and_adopt(self):
        state = self.home / 'state'
        state.mkdir(mode=0o700)
        targets = [(state, 'install-1')]
        fake = SimpleNamespace(
            draining=threading.Event(), bundle={'version': '1.0.0'},
            lock=threading.RLock(), bindings={
                '/p': SimpleNamespace(events=targets[0], closed=False)},
            _close_listener=lambda _binding: None, _finish_drain=lambda: None,
            root=self.home, control_inode=None, exit=lambda _code: None)
        fake._event_targets = lambda: service.Service._event_targets(fake)
        service.Service.begin_drain(fake)
        with patch.object(service, '_kill_children', return_value=([11], [])):
            service.Service._force_exit(fake, 12.34)
        service._event(targets, 'resident.adopt', 'adopted', mode='full')
        rows = [json.loads(line) for line in (state / 'events.jsonl').read_text().splitlines()]
        drain, forced, adopt = rows
        self.assertEqual((drain['event'], drain['outcome']), ('resident.drain', 'started'))
        self.assertEqual((forced['event'], forced['reason'], forced['killedChildren']),
                         ('resident.forced_exit', 'cold_load_hung', [11]))
        self.assertEqual((adopt['event'], adopt['mode'], adopt['install']),
                         ('resident.adopt', 'full', 'install-1'))

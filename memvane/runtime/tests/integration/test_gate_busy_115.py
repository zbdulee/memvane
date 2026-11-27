"""A busy kernel gate names itself in the deferral (1.0.x-4).

``learning_compute_busy`` is raised whichever of the coordinator's gates is
held; its report carries that gate (``provider``, ``control``, ``trainer``,
``compute``) so a sync, initial build or recall put off by learning records
what blocked it. The reason code itself is unchanged.
"""
import fcntl
import os

from tests.support.case import FixtureCase
from memvane.runtime.resource_coordination import Coordinator
from memvane.runtime.types import ResourceDeferred

POLICY = {'maxRssMib': 512, 'minAvailableMib': 768, 'maxLoadPerCpu': 1}


class GateBusy(FixtureCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / 'state'
        self.root.mkdir(mode=0o700)
        self.report = {'ready': True, 'observed_at_mono': 0., 'total_mib': 16000, 'available_mib': 12000,
                       'load_per_cpu': .1, 'query_status': {'memory_pressure': 'ok'},
                       'memory_pressure': {'level': 'normal', 'system_free_percent': 70}}

    def held(self, name):
        fd = os.open(self.root / name, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, fd)

    def refused(self, enter):
        with self.assertRaises(ResourceDeferred) as caught:
            enter()
        return str(caught.exception), caught.exception.report

    def test_each_held_gate_is_named_in_the_report(self):
        controller = Coordinator(self.root, clock=lambda: 0)

        def provider():
            with controller.activity('provider', POLICY, self.report):
                self.fail('provider lane admitted while held')

        def trainer():
            with controller.training(POLICY, self.report):
                self.fail('trainer admitted while held')
        self.held('provider.lock')
        self.assertEqual(self.refused(provider), ('learning_compute_busy', {'gate': 'provider'}))
        self.held('trainer.lock')
        self.assertEqual(self.refused(trainer), ('learning_compute_busy', {'gate': 'trainer'}))
        self.held('control.lock')
        self.assertEqual(self.refused(lambda: controller.check(POLICY, self.report)),
                         ('learning_compute_busy', {'gate': 'control'}))

"""1.0.22 G-1 r2: a coordinated trainer child resolves the same runtime root as its parent.

The worker command (``project_worker.run`` → ``run_training_pending`` →
``automatic_staging._trainer`` → ``LocalTrainer``) starts a real trainer child
with the coordinator's ``learning_control`` and lease fd. The child is a small
script standing in for the adapter: it builds the runtime ``TrainingGuard``
from the request exactly as ``adapter._training_guard`` does and writes the
adapter's result or error sidecar. The focused harness sets
``MEMVANE_TEST_RUNTIME_ROOT`` like every guarded test run.
"""
from __future__ import annotations

import json
import os
import sys
from unittest import mock

from memvane.learning.context_learning import automatic_staging
from memvane.learning.context_learning.local_cycle import LocalTrainer
from memvane.learning.tests.integration.test_worker_teacher_121 import WorkerFixture
from memvane.learning.tests.support import REPO

REAL_TRAINER = automatic_staging._trainer
CHILD = r'''
import json, os, sys
sys.path.insert(0, %r)
mode, request_path, pending = sys.argv[1:4]
request = json.load(open(request_path))
def write(value):
    with open(pending, 'w') as out:
        json.dump(value, out)
    os.chmod(pending, 0o600)
try:
    from memvane.runtime.api import training_guard
    training_guard(request['learning_control'])
except ValueError as exc:
    write({'schema_version': 1, 'error_code': str(exc)})
    sys.exit(1)
with open(request['artifact_path'], 'wb') as out:
    out.write(b'candidate')
os.chmod(request['artifact_path'], 0o600)
write({'schema_version': 1, 'status': 'trained'})
''' % str(REPO)


class CoordinatedTrainerChild(WorkerFixture):
    def setUp(self):
        super().setUp()
        self.assertTrue(os.environ.get('MEMVANE_TEST_RUNTIME_ROOT'), 'guarded runs set the test runtime root')
        script = self.home / 'trainer_child.py'
        script.write_text(CHILD)
        self.children = []

        def trainer(policy, factory, **kwargs):
            def make(policy):
                child = LocalTrainer([sys.executable, '-B', str(script)], max_seconds=60, max_rss_mib=4096)
                self.children.append(child)
                return child
            return REAL_TRAINER(policy, make, **kwargs)
        self.patches.enter_context(mock.patch.object(automatic_staging, '_trainer', side_effect=trainer))

    def test_worker_training_child_accepts_the_learning_control(self):
        self.golds('g', 100, empty=20)
        result = self.worker()
        self.assertEqual(result['cycle']['status'], 'staged_waiting_quality_cohort', json.dumps(result['cycle']))
        self.assertEqual(len(self.children), 1)
        self.assertEqual((self.children[0].last_exit_code, self.children[0].last_error_code), (0, None))
        self.assertEqual(len(self.jobs()), 1)

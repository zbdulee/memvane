"""1.0.19: a host plugin listing past its bound defers setup with a fixed code.

``setup_entry.main`` maps ``subprocess.TimeoutExpired`` and
``RuntimeError('host_inventory_unavailable')`` (``HealDeferred``) to
``deferred``/``host_inventory_unavailable`` (exit 75) without a traceback or
the CLI command line. One case reaches the real inventory: a synthetic
``codex`` on a private PATH that never answers (never the real CLI).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
from unittest import mock

from tests.support.case import FixtureCase
from memvane.app import setup_entry
from memvane.learning.context_learning import host_inventory, host_self, selector_heal

DEFERRED = {'status': 'deferred', 'reason': 'host_inventory_unavailable', 'changesApplied': 0}
HANG = '''#!/bin/sh
if [ "$1" = "--warm" ]; then exit 0; fi
echo "$*" >> '{counter}'
exec /bin/sleep 60
'''


class SetupInventoryDeferral(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = self.home / 'project'
        self.project.mkdir(mode=0o700)
        self.namespace = self.home / 'namespace'
        for target, kwargs in (
                (os.environ, {'values': {'MEMVANE_TEST_ALLOW_UNINSTALLED_ROOT': '1'}}),
                (host_inventory._SETTLED, {'values': {}, 'clear': True}),
                (host_inventory._MODE, {'values': {'maintenance': False, 'refreshing': False}})):
            patcher = mock.patch.dict(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        registry = mock.patch('memvane.registry.api.after_setup')
        registry.start()
        self.addCleanup(registry.stop)

    def call(self, action, *options):
        out, err = io.StringIO(), io.StringIO()
        argv = [action, '--project', str(self.project), '--learning-choice', 'local-staged',
                '--learning-namespace', str(self.namespace)] + list(options)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = setup_entry.main(argv)
        return code, out.getvalue(), err.getvalue()

    def assert_deferred(self, action, *options):
        code, out, err = self.call(action, *options)
        self.assertEqual((code, json.loads(out)), (75, DEFERRED))
        self.assertEqual(err, '')
        self.assertNotIn('Traceback', out)
        self.assertNotIn('plugin', out)

    # The real project listing with a CLI that never answers: its TimeoutExpired defers setup.
    def test_hanging_inventory_defers_plan(self):
        bin_dir = self.home / 'bin'
        bin_dir.mkdir(mode=0o700)
        counter = self.home / 'codex.count'
        codex = bin_dir / 'codex'
        codex.write_text(HANG.format(counter=counter))
        codex.chmod(0o700)
        subprocess.run([str(codex), '--warm'], check=True, timeout=30)
        raised = []

        def propose(project, root, **kwargs):
            try:
                return host_self.selection(project, host='codex')
            except BaseException as exc:
                raised.append(type(exc))
                raise
        with mock.patch.dict(os.environ, {'PATH': os.pathsep.join((str(bin_dir), '/usr/bin', '/bin'))}), \
                mock.patch.object(host_inventory, 'PROJECT_LISTING_SECONDS', 1), \
                mock.patch('memvane.learning.api.propose_learning_profile', side_effect=propose):
            os.environ.pop('CODEX_HOME', None)
            os.environ.pop('CLAUDE_CONFIG_DIR', None)
            self.assert_deferred('plan-learning-profile')
        self.assertEqual(raised, [subprocess.TimeoutExpired])
        self.assertEqual(counter.read_text().splitlines(), ['plugin list --json'])

    # Each deferral form, for the read-only plan and the writing activation.
    def test_inventory_errors_defer_plan_and_activation(self):
        errors = (subprocess.TimeoutExpired(['codex', 'plugin', 'list', '--json'], 120),
                  RuntimeError('host_inventory_unavailable'),
                  selector_heal.HealDeferred('host_inventory_unavailable'))
        for error in errors:
            with self.subTest(type(error).__name__):
                with mock.patch('memvane.learning.api.propose_learning_profile', side_effect=error) as plan:
                    self.assert_deferred('plan-learning-profile')
                self.assertEqual(plan.call_count, 1)
                with mock.patch('memvane.learning.api.activate_learning_profile', side_effect=error) as act:
                    self.assert_deferred('activate-learning-profile', '--accepted-learning-profile', 'a' * 64)
                self.assertEqual(act.call_count, 1)

    # Other runtime errors stay rejections.
    def test_other_runtime_error_is_rejected(self):
        with mock.patch('memvane.learning.api.propose_learning_profile',
                        side_effect=RuntimeError('host_inventory_other')):
            code, out, _err = self.call('plan-learning-profile')
        row = json.loads(out)
        self.assertEqual((code, row['status'], row['reason']), (1, 'rejected', 'host_inventory_other'))

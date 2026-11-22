"""A ``learning`` wake is dispatched only by the worker of the highest installed bundle.

Two bundles are installed. The worker of the older one leaves the wake as it
is and ends (``bundle_changed``); the worker of the higher one runs it. The
learning worker therefore judges candidates with the newest bundle's rules.
"""
import json
import os
from unittest import mock

from tests.support.case import FixtureCase

from memvane.runtime import host_bundle
from memvane.runtime import runtime_work as runtime


class LearningWakeBundle(FixtureCase):
    def bundle(self, version):
        root = self.home / '.codex/plugins/cache/market/memvane' / version
        (root / 'entry').mkdir(parents=True)
        (root / '.codex-plugin').mkdir()
        for path in (root / 'plugin.json', root / 'package.json', root / '.codex-plugin/plugin.json'):
            path.write_text(json.dumps({'name': 'memvane', 'version': version}), encoding='utf8')
        (root / 'entry/run.py').write_text('# synthetic entry\n', encoding='utf8')
        return root

    def drain(self, bundle, ran):
        def execute(row):
            ran.append((row['kind'], row['target']))
            return True
        with mock.patch.dict(os.environ, {'MEMVANE_TEST_RUNTIME_ROOT': ''}), \
                mock.patch.object(host_bundle, 'background_installed', return_value=self.newer), \
                mock.patch.object(runtime, 'BACKGROUND_BUNDLE', host_bundle.BundleIdentity(bundle)), \
                mock.patch.object(runtime, '_uninstalled', return_value=False):
            return runtime.drain(self.queue, execute=execute, ready=lambda: True, clock=lambda: 0,
                                 wall=lambda: 100, sleep=lambda _: None, max_runs=1)

    def test_only_the_highest_bundle_runs_the_learning_wake(self):
        older, self.newer = self.bundle('1.0.24'), self.bundle('1.1.0')
        root = self.tmp / 'state'
        root.mkdir(mode=0o700)
        self.queue = runtime.WakeQueue(root)
        self.queue.put('learning', '/synthetic/project', now=0)
        ran = []
        self.assertEqual(self.drain(older, ran), {'status': 'bundle_changed', 'runs': 0})
        row, = self.queue.pending()
        self.assertEqual((ran, row['kind'], row['status'], row['attempts']), ([], 'learning', 'pending', 0))
        self.assertEqual(self.drain(self.newer, ran), {'status': 'drained', 'runs': 1})
        self.assertEqual((ran, self.queue.pending()), ([('learning', '/synthetic/project')], []))

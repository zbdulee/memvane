"""No host has MemVane enabled: background processes end and arm no successor.

Synthetic host records under a fixture HOME. The grace is driven through the
inventory's own first-sight stamp, never a wall clock; every check counts
spawns, rows and states.
"""
import json
import os
import subprocess
import types
from unittest import mock

from tests.support.case import FixtureCase

from memvane.runtime import host_bundle

LIVE = {'MEMVANE_TEST_RUNTIME_ROOT': '', 'QMD_SETUP_GUARD_FIXTURE': '0'}
NONE = {'installed': []}


class Uninstalled(FixtureCase):
    def setUp(self):
        super().setUp()
        self.reset()
        self.addCleanup(self.reset)
        environment = mock.patch.dict(os.environ, dict(
            LIVE, CODEX_HOME=str(self.home / '.codex'), CLAUDE_CONFIG_DIR=str(self.home / '.claude'),
            HERMES_HOME=str(self.home / '.hermes')))
        environment.start()
        self.addCleanup(environment.stop)

    def reset(self):
        host_bundle._BACKGROUND_INVENTORY.clear()
        host_bundle._BACKGROUND_INVENTORY.update(until=0.0, root=None)

    def bundle(self, version='1.1.0'):
        root = self.home / '.codex' / 'plugins' / 'cache' / 'market' / 'memvane' / version
        (root / 'entry').mkdir(parents=True)
        (root / '.codex-plugin').mkdir()
        for path in (root / 'plugin.json', root / 'package.json', root / '.codex-plugin' / 'plugin.json'):
            path.write_text(json.dumps({'name': 'memvane', 'version': version}), encoding='utf8')
        (root / 'entry' / 'run.py').write_text('# synthetic entry\n', encoding='utf8')
        return root

    def codex(self, listing):
        if isinstance(listing, BaseException):
            return mock.patch.object(host_bundle, '_codex_inventory', side_effect=listing)
        return mock.patch.object(host_bundle, '_codex_inventory', return_value=listing)

    def aged(self, seconds):
        """The absence was first seen ``seconds`` ago."""
        since = host_bundle._BACKGROUND_INVENTORY.get('absentSince')
        self.assertIsNotNone(since)
        host_bundle._BACKGROUND_INVENTORY['absentSince'] = since - seconds

    def test_only_a_fully_read_empty_inventory_counts_as_absent(self):
        root = self.bundle()
        enabled = {'installed': [{'name': 'memvane', 'marketplaceName': 'market', 'version': '1.1.0',
                                  'installed': True, 'enabled': True}]}
        disabled = {'installed': [dict(enabled['installed'][0], enabled=False)]}
        for listing, absent in ((enabled, False), (disabled, True), (NONE, True),
                                (ValueError('bundle_inventory_unavailable'), False),
                                (subprocess.TimeoutExpired('codex', 1), False)):
            self.reset()
            with self.codex(listing):
                self.assertEqual(host_bundle.background_absent_seconds() is not None, absent, listing)
        # A Claude record that cannot be read leaves the answer unknown although Codex lists nothing.
        plugins = self.home / '.claude' / 'plugins'
        plugins.mkdir(parents=True)
        (plugins / 'installed_plugins.json').write_text('{"plugins": 3}', encoding='utf8')
        self.reset()
        with self.codex(NONE):
            self.assertIsNone(host_bundle.background_absent_seconds())
            self.assertFalse(host_bundle.BundleIdentity(root).uninstalled())

    def test_the_absence_must_outlast_the_grace(self):
        identity = host_bundle.BundleIdentity(self.bundle())
        with self.codex(NONE) as listing:
            self.assertFalse(identity.uninstalled())
            self.aged(host_bundle.UNINSTALLED_GRACE_SECONDS - 1)
            self.assertFalse(identity.uninstalled())
            self.aged(2)
            self.assertTrue(identity.uninstalled())
            self.assertEqual(listing.call_count, 1)  # The host records did not change: no second listing.
        # An unreadable inventory after that says nothing, and the next absence is timed afresh.
        self.reset()
        with self.codex(ValueError('bundle_inventory_unavailable')):
            self.assertFalse(identity.uninstalled())
        self.reset()
        with self.codex(NONE):
            self.assertFalse(identity.uninstalled())

    def test_a_reinstalled_bundle_ends_the_absence(self):
        identity = host_bundle.BundleIdentity(self.bundle())
        with self.codex(NONE):
            identity.uninstalled()
            self.aged(host_bundle.UNINSTALLED_GRACE_SECONDS + 1)
            self.assertTrue(identity.uninstalled())
        host_bundle._BACKGROUND_INVENTORY['until'] = 0.0
        enabled = {'installed': [{'name': 'memvane', 'marketplaceName': 'market', 'version': '1.1.0',
                                  'installed': True, 'enabled': True}]}
        with self.codex(enabled):
            self.assertFalse(identity.uninstalled())
            self.assertIsNone(host_bundle.background_absent_seconds())

    def test_fixtures_and_bundles_outside_the_resolved_host_roots_are_never_uninstalled(self):
        root = self.bundle()
        with self.codex(NONE):
            host_bundle.background_absent_seconds()
            self.aged(host_bundle.UNINSTALLED_GRACE_SECONDS + 1)
            self.assertTrue(host_bundle.BundleIdentity(root).uninstalled())
            for name, value in (('MEMVANE_TEST_RUNTIME_ROOT', str(self.tmp)), ('QMD_SETUP_GUARD_FIXTURE', '1')):
                with mock.patch.dict(os.environ, {name: value}):
                    self.assertFalse(host_bundle.BundleIdentity(root).uninstalled())
            # This process resolves another Codex home: its inventory did not judge that bundle.
            with mock.patch.dict(os.environ, {'CODEX_HOME': str(self.home / 'elsewhere')}):
                self.assertFalse(host_bundle.BundleIdentity(root).uninstalled())


class Processes(FixtureCase):
    """The wake worker, timer and monitor leave the queue as it is and start nothing."""

    def identity(self, uninstalled, intact=True):
        return types.SimpleNamespace(intact=lambda: intact, preferred_background=lambda: intact,
                                     uninstalled=lambda: uninstalled)

    def queue(self):
        from memvane.runtime import runtime_work as runtime
        queue = runtime.WakeQueue()
        queue.put('laya-sync', '/synthetic/project', now=0)
        return runtime, queue

    def test_the_timer_ends_without_a_successor(self):
        runtime, queue = self.queue()
        for intact in (True, False):
            with mock.patch.object(runtime, 'BACKGROUND_BUNDLE', self.identity(True, intact)), \
                    mock.patch.object(runtime, '_wake', side_effect=AssertionError('spawned')) as wake:
                outcome = runtime.timer(queue, wall=lambda: 1, clock=lambda: 0.0,
                                        sleep=lambda _seconds: self.fail('waited'), max_seconds=1)
                runtime._after_timer(queue, outcome)
            self.assertEqual((outcome, wake.call_count, len(queue.pending())), ('uninstalled', 0, 1))

    def test_a_removed_bundle_still_waits_while_the_inventory_is_unknown(self):
        runtime, queue = self.queue()
        tick = [0.0]
        with mock.patch.object(runtime, 'BACKGROUND_BUNDLE', self.identity(False, intact=False)), \
                mock.patch.object(host_bundle, 'background_installed', return_value=None):
            outcome = runtime.timer(queue, wall=lambda: 1, clock=lambda: tick[0],
                                    sleep=lambda _seconds: tick.__setitem__(0, tick[0] + .4), max_seconds=1)
        self.assertEqual((outcome, len(queue.pending())), ('expired', 1))

    def test_the_worker_claims_nothing_and_arms_nothing(self):
        runtime, queue = self.queue()
        lease = runtime._lock(queue.root / 'runtime-worker.lock')
        with mock.patch.object(runtime, 'BACKGROUND_BUNDLE', self.identity(True)), \
                mock.patch.object(runtime, '_wake', side_effect=AssertionError('spawned')) as wake, \
                mock.patch.object(runtime, 'wake', side_effect=AssertionError('spawned')) as rewake:
            result = runtime._drain_and_rewake(queue, lease, execute=lambda _row: self.fail('claimed'))
        self.assertEqual(result, {'status': 'uninstalled', 'runs': 0})
        self.assertEqual((wake.call_count, rewake.call_count, len(queue.pending())), (0, 0, 1))

    def test_the_monitor_ends(self):
        from memvane.runtime import runtime_monitor as monitor
        runtime, queue = self.queue()
        with mock.patch.object(monitor, 'BACKGROUND_BUNDLE', self.identity(True)):
            result = monitor.run(queue, alive=lambda: True, wait=lambda _seconds: self.fail('waited'),
                                 wake=lambda: self.fail('woke a worker'))
        self.assertEqual((result['status'], result['wakes'], len(queue.pending())), ('uninstalled', 0, 1))

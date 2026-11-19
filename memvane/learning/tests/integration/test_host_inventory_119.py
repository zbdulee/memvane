"""1.0.19: host plugin listings are recorded, bounded and never escape as a timeout.

Host CLIs are synthetic shell scripts on a private PATH (never the real
``codex``/``claude``). Each run appends one line to its counter file, and a
mode file outside the host installation records selects ``ok`` (print the
listing), ``fail`` (exit 3) or ``hang`` (block until the bounded run ends it),
so switching modes never moves the records stamp. Assertions are counts,
notes, files and events only.

``InventoryMaintenance`` runs the real ``selector-maintenance`` entry with real
renewal, heal ordering and receipt publication (``AdapterUpdate`` fixture) and
the real ``_project_listing`` for the hosts named per test.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import subprocess
import time
import unittest
from unittest import mock

from tests.support.case import FixtureCase
from memvane.learning.context_learning import host_inventory, host_self, project_scope, selector_heal
from memvane.learning.tests.integration import test_adapter_update_119 as base

# Captured at import, before RenewalFixture replaces it for each test.
REAL_LISTING = host_inventory._project_listing
CODEX_LISTING = {'installed': [{'name': 'memvane', 'pluginId': 'memvane@memvane-marketplace',
                                'marketplaceName': 'memvane-marketplace', 'version': '1.0.19',
                                'installed': True, 'enabled': True}]}
SCRIPT = '''#!/bin/sh
echo "$*" >> '{counter}'
read mode < '{mode}'
case "$mode" in
  ok) exec /bin/cat '{listing}' ;;
  fail) exit 3 ;;
  hang) exec /bin/sleep 60 ;;
esac
exit 4
'''


class FakeHosts:
    """Synthetic host CLIs with per-host counters, listings and modes."""

    def __init__(self, home):
        self.home = home
        self.bin = home / 'bin'
        self.bin.mkdir(mode=0o700, exist_ok=True)
        self.state = home / 'cli-state'
        self.state.mkdir(mode=0o700, exist_ok=True)
        for host in ('codex', 'claude'):
            script = self.bin / host
            script.write_text(SCRIPT.format(counter=self.state / (host + '.count'),
                                            mode=self.state / (host + '.mode'),
                                            listing=self.state / (host + '.json')))
            script.chmod(0o700)
            self.mode(host, 'fail')

    def mode(self, host, value):
        (self.state / (host + '.mode')).write_text(value + '\n')

    def listing(self, host, value):
        (self.state / (host + '.json')).write_text(json.dumps(value))

    def runs(self, host):
        path = self.state / (host + '.count')
        if not path.exists():
            return 0
        lines = path.read_text().splitlines()
        assert all(line == 'plugin list --json' for line in lines), lines
        return len(lines)


@contextmanager
def host_environment(home):
    """Only the fake CLIs and system directories on PATH; no redirected config homes."""
    with mock.patch.dict(os.environ):
        os.environ['PATH'] = os.pathsep.join((str(home / 'bin'), '/usr/bin', '/bin'))
        for name in ('CLAUDE_CONFIG_DIR', 'CODEX_HOME'):
            os.environ.pop(name, None)
        with mock.patch.dict(host_inventory._SETTLED, clear=True):
            del host_inventory._NOTES[:]
            try:
                yield
            finally:
                del host_inventory._NOTES[:]


@contextmanager
def run_errors():
    """Record what ``subprocess.run`` raised, to prove which failure path ran."""
    raised = []
    real = subprocess.run

    def run(*args, **kwargs):
        try:
            return real(*args, **kwargs)
        except BaseException as exc:
            raised.append(type(exc))
            raise
    with mock.patch.object(host_inventory.subprocess, 'run', side_effect=run):
        yield raised


class HostInventoryCache(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = self.home / 'project'
        self.project.mkdir(mode=0o700)
        self.hosts = FakeHosts(self.home)
        self.hosts.listing('codex', CODEX_LISTING)
        env = host_environment(self.home)
        env.__enter__()
        self.addCleanup(env.__exit__, None, None, None)
        self.argv = ('codex', 'plugin', 'list', '--json')
        self.cache = host_inventory._cache_path(self.home, 'codex', self.project)
        self.config = self.home / '.codex' / 'config.toml'

    def listing(self):
        return REAL_LISTING(self.argv, self.home, self.project)

    def touch_record(self):
        self.config.parent.mkdir(mode=0o700, exist_ok=True)
        with open(self.config, 'a') as out:
            out.write('# change\n')

    def seed(self):
        self.hosts.mode('codex', 'ok')
        self.assertEqual(self.listing(), CODEX_LISTING)
        self.assertEqual(host_inventory.take_notes(), [('codex', 'refreshed')])
        self.assertTrue(self.cache.is_file())
        return self.hosts.runs('codex')

    def deferred(self):
        with self.assertRaises(selector_heal.HealDeferred) as caught:
            self.listing()
        self.assertEqual(str(caught.exception), 'host_inventory_unavailable')

    # 1. One CLI run per records stamp; the record survives the process memory.
    def test_listing_is_recorded_and_reused_until_a_record_changes(self):
        self.hosts.mode('codex', 'ok')
        self.assertEqual(self.listing(), CODEX_LISTING)
        self.assertEqual(self.hosts.runs('codex'), 1)
        self.assertEqual(host_inventory.take_notes(), [('codex', 'refreshed')])
        info = self.cache.lstat()
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertEqual(info.st_uid, os.getuid())
        row = json.loads(self.cache.read_text())
        self.assertEqual(row['schema'], host_inventory.CACHE_SCHEMA)
        self.assertEqual((row['host'], row['project'], row['listing']),
                         ('codex', str(self.project), CODEX_LISTING))
        self.assertEqual(row['executable'], str((self.home / 'bin/codex').resolve()))
        self.assertEqual(stat.S_IMODE(self.cache.parent.lstat().st_mode), 0o700)
        # A new process: nothing settled in memory, only the recorded listing.
        host_inventory._SETTLED.clear()
        self.hosts.mode('codex', 'fail')
        for _ in range(2):
            self.assertEqual(self.listing(), CODEX_LISTING)
        self.assertEqual(self.hosts.runs('codex'), 1)
        self.assertEqual(host_inventory.take_notes(), [])
        # A host installation record changes: the CLI runs again.
        self.hosts.mode('codex', 'ok')
        self.touch_record()
        self.assertEqual(self.listing(), CODEX_LISTING)
        self.assertEqual(self.hosts.runs('codex'), 2)
        self.assertEqual(host_inventory.take_notes(), [('codex', 'refreshed')])
        self.assertNotEqual(json.loads(self.cache.read_text())['stamp'], row['stamp'])
        # A plugin cache entry is a record too.
        (self.home / '.codex/plugins/cache/memvane-marketplace/memvane/1.0.19').mkdir(parents=True)
        self.assertEqual(self.listing(), CODEX_LISTING)
        self.assertEqual(self.hosts.runs('codex'), 3)

    # 2a. A timeout with a recent listing serves it as stale, once per stamp.
    def test_timeout_with_recent_listing_is_stale_and_settled(self):
        seeded = self.seed()
        recorded = self.cache.read_bytes()
        self.touch_record()
        self.hosts.mode('codex', 'hang')
        with mock.patch.object(host_inventory, 'INVENTORY_SECONDS', 1), run_errors() as raised:
            self.assertEqual(self.listing(), CODEX_LISTING)
            self.assertEqual(raised, [subprocess.TimeoutExpired])
            self.assertEqual(self.hosts.runs('codex'), seeded + 1)
            self.assertEqual(host_inventory.take_notes(), [('codex', 'stale')])
            # Same stamp: the settled result, no second wait.
            self.assertEqual(self.listing(), CODEX_LISTING)
            self.assertEqual(self.hosts.runs('codex'), seeded + 1)
            self.assertEqual(host_inventory.take_notes(), [])
            # The stale use never rewrites the record.
            self.assertEqual(self.cache.read_bytes(), recorded)
            # Another record change retries the CLI.
            self.touch_record()
            self.assertEqual(self.listing(), CODEX_LISTING)
            self.assertEqual(self.hosts.runs('codex'), seeded + 2)
            self.assertEqual(raised, [subprocess.TimeoutExpired] * 2)
            self.assertEqual(host_inventory.take_notes(), [('codex', 'stale')])

    # 2b. A failing CLI (non-zero exit) is handled like a timeout.
    def test_failing_cli_with_recent_listing_is_stale(self):
        seeded = self.seed()
        self.touch_record()
        self.hosts.mode('codex', 'fail')
        self.assertEqual(self.listing(), CODEX_LISTING)
        self.assertEqual(self.hosts.runs('codex'), seeded + 1)
        self.assertEqual(host_inventory.take_notes(), [('codex', 'stale')])

    # 2c. No listing at all: deferred, not repeated within the stamp.
    def test_timeout_without_listing_defers_once_per_stamp(self):
        self.hosts.mode('codex', 'hang')
        with mock.patch.object(host_inventory, 'INVENTORY_SECONDS', 1), run_errors() as raised:
            self.deferred()
            self.assertEqual(raised, [subprocess.TimeoutExpired])
            self.assertEqual(self.hosts.runs('codex'), 1)
            self.assertEqual(host_inventory.take_notes(), [('codex', 'unavailable')])
            self.assertFalse(self.cache.exists())
            self.deferred()
            self.assertEqual(self.hosts.runs('codex'), 1)
            self.assertEqual(host_inventory.take_notes(), [])
            self.touch_record()
            self.deferred()
            self.assertEqual(self.hosts.runs('codex'), 2)
            self.assertEqual(host_inventory.take_notes(), [('codex', 'unavailable')])
        # Once the CLI answers again, the listing is recorded.
        self.touch_record()
        self.hosts.mode('codex', 'ok')
        self.assertEqual(self.listing(), CODEX_LISTING)
        self.assertEqual(self.hosts.runs('codex'), 3)
        self.assertTrue(self.cache.is_file())

    # 2d. A listing older than STALE_SECONDS does not stand in.
    def test_listing_older_than_stale_window_defers(self):
        self.seed()
        row = json.loads(self.cache.read_text())
        row['recordedAt'] = time.time() - host_inventory.STALE_SECONDS - 3600
        self.cache.write_text(json.dumps(row))
        self.assertEqual(stat.S_IMODE(self.cache.lstat().st_mode), 0o600)
        self.touch_record()
        self.hosts.mode('codex', 'fail')
        self.deferred()
        self.assertEqual(host_inventory.take_notes(), [('codex', 'unavailable')])

    # 2e. The settled result expires after SETTLE_SECONDS even with the same stamp.
    def test_settled_failure_expires(self):
        self.hosts.mode('codex', 'fail')
        self.deferred()
        self.assertEqual(self.hosts.runs('codex'), 1)
        with mock.patch.object(host_inventory, 'SETTLE_SECONDS', -1):
            self.deferred()
        self.assertEqual(self.hosts.runs('codex'), 2)

    # 3. TimeoutExpired never escapes the callers that read the listing.
    def test_timeout_never_escapes_selection_or_installations(self):
        # _project_host_roots asks Claude first; both hosts time out.
        self.hosts.mode('codex', 'hang')
        self.hosts.mode('claude', 'hang')
        adapter = (self.home / '.codex/plugins/cache/memvane-marketplace/memvane/1.0.19'
                   / 'memvane/learning/context_learning/adapter.py')
        calls = (
            ('_project_listing', self.listing),
            ('host_self.selection', lambda: host_self.selection(self.project, host='codex')),
            ('project_scope._local_installations', lambda: project_scope._local_installations(self.project)),
            ('_project_host_roots', lambda: host_inventory._project_host_roots(self.home, self.project)),
        )
        with mock.patch.object(host_inventory, 'INVENTORY_SECONDS', 1), \
                mock.patch.object(selector_heal, 'running_adapter', return_value=adapter):
            for name, call in calls:
                with self.subTest(name):
                    host_inventory._SETTLED.clear()
                    before = self.hosts.runs('codex') + self.hosts.runs('claude')
                    with run_errors() as raised:
                        try:
                            call()
                        except subprocess.TimeoutExpired:
                            self.fail(name + ' let TimeoutExpired escape')
                        except (selector_heal.HealDeferred, ValueError) as exc:
                            if isinstance(exc, selector_heal.HealDeferred):
                                self.assertEqual(str(exc), 'host_inventory_unavailable')
                        else:
                            self.fail(name + ' returned without a listing')
                    # One bounded run; the first deferral stops the caller.
                    self.assertEqual(raised, [subprocess.TimeoutExpired])
                    self.assertEqual(self.hosts.runs('codex') + self.hosts.runs('claude'), before + 1)

    # Claude's ``plugin list --json`` prints a top-level JSON array (the shape
    # ``_project_claude_selection`` parses first); the record keeps it as is.
    def test_claude_array_listing_is_recorded_and_reused(self):
        rows = [{'id': 'memvane@memvane-marketplace', 'enabled': True, 'version': '1.0.19',
                 'installPath': str(self.home / '.claude/plugins/cache/x'), 'scope': 'user'}]
        self.hosts.listing('claude', rows)
        self.hosts.mode('claude', 'ok')
        argv = ('claude', 'plugin', 'list', '--json')
        self.assertEqual(REAL_LISTING(argv, self.home, self.project), rows)
        host_inventory._SETTLED.clear()
        self.hosts.mode('claude', 'fail')
        self.assertEqual(REAL_LISTING(argv, self.home, self.project), rows)
        self.assertEqual(self.hosts.runs('claude'), 1)


class InventoryMaintenance(base.AdapterUpdate):
    """The real selector-maintenance entry with a host CLI that times out."""

    def setUp(self):
        super().setUp()
        self.select()
        self.hosts = FakeHosts(self.home)
        self.hosts.listing('codex', {'installed': [dict(self.codex)]})
        env = host_environment(self.home)
        env.__enter__()
        self.addCleanup(env.__exit__, None, None, None)
        self.real_hosts = {'codex'}

        def listing(argv, account, project):
            if argv[0] in self.real_hosts:
                return REAL_LISTING(argv, account, project)
            return self.listing(argv, account, project)
        patcher = mock.patch.object(host_inventory, '_project_listing', side_effect=listing)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(host_inventory, 'INVENTORY_SECONDS', 1)
        patcher.start()
        self.addCleanup(patcher.stop)

    def seed(self):
        self.hosts.mode('codex', 'ok')
        self.assertEqual(host_inventory.refresh(self.project), [('codex', 'refreshed')])
        self.assertEqual(self.hosts.runs('codex'), 1)
        host_inventory._SETTLED.clear()  # The maintenance run is a new process.

    def events(self):
        from memvane.diagnostics import api
        return [(call.args[0], call.kwargs) for call in api.emit.call_args_list if call.args]

    def run_maintenance(self):
        from memvane.diagnostics import api
        api.emit.reset_mock()
        with self.updating() as log:
            self.maintenance()
        events = self.events()
        inventory = [(row['outcome'], row['reason'], row['inventoryHost'])
                     for name, row in events if name == 'learning.selector.inventory']
        maintenance = [(row.get('outcome'), row.get('reason'))
                       for name, row in events if name == 'learning.selector.maintenance']
        self.assertNotIn('failed', [outcome for outcome, _ in maintenance])
        return log, events, inventory, maintenance

    def assert_completed(self, log, events, maintenance):
        self.assert_prepared(log)
        self.assertEqual(self.published, [base.RUNNING])
        self.assertEqual(maintenance[0], ('started', None))
        self.assertEqual(maintenance[-1], ('completed', None))
        self.assertIn(('learning.selector.warm', 'selector_warm_current'),
                      [(name, row.get('outcome')) for name, row in events])

    # 4. A recorded listing for unchanged records: no CLI run at all.
    def test_recorded_listing_completes_without_cli(self):
        self.seed()
        self.hosts.mode('codex', 'hang')
        log, events, inventory, maintenance = self.run_maintenance()
        self.assert_completed(log, events, maintenance)
        self.assertEqual(self.hosts.runs('codex'), 1)
        self.assertEqual(inventory, [])

    # 5. Records changed and only a recent listing: one bounded run, then stale.
    def test_changed_records_with_recent_listing_completes_stale(self):
        self.seed()
        config = self.home / '.codex/config.toml'
        config.write_text('# changed\n')
        self.hosts.mode('codex', 'hang')
        log, events, inventory, maintenance = self.run_maintenance()
        self.assert_completed(log, events, maintenance)
        self.assertEqual(self.hosts.runs('codex'), 2)
        self.assertEqual(inventory, [('stale', 'host_inventory_stale', 'codex')])

    # 6. No listing anywhere: deferred, not failed; each CLI waited at most once.
    def test_no_listing_defers_maintenance(self):
        self.real_hosts = {'codex', 'claude'}
        for host in ('codex', 'claude'):
            self.hosts.mode(host, 'hang')
        before = self.targets()
        log, events, inventory, maintenance = self.run_maintenance()
        self.assertEqual(sorted(inventory), [('unavailable', 'host_inventory_unavailable', 'claude'),
                                             ('unavailable', 'host_inventory_unavailable', 'codex')])
        self.assertEqual(maintenance[0], ('started', None))
        self.assertEqual(maintenance[-1], ('deferred', 'host_inventory_unavailable'))
        self.assertNotIn('maintenance_failed', [reason for _, reason in maintenance])
        for host in ('codex', 'claude'):
            self.assertEqual(self.hosts.runs(host), 1, host)
        self.assertEqual(log.steps, [])
        self.assertEqual(self.published, [])
        self.assertEqual(self.targets(), before)


def load_tests(loader, standard_tests, pattern):
    """Only the tests defined here, not the inherited AdapterUpdate tests."""
    suite = unittest.TestSuite()
    for case in (HostInventoryCache, InventoryMaintenance):
        names = [name for name in loader.getTestCaseNames(case) if name in vars(case)]
        suite.addTests(case(name) for name in names)
    return suite


if __name__ == '__main__':
    unittest.main()

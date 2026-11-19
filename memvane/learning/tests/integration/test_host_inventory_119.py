"""1.0.19: host plugin listings are recorded, bounded and never escape maintenance as a timeout.

Host CLIs are synthetic shell scripts on a private PATH (never the real
``codex``/``claude``). Each run appends its arguments to a counter file, and a
mode file outside the host installation records selects ``ok`` (print the
listing), ``fail`` (exit 3) or ``hang`` (block until the bounded run ends it),
so switching modes never moves the records stamp. Assertions are counts,
notes, files and events only.

Two modes (``host_inventory._MODE``): outside selector maintenance a record
or a recent record stands in without the CLI, and only a missing record runs
the CLI with the caller's earlier short bound and its earlier error; inside
``refresh`` the CLI waits up to INVENTORY_SECONDS and a failure becomes stale
or ``HealDeferred('host_inventory_unavailable')``.

``InventoryMaintenance`` runs the real ``selector-maintenance`` entry from the
Codex host's cache copy with real renewal (``default_renewal_plan`` ->
``selector_heal._official`` -> ``_installed_current`` -> ``_codex_installed``),
heal ordering and receipt publication (``AdapterUpdate`` fixture).
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
from memvane.learning.context_learning import (adapter_binding, host_inventory, host_self,
                                               project_scope, selector_heal)
from memvane.learning.tests.integration import test_adapter_update_119 as base

# Captured at import, before RenewalFixture replaces them for each test.
REAL_LISTING = host_inventory._project_listing
REAL_CODEX_INSTALLED = adapter_binding._codex_installed
PROJECT_ARGS = 'plugin list --json'
MARKET_ARGS = 'plugin list --marketplace memvane-marketplace --json'
VERSION = '1.0.19'
CODEX_ROW = {'name': 'memvane', 'pluginId': 'memvane@memvane-marketplace',
             'marketplaceName': 'memvane-marketplace', 'version': VERSION,
             'installed': True, 'enabled': True,
             'source': {'source': 'github', 'repo': adapter_binding.REPOSITORY}}
CODEX_LISTING = {'installed': [CODEX_ROW]}
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

    def runs(self, host, args=PROJECT_ARGS):
        path = self.state / (host + '.count')
        if not path.exists():
            return 0
        lines = path.read_text().splitlines()
        assert all(line in (PROJECT_ARGS, MARKET_ARGS) for line in lines), lines
        return sum(line == args for line in lines)


@contextmanager
def host_environment(home):
    """Only the fake CLIs and system directories on PATH; a fresh, non-maintenance process."""
    with mock.patch.dict(os.environ):
        os.environ['PATH'] = os.pathsep.join((str(home / 'bin'), '/usr/bin', '/bin'))
        for name in ('CLAUDE_CONFIG_DIR', 'CODEX_HOME'):
            os.environ.pop(name, None)
        with mock.patch.dict(host_inventory._SETTLED, clear=True), \
                mock.patch.dict(host_inventory._MODE, {'maintenance': False, 'refreshing': False}):
            del host_inventory._NOTES[:]
            try:
                yield
            finally:
                del host_inventory._NOTES[:]


def new_process():
    """What a new process starts with: no memo, not maintenance."""
    host_inventory._SETTLED.clear()
    host_inventory._MODE.update(maintenance=False, refreshing=False)
    del host_inventory._NOTES[:]


@contextmanager
def maintenance(refreshing):
    with mock.patch.dict(host_inventory._MODE, {'maintenance': True, 'refreshing': refreshing}):
        yield


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
        self.cache = host_inventory._cache_path(self.home, 'codex', 'project', self.project)
        self.market_cache = host_inventory._cache_path(self.home, 'codex', 'marketplace', None)
        self.config = self.home / '.codex' / 'config.toml'
        self.cache_root = self.home / '.codex/plugins/cache/memvane-marketplace/memvane' / VERSION
        self.adapter = self.cache_root / 'memvane/learning/context_learning/adapter.py'

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
        new_process()
        return self.hosts.runs('codex')

    def deferred(self, call=None):
        with self.assertRaises(selector_heal.HealDeferred) as caught:
            (call or self.listing)()
        self.assertEqual(str(caught.exception), 'host_inventory_unavailable')

    def bundle(self):
        """A minimal official Codex cache copy (what ``_official_cache`` reads)."""
        self.adapter.parent.mkdir(parents=True)
        self.adapter.write_text('# synthetic adapter\n')
        for relative in ('.codex-plugin/plugin.json', 'package.json'):
            path = self.cache_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({'name': 'memvane', 'version': VERSION}))

    # --- Outside selector maintenance -------------------------------------

    # One CLI run per records stamp; the record survives the process memory.
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
        self.assertEqual((row['host'], row['kind'], row['project'], row['listing']),
                         ('codex', 'project', str(self.project), CODEX_LISTING))
        self.assertEqual(row['executable'], str((self.home / 'bin/codex').resolve()))
        self.assertEqual(stat.S_IMODE(self.cache.parent.lstat().st_mode), 0o700)
        new_process()
        self.hosts.mode('codex', 'hang')
        for _ in range(2):
            self.assertEqual(self.listing(), CODEX_LISTING)
        self.assertEqual(self.hosts.runs('codex'), 1)
        self.assertEqual(host_inventory.take_notes(), [])

    # A moved stamp with a recent record: that record, as stale, and no CLI at all.
    def test_moved_stamp_with_recent_record_is_stale_without_cli(self):
        seeded = self.seed()
        recorded = self.cache.read_bytes()
        self.hosts.mode('codex', 'hang')
        for change in (self.touch_record, lambda: self.cache_root.mkdir(parents=True)):
            change()
            self.assertEqual(self.listing(), CODEX_LISTING)
            self.assertEqual(host_inventory.take_notes(), [('codex', 'stale')])
            self.assertEqual(self.listing(), CODEX_LISTING)
            self.assertEqual(host_inventory.take_notes(), [])
        self.assertEqual(self.hosts.runs('codex'), seeded)
        self.assertEqual(self.cache.read_bytes(), recorded)

    # No usable record: the CLI with the short bound, and its own TimeoutExpired.
    def test_missing_record_runs_short_bound_and_keeps_the_timeout(self):
        self.hosts.mode('codex', 'hang')
        with mock.patch.object(host_inventory, 'PROJECT_LISTING_SECONDS', 1), \
                mock.patch.object(host_inventory, 'INVENTORY_SECONDS', 3600), run_errors() as raised:
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                self.listing()
            self.assertEqual(caught.exception.timeout, 1)
            self.assertEqual(raised, [subprocess.TimeoutExpired])
            self.assertEqual(self.hosts.runs('codex'), 1)
            self.assertEqual(host_inventory.take_notes(), [('codex', 'unavailable')])
            self.assertFalse(self.cache.exists())
            # Memo for the same stamp: the earlier ValueError, no second wait.
            with self.assertRaisesRegex(ValueError, '^history_project_inventory_unavailable$'):
                self.listing()
            self.assertEqual(self.hosts.runs('codex'), 1)
            # A record change retries within the short bound.
            self.touch_record()
            with self.assertRaises(subprocess.TimeoutExpired):
                self.listing()
            self.assertEqual(self.hosts.runs('codex'), 2)

    # A failing CLI without a record keeps its own ValueError; an old record does not stand in.
    def test_failing_cli_and_old_record_keep_the_earlier_error(self):
        with self.assertRaisesRegex(ValueError, '^history_project_inventory_unavailable$'):
            self.listing()
        self.assertEqual(self.hosts.runs('codex'), 1)
        new_process()
        seeded = self.seed()
        row = json.loads(self.cache.read_text())
        row['recordedAt'] = time.time() - host_inventory.STALE_SECONDS - 3600
        self.cache.write_text(json.dumps(row))
        self.touch_record()
        self.hosts.mode('codex', 'fail')
        with self.assertRaisesRegex(ValueError, '^history_project_inventory_unavailable$'):
            self.listing()
        self.assertEqual(self.hosts.runs('codex'), seeded + 1)
        self.assertEqual(host_inventory.take_notes(), [('codex', 'unavailable')])

    # Outside maintenance the callers behave as 1.0.18 did on a CLI timeout.
    def test_outside_maintenance_callers_keep_the_118_errors(self):
        self.hosts.mode('codex', 'hang')
        self.hosts.mode('claude', 'hang')
        calls = (
            ('host_self.selection', subprocess.TimeoutExpired,
             lambda: host_self.selection(self.project, host='codex')),
            ('project_scope._local_installations', subprocess.TimeoutExpired,
             lambda: project_scope._local_installations(self.project)),
            ('_project_host_roots', ValueError,
             lambda: host_inventory._project_host_roots(self.home, self.project)),
        )
        with mock.patch.object(host_inventory, 'PROJECT_LISTING_SECONDS', 1), \
                mock.patch.object(selector_heal, 'running_adapter', return_value=self.adapter):
            for name, error, call in calls:
                with self.subTest(name):
                    host_inventory._SETTLED.clear()
                    before = self.hosts.runs('codex') + self.hosts.runs('claude')
                    with run_errors() as raised, self.assertRaises(error):
                        call()
                    self.assertEqual(raised, [subprocess.TimeoutExpired])
                    self.assertEqual(self.hosts.runs('codex') + self.hosts.runs('claude'), before + 1)

    # Outside maintenance a moved stamp never waits: the project-effective root resolves from the record.
    def test_local_installations_with_moved_stamp_runs_no_cli(self):
        seeded = self.seed()
        self.adapter.parent.mkdir(parents=True)  # The new cache entry moves the stamp.
        self.adapter.write_text('# synthetic adapter\n')
        self.hosts.mode('codex', 'hang')
        with mock.patch.object(selector_heal, 'running_adapter', return_value=self.adapter):
            self.assertEqual(project_scope._local_installations(self.project),
                             [{'host': 'codex', 'root': str(self.cache_root)}])
            rows, projection = host_self.selection(self.project, host='codex')
        self.assertEqual(rows, [{'host': 'codex', 'root': str(self.cache_root)}])
        self.assertEqual(projection['pluginId'], 'memvane@memvane-marketplace')
        self.assertEqual(self.hosts.runs('codex'), seeded)
        self.assertEqual(host_inventory.take_notes(), [('codex', 'stale')])

    # Codex's marketplace listing: recorded, short-bounded, its own error; no codex -> nothing recorded.
    def test_marketplace_listing_outside_maintenance(self):
        self.hosts.mode('codex', 'ok')
        self.assertEqual(REAL_CODEX_INSTALLED(self.home, None), CODEX_ROW)
        self.assertEqual(self.hosts.runs('codex', MARKET_ARGS), 1)
        row = json.loads(self.market_cache.read_text())
        self.assertEqual((row['kind'], row['project'], row['listing']), ('marketplace', 'None', CODEX_LISTING))
        self.assertEqual(stat.S_IMODE(self.market_cache.lstat().st_mode), 0o600)
        new_process()
        self.hosts.mode('codex', 'hang')
        self.assertEqual(REAL_CODEX_INSTALLED(self.home, None), CODEX_ROW)
        # The project record files are not part of an account-level stamp.
        (self.project / '.codex').mkdir()
        (self.project / '.codex/config.toml').write_text('# project\n')
        self.assertEqual(REAL_CODEX_INSTALLED(self.home, None), CODEX_ROW)
        self.assertEqual(self.hosts.runs('codex', MARKET_ARGS), 1)
        self.assertEqual(host_inventory.take_notes(), [])
        # Without a record: the earlier short bound (deadline-limited) and its TimeoutExpired.
        self.market_cache.unlink()
        new_process()
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            REAL_CODEX_INSTALLED(self.home, time.monotonic() + 1.5)
        self.assertLessEqual(caught.exception.timeout, 1.5)
        self.assertEqual(self.hosts.runs('codex', MARKET_ARGS), 2)
        self.assertFalse(self.market_cache.exists())
        # No codex on PATH: FileNotFoundError before anything is run or recorded.
        new_process()
        with mock.patch.dict(os.environ, {'PATH': '/usr/bin:/bin'}):
            with self.assertRaises(FileNotFoundError):
                REAL_CODEX_INSTALLED(self.home, None)
        self.assertEqual(self.hosts.runs('codex', MARKET_ARGS), 2)
        self.assertEqual(host_inventory.take_notes(), [])
        self.assertFalse(self.market_cache.exists())

    # ``_official`` turns the marketplace CLI's TimeoutExpired into a deferral.
    def test_official_defers_on_marketplace_timeout(self):
        self.bundle()
        self.hosts.mode('codex', 'hang')
        with run_errors() as raised:
            self.deferred(lambda: selector_heal._official(self.adapter, self.home, time.monotonic() + 1.5))
        self.assertEqual(raised, [subprocess.TimeoutExpired])
        self.assertEqual(self.hosts.runs('codex', MARKET_ARGS), 1)
        # With the listing recorded, the same check passes without the CLI.
        new_process()
        self.hosts.mode('codex', 'ok')
        selector_heal._official(self.adapter, self.home, None)
        new_process()
        self.hosts.mode('codex', 'hang')
        selector_heal._official(self.adapter, self.home, None)
        self.assertEqual(self.hosts.runs('codex', MARKET_ARGS), 2)

    # Claude prints a top-level JSON array; the record keeps it as is.
    def test_claude_array_listing_is_recorded_and_reused(self):
        rows = [{'id': 'memvane@memvane-marketplace', 'enabled': True, 'version': VERSION,
                 'installPath': str(self.home / '.claude/plugins/cache/x'), 'scope': 'user'}]
        self.hosts.listing('claude', rows)
        self.hosts.mode('claude', 'ok')
        argv = ('claude', 'plugin', 'list', '--json')
        self.assertEqual(REAL_LISTING(argv, self.home, self.project), rows)
        new_process()
        self.hosts.mode('claude', 'fail')
        self.assertEqual(REAL_LISTING(argv, self.home, self.project), rows)
        self.assertEqual(self.hosts.runs('claude'), 1)

    # --- Inside selector maintenance --------------------------------------

    # refresh waits the long bound; a recent record then serves as stale, once per stamp.
    def test_refresh_timeout_with_recent_record_is_stale_and_settled(self):
        seeded = self.seed()
        recorded = self.cache.read_bytes()
        self.touch_record()
        self.hosts.mode('codex', 'hang')
        with mock.patch.object(host_inventory, 'INVENTORY_SECONDS', 1), \
                mock.patch.object(host_inventory, 'PROJECT_LISTING_SECONDS', 3600), run_errors() as raised:
            with maintenance(refreshing=True):
                self.assertEqual(self.listing(), CODEX_LISTING)
            self.assertEqual(raised, [subprocess.TimeoutExpired])
            self.assertEqual(self.hosts.runs('codex'), seeded + 1)
            self.assertEqual(host_inventory.take_notes(), [('codex', 'stale')])
            with maintenance(refreshing=False):
                self.assertEqual(self.listing(), CODEX_LISTING)
                self.assertEqual(self.hosts.runs('codex'), seeded + 1)
                self.assertEqual(host_inventory.take_notes(), [])
            self.assertEqual(self.cache.read_bytes(), recorded)
            # Another record change inside refresh waits again.
            self.touch_record()
            with maintenance(refreshing=True):
                self.assertEqual(self.listing(), CODEX_LISTING)
            self.assertEqual(self.hosts.runs('codex'), seeded + 2)
            self.assertEqual(host_inventory.take_notes(), [('codex', 'stale')])

    # refresh: a failing CLI with a recent record is stale too.
    def test_refresh_failing_cli_with_recent_record_is_stale(self):
        seeded = self.seed()
        self.touch_record()
        self.hosts.mode('codex', 'fail')
        with maintenance(refreshing=True):
            self.assertEqual(self.listing(), CODEX_LISTING)
        self.assertEqual(self.hosts.runs('codex'), seeded + 1)
        self.assertEqual(host_inventory.take_notes(), [('codex', 'stale')])

    # refresh without a record: deferred, and the rest of the pass defers without waiting.
    def test_refresh_without_record_defers_once_per_stamp(self):
        self.hosts.mode('codex', 'hang')
        with mock.patch.object(host_inventory, 'INVENTORY_SECONDS', 1), run_errors() as raised:
            with maintenance(refreshing=True):
                self.deferred()
            self.assertEqual(raised, [subprocess.TimeoutExpired])
            self.assertEqual(host_inventory.take_notes(), [('codex', 'unavailable')])
            with maintenance(refreshing=False):
                self.deferred()
            self.assertEqual(self.hosts.runs('codex'), 1)
            self.assertFalse(self.cache.exists())
            self.touch_record()
            with maintenance(refreshing=True):
                self.deferred()
            self.assertEqual(self.hosts.runs('codex'), 2)
        self.touch_record()
        self.hosts.mode('codex', 'ok')
        with maintenance(refreshing=True):
            self.assertEqual(self.listing(), CODEX_LISTING)
        self.assertEqual(self.hosts.runs('codex'), 3)
        self.assertTrue(self.cache.is_file())

    # refresh: a record older than STALE_SECONDS does not stand in.
    def test_refresh_with_old_record_defers(self):
        self.seed()
        row = json.loads(self.cache.read_text())
        row['recordedAt'] = time.time() - host_inventory.STALE_SECONDS - 3600
        self.cache.write_text(json.dumps(row))
        self.assertEqual(stat.S_IMODE(self.cache.lstat().st_mode), 0o600)
        self.touch_record()
        self.hosts.mode('codex', 'fail')
        with maintenance(refreshing=True):
            self.deferred()
        self.assertEqual(host_inventory.take_notes(), [('codex', 'unavailable')])

    # The settled result expires after SETTLE_SECONDS even with the same stamp.
    def test_settled_failure_expires(self):
        with maintenance(refreshing=True):
            self.deferred()
            self.assertEqual(self.hosts.runs('codex'), 1)
            with mock.patch.object(host_inventory, 'SETTLE_SECONDS', -1):
                self.deferred()
        self.assertEqual(self.hosts.runs('codex'), 2)

    # ``refresh`` itself: both Codex listings settled, notes per listing, maintenance mode kept.
    def test_refresh_settles_project_and_marketplace_listings(self):
        self.hosts.mode('codex', 'ok')
        self.assertEqual(host_inventory.refresh(self.project, hosts=('codex',)),
                         [('codex', 'refreshed'), ('codex', 'refreshed')])
        self.assertEqual((self.hosts.runs('codex'), self.hosts.runs('codex', MARKET_ARGS)), (1, 1))
        self.assertTrue(self.cache.is_file() and self.market_cache.is_file())
        self.assertEqual(host_inventory._MODE, {'maintenance': True, 'refreshing': False})
        new_process()
        self.hosts.mode('codex', 'hang')
        self.touch_record()
        with mock.patch.object(host_inventory, 'INVENTORY_SECONDS', 1):
            self.assertEqual(host_inventory.refresh(self.project, hosts=('codex',)),
                             [('codex', 'stale'), ('codex', 'stale')])
        self.assertEqual((self.hosts.runs('codex'), self.hosts.runs('codex', MARKET_ARGS)), (2, 2))

    # In maintenance, TimeoutExpired never escapes the callers that read a listing.
    def test_maintenance_callers_defer_instead_of_timing_out(self):
        self.hosts.mode('codex', 'hang')
        self.hosts.mode('claude', 'hang')
        self.bundle()
        calls = (
            ('_project_listing', self.listing),
            ('host_self.selection', lambda: host_self.selection(self.project, host='codex')),
            ('project_scope._local_installations', lambda: project_scope._local_installations(self.project)),
            ('_project_host_roots', lambda: host_inventory._project_host_roots(self.home, self.project)),
            ('selector_heal._official', lambda: selector_heal._official(self.adapter, self.home, None)),
        )
        with mock.patch.object(host_inventory, 'INVENTORY_SECONDS', 1), \
                mock.patch.object(host_inventory, 'PROJECT_LISTING_SECONDS', 1), \
                mock.patch.object(selector_heal, 'running_adapter', return_value=self.adapter):
            for refreshing in (True, False):
                for name, call in calls:
                    with self.subTest(name, refreshing=refreshing):
                        host_inventory._SETTLED.clear()
                        before = sum(self.hosts.runs(host, args) for host in ('codex', 'claude')
                                     for args in (PROJECT_ARGS, MARKET_ARGS))
                        with maintenance(refreshing), run_errors() as raised:
                            self.deferred(call)
                        # One bounded run; the first deferral stops the caller.
                        self.assertEqual(raised, [subprocess.TimeoutExpired])
                        self.assertEqual(sum(self.hosts.runs(host, args) for host in ('codex', 'claude')
                                             for args in (PROJECT_ARGS, MARKET_ARGS)), before + 1)


class InventoryMaintenance(base.AdapterUpdate):
    """The real selector-maintenance entry, started from the Codex cache copy, with a hanging CLI."""

    def setUp(self):
        super().setUp()
        self.select()  # Codex 1.0.19 runs from .codex/plugins/cache/memvane-marketplace/memvane/1.0.19.
        self.assertEqual(self.host, 'codex')
        self.assertEqual(self.running, self.home / '.codex/plugins/cache/memvane-marketplace/memvane' / base.RUNNING)
        self.hosts = FakeHosts(self.home)
        self.hosts.listing('codex', {'installed': [dict(self.codex)]})
        env = host_environment(self.home)
        env.__enter__()
        self.addCleanup(env.__exit__, None, None, None)
        self.real_hosts = {'codex'}
        self.market = []

        def listing(argv, account, project):
            if argv[0] in self.real_hosts:
                return REAL_LISTING(argv, account, project)
            return self.listing(argv, account, project)

        def codex_installed(home, deadline):
            try:
                row = REAL_CODEX_INSTALLED(home, deadline)
            except BaseException as exc:
                self.market.append(type(exc).__name__)
                raise
            self.market.append('ok')
            return row
        for target, kwargs in ((host_inventory, {'attribute': '_project_listing', 'side_effect': listing}),
                               (adapter_binding, {'attribute': '_codex_installed', 'side_effect': codex_installed}),
                               (host_inventory, {'attribute': 'INVENTORY_SECONDS', 'new': 1}),
                               (host_inventory, {'attribute': 'PROJECT_LISTING_SECONDS', 'new': 1})):
            patcher = mock.patch.object(target, kwargs.pop('attribute'), **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def seed(self, *, marketplace=True):
        self.hosts.mode('codex', 'ok')
        if marketplace:
            self.assertEqual(host_inventory.refresh(self.project), [('codex', 'refreshed')] * 2)
        else:
            REAL_LISTING(('codex', 'plugin', 'list', '--json'), self.home, self.project)
        self.assertEqual(self.hosts.runs('codex'), 1)
        self.assertEqual(self.hosts.runs('codex', MARKET_ARGS), 1 if marketplace else 0)
        new_process()  # The maintenance run is a new process.
        del self.market[:]

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
        self.assertNotIn('TimeoutExpired', [row.get('error') for _, row in events])
        return log, events, inventory, maintenance

    def assert_completed(self, log, events, maintenance):
        self.assert_prepared(log)
        self.assertEqual(self.published, [base.RUNNING])
        self.assertEqual(maintenance[0], ('started', None))
        self.assertEqual(maintenance[-1], ('completed', None))
        self.assertIn(('learning.selector.warm', 'selector_warm_current'),
                      [(name, row.get('outcome')) for name, row in events])
        # The Codex marketplace listing really was read (renewal's _official proof).
        self.assertGreaterEqual(self.market.count('ok'), 2)
        self.assertEqual([item for item in self.market if item != 'ok'], [])

    # R-1 (a): recorded listings for unchanged records: no CLI run at all.
    def test_recorded_listings_complete_without_cli(self):
        self.seed()
        self.hosts.mode('codex', 'hang')
        log, events, inventory, maintenance = self.run_maintenance()
        self.assert_completed(log, events, maintenance)
        self.assertEqual((self.hosts.runs('codex'), self.hosts.runs('codex', MARKET_ARGS)), (1, 1))
        self.assertEqual(inventory, [])

    # Records changed and only recent listings: one bounded run per listing, then stale.
    def test_changed_records_with_recent_listings_complete_stale(self):
        self.seed()
        (self.home / '.codex/config.toml').write_text('# changed\n')
        self.hosts.mode('codex', 'hang')
        log, events, inventory, maintenance = self.run_maintenance()
        self.assert_completed(log, events, maintenance)
        self.assertEqual((self.hosts.runs('codex'), self.hosts.runs('codex', MARKET_ARGS)), (2, 2))
        self.assertEqual(inventory, [('stale', 'host_inventory_stale', 'codex')] * 2)

    # No listing anywhere: deferred, not failed; each CLI waited at most once.
    def test_no_listing_defers_maintenance(self):
        self.real_hosts = {'codex', 'claude'}
        for host in ('codex', 'claude'):
            self.hosts.mode(host, 'hang')
        before = self.targets()
        log, events, inventory, maintenance = self.run_maintenance()
        self.assertEqual(sorted(inventory), [('unavailable', 'host_inventory_unavailable', 'claude')]
                         + [('unavailable', 'host_inventory_unavailable', 'codex')] * 2)
        self.assertEqual(maintenance[0], ('started', None))
        self.assertEqual(maintenance[-1], ('deferred', 'host_inventory_unavailable'))
        self.assertNotIn('maintenance_failed', [reason for _, reason in maintenance])
        self.assertEqual((self.hosts.runs('codex'), self.hosts.runs('codex', MARKET_ARGS),
                          self.hosts.runs('claude')), (1, 1, 1))
        self.assertEqual(log.steps, [])
        self.assertEqual(self.published, [])
        self.assertEqual(self.targets(), before)

    # R-1 (b): the project listing is recorded, the marketplace listing is not:
    # renewal's _official proof defers instead of failing or timing out.
    def test_missing_marketplace_listing_defers_at_official_proof(self):
        self.seed(marketplace=False)
        self.hosts.mode('codex', 'hang')
        before = self.targets()
        log, events, inventory, maintenance = self.run_maintenance()
        self.assertEqual(inventory, [('unavailable', 'host_inventory_unavailable', 'codex')])
        self.assertEqual(maintenance[-1], ('deferred', 'host_inventory_unavailable'))
        deferred = [row for name, row in events
                    if name == 'learning.selector.maintenance' and row.get('outcome') == 'deferred']
        self.assertEqual([row.get('error') for row in deferred], ['HealDeferred'])
        # refresh waited once (and deferred); renewal's proof read the settled result.
        self.assertEqual(self.market, ['HealDeferred', 'HealDeferred'])
        self.assertEqual((self.hosts.runs('codex'), self.hosts.runs('codex', MARKET_ARGS)), (1, 1))
        self.assertEqual(log.renewals, [('raised', 'host_inventory_unavailable')])
        self.assertEqual((log.heals, log.steps, self.published), ([], [], []))
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

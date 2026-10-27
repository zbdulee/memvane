#!/usr/bin/env python3
"""Import-graph guard: fresh-wiki selection never loads the QMD-origin code.

Every check runs in a new interpreter so ``sys.modules`` starts empty. The
shared ledger primitives live in ``laya_ledger_common``; only a QMD-origin
marker or ledger may load ``laya_qmd_cutover`` / ``laya_qmd_migration``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / 'core'
QMD_MODULES = ('laya_qmd_cutover', 'laya_qmd_migration')

PRELUDE = f'''
import json, os, sys
from pathlib import Path
sys.path.insert(0, {str(CORE)!r})
def loaded():
    return sorted(name for name in {QMD_MODULES!r} if name in sys.modules)
def report(**fields):
    print(json.dumps(fields, sort_keys=True))
'''


def run(body, *args):
    env = {**os.environ, 'QMD_SANDBOX': '1', 'QMD_RECALL_LOG': '',
           'QMD_SETUP_GUARD_FIXTURE': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
    result = subprocess.run(
        [sys.executable, '-B', '-c', PRELUDE + textwrap.dedent(body), *args],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8')


class ImportGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve(strict=True)
        self.project = base / 'project'
        self.state = base / 'state'
        (self.project / '.auto-context').mkdir(parents=True, mode=0o700)
        self.state.mkdir(mode=0o700)
        self.marker = self.project / '.auto-context' / 'laya-index-selection.json'

    def write_marker(self, schema):
        fd = os.open(self.marker, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(canonical({
                'schema': schema, 'projectRoot': str(self.project),
                'journalPath': str(self.state / 'install-journal.sqlite'),
                'ledgerPath': str(self.state / 'fresh-wiki-cutover.json'),
                'routeSha256': '0' * 64}))

    def test_hot_path_and_onboarding_modules_import_without_qmd_origin(self):
        out = run('''
            import laya_index_host_route, laya_host_response
            import laya_fresh_wiki_origin, laya_ledger_common
            import laya_active_upgrade, laya_bound_upgrade, laya_inactive_upgrade
            import laya_bundle_upgrade, laya_settings_rebind
            import laya_product_install, laya_product_sync_worker, setup_entry
            report(loaded=loaded())
        ''')
        self.assertEqual(out['loaded'], [])

    def test_fresh_marker_selection_uses_only_the_fresh_classifier(self):
        self.write_marker('laya-fresh-wiki-selection-marker-v1')
        out = run('''
            import laya_fresh_wiki_origin as fresh
            import laya_index_host_route as route
            calls = []
            real = fresh.classify_marker
            def counted(*args):
                calls.append(args[1])
                return real(*args)
            fresh.classify_marker = counted
            decisions = [route.select_route(sys.argv[1], action=action)
                         for action in ('recall', 'update', 'gate')]
            report(loaded=loaded(), calls=len(calls),
                   decisions=[[d['state'], d['reason']] for d in decisions])
        ''', str(self.project))
        self.assertEqual(out['calls'], 3)
        # No ledger exists, so the fresh classifier fails closed.
        self.assertEqual(out['decisions'], [['blocked', 'qmd_selection_uncertain']] * 3)
        self.assertEqual(out['loaded'], [])

    def test_qmd_marker_still_loads_the_qmd_origin_classifier(self):
        self.write_marker('laya-qmd-selection-marker-v1')
        out = run('''
            import laya_index_host_route as route
            decision = route.select_route(sys.argv[1], action='recall')
            report(loaded=loaded(), decision=[decision['state'], decision['reason']])
        ''', str(self.project))
        self.assertEqual(out['decision'], ['blocked', 'qmd_selection_uncertain'])
        self.assertIn('laya_qmd_cutover', out['loaded'])

    def test_fresh_selection_and_upgrade_flows_never_load_qmd_origin(self):
        out = run('''
            import io, runpy, unittest
            ran, failed = 0, 0
            for name in ('laya-fresh-wiki-origin-checks.py',
                         'laya-active-upgrade-checks.py',
                         'laya-inactive-upgrade-checks.py'):
                module = runpy.run_path(str(Path('test/fixtures') / name),
                                        run_name='guarded')
                suite = unittest.TestSuite()
                for value in module.values():
                    if (isinstance(value, type) and issubclass(value, unittest.TestCase)
                            and value.__module__ == 'guarded'):
                        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(value))
                result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
                ran += result.testsRun
                failed += len(result.failures) + len(result.errors)
            report(loaded=loaded(), ran=ran, failed=failed)
        ''')
        self.assertEqual(out['failed'], 0)
        self.assertEqual(out['ran'], 18)
        self.assertEqual(out['loaded'], [])

    def test_absent_qmd_ledger_probe_never_loads_qmd_origin(self):
        out = run('''
            import laya_active_upgrade as active
            import laya_settings_rebind as rebind
            from laya_ledger_common import inspect_qmd_ledger
            state = Path(sys.argv[1])
            probe = inspect_qmd_ledger(state / 'install-journal.sqlite')
            ledgers = [active._Ledger('fresh').name, rebind._Ledger('fresh').name]
            report(loaded=loaded(), probe=probe, ledgers=ledgers)
        ''', str(self.state))
        self.assertIsNone(out['probe'])
        self.assertEqual(out['ledgers'], ['fresh-wiki-cutover.json'] * 2)
        self.assertEqual(out['loaded'], [])

    def test_shared_primitives_keep_lock_name_and_identity(self):
        out = run('''
            import laya_ledger_common as common
            import laya_fresh_wiki_origin as fresh
            import laya_qmd_cutover as qmd
            marker = Path(sys.argv[1])
            with common._operation_lock(marker):
                created = sorted(path.name for path in marker.parent.iterdir())
            same = all(getattr(qmd, name) is getattr(common, name) for name in (
                'CutoverBlocked', '_fail', '_canon', '_sha', '_private_file',
                '_read_json', '_fsync_dir', '_write', '_identity', '_ledger_lock',
                '_operation_lock', '_preflight_multi_settings', 'BOUND_EVIDENCE'))
            same_fresh = all(getattr(fresh, name) is getattr(common, name) for name in (
                '_fsync_dir', '_identity', '_ledger_lock', '_operation_lock',
                '_preflight_multi_settings', '_private_file', '_read_json', '_write'))
            report(created=created, same=same, same_fresh=same_fresh,
                   ledger=qmd.LEDGER, marker_schema=qmd.MARKER_SCHEMA,
                   blocked=common.CutoverBlocked.__name__)
        ''', str(self.marker))
        # Older installed bundles lock the same file; the name must not move.
        self.assertEqual(out['created'], ['laya-qmd-cutover-operation.lock'])
        self.assertTrue(out['same'])
        self.assertTrue(out['same_fresh'])
        self.assertEqual(out['ledger'], 'qmd-cutover.json')
        self.assertEqual(out['marker_schema'], 'laya-qmd-selection-marker-v1')
        self.assertEqual(out['blocked'], 'CutoverBlocked')

    def test_ledger_writer_bytes_are_canonical_and_private(self):
        out = run('''
            import stat
            from laya_ledger_common import _read_json, _write
            path = Path(sys.argv[1]) / 'record.json'
            _write(path, {'b': 'é', 'a': [1, 2]}, replace=False)
            report(raw=path.read_bytes().decode('utf-8'),
                   mode=stat.S_IMODE(path.stat().st_mode), back=_read_json(path))
        ''', str(self.state))
        self.assertEqual(out['raw'], '{"a":[1,2],"b":"é"}')
        self.assertEqual(out['mode'], 0o600)
        self.assertEqual(out['back'], {'a': [1, 2], 'b': 'é'})


if __name__ == '__main__':
    unittest.main()

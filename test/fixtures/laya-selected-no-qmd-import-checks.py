#!/usr/bin/env python3
"""Import guard: selected Laya flows never load a QMD module.

Each check runs in a new interpreter so ``sys.modules`` starts empty. The
selected hook, onboarding, sync, compile and upgrade fixtures run there in
full; afterwards no QMD runtime, store, installer, daemon, setup-guard,
legacy recall or legacy queue module may be loaded.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / 'core'
FORBIDDEN = (r'^(qmd_.*|shared_install|shared_qmd_setup|install_update|install_maintenance'
             r'|install_evidence|install_deadline|setup_guard|setup_readiness|recall|sync'
             r'|update_hook_queue|dirty_queue|dirty_queue_claim|crowding_probe'
             r'|runtime_update|runtime_update_cancel|orphan_reclaim|index_snapshot'
             r'|index_enqueue|recommend_config|worker_lock)$')

PRELUDE = f'''
import io, json, re, runpy, sys, unittest
from pathlib import Path
sys.path.insert(0, {str(CORE)!r})
FORBIDDEN = re.compile({FORBIDDEN!r})
def loaded():
    return sorted(name for name in sys.modules if FORBIDDEN.match(name))
def report(**fields):
    print(json.dumps(fields, sort_keys=True))
def run_fixture(name):
    module = runpy.run_path(str(Path({str(ROOT / 'test/fixtures')!r}) / name),
                            run_name='guarded')
    suite = unittest.TestSuite()
    for value in module.values():
        if (isinstance(value, type) and issubclass(value, unittest.TestCase)
                and value.__module__ == 'guarded'):
            suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(value))
    result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
    problems = [text[-2000:] for _, text in result.failures + result.errors]
    return result.testsRun, problems
'''


def run(body, *args):
    env = {**os.environ, 'QMD_SANDBOX': '1', 'QMD_RECALL_LOG': '',
           'QMD_SETUP_GUARD_FIXTURE': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
    result = subprocess.run(
        [sys.executable, '-B', '-c', PRELUDE + textwrap.dedent(body), *args],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=900)
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class SelectedNoQmdImport(unittest.TestCase):
    def test_selected_entry_modules_import_without_qmd(self):
        out = run('''
            import laya_index_host_route, laya_host_response, laya_wake_entry
            import laya_runtime_dispatch, laya_product_sync_worker, wiki_selected_laya
            import laya_bundle_upgrade, laya_resident_service, runtime_work
            import wiki_card, wiki_compile, wiki_verify_worker, wiki_dedup_resolve
            import wiki_lifecycle, wiki_source_missing, wiki_extract
            import context_learning.laya_learning
            report(loaded=loaded())
        ''')
        self.assertEqual(out['loaded'], [])

    def test_selected_flows_never_load_qmd(self):
        for name, minimum in (('laya-fresh-onboarding-e2e-checks.py', 2),
                              ('laya-host-response-checks.py', 50),
                              ('laya-index-host-route-checks.py', 15),
                              ('laya-product-sync-worker-checks.py', 30),
                              ('laya-runtime-dispatch-checks.py', 10),
                              ('wiki-selected-laya-checks.py', 8),
                              ('laya-bundle-auto-upgrade-checks.py', 15),
                              ('laya-embed-runtime-checks.py', 13),
                              ('laya-runtime-provision-checks.py', 21)):
            with self.subTest(fixture=name):
                out = run('''
                    ran, problems = run_fixture(sys.argv[1])
                    report(loaded=loaded(), ran=ran, problems=problems)
                ''', name)
                self.assertEqual(out['problems'], [])
                self.assertGreaterEqual(out['ran'], minimum)
                self.assertEqual(out['loaded'], [])


if __name__ == '__main__':
    unittest.main()

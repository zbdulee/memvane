#!/usr/bin/env python3
"""A released 0.44.18 core refuses a row-format shadow candidate whose intent names a ready stage."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
spec = importlib.util.spec_from_file_location('s_shadow_switch_fixture', HERE / 'laya-shadow-switch-checks.py')
switch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(switch)
RELEASE_0_44_18 = '6d26d8afeebf2931f515bb12849377b458e2872d'
CHILD = r'''
import json, sys
from pathlib import Path
core, state, project, scope, candidate = sys.argv[1:6]
sys.path.insert(0, core)
import laya_product_sync_worker as W
assert W.__file__.startswith(core), W.__file__
try:
    W._shadow_open(Path(state), Path(project), scope, Path(candidate)).close()
    print(json.dumps({'shadow_open': 'accepted'}))
except Exception as exc:
    print(json.dumps({'shadow_open': str(getattr(exc, 'reason', None) or exc)}))
'''


def file_state(*paths):
    state = {}
    for path in paths:
        for name in (path, Path(str(path) + '-journal')):
            if name.exists():
                info = name.stat()
                state[str(name)] = (info.st_ino, info.st_size, hashlib.sha256(name.read_bytes()).hexdigest())
    return state


class ShadowQ4Checks(switch.ShadowSwitchChecks):
    def setUp(self):
        patch = mock.patch.dict(os.environ, {'MEMVANE_TEST_SHADOW_ROW_FORMAT': '1', 'QMD_SETUP_GUARD_FIXTURE': '1'})
        patch.start()
        self.addCleanup(patch.stop)
        super().setUp()

    def test_released_core_refuses_a_ready_stage_intent_at_the_header(self):
        with self.driver():
            for _ in range(60):
                with mock.patch.object(self.W, 'SHADOW_STEPS_PER_PASS', 1):
                    self.run_pass()
                intent = self.intent() or {}
                if intent.get('prepared') is not None and intent.get('published') is None:
                    break
            else:
                self.fail('no prepared-but-unpublished intent: %s' % (self.intent(),))
        candidate = Path(intent['candidate'])
        generation = intent['prepared']['generation']
        with sqlite3.connect(candidate.as_uri() + '?mode=ro', uri=True) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT state FROM s_stage_jobs WHERE job_id=?",
                                        (generation[2:34],)).fetchone(), ('ready',))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM s_generations WHERE gen_id=?',
                                        (generation,)).fetchone()[0], 0)
        intent_path = self.case.state / self.M._INTENT
        before = file_state(candidate, intent_path, Path(self.case.journal))
        exported = Path(tempfile.mkdtemp(prefix='q4-core-', dir=self.case.home))
        archive = subprocess.run(['git', '-C', str(ROOT), 'archive', RELEASE_0_44_18, 'core'],
                                 capture_output=True, timeout=60)
        self.assertEqual(archive.returncode, 0, archive.stderr.decode(errors='replace'))
        subprocess.run(['tar', '-x', '-C', str(exported)], input=archive.stdout, check=True, timeout=60)
        result = subprocess.run([sys.executable, '-B', '-c', CHILD, str(exported / 'core'), str(self.case.state),
                                 str(self.case.project), self.case.route['scopeId'], str(candidate)],
                                capture_output=True, text=True, timeout=120,
                                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        observed = json.loads(result.stdout.strip().splitlines()[-1])
        print(json.dumps(dict(q4=observed, generation=generation, python=sys.version.split()[0])))
        self.assertEqual(observed, {'shadow_open': 'candidate_format_mismatch'})
        self.assertEqual(file_state(candidate, intent_path, Path(self.case.journal)), before)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    suite.addTest(ShadowQ4Checks('test_released_core_refuses_a_ready_stage_intent_at_the_header'))
    return suite


if __name__ == '__main__':
    unittest.main()

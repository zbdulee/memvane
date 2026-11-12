"""4,000 synthetic trusted source cards through the real shadow driver."""
from contextlib import ExitStack
import importlib.util
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('parity_scale_driver', HERE / 'laya-shadow-driver-checks.py')
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)
spec = importlib.util.spec_from_file_location('parity_scale_seed', HERE / 'laya-reader-move-seal-checks.py')
seed_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed_module)

class ScaleSeed(seed_module.MoveSealChecks):
    def before_conversion(self):
        with sqlite3.connect(self.sidecar) as db:
            path, digest, title, body, profile, scan = db.execute(
                'SELECT path,hash,title,body,profile_id,scan_id FROM docs ORDER BY path LIMIT 1').fetchone()
            collection = path.split('/', 1)[0]
            for i in range(3997):
                synthetic = collection + '/scale/card-%04d.md' % i
                cur = db.execute('INSERT INTO docs(path,hash,title,body,profile_id,chunk_count,scan_id) '
                                 'VALUES(?,?,?,?,?,?,?)', (synthetic, digest, title, body, profile, 1, scan))
                db.execute('INSERT INTO docs_fts(rowid,path,title,body) VALUES(?,?,?,?)',
                           (cur.lastrowid, synthetic, title, body))

class ScaleChecks(unittest.TestCase):
    def test_4000_trusted_members_finish_candidate_sampling_in_bounded_steps(self):
        seed = ScaleSeed('runTest')
        seed.setUp()
        self.addCleanup(seed.doCleanups)
        case = seed.case
        git = ['git', '-C', str(case.project), '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null',
               '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid']
        subprocess.run(['git', '-c', 'init.defaultBranch=main', 'init', '-q', str(case.project)],
                       check=True, capture_output=True, timeout=10)
        subprocess.run(git + ['commit', '--allow-empty', '-qm', 'fixture'],
                       check=True, capture_output=True, timeout=10)
        time.sleep(1.1)
        fixture = driver.DriverChecks('runTest')
        fixture.setUp()
        worker = fixture.W
        migration = fixture.M
        parity_ms = []
        original_advance = migration._advance_parity
        def measured(*args, **kwargs):
            started = time.monotonic()
            try:
                return original_advance(*args, **kwargs)
            finally:
                parity_ms.append(round((time.monotonic() - started) * 1000, 3))
        with seed.authority(), mock.patch.object(worker, '_writer_executing_root', return_value=case.plugin), \
             mock.patch.object(worker, '_run_sync', return_value={'state': 'unchanged'}), \
             mock.patch.object(migration, '_advance_parity', side_effect=measured):
            start = time.monotonic()
            for iteration in range(1000):
                result = worker.shadow_pass(case.project)
                if result['state'] == 'error':
                    self.fail(str(result))
                intent = fixture.intent(case)
                if intent and intent['migration_state'] == 'shadow_verified':
                    break
            else:
                self.fail('shadow did not complete in 1000 passes')
            parity = intent['parity']
            self.assertEqual(parity['members'], 4000)
            self.assertEqual(parity['prompts'], 10)
            self.assertEqual(parity['pages'], 10)
            self.assertTrue(parity['compared'])
            events = [row for row in fixture.events(case) if row.get('event') == 'migration.shadow'
                      and row.get('op') == 'parity' and row.get('outcome') == 'parity_page']
            self.assertEqual(len(events), 10)
            self.assertEqual(len(parity_ms), 10)
            print('synthetic_scale_members=4000 sampled=10 passes=%d total_seconds=%.3f parity_ms=%s write_event_ms=%s' %
                  (iteration + 1, time.monotonic() - start, parity_ms, [row.get('ms') for row in events]), flush=True)

if __name__ == '__main__':
    print('python-executable=%s python-version=%s' % (sys.executable, sys.version.split()[0]), flush=True)
    unittest.main(defaultTest="ScaleChecks", verbosity=2)

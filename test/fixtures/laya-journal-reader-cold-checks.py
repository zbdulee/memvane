"""Diagnose interpreter-specific journal entry failures via fresh subprocesses."""
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('cold_parent_base', HERE / 'laya-journal-reader-checks.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)


class ColdChecks(unittest.TestCase):
    @contextmanager
    def fixture(self):
        helper = base.JournalReaderChecks('runTest')
        helper.setUp()
        try:
            case = helper.published_case()
            payload = case.home / 'cold-payload.json'
            payload.write_text(json.dumps(dict(state=str(case.state), project=str(case.project),
                                               plugin=str(case.plugin), proof=case.proof)), encoding='utf8')
            payload.chmod(0o600)
            before = dict(data=helper.data_snapshot(case), metadata=helper.metadata_snapshot(case),
                          head=case.head(), journal=base.J.inspect_journal(case.journal),
                          inode=case.journal.stat().st_ino)
            yield helper, case, payload, before
        finally:
            helper.doCleanups()

    def phase(self, payload, phase):
        executable = os.environ.get('MEMVANE_READER_COLD_PYTHON') or sys.executable
        result = subprocess.run([executable, '-B', '-I', str(HERE / 'laya-journal-reader-coldworker.py'),
                                 str(payload), phase], capture_output=True, encoding='utf8', timeout=40,
                                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        rows = []
        for line in result.stdout.splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get('cold_worker_result'):
                rows.append(item)
        print(json.dumps(dict(cold_phase=phase, executable=executable, returncode=result.returncode,
                              worker=rows, stdout=result.stdout, stderr=result.stderr)), flush=True)
        return result, rows

    def preserved(self, helper, case, before):
        self.assertEqual(helper.data_snapshot(case), before['data'])
        self.assertEqual(helper.metadata_snapshot(case), before['metadata'])
        self.assertEqual(case.head(), before['head'])
        self.assertEqual(base.J.inspect_journal(case.journal), before['journal'])
        self.assertEqual(case.journal.stat().st_ino, before['inode'])
        # Actual published recall after byte-preservation assertions. These
        # reads use/release transient pins; they are not converter mutations.
        rows = {}
        for term in ('otter', 'heron', 'kestrel'):
            for row in case.recall(case.decision(), query=term)[1]:
                rows[row['path']] = row
        self.assertEqual(len(rows), 3)

    def success(self, observed, expected=0):
        result, rows = observed
        self.assertTrue(rows, result.stdout + result.stderr)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertNotEqual(rows[-1]['pid'], os.getpid())
        self.assertEqual(rows[-1]['initial_reader_tokens'], 0)
        self.assertEqual(rows[-1]['initial_bridge_tokens'], 0)
        return rows[-1]

    def test_fresh_child_selected_full_and_readonly_proof_baseline(self):
        with self.fixture() as (helper, case, payload, before):
            self.success(self.phase(payload, 'baseline'))
            self.preserved(helper, case, before)

    def test_fresh_child_normal_seal_ready_convert_and_exact_replay(self):
        with self.fixture() as (helper, case, payload, before):
            baseline = self.phase(payload, 'baseline')
            converted = self.phase(payload, 'convert')
            self.success(baseline)
            row = self.success(converted)
            self.assertEqual((row['version'], row['selection'][0]), (5, 1))
            self.preserved(helper, case, before)

    def test_external_fresh_hot_v4_writer_and_fresh_issuer_recovery(self):
        with self.fixture() as (helper, case, payload, before):
            intent = self.success(self.phase(payload, 'intent'), 91)
            self.assertTrue((case.state / base.J._INITIAL_INTENT / 'intent.json').is_file())
            hot = self.success(self.phase(payload, 'hot'), 91)
            self.assertEqual(hot['header_version'], 4)
            self.assertTrue(Path(str(case.journal) + '-journal').is_file())
            recovered = self.success(self.phase(payload, 'recover'))
            self.assertEqual((recovered['version'], recovered['selection'][0]), (5, 1))
            self.assertEqual(len({intent['pid'], hot['pid'], recovered['pid']}), 3)
            self.preserved(helper, case, before)
            print(json.dumps(dict(cold_recovery='actual-hot-v4-only', header5_commit_window_claim=False)), flush=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)

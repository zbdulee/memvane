"""Reproduce historical queue outcomes in immutable release trees and the e8 baseline."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from memvane.wiki.compile.tests.integration import test_pending_handoff as fixtures
from tests.support.case import FixtureCase


TAGS = ('v1.0.22', 'v1.0.23', 'v1.0.24', 'v1.1.0')
BASELINE = 'e8fdfdaa'
REPO = Path(__file__).resolve().parents[5]
DRIVER = REPO / 'memvane/wiki/compile/tests/tagged_queue_driver.py'


def rows(path):
    return [json.loads(line) for line in path.read_bytes().split(b'\n') if line]


class TaggedQueueControls(FixtureCase):
    prepare = fixtures.PendingHandoff.prepare

    def invoke(self, tag, action, **input_fields):
        tree = self.tmp / tag
        if not tree.exists():
            commit = subprocess.check_output(['git', 'rev-parse', tag + '^{commit}'], cwd=REPO).strip()
            archive = subprocess.check_output(['git', 'archive', tag, 'memvane', 'package.json'], cwd=REPO)
            self.assertEqual(subprocess.check_output(['git', 'get-tar-commit-id'], input=archive).strip(), commit)
            tree.mkdir()
            subprocess.run(['tar', '-x', '-C', str(tree)], input=archive, check=True)
        req = dict(input_fields, action=action, root=str(self.root), config=self.config,
                   bound=self.bound, home=str(self.home))
        child = subprocess.run([sys.executable, '-B', str(DRIVER)], cwd=tree, input=json.dumps(req),
                               env={**os.environ, 'PYTHONPATH': str(tree), 'PYTHONNOUSERSITE': '1'},
                               capture_output=True, text=True)
        self.assertEqual(child.returncode, 0, child.stderr)
        return json.loads(child.stdout)

    def candidate(self, tag, source='docs/source.md', text='Newest source.', stamp='2026-01-01T00:00:00Z'):
        path = self.project.write_note(source, text)
        revision = self.project.revision(path)
        card = {'title': 'Decision', 'summary': 'Decision in ' + text, 'engine': 'claude',
                'trigger': 'post_tool_source', 'targetPath': self.target.relative_to(self.root).as_posix(),
                'sourceRevisions': [revision], 'sources': [{k: revision[k] for k in ('kind', 'path', 'collection')}]}
        return self.invoke(tag, 'write', card=card, stamp=stamp)['code']

    def test_returned_ordinary_row_shadows_new_pending(self):
        for tag in TAGS:
            with self.subTest(tag=tag):
                self.prepare(tag)
                self.target.unlink()
                self.assertEqual(self.candidate(tag, text='Newest source old.', stamp='2099-01-01T00:00:00Z'), 0)
                ordinary, = rows(self.queue)
                self.assertNotIn('pendingPath', ordinary)
                self.assertEqual(self.invoke(tag, 'verify'), {'remaining': 0, 'calls': 1})
                before = self.target.read_bytes()
                self.assertEqual(self.candidate(tag), 0)
                current, = rows(self.queue)
                with self.queue.open('a') as output:
                    print(json.dumps(dict(ordinary, _requeue_count=1)), file=output)
                self.assertTrue(self.invoke(BASELINE, 'prepare'))
                self.assertEqual(len(rows(self.queue)), 2)
                self.assertEqual(self.invoke(tag, 'verify'), {'remaining': 0, 'calls': 0})
                self.assertEqual(self.target.read_bytes(), before)
                self.assertFalse((self.root / current['pendingPath']).exists())

    def test_two_fresh_sources_and_actual_source_worker(self):
        for tag in TAGS:
            with self.subTest(tag=tag):
                self.prepare(tag)
                self.bound[0].update(installId='a'*32, scopeId='b'*32, storeIncarnation='c'*32,
                                     state='candidate', generation='g-' + 'd'*32)
                for name in ('source', 'other'):
                    self.assertEqual(self.candidate(tag, 'docs/' + name + '.md', 'Newest source ' + name), 0)
                first, second = rows(self.queue)
                self.assertEqual(first['expectedTargetHash'], hashlib.sha256(self.target.read_bytes()).hexdigest())
                self.assertEqual(first['expectedTargetHash'], second['expectedTargetHash'])
                self.assertNotEqual(first['pendingPath'], second['pendingPath'])
                for job in (first, second):
                    revision, = job['sourceRevisions']
                    self.assertEqual(revision['sha256'], hashlib.sha256((self.root / revision['path']).read_bytes()).hexdigest())
                    self.assertIn(revision['sha256'], (self.root / job['pendingPath']).read_text())
                source = self.project.write_note('docs/third.md', 'Newest source for third card.')
                job = {'cwd': str(self.root), 'engine': 'claude', 'trigger': 'post_tool_source',
                       'source': {'kind': 'file', 'path': 'docs/third.md', 'collection': self.project.raw_name}}
                self.assertEqual(self.invoke(tag, 'compile', job=job), {'main': 1})
                self.assertIn(hashlib.sha256(source.read_bytes()).hexdigest(),
                              (self.project.wiki / 'concepts/source-observation.md').read_text())

    def test_fourth_orphan_recovery_discards_last_occurrence(self):
        for tag in (*TAGS, BASELINE):
            with self.subTest(tag=tag):
                self.prepare(tag)
                self.assertEqual(self.candidate('v1.1.0'), 0)
                original, = rows(self.queue)
                for count in range(4):
                    claim = self.queue.parent / self.invoke(tag, 'claim')
                    job, = rows(claim)
                    self.assertEqual(job.get('_requeue_count', 0), count)
                empty = self.queue.parent / self.invoke(tag, 'claim')
                self.assertEqual(rows(empty), [])
                self.assertEqual(rows(self.queue), [])
                self.assertTrue((self.root / original['pendingPath']).is_file())
                ledger, = self.queue.parent.glob('*discard*.jsonl')
                self.assertEqual(rows(ledger)[-1]['requeue_count'], 4)

    def test_long_pending_stamp_retries_twice(self):
        for tag in (*TAGS, BASELINE):
            with self.subTest(tag=tag):
                self.prepare(tag)
                self.target = self.target.rename(self.target.with_name('a'*223 + '.md'))
                before = self.target.read_bytes()
                self.assertNotEqual(self.candidate(BASELINE), 0)
                self.assertEqual(self.candidate('0e187c68'), 0)
                job, = rows(self.queue)
                self.assertEqual(len(os.fsencode(Path(job['pendingPath']).name)), 252)
                for count in (1, 2):
                    self.assertEqual(self.invoke(tag, 'verify'), {'remaining': 1, 'calls': 1})
                    log = rows(self.queue.with_name('verify-log.jsonl'))
                    self.assertEqual(sum(row.get('result') == 'stamp_failed' for row in log), count)
                    self.assertEqual(self.target.read_bytes(), before)
                    self.assertTrue((self.root / job['pendingPath']).is_file())

"""A new writer hands each old verifier just one reviewable candidate per target."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from contextlib import redirect_stdout
from unittest.mock import patch

from memvane.wiki.compile import wiki_compile as writer
from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase
from tests.support.project import make_project
from tests.support.settings_fake import expanded


class PendingHandoff(FixtureCase):
    def prepare(self, name):
        self.project = make_project(self.home, name=name, dirname=name, git=False)
        self.root = self.project.root.resolve()
        self.source = self.project.write_note('docs/source.md', 'Original source.')
        self.config = expanded(self.project, compile={'semanticDedup': {'enabled': False}})
        self.queue = self.root / '.memvane/compile/verify-queue.jsonl'
        self.target = self.project.wiki / 'concepts/decision.md'
        self.target.parent.mkdir(parents=True)
        revision = self.project.revision(self.source)
        source = {k: revision[k] for k in ('kind', 'path', 'collection')}
        body = writer.markdown_page({'title': 'Decision', 'sources': [source],
                                     'sourceRevisions': [revision]}, 'Original card.', 'verified', [], 'a' * 16)
        self.target.write_text(body)
        self.bound = ({'projectRoot': str(self.root), 'recallSettings': {'skipPaths': []},
                       'collections': [{'name': self.project.raw_name, 'role': 'raw', 'sourceRoot': str(self.root)},
                                       {'name': self.project.wiki_name, 'role': 'wiki', 'sourceRoot': str(self.project.wiki)}],
                       'collectionRoots': {self.project.raw_name: str(self.root), self.project.wiki_name: str(self.project.wiki)}}, {})

    def write(self, content):
        if self.source.read_text() != content:
            self.source.write_text(content)
        revision = self.project.revision(self.source)
        candidate = {'title': 'Decision', 'summary': 'Decision in the current source.',
                     'engine': 'claude', 'trigger': 'post_tool_source',
                     'targetPath': self.target.relative_to(self.root).as_posix(),
                     'sourceRevisions': [revision],
                     'sources': [{k: revision[k] for k in ('kind', 'path', 'collection')}]}
        with workspace.card_write_lock(self.root), workspace.mutation_lock(self.root), \
             patch.object(workspace, 'selected', return_value=self.bound), patch.object(workspace, 'recheck'), \
             patch.object(workspace, 'request_sync'), redirect_stdout(io.StringIO()):
            return writer._compile_locked(self.root, self.config, self.config['compile'],
                                          self.project.wiki, candidate, False)

    def older(self):
        self.assertEqual(self.write('Older source for the decision.'), 0)
        job = json.loads(self.queue.read_text())
        job['ts'] = '2099-05-01T00:00:00Z'
        self.queue.write_text(json.dumps(job) + '\n')
        self.assertTrue((self.root / job['pendingPath']).exists())
        return job

    def run_tag(self, tag):
        repo = Path(__file__).resolve().parents[5]
        bundle = self.tmp / tag
        bundle.mkdir()
        archive = subprocess.run(['git', '-C', str(repo), 'archive', tag, 'memvane', 'package.json'],
                                 check=True, capture_output=True)
        subprocess.run(['tar', '-x', '-C', str(bundle)], input=archive.stdout, check=True)
        self.assertEqual(json.loads((bundle / 'package.json').read_text())['version'], tag[1:])
        harness = repo / 'memvane/wiki/verify/tests/tagged_consumer.py'
        child = subprocess.run([sys.executable, '-B', str(harness), str(bundle), str(self.root),
                                json.dumps(self.config)], cwd=bundle, capture_output=True, text=True,
                               env={**os.environ, 'PYTHONPATH': os.pathsep.join((str(bundle), str(repo)))})
        self.assertEqual(child.returncode, 0, child.stderr)
        return json.loads(child.stdout)

    def test_four_released_verifiers_publish_current_revision_despite_future_stale_timestamp(self):
        for tag in ('v1.0.22', 'v1.0.23', 'v1.0.24', 'v1.1.0'):
            with self.subTest(tag=tag):
                self.prepare(tag)
                old = self.older()
                self.assertEqual(self.write('Newest source for the decision.'), 0)
                result = self.run_tag(tag)
                self.assertEqual(result, {'remaining': 0, 'calls': 1})
                self.assertIn(self.project.revision(self.source)['sha256'], self.target.read_text())
                self.assertFalse((self.root / old['pendingPath']).exists())
                verdicts = [json.loads(line) for line in self.queue.with_name('verify-log.jsonl').read_text().splitlines()]
                self.assertTrue(any(row.get('result') == 'stale_replacement' and
                                    row.get('reason') == 'pending_source_changed' for row in verdicts))

    def test_failed_queue_replacement_keeps_previous_work_and_retries(self):
        self.prepare('retry')
        old = self.older()
        original = self.queue.read_bytes()
        previous = (self.root / old['pendingPath']).read_bytes()
        replace = os.replace
        def fail_queue(src, dst, *args, **kwargs):
            if Path(dst) == self.queue:
                raise OSError('injected queue publication failure')
            return replace(src, dst, *args, **kwargs)
        with patch.object(os, 'replace', side_effect=fail_queue):
            self.assertNotEqual(self.write('Newest source for the decision.'), 0)
        self.assertEqual(self.queue.read_bytes(), original)
        self.assertEqual((self.root / old['pendingPath']).read_bytes(), previous)
        self.assertEqual(list(self.target.parent.glob('.*.pending-*')), [self.root / old['pendingPath']])
        self.assertEqual(self.write('Newest source for the decision.'), 0)
        jobs = [j for j in map(json.loads, self.queue.read_text().splitlines()) if '_pendingCleanup' not in j]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['sourceRevisions'], [self.project.revision(self.source)])
        self.assertFalse((self.root / old['pendingPath']).exists())

    def receipt(self, job):
        path = self.root / job['pendingPath']
        info = path.stat()
        original = json.dumps(job, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()
        return {**job, '_pendingCleanup': {'jobKey': hashlib.sha256(original).hexdigest(),
                'fingerprint': [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]}}

    def test_recovered_job_uses_saved_receipt_and_unrelated_raw_lines_survive(self):
        self.prepare('receipt')
        old = self.older()
        marker = self.receipt(old)
        unrelated = '{"targetPath":"another.md", "engine":"unknown"}'
        self.queue.write_text(unrelated + '\n' + json.dumps(marker) + '\n' +
                              json.dumps({**old, '_requeue_count': 1}) + '\n')
        self.assertEqual(self.write('Newest source for the decision.'), 0)
        raw = self.queue.read_text().splitlines()
        self.assertEqual(raw[0], unrelated)
        self.assertEqual(json.loads(raw[1]), marker)
        reviewable = [job for job in map(json.loads, raw) if job.get('targetPath') == old['targetPath']
                      and '_pendingCleanup' not in job]
        self.assertEqual(len(reviewable), 1)
        self.assertNotEqual(reviewable[0]['pendingPath'], old['pendingPath'])
        self.run_tag('v1.1.0')
        self.assertFalse((self.root / old['pendingPath']).exists())
        self.assertIn(self.project.revision(self.source)['sha256'], self.target.read_text())

    def test_a_live_cleanup_only_candidate_cannot_be_requeued_for_verification(self):
        self.prepare('ended')
        old = self.older()
        marker = self.receipt(old)
        original = json.dumps(marker) + '\n'
        self.queue.write_text(original)
        self.assertNotEqual(self.write('Older source for the decision.'), 0)
        self.assertEqual(self.queue.read_text(), original)
        self.assertEqual(self.run_tag('v1.0.24'), {'remaining': 0, 'calls': 0})
        self.assertFalse((self.root / old['pendingPath']).exists())

    def test_active_old_claim_can_requeue_before_current_candidate_becomes_visible(self):
        self.prepare('claim')
        old = self.older()
        claimed = workspace.claim_queue(self.queue)
        old_bytes = claimed.read_bytes()
        self.assertNotEqual(self.write('Newest source for the decision.'), 0)
        self.assertEqual(self.queue.read_text(), '')
        self.assertEqual(claimed.read_bytes(), old_bytes)
        self.assertTrue((self.root / old['pendingPath']).exists())
        workspace.requeue_lines(self.queue, old_bytes.decode().splitlines())
        claimed.unlink()
        self.assertEqual(self.write('Newest source for the decision.'), 0)
        live = [j for j in map(json.loads, self.queue.read_text().splitlines()) if '_pendingCleanup' not in j]
        self.assertEqual([j['sourceRevisions'] for j in live], [[self.project.revision(self.source)]])

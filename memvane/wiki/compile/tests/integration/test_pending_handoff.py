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
from memvane.wiki.verify import api as verify
from tests.support.case import FixtureCase
from tests.support.project import make_project
from tests.support.settings_fake import expanded


def archived_tag(repo, directory, tag):
    bundle = directory / tag
    if not bundle.exists():
        bundle.mkdir()
        archive = subprocess.run(['git', '-C', str(repo), 'archive', tag, 'memvane', 'package.json'],
                                 check=True, capture_output=True)
        subprocess.run(['tar', '-x', '-C', str(bundle)], input=archive.stdout, check=True)
    return bundle


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
        bundle = archived_tag(repo, self.tmp, tag)
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
            for _ in range(2):
                self.assertNotEqual(self.write('Newest source for the decision.'), 0)
                self.assertEqual(self.audits(), [])
        self.assertEqual(self.queue.read_bytes(), original)
        self.assertEqual((self.root / old['pendingPath']).read_bytes(), previous)
        self.assertEqual(list(self.target.parent.glob('.*.pending-*')), [self.root / old['pendingPath']])
        self.assertEqual(self.write('Newest source for the decision.'), 0)
        jobs = [j for j in map(json.loads, self.queue.read_text().splitlines()) if '_pendingCleanup' not in j]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['sourceRevisions'], [self.project.revision(self.source)])
        self.assertFalse((self.root / old['pendingPath']).exists())
        self.assertEqual(len(self.audits()), 1)

    def test_rollback_preserves_edit_to_the_newly_staged_candidate(self):
        self.prepare('rollback-edited')
        old = self.older()
        stage, replace = writer.write_pending_card, os.replace
        changed = {}
        def edited_stage(path, page):
            self.assertTrue(stage(path, page))
            changed[path] = page.encode() + b'\nOperator edit during handoff.'
            path.write_bytes(changed[path])
            return True
        def failed_publish(src, dst):
            if Path(dst) == self.queue:
                raise OSError('injected publication failure')
            return replace(src, dst)
        with patch.object(writer, 'write_pending_card', side_effect=edited_stage), \
             patch.object(os, 'replace', side_effect=failed_publish):
            self.assertNotEqual(self.write('Newest source.'), 0)
        self.assertEqual(json.loads(self.queue.read_text()), old)
        self.assertEqual({path: path.read_bytes() for path in changed}, changed)
        self.assertEqual(self.audits(), [])

    def audits(self):
        ledger = self.queue.with_name('verify-deleted.jsonl')
        return list(map(json.loads, ledger.read_text().splitlines())) if ledger.exists() else []

    def test_recovered_source_can_return_to_its_first_content(self):
        self.prepare('return')
        self.assertEqual(self.write('Older source for the decision.'), 0)
        old = json.loads(self.queue.read_text())
        self.recover(old)
        self.assertEqual(self.write('Newest source.'), 0)
        self.assertEqual(self.write('Older source for the decision.'), 0)
        self.assertEqual(self.run_tag('v1.1.0'), {'calls': 1, 'remaining': 0})
        self.assertIn(self.project.revision(self.source)['sha256'], self.target.read_text())

    def recover(self, old):
        claimed = workspace.claim_queue(self.queue)
        claimed.rename(self.queue.with_name(self.queue.name + '.claimed.1234.fixture'))
        with patch.dict(os.environ, {'QMD_FORCE_DEAD_PIDS': '1234'}):
            recovered = workspace.claim_queue(self.queue)
        self.assertEqual(json.loads(recovered.read_text()), {**old, '_requeue_count': 1})
        workspace.requeue_lines(self.queue, recovered.read_text().splitlines())
        recovered.unlink()

    def test_recovered_legacy_bytes_survive_return_to_their_source(self):
        self.prepare('return-legacy')
        repo = Path(__file__).resolve().parents[5]
        bundle = archived_tag(repo, self.tmp, 'v1.1.0')
        harness = repo / 'memvane/wiki/compile/tests/tagged_writer.py'
        subprocess.run([sys.executable, '-B', str(harness), str(bundle), str(self.root),
                        json.dumps(self.config), json.dumps(self.bound), 'single'], cwd=bundle,
                       env={**os.environ, 'PYTHONPATH': str(bundle)}, capture_output=True, check=True)
        old = json.loads(self.queue.read_text())
        path = self.root / old['pendingPath']
        original = path.read_bytes()
        self.recover(old)
        self.assertFalse(self.queue.with_name('.pending-ownership.json').exists())
        self.assertEqual(self.write('Newest source.'), 0)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.audits(), [])
        self.assertEqual(self.write('Older source for the decision.'), 0)
        self.assertNotEqual(json.loads(self.queue.read_text())['pendingPath'], old['pendingPath'])
        self.assertEqual(self.run_tag('v1.1.0'), {'calls': 1, 'remaining': 0})
        self.assertIn(self.project.revision(self.source)['sha256'], self.target.read_text())
        self.assertEqual(path.read_bytes(), original)

    def test_operator_edits_remain_recoverable_when_source_reverts(self):
        self.prepare('manual')
        old = self.older()
        preserved = self.root / old['pendingPath']
        edited = preserved.read_bytes() + b'\nPrivate operator notes.\n'
        preserved.write_bytes(edited)
        self.assertEqual(self.write('Newest source.'), 0)
        self.assertEqual(preserved.read_bytes(), edited)
        log = self.queue.with_name('verify-log.jsonl')
        self.assertIn('pending_ownership_unproved', log.read_text())
        self.assertEqual(self.audits(), [])
        self.assertEqual(self.write('Older source for the decision.'), 0)
        self.assertEqual(self.run_tag('v1.1.0'), {'calls': 1, 'remaining': 0})
        self.assertIn(self.project.revision(self.source)['sha256'], self.target.read_text())
        self.assertEqual(preserved.read_bytes(), edited)

    def resume(self):
        with patch.object(workspace, 'recheck'):
            return verify.prepare_queue(self.root, self.queue, self.bound, self.config)

    def test_unpublished_plan_cannot_retire_after_the_old_queue_drains(self):
        self.prepare('unpublished')
        old = self.older()
        replace = os.replace
        def interrupted(src, dst):
            if Path(dst) == self.queue:
                raise KeyboardInterrupt
            return replace(src, dst)
        with patch.object(os, 'replace', side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            self.write('Newest source.')
        self.queue.write_text('')  # The legacy consumer can drain/drop its job independently.
        self.resume()
        self.assertTrue((self.root / old['pendingPath']).exists())
        self.assertEqual(self.audits(), [])

    def test_audit_retries_after_old_consumer_publishes_and_crash_precedes_unlink(self):
        self.prepare('failed-audit')
        old = self.older()
        retired = self.root / old['pendingPath']
        ledger = self.queue.with_name('verify-deleted.jsonl')
        ledger.mkdir()
        self.assertEqual(self.write('Newer source.'), 0)
        self.assertTrue(retired.exists())
        self.assertTrue(self.resume())  # Live review is allowed while retirement is blocked.
        ledger.rmdir()
        self.assertEqual(self.run_tag('v1.1.0'), {'remaining': 0, 'calls': 1})
        self.assertIn(self.project.revision(self.source)['sha256'], self.target.read_text())
        self.assertTrue(retired.exists())
        self.assertEqual(self.audits(), [])
        ledger.mkdir(exist_ok=True)
        self.assertFalse(self.resume())  # The emptied queue still owes the audit.
        ledger.rmdir()
        unlink = Path.unlink
        def crash_before_unlink(path, *args, **kwargs):
            if path == retired:
                raise KeyboardInterrupt
            return unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', crash_before_unlink), self.assertRaises(KeyboardInterrupt):
            self.resume()
        self.assertEqual(len(self.audits()), 1)
        self.assertTrue(retired.exists())
        self.assertTrue(self.resume())
        self.assertTrue(self.resume())
        self.assertEqual(len(self.audits()), 1)
        self.assertFalse(retired.exists())

    def test_malformed_retirement_cannot_authorize_cleanup(self):
        self.prepare('corrupt')
        old = self.older()
        state = self.queue.with_name('.pending-ownership.json')
        valid = json.loads(state.read_text())
        self.queue.write_text('')
        for plan in ({}, {'job': 1}, {'job': old, 'sha256': [], 'swap': '../wrong'}):
            state.write_text(json.dumps({**valid, 'retirements': {old['pendingPath']: plan}}))
            self.assertFalse(self.resume())
            self.assertTrue((self.root / old['pendingPath']).exists())
            self.assertEqual(self.audits(), [])

    def test_crash_after_queue_rename_leaves_the_audit_recoverable(self):
        self.prepare('published')
        old = self.older()
        replace = os.replace
        def interrupted(src, dst):
            replace(src, dst)
            if Path(dst) == self.queue:
                raise KeyboardInterrupt
        with patch.object(os, 'replace', side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            self.write('Newest source.')
        self.assertEqual(self.audits(), [])
        self.assertTrue((self.root / old['pendingPath']).exists())
        self.assertEqual(self.run_tag('v1.1.0'), {'remaining': 0, 'calls': 1})
        self.assertTrue(self.resume())
        self.assertEqual(len(self.audits()), 1)
        self.assertFalse((self.root / old['pendingPath']).exists())

    def test_two_manually_edited_slots_leave_a_fresh_name_available(self):
        self.prepare('slots')
        self.assertEqual(self.write('Newest source.'), 0)
        edited = {}
        for _ in range(2):
            job = json.loads(self.queue.read_text())
            path = self.root / job['pendingPath']
            edited[path] = path.read_bytes() + b'\nOperator note.'
            path.write_bytes(edited[path])
            self.assertEqual(self.write('Newest source.'), 0)
        self.assertEqual(self.run_tag('v1.1.0'), {'remaining': 0, 'calls': 1})
        self.assertIn(self.project.revision(self.source)['sha256'], self.target.read_text())
        self.assertEqual({path: path.read_bytes() for path in edited}, edited)

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

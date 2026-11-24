"""A released compactor preserves unrelated refresh and deletion evidence across excluded edits."""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from memvane.diagnostics import api as events
from memvane.settings import api as settings
from memvane.wiki.card import api as card, types as card_types
from memvane.wiki.compile import selected_execution as execution
from memvane.wiki.compile.tests.integration import test_pending_handoff as fixtures
from memvane.wiki.engines import api as engines
from memvane.wiki.intake import api as intake
from memvane.wiki.verify import api as verify
from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase


class SourcePathLineSeparators(FixtureCase):
    prepare = fixtures.PendingHandoff.prepare
    write = fixtures.PendingHandoff.write

    def edit(self, path):
        payload = {'hook_event_name': 'PostToolUse', 'tool_name': 'Edit', 'cwd': str(self.root),
                   'engine': 'codex', 'tool_input': {'file_path':str(path)}}
        found = {'projectRoot': str(self.root), 'config': self.config}
        with patch.object(settings, 'find_project_config', return_value=found), \
                patch.object(workspace, 'selected', return_value=self.bound), patch.object(workspace, 'request_compile') as wake:
            count = intake.enqueue_payload(payload)['queued']
        self.assertEqual(wake.call_count, int(bool(count)))
        return count

    def test_forced_and_replayed_sources_retain_legacy_event_without_paid_work(self):
        from memvane.wiki.compile import wiki_compile_worker as worker
        self.prepare('excluded-recovery')
        queue = self.queue.with_name('source-queue.jsonl')
        raw = b''
        ledger = queue.parent / card_types.SOURCE_REFRESH_PENDING
        ledger.parent.mkdir(exist_ok=True)
        original = self.target.read_bytes()
        for i, sep in enumerate(('\x85', '\u2028', '\u2029')):
            rel = 'docs/hidden' + sep + '/note.md'
            self.project.write_note(rel, 'original\n')
            event = {'sourcePath': rel, 'eventId': str(i + 1) * 32, 'ts': '2026-11-24T00:00:00Z',
                     'engine': 'codex', 'state': 'pending_refresh'}
            raw += (json.dumps(event, ensure_ascii=True) + '\n').encode()
            self.assertIsNone(card.record_pending_refresh(self.root, rel, 'codex'))
            for forced in (True, False):
                with patch.object(engines, 'run_extractor') as paid:
                    self.assertEqual(worker.process_job(self.root, self.config, self.config['compile'],
                                                        {'source': {'path': rel}, 'forceRecompile': forced}),
                                     (False, True, []))
                    paid.assert_not_called()
        ledger.write_bytes(raw)
        for _ in range(2):
            self.assertEqual(worker.recover_pending_refreshes(self.root, self.config, queue), 0)
            self.assertEqual(ledger.read_bytes(), raw)
            self.assertFalse(queue.exists())
            self.assertEqual(self.target.read_bytes(), original)
            self.assertEqual(len(card.unresolved_pending_refreshes_strict(self.root)), 3)

    def case(self, tag, sep):
        self.prepare(tag + '-' + str(ord(sep)))
        self.target.unlink()
        self.source = self.project.write_note('docs/line-name.md', 'Newest source' + sep + 'in body.')
        special = self.project.write_note('docs/line'+sep+'name.md', 'Excluded file.')
        self.project.write_note('docs/settled.md', 'Stable.')
        repo = Path(__file__).resolve().parents[5]
        bundle = fixtures.archived_tag(repo, self.tmp, tag)
        harness = repo / 'memvane/wiki/card/tests/tagged_refresh_manifest.py'
        rel = 'memvane/wiki/card/wiki_freshness.py'
        self.assertEqual((bundle / rel).read_bytes(), subprocess.check_output(['git', 'show', tag + ':' + rel], cwd=repo))

        def released(*args):
            child = subprocess.run([sys.executable, '-B', str(harness), str(bundle), str(self.root), *args],
                                   cwd=bundle, capture_output=True, text=True,
                                   env={**os.environ, 'PYTHONPATH': str(bundle), 'PYTHONNOUSERSITE': '1'})
            self.assertEqual(child.returncode, 0, child.stderr)
            return json.loads(child.stdout)

        unresolved, done = released('seed')
        ledger = self.queue.parent / card_types.SOURCE_REFRESH_PENDING
        manifest = self.queue.parent / 'generated-manifest.jsonl'
        source_queue = self.queue.with_name('source-queue.jsonl')
        before = ledger.read_bytes()
        with patch.object(events, 'emit', wraps=events.emit) as emitted:
            self.assertEqual(self.edit(special), 0)
        reasons = [c for c in emitted.call_args_list if c.kwargs.get('reason') == 'compile_source_path_line_separator']
        self.assertEqual(len(reasons), 1)
        self.assertNotIn(str(special), repr(reasons))
        self.assertEqual(ledger.read_bytes(), before)
        self.assertFalse(any(p.exists() for p in (source_queue, self.queue, manifest, self.target)))
        self.assertEqual(self.edit(self.source), 1)
        claim = workspace.claim_queue(source_queue, private=True)
        _, source_job = workspace.read_queue(claim)[0]
        self.assertEqual(source_job['source']['path'], 'docs/line-name.md')
        self.assertEqual(self.write(self.source.read_text()), 0)
        answer = {'verdict':'pass','claims':[{'claim':'Decision','supported':True,'quote':'Newest source',
                                               'sourcePath':'docs/line-name.md'}],'reasons':[]}
        with patch.object(workspace, 'selected',return_value=self.bound), patch.object(workspace,'recheck'), \
                patch.object(workspace,'request_sync'), patch.object(engines,'run_extractor',return_value=(answer,None,0)) as engine:
            self.assertEqual(verify.run(self.root,self.config,self.config['compile'])['remaining'],0)
            self.assertEqual(engine.call_count,1)
        self.assertIn(self.project.revision(self.source)['sha256'], self.target.read_text())
        written = self.project.revision(self.source)
        current_event = card.latest_unresolved_pending(self.root, source_job['source']['path'])
        self.assertTrue(card.resolve_pending_refresh(self.root, current_event, written, [written],
                                                    source_job['pendingRefresh']['eventId']))
        workspace.read_queue(claim).finish(source_queue, claim, [])
        self.assertFalse(source_queue.exists() and source_queue.read_bytes())
        self.assertFalse(self.queue.exists() and self.queue.read_bytes())
        self.assertEqual(card.unresolved_pending_refreshes_strict(self.root), [unresolved])
        self.assertFalse(execution._compile_idle(self.root, source_queue, self.queue, self.bound))
        self.target.unlink()
        target = self.target.relative_to(self.root).as_posix()
        created = [json.loads(row) for row in manifest.read_bytes().splitlines()][-1]
        self.assertEqual((created['targetPath'],created['action']), (target,'created'))
        result = released(str(manifest), str(ledger), target, unresolved['eventId'], done['eventId'])
        self.assertEqual(result, [created, created])
        self.assertEqual(card.unresolved_pending_refreshes_strict(self.root), [unresolved])
        self.assertFalse(execution._compile_idle(self.root,source_queue,self.queue,self.bound))
        before = ledger.read_bytes()
        self.assertEqual(self.edit(special), 0)
        self.assertEqual(ledger.read_bytes(),before)
        self.assertEqual(self.edit(self.source), 1)
        self.assertEqual(self.write('Newest source after deletion.'), 0)
        self.assertFalse(self.target.exists())
        tombstones = [json.loads(row) for row in (self.queue.parent/'tombstones.jsonl').read_bytes().splitlines()]
        self.assertEqual(sum(r['targetPath']==target for r in tombstones),1)

    def test_native_edits_and_two_released_compactions(self):
        for tag in ('v1.0.22','v1.0.23','v1.0.24','v1.1.0'):
            for sep in ('\x85','\u2028','\u2029'):
                with self.subTest(tag=tag,sep=repr(sep)):
                    self.case(tag,sep)

"""The released compactor preserves creation and human deletion for every source-text separator."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from contextlib import redirect_stdout
from unittest.mock import patch

from memvane.wiki.compile import wiki_compile as writer, wiki_compile_worker as worker
from memvane.wiki.compile.tests import release_tags
from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase


class SummaryHistory(FixtureCase):
    def prepare(self, name):
        release_tags.prepare_selected_card(self, name)

    def test_summary_survives_two_released_compactions_and_human_deletion(self):
        repo = Path(__file__).resolve().parents[5]
        for tag in ('v1.0.22', 'v1.0.23', 'v1.0.24', 'v1.1.0'):
            bundle = release_tags.archived_tag(repo, self.tmp, tag)
            for separator in ('\u0085', '\u2028', '\u2029'):
                with self.subTest(tag=tag, codepoint=ord(separator)):
                    self.prepare(tag + str(ord(separator)))
                    self.target.unlink()
                    self.check_summary(repo, bundle, separator)

    def test_incompatible_identity_is_held_without_rewriting_the_value(self):
        for separator in ('\u0085', '\u2028', '\u2029'):
            identity = 'docs/first' + separator + 'second.md'
            record = {'summary': 'display' + separator + 'value', 'sources': [{'path': identity}]}
            self.assertIsNone(writer.public_history(record, action='created'))
            self.assertEqual(record['sources'][0]['path'], identity)
            self.assertIn(separator, record['summary'])

    def check_summary(self, repo, bundle, separator):
        summary = 'First sentence.' + separator + 'Second sentence.'
        self.source.write_text(summary)
        source = {'kind': 'file', 'path': 'docs/source.md', 'collection': self.project.raw_name}
        job = {'cwd': str(self.root), 'engine': 'claude', 'trigger': 'post_tool_source',
               'source': source, 'selectedRoute': workspace.route_identity(self.bound)}
        returned = {'candidates': [{'title': 'Decision', 'summary': summary}]}
        results = []

        def write(root, candidate, **kwargs):
            output = io.StringIO()
            with workspace.card_write_lock(root), workspace.mutation_lock(root), redirect_stdout(output):
                code = writer._compile_locked(root, self.config, self.config['compile'],
                                              self.project.wiki, candidate)
            self.assertEqual(code, 0, output.getvalue())
            result = json.loads(output.getvalue())
            results.append(result)
            return result

        with patch.object(workspace, 'selected', return_value=self.bound), patch.object(workspace, 'recheck'), \
             patch.object(workspace, 'request_sync'), patch.object(workspace, 'request_compile'), \
             patch.object(worker, 'compile_candidate', side_effect=write), \
             patch.object(worker, 'gather_similar_pages', return_value=[]), \
             patch.object(worker, 'run_extractor', return_value=(returned, None, 0)):
            worker.process_job(self.root, self.config, self.config['compile'], job)
            result, = results
            self.assertEqual(result['action'], 'created')
            card = self.root / result['targetPath']
            self.assertIn(summary.encode(), card.read_bytes())
            ledger = self.queue.parent / 'generated-manifest.jsonl'
            original, = [json.loads(line) for line in ledger.read_bytes().split(b'\n') if line]
            self.assertEqual(original['sourceRevisions'][0]['sha256'], hashlib.sha256(self.source.read_bytes()).hexdigest())
            self.assertEqual(original['summary'], summary.replace(separator, '\n'))
            card.unlink()
            helper = repo / 'memvane/wiki/workspace/tests/tagged_manifest.py'
            output = subprocess.check_output([sys.executable, '-B', str(helper), str(bundle), str(ledger),
                         result['targetPath']], cwd=bundle, env={**os.environ, 'PYTHONPATH': str(bundle)}, text=True)
            self.assertEqual(json.loads(output), [original])
            self.source.write_text(summary + ' Changed.')
            worker.process_job(self.root, self.config, self.config['compile'], job)
            self.assertEqual(results[-1]['action'], 'created')
            self.assertEqual(results[-1]['targetPath'], original['targetPath'])
            self.assertTrue(card.exists())
            self.assertFalse((ledger.parent / 'tombstones.jsonl').exists())

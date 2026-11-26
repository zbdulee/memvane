"""Manual compact input carries compiler-owned, bounded source evidence."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from memvane.wiki.compile import wiki_extract as extract
from tests.support.case import FixtureCase
from tests.support.install_builder import build_install
from tests.support.project import make_project


class ManualExtract(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home)

    def test_compact_identity_and_transcript_redaction(self):
        item = {'title': ' Rule ', 'summary': 'A durable fact', 'type': 'decision',
                'canonicalKey': ' rule-key ', 'aliases': [' Alias ', '']}
        candidate = extract.to_candidate({'sourceRef': 'session:local'}, item)
        self.assertEqual((candidate['title'], candidate['suggestedType']), ('Rule', 'decision'))
        self.assertEqual((candidate['canonicalKey'], candidate['aliases']), ('rule-key', ['Alias']))
        self.assertEqual(candidate['sources'], [{'kind': 'session', 'ref': 'session:local'}])
        rejected = extract.to_candidate({}, {**item, 'summary': 'User: private input\nAssistant: reply'})
        self.assertNotIn('private input', repr(rejected))
        self.assertIn('[REDACTED_TRANSCRIPT]', rejected['summary'])
        self.assertEqual(extract.compact_items({'notes': 'no durable item'}), [])

    def test_manual_entry_writes_card_and_verify_job_with_observed_source(self):
        source = self.project.write_note('docs/api.md', 'synthetic source content\n')
        self.project.git('add', '--', '.memvane/settings.json', 'docs')
        self.project.git('commit', '-qm', 'fixture')
        build_install(self, self.project)
        payload = {'trigger': 'manual', 'candidates': [{'title': 'API Port',
                   'summary': 'The synthetic API port is fixed.', 'suggestedType': 'concept',
                   'canonicalKey': 'api-port', 'aliases': ['API alias'],
                   'sources': [{'kind': 'file', 'path': 'docs/api.md'}],
                   'sourceRevisions': [{'path': 'decoy.md'}]}]}
        root = Path(__file__).resolve().parents[5]
        result = subprocess.run([sys.executable, '-B', str(root / 'entry/run.py'), 'wiki-extract',
                                 '--cwd', str(self.project.root)], input=json.dumps(payload),
                                capture_output=True, text=True, timeout=60, cwd=str(root))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['action'], 'created')
        page = self.project.wiki / 'concepts/api-port.md'
        text = page.read_text()
        for name in ('SCHEMA.md', 'index.md', 'log.md'):
            self.assertFalse((self.project.wiki / name).exists())
        manifest = self.project.root / '.memvane/compile/generated-manifest.jsonl'
        row = json.loads(manifest.read_text().splitlines()[-1])
        self.assertEqual((row['action'], row['targetPath']), ('created', '.memvane/wiki/concepts/api-port.md'))
        self.assertIn('API alias', text)
        self.assertIn(hashlib.sha256(source.read_bytes()).hexdigest(), text)
        self.assertNotIn('decoy.md', text)
        queue = self.project.root / '.memvane/compile/verify-queue.jsonl'
        self.assertFalse(queue.exists() and queue.read_bytes())
        from tests.support.wiki_handoff import work_rows
        (state, raw, _), = work_rows(queue, 'hold')
        job = json.loads(raw)
        self.assertEqual((state['phase'], state['binding']['job']), ('held', job))
        self.assertEqual([x['path'] for x in job['authoritativeSources']], ['docs/api.md'])
        self.assertEqual(job['sourceRevisions'][0]['sha256'], hashlib.sha256(source.read_bytes()).hexdigest())
        bad = {'durable': {'title': 'Transcript', 'summary': 'User: private\nAssistant: reply',
                           'canonicalKey': 'transcript', 'aliases': ['Transcript alias']}}
        rejected = subprocess.run([sys.executable, '-B', str(root / 'entry/run.py'), 'wiki-extract',
                                   '--cwd', str(self.project.root)], input=json.dumps(bad),
                                  capture_output=True, text=True, timeout=60, cwd=str(root))
        self.assertEqual(rejected.returncode, 0, rejected.stderr)
        self.assertEqual(json.loads(rejected.stdout)['action'], 'rejected')
        self.assertFalse((self.project.wiki / 'concepts/transcript.md').exists())
        audit = (self.project.root / '.memvane/compile/candidates.jsonl').read_text()
        self.assertNotIn('User: private', audit)
        self.assertIn('Transcript alias', audit)
        noop = subprocess.run([sys.executable, '-B', str(root / 'entry/run.py'), 'wiki-extract',
                               '--cwd', str(self.project.root)], input=json.dumps({'notes': 'not durable'}),
                              capture_output=True, text=True, timeout=60, cwd=str(root))
        self.assertEqual((noop.returncode, noop.stdout), (0, ''))

    def test_snapshot_replaces_untrusted_caller_evidence(self):
        source = self.project.write_note('docs/api.md', 'synthetic content\n')
        candidate = {'sources': [{'kind': 'file', 'path': 'docs/api.md'},
                                 {'kind': 'session', 'ref': 'local'}],
                     'sourceRevisions': [{'path': 'decoy.md'}],
                     'authoritativeSources': [{'path': 'decoy.md'}]}
        extract.attach_compiler_provenance(str(self.project.root), candidate)
        self.assertEqual([x['path'] for x in candidate['authoritativeSources']], ['docs/api.md'])
        self.assertEqual([x['path'] for x in candidate['sourceRevisions']], ['docs/api.md'])
        self.assertEqual(candidate['sourceRevisions'][0]['sha256'],
                         hashlib.sha256(source.read_bytes()).hexdigest())
        candidate['sources'] = [{'kind': 'session', 'ref': 'local'}]
        extract.attach_compiler_provenance(str(self.project.root), candidate)
        self.assertNotIn('sourceRevisions', candidate)
        self.assertNotIn('authoritativeSources', candidate)

    def test_missing_or_outside_source_discards_all_evidence(self):
        self.project.write_note('docs/api.md', 'present\n')
        outside = self.home / 'outside.md'
        outside.write_text('outside\n')
        for bad in ('docs/missing.md', str(outside)):
            with self.subTest(bad=bad):
                sources = [{'kind': 'file', 'path': 'docs/api.md'}, {'kind': 'file', 'path': bad}]
                self.assertEqual(extract.compiler_provenance(str(self.project.root), sources), ([], []))

    def test_hardlink_aliases_are_deduplicated_before_distinct_and_scan_caps(self):
        root = self.project.root
        first = self.project.write_note('docs/first.md', 'same\n')
        aliases = [root / ('docs/alias%d.md' % i) for i in range(3)]
        for alias in aliases:
            os.link(first, alias)
        def entries(paths):
            return [{'kind': 'file', 'path': str(path.relative_to(root))} for path in paths]
        auth, revisions = extract.compiler_provenance(str(root), entries([first] + aliases))
        self.assertEqual([x['path'] for x in auth], ['docs/first.md'])
        self.assertEqual([x['path'] for x in revisions], ['docs/first.md'])
        distinct = [self.project.write_note('docs/distinct%d.md' % i, str(i)) for i in range(4)]
        self.assertEqual(extract.compiler_provenance(str(root), entries(distinct)), ([], []))
        with patch.object(extract.wiki_card, 'resolve_existing_source') as resolve:
            self.assertEqual(extract.compiler_provenance(str(root), entries([first] * 7)), ([], []))
            resolve.assert_not_called()
        self.assertEqual(extract.MAX_PROVENANCE_SOURCE_SCAN, 2 * extract.MAX_PROVENANCE_FILE_SOURCES)

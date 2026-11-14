"""Manual compact input carries compiler-owned, bounded source evidence."""
import hashlib
import os
from unittest.mock import patch

from memvane.wiki.compile import wiki_extract as extract
from tests.support.case import FixtureCase
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

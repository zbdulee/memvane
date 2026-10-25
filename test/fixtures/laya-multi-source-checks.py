"""Disposable two-collection source mapping and same-generation checks."""
from __future__ import annotations

from pathlib import Path
import hashlib
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
from laya_index_a import IndexContractError, LayaIndexA
import laya_host_response
from laya_index_host_route import wiki_index_roots
import yaml_scalars
from laya_index_source_port import (publish_collections_from_source,
                                    scan_collections,
                                    search_collections_verified,
                                    stage_collections_from_source)


class MultiSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='laya-multi-source-')
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name).resolve()
        self.raw = self.project / 'docs' / 'plans'
        self.wiki = self.project / '.auto-context' / 'wiki'
        self.raw.mkdir(parents=True)
        self.wiki.mkdir(parents=True)
        (self.raw / 'raw.md').write_text('shared phrase raw', encoding='utf-8')
        (self.wiki / 'card.md').write_text('# Card\nshared phrase wiki', encoding='utf-8')
        self.roots = {'plans': str(self.raw), 'wiki': str(self.wiki)}
        self.index = LayaIndexA(self.project / 'index.sqlite', create=True)
        self.addCleanup(self.index.close)

    def test_hidden_wiki_root_is_explicitly_scanned_in_one_manifest(self):
        source = scan_collections(self.roots, 'scope-a', self.index)
        self.assertEqual([file.path for file in source.files],
                         ['plans/raw.md', 'wiki/card.md'])
        self.assertEqual(len(source.manifest), 64)
        self.assertEqual(stage_collections_from_source(
            self.index, self.roots, 'scope-a', 'g1', base=None,
            claim_digest='a' * 64, claim_epoch=0), 'prepared')
        self.assertEqual(publish_collections_from_source(
            self.index, self.roots, 'scope-a', None, 'g1', 'claim-1',
            claim_digest='a' * 64, claim_epoch=0), 'published')
        token, gen = self.index.pin('scope-a')
        try:
            self.assertEqual(gen, 'g1')
            rows = search_collections_verified(
                self.index, self.roots, token, 'shared', 5,
                collection_names=('wiki',))
            self.assertEqual([row['path'] for row in rows], ['wiki/card.md'])
        finally:
            self.index.release(token)

    def test_namespaced_path_exclusion_cannot_be_served(self):
        self.index.exclude('scope-a', path='wiki/card.md')
        source = scan_collections(self.roots, 'scope-a', self.index)
        self.assertFalse(source.by_path['wiki/card.md'].eligible)
        self.assertTrue(source.by_path['plans/raw.md'].eligible)

    def test_missing_second_root_blocks_whole_scan(self):
        self.wiki.rename(self.project / '.auto-context' / 'moved-wiki')
        with self.assertRaisesRegex(IndexContractError, 'index_stale'):
            scan_collections(self.roots, 'scope-a', self.index)

    def test_selected_v2_indexes_wiki_excerpt_and_locator_but_no_raw_document(self):
        raw_only = 'RAW_ONLY_X'
        (self.raw / 'raw.md').write_text(raw_only + '\nSHARED_Y\n', encoding='utf-8')
        (self.wiki / 'card.md').write_text(
            '# Card\nSHARED_Y\n↳ docs/plans/raw.md#L2\n', encoding='utf-8')
        collections = [
            {'name': 'plans', 'role': 'raw', 'sourceRoot': str(self.raw)},
            {'name': 'wiki', 'role': 'wiki', 'sourceRoot': str(self.wiki)}]
        roots = wiki_index_roots(collections)
        self.assertEqual(roots, {'wiki': str(self.wiki)})
        source = scan_collections(roots, 'scope-a', self.index)
        self.assertEqual([file.path for file in source.files], ['wiki/card.md'])
        stage_collections_from_source(self.index, roots, 'scope-a', 'g-wiki',
                                      base=None, claim_digest='a' * 64,
                                      claim_epoch=0)
        rows = self.index.db.execute('SELECT path FROM membership').fetchall()
        self.assertEqual(rows, [('wiki/card.md',)])
        bodies = [row[0] for row in self.index.db.execute('SELECT body FROM content')]
        self.assertEqual(len(bodies), 1)
        self.assertIn('SHARED_Y', bodies[0])
        self.assertIn('docs/plans/raw.md#L2', bodies[0])
        self.assertNotIn(raw_only, bodies[0])
        self.assertEqual(self.index.db.execute('SELECT COUNT(*) FROM content_fts').fetchone()[0], 1)
        publish_collections_from_source(self.index, roots, 'scope-a', None,
                                        'g-wiki', 'claim-wiki', claim_digest='a' * 64,
                                        claim_epoch=0)
        token, _ = self.index.pin('scope-a')
        try:
            self.assertEqual(self.index.search_many(token, (raw_only,), 5)[0], [])
            found = self.index.search_many(token, ('SHARED_Y',), 5)[0]
            self.assertEqual([item['path'] for item in found], ['wiki/card.md'])
        finally:
            self.index.release(token)

    def test_topical_wiki_card_injects_summary_and_verified_locator_only(self):
        # The QA corpus must contain a real topic card. An index of only the
        # wiki scaffold cannot establish selected-host topical recall.
        source = self.raw / 'raw.md'
        raw_bytes = b'# Release QA\nAlpha topic is wikiblue.\nRAW_ONLY_NEVER_INJECT\n'
        source.write_bytes(raw_bytes)
        revision = {'kind': 'file', 'path': 'docs/plans/raw.md', 'collection': 'plans',
                    'sha256': hashlib.sha256(raw_bytes).hexdigest(),
                    'size': len(raw_bytes), 'mtimeNs': source.stat().st_mtime_ns}
        span = {'path': 'docs/plans/raw.md', 'sourceSha256': revision['sha256'],
                'startLine': 2, 'endLine': 2,
                'spanSha256': hashlib.sha256(b'Alpha topic is wikiblue.\n').hexdigest()}
        template = ('---\ntitle: Release QA alpha\nstatus: verified\n'
                    'createdBy: qmd-auto-context\nsourceRevisions:\n  - '
                    + json.dumps(revision) + '\nsourceSpans:\n  - '
                    + json.dumps(span) + '\nverifiedBodyHash: {digest}\n'
                    '---\n## Summary\nThe alpha topic is wikiblue.\n')
        card = template.replace('{digest}', yaml_scalars.card_body_hash(
            template.replace('{digest}', '0' * 64)))
        (self.wiki / 'card.md').write_text(card, encoding='utf-8')
        collections = [{'name': 'plans', 'role': 'raw', 'sourceRoot': str(self.raw)},
                       {'name': 'wiki', 'role': 'wiki', 'sourceRoot': str(self.wiki)}]
        roots = wiki_index_roots(collections)
        source_view = scan_collections(roots, 'scope-a', self.index)
        self.assertEqual([file.path for file in source_view.files], ['wiki/card.md'])
        stage_collections_from_source(self.index, roots, 'scope-a', 'g-topical',
                                      base=None, claim_digest='a' * 64,
                                      claim_epoch=0)
        publish_collections_from_source(self.index, roots, 'scope-a', None,
                                        'g-topical', 'claim-topical',
                                        claim_digest='a' * 64, claim_epoch=0)
        token, generation = self.index.pin('scope-a')
        try:
            self.assertEqual(self.index.search_many(token, ('RAW_ONLY_NEVER_INJECT',), 5)[0], [])
            found = self.index.search_many(token, ('wikiblue',), 5)[0]
            self.assertEqual([item['path'] for item in found], ['wiki/card.md'])
            row = {'scope': 'a' * 32, 'generation': generation,
                   'path': found[0]['path'], 'hash': found[0]['hash'],
                   'body': found[0]['body'], 'score': 0.8}
            decision = {'state': 'candidate', 'scopeId': 'a' * 32,
                        'projectRoot': str(self.project),
                        'collectionRoots': {'plans': str(self.raw), 'wiki': str(self.wiki)},
                        'collections': collections}
            locations = {}
            self.assertTrue(laya_host_response._verified_wiki_row(
                row, decision, source_locations=locations))
            context = laya_host_response.render_context(
                decision, [row], source_locations=locations)
            self.assertIn('wikiblue', context)
            self.assertIn('docs/plans/raw.md#L2-L2', context)
            self.assertNotIn('RAW_ONLY_NEVER_INJECT', context)
        finally:
            self.index.release(token)


if __name__ == '__main__':
    unittest.main()

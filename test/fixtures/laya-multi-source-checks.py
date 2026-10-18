"""Disposable two-collection source mapping and same-generation checks."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
from laya_index_a import IndexContractError, LayaIndexA
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


if __name__ == '__main__':
    unittest.main()

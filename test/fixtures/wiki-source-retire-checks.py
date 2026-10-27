import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import wiki_source_retire as retire
import yaml_scalars


class DeletedSourceRetirement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.wiki = self.root / '.auto-context/wiki/concepts'
        self.wiki.mkdir(parents=True)
        (self.root / '.auto-context/compile').mkdir()
        (self.root / 'docs').mkdir()
        self.hash = hashlib.sha256(b'old source').hexdigest()
        self.selected = ({'projectRoot': str(self.root),
                          'collectionRoots': {'wiki': str(self.wiki.parent)},
                          'collections': [{'name': 'wiki', 'role': 'wiki',
                                           'sourceRoot': str(self.wiki.parent)}]}, {})

    def card(self, name='card.md', shared=False, authored=False, survivor_only=False):
        revisions = [{'kind': 'file', 'path': 'docs/gone.md', 'collection': 'raw',
                      'sha256': self.hash, 'size': 10, 'mtimeNs': 1}]
        if shared:
            revisions.append({'kind': 'file', 'path': 'docs/still.md', 'collection': 'raw',
                              'sha256': 'b' * 64, 'size': 10, 'mtimeNs': 1})
        if survivor_only:
            revisions = [{'kind': 'file', 'path': 'docs/still.md', 'collection': 'raw',
                          'sha256': 'b' * 64, 'size': 10, 'mtimeNs': 1}]
        card = self.wiki / name
        card.write_text('---\ntitle: Card\nstatus: verified\ncreatedBy: ' +
                        ('human' if authored else 'qmd-auto-context') + '\n' +
                        'sourceRevisions:\n' + '\n'.join(
                            '  - ' + item for item in yaml_scalars.dump_source_revisions(revisions)) +
                        '\n---\n<!-- qmd:auto:start id="main" sourceHash="a" -->\n' +
                        'old completed fact\n<!-- qmd:auto:end -->\n', encoding='utf-8')
        return card

    def run_retire(self):
        with patch.object(retire.wiki_selected_laya, 'recheck'), \
             patch.object(retire.wiki_selected_laya, 'request_sync') as wake, \
             patch.object(retire.wc, 'remove_from_index'):
            result = retire.retire_deleted_source(self.root, {'wikiPath': '.auto-context/wiki'},
                                                   self.selected, 'raw', 'docs/gone.md', self.hash)
            return result['state'], wake.call_count

    def test_single_source_is_archived_and_replay_is_safe(self):
        card = self.card()
        old = card.read_bytes()
        self.assertEqual(self.run_retire(), ('retired', 1))
        self.assertFalse(card.exists())
        archive = list((self.root / '.auto-context/compile/retired-wiki').glob('*.md'))
        self.assertEqual(len(archive), 1)
        self.assertEqual(archive[0].read_bytes(), old)
        self.assertEqual(self.run_retire(), ('retired', 1))

    def test_shared_and_human_cards_are_not_unlinked(self):
        shared = self.card('shared.md', shared=True)
        self.assertEqual(self.run_retire(), ('shared_pending', 0))
        self.assertTrue(shared.exists())
        shared.unlink()
        human = self.card('human.md', authored=True)
        self.assertEqual(self.run_retire(), ('retired', 1))
        self.assertTrue(human.exists())

    def test_deleted_path_retires_older_generated_revisions_too(self):
        card = self.card()
        text = card.read_text(encoding='utf-8').replace(self.hash, 'c' * 64)
        card.write_text(text, encoding='utf-8')
        self.assertEqual(self.run_retire(), ('retired', 1))
        self.assertFalse(card.exists())

    def test_shared_card_retires_only_after_a_verified_survivor_replacement(self):
        import laya_host_response
        shared = self.card('shared.md', shared=True)
        replacement = self.card('replacement.md', survivor_only=True)
        with patch.object(laya_host_response, '_verified_wiki_row', return_value=True):
            self.assertEqual(self.run_retire(), ('retired', 1))
        self.assertFalse(shared.exists())
        self.assertTrue(replacement.exists())

    def test_reappeared_source_cancels_stale_deletion(self):
        card = self.card()
        (self.root / 'docs/gone.md').write_text('new source', encoding='utf-8')
        self.assertEqual(self.run_retire(), ('source_reappeared', 0))
        self.assertTrue(card.exists())


if __name__ == '__main__':
    unittest.main()

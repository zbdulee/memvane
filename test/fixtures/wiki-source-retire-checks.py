import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import wiki_source_retire as retire
import yaml_scalars


class RetireFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.wiki = self.root / '.auto-context/wiki/concepts'
        self.wiki.mkdir(parents=True)
        (self.root / '.auto-context/compile').mkdir()
        (self.root / 'docs').mkdir()
        self.survivor = self.root / 'docs/still.md'
        self.survivor.write_bytes(b'still source')
        self.hash = hashlib.sha256(b'old source').hexdigest()
        self.selected = ({'projectRoot': str(self.root),
                          'collectionRoots': {'wiki': str(self.wiki.parent),
                                              'raw': str(self.root / 'docs')},
                          'collections': [{'name': 'wiki', 'role': 'wiki',
                                           'sourceRoot': str(self.wiki.parent)},
                                          {'name': 'raw', 'role': 'raw',
                                           'sourceRoot': str(self.root / 'docs')}]}, {})

    def card(self, name='card.md', shared=False, authored=False, survivor_only=False,
             label='raw', path='docs/gone.md'):
        revisions = [{'kind': 'file', 'path': path, 'collection': label,
                      'sha256': self.hash, 'size': 10, 'mtimeNs': 1}]
        if shared:
            revisions.append({'kind': 'file', 'path': 'docs/still.md', 'collection': label,
                              'sha256': 'b' * 64, 'size': 12, 'mtimeNs': 1})
        if survivor_only:
            revisions = [{'kind': 'file', 'path': 'docs/still.md', 'collection': 'raw',
                          'sha256': hashlib.sha256(self.survivor.read_bytes()).hexdigest(),
                          'size': self.survivor.stat().st_size,
                          'mtimeNs': self.survivor.stat().st_mtime_ns}]
        card = self.wiki / name
        text = ('---\ntitle: Card\nstatus: verified\ncreatedBy: ' +
                        ('human' if authored else 'qmd-auto-context') + '\n' +
                        'sourceRevisions:\n' + '\n'.join(
                            '  - ' + item for item in yaml_scalars.dump_source_revisions(revisions)) +
                        '\nverifiedBodyHash: {digest}\n---\n'
                        '<!-- qmd:auto:start id="main" sourceHash="a" -->\n' +
                        'old completed fact\n<!-- qmd:auto:end -->\n')
        text = text.replace('{digest}', yaml_scalars.card_body_hash(
            text.replace('{digest}', '0' * 64)))
        card.write_text(text, encoding='utf-8')
        return card

    def run_retire(self, config=None, collection='raw', rel='docs/gone.md', full=False):
        with patch.object(retire.wiki_selected_laya, 'recheck'), \
             patch.object(retire.wiki_selected_laya, 'request_sync') as wake:
            result = retire.retire_deleted_source(
                self.root, config or {'wikiPath': '.auto-context/wiki'},
                self.selected, collection, rel, self.hash)
            return result if full else (result['state'], wake.call_count)

class DeletedSourceRetirement(RetireFixture):
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
        shared = self.card('shared.md', shared=True)
        replacement = self.card('replacement.md', survivor_only=True)
        self.assertEqual(self.run_retire(), ('retired', 1))
        self.assertFalse(shared.exists())
        self.assertTrue(replacement.exists())

    def test_shared_card_retires_when_every_source_is_deleted(self):
        shared = self.card('shared.md', shared=True)
        self.survivor.unlink()
        self.assertEqual(self.run_retire(), ('retired', 1))
        self.assertFalse(shared.exists())

    def test_archive_sidecar_identifies_the_original_card(self):
        card = self.card('recoverable.md')
        self.assertEqual(self.run_retire(), ('retired', 1))
        sidecars = list((self.root / '.auto-context/compile/retired-wiki').glob('*.json'))
        self.assertEqual(len(sidecars), 1)
        self.assertIn('concepts/recoverable.md', sidecars[0].read_text())

    def test_forced_refresh_and_shared_wait_keep_distinct_queue_keys(self):
        import wiki_source_intent as intent
        base = {'source': {'collection': 'raw', 'path': 'docs/still.md'},
                'contentRevision': 'a' * 64, 'trigger': 'post_sync_source'}
        keys = [intent.revision_key({**base, **flags}) for flags in
                ({}, {'sourceDelete': True}, {'forceRecompile': True},
                 {'sourceDelete': True, 'sharedEnqueued': True})]
        self.assertEqual(len(set(keys)), 4)

    def test_reappeared_source_cancels_stale_deletion(self):
        card = self.card()
        (self.root / 'docs/gone.md').write_text('new source', encoding='utf-8')
        self.assertEqual(self.run_retire(), ('source_reappeared', 0))
        self.assertTrue(card.exists())


class ScaffoldPagesAreNotCards(RetireFixture):
    """wiki_init scaffold pages are never retired or stamped, whatever they hold."""
    SCAFFOLD = ('SCHEMA.md', 'index.md', 'log.md')

    def test_meta_pages_survive_retirement_and_stamping(self):
        text = self.card().read_text(encoding='utf-8')
        (self.wiki / 'card.md').unlink()
        wiki_root = self.wiki.parent
        for name in self.SCAFFOLD:
            (wiki_root / name).write_text(text, encoding='utf-8')
        self.assertEqual(self.run_retire(), ('retired', 1))
        for name in self.SCAFFOLD:
            page = wiki_root / name
            self.assertEqual(page.read_text(encoding='utf-8'), text, name)
            with patch.object(retire.wiki_selected_laya, 'recheck'):
                self.assertEqual(retire.retire_card(
                    self.root, wiki_root, self.selected, page, text, 'p' * 64), 'blocked')
                self.assertEqual(retire.record_source_revisions(
                    self.root, wiki_root, self.selected, page, text, []), 'blocked')
            self.assertEqual(page.read_text(encoding='utf-8'), text, name)
        self.assertFalse((self.root / '.auto-context/compile/retired-wiki').exists())


class NestedMetaNamedCards(RetireFixture):
    """Only the wiki-root meta pages are skipped; `concepts/SCHEMA.md` is a card."""
    NAMES = ('SCHEMA.md', 'index.md', 'log.md')

    def test_nested_meta_named_cards_retire_with_their_deleted_source(self):
        cards = [self.card(name) for name in self.NAMES]
        self.assertEqual(self.run_retire(), ('retired', 1))
        for card in cards:
            self.assertFalse(card.exists(), card.name)
        archived = list((self.root / '.auto-context/compile/retired-wiki').glob('*.md'))
        self.assertEqual(len(archived), len(self.NAMES))

    def test_retire_card_and_record_source_revisions_accept_nested_meta_names(self):
        wiki_root = self.wiki.parent
        for name in self.NAMES:
            card = self.card(name)
            text = card.read_text(encoding='utf-8')
            with patch.object(retire.wiki_selected_laya, 'recheck'):
                self.assertEqual(retire.retire_card(
                    self.root, wiki_root, self.selected, card, text, 'p' * 64), 'retired', name)
            self.assertFalse(card.exists(), name)
            legacy = self.wiki / name
            legacy.write_text(
                '---\ntitle: Legacy\nstatus: generated\ncreatedBy: qmd-auto-context\n'
                'sources:\n  - {kind: "file", path: "docs/still.md", collection: "raw"}\n'
                '---\n\n<!-- qmd:auto:start id="main" sourceHash="a" -->\nfact\n'
                '<!-- qmd:auto:end -->\n', encoding='utf-8')
            revisions = [{'kind': 'file', 'path': 'docs/still.md', 'collection': 'raw',
                          'sha256': hashlib.sha256(self.survivor.read_bytes()).hexdigest(),
                          'size': self.survivor.stat().st_size, 'mtimeNs': 1}]
            with patch.object(retire.wiki_selected_laya, 'recheck'):
                self.assertEqual(retire.record_source_revisions(
                    self.root, wiki_root, self.selected, legacy,
                    legacy.read_text(encoding='utf-8'), revisions), 'recorded', name)
            self.assertIn('sourceRevisions:', legacy.read_text(encoding='utf-8'))
            legacy.unlink()


class RenamedRawLabelRetirement(RetireFixture):
    """Cards citing a renamed raw collection (old label) after a minimal migration."""
    CONFIG = {'wikiPath': '.auto-context/wiki', 'indexing': True,
              'collections': ['raw', 'raw-wiki'],
              'collectionPaths': {'raw': 'docs', 'raw-wiki': '.auto-context/wiki'},
              'collectionRoles': {'raw': 'raw', 'raw-wiki': 'wiki'}}

    def test_old_label_card_is_retired_for_the_current_collection(self):
        card = self.card(label='raw-docs')
        self.assertEqual(self.run_retire(self.CONFIG), ('retired', 1))
        self.assertFalse(card.exists())

    def test_old_label_shared_card_reports_survivors_under_the_current_label(self):
        shared = self.card('shared.md', shared=True, label='raw-docs')
        result = self.run_retire(self.CONFIG, full=True)
        self.assertEqual(result, {'state': 'shared_pending',
                                  'survivors': [('raw', 'docs/still.md')]})
        self.assertTrue(shared.exists())

    def test_unknown_label_outside_raw_root_is_not_matched(self):
        (self.root / 'notes').mkdir()
        card = self.card(label='raw-docs', path='notes/gone.md')
        self.assertEqual(self.run_retire(self.CONFIG, rel='notes/gone.md')[0], 'retired')
        self.assertTrue(card.exists())


if __name__ == '__main__':
    unittest.main()

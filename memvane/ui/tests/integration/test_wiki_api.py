"""Wiki browser reads over a real selected install, its linked worktree and its registry rows.

The install is built and selected through the install use cases and registered
the way setup registers it (``registry.api.after_setup``); cards come from the
project fixture's card writer and the ledger rows from the pipeline's own
writers. Nothing here writes a registry row, a ledger row or a route by hand.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile

from memvane.registry import api as registry
from memvane.ui import wiki_adapters
from memvane.ui.read_adapters import TargetError
from memvane.ui.types import target_id
from tests.support.case import FixtureCase

AUTO = ('<!-- qmd:auto:start id="main" sourceHash="abc123" -->\n'
        '## Summary\nAuto text.\n<!-- qmd:auto:end -->\n')


def tree_digest(root):
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob('*')):
        if path.is_file() and not path.is_symlink():
            digest.update(str(path).encode() + path.read_bytes())
    return digest.hexdigest()


class WikiApiTests(FixtureCase):
    def setUp(self):
        super().setUp()
        from tests.support.install_builder import build_install
        from tests.support.project import make_project
        self.project = make_project(self.home, name='wikiproj', dirname='wikiproj', skip_paths=['private'])
        self.project.write_note('notes.md', 'line one\nline two\nline three\n')
        self.project.write_note('drift.md', 'original\n')
        self.project.write_note('private/secret.md', 'hidden source\n')
        self.project.write_card('concepts/alpha.md', title='Alpha', body='Alpha body.\n' + AUTO
                                + '\n## Notes\nManual tail.\n', sources=[self.project.root / 'notes.md'])
        self.project.write_card('concepts/beta.md', title='Beta', body='Beta body.\n', status='generated')
        self.project.write_card('concepts/drift.md', title='Drift', body='Drift.\n', sources=[self.project.root / 'drift.md'])
        self.project.write_card('concepts/secret.md', title='Secret', body='Hidden.\n',
                                sources=[self.project.root / 'private' / 'secret.md'])
        self.project.write_card('concepts/long.md', title='Long', body='word ' * 1200 + '\n',
                                status='generated')
        self.project.write_card('concepts/human.md', title='Human', body='No status.\n', status='draft')
        self.project.commit()
        self.worktree = self.project.linked_worktree('wt', 'feature')
        self.install = build_install(self, self.project, install_id='e' * 32)
        outcome = registry.after_setup('activate-index-origin', str(self.project.root), str(self.install.state),
                                       {'status': 'selected', 'phase': 'selected'})
        self.assertIn(outcome['status'], ('registered', 'unchanged'), outcome)
        self.id = self.install.install_id
        self.wt_id = target_id(self.id, str(self.worktree.root), 'worktree')
        # The drift card's source changes after the card recorded its revision.
        (self.project.root / 'drift.md').write_text('changed\n', encoding='utf-8')

    def list(self, target=None, **kwargs):
        return wiki_adapters.wiki_list(target or self.id, home=self.home, **kwargs)

    def rels(self, view):
        return [card['rel'] for card in view['cards']]

    def test_main_list_filters_and_ledger(self):
        before = tree_digest(self.project.root / '.memvane')
        view = self.list()
        self.assertEqual(tree_digest(self.project.root / '.memvane'), before)
        self.assertEqual((view['targetId'], view['ledgerFrom'], view['truncated']), (self.id, self.id, False))
        self.assertEqual(self.rels(view), ['concepts/alpha.md', 'concepts/beta.md', 'concepts/drift.md',
                                           'concepts/long.md', 'concepts/secret.md'])
        self.assertEqual(set(view['ledger']), {'compileJobs', 'verifyJobs', 'publishTxns', 'refreshEvents',
                                               'dedupPairs'})
        self.assertTrue(all(isinstance(count, int) for count in view['ledger'].values()))
        alpha = view['cards'][0]
        self.assertEqual(set(alpha), {'rel', 'title', 'status', 'tokens', 'overBudget', 'stale', 'staleReason',
                                      'hidden', 'updatedAt'})
        self.assertIsNone(alpha['staleReason'])
        self.assertEqual((alpha['title'], alpha['status'], alpha['stale'], alpha['hidden']),
                         ('Alpha', 'verified', False, False))
        expected = {
            'verified': ['concepts/alpha.md', 'concepts/drift.md', 'concepts/secret.md'],
            'generated': ['concepts/beta.md', 'concepts/long.md'],
            'pending': ['concepts/beta.md', 'concepts/long.md'],
            'stale': ['concepts/drift.md'],
            'hidden': ['concepts/secret.md'],
            'over_budget': ['concepts/long.md'],
        }
        for status, rels in expected.items():
            self.assertEqual(self.rels(self.list(status=status)), rels, status)
        self.assertEqual(self.rels(self.list(q='manual tail')), ['concepts/alpha.md'])
        limited = self.list(limit=2)
        self.assertEqual((len(limited['cards']), limited['truncated']), (2, True))
        for kwargs, code in (({'status': 'nope'}, 'status_invalid'), ({'limit': 0}, 'limit_invalid'),
                             ({'limit': 'x'}, 'limit_invalid'), ({'q': 'x' * 300}, 'q_invalid')):
            with self.assertRaises(TargetError) as caught:
                self.list(**kwargs)
            self.assertEqual((str(caught.exception), caught.exception.status), (code, 400))

    def test_stale_cards_name_a_deleted_or_a_changed_source(self):
        self.project.write_note('gone.md', 'about to go\n')
        self.project.write_card('concepts/gone.md', title='Gone', body='Gone.\n',
                                sources=[self.project.root / 'gone.md'])
        # Two sources: the changed one comes first, the deleted one still names the card.
        self.project.write_card('concepts/mixed.md', title='Mixed', body='Mixed.\n',
                                sources=[self.project.root / 'drift.md', self.project.root / 'gone.md'],
                                status='generated')
        (self.project.root / 'drift.md').write_text('original\n', encoding='utf-8')
        self.project.write_card('concepts/drift.md', title='Drift', body='Drift.\n',
                                sources=[self.project.root / 'drift.md'])
        (self.project.root / 'drift.md').write_text('changed again\n', encoding='utf-8')
        (self.project.root / 'gone.md').unlink()
        view = self.list(status='stale')
        self.assertEqual({card['rel']: (card['stale'], card['staleReason']) for card in view['cards']}, {
            'concepts/drift.md': (True, 'source_changed'),
            'concepts/gone.md': (True, 'source_deleted'),
            'concepts/mixed.md': (True, 'source_deleted'),
        })
        fresh = {card['rel']: (card['stale'], card['staleReason']) for card in self.list()['cards']
                 if card['rel'] in ('concepts/alpha.md', 'concepts/beta.md')}
        self.assertEqual(fresh, {'concepts/alpha.md': (False, None), 'concepts/beta.md': (False, None)})

    def test_worktree_lists_its_own_cards_and_reads_the_ledger_from_main(self):
        self.worktree.write_card('concepts/feature.md', title='Feature', body='Only here.\n')
        view = self.list(self.wt_id)
        self.assertEqual((view['targetId'], view['ledgerFrom']), (self.wt_id, self.id))
        self.assertIn('concepts/feature.md', self.rels(view))
        self.assertNotIn('concepts/feature.md', self.rels(self.list()))
        self.assertEqual(view['ledger'], self.list()['ledger'])

    def test_card_detail_splits_auto_and_manual_blocks(self):
        detail = wiki_adapters.wiki_card(self.id, 'concepts/alpha.md', home=self.home)
        front = detail['frontmatter']
        self.assertEqual((front['status'], front['createdBy']), ('verified', 'memvane'))
        self.assertEqual(list(front['sourceRevisions']), ['notes.md'])
        self.assertEqual(len(front['sourceRevisions']['notes.md']), 64)
        self.assertEqual((front['sourceSpans'], front['related'], front['aliases']), ([], [], []))
        self.assertEqual([block['kind'] for block in detail['blocks']], ['manual', 'auto', 'manual'])
        self.assertIn('Alpha body.', detail['blocks'][0]['markdown'])
        self.assertEqual(detail['blocks'][1]['markdown'], '## Summary\nAuto text.')
        self.assertIn('Manual tail.', detail['blocks'][2]['markdown'])
        self.assertNotIn('qmd:auto', ''.join(block['markdown'] for block in detail['blocks']))
        for rel, code, status in (('../x.md', 'rel_invalid', 400), ('/etc/passwd.md', 'rel_invalid', 400),
                                  ('concepts/alpha.txt', 'rel_invalid', 400),
                                  ('concepts/none.md', 'card_not_found', 404),
                                  ('concepts/human.md', 'card_status_unsupported', 422)):
            with self.assertRaises(TargetError) as caught:
                wiki_adapters.wiki_card(self.id, rel, home=self.home)
            self.assertEqual((str(caught.exception), caught.exception.status), (code, status), rel)

    def test_card_detail_resolves_related_and_refuses_a_symlinked_card(self):
        card = self.project.wiki / 'concepts' / 'gamma.md'
        card.write_text('---\ntitle: "Gamma"\nstatus: generated\ncreatedBy: memvane\naliases:\n  - "G"\n'
                        'related:\n  - "alpha"\n  - "missing-card"\n---\n# Gamma\n', encoding='utf-8')
        detail = wiki_adapters.wiki_card(self.id, 'concepts/gamma.md', home=self.home)
        self.assertEqual(detail['frontmatter']['related'], ['concepts/alpha.md', 'missing-card'])
        self.assertEqual(detail['frontmatter']['aliases'], ['G'])
        self.assertEqual(detail['blocks'], [{'kind': 'manual', 'markdown': '# Gamma'}])
        outside = Path(tempfile.mkdtemp(dir=self.home)) / 'outside.md'
        outside.write_text('---\nstatus: verified\n---\nleak\n', encoding='utf-8')
        os.symlink(outside, self.project.wiki / 'concepts' / 'link.md')
        with self.assertRaises(TargetError) as caught:
            wiki_adapters.wiki_card(self.id, 'concepts/link.md', home=self.home)
        self.assertEqual(str(caught.exception), 'path_escape_refused')
        self.assertNotIn('concepts/link.md', self.rels(self.list()))

    def test_source_reads_lines_and_highlights_inside_the_checkout(self):
        view = wiki_adapters.wiki_source(self.id, 'notes.md', highlights='2-3,9-12', home=self.home)
        self.assertEqual((view['targetId'], view['rel'], view['truncated']), (self.id, 'notes.md', False))
        self.assertEqual(view['lines'], ['line one', 'line two', 'line three'])
        self.assertEqual(view['highlights'], [{'startLine': 2, 'endLine': 3}])
        listed = wiki_adapters.wiki_source(self.id, 'notes.md', highlights=[{'startLine': 1, 'endLine': 1}],
                                           home=self.home)
        self.assertEqual(listed['highlights'], [{'startLine': 1, 'endLine': 1}])
        big = self.project.write_note('big.md', 'x' * 100 + '\n')
        big.write_bytes(b'y\n' * (wiki_adapters.SOURCE_LIMIT // 2 + 10))
        self.assertTrue(wiki_adapters.wiki_source(self.id, 'big.md', home=self.home)['truncated'])
        with self.assertRaises(TargetError) as caught:
            wiki_adapters.wiki_source(self.id, 'notes.md', highlights='3-1', home=self.home)
        self.assertEqual(str(caught.exception), 'highlights_invalid')

    def test_source_refuses_escapes_denied_skipped_and_binary_files(self):
        outside_dir = Path(tempfile.mkdtemp(dir=self.home))
        (outside_dir / 'leak.md').write_text('leak\n', encoding='utf-8')
        os.symlink(outside_dir / 'leak.md', self.project.root / 'link.md')
        os.symlink(outside_dir, self.project.root / 'linkdir')
        self.project.write_note('bin.dat', 'x')
        (self.project.root / 'bin.dat').write_bytes(b'abc\x00def')
        cases = (
            ('link.md', 'path_escape_refused', 403),
            ('linkdir/leak.md', 'path_escape_refused', 403),
            ('.memvane/settings.json', 'source_denied', 403),
            ('.git/HEAD', 'source_denied', 403),
            ('private/secret.md', 'source_skipped', 403),
            ('bin.dat', 'source_binary_refused', 400),
            ('missing.md', 'source_not_found', 404),
            ('../wikiproj/notes.md', 'rel_invalid', 400),
            (str(self.project.root / 'notes.md'), 'rel_invalid', 400),
            ('a\u2028b.md', 'rel_invalid', 400),
            ('concepts', 'not_a_regular_file', 400),
        )
        os.mkdir(self.project.root / 'concepts')
        for rel, code, status in cases:
            with self.assertRaises(TargetError) as caught:
                wiki_adapters.wiki_source(self.id, rel, home=self.home)
            self.assertEqual((str(caught.exception), caught.exception.status), (code, status), rel)

    def test_source_allows_a_system_symlink_above_the_target_root(self):
        # macOS ``/tmp`` -> ``/private/tmp``: an ancestor symlink above the proved root is not an escape.
        parent = Path(tempfile.mkdtemp(dir=self.home))
        alias = parent / 'alias'
        os.symlink(self.project.root.parent, alias)
        fd = wiki_adapters._open_below(str(alias / self.project.root.name), ['notes.md'])
        try:
            self.assertEqual(os.read(fd, 8), b'line one')
        finally:
            os.close(fd)
        if Path('/tmp').is_symlink():
            real = Path(tempfile.mkdtemp(dir='/tmp'))
            try:
                (real / 'f.md').write_text('ok\n', encoding='utf-8')
                fd = wiki_adapters._open_below(str(real), ['f.md'])
                os.close(fd)
            finally:
                (real / 'f.md').unlink()
                real.rmdir()

    def test_worktree_source_reads_its_own_checkout(self):
        self.worktree.write_note('wt-only.md', 'worktree text\n')
        view = wiki_adapters.wiki_source(self.wt_id, 'wt-only.md', home=self.home)
        self.assertEqual(view['lines'], ['worktree text'])
        with self.assertRaises(TargetError) as caught:
            wiki_adapters.wiki_source(self.id, 'wt-only.md', home=self.home)
        self.assertEqual(str(caught.exception), 'source_not_found')
        with self.assertRaises(TargetError) as caught:
            wiki_adapters.wiki_source(self.wt_id, 'private/secret.md', home=self.home)
        self.assertEqual(str(caught.exception), 'source_skipped')

    def test_collection_prefixed_card_rel_resolves_and_case_variants_are_denied(self):
        from memvane.ui import read_adapters
        collection = read_adapters.resolve_target(self.id, home=self.home)['wikiCollections'][0]
        view = wiki_adapters.wiki_card(self.id, f'{collection}/concepts/alpha.md', home=self.home)
        self.assertEqual(view['rel'], 'concepts/alpha.md')
        with self.assertRaises(TargetError) as caught:
            wiki_adapters.wiki_card(self.id, 'other-collection/concepts/alpha.md', home=self.home)
        self.assertEqual((str(caught.exception), caught.exception.status), ('card_not_found', 404))
        for denied_rel in ('.MemVane/settings.json', '.GIT/config'):
            with self.assertRaises(TargetError) as caught:
                wiki_adapters.wiki_source(self.id, denied_rel, home=self.home)
            self.assertEqual((str(caught.exception), caught.exception.status), ('source_denied', 403), denied_rel)
        with self.assertRaises(TargetError) as caught:
            wiki_adapters.wiki_source(self.id, 'Private/secret.md', home=self.home)
        self.assertEqual((str(caught.exception), caught.exception.status), ('source_skipped', 403))

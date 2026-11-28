"""A branch tip that never touched a deleted source does not keep its cards (D1-D9).

Each case is a real git history in a fixture repository; cards are written by
the fixture's ``sourceRevisions`` dumper (``wiki_card.dump_source_revisions``).
State table: ``docs/dev/progress-wiki-stale-retire.md`` (lane A).
"""
import hashlib
import json
from unittest.mock import patch

from memvane.diagnostics import api as diagnostics
from memvane.wiki.lifecycle import git_branch_safety as gbs
from memvane.wiki.lifecycle import wiki_lifecycle as lifecycle
from memvane.wiki.lifecycle import wiki_source_retire as retire
from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase
from tests.support.project import make_project
from tests.support.settings_fake import expanded, use_config

SOURCE = 'docs/gone.md'


class StaleBranchTips(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home)
        self.root = self.project.root
        self.project.git('add', '--', '.memvane/settings.json')
        self.project.git('commit', '-qm', 'initial')
        use_config(self, self.project, expanded(self.project))

    # -- fixture history ---------------------------------------------------------
    def commit(self, message, *paths):
        self.project.git('add', '--', *paths)
        self.project.git('commit', '-qm', message)

    def commit_source(self, rel=SOURCE, text='source before deletion\n'):
        source = self.project.write_note(rel, text)
        self.commit('add ' + rel, rel)
        return source

    def branch(self, name, *, source_text=None):
        """An unmerged local branch from HEAD; ``source_text`` rewrites the source on it."""
        self.project.git('checkout', '-qb', name)
        paths = []
        if source_text is not None:
            self.project.write_note(SOURCE, source_text)
            paths.append(SOURCE)
        marker = 'notes/' + name + '.txt'
        self.project.write_note(marker, name + '\n')
        self.commit('work on ' + name, marker, *paths)
        self.project.git('checkout', '-q', 'main')

    def snapshot_tips(self, names, fork):
        """One unmerged branch per name, each a distinct empty commit on ``fork``."""
        tree = self.project.git('rev-parse', fork + '^{tree}').strip()
        for name in names:
            oid = self.project.git('commit-tree', tree, '-p', fork, '-m', 'tip ' + name).strip()
            self.project.git('update-ref', 'refs/heads/' + name, oid)

    def delete_source(self, rel=SOURCE):
        self.project.git('rm', '-q', '--', rel)
        self.project.git('commit', '-qm', 'delete ' + rel)

    def create_card(self, name, *sources):
        return self.project.write_card(
            'concepts/' + name, title='Card ' + name,
            body='<!-- qmd:auto:start id="main" sourceHash="a" -->\nfact\n<!-- qmd:auto:end -->\n',
            sources=sources, status='generated', created_by='qmd-auto-context')

    def commit_card(self, card):
        self.commit('card ' + card.name, card.relative_to(self.root).as_posix())

    # -- runs --------------------------------------------------------------------
    def bound(self, journal=None):
        record = {'projectRoot': str(self.root), 'installId': 'a' * 32,
                  'scopeId': 'b' * 32, 'storeIncarnation': 'c' * 32,
                  'recallSettings': {'skipPaths': []},
                  'collectionRoots': {self.project.raw_name: str(self.root),
                                      self.project.wiki_name: str(self.project.wiki)},
                  'collections': [{'name': self.project.raw_name, 'role': 'raw', 'sourceRoot': str(self.root)},
                                  {'name': self.project.wiki_name, 'role': 'wiki',
                                   'sourceRoot': str(self.project.wiki)}]}
        if journal is not None:
            record['journalPath'] = str(journal / 'journal.sqlite')
        return record, {}

    def run_selected(self):
        with patch.object(workspace, 'selected', return_value=self.bound()), \
             patch.object(workspace, 'recheck'), \
             patch.object(workspace, 'request_sync', return_value=True), \
             patch.object(workspace, 'request_compile'):
            return lifecycle.run(str(self.root))

    def retire_source(self, digest, bound=None, rel=SOURCE):
        with patch.object(workspace, 'recheck'), \
             patch.object(workspace, 'request_sync', return_value=True):
            return retire.retire_deleted_source(self.root, expanded(self.project),
                                                bound or self.bound(), self.project.raw_name,
                                                rel, digest)

    def archives(self):
        return sorted((self.root / '.memvane').rglob('retired-wiki/*.md'))

    def state_root(self):
        state = self.home / 'state'
        state.mkdir(mode=0o700)
        return state

    def wiki_events(self, state, outcome):
        events = state / 'events.jsonl'
        if not events.exists():
            return []
        rows = [json.loads(line) for line in events.read_text(encoding='utf-8').splitlines() if line]
        return [row for row in rows if row.get('event') == 'wiki' and row.get('outcome') == outcome]

    # -- table cells -------------------------------------------------------------
    def test_d1_deletion_after_card_commit_retires(self):
        source = self.commit_source()
        card = self.create_card('gone.md', source)
        self.commit_card(card)
        self.delete_source()
        result = self.run_selected()
        self.assertFalse(card.exists())
        self.assertEqual((result['retired'], result['held']), (1, 0))
        self.assertEqual(len(self.archives()), 1)

    def d2_history(self):
        source = self.commit_source()
        self.branch('old')
        card = self.create_card('gone.md', source)
        self.delete_source()
        self.commit_card(card)
        return card

    def test_d2_stale_snapshot_tip_no_longer_holds_committed_card(self):
        card = self.d2_history()
        judge = gbs.LineageJudge.open(self.root)
        self.assertEqual(judge.judge(SOURCE, card), (gbs.RETIRE, 'deleted_before_card_commit'))
        result = self.run_selected()
        self.assertFalse(card.exists())
        self.assertEqual((result['retired'], result['held']), (1, 0))
        self.assertEqual(len(self.archives()), 1)
        # The old branch keeps its own snapshot of the source.
        self.assertEqual(self.project.git('show', 'old:' + SOURCE), 'source before deletion\n')

    def test_d2_dry_run_counts_the_card_as_deletable(self):
        card = self.d2_history()
        before = card.read_bytes()
        report = lifecycle.dry_run(str(self.root))
        self.assertEqual(report['counts']['missing'], 1)
        self.assertEqual(report['held'], 0)
        self.assertEqual(report['guard']['wouldDelete'], 1)
        self.assertEqual(card.read_bytes(), before)

    def test_d3_tip_that_edited_the_source_still_holds(self):
        source = self.commit_source()
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        self.branch('old', source_text='edited on the old branch\n')
        card = self.create_card('gone.md', source)
        self.delete_source()
        self.commit_card(card)
        result = self.run_selected()
        self.assertTrue(card.exists())
        self.assertEqual((result['retired'], result['held']), (0, 1))
        outcome = self.retire_source(digest)
        self.assertEqual((outcome['state'], outcome['reason']), ('held_branch', 'source_on_other_branch'))
        self.assertTrue(card.exists())
        self.assertEqual(self.archives(), [])

    def test_d4_tip_that_re_added_the_path_after_deletion_still_holds(self):
        source = self.commit_source()
        card = self.create_card('gone.md', source)
        self.delete_source()
        self.project.git('checkout', '-qb', 'readd')
        self.project.write_note(SOURCE, 'source before deletion\n')
        self.commit('re-add', SOURCE)
        self.project.git('checkout', '-q', 'main')
        self.commit_card(card)
        result = self.run_selected()
        self.assertTrue(card.exists())
        self.assertEqual((result['retired'], result['held']), (0, 1))
        report = lifecycle.dry_run(str(self.root))
        self.assertEqual(report['heldReasons'], {'source_on_other_branch': 1})

    def test_d5_working_tree_only_deletion_still_holds(self):
        source = self.commit_source()
        card = self.create_card('gone.md', source)
        self.commit_card(card)
        self.branch('old')
        source.unlink()
        report = lifecycle.dry_run(str(self.root))
        self.assertEqual(report['heldReasons'], {'source_tracked_in_head': 1})
        result = self.run_selected()
        self.assertTrue(card.exists())
        self.assertEqual((result['retired'], result['held']), (0, 1))

    def test_d6_bundle_with_stale_tip_retires_every_card(self):
        source = self.commit_source()
        self.branch('old')
        before = self.create_card('gone-a.md', source)
        self.commit_card(before)
        after = self.create_card('gone-b.md', source)
        self.delete_source()
        self.commit_card(after)
        result = self.run_selected()
        self.assertFalse(before.exists())
        self.assertFalse(after.exists())
        self.assertEqual((result['retired'], result['held']), (2, 0))
        self.assertEqual(len(self.archives()), 2)

    def test_d7_held_bundle_logs_once_per_change_of_blocking_cards(self):
        source = self.commit_source()
        self.branch('old', source_text='edited on the old branch\n')
        early = [self.create_card(f'gone-{index}.md', source) for index in range(2)]
        for card in early:
            self.commit_card(card)
        blocker = self.create_card('gone-late.md', source)
        second = self.create_card('gone-later.md', source)
        second_text = second.read_text(encoding='utf-8')
        second.unlink()  # cites the same source; joins the bundle later
        self.delete_source()
        self.commit_card(blocker)
        state = self.state_root()

        def run():
            with patch.object(workspace, 'selected', return_value=self.bound(state)), \
                 patch.object(workspace, 'recheck'), \
                 patch.object(workspace, 'request_sync', return_value=True), \
                 patch.object(workspace, 'request_compile'):
                return lifecycle.run(str(self.root))
        # Two retirable cards reach the held bundle in one run; a second run repeats it.
        for _ in range(2):
            self.assertEqual(run()['held'], 3)
        self.assertEqual([row['path'] for row in self.wiki_events(state, 'held')],
                         ['concepts/gone-late.md'])
        # A second blocking card changes the set: both are logged once.
        second.write_text(second_text, encoding='utf-8')
        self.commit_card(second)
        for _ in range(2):
            self.assertEqual(run()['held'], 4)
        self.assertEqual(sorted(row['path'] for row in self.wiki_events(state, 'held')),
                         ['concepts/gone-late.md', 'concepts/gone-late.md', 'concepts/gone-later.md'])
        # The edited branch goes away: every card retires and nothing more is logged.
        # Live cards keep four retirements under the mass-deletion guard.
        live = self.project.write_note('docs/live.md', 'live\n')
        for index in range(13):
            self.create_card(f'live-{index}.md', live)
        self.project.git('branch', '-D', 'old')
        result = run()
        self.assertEqual((result['retired'], result['held']), (4, 0))
        self.assertEqual(len(self.wiki_events(state, 'held')), 3)
        self.assertEqual(len(self.wiki_events(state, 'retired')), 4)

    def test_d7_bundle_with_edited_tip_holds_and_names_the_blocking_card(self):
        source = self.commit_source()
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        self.branch('old', source_text='edited on the old branch\n')
        before = self.create_card('gone-a.md', source)
        self.commit_card(before)
        after = self.create_card('gone-b.md', source)
        self.delete_source()
        self.commit_card(after)
        result = self.run_selected()
        self.assertTrue(before.exists())
        self.assertTrue(after.exists())
        self.assertEqual((result['retired'], result['held']), (0, 2))
        state = self.state_root()
        outcome = self.retire_source(digest, self.bound(state))
        self.assertEqual((outcome['state'], outcome['reason']), ('held_branch', 'source_on_other_branch'))
        held = self.wiki_events(state, 'held')
        self.assertEqual([(row['path'], row['reason']) for row in held],
                         [('concepts/gone-b.md', 'source_on_other_branch')])
        self.assertEqual(self.archives(), [])
        self.assertNotIn('gone-b', diagnostics.wiki_tail(state))

    def test_d8_shared_card_with_stale_tip_goes_to_shared_pending(self):
        source = self.commit_source()
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        other = self.commit_source('docs/kept.md', 'live source\n')
        self.branch('old')
        card = self.create_card('shared.md', source, other)
        self.delete_source()
        self.commit_card(card)
        outcome = self.retire_source(digest)
        self.assertEqual(outcome['state'], 'shared_pending')
        self.assertEqual([path for _, path in outcome['survivors']], ['docs/kept.md'])
        self.assertTrue(card.exists())
        self.assertEqual(self.archives(), [])

    def test_d9_uncommitted_card_with_stale_tip_retires(self):
        source = self.commit_source()
        self.branch('old')
        card = self.create_card('gone.md', source)
        self.delete_source()
        result = self.run_selected()
        self.assertFalse(card.exists())
        self.assertEqual((result['retired'], result['held']), (1, 0))
        self.assertEqual(len(self.archives()), 1)

    # -- other tip shapes ---------------------------------------------------------
    def test_d13_stale_tip_checked_out_in_another_worktree_holds(self):
        card = self.d2_history()
        other = self.root.with_name(self.root.name + '-old')
        self.project.git('worktree', 'add', '-q', str(other), 'old')
        # That checkout may hold uncommitted edits of the source.
        (other / SOURCE).write_text('uncommitted edit in the other worktree\n', encoding='utf-8')
        report = lifecycle.dry_run(str(self.root))
        self.assertEqual((report['held'], report['guard']['wouldDelete']), (1, 0))
        self.assertEqual(report['heldReasons'], {'source_on_other_branch': 1})
        self.project.git('worktree', 'remove', '--force', str(other))
        result = self.run_selected()
        self.assertFalse(card.exists())
        self.assertEqual((result['retired'], result['held']), (1, 0))

    def criss_cross(self, *, edit_side=False, touch_and_revert=False):
        """main and ``tip`` merged each other's first commits: two merge bases."""
        source = self.commit_source()
        for name in ('side-a', 'side-b'):
            self.project.git('checkout', '-qb', name, 'main')
            paths = ['notes/' + name + '.txt']
            self.project.write_note(paths[0], name + '\n')
            if edit_side and name == 'side-b':
                self.project.write_note(SOURCE, 'edited on side b\n')
                paths.append(SOURCE)
            self.commit(name, *paths)
        self.project.git('checkout', '-qb', 'tip', 'side-b')
        self.project.git('merge', '-q', '--no-ff', '--no-edit', 'side-a')
        self.project.git('checkout', '-q', 'main')
        self.project.git('merge', '-q', '--no-ff', '--no-edit', 'side-a')
        self.project.git('merge', '-q', '--no-ff', '--no-edit', 'side-b')
        self.project.git('branch', '-D', 'side-a', 'side-b')
        if touch_and_revert:
            self.project.git('checkout', '-q', 'tip')
            text = (self.root / SOURCE).read_text(encoding='utf-8')
            self.project.write_note(SOURCE, 'temporary\n')
            self.commit('touch', SOURCE)
            self.project.write_note(SOURCE, text)
            self.commit('revert', SOURCE)
            self.project.git('checkout', '-q', 'main')
        bases = self.project.git('merge-base', '--all', 'tip', 'main').split()
        self.assertEqual(len(bases), 2)
        card = self.create_card('gone.md', source)
        self.delete_source()
        self.commit_card(card)
        return card

    def test_criss_cross_tip_that_restored_the_source_retires(self):
        card = self.criss_cross(touch_and_revert=True)
        judge = gbs.LineageJudge.open(self.root)
        self.assertEqual(judge.judge(SOURCE, card), (gbs.RETIRE, 'deleted_before_card_commit'))
        self.assertEqual(len(judge._bases), 1)  # the precise per-tip path ran
        result = self.run_selected()
        self.assertEqual((result['retired'], result['held']), (1, 0))

    def test_criss_cross_tip_matching_only_one_merge_base_holds(self):
        card = self.criss_cross(edit_side=True)
        result = self.run_selected()
        self.assertTrue(card.exists())
        self.assertEqual((result['retired'], result['held']), (0, 1))

    def test_tip_that_only_changed_the_mode_holds(self):
        source = self.commit_source()
        self.project.git('checkout', '-qb', 'old')
        source.chmod(0o755)
        self.project.git('update-index', '--chmod=+x', '--', SOURCE)
        self.project.git('commit', '-qm', 'mode only')
        self.project.git('checkout', '-q', 'main')
        self.assertEqual(self.project.git('diff', '--name-only', 'main', 'old').split(), [SOURCE])
        card = self.create_card('gone.md', source)
        self.delete_source()
        self.commit_card(card)
        result = self.run_selected()
        self.assertTrue(card.exists())
        self.assertEqual((result['retired'], result['held']), (0, 1))

    # -- bounds ------------------------------------------------------------------
    def test_stale_tips_are_proved_with_one_log_call_per_run(self):
        source = self.commit_source()
        self.branch('old-a')
        self.project.write_note('notes/main.txt', 'main\n')
        self.commit('main work', 'notes/main.txt')
        self.branch('old-b')
        cards = [self.create_card(f'gone-{index}.md', source) for index in range(3)]
        self.delete_source()
        for card in cards:
            self.commit_card(card)
        judge = gbs.LineageJudge.open(self.root)
        judge.prefetch({SOURCE})
        calls = []
        real = gbs.run_git

        def counting(args, *rest, **kwargs):
            calls.append(tuple(args))
            return real(args, *rest, **kwargs)
        with patch.object(gbs, 'run_git', side_effect=counting):
            verdicts = [judge.judge(SOURCE, card) for card in cards]
        self.assertEqual(verdicts, [(gbs.RETIRE, 'deleted_before_card_commit')] * 3)
        self.assertEqual(sum(1 for args in calls if args[:2] == ('log', '--full-history')), 1)
        self.assertEqual(sum(1 for args in calls if args[:2] == ('worktree', 'list')), 1)
        self.assertEqual(sum(1 for args in calls if args[0] == 'for-each-ref'), 1)
        self.assertEqual(sum(1 for args in calls if args[:2] == ('merge-base', '--all')), 0)
        self.assertEqual(sum(1 for args in calls if args[:3] == ('ls-tree', '-r', '-z')), 0)

    def test_branch_tip_bound_is_exact_at_max_branch_tips(self):
        source = self.commit_source()
        fork = self.project.git('rev-parse', 'HEAD').strip()
        self.snapshot_tips([f'snap-{index:03d}' for index in range(gbs.MAX_BRANCH_TIPS)], fork)
        card = self.create_card('gone.md', source)
        self.delete_source()
        self.commit_card(card)
        report = lifecycle.dry_run(str(self.root))
        self.assertEqual((report['held'], report['guard']['wouldDelete']), (0, 1))
        self.snapshot_tips(['snap-extra'], fork)  # one more unmerged tip than the bound
        report = lifecycle.dry_run(str(self.root))
        self.assertEqual((report['held'], report['guard']['wouldDelete']), (1, 0))
        self.assertEqual(report['heldReasons'], {'source_on_other_branch': 1})

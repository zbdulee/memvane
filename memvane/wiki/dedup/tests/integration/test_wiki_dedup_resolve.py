"""Resolving one retroactive dedup pair: merge deletes after a durable record, skip suppresses.

``wiki-dedup-resolve`` is an entry command, so one test crosses the process
boundary; the rest call ``wiki_dedup_resolve.main`` in process.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from memvane.wiki.dedup import wiki_dedup_resolve as resolve
from memvane.wiki.dedup import wiki_dedup_scan as scan
from tests.support.case import FixtureCase
from tests.support.project import make_project

REPO = Path(__file__).resolve().parents[5]


def jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


class ResolveCase(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)
        self.root = self.project.root
        self.wiki = self.project.wiki
        self.compile = self.root / '.memvane' / 'compile'
        self.compile.mkdir(parents=True, exist_ok=True)
        self.queue = self.compile / 'dedup-needed.jsonl'

    def page(self, rel, text):
        path = self.wiki / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def enqueue(self, *entries):
        self.queue.write_text(''.join(json.dumps(entry) + '\n' for entry in entries), encoding='utf-8')

    def run_main(self, *argv):
        out = io.StringIO()
        with mock.patch.object(sys, 'argv', ['wiki_dedup_resolve.py', '--cwd', str(self.root), *argv]), \
                contextlib.redirect_stdout(out):
            code = resolve.main()
        return code, json.loads(out.getvalue())

    def merge(self, index, delete):
        return self.run_main('--index', str(index), '--action', 'merge', '--delete', delete)

    def skip(self, index):
        return self.run_main('--index', str(index), '--action', 'skip')


class Merge(ResolveCase):
    def test_merge_deletes_the_named_loser_after_logging_its_full_content(self):
        loser = self.page('entities/loser.md', '---\ntitle: Loser\n---\n\nLoser content.\n')
        winner = self.page('entities/winner.md', '---\ntitle: Winner\n---\n\nWinner content.\n')
        self.enqueue({'pageA': 'entities/loser.md', 'pageB': 'entities/winner.md', 'score': 0.95})
        code, out = self.merge(0, 'entities/loser.md')
        self.assertEqual((code, out), (0, {'action': 'deleted', 'deletedPath': 'entities/loser.md'}))
        self.assertFalse(loser.exists())
        self.assertTrue(winner.exists())
        deleted = jsonl(self.compile / 'dedup-deleted.jsonl')
        self.assertEqual(len(deleted), 1)
        self.assertEqual({key: deleted[0][key] for key in ('deletedPath', 'pairedWith', 'score')},
                         {'deletedPath': 'entities/loser.md', 'pairedWith': 'entities/winner.md',
                          'score': 0.95})
        self.assertIn('Loser content.', deleted[0]['content'])
        self.assertTrue(deleted[0]['resolvedAt'])
        self.assertEqual(jsonl(self.queue), [])
        # No selected route: no recovery copy, nothing to wake.
        self.assertFalse((self.compile / 'dedup-recovery').exists())

    def test_rejections_keep_both_pages_and_restore_the_queue(self):
        self.page('entities/a.md', '---\ntitle: A\n---\n\nA content.\n')
        self.page('entities/b.md', '---\ntitle: B\n---\n\nB content.\n')
        table = [
            ({'pageA': 'entities/a.md', 'pageB': 'entities/b.md', 'score': 0.95},
             'entities/not-in-entry.md', 'delete_not_in_entry'),
            ({'pageA': 'entities/a.md', 'pageB': '../../etc/passwd', 'score': 0.95},
             '../../etc/passwd', 'unsafe_delete_path'),
        ]
        for entry, delete, reason in table:
            with self.subTest(reason=reason):
                self.enqueue(entry)
                code, out = self.merge(0, delete)
                self.assertEqual((code, out), (1, {'action': 'rejected', 'reason': reason}))
                self.assertTrue((self.wiki / 'entities/a.md').exists())
                self.assertTrue((self.wiki / 'entities/b.md').exists())
                self.assertEqual(jsonl(self.queue), [entry], 'a rejected resolution stays queued')
        self.assertEqual(jsonl(self.compile / 'dedup-deleted.jsonl'), [])

    def test_cli_rejections_before_a_claim(self):
        self.page('entities/a.md', 'a')
        entry = {'pageA': 'entities/a.md', 'pageB': 'entities/b.md'}
        self.enqueue(entry)
        self.assertEqual(self.run_main('--index', '0', '--action', 'merge'),
                         (1, {'action': 'rejected', 'reason': 'missing_delete_arg'}))
        self.assertEqual(self.merge(3, 'entities/a.md'),
                         (1, {'action': 'rejected', 'reason': 'index_out_of_range'}))
        self.assertEqual(jsonl(self.queue), [entry])
        self.queue.write_text('not json\n' + json.dumps(entry) + '\n', encoding='utf-8')
        self.assertEqual(self.skip(0), (1, {'action': 'rejected', 'reason': 'malformed_entry'}))
        self.assertEqual(jsonl(self.queue), [entry], 'only the malformed row is dropped')
        self.queue.write_text('', encoding='utf-8')
        self.assertEqual(self.skip(0), (1, {'action': 'rejected', 'reason': 'index_out_of_range'}))
        self.queue.unlink()
        self.assertEqual(self.skip(0), (1, {'action': 'rejected', 'reason': 'queue_empty'}))

    def test_an_already_missing_loser_degrades_to_an_unrecorded_skip(self):
        self.page('entities/winner.md', '---\ntitle: Winner\n---\n\nWinner content.\n')
        self.enqueue({'pageA': 'entities/already-gone.md', 'pageB': 'entities/winner.md', 'score': 0.95})
        self.assertEqual(self.merge(0, 'entities/already-gone.md'),
                         (0, {'action': 'skipped', 'reason': 'stale_target'}))
        self.assertEqual(jsonl(self.queue), [])
        self.assertEqual(jsonl(self.compile / 'dedup-skipped.jsonl'), [])

    def test_a_crash_mid_resolve_leaves_the_queue_exactly_as_it_was(self):
        self.page('entities/loser.md', 'loser')
        self.page('entities/winner.md', 'winner')
        self.enqueue({'pageA': 'entities/loser.md', 'pageB': 'entities/winner.md', 'score': 0.95})
        before = self.queue.read_text(encoding='utf-8')
        entities = self.wiki / 'entities'
        os.chmod(entities, 0o555)  # unlink raises while the file is present
        try:
            with self.assertRaises(PermissionError):
                self.merge(0, 'entities/loser.md')
        finally:
            os.chmod(entities, 0o755)
        self.assertEqual(self.queue.read_text(encoding='utf-8'), before)
        self.assertTrue((entities / 'loser.md').exists())
        self.assertEqual(list(self.compile.glob('dedup-needed.jsonl.claimed.*')), [])


class Skip(ResolveCase):
    def test_skip_removes_the_entry_and_changes_no_page(self):
        self.page('entities/a.md', '---\ntitle: A\n---\n\nA content.\n')
        self.page('entities/b.md', '---\ntitle: B\n---\n\nB content.\n')
        self.enqueue({'pageA': 'entities/a.md', 'pageB': 'entities/b.md', 'score': 0.91})
        code, out = self.skip(0)
        self.assertEqual((code, out['action'], out['recorded']), (0, 'skipped', True))
        self.assertTrue((self.wiki / 'entities/a.md').exists())
        self.assertTrue((self.wiki / 'entities/b.md').exists())
        self.assertEqual(jsonl(self.compile / 'dedup-deleted.jsonl'), [])
        self.assertEqual(jsonl(self.queue), [])

    def test_entries_around_the_resolved_index_keep_their_order(self):
        for name in ('p1', 'p2', 'p3', 'p4', 'p5', 'p6'):
            self.page(f'entities/{name}.md', name)
        entries = [{'pageA': 'entities/p1.md', 'pageB': 'entities/p2.md', 'score': 0.9},
                   {'pageA': 'entities/p3.md', 'pageB': 'entities/p4.md', 'score': 0.91},
                   {'pageA': 'entities/p5.md', 'pageB': 'entities/p6.md', 'score': 0.92}]
        self.enqueue(*entries)
        self.skip(1)
        self.assertEqual(jsonl(self.queue), [entries[0], entries[2]])

    def test_skip_on_an_intact_pair_records_one_sorted_hashed_row(self):
        a = self.page('entities/a.md', '---\ntitle: A\n---\n\nA content.\n')
        b = self.page('entities/b.md', '---\ntitle: B\n---\n\nB content.\n')
        self.enqueue({'pageA': 'entities/b.md', 'pageB': 'entities/a.md', 'score': 0.91})
        self.assertEqual(self.skip(0)[1]['recorded'], True)
        skipped = jsonl(self.compile / 'dedup-skipped.jsonl')
        self.assertEqual(len(skipped), 1)
        row = skipped[0]
        self.assertEqual((row['pageA'], row['pageB']), ('entities/a.md', 'entities/b.md'))
        # The CLI hashes with the scanner's own function: both sides of suppression agree.
        self.assertEqual((row['pageAHash'], row['pageBHash']),
                         (scan.body_hash(a.read_text(encoding='utf-8')),
                          scan.body_hash(b.read_text(encoding='utf-8'))))
        self.assertTrue(row['skippedAt'])
        self.assertEqual(scan.load_skip_suppressions(self.compile / 'dedup-skipped.jsonl'),
                         {('entities/a.md', 'entities/b.md'): (row['pageAHash'], row['pageBHash'])})

    def test_a_stale_skip_records_nothing(self):
        self.page('entities/b.md', '---\ntitle: B\n---\n\nB content.\n')
        table = [{'pageA': 'entities/gone.md', 'pageB': 'entities/b.md'},
                 {'pageA': 'entities/b.md', 'pageB': 'entities/b.md'},
                 {'pageA': '../outside.md', 'pageB': 'entities/b.md'}]
        for entry in table:
            with self.subTest(entry=entry):
                self.enqueue(entry)
                self.assertEqual(self.skip(0), (0, {'action': 'skipped', 'recorded': False}))
                self.assertEqual(jsonl(self.queue), [])
        self.assertEqual(jsonl(self.compile / 'dedup-skipped.jsonl'), [])

    def test_an_unwritable_skip_ledger_is_reported_not_raised(self):
        self.page('entities/a.md', 'a')
        self.page('entities/b.md', 'b')
        (self.compile / 'dedup-skipped.jsonl').mkdir()
        self.enqueue({'pageA': 'entities/a.md', 'pageB': 'entities/b.md'})
        self.assertEqual(self.skip(0), (0, {'action': 'skipped', 'recorded': False}))


class KeeperBudget(ResolveCase):
    """A merge into a keeper the local selector cannot score keeps both cards."""

    def setUp(self):
        super().setUp()
        self.loser = self.page('entities/loser.md',
                               '---\ntitle: Loser Card\nstatus: generated\ncreatedBy: memvane\n---\n\nloser fact\n')
        self.keeper = self.page('entities/winner.md',
                                '---\ntitle: Winner Card\nstatus: generated\ncreatedBy: memvane\n---\n\n'
                                'winner fact\nloser fact\n')
        self.entry = {'pageA': 'entities/loser.md', 'pageB': 'entities/winner.md', 'score': 0.95}

    def resolve_entry(self, bound=None):
        with mock.patch('memvane.wiki.workspace.api.recheck'), \
                mock.patch('memvane.wiki.workspace.api.request_sync', return_value=True):
            return resolve.resolve_entry(self.root, self.wiki, self.compile, self.entry, 'merge',
                                         'entities/loser.md', laya_bound=bound)

    def state_root(self):
        state = self.home / 'state'
        state.mkdir(mode=0o700)
        return state

    def test_merge_into_an_over_budget_keeper_keeps_both_cards(self):
        state = self.state_root()
        bound = ({'journalPath': str(state / 'journal.sqlite')}, {})
        measured = {'fits': False, 'size': 900, 'budget': 512, 'method': 'tokenizer', 'reason': 'over_budget'}
        with mock.patch('memvane.learning.api.measure_card', return_value=measured) as measure:
            out = self.resolve_entry(bound)
        self.assertEqual(measure.call_args.args[0], self.keeper.read_text(encoding='utf-8'),
                         'the keeper is measured, not the loser')
        self.assertEqual(out, {'action': 'kept_separate', 'reason': 'keeper_over_budget',
                               'keeper': 'entities/winner.md', 'kept': 'entities/loser.md',
                               'recorded': True, 'size': 900, 'budget': 512, 'method': 'tokenizer'})
        self.assertTrue(self.loser.exists())
        self.assertEqual(len(jsonl(self.compile / 'dedup-skipped.jsonl')), 1)
        self.assertEqual(jsonl(self.compile / 'dedup-deleted.jsonl'), [])
        self.assertFalse((self.compile / 'dedup-recovery').exists())
        events = [e for e in jsonl(state / 'events.jsonl') if e.get('event') == 'wiki.card']
        self.assertEqual([(e['outcome'], e['path'], e['kept']) for e in events],
                         [('merge_kept_separate', 'entities/winner.md', 'entities/loser.md')])

    def test_a_keeper_that_fits_or_cannot_be_measured_still_merges(self):
        table = [
            ('within budget', {'fits': True, 'size': 10, 'budget': 512, 'method': 'tokenizer',
                               'reason': None}),
            ('no tokenizer', None),  # the real measurement on a machine without a selector
        ]
        for label, measured in table:
            with self.subTest(label=label):
                self.loser.write_text('---\ntitle: Loser Card\n---\n\nloser fact\n', encoding='utf-8')
                patcher = (mock.patch('memvane.learning.api.measure_card', return_value=measured)
                           if measured is not None else contextlib.nullcontext())
                with patcher:
                    self.assertEqual(self.resolve_entry()['action'], 'deleted')
                self.assertFalse(self.loser.exists())

    def test_a_keeper_without_frontmatter_is_not_a_budget_case(self):
        self.keeper.write_text('winner\n', encoding='utf-8')
        self.assertEqual(self.resolve_entry()['action'], 'deleted')
        self.assertFalse(self.loser.exists())


class SelectedRoute(ResolveCase):
    """A selected route keeps a recovery copy and wakes sync after the locks are released."""

    def test_a_failed_wake_requeues_and_a_stale_retry_wakes_then_acknowledges(self):
        winner = self.page('entities/winner.md', '---\ntitle: Winner\n---\n\nWinner.\n')
        loser = self.page('entities/loser.md', '---\ntitle: Loser\n---\n\nLoser.\n')
        self.enqueue({'pageA': 'entities/winner.md', 'pageB': 'entities/loser.md'})
        bound = ({'collections': [{'role': 'raw', 'sourceRoot': str(self.root)},
                                  {'role': 'wiki', 'sourceRoot': str(self.wiki.resolve())}]},
                 {'proof': 'synthetic'})
        from memvane.wiki.workspace import api as workspace
        real_card, real_mutation = workspace.card_write_lock, workspace.mutation_lock
        held = {'card': 0, 'wiki': 0}
        wakes = []

        @contextlib.contextmanager
        def card_lock(project, **kwargs):
            with real_card(project, **kwargs):
                held['card'] += 1
                try:
                    yield
                finally:
                    held['card'] -= 1

        @contextlib.contextmanager
        def mutation_lock(project):
            self.assertGreater(held['card'], 0, 'the wiki lock follows the card-write lock')
            with real_mutation(project):
                held['wiki'] += 1
                try:
                    yield
                finally:
                    held['wiki'] -= 1

        def recheck(selected, **kwargs):
            self.assertIs(selected, bound)
            self.assertEqual(held, {'card': 1, 'wiki': 1}, 'recheck precedes the mutation under both locks')

        def request_sync(project, selected=None):
            self.assertEqual((project, selected), (self.root, bound))
            self.assertFalse(loser.exists(), 'the wake follows the deletion')
            self.assertEqual(held, {'card': 0, 'wiki': 0}, 'the wake follows lock release')
            wakes.append('wake')
            if len(wakes) == 1:
                raise RuntimeError('synthetic wake failure')
            return True

        with mock.patch('memvane.wiki.workspace.api.selected', return_value=bound), \
                mock.patch('memvane.wiki.workspace.api.recheck', side_effect=recheck), \
                mock.patch('memvane.wiki.workspace.api.request_sync', side_effect=request_sync), \
                mock.patch('memvane.wiki.workspace.api.card_write_lock', side_effect=card_lock), \
                mock.patch('memvane.wiki.workspace.api.mutation_lock', side_effect=mutation_lock):
            with self.assertRaisesRegex(RuntimeError, 'synthetic wake failure'):
                self.merge(0, 'entities/loser.md')
            self.assertFalse(loser.exists())
            self.assertTrue(winner.exists())
            self.assertEqual(len(jsonl(self.queue)), 1, 'a failed wake requeues the pair')
            # A hard crash leaves the claimed file instead of running the handler;
            # the next resolver start reclaims it.
            self.queue.rename(self.queue.with_name(self.queue.name + '.claimed.99999999.synthetic'))
            self.queue.touch()
            self.assertEqual(self.run_main('--recover-queue'), (0, {'action': 'recovered'}))
            self.assertEqual(len(jsonl(self.queue)), 1)
            # The agent retries the stale pair as a skip: wake first, then acknowledge.
            self.assertEqual(self.skip(0), (0, {'action': 'skipped', 'recorded': False}))
        self.assertEqual(self.queue.read_text(encoding='utf-8'), '')
        self.assertEqual(wakes, ['wake', 'wake'])
        self.assertIn('Loser.', (self.compile / 'dedup-deleted.jsonl').read_text(encoding='utf-8'))
        recovery = list((self.compile / 'dedup-recovery').glob('*.json'))
        self.assertEqual(len(recovery), 1)
        saved = json.loads(recovery[0].read_text(encoding='utf-8'))
        self.assertEqual((saved['deletedPath'], saved['pairedWith']), ('entities/loser.md', 'entities/winner.md'))
        self.assertTrue(saved['content'].endswith('Loser.\n'))
        self.assertEqual(recovery[0].stat().st_mode & 0o777, 0o600)

    def test_a_changed_selected_wiki_root_is_refused_before_a_claim(self):
        self.page('entities/a.md', 'a')
        entry = {'pageA': 'entities/a.md', 'pageB': 'entities/b.md'}
        self.enqueue(entry)
        bound = ({'collections': [{'role': 'wiki', 'sourceRoot': str(self.home / 'elsewhere')}]}, {})
        with mock.patch('memvane.wiki.workspace.api.selected', return_value=bound):
            with self.assertRaisesRegex(ValueError, 'selected_dedup_wiki_root_changed'):
                self.skip(0)
        self.assertEqual(jsonl(self.queue), [entry])
        self.assertEqual(list(self.compile.glob('dedup-needed.jsonl.claimed.*')), [])


class EntryProcess(ResolveCase):
    def test_wiki_dedup_resolve_entry_merges_one_pair(self):
        self.page('entities/loser.md', '---\ntitle: Loser\n---\n\nLoser content.\n')
        self.page('entities/winner.md', '---\ntitle: Winner\n---\n\nWinner content.\n')
        self.enqueue({'pageA': 'entities/loser.md', 'pageB': 'entities/winner.md', 'score': 0.95})
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
        proc = subprocess.run([sys.executable, '-B', str(REPO / 'entry' / 'run.py'), 'wiki-dedup-resolve',
                               '--cwd', str(self.root), '--index', '0', '--action', 'merge',
                               '--delete', 'entities/loser.md'],
                              capture_output=True, text=True, timeout=60, env=env, cwd=str(self.root))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {'action': 'deleted', 'deletedPath': 'entities/loser.md'})
        self.assertFalse((self.wiki / 'entities/loser.md').exists())
        self.assertEqual(jsonl(self.queue), [])


if __name__ == '__main__':
    unittest.main()

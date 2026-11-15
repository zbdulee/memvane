"""The retroactive dedup scan: which near-duplicate pairs it queues, once, within its caps.

Most cases give the scan a fixed set of indexed cards through its own reader
seam (``indexed_cards_schema3``); one case reads a real selected install's
published store.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from memvane.wiki.dedup import api as dedup
from memvane.wiki.dedup import wiki_dedup_scan as scan
from memvane.wiki.workspace import api as workspace
from tests.support import settings_fake
from tests.support.case import FixtureCase
from tests.support.install_builder import build_install
from tests.support.project import make_project


def card(title, fact, status='generated'):
    return '---\ntitle: %s\nstatus: %s\ncreatedBy: memvane\n---\n\n%s\n' % (title, status, fact)


ALPHA = card('Alpha bridge', 'The north bridge closes at 22:00 for repairs.')
ALPHA_COPY = card('Alpha bridge closure', 'North bridge repairs close it at 22:00.')
BETA = card('Beta harbor', 'The harbor ferry leaves every 40 minutes.')
GAMMA = card('Gamma market', 'The market opens on weekends only.')
ALPHA_PAIR = {'entities/alpha.md', 'entities/alpha-copy.md'}


def standard():
    return {'entities/alpha.md': (ALPHA, [1.0, 0.0]),
            'entities/alpha-copy.md': (ALPHA_COPY, [0.99, 0.1]),
            'entities/beta.md': (BETA, [0.0, 1.0]),
            'entities/gamma.md': (GAMMA, [0.6, 0.8])}


class ScanCase(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)
        self.root = self.project.root
        self.wiki = self.project.wiki
        self.queue = self.root / '.memvane' / 'compile' / 'dedup-needed.jsonl'
        self.state_dir = self.home / 'scan-state'
        patcher = mock.patch.dict(os.environ, {'QMD_SYNC_STATE_DIR': str(self.state_dir),
                                               'QMD_WIKI_DEDUP_LOG': str(self.tmp / 'wiki-dedup.log')})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.generation = 0
        self.indexed = {}
        self.decision = {'projectRoot': str(self.root), 'scopeId': '7' * 32,
                         'collections': [{'role': 'raw', 'sourceRoot': str(self.root)},
                                         {'role': 'wiki', 'sourceRoot': str(self.wiki.resolve())}]}
        self.reads = []

    def publish(self, cards):
        """Index ``cards`` as one generation and write the same bytes to disk."""
        self.generation += 1
        self.indexed = {}
        for rel, (text, vector) in cards.items():
            target = self.wiki / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding='utf-8')
            self.indexed[rel] = (hashlib.sha256(text.encode('utf-8')).hexdigest(), vector)

    def reader(self, decision, deadline):
        self.reads.append(decision)
        return 'gen-%d' % self.generation, {
            rel: scan._Card(rel, digest, scan._centroid([tuple(vector)]))
            for rel, (digest, vector) in self.indexed.items()}

    def run_scan(self, *, config=None, selected=None):
        if config is not None:
            settings_fake.use_config(self, self.project, config)
        bound = (self.decision, {'productRouteAllowed': True})
        with mock.patch.object(scan, 'indexed_cards_schema3', side_effect=self.reader), \
                mock.patch('memvane.wiki.workspace.api.selected',
                           **(selected or {'return_value': bound})), \
                mock.patch('memvane.wiki.workspace.api.recheck', return_value=None):
            return dedup.run(str(self.root))

    def queued(self):
        if not self.queue.exists():
            return []
        return [json.loads(line) for line in self.queue.read_text(encoding='utf-8').splitlines() if line]

    def pairs(self):
        return [{row['pageA'], row['pageB']} for row in self.queued()]


class Queueing(ScanCase):
    def test_queues_only_the_near_duplicate_pair_once(self):
        self.publish(standard())
        result = self.run_scan()
        self.assertEqual((result['state'], result['cards'], result['queued']), ('scanned', 4, 1), result)
        rows = self.queued()
        self.assertEqual(self.pairs(), [ALPHA_PAIR])
        self.assertGreaterEqual(rows[0]['score'], 0.9)
        # The snapshot records every compared card: nothing is compared or queued again.
        again = self.run_scan()
        self.assertEqual((again['compared'], again['queued']), (0, 0), again)
        self.assertEqual(len(self.queued()), 1)

    def test_resolved_queue_is_not_refilled_until_a_card_changes(self):
        self.publish(standard())
        self.run_scan()
        self.queue.write_text('', encoding='utf-8')
        self.assertEqual(self.run_scan()['queued'], 0)
        changed = standard()
        changed['entities/alpha-copy.md'] = (ALPHA_COPY + '\nAdded detail.\n', [0.98, 0.12])
        self.publish(changed)
        result = self.run_scan()
        self.assertEqual((result['compared'], result['queued']), (1, 1), result)

    def test_pair_held_by_a_running_resolver_is_not_queued_again(self):
        self.publish(standard())
        claimed = self.queue.with_name(self.queue.name + '.claimed.4242.' + 'a' * 32)
        claimed.parent.mkdir(parents=True, exist_ok=True)
        claimed.write_text(json.dumps({'pageA': 'entities/alpha-copy.md', 'pageB': 'entities/alpha.md',
                                       'score': 0.99}) + '\n', encoding='utf-8')
        self.assertEqual(self.run_scan()['queued'], 0)
        self.assertEqual(self.queued(), [])

    def test_skip_ledger_suppresses_unchanged_bodies_only(self):
        self.publish(standard())
        ledger = self.queue.with_name('dedup-skipped.jsonl')
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps({'pageA': 'entities/alpha-copy.md', 'pageB': 'entities/alpha.md',
                                      'pageAHash': scan.body_hash(ALPHA_COPY),
                                      'pageBHash': scan.body_hash(ALPHA)}) + '\n', encoding='utf-8')
        result = self.run_scan()
        self.assertEqual(result['queued'], 0, result)
        self.assertGreaterEqual(result['suppressed'], 1)
        self.assertEqual(self.queued(), [])
        changed = standard()
        changed['entities/alpha.md'] = (ALPHA + '\nA new fact.\n', [1.0, 0.0])
        self.publish(changed)
        self.assertEqual(self.run_scan()['queued'], 1, 'a changed body reopens the pair')

    def test_pair_cap_leaves_the_rest_pending_for_the_next_run(self):
        cards = standard()
        cards['entities/beta-copy.md'] = (card('Beta ferry', 'Ferries leave the harbor every 40 minutes.'),
                                          [0.1, 0.99])
        self.publish(cards)
        config = settings_fake.expanded(self.project, compile={'semanticDedup': {'maxPairsPerScan': 1}})
        first = self.run_scan(config=config)
        self.assertEqual(first['queued'], 1, first)
        self.assertGreater(first['pending'], 0)
        second = self.run_scan()
        self.assertEqual(second['queued'], 1, second)
        self.assertEqual({frozenset(pair) for pair in self.pairs()},
                         {frozenset(ALPHA_PAIR), frozenset(('entities/beta.md', 'entities/beta-copy.md'))})


class Scope(ScanCase):
    def test_card_edited_after_publish_waits_for_the_next_publish(self):
        self.publish(standard())
        (self.wiki / 'entities/alpha-copy.md').write_text(ALPHA_COPY + '\nUnpublished edit.\n', encoding='utf-8')
        self.assertEqual(self.run_scan()['queued'], 0)
        self.assertEqual(self.queued(), [])

    def test_superseded_and_discarded_cards_are_out_of_scope(self):
        for status in ('superseded', 'discarded'):
            with self.subTest(status=status):
                cards = standard()
                cards['entities/alpha-copy.md'] = (card('Alpha bridge closure',
                                                        'North bridge repairs close it at 22:00.',
                                                        status=status), [0.99, 0.1])
                self.publish(cards)
                self.assertEqual(self.run_scan()['queued'], 0)

    def test_scaffold_pages_symlinks_and_hidden_files_are_out_of_scope(self):
        cards = standard()
        for name in ('SCHEMA.md', 'index.md', 'log.md', 'entities/.hidden.md', 'entities/beta.txt'):
            cards[name] = (card('Beta harbor ' + name, 'The harbor ferry leaves every 40 minutes.'), [0.01, 1.0])
        self.publish(cards)
        link = self.wiki / 'entities' / 'beta-link.md'
        link.symlink_to(self.wiki / 'entities' / 'beta.md')
        self.indexed['entities/beta-link.md'] = (self.indexed['entities/beta.md'][0], [0.02, 1.0])
        self.run_scan()
        self.assertEqual(self.pairs(), [ALPHA_PAIR])


class NotScanned(ScanCase):
    def test_unavailable_route_or_store_writes_nothing(self):
        self.publish(standard())
        result = self.run_scan(selected={'side_effect': ValueError('selected_compile_route_unavailable')})
        self.assertEqual(result['state'], 'deferred')
        for failure in (ValueError('selected_dedup_schema3_endpoint_changed'), OSError('locked'),
                        TimeoutError('selected_dedup_deadline'), RuntimeError('busy')):
            with self.subTest(failure=failure):
                with mock.patch.object(scan, 'indexed_cards_schema3', side_effect=failure), \
                        mock.patch('memvane.wiki.workspace.api.selected',
                                   return_value=(self.decision, {})):
                    self.assertEqual(dedup.run(str(self.root))['state'], 'deferred')
        self.assertFalse(self.queue.exists())
        self.assertFalse(self.state_dir.exists())

    def test_skips_without_a_route_compile_dedup_or_vectors(self):
        self.publish(standard())
        off = settings_fake.expanded(self.project, compile={'mode': 'off'})
        no_dedup = settings_fake.expanded(self.project, compile={'semanticDedup': {'enabled': False}})
        table = [('compile off', {'config': off}),
                 ('dedup off', {'config': no_dedup}),
                 ('no selected route', {'selected': {'return_value': None}})]
        for label, kwargs in table:
            with self.subTest(label=label):
                result = self.run_scan(**kwargs)
                self.assertEqual((result['state'], result['queued']), ('skipped', 0))
                settings_fake.use_config(self, self.project, settings_fake.expanded(self.project))
        self.assertEqual(self.reads, [], 'no store read without a scan')
        with mock.patch.object(scan, 'indexed_cards_schema3', return_value=('gen-1', None)), \
                mock.patch('memvane.wiki.workspace.api.selected', return_value=(self.decision, {})):
            result = dedup.run(str(self.root))
        self.assertEqual((result['state'], result['reason']), ('skipped', scan.SCHEMA3_VECTORS_UNAVAILABLE))
        self.assertFalse(self.queue.exists())
        self.assertFalse(self.state_dir.exists())

    def test_a_wiki_root_other_than_the_selected_one_aborts(self):
        self.publish(standard())
        self.decision['collections'][1]['sourceRoot'] = str(self.home / 'elsewhere')
        self.assertEqual(self.run_scan()['state'], 'skipped')
        self.assertEqual(self.reads, [])
        self.assertFalse(self.queue.exists())


class SelectedStore(FixtureCase):
    """The scan reads the selected install's published store; the fixture provider
    gives every card the same vector, so the two cards are one near-duplicate pair."""

    def test_reads_the_published_view_of_a_selected_install(self):
        project = make_project(self.home)
        note = project.write_note('notes.md', 'The north bridge closes at 22:00.\n')
        project.write_card('entities/alpha.md', title='Alpha', body='The north bridge closes at 22:00.\n',
                           sources=[note])
        project.write_card('entities/alpha-copy.md', title='Alpha copy', body='North bridge closes at 22:00.\n',
                           sources=[note])
        project.commit()
        build_install(self, project)
        with mock.patch.dict(os.environ, {'QMD_SYNC_STATE_DIR': str(self.home / 'scan-state'),
                                          'QMD_WIKI_DEDUP_LOG': str(self.tmp / 'wiki-dedup.log')}):
            result = dedup.run(str(project.root))
            self.assertEqual((result['state'], result['cards'], result['queued']), ('scanned', 2, 1), result)
            queue = project.root / '.memvane' / 'compile' / 'dedup-needed.jsonl'
            rows = [json.loads(line) for line in queue.read_text(encoding='utf-8').splitlines()]
            self.assertEqual([{row['pageA'], row['pageB']} for row in rows],
                             [{'entities/alpha.md', 'entities/alpha-copy.md'}])
            self.assertEqual(dedup.run(str(project.root))['queued'], 0)


if __name__ == '__main__':
    unittest.main()

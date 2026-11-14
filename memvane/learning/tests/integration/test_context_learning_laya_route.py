"""Context learning on a selected route: corpus fingerprint, capture and training gate.

Every check runs on a project onboarded through the 1.0 install use cases
(``tests.support.install_builder``) with the fixture provider. Learning reads
the selected route and the sealed view head only; nothing here patches the
route or the store.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from unittest.mock import patch

from memvane.learning.context_learning import corpus, learning, shared
from memvane.learning.context_learning.contracts import digest
from memvane.learning.context_learning.runtime_setup import learning_state_base
from tests.support.case import FixtureCase
from tests.support.install_builder import build_install
from tests.support.project import make_project

PROMPT = 'Which card keeps the bridge marker?'
TOPICS = ('bridge', 'harbor', 'lantern')


class SelectedRoute(FixtureCase):
    def setUp(self):
        super().setUp()
        self.fixture = make_project(self.home, name='learning')
        self.project = self.fixture.root.resolve()
        self.note = self.fixture.write_note('notes.md', 'Field notes about the bridge and the harbor.\n')
        for topic in TOPICS:
            self.write_card(topic)
        self.fixture.commit('fixture')
        self.install = build_install(self, self.fixture)
        self.install.settle()
        from memvane.settings import api as settings
        self.settings = settings.find_project_config(str(self.project))['config']

    def write_card(self, topic, extra=''):
        return self.fixture.write_card(
            f'concepts/{topic}.md', title=topic.title() + ' card', sources=[self.note],
            body=f'The {topic} card keeps marker zq{topic}x.{extra}\n')

    def republish(self, message):
        self.fixture.commit(message)
        self.install.settle()

    def snapshot(self):
        return corpus.snapshot(self.project, self.settings)

    def private_state(self):
        """The prepared per-user selection parent and this project's state directory."""
        parent = learning_state_base() / 'live-selection'
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return parent / digest(str(self.project))

    def pool(self, current):
        """The verified rows a selected recall scores, read back from the snapshot's head."""
        rows = []
        for index, (collection, relative, revision) in enumerate(current['material']['activeDocuments']):
            path = self.fixture.wiki / relative
            body = path.read_text(encoding='utf-8')
            self.assertEqual(hashlib.sha256(body.encode('utf-8')).hexdigest(), revision)
            rows.append({'scope': self.install.decision['scopeId'], 'generation': current['generation'],
                         'path': collection + '/' + relative, 'hash': revision, 'body': body,
                         'score': 1 / (index + 1)})
        return rows

    def test_corpus_fingerprint_reads_the_selected_head_only(self):
        self.assertTrue(learning.ready(self.project))
        first, again = self.snapshot(), self.snapshot()
        self.assertIsNotNone(first)
        self.assertEqual(first['material']['schema'], 'laya-corpus-snapshot-v1')
        self.assertEqual(first['fingerprint'], again['fingerprint'])
        expected = sorted([self.fixture.wiki_name, 'concepts/%s.md' % topic,
                           hashlib.sha256((self.fixture.wiki / 'concepts' / ('%s.md' % topic)).read_bytes()).hexdigest()]
                          for topic in TOPICS)
        self.assertEqual(sorted(first['material']['activeDocuments']), expected)
        # A raw-only change publishes a new generation with the same wiki rows.
        self.fixture.write_note('notes-extra.md', 'Unrelated raw note.\n')
        self.republish('raw only')
        raw_only = self.snapshot()
        self.assertEqual(raw_only['fingerprint'], first['fingerprint'])
        self.assertNotEqual(raw_only['generation'], first['generation'])
        self.write_card('harbor', ' Revised.')
        self.republish('revised card')
        revised = self.snapshot()
        self.assertNotEqual(revised['fingerprint'], first['fingerprint'])

    def test_a_project_without_a_selected_route_has_no_corpus(self):
        other = make_project(self.home, name='other', dirname='other')
        other.write_note('notes.md', 'Not onboarded.\n')
        other.commit('fixture')
        self.assertFalse(learning.ready(other.root))
        self.assertIsNone(corpus.snapshot(other.root.resolve(), self.settings))

    def test_capture_stores_the_scored_pool_with_the_head_fingerprint(self):
        current = self.snapshot()
        pool = self.pool(current)
        state = self.private_state()
        status = learning.capture_recall('codex', self.install.decision, {'prompt': PROMPT},
                                         self.settings, pool, [pool[0]])
        self.assertEqual(status, 'stored')
        with closing(sqlite3.connect(state / 'learning.sqlite3')) as db:
            (raw,), = db.execute('SELECT body FROM samples').fetchall()
        sample = json.loads(raw)
        self.assertEqual(sample['schema_version'], 3)
        self.assertEqual(sample['host'], 'codex')
        self.assertEqual([c['candidate_id'] for c in sample['candidates']], [row['path'] for row in pool])
        self.assertEqual(sample['sampling']['baseline_selected_ids'], [pool[0]['path']])
        self.assertEqual(sample['sampling']['index_revision'], current['fingerprint'])
        self.assertTrue(sample['sampling']['complete_within_returned_bound'])

    def test_capture_honors_setting_and_hook_budget(self):
        pool = self.pool(self.snapshot())
        disabled = dict(self.settings)
        disabled['contextLearning'] = {**disabled['contextLearning'], 'capture': False}
        self.assertEqual(learning.capture_recall('codex', self.install.decision, {'prompt': PROMPT},
                                                 disabled, pool, []), 'disabled')
        self.assertEqual(learning.capture_recall('codex', self.install.decision, {'prompt': PROMPT},
                                                 self.settings, pool, [], started=time.monotonic() - 60),
                         'capture_deadline')
        self.assertFalse((learning_state_base() / 'live-selection' / digest(str(self.project))).exists())

    def test_stale_incremental_row_stays_private(self):
        current = self.snapshot()
        pool = self.pool(current)
        newer = pool[0]['body'] + 'Incremental edit.\n'
        pool[0] = {**pool[0], 'generation': 'gen-incremental',
                   'hash': hashlib.sha256(newer.encode('utf-8')).hexdigest(), 'body': newer}
        state = self.private_state()
        status = learning.capture_recall('codex', self.install.decision, {'prompt': PROMPT},
                                         self.settings, pool, [])
        self.assertEqual(status, 'stored')
        with closing(sqlite3.connect(state / 'learning.sqlite3')) as db:
            sample = json.loads(db.execute('SELECT body FROM samples').fetchone()[0])
        self.assertEqual(sample['sampling']['snapshot_failures'],
                         [{'candidate_id': pool[0]['path'], 'reason': 'candidate_index_stale'}])
        self.assertFalse(sample['sampling']['complete_within_returned_bound'])

    def test_training_gate_accepts_only_a_verified_selected_route(self):
        lock = self.tmp / 'lease.lock'

        def run_owned(project):
            fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
            with patch.object(shared, '_owned_root', return_value=(project, self.settings, self.tmp)), \
                    patch.object(shared, '_check_owned_lease'), \
                    patch.object(shared, '_training_worker_state', return_value={'outcome': None}), \
                    patch.object(shared, 'run_training_pending', return_value={'status': 'trained'}), \
                    patch.object(shared, '_settle_owned', side_effect=lambda _r, _n, _s, result, _p: result):
                return shared._run_owned_training(project, fd)

        self.assertEqual(run_owned(self.project), {'status': 'trained'})
        other = make_project(self.home, name='other', dirname='other')
        other.commit('fixture')
        self.assertEqual(run_owned(other.root.resolve()), {'status': 'setup_in_progress'})
        self.assertFalse(learning.ready(other.root))


if __name__ == '__main__':
    import unittest
    unittest.main()

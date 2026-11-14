"""Corpus changes queue captured samples for review and never rewrite captured gold.

The active-index snapshot comes from the selected Laya route; here a sequence
of synthetic snapshots stands in for five index changes (update, create,
delete, selection policy, embedding model).
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from memvane.learning.context_learning import corpus
from memvane.learning.context_learning.contracts import digest
from memvane.learning.context_learning.corpus import reconcile, current_evaluation_ready
from memvane.learning.context_learning.seam import observe


def _snapshot(label, documents):
    material = {'activeDocuments': [list(row) for row in documents], 'policy': label}
    return {'fingerprint': digest(json.dumps(material, sort_keys=True)), 'material': material,
            'activeDocuments': len(documents)}


class CorpusReview(unittest.TestCase):
    def test_index_changes_queue_review_and_keep_the_captured_sample(self):
        with tempfile.TemporaryDirectory(prefix='qmd-corpus-check-') as temporary:
            base = Path(temporary).resolve()
            project = base / 'project'; project.mkdir(mode=0o700)
            docs = project / 'docs'; docs.mkdir()
            source = docs / 'one.md'; source.write_text('Synthetic evidence version one.\n')
            state_parent = base / 'private'; state_parent.mkdir(mode=0o700)
            config = {'collectionPaths': {'docs': 'docs'}, 'contextLearning': {
                'capture': True, 'stateRoot': str(state_parent)}}
            one = ('docs', 'one.md', hashlib.sha256(source.read_bytes()).hexdigest())
            first = _snapshot('top3', [one])
            hit = {'file': 'docs/one.md', 'score': 1.0}
            with patch.object(corpus, 'snapshot', return_value=first):
                status = observe({'prompt': 'Synthetic evidence request'}, config, project, [],
                                 phases=[{'name': 'primary', 'results': [hit], 'wiki_scoped': False,
                                          'returned_count': 1}], verdict=lambda *_: 'eligible')
            self.assertEqual(status, 'stored')
            state = state_parent / digest(str(project))
            with sqlite3.connect(state / 'learning.sqlite3') as db:
                rid, original = db.execute('SELECT request_id,body FROM samples').fetchone()
                self.assertEqual(json.loads(original)['sampling']['index_revision'], first['fingerprint'])
                db.execute('INSERT INTO manifest_cases VALUES (?,?,?)', ('v1', rid, 'docs/one.md'))
            self.assertTrue(current_evaluation_ready(state, 'v1', first))
            one_v2 = ('docs', 'one.md', digest('version two'))
            two = ('docs', 'two.md', digest('new document'))
            changes = [_snapshot('top3', [one_v2]), _snapshot('top3', [one_v2, two]),
                       _snapshot('top3', [two]), _snapshot('top2', [two]),
                       _snapshot('top2-model2', [two])]
            for current in changes:
                result = reconcile(state, current)
                self.assertTrue(result['changed'])
                self.assertEqual(result['queued'], 1)
                self.assertFalse(current_evaluation_ready(state, 'v1', current))
            with sqlite3.connect(state / 'learning.sqlite3') as db:
                saved = db.execute('SELECT body FROM samples WHERE request_id=?', (rid,)).fetchone()[0]
                queued = db.execute('SELECT COUNT(*) FROM corpus_review_queue WHERE request_id=? AND status=?',
                                    (rid, 'pending_review')).fetchone()[0]
            self.assertEqual(saved, original)
            self.assertEqual(queued, 5)

    def test_an_unavailable_snapshot_is_not_guessed(self):
        self.assertEqual(reconcile(Path('/synthetic/unused'), None), {'status': 'corpus_unavailable'})
        self.assertFalse(current_evaluation_ready(Path('/synthetic/unused'), 'v1', None))


if __name__ == '__main__':
    unittest.main()

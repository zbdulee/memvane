"""The dry run's pure parts: RRF scores before ``1/rank`` and the frozen result value."""
from __future__ import annotations

import hashlib
import json
import unittest

from memvane.recall.domain import rules
from memvane.recall.types import DryRunRecallResult, RRF_K

SCOPE = '6' * 32


def row(path):
    return {'scope': SCOPE, 'generation': 'generation-7', 'path': path,
            'hash': hashlib.sha256(path.encode('utf-8')).hexdigest(), 'body': path, 'score': 0.5}


class RrfRanked(unittest.TestCase):
    def test_scores_follow_the_served_order_with_weights_and_bonuses(self):
        first = [row(path) for path in ('b.md', 'x.md', 'y.md', 'a.md')]
        second = [row(path) for path in ('z.md', 'x.md', 'y.md', 'a.md')]
        ranked = rules.rrf_ranked([[], first, second])
        served = rules._fuse_verified_lists([[], first, second], max_rows=5)
        self.assertEqual([item['path'] for item, _score in ranked[:5]],
                         [item['path'] for item in served])
        scores = {item['path']: score for item, score in ranked}
        self.assertAlmostEqual(scores['b.md'], 2 / (RRF_K + 1) + 0.05)
        self.assertAlmostEqual(scores['z.md'], 1 / (RRF_K + 1) + 0.05)
        self.assertAlmostEqual(scores['x.md'], 2 / (RRF_K + 2) + 1 / (RRF_K + 2) + 0.02)
        self.assertEqual([score for _item, score in ranked], sorted((s for _i, s in ranked), reverse=True))


class ResultValue(unittest.TestCase):
    def value(self, **changes):
        fields = dict(route='candidate', outcome='served', reason='ranked',
                      lexical_lists=[{'query': '"heron"*', 'rows': [{'path': 'w/a.md', 'score': -1.5}]}],
                      selector_result={'ran': False, 'final': []}, timings={'lexicalMs': 1.0})
        fields.update(changes)
        return DryRunRecallResult(**fields)

    def test_frozen_equal_by_fields_and_json_ready(self):
        value = self.value()
        self.assertEqual(value, self.value())
        self.assertEqual(hash(value), hash(self.value()))
        self.assertNotEqual(value, self.value(reason='selected'))
        with self.assertRaises(AttributeError):
            value.reason = 'selected'
        with self.assertRaises(TypeError):
            value.timings['lexicalMs'] = 2.0
        with self.assertRaises(TypeError):
            value.lexical_lists[0]['rows'][0]['score'] = 0
        view = value.as_dict()
        self.assertEqual(set(view), {'route', 'linked', 'outcome', 'reason', 'generation', 'lexicalLists',
                                     'semanticList', 'fusedList', 'selectorResult', 'semanticStatus',
                                     'vectorCoveredMembers', 'vectorEligibleMembers', 'fallbackReason',
                                     'notes', 'timings'})
        self.assertEqual(json.loads(json.dumps(view)), view)


if __name__ == '__main__':
    unittest.main()

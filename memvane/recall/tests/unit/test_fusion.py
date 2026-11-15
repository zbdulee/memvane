"""QMD-pinned lexical atoms and RRF fusion of verified ranked lists."""
import hashlib
import unittest

from memvane.recall.domain import rules
from memvane.recall.types import HostResponseError

SCOPE = '6' * 32


def row(path, body, **changes):
    result = {'scope': SCOPE, 'generation': 'generation-7', 'path': path,
              'hash': hashlib.sha256(body.encode('utf-8')).hexdigest(), 'body': body, 'score': 0.5}
    result.update(changes)
    return result


class Fusion(unittest.TestCase):
    def test_fts_atom_follows_the_compound_and_cjk_policy(self):
        self.assertEqual(rules._fts_atom('PIO-1384'), '"pio 1384"')
        self.assertEqual(rules._fts_atom('한국어'), '"한 국 어"')
        self.assertEqual(rules._fts_atom('target'), '"target"*')

    def test_first_nonempty_list_weighs_double_and_scores_are_positions(self):
        found = rules._fuse_verified_lists([[], [row('z-ep.md', 'ep')],
                                            [row('a-vector.md', 'semantic')]])
        self.assertEqual([item['path'] for item in found], ['z-ep.md', 'a-vector.md'])
        self.assertEqual([item['score'] for item in found], [1.0, 0.5])

    def test_top_rank_bonus_changes_the_winner(self):
        first = [row(path, path) for path in ('b-first.md', 'x.md', 'y.md', 'a-both-fourth.md')]
        second = [row(path, path) for path in ('z.md', 'x.md', 'y.md', 'a-both-fourth.md')]
        found = rules._fuse_verified_lists([first, second])
        self.assertEqual((found[0]['path'], found[0]['score']), ('b-first.md', 1.0))

    def test_equal_scores_keep_first_seen_list_order(self):
        found = rules._fuse_verified_lists([[row('x-double.md', 'x')], [row('z-seen-first.md', 'z')],
                                            [row('a-seen-later.md', 'a')]])
        self.assertEqual([item['path'] for item in found],
                         ['x-double.md', 'z-seen-first.md', 'a-seen-later.md'])

    def test_fusion_refuses_mixed_generations_and_conflicting_bodies(self):
        cases = {
            'mixed_result_generation': [[row('a.md', 'a')], [row('b.md', 'b', generation='generation-8')]],
            'hybrid_source_mismatch': [[row('a.md', 'a')], [row('a.md', 'changed')]],
            'duplicate_hybrid_candidate': [[row('a.md', 'a'), row('a.md', 'a')]],
            'invalid_result_shape': [[{'path': 'a.md'}]],
        }
        for code, lists in cases.items():
            with self.subTest(code=code):
                with self.assertRaises(HostResponseError) as caught:
                    rules._fuse_verified_lists(lists)
                self.assertEqual(str(caught.exception), code)


if __name__ == '__main__':
    unittest.main()

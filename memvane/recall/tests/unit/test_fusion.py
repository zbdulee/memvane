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

    def test_hybrid_fts_queries_emit_standalone_structural_identifiers(self):
        q1 = rules._hybrid_fts_queries(
            '도구는 쓰지 말고 답만 해 줘. 확인 토큰 SMKFEED0900이 적힌 문서는 무엇에 관한 문서야?', ())
        self.assertIn('"smkfeed0900"*', q1)

        q2 = rules._hybrid_fts_queries('우리 프로젝트에서 옛날에 적어둔 SMKFEED0900 위치 알려줘', ())
        self.assertEqual(len(q2), 2)
        self.assertEqual(q2[1], '"smkfeed0900"*')

        q3 = rules._hybrid_fts_queries('SMK001AD, SMK001CH, SMK00104 확인', ())
        for tok in ('"smk001ad"*', '"smk001ch"*', '"smk00104"*'):
            self.assertIn(tok, q3)

        # Non-structural path stems, plain backtick words, short alnum tokens and disabled EP terms do not fan out.
        self.assertEqual(len(rules._hybrid_fts_queries('docs/settings.md 구조', ())), 1)
        self.assertEqual(len(rules._hybrid_fts_queries('`npm test` 실패', ())), 1)
        self.assertEqual(len(rules._hybrid_fts_queries('v2 API에서 mp3 파일을 어떻게 처리해?', ())), 1)
        self.assertEqual(len(rules._hybrid_fts_queries('`EP12` 복선 정리해줘', ())), 1)
        for token, expected in (('RS-1', '"rs 1"'), ('foo_bar', '"foo_bar"*'),
                                ('sha256', '"sha256"*'), ('camelCase', '"camelcase"*')):
            self.assertIn(expected, rules._hybrid_fts_queries(f'확인 {token} 위치', ()))
        # Single structural identifier does not duplicate lexQueries[0].
        self.assertEqual(rules._hybrid_fts_queries('SMKFEED0900', ()), ['"smkfeed0900"*'])

    def test_empty_general_and_standalone_identifier_outranks_semantic_winner(self):
        found = rules._fuse_verified_lists([
            [],
            [row('exact-token.md', 'SMKFEED0900 card')],
            [row('semantic-noise.md', 'unrelated semantic card')],
        ])
        self.assertEqual([item['path'] for item in found], ['exact-token.md', 'semantic-noise.md'])

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

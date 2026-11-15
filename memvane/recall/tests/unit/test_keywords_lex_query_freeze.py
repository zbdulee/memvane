"""Frozen choice of the AND terms in the general lexical query (``lexQueries[0]``).

Two axes: without the daemon (``build_lexical_terms``) the general query is a
positional cut; with a corpus DF set (``select_general_terms_explained``) the
terms present in the corpus win, under the same cap, and a lone survivor keeps
the positional query. EP variants (``lexQueries[1:]``) are outside both rules.
The ``present`` sets are the live DF>0 measurements these values were frozen on.
"""
import unittest

from memvane.recall import keywords

# DF_PROBE_MAX_TERMS: the number of general terms the recall path probes.
PROBED_TERMS = 6

CASES = (
    # Colloquial filler in front of a related question.
    dict(prompt='이 부분 관련해서 sendbird 장애 원인 알려줘', positional='부분 sendbird 장애',
         present=('부분', 'sendbird', '장애', '원인'), narrowed='부분 sendbird 장애', mode='present'),
    dict(prompt='아까 말한 그거 있잖아 VN 콜백 이벤트', positional='아까 말한 있잖아',
         present=('VN', '콜백'), narrowed='VN 콜백', mode='present'),
    # Direct questions: neither axis changes them.
    dict(prompt='sendbird 장애 원인이 뭐였지?', positional='sendbird 장애 원인',
         present=('sendbird', '장애', '원인'), narrowed='sendbird 장애 원인', mode='present'),
    dict(prompt='VN 콜백 이벤트 언제 발생해?', positional='VN 콜백 이벤트',
         present=('VN', '콜백', '이벤트', '발생해'), narrowed='VN 콜백 이벤트', mode='present'),
    dict(prompt='중복 판정은 어떻게 하나요?', positional='중복 판정',
         present=('중복', '판정'), narrowed='중복 판정', mode='present'),
    # Unrelated questions: the DF filter must not open the gate.
    dict(prompt='python list comprehension 문법 알려줘', positional='python list comprehension',
         present=('python', 'list', 'comprehension'), narrowed='python list comprehension',
         mode='present'),
    dict(prompt='리액트 useEffect 의존성 배열 규칙 설명', positional='useEffect 리액트 의존성',
         present=('의존성', '배열', '규칙'), narrowed='의존성 배열 규칙', mode='present'),
    dict(prompt='오늘 점심 뭐 먹을까 고민이네', positional='오늘 점심 먹을까',
         present=('오늘',), narrowed='오늘 점심 먹을까', mode='lone_survivor'),
    dict(prompt='이 함수 이름을 뭐로 지을까 고민', positional='함수 이름 뭐로',
         present=('함수', '이름', '고민'), narrowed='함수 이름 고민', mode='present'),
    # Three tokens or fewer, all present: no rule changes anything.
    dict(prompt='설정 파일 위치 알려줘', positional='설정 파일 위치',
         present=('설정', '파일', '위치'), narrowed='설정 파일 위치', mode='present'),
)


def narrow(prompt, present):
    """The daemon path: the built general terms narrowed by a DF>0 set."""
    probed = keywords.build_lexical_terms(prompt, [])['generalTerms'][:PROBED_TERMS]
    terms, mode = keywords.select_general_terms_explained(
        probed, {term.lower() for term in present})
    return ' '.join(terms), mode, probed


class LexQueryFreeze(unittest.TestCase):
    def test_without_the_daemon_the_general_query_is_the_positional_cut(self):
        for case in CASES:
            with self.subTest(prompt=case['prompt']):
                built = keywords.build_lexical_terms(case['prompt'], [])
                self.assertEqual(built['lexQueries'][0], case['positional'])
                self.assertEqual(len(built['lexQueries']), 1)

    def test_with_the_daemon_present_terms_are_kept_under_the_cap(self):
        for case in CASES:
            with self.subTest(prompt=case['prompt']):
                query, mode, _probed = narrow(case['prompt'], case['present'])
                self.assertEqual(query, case['narrowed'])
                self.assertEqual(mode, case['mode'])

    def test_df_filter_recovers_content_words_behind_leading_filler(self):
        query, _mode, probed = narrow('아까 말한 그거 있잖아 VN 콜백 이벤트', ('VN', '콜백'))
        self.assertEqual(query, 'VN 콜백')
        self.assertEqual(probed, ['아까', '말한', '있잖아', 'VN', '콜백'])

    def test_a_lone_survivor_keeps_the_positional_query(self):
        query, mode, _probed = narrow('오늘 점심 뭐 먹을까 고민이네', ('오늘',))
        self.assertEqual((query, mode), ('오늘 점심 먹을까', 'lone_survivor'))
        self.assertEqual(narrow('오늘 점심 뭐 먹을까 고민이네', ('오늘', '점심'))[1], 'present')

    def test_unknown_df_falls_back_to_the_positional_cut(self):
        terms, mode = keywords.select_general_terms_explained(
            ['sendbird', '장애', '원인', '뭐였지'], None)
        self.assertEqual((' '.join(terms), mode), ('sendbird 장애 원인', 'positional'))

    def test_the_cap_holds_after_the_df_filter(self):
        terms, mode = keywords.select_general_terms_explained(
            ['a', 'b', 'c', 'd', 'e'], {'a', 'b', 'c', 'd', 'e'})
        self.assertEqual(keywords.GENERAL_LEX_TERM_CAP, 3)
        self.assertEqual((len(terms), mode), (3, 'present'))

    def test_the_cap_cuts_only_the_count_never_order_or_stemming(self):
        built = keywords.build_lexical_terms('sendbird 장애 원인이 뭐였지?', [])
        self.assertEqual(built['lexQueries'][0].split(' '), ['sendbird', '장애', '원인'])
        for key in ('lexicalTerms', 'keywords', 'generalTerms'):
            self.assertEqual(built[key], ['sendbird', '장애', '원인', '뭐였지'], key)

    def test_ep_variants_are_outside_every_selection_rule(self):
        built = keywords.build_lexical_terms('EP12 와 EP7 에서 주인공 감정선 변화 정리해줘', ['ep'])
        self.assertNotRegex(built['lexQueries'][0], '(?i)EP')
        self.assertLessEqual(len(built['lexQueries'][0].split()), 3)
        self.assertFalse(any(term.upper().startswith('EP') for term in built['generalTerms']))
        self.assertEqual(built['lexQueries'][1:], ['EP012', 'EP12', 'EP007', 'EP7'])
        self.assertEqual(built['epTerms'], ['EP012', 'EP12', 'EP007', 'EP7'])

    def test_ep_searches_stay_within_the_budget(self):
        built = keywords.build_lexical_terms('EP1 EP2 EP3 EP4 EP5 화 정리', ['ep'])
        self.assertEqual(keywords.EP_SEARCH_BUDGET, 6)
        self.assertLessEqual(len(built['lexQueries']) - 1, keywords.EP_SEARCH_BUDGET)
        # General lex 1 + EP variants + vector 1 stay within the 10-search payload.
        self.assertLessEqual(len(built['lexQueries']) + 1, 10)


if __name__ == '__main__':
    unittest.main()

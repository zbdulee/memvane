"""Recall query terms: keywords, exact identifiers, EP variants and the question span.

``kw`` is what the old keyword CLI printed: ``build_lexical_terms`` of the
stripped prompt. Identifier terms must be plain terms the QMD lexical index can
match (no quoted phrases, no separators the FTS sanitizer glues together).
"""
import re
import time
import unittest

from memvane.recall import keywords


def kw(prompt, patterns=()):
    return keywords.build_lexical_terms(prompt.strip(), list(patterns))


def sanitize_fts5_term(term):
    """QMD's ``sanitizeFTS5Term``: drop all but letters, digits, ``'`` and ``_``."""
    return ''.join(ch for ch in term if ch.isalnum() or ch in "'_").lower()


# QMD's ``isHyphenatedToken`` (letters and digits joined by hyphens).
_HYPHENATED = re.compile(r"^[^\W_](?:[^\W_]|['-])*-[^\W_](?:[^\W_]|['-])*$")


class LexTerms(unittest.TestCase):
    def assert_indexable(self, term):
        self.assertNotIn('"', term, 'no quoted phrase')
        self.assertIsNone(re.search(r'[./]', term), 'no separator the sanitizer glues')
        if _HYPHENATED.match(term):
            return  # QMD splits hyphenated tokens itself
        self.assertEqual(sanitize_fts5_term(term), term.lower())

    def test_stopwords_dropped_and_korean_stems(self):
        built = kw('검색 결과 기반 정렬은 어떻게 동작하나요')
        self.assertTrue('검색' in built['keywords'] or '정렬' in built['keywords'])
        self.assertNotIn('어떻게', built['keywords'])

    def test_ep_pattern_off_has_no_ep_terms(self):
        self.assertFalse(any(re.search('EP0?12', term) for term in kw('EP12 복선')['lexicalTerms']))

    def test_ep_pattern_on_builds_normalized_ep_terms(self):
        terms = kw('EP12 복선', ['ep'])['lexicalTerms']
        self.assertIn('EP012', terms)
        self.assertIn('EP12', terms)

    def test_keywords_capped_at_five(self):
        self.assertLessEqual(len(kw('하나 둘 셋 넷 다섯 여섯 일곱 여덟 아홉 열')['keywords']), 5)

    def test_host_instruction_identifiers_cannot_contaminate_the_selected_question(self):
        built = kw('도구를 쓰지 말고 `MCP/Tool.py` 컨텍스트만 써.\r\nrelease QA alpha 문서의 codename은?')
        self.assertEqual(built['keywordSpan'], 'question')
        self.assertFalse(any(re.search('(?i)MCP|Tool', term) for term in built['identifiers']))
        self.assertNotRegex(built['lexQueries'][0], '(?i)MCP|Tool')

    def test_multi_line_question_selection_leaves_ambiguous_and_ordinary_requests(self):
        ambiguous = '도구를 쓰지 말고 컨텍스트만 써.\n첫째 질문은?\n둘째 질문은?'
        ordinary = '원문에서 중요한 맥락을 설명해.\nrelease QA alpha 문서의 codename은?'
        for prompt in (ambiguous, ordinary):
            with self.subTest(prompt=prompt):
                self.assertEqual(kw(prompt)['keywordSpan'], 'full')
                self.assertEqual(kw(prompt)['keywords'], kw(prompt.replace('\n', ' '))['keywords'])
        self.assertEqual(kw('Use only tool context.\nhttps://example.test/?a=1\n'
                            'release QA alpha codename?')['keywordSpan'], 'question')
        self.assertEqual(kw('Use only tool context.\nhttps://example.test/?a=1')['keywordSpan'], 'full')
        self.assertEqual(kw('auto-context tool 설정 문제\nrelease QA alpha codename?')['keywordSpan'],
                         'full')
        self.assertEqual(kw('도구 없이 컨텍스트 주입되는 이유가 뭐야?\nrecall hook 로그 어디서 봐?')
                         ['keywordSpan'], 'full')
        ep = 'EP012 도구를 쓰지 말고 컨텍스트만 써.\nrelease QA alpha 문서의 codename은?'
        self.assertEqual(kw(ep, ['ep'])['keywordSpan'], 'question')
        self.assertTrue(kw(ep, ['ep'])['epTerms'])
        self.assertEqual(kw(ep, ['ep'])['epTerms'], kw(ep.replace('\n', ' '), ['ep'])['epTerms'])

    # -- exact identifiers ---------------------------------------------------------
    def test_backtick_code_span_becomes_an_identifier_first(self):
        built = kw('`recallVerifiedOnly` 기본값이 뭐야')
        self.assertIn('recallVerifiedOnly', built['identifiers'])
        self.assertEqual(built['lexicalTerms'][0], 'recallVerifiedOnly')

    def test_each_token_inside_backticks_is_extracted(self):
        identifiers = kw('`npm test` 돌리면 실패하는 이유')['identifiers']
        self.assertIn('npm', identifiers)
        self.assertIn('test', identifiers)

    def test_path_token_promotes_its_basename_stem_to_a_plain_term(self):
        built = kw('docs/settings.md 파일 구조 설명해줘')
        self.assertNotIn('docs/settings.md', built['identifiers'])
        self.assertIn('settings', built['identifiers'])
        self.assertIn('settings', built['lexicalTerms'])
        self.assertFalse(any(sanitize_fts5_term(term) == 'docssettingsmd'
                             for term in built['lexicalTerms']))
        for term in built['identifiers']:
            self.assert_indexable(term)

    def test_path_and_dotted_tokens_never_make_quoted_phrases(self):
        for prompt in ('docs/settings.md 파일 구조 설명해줘',
                       'memvane/wiki/compile/wiki_verify_worker.py 동작 확인해줘',
                       '`verify-queue.jsonl` 에 enqueue 되는 조건이 뭐야',
                       '.memvane/settings.json 의 recallStrategy 기본값',
                       'config.load_project_config 가 어디서 불리나'):
            with self.subTest(prompt=prompt):
                built = kw(prompt)
                self.assertFalse(any('"' in term for term in built['lexicalTerms']))
                for term in built['identifiers']:
                    self.assert_indexable(term)

    def test_path_stem_drops_the_extension_and_keeps_the_most_specific_part(self):
        self.assertIn('wiki_verify_worker', kw('lib/wiki_verify_worker.py 동작 확인해줘')['identifiers'])
        self.assertIn('load_project_config',
                      kw('config.load_project_config 가 어디서 불리나')['identifiers'])
        built = kw('docs/settings.md 파일 구조 설명해줘')
        self.assertNotIn('"settings md"', built['identifiers'])
        self.assertEqual(built['lexicalTerms'].count('settings'), 1)

    def test_every_identifier_term_is_indexable_by_the_lexical_grammar(self):
        for prompt in ('docs/settings.md 랑 lib/wiki_verify_worker.py 를 비교해줘',
                       '`verify-queue.jsonl` 에 enqueue 되는 조건이 뭐야',
                       'wiki_compile_worker 가 sqlite-wal 을 키우는 이유',
                       'CLAUDE_PLUGIN_ROOT 비면 HTTPServer 어댑터가 죽어',
                       '.memvane/settings.json 의 recallStrategy 기본값'):
            with self.subTest(prompt=prompt):
                for term in kw(prompt)['identifiers']:
                    self.assert_indexable(term)

    def test_call_snake_kebab_and_camel_identifiers(self):
        identifiers = kw('classifyOrigin() 이 wiki_compile_worker 랑 sqlite-wal 을 '
                         'CamelHumpName 으로 다루나')['identifiers']
        for name in ('classifyOrigin', 'wiki_compile_worker', 'CamelHumpName', 'sqlite-wal'):
            self.assertIn(name, identifiers)

    def test_screaming_snake_and_bracket_tag_identifiers(self):
        identifiers = kw('CLAUDE_PLUGIN_ROOT 비면 [DEV20100] 처럼 exit 127 나고 minScore 0.8 이 '
                         '안 맞아')['identifiers']
        self.assertIn('CLAUDE_PLUGIN_ROOT', identifiers)
        self.assertIn('DEV20100', identifiers)

    def test_bare_numbers_are_never_identifiers(self):
        identifiers = kw('CLAUDE_PLUGIN_ROOT 비면 exit 127 나고 minScore 0.8 이 안 맞아')['identifiers']
        self.assertNotIn('127', identifiers)
        self.assertNotIn('0.8', identifiers)
        self.assertFalse(any(re.fullmatch(r'"?[\d\s.]+"?', term) for term in identifiers))

    def test_numbers_become_identifiers_only_inside_backticks(self):
        identifiers = kw('exit code 는 `127` 이고 임계치는 `12000` 이야')['identifiers']
        self.assertIn('127', identifiers)
        self.assertIn('12000', identifiers)
        self.assertFalse(any(re.search(r'\d', term)
                             for term in kw('임계치는 `0.8` 이야 확인해줘')['identifiers']))

    def test_dates_and_ips_do_not_leak_into_identifiers(self):
        dated = kw('2026년 7월 29일 배포된 docs/settings.md 확인')['identifiers']
        self.assertFalse(any(re.search(r'\d', term) for term in dated))
        self.assertIn('settings', dated)
        ip = kw('192.168.0.1 에서 timeout 이 나는데 lib/recall.py 확인해줘')['identifiers']
        self.assertFalse(any(re.search(r'\d', term) for term in ip))
        self.assertIn('recall', ip)

    def test_acronym_camel_case_is_kept_whole(self):
        identifiers = kw('HTTPServer 랑 XMLParser 어댑터 구조 설명해줘')['identifiers']
        self.assertIn('HTTPServer', identifiers)
        self.assertIn('XMLParser', identifiers)

    def test_overlapping_names_survive_together(self):
        identifiers = kw('`notebook` 이랑 book() 이 서로 충돌해')['identifiers']
        self.assertIn('notebook', identifiers)
        self.assertIn('book', identifiers)

    def test_an_identifier_after_the_fifth_word_is_still_a_lexical_term(self):
        built = kw('하나 둘 셋 넷 다섯 여섯 일곱 여덟 문서에서 wiki_verify_worker.py 동작 확인')
        self.assertEqual(len(built['keywords']), 5)
        self.assertNotIn('wiki_verify_worker', built['keywords'])
        self.assertIn('wiki_verify_worker', built['lexicalTerms'])

    def test_identifiers_skip_korean_suffix_stemming(self):
        built = kw('`settings` 값은 어디서 읽어')
        self.assertIn('settings', built['identifiers'])
        self.assertIn('settings', built['lexicalTerms'])

    def test_identifier_budget_is_four(self):
        built = kw('`a_one` `b_two` `c_three` `d_four` `e_five` `f_six` `g_seven` `h_eight`')
        self.assertEqual(keywords.IDENTIFIER_BUDGET, 4)
        self.assertEqual(len(built['identifiers']), 4)

    def test_korean_only_prompt_keeps_keywords_without_identifiers(self):
        built = kw('검색 결과 기반 정렬은 어떻게 동작하나요')
        self.assertEqual(built['identifiers'], [])
        self.assertEqual(built['lexicalTerms'], built['keywords'])
        self.assertNotIn('어떻게', built['keywords'])

    def test_ep_pattern_off_keeps_ep_terms_out_of_identifiers_too(self):
        for prompt in ('`EP12` 복선 정리해줘', 'EP-12 복선 정리해줘', 'ep_12 복선 정리해줘',
                       'docs/EP12.md 확인해줘 복선 정리', 'docs/EP-12.md 확인해줘 복선 정리',
                       'docs/ep_12.md 확인해줘 복선 정리'):
            with self.subTest(prompt=prompt):
                self.assertFalse(any(re.search(r'(?i)EP[\s_-]?0?12', term)
                                     for term in kw(prompt)['lexicalTerms']))

    def test_backtick_identifier_survives_past_the_candidate_cap(self):
        filler = ' '.join(['alphaword'] * 405)
        built = kw(filler + ' `criticalIdentifier` 확인해줘')
        self.assertIn('criticalIdentifier', built['identifiers'])
        self.assertIn('criticalIdentifier', built['lexicalTerms'])

    def test_identifier_extraction_survives_any_input(self):
        for prompt in ('``` ``` `` ` ...', '((((((((((()))))))', '[[[[]]]] ///// ____ ....',
                       '`' + 'a' * 500 + '`'):
            with self.subTest(prompt=prompt[:20]):
                self.assertIsInstance(kw(prompt)['identifiers'], list)

    def test_adversarial_input_extracts_within_fifty_ms(self):
        # Timing-sensitive (moved as is; listed for the last layer): the
        # UserPromptSubmit hook is blocking.
        samples = {'a': 'a' * 10000, 'paren': '(' * 5000, 'dots': 'a.' * 5000,
                   'log': '2026-07-29 12:00:00 WARN lib/recall.py:123 query_failed retry=3\n' * 140}
        for label, text in samples.items():
            with self.subTest(label=label):
                start = time.perf_counter()
                keywords.build_lexical_terms(text, [])
                self.assertLess((time.perf_counter() - start) * 1000, 50)

    # -- EP variants are independent lexical searches ------------------------------
    def test_bare_number_variants_are_never_lexical_entries(self):
        for prompt in ('EP12 에서 서미래가 먹은 약과 부작용', 'EP 12 무슨 일이 있었는지',
                       '12화 줄거리 정리', 'EP1 EP2 EP3 정리'):
            with self.subTest(prompt=prompt):
                built = kw(prompt, ['ep'])
                for query in built['lexQueries'][1:]:
                    self.assertRegex(query, r'(?i)^EP\d+$')
                self.assertFalse(any(term.isdigit() for term in built['epTerms']))
                self.assertIsNone(re.search(r'(^|\s)\d+(\s|$)', built['lexQueries'][0]))
        self.assertEqual(kw('EP12 복선', ['ep'])['lexQueries'][1:], ['EP012', 'EP12'])

    def test_standalone_particles_are_stopwords(self):
        built = kw('EP12 에서 서미래가 먹은 약과 부작용')
        self.assertNotIn('에서', built['keywords'])
        for word in ('부작용', '먹은', '약과'):
            self.assertIn(word, built['keywords'])


if __name__ == '__main__':
    unittest.main()

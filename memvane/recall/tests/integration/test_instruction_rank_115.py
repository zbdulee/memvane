"""A leading answer-format instruction does not push the exact-token card out of the top three.

``도구는 쓰지 말고 답만 해 줘.`` in front of a question used to fill the general
lexical query's three AND terms (``GENERAL_LEX_TERM_CAP``) with instruction
words: the conjunctive query matched nothing and only the semantic lane, asked
with the instruction, ranked the cards. The question's rare token then decided
nothing. The same question with and without the instruction must inject the
card that holds the token first; the fixture provider gives every card the same
vector, so only the lexical lane can put that card ahead of the others.
"""
from __future__ import annotations

from unittest import mock

from memvane.recall import keywords
from memvane.recall.tests.support import TOPICS, RecallInstallCase

INSTRUCTIONS = ('도구는 쓰지 말고 답만 해 줘.', "Don't use any tools, just answer.")
TARGET = TOPICS[4]


def question(topic=TARGET):
    return f'which protocol keeps the zq{topic}x marker?'


class InstructionRank(RecallInstallCase):
    CARDS = 6

    def setUp(self):
        super().setUp()
        self.install.provider.admit_queries()

    def served(self, prompt):
        _context, _err, event = self.recall(prompt)
        return [card['path'].rsplit('/', 1)[-1] for card in event.get('cards') or []], event

    def test_the_rare_token_card_leads_with_and_without_the_instruction(self):
        plain, event = self.served(question())
        self.assertEqual(plain[:1], [f'{TARGET}.md'], event)
        self.assertNotIn('queryInstructionFree', event)
        for instruction in INSTRUCTIONS:
            for prompt in (instruction + ' ' + question(), instruction + '\n' + question(),
                           question() + ' ' + instruction):
                with self.subTest(prompt=prompt):
                    cards, event = self.served(prompt)
                    self.assertEqual(cards[:1], [f'{TARGET}.md'], event)
                    self.assertGreaterEqual(event.get('lexicalHits'), 1, event)
                    self.assertIs(event.get('queryInstructionFree'), True, event)

    def test_without_the_instruction_filter_the_lexical_lane_finds_nothing(self):
        # The regression this guards: the instruction words alone make the conjunctive query.
        with mock.patch.object(keywords, 'strip_instructions', side_effect=lambda prompt: (prompt, False)):
            _cards, event = self.served(INSTRUCTIONS[0] + ' ' + question())
        self.assertEqual(event.get('lexicalHits'), 0, event)
        self.assertNotIn('queryInstructionFree', event)

    def test_a_question_that_only_mentions_tools_keeps_its_words(self):
        built = keywords.build_lexical_terms('도구 설정 방법은 어디에 있어?', [])
        self.assertFalse(built['instructionFree'])
        self.assertEqual(built['lexQueries'][0], '도구 설정 방법')
        # A prompt that is nothing but the instruction keeps it: there is no question to search for.
        alone = keywords.build_lexical_terms(INSTRUCTIONS[0], [])
        self.assertFalse(alone['instructionFree'])
        self.assertEqual(keywords.semantic_query_text(INSTRUCTIONS[0]), INSTRUCTIONS[0])

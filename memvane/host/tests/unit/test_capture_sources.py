"""Prompt recall keeps no learning capture (design r3 D); edit recall owns capture."""
import unittest

from memvane.host import host_response
from memvane.recall.types import RecallAnswer


class CaptureSources(unittest.TestCase):
    def test_prompt_recall_leaves_the_capture_slot_empty(self):
        payload = {'prompt': 'synthetic prompt'}
        pool = [{'path': 'wiki/a.md'}]
        answer = RecallAnswer({}, capture=({}, pool, []))
        answer.capture_sources = {'wiki/a.md': 'store'}
        with host_response.learning_capture() as slot:
            host_response._render_answer_timed('codex', payload, answer)
        self.assertEqual(payload, {'prompt': 'synthetic prompt'})
        self.assertEqual(slot, [])

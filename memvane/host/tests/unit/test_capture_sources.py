"""Transient recall source metadata reaches capture without mutating hook input."""
import unittest

from memvane.host import host_response
from memvane.recall.types import RecallAnswer


class CaptureSources(unittest.TestCase):
    def test_render_joins_sources_to_the_post_output_capture_payload(self):
        payload = {'prompt': 'synthetic prompt'}
        pool = [{'path': 'wiki/a.md'}]
        answer = RecallAnswer({}, capture=({}, pool, []))
        answer.capture_sources = {'wiki/a.md': 'store'}
        with host_response.learning_capture() as slot:
            host_response._render_answer_timed('codex', payload, answer)
        self.assertEqual(payload, {'prompt': 'synthetic prompt'})
        self.assertEqual(slot[0][2]['_memvane_semantic_sources'], answer.capture_sources)
        self.assertEqual(slot[0][3:], ({}, pool, []))

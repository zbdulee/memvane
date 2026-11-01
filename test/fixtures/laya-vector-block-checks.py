"""Vector fill block streak and SessionStart notice; temp state only."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

CORE = Path(__file__).resolve().parents[2] / 'core'
sys.path.insert(0, str(CORE))
import laya_vector_block as block

CARD = 'proj-wiki/entities/카드.md'


class BlockStreakTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='laya-vector-block-')
        self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve()
        self.project = base / 'project'
        (self.project / '.memvane' / 'wiki').mkdir(parents=True)
        self.state = base / 'state'
        self.state.mkdir(mode=0o700)
        self.decision = {'journalPath': str(self.state / 'install-journal.sqlite'),
                         'projectRoot': str(self.project),
                         'collections': [
                             {'name': 'proj', 'role': 'raw', 'sourceRoot': str(self.project)},
                             {'name': 'proj-wiki', 'role': 'wiki',
                              'sourceRoot': str(self.project / '.memvane' / 'wiki')}]}

    def stuck(self, now, sha='a' * 64, cause='chunk_pos_not_increasing'):
        return block.observe(self.state, {'state': 'deferred', 'reason': 'provider_result_invalid',
                                          'path': CARD, 'hash': sha, 'cause': cause}, now=now)

    def test_same_member_counts_wakes_and_a_new_hash_restarts(self):
        for wake in (1, 2, 3):
            row = self.stuck(100 + wake)
            self.assertEqual((row['wakes'], row['first']), (wake, 101))
        self.assertEqual(oct((self.state / block.FILE).stat().st_mode & 0o777), '0o600')
        row = self.stuck(200, sha='b' * 64)
        self.assertEqual((row['wakes'], row['hash']), (1, 'b' * 64))

    def test_other_deferrals_keep_and_a_completed_sync_clears(self):
        self.stuck(100)
        row = block.observe(self.state, {'state': 'deferred', 'reason': 'laya_sync_yield_priority'})
        self.assertEqual(row['wakes'], 1)
        self.assertIsNone(block.observe(self.state, {'state': 'unchanged'}))
        self.assertIsNone(block.read(self.state))

    def test_an_unknown_cause_keeps_an_earlier_diagnosis(self):
        self.stuck(100)
        row = self.stuck(101, cause='unknown')
        self.assertEqual(row['cause'], 'chunk_pos_not_increasing')

    def test_a_diagnosis_attempt_is_kept_whatever_it_found(self):
        # An attempt that names nothing is still an attempt: the streak must
        # not embed the member again on every wake.
        self.assertIsNone(block.diagnosis_of(self.state, CARD, 'a' * 64))
        block.mark_diagnosed(self.state, CARD, 'a' * 64, 'unknown', now=100)
        self.assertEqual(block.diagnosis_of(self.state, CARD, 'a' * 64), 'unknown')
        row = self.stuck(101, cause='unknown')
        self.assertEqual((row['wakes'], row['diagnosed']), (1, True))
        self.assertEqual(block.diagnosis_of(self.state, CARD, 'a' * 64), 'unknown')
        block.mark_diagnosed(self.state, CARD, 'a' * 64, 'zero_norm_vector')
        self.assertEqual(block.diagnosis_of(self.state, CARD, 'a' * 64), 'zero_norm_vector')
        self.assertIsNone(block.diagnosis_of(self.state, CARD, 'c' * 64))

    def test_a_notice_cannot_restore_a_streak_a_completed_sync_cleared(self):
        import threading
        import time as clock
        for now in (100, 101, 102):
            self.stuck(now)
        real_read = block.read
        cleared = []

        def slow_read(root):
            row = real_read(root)
            # The worker completes a sync while SessionStart holds its row.
            worker = threading.Thread(target=lambda: cleared.append(
                block.observe(self.state, {'state': 'indexed'})))
            worker.start()
            clock.sleep(0.3)
            slow_read.worker = worker
            return row
        block.read = slow_read
        try:
            line = block.notice(self.decision, 's1', now=103)
        finally:
            block.read = real_read
        slow_read.worker.join(10)
        self.assertIsNotNone(line)
        self.assertEqual(cleared, [None])
        self.assertIsNone(block.read(self.state))  # not resurrected by the notice

    def test_notice_sessions_expire_by_time(self):
        for now in (100, 101, 102):
            self.stuck(now)
        self.assertIsNotNone(block.notice(self.decision, 's1', now=200))
        self.assertIsNone(block.notice(self.decision, 's1', now=300))
        later = 200 + block.SESSION_RETENTION_SECONDS
        self.assertIsNotNone(block.notice(self.decision, 's1', now=later))
        self.assertEqual(set(block.read(self.state)['noticed']), {'s1'})

    def test_rejected_member_uses_its_detail_as_the_cause(self):
        row = block.observe(self.state, {'state': 'blocked', 'reason': 'vector_member_rejected',
                                         'path': CARD, 'hash': 'a' * 64,
                                         'detail': 'provider_text_invalid'}, now=1)
        self.assertEqual(row['cause'], 'provider_text_invalid')

    def test_notice_after_three_wakes_once_per_session(self):
        self.stuck(100)
        self.stuck(101)
        self.assertIsNone(block.notice(self.decision, 's1', now=102))
        self.stuck(102)
        line = block.notice(self.decision, 's1', now=103)
        self.assertEqual(line, '[memvane] 위키 카드 .memvane/wiki/entities/카드.md 때문에 '
                               'vector 색인이 멈춰 있어요 (원인: 청크 위치 역행, '
                               'chunk_pos_not_increasing). 카드를 고치거나 알려 주세요.')
        self.assertIsNone(block.notice(self.decision, 's1', now=104))
        self.assertIsNotNone(block.notice(self.decision, 's2', now=104))
        # Cleared when the member embeds (a completed sync).
        block.observe(self.state, {'state': 'indexed'})
        self.assertIsNone(block.notice(self.decision, 's3', now=105))

    def test_notice_after_the_time_threshold_even_with_few_wakes(self):
        self.stuck(100)
        self.assertIsNone(block.notice(self.decision, 's1', now=100 + block.NOTICE_SECONDS - 1))
        self.assertIn('원인: 청크 위치 역행',
                      block.notice(self.decision, 's1', now=100 + block.NOTICE_SECONDS))

    def test_notice_never_raises_on_a_bad_file(self):
        (self.state / block.FILE).write_text('{not json')
        (self.state / block.FILE).chmod(0o600)
        self.assertIsNone(block.notice(self.decision, 's1'))
        self.assertIsNone(block.notice({'journalPath': None}, 's1'))


    def test_session_start_hook_shows_it_through_the_payload_session(self):
        import laya_host_response
        for now in (100, 101, 102):
            self.stuck(now)
        payload = {'session_id': '0b6c1f2e-1111-4222-8333-944445555666'}
        line = laya_host_response._vector_block_notice(self.decision, payload)
        self.assertIn('vector 색인이 멈춰 있어요', line)
        self.assertIsNone(laya_host_response._vector_block_notice(self.decision, payload))


if __name__ == '__main__':
    unittest.main()

"""A direct writer respects an outstanding generation after a released verifier drains its queue."""
import os
from pathlib import Path
from unittest.mock import patch

from memvane.wiki.compile import wiki_compile as writer
from memvane.wiki.verify import api as verify
from memvane.wiki.compile.tests.integration import test_pending_handoff as cases
from tests.support.case import FixtureCase


class CandidateReservations(FixtureCase):
    prepare = cases.PendingHandoff.prepare
    write = cases.PendingHandoff.write
    run_tag = cases.PendingHandoff.run_tag

    current_private_job = cases.PendingHandoff.current_private_job
    native_verify = cases.PendingHandoff.native_verify
    finish_after_downgrade = cases.PendingHandoff.finish_after_downgrade

    def test_released_verifier_leaves_the_unfinished_writers_name_for_native(self):
        replace = os.replace
        for tag in ('v1.0.22', 'v1.0.23', 'v1.0.24', 'v1.1.0'):
            with self.subTest(tag=tag):
                self.prepare(tag)
                def after_queue(src, dst, **kwargs):
                    replace(src, dst, **kwargs)
                    if Path(dst) == self.queue:
                        raise KeyboardInterrupt('published without commit confirmation')
                with patch.object(os, 'replace', side_effect=after_queue), self.assertRaises(KeyboardInterrupt):
                    self.write('Newest source for the first generation.')
                self.assertEqual(self.queue.read_bytes(), b'')
                pending, = self.target.parent.glob('.*.pending-*')
                self.assertEqual(self.run_tag(tag), {'remaining': 0, 'calls': 0})
                self.assertTrue(pending.exists())
                old_bytes = pending.read_bytes()
                self.assertEqual(self.write('Newest source for the second generation.'), 0)
                second = self.current_private_job()
                self.assertNotEqual(pending, self.root / second['pendingPath'])
                if pending.exists():
                    self.assertEqual(pending.read_bytes(), old_bytes)
                self.finish_after_downgrade(tag)
                self.assertEqual(self.write('Newest source for the third generation.'), 0)
                self.finish_after_downgrade(tag)
                self.assertIn(self.project.revision(self.source)['sha256'], self.target.read_text())

    def test_identical_unregistered_image_keeps_its_name_and_bytes(self):
        self.prepare('unregistered')
        decoy = {}
        def intercept(root, config, wiki, queue, job, bound, stage, *, produced_bytes):
            path = root / job['pendingPath']
            path.write_bytes(produced_bytes)
            decoy.update(path=path, data=produced_bytes, inode=path.stat().st_ino)
            return False
        with patch.object(writer, 'now_iso', return_value='2026-01-02T00:00:00Z'):
            with patch.object(verify, 'enqueue_replacement', side_effect=intercept):
                self.assertNotEqual(self.write('Newest source for this decision.'), 0)
            self.assertEqual(self.write('Newest source for this decision.'), 0)
        job = self.current_private_job()
        self.assertNotEqual(self.root / job['pendingPath'], decoy['path'])
        self.assertEqual((decoy['path'].read_bytes(), decoy['path'].stat().st_ino), (decoy['data'], decoy['inode']))
        self.finish_after_downgrade('v1.1.0')
        self.assertEqual(decoy['path'].read_bytes(), decoy['data'])

    def test_native_writer_stages_a_252_byte_final_name(self):
        self.prepare('long-target')
        self.target = self.target.rename(self.target.with_name('a'*223 + '.md'))
        self.assertEqual(self.write('Newest source for the long target.'), 0)
        job = self.current_private_job()
        path = self.root / job['pendingPath']
        self.assertEqual(len(os.fsencode(path.name)), 252)
        self.assertIn(self.project.revision(self.source)['sha256'], path.read_text())
        self.assertEqual(list(self.target.parent.glob('.pc-*')), [])
        self.assertEqual(self.run_tag('v1.1.0'), {'remaining': 0, 'calls': 0})
        self.assertTrue(path.is_file())

    def test_unreadable_reservation_allows_only_a_new_random_marker(self):
        self.prepare('unknown')
        with patch.object(verify, 'stage_candidate', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.write('Newest source for this decision.')
        record, = (self.queue.parent / '.handoff/v1/publication').glob('*/work')
        record.write_bytes(b'unreadable reservation')
        with patch.object(verify, 'pending_name_reserved', wraps=verify.pending_name_reserved) as checked:
            self.assertEqual(self.write('Newest source for this decision.'), 0)
        self.assertEqual([call.kwargs['fresh'] for call in checked.call_args_list], [False, False, True])
        paths = [call.args[1] for call in checked.call_args_list]
        self.assertEqual(len(set(paths)), 3)
        self.assertEqual(self.current_private_job()['pendingPath'], paths[-1])
        self.assertEqual(record.read_bytes(), b'unreadable reservation')
        self.finish_after_downgrade('v1.1.0')

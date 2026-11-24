"""Crash cuts isolate candidate reservations, created inodes, and existing files."""
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from memvane.wiki.verify import candidate_stage as stage
from memvane.wiki.verify.handoff_work import Work
from tests.support.case import FixtureCase


class CandidateStage(FixtureCase):
    data = b'complete candidate\n'

    def fixture(self, name):
        root = self.tmp / name
        root.mkdir(mode=0o700)
        queue = root / 'queue'
        target = 'a'*223 + '.md'
        marker = 'f'*16
        name = '.' + target + '.pending-' + marker
        job = {'targetPath': target, 'pendingPath': name, 'sourceHash': marker}
        work = Work.create(queue, 'publication', {'job': job}, b'original queue\n', image=self.data)
        return root, work, root / name

    def write(self, work, path):
        with stage.producer(work, path, self.data):
            self.assertTrue(stage.write(path, self.data))

    def cancel(self, root, work):
        fresh, = Work.entries(work.queue, 'publication')
        fresh.commit('cancelled')
        return stage.cancel(fresh, root)

    def test_cuts_before_and_after_each_candidate_record(self):
        for phase in ('reserved', 'created', 'ready', 'staged'):
            for after in (False, True):
                with self.subTest(phase=phase, after=after):
                    root, work, path = self.fixture(f'{phase}-{after}')
                    child = subprocess.run([sys.executable, '-B', '-m', __name__, str(work.queue), str(path),
                                            phase, str(int(after))], capture_output=True, text=True)
                    self.assertEqual(child.returncode, 17, child.stderr)
                    fresh, = Work.entries(work.queue, 'publication')
                    temp = list(root.glob('.pc-*'))
                    if phase == 'staged':
                        self.assertFalse(temp)
                        self.assertEqual(path.read_bytes(), self.data)
                    if phase == 'created':
                        self.assertEqual(len(temp), 1)
                        self.assertEqual(temp[0].read_bytes(), b'')
                    uncertain = phase == 'created' and not after
                    self.assertEqual(self.cancel(root, fresh), not uncertain)
                    self.assertFalse(path.exists())
                    self.assertEqual(list(root.glob('.pc-*')), temp if uncertain else [])

    def test_same_bytes_do_not_prove_a_new_writer_and_changed_files_remain(self):
        for case in ('existing', 'edited', 'replaced'):
            with self.subTest(case=case):
                root, work, path = self.fixture(case)
                if case == 'existing':
                    path.write_bytes(self.data)
                    with self.assertRaises(ValueError), stage.producer(work, path, self.data):
                        self.assertFalse(stage.write(path, self.data))
                else:
                    self.write(work, path)
                    self.write(work, path)  # An exact retry under the same durable work.
                if case == 'edited':
                    path.write_bytes(b'manual edit')
                if case == 'replaced':
                    path.rename(root / 'saved-inode')
                    path.write_bytes(self.data)
                before = path.read_bytes(), path.stat().st_ino
                self.assertEqual(self.cancel(root, work), case == 'existing')
                self.assertEqual((path.read_bytes(), path.stat().st_ino), before)

    def test_no_replace_keeps_an_intervening_final_and_removes_only_our_temp(self):
        root, work, path = self.fixture('no-replace')
        publish = stage.rename_noreplace
        def collision(parent, tmp, final):
            path.write_bytes(b'independent')
            return publish(parent, tmp, final)
        with patch.object(stage, 'rename_noreplace', side_effect=collision), self.assertRaises(ValueError):
            with stage.producer(work, path, self.data):
                self.assertFalse(stage.write(path, self.data))
        self.assertTrue(self.cancel(root, work))
        self.assertEqual(path.read_bytes(), b'independent')
        self.assertEqual(list(root.glob('.pc-*')), [])

    def test_cancel_restores_a_foreign_inode_swapped_just_before_detach(self):
        root, work, path = self.fixture('concurrent')
        self.write(work, path)
        rename = stage.rename_noreplace
        def swap(parent, src, dst):
            if src == path.name:
                path.rename(root / 'original')
                path.write_bytes(b'intervening edit')
            return rename(parent, src, dst)
        with patch.object(stage, 'rename_noreplace', side_effect=swap):
            self.assertFalse(self.cancel(root, work))
        self.assertEqual(path.read_bytes(), b'intervening edit')
        self.assertEqual((root / 'original').read_bytes(), self.data)
        self.assertEqual(list(root.glob('.px-*')), [])

    def test_cancel_restarts_on_detached_file_and_leaves_new_final_alone(self):
        root, work, path = self.fixture('restart-cancel')
        self.write(work, path)
        work.commit('cancelled')
        real = work.commit
        def stop(top, **updates):
            if updates['candidate'].get('detached'):
                raise KeyboardInterrupt('after detach')
            return real(top, **updates)
        with patch.object(work, 'commit', side_effect=stop), self.assertRaises(KeyboardInterrupt):
            stage.cancel(work, root)
        self.assertFalse(path.exists())
        held, = root.glob('.px-*')
        self.assertEqual(held.read_bytes(), self.data)
        path.write_bytes(b'new generation')
        self.assertTrue(self.cancel(root, work))
        self.assertFalse(held.exists())
        self.assertEqual(path.read_bytes(), b'new generation')


if __name__ == '__main__':
    queue_name, path_name, phase, after = sys.argv[1:]
    queue, path = Path(queue_name), Path(path_name)
    assert queue.is_relative_to(Path.home()) and path.parent == queue.parent
    work, = Work.entries(queue, 'publication')
    commit = work.commit
    def crash(top, **update):
        if update['candidate']['stage'] == phase:
            if int(after):
                commit(top, **update)
            os._exit(17)
        return commit(top, **update)
    with patch.object(work, 'commit', side_effect=crash), stage.producer(work, path, CandidateStage.data):
        assert stage.write(path, CandidateStage.data)

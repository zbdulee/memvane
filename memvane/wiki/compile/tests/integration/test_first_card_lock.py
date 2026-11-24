"""A released queue holder can keep its lock while other card writers proceed."""
import fcntl
import os
import select
from unittest.mock import patch

from memvane.wiki.compile.tests.integration import test_pending_handoff as fixtures
from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase


class FirstCardLock(FixtureCase):
    prepare = fixtures.PendingHandoff.prepare
    write = fixtures.PendingHandoff.write

    def test_queue_contention_defers_before_publication_and_releases_card_lock(self):
        self.prepare('queue-lock')
        self.target.unlink()
        self.queue.parent.mkdir(exist_ok=True)
        lock_path = workspace.queue_lock_path(self.queue)
        content = 'Newest source while the released queue lock is held.'
        flock = fcntl.flock
        with lock_path.open('a') as held:
            flock(held, fcntl.LOCK_EX)
            reader, writer = os.pipe()
            pid = os.fork()
            if pid == 0:
                try:
                    os.close(reader)
                    held.close()

                    def observe(fd, flags):
                        fileno = fd if isinstance(fd, int) else fd.fileno()
                        if flags & fcntl.LOCK_EX and os.path.samestat(os.fstat(fileno), lock_path.stat()):
                            os.write(writer, str(flags).encode())
                        return flock(fd, flags)

                    with patch.object(fcntl, 'flock', side_effect=observe):
                        code = self.write(content)
                    os._exit(code)
                except BaseException:
                    os._exit(29)
            os.close(writer)
            try:
                self.assertTrue(select.select([reader], [], [], 10)[0])
                self.assertEqual(os.read(reader, 20), str(fcntl.LOCK_EX | fcntl.LOCK_NB).encode())
                # EOF means the child has returned, while the legacy lock is still held.
                self.assertTrue(select.select([reader], [], [], 10)[0])
                self.assertEqual(os.read(reader, 20), b'')
                with workspace.card_write_lock(self.root, blocking=False):
                    self.assertFalse(self.target.exists())
                self.assertFalse(self.queue.with_name('generated-manifest.jsonl').exists())
            finally:
                flock(held, fcntl.LOCK_UN)
                os.close(reader)
                _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 1)
        self.assertEqual(self.write(content), 0)
        self.assertTrue(self.target.is_file())
        self.assertEqual(len(self.queue.with_name('generated-manifest.jsonl').read_bytes().splitlines()), 1)

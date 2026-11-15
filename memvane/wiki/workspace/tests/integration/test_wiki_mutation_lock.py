"""Compile and verification writes share the reentrant project wiki lock."""
import subprocess
import sys

from memvane.wiki.verify import api as verify_api
from memvane.wiki.workspace import api as workspace_api
from tests.support.case import FixtureCase


CHILD = '''import fcntl, os, sys
from pathlib import Path
from memvane.wiki.verify import api as verify
from memvane.wiki.workspace import api as workspace
root, target, marker, action = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
fd = os.open(root / '.topical-publish.lock', os.O_RDWR | os.O_NOFOLLOW)
try:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        marker.write_text('locked')
    else:
        marker.write_text('unexpectedly-unlocked')
        fcntl.flock(fd, fcntl.LOCK_UN)
finally:
    os.close(fd)
if action == 'compile':
    assert workspace.write_text_atomic(target, 'after\\n')
else:
    assert verify.stamp_verification(target, 'verified', 'synthetic', 'self', None)
'''


class MutationLock(FixtureCase):
    def test_cross_process_writers_block_and_nested_writes_are_reentrant(self):
        wiki = self.home / '.memvane' / 'wiki'
        wiki.mkdir(parents=True)
        index, card = wiki / 'index.md', wiki / 'card.md'
        index.write_text('before\n')
        card.write_text('---\ntitle: Synthetic\nstatus: generated\n---\nSynthetic body.\n')
        for action, target in (('compile', index), ('verify', card)):
            with self.subTest(action=action):
                before = target.read_text()
                marker = self.home / (action + '.marker')
                with workspace_api.mutation_lock(self.home):
                    child = subprocess.Popen([sys.executable, '-B', '-c', CHILD, str(self.home),
                                              str(target), str(marker), action],
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    self.addCleanup(lambda proc=child: proc.kill() if proc.poll() is None else None)
                    self.wait_until(marker.exists, timeout=10)
                    self.assertEqual(marker.read_text(), 'locked')
                    self.assertEqual(target.read_text(), before)
                out, err = child.communicate(timeout=10)
                self.assertEqual(child.returncode, 0, (out, err))
        self.assertEqual(index.read_text(), 'after\n')
        self.assertIn('status: verified', card.read_text())
        with workspace_api.mutation_lock(self.home):
            self.assertTrue(workspace_api.write_text_atomic(index, 'nested\n'))
            self.assertTrue(verify_api.stamp_verification(card, 'contested', 'synthetic', 'self', None))
        self.assertEqual(index.read_text(), 'nested\n')
        self.assertIn('status: contested', card.read_text())

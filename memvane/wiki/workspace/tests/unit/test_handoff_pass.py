"""One native compile invocation shares an allowance with synchronous children and reuses stable evidence."""
import subprocess
import sys

from memvane.wiki.workspace.handoff import api
from tests.support.case import FixtureCase


class HandoffPass(FixtureCase):
    def test_child_failure_cannot_reissue_spent_parent_bytes(self):
        with api.budget.scope() as budget:
            api.charge('source', 2048)
            script = ("from memvane.wiki.workspace.handoff import api; "
                      "f=api.bounded(lambda: api.charge('source', 65536)); f(); raise SystemExit(17)")
            child = subprocess.run([sys.executable, '-B', '-c', script], **api.child_options())
            self.assertEqual(child.returncode, 17)
            left = api.MAX_BYTES - 2048 - 65536
            self.assertEqual(budget.available('source'), left)
            with self.assertRaises(BlockingIOError):
                api.charge('source', left + 1)
            self.assertEqual(budget.available('source'), left)
            api.charge('source', left)
            self.assertEqual(budget.available('verify'), api.MAX_BYTES)
        with api.budget.scope() as next_pass:
            self.assertEqual(next_pass.available('source'), api.MAX_BYTES)

    def test_4096_original_offsets_and_changed_in_place_snapshot(self):
        path = self.tmp / 'source-queue.jsonl'
        path.write_bytes(b'{}\n' * 4096)
        with api.budget.scope() as budget:
            self.assertEqual(sum(1 for _ in api.bounded_rows(path)), 4096)
            self.assertEqual(sum(1 for _ in api.bounded_rows(path)), 4096)
            self.assertEqual((budget.available('source_rows'), budget.available('source')),
                             (0, api.MAX_BYTES - 2 * path.stat().st_size))
            with path.open('ab') as output:
                output.write(b'{}\n')
            with self.assertRaises(OSError):
                list(api.bounded_rows(path))
        body = self.tmp / 'sample.md'
        with api.budget.scope() as budget:
            body.write_bytes(b'x' * 1024)
            material = api.evidence(self.tmp, body.name, 1024)
            self.assertEqual(api.evidence(self.tmp, body.name, 1024), material)
            left = budget.available('evidence')
            body.write_bytes(b'y' * 1024)
            self.assertEqual(api.evidence(self.tmp, body.name, 1024)[0], b'y' * 1024)
            self.assertEqual(budget.available('evidence'), left - 1024)

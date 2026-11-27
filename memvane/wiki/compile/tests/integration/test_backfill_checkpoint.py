"""A frozen backfill item keeps its paid answer in ``backfill_checkpoints``, never under ``.handoff``."""
from __future__ import annotations

from memvane.wiki.compile import backfill_work
from memvane.wiki.pipeline import api as pipeline
from tests.support.case import FixtureCase
from tests.support.project import make_project


class BackfillCheckpoint(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)
        self.queue = self.project.root / '.memvane/compile/source-queue.jsonl'
        self.queue.parent.mkdir(parents=True, exist_ok=True)
        self.job = {'cwd': str(self.project.root), 'engine': 'codex', 'source': {'path': 'docs/a.md'}}
        self.item = ['cohort-1', 'raw/docs/a.md', 'a' * 64]

    def open(self, *, resume_only=False):
        return backfill_work.FrozenWork.open(self.queue, '{"line": 1}', dict(self.job), self.item,
                                             resume_only=resume_only)

    def rows(self):
        with pipeline.open_pipeline(self.project.root, readonly=True) as store:
            return store._conn.execute('SELECT COUNT(*) FROM backfill_checkpoints').fetchone()[0]

    def test_answer_survives_a_new_process_and_settle_removes_the_row(self):
        self.assertIsNone(self.open(resume_only=True))  # nothing to resume
        work = self.open()
        self.assertFalse(work.has_answer)
        self.assertEqual(self.rows(), 1)
        self.assertEqual(work.prepare({'candidates': [{'title': 'T'}]}, 'codex'), {'candidates': [{'title': 'T'}]})
        calls = []
        result = work.write_sibling('k1', lambda: calls.append(1) or {'action': 'created', 'targetPath': 'x'})
        resumed = self.open(resume_only=True)
        self.assertTrue(resumed.has_answer)
        self.assertEqual(resumed.answer(), {'candidates': [{'title': 'T'}]})
        # The recorded sibling write is replayed, not written again.
        self.assertEqual(resumed.write_sibling('k1', lambda: calls.append(2)), result)
        self.assertEqual(calls, [1])
        resumed.settled()
        self.assertEqual(self.rows(), 0)
        self.assertIsNone(self.open(resume_only=True))
        self.assertFalse((self.queue.parent / '.handoff').exists())

    def test_a_failed_answer_write_keeps_the_claimed_stage(self):
        work = self.open()
        with pipeline.open_pipeline(self.project.root) as store:
            store._conn.execute("CREATE TRIGGER refuse BEFORE UPDATE ON backfill_checkpoints "
                                "BEGIN SELECT RAISE(ABORT, 'disk'); END")
        with self.assertRaises(OSError):
            work.prepare({'candidates': []}, 'codex')
        with pipeline.open_pipeline(self.project.root) as store:
            store._conn.execute('DROP TRIGGER refuse')
        resumed = self.open(resume_only=True)
        self.assertFalse(resumed.has_answer)  # no answer without its stage, so the next pass re-extracts

    def test_a_corrupt_checkpoint_is_refused(self):
        self.open()
        with pipeline.open_pipeline(self.project.root) as store:
            key, = [r[0] for r in store._conn.execute('SELECT binding_digest FROM backfill_checkpoints')]
            row = store.backfill_checkpoint(key)
            store.put_backfill_checkpoint(key, row['state'], b'other bytes')
        with self.assertRaises(OSError):
            self.open(resume_only=True)

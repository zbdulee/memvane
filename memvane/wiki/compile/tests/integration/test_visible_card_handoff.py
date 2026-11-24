"""Released and native verifiers meet different ownership conditions on the same visible paths."""
from memvane.wiki.compile.tests.integration import test_pending_handoff as fixtures
from tests.support.case import FixtureCase


TAGS = ('v1.0.22', 'v1.0.23', 'v1.0.24', 'v1.1.0')


class VisibleCardHandoff(FixtureCase):
    prepare = fixtures.PendingHandoff.prepare
    write = fixtures.PendingHandoff.write
    current_private_job = fixtures.PendingHandoff.current_private_job
    run_tag = fixtures.PendingHandoff.run_tag
    native_verify = fixtures.PendingHandoff.native_verify
    finish_after_downgrade = fixtures.PendingHandoff.finish_after_downgrade

    def test_first_stamp_and_replacements_wait_for_native_on_every_release(self):
        for tag in TAGS:
            for kind in ('new', 'generated', 'verified'):
                with self.subTest(tag=tag, kind=kind):
                    self.prepare(tag + '-' + kind)
                    if kind == 'new':
                        self.target.unlink()
                    elif kind == 'generated':
                        self.target.write_text(self.target.read_text().replace('status: verified', 'status: generated'))
                    old = self.target.read_bytes() if self.target.exists() else None
                    self.assertEqual(self.write('Newest source for an editable decision.'), 0)
                    if old is not None:
                        self.assertEqual(self.target.read_bytes(), old)
                    job = self.current_private_job()
                    self.assertEqual('pendingPath' in job, kind != 'new')
                    self.assertFalse(self.queue.exists() and self.queue.read_bytes())
                    self.finish_after_downgrade(tag)
                    result = self.target.read_text()
                    self.assertIn('status: verified', result)
                    self.assertIn(self.project.revision(self.source)['sha256'], result)
                    self.assertEqual(self.native_verify(), {'calls': 0, 'remaining': 0})

    def test_source_update_before_first_review_transfers_the_first_card_obligation(self):
        for tag in TAGS:
            with self.subTest(tag=tag):
                self.prepare(tag)
                self.target.unlink()
                self.assertEqual(self.write('Newest source, original decision.'), 0)
                initial = self.target.read_bytes()
                first = self.current_private_job()
                self.assertNotIn('pendingPath', first)
                self.assertEqual(self.write('Newest source, revised decision.'), 0)
                self.assertEqual(self.target.read_bytes(), initial)
                self.assertFalse(self.queue.exists() and self.queue.read_bytes())
                replacement = self.current_private_job()
                self.assertNotEqual(first['sourceRevisions'], replacement['sourceRevisions'])
                self.assertIn('pendingPath', replacement)
                self.finish_after_downgrade(tag)
                self.assertIn(self.project.revision(self.source)['sha256'], self.target.read_text())
                self.assertEqual(self.native_verify(), {'calls': 0, 'remaining': 0})

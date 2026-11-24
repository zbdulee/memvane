"""One ordinary verifier can run while 65 released claims keep a replacement private."""
import hashlib
import json
import os

from memvane.wiki.compile.tests.integration import test_pending_handoff as fixtures
from tests.support.wiki_handoff import deferred_replacements, full_copy_quota
from tests.support.case import FixtureCase


class BoundedHandoff(FixtureCase):
    prepare = fixtures.PendingHandoff.prepare
    write = fixtures.PendingHandoff.write
    native_verify = fixtures.PendingHandoff.native_verify

    def test_full_copy_quota_keeps_replacement_retryable_and_completes_an_independent_new_card(self):
        self.prepare('full')
        self.queue.parent.mkdir(parents=True, exist_ok=True)
        retained = full_copy_quota(self.queue)
        before = self.target.read_bytes()
        self.assertNotEqual(self.write('Newest source, replacement blocked.'), 0)
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(list(self.target.parent.glob('.*.pending-*')), [])
        prior = self.target
        self.target = prior.with_name('independent.md')
        self.assertEqual(self.write('Newest source, independent card.'), 0)
        self.assertTrue(self.target.is_file())
        self.assertEqual(self.native_verify(), {'remaining': 0, 'calls': 1})
        self.assertIn('status: verified', self.target.read_text())
        self.assertEqual(self.native_verify()['calls'], 0)
        self.target = prior
        self.assertEqual(self.target.read_bytes(), before)
        record = retained[0]
        self.assertEqual(record.read('raw'), bytes(8 * 1024 ** 2))
        record.release()  # The fixture has independently retained these exact bytes.
        self.assertEqual(self.write('Newest source, replacement retry.'), 0)
        self.assertEqual(self.native_verify(), {'remaining': 0, 'calls': 1})
        self.assertIn(self.project.revision(self.source)['sha256'], prior.read_text())

    def test_same_target_either_side_of_eight_mib_with_an_independent_ordinary_neighbor(self):
        from tests.support.wiki_handoff import retained_batches
        self.prepare('opposite-ends')
        before = self.target.read_bytes()
        target = self.target.relative_to(self.root).as_posix()

        def stage(marker, path, *, hidden):
            revision = self.project.revision(self.source)
            src = {k: revision[k] for k in ('kind', 'path', 'collection')}
            job = dict(targetPath=path.relative_to(self.root).as_posix(), engine='claude', sourceHash=marker,
                       sources=[src], sourceRevisions=[revision])
            if hidden:
                path = path.with_name('.' + path.name + '.pending-' + marker)
                job.update(pendingPath=path.relative_to(self.root).as_posix(),
                           expectedTargetHash=hashlib.sha256(before).hexdigest())
            path.write_text(fixtures.writer.markdown_page(dict(title='Decision', sources=[src], sourceRevisions=[revision]),
                                                         'Decision in the source.', 'generated', [], marker))
            return json.dumps(job).encode() + b'\n'

        old = stage('1'*16, self.target, hidden=True)
        self.source.write_text('Newest source for the second pending.')
        new = stage('2'*16, self.target, hidden=True)
        neighbor = self.target.with_name('independent.md')
        ordinary = stage('3'*16, neighbor, hidden=False)
        raw = ordinary + old + b'!' * (8 * 1024 ** 2) + b'\n' + new
        self.queue.parent.mkdir(exist_ok=True, parents=True)
        self.queue.write_bytes(raw)
        paid = 0
        witnessed_early = False
        for _ in range(300):
            paid += self.native_verify()['calls']
            batch, = retained_batches(self.queue)
            if batch.cursor < len(raw) and 'status: verified' in neighbor.read_text():
                witnessed_early = True
                self.assertEqual(self.target.read_bytes(), before)
            if self.project.revision(self.source)['sha256'] in self.target.read_text():
                break
        self.assertTrue(witnessed_early)
        self.assertEqual(paid, 2)
        self.assertEqual(target, json.loads(new)['targetPath'])
        self.assertIn(self.project.revision(self.source)['sha256'], self.target.read_text())
        self.assertEqual((batch.work.folder / 'raw').read_bytes(), raw)
        self.assertEqual(self.queue.read_bytes(), b'')

    def test_unobserved_claim_defers_hidden_and_consumes_independent_ordinary_once(self):
        self.prepare('unknown')
        self.source.write_text('Newest source, independent ordinary evidence.')
        revision = self.project.revision(self.source)
        src = {k: revision[k] for k in ('kind', 'path', 'collection')}
        marker = '1'*16
        ordinary = self.target.with_name('ordinary.md')
        job = dict(targetPath=ordinary.relative_to(self.root).as_posix(), sourceHash=marker, engine='claude',
                   sources=[src], sourceRevisions=[revision])
        ordinary.write_text(fixtures.writer.markdown_page(dict(title='Ordinary', sources=[src], sourceRevisions=[revision]),
                                                          'Decision in this source.', 'generated', [], marker))
        self.queue.parent.mkdir(exist_ok=True, parents=True)
        original = json.dumps(job).encode() + b'\r\n'
        self.queue.write_bytes(original)
        claims = [self.queue.with_name(f'{self.queue.name}.claimed.{os.getpid()}.{i}') for i in range(65)]
        for claim in claims:
            claim.write_bytes(b'')
        held_ref = json.dumps({'targetPath': self.target.relative_to(self.root).as_posix(),
                               'pendingPath': 'preserve-this-hidden-name'}).encode() + b'\n'
        claims[-1].write_bytes(held_ref)
        prior = self.target.read_bytes()
        self.assertEqual(self.write(self.source.read_text()), 0)
        held, = deferred_replacements(self.queue)
        candidate = self.root / held.state['binding']['job']['pendingPath']
        self.assertFalse(candidate.exists())
        self.assertTrue(held.read('image'))
        self.assertEqual(self.native_verify()['calls'], 1)
        self.assertIn('status: verified', ordinary.read_text())
        self.assertEqual(self.native_verify()['calls'], 0)
        self.assertFalse(candidate.exists())
        self.assertEqual(self.target.read_bytes(), prior)
        self.assertEqual(self.queue.read_bytes(), original)
        self.assertEqual(claims[-1].read_bytes(), held_ref)
        for claim in claims:
            claim.unlink()
        self.assertEqual(self.native_verify(), {'remaining': 0, 'calls': 1})
        self.assertFalse(deferred_replacements(self.queue))
        self.assertIn(revision['sha256'], self.target.read_text())
        self.assertEqual(self.native_verify(), {'remaining': 0, 'calls': 0})

"""Compile intent consumption: failures end the pass or reject by the review rules.

Real intent files, renames and cursor writes in a private temporary state
root; a fake store records what the pass writes. Card parity and witness
evidence are covered by tests/e2e/wiki_db_provenance.py; here they are
stubbed so each case isolates one rule (Fable PR8 F1-F10).
"""
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from memvane.kernel.atomic_rename import canonical_json_bytes
from memvane.kernel.resource_deferred import ResourceDeferred
from memvane.store import types as store_types
from memvane.sync import compile_intents as ci
from memvane.wiki.verify.api import compile_intent_id

VIEW, WORKTREE, BRANCH = 'v' * 64, '0' * 8 + '-0000-4000-8000-' + '0' * 12, 'refs/heads/main'


class FakeStore:
    def __init__(self, failure=None, during=None):
        self.failure, self.during, self.writes = failure, during, []

    def put_compiled_attestation(self, worktree_id, intent, *, policy_digest):
        if self.during:
            self.during()
        if self.failure:
            raise store_types.BlobStoreError(self.failure)
        self.writes.append(intent)


class CompileIntentRules(unittest.TestCase):
    def setUp(self):
        self.state = Path(os.path.realpath(tempfile.mkdtemp(prefix='mvi-', dir='/tmp')))
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)
        self.state.chmod(0o700)
        self.dir = self.state / 'provenance-intents'
        self.dir.mkdir(mode=0o700)
        self.handle = SimpleNamespace(view_id=VIEW, worktree_id=WORKTREE, branch_key=BRANCH,
                                      policy_digest='p' * 64)
        self.members, self.current = {}, {}
        for target, value in (('_state', lambda path: path), ):
            patcher = mock.patch('memvane.journal.journal_reader.' + target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (('_parity', lambda intent, image: True),
                            ('_stored', lambda store, handle, intent: True)):
            patcher = mock.patch.object(ci, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.marks = []

    def intent(self, card, *, observed=10, scan='s1', fingerprints=None, extra=None, published=True):
        target = 'concepts/%s.md' % card[:6]
        value = {'kind': 'compile', 'viewId': VIEW, 'targetPath': target, 'cardSha': card,
                 'expectedTargetHash': None,
                 'sourceFingerprints': {'scanId': scan, 'observedNs': observed, 'fingerprints':
                     fingerprints if fingerprints is not None else
                     [{'worktree_id': WORKTREE, 'branch_key': BRANCH, 'path': 'notes.md'}]},
                 'sourceSpans': [], 'verifiedMode': 'cross-engine', 'verifyEngine': 'claude',
                 'producingEngine': 'codex', 'verdict_digest': 'd' * 64, 'result_digest': 'r' * 64,
                 **(extra or {})}
        if published:
            self.members[target] = {'blob_sha': card}
            self.current[target] = (card, b'card')
        return value

    def write(self, value, suffix='.json'):
        path = self.dir / ('compile-' + compile_intent_id(value) + suffix)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, canonical_json_bytes(value))
        os.close(fd)
        return path

    def consume(self, store, **kwargs):
        options = dict(mark=lambda: self.marks.append(1), validate=lambda: None, ingest_limit=64)
        options.update(kwargs)
        return ci.consume(store, {'journalPath': str(self.state / 'journal.sqlite')}, self.handle,
                          self.members, self.current, **options)

    def outcomes(self):
        return sorted(p.name.split('.', 1)[1] for p in self.dir.iterdir())

    # F1: storage and I/O failures are not verdicts on the intent.
    def test_transient_store_failures_end_the_pass_and_keep_the_intent_pending(self):
        for reason in ('candidate_storage_full', 'candidate_io_failed', 'candidate_file_changed',
                       'candidate_binding_changed', 'reader_write_forbidden'):
            with self.subTest(reason=reason):
                path = self.write(self.intent(reason.encode().hex()[:64].ljust(64, 'a')))
                with self.assertRaises(store_types.BlobStoreError):
                    self.consume(FakeStore(reason))
                self.assertTrue(path.exists())
                path.unlink()
        self.assertEqual(self.outcomes(), [])

    def test_value_and_conflict_reasons_reject_with_their_codes(self):
        self.write(self.intent('a' * 64))
        result = self.consume(FakeStore('invalid_compile_result'))
        self.assertEqual(result['rejected'], {'provenance_value_invalid': 1})
        self.write(self.intent('b' * 64))
        result = self.consume(FakeStore('card_attestation_conflict'))
        self.assertEqual(result['rejected'], {'provenance_conflict': 1})
        self.assertEqual(self.outcomes(), ['rejected.json', 'rejected.json'])

    # F2: fence and writer checks are outside the content verdict.
    def test_fence_and_writer_failures_never_reject(self):
        path = self.write(self.intent('c' * 64))
        for failure in (ResourceDeferred('busy'), json.JSONDecodeError('x', 'y', 0), KeyError('k')):
            with self.subTest(failure=type(failure).__name__):
                def boom(failure=failure):
                    raise failure
                with self.assertRaises(type(failure)):
                    self.consume(FakeStore(), mark=boom)
                with self.assertRaises(type(failure)):
                    self.consume(FakeStore(), validate=boom)
        self.assertTrue(path.exists())

    # F3: odd files and a loose directory never end the pass.
    def test_unsafe_files_are_counted_and_the_valid_intent_is_consumed(self):
        good = self.intent('d' * 64)
        self.write(good)
        os.symlink('/dev/null', self.dir / ('compile-' + 'e' * 64 + '.json'))
        locked = self.write(self.intent('f' * 64))
        locked.chmod(0o000)
        self.addCleanup(locked.chmod, 0o600)
        store = FakeStore()
        result = self.consume(store)
        self.assertEqual((result['consumed'], result['unsafe']), (1, 2))
        self.assertEqual([w['cardSha'] for w in store.writes], [good['cardSha']])

    def test_malformed_fingerprints_reject_instead_of_raising(self):
        self.write(self.intent('1' * 64, fingerprints=['x']))
        self.write(self.intent('2' * 64, fingerprints={'path': 'x'}))
        result = self.consume(FakeStore())
        self.assertEqual(result['rejected'], {'provenance_value_invalid': 2})

    def test_loose_intent_directory_is_skipped_without_ending_the_pass(self):
        path = self.write(self.intent('3' * 64))
        self.dir.chmod(0o755)
        result = self.consume(FakeStore())
        self.assertEqual((result['consumed'], result['unsafe']), (0, 1))
        self.assertTrue(path.exists())

    # F4: an intent superseded during the write is left to its new name.
    def test_intent_renamed_away_meanwhile_does_not_end_the_pass(self):
        first = self.write(self.intent('4' * 64))
        self.write(self.intent('5' * 64))
        renamed = []

        def supersede():
            if first.exists():
                first.rename(first.with_name(first.stem + '.superseded.json'))
                renamed.append(1)
        result = self.consume(FakeStore(during=supersede))
        self.assertEqual((len(renamed), result['consumed']), (1, 1))
        self.assertEqual(self.outcomes(), ['consumed.json', 'superseded.json'])

    # F5/F6: the sweep keeps the current card's intent and clears equal-time orphans.
    def test_sweep_never_supersedes_the_intent_for_the_published_bytes(self):
        witness = self.intent('6' * 64, observed=50, published=False)
        self.write(witness, '.consumed.json')
        current = self.intent('7' * 64, observed=40, scan='s2')
        current['targetPath'] = witness['targetPath']
        self.members = {witness['targetPath']: {'blob_sha': current['cardSha']}}
        self.current = {witness['targetPath']: (current['cardSha'], b'card')}
        self.write(current)
        result = self.consume(FakeStore())
        self.assertEqual((result['superseded'], result['consumed']), (0, 1))

    def test_sweep_supersedes_an_unpublished_intent_with_the_same_observation(self):
        witness = self.intent('8' * 64, observed=50, published=False)
        self.write(witness, '.consumed.json')
        orphan = self.intent('9' * 64, observed=50, published=False)
        orphan['targetPath'] = witness['targetPath']
        self.write(orphan)
        result = self.consume(FakeStore())
        self.assertEqual((result['superseded'], result['waiting']), (1, 0))

    # Verify R4: stamp, death, consumption meanwhile and a delayed ingest keep the current card's intent.
    def test_sweep_keeps_the_intent_for_the_card_bytes_on_disk_while_ingest_waits(self):
        first = self.intent('a' * 64, observed=50)
        target = first['targetPath']
        self.write(first)
        self.assertEqual(self.consume(FakeStore())['consumed'], 1)  # consumed before the job reruns
        second = self.intent('b' * 64, observed=50, published=False)
        second['targetPath'] = target
        self.write(second)
        # The card on disk is the second stamp; the pass has not ingested it yet.
        self.members = {target: {'blob_sha': first['cardSha']}}
        self.current = {target: (second['cardSha'], b'card')}
        for _ in range(2):
            result = self.consume(FakeStore())
            self.assertEqual((result['superseded'], result['waiting'], result['consumed']), (0, 1, 0))
        self.members = {target: {'blob_sha': second['cardSha']}}
        result = self.consume(FakeStore())
        self.assertEqual((result['superseded'], result['waiting'], result['consumed']), (0, 0, 1))
        self.assertEqual(self.outcomes(), ['consumed.json', 'consumed.json'])

    # F7: the cursor keeps a bounded number of views.
    def test_cursor_keeps_at_most_the_view_bound(self):
        from memvane.journal.api import ledger_read_json, ledger_write
        cursor = self.state / 'provenance-intents.cursor'
        ledger_write(cursor, {'views': {'%064x' % n: {'last': ''} for n in range(300)}}, replace=False)
        self.write(self.intent('a1'.ljust(64, '0')))
        self.consume(FakeStore())
        views = ledger_read_json(cursor)['views']
        self.assertEqual(len(views), ci.CURSOR_VIEWS)
        self.assertIn(VIEW, views)

    # F8: a file still being linked is counted, not silently skipped.
    def test_unsettled_link_is_counted_as_unsafe(self):
        path = self.write(self.intent('b1'.ljust(64, '0')))
        os.link(path, self.state / 'temp-link')
        result = self.consume(FakeStore())
        self.assertEqual((result['unsafe'], result['consumed']), (1, 0))
        self.assertTrue(path.exists())

    # Verify R6: an orphaned link or unopenable file is counted once, never touched.
    def test_unsafe_files_count_once_per_name_and_inode(self):
        orphan = self.write(self.intent('b2'.ljust(64, '0'), published=False))
        os.link(orphan, self.state / 'dead-writer-temp')
        target = self.intent('b3'.ljust(64, '0'), published=False)
        os.symlink(self.state / 'elsewhere', self.dir / ('compile-' + compile_intent_id(target) + '.json'))
        counts = [self.consume(FakeStore())['unsafe'] for _ in range(3)]
        self.assertEqual(counts, [2, 0, 0])
        self.assertEqual(os.stat(orphan).st_nlink, 2)
        # The same name with another inode is a new file: counted again.
        os.unlink(orphan)
        orphan = self.write(self.intent('b2'.ljust(64, '0'), published=False))
        os.link(orphan, self.state / 'dead-writer-temp-2')
        self.assertEqual([self.consume(FakeStore())['unsafe'] for _ in range(2)], [1, 0])
        # Once it settles it is read like any intent; the next pass drops it from the cursor.
        os.unlink(self.state / 'dead-writer-temp-2')
        self.members[target['targetPath']] = {'blob_sha': 'x' * 64}
        self.members['concepts/b20000.md'] = {'blob_sha': 'b2'.ljust(64, '0')}
        self.current['concepts/b20000.md'] = ('b2'.ljust(64, '0'), b'card')
        result = self.consume(FakeStore())
        self.assertEqual((result['unsafe'], result['consumed']), (0, 1))
        self.assertEqual(self.consume(FakeStore())['unsafe'], 0)
        from memvane.journal.api import ledger_read_json
        kept = ledger_read_json(self.state / 'provenance-intents.cursor')['views'][VIEW]['unsafe']
        self.assertEqual([key.split(':')[0] for key in kept],
                         ['compile-' + compile_intent_id(target) + '.json'])

    # F9: a cursor that cannot be written does not end the pass.
    def test_cursor_write_failure_keeps_the_result(self):
        (self.state / 'provenance-intents.cursor').mkdir(mode=0o700)
        self.write(self.intent('c1'.ljust(64, '0')))
        result = self.consume(FakeStore())
        self.assertEqual(result['consumed'], 1)

    # F10: keys added by a later 1.x producer are ignored.
    def test_extra_keys_are_ignored_and_never_reach_the_store(self):
        self.write(self.intent('d1'.ljust(64, '0'), extra={'futureField': 1}))
        store = FakeStore()
        result = self.consume(store)
        self.assertEqual(result['consumed'], 1)
        self.assertEqual(set(store.writes[0]), set(ci.REQUIRED_KEYS))

    def test_missing_required_key_rejects(self):
        value = self.intent('e1'.ljust(64, '0'))
        del value['producingEngine']
        self.write(value)
        self.assertEqual(self.consume(FakeStore())['rejected'], {'provenance_value_invalid': 1})


if __name__ == '__main__':
    unittest.main()

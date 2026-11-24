"""Crash and evidence boundaries for importing an unpublished e8 source ledger."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import types
from unittest.mock import patch

from memvane.wiki.verify import handoff_work as work, legacy_handoff, pending_ownership as ownership
from tests.support.case import FixtureCase


def _reader_child(queue, event):
    def stop():
        os._exit(78)
    create = work.Work.create
    commit = work.Work.commit
    save = legacy_handoff._save
    def new(*args, **kwargs):
        if event == 'before-reserve':
            stop()
        result = create(*args, **kwargs)
        if event == 'reserved':
            stop()
        return result
    def advance(item, phase, **updates):
        if event == 'before-' + phase:
            stop()
        result = commit(item, phase, **updates)
        if event == phase:
            stop()
        return result
    def publish(*args):
        if event == 'before-reference':
            stop()
        save(*args)
        if event == 'reference':
            stop()
    with patch.object(work.Work, 'create', side_effect=new), patch.object(work.Work, 'commit', advance), \
         patch.object(legacy_handoff, '_save', side_effect=publish):
        print(json.dumps(ownership.load(Path(queue))))


class LegacyHandoff(FixtureCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / 'project'
        self.queue = self.root / '.memvane/compile/verify-queue.jsonl'
        self.queue.parent.mkdir(parents=True)
        self.rel = 'wiki/.card.md.pending-' + 'a' * 16
        self.candidate = self.root / self.rel
        self.candidate.parent.mkdir()
        self.candidate.write_bytes(b'candidate proof')
        self.sha = hashlib.sha256(self.candidate.read_bytes()).hexdigest()
        self.source = self.queue.with_name(ownership.NAME)
        self.job = {'targetPath': 'wiki/card.md', 'pendingPath': self.rel, 'sourceHash': 'a' * 16,
                    'expectedTargetHash': 'b' * 64, 'engine': 'claude', 'ts': '2026-11-01T00:00:00Z',
                    'sourceRevisions': [{'kind': 'file', 'path': 'docs/a.md', 'collection': 'docs',
                                         'sha256': 'c' * 64, 'size': 23, 'mtimeNs': 7}]}
        self.plan = {'job': self.job, 'sha256': self.sha, 'reason': 'pending_source_changed',
                     'swap': '.verify-swap-abcdefgh'}
        repo = Path(__file__).resolve().parents[5]
        path = 'memvane/wiki/verify/pending_ownership.py'
        src = subprocess.check_output(['git', 'show', 'e8fdfdaaf1ab1ba8cd3787c88dc0b6335d35ffbf:' + path], cwd=repo)
        self.old = types.ModuleType('e8_ownership')
        exec(compile(src, 'e8:' + path, 'exec'), self.old.__dict__)

    def seed(self, *, plan=False):
        original = {'owners': {self.rel: self.sha}, 'retirements': {self.rel: self.plan} if plan else {}}
        self.old.store(self.queue, original)
        self.assertEqual(self.old.load(self.queue), original)
        return self.source.read_bytes()

    def imported(self):
        index = legacy_handoff._index(self.queue)
        identity, = index['imports'].values()
        return work.Work.lookup(self.queue, 'legacy', identity)

    def test_import_survives_each_boundary_and_never_replays_a_finished_owner(self):
        original = self.seed()
        for i, boundary in enumerate(('before-reserve', 'reserved', 'before-imported', 'imported',
                                      'before-reference', 'reference', 'before-finished', 'finished')):
            with self.subTest(boundary=boundary):
                root = self.tmp / ('case-' + str(i))
                candidate = root / self.rel
                candidate.parent.mkdir(parents=True)
                candidate.write_bytes(self.candidate.read_bytes())
                q = root / '.memvane/compile/verify-queue.jsonl'
                q.parent.mkdir(parents=True)
                old = q.with_name(ownership.NAME)
                old.write_bytes(original)
                cmd = [sys.executable, '-B', '-c', 'from memvane.wiki.verify.tests.integration.test_legacy_handoff '
                       'import _reader_child; import sys; _reader_child(*sys.argv[1:])', str(q)]
                child = subprocess.run(cmd + [boundary], capture_output=True, timeout=30)
                self.assertEqual(child.returncode, 78, child.stderr)
                for _ in range(2):
                    self.assertEqual(json.loads(subprocess.check_output(cmd + ['resume'], timeout=30))['owners'],
                                     {self.rel: self.sha})
                index = legacy_handoff._index(q)
                self.assertEqual(len(index['imports']), 1)
                item = work.Work.lookup(q, 'legacy', next(iter(index['imports'].values())))
                self.assertEqual((item.state['phase'], item.read('raw')), ('finished', original))
                ownership.store(q, {'owners': {}, 'retirements': {}})
                self.assertEqual(ownership.load(q), {'owners': {}, 'retirements': {}})
                self.assertTrue(ownership.reserved(q, self.rel))
                with self.assertRaisesRegex(ValueError, 'legacy_pending_name_reserved'):
                    ownership.store(q, {'owners': {self.rel: self.sha}, 'retirements': {}})
                self.assertEqual(old.read_bytes(), original)

    def test_finished_import_cannot_restore_superseded_ownership_if_mutable_index_disappears(self):
        original = self.seed()
        self.assertEqual(ownership.load(self.queue)['owners'][self.rel], self.sha)
        item = self.imported()
        self.assertEqual(work.Work.entries(self.queue, 'legacy'), [])
        ownership.store(self.queue, {'owners': {}, 'retirements': {}})
        with work.private_root(self.queue) as fd:
            os.unlink(legacy_handoff._name(self.queue), dir_fd=fd)
        self.assertEqual(ownership.load(self.queue), {'owners': {}, 'retirements': {}})
        self.assertTrue(ownership.reserved(self.queue, self.rel))
        self.assertEqual(item.read('raw'), self.source.read_bytes())
        self.assertEqual(self.source.read_bytes(), original)

    def test_new_contradictory_digest_retires_previous_authority_without_reassigning_name(self):
        original = self.seed()
        self.assertEqual(ownership.load(self.queue)['owners'][self.rel], self.sha)
        prior = self.imported()
        self.source.write_bytes(b'{"owners":')
        self.assertEqual(ownership.load(self.queue)['owners'], {})
        self.assertTrue(ownership.reserved(self.queue, self.rel))
        self.source.write_bytes(original)
        self.assertEqual(ownership.load(self.queue)['owners'], {})
        self.assertEqual(prior.read('raw'), original)

    def test_e8_swap_stays_read_only_and_does_not_authorize_retirement(self):
        original = self.seed(plan=True)
        swap = self.queue.with_name(self.plan['swap'])
        image = b'{"previous":"unpublished"}\n'
        swap.write_bytes(image)
        state = ownership.load(self.queue)
        self.assertEqual(state, {'owners': {}, 'retirements': {}})
        ownership.discard_unpublished(self.queue, state)
        ownership.finish(self.root, {}, self.candidate.parent, self.queue, state, set(), None)
        detail = self.imported().state['proof']['records'][self.rel]
        self.assertEqual(detail['operation'], legacy_handoff.operation(self.plan))
        self.assertEqual(detail['swap']['sha256'], hashlib.sha256(image).hexdigest())
        self.assertEqual((self.source.read_bytes(), swap.read_bytes()), (original, image))
        self.assertTrue(self.candidate.exists())

    def test_torn_unknown_and_contradictory_e8_keep_names_reserved_but_allow_new_work(self):
        fresh = 'wiki/.new.pending-' + 'f' * 16
        raw = self.seed()
        for data in (raw[:-8], json.dumps({'version': 4, 'owners': {self.rel: self.sha}}).encode(),
                     json.dumps({'owners': {self.rel: '1' * 64}, 'retirements': {}}).encode()):
            with self.subTest(source=data[:15]):
                self.source.write_bytes(data)
                state = ownership.load(self.queue)
                self.assertNotIn(self.rel, state['owners'])
                self.assertTrue(ownership.reserved(self.queue, self.rel))
                self.assertFalse(ownership.reserved(self.queue, fresh, fresh=True))
                self.assertTrue(ownership.reserved(self.queue, self.rel, fresh=True))
                state['owners'][fresh] = '2' * 64
                ownership.store(self.queue, state)
                self.assertEqual(ownership.load(self.queue)['owners'][fresh], '2' * 64)
                self.assertEqual(self.source.read_bytes(), data)
                self.assertTrue(self.candidate.exists())

    def test_matching_audit_of_an_absent_candidate_keeps_the_original_row(self):
        raw = self.seed(plan=True)
        self.assertTrue(self.old._audited(self.root, self.job, self.plan))
        log = self.queue.with_name('verify-deleted.jsonl')
        original = log.read_bytes()
        self.candidate.unlink()
        for _ in range(2):
            self.assertEqual(ownership.load(self.queue), {'owners': {}, 'retirements': {}})
        self.assertEqual(self.imported().state['proof']['records'][self.rel]['result'], 'past-audit')
        self.assertEqual((self.source.read_bytes(), log.read_bytes()), (raw, original))

    def test_actual_e8_preunlink_audit_is_historical_evidence_and_is_not_repeated(self):
        raw = self.seed(plan=True)
        self.assertTrue(self.old._audited(self.root, self.job, self.plan))
        log = self.queue.with_name('verify-deleted.jsonl')
        original = log.read_bytes()
        self.assertEqual(json.loads(original)['reasons'][-1], legacy_handoff.operation(self.plan))
        for _ in range(2):
            state = ownership.load(self.queue)
            self.assertEqual(state['retirements'], {})
            ownership.finish(self.root, {}, self.candidate.parent, self.queue, state, set(), None)
        self.assertEqual(self.imported().state['proof']['records'][self.rel]['result'], 'prior-attempt')
        self.assertEqual(self.source.read_bytes(), raw)
        self.assertEqual(log.read_bytes(), original)
        self.assertTrue(self.candidate.exists())

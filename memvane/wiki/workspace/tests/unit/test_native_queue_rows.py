import json
import os

from memvane.wiki.workspace import api
from memvane.wiki.workspace.handoff import api as raw
from tests.support.case import FixtureCase


class NativeRows(FixtureCase):
    def test_producer_and_consumer_share_one_lock_and_keep_opaque_occurrences(self):
        root = self.tmp / 'project'
        root.mkdir()
        public = api.safe_queue_path(root, '.memvane/compile/source-queue.jsonl')
        private = api.safe_queue_path(root, '.memvane/compile/source-queue.jsonl', native=True)
        self.assertEqual(api.queue_lock_path(private), api.queue_lock_path(public))
        bad = b'{"same":2,"same":1}\n' + b'incomplete \xff bytes\r\n'
        good = b'{"sourcePath":"docs/good.md"}\r\n'
        private.write_bytes(bad + good + good)
        claim = api.claim_queue(public, private=True)
        self.assertNotEqual(claim.parent, public.parent)
        rows = api.read_queue(claim)
        self.assertEqual(rows, [(good[:-1].decode(), {'sourcePath': 'docs/good.md'})] * 2)
        rows.finish(public, claim, [rows[1][0]])
        self.assertEqual(public.read_bytes(), b'')
        self.assertEqual(next(private.parent.glob('fragment-*')).read_bytes(), bad)
        second = api.claim_queue(public, private=True)
        self.assertEqual(second.read_bytes(), good)

    def test_released_reader_and_orphan_round_trip_use_physical_lf(self):
        for char in ('\u0085', '\u2028', '\u2029'):
            for escaped in (False, True):
                value = {'sourceFingerprints': {'branch': 'left' + char + 'right'}}
                physical = json.dumps(value, ensure_ascii=escaped).encode() + b'\r\n'
                self.assertEqual(raw.occurrence(physical), (value, False))
                self.assertEqual(len(physical.decode().splitlines()), 1 if escaped else 2)
        self.assertEqual(raw.occurrence(b'{"a":1}\r\n'), ({'a': 1}, True))

    def test_partial_old_append_returns_privately_after_public_claim(self):
        queue = self.tmp / 'verify-queue.jsonl'
        row = b'{"targetPath":"one"}\n'
        queue.write_bytes(row)
        with queue.open('ab', buffering=0) as old:
            claim = api.claim_queue(queue, private=True)
            rows = api.read_queue(claim)
            old.write(b'{"targetPath":"two"}\n\xff')
            os.fsync(old.fileno())
            rows.finish(queue, claim, [])
        self.assertEqual(queue.read_bytes(), b'')
        again = api.claim_queue(queue, private=True)
        self.assertEqual(again.read_bytes(), b'{"targetPath":"two"}\n')
        self.assertEqual(next(again.parent.glob('fragment-*')).read_bytes(), b'\xff')

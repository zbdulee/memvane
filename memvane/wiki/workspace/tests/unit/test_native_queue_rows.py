import json
import os

from memvane.wiki.workspace import api
from memvane.wiki.workspace.handoff import api as raw
from tests.support.case import FixtureCase


class NativeRows(FixtureCase):
    def test_released_reader_and_orphan_round_trip_use_physical_lf(self):
        for char in ('\u0085', '\u2028', '\u2029'):
            for escaped in (False, True):
                value = {'sourceFingerprints': {'branch': 'left' + char + 'right'}}
                physical = json.dumps(value, ensure_ascii=escaped).encode() + b'\r\n'
                self.assertEqual(raw.occurrence(physical), (value, False))
                self.assertEqual(len(physical.decode().splitlines()), 1 if escaped else 2)
        self.assertEqual(raw.occurrence(b'{"a":1}\r\n'), ({'a': 1}, True))

    def test_released_return_rechecks_literal_and_escaped_rows_before_publication(self):
        for name in ('source-queue.jsonl', 'verify-queue.jsonl'):
            folder = self.tmp / name[:-6]
            folder.mkdir()
            queue = folder / name
            good = '{"allowed":true}'
            hidden = [json.dumps({'value': 'x' + ch + 'y'}, ensure_ascii=escaped)
                      for escaped in (False, True) for ch in ('\x85', '\u2028', '\u2029')]
            api.requeue_lines(queue, [good, *hidden, 'opaque'])
            self.assertFalse(queue.exists() and queue.read_bytes())
            private = raw.native_live(queue).parent
            exact = ''.join(r + '\n' for r in [good, *hidden, 'opaque']).encode()
            self.assertEqual(next(private.glob('ready-*')).read_bytes(), exact)

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

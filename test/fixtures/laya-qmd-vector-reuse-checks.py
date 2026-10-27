"""QMD-origin vector reuse: exact chunk identity, sampled parity, fail-closed."""
import hashlib
import sqlite3
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import laya_qmd_vector_reuse as reuse

MODEL = '/models/hf_ggml-org_embeddinggemma-300M-Q8_0.gguf'
DIM = 4


def vec(*xs): return struct.pack(f'<{len(xs)}f', *xs)


class Reuse(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'index.sqlite'
        db = sqlite3.connect(self.db)
        db.execute('CREATE TABLE content_vectors(hash TEXT, seq INTEGER, pos INTEGER, model TEXT,'
                   ' embed_fingerprint TEXT, total_chunks INTEGER, embedded_at TEXT, PRIMARY KEY(hash,seq))')
        # vec0 is replaced by an ordinary table with the same columns in fixtures.
        db.execute('CREATE TABLE vectors_vec(hash_seq TEXT PRIMARY KEY, embedding BLOB)')
        self.body = '# Title\n\nbody text\n'
        self.sha = hashlib.sha256(self.body.encode()).hexdigest()
        for seq, pos, v in ((0, 0, vec(1, 0, 0, 0)), (1, 40, vec(0, 1, 0, 0))):
            db.execute('INSERT INTO content_vectors VALUES(?,?,?,?,?,?,?)',
                       (self.sha, seq, pos, MODEL, 'b9747b', 2, 'now'))
            db.execute('INSERT INTO vectors_vec VALUES(?,?)', (f'{self.sha}_{seq}', v))
        db.commit(); db.close()

    def source(self, **kw):
        source = reuse.QmdVectorReuse(self.db, None, MODEL, DIM, **kw)
        self.addCleanup(source.close)
        return source

    def test_complete_chunk_set_is_reused(self):
        rows = self.source().lookup(self.body)
        self.assertEqual([(r['seq'], r['pos']) for r in rows], [(0, 0), (1, 40)])
        self.assertEqual(rows[1]['vector'], [0.0, 1.0, 0.0, 0.0])

    def test_incomplete_or_foreign_sets_are_not_reused(self):
        db = sqlite3.connect(self.db); db.execute('DELETE FROM vectors_vec WHERE hash_seq=?', (f'{self.sha}_1',)); db.commit(); db.close()
        self.assertIsNone(self.source().lookup(self.body))
        self.assertIsNone(self.source().lookup('# other\n'))
        other = reuse.QmdVectorReuse(self.db, None, '/models/other-model.gguf', DIM); self.addCleanup(other.close)
        self.assertIsNone(other.lookup(self.body))
        wide = reuse.QmdVectorReuse(self.db, None, MODEL, 8); self.addCleanup(wide.close)
        self.assertIsNone(wide.lookup(self.body))

    def test_sampling_and_parity_failure_turn_reuse_off(self):
        source = self.source(sample_every=3)
        self.assertEqual([source.take_sample() for _ in range(6)], [True, False, False, True, False, False])
        rows = source.lookup(self.body)
        close = [{'seq': r['seq'], 'pos': r['pos'], 'vector': [x * 2 for x in r['vector']]} for r in rows]
        self.assertTrue(source.verify(rows, close)); self.assertTrue(source.enabled)
        drift = [dict(close[0]), {'seq': 1, 'pos': 40, 'vector': [1.0, 0.0, 0.0, 0.0]}]
        self.assertFalse(source.verify(rows, drift))
        self.assertFalse(source.enabled); self.assertEqual(source.disabled_reason, 'qmd_vector_reuse_mismatch')
        self.assertIsNone(source.lookup(self.body))
        moved = [dict(close[0]), {'seq': 1, 'pos': 41, 'vector': close[1]['vector']}]
        self.assertFalse(self.source().verify(rows, moved))

    def test_unavailable_source_falls_back_silently(self):
        missing = reuse.QmdVectorReuse(Path(self.tmp.name) / 'absent.sqlite', None, MODEL, DIM)
        self.assertIsNone(missing.lookup(self.body)); self.assertFalse(missing.enabled)
        bad_ext = reuse.QmdVectorReuse(self.db, Path(self.tmp.name) / 'no-vec0', MODEL, DIM)
        self.assertIsNone(bad_ext.lookup(self.body)); self.assertEqual(bad_ext.disabled_reason, 'qmd_vector_reuse_unavailable')


if __name__ == '__main__':
    unittest.main(verbosity=2)

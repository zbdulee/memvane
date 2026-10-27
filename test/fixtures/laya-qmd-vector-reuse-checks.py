"""QMD vector reuse: exact chunk identity, sampled parity, fail-closed.

HOME is a temp directory, so the shared QMD store is always a fixture.
"""
import hashlib
import json
import os
import sqlite3
import struct
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import laya_qmd_vector_reuse as reuse

MODEL = '/models/hf_ggml-org_embeddinggemma-300M-Q8_0.gguf'
DIM = 4


def vec(*xs): return struct.pack(f'<{len(xs)}f', *xs)


class Reuse(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'home'
        self.home.mkdir(mode=0o700)
        env = mock.patch.dict(os.environ, {'HOME': str(self.home)})
        env.start(); self.addCleanup(env.stop)
        self.db = Path(self.tmp.name) / 'index.sqlite'
        self.fill(self.db)

    def fill(self, path):
        db = sqlite3.connect(path)
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

    def shared_store(self):
        """A selected plugin shared QMD store holding the same vectors."""
        import qmd_shared_store as S
        import runtime_update
        generation = 'g-' + '1' * 32
        base = S.root(self.home)
        target = base / 'generations' / generation
        (target / 'config').mkdir(parents=True)
        (target / 'cache').mkdir()
        for directory in (base, base / 'generations', target, target / 'config', target / 'cache'):
            directory.chmod(0o700)
        index = target / 'index.sqlite'
        self.fill(index)
        with sqlite3.connect(index) as db:
            db.execute('CREATE TABLE documents(collection TEXT, path TEXT, hash TEXT, active INTEGER)')
            db.execute('CREATE TABLE content(hash TEXT)')
            db.execute('CREATE TABLE store_collections(name TEXT)')
        index.chmod(0o600)

        def private(path, text):
            path.write_text(text); path.chmod(0o600)
        private(target / 'config/index.yml', 'models: {}\n')
        prepared = {'schema': S.SCHEMA, 'generation': generation,
                    'configSha256': hashlib.sha256((target / 'config/index.yml').read_bytes()).hexdigest(),
                    'indexSchemaSha256': runtime_update.inspect_index(
                        index, include_file_hash=False)['schemaSha256']}
        private(target / 'prepared.json', json.dumps(prepared))
        private(base / 'active.json', json.dumps({'schema': S.SCHEMA, 'generation': generation}))
        return index

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
        source.defer_sample()  # the 6th call's slot is retried by the next one
        self.assertEqual(source.take_sample(), False)
        fresh = self.source(sample_every=3)
        self.assertTrue(fresh.take_sample()); fresh.defer_sample(); self.assertTrue(fresh.take_sample())
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


    def test_initial_build_without_provider_paths_does_not_reuse(self):
        identity = {'sourceDb': str(self.db)}
        self.assertIsNone(reuse.for_initial_build(identity, {'recipeId': 'x'}, DIM))
        self.assertIsNone(reuse.for_initial_build({'originMode': 'fresh_wiki_only'}, {}, DIM))
        found = reuse.for_initial_build(identity, {'moduleEntryPath': '/r/package/node_modules/node-llama-cpp/dist/index.js',
                                                   'modelPath': MODEL}, DIM)
        self.addCleanup(found.close)
        self.assertEqual(found.extension, Path('/r/package/node_modules/sqlite-vec-darwin-arm64/vec0'))

    MANIFEST = {'moduleEntryPath': '/r/package/node_modules/node-llama-cpp/dist/index.js', 'modelPath': MODEL}

    def test_fresh_build_reuses_the_selected_shared_store(self):
        index = self.shared_store()
        before = {p: p.read_bytes() for p in index.parent.parent.parent.rglob('*') if p.is_file()}
        fresh = {'originMode': 'fresh_wiki_only'}
        found = reuse.for_initial_build(fresh, self.MANIFEST, DIM)
        self.addCleanup(found.close)
        self.assertEqual(found.source_db, index)
        self.assertNotIn('sourceDb', fresh)
        # Fixture vectors_vec is an ordinary table; the real build loads vec0.
        found.extension = None
        self.assertEqual([(r['seq'], r['pos']) for r in found.lookup(self.body)], [(0, 0), (1, 40)])
        found.close()
        self.assertEqual({p: p.read_bytes() for p in index.parent.parent.parent.rglob('*') if p.is_file()}, before)
        origin = reuse.for_initial_build({'sourceDb': str(self.db)}, self.MANIFEST, DIM)
        self.addCleanup(origin.close)
        self.assertEqual(origin.source_db, self.db)

    def test_fresh_build_without_selected_store_embeds(self):
        import qmd_shared_store as S
        fresh = {'originMode': 'fresh_wiki_only'}
        self.assertIsNone(reuse.for_initial_build(fresh, self.MANIFEST, DIM))
        self.assertFalse(S.root(self.home).exists())
        index = self.shared_store()
        (index.parent.parent.parent / 'active.json').unlink()
        self.assertIsNone(reuse.for_initial_build(fresh, self.MANIFEST, DIM))

if __name__ == '__main__':
    unittest.main(verbosity=2)

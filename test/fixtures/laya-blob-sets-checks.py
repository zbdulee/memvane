"""Synthetic complete-set contracts; no model or operational state."""
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))


def sha(value):
    return hashlib.sha256(value.encode('utf8')).hexdigest()


def recipe():
    return dict(input_recipe='blob-input-v1', tokenizer_sha='1' * 64,
                chunker_sha='2' * 64, window_tokens=900, overlap_tokens=135,
                window_search_tokens=200, offset_unit='utf16-code-unit',
                heading_rule='qmd-markdown-v1',
                document_template='title: {title} | text: {text}',
                synthetic_path='blobs/{blob_sha}.md')


def profile():
    return dict(model_id='synthetic-model', model_sha='3' * 64,
                embedding_fingerprint='4' * 64, dimension=2, metric='cosine',
                document_input_recipe='blob-input-v1',
                query_input_recipe='qmd-query-v1',
                query_template='task: search result | query: {text}')


def chunk(text, seq=0, position=0):
    return dict(seq=seq, position=position, text=text, text_sha=sha(text))


class SetChecks(unittest.TestCase):
    def setUp(self):
        module = importlib.import_module('laya_blob_store')
        self.Store, self.Error = module.BlobStore, module.BlobStoreError
        self.temp = tempfile.TemporaryDirectory(dir=os.environ['HOME'])
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / 'project'
        self.wiki = self.project / '.memvane' / 'wiki'
        self.wiki.mkdir(parents=True)
        (self.wiki.parent / 'settings.json').write_text(
            json.dumps({'name': 'fixture', 'skipPaths': []}), encoding='utf8')
        self.body = '# 고정\n별빛😀 둘째 문장\n'
        (self.wiki / 'card.md').write_bytes(self.body.encode('utf8'))
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.path = self.state / 'candidate.sqlite'
        checkout = importlib.import_module('laya_checkout_view')
        self.repository = checkout.register_repository(str(self.project))
        self.worktree = checkout.register_worktree(str(self.project), self.repository)
        self.scope = 'f' * 32
        self.store = self.Store.create(self.path, project_root=str(self.project),
                                       repository=self.repository, worktree=self.worktree,
                                       scope_id=self.scope)
        self.addCleanup(self.store.close)
        self.blob = self.store.ingest_card(self.worktree['worktreeId'], 'card.md')

    def prepared(self):
        r = self.store.register_recipe(recipe())
        p = self.store.register_profile(profile())
        return r, p

    def vector(self, r, p, *, embedding=None):
        return dict(seq=0, input_sha=self.store.vector_input_sha(self.blob, r, p, 0),
                    embedding=embedding if embedding is not None else struct.pack('<ff', 1, 2))

    def test_recipe_is_canonical_and_replay_is_noop(self):
        spec = recipe()
        first = self.store.register_recipe(spec)
        self.assertEqual(first, self.store.register_recipe(dict(reversed(list(spec.items())))))
        self.assertRegex(first, r'^[0-9a-f]{64}$')
        self.assertEqual(self.store.counts()['chunk_recipes'], 1)

    def test_recipe_artifacts_and_window_change_identity(self):
        first = self.store.register_recipe(recipe())
        for key, value in [('tokenizer_sha', '8' * 64), ('chunker_sha', '9' * 64),
                           ('window_tokens', 901)]:
            spec = recipe(); spec[key] = value
            self.assertNotEqual(first, self.store.register_recipe(spec))

    def test_recipe_rejects_unbound_or_malformed_specs(self):
        for key, value in [('tokenizer_sha', 'bad'), ('window_tokens', True),
                           ('overlap_tokens', 900), ('window_search_tokens', -1),
                           ('offset_unit', 'characters'), ('extra', 'body'),
                           ('document_template', '{text}'), ('window_tokens', float('nan'))]:
            with self.subTest(key=key, value=value):
                spec = recipe(); spec[key] = value
                with self.assertRaises(self.Error): self.store.register_recipe(spec)
        self.assertEqual(self.store.counts()['chunk_recipes'], 0)

    def test_profile_binds_model_bytes_not_only_model_name(self):
        spec = profile(); first = self.store.register_profile(spec)
        spec['model_sha'] = '9' * 64
        self.assertNotEqual(first, self.store.register_profile(spec))
        self.assertEqual(first, self.store.register_profile(profile()))

    def test_profile_refuses_bad_dimensions_fingerprints_and_recipes(self):
        for key, value in [('dimension', True), ('dimension', 0), ('dimension', 2049),
                           ('model_id', 'bad\nmodel'), ('model_sha', 'bad'),
                           ('metric', 'dot'), ('document_input_recipe', 'legacy'),
                           ('query_template', '{text}'), ('body', 'raw')]:
            with self.subTest(key=key, value=value):
                spec = profile(); spec[key] = value
                with self.assertRaises(self.Error): self.store.register_profile(spec)

    def test_chunk_set_complete_replay_and_read(self):
        r, _ = self.prepared(); rows = [chunk(self.body)]
        first = self.store.put_chunk_set(self.blob, r, rows)
        self.assertEqual(first, self.store.put_chunk_set(self.blob, r, rows))
        self.assertEqual(self.store.chunks(self.blob, r), rows)
        self.assertEqual(self.store.counts()['chunk_sets'], 1)
        self.assertEqual(self.store.counts()['chunks'], 1)

    def test_chunks_reject_gaps_duplicate_positions_and_hashes(self):
        r, _ = self.prepared()
        invalid = [[chunk(self.body, seq=1)], [chunk(self.body), chunk(self.body, seq=1)],
                   [dict(chunk(self.body), text_sha='a' * 64)], [chunk('unrelated raw text')],
                   [dict(chunk(self.body), position=True)], [], [chunk(self.body)] * 1025]
        for rows in invalid:
            with self.subTest(rows=len(rows)):
                with self.assertRaises(self.Error): self.store.put_chunk_set(self.blob, r, rows)
        self.assertEqual(self.store.counts()['chunk_sets'], 0)
        self.assertEqual(self.store.counts()['chunks'], 0)

    def test_utf16_offsets_follow_non_bmp_boundary(self):
        r, _ = self.prepared(); cut = self.body.index('😀') + 1
        pos = len(self.body[:cut].encode('utf-16-le')) // 2
        rows = [chunk(self.body[:cut]), chunk(self.body[cut:], 1, pos)]
        self.store.put_chunk_set(self.blob, r, rows)
        self.assertEqual(self.store.chunks(self.blob, r), rows)

    def test_utf16_mid_surrogate_offset_refused(self):
        r, _ = self.prepared(); cut = self.body.index('😀')
        pos = len(self.body[:cut].encode('utf-16-le')) // 2 + 1
        with self.assertRaises(self.Error):
            self.store.put_chunk_set(self.blob, r, [chunk(self.body[cut + 1:], position=pos)])

    def test_changed_chunk_replay_conflicts_without_overwrite(self):
        r, _ = self.prepared(); original = [chunk(self.body)]
        self.store.put_chunk_set(self.blob, r, original)
        with self.assertRaises(self.Error):
            self.store.put_chunk_set(self.blob, r, [chunk(self.body[:4])])
        self.assertEqual(self.store.chunks(self.blob, r), original)

    def test_separate_recipe_has_separate_set(self):
        r, _ = self.prepared(); spec = recipe(); spec['tokenizer_sha'] = '8' * 64
        r2 = self.store.register_recipe(spec)
        self.assertNotEqual(self.store.put_chunk_set(self.blob, r, [chunk(self.body)]),
                            self.store.put_chunk_set(self.blob, r2, [chunk(self.body)]))
        self.assertEqual(self.store.counts()['chunk_sets'], 2)

    def test_sql_child_failure_rolls_back_whole_set(self):
        r, _ = self.prepared()
        def deny_child(action, name, *_):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and name == 'chunks' else sqlite3.SQLITE_OK
        self.store._db.set_authorizer(deny_child)
        try:
            with self.assertRaises(self.Error): self.store.put_chunk_set(self.blob, r, [chunk(self.body)])
        finally:
            self.store._db.set_authorizer(None)
        self.assertEqual(self.store.counts()['chunk_sets'], 0)
        self.assertEqual(self.store.counts()['chunks'], 0)
        self.store.put_chunk_set(self.blob, r, [chunk(self.body)])

    def test_titleless_rename_has_identical_exact_provider_input(self):
        text = '별빛 아래 😀\r\n'
        (self.wiki / 'first.md').write_bytes(text.encode('utf8'))
        b = self.store.ingest_card(self.worktree['worktreeId'], 'first.md')
        (self.wiki / 'second.md').write_bytes(text.encode('utf8'))
        self.assertEqual(b, self.store.ingest_card(self.worktree['worktreeId'], 'second.md'))
        r, p = self.prepared(); self.store.put_chunk_set(b, r, [chunk(text)])
        doc = self.store.document(b)
        self.assertEqual(doc, dict(body=text, path=f'blobs/{b}.md', title=b))
        self.assertEqual(self.store.vector_input_sha(b, r, p, 0), sha(f'title: {b} | text: {text}'))

    def test_embedding_complete_replay_and_read(self):
        r, p = self.prepared(); self.store.put_chunk_set(self.blob, r, [chunk(self.body)])
        rows = [self.vector(r, p)]; first = self.store.put_embedding_set(self.blob, r, p, rows)
        self.assertEqual(first, self.store.put_embedding_set(self.blob, r, p, rows))
        self.assertEqual(self.store.vectors(self.blob, r, p), rows)
        self.assertEqual(self.store.counts()['embedding_sets'], 1)

    def test_embedding_refuses_invalid_inputs_and_partial_rows(self):
        r, p = self.prepared(); self.store.put_chunk_set(self.blob, r, [chunk(self.body)])
        valid = self.vector(r, p)
        bad = [[], [dict(valid, seq=1)], [dict(valid, input_sha='f' * 64)],
               [dict(valid, embedding=struct.pack('<f', 1))],
               [dict(valid, embedding=struct.pack('<ff', float('nan'), 1))],
               [dict(valid, embedding=struct.pack('<ff', float('inf'), 1))],
               [dict(valid, embedding=struct.pack('<ff', 0, -0.0))],
               [valid, dict(valid, seq=1)]]
        for rows in bad:
            with self.subTest(rows=len(rows)):
                with self.assertRaises(self.Error): self.store.put_embedding_set(self.blob, r, p, rows)
        self.assertEqual(self.store.counts()['embedding_sets'], 0)
        self.assertEqual(self.store.counts()['vectors'], 0)

    def test_changed_vector_replay_conflicts(self):
        r, p = self.prepared(); self.store.put_chunk_set(self.blob, r, [chunk(self.body)])
        rows = [self.vector(r, p)]; self.store.put_embedding_set(self.blob, r, p, rows)
        with self.assertRaises(self.Error):
            self.store.put_embedding_set(self.blob, r, p, [self.vector(r, p, embedding=struct.pack('<ff', 2, 3))])
        self.assertEqual(self.store.vectors(self.blob, r, p), rows)

    def test_profile_separates_embedding_set_identity(self):
        r, p = self.prepared(); self.store.put_chunk_set(self.blob, r, [chunk(self.body)])
        spec = profile(); spec['model_sha'] = '8' * 64; p2 = self.store.register_profile(spec)
        first = self.store.put_embedding_set(self.blob, r, p, [self.vector(r, p)])
        second = self.store.put_embedding_set(self.blob, r, p2, [self.vector(r, p2)])
        self.assertNotEqual(first, second)
        self.assertEqual(self.store.counts()['embedding_sets'], 2)

    def test_blob_sql_update_delete_and_replace_refused(self):
        with sqlite3.connect(self.path) as db:
            for sql in ['UPDATE wiki_blobs SET body=body', 'DELETE FROM wiki_blobs',
                        'INSERT OR REPLACE INTO wiki_blobs SELECT * FROM wiki_blobs']:
                with self.subTest(sql=sql):
                    with self.assertRaises(sqlite3.Error): db.execute(sql)
            db.rollback()
        self.assertEqual(self.store.document(self.blob)['body'], self.body)

    def test_two_connections_coalesce_identical_set(self):
        r, _ = self.prepared(); barrier = threading.Barrier(2)
        def write():
            s = self.Store.open(self.path, repository=self.repository, scope_id=self.scope)
            try:
                barrier.wait(timeout=5)
                return s.put_chunk_set(self.blob, r, [chunk(self.body)])
            finally: s.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(write) for _ in range(2)]
            results = [f.result(timeout=10) for f in futures]
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.store.counts()['chunk_sets'], 1)
        self.assertEqual(self.store.counts()['chunks'], 1)

    def test_verifier_checks_all_complete_rows(self):
        r, p = self.prepared(); self.store.put_chunk_set(self.blob, r, [chunk(self.body)])
        self.store.put_embedding_set(self.blob, r, p, [self.vector(r, p)])
        self.assertTrue(self.store.verify())

    def test_qmd_title_and_input_match_actual_js_unicode_rules(self):
        bodies = ['# \u0085Title\u0085\nbody', '# Title\u2028## Next\nbody',
                  '\u2028## Topic\nbody', '# Notes\r## Second\rbody', '# \u001cTitle\u001c\nbody',
                  '# \ufeffTitle\ufeff\nbody', '#   \nbody']
        path = (Path(__file__).resolve().parents[2] / 'core/laya_qmd_chunking.mjs').as_uri()
        code = ('import {extractQmdMarkdownTitle as title} from ' + json.dumps(path) + ';'
                'import {readFileSync} from "node:fs";'
                'const rows=JSON.parse(readFileSync(0,"utf8"));'
                'process.stdout.write(JSON.stringify(rows.map(x=>title(x.body,x.path))));')
        payload = [dict(body=body, path=f'blobs/{sha(body)}.md') for body in bodies]
        child = subprocess.run(['node', '--input-type=module', '-e', code],
                               input=json.dumps(payload), encoding='utf8', capture_output=True, timeout=10)
        self.assertEqual(child.returncode, 0, child.stderr)
        expected = json.loads(child.stdout)
        for i, body in enumerate(bodies):
            with self.subTest(i=i):
                name = f'edge-{i}.md'; (self.wiki / name).write_bytes(body.encode('utf8'))
                blob = self.store.ingest_card(self.worktree['worktreeId'], name)
                self.assertEqual(self.store.document(blob)['title'], expected[i])
                r, p = self.prepared(); self.store.put_chunk_set(blob, r, [chunk(body)])
                self.assertEqual(self.store.vector_input_sha(blob, r, p, 0),
                                 sha(f'title: {expected[i] or "none"} | text: {body}'))


if __name__ == '__main__':
    unittest.main()

"""Disposable source-format fixtures; never opens a QMD installation or user DB."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
from laya_qmd_migration import (QMD_253, FORK_F463, QmdMigrationBlocked,
                                inspect_qmd, backup_qmd, verify_restore,
                                inspect_qmd_collections, backup_qmd_collections,
                                verify_restore_collections, prepare_qmd_candidate)
from laya_index_a import LayaIndexA
import laya_index_a
import laya_qmd_migration
from laya_index_vector import qmd_markdown_title, verify_generation_seal


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def make_source(path, kind, *, documents=None, vectors=()):
    if documents is None:
        documents = [('docs', 'live.md', 'astral manuscript', 1),
                     ('docs', 'gone.md', 'old deleted chapter', 0),
                     ('other', 'live.md', 'other worktree', 1)]
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE content(hash TEXT PRIMARY KEY,doc TEXT NOT NULL,
                                 created_at TEXT NOT NULL);
            CREATE TABLE documents(id INTEGER PRIMARY KEY AUTOINCREMENT,
                collection TEXT NOT NULL,path TEXT NOT NULL,title TEXT NOT NULL,
                hash TEXT NOT NULL,created_at TEXT NOT NULL,modified_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY(hash) REFERENCES content(hash) ON DELETE CASCADE,
                UNIQUE(collection,path));
            CREATE TABLE content_vectors(hash TEXT NOT NULL,seq INTEGER NOT NULL DEFAULT 0,
                pos INTEGER NOT NULL DEFAULT 0,model TEXT NOT NULL,
                embed_fingerprint TEXT NOT NULL DEFAULT '',
                total_chunks INTEGER NOT NULL DEFAULT 1,embedded_at TEXT NOT NULL,
                PRIMARY KEY(hash,seq));
            CREATE TABLE store_collections(name TEXT PRIMARY KEY,path TEXT NOT NULL,
                pattern TEXT NOT NULL DEFAULT '**/*.md',ignore_patterns TEXT,
                include_by_default INTEGER DEFAULT 1,update_command TEXT,context TEXT);
            CREATE TABLE store_config(key TEXT PRIMARY KEY,value TEXT);
            CREATE VIRTUAL TABLE documents_fts USING fts5(filepath,title,body,
                tokenize='porter unicode61');
            CREATE TRIGGER documents_ai AFTER INSERT ON documents BEGIN SELECT 1; END;
            CREATE TRIGGER documents_ad AFTER DELETE ON documents BEGIN SELECT 1; END;
            CREATE TRIGGER documents_au AFTER UPDATE ON documents BEGIN SELECT 1; END;
        ''')
        if kind == FORK_F463:
            db.executescript('''
                CREATE TABLE file_sync_state(collection TEXT NOT NULL,
                    relative_path TEXT NOT NULL,mtime_ms INTEGER NOT NULL,
                    size INTEGER NOT NULL,content_hash TEXT NOT NULL,
                    document_id INTEGER NOT NULL,PRIMARY KEY(collection,relative_path));
                CREATE TABLE document_metadata(document_id INTEGER PRIMARY KEY,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    extraction_version INTEGER NOT NULL,extraction_error TEXT,
                    extracted_at TEXT NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE);
                CREATE TABLE document_metadata_values(document_id INTEGER NOT NULL,
                    key TEXT NOT NULL,ordinal INTEGER NOT NULL,value_type TEXT NOT NULL,
                    text_value TEXT,number_value REAL,boolean_value INTEGER,
                    PRIMARY KEY(document_id,key,ordinal));
                PRAGMA user_version=1;
            ''')
        db.executemany('INSERT INTO store_collections(name,path) VALUES(?,?)',
                       [('docs', '/synthetic/worktree-a'),
                        ('wiki', '/synthetic/wiki'),
                        ('other', '/synthetic/worktree-b')])
        for collection, rel, body, active in documents:
            hash_ = digest(body)
            db.execute('INSERT OR IGNORE INTO content VALUES(?,?,?)',
                       (hash_, body, '2026-01-01'))
            db.execute('''INSERT INTO documents(collection,path,title,hash,
                        created_at,modified_at,active) VALUES(?,?,?,?,?,?,?)''',
                       (collection, rel, qmd_markdown_title(body, rel), hash_,
                        '2026-01-01', '2026-01-01', active))
        db.executemany('INSERT INTO content_vectors VALUES(?,?,?,?,?,?,?)', vectors)


class QmdFormatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='laya-qmd-format-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def source(self, kind=QMD_253, **kw):
        path = self.root / 'source.sqlite'
        make_source(path, kind, **kw)
        return path

    def prepare(self, backup, current_files, candidate=None, **overrides):
        if candidate is None:
            candidate = self.root / 'candidate.sqlite'
        options = {'migration_allowed': True,
                   'expected_collection_root': '/synthetic/worktree-a',
                   'expected_source_digest': inspect_qmd(backup, QMD_253, 'docs')['digest']}
        options.update(overrides)
        return prepare_qmd_candidate(backup, QMD_253, 'docs', current_files,
                                     candidate, 'scope-a', 'gen-1', **options)

    def vector_options(self, source, backup, *, files=None, chunks=None,
                       source_format=QMD_253):
        if files is None:
            files = {'live.md': 'astral manuscript'}
        evidence = inspect_qmd(backup, source_format, 'docs')['digest']
        manifest = laya_index_a._digest('scope-a', [
            (path, digest(body), 1) for path, body in sorted(files.items())])
        if chunks is None:
            chunks = [{'hash': hash_, 'title': title, 'seq': 0, 'pos': 0,
                       'vector': [1.0, 0.0]}
                      for hash_, title in sorted({
                          (digest(body), qmd_markdown_title(body, path))
                          for path, body in files.items()})]
        return {'provider_approved': True, 'current_source_db': source,
                'scope_collections': ('docs',),
                'vector_batch': {'scope': 'scope-a', 'source_manifest': manifest,
                    'qmd_source_digest': evidence, 'model_id': 'synthetic-approved',
                    'embedding_fingerprint': 'a' * 64, 'dimension': 2,
                    'chunks': chunks}}

    def test_upstream_inventory_scoped_and_tombstone(self):
        inv = inspect_qmd(self.source(), QMD_253, 'docs')
        self.assertEqual((inv['active_count'], inv['tombstone_count']), (1, 1))
        self.assertEqual([x[0] for x in inv['documents']], ['gone.md', 'live.md'])
        self.assertEqual(inv['needs_reembed'], [digest('astral manuscript')])

    def test_fork_inventory_exact_version_and_columns(self):
        inv = inspect_qmd(self.source(FORK_F463), FORK_F463, 'docs')
        self.assertEqual(inv['source_format'], FORK_F463)
        self.assertEqual(inv['active_count'], 1)

    def test_multi_collection_backup_and_candidate_share_one_generation(self):
        raw = 'astral manuscript'
        wiki = '# Card\nverified synthetic summary'
        source = self.source(documents=[('docs', 'live.md', raw, 1),
                                        ('wiki', 'card.md', wiki, 1),
                                        ('other', 'unrelated.md', 'untouched', 1)])
        backup = self.root / 'backup.sqlite'
        names = ('docs', 'wiki')
        proof = backup_qmd_collections(source, QMD_253, names, backup)
        inventory = inspect_qmd_collections(backup, QMD_253, names)
        self.assertEqual(proof['source_digest'], inventory['digest'])
        self.assertTrue(verify_restore_collections(
            backup, QMD_253, names, inventory['digest'])['verified'])
        files = {'docs/live.md': raw, 'wiki/card.md': wiki}
        manifest = laya_index_a._digest('scope-a', [
            (path, digest(body), 1) for path, body in sorted(files.items())])
        chunks = [{'hash': digest(body),
                   'title': qmd_markdown_title(body, path),
                   'seq': 0, 'pos': 0, 'vector': [1.0, 0.0]}
                  for path, body in sorted(files.items())]
        result = prepare_qmd_candidate(
            backup, QMD_253, names, files, self.root / 'candidate.sqlite',
            'scope-a', 'gen-1', migration_allowed=True,
            expected_collection_root={'docs': '/synthetic/worktree-a',
                                      'wiki': '/synthetic/wiki'},
            expected_source_digest=inventory['digest'], provider_approved=True,
            current_source_db=source, scope_collections=names,
            vector_batch={'scope': 'scope-a', 'source_manifest': manifest,
                          'qmd_source_digest': inventory['digest'],
                          'model_id': 'synthetic-approved',
                          'embedding_fingerprint': 'a' * 64,
                          'dimension': 2, 'chunks': chunks})
        index = LayaIndexA(result['candidate'])
        try:
            self.assertEqual(index.db.execute(
                'SELECT path FROM membership WHERE gen=? ORDER BY path',
                ('gen-1',)).fetchall(),
                [('docs/live.md',), ('wiki/card.md',)])
            self.assertEqual(verify_generation_seal(index, 'gen-1')['rows_count'], 2)
        finally:
            index.close()
        with sqlite3.connect(source) as db:
            db.execute("UPDATE documents SET title='changed wiki title' "
                       "WHERE collection='wiki' AND path='card.md'")
        self.assertNotEqual(inspect_qmd_collections(
            source, QMD_253, names)['digest'], inventory['digest'])

    def test_wrong_format_refused_both_directions(self):
        with self.assertRaises(QmdMigrationBlocked):
            inspect_qmd(self.source(FORK_F463), QMD_253, 'docs')
        other = self.root / 'upstream.sqlite'
        make_source(other, QMD_253)
        with self.assertRaises(QmdMigrationBlocked):
            inspect_qmd(other, FORK_F463, 'docs')

    def test_unknown_version_and_missing_collection_refused(self):
        source = self.source()
        with self.assertRaisesRegex(QmdMigrationBlocked, 'unsupported_source_version'):
            inspect_qmd(source, 'qmd-2.6', 'docs')
        with self.assertRaisesRegex(QmdMigrationBlocked, 'collection_unrecorded'):
            inspect_qmd(source, QMD_253, 'missing')

    def test_changed_schema_and_header_refused(self):
        source = self.source()
        with sqlite3.connect(source) as db:
            db.execute('PRAGMA user_version=2')
        with self.assertRaisesRegex(QmdMigrationBlocked, 'source_header_mismatch'):
            inspect_qmd(source, QMD_253, 'docs')

    def test_wrong_content_hash_refused(self):
        source = self.source()
        with sqlite3.connect(source) as db:
            db.execute("UPDATE content SET doc='altered' WHERE doc='astral manuscript'")
        with self.assertRaisesRegex(QmdMigrationBlocked, 'source_document_invalid'):
            inspect_qmd(source, QMD_253, 'docs')

    def test_read_only_inventory_does_not_change_source_bytes(self):
        source = self.source()
        before = source.read_bytes()
        inspect_qmd(source, QMD_253, 'docs')
        self.assertEqual(source.read_bytes(), before)

    def test_title_only_change_stales_backup_evidence(self):
        source = self.source()
        before = inspect_qmd(source, QMD_253, 'docs')
        self.assertEqual(before['documents'][1][1], 'live')
        with sqlite3.connect(source) as db:
            db.execute("UPDATE documents SET title='renamed title' WHERE path='live.md'")
        after = inspect_qmd(source, QMD_253, 'docs')
        self.assertNotEqual(before['digest'], after['digest'])
        self.assertEqual(after['documents'][1][1], 'renamed title')

    def test_online_backup_restore_no_clobber(self):
        source = self.source()
        backup = self.root / 'backup.sqlite'
        proof = backup_qmd(source, QMD_253, 'docs', backup)
        self.assertEqual(proof['source_digest'], inspect_qmd(source, QMD_253, 'docs')['digest'])
        self.assertTrue(verify_restore(backup, QMD_253, 'docs', proof['source_digest'])['verified'])
        with self.assertRaisesRegex(QmdMigrationBlocked, 'backup_target_unavailable'):
            backup_qmd(source, QMD_253, 'docs', backup)

    def test_wal_backup_includes_uncheckpointed_row(self):
        source = self.source()
        writer = sqlite3.connect(source)
        try:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('INSERT INTO content VALUES(?,?,?)',
                           (digest('later'), 'later', '2026-01-01'))
            writer.execute('''INSERT INTO documents(collection,path,title,hash,
                created_at,modified_at,active) VALUES(?,?,?,?,?,?,?)''',
                ('docs', 'later.md', 'later', digest('later'),
                 '2026-01-01', '2026-01-01', 1))
            writer.commit()
            proof = backup_qmd(source, QMD_253, 'docs', self.root / 'backup.sqlite')
            self.assertEqual(inspect_qmd(proof['backup'], QMD_253, 'docs')['active_count'], 2)
        finally:
            writer.close()

    def test_prepare_lexical_candidate_preserves_tombstone_boundary(self):
        source = self.source()
        backup = self.root / 'backup.sqlite'
        backup_qmd(source, QMD_253, 'docs', backup)
        candidate = self.root / 'candidate.sqlite'
        result = self.prepare(backup, {'live.md': 'astral manuscript'}, candidate)
        self.assertEqual(result['state'], 'prepared')
        self.assertFalse(result['cutover_ready'])
        self.assertFalse(result['vector_reuse'])
        self.assertEqual(result['tombstone_count'], 1)
        index = LayaIndexA(candidate)
        try:
            rows = index.db.execute('SELECT path FROM membership').fetchall()
            self.assertEqual(rows, [('live.md',)])
            self.assertEqual(index.db.execute('SELECT COUNT(*) FROM head').fetchone()[0], 0)
        finally:
            index.close()

    def test_stale_active_document_dropped_by_current_manifest(self):
        source = self.source(documents=[('docs', 'missing.md', 'old', 1)])
        backup = self.root / 'backup.sqlite'
        backup_qmd(source, QMD_253, 'docs', backup)
        result = self.prepare(backup, {})
        self.assertEqual((result['deleted_count'], result['staged_count']), (1, 0))

    def test_new_or_changed_current_file_refused(self):
        source = self.source()
        backup = self.root / 'backup.sqlite'
        backup_qmd(source, QMD_253, 'docs', backup)
        for live, reason in (({'new.md': 'new'}, 'current_manifest_unindexed'),
                             ({'live.md': 'modified'}, 'current_manifest_changed')):
            with self.assertRaisesRegex(QmdMigrationBlocked, reason):
                self.prepare(backup, live)
            self.assertFalse((self.root / 'candidate.sqlite').exists())

    def test_vector_provenance_never_reused(self):
        hash_ = digest('astral manuscript')
        source = self.source(vectors=[(hash_, 0, 0, 'old-model', 'old-fingerprint',
                                       1, '2026-01-01')])
        backup = self.root / 'backup.sqlite'
        backup_qmd(source, QMD_253, 'docs', backup)
        result = self.prepare(backup, {'live.md': 'astral manuscript'})
        self.assertEqual(result['needs_reembed'], [hash_])
        self.assertFalse(result['vector_reuse'])
        self.assertFalse(result['cutover_ready'])

    def test_opt_out_root_and_backup_digest_block_candidate(self):
        source = self.source()
        backup = self.root / 'backup.sqlite'
        backup_qmd(source, QMD_253, 'docs', backup)
        for options, reason in (({'migration_allowed': False}, 'invalid_migration_request'),
                                ({'expected_collection_root': '/synthetic/worktree-b'},
                                 'collection_root_mismatch'),
                                ({'expected_source_digest': '0' * 64},
                                 'backup_digest_mismatch')):
            with self.assertRaisesRegex(QmdMigrationBlocked, reason):
                self.prepare(backup, {'live.md': 'astral manuscript'}, **options)
            self.assertFalse((self.root / 'candidate.sqlite').exists())

    def test_candidate_no_clobber(self):
        source = self.source()
        backup = self.root / 'backup.sqlite'
        backup_qmd(source, QMD_253, 'docs', backup)
        candidate = self.root / 'candidate.sqlite'
        candidate.write_text('ownership witness')
        with self.assertRaisesRegex(QmdMigrationBlocked, 'candidate_exists'):
            self.prepare(backup, {'live.md': 'astral manuscript'})
        self.assertEqual(candidate.read_text(), 'ownership witness')

    def test_candidate_creation_race_preserves_other_owner(self):
        source = self.source()
        backup = self.root / 'backup.sqlite'
        backup_qmd(source, QMD_253, 'docs', backup)
        candidate = self.root / 'candidate.sqlite'
        real = laya_qmd_migration.LayaIndexA

        def racing(path, *, create):
            with sqlite3.connect(candidate) as other:
                other.execute('CREATE TABLE marker(value TEXT)')
                other.execute("INSERT INTO marker VALUES('theirs')")
            return real(path, create=create)

        with mock.patch.object(laya_qmd_migration, 'LayaIndexA', racing):
            with self.assertRaisesRegex(QmdMigrationBlocked, 'candidate_exists'):
                self.prepare(backup, {'live.md': 'astral manuscript'})
        with sqlite3.connect(candidate) as other:
            self.assertEqual(other.execute('SELECT value FROM marker').fetchall(),
                             [('theirs',)])
        self.assertEqual(list(self.root.glob('.qmd-candidate-*')), [])

    def test_stage_failure_leaves_no_candidate(self):
        source = self.source()
        backup = self.root / 'backup.sqlite'
        backup_qmd(source, QMD_253, 'docs', backup)
        with mock.patch.object(laya_qmd_migration.LayaIndexA, 'stage',
                               side_effect=RuntimeError('synthetic_fault')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic_fault'):
                self.prepare(backup, {'live.md': 'astral manuscript'})
        self.assertFalse((self.root / 'candidate.sqlite').exists())
        self.assertEqual(list(self.root.glob('.qmd-candidate-*')), [])

    def test_invalid_fts_variant_refused(self):
        source = self.source()
        with sqlite3.connect(source) as db:
            db.execute('DROP TABLE documents_fts')
            db.execute("CREATE VIRTUAL TABLE documents_fts USING fts5(name,body)")
        with self.assertRaisesRegex(QmdMigrationBlocked, 'source_schema_mismatch'):
            inspect_qmd(source, QMD_253, 'docs')

    def test_source_symlink_refused(self):
        source = self.source()
        os.symlink(source, self.root / 'alias.sqlite')
        with self.assertRaisesRegex(QmdMigrationBlocked, 'source_missing_or_symlink'):
            inspect_qmd(self.root / 'alias.sqlite', QMD_253, 'docs')

    def test_precomputed_full_scope_vector_seals_v5_candidate(self):
        source = self.source()
        backup = self.root / 'backup.sqlite'
        backup_qmd(source, QMD_253, 'docs', backup)
        options = self.vector_options(source, backup)
        result = self.prepare(backup, {'live.md': 'astral manuscript'}, **options)
        self.assertTrue(result['candidate_ready'])
        self.assertEqual(result['needs_reembed'], [])
        self.assertFalse(result['cutover_ready'])
        self.assertFalse(result['vector_reuse'])
        index = LayaIndexA(result['candidate'])
        try:
            seal = verify_generation_seal(index, 'gen-1')
            self.assertEqual(seal['profile_id'], result['vector_profile_id'])
            self.assertEqual(seal['rows_count'], 1)
            self.assertEqual(index.db.execute('SELECT COUNT(*) FROM vector_chunk_sets').fetchone()[0], 1)
            self.assertEqual(index.db.execute('SELECT COUNT(*) FROM head').fetchone()[0], 0)
        finally:
            index.close()

    def test_distinct_qmd_titles_sharing_body_get_separate_vectors(self):
        source = self.source(documents=[('docs', 'a.md', 'same body', 1),
                                        ('docs', 'b.md', 'same body', 1)])
        backup = self.root / 'backup.sqlite'
        backup_qmd(source, QMD_253, 'docs', backup)
        files = {'a.md': 'same body', 'b.md': 'same body'}
        result = self.prepare(backup, files,
                              **self.vector_options(source, backup, files=files))
        index = LayaIndexA(result['candidate'])
        try:
            self.assertEqual(verify_generation_seal(index, 'gen-1')['rows_count'], 2)
            self.assertEqual(index.db.execute('SELECT DISTINCT title FROM vector_chunks '
                                              'ORDER BY title').fetchall(),
                             [('a',), ('b',)])
            self.assertEqual(index.db.execute('SELECT COUNT(*) FROM vector_chunk_sets').fetchone()[0], 2)
        finally:
            index.close()

    def test_same_hash_and_title_reuses_one_chunk_for_two_paths(self):
        body = '# Shared\nsame body'
        source = self.source(documents=[('docs', 'a.md', body, 1),
                                        ('docs', 'b.md', body, 1)])
        backup = self.root / 'backup.sqlite'
        backup_qmd(source, QMD_253, 'docs', backup)
        files = {'a.md': body, 'b.md': body}
        options = self.vector_options(source, backup, files=files)
        self.assertEqual(len(options['vector_batch']['chunks']), 1)
        result = self.prepare(backup, files, **options)
        index = LayaIndexA(result['candidate'])
        try:
            self.assertEqual(verify_generation_seal(index, 'gen-1')['rows_count'], 2)
            self.assertEqual(index.db.execute('SELECT COUNT(*) FROM vector_chunks').fetchone()[0], 1)
            self.assertEqual(index.db.execute('SELECT COUNT(*) FROM vector_chunk_sets').fetchone()[0], 1)
        finally:
            index.close()

    def test_missing_one_title_variant_blocks_candidate(self):
        source = self.source(documents=[('docs', 'a.md', 'same body', 1),
                                        ('docs', 'b.md', 'same body', 1)])
        backup = self.root / 'backup.sqlite'
        backup_qmd(source, QMD_253, 'docs', backup)
        files = {'a.md': 'same body', 'b.md': 'same body'}
        options = self.vector_options(source, backup, files=files)
        options['vector_batch']['chunks'].pop()
        with self.assertRaisesRegex(QmdMigrationBlocked, 'vector_coverage_incomplete'):
            self.prepare(backup, files, **options)
        self.assertFalse((self.root / 'candidate.sqlite').exists())

    def test_qmd_title_must_match_current_markdown_title(self):
        source = self.source()
        with sqlite3.connect(source) as db:
            db.execute("UPDATE documents SET title='foreign title' WHERE path='live.md'")
        backup = self.root / 'backup.sqlite'
        backup_qmd(source, QMD_253, 'docs', backup)
        with self.assertRaisesRegex(QmdMigrationBlocked, 'source_title_mismatch'):
            self.prepare(backup, {'live.md': 'astral manuscript'},
                         **self.vector_options(source, backup))
        self.assertFalse((self.root / 'candidate.sqlite').exists())

    def test_prepared_qmd_candidate_publish_requires_sealed_source_and_reopens(self):
        """Exercise the disposable A-int boundary, not installer cutover."""
        source = self.source()
        backup = self.root / 'backup.sqlite'
        backup_qmd(source, QMD_253, 'docs', backup)
        result = self.prepare(backup, {'live.md': 'astral manuscript'},
                              **self.vector_options(source, backup))
        candidate = result['candidate']
        index = LayaIndexA(candidate)
        try:
            proof = {'source_manifest': result['source_manifest'],
                     'claim_digest': result['source_digest'], 'claim_epoch': 0,
                     'require_vector': True}
            self.assertEqual(index.publish(None, 'gen-1', 'migration-1',
                **{**proof, 'source_manifest': '0' * 64}), 'index_stale')
            self.assertEqual(index.db.execute('SELECT COUNT(*) FROM head').fetchone()[0], 0)
            self.assertEqual(index.publish(None, 'gen-1', 'migration-1', **proof),
                             'published')
            self.assertEqual(index.publish(None, 'gen-1', 'migration-1', **proof),
                             'replayed')
        finally:
            index.close()
        reopened = LayaIndexA(candidate)
        try:
            reopened.inspect()
            self.assertEqual(reopened.db.execute('PRAGMA integrity_check').fetchone()[0],
                             'ok')
            self.assertEqual(reopened.pin('scope-a')[1], 'gen-1')
            self.assertEqual(verify_generation_seal(reopened, 'gen-1')['rows_count'], 1)
        finally:
            reopened.close()

    def test_vector_scope_and_provider_require_explicit_approval(self):
        source = self.source()
        backup = self.root / 'backup.sqlite'
        backup_qmd(source, QMD_253, 'docs', backup)
        options = self.vector_options(source, backup)
        for change in ({'provider_approved': False},
                       {'scope_collections': ('docs', 'other')},
                       {'scope_collections': ('other',)},
                       {'current_source_db': None}):
            with self.assertRaisesRegex(QmdMigrationBlocked,
                                        'vector_provider_or_scope_unapproved'):
                self.prepare(backup, {'live.md': 'astral manuscript'},
                             **{**options, **change})
            self.assertFalse((self.root / 'candidate.sqlite').exists())

    def test_vector_full_hash_coverage_and_no_foreign_hash(self):
        source = self.source()
        backup = self.root / 'backup.sqlite'
        backup_qmd(source, QMD_253, 'docs', backup)
        for chunks in ([], [{'hash': digest('other worktree'), 'title': 'live', 'seq': 0,
                              'pos': 0, 'vector': [1.0, 0.0]}],
                       [{'hash': digest('old deleted chapter'), 'title': 'gone', 'seq': 0,
                         'pos': 0, 'vector': [1.0, 0.0]}]):
            options = self.vector_options(source, backup, chunks=chunks)
            with self.assertRaisesRegex(QmdMigrationBlocked,
                                        'vector_coverage_incomplete|invalid_vector_batch'):
                self.prepare(backup, {'live.md': 'astral manuscript'}, **options)
            self.assertFalse((self.root / 'candidate.sqlite').exists())

    def test_legacy_six_hex_fingerprint_cannot_be_approved(self):
        source = self.source()
        backup = self.root / 'backup.sqlite'
        backup_qmd(source, QMD_253, 'docs', backup)
        options = self.vector_options(source, backup)
        options['vector_batch']['embedding_fingerprint'] = 'a1b2c3'
        with self.assertRaisesRegex(Exception, 'invalid_vector_profile'):
            self.prepare(backup, {'live.md': 'astral manuscript'}, **options)
        self.assertFalse((self.root / 'candidate.sqlite').exists())

    def test_source_advance_after_backup_blocks_vector_candidate(self):
        source = self.source()
        backup = self.root / 'backup.sqlite'
        backup_qmd(source, QMD_253, 'docs', backup)
        options = self.vector_options(source, backup)
        with sqlite3.connect(source) as db:
            db.execute('INSERT INTO content VALUES(?,?,?)',
                       (digest('later'), 'later', '2026-01-01'))
            db.execute('''INSERT INTO documents(collection,path,title,hash,
                created_at,modified_at,active) VALUES(?,?,?,?,?,?,?)''',
                ('docs', 'later.md', 'later', digest('later'),
                 '2026-01-01', '2026-01-01', 1))
        with self.assertRaisesRegex(QmdMigrationBlocked, 'source_changed_since_backup'):
            self.prepare(backup, {'live.md': 'astral manuscript'}, **options)
        self.assertFalse((self.root / 'candidate.sqlite').exists())

    def test_fork_precomputed_candidate(self):
        source = self.source(FORK_F463)
        backup = self.root / 'backup.sqlite'
        backup_qmd(source, FORK_F463, 'docs', backup)
        files = {'live.md': 'astral manuscript'}
        options = self.vector_options(source, backup, files=files,
                                      source_format=FORK_F463)
        result = prepare_qmd_candidate(backup, FORK_F463, 'docs', files,
            self.root / 'candidate.sqlite', 'scope-a', 'gen-1',
            migration_allowed=True, expected_collection_root='/synthetic/worktree-a',
            expected_source_digest=inspect_qmd(backup, FORK_F463, 'docs')['digest'],
            **options)
        self.assertTrue(result['candidate_ready'])



if __name__ == '__main__':
    unittest.main()

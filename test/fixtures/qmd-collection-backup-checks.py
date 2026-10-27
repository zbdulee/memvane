"""Disposable QMD source fixtures; never opens a QMD installation or user DB."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
from qmd_collection_backup import (QMD_253, FORK_F463, QmdBackupBlocked,
                                   inspect_qmd_collections, backup_qmd_collections,
                                   verify_restore_collections)
from laya_index_vector import qmd_markdown_title


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


class QmdCollectionBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='qmd-collection-backup-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def source(self, kind=QMD_253, **kw):
        path = self.root / 'source.sqlite'
        make_source(path, kind, **kw)
        return path

    def test_upstream_inventory_scoped_and_tombstone(self):
        inv = inspect_qmd_collections(self.source(), QMD_253, ('docs',))
        self.assertEqual((inv['active_count'], inv['tombstone_count']), (1, 1))
        self.assertEqual([x[0] for x in inv['documents']], ['docs/gone.md', 'docs/live.md'])
        self.assertEqual(inv['needs_reembed'], [digest('astral manuscript')])

    def test_fork_inventory_exact_version_and_columns(self):
        inv = inspect_qmd_collections(self.source(FORK_F463), FORK_F463, ('docs',))
        self.assertEqual(inv['source_format'], FORK_F463)
        self.assertEqual(inv['active_count'], 1)

    def test_multi_collection_backup_restores_one_digest(self):
        source = self.source(documents=[('docs', 'live.md', 'astral manuscript', 1),
                                        ('wiki', 'card.md', '# Card\nsummary', 1),
                                        ('other', 'unrelated.md', 'untouched', 1)])
        backup = self.root / 'backup.sqlite'
        names = ('docs', 'wiki')
        proof = backup_qmd_collections(source, QMD_253, names, backup)
        inventory = inspect_qmd_collections(backup, QMD_253, names)
        self.assertEqual(proof['source_digest'], inventory['digest'])
        self.assertTrue(verify_restore_collections(
            backup, QMD_253, names, inventory['digest'])['verified'])
        with sqlite3.connect(source) as db:
            db.execute("UPDATE documents SET title='changed wiki title' "
                       "WHERE collection='wiki' AND path='card.md'")
        self.assertNotEqual(inspect_qmd_collections(
            source, QMD_253, names)['digest'], inventory['digest'])
        with self.assertRaisesRegex(QmdBackupBlocked, 'restore_digest_mismatch'):
            verify_restore_collections(backup, QMD_253, names, '0' * 64)

    def test_backup_is_no_clobber(self):
        source = self.source()
        backup = self.root / 'backup.sqlite'
        backup_qmd_collections(source, QMD_253, ('docs',), backup)
        with self.assertRaisesRegex(QmdBackupBlocked, 'backup_target_unavailable'):
            backup_qmd_collections(source, QMD_253, ('docs',), backup)

    def test_wrong_format_refused_both_directions(self):
        with self.assertRaises(QmdBackupBlocked):
            inspect_qmd_collections(self.source(FORK_F463), QMD_253, ('docs',))
        other = self.root / 'upstream.sqlite'
        make_source(other, QMD_253)
        with self.assertRaises(QmdBackupBlocked):
            inspect_qmd_collections(other, FORK_F463, ('docs',))

    def test_unknown_version_missing_collection_and_bad_set_refused(self):
        source = self.source()
        with self.assertRaisesRegex(QmdBackupBlocked, 'unsupported_source_version'):
            inspect_qmd_collections(source, 'qmd-2.6', ('docs',))
        with self.assertRaisesRegex(QmdBackupBlocked, 'collection_unrecorded'):
            inspect_qmd_collections(source, QMD_253, ('missing',))
        with self.assertRaisesRegex(QmdBackupBlocked, 'invalid_collection_set'):
            inspect_qmd_collections(source, QMD_253, ('docs', 'docs'))

    def test_changed_header_and_content_hash_refused(self):
        source = self.source()
        with sqlite3.connect(source) as db:
            db.execute("UPDATE content SET doc='altered' WHERE doc='astral manuscript'")
        with self.assertRaisesRegex(QmdBackupBlocked, 'source_document_invalid'):
            inspect_qmd_collections(source, QMD_253, ('docs',))
        with sqlite3.connect(source) as db:
            db.execute('PRAGMA user_version=2')
        with self.assertRaisesRegex(QmdBackupBlocked, 'source_header_mismatch'):
            inspect_qmd_collections(source, QMD_253, ('docs',))

    def test_read_only_inventory_does_not_change_source_bytes(self):
        source = self.source()
        before = source.read_bytes()
        inspect_qmd_collections(source, QMD_253, ('docs',))
        self.assertEqual(source.read_bytes(), before)

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
            proof = backup_qmd_collections(source, QMD_253, ('docs',),
                                           self.root / 'backup.sqlite')
            self.assertEqual(inspect_qmd_collections(
                proof['backup'], QMD_253, ('docs',))['active_count'], 2)
        finally:
            writer.close()


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
"""Disposable synthetic migration rehearsal. No operating paths or QMD DB."""

from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

CORE = Path(__file__).resolve().parents[2] / 'core'
sys.path.insert(0, str(CORE))
from laya_index_a import LayaIndexA
from laya_index_migration_rehearsal import (MARKER, MigrationBlocked,
    MigrationRehearsal, create_synthetic_legacy)


class SyntheticMigrationChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='laya-migration-synthetic-')
        self.root = Path(self.temp.name).resolve()
        (self.root / MARKER).write_text('synthetic-only-v1\n', encoding='ascii')
        self.source = self.root / 'source'
        self.source.mkdir()
        self.body = 'hello migration\n'
        (self.source / 'a.md').write_text(self.body, encoding='utf-8')
        self.hash = hashlib.sha256(self.body.encode()).hexdigest()
        create_synthetic_legacy(self.root / 'legacy.sqlite',
                                [('alpha', 'a.md', self.hash, 1)])
        self.rehearsal = MigrationRehearsal(self.root)

    def tearDown(self):
        self.rehearsal.close()
        self.temp.cleanup()

    def reopen(self):
        self.rehearsal.close()
        self.rehearsal = MigrationRehearsal(self.root)

    def test_backup_restore_candidate_cutover_and_prewrite_rollback(self):
        backup_digest = self.rehearsal.backup_snapshot()
        proof = self.rehearsal.restore_proof()
        self.assertEqual(proof['rows'], 1)
        self.assertEqual(proof['digest'], backup_digest)
        self.assertEqual(proof['integrity'], 'ok')
        result = self.rehearsal.prepare(self.source, 'alpha')
        self.assertEqual(result['backup_digest'], backup_digest)
        self.assertEqual(self.rehearsal.state()['state'], 'candidate_ready')
        with closing(LayaIndexA(self.root / 'candidate.sqlite')) as index:
            self.assertEqual(index.inspect()['generations'], 1)
            token, gen = index.pin('alpha')
            self.assertEqual(gen, 'migrated-1')
            hits = index.search(token, 'hello', 5)
            self.assertEqual([(h['scope'], h['path'], h['hash']) for h in hits],
                             [('alpha', 'a.md', self.hash)])
            index.release(token)
        self.assertEqual(self.rehearsal.cutover(self.source, 'alpha'), 'new_active')
        self.reopen()
        self.assertEqual(self.rehearsal.state()['pointer'], 'new')
        self.assertEqual(self.rehearsal.rollback(), 'restored_old')
        self.reopen()
        self.assertEqual(self.rehearsal.state()['pointer'], 'old')
        self.assertEqual(self.rehearsal.state()['state'], 'restored_old')

    def test_backup_and_candidate_are_never_clobbered(self):
        self.rehearsal.backup_snapshot()
        with self.assertRaisesRegex(MigrationBlocked, 'backup_already_exists'):
            self.rehearsal.backup_snapshot()
        first = self.rehearsal.prepare(self.source, 'alpha')
        again = self.rehearsal.prepare(self.source, 'alpha')
        self.assertEqual(again['result'], 'already_prepared')
        self.assertEqual(again['candidate_digest'], first['candidate_digest'])
        with self.assertRaisesRegex(MigrationBlocked, 'migration_conflict'):
            self.rehearsal.prepare(self.source, 'alpha', claim='different-claim')
        self.assertEqual(self.rehearsal.state()['pointer'], 'old')

    def test_source_hash_mismatch_blocks_candidate(self):
        (self.source / 'a.md').write_text('changed\n', encoding='utf-8')
        with self.assertRaisesRegex(MigrationBlocked, 'source_or_hash_mismatch'):
            self.rehearsal.prepare(self.source, 'alpha')
        self.assertFalse((self.root / 'candidate.sqlite').exists())
        self.assertEqual(self.rehearsal.state()['pointer'], 'old')

    def test_opt_out_blocks_candidate_before_creation(self):
        with sqlite3.connect(self.root / 'legacy.sqlite') as db:
            db.execute("UPDATE policies SET opt_out=1 WHERE scope='alpha'")
        with self.assertRaisesRegex(MigrationBlocked, 'opt_out'):
            self.rehearsal.prepare(self.source, 'alpha')
        self.assertFalse(self.rehearsal.state()['candidate_exists'])

    def test_worktree_scope_mismatch_blocks_candidate(self):
        with self.assertRaisesRegex(MigrationBlocked, 'worktree_scope_mismatch'):
            self.rehearsal.prepare(self.source, 'alpha', worktree='another-tree')
        self.assertFalse(self.rehearsal.state()['candidate_exists'])

    def test_tombstoned_document_cannot_reappear_in_candidate(self):
        with sqlite3.connect(self.root / 'legacy.sqlite') as db:
            db.execute('INSERT INTO deletions VALUES(?,?)', ('alpha', 'a.md'))
        with self.assertRaisesRegex(MigrationBlocked, 'deleted_document_resurrection'):
            self.rehearsal.prepare(self.source, 'alpha')
        self.assertFalse(self.rehearsal.state()['candidate_exists'])

    def test_case_variant_tombstone_blocks_same_document(self):
        with sqlite3.connect(self.root / 'legacy.sqlite') as db:
            db.execute('INSERT INTO deletions VALUES(?,?)', ('alpha', 'A.md'))
        with self.assertRaisesRegex(MigrationBlocked, 'deleted_document_resurrection'):
            self.rehearsal.prepare(self.source, 'alpha')

    def test_noncanonical_tombstone_is_rejected(self):
        with sqlite3.connect(self.root / 'legacy.sqlite') as db:
            db.execute('INSERT INTO deletions VALUES(?,?)', ('alpha', './a.md'))
        with self.assertRaisesRegex(MigrationBlocked, 'legacy_path_noncanonical'):
            self.rehearsal.prepare(self.source, 'alpha')

    def test_second_worktree_remains_outside_migrated_scope(self):
        with sqlite3.connect(self.root / 'legacy.sqlite') as db:
            db.execute('INSERT INTO docs VALUES(?,?,?,1)',
                       ('beta', 'b.md', self.hash))
            db.execute('INSERT INTO policies VALUES(?,?,0)', ('beta', 'beta-tree'))
        self.rehearsal.prepare(self.source, 'alpha', worktree='alpha')
        with closing(LayaIndexA(self.root / 'candidate.sqlite')) as index:
            self.assertIsNone(index.db.execute("SELECT gen FROM head WHERE scope='beta'").fetchone())
            self.assertEqual(index.db.execute('SELECT COUNT(*) FROM membership').fetchone()[0], 1)
        with self.assertRaisesRegex(MigrationBlocked, 'worktree_scope_mismatch'):
            self.rehearsal.prepare(self.source, 'alpha', worktree='beta-tree')
        self.assertEqual(self.rehearsal.state()['pointer'], 'old')

    def test_interruption_after_backup_reopens_and_resumes(self):
        with self.assertRaisesRegex(MigrationBlocked, 'injected_after_backup'):
            self.rehearsal.prepare(self.source, 'alpha', fault_at='after_backup')
        self.reopen()
        self.assertTrue(self.rehearsal.state()['backup_exists'])
        self.assertFalse(self.rehearsal.state()['candidate_exists'])
        self.assertEqual(self.rehearsal.prepare(self.source, 'alpha')['state'],
                         'candidate_ready')

    def test_interruption_after_candidate_create_resumes_same_plan(self):
        with self.assertRaisesRegex(MigrationBlocked, 'injected_after_candidate_create'):
            self.rehearsal.prepare(self.source, 'alpha', fault_at='after_candidate_create')
        self.reopen()
        state = self.rehearsal.state()
        self.assertEqual(state['pointer'], 'old')
        self.assertTrue(state['candidate_unselected'])
        self.assertEqual(state['state'], 'preparing')
        with self.assertRaisesRegex(MigrationBlocked, 'migration_conflict'):
            self.rehearsal.prepare(self.source, 'alpha', claim='other')
        self.assertEqual(self.rehearsal.prepare(self.source, 'alpha')['state'],
                         'candidate_ready')

    def test_foreign_candidate_after_plan_commit_is_not_adopted(self):
        with self.assertRaisesRegex(MigrationBlocked, 'injected_after_preparing_commit'):
            self.rehearsal.prepare(self.source, 'alpha', fault_at='after_preparing_commit')
        with closing(LayaIndexA(self.root / 'candidate.sqlite', create=True)) as foreign:
            foreign.stage('foreign-gen', 'beta', [('b.md', 'foreign body', True)])
            foreign.publish(None, 'foreign-gen', 'foreign-claim')
        self.reopen()
        with self.assertRaisesRegex(MigrationBlocked, 'candidate_not_owned'):
            self.rehearsal.prepare(self.source, 'alpha')
        with closing(LayaIndexA(self.root / 'candidate.sqlite')) as foreign:
            self.assertEqual(foreign.receipt('foreign-claim'), ('beta', 'foreign-gen'))

    def test_interruption_after_stage_resumes_without_duplicate_generation(self):
        with self.assertRaisesRegex(MigrationBlocked, 'injected_after_stage'):
            self.rehearsal.prepare(self.source, 'alpha', fault_at='after_stage')
        self.reopen()
        state = self.rehearsal.state()
        self.assertTrue(state['candidate_unselected'])
        self.assertEqual(state['pointer'], 'old')
        with closing(LayaIndexA(self.root / 'candidate.sqlite')) as index:
            self.assertEqual(index.inspect()['generations'], 1)
            self.assertIsNone(index.db.execute('SELECT gen FROM head').fetchone())
        self.assertEqual(self.rehearsal.prepare(self.source, 'alpha')['state'],
                         'candidate_ready')
        with closing(LayaIndexA(self.root / 'candidate.sqlite')) as index:
            self.assertEqual(index.inspect()['generations'], 1)
            self.assertEqual(index.db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0], 1)

    def test_published_candidate_before_journal_commit_replays_receipt(self):
        with self.assertRaisesRegex(MigrationBlocked, 'injected_after_publish'):
            self.rehearsal.prepare(self.source, 'alpha', fault_at='after_publish')
        self.reopen()
        self.assertEqual(self.rehearsal.state()['state'], 'preparing')
        self.assertEqual(self.rehearsal.state()['pointer'], 'old')
        self.assertEqual(self.rehearsal.prepare(self.source, 'alpha')['state'],
                         'candidate_ready')
        with closing(LayaIndexA(self.root / 'candidate.sqlite')) as index:
            self.assertEqual(index.db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0], 1)
            self.assertEqual(index.inspect()['generations'], 1)

    def test_source_changes_before_cutover_keep_old_selected(self):
        self.rehearsal.prepare(self.source, 'alpha')
        (self.source / 'a.md').write_text('new revision\n', encoding='utf-8')
        with self.assertRaisesRegex(MigrationBlocked, 'candidate_source_stale'):
            self.rehearsal.cutover(self.source, 'alpha')
        self.assertEqual(self.rehearsal.state()['pointer'], 'old')

    def test_source_deletion_after_cutover_blocks_old_version_resurrection(self):
        self.rehearsal.prepare(self.source, 'alpha')
        self.rehearsal.cutover(self.source, 'alpha')
        (self.source / 'a.md').unlink()
        with self.assertRaisesRegex(MigrationBlocked, 'rollback_blocked_source_changed'):
            self.rehearsal.rollback()
        self.assertEqual(self.rehearsal.state()['pointer'], 'new')

    def test_incompatible_embedding_requires_regeneration_without_reuse(self):
        with sqlite3.connect(self.root / 'legacy.sqlite') as db:
            db.execute('INSERT INTO embeddings VALUES(?,?,?,?,?,?,?)',
                       ('alpha', 'a.md', 'old-model', 384, 'old-fp', self.hash, 'deadbeef'))
        result = self.rehearsal.prepare(
            self.source, 'alpha', expected_embedding=('new-model', 768, 'new-fp'))
        self.assertEqual(result['state'], 'candidate_pending_embeddings')
        self.assertEqual(result['embedding_metadata_verified'], 0)
        self.assertEqual(result['embedding_reuse'], 0)
        self.assertEqual(result['embedding_regeneration_required'], 1)
        with closing(LayaIndexA(self.root / 'candidate.sqlite')) as index:
            self.assertIsNone(index.db.execute(
                "SELECT name FROM sqlite_master WHERE name='embeddings'").fetchone())
        with self.assertRaisesRegex(MigrationBlocked, 'candidate_not_ready'):
            self.rehearsal.cutover(self.source, 'alpha')

    def test_matching_embedding_metadata_still_does_not_import_vector(self):
        with sqlite3.connect(self.root / 'legacy.sqlite') as db:
            db.execute('INSERT INTO embeddings VALUES(?,?,?,?,?,?,?)',
                       ('alpha', 'a.md', 'same-model', 384, 'same-fp', self.hash, 'cafebabe'))
        result = self.rehearsal.prepare(
            self.source, 'alpha', expected_embedding=('same-model', 384, 'same-fp'))
        self.assertEqual(result['embedding_metadata_verified'], 1)
        self.assertEqual(result['embedding_reuse'], 0)
        self.assertEqual(result['embedding_regeneration_required'], 1)
        self.assertEqual(self.rehearsal.state()['pointer'], 'old')

    def test_embedding_for_nonmember_does_not_block_fts_candidate(self):
        with sqlite3.connect(self.root / 'legacy.sqlite') as db:
            db.execute('INSERT INTO embeddings VALUES(?,?,?,?,?,?,?)',
                       ('alpha', 'deleted.md', 'old', 384, 'fp', self.hash, 'cafebabe'))
            db.execute('INSERT INTO deletions VALUES(?,?)', ('alpha', 'deleted.md'))
        result = self.rehearsal.prepare(self.source, 'alpha')
        self.assertEqual(result['state'], 'candidate_ready')
        self.assertEqual(result['embedding_regeneration_required'], 0)

    def test_old_writer_after_backup_blocks_cutover(self):
        self.rehearsal.prepare(self.source, 'alpha')
        with sqlite3.connect(self.root / 'legacy.sqlite') as db:
            db.execute('INSERT INTO docs VALUES(?,?,?,1)',
                       ('alpha', 'new.md', self.hash))
        with self.assertRaisesRegex(MigrationBlocked, 'legacy_changed_after_backup'):
            self.rehearsal.cutover(self.source, 'alpha')
        self.assertEqual(self.rehearsal.state()['pointer'], 'old')

    def test_sqlite_backup_uses_committed_snapshot(self):
        with closing(sqlite3.connect(self.root / 'legacy.sqlite', isolation_level=None)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            writer.execute('DELETE FROM docs')
            self.rehearsal.backup_snapshot()
            writer.rollback()
        proof = self.rehearsal.restore_proof()
        self.assertEqual(proof['rows'], 1)

    def test_backup_replacement_cannot_pass_restore_proof(self):
        self.rehearsal.backup_snapshot()
        (self.root / 'backup.sqlite').unlink()
        create_synthetic_legacy(self.root / 'backup.sqlite',
                                [('alpha', 'different.md', self.hash, 1)])
        with self.assertRaisesRegex(MigrationBlocked, 'restore_mismatch'):
            self.rehearsal.restore_proof()

    def test_restore_proof_is_idempotent_and_refuses_changed_restore(self):
        self.rehearsal.backup_snapshot()
        first = self.rehearsal.restore_proof()
        self.assertEqual(self.rehearsal.restore_proof(), first)
        (self.root / 'restore.sqlite').unlink()
        create_synthetic_legacy(self.root / 'restore.sqlite',
                                [('alpha', 'different.md', self.hash, 1)])
        with self.assertRaisesRegex(MigrationBlocked, 'restore_mismatch'):
            self.rehearsal.restore_proof()

    def test_write_marker_blocks_pointer_rollback(self):
        self.rehearsal.prepare(self.source, 'alpha')
        self.rehearsal.cutover(self.source, 'alpha')
        self.assertEqual(self.rehearsal.record_new_write(), 'new_write_seen')
        self.reopen()
        with self.assertRaisesRegex(MigrationBlocked, 'rollback_blocked_after_write_or_state'):
            self.rehearsal.rollback()
        self.assertEqual(self.rehearsal.state()['pointer'], 'new')

    def test_candidate_db_write_without_marker_blocks_pointer_rollback(self):
        self.rehearsal.prepare(self.source, 'alpha')
        self.rehearsal.cutover(self.source, 'alpha')
        with closing(LayaIndexA(self.root / 'candidate.sqlite')) as index:
            self.assertEqual(index.stage('later', 'alpha', [('a.md', self.body, True)]),
                             'prepared')
            self.assertEqual(index.publish('migrated-1', 'later', 'later-claim'),
                             'published')
        with self.assertRaisesRegex(MigrationBlocked, 'rollback_blocked_candidate_changed'):
            self.rehearsal.rollback()
        self.assertEqual(self.rehearsal.state()['pointer'], 'new')

    def test_non_synthetic_database_and_unmarked_root_rejected(self):
        self.rehearsal.close()
        (self.root / MARKER).unlink()
        with self.assertRaisesRegex(MigrationBlocked, 'unmarked_rehearsal_root'):
            MigrationRehearsal(self.root)
        (self.root / MARKER).write_text('synthetic-only-v1\n', encoding='ascii')
        self.rehearsal = MigrationRehearsal(self.root)
        with sqlite3.connect(self.root / 'legacy.sqlite') as db:
            db.execute('PRAGMA application_id=0')
        with self.assertRaisesRegex(MigrationBlocked, 'legacy_schema_or_integrity'):
            self.rehearsal.backup_snapshot()
        self.assertFalse(self.rehearsal.state()['backup_exists'])

    def test_journal_with_missing_singleton_row_is_refused(self):
        self.rehearsal.close()
        (self.root / 'selection.sqlite').unlink()
        with sqlite3.connect(self.root / 'selection.sqlite') as db:
            db.execute('PRAGMA application_id=%d' % 0x4C594D4A)
            db.execute('PRAGMA user_version=1')
            db.execute('CREATE TABLE selection(id INTEGER PRIMARY KEY CHECK(id=1), '
                       'state TEXT NOT NULL, pointer TEXT NOT NULL, '
                       'candidate_digest TEXT, new_write INTEGER NOT NULL, '
                       'plan_digest TEXT, scope TEXT, source_root TEXT, '
                       'source_manifest TEXT, backup_digest TEXT, '
                       'candidate_incarnation TEXT)')
        with self.assertRaisesRegex(MigrationBlocked, 'journal_schema_or_integrity'):
            MigrationRehearsal(self.root)

    def test_rollback_rechecks_stored_root_symlink(self):
        self.rehearsal.prepare(self.source, 'alpha')
        self.rehearsal.cutover(self.source, 'alpha')
        renamed = self.root / 'source-renamed'
        self.source.rename(renamed)
        self.source.symlink_to(renamed, target_is_directory=True)
        with self.assertRaisesRegex(MigrationBlocked, 'source_outside_rehearsal'):
            self.rehearsal.rollback()
        self.assertEqual(self.rehearsal.state()['pointer'], 'new')


if __name__ == '__main__':
    unittest.main()

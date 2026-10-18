"""Synthetic QMD-origin transition journal; no installed QMD or project route."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import runpy
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'core'))

from laya_index_a import LayaIndexA, _digest as manifest_digest
from laya_index_claim_queue import ClaimQueue
from laya_index_migration_rehearsal import (MARKER, MigrationBlocked,
                                            QmdRouteRehearsal)
from laya_qmd_migration import (QMD_253, backup_qmd, inspect_qmd,
                                prepare_qmd_candidate)
from laya_index_vector import qmd_markdown_title

helpers = runpy.run_path(str(Path(__file__).with_name('laya-qmd-migration-checks.py')))
make_source = helpers['make_source']
digest = helpers['digest']


class QmdRouteRehearsalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='laya-qmd-route-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / MARKER).write_text('synthetic-only-v1\n', encoding='ascii')
        self.source = self.root / 'qmd.sqlite'
        self.backup = self.root / 'backup.sqlite'
        self.candidate = self.root / 'candidate.sqlite'
        self.queue = self.root / 'queue.sqlite'
        make_source(self.source, QMD_253)
        backup_qmd(self.source, QMD_253, 'docs', self.backup)
        self.source_digest = inspect_qmd(self.source, QMD_253, 'docs')['digest']
        self.manifest = manifest_digest('scope-a',
                                        [('live.md', digest('astral manuscript'), 1)])
        batch = {'scope': 'scope-a', 'source_manifest': self.manifest,
                 'qmd_source_digest': self.source_digest,
                 'model_id': 'synthetic-approved', 'embedding_fingerprint': 'a' * 64,
                 'dimension': 2,
                 'chunks': [{'hash': digest('astral manuscript'),
                             'title': qmd_markdown_title('astral manuscript', 'live.md'),
                             'seq': 0,
                             'pos': 0, 'vector': [1.0, 0.0]}]}
        self.prepared = prepare_qmd_candidate(
            self.backup, QMD_253, 'docs', {'live.md': 'astral manuscript'},
            self.candidate, 'scope-a', 'gen-1', migration_allowed=True,
            expected_collection_root='/synthetic/worktree-a',
            expected_source_digest=self.source_digest, vector_batch=batch,
            provider_approved=True, current_source_db=self.source,
            scope_collections=('docs',))
        with closing(LayaIndexA(self.candidate)) as index:
            with closing(ClaimQueue.create(self.queue,
                                          index_incarnation=index.incarnation())):
                pass
        self.route = b'{"selected":"synthetic-laya"}\n'
        self.rehearsal = QmdRouteRehearsal(self.root)

    def bind(self):
        return self.rehearsal.bind(
            0, source_db=self.source, backup_db=self.backup,
            source_format=QMD_253, collection='docs',
            expected_digest=self.source_digest, candidate_db=self.candidate,
            queue_db=self.queue, scope='scope-a', generation='gen-1',
            source_manifest=self.manifest,
            vector_profile_id=self.prepared['vector_profile_id'],
            route_bytes=self.route,
            candidate_ready=self.prepared['candidate_ready'])

    def active(self):
        bound = self.bind()
        return self.rehearsal.activate_observed(bound['epoch'], self.route, True)

    def test_prewrite_restore_is_two_phase_and_reopens(self):
        active = self.active()
        self.assertEqual(active['origin'], 'legacy_qmd')
        self.assertEqual(active['phase'], 'synthetic_active')
        restoring = self.rehearsal.begin_restore(active['epoch'], self.route, True)
        self.assertEqual(QmdRouteRehearsal(self.root).recover(True)['phase'], 'restoring')
        restored = QmdRouteRehearsal(self.root).recover(False)
        self.assertEqual(restored['phase'], 'restored')
        self.assertEqual(QmdRouteRehearsal(self.root).inspect()['epoch'], 4)

    def test_after_first_new_write_requires_forward_recovery(self):
        active = self.active()
        written = self.rehearsal.mark_first_new_write(active['epoch'])
        self.assertEqual(written['phase'], 'new_write_seen')
        with self.assertRaisesRegex(MigrationBlocked, 'qmd_transition_phase_conflict'):
            self.rehearsal.begin_restore(written['epoch'], self.route, True)
        with self.assertRaisesRegex(MigrationBlocked, 'after_write_route_missing_forward_only'):
            QmdRouteRehearsal(self.root).recover(False)
        self.assertEqual(QmdRouteRehearsal(self.root).recover(True)['phase'],
                         'new_write_seen')

    def test_stale_epoch_and_route_identity_refused(self):
        bound = self.bind()
        with self.assertRaisesRegex(MigrationBlocked, 'stale_qmd_transition_cas'):
            self.rehearsal.activate_observed(0, self.route, True)
        with self.assertRaisesRegex(MigrationBlocked, 'synthetic_route_not_selected'):
            self.rehearsal.activate_observed(bound['epoch'], b'other route', True)
        with self.assertRaisesRegex(MigrationBlocked, 'synthetic_route_not_selected'):
            self.rehearsal.activate_observed(bound['epoch'], self.route, False)
        self.assertEqual(self.rehearsal.inspect()['phase'], 'candidate_bound')

    def test_source_change_blocks_activation(self):
        bound = self.bind()
        with sqlite3.connect(self.source) as db:
            db.execute("UPDATE documents SET title='changed' WHERE path='live.md'")
        with self.assertRaisesRegex(MigrationBlocked, 'qmd_source_changed'):
            self.rehearsal.activate_observed(bound['epoch'], self.route, True)

    def test_corrupted_backup_blocks_restore(self):
        active = self.active()
        restoring = self.rehearsal.begin_restore(active['epoch'], self.route, True)
        with sqlite3.connect(self.backup) as db:
            db.execute("UPDATE documents SET title='changed' WHERE path='live.md'")
        with self.assertRaises(Exception):
            self.rehearsal.finish_restore(restoring['epoch'], False)
        self.assertEqual(self.rehearsal.inspect()['phase'], 'restoring')

    def test_candidate_and_queue_inode_replacement_block_activation(self):
        bound = self.bind()
        replacement = self.root / 'copy.sqlite'
        replacement.write_bytes(self.candidate.read_bytes())
        self.candidate.unlink()
        replacement.rename(self.candidate)
        with self.assertRaisesRegex(MigrationBlocked, 'qmd_bound_file_replaced'):
            self.rehearsal.activate_observed(bound['epoch'], self.route, True)

    def test_queue_binding_and_unmarked_root_refused(self):
        with self.assertRaisesRegex(MigrationBlocked, 'unmarked_qmd_temp_root'):
            QmdRouteRehearsal(self.root / 'missing')
        self.queue = self.root / 'wrong-queue.sqlite'
        with closing(ClaimQueue.create(self.queue, index_incarnation='b' * 32)):
            pass
        with self.assertRaisesRegex(MigrationBlocked, 'qmd_queue_binding_mismatch'):
            self.bind()

    def test_active_route_loss_refused_without_restore(self):
        self.active()
        with self.assertRaisesRegex(MigrationBlocked,
                                    'active_route_missing_without_restore'):
            QmdRouteRehearsal(self.root).recover(False)


if __name__ == '__main__':
    unittest.main()

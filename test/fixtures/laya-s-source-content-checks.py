"""Synthetic source content identity contract; no project or operating data."""
import hashlib
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'core'))
import laya_blob_values as v
import laya_view_values as values
import laya_view_store as view_store
import laya_view_schema as view_schema
import laya_view_fts as view_fts


class ContentIdentityTests(unittest.TestCase):
    def test_identity_depends_only_on_content_digest(self):
        raw = '합성 카드 source\n'.encode('utf8')
        sha = hashlib.sha256(raw).hexdigest()
        first = values.source_content(sha, len(raw))
        self.assertEqual(first, values.source_content(sha, len(raw)))
        self.assertEqual(first['source_sha'], sha)
        self.assertEqual(first['byte_length'], len(raw))
        self.assertNotEqual(first['content_id'], values.source_content('0' * 64, len(raw))['content_id'])
        # Length is checked separately against the persisted row, never an identity input.
        self.assertEqual(first['content_id'], values.source_content(sha, len(raw) + 1)['content_id'])

    def test_existing_content_must_match_length_and_digest(self):
        row = values.source_content('a' * 64, 42)
        self.assertEqual(values.check_source_content(row, 'a' * 64, 42), row)
        for sha, length in [('a' * 64, 43), ('b' * 64, 42)]:
            with self.subTest(sha=sha, length=length), self.assertRaises(v.BlobStoreError):
                values.check_source_content(row, sha, length)
        for corrupt in [dict(row, content_id='0' * 64), dict(row, source_sha='bad'), dict(row, byte_length=-1)]:
            with self.subTest(corrupt=corrupt), self.assertRaises(v.BlobStoreError):
                values.check_source_content(corrupt, 'a' * 64, 42)

    def test_invalid_identity_inputs_rejected(self):
        for sha, length in [('bad', 0), ('A' * 64, 0), ('a' * 64, -1), ('a' * 64, True)]:
            with self.subTest(sha=sha, length=length), self.assertRaises(v.BlobStoreError):
                values.source_content(sha, length)

    def test_observation_separates_path_and_tracked_state_but_reuses_content(self):
        content = values.source_content('a' * 64, 7)['content_id']
        args = dict(worktree_id='1' * 32, branch_key='refs/heads/main', path='docs/a.md',
                    tracked_state='tracked', binding_state='observed', content_id=content,
                    legacy_fingerprint_id=None)
        first = values.source_observation(**args)
        self.assertEqual(first, values.source_observation(**args))
        for key, value in [('path', 'docs/b.md'), ('tracked_state', 'untracked'),
                           ('branch_key', 'refs/heads/other'), ('worktree_id', '2' * 32)]:
            changed = dict(args, **{key: value})
            self.assertNotEqual(first['observation_id'], values.source_observation(**changed)['observation_id'])
            self.assertEqual(values.source_observation(**changed)['content_id'], content)
        legacy = dict(args, content_id=None, legacy_fingerprint_id='b' * 64,
                      binding_state='legacy_unbound', tracked_state='unknown')
        self.assertNotEqual(first['observation_id'], values.source_observation(**legacy)['observation_id'])

    def test_observation_rejects_invalid_bindings_and_tampering(self):
        args = dict(worktree_id='1' * 32, branch_key='refs/heads/main', path='docs/a.md',
                    tracked_state='tracked', binding_state='observed', content_id='a' * 64,
                    legacy_fingerprint_id=None)
        row = values.source_observation(**args)
        self.assertEqual(values.check_source_observation(row), row)
        for update in [dict(content_id=None), dict(legacy_fingerprint_id='b' * 64),
                       dict(path='../outside'), dict(tracked_state='invalid'),
                       dict(binding_state='invalid'), dict(worktree_id='bad')]:
            with self.subTest(update=update), self.assertRaises(v.BlobStoreError):
                values.source_observation(**dict(args, **update))
        with self.assertRaises(v.BlobStoreError):
            values.check_source_observation(dict(row, observation_id='f' * 64))

    def test_stage_item_keeps_only_one_path_change_and_validates_deletion(self):
        item = dict(path='cards/a.md', action='put', input_sha='a' * 64,
                    blob_sha='b' * 64, trust_digest='c' * 64, metadata_json='{"title":"A"}')
        self.assertEqual(values.check_stage_item(item), item)
        delete = dict(path='cards/a.md', action='delete', input_sha=None, blob_sha=None,
                      trust_digest=None, metadata_json=None)
        self.assertEqual(values.check_stage_item(delete), delete)
        invalid = [dict(delete, blob_sha='b' * 64), dict(item, input_sha=None),
                   dict(item, path='../outside.md'), dict(item, trust_digest='bad'),
                   dict(item, metadata_json='{"title": "A"}'), dict(item, action='other')]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(v.BlobStoreError):
                values.check_stage_item(value)

    def test_stage_item_replay_conflict_and_rollback(self):
        # Isolated SQL subset, not the final schema manifest or authority proof.
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE s_stage_jobs(job_id TEXT PRIMARY KEY, state TEXT NOT NULL)')
        db.execute('''CREATE TABLE s_stage_items(job_id TEXT NOT NULL REFERENCES s_stage_jobs(job_id),
                   path TEXT NOT NULL, action TEXT NOT NULL, input_sha TEXT, blob_sha TEXT,
                   trust_digest TEXT, metadata_json TEXT, PRIMARY KEY(job_id,path))''')
        job = '1' * 32
        db.execute('INSERT INTO s_stage_jobs VALUES (?, ?)', (job, 'collecting'))
        db.commit()
        item = dict(path='cards/a.md', action='put', input_sha='a' * 64,
                    blob_sha='b' * 64, trust_digest=None, metadata_json='{"title":"A"}')
        with self.assertRaises(v.BlobStoreError):
            view_store._stage_put_item(db, job, item)
        db.execute('BEGIN')
        self.assertTrue(view_store._stage_put_item(db, job, item))
        self.assertFalse(view_store._stage_put_item(db, job, item))
        with self.assertRaises(v.BlobStoreError):
            view_store._stage_put_item(db, job, dict(item, blob_sha='c' * 64))
        db.rollback()
        self.assertEqual(db.execute('SELECT COUNT(*) FROM s_stage_items').fetchone()[0], 0)
        db.execute('BEGIN')
        self.assertTrue(view_store._stage_put_item(db, job, item))
        db.commit()
        db.execute('UPDATE s_stage_jobs SET state=? WHERE job_id=?', ('ready', job))
        with self.assertRaises(v.BlobStoreError):
            view_store._stage_put_item(db, job, item)
        db.rollback()

    def test_stage_relations_use_foreign_keys_and_commit_cursor_with_items(self):
        # Stubs represent unchanged owner relations; this is not the full manifest.
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('CREATE TABLE views(view_id TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE s_generations(gen_id TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE wiki_blobs(blob_sha TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE card_attestations(trust_digest TEXT, blob_sha TEXT, PRIMARY KEY(trust_digest,blob_sha))')
        db.execute('CREATE TABLE s_source_observations(observation_id TEXT PRIMARY KEY)')
        for sql in view_schema.row_stage_statements():
            db.execute(sql)
        view, job, blob = 'a' * 64, '1' * 32, 'b' * 64
        db.execute('INSERT INTO views VALUES (?)', (view,))
        db.execute('INSERT INTO wiki_blobs VALUES (?)', (blob,))
        db.execute('''INSERT INTO s_stage_jobs(view_id,job_id,incarnation,base_gen_id,base_epoch,
                   capture_json,policy_digest,scan_digest,scan_complete,state)
                   VALUES (?,?,?,?,?,?,?,?,?,?)''',
                   (view, job, '2' * 32, None, 0, '{}', 'c' * 64, 'd' * 64, 1, 'collecting'))
        db.commit()
        item = dict(path='cards/a.md', action='put', input_sha='e' * 64, blob_sha=blob,
                    trust_digest=None, metadata_json='{}')
        db.execute('BEGIN')
        view_store._stage_put_item(db, job, item)
        db.execute('UPDATE s_stage_jobs SET cursor_path=? WHERE job_id=?', (item['path'], job))
        db.rollback()
        self.assertEqual(db.execute('SELECT COUNT(*) FROM s_stage_items').fetchone()[0], 0)
        self.assertIsNone(db.execute('SELECT cursor_path FROM s_stage_jobs').fetchone()[0])
        db.execute('BEGIN')
        view_store._stage_put_item(db, job, item)
        db.execute('UPDATE s_stage_jobs SET cursor_path=? WHERE job_id=?', (item['path'], job))
        db.commit()
        self.assertEqual(db.execute('SELECT cursor_path FROM s_stage_jobs').fetchone()[0], item['path'])
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute('INSERT INTO s_stage_items(job_id,path,action) VALUES (?,?,?)',
                       ('3' * 32, 'cards/b.md', 'delete'))

    def test_bounded_stage_pass_rejects_stale_cursor_and_is_atomic(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE s_stage_jobs(job_id TEXT PRIMARY KEY, state TEXT NOT NULL, cursor_path TEXT)')
        db.execute('''CREATE TABLE s_stage_items(job_id TEXT NOT NULL, path TEXT NOT NULL,
                   action TEXT NOT NULL, input_sha TEXT, blob_sha TEXT, trust_digest TEXT,
                   metadata_json TEXT, PRIMARY KEY(job_id,path))''')
        job = '1' * 32
        db.execute('INSERT INTO s_stage_jobs VALUES (?, ?, NULL)', (job, 'collecting'))
        db.commit()
        first = dict(path='cards/a.md', action='put', input_sha='a' * 64, blob_sha='b' * 64,
                     trust_digest=None, metadata_json='{}')
        second = dict(path='cards/b.md', action='delete', input_sha=None, blob_sha=None,
                      trust_digest=None, metadata_json=None)
        db.execute('BEGIN')
        view_store._stage_put_batch(db, job, None, ['cards/a.md', 'cards/b.md'], [first, second])
        db.rollback()

        self.assertEqual(db.execute('SELECT COUNT(*) FROM s_stage_items').fetchone()[0], 0)
        self.assertIsNone(db.execute('SELECT cursor_path FROM s_stage_jobs').fetchone()[0])
        db.execute('BEGIN')
        view_store._stage_put_batch(db, job, None, ['cards/a.md', 'cards/b.md'], [first, second])
        db.commit()
        db.execute('BEGIN')
        with self.assertRaises(v.BlobStoreError):
            view_store._stage_put_batch(db, job, None, ['cards/a.md'], [first])
        with self.assertRaises(v.BlobStoreError):
            view_store._stage_put_batch(db, job, 'cards/b.md', ['cards/d.md', 'cards/c.md'], [])
        with self.assertRaises(v.BlobStoreError):
            view_store._stage_put_batch(db, job, 'cards/b.md', [f'cards/{i:03}.md' for i in range(65)], [])
        with self.assertRaises(v.BlobStoreError):
            view_store._stage_put_batch(db, job, 'cards/b.md', ['cards/c.md'], [first])
        self.assertEqual(db.execute('SELECT cursor_path FROM s_stage_jobs').fetchone()[0], 'cards/b.md')
        db.rollback()

        db.execute('BEGIN')
        with patch.object(values, 'MAX_MEMBERS', 2), self.assertRaises(v.BlobStoreError):
            view_store._stage_put_batch(db, job, 'cards/b.md', ['cards/c.md'], [dict(first, path='cards/c.md')])
        self.assertEqual(db.execute('SELECT COUNT(*) FROM s_stage_items WHERE path=?', ('cards/c.md',)).fetchone()[0], 0)
        self.assertEqual(db.execute('SELECT cursor_path FROM s_stage_jobs').fetchone()[0], 'cards/b.md')
        db.rollback()
        old = dict(path='cards/d.md', action='put', input_sha='a' * 64, blob_sha='b' * 64,
                   trust_digest=None, metadata_json='{}')
        db.execute('BEGIN')
        view_store._stage_put_item(db, job, old)
        db.commit()
        added = dict(old, path='cards/c.md')
        changed = dict(old, blob_sha='c' * 64)
        db.execute('BEGIN')
        with self.assertRaises(v.BlobStoreError):
            view_store._stage_put_batch(db, job, 'cards/b.md', ['cards/c.md', 'cards/d.md'], [added, changed])
        self.assertEqual(db.execute('SELECT COUNT(*) FROM s_stage_items WHERE path=?', ('cards/c.md',)).fetchone()[0], 0)
        self.assertEqual(db.execute('SELECT cursor_path FROM s_stage_jobs').fetchone()[0], 'cards/b.md')
        db.rollback()

    def test_stage_job_reuses_only_matching_capture_and_scan(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE views(view_id TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE s_generations(gen_id TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE wiki_blobs(blob_sha TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE card_attestations(trust_digest TEXT, blob_sha TEXT, PRIMARY KEY(trust_digest,blob_sha))')
        db.execute('CREATE TABLE s_source_observations(observation_id TEXT PRIMARY KEY)')
        for sql in view_schema.row_stage_statements():
            db.execute(sql)
        view, job = 'a' * 64, '1' * 32
        db.execute('INSERT INTO views VALUES (?)', (view,))
        db.commit()
        identity = dict(view_id=view, job_id=job, incarnation='2' * 32, base_gen_id=None,
                        base_epoch=0, capture_json='{}', policy_digest='b' * 64,
                        scan_digest='c' * 64)
        db.execute('BEGIN')
        self.assertTrue(view_store._stage_start_job(db, identity))
        self.assertFalse(view_store._stage_start_job(db, identity))
        for changed in [dict(identity, scan_digest='d' * 64), dict(identity, capture_json='{"head":"other"}'),
                        dict(identity, job_id='3' * 32), dict(identity, base_epoch=1)]:
            with self.subTest(changed=changed), self.assertRaises(v.BlobStoreError):
                view_store._stage_start_job(db, changed)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM s_stage_jobs').fetchone()[0], 1)
        db.rollback()

    def test_owned_child_restart_before_and_after_stage_commit(self):
        with tempfile.TemporaryDirectory(prefix='s-stage-crash-', dir=os.environ['HOME']) as root:
            path = str(pathlib.Path(root) / 'stage.sqlite')
            db = sqlite3.connect(path)
            db.execute('CREATE TABLE s_stage_jobs(job_id TEXT PRIMARY KEY, state TEXT NOT NULL, cursor_path TEXT)')
            db.execute('''CREATE TABLE s_stage_items(job_id TEXT NOT NULL, path TEXT NOT NULL,
                       action TEXT NOT NULL, input_sha TEXT, blob_sha TEXT, trust_digest TEXT,
                       metadata_json TEXT, PRIMARY KEY(job_id,path))''')
            db.execute('INSERT INTO s_stage_jobs VALUES (?, ?, NULL)', ('1' * 32, 'collecting'))
            db.commit()
            db.close()
            for phase, expected in [('before-commit', 0), ('after-commit', 1)]:
                result = subprocess.run([sys.executable, '-B', __file__, '--crash-child', path, phase],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                print(result.stdout, result.stderr, flush=True)
                self.assertEqual(result.returncode, 17)
                self.assertIn('stage-child-marker ' + phase, result.stdout)
                db = sqlite3.connect(path)
                count = db.execute('SELECT COUNT(*) FROM s_stage_items').fetchone()[0]
                cursor = db.execute('SELECT cursor_path FROM s_stage_jobs').fetchone()[0]
                db.close()
                self.assertEqual(count, expected)
                self.assertEqual(cursor, 'cards/a.md' if expected else None)
            db = sqlite3.connect(path)
            second = dict(path='cards/b.md', action='delete', input_sha=None, blob_sha=None,
                          trust_digest=None, metadata_json=None)
            db.execute('BEGIN')
            view_store._stage_put_batch(db, '1' * 32, 'cards/a.md', ['cards/b.md'], [second])
            db.commit()
            self.assertEqual(db.execute('SELECT COUNT(*) FROM s_stage_items').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT cursor_path FROM s_stage_jobs').fetchone()[0], 'cards/b.md')
            db.close()

    def test_head_fts_manifest_matches_head_and_declared_sidecar(self):
        temporary = tempfile.TemporaryDirectory(prefix='s-head-schema-', dir=os.environ['HOME'])
        self.addCleanup(temporary.cleanup)
        path = str(pathlib.Path(temporary.name) / 'fts.sqlite')
        db = sqlite3.connect(path, cached_statements=0)
        self.addCleanup(db.close)
        db.execute('CREATE TABLE s_generations(view_id TEXT,ordinal INTEGER,sidecar_present INTEGER)')
        db.execute('CREATE TABLE s_heads(view_id TEXT PRIMARY KEY,ordinal INTEGER)')
        guard = view_fts.ConnectionGuard(db)
        view = 'a' * 64
        db.execute('BEGIN')
        with guard.view_head(view):
            db.execute(view_fts.head_ddl(view))
            db.execute(view_fts.head_ddl(view, sidecar=True))
            db.execute('INSERT INTO s_generations VALUES (?,?,?)', (view, 1, 1))
            db.execute('INSERT INTO s_heads VALUES (?,?)', (view, 1))
            db.commit()
        self.assertTrue(view_fts.head_schema_rows(db))
        db.execute('UPDATE s_generations SET sidecar_present=0')
        with self.assertRaises(v.BlobStoreError):
            view_fts.head_schema_rows(db)
        db.rollback()
        self.assertTrue(view_fts.head_schema_rows(db))
        external = sqlite3.connect(path, cached_statements=0)
        external.execute('CREATE TABLE wiki_fts_h_ghost(x)')
        external.commit()
        external.close()
        with self.assertRaises(v.BlobStoreError):
            view_fts.head_schema_rows(db)

    def test_head_fts_owned_child_crash_commit_boundary(self):
        with tempfile.TemporaryDirectory(prefix='s-fts-crash-', dir=os.environ['HOME']) as root:
            path = str(pathlib.Path(root) / 'fts.sqlite')
            view = 'a' * 64
            db = sqlite3.connect(path, cached_statements=0)
            db.execute('CREATE TABLE seals(physical TEXT)')
            guard = view_fts.ConnectionGuard(db)
            name = view_fts.head_table(view)
            db.execute('BEGIN')
            with guard.view_head(view):
                db.execute(view_fts.head_ddl(view))
                db.execute(f'INSERT INTO {name}(filepath,title,body,path_key) VALUES (?,?,?,?)',
                           ('a.md', 'A', 'before', 'a.md'))
                db.commit()
            original = view_fts.head_storage_digest(db, view)
            db.close()
            for phase in ('before-commit', 'after-commit'):
                result = subprocess.run([sys.executable, '-B', __file__, '--fts-crash-child', path, phase],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                print(result.stdout, result.stderr, flush=True)
                self.assertEqual(result.returncode, 17)
                self.assertIn('fts-child-marker ' + phase, result.stdout)
                db = sqlite3.connect(path, cached_statements=0)
                body = db.execute(f'SELECT body FROM {name}').fetchone()[0]
                seal = db.execute('SELECT physical FROM seals').fetchall()
                physical = view_fts.head_storage_digest(db, view)
                if phase == 'before-commit':
                    self.assertEqual((body, seal, physical), ('before', [], original))
                else:
                    self.assertEqual(body, 'after')
                    self.assertEqual(seal, [(physical,)])
                db.close()

    def test_head_fts_applies_only_checked_deltas_and_rolls_back_conflict(self):
        db = sqlite3.connect(':memory:', cached_statements=0)
        self.addCleanup(db.close)
        guard = view_fts.ConnectionGuard(db)
        view = 'a' * 64
        name = view_fts.head_table(view)
        old = dict(filepath='a.md', title='A', body='alpha', path_key='a.md')
        keep = dict(filepath='b.md', title='B', body='beta', path_key='b.md')
        new = dict(old, body='changed')
        db.execute('BEGIN')
        with guard.view_head(view):
            db.execute(view_fts.head_ddl(view))
            for row in (old, keep):
                db.execute(f'INSERT INTO {name}(filepath,title,body,path_key) VALUES (?,?,?,?)', tuple(row.values()))
            db.commit()
        db.execute('BEGIN')
        with guard.view_head(view):
            view_fts.apply_head_changes(db, view, [dict(old=old, new=new)])
            db.commit()
        rows = [tuple(row) for row in db.execute(f'SELECT filepath,title,body,path_key FROM {name} ORDER BY path_key')]
        self.assertEqual(rows, [tuple(new.values()), tuple(keep.values())])
        db.execute('BEGIN')
        with guard.view_head(view):
            with self.assertRaises(v.BlobStoreError):
                view_fts.apply_head_changes(db, view, [dict(old=new, new=old), dict(old=dict(keep, body='wrong'), new=None)])
            db.commit()
        rows = [tuple(row) for row in db.execute(f'SELECT filepath,title,body,path_key FROM {name} ORDER BY path_key')]
        self.assertEqual(rows, [tuple(new.values()), tuple(keep.values())])

    def test_head_delta_looks_up_many_paths_with_one_corpus_scan(self):
        db = sqlite3.connect(':memory:', cached_statements=0)
        self.addCleanup(db.close)
        guard = view_fts.ConnectionGuard(db)
        view = 'a' * 64
        name = view_fts.head_table(view)
        rows = [dict(filepath=f'{i}.md', title='A', body='old', path_key=f'{i}.md') for i in range(5)]
        db.execute('BEGIN')
        with guard.view_head(view):
            db.execute(view_fts.head_ddl(view))
            for row in rows:
                db.execute(f'INSERT INTO {name}(filepath,title,body,path_key) VALUES (?,?,?,?)', tuple(row.values()))
            db.commit()
        trace = []
        db.set_trace_callback(trace.append)
        db.execute('BEGIN')
        with guard.view_head(view):
            view_fts.apply_head_changes(db, view, [dict(old=row, new=dict(row, body='new')) for row in rows[:2]])
            db.commit()
        scans = [sql for sql in trace if sql.lower().startswith('select rowid,path_key from wiki_fts_h_')]
        self.assertEqual(len(scans), 1, trace)
        self.assertFalse(any('where path_key=' in sql.lower() for sql in trace), trace)
        self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {name} WHERE body=?', ('new',)).fetchone()[0], 2)

    def test_head_fts_sidecar_delta_does_not_touch_base(self):
        db = sqlite3.connect(':memory:', cached_statements=0)
        self.addCleanup(db.close)
        guard = view_fts.ConnectionGuard(db)
        view = 'a' * 64
        base, side = view_fts.head_table(view), view_fts.head_table(view, sidecar=True)
        old = dict(filepath='a.md', title='A', body='side-old', path_key='a.md')
        new = dict(old, body='side-new')
        db.execute('BEGIN')
        with guard.view_head(view):
            db.execute(view_fts.head_ddl(view))
            db.execute(view_fts.head_ddl(view, sidecar=True))
            db.execute(f'INSERT INTO {base}(filepath,title,body,path_key) VALUES (?,?,?,?)',
                       ('a.md', 'A', 'base', 'a.md'))
            db.execute(f'INSERT INTO {side}(filepath,title,body,path_key) VALUES (?,?,?,?)', tuple(old.values()))
            db.execute(f'INSERT INTO {side}(filepath,title,body,path_key) VALUES (?,?,?,?)',
                       ('b.md', 'B', 'side-second', 'b.md'))
            db.commit()
        db.execute('BEGIN')
        with guard.view_head(view):
            view_fts.apply_head_changes(db, view, [dict(old=old, new=new)], sidecar=True)
            db.execute('SAVEPOINT fts_flush')
            db.execute('RELEASE fts_flush')
            before_commit = view_fts.head_storage_digest(db, view, sidecar=True)
            db.commit()
        self.assertEqual(view_fts.head_storage_digest(db, view, sidecar=True), before_commit)
        self.assertEqual(db.execute(f'SELECT body FROM {base}').fetchone()[0], 'base')
        self.assertEqual(db.execute(f'SELECT body FROM {side} ORDER BY rowid').fetchall(),
                         [('side-new',), ('side-second',)])
        occupied = db.execute(f'SELECT rowid FROM {side} WHERE path_key=?', ('b.md',)).fetchone()[0]
        extra = dict(filepath='c.md', title='C', body='third', path_key='c.md')
        db.execute('BEGIN')
        with guard.view_head(view):
            with self.assertRaises((sqlite3.IntegrityError, v.BlobStoreError)):
                view_fts.apply_head_changes(db, view,
                    [dict(old=None, new=extra, restore_rowid=occupied)], sidecar=True)
            db.commit()
        self.assertEqual(db.execute(f'SELECT body FROM {side} ORDER BY rowid').fetchall(),
                         [('side-new',), ('side-second',)])
        db.execute('BEGIN')
        with guard.view_head(view):
            deleted = view_fts.apply_head_changes(db, view, [dict(old=new, new=None)], sidecar=True)
            db.commit()
        db.execute('BEGIN')
        with guard.view_head(view):
            view_fts.apply_head_changes(db, view,
                [dict(old=None, new=new, restore_rowid=deleted[0]['old_rowid'])], sidecar=True)
            db.commit()
        self.assertEqual(db.execute(f'SELECT body FROM {side} ORDER BY rowid').fetchall(),
                         [('side-new',), ('side-second',)])

    def test_view_head_fts_capability_is_scoped_and_commits_with_metadata(self):
        db = sqlite3.connect(':memory:', cached_statements=0)
        self.addCleanup(db.close)
        db.execute('CREATE TABLE markers(value TEXT)')
        guard = view_fts.ConnectionGuard(db)
        view, other = 'a' * 64, 'b' * 64
        name = view_fts.head_table(view)
        other_name = view_fts.head_table(other)
        self.assertNotEqual(name, other_name)
        with self.assertRaises(v.BlobStoreError):
            view_fts.head_table('not-an-id')
        db.execute('BEGIN')
        with guard.view_head(view):
            db.execute(view_fts.head_ddl(view))
            db.execute(f'INSERT INTO {name}(filepath,title,body,path_key) VALUES (?,?,?,?)',
                       ('a.md', 'A', 'needle', 'a.md'))
            db.execute('INSERT INTO markers VALUES (?)', ('head-one',))
            db.execute('SAVEPOINT fts_flush')
            db.execute('RELEASE fts_flush')
            view_fts.verify_head_index(db, view)
            before_commit = view_fts.head_storage_digest(db, view)
            rows = [dict(filepath='a.md', title='A', body='needle', path_key='a.md')]
            proof, physical = view_fts.head_proof(db, view, rows)
            self.assertEqual(physical, before_commit)
            self.assertEqual(len(proof), 64)
            streamed, _ = view_fts.head_proof(db, view, (row for row in rows))
            self.assertEqual(streamed, proof)
            with self.assertRaises(v.BlobStoreError):
                view_fts.head_proof(db, view, [dict(rows[0], body='wrong')])
            with self.assertRaises(v.BlobStoreError):
                view_fts.head_proof(db, view, None)
            db.commit()
        self.assertFalse(db.in_transaction)
        self.assertEqual(view_fts.head_storage_digest(db, view), before_commit)
        db.execute('BEGIN')
        with guard.view_head(view):
            db.execute(f'DELETE FROM {name} WHERE path_key=?', ('a.md',))
            db.execute(f'INSERT INTO {name}(filepath,title,body,path_key) VALUES (?,?,?,?)',
                       ('a.md', 'A', 'needle alpha', 'a.md'))
            db.execute('SAVEPOINT fts_flush')
            db.execute('RELEASE fts_flush')
            changed_before_commit = view_fts.head_storage_digest(db, view)
            db.commit()
        self.assertEqual(view_fts.head_storage_digest(db, view), changed_before_commit)
        self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {name} WHERE {name} MATCH ?', ('needle',)).fetchone()[0], 1)
        self.assertEqual(db.execute('SELECT value FROM markers').fetchone()[0], 'head-one')
        with self.assertRaises(sqlite3.DatabaseError):
            db.execute(f'DELETE FROM {name}')
        db.execute('BEGIN')
        with self.assertRaises(RuntimeError):
            with guard.view_head(view):
                db.execute(f'DELETE FROM {name}')
                db.execute("INSERT INTO markers VALUES ('rolled-back')")
                raise RuntimeError('synthetic failure')
        self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0], 1)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM markers').fetchone()[0], 1)
        db.execute('BEGIN')
        with guard.view_head(view):
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute(view_fts.head_ddl(other))
            db.commit()
        db.execute('BEGIN')
        with self.assertRaises(v.BlobStoreError):
            with guard.view_head(view):
                db.execute("INSERT INTO markers VALUES ('uncommitted')")
        self.assertFalse(db.in_transaction)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM markers').fetchone()[0], 1)

    def test_card_delta_closes_old_interval_and_saves_rollback_references_atomically(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('CREATE TABLE views(view_id TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE wiki_blobs(blob_sha TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE card_attestations(trust_digest TEXT, blob_sha TEXT, PRIMARY KEY(trust_digest,blob_sha))')
        db.execute('CREATE TABLE s_generations(view_id TEXT, ordinal INTEGER, PRIMARY KEY(view_id,ordinal))')
        db.execute('CREATE TABLE s_source_observations(observation_id TEXT PRIMARY KEY)')
        for sql in [*view_schema.row_card_statements(), *view_schema.row_delta_statements()]:
            db.execute(sql)
        view, old_blob, new_blob = 'a' * 64, 'b' * 64, 'c' * 64
        db.execute('INSERT INTO views VALUES (?)', (view,))
        db.executemany('INSERT INTO wiki_blobs VALUES (?)', [(old_blob,), (new_blob,)])
        db.execute('INSERT INTO s_generations VALUES (?,?)', (view, 2))
        old = dict(path='a.md', blob_sha=old_blob, title='A', eligible=0, trust_digest=None, metadata_json='{}')
        new = dict(old, blob_sha=new_blob, title='B')
        db.execute('''INSERT INTO s_cards(view_id,path,gen_from,blob_sha,title,eligible,trust_digest,metadata_json)
                   VALUES (?,?,?,?,?,?,?,?)''', (view, 'a.md', 1, old_blob, 'A', 0, None, '{}'))
        db.commit()
        db.execute('BEGIN')
        with self.assertRaises(v.BlobStoreError):
            view_store._apply_card_delta(db, view, 2, dict(old, title='wrong'), new)
        self.assertEqual(db.execute('SELECT gen_to FROM s_cards').fetchone()[0], None)
        view_store._apply_card_delta(db, view, 2, old, new)
        self.assertEqual(db.execute('SELECT gen_from,gen_to,blob_sha FROM s_cards ORDER BY gen_from').fetchall(),
                         [(1, 2, old_blob), (2, None, new_blob)])
        self.assertEqual(set(db.execute('SELECT side,blob_sha FROM s_delta_blobs')),
                         {('old', old_blob), ('new', new_blob)})
        db.rollback()
        self.assertEqual(db.execute('SELECT gen_from,gen_to,blob_sha FROM s_cards').fetchall(), [(1, None, old_blob)])
        self.assertEqual(db.execute('SELECT COUNT(*) FROM s_deltas').fetchone()[0], 0)

    def test_rollback_delta_children_keep_references_until_removed(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('CREATE TABLE s_generations(view_id TEXT, ordinal INTEGER, PRIMARY KEY(view_id,ordinal))')
        db.execute('CREATE TABLE wiki_blobs(blob_sha TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE s_source_observations(observation_id TEXT PRIMARY KEY)')
        for sql in view_schema.row_delta_statements():
            db.execute(sql)
        view, blob, obs = 'a' * 64, 'b' * 64, 'c' * 64
        db.execute('INSERT INTO s_generations VALUES (?,?)', (view, 1))
        db.execute('INSERT INTO wiki_blobs VALUES (?)', (blob,))
        db.execute('INSERT INTO s_source_observations VALUES (?)', (obs,))
        db.execute('INSERT INTO s_deltas VALUES (?,?,?,?,?,?)', (view, 1, 'card', '["a.md"]', '{}', '{}'))
        db.execute('INSERT INTO s_delta_blobs VALUES (?,?,?,?,?,?)', (view, 1, 'card', '["a.md"]', 'old', blob))
        db.execute('INSERT INTO s_delta_observations VALUES (?,?,?,?,?,?)', (view, 1, 'card', '["a.md"]', 'old', obs))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute('DELETE FROM wiki_blobs WHERE blob_sha=?', (blob,))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute('DELETE FROM s_deltas WHERE view_id=?', (view,))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE s_deltas SET old_json='null' WHERE view_id=?", (view,))
        db.execute('DELETE FROM s_delta_blobs WHERE view_id=?', (view,))
        db.execute('DELETE FROM s_delta_observations WHERE view_id=?', (view,))
        db.execute('DELETE FROM s_deltas WHERE view_id=?', (view,))
        self.assertEqual(db.execute('SELECT COUNT(*) FROM wiki_blobs').fetchone()[0], 1)

    def test_source_observation_identity_and_interval_constraints(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('CREATE TABLE views(view_id TEXT PRIMARY KEY, worktree_id TEXT, branch_key TEXT)')
        db.execute('CREATE TABLE worktrees(worktree_id TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE source_fingerprints(fingerprint_id TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE card_attestations(trust_digest TEXT PRIMARY KEY)')
        for sql in view_schema.row_source_statements():
            db.execute(sql)
        view, worktree, sha = 'a' * 64, '1' * 32, 'b' * 64
        db.execute('INSERT INTO views VALUES (?,?,?)', (view, worktree, 'refs/heads/main'))
        db.execute('INSERT INTO worktrees VALUES (?)', (worktree,))
        content = values.source_content(sha, 7)
        db.execute('INSERT INTO s_source_content VALUES (?,?,?)',
                   (content['content_id'], content['source_sha'], content['byte_length']))
        row = values.source_observation(worktree_id=worktree, branch_key='refs/heads/main',
            path='src/a', tracked_state='tracked', binding_state='observed',
            content_id=content['content_id'], legacy_fingerprint_id=None)
        fields = ('observation_id', *values.SOURCE_OBSERVATION_FIELDS)
        insert = 'INSERT INTO s_source_observations VALUES (?,?,?,?,?,?,?,?)'
        db.execute(insert, tuple(row[key] for key in fields))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(insert, tuple(('f' * 64 if key == 'observation_id' else row[key]) for key in fields))
        db.execute('INSERT INTO s_sources VALUES (?,?,?,?,?)', (view, 'src/a', 1, None, row['observation_id']))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute('INSERT INTO s_sources VALUES (?,?,?,?,?)', (view, 'src/wrong', 1, None, row['observation_id']))
        other_view = 'd' * 64
        db.execute('INSERT INTO views VALUES (?,?,?)', (other_view, worktree, 'refs/heads/other'))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute('INSERT INTO s_sources VALUES (?,?,?,?,?)', (other_view, 'src/a', 1, None, row['observation_id']))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute('INSERT INTO s_card_sources VALUES (?,?,?,?,?,?,?)',
                       (view, 'card.md', 'src/wrong', 1, None, row['observation_id'], '[]'))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute('INSERT INTO s_sources VALUES (?,?,?,?,?)', (view, 'src/a', 2, None, row['observation_id']))
        db.execute('UPDATE s_sources SET gen_to=2 WHERE view_id=?', (view,))
        db.execute('INSERT INTO s_sources VALUES (?,?,?,?,?)', (view, 'src/a', 2, None, row['observation_id']))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute('UPDATE s_sources SET gen_to=3 WHERE view_id=? AND gen_from=1', (view,))

    def test_generation_head_is_same_view_next_epoch_and_ordinal_never_reused(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('CREATE TABLE views(view_id TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE chunk_recipes(recipe_id TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE vector_profiles(profile_id TEXT PRIMARY KEY)')
        for sql in view_schema.row_generation_statements():
            db.execute(sql)
        view = 'a' * 64
        db.execute('INSERT INTO views VALUES (?)', (view,))
        insert = '''INSERT INTO s_generations(gen_id,view_id,epoch,prior_ordinal,kind,capture_json,
                    policy_digest,skip_digest,coverage,member_digest,source_digest,logical_fts_digest,
                    physical_fts_digest,seal_digest) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)'''
        def generation(gen_id, epoch, prior):
            db.execute(insert, (gen_id, view, epoch, prior, 'normal', '{}', *(['b' * 64] * 2),
                                'lexical', *(['c' * 64] * 5)))
            return db.execute('SELECT ordinal FROM s_generations WHERE gen_id=?', (gen_id,)).fetchone()[0]
        first = generation('g1', 1, None)
        db.execute('INSERT INTO s_heads VALUES (?,?,?)', (view, first, 1))
        other_view = 'd' * 64
        db.execute('INSERT INTO views VALUES (?)', (other_view,))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute('INSERT INTO s_heads VALUES (?,?,?)', (other_view, first, 1))
        second = generation('g2', 2, first)
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute('UPDATE s_heads SET ordinal=?,epoch=? WHERE view_id=?', (second, 3, view))
        db.execute('UPDATE s_heads SET ordinal=?,epoch=? WHERE view_id=?', (second, 2, view))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE s_generations SET seal_digest='changed' WHERE gen_id='g1'")
        unused = generation('g3', 3, second)
        db.execute('DELETE FROM s_generations WHERE gen_id=?', ('g3',))
        self.assertGreater(generation('g4', 3, second), unused)

    def test_card_intervals_cannot_overlap_or_mutate_published_contents(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('CREATE TABLE views(view_id TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE wiki_blobs(blob_sha TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE card_attestations(trust_digest TEXT, blob_sha TEXT, PRIMARY KEY(trust_digest,blob_sha))')
        for sql in view_schema.row_card_statements():
            db.execute(sql)
        view, blob = 'a' * 64, 'b' * 64
        db.execute('INSERT INTO views VALUES (?)', (view,))
        db.execute('INSERT INTO wiki_blobs VALUES (?)', (blob,))
        insert = '''INSERT INTO s_cards(view_id,path,gen_from,gen_to,blob_sha,title,eligible,metadata_json)
                    VALUES (?,?,?,?,?,?,?,?)'''
        db.execute(insert, (view, 'a.md', 1, None, blob, 'A', 0, '{}'))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(insert, (view, 'a.md', 2, None, blob, 'A', 0, '{}'))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE s_cards SET title='changed' WHERE view_id=?", (view,))
        db.execute('UPDATE s_cards SET gen_to=3 WHERE view_id=?', (view,))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(insert, (view, 'a.md', 2, 4, blob, 'A', 0, '{}'))
        db.execute(insert, (view, 'a.md', 3, None, blob, 'B', 0, '{}'))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute('UPDATE s_cards SET gen_to=4 WHERE view_id=? AND gen_from=1', (view,))
        self.assertEqual(db.execute('SELECT COUNT(*) FROM s_cards').fetchone()[0], 2)

    def test_stage_abandon_bounded_resume_keeps_other_jobs(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('CREATE TABLE views(view_id TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE s_generations(gen_id TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE wiki_blobs(blob_sha TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE card_attestations(trust_digest TEXT, blob_sha TEXT, PRIMARY KEY(trust_digest,blob_sha))')
        db.execute('CREATE TABLE s_source_observations(observation_id TEXT PRIMARY KEY)')
        for sql in view_schema.row_stage_statements():
            db.execute(sql)
        view, job = 'a' * 64, '1' * 32
        db.execute('INSERT INTO views VALUES (?)', (view,))
        db.execute('''INSERT INTO s_stage_jobs(view_id,job_id,incarnation,base_gen_id,base_epoch,
                   capture_json,policy_digest,scan_digest,scan_complete,state)
                   VALUES (?,?,?,?,?,?,?,?,?,?)''',
                   (view, job, '2' * 32, None, 0, '{}', 'c' * 64, 'd' * 64, 1, 'collecting'))
        db.execute('INSERT INTO views VALUES (?)', ('b' * 64,))
        db.execute('''INSERT INTO s_stage_jobs(view_id,job_id,incarnation,base_gen_id,base_epoch,
                   capture_json,policy_digest,scan_digest,scan_complete,state)
                   VALUES (?,?,?,?,?,?,?,?,?,?)''',
                   ('b' * 64, '4' * 32, '2' * 32, None, 0, '{}', 'c' * 64, 'd' * 64, 1, 'collecting'))
        db.executemany('INSERT INTO s_stage_items(job_id,path,action) VALUES (?,?,?)',
                       [(job, 'cards/a.md', 'delete'), (job, 'cards/b.md', 'delete')])
        db.execute('INSERT INTO s_stage_card_source_changes(job_id,card_path,source_path,action) VALUES (?,?,?,?)',
                   (job, 'cards/a.md', 'src/a', 'delete'))
        db.execute('INSERT INTO s_stage_source_changes(job_id,path,action) VALUES (?,?,?)',
                   (job, 'src/a', 'delete'))
        db.commit()
        db.execute('BEGIN')
        view_store._stage_mark_abandon(db, job)
        self.assertEqual(view_store._stage_discard_step(db, job, limit=1), 1)
        db.commit()
        self.assertEqual(db.execute('SELECT state FROM s_stage_jobs WHERE job_id=?', (job,)).fetchone()[0], 'abandon')
        self.assertEqual(db.execute('SELECT COUNT(*) FROM s_stage_source_changes').fetchone()[0], 1)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM s_stage_items').fetchone()[0], 2)
        for expected in (1, 1, 1, 0):
            db.execute('BEGIN')
            self.assertEqual(view_store._stage_discard_step(db, job, limit=1), expected)
            db.commit()
        self.assertEqual(db.execute('SELECT job_id FROM s_stage_jobs').fetchone()[0], '4' * 32)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM s_stage_items').fetchone()[0], 0)

    def test_stage_page_binds_identity_and_inserts_job_atomically(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE views(view_id TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE s_generations(gen_id TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE wiki_blobs(blob_sha TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE card_attestations(trust_digest TEXT, blob_sha TEXT, PRIMARY KEY(trust_digest,blob_sha))')
        db.execute('CREATE TABLE s_source_observations(observation_id TEXT PRIMARY KEY)')
        for sql in view_schema.row_stage_statements():
            db.execute(sql)
        identity = dict(view_id='a' * 64, job_id='1' * 32, incarnation='2' * 32,
                        base_gen_id=None, base_epoch=0, capture_json='{}',
                        policy_digest='b' * 64, scan_digest='c' * 64)
        db.execute('INSERT INTO views VALUES (?)', (identity['view_id'],))
        db.commit()
        db.execute('BEGIN')
        with self.assertRaises(v.BlobStoreError):
            view_store._stage_apply_page(db, identity, None, [], [])
        self.assertEqual(db.execute('SELECT COUNT(*) FROM s_stage_jobs').fetchone()[0], 0)
        item = dict(path='cards/a.md', action='delete', input_sha=None, blob_sha=None,
                    trust_digest=None, metadata_json=None)
        view_store._stage_apply_page(db, identity, None, ['cards/a.md'], [item])
        db.commit()
        db.execute('BEGIN')
        with self.assertRaises(v.BlobStoreError):
            view_store._stage_apply_page(db, dict(identity, scan_digest='d' * 64), 'cards/a.md',
                                        ['cards/b.md'], [])
        self.assertEqual(db.execute('SELECT cursor_path FROM s_stage_jobs').fetchone()[0], 'cards/a.md')
        self.assertEqual(db.execute('SELECT COUNT(*) FROM s_stage_items').fetchone()[0], 1)
        db.rollback()


def crash_child(path, phase):
    db = sqlite3.connect(path)
    item = dict(path='cards/a.md', action='put', input_sha='a' * 64, blob_sha='b' * 64,
                trust_digest=None, metadata_json='{}')
    db.execute('BEGIN')
    view_store._stage_put_batch(db, '1' * 32, None, ['cards/a.md'], [item])
    if phase == 'after-commit':
        db.commit()
    print('stage-child-marker', phase, 'pid', os.getpid(), 'ppid', os.getppid(),
          'python', sys.executable, sys.version, flush=True)
    os._exit(17)


def fts_crash_child(path, phase):
    db = sqlite3.connect(path, cached_statements=0)
    view = 'a' * 64
    guard = view_fts.ConnectionGuard(db)
    old = dict(filepath='a.md', title='A', body='before', path_key='a.md')
    db.execute('BEGIN')
    with guard.view_head(view):
        view_fts.apply_head_changes(db, view, [dict(old=old, new=dict(old, body='after'))])
        db.execute('SAVEPOINT fts_flush')
        db.execute('RELEASE fts_flush')
        view_fts.verify_head_index(db, view)
        physical = view_fts.head_storage_digest(db, view)
        db.execute('INSERT INTO seals VALUES (?)', (physical,))
        if phase == 'after-commit':
            db.commit()
        print('fts-child-marker', phase, 'pid', os.getpid(), 'ppid', os.getppid(),
              'python', sys.executable, sys.version, 'sqlite', sqlite3.sqlite_version, flush=True)
        os._exit(17)


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--crash-child':
        crash_child(sys.argv[2], sys.argv[3])
    if len(sys.argv) == 4 and sys.argv[1] == '--fts-crash-child':
        fts_crash_child(sys.argv[2], sys.argv[3])
    print('synthetic child python:', sys.executable, sys.version, flush=True)
    unittest.main()

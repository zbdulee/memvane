#!/usr/bin/env python3
"""S view publication contract on an isolated, synthetic row-format ViewStore."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
CORE = HERE.parent.parent / 'core'
spec = importlib.util.spec_from_file_location('s_generation_fixture', HERE / 'laya-view-generations-checks.py')
seed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed)

# A child publishes once and stops at a named point; parent verifies the store.
CRASH_CHILD = r'''
import json, os, sys
args = json.loads(sys.argv[1])
sys.path.insert(0, args['core'])
import laya_view_store as views
store = views.ViewStore.open(args['db'], repository=args['repository'], scope_id=args['scope'])
handle = store.capture_view(args['worktree_id'])
manifest = args['manifest']
head = store.head(handle)
gen = store.prepare_generation(handle, manifest, expected_head=head)
point = args['point']
def stop(*_):
    os._exit(17)
if point == 'before_commit':
    # The publication's guarded commit is the first commit on this connection
    # once the head CAS has run.
    import laya_view_fts as fts
    original = fts.ConnectionGuard.view_head
    from contextlib import contextmanager
    @contextmanager
    def crashing(self, view_id):
        with original(self, view_id):
            self._db.set_trace_callback(lambda sql: stop() if sql.strip().upper() == 'COMMIT' else None)
            yield
    fts.ConnectionGuard.view_head = crashing
    store.publish_generation(handle, gen, expected_head=head)
else:
    store.publish_generation(handle, gen, expected_head=head)
    stop()
'''


GC_CHILD = r'''
import json, os, sys
args = json.loads(sys.argv[1])
sys.path.insert(0, args['core'])
import laya_view_store as views
store = views.ViewStore.open(args['db'], repository=args['repository'], scope_id=args['scope'])
handle = store.capture_view(args['worktree_id'])
def stop(*_):
    os._exit(17)
if args['point'] == 'before_commit':
    store._db.set_trace_callback(lambda sql: stop() if sql.strip().upper() == 'COMMIT' else None)
    store.collect_view_rows(handle, limit=64)
else:
    store.collect_view_rows(handle, limit=64)
    stop()
'''


class IncrementalPublicationChecks(seed.GenerationCase):
    def rows_store(self, root=None):
        repository, worktree = self.bind(root)
        store = self.Store.create(str(self.db), project_root=str(root or self.root),
                                  repository=repository, worktree=worktree, scope_id=self.scope,
                                  row_generations=True)
        self.track(store)
        return store, repository, store.capture_view(worktree['worktreeId'])

    def publish(self, store, handle, body):
        manifest = self.snapshot(store, handle, body=body)
        head = store.head(handle)
        gen = store.prepare_generation(handle, manifest, expected_head=head)
        return store.publish_generation(handle, gen, expected_head=head)

    @staticmethod
    def fts_tables(store):
        return {row[0] for row in store._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND upper(sql) LIKE '%USING FTS5%'").fetchall()}

    def found(self, store, handle, query):
        pin = store.pin_view(handle)
        try:
            return [row['path'] for row in store.search(pin, query, 10)]
        finally:
            store.release(pin)

    def test_one_card_change_reuses_view_fts_and_does_not_persist_full_stage(self):
        store, _, handle = self.rows_store()
        first_head = self.publish(store, handle, b'# First\nuniquealpha synthetic card\n')
        first_fts = self.fts_tables(store)
        self.assertEqual(len(first_fts), 1, first_fts)

        next_head = self.publish(store, handle, b'# Second\nuniquebeta synthetic card\n')
        self.assertEqual(next_head['epoch'], first_head['epoch'] + 1)
        self.assertEqual(self.found(store, handle, 'uniquebeta'), ['cards/a.md'])
        self.assertEqual(self.found(store, handle, 'uniquealpha'), [])
        self.assertEqual(self.fts_tables(store), first_fts,
                         'each view keeps one head FTS instead of one FTS per generation')
        tables = {row[0] for row in store._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertNotIn('generation_slots', tables, 'full manifest/files must not accumulate per generation')

    def test_change_records_one_interval_and_one_delta(self):
        store, _, handle = self.rows_store()
        first = self.publish(store, handle, b'# First\nuniquealpha synthetic card\n')
        second = self.publish(store, handle, b'# Second\nuniquebeta synthetic card\n')
        db = store._db
        gens = db.execute('SELECT ordinal,gen_id,epoch,prior_ordinal FROM s_generations '
                          'WHERE view_id=? ORDER BY ordinal', (handle.view_id,)).fetchall()
        self.assertEqual([(row[1], row[2]) for row in gens],
                         [(first['gen_id'], 1), (second['gen_id'], 2)])
        self.assertEqual(gens[1][3], gens[0][0])
        self.assertEqual(tuple(db.execute('SELECT ordinal,epoch FROM s_heads WHERE view_id=?',
                                          (handle.view_id,)).fetchone()), (gens[1][0], 2))
        cards = db.execute('SELECT gen_from,gen_to FROM s_cards WHERE view_id=? AND path=? ORDER BY gen_from',
                           (handle.view_id, 'cards/a.md')).fetchall()
        self.assertEqual([tuple(row) for row in cards], [(gens[0][0], gens[1][0]), (gens[1][0], None)])
        deltas = db.execute('SELECT ordinal,rel,old_json IS NOT NULL,new_json IS NOT NULL FROM s_deltas '
                            'WHERE view_id=? ORDER BY ordinal', (handle.view_id,)).fetchall()
        self.assertEqual([tuple(row) for row in deltas], [(gens[1][0], 'card', 1, 1)],
                         'the first generation has no prior and records no rollback delta')
        self.assertEqual(db.execute("SELECT COUNT(*) FROM s_stage_jobs WHERE state<>'abandon'").fetchone()[0], 0)

    def test_unchanged_republish_writes_no_card_rows(self):
        store, _, handle = self.rows_store()
        body = b'# Same\nuniquegamma synthetic card\n'
        self.publish(store, handle, body)
        before = store._db.execute('SELECT COUNT(*) FROM s_cards').fetchone()[0]
        self.publish(store, handle, body)
        self.assertEqual(store._db.execute('SELECT COUNT(*) FROM s_cards').fetchone()[0], before)

    def test_stale_pin_refuses_after_new_head(self):
        store, _, handle = self.rows_store()
        self.publish(store, handle, b'# First\nuniquealpha synthetic card\n')
        pin = store.pin_view(handle)
        try:
            self.publish(store, handle, b'# Second\nuniquebeta synthetic card\n')
            self.assertEqual(self.refused(store.search, pin, 'uniquealpha', 10).reason, 'view_head_changed')
        finally:
            store.release(pin)

    def test_sealed_physical_digest_survives_commit_and_reopen(self):
        import laya_view_fts as fts
        store, repository, handle = self.rows_store()
        self.publish(store, handle, b'# First\nuniquealpha synthetic card\n')
        self.publish(store, handle, b'# Second\nuniquebeta synthetic card\n')
        store.close()
        again = self.reopen(repository)
        sealed = again._db.execute('SELECT g.physical_fts_digest FROM s_heads h JOIN s_generations g '
                                   'ON g.view_id=h.view_id AND g.ordinal=h.ordinal WHERE h.view_id=?',
                                   (handle.view_id,)).fetchone()[0]
        self.assertEqual(fts.head_storage_digest(again._db, handle.view_id), sealed)
        handle2 = again.capture_view(handle.worktree_id)
        self.assertEqual(self.found(again, handle2, 'uniquebeta'), ['cards/a.md'])

    def test_failure_before_commit_keeps_previous_head_and_fts(self):
        store, _, handle = self.rows_store()
        first = self.publish(store, handle, b'# First\nuniquealpha synthetic card\n')
        manifest = self.snapshot(store, handle, body=b'# Second\nuniquebeta synthetic card\n')
        gen = store.prepare_generation(handle, manifest, expected_head=first)
        calls = []

        def refuse(*args, **kwargs):
            calls.append(kwargs)
            self.views.v.fail('checkout_changed')
        with mock.patch.object(self.views, '_current_capture', side_effect=refuse):
            self.refused(store.publish_generation, handle, gen, expected_head=first)
        self.assertTrue(calls)
        self.assertEqual(store.head(handle), first)
        self.assertEqual(store._db.execute('SELECT COUNT(*) FROM s_generations').fetchone()[0], 1)
        self.assertEqual(self.found(store, handle, 'uniquealpha'), ['cards/a.md'])
        self.assertEqual(self.found(store, handle, 'uniquebeta'), [])

    def test_other_view_fts_is_not_changed_by_a_neighbor_publication(self):
        import laya_view_fts as fts
        main, linked = self.project('rows'), self.home / 'projects' / 'rows-linked'
        for args in [('init', '-q'), ('add', '-A'), ('commit', '-q', '-m', 'fixture'),
                     ('worktree', 'add', '-q', '-b', 'side', str(linked))]:
            self.git(main, *args)
        store, repository, a = self.rows_store(main)
        record = seed.helpers.view.register_worktree(str(linked), repository)
        b = store.capture_view(store.register_worktree(str(linked), record))
        ma = self.snapshot(store, a, root=main, body=b'# Main\nuniquemain synthetic card\n')
        ha = store.prepare_generation(a, ma, expected_head=store.head(a))
        ha = store.publish_generation(a, ha, expected_head=store.head(a))
        before = fts.head_storage_digest(store._db, a.view_id)
        mb = self.snapshot(store, b, root=linked, body=b'# Side\nuniqueside synthetic card\n')
        gb = store.prepare_generation(b, mb, expected_head=store.head(b))
        store.publish_generation(b, gb, expected_head=store.head(b))
        self.assertEqual(fts.head_storage_digest(store._db, a.view_id), before)
        self.assertEqual(store.head(a), ha)
        self.assertEqual(len(self.fts_tables(store)), 2)
        self.assertEqual(self.found(store, a, 'uniqueside'), [])
        self.assertEqual(self.found(store, b, 'uniqueside'), ['cards/a.md'])

    def test_structural_guards_refuse_rows_outside_the_in_flight_generation(self):
        import sqlite3
        store, repository, handle = self.rows_store()
        self.publish(store, handle, b'# First\nuniquealpha synthetic card\n')
        second = self.publish(store, handle, b'# Second\nuniquebeta synthetic card\n')
        store.close()
        with sqlite3.connect(self.db) as db:
            ordinals = [row[0] for row in db.execute('SELECT ordinal FROM s_generations ORDER BY ordinal')]
            blob = db.execute('SELECT blob_sha FROM s_cards LIMIT 1').fetchone()[0]
            for sql, args, reason in [
                    ('INSERT INTO s_cards(view_id,path,gen_from,gen_to,blob_sha,title,eligible,trust_digest,metadata_json) '
                     "VALUES (?,?,?,NULL,?,'t',0,NULL,'{}')", (handle.view_id, 'cards/z.md', ordinals[-1], blob),
                     'card_generation_invalid'),
                    ('UPDATE s_heads SET ordinal=?,epoch=epoch+1 WHERE view_id=?', (ordinals[0], handle.view_id),
                     'view_head_invalid'),
                    ('UPDATE s_generations SET epoch=9 WHERE ordinal=?', (ordinals[0],), 'generation_immutable'),
                    ('UPDATE s_cards SET title=? WHERE view_id=? AND gen_to IS NULL', ('x', handle.view_id),
                     'card_interval_immutable')]:
                with self.subTest(reason=reason):
                    with self.assertRaisesRegex(sqlite3.DatabaseError, reason):
                        db.execute(sql, args)
        again = self.reopen(repository)
        self.assertEqual(again.head(again.capture_view(handle.worktree_id)), second)

    def test_tampered_card_row_refuses_the_pin(self):
        import sqlite3
        store, repository, handle = self.rows_store()
        self.publish(store, handle, b'# First\nuniquealpha synthetic card\n')
        store.close()
        with sqlite3.connect(self.db) as db:
            trigger = db.execute("SELECT sql FROM sqlite_master WHERE name='s_cards_close_only'").fetchone()[0]
            db.execute('DROP TRIGGER s_cards_close_only')
            db.execute("UPDATE s_cards SET title='Forged' WHERE gen_to IS NULL")
            db.execute(trigger)
        again = self.reopen(repository)
        fresh = again.capture_view(handle.worktree_id)
        self.assertEqual(self.refused(again.pin_view, fresh).reason, 'generation_rows_mismatch')

    def test_prepared_stage_publishes_from_another_store_object(self):
        store, repository, handle = self.rows_store()
        first = self.publish(store, handle, b'# First\nuniquealpha synthetic card\n')
        manifest = self.snapshot(store, handle, body=b'# Second\nuniquebeta synthetic card\n')
        gen = store.prepare_generation(handle, manifest, expected_head=first)
        store.close()
        again = self.reopen(repository)
        fresh = again.capture_view(handle.worktree_id)
        head = again.publish_generation(fresh, gen, expected_head=again.head(fresh))
        self.assertEqual((head['gen_id'], head['epoch']), (gen, first['epoch'] + 1))
        self.assertEqual(self.found(again, fresh, 'uniquebeta'), ['cards/a.md'])
        self.assertEqual(again.publish_generation(fresh, gen, expected_head=first), head)

    def test_card_bytes_changed_after_prepare_refuse_the_publication(self):
        store, _, handle = self.rows_store()
        first = self.publish(store, handle, b'# First\nuniquealpha synthetic card\n')
        manifest = self.snapshot(store, handle, body=b'# Second\nuniquebeta synthetic card\n')
        gen = store.prepare_generation(handle, manifest, expected_head=first)
        (self.root / '.memvane/wiki/cards/a.md').write_bytes(b'# Third\nuniquedelta synthetic card\n')
        self.refused(store.publish_generation, handle, gen, expected_head=first)
        self.assertEqual(store.head(handle), first)
        self.assertEqual(store._db.execute('SELECT COUNT(*) FROM s_generations').fetchone()[0], 1)

    def counts(self, store, view_id):
        db = store._db
        return {table: db.execute(f'SELECT COUNT(*) FROM {table} WHERE view_id=?', (view_id,)).fetchone()[0]
                for table in ('s_generations', 's_cards', 's_deltas', 's_delta_blobs')}

    def test_rollback_reverses_the_last_publication_as_a_new_generation(self):
        store, _, handle = self.rows_store()
        first = self.publish(store, handle, b'# First\nuniquealpha synthetic card\n')
        second = self.publish(store, handle, b'# Second\nuniquebeta synthetic card\n')
        back = store.rollback_generation(handle, expected_head=second)
        self.assertEqual(back['epoch'], second['epoch'] + 1)
        self.assertNotIn(back['gen_id'], (first['gen_id'], second['gen_id']))
        self.assertEqual(self.found(store, handle, 'uniquealpha'), ['cards/a.md'])
        self.assertEqual(self.found(store, handle, 'uniquebeta'), [])
        db = store._db
        rows = {row[0]: row[1:] for row in db.execute(
            'SELECT gen_id,kind,member_digest,logical_fts_digest FROM s_generations WHERE view_id=?',
            (handle.view_id,))}
        self.assertEqual(rows[back['gen_id']][0], 'rollback')
        self.assertEqual(rows[back['gen_id']][1:], rows[first['gen_id']][1:])
        again = store.rollback_generation(handle, expected_head=back)
        self.assertEqual(self.found(store, handle, 'uniquebeta'), ['cards/a.md'])
        self.assertEqual(rows[second['gen_id']][1:], tuple(db.execute(
            'SELECT member_digest,logical_fts_digest FROM s_generations WHERE gen_id=?',
            (again['gen_id'],)).fetchone()))
        third = self.publish(store, handle, b'# Third\nuniquedelta synthetic card\n')
        self.assertEqual(third['epoch'], again['epoch'] + 1)
        self.assertEqual(self.found(store, handle, 'uniquedelta'), ['cards/a.md'])

    def test_rollback_needs_a_prior_generation_and_the_current_head(self):
        store, _, handle = self.rows_store()
        first = self.publish(store, handle, b'# First\nuniquealpha synthetic card\n')
        self.assertEqual(self.refused(store.rollback_generation, handle, expected_head=first).reason,
                         'rollback_prior_missing')
        second = self.publish(store, handle, b'# Second\nuniquebeta synthetic card\n')
        self.assertEqual(self.refused(store.rollback_generation, handle, expected_head=first).reason,
                         'view_head_conflict')
        self.assertEqual(store.head(handle), second)

    def test_collection_keeps_head_and_prior_and_rollback_still_works(self):
        store, _, handle = self.rows_store()
        bodies = [b'# One\nuniqueone synthetic card\n', b'# Two\nuniquetwo synthetic card\n',
                  b'# Three\nuniquethree synthetic card\n', b'# Four\nuniquefour synthetic card\n']
        heads = [self.publish(store, handle, body) for body in bodies]
        before = self.counts(store, handle.view_id)
        self.assertEqual(before['s_generations'], 4)
        removed = store.collect_view_rows(handle, limit=1)
        self.assertEqual(removed['s_cards'], 1)
        while any(store.collect_view_rows(handle, limit=64).get(t) for t in
                  ('s_cards', 's_deltas', 's_generations', 'stage')):
            pass
        after = self.counts(store, handle.view_id)
        self.assertEqual(after, dict(s_generations=2, s_cards=2, s_deltas=2, s_delta_blobs=4))
        self.assertEqual(store._db.execute('SELECT COUNT(*) FROM s_stage_jobs').fetchone()[0], 0)
        self.assertEqual(self.found(store, handle, 'uniquefour'), ['cards/a.md'])
        back = store.rollback_generation(handle, expected_head=heads[-1])
        self.assertEqual(self.found(store, handle, 'uniquethree'), ['cards/a.md'])
        self.assertEqual(back['epoch'], 5)
        self.assertEqual(store._db.execute('SELECT COUNT(*) FROM wiki_blobs').fetchone()[0], 4,
                         'shared blobs are never collected by view row collection')

    def test_head_and_prior_rows_refuse_direct_deletion(self):
        import sqlite3
        store, repository, handle = self.rows_store()
        self.publish(store, handle, b'# First\nuniquealpha synthetic card\n')
        self.publish(store, handle, b'# Second\nuniquebeta synthetic card\n')
        store.close()
        with sqlite3.connect(self.db) as db:
            for sql, reason in [('DELETE FROM s_cards', 'view_row_retained'),
                                ('DELETE FROM s_delta_blobs', 'view_delta_retained'),
                                ('DELETE FROM s_generations', 'view_generation_retained')]:
                with self.subTest(sql=sql):
                    with self.assertRaisesRegex(sqlite3.DatabaseError, reason):
                        db.execute(sql)

    def test_collection_batch_failure_rolls_back_the_whole_batch(self):
        store, _, handle = self.rows_store()
        for body in (b'# One\nuniqueone x\n', b'# Two\nuniquetwo x\n', b'# Three\nuniquethree x\n'):
            self.publish(store, handle, body)

        def state():
            db = store._db
            return (self.counts(store, handle.view_id),
                    db.execute('SELECT COUNT(*) FROM s_stage_jobs').fetchone()[0],
                    db.execute('SELECT COUNT(*) FROM s_stage_items').fetchone()[0])
        before = state()
        self.assertGreater(before[1], 0)
        import laya_view_rows as rows
        real, failed = rows._expired, []

        def late(deadline):
            # Fail only at the batch's last check, after every DELETE ran.
            frame = sys._getframe(1)
            while frame is not None and frame.f_code.co_name != '_rows_collect':
                frame = frame.f_back
            done = {} if frame is None else frame.f_locals.get('removed', {})
            if 's_generations' in done:
                failed.append(dict(done))
                rows.v.fail('fts_deadline_exceeded')
            return real(deadline)
        with mock.patch.object(rows, '_expired', side_effect=late):
            self.refused(store.collect_view_rows, handle, limit=64)
        self.assertTrue(failed and failed[0]['s_cards'] and failed[0]['s_generations'], failed)
        self.assertEqual(state(), before)
        removed = store.collect_view_rows(handle, limit=64)
        self.assertEqual({k: removed[k] for k in ('s_cards', 's_generations')},
                         {k: failed[0][k] for k in ('s_cards', 's_generations')})

    def cards_manifest(self, store, handle, cards, *, scan_id=seed.SCAN):
        """Manifest of several cards: ``{path: (bytes, trusted)}`` written under the wiki."""
        import hashlib
        wiki = self.root / '.memvane/wiki'
        for old in wiki.rglob('*.md'):
            if old.parent != wiki:
                old.unlink()
        raw = (self.root / 'notes/src.md').read_bytes()
        fp = store.put_fingerprint(dict(worktree_id=handle.worktree_id, branch_key=handle.branch_key,
            path='notes/src.md', source_sha=hashlib.sha256(raw).hexdigest(), byte_length=len(raw),
            head_oid=handle.head_oid, tracked_state='unknown' if handle.branch_key == 'non-git' else 'tracked',
            binding_state='observed', scan_id=scan_id, observed_ns=1))
        members = []
        for path, (body, trusted) in sorted(cards.items()):
            target = wiki / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            blob = store.ingest_card(handle.worktree_id, path)
            trust = store.put_attestation(dict(blob_sha=blob, verification_kind='compiled',
                engine_identity='synthetic:codex', policy_digest=handle.policy_digest,
                receipt=dict(schema='memvane-card-evidence-v1', verdict='verified',
                             result_digest='e' * 64, prior_trust=None),
                sources=[dict(fingerprint_id=fp, spans=[dict(start=0, end=4)])])) if trusted else None
            members.append(dict(path=path, blob_sha=blob, display_title='T ' + path, trust_digest=trust))
        return dict(scan_id=scan_id, policy_digest=handle.policy_digest, revocation_epoch=0, complete=True,
                    sources=[dict(path='notes/src.md', fingerprint_id=fp, presence='present')], members=members)

    def publish_cards(self, store, handle, cards):
        head = store.head(handle)
        gen = store.prepare_generation(handle, self.cards_manifest(store, handle, cards), expected_head=head)
        return store.publish_generation(handle, gen, expected_head=head)

    def test_add_delete_eligibility_and_many_paths_publish_reverse_and_collect(self):
        store, _, handle = self.rows_store()
        many = {f'cards/한글-{i:03d}.md': (f'# Card {i}\nuniquemany{i:03d} 본문\n'.encode('utf8'), True)
                for i in range(70)}
        first = self.publish_cards(store, handle, many)
        self.assertEqual(self.found(store, handle, 'uniquemany069'), ['cards/한글-069.md'])
        added = dict(many, **{'cards/zz-new.md': (b'# New\nuniqueadded card\n', True)})
        second = self.publish_cards(store, handle, added)
        self.assertEqual(self.found(store, handle, 'uniqueadded'), ['cards/zz-new.md'])
        removed = {k: val for k, val in added.items() if k != 'cards/한글-000.md'}
        third = self.publish_cards(store, handle, removed)
        self.assertEqual(self.found(store, handle, 'uniquemany000'), [])
        flipped = dict(removed, **{'cards/한글-001.md': (many['cards/한글-001.md'][0], False)})
        fourth = self.publish_cards(store, handle, flipped)
        self.assertEqual(self.found(store, handle, 'uniquemany001'), [])
        db = store._db
        deltas = [tuple(row) for row in db.execute(
            "SELECT SUM(rel='card'),SUM(rel='card_source'),COUNT(*) FROM s_deltas WHERE view_id=? "
            'GROUP BY ordinal ORDER BY ordinal', (handle.view_id,))]
        # One card row and its trusted card source row per change.
        self.assertEqual(deltas, [(1, 1, 2), (1, 1, 2), (1, 1, 2)])
        back = store.rollback_generation(handle, expected_head=fourth)
        self.assertEqual(self.found(store, handle, 'uniquemany001'), ['cards/한글-001.md'])
        self.assertEqual(self.found(store, handle, 'uniquemany000'), [])
        while any(store.collect_view_rows(handle, limit=16).get(t) for t in
                  ('s_cards', 's_deltas', 's_generations', 'stage')):
            pass
        gens = [row[0] for row in db.execute('SELECT gen_id FROM s_generations WHERE view_id=? ORDER BY ordinal',
                                             (handle.view_id,))]
        self.assertEqual(gens, [fourth['gen_id'], back['gen_id']])
        self.assertEqual(db.execute('SELECT COUNT(*) FROM s_cards WHERE view_id=? AND gen_to IS NULL',
                                    (handle.view_id,)).fetchone()[0], 70)
        again = store.rollback_generation(handle, expected_head=back)
        self.assertEqual(self.found(store, handle, 'uniquemany001'), [])
        self.assertEqual(self.found(store, handle, 'uniqueadded'), ['cards/zz-new.md'])
        self.assertEqual(again['epoch'], 6)
        self.assertNotIn(first['gen_id'], gens)
        self.assertNotEqual(second['gen_id'], third['gen_id'])

    def collectable(self):
        store, repository, handle = self.rows_store()
        for body in (b'# One\nuniqueone x\n', b'# Two\nuniquetwo x\n', b'# Three\nuniquethree x\n'):
            head = self.publish(store, handle, body)
        return store, repository, handle, head

    def gc_crash(self, point):
        store, repository, handle, head = self.collectable()
        before = self.counts(store, handle.view_id)
        store.close()
        args = dict(core=str(CORE), db=str(self.db), repository=repository, scope=self.scope,
                    worktree_id=handle.worktree_id, point=point)
        result = subprocess.run([sys.executable, '-B', '-c', GC_CHILD, json.dumps(args)],
                                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
                                capture_output=True, encoding='utf8', timeout=120, check=False)
        self.assertEqual(result.returncode, 17, result.stdout + result.stderr)
        again = self.reopen(repository)
        fresh = again.capture_view(handle.worktree_id)
        self.assertEqual(again.head(fresh)['gen_id'], head['gen_id'])
        self.assertEqual(self.found(again, fresh, 'uniquethree'), ['cards/a.md'])
        return again, fresh, before, head

    def test_collection_exit_before_commit_keeps_every_row(self):
        store, handle, before, _ = self.gc_crash('before_commit')
        self.assertEqual(self.counts(store, handle.view_id), before)

    def test_collection_exit_after_commit_keeps_a_complete_batch_and_resumes(self):
        store, handle, before, head = self.gc_crash('after_commit')
        after = self.counts(store, handle.view_id)
        self.assertEqual(after['s_generations'], 2)
        self.assertLess(after['s_cards'], before['s_cards'])
        self.assertFalse(any(store.collect_view_rows(handle, limit=64).get(t)
                             for t in ('s_cards', 's_deltas', 's_generations')))
        back = store.rollback_generation(handle, expected_head=head)
        self.assertEqual(self.found(store, handle, 'uniquetwo'), ['cards/a.md'])
        self.assertEqual(back['epoch'], head['epoch'] + 1)

    def test_collection_leaves_a_pinned_head_and_a_ready_stage_usable(self):
        store, _, handle, head = self.collectable()
        pin = store.pin_view(handle)
        manifest = self.snapshot(store, handle, body=b'# Four\nuniquefour x\n')
        gen = store.prepare_generation(handle, manifest, expected_head=head)
        try:
            while any(store.collect_view_rows(handle, limit=64).get(t)
                      for t in ('s_cards', 's_deltas', 's_generations', 'stage')):
                pass
            self.assertEqual([row['path'] for row in store.search(pin, 'uniquethree', 10)], ['cards/a.md'])
        finally:
            store.release(pin)
        self.assertEqual(store._db.execute("SELECT state FROM s_stage_jobs WHERE view_id=?",
                                           (handle.view_id,)).fetchone()[0], 'ready')
        published = store.publish_generation(handle, gen, expected_head=head)
        self.assertEqual(published['gen_id'], gen)
        self.assertEqual(self.found(store, handle, 'uniquefour'), ['cards/a.md'])

    def test_collection_waits_for_an_open_reader_and_deletes_nothing(self):
        import sqlite3
        store, _, handle, _ = self.collectable()
        before = self.counts(store, handle.view_id)
        reader = sqlite3.connect(str(self.db), timeout=0)
        self.addCleanup(reader.close)
        reader.execute('BEGIN')
        reader.execute('SELECT COUNT(*) FROM s_cards').fetchone()
        self.assertEqual(self.refused(store.collect_view_rows, handle, limit=64).reason, 'candidate_busy')
        self.assertEqual(self.counts(store, handle.view_id), before)
        reader.rollback()
        self.assertTrue(store.collect_view_rows(handle, limit=64)['s_cards'])

    def test_generations_persist_seal_accumulators_and_a_head_rowid_map(self):
        import laya_view_fts as fts
        store, _, handle = self.rows_store()
        self.publish(store, handle, b'# First\nuniquealpha synthetic card\n')
        db = store._db
        accs = db.execute('SELECT member_acc,fts_acc FROM s_generations').fetchone()
        for acc in accs:
            self.assertEqual(set(json.loads(acc)), {'count', 'bytes', 'sum'})
        name = fts.head_table(handle.view_id)
        self.assertEqual(sorted(tuple(r) for r in db.execute(
            'SELECT path_key,fts_rowid FROM s_head_fts_map WHERE view_id=?', (handle.view_id,))),
            sorted(tuple(r) for r in db.execute(f'SELECT path_key,rowid FROM {name}')))

    def test_rollback_after_restart_reads_only_the_delta(self):
        import laya_view_fts as fts
        import laya_view_rows as rows
        store, repository, handle = self.rows_store()
        many = {f'cards/m{i:03d}.md': (f'# M {i}\nuniquemany{i:03d} x\n'.encode(), True) for i in range(40)}
        self.publish_cards(store, handle, many)
        changed = dict(many, **{'cards/m000.md': (b'# M 0\nuniquechanged x\n', True)})
        head = self.publish_cards(store, handle, changed)
        store.close()
        again = self.reopen(repository)
        fresh = again.capture_view(handle.worktree_id)
        reads = []
        real_blob = type(again)._blob

        def counted(self_, blob, *a, **k):
            reads.append(blob)
            return real_blob(self_, blob, *a, **k)

        def forbidden(*a, **k):
            raise AssertionError('rollback must not read the whole head')
        with mock.patch.object(type(again), '_blob', counted), \
             mock.patch.object(rows.RowsMixin, '_rows_verify', forbidden), \
             mock.patch.object(fts, 'head_storage_digest', forbidden), \
             mock.patch.object(fts, 'head_proof', forbidden), \
             mock.patch.object(fts, 'verify_head_index', forbidden):
            back = again.rollback_generation(fresh, expected_head=head)
        self.assertLessEqual(len(set(reads)), 2, reads)
        self.assertEqual(self.found(again, fresh, 'uniquemany000'), ['cards/m000.md'])
        self.assertEqual(self.found(again, fresh, 'uniquechanged'), [])
        db = again._db
        prior, rolled = (db.execute('SELECT member_digest,logical_fts_digest,physical_fts_digest FROM s_generations '
                                    'WHERE gen_id=?', (gen,)).fetchone() for gen in (
            db.execute('SELECT g.gen_id FROM s_generations g WHERE g.ordinal=(SELECT prior_ordinal FROM '
                       's_generations WHERE gen_id=?)', (head['gen_id'],)).fetchone()[0], back['gen_id']))
        self.assertEqual(tuple(rolled)[:2], tuple(prior)[:2])
        self.assertNotEqual(rolled[2], fts.head_storage_digest(db, handle.view_id),
                            'a rollback seals the deferral value, not a recomputed physical digest')
        third = self.publish_cards(again, fresh, changed)
        self.assertEqual(db.execute('SELECT physical_fts_digest FROM s_generations WHERE gen_id=?',
                                    (third['gen_id'],)).fetchone()[0], fts.head_storage_digest(db, handle.view_id))

    def test_tampered_head_rowid_map_refuses_the_pin(self):
        import sqlite3
        store, repository, handle = self.rows_store()
        self.publish(store, handle, b'# First\nuniquealpha synthetic card\n')
        store.close()
        with sqlite3.connect(self.db) as db:
            db.execute('UPDATE s_head_fts_map SET fts_rowid=fts_rowid+100')
        again = self.reopen(repository)
        self.assertEqual(self.refused(again.pin_view, again.capture_view(handle.worktree_id)).reason,
                         'fts_head_map_mismatch')

    def test_stale_prepare_handle_after_a_head_move_leaves_no_orphan_and_collection_keeps_head_and_prior(self):
        store, repository, handle = self.rows_store()
        first = self.publish(store, handle, b'# First\nuniquealpha synthetic card\n')
        stale_manifest = self.snapshot(store, handle, body=b'# Stale\nuniquestale synthetic card\n')
        stale = store.prepare_generation(handle, stale_manifest, expected_head=first)
        other = self.reopen(repository)
        moved_handle = other.capture_view(handle.worktree_id)
        moved = self.publish(other, moved_handle, b'# Moved\nuniquemoved synthetic card\n')
        reason = self.refused(store.publish_generation, handle, stale, expected_head=first).reason
        self.assertIn(reason, ('view_head_conflict', 'generation_missing'))
        db = other._db
        self.assertEqual(other.head(moved_handle), moved)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM s_generations WHERE gen_id=?', (stale,)).fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM s_stage_jobs WHERE state<>'abandon'").fetchone()[0], 0)
        while any(other.collect_view_rows(moved_handle, limit=64).get(t) for t in ('s_cards', 'stage', 's_deltas')):
            pass
        self.assertEqual(db.execute('SELECT COUNT(*) FROM s_stage_jobs').fetchone()[0], 0)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM s_stage_items').fetchone()[0], 0)
        self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertEqual(sorted(r[0] for r in db.execute('SELECT gen_id FROM s_generations')),
                         sorted([first['gen_id'], moved['gen_id']]))
        back = other.rollback_generation(moved_handle, expected_head=moved)
        self.assertEqual(self.found(other, moved_handle, 'uniquealpha'), ['cards/a.md'])
        self.assertEqual(back['epoch'], moved['epoch'] + 1)

    def sources(self, store, view_id, *, open_only=True):
        return [tuple(r) for r in store._db.execute(
            'SELECT s.path,s.gen_from,s.gen_to,c.source_sha,o.tracked_state FROM s_sources s '
            'JOIN s_source_observations o ON o.observation_id=s.observation_id '
            'LEFT JOIN s_source_content c ON c.content_id=o.content_id WHERE s.view_id=? '
            + ('AND s.gen_to IS NULL ' if open_only else '') + 'ORDER BY s.path,s.gen_from', (view_id,))]

    def test_source_rows_follow_content_not_scan_and_roll_back(self):
        store, repository, handle = self.rows_store()
        first = self.publish(store, handle, b'# First\nuniquealpha synthetic card\n')
        opened = self.sources(store, handle.view_id)
        self.assertEqual([row[0] for row in opened], ['notes/src.md'])
        # Same bytes under another scan id: the same observation, no source rows written.
        manifest = self.snapshot(store, handle, scan_id='2' * 32)
        second = store.publish_generation(handle, store.prepare_generation(handle, manifest, expected_head=first),
                                          expected_head=first)
        self.assertEqual(self.sources(store, handle.view_id, open_only=False), opened)
        (self.root / 'notes/src.md').write_text('# Source\nchanged source body\n', encoding='utf8')
        third = self.publish(store, handle, b'# First\nuniquealpha synthetic card\n')
        rows = self.sources(store, handle.view_id, open_only=False)
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0][3], rows[1][3])
        self.assertEqual(store._db.execute("SELECT COUNT(*) FROM s_deltas WHERE rel='source'").fetchone()[0], 1)
        store.rollback_generation(handle, expected_head=third)
        self.assertEqual(self.sources(store, handle.view_id)[0][3], opened[0][3])
        self.assertEqual(second['epoch'], first['epoch'] + 1)

    def test_tampered_source_row_refuses_the_pin(self):
        import sqlite3
        store, repository, handle = self.rows_store()
        self.publish(store, handle, b'# First\nuniquealpha synthetic card\n')
        store.close()
        with sqlite3.connect(self.db) as db:
            for trigger in ('s_sources_retained',):
                ddl = db.execute('SELECT sql FROM sqlite_master WHERE name=?', (trigger,)).fetchone()[0]
                db.execute('DROP TRIGGER ' + trigger)
            db.execute('DELETE FROM s_sources')
            db.execute(ddl)
        again = self.reopen(repository)
        self.assertEqual(self.refused(again.pin_view, again.capture_view(handle.worktree_id)).reason,
                         'generation_rows_mismatch')

    def test_unsupported_row_inputs_refuse_before_any_write(self):
        store, _, handle = self.rows_store()
        manifest = self.snapshot(store, handle)
        spec = dict(recipe_id='a' * 64, profile_id='b' * 64)
        self.refused(store.prepare_generation, handle, manifest, expected_head=store.head(handle),
                     vector_spec=spec)
        self.assertEqual(store._db.execute('SELECT COUNT(*) FROM s_stage_jobs').fetchone()[0], 0)

    def test_default_create_keeps_the_existing_format(self):
        store, _, handle = self.seeded()
        tables = {row[0] for row in store._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn('generation_slots', tables)
        self.assertNotIn('s_generations', tables)

    def crash(self, point):
        store, repository, handle = self.rows_store()
        first = self.publish(store, handle, b'# First\nuniquealpha synthetic card\n')
        (self.root / '.memvane/wiki/cards/a.md').write_bytes(b'# Second\nuniquebeta synthetic card\n')
        manifest = self.snapshot(store, handle)
        store.close()
        args = dict(core=str(CORE), db=str(self.db), repository=repository, scope=self.scope,
                    worktree_id=handle.worktree_id, manifest=manifest, point=point)
        result = subprocess.run([sys.executable, '-B', '-c', CRASH_CHILD, json.dumps(args)],
                                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
                                capture_output=True, encoding='utf8', timeout=120, check=False)
        self.assertEqual(result.returncode, 17, result.stdout + result.stderr)
        again = self.reopen(repository)
        return again, again.capture_view(handle.worktree_id), first

    def test_process_exit_before_commit_leaves_the_previous_head(self):
        store, handle, first = self.crash('before_commit')
        self.assertEqual(store.head(handle)['gen_id'], first['gen_id'])
        self.assertEqual(self.found(store, handle, 'uniquealpha'), ['cards/a.md'])

    def test_process_exit_after_commit_leaves_the_complete_new_head(self):
        store, handle, first = self.crash('after_commit')
        head = store.head(handle)
        self.assertEqual(head['epoch'], first['epoch'] + 1)
        self.assertEqual(self.found(store, handle, 'uniquebeta'), ['cards/a.md'])
        self.assertEqual(self.found(store, handle, 'uniquealpha'), [])


if __name__ == '__main__':
    unittest.main()

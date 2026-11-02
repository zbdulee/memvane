"""Bounded synthetic candidate budgets; elapsed times are observations, not SLAs."""
import copy
import importlib.util
import json
import sys
from pathlib import Path
import time
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('generation_fixture', HERE / 'laya-view-generations-checks.py')
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
blob_helpers = helpers.helpers


class BudgetChecks(helpers.GenerationCase):
    def measured(self, store, operation, *args, **kwargs):
        counts = {'total': 0, 'select': 0, 'match': 0}

        def trace(sql):
            counts['total'] += 1
            counts['select'] += int(sql.lstrip().upper().startswith('SELECT'))
            counts['match'] += int(' MATCH ' in sql.upper())

        store._db.set_trace_callback(trace)
        started = time.perf_counter_ns()
        try:
            result = operation(*args, **kwargs)
        except BaseException:
            print(json.dumps(dict(budget_operation_failed=operation.__name__,
                milliseconds=(time.perf_counter_ns()-started)/1_000_000,
                sql_trace=counts),sort_keys=True),file=sys.stderr,flush=True)
            raise
        finally:
            elapsed = (time.perf_counter_ns() - started) / 1_000_000
            store._db.set_trace_callback(None)
        return result, dict(milliseconds=elapsed, sql_trace=counts)

    def db_bytes(self, store):
        page_count = store._db.execute('PRAGMA page_count').fetchone()[0]
        page_size = store._db.execute('PRAGMA page_size').fetchone()[0]
        return dict(logical=page_count * page_size, file=store._path.stat().st_size)

    def cards(self, root, count):
        paths = [f'cards/{i:05d}.md' for i in range(count)]
        for path in paths:
            (self.wiki(root) / path).write_bytes(blob_helpers.CARD)
        return paths

    def claims(self, store, handle, blob, paths, *, source_count=1):
        # Source identities and receipts are caller-owned synthetic assertions.
        # No source-body insertion, provider, compiler or owner proof is implied.
        sources = []
        for i in range(source_count):
            path = f'notes/claimed-{i:05d}.md'
            fingerprint = store.put_fingerprint(dict(
                worktree_id=handle.worktree_id, branch_key=handle.branch_key,
                path=path, source_sha='a' * 64, byte_length=32,
                head_oid=handle.head_oid,
                tracked_state='unknown' if handle.branch_key == 'non-git' else 'tracked',
                binding_state='observed', scan_id=helpers.SCAN, observed_ns=1))
            sources.append(dict(path=path, fingerprint_id=fingerprint, presence='present'))
        trust = store.put_attestation(dict(
            blob_sha=blob, verification_kind='compiled', engine_identity='synthetic:budget',
            policy_digest=handle.policy_digest,
            receipt=dict(schema='memvane-card-evidence-v1', verdict='verified',
                         result_digest='e' * 64, prior_trust=None),
            sources=[dict(fingerprint_id=sources[-1]['fingerprint_id'],
                          spans=[dict(start=0, end=4)])]))
        return dict(scan_id=helpers.SCAN, policy_digest=handle.policy_digest,
                    revocation_epoch=0, complete=True, sources=sources,
                    members=[dict(path=path, blob_sha=blob, display_title='Shared',
                                  trust_digest=trust) for path in paths])

    def publish_and_read(self, store, handle, manifest, *, limit=10, **filters):
        # Measure complete candidate work even on a slow CI host. The product
        # default stays 2s; deadline refusal/recovery has its own reader case.
        # This is a scale/latency observation, never an assertion of the SLA.
        filters = dict(filters, deadline_seconds=30)
        base = store.head(handle)
        gen, prepare = self.measured(store, store.prepare_generation, handle, manifest,
                                     expected_head=base)
        head, publish = self.measured(store, store.publish_generation, handle, gen,
                                     expected_head=base)
        self.assertEqual((head['view_id'], head['gen_id'], head['epoch']),
                         (handle.view_id, gen, base['epoch'] + 1))
        pin, pin_time = self.measured(store, store.pin_view, handle)
        try:
            self.assertEqual((pin.view_id, pin.gen_id, pin.epoch),
                             (handle.view_id, gen, head['epoch']))
            rows, search = self.measured(store, store.search, pin, 'wiki', limit, **filters)
            self.assertTrue(rows)
            self.assertEqual({(r['view_id'], r['gen_id']) for r in rows},
                             {(handle.view_id, gen)})
            for row in rows:
                self.assertEqual(row['evidence'], 'commitment-only')
                self.assertIs(row['owner_verified'], False)
                self.assertEqual(row['body'], blob_helpers.CARD.decode('utf8'))
            self.assertGreater(search['sql_trace']['match'], 0)
        finally:
            store.release(pin)
        warm = []
        for _ in range(3):
            repeat, repeat_pin = self.measured(store, store.pin_view, handle)
            try:
                repeated, repeat_search = self.measured(store, store.search, repeat,
                                                        'wiki', limit, **filters)
                self.assertEqual(repeated, rows)
                self.assertEqual((repeat.view_id, repeat.gen_id, repeat.epoch),
                                 (handle.view_id, gen, head['epoch']))
                warm.append(dict(pin=repeat_pin, search=repeat_search))
            finally:
                store.release(repeat)
        return head, rows, dict(default_search_deadline_seconds=2,
                               measurement_deadline_seconds=30,
                               prepare=prepare, publish=publish,
                               cold_pin=pin_time, cold_search=search, warm=warm)

    def test_01_ten_git_views_share_one_blob_and_keep_their_own_trust(self):
        main = self.project('budget-main')
        paths = self.cards(main, 1800)
        for args in [('init', '-q'), ('add', '-A'), ('commit', '-q', '-m', 'synthetic budget')]:
            self.git(main, *args)
        roots = [main]
        for i in range(1, 10):
            linked = self.home / 'projects' / f'budget-linked-{i}'
            self.git(main, 'worktree', 'add', '-q', '-b', f'budget-{i}', str(linked))
            roots.append(linked)
        store, repository, first = self.seeded(main)
        handles = [first]
        for root in roots[1:]:
            record = blob_helpers.view.register_worktree(str(root), repository)
            wid = store.register_worktree(str(root), record)
            handles.append(store.capture_view(wid))
        self.assertEqual(len({h.view_id for h in handles}), 10)
        # One wiki admission for all paths and all views, never one ingest per card.
        blob = store.ingest_card(first.worktree_id, paths[0])
        before = self.db_bytes(store)
        heads, fingerprints, trusts, metrics = [], set(), set(), []
        for root, handle in zip(roots, handles):
            self.assertTrue(all((self.wiki(root) / p).is_file() for p in paths))
            manifest = self.claims(store, handle, blob, paths)
            fingerprint = manifest['sources'][0]['fingerprint_id']
            trust = manifest['members'][0]['trust_digest']
            fingerprints.add(fingerprint); trusts.add(trust)
            self.assertEqual(store.fingerprint(fingerprint)['worktree_id'], handle.worktree_id)
            self.assertEqual(store.attestation(trust)['sources'][0]['fingerprint_id'], fingerprint)
            head, rows, timing = self.publish_and_read(store, handle, manifest)
            self.assertEqual(len(rows), 10)
            self.assertEqual({r[0] for r in store._db.execute(
                'SELECT trust_digest FROM wiki_members WHERE gen_id=?', (head['gen_id'],))}, {trust})
            self.assertEqual(store._db.execute(
                'SELECT COUNT(*) FROM wiki_members WHERE gen_id=? AND eligible=1',
                (head['gen_id'],)).fetchone()[0], 1800)
            heads.append(head)
            metrics.append(dict(view_id=handle.view_id, worktree_id=handle.worktree_id,
                                gen_id=head['gen_id'], epoch=head['epoch'],
                                fingerprint_id=fingerprint, trust_digest=trust, eligible_members=1800,
                                operations=timing, database_bytes=self.db_bytes(store)))
        self.assertEqual((len(fingerprints), len(trusts), len({h['gen_id'] for h in heads})), (10, 10, 10))
        for handle, expected in zip(handles, heads):
            self.assertEqual(store.head(handle), expected)
        counts = store.counts()
        self.assertEqual((counts['wiki_blobs'], counts['card_attestations']), (1, 10))
        after = self.db_bytes(store)
        self.assertGreater(after['logical'], before['logical'])
        print(json.dumps(dict(case='ten_git_views', views=10, members_per_view=1800,
            wiki_blobs=counts['wiki_blobs'], fingerprints=len(fingerprints), trusts=len(trusts),
            database_before=before, database_after=after,
            database_growth={k: after[k] - before[k] for k in before}, metrics=metrics), sort_keys=True))

    def test_02_2049_members_and_sources_survive_sealing_and_filtered_limit(self):
        paths = self.cards(self.root, 2049)
        store, _, handle = self.seeded()
        blob = store.ingest_card(handle.worktree_id, paths[0])
        before = self.db_bytes(store)
        manifest = self.claims(store, handle, blob, paths, source_count=2049)
        head, rows, timing = self.publish_and_read(store, handle, manifest, limit=1,
                                                  path_prefixes=[paths[-1]])
        self.assertEqual([row['path'] for row in rows], [paths[-1]])
        counts = {}
        for table in ('wiki_members', 'source_members', 'card_sources'):
            counts[table] = store._db.execute(
                f'SELECT COUNT(*) FROM {table} WHERE gen_id=?', (head['gen_id'],)).fetchone()[0]
            self.assertEqual(counts[table], 2049)
        actual = {r[0] for r in store._db.execute(
            'SELECT fingerprint_id FROM source_members WHERE gen_id=?', (head['gen_id'],))}
        self.assertEqual(actual, {s['fingerprint_id'] for s in manifest['sources']})
        ordinal = store._db.execute('SELECT ordinal FROM generations WHERE gen_id=?',
                                    (head['gen_id'],)).fetchone()[0]
        fts_count = store._db.execute(f'SELECT COUNT(*) FROM wiki_fts_{ordinal}').fetchone()[0]
        self.assertEqual(fts_count, 2049)
        pin = store.pin_view(handle)
        try:
            limited, limited_time = self.measured(store, store.search, pin, 'wiki', 100)
            self.assertEqual(len(limited), 100)
            self.assertEqual(len({row['path'] for row in limited}), 100)
        finally:
            store.release(pin)
        # Metadata-only cap rejection happens before missing-file reads or new slots.
        too_many = copy.deepcopy(manifest)
        too_many['members'] = [dict(manifest['members'][0], path=f'cap/{i:05d}.md')
                               for i in range(20001)]
        previous = store.counts()
        error = self.refused(store.prepare_generation, handle, too_many,
                             expected_head=store.head(handle))
        self.assertEqual(error.reason, 'manifest_bounds')
        self.assertEqual(store.counts(), previous)
        self.assertEqual(store.counts()['wiki_blobs'], 1)
        after = self.db_bytes(store)
        print(json.dumps(dict(case='above_2048', members=2049, sources=2049,
            stored_counts=counts, fts_rows=fts_count, returned_limit_100=len(limited),
            filtered_tail_path=rows[0]['path'], cap_rejection=error.reason,
            operations=timing, limited_search=limited_time,
            database_before=before, database_after=after,
            database_growth={k: after[k] - before[k] for k in before}), sort_keys=True))


if __name__ == '__main__':
    unittest.main()

"""Bounded synthetic candidate budgets; elapsed times are observations, not SLAs."""
import copy
import importlib.util
import json
import math
import os
import subprocess
import sys
from pathlib import Path
import time
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('generation_fixture', HERE / 'laya-view-generations-checks.py')
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
blob_helpers = helpers.helpers
WARM_FLAG = 'MEMVANE_VIEW_WARM_RED'
WARM_ARG = '--warm-sample'
WARM_SAMPLES = 30
WARM_BODIES = 'MEMVANE_VIEW_WARM_BODIES'


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

    def cards(self, root, count, *, distinct=False):
        paths = [f'cards/{i:05d}.md' for i in range(count)]
        for i, path in enumerate(paths):
            body = blob_helpers.CARD + (f'synthetic distinct card {i:05d}\n'.encode() if distinct else b'')
            (self.wiki(root) / path).write_bytes(body)
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

    def ten_views(self, *, distinct=False):
        """Ten linked Git worktrees over the same 1,800 committed cards."""
        main = self.project('budget-main')
        paths = self.cards(main, 1800, distinct=distinct)
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
        if distinct:
            # One admission per distinct body, shared by all ten views.
            return store, repository, roots, handles, paths, [
                store.ingest_card(first.worktree_id, path) for path in paths]
        # One wiki admission for all paths and all views, never one ingest per card.
        blob = store.ingest_card(first.worktree_id, paths[0])
        return store, repository, roots, handles, paths, blob

    def test_01_ten_git_views_share_one_blob_and_keep_their_own_trust(self):
        store, repository, roots, handles, paths, blob = self.ten_views()
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

    @unittest.skipUnless(os.environ.get(WARM_FLAG) == '1',
                         'K3 read-trust targets are pending; set ' + WARM_FLAG + '=1')
    def test_03_warm_fresh_process_pin_and_search_meet_k3_targets(self):
        # RED for K3: hooks start one python process per call, so every sample
        # is a fresh process over the already-published ten-view database.
        bodies = os.environ.get(WARM_BODIES, 'shared')
        self.assertIn(bodies, ('shared', 'distinct'))
        store, repository, _, handles, paths, blob = self.ten_views(distinct=bodies == 'distinct')
        for handle in handles:
            if bodies == 'distinct':
                manifest = self.claims(store, handle, blob[0], paths[:1])
                trust = store.attestation(manifest['members'][0]['trust_digest'])
                manifest['members'] = [dict(path=path, blob_sha=sha, display_title='Shared',
                    trust_digest=store.put_attestation(dict(trust, blob_sha=sha)))
                    for path, sha in zip(paths, blob)]
            else:
                manifest = self.claims(store, handle, blob, paths)
            base = store.head(handle)
            gen = store.prepare_generation(handle, manifest, expected_head=base)
            store.publish_generation(handle, gen, expected_head=base)
        path, scope = str(store._path), store._scope
        repository = json.loads(json.dumps(store._repository))
        store.close()

        def child(handle, profile):
            raw = subprocess.run(
                [sys.executable, '-B', str(Path(__file__).resolve()), WARM_ARG, json.dumps(dict(
                    path=path, scope=scope, repository=repository, worktree_id=handle.worktree_id,
                    view_id=handle.view_id, query='wiki', limit=10, profile=profile))],
                stdin=subprocess.DEVNULL, capture_output=True, encoding='utf8', timeout=120, check=False)
            self.assertEqual(raw.returncode, 0, raw.stdout + raw.stderr)
            return json.loads(raw.stdout.strip().splitlines()[-1])

        profiles = [child(handles[i], True) for i in (0, 5)]
        samples = []
        for i in range(WARM_SAMPLES):
            started = time.perf_counter_ns()
            sample = child(handles[i % len(handles)], False)
            sample['process_ms'] = (time.perf_counter_ns() - started) / 1_000_000
            samples.append(sample)
        summary = {}
        for op in ('open', 'capture', 'pin', 'search', 'capture_pin', 'process'):
            key = op + '_ms'
            values = [s[key] for s in samples if s.get(key) is not None]
            summary[op] = dict(n=len(values), p50=nearest_rank(values, 50), p95=nearest_rank(values, 95),
                               max=max(values) if values else None)
        outcomes = {}
        for sample in samples:
            outcomes[sample['outcome']] = outcomes.get(sample['outcome'], 0) + 1
        print(json.dumps(dict(case='warm_fresh_process', bodies=bodies, python=sys.version.split()[0], views=len(handles),
            members_per_view=len(paths), samples=len(samples), percentile='nearest-rank',
            search_deadline_seconds=2, pin_deadline='default(30s)', targets_ms=dict(pin=50, search=150),
            outcomes=outcomes, summary=summary, samples_detail=samples, profiles=profiles),
            sort_keys=True), flush=True)
        self.assertEqual(outcomes, {'served': len(samples)})
        self.assertLessEqual(summary['pin']['p95'], 50)
        self.assertLessEqual(summary['search']['p95'], 150)


def nearest_rank(values, percent):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percent / 100 * len(ordered)) - 1)]


def _timed(stats, name, function):
    def wrapper(*args, **kwargs):
        started = time.perf_counter_ns()
        try:
            return function(*args, **kwargs)
        finally:
            item = stats.setdefault(name, dict(calls=0, ms=0.0))
            item['calls'] += 1
            item['ms'] += (time.perf_counter_ns() - started) / 1_000_000
    return wrapper


def _profile_top(profiler, limit=25):
    import pstats
    stats = pstats.Stats(profiler).stats
    rows = [dict(fn=f'{Path(fn).name}:{line}:{name}', calls=nc, tottime_ms=round(tt * 1000, 2),
                 cumtime_ms=round(ct * 1000, 2)) for (fn, line, name), (_, nc, tt, ct, _) in stats.items()]
    core = [r for r in rows if r['fn'].startswith(('laya_', 'source_spans'))]
    return dict(by_tottime=sorted(rows, key=lambda r: -r['tottime_ms'])[:limit],
                core_by_cumtime=sorted(core, key=lambda r: -r['cumtime_ms'])[:limit])


def warm_sample(raw):
    """One hook-shaped reader process: open, capture, pin, search, exit."""
    from unittest import mock
    from memvane.store.blob import blob_store as blob_store
    from memvane.store.checkout import checkout_view as checkout
    from memvane.store.view import view_fts as fts
    from memvane.store.view import view_store as views
    args = json.loads(raw)
    timers, sql = {}, {}
    patches = [mock.patch.object(checkout, '_git', _timed(timers, 'git', checkout._git)),
               mock.patch.object(blob_store, '_header', _timed(timers, '_header', blob_store._header)),
               mock.patch.object(fts, 'storage_digest', _timed(timers, 'fts.storage_digest', fts.storage_digest)),
               mock.patch.object(fts, 'assert_rows', _timed(timers, 'fts.assert_rows', fts.assert_rows))]
    for name in ('_observe', '_read_generation', '_verify_generation', '_derive', '_stored',
                 '_verified_vectors', '_blob', '_attestation', '_fingerprint'):
        patches.append(mock.patch.object(views.ViewStore, name,
                                         _timed(timers, name, getattr(views.ViewStore, name))))
    result = dict(view_id=args['view_id'], outcome='served')
    phase = {}

    def run(op, function, *call_args, **kwargs):
        phase.clear()
        before = {k: dict(x) for k, x in timers.items()}
        counts = {}
        if store is not None:
            store._db.set_trace_callback(lambda q: counts.__setitem__(
                q.lstrip().split(None, 1)[0].upper() if q.strip() else '', counts.get(
                    q.lstrip().split(None, 1)[0].upper() if q.strip() else '', 0) + 1))
        profiler = None
        if args['profile'] and op in ('pin', 'search'):
            import cProfile
            profiler = cProfile.Profile()
            profiler.enable()
        started = time.perf_counter_ns()
        try:
            return function(*call_args, **kwargs)
        except views.BlobStoreError as exc:
            result['outcome'] = 'refused:' + exc.reason
            result['refused_op'] = op
            return None
        finally:
            result[op + '_ms'] = (time.perf_counter_ns() - started) / 1_000_000
            if profiler is not None:
                profiler.disable()
                result[op + '_profile'] = _profile_top(profiler)
            if store is not None:
                store._db.set_trace_callback(None)
                sql[op] = counts
            result[op + '_inclusive'] = {k: dict(calls=x['calls'] - before.get(k, {}).get('calls', 0),
                ms=round(x['ms'] - before.get(k, {}).get('ms', 0.0), 3)) for k, x in timers.items()
                if x['calls'] != before.get(k, {}).get('calls', 0)}

    store = None
    for item in patches:
        item.start()
    try:
        store = run('open', views.ViewStore.open_reader, args['path'], repository=args['repository'],
                    scope_id=args['scope'])
        handle = store and run('capture', store.capture_view, args['worktree_id'])
        pin = handle and run('pin', store.pin_view, handle)
        if pin is not None:
            # Product default search deadline (2 s); profiles use 30 s to see the whole cost.
            seconds = 30 if args['profile'] else 2
            rows = run('search', store.search, pin, args['query'], args['limit'], deadline_seconds=seconds)
            result['rows'] = None if rows is None else len(rows)
        if 'capture_ms' in result and 'pin_ms' in result:
            result['capture_pin_ms'] = result['capture_ms'] + result['pin_ms']
        result['sql'] = sql
        result['git_calls'] = {op: x.get('git', {}).get('calls', 0) for op, x in (
            (k[:-len('_inclusive')], result[k]) for k in list(result) if k.endswith('_inclusive'))}
    finally:
        for item in patches:
            item.stop()
        if store is not None:
            store.close()
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == WARM_ARG:
        warm_sample(sys.argv[2])
    else:
        unittest.main()

"""Per-step effective inventory reads stay within one page (U8 S2)."""
import importlib
import importlib.util
import json
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / 'core'))
SCALE = int(os.environ.get('U1C_SCALE_CARDS', '3749'))


def reference_effective(snapshot, decision, *, after, include_ineligible):
    """The pre-paging full-scan merge, kept as an independent oracle."""
    from laya_index_host_route import wiki_index_roots
    from laya_incremental_docs_query import _exclusions
    import laya_shadow_migration as migration
    base, _, _, side = snapshot.connections
    prefixes = tuple(name + '/' for name in wiki_index_roots(decision['collections']))
    excluded_paths, excluded_hashes = _exclusions(base, decision['scopeId'])
    columns = 'm.path,m.hash,m.eligible' if include_ineligible else 'm.path,m.hash'
    eligibility = '' if include_ineligible else 'AND m.eligible=1 '
    rows = base.execute('SELECT ' + columns + ' FROM membership m WHERE m.gen=? AND m.scope=? ' + eligibility +
                        'AND EXISTS (SELECT 1 FROM json_each(?) p '
                        'WHERE substr(m.path,1,length(p.value))=p.value) ORDER BY m.path',
                        (snapshot.published[0], decision['scopeId'], json.dumps(prefixes))).fetchall()
    docs = [] if side is None else side.execute('SELECT path,hash FROM docs ORDER BY path').fetchall()
    return reference_merge(rows, docs, prefixes=prefixes, excluded_paths=excluded_paths,
                           excluded_hashes=excluded_hashes, include_ineligible=include_ineligible,
                           after=after, card=migration._wiki_card_path)


def reference_merge(rows, docs, *, prefixes, excluded_paths, excluded_hashes, include_ineligible,
                    after, card):
    if include_ineligible:
        members = {path: (digest, 'base', eligible) for path, digest, eligible in rows}
    else:
        members = {row[0]: (row[1], 'base', 1) for row in rows}
    members = {path: value for path, value in members.items() if card(path, prefixes)}
    for path, digest in docs:
        if not card(path, prefixes):
            continue
        previous = members.get(path)
        if previous is not None and previous[0] == digest and previous[2] == 1:
            continue
        members.pop(path, None)
        if include_ineligible or (path not in excluded_paths and digest not in excluded_hashes):
            members[path] = (digest, 'sidecar', 1)
    result = []
    for path in sorted(members):
        digest, source, eligible = members[path]
        if after is not None and path <= after:
            continue
        hidden = path in excluded_paths or digest in excluded_hashes
        if not include_ineligible and hidden:
            continue
        row = {'path': path, 'hash': digest, 'source': source}
        if include_ineligible:
            row.update(eligible=eligible, excluded=hidden)
        result.append(row)
    return result


def scale_fixture(test):
    normal_spec = importlib.util.spec_from_file_location(
        'paging_normal_seed', HERE / 'laya-normal-writer-connections-checks.py')
    normal_module = importlib.util.module_from_spec(normal_spec)
    normal_spec.loader.exec_module(normal_module)
    seed_module = normal_module
    for _ in range(6):
        seed_module = seed_module.existing
    parent = seed_module.base.published.MoveCase
    publish, head = parent.publish_all, parent.head

    # Same generation recipe as the U8 step-budget harness (pr6-scale-shadow.py).
    def many(case, roots):
        wiki = case.auto / 'wiki'
        template = (wiki / 'concepts/heron-route.md').read_text(encoding='utf8') + (' synthetic filler' * 180) + '\n'
        target = wiki / 'scale'
        target.mkdir()
        for i in range(SCALE - 3):
            body = template + ' synthetic unique %04d\n' % i
            (target / ('synthetic-%04d.md' % i)).write_text(body, encoding='utf8')
        return publish(case, roots)

    def initial_three(case):
        gen, members = head(case)
        return gen, members[:3]

    normal = normal_module.NormalWriterConnectionChecks('runTest')
    with mock.patch.object(parent, 'publish_all', new=many), \
            mock.patch.object(parent, 'head', new=initial_three), \
            mock.patch.object(seed_module.MoveSealChecks, 'PUBLISH_SIDECAR', False):
        normal.setUp()
    test.addCleanup(normal.doCleanups)
    case = normal.case
    subprocess.run(['git', '-c', 'init.defaultBranch=main', 'init', '-q', str(case.project)],
                   check=True, capture_output=True, timeout=10)
    subprocess.run(['git', '-C', str(case.project), '-c', 'commit.gpgsign=false',
                    '-c', 'core.hooksPath=' + os.devnull, '-c', 'user.name=Fixture',
                    '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty',
                    '-qm', 'fixture'], check=True, capture_output=True, timeout=10)
    time.sleep(1.1)
    return normal


class ReadCounter:
    """Member rows read in one step.

    ``count`` is the _wiki_card_path calls (the full scan made one per base and
    sidecar row); ``fetched`` is the rows actually returned by SQLite from
    membership and docs by the paged readers.
    """

    def __init__(self, migration):
        self.migration = migration
        self.count = self.fetched = 0

    def __enter__(self):
        original = self.migration._wiki_card_path
        def counted(path, prefixes):
            self.count += 1
            return original(path, prefixes)
        self.patches = [mock.patch.object(self.migration, '_wiki_card_path', side_effect=counted)]
        for name in ('_membership_rows', '_sidecar_rows', '_membership_rows_at', '_sidecar_rows_at'):
            reader = getattr(self.migration, name, None)
            if reader is not None:
                def fetched(*args, _reader=reader, **kwargs):
                    rows = _reader(*args, **kwargs)
                    self.fetched += len(rows)
                    return rows
                self.patches.append(mock.patch.object(self.migration, name, side_effect=fetched))
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self.patches):
            patch.stop()

    def cost(self):
        return max(self.count, self.fetched)


class CapturePagingChecks(unittest.TestCase):
    def test_scale_step_reads_one_page_and_resumes_from_intent_cursor(self):
        normal = scale_fixture(self)
        seed, case = normal.seed, normal.case
        checkout = importlib.import_module('laya_checkout_view')
        views = importlib.import_module('laya_view_store')
        migration = importlib.import_module('laya_shadow_migration')
        readers = importlib.import_module('laya_reader_open')
        ledger = importlib.import_module('laya_ledger_common')
        repository = checkout.register_repository(str(case.project))
        worktree = checkout.register_worktree(str(case.project), repository)
        candidate = case.state / 'paging-shadow.sqlite'
        views.ViewStore.create(candidate, project_root=str(case.project), repository=repository,
                               worktree=worktree, scope_id=case.route['scopeId']).close()
        store = views.ViewStore.open(candidate, repository=repository, scope_id=case.route['scopeId'])
        self.addCleanup(store.close)
        intent_path = case.state / 'shadow-migration-intent.json'
        page = migration.MAX_BATCH
        # A step may open several page captures (copy page, observation
        # rechecks); each reads at most page + 1 rows per source.
        bound = 4 * (page + 1)
        costs = []
        with seed.authority():
            decision = seed.reader.journal.select_route(str(case.project), action='recall')
            with seed.lease.resolve_endpoint(case.project, action='recall', host_root=case.plugin) as endpoint:
                with readers.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 30) as snapshot:
                    expected = reference_effective(snapshot, decision, after=None, include_ineligible=False)
                    inventory = reference_effective(snapshot, decision, after=None, include_ineligible=True)
                    total = len(expected)
                    self.assertEqual(total, SCALE)
                    # Concatenated pages equal the full-scan oracle page by page.
                    for include in (False, True):
                        oracle = inventory if include else expected
                        cursor, joined, pages = None, [], 0
                        while True:
                            got = migration.capture_effective(snapshot, endpoint=endpoint, decision=decision,
                                after=cursor, deadline=time.monotonic() + 30, include_ineligible=include)
                            after = None if cursor is None else cursor['after']
                            rest = [row for row in oracle if after is None or row['path'] > after]
                            self.assertEqual(got['members'], rest[:page])
                            self.assertEqual(got['next'] is None, len(rest) <= page)
                            joined.extend(got['members'])
                            pages += 1
                            cursor = got['next']
                            if cursor is None:
                                break
                        self.assertEqual(joined, oracle)
                        self.assertEqual(pages, -(-total // page))
                    # Catch-up pages (_only) read only the requested paths and
                    # keep the full-scan filter, exclusion and page boundaries.
                    picked = [row['path'] for row in expected[::7]]
                    only = frozenset(picked + [case.WIKI + '/scale/absent.md', case.WIKI + '/index.md'])
                    wanted = [row for row in expected if row['path'] in only]
                    cursor, joined = None, []
                    while True:
                        with ReadCounter(migration) as reads:
                            got = migration.capture_effective(snapshot, endpoint=endpoint, decision=decision,
                                after=cursor, deadline=time.monotonic() + 30, _only=only)
                        # Skipped targets (absent, meta) may pull one more chunk.
                        self.assertLessEqual(reads.cost(), 2 * (page + 1))
                        after = None if cursor is None else cursor['after']
                        rest = [row for row in wanted if after is None or row['path'] > after]
                        self.assertEqual(got['members'], rest[:page])
                        self.assertEqual(got['next'] is None, len(rest) <= page)
                        joined.extend(got['members'])
                        cursor = got['next']
                        if cursor is None:
                            break
                    self.assertEqual(joined, wanted)
                    self.assertGreater(len(wanted), page)
                    full = migration.capture_effective(snapshot, endpoint=endpoint, decision=decision,
                        after=None, deadline=time.monotonic() + 30, include_ineligible=True,
                        _all_inventory=True)
                    self.assertEqual(full['members'], inventory)
                    self.assertIsNone(full['next'])
            with ReadCounter(migration) as reads:
                intent = migration.begin_migration(store, worktree_id=worktree['worktreeId'],
                    project=case.project, executing_root=case.plugin, deadline=time.monotonic() + 30)
            costs.append(('begin', reads.cost()))
            store.close()
            store = views.ViewStore.open(candidate, repository=repository, scope_id=case.route['scopeId'])
            self.addCleanup(store.close)
            for number in (1, 2):
                with ReadCounter(migration) as reads:
                    result = migration.advance_shadow(store, project=case.project, executing_root=case.plugin,
                                                      deadline=time.monotonic() + 30)
                self.assertEqual(result['stage'], 'copy')
                costs.append(('copy%d' % number, reads.cost()))
                saved = ledger._read_json(intent_path)
                self.assertEqual(saved['cursor'], {'after': expected[number * page - 1]['path'],
                                                   'observation': intent['observation']})
            self.assertEqual(store.counts()['wiki_blobs'], 2 * page)
            before_crash = intent_path.read_bytes()
            payload = case.home / 'paging-crash.json'
            payload.write_text(json.dumps({'home': str(case.home),
                'runtime': os.environ['MEMVANE_TEST_RUNTIME_ROOT'], 'plugin': str(case.plugin),
                'proof': case.proof, 'candidate': str(candidate), 'repository': repository,
                'worktree': worktree, 'scope': case.route['scopeId'], 'project': str(case.project),
                'batch_limit': page, 'mode': 'shadow-intent-kill'}), encoding='utf8')
            killed = subprocess.run([sys.executable, '-B', str(HERE / 'laya-shadow-crash-child.py'),
                                     str(payload)], capture_output=True, text=True, timeout=120)
            self.assertEqual(killed.returncode, -9, killed.stdout + killed.stderr)
            self.assertIn('shadow-step-before-intent', killed.stdout)
            self.assertEqual(intent_path.read_bytes(), before_crash)
            self.assertEqual(store.counts()['wiki_blobs'], 3 * page)
            with ReadCounter(migration) as reads:
                result = migration.advance_shadow(store, project=case.project, executing_root=case.plugin,
                                                  deadline=time.monotonic() + 30)
            costs.append(('copy3_resumed', reads.cost()))
            self.assertEqual(result['stage'], 'copy')
            self.assertEqual(ledger._read_json(intent_path)['cursor']['after'], expected[3 * page - 1]['path'])
            self.assertEqual(store.counts()['wiki_blobs'], 3 * page)
        print('paging_step_reads scale=%d page=%d bound=%d %s' % (
            SCALE, page, bound, ' '.join('%s=%d' % item for item in costs)), flush=True)
        for label, count in costs:
            self.assertLessEqual(count, bound, (label, count, 'effective inventory', total))

    def test_merge_matches_full_scan_oracle_on_synthetic_rows(self):
        migration = importlib.import_module('laya_shadow_migration')
        prefixes = ('w/',)
        names = ['a.md', 'b/c.md', 'index.md', 'SCHEMA.md', 'log.md', 'z/index.md', 'é.md',
                 '가.md', 'ab.md', 'a-b.md', 'b.md', 'zz.md']
        digests = ['%064x' % n for n in range(6)]
        rng = random.Random(1103)
        cases = 0
        for _ in range(400):
            paths = sorted({'w/' + rng.choice(names) for _ in range(rng.randint(0, 10))})
            rows = [(p, rng.choice(digests), rng.choice((0, 1))) for p in paths if rng.random() < .8]
            docs = [(p, rng.choice(digests)) for p in paths if rng.random() < .5]
            excluded_paths = frozenset(p for p in paths if rng.random() < .2)
            excluded_hashes = frozenset(d for d in digests if rng.random() < .15)
            for include in (False, True):
                base = rows if include else [(p, d) for p, d, e in rows if e == 1]
                oracle = reference_merge(base, docs, prefixes=prefixes, excluded_paths=excluded_paths,
                    excluded_hashes=excluded_hashes, include_ineligible=include, after=None,
                    card=migration._wiki_card_path)
                for limit in (1, 2, 3, None):
                    after, joined = None, []
                    while True:
                        members, more = migration._merge_effective(
                            iter([r for r in rows if (include or r[2] == 1) and (after is None or r[0] > after)]),
                            iter([d for d in docs if after is None or d[0] > after]),
                            prefixes=prefixes, excluded_paths=excluded_paths,
                            excluded_hashes=excluded_hashes, include_ineligible=include, limit=limit)
                        rest = [row for row in oracle if after is None or row['path'] > after]
                        self.assertEqual(members, rest if limit is None else rest[:limit])
                        self.assertEqual(more, limit is not None and len(rest) > limit)
                        joined.extend(members)
                        cases += 1
                        if not more:
                            break
                        after = members[-1]['path']
                    self.assertEqual(joined, oracle)
        print('paging_merge_oracle_cases %d' % cases, flush=True)


if __name__ == '__main__':
    unittest.main()

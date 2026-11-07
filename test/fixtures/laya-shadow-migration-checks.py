"""Synthetic selected-v5 endpoint checks for migration membership."""
import hashlib
import errno
from contextlib import contextmanager
import json
import importlib
import importlib.util
from pathlib import Path
import sqlite3
import os
import re
import shutil
import subprocess
import sys
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / 'core'))
spec = importlib.util.spec_from_file_location('migration_seed', HERE / 'laya-reader-move-seal-checks.py')
seed_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed_module)


class busy_timeouts_armed:
    """Record every ``PRAGMA busy_timeout=N`` the connection runs while active."""

    _pattern = re.compile(r'\s*PRAGMA\s+busy_timeout\s*=\s*(\d+)\s*;?\s*', re.I)

    def __init__(self, db):
        self.db, self.values = db, []

    def _trace(self, sql):
        match = self._pattern.fullmatch(sql)
        if match:
            self.values.append(int(match.group(1)))

    def __enter__(self):
        self.db.set_trace_callback(self._trace)
        return self

    def __exit__(self, *exc):
        self.db.set_trace_callback(None)
        return False


class MigrationSnapshotChecks(unittest.TestCase):
    def test_base_search_applies_limit_before_legacy_trust(self):
        class BaseOnly(seed_module.MoveSealChecks):
            PUBLISH_SIDECAR = False
        parent = seed_module.base.published.MoveCase
        original = parent.publish_all
        def with_untrusted(case, roots):
            wiki = case.auto / 'wiki' / 'concepts'
            trusted = wiki / 'heron-route.md'
            trusted.write_text(trusted.read_text(encoding='utf8') + '\ncaporderprobe\n', encoding='utf8')
            (wiki / 'otter-ledger.md').write_text('caporderprobe ' * 100, encoding='utf8')
            return original(case, roots)
        seed = BaseOnly('runTest')
        with mock.patch.object(parent, 'publish_all', new=with_untrusted):
            seed.setUp()
        self.addCleanup(seed.doCleanups)
        case = seed.case
        subprocess.run(['git', '-c', 'init.defaultBranch=main', 'init', '-q', str(case.project)],
                       check=True, capture_output=True, timeout=10)
        subprocess.run(['git', '-C', str(case.project), '-c', 'commit.gpgsign=false',
                        '-c', 'core.hooksPath=' + os.devnull, '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty',
                        '-qm', 'fixture'], check=True, capture_output=True, timeout=10)
        time.sleep(1.1)
        checkout = importlib.import_module('laya_checkout_view')
        blobs = importlib.import_module('laya_blob_store')
        migration = importlib.import_module('laya_shadow_migration')
        opens = importlib.import_module('laya_reader_open')
        repository = checkout.register_repository(str(case.project))
        worktree = checkout.register_worktree(str(case.project), repository)
        store = blobs.BlobStore.create(case.state / 'base-cap-shadow.sqlite',
            project_root=str(case.project), repository=repository, worktree=worktree,
            scope_id=case.route['scopeId'])
        self.addCleanup(store.close)
        scan = {'id': '1' * 32, 'observed_ns': time.time_ns()}
        with seed.authority():
            decision = seed.reader.journal.select_route(str(case.project), action='recall')
            with seed.lease.resolve_endpoint(case.project, action='recall', host_root=case.plugin) as endpoint:
                with opens.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 10) as first:
                    self.assertIsNone(first.connections[3])
                    observation = migration.capture_effective(first, endpoint=endpoint, decision=decision,
                        after=None, deadline=time.monotonic() + 10)['observation']
                original_inspect = opens.inspect_warm_snapshot
                absent_sidecar = Path(endpoint.record['sidecar_path'])
                self.assertFalse(absent_sidecar.exists())
                with mock.patch.object(migration, 'MAX_BATCH', 1), \
                     mock.patch.object(opens, 'inspect_warm_snapshot', wraps=original_inspect) as inspected:
                    self.assertIsNone(migration.inspect_sidecar_bounded(endpoint=endpoint, decision=decision,
                        observation=observation, deadline=time.monotonic() + 30))
                    self.assertEqual(inspected.call_count, 1)
                    with self.assertRaisesRegex(Exception, 'migration_snapshot_changed'):
                        migration.inspect_sidecar_bounded(endpoint=endpoint, decision=decision,
                            observation='0' * 64, deadline=time.monotonic() + 30)
                self.assertFalse(absent_sidecar.exists())
                with opens.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 10) as snapshot:
                    staged = migration.import_wiki_batch(store, worktree_id=worktree['worktreeId'],
                        snapshot=snapshot, endpoint=endpoint, decision=decision, after=None,
                        provenance_scan=scan, deadline=time.monotonic() + 10)
                    self.assertNotIn(case.WIKI + '/concepts/otter-ledger.md',
                                     [row['path'] for row in staged['trusted']])
                    view_module = importlib.import_module('laya_view_store')
                    view = view_module.ViewStore.open(case.state / 'base-cap-shadow.sqlite',
                        repository=repository, scope_id=case.route['scopeId'])
                    try:
                        handle = view.capture_view(worktree['worktreeId'])
                        proof = {'snapshot': snapshot, 'endpoint': endpoint, 'decision': decision,
                                 'provenance_scan': scan}
                        evidence = migration.verify_generation_members(view, worktree_id=worktree['worktreeId'],
                            **proof, deadline=time.monotonic() + 30)
                        manifest = {'scan_id': scan['id'], 'policy_digest': handle.policy_digest,
                            'revocation_epoch': 0, 'complete': True, 'sources': [], 'members': evidence['members']}
                        head = view.head(handle)
                        generation = view.prepare_generation(handle, manifest, expected_head=head,
                                                             legacy_snapshot=proof)
                        view.publish_generation(handle, generation, expected_head=head, legacy_snapshot=proof)
                        pin = view.pin_view(handle)
                        try:
                            index = importlib.import_module('laya_index_a')
                            base = snapshot.connections[0]
                            ordinal = base.execute('SELECT ord FROM gen_index WHERE gen=?',
                                                   (snapshot.published[0],)).fetchone()[0]
                            old = index._search_pinned_fts(base, decision['scopeId'], snapshot.published[0],
                                ordinal, ('caporderprobe',), 1, path_prefixes=(case.WIKI + '/',),
                                deadline=time.monotonic() + 10)[0]
                            self.assertEqual(old[0]['path'], case.WIKI + '/concepts/otter-ledger.md')
                            self.assertEqual(view.search(pin, 'caporderprobe', 1), [])
                            self.assertEqual([row['path'] for row in view.search(pin, 'caporderprobe', 2)],
                                             ['concepts/heron-route.md'])
                            self.assertEqual([row['path'] for row in view.search(pin, 'caporderprobe', 1,
                                excluded_paths=['concepts/otter-ledger.md'])], ['concepts/heron-route.md'])
                        finally:
                            view.release(pin)
                    finally:
                        view.close()

    def test_inventory_includes_actual_excluded_published_generation_member(self):
        class BaseOnly(seed_module.MoveSealChecks):
            PUBLISH_SIDECAR = False
        parent = seed_module.base.published.MoveCase
        original = parent.publish_all
        def exclude_before_publish(case, roots):
            path = case.WIKI + '/concepts/otter-ledger.md'
            with case.runtime(roots) as runtime:
                runtime.index.exclude(runtime.scope, path=path)
            return original(case, roots)
        seed = BaseOnly('runTest')
        with mock.patch.object(parent, 'publish_all', new=exclude_before_publish):
            seed.setUp()
        self.addCleanup(seed.doCleanups)
        case = seed.case
        opens = importlib.import_module('laya_reader_open')
        migration = importlib.import_module('laya_shadow_migration')
        with seed.authority():
            decision = seed.reader.journal.select_route(str(case.project), action='recall')
            with seed.lease.resolve_endpoint(case.project, action='recall', host_root=case.plugin) as endpoint:
                with opens.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 10) as snapshot:
                    inventory = migration.capture_effective(snapshot, endpoint=endpoint, decision=decision,
                        after=None, deadline=time.monotonic() + 10, include_ineligible=True)
                    rows = {row['path']: row for row in inventory['members']}
                    path = case.WIKI + '/concepts/otter-ledger.md'
                    self.assertEqual(rows[path]['eligible'], 0)
                    self.assertTrue(rows[path]['excluded'])
                    search = migration.capture_effective(snapshot, endpoint=endpoint, decision=decision,
                        after=None, deadline=time.monotonic() + 10)
                    self.assertNotIn(path, [row['path'] for row in search['members']])

    def test_inventory_includes_ineligible_and_excluded_rows_without_changing_search_capture(self):
        seed = seed_module.MoveSealChecks('runTest')
        seed.setUp()
        self.addCleanup(seed.doCleanups)
        case = seed.case
        opens = importlib.import_module('laya_reader_open')
        migration = importlib.import_module('laya_shadow_migration')
        with seed.authority():
            decision = seed.reader.journal.select_route(str(case.project), action='recall')
            with seed.lease.resolve_endpoint(case.project, action='recall', host_root=case.plugin) as endpoint:
                with opens.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 10) as snapshot:
                    base = snapshot.connections[0]
                    current = migration.capture_effective(snapshot, endpoint=endpoint,
                        decision=decision, after=None, deadline=time.monotonic() + 10)
                    self.assertEqual(len(current['members']), 3)
                    excluded = current['members'][0]['path']
                    fake_path = excluded.rsplit('/', 1)[0] + '/synthetic-ineligible.md'
                    fake_hash = current['members'][0]['hash']
                    db_type, original = type(base), type(base).execute
                    class Rows:
                        def __init__(self, rows): self.rows = rows
                        def fetchall(self): return self.rows
                    def synthetic_rows(db, statement, *args, **kwargs):
                        if db is base and statement.startswith('SELECT m.path,m.hash,m.eligible '):
                            actual = original(db, statement, *args, **kwargs).fetchall()
                            actual = [(path, digest, 0 if path == excluded else eligible)
                                      for path, digest, eligible in actual]
                            return Rows(sorted(actual + [(fake_path, fake_hash, 0)]))
                        return original(db, statement, *args, **kwargs)
                    with mock.patch.object(db_type, 'execute', new=synthetic_rows), \
                            mock.patch.object(migration, '_exclusions', return_value=({excluded}, set())):
                        inventory = migration.capture_effective(snapshot, endpoint=endpoint,
                            decision=decision, after=None, deadline=time.monotonic() + 10,
                            include_ineligible=True)
                    rows = {row['path']: row for row in inventory['members']}
                    self.assertEqual(rows[fake_path]['eligible'], 0)
                    self.assertFalse(rows[fake_path]['excluded'])
                    self.assertTrue(rows[excluded]['excluded'])
                    self.assertEqual(rows[excluded]['source'], 'sidecar')
                    self.assertEqual(rows[excluded]['eligible'], 1)
                    with self.assertRaisesRegex(Exception, 'migration_snapshot_changed'):
                        migration.capture_effective(snapshot, endpoint=endpoint, decision=decision,
                            after={'after': excluded, 'observation': inventory['observation']},
                            deadline=time.monotonic() + 10)

    def test_duplicate_legacy_revision_preserves_reader_trust(self):
        class DuplicateRevision(seed_module.MoveSealChecks):
            def before_conversion(self):
                with sqlite3.connect(self.sidecar) as db:
                    rowid, self.duplicate_path, title, body = db.execute(
                        'SELECT rowid,path,title,body FROM docs ORDER BY path LIMIT 1').fetchone()
                    revision = next(line for line in body.splitlines() if line.strip().startswith('- {'))
                    parsed = json.loads(revision.strip()[2:])
                    alternate = {**parsed, 'size': parsed['size'] + 1}
                    body = body.replace(revision, revision + '\n' + revision +
                                        '\n  - ' + json.dumps(alternate), 1)
                    spans = [{'path': parsed['path'], 'sourceSha256': parsed['sha256'],
                              'startLine': n, 'endLine': n, 'spanSha256': 'a' * 64}
                             for n in range(1, 11)]
                    body = body.replace('\n---\n', '\nsourceSpans:\n' +
                        ''.join('  - ' + json.dumps(span) + '\n' for span in spans) + '---\n', 1)
                    digest = hashlib.sha256(body.encode()).hexdigest()
                    db.execute('UPDATE docs SET hash=?,body=? WHERE rowid=?', (digest, body, rowid))
                    db.execute('DELETE FROM docs_fts WHERE rowid=?', (rowid,))
                    db.execute('INSERT INTO docs_fts(rowid,path,title,body) VALUES(?,?,?,?)',
                               (rowid, self.duplicate_path, title, body))
        seed = DuplicateRevision('runTest')
        seed.setUp()
        self.addCleanup(seed.doCleanups)
        case = seed.case
        checkout = importlib.import_module('laya_checkout_view')
        blobs = importlib.import_module('laya_blob_store')
        migration = importlib.import_module('laya_shadow_migration')
        opens = importlib.import_module('laya_reader_open')
        repository = checkout.register_repository(str(case.project))
        worktree = checkout.register_worktree(str(case.project), repository)
        store = blobs.BlobStore.create(case.state / 'duplicate-shadow.sqlite',
            project_root=str(case.project), repository=repository, worktree=worktree,
            scope_id=case.route['scopeId'])
        self.addCleanup(store.close)
        with seed.authority():
            decision = seed.reader.journal.select_route(str(case.project), action='recall')
            with seed.lease.resolve_endpoint(case.project, action='recall', host_root=case.plugin) as endpoint:
                with opens.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 10) as snapshot:
                    scan = {'id': '2' * 32, 'observed_ns': time.time_ns()}
                    result = migration.import_wiki_batch(store, worktree_id=worktree['worktreeId'],
                        snapshot=snapshot, endpoint=endpoint, decision=decision, after=None,
                        deadline=time.monotonic() + 10, provenance_scan=scan)
                    matched = [row for row in result['trusted'] if row['path'] == seed.duplicate_path]
                    self.assertEqual(len(matched), 1)
                    evidence = store.attestation(matched[0]['trust_digest'])
                    self.assertEqual(len(evidence['sources']), 2)
                    self.assertEqual(sum(len(source['spans']) for source in evidence['sources']), 10)
                    self.assertTrue(store.verify())
                # The writer ingest judges the same published bytes with the same rule.
                wiki_root = store._db.execute('SELECT wiki_root FROM worktrees WHERE worktree_id=?',
                                              (worktree['worktreeId'],)).fetchone()[0]
                relative = seed.duplicate_path.split('/', 1)[1]
                Path(wiki_root, relative).write_bytes(store.document(matched[0]['hash'])['body'].encode('utf8'))
                attestations = store.counts()['card_attestations']
                self.assertEqual(store.ingest_attested_card(worktree['worktreeId'], relative,
                                                            decision=decision, provenance_scan=scan),
                                 {'blob_sha': matched[0]['hash'], 'trust_digest': matched[0]['trust_digest']})
                self.assertEqual(store.counts()['card_attestations'], attestations)

    def test_legacy_evidence_matches_selected_reader_without_opening_raw_source(self):
        seed = seed_module.MoveSealChecks('runTest')
        seed.setUp()
        self.addCleanup(seed.doCleanups)
        case = seed.case
        opens = importlib.import_module('laya_reader_open')
        migration = importlib.import_module('laya_shadow_migration')
        host = importlib.import_module('laya_host_response')
        wiki_card = importlib.import_module('wiki_card')
        freshness = importlib.import_module('wiki_freshness')
        with seed.authority():
            decision = seed.reader.journal.select_route(str(case.project), action='recall')
            with seed.lease.resolve_endpoint(case.project, action='recall', host_root=case.plugin) as endpoint:
                with opens.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 10) as snapshot:
                    batch = migration.capture_wiki_bytes(snapshot, endpoint=endpoint, decision=decision,
                        after=None, deadline=time.monotonic() + 10)
                    row = batch['members'][0]
                    body = row['body'].replace('\r\n', '\n').replace('\r', '\n')
                    block = body[4:body.index('\n---\n', 4)]
                    revisions = wiki_card.frontmatter_source_revisions(block)
                    self.assertTrue(revisions)
                    (case.project / revisions[0]['path']).unlink()
                    with mock.patch.object(freshness, 'snapshot_bytes', side_effect=AssertionError('raw read')):
                        self.assertTrue(host._verified_wiki_row(row, decision,
                            source_skip_paths=tuple(decision['recallSettings']['skipPaths']), current_source=False))
                        evidence = migration.legacy_card_evidence(row, decision)
                    self.assertEqual(evidence['revisions'], revisions)
                    self.assertEqual(evidence['revisions'][0]['size'], revisions[0]['size'])
                    span = {'path': revisions[0]['path'], 'sourceSha256': revisions[0]['sha256'],
                            'startLine': 1, 'endLine': 2, 'spanSha256': 'a' * 64}
                    with_span = dict(row, body=body.replace('\n---\n',
                        '\nsourceSpans:\n  - ' + json.dumps(span) + '\n---\n', 1))
                    parsed = migration.legacy_card_evidence(with_span, decision)
                    self.assertEqual(parsed['spans'], [span])
                    self.assertEqual(parsed['locators'], [span['path'] + '#L1-L2'])
                    nested = dict(with_span, body=with_span['body'].replace(
                        revisions[0]['path'], 'docs/' + revisions[0]['path']))
                    self.assertIsNotNone(migration.legacy_card_evidence(nested, decision))
                    with_skip = dict(decision, recallSettings={**decision['recallSettings'],
                                      'skipPaths': ['docs/']})
                    self.assertIsNone(migration.legacy_card_evidence(nested, with_skip))
                    tampered = dict(with_span, body=with_span['body'].replace(
                        '"sourceSha256": "' + revisions[0]['sha256'] + '"',
                        '"sourceSha256": "' + 'b' * 64 + '"', 1))
                    self.assertIsNone(migration.legacy_card_evidence(tampered, decision))
                    bad = dict(row, body=body.replace('"size": ' + str(revisions[0]['size']), '"size": null', 1))
                    self.assertFalse(host._verified_wiki_row(bad, decision, current_source=False))
                    self.assertIsNone(migration.legacy_card_evidence(bad, decision))

    def test_selected_snapshot_imports_effective_wiki_metadata_once(self):
        seed = seed_module.MoveSealChecks('runTest')
        seed.setUp()
        self.addCleanup(seed.doCleanups)
        case, lease = seed.case, seed.lease
        opens = importlib.import_module('laya_reader_open')
        with seed.authority():
            decision = seed.reader.journal.select_route(str(case.project), action='recall')
            with lease.resolve_endpoint(case.project, action='recall', host_root=case.plugin) as endpoint:
                with opens.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 10) as snapshot:
                    base, _, _, side = snapshot.connections
                    expected = set(base.execute('SELECT path,hash FROM membership WHERE gen=? AND scope=? AND eligible=1',
                        (snapshot.published[0], decision['scopeId'])).fetchall())
                    self.assertEqual(len(expected), 3)
                    self.assertEqual(set(side.execute('SELECT path,hash FROM docs').fetchall()), expected)
                    migration = importlib.import_module('laya_shadow_migration')
                    page = migration.capture_effective(snapshot, endpoint=endpoint,
                        decision=decision, after=None, deadline=time.monotonic() + 10)
                    for invalid_deadline in (float('nan'), float('inf')):
                        with self.assertRaisesRegex(Exception, 'reader_snapshot_deadline'):
                            migration.capture_effective(snapshot, endpoint=endpoint, decision=decision,
                                after=None, deadline=invalid_deadline)
                    self.assertEqual({(row['path'], row['hash']) for row in page['members']}, expected)
                    self.assertEqual(len(page['members']), 3)
                    self.assertTrue(all(row['source'] == 'base' for row in page['members']))
                    self.assertTrue(all(set(row) == {'path', 'hash', 'source'} for row in page['members']))
                    self.assertIsNone(page['next'])
                    with mock.patch.object(migration, 'MAX_BATCH', 2):
                        first = migration.capture_effective(snapshot, endpoint=endpoint,
                            decision=decision, after=None, deadline=time.monotonic() + 10)
                        self.assertEqual(len(first['members']), 2)
                        second = migration.capture_effective(snapshot, endpoint=endpoint,
                            decision=decision, after=first['next'], deadline=time.monotonic() + 10)
                        self.assertEqual(first['members'] + second['members'], page['members'])
                        self.assertIsNone(second['next'])
                    cursor = {'after': page['members'][0]['path'], 'observation': page['observation']}
                    remaining = migration.capture_effective(snapshot, endpoint=endpoint,
                        decision=decision, after=cursor, deadline=time.monotonic() + 10)
                    self.assertEqual(remaining['members'], page['members'][1:])
                    with self.assertRaisesRegex(Exception, 'migration_snapshot_changed'):
                        migration.capture_effective(snapshot, endpoint=endpoint, decision=decision,
                            after={**cursor, 'observation': '0' * 64}, deadline=time.monotonic() + 10)


    def test_changed_sidecar_shadows_old_published_membership(self):
        class ChangedSidecar(seed_module.MoveSealChecks):
            def before_conversion(self):
                with sqlite3.connect(self.sidecar) as db:
                    rowid, self.changed_path, title, original, profile, scan = db.execute(
                        'SELECT rowid,path,title,body,profile_id,scan_id FROM docs ORDER BY path LIMIT 1').fetchone()
                    body = original + '\nSynthetic changed sidecar body for migration. overlaptoken presenceedge untrustedprobe\n'
                    self.changed_hash = hashlib.sha256(body.encode()).hexdigest()
                    db.execute('UPDATE docs SET hash=?,body=? WHERE rowid=?',
                               (self.changed_hash, body, rowid))
                    db.execute('DELETE FROM docs_fts WHERE rowid=?', (rowid,))
                    db.execute('INSERT INTO docs_fts(rowid,path,title,body) VALUES(?,?,?,?)',
                               (rowid, self.changed_path, title, body))
                    self.noise_paths = [self.changed_path.split('/', 1)[0] +
                                        ('/aaa-noise/' if number == 99 else '/noise/') +
                                        'untrusted-%03d.md' % number for number in range(100)]
                    for number, path in enumerate(self.noise_paths):
                        noise = ('overlaptoken ' * 500 + 'noiseonly ' +
                                 ('presenceedge' if number == 99 else '') +
                                 ('untrustedprobe' if number == 0 else ''))
                        noise_hash = hashlib.sha256(noise.encode()).hexdigest()
                        if number == 0:
                            cursor = db.execute('INSERT INTO docs(rowid,path,hash,title,body,profile_id,chunk_count,scan_id) '
                                                'VALUES(?,?,?,?,?,?,?,?)',
                                                (-1,path,noise_hash,'Noise',noise,profile,1,scan))
                        else:
                            cursor = db.execute('INSERT INTO docs(path,hash,title,body,profile_id,chunk_count,scan_id) '
                                                'VALUES(?,?,?,?,?,?,?)',
                                                (path,noise_hash,'Noise',noise,profile,1,scan))
                        db.execute('INSERT INTO docs_fts(rowid,path,title,body) VALUES(?,?,?,?)',
                                   (cursor.lastrowid,path,'Noise',noise))
        seed = ChangedSidecar('runTest')
        seed.setUp()
        self.addCleanup(seed.doCleanups)
        case, lease = seed.case, seed.lease
        opens = importlib.import_module('laya_reader_open')
        migration = importlib.import_module('laya_shadow_migration')
        subprocess.run(['git', '-c', 'init.defaultBranch=main', 'init', '-q', str(case.project)],
                       check=True, capture_output=True, timeout=10)
        subprocess.run(['git', '-C', str(case.project), '-c', 'commit.gpgsign=false',
                        '-c', 'core.hooksPath=' + os.devnull, '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty',
                        '-qm', 'fixture'], check=True, capture_output=True, timeout=10)
        checkout = importlib.import_module('laya_checkout_view')
        views = importlib.import_module('laya_view_store')
        repository = checkout.register_repository(str(case.project))
        worktree = checkout.register_worktree(str(case.project), repository)
        view = views.ViewStore.create(case.state / 'changed-sidecar-shadow.sqlite',
            project_root=str(case.project), repository=repository, worktree=worktree,
            scope_id=case.route['scopeId'])
        self.addCleanup(view.close)
        time.sleep(1.1)
        with seed.authority():
            intent = migration.begin_migration(view, worktree_id=worktree['worktreeId'],
                project=case.project, executing_root=case.plugin, deadline=time.monotonic() + 30)
            for _ in range(10):
                intent = migration.advance_migration(view, project=case.project,
                    executing_root=case.plugin, deadline=time.monotonic() + 30)
                if intent['phase'] == 'copied':break
            self.assertEqual(intent['phase'], 'copied')
            for _ in range(10):
                intent = migration.advance_verification(view, project=case.project,
                    executing_root=case.plugin, deadline=time.monotonic() + 30)
                if intent['verification']['complete']: break
            self.assertTrue(intent['verification']['complete'])
            for corpus_sidecar in (False, True):
                key = 'sidecar' if corpus_sidecar else 'base'
                for _ in range(10):
                    intent = migration.advance_corpus(view, project=case.project, executing_root=case.plugin,
                        sidecar=corpus_sidecar, deadline=time.monotonic() + 30)
                    if intent['corpora'][key]['complete']: break
                self.assertTrue(intent['corpora'][key]['complete'])
            decision = seed.reader.journal.select_route(str(case.project), action='recall')
            with lease.resolve_endpoint(case.project, action='recall', host_root=case.plugin) as endpoint:
                with opens.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 10) as snapshot:
                    page = migration.capture_effective(snapshot, endpoint=endpoint, decision=decision,
                        after=None, deadline=time.monotonic() + 10)
                    matches = [row for row in page['members'] if row['path'] == seed.changed_path]
                    self.assertEqual(matches, [{'path': seed.changed_path, 'hash': seed.changed_hash,
                                                'source': 'sidecar'}])
                    batch = migration.capture_wiki_bytes(snapshot, endpoint=endpoint, decision=decision,
                        after=None, deadline=time.monotonic() + 10)
                    selected = [row for row in batch['members'] if row['path'] == seed.changed_path]
                    self.assertEqual(len(selected), 1)
                    self.assertEqual(hashlib.sha256(selected[0]['body'].encode()).hexdigest(), seed.changed_hash)
                    corpus = migration.capture_sidecar_fts_corpus(snapshot, endpoint=endpoint,
                        decision=decision, after=None, deadline=time.monotonic() + 10)
                    selected_corpus = [row for row in corpus['members'] if row['path'] == seed.changed_path]
                    self.assertEqual(len(selected_corpus), 1)
                    self.assertEqual(selected_corpus[0]['hash'], seed.changed_hash)
                    self.assertEqual(selected_corpus[0]['body'], selected[0]['body'])
                    with mock.patch.dict(lease._reader_selections, {}, clear=True):
                        with self.assertRaisesRegex(Exception, 'reader_selection_unproved'):
                            migration.sidecar_fts_commitment(snapshot, endpoint=endpoint, decision=decision,
                                                             deadline=time.monotonic() + 10)
                    with self.assertRaisesRegex(Exception, 'migration_lexical_sidecar_unproved'):
                        migration.stage_base_fts_corpus(None, worktree_id='0' * 32, snapshot=snapshot,
                            endpoint=endpoint, decision=decision, deadline=time.monotonic() + 10)
                    handle = view.capture_view(worktree['worktreeId'])
                    proof = {'snapshot': snapshot, 'endpoint': endpoint, 'decision': decision,
                             'provenance_scan': intent['provenance_scan']}
                    mapped = migration.verify_generation_members(view, worktree_id=worktree['worktreeId'],
                        **proof, deadline=time.monotonic() + 30)
                    relative = seed.changed_path.split('/', 1)[1]
                    self.assertTrue(any(row['path'] == relative and row['blob_sha'] == seed.changed_hash
                                        for row in mapped['members']))
                    manifest = {'scan_id': intent['provenance_scan']['id'],
                        'policy_digest': handle.policy_digest, 'revocation_epoch': 0,
                        'complete': True, 'sources': [], 'members': mapped['members']}
                    base = view.head(handle)
                bounded_proof = {'observation': intent['observation'], 'endpoint': endpoint,
                    'decision': decision, 'provenance_scan': intent['provenance_scan'],
                    'commitments': {'migration_id': intent['migration_id'],
                        'verification': intent['verification'], 'corpora': intent['corpora']}}
                wrong_side = {**bounded_proof, 'commitments': {**bounded_proof['commitments'],
                    'corpora': {**intent['corpora'], 'sidecar': {**intent['corpora']['sidecar'],
                                                                  'digest': '0' * 64}}}}
                with self.assertRaisesRegex(Exception, 'migration_commitment_mismatch'):
                    view.prepare_generation(handle, manifest, expected_head=base, legacy_snapshot=wrong_side)
                self.assertEqual(view._db.execute('SELECT COUNT(*) FROM generation_slots').fetchone()[0], 0)
                gen = view.prepare_generation(handle, manifest, expected_head=base, legacy_snapshot=bounded_proof)
                # This test-held reader stays open across the whole-corpus pages below.
                with opens.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 180) as snapshot:
                    proof = {'snapshot': snapshot, 'endpoint': endpoint, 'decision': decision,
                             'provenance_scan': intent['provenance_scan']}
                    with mock.patch.object(migration, 'sidecar_fts_commitment',
                            return_value={'rows': len(corpus['members']), 'digest': '0'*64}):
                        with self.assertRaisesRegex(Exception, 'migration_sidecar_changed'):
                            view.publish_generation(handle, gen, expected_head=base, legacy_snapshot=proof)
                    self.assertIsNone(view.head(handle)['gen_id'])
                    migration.publish_shadow_generation(view, gen, project=case.project,
                        executing_root=case.plugin, deadline=time.monotonic() + 30)
                    self.assertTrue(view.verify_views())
                    pin = view.pin_view(handle)
                    try:
                        with self.assertRaisesRegex(Exception, 'migration_lexical_sidecar_unproved'):
                            view.search(pin, 'Synthetic', 10)
                        side = snapshot.connections[3]
                        old = side.execute("SELECT d.path,d.hash,bm25(docs_fts,1.5,4.0,1.0) "
                            "FROM docs_fts JOIN docs d ON d.rowid=docs_fts.rowid "
                            "WHERE docs_fts MATCH ? AND d.path=?", ('migration', seed.changed_path)).fetchone()
                        self.assertIsNotNone(old)
                        new = view.search(pin, 'migration', 10, _legacy_origin='sidecar')
                        match = next(row for row in new if row['path'] == relative)
                        self.assertEqual(match['blob_sha'], old[1])
                        self.assertAlmostEqual(match['score'], old[2], places=12)
                        first = side.execute("SELECT d.path FROM docs_fts JOIN docs d ON d.rowid=docs_fts.rowid "
                            "WHERE docs_fts MATCH ? ORDER BY bm25(docs_fts,1.5,4.0,1.0),d.path LIMIT 1",
                            ('overlaptoken',)).fetchone()[0]
                        self.assertIn(first, seed.noise_paths)
                        self.assertEqual(view.search(pin, 'overlaptoken', 1, _legacy_origin='sidecar'), [])
                        second = view.search(pin, 'overlaptoken', 2, _legacy_origin='sidecar')
                        self.assertEqual(second, [])
                        scoped = view.search(pin, 'overlaptoken', 1, path_prefixes=['concepts'],
                                             _legacy_origin='sidecar')
                        self.assertEqual([row['path'] for row in scoped], [relative])
                        filtered = view.search(pin, 'overlaptoken', 1,
                                               excluded_paths=[path.split('/',1)[1] for path in seed.noise_paths],
                                               _legacy_origin='sidecar')
                        self.assertEqual([row['path'] for row in filtered], [relative])
                        self.assertEqual(view.search(pin, 'overlaptoken', 1, path_prefixes=[],
                                                     _legacy_origin='sidecar'), [])
                        self.assertEqual(view.search(pin, 'overlaptoken', 10, _legacy_origin='base'), [])
                        comparison = migration.compare_lexical_queries(snapshot, endpoint=endpoint,
                            decision=decision, store=view, pin=pin,
                            queries=('migration', 'heron'), deadline=time.monotonic() + 30)
                        self.assertEqual(comparison['query_count'], 2)
                        old_probe = side.execute('SELECT d.path FROM docs_fts f JOIN docs d ON d.rowid=f.rowid '
                            'WHERE docs_fts MATCH ? AND d.path IN (SELECT value FROM json_each(?)) LIMIT 1',
                            ('presenceedge', json.dumps([seed.changed_path, *seed.noise_paths]))).fetchone()[0]
                        self.assertEqual(old_probe, seed.changed_path)
                        with view._reading(time.monotonic() + 10) as db:
                            ordinal = db.execute('SELECT ordinal FROM generations WHERE gen_id=?', (gen,)).fetchone()[0]
                            name = 'wiki_fts_side_' + str(ordinal)
                            new_probe = db.execute('SELECT path_key FROM ' + name + ' WHERE ' + name +
                                ' MATCH ? AND path_key IN (SELECT value FROM json_each(?)) LIMIT 1',
                                ('presenceedge', json.dumps([seed.changed_path, *seed.noise_paths]))).fetchone()[0]
                        self.assertEqual(new_probe, old_probe)
                        comparison = migration.compare_lexical_queries(snapshot, endpoint=endpoint, decision=decision,
                            store=view, pin=pin, queries=('presenceedge',), deadline=time.monotonic() + 30)
                        self.assertEqual(comparison['query_count'], 1)
                        recalled = migration.compare_incremental_lexical_prompts(snapshot, endpoint=endpoint,
                            decision=decision, store=view, pin=pin,
                            prompts=('migration', 'presenceedge', 'overlaptoken', 'untrustedprobe'),
                            deadline=time.monotonic() + 30)
                        self.assertEqual(recalled['prompt_count'], 4)
                        integrated = migration.compare_shadow_queries(view, gen, project=case.project,
                            executing_root=case.plugin, queries=('migration',), prompts=('migration',),
                            deadline=time.monotonic() + 30)
                        self.assertEqual((integrated['query_count'], integrated['prompt_count']), (1, 1))
                        self.assertEqual(integrated['observation'], intent['observation'])
                        # One path is in both the v6 base and the sidecar. The
                        # hook ranks the sidecar card against the base cards; a
                        # candidate whose base ranking differs changes only that
                        # merged order, which the sidecar-only comparison cannot see.
                        v6_reader = importlib.import_module('laya_v6_reader')
                        merged = v6_reader.query_v6_snapshot(snapshot, endpoint=endpoint, prompt='Summary',
                            decision=decision, provider=None, deadline=time.monotonic() + 10)
                        origins = [row['generation'].startswith('preview-') for row in merged.rows]
                        self.assertEqual((origins.count(True), origins.count(False)), (1, 2))
                        self.assertIn(seed.changed_path, [row['path'] for row in merged.rows])
                        original_search = view.search
                        def reordered_base(pin_, fts_query, limit, **options):
                            rows = original_search(pin_, fts_query, limit, **options)
                            return rows[::-1] if options.get('_legacy_origin') == 'base' else rows
                        combined_prompts = ('Summary', 'heron', 'migration', 'overlaptoken', 'untrustedprobe')
                        self.assertEqual(migration.compare_combined_lexical_prompts(snapshot, endpoint=endpoint,
                            decision=decision, store=view, pin=pin, prompts=combined_prompts,
                            deadline=time.monotonic() + 30)['prompt_count'], len(combined_prompts))
                        with mock.patch.object(view, 'search', side_effect=reordered_base):
                            self.assertEqual(migration.compare_incremental_lexical_prompts(snapshot,
                                endpoint=endpoint, decision=decision, store=view, pin=pin,
                                prompts=('Summary',), deadline=time.monotonic() + 30)['prompt_count'], 1)
                            with self.assertRaisesRegex(Exception, 'migration_recall_parity_mismatch'):
                                migration.compare_combined_lexical_prompts(snapshot, endpoint=endpoint,
                                    decision=decision, store=view, pin=pin, prompts=('Summary',),
                                    deadline=time.monotonic() + 30)
                        # Candidate trust and locators are part of parity, not re-derived.
                        real_evidence = migration.legacy_card_evidence
                        for evidence in (lambda row, chosen: None,
                                         lambda row, chosen: {**real_evidence(row, chosen),
                                                              'locators': ['other.md#L1-L1']}):
                            with mock.patch.object(migration, 'legacy_card_evidence', side_effect=evidence):
                                with self.assertRaisesRegex(Exception, 'migration_recall_parity_mismatch'):
                                    migration.compare_combined_lexical_prompts(snapshot, endpoint=endpoint,
                                        decision=decision, store=view, pin=pin, prompts=('heron',),
                                        deadline=time.monotonic() + 30)
                        # The whole member-derived corpus, one v6 snapshot per page.
                        inventory, page_cursor = [], None
                        while True:
                            listed = migration.capture_effective(snapshot, endpoint=endpoint,
                                decision=decision, after=page_cursor, deadline=time.monotonic() + 10)
                            inventory.extend(row['path'] for row in listed['members'])
                            page_cursor = listed['next']
                            if page_cursor is None:
                                break
                        corpus = {'pages': 0, 'members': 0, 'prompts': 0, 'max_page_ms': 0.0}
                        corpus_started, cursor, digests = time.monotonic(), None, set()
                        with mock.patch.object(opens, 'inspect_warm_snapshot',
                                               wraps=opens.inspect_warm_snapshot) as corpus_snapshots:
                            while True:
                                page_started = time.monotonic()
                                page = migration.compare_recall_corpus(view, gen, project=case.project,
                                    executing_root=case.plugin, after=cursor, deadline=time.monotonic() + 30)
                                corpus['max_page_ms'] = max(corpus['max_page_ms'],
                                                            (time.monotonic() - page_started) * 1000)
                                self.assertLessEqual(page['members'], migration._PARITY_MEMBERS)
                                self.assertLessEqual(page['prompts'], migration._PARITY_PROMPTS)
                                self.assertEqual(page['observation'], intent['observation'])
                                digests.add(page['digest'])
                                for key in ('members', 'prompts'):
                                    corpus[key] += page[key]
                                corpus['pages'] += 1
                                cursor = page['next']
                                if cursor is None:
                                    break
                        self.assertEqual(corpus_snapshots.call_count, corpus['pages'])
                        self.assertEqual(corpus['members'], len(inventory))
                        self.assertEqual(len(digests), corpus['pages'])
                        self.assertGreater(corpus['prompts'], corpus['members'])
                        print('recall_corpus_pages %d members %d prompts %d total_ms %.1f max_page_ms %.1f' % (
                            corpus['pages'], corpus['members'], corpus['prompts'],
                            (time.monotonic() - corpus_started) * 1000, corpus['max_page_ms']), flush=True)
                        with self.assertRaisesRegex(Exception, 'migration_cursor_invalid'):
                            migration.compare_recall_corpus(view, gen, project=case.project,
                                executing_root=case.plugin, after={'after': inventory[0], 'observation': '0' * 64},
                                deadline=time.monotonic() + 30)
                        corpus_events = [json.loads(line) for line in
                                         (case.state / 'events.jsonl').read_text().splitlines()]
                        self.assertEqual(len([event for event in corpus_events if event.get('event') == 'migration.shadow'
                                              and event.get('stage') == 'compare_corpus'
                                              and event.get('outcome') == 'compared']), corpus['pages'])
                        corpus_text = (case.state / 'events.jsonl').read_text()
                        for derived in ('Otter ledger', 'Kestrel plan', 'untrusted 042', 'Summary'):
                            self.assertIn(derived, migration.recall_corpus_prompts(*{
                                'Otter ledger': (case.WIKI + '/concepts/otter-ledger.md', '', 'Otter ledger'),
                                'Kestrel plan': (case.WIKI + '/decisions/kestrel-plan.md', '', 'Kestrel plan'),
                                'untrusted 042': (seed.noise_paths[42], '', 'Noise'),
                                'Summary': (case.WIKI + '/x.md', '## Summary\n', 'X')}[derived]))
                            self.assertNotIn(derived, corpus_text)
                        with mock.patch.object(migration, 'compare_incremental_lexical_prompts',
                                side_effect=migration.IndexContractError('migration_recall_parity_mismatch')):
                            with self.assertRaisesRegex(Exception, 'migration_recall_parity_mismatch'):
                                migration.compare_shadow_queries(view, gen, project=case.project,
                                    executing_root=case.plugin, queries=('migration',),
                                    prompts=('privatepromptXYZ',), deadline=time.monotonic() + 30)
                        self.assertEqual(len(view._pins), 1)  # The caller's earlier pin remains live.
                        event_text = (case.state / 'events.jsonl').read_text()
                        self.assertNotIn('privatepromptXYZ', event_text)
                        events = [json.loads(line) for line in event_text.splitlines()]
                        self.assertEqual([event for event in events if event.get('event') == 'migration.shadow'
                                          and event.get('outcome') == 'error'][-1]['reason'],
                                         'migration_recall_parity_mismatch')
                        from laya_incremental_docs_query import query_incremental_snapshot
                        old_rows, old_titles, _, _, _ = query_incremental_snapshot(
                            'untrustedprobe', decision, {'vectorProfile': decision['vectorProfile']}, None,
                            base=snapshot.connections[0], side=side, deadline=time.monotonic() + 10)
                        self.assertTrue(old_rows)
                        self.assertIn(seed.changed_path, old_titles)
                        with mock.patch.object(view, 'search', return_value=[]):
                            with self.assertRaisesRegex(Exception, 'migration_recall_parity_mismatch'):
                                migration.compare_incremental_lexical_prompts(snapshot, endpoint=endpoint,
                                    decision=decision, store=view, pin=pin, prompts=('migration',),
                                    deadline=time.monotonic() + 30)
                        with mock.patch.object(importlib.import_module('laya_host_response'),
                                               '_verified_wiki_row', return_value=False):
                            with self.assertRaisesRegex(Exception, 'migration_lexical_probe_mismatch'):
                                migration.compare_lexical_queries(snapshot, endpoint=endpoint, decision=decision,
                                    store=view, pin=pin, queries=('presenceedge',), deadline=time.monotonic() + 30)
                        self.assertEqual(view.search(pin, 'overlaptoken', 100, _legacy_origin='sidecar'), [])
                        old_query = importlib.import_module('laya_v6_reader').query_v6_snapshot(
                            snapshot, endpoint=endpoint, prompt='overlaptoken', decision=decision,
                            provider=None, deadline=time.monotonic() + 10)
                        self.assertEqual(old_query.rows, [])
                        with self.assertRaisesRegex(Exception, 'migration_lexical_limit_unproved'):
                            migration.compare_lexical_queries(snapshot, endpoint=endpoint, decision=decision,
                                store=view, pin=pin, queries=('noiseonly',), deadline=time.monotonic() + 30)
                    finally:
                        view.release(pin)
                    ordinal = view._db.execute('SELECT ordinal FROM generations WHERE gen_id=?', (gen,)).fetchone()[0]
                    name = 'wiki_fts_side_' + str(ordinal)
                    with sqlite3.connect(view._path) as tamper:
                        tamper.execute('UPDATE ' + name + " SET body=body || ' synthetic-tamper' "
                                       'WHERE rowid=(SELECT min(rowid) FROM ' + name + ')')
                    with mock.patch.object(views.view_fts, 'assert_rows',
                                           side_effect=AssertionError('unverified virtual FTS read')):
                        with self.assertRaisesRegex(Exception, 'fts_storage_mismatch'):
                            view.pin_view(handle)
                    retained = importlib.import_module('laya_v6_reader').query_v6_snapshot(
                        snapshot, endpoint=endpoint, prompt='heron', decision=decision,
                        provider=None, deadline=time.monotonic() + 10)
                    self.assertTrue(retained.rows)

    def test_excluded_sidecar_does_not_restore_the_old_base(self):
        class ExcludedSidecar(seed_module.MoveSealChecks):
            def before_conversion(self):
                with sqlite3.connect(self.sidecar) as db:
                    rowid, self.excluded_path, title = db.execute(
                        'SELECT rowid,path,title FROM docs ORDER BY path LIMIT 1').fetchone()
                    body = 'Synthetic excluded newer version.\n'
                    digest = hashlib.sha256(body.encode()).hexdigest()
                    self.excluded_hash = digest
                    db.execute('UPDATE docs SET hash=?,body=? WHERE rowid=?', (digest, body, rowid))
                    db.execute('DELETE FROM docs_fts WHERE rowid=?', (rowid,))
                    db.execute('INSERT INTO docs_fts(rowid,path,title,body) VALUES(?,?,?,?)',
                               (rowid, self.excluded_path, title, body))
        seed = ExcludedSidecar('runTest')
        seed.setUp()
        self.addCleanup(seed.doCleanups)
        case = seed.case
        opens = importlib.import_module('laya_reader_open')
        migration = importlib.import_module('laya_shadow_migration')
        with seed.authority():
            decision = seed.reader.journal.select_route(str(case.project), action='recall')
            with seed.lease.resolve_endpoint(case.project, action='recall', host_root=case.plugin) as endpoint:
                with opens.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 10) as snapshot:
                    # The exclusion input is synthetic; real NormalWriter
                    # integration is tracked separately.
                    with mock.patch.object(migration, '_exclusions', return_value=(frozenset(), {seed.excluded_hash})):
                        page = migration.capture_effective(snapshot, endpoint=endpoint, decision=decision,
                            after=None, deadline=time.monotonic() + 10)
                    self.assertNotIn(seed.excluded_path, [row['path'] for row in page['members']])

    def test_missing_optional_sidecar_is_not_created(self):
        class BaseOnly(seed_module.MoveSealChecks):
            PUBLISH_SIDECAR = False
        seed = BaseOnly('runTest')
        seed.setUp()
        self.addCleanup(seed.doCleanups)
        case = seed.case
        opens = importlib.import_module('laya_reader_open')
        migration = importlib.import_module('laya_shadow_migration')
        self.assertFalse(seed.sidecar.exists())
        with seed.authority():
            decision = seed.reader.journal.select_route(str(case.project), action='recall')
            with seed.lease.resolve_endpoint(case.project, action='recall', host_root=case.plugin) as endpoint:
                with opens.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 10) as snapshot:
                    page = migration.capture_effective(snapshot, endpoint=endpoint, decision=decision,
                        after=None, deadline=time.monotonic() + 10)
                    self.assertEqual(len(page['members']), 3)
                    self.assertTrue(all(row['source'] == 'base' for row in page['members']))
        self.assertFalse(seed.sidecar.exists())

    def test_sqlite_scan_is_interrupted_when_step_deadline_expires(self):
        seed = seed_module.MoveSealChecks('runTest')
        seed.setUp()
        self.addCleanup(seed.doCleanups)
        case = seed.case
        opens = importlib.import_module('laya_reader_open')
        migration = importlib.import_module('laya_shadow_migration')
        with seed.authority():
            decision = seed.reader.journal.select_route(str(case.project), action='recall')
            with seed.lease.resolve_endpoint(case.project, action='recall', host_root=case.plugin) as endpoint:
                with opens.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 10) as snapshot:
                    connection_type = type(snapshot.connections[0])
                    actual = connection_type.execute
                    def slow_scan(db, statement, *args):
                        if statement.startswith('SELECT m.path,m.hash FROM membership'):
                            actual(db, 'WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL '
                                       'SELECT x+1 FROM n WHERE x<10000000) SELECT sum(x) FROM n').fetchone()
                        return actual(db, statement, *args)
                    with mock.patch.object(connection_type, 'execute', slow_scan):
                        start = time.monotonic()
                        with self.assertRaisesRegex(Exception, 'reader_snapshot_deadline'):
                            migration.capture_effective(snapshot, endpoint=endpoint, decision=decision,
                                after=None, deadline=time.monotonic() + .05)
                        self.assertLess(time.monotonic() - start, 1.0)

    def test_wiki_root_meta_pages_are_not_cards(self):
        migration = importlib.import_module('laya_shadow_migration')
        self.assertFalse(migration._wiki_card_path('project-wiki/index.md', ('project-wiki/',)))
        self.assertFalse(migration._wiki_card_path('project-wiki/SCHEMA.md', ('project-wiki/',)))
        self.assertTrue(migration._wiki_card_path('project-wiki/nested/index.md', ('project-wiki/',)))
        self.assertFalse(migration._wiki_card_path('project/notes.md', ('project-wiki/',)))

    def test_wiki_bytes_are_from_published_pin_not_changed_disk(self):
        class BaseOnly(seed_module.MoveSealChecks):
            PUBLISH_SIDECAR = False
        seed = BaseOnly('runTest')
        seed.setUp()
        self.addCleanup(seed.doCleanups)
        case = seed.case
        path, original = case.head()[1][0], None
        with sqlite3.connect(case.db) as db:
            original = db.execute('SELECT c.body FROM membership m JOIN content c ON c.hash=m.hash '
                'WHERE m.path=? LIMIT 1', (path,)).fetchone()[0]
        disk = case.auto / 'wiki' / path.split('/', 1)[1]
        disk.write_text('Synthetic later disk content, not published.\n', encoding='utf8')
        opens = importlib.import_module('laya_reader_open')
        migration = importlib.import_module('laya_shadow_migration')
        with seed.authority():
            decision = seed.reader.journal.select_route(str(case.project), action='recall')
            with seed.lease.resolve_endpoint(case.project, action='recall', host_root=case.plugin) as endpoint:
                with opens.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 10) as snapshot:
                    batch = migration.capture_wiki_bytes(snapshot, endpoint=endpoint, decision=decision,
                        after=None, deadline=time.monotonic() + 10)
                    matched = [row for row in batch['members'] if row['path'] == path]
                    self.assertEqual(len(matched), 1)
                    self.assertEqual(matched[0]['body'], original)
                    self.assertEqual(hashlib.sha256(original.encode()).hexdigest(), matched[0]['hash'])
                    self.assertLessEqual(sum(len(row['body'].encode()) for row in batch['members']), 8 * 1024 * 1024)
                    byte_cap = len(batch['members'][0]['body'].encode())
                    with mock.patch.object(migration, 'MAX_BATCH_BYTES', byte_cap):
                        first = migration.capture_wiki_bytes(snapshot, endpoint=endpoint, decision=decision,
                            after=None, deadline=time.monotonic() + 10)
                        self.assertEqual(len(first['members']), 1)
                        self.assertEqual(first['members'][0], batch['members'][0])
                        self.assertIsNotNone(first['next'])

    def test_import_preserves_published_bytes_in_private_candidate(self):
        class BaseOnly(seed_module.MoveSealChecks):
            PUBLISH_SIDECAR = False
        seed = BaseOnly('runTest')
        parent = seed_module.base.published.MoveCase
        original_publish = parent.publish_all
        def publish_with_span(case, roots):
            raw = (case.project / 'retained-source.md').read_bytes()
            span = {'path': 'retained-source.md', 'sourceSha256': hashlib.sha256(raw).hexdigest(),
                    'startLine': 1, 'endLine': 1, 'spanSha256': hashlib.sha256(raw).hexdigest()}
            path = case.auto / 'wiki/concepts/heron-route.md'
            body = path.read_text(encoding='utf8')
            body = body.replace('\n---\n', '\nsourceSpans:\n  - ' + json.dumps(span) + '\n---\n', 1)
            path.write_text(body, encoding='utf8')
            return original_publish(case, roots)
        with mock.patch.object(parent, 'publish_all', new=publish_with_span):
            seed.setUp()
        self.addCleanup(seed.doCleanups)
        case = seed.case
        subprocess.run(['git', '-c', 'init.defaultBranch=main', 'init', '-q', str(case.project)],
                       check=True, capture_output=True, timeout=10)
        subprocess.run(['git', '-C', str(case.project), '-c', 'commit.gpgsign=false',
                        '-c', 'core.hooksPath=' + os.devnull, '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty',
                        '-qm', 'fixture'], check=True, capture_output=True, timeout=10)
        time.sleep(1.1)  # Let freshly-created Git metadata leave its timestamp tick.
        checkout = importlib.import_module('laya_checkout_view')
        blobs = importlib.import_module('laya_blob_store')
        migration = importlib.import_module('laya_shadow_migration')
        repository = checkout.register_repository(str(case.project))
        worktree = checkout.register_worktree(str(case.project), repository)
        store = blobs.BlobStore.create(case.state / 'shadow-candidate.sqlite',
            project_root=str(case.project), repository=repository, worktree=worktree,
            scope_id=case.route['scopeId'])
        self.addCleanup(store.close)
        other = blobs.BlobStore.create(case.state / 'other-scope.sqlite',
            project_root=str(case.project), repository=repository, worktree=worktree,
            scope_id='a' * 32)
        self.addCleanup(other.close)
        cursor_store = blobs.BlobStore.create(case.state / 'cursor-candidate.sqlite',
            project_root=str(case.project), repository=repository, worktree=worktree,
            scope_id=case.route['scopeId'])
        self.addCleanup(cursor_store.close)
        changed_path = case.head()[1][0]
        (case.auto / 'wiki' / changed_path.split('/', 1)[1]).write_text(
            'Synthetic unpublished edit.\n', encoding='utf8')
        opens = importlib.import_module('laya_reader_open')
        with seed.authority():
            decision = seed.reader.journal.select_route(str(case.project), action='recall')
            with seed.lease.resolve_endpoint(case.project, action='recall', host_root=case.plugin) as endpoint:
                with opens.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 10) as snapshot:
                    page = migration.capture_wiki_bytes(snapshot, endpoint=endpoint, decision=decision,
                        after=None, deadline=time.monotonic() + 10)
                    empty_side = migration.capture_sidecar_fts_corpus(snapshot, endpoint=endpoint,
                        decision=decision, after=None, deadline=time.monotonic() + 10)
                    self.assertEqual(empty_side['members'], [])
                    self.assertIsNone(empty_side['next'])
                    with self.assertRaisesRegex(Exception, 'migration_endpoint_changed'):
                        migration.import_wiki_batch(other, worktree_id=worktree['worktreeId'],
                            snapshot=snapshot, endpoint=endpoint, decision=decision, after=None,
                            deadline=time.monotonic() + 10)
                    self.assertEqual(other.counts()['wiki_blobs'], 0)
                    before_mismatch = store.counts()
                    with self.assertRaisesRegex(Exception, 'migration_snapshot_changed'):
                        migration.import_wiki_batch(store, worktree_id=worktree['worktreeId'],
                            snapshot=snapshot, endpoint=endpoint, decision=decision, after=None,
                            deadline=time.monotonic() + 10, _expected_observation='0' * 64)
                    self.assertEqual(store.counts(), before_mismatch)
                    initial_timeout = store._db.execute('PRAGMA busy_timeout').fetchone()[0]
                    blocker = sqlite3.connect(case.state / 'shadow-candidate.sqlite', timeout=1)
                    try:
                        blocker.execute('BEGIN IMMEDIATE')
                        # The busy wait is bounded by the step deadline, not the
                        # store default: every timeout armed for the call fits 1s.
                        armed = busy_timeouts_armed(store._db)
                        with armed, self.assertRaisesRegex(Exception, 'candidate_busy'):
                            migration.import_wiki_batch(store, worktree_id=worktree['worktreeId'],
                                snapshot=snapshot, endpoint=endpoint, decision=decision, after=None,
                                deadline=time.monotonic() + 1)
                        self.assertGreater(initial_timeout, 1000)
                        self.assertGreaterEqual(len(armed.values), 2, armed.values)
                        self.assertTrue(all(value <= 1000 for value in armed.values[:-1]), armed.values)
                        self.assertEqual(armed.values[-1], initial_timeout)
                    finally:
                        blocker.rollback()
                        blocker.close()
                    self.assertEqual(store._db.execute('PRAGMA busy_timeout').fetchone()[0], initial_timeout)
                    with mock.patch.object(migration.journal.journal, 'select_route',
                                           return_value={**decision, 'state': 'blocked'}):
                        with self.assertRaisesRegex(Exception, 'reader_selection_changed'):
                            migration.import_wiki_batch(store, worktree_id=worktree['worktreeId'],
                                snapshot=snapshot, endpoint=endpoint, decision=decision, after=None,
                                deadline=time.monotonic() + 10)
                    self.assertEqual(store.counts()['wiki_blobs'], 0)
                    settings_path = case.auto / 'settings.json'
                    original_settings = settings_path.read_bytes()
                    original_capture = migration.capture_wiki_bytes
                    def change_settings(*args, **kwargs):
                        captured = original_capture(*args, **kwargs)
                        settings_path.write_text(json.dumps(dict(json.loads(original_settings), name='renamed')))
                        return captured
                    try:
                        with mock.patch.object(migration, 'capture_wiki_bytes', side_effect=change_settings):
                            with self.assertRaisesRegex(Exception, 'wiki_admission_changed'):
                                migration.import_wiki_batch(store, worktree_id=worktree['worktreeId'],
                                    snapshot=snapshot, endpoint=endpoint, decision=decision, after=None,
                                    deadline=time.monotonic() + 10)
                    finally:
                        settings_path.write_bytes(original_settings)
                    self.assertEqual(store.counts()['wiki_blobs'], 0)
                    actual_insert = blobs._insert
                    inserted = []
                    def fail_second_blob(db, table, row):
                        if table == 'wiki_blobs':
                            inserted.append(row['blob_sha'])
                            if len(inserted) == 2:
                                raise sqlite3.OperationalError('database or disk is full')
                        return actual_insert(db, table, row)
                    with mock.patch.object(blobs, '_insert', side_effect=fail_second_blob):
                        with self.assertRaisesRegex(Exception, 'candidate_storage_full'):
                            migration.import_wiki_batch(store, worktree_id=worktree['worktreeId'],
                                snapshot=snapshot, endpoint=endpoint, decision=decision, after=None,
                                deadline=time.monotonic() + 10)
                    self.assertEqual(store.counts()['wiki_blobs'], 0)
                    with mock.patch.object(checkout, '_git', wraps=checkout._git) as git_calls:
                        result = migration.import_wiki_batch(store, worktree_id=worktree['worktreeId'],
                            snapshot=snapshot, endpoint=endpoint, decision=decision, after=None,
                            deadline=time.monotonic() + 10)
                    print('synthetic_import_git_calls', git_calls.call_count, flush=True)
                    self.assertEqual(result['next'], None)
                    self.assertEqual(result['imported'], len(page['members']))
                    durations = []
                    with mock.patch.object(checkout, '_git', wraps=checkout._git) as hot_git:
                        for _ in range(10):
                            began = time.monotonic()
                            replay = migration.import_wiki_batch(store, worktree_id=worktree['worktreeId'],
                                snapshot=snapshot, endpoint=endpoint, decision=decision, after=None,
                                deadline=time.monotonic() + 10)
                            durations.append((time.monotonic() - began) * 1000)
                            self.assertEqual(replay['imported'], len(page['members']))
                    print('synthetic_hot_import_p95_ms', round(sorted(durations)[-1], 2),
                          'git_calls', hot_git.call_count, flush=True)
                    for row in page['members']:
                        self.assertEqual(store.document(row['hash'])['body'], row['body'])
                    scan = {'id': '1' * 32, 'observed_ns': time.time_ns()}
                    prior = store.counts()
                    def fail_source(db, table, row):
                        if table == 'attestation_sources':
                            raise sqlite3.OperationalError('database or disk is full')
                        return actual_insert(db, table, row)
                    with mock.patch.object(blobs, '_insert', side_effect=fail_source):
                        with self.assertRaisesRegex(Exception, 'candidate_storage_full'):
                            migration.import_wiki_batch(store, worktree_id=worktree['worktreeId'],
                                snapshot=snapshot, endpoint=endpoint, decision=decision, after=None,
                                deadline=time.monotonic() + 10, provenance_scan=scan)
                    self.assertEqual(store.counts(), prior)
                    with mock.patch.object(migration.journal.journal, 'select_route',
                                           return_value={**decision, 'state': 'blocked'}):
                        with self.assertRaisesRegex(Exception, 'reader_selection_changed'):
                            migration.import_wiki_batch(store, worktree_id=worktree['worktreeId'],
                                snapshot=snapshot, endpoint=endpoint, decision=decision, after=None,
                                deadline=time.monotonic() + 10, provenance_scan=scan)
                    self.assertEqual(store.counts(), prior)
                    staged = migration.import_wiki_batch(store, worktree_id=worktree['worktreeId'],
                        snapshot=snapshot, endpoint=endpoint, decision=decision, after=None,
                        deadline=time.monotonic() + 10, provenance_scan=scan)
                    self.assertEqual(len(staged['trusted']), len(page['members']))
                    for result in staged['trusted']:
                        record = store.attestation(result['trust_digest'])
                        self.assertEqual(record['verification_kind'], 'legacy_verified')
                        self.assertEqual(record['blob_sha'], result['hash'])
                        self.assertTrue(record['sources'])
                        for source in record['sources']:
                            fp = store.fingerprint(source['fingerprint_id'])
                            self.assertEqual(fp['binding_state'], 'legacy_unbound')
                            self.assertIsNone(fp['head_oid'])
                            self.assertEqual(fp['scan_id'], scan['id'])
                            self.assertEqual(fp['observed_ns'], scan['observed_ns'])
                    replay = migration.import_wiki_batch(store, worktree_id=worktree['worktreeId'],
                        snapshot=snapshot, endpoint=endpoint, decision=decision, after=None,
                        deadline=time.monotonic() + 10, provenance_scan=scan)
                    self.assertEqual(staged['trusted'], replay['trusted'])
                    with mock.patch.object(blobs, '_insert', side_effect=AssertionError('unexpected write')):
                        verified = migration.import_wiki_batch(store, worktree_id=worktree['worktreeId'],
                            snapshot=snapshot, endpoint=endpoint, decision=decision, after=None,
                            deadline=time.monotonic() + 10, provenance_scan=scan, verify_only=True)
                        self.assertEqual(staged['trusted'], verified['trusted'])
                        with self.assertRaisesRegex(Exception, 'migration_staged_missing'):
                            migration.import_wiki_batch(store, worktree_id=worktree['worktreeId'],
                                snapshot=snapshot, endpoint=endpoint, decision=decision, after=None,
                                deadline=time.monotonic() + 10,
                                provenance_scan={**scan, 'id': '3' * 32}, verify_only=True)
                    readonly = blobs.BlobStore._open(case.state / 'shadow-candidate.sqlite',
                        repository=repository, scope_id=case.route['scopeId'], reader_only=True)
                    try:
                        self.assertEqual(readonly._db.execute('PRAGMA query_only').fetchone()[0], 1)
                        verified = migration.import_wiki_batch(readonly, worktree_id=worktree['worktreeId'],
                            snapshot=snapshot, endpoint=endpoint, decision=decision, after=None,
                            deadline=time.monotonic() + 10, provenance_scan=scan, verify_only=True)
                        self.assertEqual(staged['trusted'], verified['trusted'])
                    finally:
                        readonly.close()
                    view_module = importlib.import_module('laya_view_store')
                    view = view_module.ViewStore.open(case.state / 'shadow-candidate.sqlite',
                        repository=repository, scope_id=case.route['scopeId'])
                    try:
                        handle = view.capture_view(worktree['worktreeId'])
                        retained = next(row for row in staged['trusted'] if row['path'] == changed_path)
                        manifest = {'scan_id': scan['id'], 'policy_digest': handle.policy_digest,
                            'revocation_epoch': 0, 'complete': True, 'sources': [], 'members': [{
                                'path': retained['path'].split('/', 1)[1], 'blob_sha': retained['hash'],
                                'display_title': 'Synthetic retained card', 'trust_digest': retained['trust_digest']}]}
                        with self.assertRaisesRegex(Exception, 'wiki_member_changed'):
                            view.prepare_generation(handle, manifest, expected_head=view.head(handle))
                        legacy = {'snapshot': snapshot, 'endpoint': endpoint,
                                  'decision': decision, 'provenance_scan': scan}
                        with self.assertRaisesRegex(Exception, 'migration_manifest_mismatch'):
                            view.prepare_generation(handle, manifest, expected_head=view.head(handle),
                                                    legacy_snapshot=legacy)
                        titles = importlib.import_module('laya_index_vector').qmd_markdown_title
                        source_by_path = {row['path']: row for row in page['members']}
                        manifest['members'] = [dict(path=row['path'].split('/', 1)[1],
                            blob_sha=row['hash'], trust_digest=row['trust_digest'],
                            display_title=titles(source_by_path[row['path']]['body'], row['path']))
                            for row in staged['trusted']]
                        altered = {**manifest, 'members': [dict(item) for item in manifest['members']]}
                        altered['members'][0]['display_title'] = 'forged synthetic title'
                        with self.assertRaisesRegex(Exception, 'migration_manifest_mismatch'):
                            view.prepare_generation(handle, altered, expected_head=view.head(handle),
                                                    legacy_snapshot=legacy)
                        with self.assertRaisesRegex(Exception, 'migration_manifest_mismatch'):
                            view.prepare_generation(handle, manifest, expected_head=view.head(handle),
                                legacy_snapshot={**legacy, 'provenance_scan': {**scan, 'id': '4' * 32}})
                        base_head = view.head(handle)
                        generation = view.prepare_generation(handle, manifest, expected_head=base_head,
                                                             legacy_snapshot=legacy)
                        stage_row = view._db.execute('SELECT s.stage_json FROM generation_slots s '
                            'JOIN generations g ON g.ordinal=s.ordinal WHERE g.gen_id=?', (generation,)).fetchone()
                        first_corpus = json.loads(stage_row['stage_json'])['legacy']['corpus'][0]
                        staged_value = json.loads(stage_row['stage_json'])
                        with view._session(), mock.patch.object(view, '_blob', wraps=view._blob) as verified_blob:
                            view._derive(handle, staged_value['manifest'], staged_value['skip_paths'],
                                         legacy_paths=staged_value['legacy']['paths'])
                            unique_blobs = {member['blob_sha'] for member in staged_value['manifest']['members']}
                            self.assertEqual(verified_blob.call_count, len(unique_blobs))
                        blob_module = importlib.import_module('laya_blob_store')
                        with view._session(), mock.patch.object(blob_module, '_worktree_record',
                                wraps=blob_module._worktree_record) as worktree_proof:
                            view._derive(handle, staged_value['manifest'], staged_value['skip_paths'],
                                         legacy_paths=staged_value['legacy']['paths'])
                            self.assertEqual(worktree_proof.call_count, 1)
                        with view._session(), mock.patch.object(view_module.checkout, '_capture_shape',
                                wraps=view_module.checkout._capture_shape) as capture_proof:
                            view._derive(handle, staged_value['manifest'], staged_value['skip_paths'],
                                         legacy_paths=staged_value['legacy']['paths'])
                            self.assertEqual(capture_proof.call_count, 1)
                        reads=[]
                        def count_blob_read(sql):
                            lower=sql.lower()
                            if lower.startswith('select ') and ('wiki_blobs' in lower or 'blob_admissions' in lower):
                                reads.append(True)
                        with view._session():
                            view._db.set_trace_callback(count_blob_read)
                            try:
                                view._derive(handle, staged_value['manifest'], staged_value['skip_paths'],
                                             legacy_paths=staged_value['legacy']['paths'])
                            finally:
                                view._db.set_trace_callback(None)
                        self.assertEqual(len(reads),len(unique_blobs))
                        with view._session(), mock.patch.object(view, '_blob', wraps=view._blob) as verified_blob:
                            with self.assertRaisesRegex(Exception, 'fts_deadline_exceeded'):
                                view._derive(handle, staged_value['manifest'], staged_value['skip_paths'],
                                             legacy_paths=staged_value['legacy']['paths'], deadline=0)
                            self.assertEqual(verified_blob.call_count, 0)
                        entry = view._check(handle)
                        saved = view._observe(entry)
                        with view._session() as db, mock.patch.object(view, '_blob', wraps=view._blob) as verified_blob:
                            generation_row = db.execute('SELECT * FROM generations WHERE gen_id=?', (generation,)).fetchone()
                            view._verify_generation(db, handle, entry, saved, generation_row,
                                                    full_vectors=True)
                            all_blobs = unique_blobs | {row['hash'] for row in staged_value['legacy']['corpus']}
                            self.assertEqual(verified_blob.call_count, len(all_blobs))
                        with view._session() as db, \
                             mock.patch.object(view_module.view_fts, 'storage_digest', return_value='0'*64), \
                             mock.patch.object(view_module.view_fts, 'assert_rows',
                                               side_effect=AssertionError('unverified virtual FTS read')):
                            generation_row = db.execute('SELECT * FROM generations WHERE gen_id=?', (generation,)).fetchone()
                            with self.assertRaisesRegex(Exception, 'fts_storage_mismatch'):
                                view._verify_generation(db, handle, entry, saved, generation_row,
                                                        full_vectors=True)
                        repeated_corpus = {'corpus': [first_corpus,
                            {**first_corpus, 'path': first_corpus['path'] + '-copy'}]}
                        with mock.patch.object(view, '_blob', wraps=view._blob) as blob_proof:
                            self.assertEqual(len(view._legacy_fts_rows(repeated_corpus)), 2)
                            self.assertEqual(blob_proof.call_count, 1)
                        with mock.patch.object(view, '_blob', wraps=view._blob) as blob_proof:
                            with self.assertRaisesRegex(Exception, 'fts_deadline_exceeded'):
                                view._legacy_fts_rows(repeated_corpus, deadline=0)
                            self.assertEqual(blob_proof.call_count, 0)
                        with self.assertRaisesRegex(Exception, 'migration_publish_proof_required'):
                            view.publish_generation(handle, generation, expected_head=base_head)
                        with self.assertRaisesRegex(Exception, 'migration_snapshot_changed'):
                            view.publish_generation(handle, generation, expected_head=base_head,
                                legacy_snapshot={**legacy, 'provenance_scan': {
                                    **scan, 'observed_ns': scan['observed_ns'] + 1}})
                        self.assertIsNone(view.head(handle)['gen_id'])
                        import fcntl
                        lock_path = seed.reader._lock_dir(seed.install, create=False) / 'publish-cut.lock'
                        lock_fd = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW)
                        try:
                            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            with self.assertRaisesRegex(Exception, 'reader_publish_busy'):
                                view.publish_generation(handle, generation, expected_head=base_head,
                                                        legacy_snapshot=legacy)
                            self.assertIsNone(view.head(handle)['gen_id'])
                        finally:
                            os.close(lock_fd)
                        previous_timeout = view._db.execute('PRAGMA busy_timeout').fetchone()[0]
                        blocker = sqlite3.connect(case.state / 'shadow-candidate.sqlite', timeout=1)
                        try:
                            blocker.execute('BEGIN IMMEDIATE')
                            armed = busy_timeouts_armed(view._db)
                            with armed, self.assertRaisesRegex(Exception, 'candidate_busy|reader_snapshot_deadline') as busy:
                                view.publish_generation(handle, generation, expected_head=base_head,
                                                        legacy_snapshot=legacy)
                            # A busy candidate waits within the 1s cut budget (below
                            # the store default), and the default is restored.
                            self.assertGreater(previous_timeout, 1000)
                            if 'candidate_busy' in str(busy.exception):
                                self.assertGreaterEqual(len(armed.values), 2, armed.values)
                            self.assertTrue(all(value <= 1000 for value in armed.values[:-1]), armed.values)
                            if armed.values:
                                self.assertEqual(armed.values[-1], previous_timeout)
                        finally:
                            blocker.rollback()
                            blocker.close()
                        self.assertEqual(view._db.execute('PRAGMA busy_timeout').fetchone()[0], previous_timeout)
                        self.assertIsNone(view.head(handle)['gen_id'])
                        journal_reader = importlib.import_module('laya_journal_reader')
                        with mock.patch.object(migration, 'verify_generation_members',
                                               side_effect=AssertionError('repeated mapping')), \
                             mock.patch.object(migration, 'stage_base_fts_corpus',
                                               side_effect=AssertionError('repeated corpus')), \
                             mock.patch.object(journal_reader, '_published_snapshot',
                                               wraps=journal_reader._published_snapshot) as old_proof, \
                             mock.patch.object(view_module.view_fts, 'storage_digest',
                                               wraps=view_module.view_fts.storage_digest) as fts_proof, \
                             mock.patch.object(view, '_verify_generation',
                                               wraps=view._verify_generation) as candidate_proof:
                            view.publish_generation(handle, generation, expected_head=base_head,
                                                    legacy_snapshot=legacy)
                            self.assertGreater(old_proof.call_count, 0)
                            self.assertGreater(candidate_proof.call_count, 0)
                            self.assertEqual(fts_proof.call_count, 1)
                        pin = view.pin_view(handle)
                        try:
                            query = changed_path.rsplit('/', 1)[-1].split('-', 1)[0]
                            found = view.search(pin, query, 10)
                            self.assertTrue(any(row['path'] == changed_path.split('/', 1)[1] for row in found))
                            self.assertTrue(all('Synthetic unpublished edit.' not in row['body'] for row in found))
                            (case.project / 'retained-source.md').unlink()
                            with mock.patch.object(importlib.import_module('wiki_freshness'), 'snapshot_bytes',
                                                   side_effect=AssertionError('raw source read')):
                                old_result = importlib.import_module('laya_v6_reader').query_v6_snapshot(
                                    snapshot, endpoint=endpoint, prompt='heron', decision=decision,
                                    provider=None, deadline=time.monotonic() + 10)
                                new_results = view.search(pin, 'heron', 10)
                            old_path = case.WIKI + '/concepts/heron-route.md'
                            old_locations = old_result.source_locations[old_path]
                            self.assertTrue(old_locations)
                            new_heron = next(row for row in new_results
                                             if row['path'] == 'concepts/heron-route.md')
                            self.assertEqual(new_heron['source_locations'], list(old_locations))
                            index = importlib.import_module('laya_index_a')
                            base_db = snapshot.connections[0]
                            ordinal = base_db.execute('SELECT ord FROM gen_index WHERE gen=?',
                                                      (snapshot.published[0],)).fetchone()[0]
                            prefix_token = case.WIKI.split('-', 1)[0]
                            for term in ('heron', 'otter', 'synthetic', 'filepath:"' + prefix_token + '"',
                                         'heron OR otter'):
                                old = index._search_pinned_fts(base_db, decision['scopeId'],
                                    snapshot.published[0], ordinal, (term,), 10,
                                    path_prefixes=(case.WIKI + '/',), deadline=time.monotonic() + 10)[0]
                                new = view.search(pin, term, 10)
                                self.assertEqual([(row['path'].split('/', 1)[1], row['hash']) for row in old],
                                    [(row['path'], row['blob_sha']) for row in new], term)
                                for old_row, new_row in zip(old, new):
                                    self.assertAlmostEqual(old_row['score'], new_row['score'], places=12, msg=term)
                            migration.compare_lexical_queries(snapshot, endpoint=endpoint, decision=decision,
                                store=view, pin=pin, queries=('heron', 'otter', 'heron OR otter'),
                                deadline=time.monotonic() + 10)
                            actual_search = view.search
                            def wrong_locator(*args, **kwargs):
                                rows = actual_search(*args, **kwargs)
                                if rows:
                                    rows[0] = {**rows[0], 'source_locations': ['synthetic/wrong.md#L1-L1']}
                                return rows
                            with mock.patch.object(view, 'search', side_effect=wrong_locator):
                                with self.assertRaisesRegex(Exception, 'migration_lexical_parity_mismatch'):
                                    migration.compare_lexical_queries(snapshot, endpoint=endpoint, decision=decision,
                                        store=view, pin=pin, queries=('heron',), deadline=time.monotonic() + 10)
                        finally:
                            view.release(pin)
                    finally:
                        view.close()
            view = view_module.ViewStore.open(case.state / 'shadow-candidate.sqlite',
                repository=repository, scope_id=case.route['scopeId'])
            try:
                handle = view.capture_view(worktree['worktreeId'])
                with self.assertRaisesRegex(Exception, 'reader_snapshot_unpinned|reader_endpoint'):
                    view.prepare_generation(handle, manifest, expected_head=view.head(handle),
                                            legacy_snapshot=legacy)
            finally:
                view.close()
            with mock.patch.object(migration, 'MAX_BATCH', 2):
                ledger = importlib.import_module('laya_ledger_common')
                started = migration.begin_migration(cursor_store, worktree_id=worktree['worktreeId'],
                    project=case.project, executing_root=case.plugin, deadline=time.monotonic() + 30)
                self.assertEqual(started['phase'], 'copying')
                event_path = case.state / 'events.jsonl'
                def migration_events():
                    return [json.loads(line) for line in event_path.read_text().splitlines()
                            if json.loads(line).get('event') == 'migration.shadow']
                self.assertEqual(migration_events()[-1]['outcome'], 'started')
                with self.assertRaisesRegex(Exception, 'migration_copy_incomplete'):
                    migration.advance_verification(cursor_store, project=case.project,
                        executing_root=case.plugin, deadline=time.monotonic() + 30)
                self.assertEqual(migration_events()[-1]['reason'], 'migration_copy_incomplete')
                secret = 'synthetic-private-prompt-and-document'
                with mock.patch.object(migration, 'import_wiki_batch', side_effect=RuntimeError(secret)):
                    with self.assertRaisesRegex(RuntimeError, secret):
                        migration.advance_migration(cursor_store, project=case.project,
                            executing_root=case.plugin, deadline=time.monotonic() + 30)
                failed = migration_events()[-1]
                self.assertEqual((failed['outcome'], failed['reason'], failed['stage']),
                                 ('error', 'unexpected', 'copy'))
                self.assertEqual(failed['migration'], started['migration_id'])
                self.assertNotIn(secret, event_path.read_text())
                with mock.patch.object(migration, 'import_wiki_batch',
                        side_effect=migration.IndexContractError('reader_selection_changed')):
                    with self.assertRaisesRegex(Exception, 'reader_selection_changed'):
                        migration.advance_migration(cursor_store, project=case.project,
                            executing_root=case.plugin, deadline=time.monotonic() + 30)
                self.assertEqual(migration_events()[-1]['reason'], 'reader_selection_changed')
                with mock.patch.object(migration, 'import_wiki_batch',
                                       side_effect=OSError(errno.ENOSPC, secret)):
                    with self.assertRaises(OSError):
                        migration.advance_migration(cursor_store, project=case.project,
                            executing_root=case.plugin, deadline=time.monotonic() + 30)
                self.assertEqual(migration_events()[-1]['reason'], 'storage_full')
                self.assertNotIn(secret, event_path.read_text())
                original_insert = blobs._insert
                def sqlite_full(db, table, row):
                    if table == 'wiki_blobs':
                        raise sqlite3.OperationalError('database or disk is full')
                    return original_insert(db, table, row)
                with mock.patch.object(blobs, '_insert', side_effect=sqlite_full):
                    with self.assertRaisesRegex(Exception, 'candidate_storage_full'):
                        migration.advance_migration(cursor_store, project=case.project,
                            executing_root=case.plugin, deadline=time.monotonic() + 30)
                self.assertEqual(migration_events()[-1]['reason'], 'candidate_storage_full')
                self.assertEqual(cursor_store.counts()['wiki_blobs'], 0)
                intent_path = case.state / 'shadow-migration-intent.json'
                before_intent = intent_path.read_bytes()
                original_snapshot = opens.inspect_warm_snapshot
                original_capture = migration.capture_effective
                @contextmanager
                def fail_snapshot_exit(*args, **kwargs):
                    with original_snapshot(*args, **kwargs) as observed_snapshot:
                        yield observed_snapshot
                        raise RuntimeError('synthetic snapshot postcheck failure')
                def changed_observation(*args, **kwargs):
                    page = original_capture(*args, **kwargs)
                    return {**page, 'observation': 'a' * 64}
                with mock.patch.object(opens, 'inspect_warm_snapshot', fail_snapshot_exit), \
                     mock.patch.object(migration, 'capture_effective', changed_observation):
                    with self.assertRaisesRegex(RuntimeError, 'synthetic snapshot postcheck failure'):
                        migration.advance_migration(cursor_store, project=case.project,
                            executing_root=case.plugin, deadline=time.monotonic() + 30)
                self.assertEqual(intent_path.read_bytes(), before_intent)
                self.assertEqual(migration_events()[-1]['stage'], 'snapshot_postcheck')
                payload_path = case.home / 'synthetic-shadow-crash.json'
                payload_path.write_text(json.dumps({'home': str(case.home),
                    'runtime': os.environ['MEMVANE_TEST_RUNTIME_ROOT'], 'plugin': str(case.plugin),
                    'proof': case.proof, 'candidate': str(case.state / 'cursor-candidate.sqlite'),
                    'repository': repository, 'worktree': worktree, 'scope': case.route['scopeId'],
                    'project': str(case.project), 'batch_limit': 2}), encoding='utf8')
                child = subprocess.run([sys.executable, '-B', str(HERE / 'laya-shadow-crash-child.py'),
                    str(payload_path)], text=True, capture_output=True, timeout=40)
                self.assertEqual(child.returncode, -9, child.stdout + child.stderr)
                self.assertIn('blob-commit-before-intent', child.stdout)
                interrupted = [event for event in migration_events()
                               if event.get('migration') == started['migration_id']]
                begun = {event['attempt'] for event in interrupted if event['outcome'] == 'attempt'}
                ended = {event['attempt'] for event in interrupted if event['outcome'] != 'attempt'}
                self.assertTrue(begun - ended, 'killed copy retains an unmatched attempt event')
                self.assertEqual(intent_path.read_bytes(), before_intent)
                self.assertEqual(cursor_store.counts()['wiki_blobs'], 2)
                self.assertEqual(cursor_store.counts()['card_attestations'], 2)
                before_replay = cursor_store.counts()
                with mock.patch.object(opens, 'inspect_warm_snapshot', fail_snapshot_exit):
                    with self.assertRaisesRegex(RuntimeError, 'synthetic snapshot postcheck failure'):
                        migration.advance_migration(cursor_store, project=case.project,
                            executing_root=case.plugin, deadline=time.monotonic() + 30)
                self.assertEqual(intent_path.read_bytes(), before_intent)
                self.assertEqual(cursor_store.counts(), before_replay)
                with seed.lease.resolve_endpoint(case.project, action='recall', host_root=case.plugin) as endpoint:
                    with opens.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 10) as snapshot:
                        readonly = blobs.BlobStore._open(case.state / 'cursor-candidate.sqlite',
                            repository=repository, scope_id=case.route['scopeId'], reader_only=True)
                        try:
                            recovered = migration.import_wiki_batch(readonly, worktree_id=worktree['worktreeId'],
                                snapshot=snapshot, endpoint=endpoint, decision=decision, after=None,
                                deadline=time.monotonic() + 10, provenance_scan=started['provenance_scan'],
                                verify_only=True)
                            self.assertEqual(len(recovered['trusted']), 2)
                            self.assertIsNotNone(recovered['next'])
                        finally:
                            readonly.close()
                        selected = importlib.import_module('laya_v6_reader').query_v6_snapshot(
                            snapshot, endpoint=endpoint, prompt='otter', decision=decision,
                            provider=None, deadline=time.monotonic() + 10)
                        self.assertTrue(selected.rows)
                with mock.patch.object(migration, 'capture_effective', wraps=migration.capture_effective) as captured:
                    first = migration.advance_migration(cursor_store, project=case.project,
                        executing_root=case.plugin, deadline=time.monotonic() + 30)
                self.assertEqual(captured.call_count, 1, 'one proved membership capture per unchanged copy step')
                self.assertEqual(first['phase'], 'copying')
                self.assertIsNotNone(first['cursor'])
                self.assertEqual(cursor_store.counts()['wiki_blobs'], 2)
                self.assertEqual(cursor_store.counts(), before_replay)
                done = migration.advance_migration(cursor_store, project=case.project,
                    executing_root=case.plugin, deadline=time.monotonic() + 30)
                self.assertEqual(done['phase'], 'copied')
                events = migration_events()
                related = [event for event in events if event.get('migration') == started['migration_id']]
                self.assertTrue({'started', 'error', 'progress', 'copied'} <= {event['outcome'] for event in related})
                attempts = [event['attempt'] for event in related if event['outcome'] != 'attempt']
                beginnings = {event['attempt'] for event in related if event['outcome'] == 'attempt'}
                self.assertEqual(len(attempts), len(set(attempts)))
                self.assertTrue(set(attempts) <= beginnings)
                with mock.patch.object(importlib.import_module('event_log'), 'emit',
                                       side_effect=OSError('synthetic event log unavailable')):
                    again = migration.advance_migration(cursor_store, project=case.project,
                        executing_root=case.plugin, deadline=time.monotonic() + 30)
                self.assertEqual(again, done)
                self.assertEqual(cursor_store.counts()['wiki_blobs'], len(page['members']))
                self.assertEqual(cursor_store.counts()['card_attestations'], len(page['members']))
        self.assertTrue(store.verify())
        store.close()
        reopened = blobs.BlobStore.open(case.state / 'shadow-candidate.sqlite',
            repository=repository, scope_id=case.route['scopeId'])
        self.addCleanup(reopened.close)
        self.assertTrue(reopened.verify())
        self.assertNotIn('Synthetic unpublished edit.', reopened.document(page['members'][0]['hash'])['body'])
        with sqlite3.connect(case.state / 'shadow-candidate.sqlite') as db:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertFalse(any(name.startswith(('raw_', 'source_fts')) for name in names))

    def test_linked_worktree_cannot_be_initial_migration_owner(self):
        class BaseOnly(seed_module.MoveSealChecks):
            PUBLISH_SIDECAR = False
        seed = BaseOnly('runTest')
        seed.setUp()
        self.addCleanup(seed.doCleanups)
        case = seed.case
        env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                   GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0')
        def git(*args):
            return subprocess.run(['git', '-c', 'init.defaultBranch=main', '-c', 'commit.gpgsign=false',
                '-c', 'core.hooksPath=' + os.devnull, '-c', 'user.name=Fixture',
                '-c', 'user.email=fixture@example.invalid', *args], cwd=case.project, env=env,
                check=True, capture_output=True, timeout=10)
        git('init', '-q')
        git('commit', '--allow-empty', '-qm', 'fixture')
        linked = case.home / 'linked-project'
        git('worktree', 'add', '-q', '-b', 'fixture-linked', str(linked))
        shutil.copytree(case.auto, linked / '.auto-context')
        checkout = importlib.import_module('laya_checkout_view')
        blobs = importlib.import_module('laya_blob_store')
        migration = importlib.import_module('laya_shadow_migration')
        repository = checkout.register_repository(str(case.project))
        worktree = checkout.register_worktree(str(linked), repository)
        store = blobs.BlobStore.create(case.state / 'linked-candidate.sqlite',
            project_root=str(linked), repository=repository, worktree=worktree,
            scope_id=case.route['scopeId'])
        self.addCleanup(store.close)
        opens = importlib.import_module('laya_reader_open')
        with seed.authority():
            decision = seed.reader.journal.select_route(str(case.project), action='recall')
            with seed.lease.resolve_endpoint(case.project, action='recall', host_root=case.plugin) as endpoint:
                with opens.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 10) as snapshot:
                    with self.assertRaisesRegex(Exception, 'migration_main_checkout_required'):
                        migration.import_wiki_batch(store, worktree_id=worktree['worktreeId'],
                            snapshot=snapshot, endpoint=endpoint, decision=decision, after=None,
                            deadline=time.monotonic() + 10)
        self.assertEqual(store.counts()['wiki_blobs'], 0)

    def test_unissued_snapshot_is_refused(self):
        migration = importlib.import_module('laya_shadow_migration')
        with self.assertRaisesRegex(Exception, 'reader_snapshot_unpinned'):
            migration.capture_effective(None, endpoint=None, decision={}, after=None,
                                        deadline=time.monotonic() + 10)


if __name__ == '__main__':
    unittest.main()

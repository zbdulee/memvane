"""Synthetic v5 sidecar update during inactive shadow copy."""
import importlib
import importlib.util
import json
from contextlib import ExitStack, closing, contextmanager
import os
import select
import sqlite3
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / 'core'))
spec = importlib.util.spec_from_file_location('shadow_sidecar_seed', HERE / 'laya-reader-sidecar-write-checks.py')
sidecar_seed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sidecar_seed)


class CatchupChecks(unittest.TestCase):
    def test_writer_commit_during_pinned_copy_is_recaptured(self):
        self._check_writer_during_copy(sidecar=False)

    def test_sidecar_commit_during_pinned_copy_is_recaptured(self):
        self._check_writer_during_copy(sidecar=True)

    def test_writer_commit_after_prepare_is_refused(self):
        self._check_writer_during_copy(sidecar=False, final_race=True)

    def test_writer_commit_after_comparison_is_refused(self):
        self._check_writer_during_copy(sidecar=False, compare_race=True)

    def test_missing_base_revision_forces_full_recopy(self):
        self._check_writer_during_copy(sidecar=False, compare_race=True, missing_receipt=True)

    def _check_writer_during_copy(self, *, sidecar, final_race=False, compare_race=False,
                                  missing_receipt=False):
        normal_spec = importlib.util.spec_from_file_location(
            'shadow_overlap_normal_seed', HERE / 'laya-normal-writer-connections-checks.py')
        normal_module = importlib.util.module_from_spec(normal_spec)
        normal_spec.loader.exec_module(normal_module)
        normal = normal_module.NormalWriterConnectionChecks('runTest')
        if sidecar:
            seed_module = normal_module
            for _ in range(6):
                seed_module = seed_module.existing
            def remove_completed_row(seed):
                with sqlite3.connect(seed.sidecar) as db:
                    seed.overlap_sidecar_row = db.execute(
                        'SELECT path,hash,title,body,profile_id FROM docs ORDER BY path LIMIT 1').fetchone()
                    path = seed.overlap_sidecar_row[0]
                    db.execute('DELETE FROM docs_fts WHERE path=?', (path,))
                    db.execute('DELETE FROM docs WHERE path=?', (path,))
            with mock.patch.object(seed_module.MoveSealChecks, 'before_conversion',
                                   new=remove_completed_row, create=True):
                normal.setUp()
        else:
            normal.setUp()
        self.addCleanup(normal.doCleanups)
        seed, case = normal.seed, normal.case
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
        repository = checkout.register_repository(str(case.project))
        worktree = checkout.register_worktree(str(case.project), repository)
        store = blobs.BlobStore.create(case.state / 'overlap-shadow.sqlite',
            project_root=str(case.project), repository=repository, worktree=worktree,
            scope_id=case.route['scopeId'])
        self.addCleanup(store.close)
        with seed.authority(), ExitStack() as owners, mock.patch.object(migration, 'MAX_BATCH', 1):
            started = migration.begin_migration(store, worktree_id=worktree['worktreeId'],
                project=case.project, executing_root=case.plugin, deadline=time.monotonic() + 30)
            if not sidecar:
                intent_path = case.state / 'shadow-migration-intent.json'
                before_intent = intent_path.read_bytes()
                actual_capture = migration.capture_effective
                calls = []
                def delay_after_first_capture(*args, **kwargs):
                    result = actual_capture(*args, **kwargs)
                    calls.append(True)
                    if len(calls) == 1:
                        time.sleep(1.05)
                    return result
                with mock.patch.object(migration, '_STEP_SECONDS', 1.0), \
                     mock.patch.object(migration, 'capture_effective', side_effect=delay_after_first_capture):
                    with self.assertRaisesRegex(Exception, 'reader_snapshot_deadline'):
                        migration.advance_migration(store, project=case.project,
                            executing_root=case.plugin, deadline=time.monotonic() + 30)
                self.assertEqual(intent_path.read_bytes(), before_intent)
            writer, _ = normal.admission(owners)
            if sidecar:
                runtime_type = importlib.import_module('laya_index_runtime').LayaIndexRuntime
                runtime = owners.enter_context(runtime_type(
                    source_root={case.WIKI: str(case.auto / 'wiki')}, scope=case.route['scopeId'],
                    db_path=str(case.db), queue_path=str(normal.queue),
                    db_incarnation=case.route['dbIncarnation'], catalog_path=str(case.catalog),
                    binding_store_path=str(case.store), wiki_only=True,
                    connection_windows={role: normal.component(writer, role)
                                        for role in ('base', 'queue', 'catalog', 'binding')}))
                component = normal.component(writer, 'sidecar')
                identity = importlib.import_module('laya_initial_build').read(case.state)['identity']
                catalog = runtime.catalog.require_complete()
            else:
                runtime = owners.enter_context(normal.runtime(writer))
            normal.N.mark_new_write(writer)
            payload = case.home / 'synthetic-shadow-overlap.json'
            payload.write_text(json.dumps({'home': str(case.home),
                'runtime': os.environ['MEMVANE_TEST_RUNTIME_ROOT'], 'plugin': str(case.plugin),
                'proof': case.proof, 'candidate': str(case.state / 'overlap-shadow.sqlite'),
                'repository': repository, 'worktree': worktree, 'scope': case.route['scopeId'],
                'project': str(case.project), 'batch_limit': 1, 'mode': 'writer-overlap'}), encoding='utf8')
            child = subprocess.Popen([sys.executable, '-B', str(HERE / 'laya-shadow-crash-child.py'),
                str(payload)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1)
            try:
                ready, _, _ = select.select([child.stdout], [], [], 20)
                self.assertTrue(ready, 'copy did not reach the pinned snapshot checkpoint')
                self.assertEqual(child.stdout.readline().strip(), 'snapshot-pinned')
                if sidecar:
                    incremental = importlib.import_module('laya_incremental_docs')
                    with self.assertRaisesRegex(Exception, 'reader_physical_busy'):
                        incremental.publish_document(runtime, identity, catalog,
                            *seed.overlap_sidecar_row, require_verified=False, coordination=component)
                    with seed.ro(seed.sidecar) as db:
                        self.assertIsNone(db.execute('SELECT 1 FROM docs WHERE path=?',
                                                     (seed.overlap_sidecar_row[0],)).fetchone())
                    output, errors = child.communicate(input='continue\n', timeout=35)
                    if child.returncode == 0:
                        copied = json.loads(output.strip())
                    else:
                        self.assertIn('reader_snapshot_deadline', errors)
                        copied = None
                    publication = incremental.publish_document(runtime, identity, catalog,
                        *seed.overlap_sidecar_row, require_verified=False, coordination=component)
                    self.assertEqual(publication['status'], 'published')
                else:
                    self.assertEqual(runtime.index.exclude(writer.scope_id,
                        path=case.WIKI + '/concepts/otter-ledger.md'), 'excluded')
                readers = importlib.import_module('laya_reader_open')
                with seed.lease.resolve_endpoint(case.project, action='recall', host_root=case.plugin) as endpoint:
                    decision = seed.reader.journal.select_route(str(case.project), action='recall')
                    with readers.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 10) as pinned:
                        live = importlib.import_module('laya_v6_reader').query_v6_snapshot(
                            pinned, endpoint=endpoint, prompt='heron', decision=decision,
                            provider=None, deadline=time.monotonic() + 10)
                        self.assertTrue(live.rows)
                        if not sidecar:
                            self.assertNotIn(case.WIKI + '/concepts/otter-ledger.md',
                                             [item['path'] for item in live.rows])
                if not sidecar:
                    output, errors = child.communicate(input='continue\n', timeout=35)
                    if child.returncode == 0:
                        copied = json.loads(output.strip())
                    else:
                        self.assertIn('reader_snapshot_deadline', errors)
                        copied = None
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=5)
            # The writer admission has a real 20-second lifetime. Release it
            # after the overlap and obtain a fresh one for each later race.
            owners.close()
            def exclude_with_fresh_writer(path):
                normal.plan = normal.P.capture_writer_plan(case.project, executing_root=case.plugin)
                with ExitStack() as fresh_owners:
                    fresh_writer, _ = normal.admission(fresh_owners)
                    with normal.runtime(fresh_writer) as fresh_runtime:
                        normal.N.mark_new_write(fresh_writer)
                        return fresh_runtime.index.exclude(fresh_writer.scope_id, path=path)
            if copied is None:
                saved = json.loads((case.state / 'shadow-migration-intent.json').read_text())
                self.assertEqual(saved, started)
            else:
                self.assertEqual(copied['observation'], started['observation'])
                self.assertEqual(copied['phase'], 'copying')
            before_restart = json.loads((case.state / 'shadow-migration-intent.json').read_text())
            restarted = migration.advance_migration(store, project=case.project,
                executing_root=case.plugin, deadline=time.monotonic() + 30)
            self.assertEqual(restarted['phase'], 'copying')
            # No base receipt existed when this copy began, so the first writer
            # commit cannot be named by receipts: full recopy with a new scan.
            self.assertIsNone(before_restart['source_revision']['base_revision'])
            self.assertEqual(restarted['revision_check']['base'], 'unavailable')
            self.assertIsNone(restarted['catch_up'])
            self.assertIsNone(restarted['cursor'])
            self.assertNotEqual(restarted['observation'], started['observation'])
            self.assertNotEqual(restarted['provenance_scan']['id'], started['provenance_scan']['id'])
            for _ in range(6):
                finished = migration.advance_migration(store, project=case.project,
                    executing_root=case.plugin, deadline=time.monotonic() + 30)
                if finished['phase'] == 'copied':
                    break
            self.assertEqual(finished['phase'], 'copied')
            self.assertEqual(seed.selection.preferred_endpoint, started['endpoint_id'])
            verified = migration.advance_verification(store, project=case.project,
                executing_root=case.plugin, deadline=time.monotonic() + 30)
            self.assertEqual(verified['verification']['pages'], 1)
            self.assertFalse(verified['verification']['complete'])
            with self.assertRaisesRegex(Exception, 'migration_verification_incomplete'):
                migration.advance_corpus(store, project=case.project, executing_root=case.plugin,
                    sidecar=False, deadline=time.monotonic() + 30)
            intent_path = case.state / 'shadow-migration-intent.json'
            intent_before_crash = intent_path.read_bytes()
            with mock.patch.object(migration, 'MAX_BATCH', 2):
                with self.assertRaisesRegex(Exception, 'migration_intent_invalid'):
                    migration.advance_verification(store, project=case.project,
                        executing_root=case.plugin, deadline=time.monotonic() + 30)
                self.assertEqual(intent_path.read_bytes(), intent_before_crash)
                reset = migration.advance_migration(store, project=case.project,
                    executing_root=case.plugin, deadline=time.monotonic() + 30)
                self.assertEqual(reset['phase'], 'copied')
                self.assertIsNone(reset['verification'])
            verified = migration.advance_verification(store, project=case.project,
                executing_root=case.plugin, deadline=time.monotonic() + 30)
            self.assertEqual(verified['verification']['pages'], 1)
            intent_before_crash = intent_path.read_bytes()
            crash_payload = json.loads(payload.read_text())
            crash_payload['mode'] = 'verification-kill'
            payload.write_text(json.dumps(crash_payload), encoding='utf8')
            killed = subprocess.run([sys.executable, '-B', str(HERE / 'laya-shadow-crash-child.py'),
                                     str(payload)], capture_output=True, text=True, timeout=60)
            self.assertEqual(killed.returncode, -9, killed.stdout + killed.stderr)
            self.assertIn('verified-before-intent', killed.stdout)
            self.assertEqual(intent_path.read_bytes(), intent_before_crash)
            for _ in range(6):
                verified = migration.advance_verification(store, project=case.project,
                    executing_root=case.plugin, deadline=time.monotonic() + 30)
                if verified['verification']['complete']:
                    break
            self.assertTrue(verified['verification']['complete'])
            self.assertGreaterEqual(verified['verification']['pages'], 2)
            self.assertEqual(migration.advance_verification(store, project=case.project,
                executing_root=case.plugin, deadline=time.monotonic() + 30), verified)
            staged = migration.advance_corpus(store, project=case.project, executing_root=case.plugin,
                sidecar=False, deadline=time.monotonic() + 30)
            self.assertFalse(staged['corpora']['base']['complete'])
            saved_before_corpus_crash = intent_path.read_bytes()
            crash_payload = json.loads(payload.read_text())
            crash_payload['mode'] = 'corpus-kill'
            payload.write_text(json.dumps(crash_payload), encoding='utf8')
            killed = subprocess.run([sys.executable, '-B', str(HERE / 'laya-shadow-crash-child.py'),
                                     str(payload)], capture_output=True, text=True, timeout=60)
            self.assertEqual(killed.returncode, -9, killed.stdout + killed.stderr)
            self.assertIn('corpus-committed-before-intent', killed.stdout)
            self.assertEqual(intent_path.read_bytes(), saved_before_corpus_crash)
            after_corpus_crash = store.counts()
            staged = migration.advance_corpus(store, project=case.project, executing_root=case.plugin,
                sidecar=False, deadline=time.monotonic() + 30)
            self.assertEqual(store.counts(), after_corpus_crash)
            for sidecar_corpus in (False, True):
                key = 'sidecar' if sidecar_corpus else 'base'
                for _ in range(8):
                    staged = migration.advance_corpus(store, project=case.project, executing_root=case.plugin,
                        sidecar=sidecar_corpus, deadline=time.monotonic() + 30)
                    if staged['corpora'][key]['complete']:
                        break
                self.assertTrue(staged['corpora'][key]['complete'])
                self.assertEqual(migration.advance_corpus(store, project=case.project, executing_root=case.plugin,
                    sidecar=sidecar_corpus, deadline=time.monotonic() + 30), staged)
            readers = importlib.import_module('laya_reader_open')
            with seed.lease.resolve_endpoint(case.project, action='recall', host_root=case.plugin) as endpoint:
                decision = seed.reader.journal.select_route(str(case.project), action='recall')
                original_inspect = readers.inspect_warm_snapshot
                activity = {'active': 0, 'max': 0, 'count': 0}
                @contextmanager
                def tracked_snapshot(*args, **kwargs):
                    with original_inspect(*args, **kwargs) as pinned:
                        activity['active'] += 1
                        activity['max'] = max(activity['max'], activity['active'])
                        activity['count'] += 1
                        try:
                            yield pinned
                        finally:
                            activity['active'] -= 1
                before_mapping = store.counts()
                with mock.patch.object(readers, 'inspect_warm_snapshot', tracked_snapshot):
                    bounded = migration.collect_generation_members(store, worktree_id=worktree['worktreeId'],
                        endpoint=endpoint, decision=decision, provenance_scan=finished['provenance_scan'],
                        observation=finished['observation'], deadline=time.monotonic() + 30)
                self.assertGreaterEqual(activity['count'], 2)
                self.assertEqual((activity['active'], activity['max']), (0, 1))
                self.assertEqual(store.counts(), before_mapping)
                page_activity = activity['count']
                with mock.patch.object(readers, 'inspect_warm_snapshot', tracked_snapshot):
                    first = migration.verify_migration_page(store, worktree_id=worktree['worktreeId'],
                        endpoint=endpoint, decision=decision, observation=finished['observation'],
                        provenance_scan=finished['provenance_scan'], cursor=None,
                        deadline=time.monotonic() + 30)
                self.assertEqual(activity['count'] - page_activity, 1)
                self.assertEqual(first['observation'], finished['observation'])
                self.assertIsNotNone(first['next'])
                self.assertLessEqual(len(first['inventory']), 1)
                self.assertEqual(store.counts(), before_mapping)
                with self.assertRaisesRegex(Exception, 'migration_snapshot_changed'):
                    migration.collect_generation_members(store, worktree_id=worktree['worktreeId'],
                        endpoint=endpoint, decision=decision, provenance_scan=finished['provenance_scan'],
                        observation='0' * 64, deadline=time.monotonic() + 30)
                self.assertEqual(store.counts(), before_mapping)
                with self.assertRaisesRegex(Exception, 'migration_snapshot_changed'):
                    migration.stage_fts_corpus_bounded(store, worktree_id=worktree['worktreeId'],
                        endpoint=endpoint, decision=decision, observation='0' * 64,
                        deadline=time.monotonic() + 30, sidecar=False)
                self.assertEqual(store.counts(), before_mapping)
                with mock.patch.object(readers, 'inspect_warm_snapshot', tracked_snapshot):
                    base_corpus = migration.stage_fts_corpus_bounded(store, worktree_id=worktree['worktreeId'],
                        endpoint=endpoint, decision=decision, observation=finished['observation'],
                        deadline=time.monotonic() + 30, sidecar=False)
                    side_corpus = migration.stage_fts_corpus_bounded(store, worktree_id=worktree['worktreeId'],
                        endpoint=endpoint, decision=decision, observation=finished['observation'],
                        deadline=time.monotonic() + 30, sidecar=True)
                self.assertEqual(staged['corpora']['base']['rows'], len(base_corpus['members']))
                self.assertEqual(staged['corpora']['sidecar']['rows'], len(side_corpus['members']))
                self.assertEqual((activity['active'], activity['max']), (0, 1))
                before_mapping = store.counts()
                with readers.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 10) as snapshot:
                    checked_base = migration.stage_base_fts_corpus(store, worktree_id=worktree['worktreeId'],
                        snapshot=snapshot, endpoint=endpoint, decision=decision,
                        deadline=time.monotonic() + 10, allow_sidecar=True, verify_only=True)
                    checked_side = migration.stage_sidecar_fts_corpus(store, worktree_id=worktree['worktreeId'],
                        snapshot=snapshot, endpoint=endpoint, decision=decision,
                        deadline=time.monotonic() + 10, verify_only=True)
                    self.assertEqual((base_corpus, side_corpus), (checked_base, checked_side))
                    source = finished['source_revision']
                    self.assertEqual(source['generation'], snapshot.published[0])
                    self.assertEqual(source['base_revision'], snapshot.base_revision)
                    self.assertEqual(source['sidecar_revision'],
                                     None if snapshot.sidecar is None else snapshot.sidecar['revision'])
                    if sidecar:
                        self.assertEqual(snapshot.sidecar['revision'], 1)
                    evidence = migration.verify_generation_members(store, worktree_id=worktree['worktreeId'],
                        snapshot=snapshot, endpoint=endpoint, decision=decision,
                        provenance_scan=finished['provenance_scan'], deadline=time.monotonic() + 10)
                    self.assertEqual(bounded, evidence)
                    if sidecar:
                        self.assertIn(seed.overlap_sidecar_row[0].split('/', 1)[1],
                                      [row['path'] for row in evidence['members']])
                    else:
                        self.assertNotIn('concepts/otter-ledger.md', [row['path'] for row in evidence['members']])
                    self.assertIn('concepts/heron-route.md', [row['path'] for row in evidence['members']])
                    result = importlib.import_module('laya_v6_reader').query_v6_snapshot(
                        snapshot, endpoint=endpoint, prompt='heron', decision=decision,
                        provider=None, deadline=time.monotonic() + 10)
                    self.assertTrue(result.rows)
                    if not sidecar:
                        self.assertNotIn(case.WIKI + '/concepts/otter-ledger.md',
                                         [row['path'] for row in result.rows])
                views = importlib.import_module('laya_view_store')
                with closing(views.ViewStore.open(case.state / 'overlap-shadow.sqlite',
                        repository=repository, scope_id=case.route['scopeId'])) as view:
                    handle = view.capture_view(worktree['worktreeId'])
                    proof = {'observation': finished['observation'], 'endpoint': endpoint,
                             'decision': decision, 'provenance_scan': finished['provenance_scan'],
                             'commitments': {'migration_id': started['migration_id'],
                                 'verification': staged['verification'], 'corpora': staged['corpora']}}
                    head = view.head(handle)
                    with self.assertRaisesRegex(Exception, 'invalid_fields'):
                        view.prepare_generation(handle, None, expected_head=head)
                    bad = {**proof, 'commitments': {**proof['commitments'],
                        'verification': {**proof['commitments']['verification'], 'digest': '0' * 64},
                        'corpora': {**proof['commitments']['corpora'], 'verification_digest': '0' * 64}}}
                    with self.assertRaisesRegex(Exception, 'migration_commitment_mismatch'):
                        view.prepare_generation(handle, None, expected_head=head, legacy_snapshot=bad)
                    self.assertEqual(view._db.execute('SELECT COUNT(*) FROM generation_slots').fetchone()[0], 0)
                    bad_base = {**proof, 'commitments': {**proof['commitments'], 'corpora': {
                        **proof['commitments']['corpora'], 'base': {
                            **proof['commitments']['corpora']['base'], 'digest': '0' * 64}}}}
                    with self.assertRaisesRegex(Exception, 'migration_commitment_mismatch'):
                        view.prepare_generation(handle, None, expected_head=head, legacy_snapshot=bad_base)
                    self.assertEqual(view._db.execute('SELECT COUNT(*) FROM generation_slots').fetchone()[0], 0)
                    crash_payload = json.loads(payload.read_text())
                    crash_payload['mode'] = 'prepare-kill'
                    payload.write_text(json.dumps(crash_payload), encoding='utf8')
                    killed = subprocess.run([sys.executable, '-B', str(HERE / 'laya-shadow-crash-child.py'),
                                             str(payload)], capture_output=True, text=True, timeout=60)
                    self.assertEqual(killed.returncode, -9, killed.stdout + killed.stderr)
                    self.assertIn('slot-committed-before-seal', killed.stdout)
                    self.assertEqual(view._db.execute('SELECT COUNT(*) FROM generation_slots').fetchone()[0], 1)
                    original_roots = view._roots
                    def expired_roots(*args, **kwargs):
                        self.assertIsNotNone(kwargs.get('deadline'))
                        kwargs['deadline'] = time.monotonic() - 1
                        return original_roots(*args, **kwargs)
                    with mock.patch.object(view, '_roots', side_effect=expired_roots):
                        with self.assertRaisesRegex(Exception, 'fts_deadline_exceeded'):
                            view.prepare_generation(handle, None, expected_head=head, legacy_snapshot=proof,
                                                    deadline=time.monotonic() + 30)
                    self.assertEqual(view._db.execute('SELECT COUNT(*) FROM generation_slots').fetchone()[0], 1)
                    self.assertEqual(view._db.execute('SELECT COUNT(*) FROM generations').fetchone()[0], 0)
                    self.assertEqual(view.head(handle), head)
                    import project_dir
                    import laya_fresh_wiki_origin as fresh
                    import laya_ledger_common as ledger
                    from laya_product_install import _install_lock
                    original_prepare = view.prepare_generation
                    def prepare_without_install_locks(*args, **kwargs):
                        marker = project_dir.runtime_file(case.project, fresh.MARKER)
                        with _install_lock(case.state), ledger._operation_lock(marker):
                            pass
                        return original_prepare(*args, **kwargs)
                    with mock.patch.object(readers, 'inspect_warm_snapshot', tracked_snapshot), \
                         mock.patch.object(migration, 'collect_generation_members',
                                           wraps=migration.collect_generation_members) as collect, \
                         mock.patch.object(migration, 'reuse_committed_members',
                                           wraps=migration.reuse_committed_members) as reuse, \
                         mock.patch.object(view, 'prepare_generation', side_effect=prepare_without_install_locks):
                        generation = migration.prepare_shadow_generation(view, project=case.project,
                            executing_root=case.plugin, deadline=time.monotonic() + 30)
                    # The committed chains are refolded from one metadata read;
                    # prepare no longer walks every old page again.
                    self.assertEqual((collect.call_count, reuse.call_count), (0, 1))
                    self.assertIsNotNone(generation)
                    self.assertEqual(view._db.execute('SELECT COUNT(*) FROM generation_slots').fetchone()[0], 1)
                    marker = project_dir.runtime_file(case.project, fresh.MARKER)
                    saved_intent = ledger._read_json(intent_path)
                    def change_intent_after_prepare(*args, **kwargs):
                        result = original_prepare(*args, **kwargs)
                        with _install_lock(case.state), ledger._operation_lock(marker):
                            ledger._write(intent_path, {**saved_intent, 'restart_required': True}, replace=True)
                        return result
                    try:
                        with mock.patch.object(view, 'prepare_generation', side_effect=change_intent_after_prepare):
                            with self.assertRaisesRegex(Exception, 'migration_intent_changed'):
                                migration.prepare_shadow_generation(view, project=case.project,
                                    executing_root=case.plugin, deadline=time.monotonic() + 30)
                    finally:
                        with _install_lock(case.state), ledger._operation_lock(marker):
                            ledger._write(intent_path, saved_intent, replace=True)
                    forged_kind = 'legacy' if saved_intent['source_revision']['sidecar_kind'] == 'absent' else 'absent'
                    with _install_lock(case.state), ledger._operation_lock(marker):
                        ledger._write(intent_path, {**saved_intent, 'source_revision': {
                            **saved_intent['source_revision'], 'sidecar_kind': forged_kind,
                            'sidecar_revision': None}}, replace=True)
                    try:
                        with self.assertRaisesRegex(Exception, 'migration_source_revision_unproved'):
                            migration.publish_shadow_generation(view, generation, project=case.project,
                                executing_root=case.plugin, deadline=time.monotonic() + 30)
                        self.assertEqual(view.head(handle), head)
                    finally:
                        with _install_lock(case.state), ledger._operation_lock(marker):
                            ledger._write(intent_path, saved_intent, replace=True)
                    with _install_lock(case.state), ledger._operation_lock(marker):
                        ledger._write(intent_path, {**saved_intent, 'source_revision': {
                            **saved_intent['source_revision'],
                            'generation': saved_intent['source_revision']['generation'] + '-forged'}}, replace=True)
                    try:
                        with self.assertRaisesRegex(Exception, 'migration_source_revision_unproved'):
                            migration.publish_shadow_generation(view, generation, project=case.project,
                                executing_root=case.plugin, deadline=time.monotonic() + 30)
                        self.assertEqual(view.head(handle), head)
                    finally:
                        with _install_lock(case.state), ledger._operation_lock(marker):
                            ledger._write(intent_path, saved_intent, replace=True)
                    self.assertEqual(view.head(handle), head)
                    retry_deadline = time.monotonic() + 30
                    with mock.patch.object(view, '_verify_generation', wraps=view._verify_generation) as checked:
                        again = view.prepare_generation(handle, None, expected_head=head, legacy_snapshot=proof,
                                                        deadline=retry_deadline)
                    self.assertTrue(any(call.kwargs.get('deadline') == retry_deadline
                                        for call in checked.call_args_list))
                    self.assertEqual(again, generation)
                    self.assertEqual(view._db.execute('SELECT COUNT(*) FROM generation_slots').fetchone()[0], 1)
                    if final_race:
                        wrote = []
                        def publish_after_prepare(*args, **kwargs):
                            result = original_prepare(*args, **kwargs)
                            wrote.append(exclude_with_fresh_writer(case.WIKI + '/decisions/kestrel-plan.md'))
                            return result
                        with mock.patch.object(view, 'prepare_generation', side_effect=publish_after_prepare):
                            with self.assertRaisesRegex(Exception, 'migration_snapshot_changed'):
                                migration.prepare_shadow_generation(view, project=case.project,
                                    executing_root=case.plugin, deadline=time.monotonic() + 30)
                        self.assertEqual(wrote, ['excluded'])
                        self.assertEqual(view.head(handle), head)
                        self.assertEqual(seed.selection.preferred_endpoint, started['endpoint_id'])
                        # The writer commit advances the base revision, which the
                        # publication cut checks before the membership observation.
                        with self.assertRaisesRegex(Exception, 'migration_source_revision_unproved'):
                            migration.publish_shadow_generation(view, generation, project=case.project,
                                executing_root=case.plugin, deadline=time.monotonic() + 30)
                        self.assertEqual(view.head(handle), head)
                        return
                    stage = json.loads(view._db.execute(
                        'SELECT s.stage_json FROM generations g JOIN generation_slots s ON s.ordinal=g.ordinal '
                        'WHERE g.gen_id=?', (generation,)).fetchone()[0])
                    self.assertEqual(stage['manifest']['members'], bounded['members'])
                    self.assertEqual(stage['manifest']['scan_id'], finished['provenance_scan']['id'])
                    self.assertEqual((activity['active'], activity['max']), (0, 1))
                    with _install_lock(case.state), ledger._operation_lock(marker):
                        ledger._write(intent_path, {**saved_intent, 'corpora': None}, replace=True)
                    try:
                        with self.assertRaisesRegex(Exception, 'migration_verification_incomplete'):
                            migration.publish_shadow_generation(view, generation, project=case.project,
                                executing_root=case.plugin, deadline=time.monotonic() + 30)
                        self.assertEqual(view.head(handle), head)
                    finally:
                        with _install_lock(case.state), ledger._operation_lock(marker):
                            ledger._write(intent_path, saved_intent, replace=True)
                    if not sidecar:
                        crash_payload = json.loads(payload.read_text())
                        crash_payload.update(mode='publish-kill', generation=generation)
                        payload.write_text(json.dumps(crash_payload), encoding='utf8')
                        killed = subprocess.run([sys.executable, '-B', str(HERE / 'laya-shadow-crash-child.py'),
                                                 str(payload)], capture_output=True, text=True, timeout=60)
                        self.assertEqual(killed.returncode, -9, killed.stdout + killed.stderr)
                        self.assertIn('private-head-before-intent', killed.stdout)
                        self.assertEqual(view.head(handle)['gen_id'], generation)
                        self.assertIsNone(ledger._read_json(intent_path).get('published'))
                        with self.assertRaisesRegex(Exception, 'migration_publication_unrecorded'):
                            migration.compare_shadow_queries(view, generation, project=case.project,
                                executing_root=case.plugin, queries=('heron',), deadline=time.monotonic() + 30)
                    published = migration.publish_shadow_generation(view, generation, project=case.project,
                        executing_root=case.plugin, deadline=time.monotonic() + 30)
                    self.assertEqual(published['gen_id'], generation)
                    self.assertEqual(view.head(handle), published)
                    receipt = ledger._read_json(intent_path)['published']
                    self.assertEqual((receipt['generation'], receipt['view_id'], receipt['epoch']),
                                     (generation, published['view_id'], published['epoch']))
                    self.assertEqual(receipt['observation'], finished['observation'])
                    self.assertEqual(receipt['source_revision'], finished['source_revision'])
                    forged = 'g-' + 'a' * 32 + '-b' + '0' * 64
                    with self.assertRaisesRegex(Exception, 'migration_intent_changed'):
                        migration.publish_shadow_generation(view, forged, project=case.project,
                            executing_root=case.plugin, deadline=time.monotonic() + 30)
                    self.assertEqual(view.head(handle), published)
                    with _install_lock(case.state), ledger._operation_lock(marker):
                        recorded_intent = ledger._read_json(intent_path)
                        ledger._write(intent_path, {**recorded_intent, 'published': {
                            **receipt, 'base_digest': '0' * 64}}, replace=True)
                    try:
                        with self.assertRaisesRegex(Exception, 'migration_intent_invalid'):
                            migration.compare_shadow_queries(view, generation, project=case.project,
                                executing_root=case.plugin, queries=('heron',), deadline=time.monotonic() + 30)
                    finally:
                        with _install_lock(case.state), ledger._operation_lock(marker):
                            ledger._write(intent_path, recorded_intent, replace=True)
                    with mock.patch.object(readers, 'reader_publish_cut',
                            side_effect=AssertionError('read-only replay acquired publication cut')), \
                         mock.patch.object(readers, 'inspect_warm_snapshot', wraps=readers.inspect_warm_snapshot) as rechecked, \
                         mock.patch.object(view, 'pin_view', wraps=view.pin_view) as pinned:
                        self.assertEqual(migration.publish_shadow_generation(view, generation, project=case.project,
                            executing_root=case.plugin, deadline=time.monotonic() + 30), published)
                    self.assertGreaterEqual(rechecked.call_count, 1)
                    self.assertGreaterEqual(pinned.call_count, 1)
                    self.assertEqual(migration.prepare_shadow_generation(view, project=case.project,
                        executing_root=case.plugin, deadline=time.monotonic() + 30), generation)
                    self.assertEqual(view.head(handle), published)
                    self.assertTrue(view.verify_views())
                    with mock.patch.object(migration, 'capture_effective', wraps=migration.capture_effective) as captures:
                        comparison = migration.compare_shadow_queries(view, generation, project=case.project,
                            executing_root=case.plugin, queries=('heron', 'secretprobeXYZ'), deadline=time.monotonic() + 30)
                    # One selection, one complete inventory, one final recheck;
                    # the same pinned inventory is not reloaded for every page.
                    self.assertEqual(captures.call_count, 3)
                    self.assertEqual(comparison['query_count'], 2)
                    self.assertEqual(comparison['observation'], finished['observation'])
                    self.assertEqual(view.head(handle), published)
                    if sidecar:
                        with self.assertRaisesRegex(Exception, 'migration_lexical_sidecar_unproved'):
                            migration.compare_shadow_queries(view, generation, project=case.project,
                                executing_root=case.plugin, queries=('heron',), prompts=('heron',),
                                deadline=time.monotonic() + 30)
                    self.assertNotIn('secretprobeXYZ', (case.state / 'events.jsonl').read_text())
                    with mock.patch.object(migration, 'compare_lexical_queries',
                            side_effect=migration.IndexContractError('migration_lexical_parity_mismatch')):
                        with self.assertRaisesRegex(Exception, 'migration_lexical_parity_mismatch'):
                            migration.compare_shadow_queries(view, generation, project=case.project,
                                executing_root=case.plugin, queries=('heron',), deadline=time.monotonic() + 30)
                    self.assertEqual(len(view._pins), 0)
                    events = [json.loads(line) for line in (case.state / 'events.jsonl').read_text().splitlines()]
                    self.assertEqual([event for event in events if event.get('event') == 'migration.shadow'
                                      and event.get('outcome') == 'error'][-1]['reason'],
                                     'migration_lexical_parity_mismatch')
                    if compare_race:
                        changed = []
                        @contextmanager
                        def writer_after_comparison(*args, **kwargs):
                            with original_inspect(*args, **kwargs) as pinned:
                                yield pinned
                            if not changed:
                                changed.append(exclude_with_fresh_writer(case.WIKI + '/decisions/kestrel-plan.md'))
                        with mock.patch.object(readers, 'inspect_warm_snapshot', writer_after_comparison):
                            with self.assertRaisesRegex(Exception, 'migration_snapshot_changed'):
                                migration.compare_shadow_queries(view, generation, project=case.project,
                                    executing_root=case.plugin, queries=('heron',), deadline=time.monotonic() + 30)
                        self.assertEqual(changed, ['excluded'])
                        self.assertEqual(len(view._pins), 0)
                        self.assertEqual(seed.selection.preferred_endpoint, started['endpoint_id'])
                        with self.assertRaisesRegex(Exception, 'migration_snapshot_changed'):
                            migration.publish_shadow_generation(view, generation, project=case.project,
                                executing_root=case.plugin, deadline=time.monotonic() + 30)
                        if missing_receipt:
                            self.assertEqual(exclude_with_fresh_writer(case.WIKI + '/concepts/heron-route.md'),
                                             'excluded')
                            prior = recorded_intent['source_revision']['base_revision']
                            self.assertIsNotNone(prior)
                            key = 'reader-revision:base:' + f'{prior + 1:020d}'
                            with sqlite3.connect(case.db) as db:
                                trigger = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' "
                                    "AND name='schema_meta_delete_seal'").fetchone()[0]
                                db.execute('DROP TRIGGER schema_meta_delete_seal')
                                self.assertEqual(db.execute('DELETE FROM schema_meta WHERE key=?', (key,)).rowcount, 1)
                                db.execute(trigger)
                        pending = migration.advance_shadow(view, project=case.project,
                            executing_root=case.plugin, deadline=time.monotonic() + 30)
                        self.assertEqual(pending['stage'], 'restart_pending')
                        self.assertIsNone(pending['intent']['published'])
                        restarted = migration.advance_shadow(view, project=case.project,
                            executing_root=case.plugin, deadline=time.monotonic() + 30)
                        self.assertEqual(restarted['stage'], 'restart')
                        self.assertEqual(restarted['intent']['phase'], 'copying')
                        self.assertIsNone(restarted['intent']['published'])
                        self.assertNotEqual(restarted['intent']['source_revision'],
                                            recorded_intent['source_revision'])
                        self.assertEqual(restarted['intent']['revision_check']['base'],
                                         'gap' if missing_receipt else 'continuous')
                        if missing_receipt:
                            self.assertIsNone(restarted['intent']['cursor'])
                            self.assertIsNone(restarted['intent']['verification'])
                            events = [json.loads(line) for line in (case.state / 'events.jsonl').read_text().splitlines()]
                            self.assertEqual([event for event in events if event.get('event') == 'migration.shadow'
                                              and event.get('outcome') == 'restarted'][-1]['reason'],
                                             'revision_gap')
                        return
                    self.assertEqual(seed.selection.preferred_endpoint, started['endpoint_id'])
                before_mapping = store.counts()
                changed_between_pages = []
                @contextmanager
                def change_after_snapshot(*args, **kwargs):
                    with original_inspect(*args, **kwargs) as pinned:
                        yield pinned
                    if not changed_between_pages:
                        changed_between_pages.append(None)
                        changed_between_pages[0] = exclude_with_fresh_writer(
                            case.WIKI + '/decisions/kestrel-plan.md')
                with mock.patch.object(readers, 'inspect_warm_snapshot', change_after_snapshot):
                    with self.assertRaisesRegex(Exception, 'migration_snapshot_changed'):
                        migration.collect_generation_members(store, worktree_id=worktree['worktreeId'],
                            endpoint=endpoint, decision=decision, provenance_scan=finished['provenance_scan'],
                            observation=finished['observation'], deadline=time.monotonic() + 30)
                self.assertEqual(changed_between_pages, ['excluded'])
                self.assertEqual(store.counts(), before_mapping)
                intent_before = (case.state / 'shadow-migration-intent.json').read_bytes()
                with self.assertRaisesRegex(Exception, 'migration_snapshot_changed'):
                    migration.advance_verification(store, project=case.project,
                        executing_root=case.plugin, deadline=time.monotonic() + 30)
                with self.assertRaisesRegex(Exception, 'migration_snapshot_changed'):
                    migration.advance_corpus(store, project=case.project, executing_root=case.plugin,
                        sidecar=False, deadline=time.monotonic() + 30)
                self.assertEqual((case.state / 'shadow-migration-intent.json').read_bytes(), intent_before)
                with original_inspect(endpoint, deadline=time.monotonic() + 10) as latest:
                    current = migration.capture_effective(latest, endpoint=endpoint, decision=decision,
                        after=None, deadline=time.monotonic() + 10)['observation']
                changed_during_corpus = []
                @contextmanager
                def change_after_corpus_page(*args, **kwargs):
                    with original_inspect(*args, **kwargs) as pinned:
                        yield pinned
                    if not changed_during_corpus:
                        changed_during_corpus.append(None)
                        changed_during_corpus[0] = exclude_with_fresh_writer(
                            case.WIKI + '/concepts/heron-route.md')
                with mock.patch.object(readers, 'inspect_warm_snapshot', change_after_corpus_page):
                    with self.assertRaisesRegex(Exception, 'migration_snapshot_changed'):
                        migration.stage_fts_corpus_bounded(store, worktree_id=worktree['worktreeId'],
                            endpoint=endpoint, decision=decision, observation=current,
                            deadline=time.monotonic() + 30, sidecar=True, verify_only=True)
                self.assertEqual(changed_during_corpus, ['excluded'])
                self.assertEqual(store.counts(), before_mapping)
                restarted = migration.advance_migration(store, project=case.project,
                    executing_root=case.plugin, deadline=time.monotonic() + 30)
                self.assertEqual(restarted['phase'], 'copying')
                self.assertIsNone(restarted['verification'])
                self.assertIsNone(restarted['corpora'])

    def test_normal_writer_exclusion_is_caught_without_switching_reader(self):
        normal_spec = importlib.util.spec_from_file_location(
            'shadow_normal_writer_seed', HERE / 'laya-normal-writer-connections-checks.py')
        normal_module = importlib.util.module_from_spec(normal_spec)
        normal_spec.loader.exec_module(normal_module)
        normal = normal_module.NormalWriterConnectionChecks('runTest')
        seed_module = normal_module
        for _ in range(6):
            seed_module = seed_module.existing
        parent = seed_module.base.published.MoveCase
        original_publish = parent.publish_all
        def with_rank_corpus(case, roots):
            wiki = case.auto / 'wiki'
            for relative, text in (('concepts/heron-route.md', ' alpha' * 20 + ' gamma' * 2 + ' filler' * 350),
                                   ('concepts/otter-ledger.md', ' alpha' + ' filler' * 2000),
                                   ('decisions/kestrel-plan.md', ' beta gamma' + ' filler' * 50)):
                path = wiki / relative
                path.write_text(path.read_text(encoding='utf8') + text + '\n', encoding='utf8')
            return original_publish(case, roots)
        with mock.patch.object(parent, 'publish_all', new=with_rank_corpus):
            normal.setUp()
        self.addCleanup(normal.doCleanups)
        seed, case = normal.seed, normal.case
        subprocess.run(['git', '-c', 'init.defaultBranch=main', 'init', '-q', str(case.project)],
                       check=True, capture_output=True, timeout=10)
        subprocess.run(['git', '-C', str(case.project), '-c', 'commit.gpgsign=false',
                        '-c', 'core.hooksPath=' + os.devnull, '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty',
                        '-qm', 'fixture'], check=True, capture_output=True, timeout=10)
        checkout = importlib.import_module('laya_checkout_view')
        blobs = importlib.import_module('laya_blob_store')
        migration = importlib.import_module('laya_shadow_migration')
        readers = importlib.import_module('laya_reader_open')
        from laya_index_a import LayaIndexA
        from laya_index_claim_queue import ClaimQueue
        repository = checkout.register_repository(str(case.project))
        worktree = checkout.register_worktree(str(case.project), repository)
        blobs.BlobStore.create(case.state / 'normal-writer-shadow.sqlite',
            project_root=str(case.project), repository=repository, worktree=worktree,
            scope_id=case.route['scopeId']).close()
        # advance_shadow builds the private candidate, which needs the view API.
        store = importlib.import_module('laya_view_store').ViewStore.open(
            case.state / 'normal-writer-shadow.sqlite', repository=repository, scope_id=case.route['scopeId'])
        self.addCleanup(store.close)
        time.sleep(1.1)
        with closing(LayaIndexA(case.db)), closing(ClaimQueue.open(normal.queue)), seed.authority(), \
                mock.patch.object(migration, 'MAX_BATCH', 1):
            started = migration.begin_migration(store, worktree_id=worktree['worktreeId'],
                project=case.project, executing_root=case.plugin, deadline=time.monotonic() + 30)
            before_step = (case.state / 'shadow-migration-intent.json').read_bytes()
            original_state = migration._migration_state
            delayed_calls = []
            def delayed_dispatch(*args, **kwargs):
                result = original_state(*args, **kwargs)
                if not delayed_calls:
                    delayed_calls.append(True)
                    time.sleep(1.05)
                return result
            with mock.patch.object(migration, '_STEP_SECONDS', 1.0, create=True), \
                 mock.patch.object(migration, '_migration_state', side_effect=delayed_dispatch):
                with self.assertRaisesRegex(Exception, 'reader_snapshot_deadline'):
                    migration.advance_shadow(store, project=case.project,
                        executing_root=case.plugin, deadline=time.monotonic() + 30)
            self.assertEqual((case.state / 'shadow-migration-intent.json').read_bytes(), before_step)
            delayed_calls.clear()
            with mock.patch.object(migration, '_STEP_SECONDS', 3.0, create=True), \
                 mock.patch.object(migration, '_migration_state', side_effect=delayed_dispatch):
                first_step = migration.advance_shadow(store, project=case.project,
                    executing_root=case.plugin, deadline=time.monotonic() + 30)
            self.assertEqual(first_step['stage'], 'copy')
            first = first_step['intent']
            self.assertIsNotNone(first['cursor'])
            excluded = case.WIKI + '/concepts/otter-ledger.md'
            with ExitStack() as owners:
                writer, _ = normal.admission(owners)
                with normal.runtime(writer) as runtime:
                    normal.N.mark_new_write(writer)
                    self.assertEqual(runtime.index.exclude(writer.scope_id, path=excluded), 'excluded')
            changed = migration.advance_shadow(store, project=case.project,
                executing_root=case.plugin, deadline=time.monotonic() + 30)['intent']
            # The first NormalWriter commit creates the base baseline, so no
            # receipt names it yet: this one is still a full recopy.
            self.assertIsNone(changed['cursor'])
            self.assertIsNone(changed['catch_up'])
            self.assertNotEqual(changed['observation'], started['observation'])
            events = [json.loads(line) for line in (case.state / 'events.jsonl').read_text().splitlines()]
            restarted = [event for event in events if event.get('event') == 'migration.shadow'
                         and event.get('outcome') == 'restarted']
            self.assertEqual(restarted[-1]['migration'], started['migration_id'])
            for _ in range(5):
                changed = migration.advance_migration(store, project=case.project,
                    executing_root=case.plugin, deadline=time.monotonic() + 30)
                if changed['phase'] == 'copied':
                    break
            self.assertEqual(changed['phase'], 'copied')
            stages = []
            for _ in range(20):
                step = migration.advance_shadow(store, project=case.project,
                    executing_root=case.plugin, deadline=time.monotonic() + 30)
                stages.append(step['stage'])
                corpora = step['intent'].get('corpora')
                if corpora and corpora['sidecar'] and corpora['sidecar']['complete']: break
            self.assertEqual(step['stage'], 'sidecar_corpus')
            self.assertTrue({'verification', 'base_corpus', 'sidecar_corpus'} <= set(stages))
            self.assertTrue(step['intent']['verification']['complete'])
            self.assertTrue(step['intent']['corpora']['base']['complete'])
            self.assertTrue(step['intent']['corpora']['sidecar']['complete'])
            self.assertEqual(seed.selection.preferred_endpoint, started['endpoint_id'])
            with seed.lease.resolve_endpoint(case.project, action='recall', host_root=case.plugin) as endpoint:
                decision = seed.reader.journal.select_route(str(case.project), action='recall')
                with readers.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 10) as snapshot:
                    corpus, cursor = [], None
                    while True:
                        batch = migration.capture_base_fts_corpus(snapshot, endpoint=endpoint,
                            decision=decision, after=cursor, deadline=time.monotonic() + 10)
                        corpus.extend(batch['members'])
                        cursor = batch['next']
                        if cursor is None:
                            break
                    self.assertEqual(len(corpus), 3)
                    self.assertIn(excluded, [row['path'] for row in corpus])
                    self.assertTrue(all(row['path'].startswith(case.WIKI + '/') for row in corpus))
                    sidecar_corpus = migration.stage_sidecar_fts_corpus(store,
                        worktree_id=worktree['worktreeId'], snapshot=snapshot, endpoint=endpoint,
                        decision=decision, deadline=time.monotonic() + 10)
                    self.assertIn(excluded, [row['path'] for row in sidecar_corpus['members']])
                    with self.assertRaisesRegex(Exception, 'reader_sidecar_document_excluded'):
                        importlib.import_module('laya_journal_reader')._sidecar_document_snapshot(
                            snapshot.connections[3], snapshot.connections[0], decision, excluded,
                            deadline=time.monotonic() + 10, require_vectors=False)
                    with self.assertRaisesRegex(Exception, 'migration_snapshot_changed'):
                        migration.capture_base_fts_corpus(snapshot, endpoint=endpoint, decision=decision,
                            after={'after': corpus[0]['path'], 'observation': '0' * 64},
                            deadline=time.monotonic() + 10)
                    base = snapshot.connections[0]
                    db_type, original_execute = type(base), type(base).execute
                    class NonWiki:
                        def fetchone(self): return (1,)
                    def simulate_nonwiki(db, statement, *args, **kwargs):
                        if db is base and statement.startswith('SELECT 1 FROM membership m WHERE'):
                            return NonWiki()
                        return original_execute(db, statement, *args, **kwargs)
                    with mock.patch.object(db_type, 'execute', new=simulate_nonwiki):
                        with self.assertRaisesRegex(Exception, 'migration_raw_fts_corpus_unproved'):
                            migration.capture_base_fts_corpus(snapshot, endpoint=endpoint, decision=decision,
                                after=None, deadline=time.monotonic() + 10)
                    class MetaRows:
                        def fetchall(self): return [(case.WIKI + '/index.md', corpus[0]['hash'])]
                    def simulate_meta(db, statement, *args, **kwargs):
                        if db is base and statement.startswith('SELECT m.path,m.hash FROM membership m ') and 'm.path>?' in statement:
                            return MetaRows()
                        return original_execute(db, statement, *args, **kwargs)
                    with mock.patch.object(db_type, 'execute', new=simulate_meta):
                        with self.assertRaisesRegex(Exception, 'migration_meta_fts_corpus_unproved'):
                            migration.capture_base_fts_corpus(snapshot, endpoint=endpoint, decision=decision,
                                after=None, deadline=time.monotonic() + 10)
                    paths, cursor = [], None
                    while True:
                        page = migration.capture_effective(snapshot, endpoint=endpoint, decision=decision,
                            after=cursor, deadline=time.monotonic() + 10)
                        paths.extend(row['path'] for row in page['members'])
                        cursor = page['next']
                        if cursor is None:
                            break
                    self.assertNotIn(excluded, paths)
                    self.assertEqual(len(paths), 2)
                    result = importlib.import_module('laya_v6_reader').query_v6_snapshot(
                        snapshot, endpoint=endpoint, prompt='heron', decision=decision,
                        provider=None, deadline=time.monotonic() + 10)
                    self.assertTrue(result.rows)
                    self.assertNotIn(excluded, [row['path'] for row in result.rows])
                    views = importlib.import_module('laya_view_store')
                    view = views.ViewStore.open(case.state / 'normal-writer-shadow.sqlite',
                        repository=repository, scope_id=case.route['scopeId'])
                    try:
                        handle = view.capture_view(worktree['worktreeId'])
                        proof = {'snapshot': snapshot, 'endpoint': endpoint, 'decision': decision,
                                 'provenance_scan': changed['provenance_scan']}
                        selected = migration.verify_generation_members(view, worktree_id=worktree['worktreeId'],
                            **proof, deadline=time.monotonic() + 30)
                        manifest = {'scan_id': changed['provenance_scan']['id'],
                            'policy_digest': handle.policy_digest, 'revocation_epoch': 0,
                            'complete': True, 'sources': [], 'members': selected['members']}
                        base = view.head(handle)
                        generation = view.prepare_generation(handle, manifest, expected_head=base,
                                                             legacy_snapshot=proof)
                        view.publish_generation(handle, generation, expected_head=base, legacy_snapshot=proof)
                        self.assertTrue(view.verify_views())
                        pin = view.pin_view(handle)
                        try:
                            single = importlib.import_module('laya_v6_reader').query_v6_snapshot(
                                snapshot, endpoint=endpoint, prompt='gamma', decision=decision,
                                provider=None, deadline=time.monotonic() + 10)
                            gamma = view.search(pin, 'gamma', 10)
                            self.assertEqual([(row['path'].split('/', 1)[1], row['hash']) for row in single.rows],
                                             [(row['path'], row['blob_sha']) for row in gamma],
                                             'production single-term order')
                            migration.compare_lexical_queries(snapshot, endpoint=endpoint, decision=decision,
                                store=view, pin=pin, queries=('alpha OR beta',),
                                deadline=time.monotonic() + 10)
                            base_db = snapshot.connections[0]
                            ordinal = base_db.execute('SELECT ord FROM gen_index WHERE gen=?',
                                (snapshot.published[0],)).fetchone()[0]
                            for term in ('alpha', 'heron', 'kestrel', 'heron OR kestrel', 'filepath:"project"',
                                         'alpha OR beta', 'gamma'):
                                old = importlib.import_module('laya_index_a')._search_pinned_fts(
                                    base_db, decision['scopeId'], snapshot.published[0], ordinal,
                                    (term,), 10, path_prefixes=(case.WIKI + '/',),
                                    deadline=time.monotonic() + 10)[0]
                                new = view.search(pin, term, 10)
                                self.assertEqual([(row['path'].split('/', 1)[1], row['hash']) for row in old],
                                                 [(row['path'], row['blob_sha']) for row in new], term)
                                for left, right in zip(old, new):
                                    self.assertAlmostEqual(left['score'], right['score'], places=12, msg=term)
                        finally:
                            view.release(pin)
                    finally:
                        view.close()
                self.assertEqual(endpoint.endpoint_id, started['endpoint_id'])
            normal.plan = normal.P.capture_writer_plan(case.project, executing_root=case.plugin)
            with ExitStack() as owners:
                writer, _ = normal.admission(owners)
                with normal.runtime(writer) as runtime:
                    normal.N.mark_new_write(writer)
                    self.assertEqual(runtime.index.exclude(writer.scope_id,
                        path=case.WIKI + '/decisions/kestrel-plan.md'), 'excluded')
            original_inspect = readers.inspect_warm_snapshot
            @contextmanager
            def slow_snapshot(*args, **kwargs):
                with original_inspect(*args, **kwargs) as snapshot:
                    time.sleep(0.55)
                    yield snapshot
            with mock.patch.object(readers, 'inspect_warm_snapshot', slow_snapshot):
                pending = migration.advance_shadow(store, project=case.project,
                    executing_root=case.plugin, deadline=time.monotonic() + 30)
                self.assertEqual(pending['stage'], 'restart_pending')
                self.assertTrue(pending['intent']['restart_required'])
                pending_bytes = (case.state / 'shadow-migration-intent.json').read_bytes()
                with self.assertRaisesRegex(Exception, 'migration_restart_pending'):
                    migration.advance_verification(store, project=case.project,
                        executing_root=case.plugin, deadline=time.monotonic() + 30)
                with self.assertRaisesRegex(Exception, 'migration_restart_pending'):
                    migration.advance_corpus(store, project=case.project, executing_root=case.plugin,
                        sidecar=False, deadline=time.monotonic() + 30)
                self.assertEqual((case.state / 'shadow-migration-intent.json').read_bytes(), pending_bytes)
                payload = case.home / 'synthetic-shadow-restart.json'
                payload.write_text(json.dumps({'home': str(case.home),
                    'runtime': os.environ['MEMVANE_TEST_RUNTIME_ROOT'], 'plugin': str(case.plugin),
                    'proof': case.proof, 'candidate': str(case.state / 'normal-writer-shadow.sqlite'),
                    'repository': repository, 'worktree': worktree, 'scope': case.route['scopeId'],
                    'project': str(case.project), 'batch_limit': 1, 'mode': 'restart-kill'}), encoding='utf8')
                killed = subprocess.run([sys.executable, '-B', str(HERE / 'laya-shadow-crash-child.py'),
                                         str(payload)], capture_output=True, text=True, timeout=60)
                self.assertEqual(killed.returncode, -9, killed.stdout + killed.stderr)
                self.assertIn('restart-proved-before-intent', killed.stdout)
                self.assertEqual((case.state / 'shadow-migration-intent.json').read_bytes(), pending_bytes)
                restarted = migration.advance_shadow(store, project=case.project,
                    executing_root=case.plugin, deadline=time.monotonic() + 30)
            self.assertEqual(restarted['stage'], 'restart')
            # This fixture's sidecar is an unsealed legacy image without
            # receipts, so even a continuous base receipt cannot be replayed.
            self.assertEqual(restarted['intent']['revision_check'], {'base': 'continuous', 'sidecar': 'unavailable'})
            self.assertEqual(restarted['intent']['phase'], 'copying')
            self.assertIsNone(restarted['intent']['catch_up'])
            self.assertIsNone(restarted['intent']['verification'])
            self.assertIsNone(restarted['intent']['corpora'])
            self.assertEqual(seed.selection.preferred_endpoint, started['endpoint_id'])
            with sqlite3.connect(case.db) as db:
                db.execute('INSERT INTO exclusions VALUES(?,?,?)',
                           (case.route['scopeId'], 'path', 'project-wiki/unrecorded.md'))
            with self.assertRaisesRegex(Exception, 'reader_base_revision_unproved'):
                with seed.lease.resolve_endpoint(case.project, action='recall', host_root=case.plugin):
                    pass

    def test_advance_shadow_reaches_caught_up_and_resumes(self):
        normal_spec = importlib.util.spec_from_file_location(
            'shadow_state_machine_seed', HERE / 'laya-normal-writer-connections-checks.py')
        normal_module = importlib.util.module_from_spec(normal_spec)
        normal_spec.loader.exec_module(normal_module)
        normal = normal_module.NormalWriterConnectionChecks('runTest')
        normal.setUp()
        self.addCleanup(normal.doCleanups)
        seed, case = normal.seed, normal.case
        subprocess.run(['git', '-c', 'init.defaultBranch=main', 'init', '-q', str(case.project)],
                       check=True, capture_output=True, timeout=10)
        subprocess.run(['git', '-C', str(case.project), '-c', 'commit.gpgsign=false',
                        '-c', 'core.hooksPath=' + os.devnull, '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty',
                        '-qm', 'fixture'], check=True, capture_output=True, timeout=10)
        checkout = importlib.import_module('laya_checkout_view')
        blobs = importlib.import_module('laya_blob_store')
        views = importlib.import_module('laya_view_store')
        migration = importlib.import_module('laya_shadow_migration')
        readers = importlib.import_module('laya_reader_open')
        repository = checkout.register_repository(str(case.project))
        worktree = checkout.register_worktree(str(case.project), repository)
        candidate = case.state / 'state-machine-shadow.sqlite'
        blobs.BlobStore.create(candidate, project_root=str(case.project), repository=repository,
                               worktree=worktree, scope_id=case.route['scopeId']).close()
        store = views.ViewStore.open(candidate, repository=repository, scope_id=case.route['scopeId'])
        self.addCleanup(store.close)
        time.sleep(1.1)
        intent_path = case.state / 'shadow-migration-intent.json'
        payload = case.home / 'synthetic-shadow-state-machine.json'

        def step():
            return migration.advance_shadow(store, project=case.project,
                                            executing_root=case.plugin, deadline=time.monotonic() + 60)

        def slots():
            return store._db.execute('SELECT COUNT(*) FROM generation_slots').fetchone()[0]

        def corpora_complete(intent):
            corpora = intent.get('corpora')
            return bool(corpora and corpora['sidecar'] and corpora['sidecar']['complete'])

        def copy_until_prepare():
            stages = []
            for _ in range(40):
                result = step()
                stages.append(result['stage'])
                if corpora_complete(result['intent']):
                    break
            self.assertTrue(corpora_complete(result['intent']), stages)
            self.assertTrue({'copy', 'verification', 'base_corpus', 'sidecar_corpus'} <= set(stages), stages)
            self.assertIsNone(result['intent'].get('prepared'))
            self.assertIsNone(result['intent'].get('published'))
            return result['intent']

        def crash(mode, marker):
            payload.write_text(json.dumps({'home': str(case.home),
                'runtime': os.environ['MEMVANE_TEST_RUNTIME_ROOT'], 'plugin': str(case.plugin),
                'proof': case.proof, 'candidate': str(candidate), 'repository': repository,
                'worktree': worktree, 'scope': case.route['scopeId'], 'project': str(case.project),
                'batch_limit': 1, 'mode': mode}), encoding='utf8')
            before = intent_path.read_bytes()
            killed = subprocess.run([sys.executable, '-B', str(HERE / 'laya-shadow-crash-child.py'),
                                     str(payload)], capture_output=True, text=True, timeout=90)
            self.assertEqual(killed.returncode, -9, killed.stdout + killed.stderr)
            self.assertIn(marker, killed.stdout)
            self.assertEqual(intent_path.read_bytes(), before)
            self.assertEqual(seed.selection.preferred_endpoint, started['endpoint_id'])

        def exclude_with_fresh_writer(path):
            normal.plan = normal.P.capture_writer_plan(case.project, executing_root=case.plugin)
            with ExitStack() as owners:
                writer, _ = normal.admission(owners)
                with normal.runtime(writer) as runtime:
                    normal.N.mark_new_write(writer)
                    return runtime.index.exclude(writer.scope_id, path=path)

        with seed.authority(), mock.patch.object(migration, 'MAX_BATCH', 1):
            started = migration.begin_migration(store, worktree_id=worktree['worktreeId'],
                project=case.project, executing_root=case.plugin, deadline=time.monotonic() + 30)
            first = copy_until_prepare()
            handle = store.capture_view(worktree['worktreeId'])
            empty = store.head(handle)
            self.assertIsNone(empty['gen_id'])
            self.assertEqual(slots(), 0)

            # Crash inside the candidate build, then right after it and
            # before the intent records it. Each resume reuses one slot.
            crash('shadow-slot-kill', 'shadow-slot-before-seal')
            self.assertEqual((slots(), store.head(handle)), (1, empty))
            crash('shadow-intent-kill', 'shadow-step-before-intent')
            self.assertEqual((slots(), store.head(handle)), (1, empty))
            prepared = step()
            self.assertEqual(prepared['stage'], 'prepare')
            self.assertEqual(prepared['migration_state'], 'copying')
            self.assertEqual(started['migration_state'], 'copying')
            stale = prepared['intent']['prepared']['generation']
            self.assertEqual(prepared['intent']['prepared']['observation'], first['observation'])
            self.assertIsNone(prepared['intent'].get('published'))
            self.assertEqual((slots(), store.head(handle)), (1, empty))

            # A NormalWriter change between steps restarts the copy; the
            # prepared candidate for the old source is never published.
            self.assertEqual(exclude_with_fresh_writer(case.WIKI + '/concepts/otter-ledger.md'), 'excluded')
            pending = step()
            self.assertEqual(pending['stage'], 'restart_pending')
            self.assertTrue(pending['intent']['restart_required'])
            self.assertIsNone(pending['intent']['prepared'])
            self.assertIsNone(pending['intent']['published'])
            self.assertEqual(pending['intent']['migration_state'], 'copying')
            self.assertEqual(store.head(handle), empty)
            events = [json.loads(line) for line in (case.state / 'events.jsonl').read_text().splitlines()]
            self.assertIn([event for event in events if event.get('event') == 'migration.shadow'
                           and event.get('outcome') == 'restart_pending'][-1]['reason'],
                          ('migration_snapshot_changed', 'migration_source_revision_unproved'))
            restarted = step()
            self.assertEqual(restarted['stage'], 'restart')
            self.assertEqual(restarted['intent']['phase'], 'copying')
            self.assertNotEqual(restarted['intent']['observation'], first['observation'])
            second = copy_until_prepare()
            prepared = step()
            self.assertEqual(prepared['stage'], 'prepare')
            generation = prepared['intent']['prepared']['generation']
            self.assertNotEqual(generation, stale)
            # A new source scan owns a new slot; the stale slot stays private.
            self.assertEqual((slots(), store.head(handle)), (2, empty))

            # Crash right after the private head CAS and again right before
            # its receipt; resume proves the same head without a second CAS.
            crash('shadow-publish-kill', 'shadow-head-before-receipt')
            head = store.head(handle)
            self.assertEqual((head['gen_id'], head['epoch']), (generation, 1))
            crash('shadow-intent-kill', 'shadow-step-before-intent')
            self.assertEqual(store.head(handle), head)
            published = step()
            self.assertEqual(published['stage'], 'publish')
            self.assertEqual(published['migration_state'], 'caught_up')
            receipt = published['intent']['published']
            self.assertEqual((receipt['generation'], receipt['view_id'], receipt['epoch']),
                             (generation, head['view_id'], 1))
            self.assertEqual(receipt['observation'], second['observation'])
            self.assertEqual(published['intent']['prepared']['generation'], generation)
            self.assertEqual((slots(), store.head(handle)), (2, head))
            caught = intent_path.read_bytes()
            sync = importlib.import_module('laya_index_sync')
            queue = Path(seed.reader.journal.select_route(str(case.project), action='recall')['queuePath'])

            def receipt():
                return migration.shadow_parity_receipt(case.state, candidate=candidate,
                                                       candidate_incarnation=store._incarnation)

            # caught_up is not parity. A recall difference on a corpus page
            # raises and is never recorded, so no receipt can be sealed.
            self.assertIsNone(receipt())
            with mock.patch.object(migration, 'compare_combined_lexical_prompts',
                                   side_effect=migration.IndexContractError('migration_recall_parity_mismatch')):
                with self.assertRaisesRegex(Exception, 'migration_recall_parity_mismatch'):
                    step()
            self.assertEqual(intent_path.read_bytes(), caught)
            self.assertIsNone(receipt())

            # Parity pages: one bounded page per step, no publication cut and
            # no writer lock (the test holds the sync owner throughout).
            stages = []
            with mock.patch.object(migration, '_PARITY_MEMBERS', 1), \
                    mock.patch.object(readers, 'reader_publish_cut',
                                      side_effect=AssertionError('parity step acquired publication cut')), \
                    sync._owner_lock(queue) as owned:
                self.assertTrue(owned)
                for _ in range(10):
                    paged = step()
                    stages.append(paged['stage'])
                    if paged['intent']['parity']['compared']:
                        break
            first_parity = paged['intent']['parity']
            members = store._db.execute('SELECT COUNT(*) FROM wiki_members WHERE gen_id=?',
                                        (generation,)).fetchone()[0]
            self.assertEqual(set(stages), {'parity'}, stages)
            self.assertEqual(paged['migration_state'], 'caught_up')
            self.assertEqual((first_parity['members'], first_parity['attempt'], first_parity['discarded_pages']),
                             (members, 1, 0))
            self.assertGreaterEqual(first_parity['pages'], members)
            self.assertIsNone(first_parity['seal'])
            self.assertIsNone(receipt())

            # Seal: a busy writer is never waited on; nothing is written.
            compared = intent_path.read_bytes()
            with sync._owner_lock(queue) as owned:
                self.assertTrue(owned)
                busy = step()
            self.assertEqual((busy['stage'], busy['reason'], busy['migration_state']),
                             ('retry', 'sync_owner_busy', 'caught_up'))
            self.assertEqual(intent_path.read_bytes(), compared)
            # The quiesce bound is exceeded: the seal releases the writer and
            # leaves the compared parity for a later retry.
            actual_capture = migration.capture_effective
            def slow_capture(*args, **kwargs):
                result = actual_capture(*args, **kwargs)
                time.sleep(1.05)
                return result
            with mock.patch.object(migration, '_STEP_SECONDS', 1.0), \
                    mock.patch.object(migration, 'capture_effective', side_effect=slow_capture):
                slow = step()
            self.assertEqual((slow['stage'], slow['reason'], slow['migration_state']),
                             ('retry', 'reader_snapshot_deadline', 'caught_up'))
            self.assertEqual(intent_path.read_bytes(), compared)
            with sync._owner_lock(queue) as owned:
                self.assertTrue(owned)
            # SIGKILL right before the seal write leaves the compared intent.
            crash('shadow-intent-kill', 'shadow-step-before-intent')
            self.assertEqual(intent_path.read_bytes(), compared)
            self.assertIsNone(receipt())

            sealed = step()
            self.assertEqual((sealed['stage'], sealed['migration_state']), ('parity_seal', 'shadow_verified'))
            self.assertIsInstance(sealed['quiesce_ms'], float)
            self.assertGreaterEqual(sealed['quiesce_ms'], 0)
            print('quiesce_ms', sealed['quiesce_ms'], 'pages', first_parity['pages'], flush=True)
            proved = receipt()
            self.assertEqual(proved, {'migration_id': started['migration_id'], 'generation': generation,
                                      'digest': sealed['intent']['parity']['seal'], 'candidate': str(candidate),
                                      'candidate_incarnation': store._incarnation})
            self.assertIsNone(migration.shadow_parity_receipt(case.state, candidate=candidate,
                                                              candidate_incarnation='0' * 32))
            verified = intent_path.read_bytes()
            with mock.patch.object(readers, 'reader_publish_cut',
                                   side_effect=AssertionError('verified step acquired publication cut')):
                again = step()
            self.assertEqual((again['stage'], again['migration_state']), ('shadow_verified', 'shadow_verified'))
            self.assertEqual(intent_path.read_bytes(), verified)
            self.assertEqual(receipt(), proved)
            self.assertEqual((slots(), store.head(handle)), (2, head))
            self.assertEqual(seed.selection.preferred_endpoint, started['endpoint_id'])

            # A writer commit after the receipt invalidates it: copying again,
            # and the next parity starts over with the discarded pages counted.
            self.assertEqual(exclude_with_fresh_writer(case.WIKI + '/decisions/kestrel-plan.md'), 'excluded')
            pending = step()
            self.assertEqual((pending['stage'], pending['migration_state']), ('restart_pending', 'copying'))
            self.assertIsNone(receipt())
            for _ in range(40):
                resumed = step()
                if resumed['migration_state'] == 'caught_up':
                    break
            self.assertEqual(resumed['migration_state'], 'caught_up')
            self.assertIsNone(receipt())
            restarted = step()
            self.assertEqual(restarted['stage'], 'parity')
            self.assertEqual((restarted['intent']['parity']['attempt'],
                              restarted['intent']['parity']['discarded_pages']), (2, first_parity['pages']))
            self.assertEqual(seed.selection.preferred_endpoint, started['endpoint_id'])
            self.assertTrue(store.verify_views())

    def test_writer_between_publish_proof_and_cut_restarts(self):
        # The scale-proportional publish proof runs before the cut; a writer
        # commit landing after it must still be refused by the O(1) recheck.
        normal_spec = importlib.util.spec_from_file_location(
            'shadow_cut_identity_seed', HERE / 'laya-normal-writer-connections-checks.py')
        normal_module = importlib.util.module_from_spec(normal_spec)
        normal_spec.loader.exec_module(normal_module)
        normal = normal_module.NormalWriterConnectionChecks('runTest')
        normal.setUp()
        self.addCleanup(normal.doCleanups)
        seed, case = normal.seed, normal.case
        subprocess.run(['git', '-c', 'init.defaultBranch=main', 'init', '-q', str(case.project)],
                       check=True, capture_output=True, timeout=10)
        subprocess.run(['git', '-C', str(case.project), '-c', 'commit.gpgsign=false',
                        '-c', 'core.hooksPath=' + os.devnull, '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty',
                        '-qm', 'fixture'], check=True, capture_output=True, timeout=10)
        checkout = importlib.import_module('laya_checkout_view')
        blobs = importlib.import_module('laya_blob_store')
        views = importlib.import_module('laya_view_store')
        migration = importlib.import_module('laya_shadow_migration')
        readers = importlib.import_module('laya_reader_open')
        repository = checkout.register_repository(str(case.project))
        worktree = checkout.register_worktree(str(case.project), repository)
        candidate = case.state / 'cut-identity-shadow.sqlite'
        blobs.BlobStore.create(candidate, project_root=str(case.project), repository=repository,
                               worktree=worktree, scope_id=case.route['scopeId']).close()
        store = views.ViewStore.open(candidate, repository=repository, scope_id=case.route['scopeId'])
        self.addCleanup(store.close)
        time.sleep(1.1)

        def step():
            return migration.advance_shadow(store, project=case.project,
                                            executing_root=case.plugin, deadline=time.monotonic() + 60)

        def exclude_with_fresh_writer(path):
            normal.plan = normal.P.capture_writer_plan(case.project, executing_root=case.plugin)
            with ExitStack() as owners:
                writer, _ = normal.admission(owners)
                with normal.runtime(writer) as runtime:
                    normal.N.mark_new_write(writer)
                    return runtime.index.exclude(writer.scope_id, path=path)

        with seed.authority(), mock.patch.object(migration, 'MAX_BATCH', 1):
            started = migration.begin_migration(store, worktree_id=worktree['worktreeId'],
                project=case.project, executing_root=case.plugin, deadline=time.monotonic() + 30)
            for _ in range(40):
                result = step()
                corpora = result['intent'].get('corpora')
                if corpora and corpora['sidecar'] and corpora['sidecar']['complete']:
                    break
            prepared = step()
            self.assertEqual(prepared['stage'], 'prepare')
            stale = prepared['intent']['prepared']['generation']
            handle = store.capture_view(worktree['worktreeId'])
            empty = store.head(handle)
            self.assertIsNone(empty['gen_id'])

            original_cut = readers.reader_publish_cut
            original_verify = store._verify_generation
            verified, injected, cuts = [], [], []
            def verify_then_record(*args, **kwargs):
                result = original_verify(*args, **kwargs)
                verified.append(True)
                return result
            @contextmanager
            def writer_before_cut(*args, **kwargs):
                # Only the publication cut after the full candidate proof.
                if verified and not injected:
                    injected.append(exclude_with_fresh_writer(case.WIKI + '/concepts/otter-ledger.md'))
                cuts.append(len(verified))
                with original_cut(*args, **kwargs):
                    yield
            with mock.patch.object(readers, 'reader_publish_cut', writer_before_cut), \
                 mock.patch.object(store, '_verify_generation', side_effect=verify_then_record), \
                 mock.patch.object(migration, 'capture_effective', wraps=migration.capture_effective) as captured, \
                 mock.patch.object(migration, 'current_cut_identity',
                                   wraps=migration.current_cut_identity) as rechecked:
                pending = step()
            self.assertEqual(injected, ['excluded'])
            self.assertEqual(cuts, [1])
            self.assertEqual(len(verified), 1)
            # One fresh pinned proof outside the cut; no membership scan under it.
            self.assertEqual(captured.call_count, 1)
            self.assertEqual(rechecked.call_count, 1)
            self.assertEqual(pending['stage'], 'restart_pending')
            self.assertTrue(pending['intent']['restart_required'])
            self.assertIsNone(pending['intent']['prepared'])
            self.assertIsNone(pending['intent']['published'])
            self.assertEqual(store.head(handle), empty)
            state = store._db.execute('SELECT state FROM generations WHERE gen_id=?', (stale,)).fetchone()[0]
            self.assertEqual(state, 'prepared')
            events = [json.loads(line) for line in (case.state / 'events.jsonl').read_text().splitlines()]
            self.assertEqual([event for event in events if event.get('event') == 'migration.shadow'
                              and event.get('outcome') == 'restart_pending'][-1]['reason'],
                             'migration_snapshot_changed')
            restarted = step()
            self.assertEqual(restarted['stage'], 'restart')
            self.assertEqual(restarted['intent']['phase'], 'copying')
            self.assertNotEqual(restarted['intent']['observation'], started['observation'])
            self.assertEqual(store.head(handle), empty)
            self.assertEqual(seed.selection.preferred_endpoint, started['endpoint_id'])

    def test_sidecar_publication_restarts_copy_without_switching_reader(self):
        fixture = sidecar_seed.SidecarWriteChecks('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        seed, case = fixture.seed, fixture.case
        subprocess.run(['git', '-c', 'init.defaultBranch=main', 'init', '-q', str(case.project)],
                       check=True, capture_output=True, timeout=10)
        subprocess.run(['git', '-C', str(case.project), '-c', 'commit.gpgsign=false',
                        '-c', 'core.hooksPath=' + os.devnull, '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty',
                        '-qm', 'fixture'], check=True, capture_output=True, timeout=10)
        checkout = importlib.import_module('laya_checkout_view')
        blobs = importlib.import_module('laya_blob_store')
        migration = importlib.import_module('laya_shadow_migration')
        repository = checkout.register_repository(str(case.project))
        worktree = checkout.register_worktree(str(case.project), repository)
        store = blobs.BlobStore.create(case.state / 'shadow-catchup.sqlite',
            project_root=str(case.project), repository=repository, worktree=worktree,
            scope_id=case.route['scopeId'])
        self.addCleanup(store.close)
        time.sleep(1.1)
        with seed.authority(), mock.patch.object(migration, 'MAX_BATCH', 1):
            started = migration.begin_migration(store, worktree_id=worktree['worktreeId'],
                project=case.project, executing_root=case.plugin, deadline=time.monotonic() + 30)
            first = migration.advance_migration(store, project=case.project,
                executing_root=case.plugin, deadline=time.monotonic() + 30)
            self.assertEqual(first['phase'], 'copying')
            self.assertIsNotNone(first['cursor'])
            for _ in range(5):
                copied = migration.advance_migration(store, project=case.project,
                    executing_root=case.plugin, deadline=time.monotonic() + 30)
                if copied['phase'] == 'copied':
                    break
            self.assertEqual(copied['phase'], 'copied')
            views = importlib.import_module('laya_view_store')
            readers = importlib.import_module('laya_reader_open')
            view = views.ViewStore.open(case.state / 'shadow-catchup.sqlite',
                repository=repository, scope_id=case.route['scopeId'])
            self.addCleanup(view.close)
            with seed.lease.resolve_endpoint(case.project, action='recall', host_root=case.plugin) as endpoint:
                decision = seed.reader.journal.select_route(str(case.project), action='recall')
                with readers.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 10) as snapshot:
                    handle = view.capture_view(worktree['worktreeId'])
                    proof = {'snapshot': snapshot, 'endpoint': endpoint, 'decision': decision,
                             'provenance_scan': copied['provenance_scan']}
                    selected = migration.verify_generation_members(view, worktree_id=worktree['worktreeId'],
                        **proof, deadline=time.monotonic() + 30)
                    manifest = {'scan_id': copied['provenance_scan']['id'],
                        'policy_digest': handle.policy_digest, 'revocation_epoch': 0,
                        'complete': True, 'sources': [], 'members': selected['members']}
                    base = view.head(handle)
                    generation = view.prepare_generation(handle, manifest, expected_head=base,
                                                         legacy_snapshot=proof)
            # The sidecar row lands after the full publish proof and before the
            # cut; only the O(1) cut identity can see it, and it must refuse.
            original_cut = readers.reader_publish_cut
            original_verify = view._verify_generation
            verified, injected, cuts = [], [], []
            def verify_then_record(*args, **kwargs):
                result = original_verify(*args, **kwargs)
                verified.append(True)
                return result
            @contextmanager
            def sidecar_before_cut(*args, **kwargs):
                if verified and not injected:
                    with fixture.opened() as (runtime, catalog, row, component):
                        injected.append(fixture.publish(runtime, catalog, row, component)['status'])
                cuts.append(len(verified))
                with original_cut(*args, **kwargs):
                    yield
            with seed.lease.resolve_endpoint(case.project, action='recall', host_root=case.plugin) as endpoint:
                decision = seed.reader.journal.select_route(str(case.project), action='recall')
                with mock.patch.object(readers, 'reader_publish_cut', sidecar_before_cut), \
                     mock.patch.object(view, '_verify_generation', side_effect=verify_then_record), \
                     mock.patch.object(migration, 'current_cut_identity',
                                       wraps=migration.current_cut_identity) as rechecked:
                    with self.assertRaisesRegex(Exception, 'migration_snapshot_changed'):
                        view.publish_generation(handle, generation, expected_head=base, legacy_snapshot={
                            'endpoint': endpoint, 'decision': decision,
                            'provenance_scan': copied['provenance_scan']}, deadline=time.monotonic() + 30)
            self.assertEqual(injected, ['published'])
            self.assertEqual((cuts, len(verified), rechecked.call_count), ([1], 1, 1))
            self.assertIsNone(view.head(handle)['gen_id'])
            self.assertEqual(view._db.execute('SELECT state FROM generations WHERE gen_id=?',
                                              (generation,)).fetchone()[0], 'prepared')
            side_path = fixture.fixture.sidecar
            with sqlite3.connect(side_path) as db:
                rowid, original_body = db.execute('SELECT rowid,body FROM docs ORDER BY path LIMIT 1').fetchone()
                db.execute('UPDATE docs_fts SET body=? WHERE rowid=?',
                           (original_body + '\nsynthetic FTS tamper', rowid))
            try:
                with seed.lease.resolve_endpoint(case.project, action='recall', host_root=case.plugin) as endpoint:
                    decision = seed.reader.journal.select_route(str(case.project), action='recall')
                    with readers.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 10) as snapshot:
                        self.assertTrue(snapshot.sidecar['baseline'])
                        with self.assertRaisesRegex(Exception, 'reader_sidecar_revision_unproved'):
                            migration.sidecar_fts_commitment(snapshot, endpoint=endpoint, decision=decision,
                                                             deadline=time.monotonic() + 10)
            finally:
                with sqlite3.connect(side_path) as db:
                    db.execute('UPDATE docs_fts SET body=? WHERE rowid=?', (original_body, rowid))
            with seed.lease.resolve_endpoint(case.project, action='recall', host_root=case.plugin) as endpoint:
                decision = seed.reader.journal.select_route(str(case.project), action='recall')
                with readers.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 10) as snapshot:
                    with self.assertRaisesRegex(Exception, 'migration_snapshot_changed|migration_staged_missing'):
                        view.publish_generation(handle, generation, expected_head=base, legacy_snapshot={
                            'snapshot': snapshot, 'endpoint': endpoint, 'decision': decision,
                            'provenance_scan': copied['provenance_scan']})
                    self.assertIsNone(view.head(handle)['gen_id'])
            changed = migration.advance_migration(store, project=case.project,
                executing_root=case.plugin, deadline=time.monotonic() + 30)
            self.assertNotEqual(changed['observation'], started['observation'])
            if changed['revision_check']['base'] in ('continuous', 'unchanged'):
                # Receipts name the published sidecar row: catch up in place.
                self.assertEqual(changed['phase'], 'copied')
                self.assertEqual(changed['revision_check']['sidecar'], 'advanced')
                self.assertEqual(changed['provenance_scan'], started['provenance_scan'])
                self.assertIsNotNone(changed['catch_up'])
            else:
                self.assertEqual(changed['phase'], 'copying')
                self.assertIsNone(changed['cursor'])
                self.assertIsNone(changed['catch_up'])
                self.assertNotEqual(changed['provenance_scan']['id'], started['provenance_scan']['id'])
            for _ in range(5):
                changed = migration.advance_migration(store, project=case.project,
                    executing_root=case.plugin, deadline=time.monotonic() + 30)
                if changed['phase'] == 'copied':
                    break
            self.assertEqual(changed['phase'], 'copied')
            with seed.lease.resolve_endpoint(case.project, action='recall', host_root=case.plugin) as endpoint:
                self.assertEqual(endpoint.endpoint_id, started['endpoint_id'])
                self.assertEqual(endpoint.endpoint_id, seed.selection.preferred_endpoint)
                decision = seed.reader.journal.select_route(str(case.project), action='recall')
                with readers.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 10) as snapshot:
                    expected = [tuple(row) for row in snapshot.connections[3].execute(
                        'SELECT path,hash,title,body FROM docs ORDER BY path COLLATE BINARY')]
                    corpus, cursor = [], None
                    while True:
                        page = migration.capture_sidecar_fts_corpus(snapshot, endpoint=endpoint,
                            decision=decision, after=cursor, deadline=time.monotonic() + 10)
                        self.assertLessEqual(len(page['members']), 1)
                        corpus.extend((row['path'], row['hash'], row['title'], row['body'])
                                      for row in page['members'])
                        cursor = page['next']
                        if cursor is None:
                            break
                    self.assertEqual(corpus, expected)
                    self.assertTrue(corpus)
                    staged = migration.stage_sidecar_fts_corpus(view, worktree_id=worktree['worktreeId'],
                        snapshot=snapshot, endpoint=endpoint, decision=decision,
                        deadline=time.monotonic() + 30)
                    rowids = dict(snapshot.connections[3].execute('SELECT path,rowid FROM docs').fetchall())
                    self.assertEqual(staged['members'],
                        [{'path': path, 'hash': digest, 'title': title, 'rowid': rowids[path]}
                         for path, digest, title, _body in expected])
                    before = view.counts()
                    checked = migration.stage_sidecar_fts_corpus(view, worktree_id=worktree['worktreeId'],
                        snapshot=snapshot, endpoint=endpoint, decision=decision,
                        deadline=time.monotonic() + 30, verify_only=True)
                    self.assertEqual(checked, staged)
                    self.assertEqual(view.counts(), before)
                    base_db, _, _, side_db = snapshot.connections
                    reader = importlib.import_module('laya_journal_reader')
                    execute = type(base_db).execute
                    class Excluded:
                        def fetchone(self): return (1,)
                    def excluded_row(db, sql, *args, **kwargs):
                        if db is base_db and sql.startswith('SELECT 1 FROM exclusions WHERE scope=? AND '):
                            return Excluded()
                        return execute(db, sql, *args, **kwargs)
                    with mock.patch.object(type(base_db), 'execute', new=excluded_row):
                        with self.assertRaisesRegex(Exception, 'reader_sidecar_document_excluded'):
                            reader._sidecar_document_snapshot(side_db, base_db, decision, corpus[0][0],
                                deadline=time.monotonic() + 10, require_vectors=False)
                        first = migration.capture_sidecar_fts_corpus(snapshot, endpoint=endpoint,
                            decision=decision, after=None, deadline=time.monotonic() + 10)
                        self.assertEqual(first['members'][0]['path'], corpus[0][0])
                    with self.assertRaisesRegex(Exception, 'reader_snapshot_input_invalid'):
                        reader._sidecar_document_snapshot(side_db, base_db, decision, corpus[0][0],
                            deadline=time.monotonic() + 10, require_vectors=True, _for_fts_corpus=True)
                    with self.assertRaisesRegex(Exception, 'migration_snapshot_changed'):
                        migration.capture_sidecar_fts_corpus(snapshot, endpoint=endpoint, decision=decision,
                            after={'after': corpus[0][0], 'observation': '0'*64},
                            deadline=time.monotonic() + 10)
                    original_capture = migration.capture_effective
                    def sidecar_inventory(*args, **kwargs):
                        page = original_capture(*args, **kwargs)
                        if kwargs.get('include_ineligible') and page['members']:
                            page = {**page, 'members': [{**page['members'][0], 'source': 'sidecar'}]}
                        return page
                    with mock.patch.object(migration, 'capture_effective', side_effect=sidecar_inventory):
                        with self.assertRaisesRegex(Exception, 'migration_lexical_sidecar_unproved'):
                            migration.compare_lexical_queries(snapshot, endpoint=endpoint, decision=decision,
                                store=view, pin=None, queries=('heron',), deadline=time.monotonic() + 10)

    def test_writer_receipts_catch_up_only_changed_paths(self):
        normal_spec = importlib.util.spec_from_file_location(
            'shadow_receipt_seed', HERE / 'laya-normal-writer-connections-checks.py')
        normal_module = importlib.util.module_from_spec(normal_spec)
        normal_spec.loader.exec_module(normal_module)
        normal = normal_module.NormalWriterConnectionChecks('runTest')
        seed_module = normal_module
        for _ in range(6):
            seed_module = seed_module.existing
        def remove_completed_rows(seed):
            # Two completed rows leave the sidecar: the first publication seals
            # it before the migration starts, the second is the receipt change.
            with sqlite3.connect(seed.sidecar) as db:
                seed.overlap_sidecar_rows = db.execute(
                    'SELECT path,hash,title,body,profile_id FROM docs ORDER BY path LIMIT 2').fetchall()
                for row in seed.overlap_sidecar_rows:
                    db.execute('DELETE FROM docs_fts WHERE path=?', (row[0],))
                    db.execute('DELETE FROM docs WHERE path=?', (row[0],))
        with mock.patch.object(seed_module.MoveSealChecks, 'before_conversion',
                               new=remove_completed_rows, create=True):
            normal.setUp()
        self.addCleanup(normal.doCleanups)
        seed, case = normal.seed, normal.case
        subprocess.run(['git', '-c', 'init.defaultBranch=main', 'init', '-q', str(case.project)],
                       check=True, capture_output=True, timeout=10)
        subprocess.run(['git', '-C', str(case.project), '-c', 'commit.gpgsign=false',
                        '-c', 'core.hooksPath=' + os.devnull, '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty',
                        '-qm', 'fixture'], check=True, capture_output=True, timeout=10)
        checkout = importlib.import_module('laya_checkout_view')
        blobs = importlib.import_module('laya_blob_store')
        views = importlib.import_module('laya_view_store')
        migration = importlib.import_module('laya_shadow_migration')
        readers = importlib.import_module('laya_reader_open')
        incremental = importlib.import_module('laya_incremental_docs')
        import laya_ledger_common as ledger
        repository = checkout.register_repository(str(case.project))
        worktree = checkout.register_worktree(str(case.project), repository)
        candidate = case.state / 'receipt-shadow.sqlite'
        blobs.BlobStore.create(candidate, project_root=str(case.project), repository=repository,
                               worktree=worktree, scope_id=case.route['scopeId']).close()
        store = views.ViewStore.open(candidate, repository=repository, scope_id=case.route['scopeId'])
        self.addCleanup(store.close)
        time.sleep(1.1)
        intent_path = case.state / 'shadow-migration-intent.json'
        self.assertEqual(len(seed.overlap_sidecar_rows), 2)
        sealing, overlap = seed.overlap_sidecar_rows

        def step():
            return migration.advance_shadow(store, project=case.project,
                                            executing_root=case.plugin, deadline=time.monotonic() + 60)

        def exclude_with_fresh_writer(path):
            normal.plan = normal.P.capture_writer_plan(case.project, executing_root=case.plugin)
            with ExitStack() as owners:
                writer, _ = normal.admission(owners)
                with normal.runtime(writer) as runtime:
                    normal.N.mark_new_write(writer)
                    return runtime.index.exclude(writer.scope_id, path=path)

        def publish_sidecar_row(row):
            normal.plan = normal.P.capture_writer_plan(case.project, executing_root=case.plugin)
            with ExitStack() as owners:
                writer, _ = normal.admission(owners)
                runtime = owners.enter_context(importlib.import_module('laya_index_runtime').LayaIndexRuntime(
                    source_root={case.WIKI: str(case.auto / 'wiki')}, scope=case.route['scopeId'],
                    db_path=str(case.db), queue_path=str(normal.queue),
                    db_incarnation=case.route['dbIncarnation'], catalog_path=str(case.catalog),
                    binding_store_path=str(case.store), wiki_only=True,
                    connection_windows={role: normal.component(writer, role)
                                        for role in ('base', 'queue', 'catalog', 'binding')}))
                component = normal.component(writer, 'sidecar')
                identity = importlib.import_module('laya_initial_build').read(case.state)['identity']
                catalog = runtime.catalog.require_complete()
                normal.N.mark_new_write(writer)
                return incremental.publish_document(runtime, identity, catalog, *row,
                    require_verified=False, coordination=component)['status']

        def source_now():
            with seed.lease.resolve_endpoint(case.project, action='recall', host_root=case.plugin) as endpoint:
                decision = seed.reader.journal.select_route(str(case.project), action='recall')
                with readers.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 10) as snapshot:
                    live = importlib.import_module('laya_v6_reader').query_v6_snapshot(
                        snapshot, endpoint=endpoint, prompt='heron', decision=decision,
                        provider=None, deadline=time.monotonic() + 10)
                    return migration._source_revision(snapshot), [row['path'] for row in live.rows]

        def run_until(predicate, limit=40):
            stages = []
            for _ in range(limit):
                result = step()
                stages.append(result['stage'])
                if predicate(result):
                    return result, stages
            self.fail(stages)

        def corpora_complete(result):
            corpora = result['intent'].get('corpora')
            return bool(result['intent'].get('verification') and result['intent']['verification']['complete']
                        and corpora and corpora['base'] and corpora['base']['complete']
                        and corpora['sidecar'] and corpora['sidecar']['complete'])

        noise = iter(range(100))
        with seed.authority(), mock.patch.object(migration, 'MAX_BATCH', 1):
            # Both stores carry writer receipts before the copy starts.
            self.assertEqual(publish_sidecar_row(sealing), 'published')
            self.assertEqual(exclude_with_fresh_writer(
                case.WIKI + '/bench/u2-noise-%02d.md' % next(noise)), 'excluded')
            started = migration.begin_migration(store, worktree_id=worktree['worktreeId'],
                project=case.project, executing_root=case.plugin, deadline=time.monotonic() + 30)
            self.assertEqual(started['source_revision']['sidecar_kind'], 'sealed')
            self.assertIsNotNone(started['source_revision']['base_revision'])
            # Copy past the sidecar path that the writer is about to publish.
            copied, _ = run_until(lambda result: result['intent']['cursor'] is not None
                                  and result['intent']['cursor']['after'] >= overlap[0])
            cursor = copied['intent']['cursor']['after']
            # Two continuous NormalWriter revisions: one sidecar row and one
            # irrelevant base exclusion inside the wiki prefix.
            self.assertEqual(publish_sidecar_row(overlap), 'published')
            self.assertEqual(exclude_with_fresh_writer(
                case.WIKI + '/bench/u2-noise-%02d.md' % next(noise)), 'excluded')
            current, live_paths = source_now()
            self.assertEqual(current['base_revision'], started['source_revision']['base_revision'] + 1)
            self.assertEqual(current['sidecar_revision'], started['source_revision']['sidecar_revision'] + 1)

            # Condition 3: the old publications succeeded, the candidate write
            # fails. Old search stays selected; nothing records a success.
            before = intent_path.read_bytes()
            original_session = store._session
            def busy_session(*args, **kwargs):
                if kwargs.get('write'):
                    raise sqlite3.OperationalError('database is locked')
                return original_session(*args, **kwargs)
            with mock.patch.object(store, '_session', side_effect=busy_session):
                with self.assertRaisesRegex(Exception, 'database is locked|candidate_busy'):
                    step()
            self.assertEqual(intent_path.read_bytes(), before)
            self.assertEqual(seed.selection.preferred_endpoint, started['endpoint_id'])
            self.assertEqual(source_now(), (current, live_paths))

            # Condition 1: the receipt catch-up keeps the copy cursor and scan,
            # stages only the changed path and binds both revisions.
            caught = step()
            self.assertEqual(caught['stage'], 'copy')
            intent = caught['intent']
            self.assertEqual(intent['phase'], 'copying')
            self.assertEqual(intent['cursor']['after'], cursor)
            self.assertEqual(intent['cursor']['observation'], intent['observation'])
            self.assertNotEqual(intent['observation'], copied['intent']['observation'])
            self.assertEqual(intent['provenance_scan'], started['provenance_scan'])
            self.assertEqual(intent['source_revision'], current)
            self.assertEqual(intent['revision_check'], {'base': 'continuous', 'sidecar': 'advanced'})
            receipt = intent['catch_up']
            self.assertEqual((receipt['migration_id'], receipt['from'], receipt['to'], receipt['catch_ups']),
                             (started['migration_id'], started['source_revision'], current, 1))
            self.assertEqual(receipt['paths'], 1)
            with store._session() as db:
                self.assertIsNotNone(db.execute('SELECT 1 FROM wiki_blobs WHERE blob_sha=?',
                                                (overlap[1],)).fetchone())

            # Verification and both corpora complete under the new observation.
            done, stages = run_until(corpora_complete)
            self.assertNotIn('restart', stages)
            verification = done['intent']['verification']
            corpora = done['intent']['corpora']
            members = verification['rows']
            self.assertGreaterEqual(members, 3)
            self.assertEqual(verification['pages'], members)

            # Two more revisions after verification: exclude the last effective
            # member and one irrelevant path. Only the final pages re-verify.
            with seed.lease.resolve_endpoint(case.project, action='recall', host_root=case.plugin) as endpoint:
                decision = seed.reader.journal.select_route(str(case.project), action='recall')
                with readers.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 10) as snapshot:
                    effective = [row['path'] for row in migration.capture_effective(
                        snapshot, endpoint=endpoint, decision=decision, after=None,
                        deadline=time.monotonic() + 10, _all_inventory=True,
                        include_ineligible=True)['members'] if row['eligible'] == 1 and not row['excluded']]
            self.assertEqual(len(effective), members)
            last = effective[-1]
            self.assertEqual(exclude_with_fresh_writer(last), 'excluded')
            self.assertEqual(exclude_with_fresh_writer(
                case.WIKI + '/bench/u2-noise-%02d.md' % next(noise)), 'excluded')
            pending = step()
            self.assertEqual(pending['stage'], 'restart_pending')
            pending_bytes = intent_path.read_bytes()

            # Condition 4: a SIGKILL right before the receipt write; the rerun
            # writes exactly the intent the killed step had computed.
            payload = case.home / 'synthetic-shadow-receipt.json'
            payload.write_text(json.dumps({'home': str(case.home),
                'runtime': os.environ['MEMVANE_TEST_RUNTIME_ROOT'], 'plugin': str(case.plugin),
                'proof': case.proof, 'candidate': str(candidate), 'repository': repository,
                'worktree': worktree, 'scope': case.route['scopeId'], 'project': str(case.project),
                'batch_limit': 1, 'mode': 'catchup-kill'}), encoding='utf8')
            killed = subprocess.run([sys.executable, '-B', str(HERE / 'laya-shadow-crash-child.py'),
                                     str(payload)], capture_output=True, text=True, timeout=90)
            self.assertEqual(killed.returncode, -9, killed.stdout + killed.stderr)
            marker = [line for line in killed.stdout.splitlines() if line.startswith('catchup-before-intent ')]
            self.assertEqual(len(marker), 1, killed.stdout)
            self.assertEqual(intent_path.read_bytes(), pending_bytes)
            restarted = step()
            self.assertEqual(restarted['stage'], 'restart')
            self.assertEqual(restarted['intent'], json.loads(marker[0].split(' ', 1)[1]))
            intent = restarted['intent']
            self.assertEqual(intent['phase'], 'copied')
            self.assertEqual(intent['provenance_scan'], started['provenance_scan'])
            self.assertEqual(intent['catch_up']['from'], started['source_revision'])
            self.assertEqual(intent['catch_up']['catch_ups'], 2)
            # Exclusions never change the FTS corpora; their commitments stay.
            for key in ('base', 'sidecar'):
                self.assertTrue(intent['corpora'][key]['complete'])
                self.assertEqual(intent['corpora'][key]['digest'], corpora[key]['digest'])
            kept = intent['verification']
            self.assertFalse(kept['complete'])
            self.assertEqual(kept['pages'], members - 2)
            self.assertEqual(kept['marks'], verification['marks'][:members - 2])
            done, stages = run_until(corpora_complete)
            self.assertEqual(stages.count('verification'), 1)
            self.assertEqual(done['intent']['verification']['rows'], members - 1)

            # A writer admission racing endpoint resolution is a bounded retry.
            raced = importlib.import_module('laya_install_journal').JournalEvidenceError('reader_store_changed')
            before = intent_path.read_bytes()
            with mock.patch.object(migration.leases, 'resolve_endpoint', side_effect=raced):
                retried = step()
            self.assertEqual(retried['stage'], 'retry')
            self.assertEqual(intent_path.read_bytes(), before)

            # Condition 2: a missing intermediate receipt still forces the full
            # recopy, before any candidate slot exists for the kept scan.
            gap_path = next(path for path in effective[:-1] if path != overlap[0])
            prior = done['intent']['source_revision']['base_revision']
            self.assertEqual(exclude_with_fresh_writer(gap_path), 'excluded')
            self.assertEqual(exclude_with_fresh_writer(
                case.WIKI + '/bench/u2-noise-%02d.md' % next(noise)), 'excluded')
            key = 'reader-revision:base:' + f'{prior + 1:020d}'
            with sqlite3.connect(case.db) as db:
                trigger = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' "
                    "AND name='schema_meta_delete_seal'").fetchone()[0]
                db.execute('DROP TRIGGER schema_meta_delete_seal')
                self.assertEqual(db.execute('DELETE FROM schema_meta WHERE key=?', (key,)).rowcount, 1)
                db.execute(trigger)
            self.assertEqual(store._db.execute('SELECT COUNT(*) FROM generation_slots').fetchone()[0], 0)
            pending = step()
            self.assertEqual(pending['stage'], 'restart_pending')
            full = step()
            self.assertEqual(full['stage'], 'restart')
            self.assertEqual(full['intent']['revision_check']['base'], 'gap')
            self.assertEqual(full['intent']['phase'], 'copying')
            self.assertIsNone(full['intent']['cursor'])
            self.assertIsNone(full['intent']['verification'])
            self.assertIsNone(full['intent']['catch_up'])
            self.assertNotEqual(full['intent']['provenance_scan'], started['provenance_scan'])

            # The candidate build recomputes every commitment from the live
            # source; the private head records the receipt it was proved at.
            caught_up, stages = run_until(lambda result: result['migration_state'] == 'caught_up', limit=80)
            self.assertEqual(stages[-2:], ['prepare', 'publish'])
            again = step()
            self.assertEqual(again['stage'], 'parity')
            receipt = again['intent']['published']
            current, live_paths = source_now()
            self.assertEqual(receipt['source_revision'], current)
            self.assertEqual(receipt['migration_id'], started['migration_id'])
            with store._session() as db:
                paths = [row[0] for row in db.execute('SELECT path FROM wiki_members WHERE gen_id=?',
                                                      (receipt['generation'],))]
            self.assertNotIn(last.split('/', 1)[1], paths)
            self.assertNotIn(gap_path.split('/', 1)[1], paths)
            self.assertIn(overlap[0].split('/', 1)[1], paths)
            self.assertEqual(seed.selection.preferred_endpoint, started['endpoint_id'])
            self.assertTrue(store.verify_views())

    def test_prepare_reuses_committed_pages_while_writers_continue(self):
        normal_spec = importlib.util.spec_from_file_location(
            'shadow_prepare_reuse_seed', HERE / 'laya-normal-writer-connections-checks.py')
        normal_module = importlib.util.module_from_spec(normal_spec)
        normal_spec.loader.exec_module(normal_module)
        normal = normal_module.NormalWriterConnectionChecks('runTest')
        seed_module = normal_module
        for _ in range(6):
            seed_module = seed_module.existing
        def remove_completed_row(seed):
            # One completed row leaves the sidecar so a publication seals it
            # and both stores carry writer receipts before the copy starts.
            with sqlite3.connect(seed.sidecar) as db:
                seed.overlap_sidecar_rows = db.execute(
                    'SELECT path,hash,title,body,profile_id FROM docs ORDER BY path LIMIT 1').fetchall()
                for row in seed.overlap_sidecar_rows:
                    db.execute('DELETE FROM docs_fts WHERE path=?', (row[0],))
                    db.execute('DELETE FROM docs WHERE path=?', (row[0],))
        with mock.patch.object(seed_module.MoveSealChecks, 'before_conversion',
                               new=remove_completed_row, create=True):
            normal.setUp()
        self.addCleanup(normal.doCleanups)
        seed, case = normal.seed, normal.case
        subprocess.run(['git', '-c', 'init.defaultBranch=main', 'init', '-q', str(case.project)],
                       check=True, capture_output=True, timeout=10)
        subprocess.run(['git', '-C', str(case.project), '-c', 'commit.gpgsign=false',
                        '-c', 'core.hooksPath=' + os.devnull, '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty',
                        '-qm', 'fixture'], check=True, capture_output=True, timeout=10)
        checkout = importlib.import_module('laya_checkout_view')
        blobs = importlib.import_module('laya_blob_store')
        views = importlib.import_module('laya_view_store')
        migration = importlib.import_module('laya_shadow_migration')
        readers = importlib.import_module('laya_reader_open')
        incremental = importlib.import_module('laya_incremental_docs')
        import laya_ledger_common as ledger
        import laya_fresh_wiki_origin as fresh
        import project_dir
        from laya_product_install import _install_lock
        repository = checkout.register_repository(str(case.project))
        worktree = checkout.register_worktree(str(case.project), repository)
        candidate = case.state / 'prepare-reuse-shadow.sqlite'
        blobs.BlobStore.create(candidate, project_root=str(case.project), repository=repository,
                               worktree=worktree, scope_id=case.route['scopeId']).close()
        store = views.ViewStore.open(candidate, repository=repository, scope_id=case.route['scopeId'])
        self.addCleanup(store.close)
        time.sleep(1.1)
        intent_path = case.state / 'shadow-migration-intent.json'
        marker = project_dir.runtime_file(case.project, fresh.MARKER)
        (sealing,) = seed.overlap_sidecar_rows

        def step():
            return migration.advance_shadow(store, project=case.project,
                                            executing_root=case.plugin, deadline=time.monotonic() + 60)

        def exclude_with_fresh_writer(path):
            normal.plan = normal.P.capture_writer_plan(case.project, executing_root=case.plugin)
            with ExitStack() as owners:
                writer, _ = normal.admission(owners)
                with normal.runtime(writer) as runtime:
                    normal.N.mark_new_write(writer)
                    return runtime.index.exclude(writer.scope_id, path=path)

        def publish_sidecar_row(row):
            normal.plan = normal.P.capture_writer_plan(case.project, executing_root=case.plugin)
            with ExitStack() as owners:
                writer, _ = normal.admission(owners)
                runtime = owners.enter_context(importlib.import_module('laya_index_runtime').LayaIndexRuntime(
                    source_root={case.WIKI: str(case.auto / 'wiki')}, scope=case.route['scopeId'],
                    db_path=str(case.db), queue_path=str(normal.queue),
                    db_incarnation=case.route['dbIncarnation'], catalog_path=str(case.catalog),
                    binding_store_path=str(case.store), wiki_only=True,
                    connection_windows={role: normal.component(writer, role)
                                        for role in ('base', 'queue', 'catalog', 'binding')}))
                component = normal.component(writer, 'sidecar')
                identity = importlib.import_module('laya_initial_build').read(case.state)['identity']
                catalog = runtime.catalog.require_complete()
                normal.N.mark_new_write(writer)
                return incremental.publish_document(runtime, identity, catalog, *row,
                    require_verified=False, coordination=component)['status']

        def source_now():
            with seed.lease.resolve_endpoint(case.project, action='recall', host_root=case.plugin) as endpoint:
                decision = seed.reader.journal.select_route(str(case.project), action='recall')
                with readers.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 10) as snapshot:
                    observed = migration.capture_effective(snapshot, endpoint=endpoint, decision=decision,
                        after=None, deadline=time.monotonic() + 10)['observation']
                    effective = [row['path'] for row in migration.capture_effective(
                        snapshot, endpoint=endpoint, decision=decision, after=None,
                        deadline=time.monotonic() + 10, _all_inventory=True,
                        include_ineligible=True)['members'] if row['eligible'] == 1 and not row['excluded']]
                    return observed, migration._source_revision(snapshot), effective

        def run_until(predicate, limit=80):
            stages = []
            for _ in range(limit):
                result = step()
                stages.append(result['stage'])
                if predicate(result):
                    return result, stages
            self.fail(stages)

        def corpora_complete(result):
            corpora = result['intent'].get('corpora')
            return bool(result['intent'].get('verification') and result['intent']['verification']['complete']
                        and corpora and corpora['base'] and corpora['base']['complete']
                        and corpora['sidecar'] and corpora['sidecar']['complete'])

        def write_intent(value):
            with _install_lock(case.state), ledger._operation_lock(marker):
                ledger._write(intent_path, value, replace=True)

        def slots():
            return store._db.execute('SELECT COUNT(*) FROM generation_slots').fetchone()[0]

        # Members that a prepare call re-verifies against old wiki bodies.
        prepare_state = {'active': False, 'verified': [], 'calls': 0}
        original_batch = migration.import_wiki_batch
        def counting_batch(*args, **kwargs):
            page = original_batch(*args, **kwargs)
            if prepare_state['active']:
                prepare_state['verified'].append(len(page['inventory']) + len(page['corpus']))
            return page
        original_prepare = migration.prepare_shadow_generation
        def tracked_prepare(*args, **kwargs):
            prepare_state['active'] = True
            prepare_state['calls'] += 1
            try:
                return original_prepare(*args, **kwargs)
            finally:
                prepare_state['active'] = False

        with seed.authority(), mock.patch.object(migration, 'MAX_BATCH', 1):
            self.assertEqual(publish_sidecar_row(sealing), 'published')
            self.assertEqual(exclude_with_fresh_writer(case.WIKI + '/bench/u2b-noise-00.md'), 'excluded')
            started = migration.begin_migration(store, worktree_id=worktree['worktreeId'],
                project=case.project, executing_root=case.plugin, deadline=time.monotonic() + 30)
            ready, _ = run_until(corpora_complete)
            committed = ready['intent']
            members = committed['verification']['rows']
            self.assertGreater(members, 2)
            observation, revision, effective = source_now()
            self.assertEqual(observation, committed['observation'])
            self.assertEqual(len(effective), members)

            # A reused chain that differs from the pinned source refuses: the
            # source changes, then the intent is rebound to it without a catch-up.
            first_change = effective[-1]
            self.assertEqual(exclude_with_fresh_writer(first_change), 'excluded')
            changed, changed_revision, _ = source_now()
            self.assertNotEqual(changed, committed['observation'])
            def rebind(progress, key=None):
                cursor_observation = changed if key is None else migration._corpus_observation(key, changed)
                return {**progress, 'observation': changed,
                        'cursor': None if progress['cursor'] is None else {
                            'after': progress['cursor']['after'], 'observation': cursor_observation}}
            forged = {**committed, 'observation': changed, 'source_revision': changed_revision,
                      'verification': rebind(committed['verification']),
                      'corpora': {**committed['corpora'], 'base': rebind(committed['corpora']['base'], 'base'),
                                  'sidecar': rebind(committed['corpora']['sidecar'], 'sidecar')}}
            before_counts = store.counts()
            write_intent(forged)
            try:
                with self.assertRaisesRegex(Exception, 'migration_commitment_mismatch'):
                    migration.prepare_shadow_generation(store, project=case.project,
                        executing_root=case.plugin, deadline=time.monotonic() + 30)
            finally:
                write_intent(committed)
            self.assertEqual((store.counts(), slots()), (before_counts, 0))

            # The unchanged chain against the moved source refuses before any
            # candidate body is read.
            with mock.patch.object(store, '_blob', wraps=store._blob) as bodies:
                with self.assertRaisesRegex(Exception, 'migration_snapshot_changed'):
                    migration.prepare_shadow_generation(store, project=case.project,
                        executing_root=case.plugin, deadline=time.monotonic() + 30)
            self.assertEqual(bodies.call_count, 0)
            self.assertEqual((store.counts(), slots()), (before_counts, 0))

            # One writer commit lands inside the next prepare, another right
            # after the following build sealed its slot but before publish.
            # Both are caught up from receipts: no full recopy, no new scan.
            second_change = effective[-2]
            injected = []
            original_build = store.prepare_generation
            def build_with_writer(*args, **kwargs):
                if not injected:
                    injected.append(exclude_with_fresh_writer(second_change))
                    return original_build(*args, **kwargs)
                result = original_build(*args, **kwargs)
                if len(injected) == 1:
                    self.assertEqual(slots(), 1)
                    injected.append(exclude_with_fresh_writer(case.WIKI + '/bench/u2b-noise-01.md'))
                return result
            with mock.patch.object(migration, 'import_wiki_batch', side_effect=counting_batch), \
                 mock.patch.object(migration, 'prepare_shadow_generation', side_effect=tracked_prepare), \
                 mock.patch.object(store, 'prepare_generation', side_effect=build_with_writer):
                caught_up, stages = run_until(lambda result: result['migration_state'] == 'caught_up')
            self.assertEqual(injected, ['excluded', 'excluded'])
            self.assertGreaterEqual(prepare_state['calls'], 3)
            self.assertEqual(stages.count('restart_pending'), 2)
            # Full recopies: none; the stale slot stays private beside the new one.
            self.assertNotIn('copy', stages)
            self.assertEqual(slots(), 2)
            print('prepare_reverified_members', sum(prepare_state['verified']), 'members', members,
                  'prepare_calls', prepare_state['calls'], flush=True)
            self.assertLessEqual(sum(prepare_state['verified']), 2)
            intent = caught_up['intent']
            self.assertEqual(intent['provenance_scan'], started['provenance_scan'])
            self.assertEqual(intent['catch_up']['from'], started['source_revision'])
            self.assertGreaterEqual(intent['catch_up']['paths'], 2)
            observation, revision, effective = source_now()
            self.assertEqual((intent['observation'], intent['source_revision']), (observation, revision))
            with store._session() as db:
                paths = [row[0] for row in db.execute('SELECT path FROM wiki_members WHERE gen_id=?',
                                                      (intent['published']['generation'],))]
            self.assertEqual(sorted(paths), sorted(path.split('/', 1)[1] for path in effective))
            self.assertNotIn(first_change.split('/', 1)[1], paths)
            self.assertNotIn(second_change.split('/', 1)[1], paths)
            self.assertEqual(seed.selection.preferred_endpoint, started['endpoint_id'])
            self.assertTrue(store.verify_views())

if __name__ == '__main__':
    unittest.main()

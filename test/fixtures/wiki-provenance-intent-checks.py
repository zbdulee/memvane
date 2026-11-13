#!/usr/bin/env python3
"""Compile provenance from a verified card on an actual selected schema3 store."""
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    'provenance_schema3_seed', ROOT / 'test/fixtures/laya-writer-schema3-plan-checks.py')
seed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed)


class IntentAckChecks(unittest.TestCase):
    def test_replay_rejects_symlinked_card_before_intent_ack(self):
        verify = importlib.import_module('memvane.wiki.verify.wiki_verify_worker')
        reader = importlib.import_module('memvane.journal.journal_reader')
        ledger = importlib.import_module('laya_ledger_common')
        with tempfile.TemporaryDirectory(dir=str(Path.home())) as directory:
            root = Path(directory)
            wiki = root / 'wiki'; wiki.mkdir()
            state = root / 'state'; state.mkdir()
            intents = state / 'provenance-intents'; intents.mkdir(mode=0o700)
            foreign = root / 'foreign'; foreign.write_text('foreign card', encoding='utf8')
            pending = wiki / '.pending'; pending.symlink_to(foreign)
            spec = {'viewId': 'a' * 64, 'scanId': 'b' * 32}
            card_sha = hashlib.sha256(foreign.read_bytes()).hexdigest()
            result = {'viewId': spec['viewId'], 'targetPath': 'live.md', 'cardSha': card_sha,
                      'sourceFingerprints': spec}
            path = intents / ('compile-' + verify._compile_intent_id(result) + '.json')
            ledger._write(path, {'kind': 'compile', **result}, replace=False)
            with mock.patch.object(reader, '_state', return_value=state), \
                    self.assertRaises((OSError, ValueError, RuntimeError)):
                verify._supersede_job_intent(root, {'wikiPath': 'wiki'},
                    {'targetPath': 'wiki/live.md', 'sourceFingerprints': spec},
                    pending, {'journalPath': str(state / 'journal.sqlite')})
            self.assertTrue(path.exists())
            self.assertFalse(path.with_name(path.stem + '.superseded.json').exists())

    def test_existing_ack_is_never_overwritten(self):
        verify = importlib.import_module('memvane.wiki.verify.wiki_verify_worker')
        with tempfile.TemporaryDirectory(dir=str(Path.home())) as directory:
            path = Path(directory) / 'compile-a.json'
            ack = path.with_name('compile-a.superseded.json')
            path.write_text('new', encoding='utf8'); ack.write_text('old', encoding='utf8')
            path.chmod(0o600); ack.chmod(0o600)
            with self.assertRaises((OSError, ValueError, RuntimeError)):
                verify._supersede_compile_intent(path)
            self.assertEqual(path.read_text(encoding='utf8'), 'new')
            self.assertEqual(ack.read_text(encoding='utf8'), 'old')

    def test_replaced_source_path_is_never_acknowledged(self):
        verify = importlib.import_module('memvane.wiki.verify.wiki_verify_worker')
        with tempfile.TemporaryDirectory(dir=str(Path.home())) as directory:
            path = Path(directory) / 'compile-a.json'
            old = path.with_name('original.json')
            replacement = path.with_name('replacement.json')
            path.write_text('original', encoding='utf8')
            replacement.write_text('replacement', encoding='utf8')
            path.chmod(0o600); replacement.chmod(0o600)
            real_lstat = Path.lstat
            def swap(candidate, *args, **kwargs):
                if candidate == path:
                    os.replace(path, old); os.replace(replacement, path)
                return real_lstat(candidate, *args, **kwargs)
            with mock.patch.object(Path, 'lstat', new=swap), \
                    self.assertRaises((OSError, ValueError, RuntimeError)):
                verify._supersede_compile_intent(path)
            self.assertEqual(old.read_text(encoding='utf8'), 'original')
            self.assertEqual(path.read_text(encoding='utf8'), 'replacement')
            self.assertFalse(path.with_name('compile-a.superseded.json').exists())


class ProvenanceIntentChecks(seed.Schema3WriterPlanChecks):
    def test_reader_selection_change_during_issuance_is_rejected(self):
        selected = importlib.import_module('wiki_selected_laya')
        views = importlib.import_module('memvane.store.view_store')
        worker = importlib.import_module('laya_product_sync_worker')
        _, previous, _ = self.select_schema3()
        with self.F.warm(), self.seed.authority(), \
                mock.patch.object(worker, '_writer_executing_root', return_value=self.case.plugin):
            authority = selected.selected(self.case.project)
            actual = views.ViewStore._observe_worktree
            def switch_after_observation(store, *args, **kwargs):
                value = actual(store, *args, **kwargs)
                self.select(previous, '7' * 32)
                return value
            with mock.patch.object(views.ViewStore, '_observe_worktree', new=switch_after_observation):
                with self.assertRaises((ValueError, RuntimeError)):
                    selected.schema3_checkout(authority)

    def test_reader_selection_change_during_capture_is_rejected(self):
        selected = importlib.import_module('wiki_selected_laya')
        checkout_module = importlib.import_module('memvane.store.checkout_view')
        worker = importlib.import_module('laya_product_sync_worker')
        _, previous, _ = self.select_schema3()
        with self.F.warm(), self.seed.authority(), \
                mock.patch.object(worker, '_writer_executing_root', return_value=self.case.plugin):
            checkout = selected.schema3_checkout(selected.selected(self.case.project))
            actual_capture = checkout_module.capture_view
            def switch_after_capture(*args, **kwargs):
                value = actual_capture(*args, **kwargs)
                self.select(previous, '8' * 32)
                return value
            with mock.patch.object(checkout_module, 'capture_view', side_effect=switch_after_capture):
                with self.assertRaises((ValueError, RuntimeError)):
                    checkout.capture()

    def test_checkout_refuses_route_fence_after_issuance(self):
        selected = importlib.import_module('wiki_selected_laya')
        routes = importlib.import_module('laya_index_host_route')
        worker = importlib.import_module('laya_product_sync_worker')
        self.select_schema3()
        with self.F.warm(), self.seed.authority(), \
                mock.patch.object(worker, '_writer_executing_root', return_value=self.case.plugin):
            checkout = selected.schema3_checkout(selected.selected(self.case.project))
            capture = checkout.capture()
            with mock.patch.object(routes, 'select_route', return_value={'state': 'blocked'}):
                self.assertFalse(checkout.matches(capture))
                with self.assertRaises((ValueError, RuntimeError)):
                    checkout.capture()

    def test_checkout_rejects_forged_and_stale_route_authority(self):
        selected = importlib.import_module('wiki_selected_laya')
        worker = importlib.import_module('laya_product_sync_worker')
        _, previous, _ = self.select_schema3()
        with self.F.warm(), self.seed.authority(), \
                mock.patch.object(worker, '_writer_executing_root', return_value=self.case.plugin):
            bound = selected.selected(self.case.project)
            self.assertIsNotNone(bound)
            with self.assertRaises((ValueError, RuntimeError)):
                selected.schema3_checkout((bound[0], {}))
            checkout = selected.schema3_checkout(bound)
            capture = checkout.capture()
            self.select(previous, '9' * 32)
            self.assertFalse(checkout.matches(capture))
            with self.assertRaises((ValueError, RuntimeError)):
                checkout.capture()
            self.assertIsNone(selected.schema3_checkout(bound))

    def test_selected_checkout_tracks_full_registered_capture(self):
        selected = importlib.import_module('wiki_selected_laya')
        worker = importlib.import_module('laya_product_sync_worker')
        self.select_schema3()
        with self.F.warm(), self.seed.authority(), \
                mock.patch.object(worker, '_writer_executing_root', return_value=self.case.plugin):
            self.assertTrue(hasattr(selected, 'schema3_checkout'), 'selected checkout integration missing')
            authority = selected.selected(self.case.project)
            bound = selected.schema3_checkout(authority)
            capture = bound.capture()
            self.assertEqual(capture['worktreeId'], self.worktree['worktreeId'])
            self.assertEqual(capture['repoId'], self.repository['repoId'])
            self.assertTrue(bound.matches(capture))
            subprocess.run(['git', '-C', str(self.case.project), '-c', 'core.hooksPath=' + os.devnull,
                            'checkout', '-q', '-b', 'provenance-next'], check=True,
                           capture_output=True, timeout=30)
            self.assertFalse(bound.matches(capture))
            after = selected.schema3_checkout(authority).capture()
            self.assertEqual(after['branchKey'], 'refs/heads/provenance-next')

    def run_verify_case(self, *, malformed=False, branch=False, advance=False, fail_stamp=False,
                        crash=False, through_process=False, unavailable=False, sqlite_failure=False,
                        initial_sqlite=False):
        checkout = importlib.import_module('memvane.store.checkout_view')
        compiler = importlib.import_module('wiki_compile_worker')
        verify = importlib.import_module('memvane.wiki.verify.wiki_verify_worker')
        selected = importlib.import_module('wiki_selected_laya')
        worker = importlib.import_module('laya_product_sync_worker')
        locks = importlib.import_module('memvane.wiki.workspace.compile_paths')
        mutations = importlib.import_module('memvane.wiki.workspace.wiki_mutation_lock')
        freshness = importlib.import_module('memvane.wiki.card.wiki_freshness')
        yaml = importlib.import_module('memvane.wiki.card.yaml_scalars')
        project = self.case.project
        source = project / 'notes' / 'provenance-source.md'
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text('Synthetic source for verification.\n', encoding='utf8')
        revision, _ = freshness.snapshot_bytes(source)
        revision = {'kind': 'file', 'path': 'notes/provenance-source.md',
                    'collection': 'docs', **revision}
        capture = checkout.capture_view(str(project), self.repository, self.worktree, fresh=True)
        class BoundCheckout:
            def matches(self, value):
                return checkout.capture_matches(value, self_repository, self_worktree, fresh=True)
        self_repository, self_worktree = self.repository, self.worktree
        fingerprints = compiler.source_fingerprints(project, BoundCheckout(), capture, [revision], time.time_ns())
        fingerprints.update(scanId=fingerprints['fingerprints'][0]['scan_id'],
                            observedNs=fingerprints['fingerprints'][0]['observed_ns'])
        target = self.case.auto / 'wiki' / 'concepts' / 'intent.md'
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = ('---\ntitle: Intent\nstatus: generated\ncreatedBy: qmd-auto-context\n'
               'sourceRevisions:\n  - ' + yaml.dump_source_revisions([revision])[0] +
               '\n---\n\nSynthetic claim.\n')
        target.write_text(raw, encoding='utf8')
        job = {'targetPath': target.relative_to(project).as_posix(), 'engine': 'claude',
               'sources': [{'kind': 'file', 'path': revision['path'], 'collection': revision['collection']}],
               'sourceRevisions': [revision], 'sourceFingerprints': fingerprints}
        config = {'wikiPath': '.auto-context/wiki', 'collectionPaths': {'docs': 'notes'},
                  'collectionRoles': {'docs': 'raw'}}
        self.select_schema3()
        with self.F.warm(), self.seed.authority(), \
                mock.patch.object(worker, '_writer_executing_root', return_value=self.case.plugin), \
                mock.patch.object(verify, 'reindex_wiki'), \
                mock.patch.object(verify, 'wake_pending_retirement'):
            authority = selected.selected(project)
            observed = selected.schema3_checkout(authority)
            git = ['git', '-C', str(project), '-c', 'core.hooksPath=' + os.devnull,
                   '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid']
            if branch:
                subprocess.run(git + ['checkout', '-q', '-b', 'verify-next'], check=True,
                               capture_output=True, timeout=30)
            if advance:
                subprocess.run(git + ['commit', '-q', '--allow-empty', '-m', 'advance'], check=True,
                               capture_output=True, timeout=30)
            from contextlib import ExitStack
            with ExitStack() as patches:
                if unavailable:
                    error = sqlite3.OperationalError('fixture reader busy') if sqlite_failure else OSError('git unavailable')
                    patches.enter_context(mock.patch.object(observed, 'capture', side_effect=error))
                if fail_stamp:
                    patches.enter_context(mock.patch.object(verify.wc, 'write_rendered_verification',
                                                            return_value=False))
                if crash:
                    Path(os.environ['QMD_TEST_CRASH_MARKER']).write_text(json.dumps({
                        'target': str(target), 'state': str(self.case.state)}), encoding='utf8')
                    def kill_before_stamp(*_args, **_kwargs):
                        os.kill(os.getpid(), signal.SIGKILL)
                    patches.enter_context(mock.patch.object(verify.wc, 'write_rendered_verification',
                                                            side_effect=kill_before_stamp))
                claims = [{'claim': float('nan') if malformed else 'Synthetic claim'}]
                if through_process:
                    real_proof = selected._compile_proof
                    def outside_lock(*args, **kwargs):
                        if getattr(mutations._local, 'held', {}):
                            raise AssertionError('owner acquired inside wiki lock')
                        return real_proof(*args, **kwargs)
                    patches.enter_context(mock.patch.object(selected, '_compile_proof', side_effect=outside_lock))
                    if initial_sqlite:
                        patches.enter_context(mock.patch.object(selected, 'schema3_checkout',
                                              side_effect=sqlite3.OperationalError('fixture reader busy')))
                    patches.enter_context(mock.patch.object(verify, 'plan_verify_attempts', return_value=(
                        [{'engine': 'claude', 'argv': ['synthetic'], 'mode': 'self', 'key': 'claude'}],
                        'prefer', None)))
                    patches.enter_context(mock.patch.object(verify.wcw, 'run_extractor', return_value=(
                        {'verdict': 'pass', 'claims': claims, 'reasons': []}, None, 0)))
                    result = verify.process_verify_job(project, config, {}, {}, job, project / 'verify-log.jsonl')
                else:
                    patches.enter_context(locks.card_write_lock(project))
                    patches.enter_context(mutations.lock(project))
                    patches.enter_context(mock.patch.object(selected, '_compile_proof',
                                          side_effect=AssertionError('owner acquired inside wiki lock')))
                    result = verify.apply_verify_verdict_locked(
                        project, config, {}, {}, job, target, '', raw, 'claude', 'self', {},
                        'pass', claims, [], [], project / 'verify-log.jsonl',
                        spans=[], checkout=observed)
        self.assertEqual(result, (False, True) if fail_stamp else (True, False))
        intents = list((self.case.state / 'provenance-intents').glob('compile-*.json'))
        if fail_stamp:
            self.assertEqual(len(intents), 1)
            self.assertTrue(intents[0].name.endswith('.superseded.json'))
            self.assertIn('status: generated', target.read_text(encoding='utf8'))
            return
        if branch:
            self.assertEqual(intents, [])
            self.assertIn('status: verified', target.read_text(encoding='utf8'))
            log = (project / 'verify-log.jsonl').read_text(encoding='utf8')
            self.assertIn('provenance_checkout_changed', log)
            return
        if unavailable or initial_sqlite:
            self.assertEqual(intents, [])
            self.assertIn('status: verified', target.read_text(encoding='utf8'))
            self.assertIn('provenance_checkout_unavailable',
                          (project / 'verify-log.jsonl').read_text(encoding='utf8'))
            return
        if malformed:
            self.assertEqual(intents, [])
            self.assertIn('status: verified', target.read_text(encoding='utf8'))
            return
        self.assertEqual(len(intents), 1, 'verified schema3 card must have a durable compile intent: '
                         + (project / 'verify-log.jsonl').read_text(encoding='utf8'))
        intent = json.loads(intents[0].read_text(encoding='utf8'))
        self.assertIn(hashlib.sha256(target.read_bytes()).hexdigest(), intent.values())

    def test_verified_card_creates_intent_for_stamped_bytes(self):
        self.run_verify_case()

    def test_invalid_digest_input_keeps_existing_verify_and_creates_no_intent(self):
        self.run_verify_case(malformed=True)

    def test_actual_verify_process_creates_intent_without_owner_under_wiki_lock(self):
        self.run_verify_case(through_process=True)

    def test_unavailable_checkout_keeps_verification_without_intent(self):
        self.run_verify_case(unavailable=True)

    def test_reader_error_keeps_verification_without_intent(self):
        self.run_verify_case(unavailable=True, sqlite_failure=True)

    def test_reader_error_before_verify_lock_preserves_stamp(self):
        self.run_verify_case(through_process=True, initial_sqlite=True)

    def test_branch_change_skips_intent_without_blocking_stamp(self):
        self.run_verify_case(branch=True)

    def test_head_advance_on_same_branch_keeps_provenance(self):
        self.run_verify_case(advance=True)

    def test_stamp_failure_supersedes_the_intent(self):
        self.run_verify_case(fail_stamp=True)

    def test_crash_before_stamp_leaves_only_waiting_intent(self):
        self.run_verify_case(crash=True)

    def run_pending_replay(self, mode):
        checkout_module = importlib.import_module('memvane.store.checkout_view')
        compiler = importlib.import_module('wiki_compile_worker')
        verify = importlib.import_module('memvane.wiki.verify.wiki_verify_worker')
        selected = importlib.import_module('wiki_selected_laya')
        worker = importlib.import_module('laya_product_sync_worker')
        freshness = importlib.import_module('memvane.wiki.card.wiki_freshness')
        yaml = importlib.import_module('memvane.wiki.card.yaml_scalars')
        locks = importlib.import_module('memvane.wiki.workspace.compile_paths')
        mutations = importlib.import_module('memvane.wiki.workspace.wiki_mutation_lock')
        retire = importlib.import_module('memvane.wiki.lifecycle.wiki_source_retire')
        project = self.case.project
        source = project / 'notes' / 'replay-source.md'
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text('Synthetic replay source.\n', encoding='utf8')
        revision, _ = freshness.snapshot_bytes(source)
        revision = {'kind': 'file', 'path': 'notes/replay-source.md', 'collection': 'docs', **revision}
        capture = checkout_module.capture_view(str(project), self.repository, self.worktree, fresh=True)
        class BoundCheckout:
            def matches(self, value):
                return checkout_module.capture_matches(value, repository, worktree, fresh=True)
        repository, worktree = self.repository, self.worktree
        fingerprints = compiler.source_fingerprints(project, BoundCheckout(), capture, [revision], time.time_ns())
        fingerprints.update(scanId=fingerprints['fingerprints'][0]['scan_id'],
                            observedNs=fingerprints['fingerprints'][0]['observed_ns'])
        live = self.case.auto / 'wiki' / 'concepts' / 'replay.md'
        live.parent.mkdir(parents=True, exist_ok=True)
        live.write_text('previous live card\n', encoding='utf8')
        previous_hash = hashlib.sha256(live.read_bytes()).hexdigest()
        source_hash = 'a' * 16
        pending = live.with_name('.' + live.name + '.pending-' + source_hash)
        raw = ('---\ntitle: Replay\nstatus: generated\ncreatedBy: qmd-auto-context\n'
               'sourceRevisions:\n  - ' + yaml.dump_source_revisions([revision])[0] +
               '\n---\n<!-- qmd:auto:start id="main" sourceHash="' + source_hash + '" -->\n'
               'Synthetic claim.\n<!-- qmd:auto:end -->\n')
        pending.write_text(raw, encoding='utf8')
        job = {'targetPath': live.relative_to(project).as_posix(),
               'pendingPath': pending.relative_to(project).as_posix(), 'expectedTargetHash': previous_hash,
               'sourceHash': source_hash, 'engine': 'claude', 'sourceRevisions': [revision],
               'sourceFingerprints': fingerprints}
        config = {'wikiPath': '.auto-context/wiki', 'collectionPaths': {'docs': 'notes'},
                  'collectionRoles': {'docs': 'raw'}}
        self.select_schema3()
        with self.F.warm(), self.seed.authority(), \
                mock.patch.object(worker, '_writer_executing_root', return_value=self.case.plugin), \
                mock.patch.object(verify, 'reindex_wiki'), \
                mock.patch.object(verify, 'wake_pending_retirement'), \
                mock.patch.object(retire, 'archive_card', return_value=True):
            observed = selected.schema3_checkout(selected.selected(project))
            def interrupted_publish(*_args, **_kwargs):
                if mode == 'crash':
                    Path(os.environ['QMD_TEST_CRASH_MARKER']).write_text(json.dumps({
                        'target': str(pending), 'live': str(live), 'state': str(self.case.state)}),
                        encoding='utf8')
                    os.kill(os.getpid(), signal.SIGKILL)
                return 'retry'
            with locks.card_write_lock(project), mutations.lock(project), \
                    mock.patch.object(verify, 'publish_pending_replacement', side_effect=interrupted_publish):
                result = verify.apply_verify_verdict_locked(project, config, {}, {}, job, pending,
                    source_hash, raw, 'claude', 'self', {}, 'pass', [{'claim': 'Synthetic claim'}],
                    [], [], project / 'verify-log.jsonl', spans=[], checkout=observed)
            self.assertEqual(result, (False, True))
            if mode in ('live', 'same-live'):
                os.replace(pending, live)
            elif mode == 'stale':
                live.write_text('new live card\n', encoding='utf8')
            if mode in ('pending', 'live'):
                subprocess.run(['git', '-C', str(project), '-c', 'core.hooksPath=' + os.devnull,
                                'checkout', '-q', '-b', 'replay-next'], check=True,
                               capture_output=True, timeout=30)
            result = verify.process_verify_job(project, config, {}, {}, job, project / 'verify-log.jsonl')
            self.assertEqual(result, (True, False))
        files = list((self.case.state / 'provenance-intents').glob('compile-*.json'))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name.endswith('.superseded.json'),
                         mode not in ('same-pending', 'same-live'))
        if mode != 'stale':
            self.assertIn('status: verified', live.read_text(encoding='utf8'))
        else:
            self.assertEqual(live.read_text(encoding='utf8'), 'new live card\n')

    def test_stamped_pending_replay_after_branch_change_supersedes_intent(self):
        self.run_pending_replay('pending')

    def test_live_replay_after_branch_change_supersedes_intent(self):
        self.run_pending_replay('live')

    def test_stale_pending_replay_supersedes_intent(self):
        self.run_pending_replay('stale')

    def test_same_view_pending_replay_retains_intent(self):
        self.run_pending_replay('same-pending')

    def test_same_view_live_replay_retains_intent(self):
        self.run_pending_replay('same-live')

    def test_crash_after_stamp_before_publish(self):
        self.run_pending_replay('crash')


if __name__ == '__main__':
    unittest.main()

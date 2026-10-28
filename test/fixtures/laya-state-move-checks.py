#!/usr/bin/env python3
"""Install state root move from ``~/.local/state/laya`` to ``~/.local/state/memvane``.

A real-shaped selected install (route, marker, ledger, sqlite journal, Laya
index and claim queue, build record and pointer) lives under the pre-rename
state base of a temporary HOME. Only bundle identity, provider file identity
and store-count reads are patched.
"""
from __future__ import annotations

from contextlib import ExitStack, closing, nullcontext
import hashlib
import json
import os
from pathlib import Path
import pwd
import sqlite3
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from laya_selected_project import SelectedProject, canon  # noqa: E402

import laya_bundle_upgrade as upgrade  # noqa: E402
import laya_fresh_wiki_origin as fresh  # noqa: E402
import laya_host_response as response  # noqa: E402
import laya_inactive_upgrade as inactive  # noqa: E402
import laya_index_host_route as host_route  # noqa: E402
import laya_initial_build as build  # noqa: E402
import laya_install_journal as journal_mod  # noqa: E402

INSTALL = '3' * 32
COUNTS = {'vectorChunkSets': 1, 'vectorChunks': 1, 'publishedDocuments': 1}
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
REAL_STATE = [REAL_HOME / '.local' / 'state' / name for name in ('laya', 'memvane')]


def _mtimes():
    return {str(path): path.stat().st_mtime_ns if path.exists() else None
            for path in REAL_STATE}


BEFORE = _mtimes()


class Kill(BaseException):
    """A process death at one phase of the move."""


class MoveCase(SelectedProject):
    STATE_PARTS = ('.local', 'state', 'laya', INSTALL)
    PROVIDER_EXTRA = {'serviceSocketPath': '{state}/runtime/laya-embed.sock'}

    def setUp(self):
        super().setUp()
        self.legacy = self.home / '.local' / 'state' / 'laya'
        self.target = self.home / '.local' / 'state' / 'memvane' / INSTALL
        identity = {'projectRoot': str(self.project), 'stateRoot': str(self.state),
                    'installId': INSTALL, 'scopeId': '6' * 32,
                    'collections': self.collections(),
                    'candidateDb': str(self.db), 'queueDb': str(self.queue),
                    'catalogDb': str(self.state / 'catalog.sqlite'),
                    'bindingStore': str(self.state / 'binding.sqlite'),
                    'dbIncarnation': self.incarnation,
                    'journalPath': str(self.journal),
                    'routeFile': str(self.state / 'reviewed-route.json'),
                    'routeSha256': hashlib.sha256(self.route_raw).hexdigest(),
                    'attestationSha256': 'a' * 64,
                    'vectorProfileId': self.ledger['vectorProfileId'],
                    'proposalSha256': 'b' * 64, 'providerProposalSha256': 'c' * 64,
                    'originMode': 'fresh_wiki_only'}
        build.create_or_verify(identity)
        pointers = self.legacy / 'initial-builds'
        pointers.mkdir(mode=0o700)
        self.old_pointer = {'schema': build.POINTER_SCHEMA,
                            'projectRoot': str(self.project),
                            'stateRoot': str(self.state), 'installId': INSTALL}
        build._write_json(build.pointer_path(str(self.project), pointers),
                          self.old_pointer, replace=False)
        rows = [[str(self.plugin / 'p.py'), 1, 2, 3, 4, 5]]
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(journal_mod, '_provider_file_identity',
                                              return_value=rows))
        stack.enter_context(mock.patch.object(inactive, '_store_counts',
                                              side_effect=self._counts))
        # The bundle step is covered by its own suite; here it is current.
        stack.enter_context(mock.patch.object(upgrade, 'upgrade_install',
                                              return_value={'status': 'current'}))
        stack.enter_context(mock.patch.object(upgrade, '_convert_journal',
                                              return_value={}))
        # The real per-user provider lane lives under the account home.
        try:
            import laya_state_move
        except ImportError:  # a release without the move: nothing takes the lane
            laya_state_move = None
        if laya_state_move is not None:
            stack.enter_context(mock.patch.object(laya_state_move, '_provider_lane',
                                                  lambda: nullcontext()))
        # The real hook-side request: only the detached spawn is recorded.
        self.spawned = []
        stack.enter_context(mock.patch.object(upgrade, '_fixture', return_value=False))
        stack.enter_context(mock.patch.object(
            upgrade, '_spawn', side_effect=lambda project: self.spawned.append(project)))

    fail_new_counts = False

    def _counts(self, identity):
        for key in ('candidateDb', 'queueDb', 'catalogDb', 'bindingStore'):
            if not Path(identity[key]).is_file():
                raise RuntimeError('store_missing')
        if self.fail_new_counts and identity['stateRoot'] == str(self.target):
            return {**COUNTS, 'vectorChunks': 2}
        return dict(COUNTS)

    def run_worker(self):
        # The executing bundle, as the SessionStart worker runs it.
        return upgrade.run(str(self.project))

    def assert_moved(self):
        self.assertFalse(os.path.lexists(self.state))
        decision = self.decision()
        self.assertEqual(decision['state'], 'candidate', decision)
        for key, name in (('journalPath', 'install-journal.sqlite'),
                          ('dbPath', 'index.sqlite'), ('queuePath', 'claims.sqlite')):
            self.assertEqual(decision[key], str(self.target / name))
        proof = journal_mod.verify_selected_route_read_only(decision)
        self.assertTrue(proof['readOnlyAllowed'])
        record = build.lookup(str(self.project))
        self.assertEqual(record['identity']['stateRoot'], str(self.target))
        self.assertEqual(record['identity']['candidateDb'], str(self.target / 'index.sqlite'))
        self.assertFalse((self.legacy / 'initial-builds' /
                          build.pointer_path(str(self.project), self.legacy).name).exists())
        attestation = json.loads((self.target / journal_mod.PRODUCTION_ATTESTATION)
                                 .read_bytes())
        self.assertEqual(attestation['journalPath'], str(self.target / 'install-journal.sqlite'))
        ledger = fresh._load(self.target / fresh.LEDGER)
        self.assertEqual(ledger['candidateIdentity'], self.ledger['candidateIdentity'])
        self.assertEqual(ledger['phase'], 'active')
        journal = journal_mod.inspect_journal(self.target / 'install-journal.sqlite')
        self.assertIsNone(journal['previousRouteSha256'])
        self.assertFalse((self.auto / 'memvane-state-move.json').exists())
        self.assertEqual([name for name in os.listdir(self.target)
                          if name == 'state-move-intent.json'], [])
        return decision

    def serve_recall(self, decision):
        served = []

        def fake(host, chosen, payload, proof, recheck, *, degraded=False, **_):
            self.assertEqual(recheck(chosen), proof)
            with closing(sqlite3.connect(Path(chosen['dbPath']).as_uri() + '?mode=ro',
                                         uri=True)) as db:
                db.execute('SELECT count(*) FROM schema_meta').fetchone()
            served.append(chosen['dbPath'])
            return {'served': True}
        with mock.patch.object(response, '_serve_recall', fake):
            out = response.serve_selected('recall', 'claude', decision,
                                          {'prompt': 'x', 'cwd': str(self.project)})
        return out, served


class WorkerMoveTests(MoveCase):
    def test_worker_moves_legacy_install_while_recall_keeps_serving(self):
        failures, served = [], []
        done = threading.Event()

        def reader():
            while not done.is_set():
                decision = host_route.select_route_settled(str(self.project),
                                                           action='recall')
                if decision['state'] != 'candidate':
                    failures.append(decision.get('reason'))
                    continue
                try:
                    _out, paths = self.serve_recall(decision)
                    served.extend(paths)
                except Exception as exc:  # noqa: BLE001
                    failures.append(repr(exc))
        thread = threading.Thread(target=reader)
        thread.start()
        try:
            time.sleep(0.2)
            results = self.run_worker()
            time.sleep(0.2)
        finally:
            done.set()
            thread.join()
        self.assertEqual(failures, [])
        self.assertTrue(served)
        self.assertEqual(results[0]['status'], 'state_move_complete', results)
        self.assert_moved()
        self.assertEqual(served[-1], str(self.target / 'index.sqlite'))
        # Nothing else was left under the pre-rename base: it is gone.
        self.assertFalse(self.legacy.exists())

    def test_moved_install_is_current_on_the_next_run(self):
        self.run_worker()
        self.assert_moved()
        again = self.run_worker()
        self.assertEqual([row['stateRoot'] for row in again], [str(self.target)])
        self.assertEqual(again[0]['status'], 'current', again)


class NoticeTests(MoveCase):
    def test_session_start_names_a_pending_move_until_it_is_done(self):
        decision = self.decision()
        line = response._state_move_notice(decision)
        self.assertIn('~/.local/state/memvane', line)
        self.assertIn('search keeps serving', line)
        self.assertEqual(self.spawned, [str(self.project)])
        self.run_worker()
        self.assertIsNone(response._state_move_notice(self.decision()))

    def test_a_refused_move_names_its_reason(self):
        upgrade._write_status(self.state, status='refused', executingRoot='/x',
                              attempts=1, reason='state_move_upgrade_pending')
        with mock.patch.object(upgrade, 'request',
                               side_effect=lambda *a, **k: upgrade.read_status(self.state)):
            line = response._state_move_notice(self.decision())
        self.assertIn('needs review (state_move_upgrade_pending)', line)


class WindowTests(MoveCase):
    def test_reader_inside_the_rename_window_waits_for_the_moved_route(self):
        import laya_state_move as move
        failures, results, inside = [], [], threading.Event()

        def pause(name):
            if name in ('moved', 'journal'):
                inside.set()
                time.sleep(0.4)

        def reader():
            inside.wait(5)
            for _ in range(3):
                decision = host_route.select_route_settled(str(self.project),
                                                           action='recall')
                if decision['state'] != 'candidate':
                    failures.append(decision.get('reason'))
                    continue
                results.append(decision['journalPath'])
        thread = threading.Thread(target=reader)
        thread.start()
        with mock.patch.object(move, '_step', pause):
            self.run_worker()
        thread.join()
        self.assertEqual(failures, [])
        self.assertEqual(results[0], str(self.target / 'install-journal.sqlite'))
        self.assert_moved()

    def test_recall_proved_before_the_move_retries_at_the_new_root(self):
        before = self.decision()
        self.assertEqual(before['journalPath'], str(self.journal))
        self.run_worker()
        out, served = self.serve_recall(before)
        self.assertEqual(out, {'served': True})
        self.assertEqual(served, [str(self.target / 'index.sqlite')])

    def test_another_install_under_memvane_is_not_a_relocation(self):
        import laya_state_move as move
        before = self.decision()
        self.run_worker()
        after = self.decision()
        self.assertTrue(move.relocated_decision(before, after))
        self.assertFalse(move.relocated_decision(before, {**after, 'installId': '5' * 32}))
        other = {**after, 'journalPath': str(self.target.parent / ('5' * 32) /
                                              'install-journal.sqlite')}
        self.assertFalse(move.relocated_decision(before, other))


class CrashTests(MoveCase):
    def crash_at(self, phase):
        import laya_state_move as move

        def die(name):
            if name == phase:
                raise Kill(name)
        with mock.patch.object(move, '_step', die):
            with self.assertRaises(Kill):
                self.run_worker()

    def check_resume(self, phase):
        import laya_state_move as move
        self.crash_at(phase)
        self.assertTrue((self.auto / 'memvane-state-move.json').exists())
        with mock.patch.object(move, 'WAIT_SECONDS', 0.1):
            blocked = host_route.select_route_settled(str(self.project), action='recall')
        self.assertEqual(blocked['state'], 'blocked', blocked)
        self.assertEqual(blocked['reason'], 'state_root_move_pending')
        # The crashed worker left ``running``; its lock is free, so the very
        # next prompt starts a worker (no 30-minute running window).
        self.assertEqual(upgrade.read_status(self.target)['status'], 'running')
        self.assertEqual(self.spawned, [str(self.project)])
        results = self.run_worker()
        self.assertEqual(results[0]['stateRoot'], str(self.target), results)
        self.assertEqual(results[0]['status'], 'state_move_complete', results)
        self.assert_moved()
        receipts = [name for name in os.listdir(self.target)
                    if name.startswith('state-move-') and name.endswith('-settled.json')]
        self.assertEqual(len(receipts), 1)

    def test_crash_after_rename_resumes_from_the_pointer(self):
        self.check_resume('moved')

    def test_crash_after_record_rewrites_resumes(self):
        self.check_resume('records')

    def test_crash_after_journal_cas_resumes(self):
        self.check_resume('journal')

    def test_crash_after_route_write_resumes(self):
        self.check_resume('route')

    def test_crash_after_marker_write_settles_without_blocking_readers(self):
        self.crash_at('marker')
        self.assertTrue((self.auto / 'memvane-state-move.json').exists())
        started = time.monotonic()
        decision = host_route.select_route_settled(str(self.project), action='recall')
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(decision['state'], 'candidate')
        self.assertEqual(decision['journalPath'], str(self.target / 'install-journal.sqlite'))
        results = self.run_worker()
        self.assertEqual(results[0]['status'], 'state_move_complete', results)
        self.assert_moved()
        receipts = [name for name in os.listdir(self.target)
                    if name.startswith('state-move-') and name.endswith('-settled.json')]
        self.assertEqual(len(receipts), 1)

    def test_failure_after_rename_is_recorded_at_the_new_root_then_resumes(self):
        self.fail_new_counts = True
        results = self.run_worker()
        self.assertEqual(results[0]['status'], 'failed', results)
        self.assertEqual(results[0]['reason'], 'state_move_store_changed', results)
        status = upgrade.read_status(self.target)
        self.assertEqual((status['status'], status['reason']),
                         ('failed', 'state_move_store_changed'))
        self.fail_new_counts = False
        results = self.run_worker()
        self.assertEqual(results[0]['status'], 'state_move_complete', results)
        self.assert_moved()

    def test_crash_before_rename_leaves_recall_on_the_old_root_then_restarts(self):
        self.crash_at('prepared')
        decision = self.decision()
        self.assertEqual(decision['state'], 'candidate')
        self.assertEqual(decision['journalPath'], str(self.journal))
        self.assertTrue((self.state / 'state-move-intent.json').exists())
        results = self.run_worker()
        self.assertEqual(results[0]['status'], 'state_move_complete', results)
        self.assert_moved()


class JournalTests(MoveCase):
    def test_relocation_cas_refuses_a_route_that_changes_more_than_paths(self):
        import laya_state_move as move
        target = self.target
        target.parent.mkdir(mode=0o700)
        old_att = (self.state / journal_mod.PRODUCTION_ATTESTATION).read_bytes()
        os.rename(self.state, target)
        route = json.loads(self.route_raw)
        moved = {**route, **{key: str(target / Path(route[key]).name)
                             for key in move.ROUTE_KEYS}}
        (self.auto / host_route.RECORD).unlink()
        self.private_write(self.auto / host_route.RECORD, canon({**moved,
                                                               'generation': 'g-' + '9' * 32}))
        journal = journal_mod.inspect_journal(target / 'install-journal.sqlite')
        with self.assertRaisesRegex(Exception, 'relocation_route_mismatch'):
            journal_mod.cas_relocate_state_root(
                target / 'install-journal.sqlite', journal['epoch'], self.route_raw,
                canon({**moved, 'generation': 'g-' + '9' * 32}), old_att)


class SharedTreeTests(MoveCase):
    def setUp(self):
        super().setUp()
        self.runtime = self.legacy / 'embed-runtime'
        (self.runtime / 'generations' / ('g-' + 'e' * 32)).mkdir(parents=True, mode=0o700)
        (self.runtime / 'generations' / ('g-' + 'e' * 32) / 'node').write_bytes(b'x')
        (self.legacy / 'service').mkdir(mode=0o700)
        (self.legacy / 'service' / 'service.lock').write_bytes(b'')
        auto = self.legacy / 'auto-context'
        (auto / 'scan-state').mkdir(parents=True)
        (auto / 'scan-state' / 'cursor.json').write_text('{}')
        self.ps = []
        # The worker's own shared pass is exercised in WorkerMoveTests.
        patcher = mock.patch.object(upgrade, '_collect_shared', return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def collect(self):
        import laya_state_move as move
        return move.collect_shared(self.home, ps_all=lambda: list(self.ps))

    def test_runtime_stays_while_an_install_is_still_legacy(self):
        result = self.collect()
        row = next(item for item in result['trees'] if item['tree'] == 'embed-runtime')
        self.assertEqual(row['blockers'], ['legacy_install_present'])
        self.assertTrue(self.runtime.exists())

    def test_runtime_stays_while_an_attestation_or_process_names_it(self):
        self.run_worker()
        self.assert_moved()
        attestation = self.target / journal_mod.PRODUCTION_ATTESTATION
        record = json.loads(attestation.read_bytes())
        named = {**record, 'extra': str(self.runtime / 'generations' / ('g-' + 'e' * 32) /
                                        'node')}
        attestation.write_bytes(canon(named))
        result = self.collect()
        row = next(item for item in result['trees'] if item['tree'] == 'embed-runtime')
        self.assertIn('runtime_attested', row['blockers'])
        self.assertTrue(self.runtime.exists())
        attestation.write_bytes(canon(record))
        self.ps = [(4242, os.getuid(), str(self.runtime / 'generations' / ('g-' + 'e' * 32) /
                                           'node') + ' worker.mjs')]
        result = self.collect()
        row = next(item for item in result['trees'] if item['tree'] == 'embed-runtime')
        self.assertEqual(row['blockers'], ['runtime_in_use'])
        self.assertTrue(self.runtime.exists())

    def test_unreferenced_trees_are_removed_and_logs_move(self):
        self.run_worker()
        self.assert_moved()
        installed = self.home / '.claude' / 'plugins' / 'installed_plugins.json'
        installed.parent.mkdir(parents=True)
        installed.write_text(json.dumps({'plugins': {
            'qmd-auto-context@qmd-auto-context-marketplace': [{'installPath': '/x'}]}}))
        result = self.collect()
        self.assertFalse(self.runtime.exists())
        self.assertTrue((self.legacy / 'service').exists())
        service = next(item for item in result['trees'] if item['tree'] == 'service')
        self.assertEqual(service['blockers'], ['legacy_plugin_installed'])
        moved = self.home / '.local' / 'state' / 'memvane' / 'auto-context' / 'scan-state'
        self.assertTrue((moved / 'cursor.json').exists())
        installed.write_text(json.dumps({'plugins': {}}))
        result = self.collect()
        self.assertEqual(result['status'], 'collected', result)
        self.assertFalse(self.legacy.exists())


class ReviewFixTests(MoveCase):
    def test_owner_command_finishes_a_move_that_stopped_after_its_rename(self):
        import laya_state_move as move

        def die(name):
            if name == 'moved':
                raise Kill(name)
        with mock.patch.object(move, '_step', die):
            with self.assertRaises(Kill):
                self.run_worker()
        pending = move.status()['pendingMoves']
        self.assertEqual([row['stateRoot'] for row in pending], [str(self.target)])
        result = upgrade.move_all()
        self.assertEqual(result['status'], 'moved', result)
        self.assert_moved()
        self.assertEqual(move.status().get('pendingMoves', []), [])

    def test_hook_main_accepts_the_route_moved_between_its_two_reads(self):
        payload = self.home / 'payload.json'
        payload.write_text(json.dumps({'prompt': 'synthetic', 'cwd': str(self.project),
                                       'hook_event_name': 'UserPromptSubmit'}))
        real = response.select_hook_route
        served = []

        def classify_then_move(*args):
            decision = real(*args)
            self.run_worker()  # the worker finishes between the two route reads
            return decision

        def fake(host, chosen, payload, proof, recheck, *, degraded=False, **_):
            served.append(chosen['journalPath'])
            return {'served': True}
        with mock.patch.object(response, 'select_hook_route', classify_then_move), \
                mock.patch.object(response, '_serve_recall', fake), \
                mock.patch.object(response, '_capture_learning', lambda *_: None):
            code = response.main(['serve-hook', 'recall', 'claude', str(payload),
                                  str(self.project)])
        self.assertEqual(code, 0)
        self.assertEqual(served, [str(self.target / 'install-journal.sqlite')])

    def test_creating_the_memvane_base_fsyncs_its_parent(self):
        import laya_state_move as move
        synced = []
        real = move._fsync

        def record(path):
            synced.append(str(path))
            return real(path)
        with mock.patch.object(move, '_fsync', record):
            self.run_worker()
        self.assert_moved()
        state_dir = str(self.home / '.local' / 'state')
        self.assertIn(state_dir, synced)
        self.assertLess(synced.index(state_dir), synced.index(str(self.legacy)))

    def test_wiki_grant_from_a_pre_move_decision_lands_at_the_new_root(self):
        import laya_wiki_discovery as discovery
        before = self.decision()
        self.run_worker()
        grant = discovery.record_grant(before, 'session_start')
        _state, grants, _lock = discovery._paths(self.decision())
        self.assertTrue(str(grants).startswith(str(self.target)))
        self.assertIn(grant, grants.read_text())

    def test_a_writer_waiting_on_the_discovery_lock_follows_the_move(self):
        import laya_state_move as move
        import laya_wiki_discovery as discovery
        before = self.decision()
        results, entered = [], threading.Event()

        def pause(name):
            if name == 'prepared':
                entered.set()
                time.sleep(0.3)

        def writer():
            entered.wait(5)
            try:
                results.append(discovery.record_grant(before, 'session_start', wait=3.0))
            except Exception as exc:  # noqa: BLE001
                results.append(exc)
        thread = threading.Thread(target=writer)
        thread.start()
        with mock.patch.object(move, '_step', pause):
            self.run_worker()
        thread.join()
        self.assertIsInstance(results[0], str, results)
        _state, grants, _lock = discovery._paths(self.decision())
        self.assertIn(results[0], grants.read_text())

    def test_recall_with_an_unusable_provider_manifest_reaches_the_lexical_search(self):
        decision = self.decision()
        proof = journal_mod.verify_selected_route_read_only(decision)
        # The fixture manifest is not a usable provider manifest: before this
        # change the recall failed building the provider, now it searches.
        with self.assertRaisesRegex(Exception, 'no_published_generation'):
            response._serve_recall('claude', decision,
                                   {'prompt': 'synthetic', 'cwd': str(self.project)},
                                   proof, journal_mod.verify_selected_route_read_only,
                                   degraded=True)


# The socket path under HOME is '/.local/state/laya/<32 hex>/runtime/laya-embed.sock'
# (75 bytes); a 24-byte HOME puts it at 99 bytes before the move and 102 after.
SHORT_HOME_BYTES = 24
_RANDOM = 8  # tempfile's random name length


def _short_home_base():
    """(temp base, prefix) giving a HOME of exactly SHORT_HOME_BYTES, or None.

    The platform temp dir first (realpath), then the system's short temp roots;
    a prefix pads the name to the exact length.
    """
    import tempfile
    seen = []
    for raw in (tempfile.gettempdir(), '/tmp', '/var/tmp'):
        try:
            base = os.path.realpath(raw)
        except OSError:
            continue
        if base in seen or not os.path.isdir(base) or not os.access(base, os.W_OK):
            continue
        seen.append(base)
        pad = SHORT_HOME_BYTES - len(base.encode()) - 1 - _RANDOM
        if pad >= 1:
            return base, 'l' * pad
    return None


class ShortHomeTests(MoveCase):
    def setUp(self):
        found = _short_home_base()
        if found is None:
            self.skipTest('no writable temp directory short enough for a '
                          f'{SHORT_HOME_BYTES}-byte HOME')
        self.TEMP_DIR, self.TEMP_PREFIX = found
        super().setUp()
        self.assertEqual(len(str(self.home).encode()), SHORT_HOME_BYTES, self.home)

    def test_a_move_past_the_socket_path_limit_is_refused_before_the_rename(self):
        socket = json.loads((self.state / journal_mod.PRODUCTION_ATTESTATION).read_bytes())[
            'providerManifest']['serviceSocketPath']
        self.assertEqual(len(socket.encode()), 99)
        results = self.run_worker()
        self.assertEqual((results[0]['status'], results[0]['reason']),
                         ('refused', 'state_move_socket_path_too_long'), results)
        self.assertTrue(self.state.is_dir())
        self.assertFalse(self.target.exists())
        decision = self.decision()
        self.assertEqual(decision['state'], 'candidate')
        self.assertEqual(decision['journalPath'], str(self.journal))
        self.assertIsNone(response._state_move_notice(decision))


class ServiceLeaseTests(MoveCase):
    def setUp(self):
        super().setUp()
        self.service = self.legacy / 'service'
        self.service.mkdir(mode=0o700)
        (self.service / 'service.lock').write_bytes(b'')
        (self.service / 'service.lock').chmod(0o600)
        patcher = mock.patch.object(upgrade, '_collect_shared', return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_held_service_lease_keeps_the_pre_rename_service_tree(self):
        import fcntl
        import laya_state_move as move
        self.run_worker()
        holder = os.open(self.service / 'service.lock', os.O_RDWR)
        try:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = move.collect_shared(self.home, ps_all=lambda: [])
            row = next(item for item in result['trees'] if item['tree'] == 'service')
            self.assertEqual(row['blockers'], ['legacy_service_lease_held'])
            self.assertTrue((self.service / 'service.lock').exists())
        finally:
            os.close(holder)
        result = move.collect_shared(self.home, ps_all=lambda: [])
        self.assertFalse(self.service.exists(), result)

    def test_the_memvane_service_retires_the_tree_whose_lease_it_holds(self):
        import laya_resident_service as svc
        self.run_worker()
        root = self.home / '.local' / 'state' / 'memvane' / 'service'
        root.mkdir(mode=0o700)
        service = svc.Service(root, bundle={'version': '0', 'digest': 'x'},
                              legacy_root=self.service, tick=.05)
        self.addCleanup(service.close)
        self.assertTrue(service._hold_legacy(time.monotonic() + 1))
        installed = self.home / '.claude' / 'plugins' / 'installed_plugins.json'
        installed.parent.mkdir(parents=True)
        installed.write_text(json.dumps({'plugins': {
            'qmd-auto-context@qmd-auto-context-marketplace': [{'installPath': '/x'}]}}))
        self.assertFalse(service._retire_legacy(time.monotonic()))
        self.assertTrue(self.service.exists())
        installed.write_text(json.dumps({'plugins': {}}))
        service.legacy_retire_at = 0.0
        self.assertTrue(service._retire_legacy(time.monotonic()))
        self.assertFalse(self.service.exists())
        self.assertIsNone(service.legacy_lease)


def tearDownModule():
    if _mtimes() != BEFORE:
        raise AssertionError('real per-user state changed: ' + json.dumps(_mtimes()))


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
"""T3 journal content proof routing, warm only; no host/cohort/cold claim."""
from contextlib import ExitStack
from contextvars import copy_context
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('journal_proof_seed', ROOT / 'test/fixtures/laya-writer-proof-reader-checks.py')
existing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(existing)

ENGINE = r'''
import json,signal,sys
from pathlib import Path
signal.alarm(8)
sys.path.insert(0,sys.argv[1])
import laya_install_journal as J
with J._identity_connection(Path(sys.argv[2])) as db:
    value=db.execute('SELECT value FROM fixture').fetchone()[0]
assert not any(n in sys.modules for n in ('laya_writer_open_plan','laya_writer_proof_reader','laya_journal_reader'))
print(json.dumps(dict(value=value,module=J.__file__,fullPluginLoaded=False)),flush=True)
'''

def emit(label, **fields):
    print(json.dumps(dict(case=label, **fields), sort_keys=True), flush=True)

class WriterJournalProofChecks(unittest.TestCase):
    def setUp(self):
        self.fixture = existing.WriterProofReaderChecks('runTest')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.case, self.seed = self.fixture.case, self.fixture.seed
        self.J = self.fixture.fixture.fixture.J
        self.P, self.W = self.fixture.P, self.fixture.W
        self.sync = self.fixture.sync
        self.queue = Path(self.fixture.fixture.plan.queue_path)
        self.content = self.fixture.fixture.fixture.content
        self.decision = self.J.select_route(str(self.case.project), action='update')

    def api(self):
        function = getattr(self.J, 'writer_identity_reads', None)
        if not callable(function):
            emit('journal-proof-feature-absence', actualV5=True, issuedWindowBaseline=True)
            self.fail('guarded journal identity reads absent after actual proof-window baseline')
        return function

    def proof(self):
        return self.J.verify_selected_route_for_write(self.decision)

    def trace(self, calls):
        from urllib.parse import unquote, urlparse
        actual = sqlite3.connect
        def connect(database, *args, **kwargs):
            raw = str(database)
            path = Path(unquote(urlparse(raw).path)) if raw.startswith('file:') else Path(raw)
            if path in self.content:
                calls.append(dict(path=str(path), uri=raw,
                    managed=kwargs.get('factory') is self.W._OwnedSQLite))
            return actual(database, *args, **kwargs)
        return mock.patch.object(sqlite3, 'connect', side_effect=connect)

    def test_actual_full_proof_uses_two_owned_ro_connections_and_preserves_warm_files(self):
        with self.fixture.warm(), self.seed.authority():
            stack, window = self.fixture.scope()
            calls, closes = [], []
            native = self.W._OwnedSQLite._native_close_locked
            def close(db):
                closes.append(not self.fixture.fixture.probe('base-physical.lock', 'SH'))
                return native(db)
            before = {str(p): existing.files(p) for p in self.content}
            try:
                api = self.api()
                with self.trace(calls), mock.patch.object(self.W._OwnedSQLite, '_native_close_locked', new=close):
                    with api(window):
                        proof = self.proof()
                self.assertTrue(proof['productRouteAllowed'])
                self.assertEqual({c['path'] for c in calls}, {str(self.case.db), str(self.queue)})
                self.assertEqual(len(calls), 2)
                self.assertTrue(all(c['managed'] and c['uri'].endswith('?mode=ro') for c in calls))
                self.assertEqual(closes, [True, True])
                self.assertEqual({str(p): existing.files(p) for p in self.content}, before)
                emit('journal-owned-full-proof', contentConnects=calls, closesUnderEX=closes,
                     productRouteAllowed=True, warmOnly=True, newAuxiliaryFiles=0)
            finally:
                stack.close()

    def test_expired_owner_never_falls_back_to_raw_connection(self):
        with self.fixture.warm(), self.seed.authority(), ExitStack() as owners:
            owner = owners.enter_context(self.sync.sync_owner(self.queue))
            context = self.P.writer_proof_windows(self.fixture.fixture.plan,
                queue_owner=owner, deadline=time.monotonic()+20)
            window = context.__enter__()
            calls = []
            try:
                with self.api()(window), self.trace(calls):
                    owners.close()
                    with self.assertRaises((ValueError, RuntimeError, sqlite3.Error)):
                        self.proof()
                self.assertEqual(calls, [])
                emit('journal-expired-owner', contentConnects=calls, rawFallback=False)
            finally:
                try: context.__exit__(None, None, None)
                except (ValueError, RuntimeError): pass

    def test_owner_expiry_after_last_native_close_prevents_success_return(self):
        with self.fixture.warm(), self.seed.authority(), ExitStack() as owners:
            owner = owners.enter_context(self.sync.sync_owner(self.queue))
            context = self.P.writer_proof_windows(self.fixture.fixture.plan,
                queue_owner=owner, deadline=time.monotonic()+20)
            window = context.__enter__()
            calls, closes = [], []
            native = self.W._OwnedSQLite._native_close_locked
            def close(db):
                result = native(db)
                closes.append(True)
                if len(closes) == 2: owners.close()
                return result
            error = None
            try:
                with self.api()(window), self.trace(calls), mock.patch.object(self.W._OwnedSQLite, '_native_close_locked', new=close):
                    try: self.proof()
                    except (ValueError, RuntimeError, sqlite3.Error) as exc: error = str(exc)
                self.assertEqual(len(calls), 2)
                self.assertEqual(len(closes), 2)
                self.assertIsNotNone(error, 'post-close expired owner must not return full proof')
                emit('journal-final-owner-recheck', contentConnects=calls, nativeClosed=2, error=error)
            finally:
                try: context.__exit__(None, None, None)
                except (ValueError, RuntimeError): pass

    def test_copied_context_is_revoked_and_foreign_exit_preserves_creator_reset(self):
        with self.fixture.warm(), self.seed.authority():
            stack, window = self.fixture.scope()
            try:
                binding = self.api()(window)
                binding.__enter__()
                copied = copy_context()
                errors = []
                def foreign():
                    try: binding.__exit__(None, None, None)
                    except (ValueError, RuntimeError) as exc: errors.append(str(exc))
                thread = threading.Thread(target=foreign)
                thread.start(); thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(errors), 1)
                self.assertTrue(self.proof()['productRouteAllowed'])
                binding.__exit__(None, None, None)
                calls = []
                with self.trace(calls), self.assertRaises((ValueError, RuntimeError, sqlite3.Error)):
                    copied.run(self.proof)
                self.assertEqual(calls, [])
                # Ordinary default proof remains available after creator reset.
                with self.trace(calls): self.assertTrue(self.proof()['productRouteAllowed'])
                self.assertEqual(len(calls), 2)
                self.assertTrue(all(not c['managed'] for c in calls))
                emit('journal-context-revocation', staleContentConnects=0,
                     foreignExitRejected=True, defaultProofRestored=True)
            finally: stack.close()

    def test_unissued_window_cannot_install_an_opener(self):
        api = self.api()
        for bad in (True, {}, lambda: None, object.__new__(self.P.WriterProofWindow)):
            with self.subTest(kind=type(bad).__name__), self.assertRaises((ValueError, TypeError, RuntimeError)):
                with api(bad): self.fail('unissued context entered')

    def test_default_identity_reader_loads_no_proof_dependencies(self):
        self.api()
        # The fixed engine's pre-existing project_dir import failure is tracked
        # separately by the minimal previous-head comparison, not as a pass.
        core = ROOT / 'core'
        path = self.case.home / 'identity-engine.sqlite'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE fixture(value TEXT)')
            db.execute("INSERT INTO fixture VALUES('default-ro-preserved')")
        child = subprocess.run([sys.executable, '-B', '-I', '-c', ENGINE, str(core), str(path)],
            encoding='utf8', capture_output=True, timeout=10)
        self.assertEqual(child.returncode, 0, child.stderr)
        value = json.loads(child.stdout)
        self.assertEqual(value['value'], 'default-ro-preserved')
        self.assertFalse(value['fullPluginLoaded'])
        self.assertEqual(Path(value['module']).parent, core)
        emit('journal-default-no-proof-dependencies', **value)

if __name__ == '__main__': unittest.main()

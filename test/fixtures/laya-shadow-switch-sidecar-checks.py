#!/usr/bin/env python3
"""U15 switch of a v6 install whose completed sidecar differs from its base.

Builds on the U15 fixture (U11 -> U10): a real v5 install, v6 index, journal
images, Git checkout, the actual shadow migration driver and main recall.
The seed publishes the completed sidecar rows and then changes one sidecar
document only (body, hash and FTS row; the wiki file on disk is the base
bytes), as ``laya-shadow-migration-checks`` ``ChangedSidecar`` does. The
migration generation therefore carries paired base and sidecar FTS corpora.
After the switch CAS the head is that paired generation until the next
schema3 sync pass publishes an ordinary one; recall in between and after
is checked through ``serve_selected`` and the registered hook entry point.
Synthetic authority as in U15; the fixture provider is unusable, so both
readers answer lexically.
"""
from contextlib import ExitStack, closing, redirect_stderr, redirect_stdout
import hashlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'core'))
spec = importlib.util.spec_from_file_location('switch_sidecar_checks', HERE / 'laya-shadow-switch-checks.py')
switch_checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(switch_checks)
recall_checks = switch_checks.recall_checks
seed_module = recall_checks.seed_module
J = recall_checks.J
PROBE = 'sidecarprobe'
CHANGED = 'concepts/heron-route.md'
PROMPT = 'heron route northern marsh ' + PROBE


def emit(case, **evidence):
    print(json.dumps(dict(case=case, **evidence), sort_keys=True, default=str), flush=True)


def changed_sidecar_seed():
    """The U11 seed with completed sidecar rows, one changed after publication."""
    class ChangedSidecarSeed(seed_module.MoveSealChecks):
        """Completed sidecar rows, one of them changed after its publication."""

        def setUp(self):
            # U11 subclasses this seed with a class-level PUBLISH_SIDECAR=False.
            self.PUBLISH_SIDECAR = True
            super().setUp()

        def before_conversion(self):
            with closing(sqlite3.connect(self.sidecar)) as db, db:
                rowid, path, title, body = db.execute(
                    'SELECT rowid,path,title,body FROM docs WHERE path LIKE ?', ('%/' + CHANGED,)).fetchone()
                changed = body + '\nSynthetic completed sidecar revision mentions the %s marker.\n' % PROBE
                digest = hashlib.sha256(changed.encode()).hexdigest()
                db.execute('UPDATE docs SET hash=?,body=? WHERE rowid=?', (digest, changed, rowid))
                db.execute('DELETE FROM docs_fts WHERE rowid=?', (rowid,))
                db.execute('INSERT INTO docs_fts(rowid,path,title,body) VALUES(?,?,?,?)',
                           (rowid, path, title, changed))
            self.changed_hash = digest
    return ChangedSidecarSeed


class ShadowSwitchSidecarChecks(unittest.TestCase):
    def setUp(self):
        # Defined here so that unittest does not collect the seed's own tests.
        ChangedSidecarSeed = changed_sidecar_seed()
        with mock.patch.object(seed_module, 'MoveSealChecks', ChangedSidecarSeed):
            self.u15 = switch_checks.ShadowSwitchChecks('runTest')
            self.u15.setUp()
        self.addCleanup(self.u15.doCleanups)
        self.u11, self.u10, self.case = self.u15.u11, self.u15.u10, self.u15.case
        self.W, self.M = self.u15.W, self.u15.M

    # ---- helpers -----------------------------------------------------------
    def stage(self, gen):
        store = self.u15.intent()['candidate']
        with closing(sqlite3.connect(Path(store).as_uri() + '?mode=ro', uri=True)) as db:
            row = db.execute('SELECT s.stage_json FROM generations g JOIN generation_slots s '
                             'ON s.ordinal=g.ordinal WHERE g.gen_id=?', (gen,)).fetchone()
        return json.loads(row[0])

    def head(self):
        store = self.u15.intent()['candidate']
        with closing(sqlite3.connect(Path(store).as_uri() + '?mode=ro', uri=True)) as db:
            return db.execute("SELECT gen_id FROM generations WHERE state='published' "
                              'ORDER BY ordinal DESC LIMIT 1').fetchone()[0]

    def recall(self):
        """``serve_selected`` recall; a refusal is returned, not raised."""
        try:
            answer = self.u11.recall(prompt=PROMPT)
        except Exception as exc:  # noqa: BLE001 - the RED records the refusal code
            return {'error': '%s: %s' % (type(exc).__name__, exc)}
        return answer

    def hook(self, host='claude'):
        """The registered recall hook entry point; returns (exit, stdout, stderr)."""
        case, u15 = self.case, self.u15
        host_response = self.u11.host
        upgrade = importlib.import_module('laya_bundle_upgrade')
        runtime_work = importlib.import_module('runtime_work')
        hook_budget = importlib.import_module('hook_budget')
        payload = {'cwd': str(case.project), 'prompt': PROMPT, 'session_id': 'sidecar-' + host,
                   'hook_event_name': 'UserPromptSubmit'}
        stdin = mock.Mock(buffer=io.BytesIO(json.dumps(payload).encode('utf8')))
        out, err, wakes = io.StringIO(), io.StringIO(), []
        with ExitStack() as stack:
            stack.enter_context(u15.driver())
            stack.enter_context(mock.patch.object(J, '_verified_executing_root', return_value=str(case.plugin)))
            stack.enter_context(mock.patch.object(host_response, '__file__',
                                                  str(case.plugin / 'core' / 'laya_host_response.py')))
            stack.enter_context(mock.patch.object(upgrade, 'host_layout', return_value=(host, case.plugin)))
            stack.enter_context(mock.patch.object(runtime_work, 'request',
                                                  side_effect=lambda kind, target='', **k: wakes.append(kind)))
            stack.enter_context(mock.patch.object(sys, 'stdin', stdin))
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            stack.enter_context(mock.patch.object(hook_budget, '_deadline', None))
            code = host_response.main(['serve-stdin', 'recall', host, str(case.project)])
        return code, out.getvalue(), err.getvalue()

    def sync(self):
        """One actual sync run (``_run_sync`` -> ``_run_schema3``) with the driver's authority."""
        real = self.u15.project_dir.layout

        def no_service(*_args, **_kwargs):
            raise AssertionError('a lexical schema3 pass started a provider service')
        with ExitStack() as stack:
            stack.enter_context(self.u10.bridge_authority())
            stack.enter_context(mock.patch.object(self.W, '_writer_executing_root', return_value=self.case.plugin))
            stack.enter_context(mock.patch.object(
                self.u15.project_dir, 'layout',
                side_effect=lambda p: 'current' if Path(p) == self.case.project else real(p)))
            return self.W.run(self.case.project, start_service=no_service)

    def switched(self):
        u15 = self.u15
        with u15.driver():
            passes = u15.drive_to('shadow_verified')
            u15.loaded()
            u15.run_pass()
            u15.run_pass()
        return passes

    # ---- the paired migration head after the switch ------------------------
    def test_01_paired_head_switches_and_recall_serves_the_sidecar_bytes(self):
        u15 = self.u15
        before = self.recall()
        self.assertNotIn('error', before)
        self.assertIn(PROBE, before['context'])
        passes = self.switched()
        intent = u15.intent()
        self.assertEqual(intent['migration_state'], 'switched', passes)
        self.assertEqual(u15.store_format(), 'schema3')
        gen = intent['published']['generation']
        self.assertEqual(self.head(), gen)
        legacy = self.stage(gen)['legacy']
        paired = 'sidecar_corpus' in legacy
        sources = sorted(path for path, origin in legacy.get('fts_sources', {}).items() if origin == 'sidecar')
        # Inside the window: the switched head is the paired migration generation.
        during = self.recall()
        code, out, err = self.hook()
        emit('switched-paired-recall', paired=paired, sidecarPaths=sources,
             recall=during.get('error') or during['event'].get('outcome'),
             reason=None if 'error' in during else during['event'].get('reason'),
             hookExit=code, hookStderr=err.strip()[-200:])
        self.assertTrue(paired)
        self.assertEqual(sources, [CHANGED])
        self.assertNotIn('error', during, during.get('error'))
        self.assertEqual(during['event']['outcome'], 'served')
        self.assertEqual(during['results'][-1].store_format, 'schema3')
        self.assertEqual(during['results'][-1].data_generation, gen)
        # The changed card answers with its sidecar bytes, never the stale base.
        self.assertIn(PROBE, during['context'])
        self.assertIn('"' + self.case.WIKI + '/' + CHANGED + '"', during['context'])
        self.assertEqual(code, 0, err)
        self.assertIn(PROBE, out)
        self.assertEqual(during['v6_runtime'], 0)
        # The next sync pass republishes an ordinary schema3 generation.
        result = self.sync()
        after_gen = self.head()
        emit('switched-paired-sync', state=result.get('state'), generation=after_gen, legacy=gen)
        self.assertEqual(result['state'], 'indexed', result)
        self.assertNotEqual(after_gen, gen)
        self.assertNotIn('legacy', self.stage(after_gen))
        after = self.recall()
        self.assertNotIn('error', after, after.get('error'))
        self.assertEqual(after['event']['outcome'], 'served')
        self.assertEqual(after['results'][-1].data_generation, after_gen)


if __name__ == '__main__':
    unittest.main(verbosity=2)

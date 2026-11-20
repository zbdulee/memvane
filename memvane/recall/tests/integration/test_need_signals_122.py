"""1.0.22 B-0: per-atom search-need signals on prompt recall, never altering recall output.

Uses ``RecallInstallCase`` with ``CARDS = 20`` (so ``zqheronx`` hits 1 card,
``zqsharedx`` hits all 20 = ``HYBRID_LIST_LIMIT``, and ``zqabsentx`` hits 0),
plus real ``hooks/run-hook recall claude|codex`` child invocations.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
import subprocess
import sys
import time
from unittest import mock

from memvane.journal import api as journal_api
from memvane.recall import domain, reader_dispatch, recall_scope, selected_recall
from memvane.recall.tests.support import RecallInstallCase, TOPICS
from memvane.recall.types import HYBRID_LIST_LIMIT, RecallTrace
from memvane.store.types import BlobStoreError
from tests.support.install_builder import ROOT

NEED_FIELDS = ('needAtoms', 'needAtomsHit', 'needAtomsRare',
               'needRareTopScore', 'needProbeMs', 'needProbeTruncated')


class _MemoryTrace(RecallTrace):
    def __init__(self):
        self.data = {}

    def fields(self, **fields):
        self.data.update(fields)


class NeedSignals(RecallInstallCase):
    CARDS = 20

    def _run_hook_env(self):
        shim = self.tmp / 'bin'
        if not shim.is_dir():
            shim.mkdir(mode=0o700)
            py = shim / 'python3'
            py.write_text('#!/bin/sh\nexec %s "$@"\n' % json.dumps(sys.executable), encoding='utf-8')
            py.chmod(0o700)
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
               'PATH': '%s:%s' % (shim, os.environ.get('PATH', ''))}
        for key in ('QMD_SANDBOX', 'CLAUDE_SANDBOX', 'CLAUDE_HEADLESS', 'CODEX_SANDBOX',
                    'CLAUDE_PLUGIN_ROOT', 'PLUGIN_ROOT', 'QMD_HOOK_DEADLINE_MONO',
                    'MEMVANE_NEED_SIGNALS'):
            env.pop(key, None)
        return env

    def _run_hook_recall(self, host, session, prompt, *, switch=None):
        env = self._run_hook_env()
        if switch is not None:
            env['MEMVANE_NEED_SIGNALS'] = switch
        before = len(self.install.events())
        payload = {'cwd': str(self.project), 'session_id': session,
                   'hook_event_name': 'UserPromptSubmit', 'prompt': prompt}
        proc = subprocess.run(['bash', str(ROOT / 'hooks' / 'run-hook'), 'recall', host],
                              input=json.dumps(payload), capture_output=True, text=True,
                              env=env, cwd=str(self.project), timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        events = [row for row in self.install.events()[before:] if row.get('event') == 'recall']
        self.assertEqual(len(events), 1, events)
        return json.loads(proc.stdout)['hookSpecificOutput']['additionalContext'], events[0]

    def test_tb0_1_rare_and_common_terms_and_real_hook_claude_codex(self):
        prompt = 'zqheronx zqsharedx zqabsentx'
        ctx, _err, event = self.recall(prompt)
        self.assertIn('zqheronx', ctx)
        self.assertEqual((event['needAtoms'], event['needAtomsHit'], event['needAtomsRare']), (3, 2, 1))
        self.assertIsInstance(event['needRareTopScore'], float)
        self.assertTrue(math.isfinite(event['needRareTopScore']))
        self.assertGreaterEqual(event['needProbeMs'], 0)
        self.assertFalse(event['needProbeTruncated'])

        for host in ('claude', 'codex'):
            with self.subTest(host=host):
                on_ctx, on_ev = self._run_hook_recall(host, f'hook-on-{host}', prompt)
                off_ctx, off_ev = self._run_hook_recall(host, f'hook-off-{host}', prompt, switch='off')
                self.assertEqual(on_ctx, off_ctx)
                self.assertIn('zqheronx', on_ctx)
                for key in NEED_FIELDS:
                    self.assertIn(key, on_ev, key)
                    self.assertNotIn(key, off_ev, key)
                self.assertEqual((on_ev['needAtoms'], on_ev['needAtomsHit'], on_ev['needAtomsRare']),
                                 (3, 2, 1))

    def test_tb0_2_edit_recall_runs_zero_probes_and_records_no_need_fields(self):
        decision = journal_api.select_route(str(self.project), action='recall')
        with journal_api.resolve_endpoint(self.project, action='recall', host_root=ROOT) as endpoint:
            _rec, _att, proof = journal_api.recheck_reader(endpoint)
        trace = _MemoryTrace()
        with mock.patch.object(reader_dispatch, '_need_signals',
                               wraps=reader_dispatch._need_signals) as spy:
            with recall_scope.recall_scope():
                recall_scope.set_edit_recall(deadline=time.monotonic() + 30,
                                             selector_deadline=time.monotonic() + 5)
                answer = selected_recall.serve_endpoint_recall(
                    'claude', {'cwd': str(self.project), 'prompt': 'zqheronx zqsharedx',
                               'session_id': 'edit-in-recall'},
                    cwd=str(self.project), trace=trace, route_decision=decision, main_proof=proof)
        self.assertTrue(answer.rows)
        self.assertEqual(spy.call_count, 0)
        for key in NEED_FIELDS:
            self.assertNotIn(key, trace.data, key)

    def test_tb0_3_twenty_terms_capped_at_eight_probes(self):
        prompt = ' '.join(f'zq{topic}x' for topic in TOPICS)
        _ctx, _err, natural = self.recall(prompt)
        self.assertLessEqual(natural['needAtoms'], 8)

        self.install.provider.admit_queries()
        twenty = ' AND '.join(f'"zq{topic}x"*' for topic in TOPICS)
        real_pin = journal_api.pin_schema3
        search_calls = []

        @contextmanager
        def spied_pin(*args, **kwargs):
            with real_pin(*args, **kwargs) as (store, handle, pin):
                orig = store.search

                def counted(p, fts_query, limit, **kw):
                    search_calls.append(fts_query)
                    return orig(p, fts_query, limit, **kw)

                with mock.patch.object(store, 'search', side_effect=counted):
                    yield store, handle, pin

        with mock.patch.object(domain.rules, '_hybrid_fts_queries', return_value=[twenty]), \
                mock.patch.object(journal_api, 'pin_schema3', side_effect=spied_pin):
            _ctx2, _err2, event = self.recall('zqheronx')
        self.assertEqual(event['needAtoms'], 8)
        # 1 conjunctive lexical query + 8 single-atom probes = 9 store.search calls.
        self.assertEqual(len(search_calls), 9)

    def test_tb0_4_probe_exception_and_deadline_preserve_recall_and_search_exact(self):
        self.install.provider.admit_queries()
        prompt = 'zqheronx zqsharedx'
        with mock.patch.dict(os.environ, {'MEMVANE_NEED_SIGNALS': 'off'}):
            base_ctx, _err, base_ev = self.recall(prompt)
        self.assertEqual(base_ev['semantic'], 'complete')
        real_pin = journal_api.pin_schema3

        def pin_with_probe_error(error):
            @contextmanager
            def pinned(*args, **kwargs):
                with real_pin(*args, **kwargs) as (store, handle, pin):
                    orig = store.search
                    calls = [0]

                    def search(p, q, limit, **kw):
                        calls[0] += 1
                        if calls[0] > 1:
                            raise error
                        return orig(p, q, limit, **kw)

                    with mock.patch.object(store, 'search', side_effect=search):
                        yield store, handle, pin
            return pinned

        for label, exc in (('runtime', RuntimeError('probe boom')),
                           ('fts_deadline', BlobStoreError('fts_deadline_exceeded'))):
            with self.subTest(case=label), \
                    mock.patch.object(journal_api, 'pin_schema3', side_effect=pin_with_probe_error(exc)):
                ctx, _err, ev = self.recall(prompt)
            self.assertEqual(ctx, base_ctx)
            self.assertEqual(ev['cards'], base_ev['cards'])
            self.assertEqual(ev['semantic'], 'complete')
            self.assertTrue(ev['needProbeTruncated'])

        # Real deadline exhaustion via NEED_BUDGET_SECONDS still lets search_exact complete on the same pin.
        with mock.patch.object(reader_dispatch, 'NEED_BUDGET_SECONDS', 1e-9):
            ctx_b, _err_b, ev_b = self.recall(prompt)
        self.assertEqual(ctx_b, base_ctx)
        self.assertEqual(ev_b['cards'], base_ev['cards'])
        self.assertEqual(ev_b['semantic'], 'complete')
        self.assertTrue(ev_b['needProbeTruncated'])

        # Reader reserve guard skips probes when under NEED_READER_RESERVE_SECONDS and keeps search_exact.
        with mock.patch.object(reader_dispatch, 'NEED_READER_RESERVE_SECONDS', 600.0):
            ctx_r, _err_r, ev_r = self.recall(prompt)
        self.assertEqual(ctx_r, base_ctx)
        self.assertEqual(ev_r['semantic'], 'complete')
        self.assertEqual(ev_r['needAtoms'], 0)
        self.assertTrue(ev_r['needProbeTruncated'])

    def test_tb0_5_env_switch_off_runs_zero_probes(self):
        for val in ('off', 'OFF'):
            with self.subTest(val=val), \
                    mock.patch.dict(os.environ, {'MEMVANE_NEED_SIGNALS': val}), \
                    mock.patch.object(reader_dispatch, '_need_signals',
                                      wraps=reader_dispatch._need_signals) as spy:
                ctx, _err, ev = self.recall('zqheronx zqsharedx')
            self.assertIn('zqheronx', ctx)
            self.assertEqual(spy.call_count, 0)
            for key in NEED_FIELDS:
                self.assertNotIn(key, ev, key)

    def test_tb0_6_event_keeps_no_prompt_or_term_text(self):
        prompt = 'zqheronx zqsharedx zqabsentx'
        _ctx, _err, ev = self.recall(prompt)
        raw = json.dumps(ev)
        for secret in (prompt, 'zqheronx', 'zqsharedx', 'zqabsentx', 'Field notes'):
            self.assertNotIn(secret, raw)

    def test_tb0_7_hermes_recall_unchanged_with_signals_on_and_off(self):
        prompt = 'zqheronx zqsharedx'
        with mock.patch.dict(os.environ, {'MEMVANE_NEED_SIGNALS': 'off'}):
            off_ctx, _err, off_ev = self.recall(prompt, host='hermes')
        on_ctx, _err, on_ev = self.recall(prompt, host='hermes')
        self.assertEqual(on_ctx, off_ctx)
        self.assertIn('zqheronx', on_ctx)
        for key in NEED_FIELDS:
            self.assertIn(key, on_ev, key)
            self.assertNotIn(key, off_ev, key)


if __name__ == '__main__':
    import unittest
    unittest.main()

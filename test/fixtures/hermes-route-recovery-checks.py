#!/usr/bin/env python3
"""Hermes registered hook over real preview/journal/fence fixtures.

The existing onboarding fixture supplies synthetic host bundle attestations and
fixed vectors; selector CLI, journal, build, card proof and preview reader are
real. No classifier/select_route/verify_preview_route result is fabricated.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'core'))
E2E = runpy.run_path(str(HERE / 'laya-fresh-onboarding-e2e-checks.py'))
import laya_fresh_wiki_origin as fresh
import laya_host_response as response
from memvane.route import index_host_route as route
import laya_install_journal as journal
import laya_product_install as product
from hermes_adapter import core_bridge as bridge, plugin

REAL_RUN = subprocess.run
REAL_PREVIEW = response.serve_preview


class HermesRouteChecks(unittest.TestCase):
    def fixture(self):
        case = E2E['FreshOnboarding']('runTest')
        self.addCleanup(case.doCleanups)
        case.setUp()
        plan = product.plan_fresh_wiki_origin(case.project)
        prepared = product.prepare_fresh_wiki_origin(
            project_root=case.project, source_root=str(case.source),
            plugin_root=str(case.plugin), state_root=str(case.state),
            commit_id=E2E['COMMIT'], expected_proposal_sha256=plan['proposalSha256'],
            install_id=E2E['BUILD']['INSTALL'],
            accepted_provider_sha256=case.review['proposalSha256'],
            model_path='/models/m.gguf', dimension=2)
        self.assertEqual(prepared['status'], 'initial_build_registered')
        self.assertEqual(case.until_settled()[-1]['initialBuild'], 'sealed')
        self.assertEqual(route.select_route(str(case.project))['state'], 'preview')
        return case

    def invoke(self, case, *, after_classify=None, classifier_start_delay=0):
        hooks = {}
        class Context:
            def register_hook(self, name, callback):
                hooks[name] = callback
        plugin.register(Context())
        self.assertIn('pre_llm_call', hooks)
        calls, previews = [], []
        selector = ROOT / 'core' / 'laya_index_host_route.py'

        def observed_run(argv, *args, **kwargs):
            try:
                actual_argv = argv
                if classifier_start_delay and len(argv) > 1 and Path(argv[1]) == selector:
                    actual_argv = [sys.executable, '-c',
                                   'import os,sys,time; time.sleep(float(sys.argv[1])); '
                                   'os.execv(sys.executable,[sys.executable,*sys.argv[2:]])',
                                   str(classifier_start_delay), *argv[1:]]
                answer = REAL_RUN(actual_argv, *args, **kwargs)
            except (OSError, subprocess.TimeoutExpired) as exc:
                if len(argv) > 1 and Path(argv[1]) == selector:
                    print(json.dumps({'case': 'selector-exception',
                                      'type': type(exc).__name__,
                                      'timeout': getattr(exc, 'timeout', None)}), flush=True)
                raise
            if len(argv) > 1 and Path(argv[1]) == selector:
                calls.append({'argv': list(argv), 'stdout': answer.stdout.strip(),
                              'returncode': answer.returncode})
                if after_classify is not None:
                    after_classify()
            return answer

        def observed_preview(*args, **kwargs):
            previews.append({'action': args[0], 'host': args[1]})
            return REAL_PREVIEW(*args, **kwargs)

        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {
                'QMD_SANDBOX': '', 'HERMES_SANDBOX': '', 'CODEX_SANDBOX': '',
                'CLAUDE_SANDBOX': '', 'GEMINI_SANDBOX': '',
                'HERMES_HEADLESS': '', 'CLAUDE_HEADLESS': '',
                'QMD_SETUP_GUARD_FIXTURE': '1', 'PYTHONDONTWRITEBYTECODE': '1'}))
            # Only the executing host's bundle authority is synthetic, as in
            # the reused E2E fixture; the journal/pending proof remains real.
            stack.enter_context(mock.patch.object(journal, '_verified_executing_root',
                                                   return_value=str(case.plugin)))
            stack.enter_context(mock.patch('laya_local_embed_provider.ReviewedLocalQmdProvider',
                                           E2E['QueryProvider']))
            stack.enter_context(mock.patch('laya_embed_resident.admit_query',
                                           lambda **_kw: None))
            stack.enter_context(mock.patch.object(bridge.subprocess, 'run', observed_run))
            stack.enter_context(mock.patch.object(response, 'serve_preview', observed_preview))
            # working_directory exercises the registered Hermes callback without
            # supplying duplicate cwd keywords in its separate host signature.
            answer = hooks['pre_llm_call'](user_message='emerald heron',
                                          working_directory=str(case.project))
        print(json.dumps({'case': self.id(), 'classifier': calls,
                          'previewCalls': previews, 'answer': answer}), flush=True)
        self.assertTrue(calls, 'registered hook must invoke the real selector CLI')
        self.assertTrue(all(row['returncode'] == 0 for row in calls), calls)
        self.assertTrue(all('recall' in row['argv'] for row in calls), calls)
        self.assertTrue(all(row['argv'][2:4] == ['classify-hook', 'recall']
                            for row in calls), calls)
        return answer, calls, previews

    def test_preview_injects_the_real_verified_card(self):
        case = self.fixture()
        # Establish core/Hermes compatibility independently of the adapter's
        # current missing preview branch, so that branch's RED cannot conceal
        # an invalid pending journal or an unservable fixture card.
        decision = route.select_route(str(case.project))
        with mock.patch.object(journal, '_verified_executing_root',
                               return_value=str(case.plugin)), \
                mock.patch('laya_local_embed_provider.ReviewedLocalQmdProvider',
                           E2E['QueryProvider']), \
                mock.patch('laya_embed_resident.admit_query', lambda **_kw: None):
            baseline = response.serve_preview('recall', 'hermes', decision,
                {'prompt': 'emerald heron', 'cwd': str(case.project)})
        print(json.dumps({'case': 'real-core-hermes-preview', 'answer': baseline}), flush=True)
        self.assertEqual(set(baseline), {'context'})
        self.assertIn('concepts/heron.md', baseline['context'])
        answer, calls, previews = self.invoke(case)
        self.assertEqual(calls[0]['stdout'], 'preview')
        self.assertEqual(previews, [{'action': 'recall', 'host': 'hermes'}])
        self.assertIn('concepts/heron.md', (answer or {}).get('context', ''))
        self.assertNotIn('request refused', answer['context'])

    def test_persistent_upgrade_fence_returns_no_context(self):
        case = self.fixture()
        self.assertEqual(case.finalize()['status'], 'freshness_pending')
        self.assertEqual(case.until_settled()[-1]['initialBuild'], 'sealed')
        self.assertEqual(case.finalize()['status'], 'cutover_prepared')
        raw = (case.state / 'reviewed-route.json').read_bytes()
        with product._install_lock(case.state):
            product.activate_origin_selection(case.project, case.journal_path, raw)
        ledger = case.state / fresh.LEDGER
        before = fresh._load(ledger)
        fresh._change(ledger, before['epoch'], 'active', 'upgrading')
        self.assertTrue(fresh.upgrade_in_progress(str(case.project)))
        answer, calls, previews = self.invoke(case)
        self.assertEqual(calls[0]['stdout'], 'upgrading')
        self.assertIn(answer, (None, {}, {'context': ''}))
        self.assertEqual(previews, [])
        self.assertEqual(fresh._load(ledger)['phase'], 'upgrading')
        # The real classifier still gets its bounded fence wait when process
        # scheduling consumes part of the adapter's subprocess budget.
        answer, calls, previews = self.invoke(case, classifier_start_delay=.7)
        self.assertEqual(calls[0]['stdout'], 'upgrading')
        self.assertIn(answer, (None, {}, {'context': ''}))
        self.assertEqual(previews, [])

    def test_damaged_pending_proof_is_a_refusal(self):
        case = self.fixture()
        attestation = case.state / journal.PRODUCTION_ATTESTATION
        attestation.write_bytes(b'{}')
        decision = route.select_route(str(case.project))
        self.assertEqual(decision['state'], 'preview')
        with mock.patch.object(journal, '_verified_executing_root',
                               return_value=str(case.plugin)):
            with self.assertRaises(journal.JournalEvidenceError):
                route.verify_preview_route(decision)
        answer, calls, _previews = self.invoke(case)
        self.assertEqual(calls[0]['stdout'], 'preview')
        self.assertIn('request refused', (answer or {}).get('context', ''))

    def test_ordinary_blocked_route_is_a_refusal(self):
        case = self.fixture()
        (case.auto / fresh.ROUTE).write_bytes(b'{}')
        self.assertEqual(route.select_route(str(case.project))['state'], 'blocked')
        answer, calls, previews = self.invoke(case)
        self.assertEqual(calls[0]['stdout'], 'blocked')
        self.assertIn('request refused', (answer or {}).get('context', ''))
        self.assertEqual(previews, [])

    def test_preview_recheck_cannot_switch_to_candidate_or_damaged_route(self):
        for next_state in ('candidate', 'blocked'):
            with self.subTest(next_state=next_state):
                case = self.fixture()
                def mutate():
                    raw = ((case.state / 'reviewed-route.json').read_bytes()
                           if next_state == 'candidate' else b'{}')
                    (case.auto / fresh.ROUTE).write_bytes(raw)
                answer, calls, previews = self.invoke(case, after_classify=mutate)
                self.assertEqual(calls[0]['stdout'], 'preview')
                self.assertEqual(route.select_route(str(case.project))['state'], next_state)
                self.assertIn('request refused', (answer or {}).get('context', ''))
                self.assertEqual(previews, [])
                # Independent fixture HOME and authority patches per subcase.
                case.doCleanups()


if __name__ == '__main__':
    unittest.main()

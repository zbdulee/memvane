#!/usr/bin/env python3
"""Six independent review hypotheses, on actual .37 product boundaries.

Composition reuses real selected/preview journals, stores and record schemas.
Only host/provider/OS authority is synthetic. No model, signal, launchctl or
operational state is used. Every hypothesis prints evidence before assertions;
valid plist/build parsing is established independently of its refusal test.
"""
from contextlib import ExitStack
import builtins
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import signal
import stat
import sys
import tempfile
import unittest
from unittest import mock
from xml.etree import ElementTree

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'core'))
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT))


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HERMES = load('hermes_fixture_for_review_regressions', 'hermes-route-recovery-checks.py')
from memvane.host import onboarding_choice as choice
import laya_initial_build as build
import laya_install_journal as journal
from hermes_adapter import plugin


def emit(finding, **evidence):
    print(json.dumps({'finding': finding, **evidence}, sort_keys=True), flush=True)


class ReviewRegressions(unittest.TestCase):
    def compose(self, cls):
        case = cls('runTest')
        self.addCleanup(case.doCleanups)
        case.setUp()
        return case

    def preview(self):
        helper = self.compose(HERMES.HermesRouteChecks)
        case = helper.fixture()
        return helper, case

    def activate(self, case):
        self.assertEqual(case.finalize()['status'], 'freshness_pending')
        self.assertEqual(case.until_settled()[-1]['initialBuild'], 'sealed')
        self.assertEqual(case.finalize()['status'], 'cutover_prepared')
        raw = (case.state / 'reviewed-route.json').read_bytes()
        with HERMES.product._install_lock(case.state):
            HERMES.product.activate_origin_selection(case.project, case.journal_path, raw)
        self.assertEqual(HERMES.route.select_route(str(case.project))['state'], 'candidate')

    @staticmethod
    def fence(case):
        path = case.state / HERMES.fresh.LEDGER
        row = HERMES.fresh._load(path)
        HERMES.fresh._change(path, row['epoch'], 'active', 'upgrading')
        return HERMES.fresh._load(path)

    def test_02_hermes_actual_fences_after_classify_and_during_serve_are_context_free(self):
        for boundary in ('after-classify', 'during-serve'):
            with self.subTest(boundary=boundary), ExitStack() as cleanup:
                helper, case = self.preview()
                cleanup.callback(helper.doCleanups)
                self.activate(case)
                mutations = []
                def activate_fence():
                    mutations.append(self.fence(case))
                if boundary == 'after-classify':
                    answer, calls, _ = helper.invoke(case, after_classify=activate_fence)
                else:
                    actual = HERMES.response.serve_selected
                    def fenced_serve(*args, **kwargs):
                        activate_fence()
                        return actual(*args, **kwargs)
                    with mock.patch.object(HERMES.response, 'serve_selected', fenced_serve):
                        answer, calls, _ = helper.invoke(case)
                emit('R2-hermes-late-fence', boundary=boundary, answer=answer,
                     classified=calls[0]['stdout'], ledgerPhase=mutations[-1]['phase'])
                self.assertEqual(calls[0]['stdout'], 'candidate')
                self.assertEqual(len(mutations), 1)
                self.assertTrue(HERMES.fresh.upgrade_in_progress(str(case.project)))
                self.assertIn(answer, (None, {}, {'context': ''}), 'an actual upgrade fence is not refusal context')

    def test_03_registered_hooks_accept_both_cwd_aliases_without_duplicate_keywords(self):
        helper, case = self.preview()
        hooks = {}
        class Context:
            def register_hook(self, name, callback):
                hooks[name] = callback
        plugin.register(Context())
        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {
                'QMD_SANDBOX': '', 'HERMES_SANDBOX': '', 'CODEX_SANDBOX': '',
                'CLAUDE_SANDBOX': '', 'GEMINI_SANDBOX': '', 'HERMES_HEADLESS': '',
                'CLAUDE_HEADLESS': '', 'QMD_SETUP_GUARD_FIXTURE': '1'}))
            stack.enter_context(mock.patch.object(journal, '_verified_executing_root',
                                                   return_value=str(case.plugin)))
            stack.enter_context(mock.patch('laya_local_embed_provider.ReviewedLocalQmdProvider',
                                           HERMES.E2E['QueryProvider']))
            stack.enter_context(mock.patch('laya_embed_resident.admit_query', lambda **kw: None))
            for alias in ('working_directory', 'cwd'):
                with self.subTest(hook='pre_llm_call', alias=alias):
                    error, answer = None, None
                    try:
                        answer = hooks['pre_llm_call'](user_message='emerald heron',
                                                      **{alias: str(case.project)})
                    except TypeError as exc:
                        error = str(exc)
                    emit('R3-registered-preview', alias=alias, error=error, answer=answer,
                         baselineWrapperDefect=True)
                    self.assertIsNone(error, 'registered wrapper must pass cwd exactly once')
                    self.assertIn('concepts/heron.md', (answer or {}).get('context', ''))
        for hook, authority, kwargs in (
                ('on_session_start', 'session_update', {}),
                ('pre_tool_call', 'pre_edit_gate', {'tool_name': 'write_file', 'args': {'path': 'x.md'}}),
                ('post_tool_call', 'post_edit_sync', {'tool_name': 'write_file', 'args': {'path': 'x.md'},
                                                   'result': '{}', 'status': 'ok'})):
            for alias in ('working_directory', 'cwd'):
                with self.subTest(hook=hook, alias=alias):
                    captured = []
                    with mock.patch.object(plugin, authority,
                            side_effect=lambda **kw: captured.append(kw)):
                        error = None
                        try:
                            hooks[hook](**kwargs, **{alias: str(case.project)})
                        except TypeError as exc:
                            error = str(exc)
                    emit('R3-registered-wrapper', hook=hook, alias=alias, error=error,
                         captured=captured, baselineWrapperDefect=True)
                    self.assertIsNone(error)
                    self.assertEqual(len(captured), 1)
                    self.assertEqual(captured[0]['cwd'], str(case.project))

    @staticmethod
    def outside_snapshot(root):
        rows = {}
        for path in (root, *sorted(root.rglob('*'))):
            info = path.lstat()
            rows[str(path.relative_to(root))] = {'mode': stat.S_IMODE(info.st_mode),
                'kind': 'directory' if stat.S_ISDIR(info.st_mode) else 'file',
                **({'bytes': path.read_text(encoding='utf-8')} if stat.S_ISREG(info.st_mode) else {})}
        return rows

    def test_05_onboarding_ancestor_links_refuse_before_external_chmod_or_creation(self):
        for ancestor in range(3):
            for existing in (False, True):
                with self.subTest(ancestor=ancestor, existing=existing), tempfile.TemporaryDirectory(
                        prefix='choice-ancestor-', dir='/tmp') as temporary:
                    base = Path(temporary).resolve()
                    home, outside = base / 'home', base / 'outside'
                    home.mkdir(mode=0o700)
                    outside.mkdir(mode=0o755)
                    outside.chmod(0o755)
                    project = home / 'project'
                    (project / '.memvane').mkdir(parents=True)
                    (project / '.memvane' / 'settings.json').write_text('{"name":"ancestor","skipPaths":[]}')
                    directory = choice.choice_dir(home)
                    parts = directory.parent.relative_to(home).parts
                    self.assertEqual(len(parts), 3, 'use actual state_base shape')
                    link = home.joinpath(*parts[:ancestor + 1])
                    link.parent.mkdir(parents=True, exist_ok=True)
                    link.symlink_to(outside, target_is_directory=True)
                    if existing:
                        target = outside / directory.relative_to(link)
                        target.mkdir(parents=True, mode=0o755)
                        for node in (outside, *outside.rglob('*')):
                            if node.is_dir():
                                node.chmod(0o755)
                        (target / 'preserved').write_text('external marker')
                    (outside / 'root-marker').write_text('outside original bytes')
                    before = self.outside_snapshot(outside)
                    with mock.patch.dict(os.environ, {'HOME': str(home), 'QMD_SETUP_GUARD_FIXTURE': '1'}):
                        refused, result = None, None
                        try:
                            result = choice.resume(str(project), home=home)
                        except (ValueError, OSError) as exc:
                            refused = str(exc)
                    after = self.outside_snapshot(outside)
                    emit('R5-onboarding-ancestor', ancestor=str(link.relative_to(home)),
                         choiceDir=str(directory), existing=existing, refusal=refused,
                         result=result, before=before, after=after)
                    self.assertIsNotNone(refused, 'unsafe ancestor must explicitly refuse')
                    self.assertEqual(after, before, 'refusal must precede external mkdir/chmod/write')

    def test_11_selected_registered_hook_contains_host_response_import_failure(self):
        helper, case = self.preview()
        real_import, real_run = builtins.__import__, HERMES.REAL_RUN
        selector = ROOT / 'core' / 'laya_index_host_route.py'
        classifications, imports = [], []
        def observed_run(argv, *args, **kwargs):
            result = real_run(argv, *args, **kwargs)
            if len(argv) > 1 and Path(argv[1]) == selector:
                classifications.append({'argv': list(argv), 'state': result.stdout.strip(),
                                        'returncode': result.returncode})
            return result
        def selected_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == 'laya_host_response':
                imports.append({'name': name, 'fromlist': list(fromlist or ()),
                                'classified': list(classifications)})
                raise ImportError('synthetic laya_host_response import unavailable')
            return real_import(name, globals, locals, fromlist, level)
        escaped, answer = None, None
        with mock.patch.object(HERMES, 'REAL_RUN', observed_run), \
                mock.patch.object(builtins, '__import__', selected_import):
            try:
                answer, _calls, _preview = helper.invoke(case)
            except ImportError as exc:
                escaped = {'class': type(exc).__name__, 'message': str(exc)}
        emit('N2-selected-import-containment', escaped=escaped, answer=answer,
             classifications=classifications, importAttempts=imports)
        self.assertTrue(classifications, 'actual selector CLI must run before the import fault')
        self.assertEqual(classifications[0]['state'], 'preview')
        self.assertEqual(classifications[0]['returncode'], 0)
        self.assertTrue(imports)
        self.assertTrue(all(item['classified'] for item in imports), 'only post-selection imports fail')
        self.assertIsNone(escaped, 'an import error must never leave the registered hook')
        self.assertIn('request refused', (answer or {}).get('context', ''))
        self.assertNotIn('synthetic laya_host_response import unavailable', answer['context'])


if __name__ == '__main__':
    unittest.main()

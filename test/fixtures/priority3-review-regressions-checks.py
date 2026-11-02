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


CLEANUP = load('cleanup_fixture_for_review_regressions', 'legacy-cleanup-partial-checks.py')
HERMES = load('hermes_fixture_for_review_regressions', 'hermes-route-recovery-checks.py')
MOVE = load('move_fixture_for_review_regressions', 'laya-project-dir-move-checks.py')
import onboarding_choice as choice
import laya_initial_build as build
import laya_install_journal as journal
import release_coordination as release
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

    def test_01_cleanup_valid_internal_label_mismatch_preserves_plist_and_trees(self):
        case = self.compose(CLEANUP.CleanupPartialChecks)
        name, internal = CLEANUP.cleanup.LABELS[:2]
        plist = case.plist(name)
        raw = plistlib.dumps({'Label': internal, 'ProgramArguments': ['/usr/bin/true'],
                             'RunAtLoad': False}, fmt=plistlib.FMT_XML)
        raw = raw.replace(b'<plist version="1.0">',
                          ('<!-- ' + CLEANUP.cleanup.MARKER + ' -->\n<plist version="1.0">').encode(), 1)
        plist.write_bytes(raw)
        parsed = plistlib.loads(plist.read_bytes())
        self.assertEqual(parsed['Label'], internal, 'fixture must be valid plist XML')
        self.assertNotEqual(plist.stem, parsed['Label'])
        before = case.tree_bytes()
        def replies(argv):
            if argv[1] == 'bootout':
                return CLEANUP.subprocess.CompletedProcess(argv, 5, '', 'Boot-out failed: 5: Input/output error\n')
            if argv[-1] == 'gui/%d/%s' % (os.getuid(), name):
                return CLEANUP.subprocess.CompletedProcess(argv, 113, '',
                    'Bad request.\nCould not find service "%s" in domain for user gui: %d\n' % (name, os.getuid()))
            self.assertEqual(argv[-1], 'gui/%d/%s' % (os.getuid(), internal))
            return CLEANUP.subprocess.CompletedProcess(argv, 0, 'synthetic actual Label is still loaded\n', '')
        case.launch_reply = replies
        try:
            report = case.apply()
        except CLEANUP.cleanup.CleanupBlocked as exc:
            report = {'status': 'blocked', 'reason': str(exc)}
        emit('R1-valid-plist-label', filenameLabel=name, internalLabel=parsed['Label'],
             report=report, argv=case.commands, plistExists=plist.exists(),
             beforeTreeFiles=sorted(before), afterTreeFiles=sorted(case.tree_bytes()))
        self.assertTrue(plist.exists(), 'absence of the filename label must not authorize deleting another job')
        self.assertEqual(plist.read_bytes(), raw)
        case.assert_trees_preserved(before)
        self.assertNotEqual(report['status'], 'cleaned')

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

    def test_04_active_release_refuses_another_installs_valid_build_record(self):
        case = self.compose(MOVE.MoveCase)
        before = journal.inspect_journal(case.journal)
        self.assertEqual(before['state'], 'active')
        original = build.read(case.state)
        baseline = release._preferred_owner(MOVE.upgrade, str(case.state), str(case.project),
                                             str(case.plugin), 'active')
        self.assertEqual(baseline['pluginRoot'], str(case.plugin))
        foreign_project, foreign_state = case.home / 'foreign-project', case.home / 'foreign-state'
        foreign_project.mkdir(mode=0o700)
        foreign_state.mkdir(mode=0o700)
        identity = {**original['identity'], 'projectRoot': str(foreign_project),
                    'stateRoot': str(foreign_state), 'installId': 'a' * 32, 'scopeId': 'b' * 32}
        for key in ('candidateDb', 'queueDb', 'catalogDb', 'bindingStore', 'journalPath', 'routeFile'):
            identity[key] = str(foreign_state / Path(identity[key]).name)
        identity['collections'] = [
            {**item, 'sourceRoot': str(foreign_project / 'wiki') if item['role'] == 'wiki'
                                         else str(foreign_project)} for item in identity['collections']]
        build.create_or_verify(identity)
        valid_foreign = build.read(foreign_state)
        self.assertEqual(valid_foreign['identity'], identity, 'real schema must accept foreign record')
        # Copy an actually schema-valid record, not a hand-made invalid dict.
        (case.state / build.RECORD).write_bytes((foreign_state / build.RECORD).read_bytes())
        (case.state / build.RECORD).chmod(0o600)
        read_back = build.read(case.state)
        self.assertEqual(read_back, valid_foreign, 'counterexample must pass actual build.read')
        after = journal.inspect_journal(case.journal)
        self.assertEqual(before, after, 'the active selected journal remains unchanged')
        emit('R4-active-foreign-build', selectedProject=before['projectRoot'],
             selectedInstall=before['selected']['installId'], selectedScope=before['scopeId'],
             foreignIdentity={key: identity[key] for key in ('projectRoot', 'stateRoot', 'installId', 'scopeId')},
             buildPhase=read_back['phase'], buildEpoch=read_back['epoch'], journalEpoch=after['epoch'])
        with self.assertRaises(release.ReleaseError):
            release._preferred_owner(MOVE.upgrade, str(case.state), str(case.project), str(case.plugin), 'active')

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

    def test_06_sigterm_accepted_but_stop_timeout_keeps_partial_failure_receipt(self):
        case = self.compose(CLEANUP.CleanupPartialChecks)
        case.live = True
        before = case.tree_bytes()
        ticks = iter(range(1000))
        def accepted(pid, sig):
            self.assertEqual((pid, sig), (CLEANUP.PID, signal.SIGTERM))
            case.signals.append((pid, int(sig)))
            # Actual observation still reports this exact process alive.
        report = case.apply(kill=accepted, clock=lambda: next(ticks))
        emit('R6-signal-stop-timeout', report=report, signals=case.signals,
             remainingTreeFiles=sorted(case.tree_bytes()), launchctl=case.commands)
        self.assertEqual(report['status'], 'blocked')
        case.assert_trees_preserved(before)
        self.assertEqual(case.signals, [(CLEANUP.PID, int(signal.SIGTERM))])
        self.assertEqual(case.commands, [])
        self.assertEqual(report['changesApplied'], 1)
        self.assertEqual(report['stoppedPids'], [])
        daemon = next(row for row in report['daemons'] if row.get('pid') == CLEANUP.PID)
        self.assertTrue(daemon['signalSent'])
        self.assertEqual(daemon['stop'], 'stop_timeout')
        self.assertIs(report['partial'], True)
        self.assertTrue(any(row.get('pid') == CLEANUP.PID and row.get('reason')
                            for row in report['failed']), 'known stop failure must remain explicit')

    def test_07_valid_readable_plists_and_malformed_marker_only_files(self):
        for kind in ('xml-0644', 'binary-0644', 'malformed'):
            with self.subTest(kind=kind), ExitStack() as cleanup:
                case = self.compose(CLEANUP.CleanupPartialChecks)
                cleanup.callback(case.doCleanups)
                label = CLEANUP.cleanup.LABELS[0]
                plist = case.plist(label)
                if kind == 'binary-0644':
                    plist.write_bytes(plistlib.dumps({'Label': label,
                        'Comment': CLEANUP.cleanup.MARKER, 'ProgramArguments': ['/usr/bin/true']},
                        fmt=plistlib.FMT_BINARY))
                elif kind == 'malformed':
                    plist.write_text('<!-- ' + CLEANUP.cleanup.MARKER + ' -->\n<plist broken')
                if kind != 'malformed':
                    plist.chmod(0o644)
                    self.assertEqual(plistlib.loads(plist.read_bytes())['Label'], label)
                before = case.tree_bytes()
                modes = []
                def reply(argv):
                    modes.append(stat.S_IMODE(plist.stat().st_mode))
                    return CLEANUP.subprocess.CompletedProcess(argv, 0, '', '')
                case.launch_reply = reply
                report = case.apply()
                emit('R1-plist-compatibility', kind=kind, report=report, modesAtLaunch=modes)
                if kind == 'malformed':
                    self.assertNotEqual(report['status'], 'cleaned')
                    self.assertTrue(plist.exists())
                    self.assertEqual(case.commands, [])
                    case.assert_trees_preserved(before)
                else:
                    self.assertEqual(report['status'], 'cleaned')
                    self.assertEqual(modes, [0o644], 'validation must not chmod LaunchAgents')
                    self.assertFalse(plist.exists())

    def test_08_hermes_recent_normal_transition_is_quiet_only_inside_core_window(self):
        for recent in (False, True):
            with self.subTest(recent=recent), ExitStack() as cleanup:
                helper, case = self.preview()
                cleanup.callback(helper.doCleanups)
                def transition():
                    self.activate(case)
                    MOVE.upgrade._write_status(case.state, status='complete',
                        executingRoot=str(case.plugin), attempts=0, reason='active_upgrade_complete',
                        updatedNs=MOVE.upgrade._now() - (0 if recent else 31_000_000_000))
                answer, calls, _ = helper.invoke(case, after_classify=transition)
                emit('R2-normal-transition-window', recent=recent,
                     classified=calls[0]['stdout'], finalState=HERMES.route.select_route(str(case.project))['state'],
                     fenced=HERMES.fresh.upgrade_fenced(str(case.project)), answer=answer)
                self.assertEqual(calls[0]['stdout'], 'preview')
                self.assertFalse(HERMES.fresh.upgrade_fenced(str(case.project)))
                if recent:
                    self.assertIn(answer, (None, {}, {'context': ''}))
                else:
                    self.assertIn('request refused', (answer or {}).get('context', ''))

    def test_09_active_legacy_bundle_metadata_does_not_change_stable_build_identity(self):
        case = self.compose(MOVE.MoveCase)
        record = build.read(case.state)
        identity = {**record['identity']}
        for key, digit in (('routeSha256', '1'), ('attestationSha256', '2'),
                           ('proposalSha256', '3'), ('providerProposalSha256', '4')):
            identity[key] = digit * 64
        build._write_json(case.state / build.RECORD, {**record, 'identity': identity}, replace=True)
        validated = build.read(case.state)
        self.assertEqual(validated['identity'], identity)
        result = release._preferred_owner(MOVE.upgrade, str(case.state), str(case.project),
                                           str(case.plugin), 'active')
        emit('R4-old-metadata-compatible', result=result, stableInstall=identity['installId'],
             stableScope=identity['scopeId'], changedMetadata=['routeSha256', 'attestationSha256',
                                                            'proposalSha256', 'providerProposalSha256'])
        self.assertEqual(result, {'stateRoot': str(case.state), 'pluginRoot': str(case.plugin)})

    def test_10_actual_date_parser_failure_keeps_the_first_cleanup_cli_receipt(self):
        case = self.compose(CLEANUP.CleanupPartialChecks)
        first = case.plist(CLEANUP.cleanup.LABELS[0])
        second = case.plist(CLEANUP.cleanup.LABELS[1])
        raw = ('<?xml version="1.0" encoding="UTF-8"?>\n<!-- ' + CLEANUP.cleanup.MARKER + ' -->\n'
               '<plist version="1.0"><dict><key>Label</key><string>' + second.stem + '</string>'
               '<key>ProgramArguments</key><array><string>/usr/bin/true</string></array>'
               '<key>LastFixtureDate</key><date>invalid</date></dict></plist>').encode('utf-8')
        second.write_bytes(raw)
        second.chmod(0o600)
        # XML syntax, marker and job identity are valid independently of
        # plistlib's date conversion. Observe the actual interpreter exception.
        xml = ElementTree.fromstring(raw)
        self.assertEqual(xml.find('./dict/string').text, second.stem)
        self.assertEqual(xml.find('./dict/date').text, 'invalid')
        parser_error = None
        try:
            plistlib.loads(raw)
        except Exception as exc:  # observation only; never a fabricated parser result
            parser_error = {'class': type(exc).__module__ + '.' + type(exc).__name__,
                            'message': str(exc)}
        before = case.tree_bytes()
        code, report = case.cli()
        emit('N1-date-parser-partial-cli', python=sys.version, parserException=parser_error,
             cliCode=code, report=report, launchctl=case.commands,
             firstRemoved=not first.exists(), secondPreserved=second.exists(),
             treeFilesBefore=sorted(before), treeFilesAfter=sorted(case.tree_bytes()))
        self.assertIsNotNone(parser_error, 'the interpreter must actually reject this date counterexample')
        self.assertFalse(first.exists(), 'first valid job must actually unload and unlink')
        self.assertTrue(second.exists())
        self.assertEqual(second.read_bytes(), raw)
        case.assert_trees_preserved(before)
        self.assertEqual((code, report.get('status'), report.get('partial')), (1, 'partial', True))
        self.assertEqual(report.get('changesApplied'), 1)
        self.assertIn({'plist': str(first)}, report.get('completed', []))
        self.assertTrue(any(row.get('plist') == str(second) and row.get('reason')
                            for row in report.get('failed', [])))

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

    def test_12_replacement_pid_after_confirmed_stop_keeps_partial_blocked_receipt(self):
        case = self.compose(CLEANUP.CleanupPartialChecks)
        case.live = True
        plist = case.plist(CLEANUP.cleanup.LABELS[0])
        plist_before = plist.read_bytes()
        before = case.tree_bytes()
        replacement = CLEANUP.PID + 1
        observations = []
        def process_table():
            rows = ([(CLEANUP.PID, os.getuid(), case.command)] if not observations else
                    [(replacement, os.getuid(), case.command)])
            observations.append(rows)
            return rows
        case.ps_all = process_table
        report = case.apply()
        emit('N3-replacement-after-stop', report=report, processTables=observations,
             signals=case.signals, oldStillObserved=case.ps(CLEANUP.PID),
             replacementPid=replacement, launchctl=case.commands,
             treeFilesBefore=sorted(before), treeFilesAfter=sorted(case.tree_bytes()))
        self.assertGreaterEqual(len(observations), 2)
        self.assertEqual(case.signals, [(CLEANUP.PID, int(signal.SIGTERM))])
        self.assertIsNone(case.ps(CLEANUP.PID), 'actual _stop must confirm the old pid absent')
        self.assertEqual(report.get('status'), 'blocked')
        self.assertEqual(report.get('changesApplied'), 1)
        self.assertEqual(report.get('stoppedPids'), [CLEANUP.PID])
        daemon = next(row for row in report['daemons'] if row.get('pid') == CLEANUP.PID)
        self.assertEqual(daemon['stop'], 'stopped')
        self.assertTrue(any(row.get('pid') == replacement and row.get('command') == case.command
                            for row in report.get('unverifiedProcesses', [])))
        case.assert_trees_preserved(before)
        self.assertEqual(plist.read_bytes(), plist_before)
        self.assertEqual(case.commands, [])
        self.assertIs(report.get('partial'), True)
        self.assertTrue(any(row.get('pid') == replacement and row.get('reason')
                            for row in report.get('failed', [])), 'replacement failure must remain explicit')


if __name__ == '__main__':
    unittest.main()

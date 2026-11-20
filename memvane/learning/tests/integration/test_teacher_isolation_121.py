"""1.0.21 I: isolation pre-check through the worker path with fake CLIs."""
from __future__ import annotations

import builtins
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from unittest import mock

from memvane.learning.context_learning import (claude_adapter, codex_adapter, general_teacher,
                                               teacher_hosts, teacher_isolation as isolation)
from memvane.learning.tests.fake_teacher_cli_121 import TeacherFixture

CLAUDE_GOLDEN = [
    '-p', '--model', 'haiku', '--effort', 'low', '--safe-mode', '--tools', '',
    '--disallowedTools', '*', '--disable-slash-commands', '--strict-mcp-config',
    '--mcp-config', '{"mcpServers":{}}', '--setting-sources', '',
    '--settings', isolation.CLAUDE_SETTINGS, '--system-prompt', general_teacher.INSTRUCTION_V2,
    '--no-session-persistence', '--no-chrome', '--permission-prompts', 'none',
    '--output-format', 'stream-json', '--input-format', 'stream-json', '--verbose',
    '--include-hook-events', '--max-turns', '1']
CODEX_GOLDEN = (['exec', '--ignore-user-config', '--ignore-rules', '--ephemeral',
                 '--skip-git-repo-check', '--sandbox', 'read-only', '--json', '--color', 'never',
                 '--model', 'gpt-6-luna']
                + [part for override in isolation.CODEX_OVERRIDES for part in ('-c', override)]
                + ['-'])


def _drop(argv, index, width):
    return argv[:index] + argv[index + width:]


def claude_variants():
    tail = CLAUDE_GOLDEN
    out = []
    index = 0
    while index < len(tail):
        flag = tail[index]
        width = 2 if flag in isolation.CLAUDE_VALUE_FLAGS else 1
        out.append(('drop ' + flag, _drop(tail, index, width)))
        if width == 2:
            changed = list(tail)
            changed[index + 1] = changed[index + 1] + 'x'
            out.append(('change ' + flag, changed))
        index += width
    for flag in isolation.CLAUDE_FORBIDDEN + ('--dangerously-skip-permissions',):
        extra = [flag] + (['/tmp'] if flag in ('--add-dir', '--plugin-dir', '-r', '--resume',
                                               '--agents', '--append-system-prompt') else [])
        out.append(('add ' + flag, tail + extra))
    return out


def codex_variants():
    tail = CODEX_GOLDEN
    out = [('drop exec', tail[1:]), ('drop -', tail[:-1])]
    index = 1
    while index < len(tail) - 1:
        flag = tail[index]
        width = 2 if flag in isolation.CODEX_VALUE_FLAGS else 1
        label = flag + (' ' + tail[index + 1] if flag == '-c' else '')
        out.append(('drop ' + label, _drop(tail, index, width)))
        if width == 2:
            changed = list(tail)
            changed[index + 1] = changed[index + 1].replace('false', 'true').replace('"', "'") + 'x'
            out.append(('change ' + label, changed))
        index += width
    for flag in isolation.CODEX_FORBIDDEN + ('--dangerously-bypass-approvals-and-sandbox',):
        extra = [flag] + (['/tmp'] if flag in ('--add-dir', '-C', '--cd', '--profile', '-p',
                                               '--image', '-i') else [])
        out.append(('add ' + flag, tail[:-1] + extra + ['-']))
    out.append(('add -c extra', tail[:-1] + ['-c', 'features.shell_tool=true', '-']))
    out.append(('repeat -c', tail[:-1] + ['-c', isolation.CODEX_OVERRIDES[0], '-']))
    return out


class Isolation(TeacherFixture):
    def test_i1_golden_argv(self):
        built = teacher_hosts.build_argv('claude', '/x/claude', 'haiku', general_teacher.INSTRUCTION_V2)
        self.assertEqual(built[1:], CLAUDE_GOLDEN)
        built = teacher_hosts.build_argv('codex', '/x/codex', 'gpt-6-luna', general_teacher.INSTRUCTION_V2)
        self.assertEqual(built[1:], CODEX_GOLDEN)
        self.assertEqual(codex_adapter.OVERRIDES, isolation.CODEX_OVERRIDES)
        self.assertEqual(len(isolation.CODEX_OVERRIDES), 19)
        self.add('cl', host='claude', tag='c')
        self.add('cx', host='codex', tag='x')
        self.run_pass()
        self.assertEqual(self.records('claude')[0]['argv'], CLAUDE_GOLDEN)
        self.assertEqual(self.records('codex')[0]['argv'], CODEX_GOLDEN)

    def _variants_never_run(self, host, variants):
        self.add('case', host=host, tag=host)
        real = teacher_hosts.build_argv
        for label, tail in variants:
            with self.subTest(variant=label):
                with self.assertRaises(isolation.IsolationRefused):
                    isolation.check_argv(host, ['/x/' + host] + tail,
                                         model='haiku' if host == 'claude' else 'gpt-6-luna',
                                         system_prompt=general_teacher.INSTRUCTION_V2)
                with mock.patch.object(teacher_hosts, 'build_argv',
                                       side_effect=lambda h, exe, *a: [exe] + tail):
                    result = self.run_pass()
                self.assertEqual(result['hosts'][host], isolation.REASON)
                self.assertEqual(self.runs(), 0)
                self.assertEqual(self.cases()['case'][0], 'queued')
                self.assertIsNone(self.stage('case'))
        refused = [e for e in self.teacher_events() if e.get('isolation') == 'refused']
        self.assertEqual(len(refused), len(variants))
        self.run_pass()
        self.assertEqual(self.runs(host), 1)
        self.assertIs(teacher_hosts.build_argv, real)

    def test_i1_every_claude_variant_runs_nothing(self):
        variants = claude_variants()
        self.assertGreaterEqual(len(variants), 40)
        self._variants_never_run('claude', variants)

    def test_i1_every_codex_variant_runs_nothing(self):
        variants = codex_variants()
        drops = [label for label, _ in variants if label.startswith('drop -c ')]
        self.assertEqual(len(drops), 19)
        self._variants_never_run('codex', variants)

    def test_i1_per_call_check_also_refuses(self):
        self.add('case', tag='z')
        made = []

        def folder():
            path = tempfile.mkdtemp(prefix='memvane-teacher-')
            made.append(path)
            if len(made) == 2:
                Path(path, 'stray').write_text('x')
            return path
        with mock.patch.object(teacher_hosts, '_new_folder', side_effect=folder):
            result = self.run_pass()
        self.assertEqual(result['hosts']['claude'], isolation.REASON)
        self.assertEqual(self.runs(), 0)
        self.assertIsNone(self.stage('case'))
        self.assertFalse(any(os.path.exists(path) for path in made))

    def test_i2_env_and_folder_checks(self):
        self.add('case', tag='q')
        base = teacher_hosts.child_environment
        for extra in ({'OPENAI_API_KEY': 'x'}, {'CLAUDE_CONFIG_DIR': '/tmp'}, {'QMD_SANDBOX': '0'},
                      {'CLAUDE_CODE_MAX_RETRIES': '3'}, {'CODEX_HOME': '/tmp'}):
            with self.subTest(extra=extra):
                with mock.patch.object(teacher_hosts, 'child_environment',
                                       side_effect=lambda h, e: dict(base(h, e), **extra)):
                    result = self.run_pass()
                self.assertEqual(result['hosts']['claude'], isolation.REASON)
                self.assertEqual(self.runs(), 0)
        for marker in ('.git', 'CLAUDE.md', 'AGENTS.md', '.claude', '.codex', '.mcp.json', 'stray'):
            with self.subTest(marker=marker):
                parent = Path(tempfile.mkdtemp(prefix='p', dir=self.tmp))

                def folder(parent=parent, marker=marker):
                    path = Path(tempfile.mkdtemp(prefix='memvane-teacher-', dir=parent))
                    target = path if marker == 'stray' else parent
                    (target / marker).mkdir() if marker.startswith('.') and marker != '.mcp.json' else (target / marker).write_text('x')
                    return str(path)
                with mock.patch.object(teacher_hosts, '_new_folder', side_effect=folder):
                    result = self.run_pass()
                self.assertEqual(result['hosts']['claude'], isolation.REASON)
                self.assertEqual(self.runs(), 0)
        self.run_pass()
        self.assertEqual(self.runs(), 1)
        env = self.records('claude')[0]['env']
        self.assertEqual(env['HOME'], str(self.user))
        self.assertEqual(env['QMD_SANDBOX'], '1')
        self.assertNotIn('CODEX_HOME', env)
        record = self.records('claude')[0]
        self.assertEqual(record['entries'], [])

    def test_i3_claude_catalog_and_events(self):
        # Plugins are a warning (test_i3_plugins_left_by_the_settings_are_a_warning).
        names = ('tools', 'mcp_servers', 'skills')
        for index, name in enumerate(names):
            with self.subTest(catalog=name, before_body=True):
                rid = f'pre-{name}'
                self.add(rid, tag=rid)
                self.scenario('claude', {'initBeforeBody': True, name: ['x']})
                result = self.run_pass()
                self.assertEqual(len(self.records('claude', 'body')), 0)
                self.assertEqual(result['hosts']['claude'], isolation.REASON)
                self.assertEqual(self.cases()[rid][0], 'queued')
                self.scenario('claude', {})
                self.run_pass()
                self.assertEqual(self.cases()[rid][0], 'gold')
                (self.logs / 'calls.jsonl').unlink()
        for index, scenario in enumerate([{'tools': ['Read']}, {'mode': 'hook'}, {'mode': 'tool_event'}]):
            with self.subTest(after_body=scenario):
                rid = f'post-{index}'
                self.add(rid, tag=rid)
                self.scenario('claude', scenario)
                self.run_pass()
                self.assertEqual(self.cases()[rid][:2], ('pending_review', 'gold_attempt_failed'))
                violated = [e for e in self.teacher_events() if e.get('request_id') == rid]
                self.assertEqual(violated[-1]['isolation'], 'violated')
        rid = 'policy'
        self.add(rid, tag=rid)
        self.scenario('claude', {'plugins': [{'name': 'cc-plugin-sec-default',
                                              'source': 'cc-plugin-sec-default@builtin'}]})
        from memvane.learning.context_learning import teacher_state
        state = teacher_state.read_state(self.root)
        state['hosts']['claude'].update(consecutiveFailures=0, waitUntilUnixMs=None, reason=None)
        self.assertTrue(teacher_state.write_state(self.root, state))
        self.run_pass()
        self.assertEqual(self.cases()[rid][0], 'gold')
        self.add('commands', tag='commands')
        self.scenario('claude', {'commands': [{'name': 'review'}]})
        self.run_pass()
        self.assertEqual(self.cases()['commands'][0], 'queued')

    def test_i3_plugins_left_by_the_settings_are_a_warning(self):
        """User decision: any plugin still in init is a warning by id; the label is kept."""
        import json as _json
        observed = [{'name': 'cc-plugin-sec-default', 'path': 'builtin',
                     'source': 'cc-plugin-sec-default@builtin'},
                    {'name': 'cc-plugin-privacy', 'path': 'builtin',
                     'source': 'cc-plugin-privacy@builtin'}]
        cases = [('observed', observed,
                  ['cc-plugin-privacy@builtin', 'cc-plugin-sec-default@builtin']),
                 ('mixed', observed + [{'name': 'helper', 'source': 'helper@market'},
                                       {'name': 'bare'}, {'path': '/x'}],
                  ['bare', 'cc-plugin-privacy@builtin', 'cc-plugin-sec-default@builtin',
                   'helper@market', 'unidentified'])]
        for rid, plugins, ids in cases:
            with self.subTest(plugins=rid):
                self.add(rid, tag=rid)
                self.scenario('claude', {'plugins': plugins})
                self.run_pass()
                self.assertEqual(self.cases()[rid][0], 'gold')
                gold = _json.loads(self.cases()[rid][5])
                self.assertEqual(gold['teacher']['pluginWarnings'], ids)
                row = [e for e in self.teacher_events() if e.get('request_id') == rid][-1]
                self.assertEqual((row['outcome'], row['isolation'], row['pluginWarnings']),
                                 ('gold', 'ok', ids))
        from memvane.learning.context_learning import teacher_state
        self.assertEqual(teacher_state.read_state(self.root)['hosts']['claude']['consecutiveFailures'], 0)
        argv = self.records('claude')[0]['argv']
        settings = _json.loads(argv[argv.index('--settings') + 1])
        self.assertIs(settings['enabledPlugins']['cc-plugin-privacy@builtin'], False)

    def test_i3_claude_pre_send_controls_gate_the_body(self):
        """MCP, tool, skill and memory content are refused before any body byte."""
        from memvane.learning.context_learning import teacher_state
        self.add('ok', tag='ok')
        self.run_pass()
        self.assertEqual(self.cases()['ok'][0], 'gold')
        self.assertEqual(self.records('claude')[0]['controls'],
                         ['initialize', 'mcp_status', 'get_context_usage'])
        refusals = [
            ('mcp', {'mcpServers': [{'name': 'x', 'status': 'connected'}]}),
            ('tools', {'context': {'categories': [{'id': 'system_prompt', 'tokens': 9},
                                                  {'id': 'system_tools', 'tokens': 900}]}}),
            ('mcp-tools', {'context': {'mcpTools': [{'name': 'x'}]}}),
            ('memory', {'context': {'memoryFiles': [{'path': 'CLAUDE.md'}]}}),
            ('agents', {'context': {'agents': [{'name': 'x'}]}}),
            ('unsupported', {'unsupported': ['get_context_usage']}),
        ]
        for name, scenario in refusals:
            with self.subTest(refusal=name):
                (self.logs / 'calls.jsonl').unlink(missing_ok=True)
                state = teacher_state.read_state(self.root)
                state['hosts']['claude'].update(consecutiveFailures=0, waitUntilUnixMs=None, reason=None)
                self.assertTrue(teacher_state.write_state(self.root, state))
                rid = 'pre-' + name
                self.add(rid, tag=rid)
                self.scenario('claude', scenario)
                result = self.run_pass()
                self.assertEqual(result['hosts']['claude'], isolation.REASON)
                self.assertEqual(len(self.records('claude', 'body')), 0)
                self.assertEqual(self.cases()[rid][0], 'queued')
        self.scenario('claude', {'context': {'model': 'claude-sonnet-5-5'}})
        (self.logs / 'calls.jsonl').unlink(missing_ok=True)
        self.add('model', tag='model')
        result = self.run_pass()
        self.assertEqual(len(self.records('claude', 'body')), 0)
        self.assertEqual(self.cases()['model'][0], 'queued')

    def test_i3_unanswered_pre_send_control_rests_the_host_only(self):
        """No answer to a pre-send control (exit or timeout) never burns the case."""
        from memvane.learning.context_learning import general_gold_ledger as ledger, shared, teacher_state
        self.proposal['timeoutSeconds'] = 1
        for name, scenario in (('exit', {'silent': ['mcp_status'], 'silentExit': True}),
                               ('timeout', {'silent': ['get_context_usage'], 'seconds': 10})):
            with self.subTest(silent=name):
                state = teacher_state.read_state(self.root)
                state['hosts']['claude'].update(consecutiveFailures=0, waitUntilUnixMs=None, reason=None)
                self.assertTrue(teacher_state.write_state(self.root, state))
                (self.logs / 'calls.jsonl').unlink(missing_ok=True)
                rid = 'silent-' + name
                self.add(rid, tag=rid)
                self.scenario('claude', scenario)
                for _ in range(6):
                    result = self.run_pass()
                    self.assertEqual(result['hosts']['claude'], isolation.REASON)
                    state = teacher_state.read_state(self.root)
                    state['hosts']['claude'].update(waitUntilUnixMs=None, reason=None)
                    self.assertTrue(teacher_state.write_state(self.root, state))
                self.assertEqual(len(self.records('claude', 'body')), 0)
                self.assertEqual(self.cases()[rid][0], 'queued')
                with shared._db(self.root) as db:
                    self.assertEqual(ledger.retries(db, shared._project_key(self.project), rid), 0)
                self.assertEqual(teacher_state.read_state(self.root)['hosts']['claude']['consecutiveFailures'], 0)
                # Queued cases keep their capture order, so the earliest queued case is
                # the one each pass tries; every one of them stays queued.
                row = [e for e in self.teacher_events() if e.get('outcome') == 'pre_send_rejected'][-1]
                self.assertEqual((row['isolation'], row.get('isolationDetail')),
                                 ('refused', 'control_' + ('incomplete' if name == 'exit' else 'timeout')))
                with shared._db(self.root) as db:
                    for other in [c for c in self.cases() if c.startswith('silent-')]:
                        self.assertEqual(ledger.retries(db, shared._project_key(self.project), other), 0)
                        self.assertEqual(self.cases()[other][0], 'queued')

    def test_i4_user_home_kept_and_auth_never_opened(self):
        auth = self.user / '.codex' / 'auth.json'
        auth.write_text('{"fixture": true}')
        self.environ['CODEX_HOME'] = str(self.user / '.codex')
        self.add('x', host='codex')
        opened = []
        real_open, real_os_open, real_io = builtins.open, os.open, io.open

        def watch(fn):
            def wrapped(path, *a, **k):
                opened.append(str(path))
                return fn(path, *a, **k)
            return wrapped
        with mock.patch.object(builtins, 'open', watch(real_open)), \
                mock.patch.object(os, 'open', watch(real_os_open)), \
                mock.patch.object(io, 'open', watch(real_io)):
            self.run_pass()
        self.assertEqual(self.cases()['x'][0], 'gold')
        self.assertFalse([path for path in opened if path.endswith('auth.json')])
        env = self.records('codex')[0]['env']
        self.assertEqual((env['HOME'], env['CODEX_HOME']), (str(self.user), str(self.user / '.codex')))

    def test_i5_codex_tool_items_warn_and_gold_commits(self):
        self.add('x', host='codex')
        self.scenario('codex', {'tools': [['command_execution', None], ['mcp_tool_call', 'search'],
                                          ['command_execution', None]]})
        self.run_pass()
        self.assertEqual(self.cases()['x'][0], 'gold')
        self.assertEqual(self.stage('x'), 'committed')
        self.assertEqual(self.host_state()['hosts']['codex']['consecutiveFailures'], 0)
        event = self.teacher_events('gold')[0]
        self.assertEqual(event['toolWarnings'], [{'name': 'command_execution', 'count': 2},
                                                 {'name': 'mcp_tool_call:search', 'count': 1}])
        text = json.dumps(self.events)
        self.assertNotIn('/secret/tool-argument', text)
        self.assertNotIn('tool-output-text', text)
        item = {'type': 'item.started', 'item': {'id': 't', 'type': 'command_execution'}}
        with self.assertRaisesRegex(ValueError, 'unexpected_action_item'):
            codex_adapter.inspect_event(item)
        self.assertEqual(codex_adapter.inspect_event(item, count_tools=True)['tool_item'],
                         {'id': 't', 'name': 'command_execution'})

    def test_i6_precheck_starts_nothing_and_mcp_config_does_not_block(self):
        (self.user / '.codex' / 'config.toml').write_text('[mcp_servers.fixture]\ncommand = "x"\n')
        folder = teacher_hosts._new_folder()
        args = teacher_hosts.build_argv('codex', str(self.bin / 'codex'), 'gpt-6-luna',
                                        general_teacher.INSTRUCTION_V2)
        env = teacher_hosts.child_environment('codex', self.environ)
        with mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('spawn')), \
                mock.patch.object(subprocess, 'run', side_effect=AssertionError('spawn')):
            isolation.assert_isolated('codex', args, env, folder, model='gpt-6-luna',
                                      parent=self.environ, project=self.project)
        os.rmdir(folder)
        self.add('x', host='codex')
        self.run_pass()
        self.assertEqual(self.cases()['x'][0], 'gold')
        self.assertFalse([r for r in self.records('codex', None) if 'debug' in r.get('argv', [])])

    def test_i7_user_instruction_size_only(self):
        self.add('a', host='codex', tag='a')
        self.run_pass()
        self.assertEqual(self.teacher_events('gold')[0]['userInstructionsBytes'], 0)
        secret = 'Always answer in French. zq-instruction-marker\n'
        (self.user / '.codex' / 'AGENTS.md').write_text(secret)
        self.add('b', host='codex', tag='b')
        self.run_pass()
        self.assertEqual(self.teacher_events('gold')[1]['userInstructionsBytes'], len(secret.encode()))
        self.assertNotIn('zq-instruction-marker', json.dumps(self.events))

    def test_i8_no_variable_skips_the_check(self):
        source = Path(isolation.__file__).read_text()
        self.assertNotIn('os.environ', source)
        self.assertNotIn('getenv', source)
        self.add('case', tag='i8')
        for name in ('MEMVANE_TEACHER_ISOLATION', 'MEMVANE_SKIP_ISOLATION', 'QMD_TEACHER_UNSAFE',
                     'MEMVANE_TEACHER_SKIP_PRECHECK', 'QMD_SKIP_ISOLATION'):
            self.environ[name] = '0'
            os.environ[name] = 'off'
            self.addCleanup(os.environ.pop, name, None)
        broken = CLAUDE_GOLDEN[:]
        broken.remove('--safe-mode')
        with mock.patch.object(teacher_hosts, 'build_argv', side_effect=lambda h, exe, *a: [exe] + broken):
            result = self.run_pass()
        self.assertEqual(result['hosts']['claude'], isolation.REASON)
        self.assertEqual(self.runs(), 0)


class TemporaryFolderBase(TeacherFixture):
    def test_tmpdir_under_a_marked_home_still_gets_a_clean_folder(self):
        marked = Path(tempfile.mkdtemp(prefix='marked-home-')).resolve()
        self.addCleanup(lambda: __import__('shutil').rmtree(marked, ignore_errors=True))
        (marked / '.claude').mkdir()
        nested = marked / 'tmp'
        nested.mkdir()
        with mock.patch.object(tempfile, 'tempdir', str(nested)):
            folder = Path(teacher_hosts._new_folder())
        self.addCleanup(lambda: __import__('shutil').rmtree(folder, ignore_errors=True))
        self.assertNotIn(marked, folder.resolve().parents)
        self.assertTrue(teacher_hosts._clean_base(folder.parent))
        self.assertFalse(teacher_hosts._clean_base(nested))


if __name__ == '__main__':
    import unittest
    unittest.main()

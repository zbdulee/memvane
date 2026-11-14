"""The user's answer to the SessionStart onboarding notice: skip one session, never, resume.

The notice and the choice record are driven in process (``onboarding_notice``
and the host api). One process test per boundary keeps the real entry points:
``hooks/run-hook`` (notice and the silent actions of a declined project),
``entry/setup.py onboarding-*`` (answers and exit codes) and the exact answer
commands the notice names.
"""
import json
import os
from pathlib import Path
import subprocess
import time
import unittest

from memvane.host import api as host
from memvane.host import onboarding_choice as choice
from memvane.host import onboarding_notice
from tests.support.case import FixtureCase

ROOT = Path(__file__).resolve().parents[4]
OTHER_ACTIONS = ('recall', 'update', 'gate', 'posttool', 'index', 'compile', 'file-change-event')
AGENT_PREFIX = '[memvane] onboarding-answer '
ASK = 'setup skill로 온보딩'
DAY_MS = 24 * 60 * 60 * 1000


class OnboardingChoice(FixtureCase):
    def make(self, name, *, settings=True):
        project = self.home / name
        (project / '.memvane').mkdir(parents=True)
        if settings:
            (project / '.memvane' / 'settings.json').write_text(
                json.dumps({'name': 'fixture', 'skipPaths': []}), encoding='utf-8')
        return str(project)

    def payload(self, project, session=None):
        body = {'hook_event_name': 'SessionStart', 'cwd': project, 'prompt': 'x'}
        if session:
            body['session_id'] = session
        path = self.tmp / 'session-start.json'
        path.write_text(json.dumps(body), encoding='utf-8')
        return path

    def notice(self, project, session=None):
        """The SessionStart notice text ('' when silent), as run-hook prints it for Claude."""
        return onboarding_notice.onboarding_notice(str(self.payload(project, session)), project) or ''

    def parts(self, text):
        lines = [line for line in text.split('\n') if line]
        self.assertEqual(len(lines), 2, text)
        self.assertTrue(lines[1].startswith(AGENT_PREFIX), text)
        return lines[0], json.loads(lines[1][len(AGENT_PREFIX):])

    def env(self):
        # The live-write guard of entry/setup.py admits an uninstalled checkout
        # only with the test runtime root (FixtureCase) and this opt-in.
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'MEMVANE_TEST_ALLOW_UNINSTALLED_ROOT': '1'}
        for key in ('QMD_SANDBOX', 'CODEX_SANDBOX', 'CLAUDE_SANDBOX', 'CLAUDE_HEADLESS',
                    'GEMINI_SANDBOX'):
            env.pop(key, None)
        return env

    def hook(self, action, engine, project, session=None):
        body = {'hook_event_name': 'SessionStart', 'cwd': project, 'prompt': 'x'}
        if session:
            body['session_id'] = session
        return subprocess.run(['bash', str(ROOT / 'hooks' / 'run-hook'), action, engine],
                              input=json.dumps(body), capture_output=True, text=True, timeout=60,
                              env=self.env(), cwd=str(ROOT))

    def setup_entry(self, action, project, *extra):
        result = subprocess.run(['python3', '-B', str(ROOT / 'entry' / 'setup.py'), action,
                                 '--project', project, *extra],
                                capture_output=True, text=True, timeout=60, env=self.env(),
                                cwd=str(ROOT))
        try:
            body = json.loads(result.stdout)
        except ValueError:
            body = None
        return result.returncode, body, result.stderr

    def record(self, project):
        return choice.record_path(project, self.home)

    def seed(self, project, value):
        path = self.record(project)
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value), encoding='utf-8')
        path.chmod(0o600)
        return path

    # -- process boundaries ----------------------------------------------------------
    def test_session_start_through_run_hook_names_the_choices_for_both_hosts(self):
        project = self.make('alpha')
        claude = self.hook('update-queued', 'claude', project, 'sess-1')
        self.assertEqual((claude.returncode, claude.stderr), (0, ''))
        self.assertEqual(claude.stdout, self.notice(project, 'sess-1') + '\n')
        codex = self.hook('update-queued', 'codex', project, 'sess-1')
        self.assertEqual(codex.returncode, 0, codex.stderr)
        event = json.loads(codex.stdout)
        self.assertEqual(event['hookSpecificOutput']['hookEventName'], 'SessionStart')
        self.assertEqual(event['hookSpecificOutput']['additionalContext'],
                         self.notice(project, 'sess-1'))
        host.never(project)
        for engine in ('claude', 'codex'):
            with self.subTest(engine=engine):
                quiet = self.hook('update-queued', engine, project, 'sess-3')
                self.assertEqual((quiet.returncode, quiet.stdout), (0, ''))

    def test_setup_entry_records_answers_with_exit_codes(self):
        project = self.make('alpha')
        code, body, err = self.setup_entry('onboarding-never', project)
        self.assertEqual((code, body['status']), (0, 'onboarding_notice_never'), err)
        self.assertEqual(self.setup_entry('onboarding-status', project)[1]['choice'], 'never')
        code, body, _err = self.setup_entry('onboarding-skip', project)
        self.assertEqual((code, body['reason']), (1, 'onboarding_skip_session_required'))
        code, body, _err = self.setup_entry('onboarding-skip', project, '--session', 'a b;rm')
        self.assertEqual((code, body['reason']), (1, 'onboarding_skip_session_required'))
        code, body, _err = self.setup_entry('onboarding-resume', project)
        self.assertEqual((code, body['status'], body['changesApplied']),
                         (0, 'onboarding_notice_enabled', 1))
        plain = self.make('plain', settings=False)
        code, body, _err = self.setup_entry('onboarding-never', plain)
        self.assertEqual((code, body['reason']), (1, 'onboarding_settings_required'))

    def test_declining_never_selects_a_route_or_creates_index_wiki_or_learning_state(self):
        project = self.make('alpha')
        host.skip(project, 'sess-1')
        host.never(project)
        classify = subprocess.run(['python3', '-B', str(ROOT / 'entry' / 'run.py'), 'classify',
                                   'update-queued', project], capture_output=True, text=True,
                                  timeout=60, env=self.env(), cwd=str(ROOT))
        self.assertEqual(classify.stdout.strip(), 'legacy', classify.stderr)
        for action in OTHER_ACTIONS:
            with self.subTest(action=action):
                result = self.hook(action, 'claude', project, 'sess-1')
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        self.assertEqual(os.listdir(Path(project) / '.memvane'), ['settings.json'])
        self.assertEqual(sorted(os.listdir(self.home)), ['.local', 'alpha', 'rt', 'tmp'])
        record = self.record(project)
        lock = record.with_suffix('.lock')
        written = sorted(str(path.relative_to(self.home)) for path in (self.home / '.local').rglob('*')
                         if path.is_file())
        self.assertEqual(written, sorted(str(path.relative_to(self.home)) for path in (record, lock)))
        info = os.lstat(lock)
        self.assertEqual((oct(info.st_mode & 0o777), info.st_uid, info.st_nlink),
                         (oct(0o600), os.getuid(), 1))
        value = json.loads(record.read_text(encoding='utf-8'))
        self.assertEqual((value['projectRoot'], value['never']), (str(Path(project).resolve()), True))

    def test_the_commands_the_notice_names_work_for_odd_paths(self):
        for index, name in enumerate(["my project's", 'tick`s `x`', 'line\nbreak',
                                      'fence\n```\nend']):
            with self.subTest(name=name):
                project = self.make(name)
                session = f'sess-{index}'
                notice, answer = self.parts(self.notice(project, session))
                self.assertNotIn(project, notice)
                self.assertEqual(answer['projectRoot'], project)
                skip = subprocess.run(answer['skip']['argv'], capture_output=True, text=True,
                                      timeout=60, env=self.env())
                self.assertEqual(skip.returncode, 0, skip.stdout + skip.stderr)
                self.assertEqual(self.notice(project, session), '')
                never = subprocess.run(['bash', '-c', answer['never']['shell']], capture_output=True,
                                       text=True, timeout=60, env=self.env())
                self.assertEqual(never.returncode, 0, never.stdout + never.stderr)
                self.assertEqual(self.notice(project, 'other-session'), '')

    # -- in process --------------------------------------------------------------------
    def test_never_silences_the_notice_in_every_later_session(self):
        project = self.make('alpha')
        self.assertIn(ASK, self.notice(project, 'sess-1'))
        self.assertEqual(host.never(project)['status'], 'onboarding_notice_never')
        for session in ('sess-1', 'sess-2', None):
            self.assertEqual(self.notice(project, session), '')
        self.assertEqual(host.status(project)['choice'], 'never')

    def test_skip_silences_only_the_session_that_asked(self):
        project = self.make('alpha')
        notice, answer = self.parts(self.notice(project, 'sess-1'))
        for phrase in (ASK, '이번 세션만 건너뛰기', '다시 묻지 않기'):
            self.assertIn(phrase, notice)
        self.assertNotIn(project, notice)
        self.assertNotIn('`', notice)
        self.assertLessEqual(len(notice), 120)
        self.assertEqual(answer['projectRoot'], project)
        self.assertEqual(answer['skip']['argv'][-4:], ['--project', project, '--session', 'sess-1'])
        self.assertIn('onboarding-skip', answer['skip']['argv'])
        self.assertIn('onboarding-never', answer['never']['argv'])
        self.assertEqual(host.skip(project, 'sess-1')['status'], 'onboarding_notice_skipped')
        self.assertEqual(self.notice(project, 'sess-1'), '')
        self.assertIn(ASK, self.notice(project, 'sess-2'))
        self.assertIn(ASK, self.notice(project, None))

    def test_skip_needs_a_session_id_and_never_turns_into_never(self):
        project = self.make('alpha')
        notice, answer = self.parts(self.notice(project, None))
        self.assertIn(ASK, notice)
        self.assertNotIn('건너뛰기', notice)
        self.assertNotIn('skip', answer)
        self.assertIn('onboarding-never', answer['never']['argv'])
        for session in (None, 'a b;rm'):
            with self.assertRaisesRegex(ValueError, '^onboarding_skip_session_required$'):
                host.skip(project, session)
        self.assertFalse(self.record(project).exists())
        self.assertIn(ASK, self.notice(project, 'sess-9'))

    def test_a_declined_project_does_not_change_another_projects_notice(self):
        alpha, beta = self.make('alpha'), self.make('beta')
        host.never(alpha)
        host.skip(str(self.home / 'alpha'), 'sess-1')
        self.assertEqual(self.notice(alpha, 'sess-1'), '')
        other = self.notice(beta, 'sess-1')
        self.assertIn(ASK, other)
        self.assertIn(beta, other)

    def test_an_explicit_onboarding_request_re_enables_the_notice(self):
        project = self.make('alpha')
        host.never(project)
        self.assertEqual(self.notice(project, 'sess-1'), '')
        resumed = host.resume(project)
        self.assertEqual((resumed['status'], resumed['changesApplied']), ('onboarding_notice_enabled', 1))
        self.assertIn(ASK, self.notice(project, 'sess-1'))
        self.assertEqual(host.status(project)['choice'], 'ask')
        self.assertEqual(host.resume(project)['changesApplied'], 0)

    def test_a_folder_without_settings_stays_silent_and_cannot_record(self):
        project = self.make('plain', settings=False)
        self.assertEqual(self.notice(project, 'sess-1'), '')
        with self.assertRaisesRegex(ValueError, '^onboarding_settings_required$'):
            host.never(project)
        self.assertFalse((self.home / '.local').exists())

    def test_an_unreadable_record_keeps_the_notice(self):
        project = self.make('alpha')
        self.seed(project, '{not json')
        self.assertIn(ASK, self.notice(project, 'sess-1'))
        self.assertEqual(host.status(project)['reason'], 'onboarding_choice_unreadable')

    def test_a_recorded_never_under_the_state_root_silences_session_start(self):
        project = self.make('alpha')
        self.seed(project, {'schema': choice.SCHEMA, 'projectRoot': str(Path(project).resolve()),
                            'never': True, 'skippedSessions': []})
        self.assertEqual(self.notice(project, 'sess-1'), '')

    def test_a_choice_under_a_case_alias_applies_to_the_project(self):
        project = self.make('alpha')
        alias = project[:-len('alpha')] + 'ALPHA'
        if not Path(alias, '.memvane', 'settings.json').exists():
            self.skipTest('case-sensitive file system')
        host.never(alias)
        canonical, aliased = host.status(project), host.status(alias)
        self.assertEqual(aliased['projectRoot'], canonical['projectRoot'])
        self.assertEqual(aliased['recordPath'], canonical['recordPath'])
        self.assertEqual(canonical['recordPath'], str(self.record(project)))
        lock = Path(canonical['recordPath']).with_suffix('.lock')
        before = os.lstat(lock)
        self.assertEqual((before.st_mode & 0o777, before.st_nlink), (0o600, 1))
        self.assertEqual(self.notice(project, 'sess-1'), '')
        self.assertEqual(self.notice(alias, 'sess-1'), '')
        self.assertEqual(host.resume(project)['changesApplied'], 1)
        host.skip(project, 'sess-2')
        self.assertEqual(self.notice(alias, 'sess-2'), '')
        self.assertEqual(sorted(os.listdir(lock.parent)), sorted([self.record(project).name, lock.name]))
        after = os.lstat(lock)
        self.assertEqual((after.st_ino, after.st_mode & 0o777, after.st_nlink),
                         (before.st_ino, 0o600, 1))

    def test_a_skipped_session_stays_skipped_after_many_later_skips(self):
        project = self.make('alpha')
        for index in range(40):
            host.skip(project, f'sess-{index}')
        self.assertEqual(self.notice(project, 'sess-0'), '')
        self.assertEqual(self.notice(project, 'sess-39'), '')
        self.assertIn(ASK, self.notice(project, 'sess-40'))

    def test_a_skip_older_than_the_retention_window_asks_again_and_is_pruned(self):
        project = self.make('alpha')
        now = int(time.time() * 1000)
        self.assertEqual(choice.SKIP_RETENTION_MS, 30 * DAY_MS)
        self.seed(project, {'schema': choice.SCHEMA, 'projectRoot': str(Path(project).resolve()),
                            'never': False, 'skippedSessions': [
                                {'session': 'old', 'at': now - 31 * DAY_MS},
                                {'session': 'recent', 'at': now - 29 * DAY_MS}]})
        self.assertIn(ASK, self.notice(project, 'old'))
        self.assertEqual(self.notice(project, 'recent'), '')
        host.skip(project, 'new')
        kept = json.loads(self.record(project).read_text(encoding='utf-8'))['skippedSessions']
        self.assertEqual(sorted(row['session'] for row in kept), ['new', 'recent'])


if __name__ == '__main__':
    unittest.main()

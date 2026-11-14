"""Selected hook grants and sync discovery share the same source cursor."""
import hashlib
import json
from unittest.mock import patch

from memvane.host import api as host
from memvane.runtime import api as runtime
from memvane.wiki.intake import wiki_discovery as discovery
from tests.support.case import FixtureCase
from tests.support.install_builder import build_install
from tests.support.project import make_project
from tests.support.settings_fake import expanded, use_config


class DiscoverySyncBoundary(FixtureCase):
    def test_ungranted_revision_waits_for_a_successful_bash_grant(self):
        project = make_project(self.home)
        source = project.write_note('docs/note.md', 'initial source\n')
        project.git('add', '--', '.memvane/settings.json', 'docs/note.md')
        project.git('commit', '-qm', 'fixture')
        install = build_install(self, project)
        use_config(self, project, expanded(project, compile={
            'mode': 'auto-wiki', 'triggers': ['post_sync_source'],
            'extractor': {'builtins': ['claude']}}))
        install.settle()
        source.write_text('changed source\n')
        first = install.run_sync()
        self.assertIn(first['state'], ('indexed', 'unchanged'), first)
        cursor = discovery._paths(install.select())[0]
        key = project.raw_name + '/docs/note.md'
        revision = hashlib.sha256(source.read_bytes()).hexdigest()
        self.assertEqual(discovery._read(cursor)['pending'], {key: revision})
        queue = project.root / '.memvane/compile/source-queue.jsonl'
        self.assertFalse(queue.exists() and queue.read_text().strip())
        session = {'hook_event_name': 'SessionStart', 'cwd': str(project.root), 'session_id': 'fixture'}
        with patch.object(runtime, 'request', return_value={'status': 'queued'}):
            host.serve_selected('update-queued', 'claude', install.select(), session)
        self.assertEqual(len(discovery.observe_grants(install.select())['grants']['session_start']), 1)
        refused = install.run_sync()
        self.assertIn(refused['state'], ('indexed', 'unchanged'), refused)
        self.assertEqual(discovery._read(cursor)['pending'], {key: revision})
        self.assertEqual(discovery.observe_grants(install.select())['grants']['session_start'], [])
        self.assertFalse(queue.exists() and queue.read_text().strip())
        payload = {'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
                   'tool_input': {'command': 'touch docs/note.md'},
                   'tool_response': {'exit_code': 0}, 'cwd': str(project.root)}
        with patch.object(runtime, 'request', return_value={'status': 'queued'}):
            self.assertEqual(host.serve_selected('file-change-event', 'claude', install.select(), payload), {})
        self.assertEqual(len(discovery.observe_grants(install.select())['grants']['sync']), 1)
        second = install.run_sync()
        self.assertIn(second['state'], ('indexed', 'unchanged'), second)
        self.assertEqual(discovery._read(cursor)['pending'], {})
        jobs = [json.loads(line) for line in queue.read_text().splitlines()]
        self.assertEqual(len(jobs), 1)
        self.assertEqual((jobs[0]['source']['path'], jobs[0]['engine']), ('docs/note.md', 'claude'))
        self.assertEqual(discovery.observe_grants(install.select())['grants']['sync'], [])

    def test_refused_wake_withdraws_event_grant_before_later_sync(self):
        from memvane.recall.types import HostResponseError
        project = make_project(self.home)
        source = project.write_note('docs/note.md', 'initial source\n')
        project.git('add', '--', '.memvane/settings.json', 'docs/note.md')
        project.git('commit', '-qm', 'fixture')
        install = build_install(self, project)
        use_config(self, project, expanded(project, compile={
            'mode': 'auto-wiki', 'triggers': ['post_sync_source'],
            'extractor': {'builtins': ['claude']}}))
        install.settle()
        source.write_text('changed without an accepted grant\n')
        install.run_sync()
        decision = install.select()
        cursor = discovery._paths(decision)[0]
        before = discovery._read(cursor)['pending']
        self.assertEqual(len(before), 1)
        payload = {'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
                   'tool_input': {'command': 'touch docs/note.md'},
                   'tool_response': {'exit_code': 0}, 'cwd': str(project.root)}
        for fail_followup in (False, True):
            calls = []
            def request(kind, root, **kwargs):
                if kind == 'laya-sync':
                    calls.append(kind)
                    if not fail_followup or len(calls) == 2:
                        return {'status': 'deferred_queue_capacity'}
                return {'status': 'queued'}
            expected = 'selected_wiki_wake_failed' if fail_followup else 'selected_sync_enqueue_failed'
            with self.subTest(fail_followup=fail_followup), \
                 patch.object(runtime, 'request', side_effect=request), patch.object(runtime, 'hook_wake'):
                with self.assertRaisesRegex(HostResponseError, expected):
                    host.serve_selected('file-change-event', 'claude', decision, payload)
            grants = discovery.observe_grants(decision)
            self.assertEqual(grants['grants']['sync'], [])
            self.assertEqual(grants['provisional']['sync'], [])
            install.run_sync()
            self.assertEqual(discovery._read(cursor)['pending'], before)
            queue = project.root / '.memvane/compile/source-queue.jsonl'
            self.assertFalse(queue.exists() and queue.read_text().strip())

    def test_sync_before_grant_activation_gets_a_follow_up_wake(self):
        project = make_project(self.home)
        source = project.write_note('docs/note.md', 'initial source\n')
        project.git('add', '--', '.memvane/settings.json', 'docs/note.md')
        project.git('commit', '-qm', 'fixture')
        install = build_install(self, project)
        use_config(self, project, expanded(project, compile={
            'mode': 'auto-wiki', 'triggers': ['post_sync_source'],
            'extractor': {'builtins': ['claude']}}))
        install.settle()
        source.write_text('new revision\n')
        install.run_sync()
        decision = install.select()
        cursor = discovery._paths(decision)[0]
        pending = discovery._read(cursor)['pending']
        queue = project.root / '.memvane/compile/source-queue.jsonl'
        calls, observed = [], []
        def request(kind, root, **kwargs):
            if kind == 'laya-sync':
                calls.append(kind)
                if len(calls) == 1:
                    observed.append(discovery.observe_grants(decision))
                    install.run_sync()
                    self.assertEqual(discovery._read(cursor)['pending'], pending)
                    self.assertFalse(queue.exists() and queue.read_text().strip())
            return {'status': 'queued'}
        payload = {'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
                   'tool_input': {'command': 'touch docs/note.md'},
                   'tool_response': {'exit_code': 0}, 'cwd': str(project.root)}
        with patch.object(runtime, 'request', side_effect=request), patch.object(runtime, 'hook_wake'):
            self.assertEqual(host.serve_selected('file-change-event', 'claude', decision, payload), {})
        self.assertEqual(len(calls), 2)
        self.assertEqual(observed[0]['grants']['sync'], [])
        self.assertEqual(len(observed[0]['provisional']['sync']), 1)
        self.assertEqual(len(discovery.observe_grants(decision)['grants']['sync']), 1)
        install.run_sync()
        self.assertEqual(discovery._read(cursor)['pending'], {})
        self.assertEqual(len(queue.read_text().splitlines()), 1)

    def test_grant_activation_failure_never_authorizes_sync(self):
        from memvane.recall.types import HostResponseError
        from memvane.wiki.intake import api as intake
        project = make_project(self.home)
        source = project.write_note('docs/note.md', 'initial source\n')
        project.git('add', '--', '.memvane/settings.json', 'docs/note.md')
        project.git('commit', '-qm', 'fixture')
        install = build_install(self, project)
        use_config(self, project, expanded(project, compile={
            'mode': 'auto-wiki', 'triggers': ['post_sync_source'],
            'extractor': {'builtins': ['claude']}}))
        install.settle()
        source.write_text('pending revision\n')
        install.run_sync()
        decision = install.select()
        cursor = discovery._paths(decision)[0]
        pending = discovery._read(cursor)['pending']
        payload = {'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
                   'tool_input': {'command': 'touch docs/note.md'},
                   'tool_response': {'exit_code': 0}, 'cwd': str(project.root)}
        cases = ((discovery.DiscoveryBusy('lock_busy'), 'selected_wiki_grant_failed', 0),
                 (intake.GrantUncertain('grant_uncertain'), 'selected_wiki_grant_revoke_failed', 1))
        for error, reason, provisional in cases:
            with self.subTest(reason=reason), patch.object(intake, 'activate_grant', side_effect=error), \
                 patch.object(runtime, 'request', return_value={'status': 'queued'}), \
                 patch.object(runtime, 'hook_wake'):
                with self.assertRaisesRegex(HostResponseError, reason):
                    host.serve_selected('file-change-event', 'claude', decision, payload)
            grants = discovery.observe_grants(decision)
            self.assertEqual(grants['grants']['sync'], [])
            self.assertEqual(len(grants['provisional']['sync']), provisional)
            install.run_sync()
            self.assertEqual(discovery._read(cursor)['pending'], pending)
            queue = project.root / '.memvane/compile/source-queue.jsonl'
            self.assertFalse(queue.exists() and queue.read_text().strip())

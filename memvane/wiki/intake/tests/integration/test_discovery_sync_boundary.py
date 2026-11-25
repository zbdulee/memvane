"""Selected hook grants and sync discovery share the same source cursor."""
import hashlib
import json
from unittest.mock import patch

from memvane.host import api as host
from memvane.runtime import api as runtime
from memvane.wiki.intake import wiki_discovery as discovery
from tests.support.case import FixtureCase
from tests.support.queue_files import pending_text
from tests.support.install_builder import build_install
from tests.support.project import make_project
from tests.support.settings_fake import expanded, use_config


class DiscoverySyncBoundary(FixtureCase):
    def test_runtime_retains_wake_and_grant_after_discovery_journal_failure(self):
        import fcntl
        import os
        from memvane.wiki.intake import wiki_compile_enqueue as enqueue
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
        decision = install.select()
        grant = discovery.record_grant(decision, 'sync', engine='claude')
        self.assertTrue(discovery.activate_grant(decision, 'sync', grant))
        wakes = runtime.wake_queue()
        wakes.put('laya-sync', str(project.root), now=0)
        timer = os.open(wakes.root / 'runtime-wake-timer.lock', os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(timer, fcntl.LOCK_EX)
        def worker():
            fd = os.open(wakes.root / 'runtime-worker.lock', os.O_CREAT | os.O_RDWR, 0o600)
            return runtime.runtime_main(['worker', str(fd)], execute=lambda row: install.run_sync())
        try:
            with patch.object(enqueue, 'enqueue_changed_sources', side_effect=OSError('synthetic disk full')):
                self.assertEqual(worker(), 0)
            rows = wakes.pending()
            self.assertEqual([(row['kind'], row['attempts']) for row in rows], [('laya-sync', 1)])
            cursor = discovery._read(discovery._paths(decision)[0])
            key = project.raw_name + '/docs/note.md'
            self.assertEqual(cursor['pending'], {key: hashlib.sha256(source.read_bytes()).hexdigest()})
            self.assertEqual(discovery.observe_grants(decision)['grants']['sync'], [grant])
            with wakes.connect() as db:
                db.execute('UPDATE wakes SET due=0')
            with patch.object(runtime, 'request', return_value={'status': 'queued'}):
                self.assertEqual(worker(), 0)
            self.assertEqual(wakes.pending(), [])
            self.assertEqual(discovery.observe_grants(decision)['grants']['sync'], [])
            queue = project.root / '.memvane/compile/source-queue.jsonl'
            self.assertEqual([json.loads(line)['source']['path'] for line in pending_text(queue).splitlines()],
                             ['docs/note.md'])
        finally:
            os.close(timer)

    def test_discovery_failure_after_publish_preserves_grant_for_retry(self):
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
        source.write_text('changed source\n')
        project.write_card('concepts/new.md', title='New', body='a newly published card\n')
        decision = install.select()
        grant = discovery.record_grant(decision, 'sync', engine='claude')
        self.assertTrue(discovery.activate_grant(decision, 'sync', grant))
        cursor = discovery._paths(decision)[0]
        before = discovery._read(cursor)
        published = []
        def crash_after_publish(*args, **kwargs):
            published.append(kwargs['view_generation'])
            raise RuntimeError('synthetic failure after publish')
        with patch.object(intake, 'drain_after_sync', side_effect=crash_after_publish):
            with self.assertRaisesRegex(RuntimeError, 'synthetic failure after publish'):
                install.run_sync()
        self.assertEqual(len(published), 1)
        self.assertTrue(published[0])
        self.assertEqual(discovery._read(cursor), before)
        self.assertEqual(discovery.observe_grants(decision)['grants']['sync'], [grant])
        queue = project.root / '.memvane/compile/source-queue.jsonl'
        self.assertFalse(pending_text(queue).strip())
        retried = install.run_sync()
        self.assertEqual(retried['state'], 'unchanged', retried)
        self.assertEqual(retried['generation'], published[0])
        jobs = [json.loads(line) for line in pending_text(queue).splitlines()]
        self.assertEqual([(job['source']['path'], job['contentRevision']) for job in jobs],
                         [('docs/note.md', hashlib.sha256(source.read_bytes()).hexdigest())])
        self.assertEqual(discovery.observe_grants(decision)['grants']['sync'], [])

    def test_unlabelled_grant_preserves_unknown_engine_for_worker_fallback(self):
        project = make_project(self.home)
        source = project.write_note('docs/note.md', 'initial source\n')
        project.git('add', '--', '.memvane/settings.json', 'docs/note.md')
        project.git('commit', '-qm', 'fixture')
        install = build_install(self, project)
        use_config(self, project, expanded(project, compile={
            'mode': 'auto-wiki', 'triggers': ['post_sync_source'],
            'extractor': {'builtins': ['claude', 'codex', 'hermes']}}))
        install.settle()
        source.write_text('new revision\n')
        decision = install.select()
        grant = discovery.record_grant(decision, 'sync')
        self.assertTrue(discovery.activate_grant(decision, 'sync', grant))
        result = install.run_sync()
        self.assertIn(result['state'], ('indexed', 'unchanged'), result)
        queue = project.root / '.memvane/compile/source-queue.jsonl'
        jobs = [json.loads(line) for line in pending_text(queue).splitlines()]
        self.assertEqual([(job['source']['path'], job['engine']) for job in jobs],
                         [('docs/note.md', 'unknown')])
        self.assertEqual(discovery._read(discovery._paths(decision)[0])['pending'], {})

    def test_wiki_only_sync_spends_grant_without_source_jobs(self):
        project = make_project(self.home)
        project.git('add', '--', '.memvane/settings.json')
        project.git('commit', '-qm', 'fixture')
        install = build_install(self, project)
        use_config(self, project, expanded(project, compile={
            'mode': 'auto-wiki', 'triggers': ['post_sync_source'],
            'extractor': {'builtins': ['claude']}}))
        install.settle()
        project.write_card('concepts/card.md', title='Card', body='only wiki changed\n')
        decision = install.select()
        payload = {'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
                   'tool_input': {'command': 'touch .memvane/wiki/concepts/card.md'},
                   'tool_response': {'exit_code': 0}, 'cwd': str(project.root)}
        with patch.object(runtime, 'request', return_value={'status': 'queued'}), \
             patch.object(runtime, 'hook_wake'):
            self.assertEqual(host.serve_selected('file-change-event', 'claude', decision, payload), {})
        result = install.run_sync()
        self.assertIn(result['state'], ('indexed', 'unchanged'), result)
        self.assertEqual(discovery._read(discovery._paths(decision)[0])['pending'], {})
        self.assertEqual(discovery.observe_grants(decision)['grants']['sync'], [])
        queue = project.root / '.memvane/compile/source-queue.jsonl'
        self.assertFalse(pending_text(queue).strip())

    def test_failed_bash_does_not_record_a_discovery_grant(self):
        project = make_project(self.home)
        project.write_note('docs/note.md', 'source\n')
        project.git('add', '--', '.memvane/settings.json', 'docs/note.md')
        project.git('commit', '-qm', 'fixture')
        install = build_install(self, project)
        decision = install.select()
        payload = {'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
                   'tool_input': {'command': 'false'},
                   'tool_response': {'exit_code': 1}, 'cwd': str(project.root)}
        with patch.object(runtime, 'request', return_value={'status': 'queued'}) as wake:
            self.assertEqual(host.serve_selected('file-change-event', 'claude', decision, payload), {})
            wake.assert_not_called()
            edit = {'hook_event_name': 'PostToolUse', 'tool_name': 'Write', 'cwd': str(project.root),
                    'tool_input': {'file_path': str(project.root / 'docs/note.md')}}
            self.assertEqual(host.serve_selected('index', 'claude', decision, edit), {})
            self.assertIn('laya-sync', [call.args[0] for call in wake.call_args_list])
        grants = discovery.observe_grants(decision)
        self.assertEqual(grants['grants']['sync'], [])
        self.assertEqual(grants['provisional']['sync'], [])

    def test_selected_session_grant_backfills_existing_sources_once(self):
        from memvane.wiki.intake import wiki_backfill as backfill
        project = make_project(self.home)
        project.write_note('docs/note.md', 'existing source\n')
        project.git('add', '--', '.memvane/settings.json', 'docs/note.md')
        project.git('commit', '-qm', 'fixture')
        install = build_install(self, project)
        use_config(self, project, expanded(project, compile={
            'mode': 'auto-wiki', 'triggers': ['session_start_source', 'post_sync_source'],
            'extractor': {'builtins': ['claude']}}))
        install.settle()
        payload = {'hook_event_name': 'SessionStart', 'cwd': str(project.root), 'session_id': 'fixture'}
        with patch.object(runtime, 'request', return_value={'status': 'queued'}), \
             patch.object(runtime, 'hook_wake'):
            host.serve_selected('update-queued', 'claude', install.select(), payload)
        first = install.run_sync()
        decision = install.select()
        progress = backfill.summary(backfill._load(decision))
        self.assertEqual((progress['total'], progress['dispatched'], progress['settled']), (1, 1, 0))
        self.assertEqual(backfill._load(decision)['engine'], 'claude')
        queue = project.root / '.memvane/compile/source-queue.jsonl'
        jobs = [json.loads(line) for line in pending_text(queue).splitlines()]
        self.assertEqual([(job['source']['path'], job['trigger']) for job in jobs],
                         [('docs/note.md', 'session_start_source')])
        self.assertIn(first['state'], ('indexed', 'unchanged'), first)
        install.run_sync()
        self.assertEqual([json.loads(line) for line in pending_text(queue).splitlines()], jobs)
        self.assertEqual(backfill.summary(backfill._load(decision)), progress)

    def test_ungranted_revision_waits_for_a_successful_bash_grant(self):
        project = make_project(self.home)
        source = project.write_note('docs/note.md', 'initial source\n')
        gone = project.write_note('docs/gone.md', 'to be deleted\n')
        gone_hash = hashlib.sha256(gone.read_bytes()).hexdigest()
        project.git('add', '--', '.memvane/settings.json', 'docs')
        project.git('commit', '-qm', 'fixture')
        install = build_install(self, project)
        use_config(self, project, expanded(project, compile={
            'mode': 'auto-wiki', 'triggers': ['post_sync_source'],
            'extractor': {'builtins': ['claude']}}))
        install.settle()
        source.write_text('changed source\n')
        new = project.write_note('docs/new.md', 'new source\n')
        gone.unlink()
        first = install.run_sync()
        self.assertIn(first['state'], ('indexed', 'unchanged'), first)
        cursor = discovery._paths(install.select())[0]
        key = project.raw_name + '/docs/note.md'
        revision = hashlib.sha256(source.read_bytes()).hexdigest()
        expected = {key: revision, project.raw_name + '/docs/gone.md': gone_hash,
                    project.raw_name + '/docs/new.md': hashlib.sha256(new.read_bytes()).hexdigest()}
        self.assertEqual(discovery._read(cursor)['pending'], expected)
        queue = project.root / '.memvane/compile/source-queue.jsonl'
        self.assertFalse(pending_text(queue).strip())
        session = {'hook_event_name': 'SessionStart', 'cwd': str(project.root), 'session_id': 'fixture'}
        with patch.object(runtime, 'request', return_value={'status': 'queued'}):
            host.serve_selected('update-queued', 'claude', install.select(), session)
        self.assertEqual(len(discovery.observe_grants(install.select())['grants']['session_start']), 1)
        refused = install.run_sync()
        self.assertIn(refused['state'], ('indexed', 'unchanged'), refused)
        self.assertEqual(discovery._read(cursor)['pending'], expected)
        self.assertEqual(discovery.observe_grants(install.select())['grants']['session_start'], [])
        self.assertFalse(pending_text(queue).strip())
        payload = {'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
                   'tool_input': {'command': 'touch docs/note.md'},
                   'tool_response': {'exit_code': 0}, 'cwd': str(project.root)}
        with patch.object(runtime, 'request', return_value={'status': 'queued'}):
            self.assertEqual(host.serve_selected('file-change-event', 'claude', install.select(), payload), {})
        self.assertEqual(len(discovery.observe_grants(install.select())['grants']['sync']), 1)
        second = install.run_sync()
        self.assertIn(second['state'], ('indexed', 'unchanged'), second)
        self.assertEqual(discovery._read(cursor)['pending'], {})
        jobs = [json.loads(line) for line in pending_text(queue).splitlines()]
        self.assertEqual({job['source']['path']: (job['engine'], job['contentRevision']) for job in jobs},
                         {'docs/new.md': ('claude', expected[project.raw_name + '/docs/new.md']),
                          'docs/note.md': ('claude', revision), 'docs/gone.md': ('claude', gone_hash)})
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
            self.assertFalse(pending_text(queue).strip())

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
                    self.assertFalse(pending_text(queue).strip())
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
        self.assertEqual(len(pending_text(queue).splitlines()), 1)

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
        cases = ((discovery.DiscoveryBlocked('state_unsafe'), 'selected_wiki_grant_failed', 0),
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
            self.assertFalse(pending_text(queue).strip())

    def test_sync_inside_rejected_session_cannot_use_provisional_grant(self):
        from memvane.recall.types import HostResponseError
        from memvane.wiki.card import api as card
        project = make_project(self.home)
        source = project.write_note('docs/note.md', 'initial source\n')
        project.git('add', '--', '.memvane/settings.json', 'docs/note.md')
        project.git('commit', '-qm', 'fixture')
        install = build_install(self, project)
        use_config(self, project, expanded(project, compile={
            'mode': 'auto-wiki', 'triggers': ['session_start_source'],
            'extractor': {'builtins': ['claude']}}))
        install.settle()
        source.write_text('pending revision\n')
        install.run_sync()
        decision = install.select()
        cursor = discovery._paths(decision)[0]
        pending = discovery._read(cursor)['pending']
        seen = []
        def concurrent_sync(root):
            seen.append(discovery.observe_grants(decision))
            install.run_sync()
            return None
        payload = {'hook_event_name': 'SessionStart', 'cwd': str(project.root), 'session_id': 'fixture'}
        with patch.object(card, 'unresolved_pending_refreshes_strict', side_effect=concurrent_sync), \
             patch.object(runtime, 'request', return_value={'status': 'queued'}), \
             patch.object(runtime, 'hook_wake'):
            with self.assertRaisesRegex(HostResponseError, 'wiki_pending_refresh_unknown'):
                host.serve_selected('update-queued', 'claude', decision, payload)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]['grants']['session_start'], [])
        self.assertEqual(len(seen[0]['provisional']['session_start']), 1)
        grants = discovery.observe_grants(decision)
        self.assertEqual(grants['grants']['session_start'], [])
        self.assertEqual(grants['provisional']['session_start'], [])
        self.assertEqual(discovery._read(cursor)['pending'], pending)
        queue = project.root / '.memvane/compile/source-queue.jsonl'
        self.assertFalse(pending_text(queue).strip())

    def test_refused_followup_is_accepted_if_sync_already_used_the_grant(self):
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
        sync_calls, in_sync = [], [False]
        def request(kind, root, **kwargs):
            if kind == 'laya-sync':
                sync_calls.append(in_sync[0])
                if len(sync_calls) == 2:
                    in_sync[0] = True
                    try:
                        install.run_sync()
                    finally:
                        in_sync[0] = False
                    return {'status': 'deferred_queue_capacity'}
            return {'status': 'queued'}
        payload = {'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
                   'tool_input': {'command': 'touch docs/note.md'},
                   'tool_response': {'exit_code': 0}, 'cwd': str(project.root)}
        with patch.object(runtime, 'request', side_effect=request), patch.object(runtime, 'hook_wake'):
            self.assertEqual(host.serve_selected('file-change-event', 'claude', decision, payload), {})
        self.assertEqual(sync_calls, [False, False, True])
        self.assertEqual(discovery.observe_grants(decision)['grants']['sync'], [])
        cursor = discovery._paths(decision)[0]
        self.assertEqual(discovery._read(cursor)['pending'], {})
        queue = project.root / '.memvane/compile/source-queue.jsonl'
        self.assertEqual(len(pending_text(queue).splitlines()), 1)

    def test_busy_discovery_lock_keeps_sync_wake_without_refusing_hook(self):
        import fcntl
        import os
        from memvane.diagnostics import api as event_log
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
        cursor_path, _grants_path, lock_path = discovery._paths(decision)
        pending = discovery._read(cursor_path)['pending']
        queue = project.root / '.memvane/compile/source-queue.jsonl'
        payload = {'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
                   'tool_input': {'command': 'touch docs/note.md'},
                   'tool_response': {'exit_code': 0}, 'cwd': str(project.root)}

        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(discovery, 'HOOK_LOCK_SECONDS', 0.05), \
                 patch('memvane.wiki.intake.api.record_grant',
                       side_effect=lambda *args, **kwargs: discovery.record_grant(*args, wait=0.05, **kwargs)), \
                 patch.object(runtime, 'request', return_value={'status': 'queued'}) as req, \
                 patch.object(runtime, 'hook_wake'):
                self.assertEqual(host.serve_selected('file-change-event', 'claude', decision, payload), {})
            req.assert_any_call('laya-sync', decision['projectRoot'])
        finally:
            os.close(lock_fd)

        grants = discovery.observe_grants(decision)
        self.assertEqual(grants['grants']['sync'], [])
        self.assertEqual(grants['provisional']['sync'], [])
        install.run_sync()
        self.assertEqual(discovery._read(cursor_path)['pending'], pending)
        self.assertFalse(pending_text(queue).strip())
        rows = [row for row in event_log.build_progress_events(install.state) if row.get('event') == 'file-change-event']
        self.assertEqual((rows[-1]['outcome'], rows[-1]['reason'], rows[-1].get('grant')),
                         ('ok', 'sync_requested', 'busy_skipped'))

        with patch.object(intake, 'activate_grant', side_effect=discovery.DiscoveryBusy('lock_busy')), \
             patch.object(runtime, 'request', return_value={'status': 'queued'}), \
             patch.object(runtime, 'hook_wake') as wake:
            self.assertEqual(host.serve_selected('file-change-event', 'claude', decision, payload), {})
            self.assertGreaterEqual(wake.call_count, 1)
        grants = discovery.observe_grants(decision)
        self.assertEqual(grants['grants']['sync'], [])
        self.assertEqual(grants['provisional']['sync'], [])
        install.run_sync()
        self.assertEqual(discovery._read(cursor_path)['pending'], pending)
        self.assertFalse(pending_text(queue).strip())
        rows = [row for row in event_log.build_progress_events(install.state) if row.get('event') == 'file-change-event']
        self.assertEqual((rows[-1]['outcome'], rows[-1]['reason'], rows[-1].get('grant')),
                         ('ok', 'sync_requested', 'busy_skipped'))

#!/usr/bin/env python3
"""U13: a linked git worktree of a schema3-selected install reads its own view.

Seeded from the U12c sync-publish fixture: the v5 install, v6 index, journal
images, Git checkout and view store are actual; the schema3 record is the U10
seal, selected with ``mutate_image``. The linked worktree is an actual
``git worktree add`` checkout at the main checkout's HEAD whose settings and
wiki are copied as untracked files (a same-HEAD dirty worktree). Hooks run
through the actual ``serve_selected``; the sync pass is the actual
``laya_product_sync_worker.run``. No model or provider starts.
"""
from contextlib import ExitStack, closing, contextmanager
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('schema3_worktree_seed',
                                              ROOT / 'test/fixtures/laya-schema3-sync-publish-checks.py')
seed_module = importlib.util.module_from_spec(spec); spec.loader.exec_module(seed_module)
HOSTS = ('claude', 'codex', 'hermes')


def emit(label, **values):
    print(json.dumps(dict(case=label, **values), sort_keys=True, default=str), flush=True)


class Schema3WorktreeViewChecks(seed_module.Schema3SyncPublishChecks):
    def setUp(self):
        super().setUp()
        self.host = importlib.import_module('laya_host_response')
        self.route = importlib.import_module('laya_index_host_route')
        self.event_log = importlib.import_module('event_log')
        self.project_dir = importlib.import_module('memvane.settings.project_dir')
        project = self.case.project
        base = Path(tempfile.mkdtemp(prefix='linked-', dir=str(project.parent))).resolve()
        self.addCleanup(shutil.rmtree, base, True)
        self.linked = base / 'linked'
        self.git(project, 'worktree', 'add', '-q', '-b', 'side', str(self.linked))
        settings = self.project_dir.settings_path(project)
        relative = settings.relative_to(project)
        (self.linked / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(settings, self.linked / relative)
        self.linked_wiki = self.linked / self.wiki.relative_to(project)
        # Tracked collection directories exist in every checkout; mirror them.
        for item in self.decision['collections']:
            (self.linked / Path(item['sourceRoot']).relative_to(project)).mkdir(parents=True, exist_ok=True)
        time.sleep(1.1)  # Git racy-index window before the linked checkout's capture.

    def git(self, cwd, *args):
        return subprocess.run(['git', '-C', str(cwd), '-c', 'commit.gpgsign=false', '-c',
                               'core.hooksPath=' + os.devnull, '-c', 'user.name=Fixture', '-c',
                               'user.email=fixture@example.invalid', *args],
                              check=True, capture_output=True, timeout=20, encoding='utf8')

    def copy_wiki(self):
        """The main checkout's current cards, copied as the worktree's own untracked files."""
        # Into the existing directory: the store binds the wiki root's identity.
        shutil.copytree(self.wiki, self.linked_wiki, dirs_exist_ok=True)

    def linked_card(self, relative, token):
        """A card written only in the linked worktree (same shape as ``card``).

        ``card`` also writes the card's raw source under the main checkout;
        that source moves to the linked worktree, so the main checkout's raw
        observation (U12e/D2) is unchanged by a linked-only edit.
        """
        wiki = self.wiki
        source = self.case.project / 'notes' / (token + '.md')
        before = source.read_bytes() if source.exists() else None
        try:
            self.wiki = self.linked_wiki
            self.card(relative, token)
        finally:
            self.wiki = wiki
        target = self.linked / 'notes' / (token + '.md')
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(source.read_bytes())
        if before is None:
            source.unlink()
        else:
            source.write_bytes(before)

    @contextmanager
    def loaded(self):
        with ExitStack() as stack:
            stack.enter_context(self.seed.authority())
            stack.enter_context(mock.patch.object(
                self.host, '__file__', str(self.case.plugin / 'core' / 'laya_host_response.py')))
            yield

    def hook(self, action, cwd, payload, host='claude', *, wakes=None):
        """One hook through the actual selector and ``serve_selected``."""
        events, stderr = [], io.StringIO()

        def recorded(event, **fields):
            events.append(dict(fields, event=event))
            return True
        seen = [] if wakes is None else wakes
        import runtime_work

        def request(kind, target, **kwargs):
            seen.append((kind, str(target)))
            return {'status': 'queued'}
        with self.loaded(), mock.patch('sys.stderr', stderr), \
                mock.patch.object(self.event_log, 'emit', side_effect=recorded), \
                mock.patch.object(runtime_work, 'request', side_effect=request):
            decision = self.route.select_route(str(cwd), action=action)
            answer = self.host.serve_selected(action, host, decision, {**payload, 'cwd': str(cwd)})
        return decision, answer, events, stderr.getvalue()

    def recall(self, cwd, prompt, host='claude'):
        decision, answer, events, stderr = self.hook(
            'recall', cwd, {'prompt': prompt, 'session_id': 'synthetic-' + host,
                            'hook_event_name': 'UserPromptSubmit'}, host)
        context = (answer['context'] if host == 'hermes'
                   else answer['hookSpecificOutput']['additionalContext'])
        recall = [event for event in events if event['event'] == 'recall']
        self.assertEqual(len(recall), 1, events)
        return dict(decision=decision, context=context, event=recall[0], stderr=stderr)

    def view_id(self, worktree_id, branch):
        checkout = importlib.import_module('laya_checkout_view')
        return checkout._digest([self.repository['repoId'], worktree_id, 'refs/heads/' + branch])

    def view_head(self, view_id):
        with closing(sqlite3.connect(self.store_path.as_uri() + '?mode=ro', uri=True)) as db:
            row = db.execute('SELECT gen_id,epoch FROM view_heads WHERE view_id=?', (view_id,)).fetchone()
        return None if row is None else tuple(row)

    def head(self):
        # The seed's helper assumes one view; here it is the main checkout's.
        found = self.view_head(self.view_id(self.worktree['worktreeId'], 'main'))
        self.assertIsNotNone(found)
        return found

    def linked_id(self):
        with closing(sqlite3.connect(self.store_path.as_uri() + '?mode=ro', uri=True)) as db:
            rows = db.execute('SELECT worktree_id FROM worktrees WHERE project_root=?',
                              (str(self.linked),)).fetchall()
        self.assertLessEqual(len(rows), 1)
        return rows[0][0] if rows else None

    # -- checks ---------------------------------------------------------------
    def test_01_linked_recall_reads_its_own_view_never_main(self):
        self.select_schema3()
        self.card('concepts/kestrel-perch.md', 'kestrelprobe')
        self.copy_wiki()
        self.linked_card('concepts/wren-linked.md', 'wrenlinkedprobe')
        seeded = self.head()[0]
        cold = self.recall(self.linked, 'kestrelprobe')
        emit('schema3-worktree-cold', decision=cold['decision'].get('state'),
             reason=cold['event'].get('reason'), context=cold['context'][:200])
        self.assertEqual(cold['decision']['state'], 'worktree')
        # RED: the main checkout's schema3 generation answered the worktree.
        self.assertNotIn(seeded, cold['context'])
        self.assertEqual(cold['context'], '')
        self.assertEqual(cold['event']['reason'], 'view_preparing')
        self.assertIn('view_preparing', cold['stderr'])
        # GREEN: the main checkout's owner publishes main, then registers and
        # publishes the linked worktree's own view in the same pass.
        self.assertIsNone(self.linked_id())
        built, _calls = self.run_pass()
        self.assertEqual(built['state'], 'indexed', built)
        main_gen = built['generation']
        self.assertEqual(self.head()[0], main_gen)
        self.assertEqual([item['root'] for item in built['worktrees']], [str(self.linked)])
        item = built['worktrees'][0]
        self.assertEqual(item['state'], 'indexed', built)
        linked_id = self.linked_id()
        self.assertIsNotNone(linked_id)
        expected = sorted(list(self.members(main_gen)) + ['concepts/wren-linked.md'])
        linked_gen = item['generation']
        self.assertEqual(item['ingested'], len(expected))
        self.assertEqual(self.view_head(self.view_id(linked_id, 'side'))[0], linked_gen)
        self.assertEqual(sorted(self.members(linked_gen)), expected)
        main_hit = self.recall(self.case.project, 'kestrelprobe')
        self.assertIn('generation ' + main_gen, main_hit['context'])
        # Same HEAD, dirty files differ: each checkout answers from its own view.
        for host in HOSTS:
            with self.subTest(host=host):
                own = self.recall(self.linked, 'wrenlinkedprobe', host)
                self.assertIn('generation ' + linked_gen, own['context'])
                self.assertIn('concepts/wren-linked.md', own['context'])
                self.assertNotIn(main_gen, own['context'])
                self.assertIn("this worktree's own last published wiki view", own['context'])
                self.assertEqual(own['event']['store_format'], 'schema3')
                self.assertEqual(own['event']['generation'], linked_gen)
        main_only = self.recall(self.case.project, 'wrenlinkedprobe')
        self.assertNotIn('concepts/wren-linked.md', main_only['context'])
        self.assertNotIn(linked_gen, main_only['context'])
        emit('schema3-worktree-own-view', mainGeneration=main_gen, linkedGeneration=linked_gen,
             linkedIngests=item['ingested'], cold=cold['event']['reason'])

    def built(self):
        """Main with one card; linked copied plus one own card; one pass publishes both views."""
        self.select_schema3()
        self.card('concepts/kestrel-perch.md', 'kestrelprobe')
        self.copy_wiki()
        self.linked_card('concepts/wren-linked.md', 'wrenlinkedprobe')
        first, _ = self.run_pass()
        self.assertEqual(first['state'], 'indexed', first)
        self.assertEqual(first['worktrees'][0]['state'], 'indexed', first)
        return first['generation'], first['worktrees'][0]['generation']

    def test_02_edit_in_one_checkout_leaves_the_other_view_unchanged(self):
        main_gen, linked_gen = self.built()
        linked_id = self.linked_id()
        linked_view = self.view_id(linked_id, 'side')
        main_before = self.recall(self.case.project, 'kestrelprobe')['context']
        # A edit (linked) -> B (main) head and answer unchanged.
        self.linked_card('concepts/kestrel-perch.md', 'ospreylinkedprobe')
        edited, calls = self.run_pass()
        self.assertEqual(calls, ['concepts/kestrel-perch.md'])
        self.assertEqual(edited['generation'], main_gen)
        self.assertEqual(self.head()[0], main_gen)
        moved = self.view_head(linked_view)[0]
        self.assertNotEqual(moved, linked_gen)
        self.assertEqual(self.recall(self.case.project, 'kestrelprobe')['context'], main_before)
        self.assertNotIn('kestrel-perch', self.recall(self.case.project, 'ospreylinkedprobe')['context'])
        self.assertIn('generation ' + moved, self.recall(self.linked, 'ospreylinkedprobe')['context'])
        # B edit (main) -> A (linked) head unchanged.
        linked_before = self.recall(self.linked, 'wrenlinkedprobe')['context']
        self.card('concepts/heron-main.md', 'heronmainprobe')
        main_edit, calls = self.run_pass()
        self.assertEqual(calls, ['concepts/heron-main.md'])
        self.assertNotEqual(main_edit['generation'], main_gen)
        self.assertEqual(main_edit['worktrees'][0]['state'], 'unchanged', main_edit)
        self.assertEqual(self.view_head(linked_view)[0], moved)
        self.assertEqual(self.recall(self.linked, 'wrenlinkedprobe')['context'], linked_before)
        self.assertNotIn('heron-main', self.recall(self.linked, 'heronmainprobe')['context'])
        emit('schema3-worktree-isolated-edits', linkedEdit=moved, mainEdit=main_edit['generation'])

    def test_03_branch_switch_is_a_cold_view_with_no_trust_reuse(self):
        _main_gen, linked_gen = self.built()
        linked_id = self.linked_id()
        self.git(self.linked, 'switch', '-q', '-c', 'other')
        time.sleep(1.1)  # Git racy-index window before the next capture.
        cold = self.recall(self.linked, 'wrenlinkedprobe')
        self.assertEqual((cold['context'], cold['event']['reason']), ('', 'view_preparing'))
        built, calls = self.run_pass()
        item = built['worktrees'][0]
        self.assertEqual(item['state'], 'indexed', built)
        # Every card is ingested again for the new view: the side view's
        # trust rows are never reused (foreign_card_trust otherwise).
        self.assertEqual(sorted(calls), sorted(self.members(linked_gen)))
        self.assertEqual(item['ingested'], len(calls))
        other = self.view_head(self.view_id(linked_id, 'other'))[0]
        self.assertEqual(other, item['generation'])
        self.assertEqual(self.view_head(self.view_id(linked_id, 'side'))[0], linked_gen)
        with closing(sqlite3.connect(self.store_path.as_uri() + '?mode=ro', uri=True)) as db:
            branches = {row[0] for row in db.execute(
                'SELECT DISTINCT f.branch_key FROM wiki_members m JOIN attestation_sources a '
                'ON a.trust_digest=m.trust_digest JOIN source_fingerprints f '
                'ON f.fingerprint_id=a.fingerprint_id WHERE m.gen_id=?', (other,))}
        self.assertEqual(branches, {'refs/heads/other'})
        answer = self.recall(self.linked, 'wrenlinkedprobe')
        self.assertIn('generation ' + other, answer['context'])
        self.assertNotIn(linked_gen, answer['context'])
        emit('schema3-worktree-branch-switch', coldReason=cold['event']['reason'],
             reingested=len(calls), branches=sorted(branches))

    def test_04_checkout_moving_under_the_read_answers_no_other_view(self):
        main_gen, linked_gen = self.built()
        BlobStoreError = importlib.import_module('laya_blob_values').BlobStoreError
        calls = []

        def moving(store, handle, **kwargs):
            calls.append(handle.worktree_id)
            raise BlobStoreError('checkout_changed')
        with mock.patch.object(self.views.ViewStore, 'pin_view', new=moving):
            answer = self.recall(self.linked, 'wrenlinkedprobe')
        self.assertEqual((answer['context'], answer['event']['reason']), ('', 'checkout_changed'))
        # One retry, both against the linked worktree's own view only.
        self.assertEqual(calls, [self.linked_id()] * 2)
        emit('schema3-worktree-checkout-moving', attempts=len(calls), reason=answer['event']['reason'])

    def test_05_worktree_hooks_wake_the_main_owner_only(self):
        self.built()
        session = {'hook_event_name': 'SessionStart', 'session_id': 'synthetic'}
        for host in HOSTS:
            wakes = []
            _d, answer, _e, _s = self.hook('update-queued', self.linked, session, host, wakes=wakes)
            notice = answer['notice'] if host == 'hermes' else answer['hookSpecificOutput']['additionalContext']
            self.assertEqual(notice, self.host.WORKTREE_VIEW_NOTICE)
            self.assertEqual(wakes, [('laya-sync', str(self.case.project))])
        edit = {'hook_event_name': 'PostToolUse', 'tool_name': 'Write', 'session_id': 'synthetic',
                'tool_input': {'file_path': str(self.linked_wiki / 'concepts/wren-linked.md')},
                'tool_response': {'success': True}}
        wakes = []
        self.assertEqual(self.hook('index', self.linked, edit, wakes=wakes)[1], {})
        self.assertEqual(wakes, [('laya-sync', str(self.case.project))])
        for action in ('posttool', 'compile'):
            wakes = []
            self.assertEqual(self.hook(action, self.linked, edit, wakes=wakes)[1], {})
            self.assertEqual(wakes, [], action)
        failed = {**edit, 'tool_response': {'success': False}}
        wakes = []
        self.hook('index', self.linked, failed, wakes=wakes)
        self.assertEqual(wakes, [])
        emit('schema3-worktree-hooks', sessionWake=True, indexWake=True, compileWake=False)

    def test_06_settings_mismatch_keeps_the_existing_classification(self):
        self.select_schema3()
        self.card('concepts/kestrel-perch.md', 'kestrelprobe')
        self.copy_wiki()
        settings = self.linked / self.project_dir.settings_path(self.case.project).relative_to(self.case.project)
        raw = json.loads(settings.read_text(encoding='utf8'))
        raw['name'] = str(raw.get('name', 'x')) + '-other'
        settings.write_text(json.dumps(raw), encoding='utf8')
        decision = self.route.select_route(str(self.linked), action='recall')
        self.assertNotEqual(decision['state'], 'worktree')
        result, calls = self.run_pass()
        self.assertEqual(result['state'], 'indexed', result)
        self.assertNotIn('worktrees', result)
        self.assertIsNone(self.linked_id())
        self.assertTrue(all(not path.startswith(str(self.linked)) for path in calls))
        emit('schema3-worktree-settings-mismatch', state=decision['state'], reason=decision.get('reason'))

    def test_07_v6_selection_keeps_the_read_only_worktree(self):
        self.copy_wiki()
        session = {'hook_event_name': 'SessionStart', 'session_id': 'synthetic'}
        wakes = []
        decision, answer, _e, _s = self.hook('update-queued', self.linked, session, wakes=wakes)
        self.assertEqual(decision['state'], 'worktree')
        self.assertEqual(answer['hookSpecificOutput']['additionalContext'], self.host.WORKTREE_NOTICE)
        edit = {'hook_event_name': 'PostToolUse', 'tool_name': 'Write', 'session_id': 'synthetic',
                'tool_input': {'file_path': str(self.linked_wiki / 'x.md')}, 'tool_response': {}}
        self.assertEqual(self.hook('index', self.linked, edit, wakes=wakes)[1], {})
        self.assertEqual(wakes, [])
        emit('schema3-worktree-v6-unchanged', notice='WORKTREE_NOTICE', wakes=0)


    def test_08_main_branch_switch_is_a_cold_view_too(self):
        """Decision D9: the main checkout's first-seen branch is ``view_preparing``."""
        self.select_schema3()
        self.card('concepts/kestrel-perch.md', 'kestrelprobe')
        first, _calls = self.run_pass()
        self.assertEqual(first['state'], 'indexed', first)
        main_gen = first['generation']
        self.git(self.case.project, 'switch', '-q', '-c', 'feature')
        time.sleep(1.1)  # Git racy-index window before the next capture.
        cold = self.recall(self.case.project, 'kestrelprobe')
        self.assertEqual((cold['context'], cold['event']['reason']), ('', 'view_preparing'))
        self.assertIn('view_preparing', cold['stderr'])
        built, calls = self.run_pass()
        self.assertEqual(built['state'], 'indexed', built)
        feature = self.view_head(self.view_id(self.worktree['worktreeId'], 'feature'))[0]
        self.assertEqual(feature, built['generation'])
        self.assertEqual(sorted(calls), sorted(self.members(main_gen)))
        self.assertEqual(self.view_head(self.view_id(self.worktree['worktreeId'], 'main'))[0], main_gen)
        answer = self.recall(self.case.project, 'kestrelprobe')
        self.assertIn('generation ' + feature, answer['context'])
        self.assertNotIn(main_gen, answer['context'])
        emit('schema3-main-branch-switch', coldReason=cold['event']['reason'], reingested=len(calls))


# Only this unit's checks run here; the seed's own checks run in their file.
for _name in dir(seed_module.Schema3SyncPublishChecks):
    if _name.startswith('test') and _name not in vars(Schema3WorktreeViewChecks):
        setattr(Schema3WorktreeViewChecks, _name, None)


if __name__ == '__main__':
    unittest.main()

"""1.0.24: a main-checkout session's edit in a linked worktree of the same repository.

The edit is this project's: recall reads the main view, the case belongs to
the main project, and path rules, git-ignore and the file hash come from the
worktree's project root. The route proves the worktree (same common git dir,
same settings identity, same project place); the capture child proves it
again. Every other outside path stays ``skip_outside_project``.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import unittest
from unittest import mock

from memvane.host import edit_recall
from memvane.host import host_response as response
from memvane.host.tests.integration.test_edit_recall import EDIT_TEXT, NEW_FILE, edit_payload
from memvane.host.tests.support import CARD_BODY, HostInstallCase, Wakes, context
from tests.support.install_builder import ROOT, build_install
from tests.support.project import make_project


def fake_git(folder, body):
    """A ``git`` that appends one line per call to ``<folder>/calls`` and then runs ``body``."""
    bin_dir = Path(folder) / 'bin'
    bin_dir.mkdir(mode=0o700)
    log = Path(folder) / 'calls'
    script = bin_dir / 'git'
    script.write_text('#!/bin/sh\necho call >> "%s"\n%s\n' % (log, body), encoding='utf-8')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return bin_dir, log


def calls(log):
    return len(log.read_text(encoding='utf-8').splitlines()) if log.exists() else 0


class LinkedCase(HostInstallCase):
    def setUp(self):
        super().setUp()
        self.wakes = Wakes().install(self)

    def worktree(self, name='wt', branch=None):
        linked = self.fixture.linked_worktree(name, branch or name + '-branch')
        linked.write_note(NEW_FILE, EDIT_TEXT)
        return linked

    def posttool(self, payload, host='claude'):
        with response.learning_capture() as slot:
            answer = self.serve('posttool', payload, host)
        return answer, list(slot)

    def rows(self, event, session):
        return [row for row in self.install.events()
                if row.get('event') == event and row.get('session') == session]

    def skipped(self, session, target, reason):
        self.assertEqual(self.rows('recall.edit', session), [])
        [row] = self.rows('posttool', session)
        self.assertEqual((row.get('editTarget'), row.get('editRecall')), (target, reason))
        return row


class LinkedEditRecall(LinkedCase):
    def test_a_linked_worktree_edit_is_recalled_and_captured_as_the_main_project(self):
        linked = self.worktree()
        for host, tool in (('claude', 'Write'), ('codex', 'apply_patch')):
            with self.subTest(host=host):
                session = 'linked-' + host
                answer, capture = self.posttool(
                    edit_payload(session, tool, rel=str(linked.root / NEW_FILE)), host)
                self.assertIn('zqheronx', context(answer))
                [row] = self.rows('recall.edit', session)
                self.assertEqual((row['outcome'], row['editTarget']), ('served', 'linked'))
                [hook] = self.rows('posttool', session)
                self.assertEqual(hook['editTarget'], 'linked')
                self.assertGreaterEqual(hook['checkoutProofMs'], 0)
                [entry] = capture
                payload = entry[2]
                self.assertEqual(entry[1]['projectRoot'], str(self.project))
                self.assertEqual(payload['cwd'], str(self.project))
                self.assertEqual(payload['_memvane_edit_root'], str(linked.root))
                self.assertEqual(payload['_memvane_edit_target'], 'linked')
                self.assertEqual(payload['_memvane_edit_paths'], [NEW_FILE])
                self.assertEqual(payload['edit_event']['path_sha256'],
                                 hashlib.sha256(NEW_FILE.encode()).hexdigest())
                self.assertNotIn(str(linked.root), json.dumps(row))
        _answer, [main] = self.posttool(edit_payload('main-same-file'))
        self.assertEqual(main[2]['edit_event']['path_sha256'],
                         hashlib.sha256(NEW_FILE.encode()).hexdigest())
        self.assertEqual(main[2]['_memvane_edit_target'], 'main')
        self.assertNotIn('_memvane_edit_root', main[2])
        self.assertEqual(self.wakes.calls, [])

    def test_another_repository_and_a_plain_folder_stay_outside(self):
        other = make_project(self.home, name='other', dirname='other')
        other.write_note('notes/a.md', 'x\n')
        other.commit('other')
        foreign = other.linked_worktree('wt', 'other-branch')
        plain = self.home / 'handoff'
        plain.mkdir()
        for index, target in enumerate((foreign.root / NEW_FILE, other.root / NEW_FILE,
                                        plain / 'notes.md')):
            with self.subTest(target=target.name, index=index):
                session = 'outside-%d' % index
                answer, capture = self.posttool(edit_payload(session, rel=str(target)))
                self.assertEqual((answer, capture), ({}, []))
                row = self.skipped(session, 'outside', 'skip_outside_project')
                self.assertNotIn('checkoutProofMs', row)

    def test_a_worktree_the_route_does_not_prove_is_unproved(self):
        renamed = self.worktree('renamed')
        renamed.write_settings(name='different')
        routed = self.worktree('routed')
        (routed.root / '.memvane' / 'route.json').write_text('{}', encoding='utf-8')
        broken = self.worktree('broken')
        broken.settings_path.write_text('{"name": "fixture", "unknown": 1}', encoding='utf-8')
        for index, linked in enumerate((renamed, routed, broken)):
            with self.subTest(worktree=linked.root.name):
                session = 'unproved-%d' % index
                answer, capture = self.posttool(
                    edit_payload(session, rel=str(linked.root / NEW_FILE)))
                self.assertEqual((answer, capture), ({}, []))
                self.skipped(session, 'linked_unproved', 'skip_outside_project')

    def test_a_failing_git_skips_without_refusing(self):
        linked = self.worktree()
        bin_dir, log = fake_git(self.tmp, 'exit 1')
        with mock.patch.dict(os.environ, {'PATH': str(bin_dir) + os.pathsep + os.environ['PATH']}):
            answer, capture = self.posttool(edit_payload('nogit', rel=str(linked.root / NEW_FILE)))
        self.assertEqual((answer, capture), ({}, []))
        self.skipped('nogit', 'linked_unproved', 'skip_outside_project')
        self.assertEqual(calls(log), 1)

    def test_a_main_path_beside_a_worktree_path_uses_only_the_main_path(self):
        linked = self.worktree()
        patch = ('*** Begin Patch\n*** Add File: ' + NEW_FILE + '\n'
                 + ''.join('+' + line + '\n' for line in EDIT_TEXT.splitlines())
                 + '*** Add File: ' + str(linked.root / 'notes/other.md') + '\n'
                 + ''.join('+other ' + line + '\n' for line in EDIT_TEXT.splitlines())
                 + '*** End Patch')
        payload = {'hook_event_name': 'PostToolUse', 'session_id': 'both', 'tool_name': 'apply_patch',
                   'tool_input': {'command': patch}, 'tool_use_id': 'tool-both'}
        _answer, [entry] = self.posttool(payload, 'codex')
        [row] = self.rows('recall.edit', 'both')
        self.assertEqual((row['editTarget'], row['files']), ('main', 1))
        self.assertEqual(entry[2]['_memvane_edit_paths'], [NEW_FILE])
        self.assertNotIn('other', entry[2]['prompt'])
        [hook] = self.rows('posttool', 'both')
        self.assertNotIn('checkoutProofMs', hook)

    def test_two_worktrees_in_one_edit_are_mixed(self):
        first, second = self.worktree('one'), self.worktree('two')
        patch = ''.join('*** Add File: ' + str(root / NEW_FILE) + '\n'
                        + ''.join('+' + line + '\n' for line in EDIT_TEXT.splitlines())
                        for root in (first.root, second.root))
        payload = {'hook_event_name': 'PostToolUse', 'session_id': 'mixed', 'tool_name': 'apply_patch',
                   'tool_input': {'command': '*** Begin Patch\n' + patch + '*** End Patch'},
                   'tool_use_id': 'tool-mixed'}
        answer, capture = self.posttool(payload, 'codex')
        self.assertEqual((answer, capture), ({}, []))
        self.skipped('mixed', 'mixed', 'skip_mixed_checkout')

    def test_one_worktree_beside_an_outside_path_uses_the_worktree_path(self):
        linked = self.worktree()
        plain = self.home / 'handoff'
        plain.mkdir()
        patch = ('*** Begin Patch\n*** Add File: ' + str(linked.root / NEW_FILE) + '\n'
                 + ''.join('+' + line + '\n' for line in EDIT_TEXT.splitlines())
                 + '*** Add File: ' + str(plain / 'memo.md') + '\n+outside memo\n*** End Patch')
        payload = {'hook_event_name': 'PostToolUse', 'session_id': 'beside', 'tool_name': 'apply_patch',
                   'tool_input': {'command': patch}, 'tool_use_id': 'tool-beside'}
        _answer, [entry] = self.posttool(payload, 'codex')
        self.assertEqual(entry[2]['_memvane_edit_paths'], [NEW_FILE])
        self.assertNotIn('outside memo', entry[2]['prompt'])

    def test_the_worktree_skip_paths_join_the_main_ones(self):
        linked = self.worktree()
        linked.write_settings(skip_paths=['notes'])
        answer, capture = self.posttool(edit_payload('skips', rel=str(linked.root / NEW_FILE)))
        self.assertEqual((answer, capture), ({}, []))
        self.skipped('skips', 'linked', 'skip_paths')

    def test_a_worktree_below_the_main_root_counts_from_its_own_root(self):
        nested = self.project / '.claude' / 'worktrees' / 'x'
        self.fixture.git('worktree', 'add', '-q', '-b', 'nested-branch', str(nested))
        (nested / 'notes').mkdir(exist_ok=True)
        (nested / NEW_FILE).write_text(EDIT_TEXT, encoding='utf-8')
        answer, [entry] = self.posttool(edit_payload('nested', rel=str(nested / NEW_FILE)))
        self.assertIn('zqheronx', context(answer))
        [row] = self.rows('recall.edit', 'nested')
        self.assertEqual(row['editTarget'], 'linked')
        self.assertEqual(entry[2]['_memvane_edit_paths'], [NEW_FILE])
        self.assertEqual(entry[2]['_memvane_edit_root'], str(nested))
        # The same place on a branch without settings: the main settings answer, not a worktree.
        bare = self.project / '.claude' / 'worktrees' / 'bare'
        self.fixture.git('worktree', 'add', '-q', '-b', 'bare-branch', str(bare))
        (bare / '.memvane' / 'settings.json').unlink()
        (bare / 'notes').mkdir(exist_ok=True)
        answer, capture = self.posttool(edit_payload('bare', rel=str(bare / NEW_FILE)))
        self.assertEqual((answer, capture), ({}, []))
        self.skipped('bare', 'linked_unproved', 'skip_outside_project')

    def test_a_card_citing_the_same_relative_file_is_not_injected(self):
        linked = self.worktree()
        answer, [entry] = self.posttool(
            edit_payload('self', rel=str(linked.root / 'notes/heron.md')))
        self.assertEqual(answer, {})
        [row] = self.rows('recall.edit', 'self')
        self.assertEqual((row['selfCited'], row['pool'], row['cards']), (1, 1, []))
        self.assertEqual(len(entry[4]), 1)

    def test_git_runs_twice_for_a_worktree_and_never_for_the_main_checkout(self):
        from memvane.kernel import paths
        linked = self.worktree()
        real = subprocess.run
        seen = []

        def counted(argv, *args, **kwargs):
            seen.append(list(argv))
            return real(argv, *args, **kwargs)
        with mock.patch.object(paths.subprocess, 'run', side_effect=counted):
            self.posttool(edit_payload('plain-main'))
            self.assertEqual(seen, [])
            self.posttool(edit_payload('short', rel=str(linked.root / NEW_FILE), text='short'))
            self.assertEqual(len(seen), 2)
            # Several folders of one worktree: still one proof.
            del seen[:]
            patch = ''.join('*** Add File: ' + str(linked.root / rel) + '\n'
                            + ''.join('+' + line + '\n' for line in EDIT_TEXT.splitlines())
                            for rel in ('notes/a.md', 'docs/b.md', 'src/deep/c.md'))
            self.posttool({'hook_event_name': 'PostToolUse', 'session_id': 'folders',
                           'tool_name': 'apply_patch', 'tool_use_id': 'tool-folders',
                           'tool_input': {'command': '*** Begin Patch\n' + patch + '*** End Patch'}},
                          'codex')
            self.assertEqual(len(seen), 2)
        self.assertTrue(all('rev-parse' in argv for argv in seen))
        [short] = self.rows('posttool', 'short')
        self.assertEqual((short['editTarget'], short['editRecall']), ('linked', 'skip_short_edit'))

    def test_hermes_keeps_no_injection_and_no_capture(self):
        linked = self.worktree()
        answer, capture = self.posttool(
            edit_payload('hermes', rel=str(linked.root / NEW_FILE)), 'hermes')
        self.assertEqual((answer, capture), ({}, []))
        self.assertEqual(self.rows('recall.edit', 'hermes'), [])


class MonorepoProject(LinkedCase):
    """A project in a subfolder of its repository: the worktree project sits at the same place."""

    def setUp(self):
        super().setUp()
        repo = self.home / 'mono'
        repo.mkdir()
        self.mono = make_project(repo, name='mono', git=False, dirname='app')
        self.mono.git('-C', str(repo), 'init', '-q')
        note = self.mono.write_note('notes/heron.md', 'The emerald heron nests here.\n')
        self.mono.write_card('heron.md', title='Emerald heron', body=CARD_BODY, sources=[note])
        (repo / 'other').mkdir()
        (repo / 'other' / 'readme.md').write_text('other project\n', encoding='utf-8')
        self.mono.git('-C', str(repo), 'add', '-A')
        self.mono.git('-C', str(repo), 'commit', '-qm', 'mono')
        self.mono_install = build_install(self, self.mono, install_id='5' * 32)
        self.mono_install.settle()
        self.tree = self.home / 'mono-wt'
        self.mono.git('-C', str(repo), 'worktree', 'add', '-q', '-b', 'mono-branch', str(self.tree))

    def mono_posttool(self, session, target):
        from memvane.host.api import serve_selected
        from memvane.journal.api import select_route
        cwd = str(self.mono.root)
        with response.learning_capture() as slot:
            answer = serve_selected('posttool', 'claude', select_route(cwd, action='posttool'),
                                    {'cwd': cwd, **edit_payload(session, rel=str(target))})
        rows = [row for row in self.mono_install.events()
                if row.get('event') == 'posttool' and row.get('session') == session]
        return answer, list(slot), rows

    def test_the_same_place_is_linked_and_another_folder_is_outside(self):
        (self.tree / 'app' / 'notes').mkdir(exist_ok=True)
        (self.tree / 'app' / NEW_FILE).write_text(EDIT_TEXT, encoding='utf-8')
        _answer, [entry], [row] = self.mono_posttool('mono-in', self.tree / 'app' / NEW_FILE)
        self.assertEqual(row['editTarget'], 'linked')
        self.assertEqual(entry[2]['_memvane_edit_root'], str(self.tree / 'app'))
        self.assertEqual(entry[2]['_memvane_edit_paths'], [NEW_FILE])
        answer, capture, [row] = self.mono_posttool('mono-out', self.tree / 'other' / 'notes.md')
        self.assertEqual((answer, capture), ({}, []))
        self.assertEqual((row['editTarget'], row['editRecall']), ('outside', 'skip_outside_project'))
        self.assertNotIn('checkoutProofMs', row)


class LinkedEditCapture(LinkedCase):
    """The capture child: proof of the edit root, both git-ignore checks, the worktree's switch."""

    def setUp(self):
        super().setUp()
        # A second card: a case needs a pool of at least two.
        self.fixture.write_card('pond.md', title='North pond', sources=[self.project / 'notes/heron.md'],
                                body=CARD_BODY.strip() + ' The survey copy.\n')
        # Explicit path: the route files of the install stay untracked, as in a real project.
        self.fixture.git('add', '--', '.memvane/wiki/pond.md')
        self.fixture.commit('second card', everything=False)
        self.install.settle()
        self.linked = self.worktree()
        self.state = self.tmp / 'learning-state'
        self.state.mkdir(mode=0o700)

    def entry(self, session, rel=NEW_FILE):
        (self.linked.root / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.linked.root / rel).write_text(EDIT_TEXT, encoding='utf-8')
        _answer, [entry] = self.posttool(edit_payload(session, rel=str(self.linked.root / rel)))
        return entry

    def store(self, entry, **changes):
        from memvane.learning.api import capture_recall
        host, decision, payload, settings, pool, selected = entry
        settings = dict(settings, contextLearning={**settings['contextLearning'],
                                                   'stateRoot': str(self.state)})
        return capture_recall(host, decision, {**payload, **changes}, settings, pool, selected)

    def samples(self):
        found = list(self.state.rglob('learning.sqlite3'))
        if not found:
            return []
        with closing(sqlite3.connect(str(found[0]))) as db:
            return [json.loads(raw) for (raw,) in db.execute('SELECT body FROM samples')]

    def capture_rows(self, session):
        return [(row['outcome'], row.get('editTarget')) for row in self.rows('learning.capture', session)]

    def test_the_case_is_complete_against_the_main_head_and_reads_the_worktree_file(self):
        self.assertFalse((self.project / NEW_FILE).exists())
        entry = self.entry('stored')
        self.assertEqual(self.store(entry), 'stored')
        [sample] = self.samples()
        self.assertEqual(sample['schema_version'], 5)
        self.assertEqual(len(sample['candidates']), 2)
        self.assertTrue(sample['sampling']['complete_within_returned_bound'])
        self.assertEqual(sample['sampling']['snapshot_failures'], [])
        self.assertEqual(sample['edit_event']['after_sha256'],
                         hashlib.sha256(EDIT_TEXT.encode()).hexdigest())
        self.assertEqual(sample['edit_event']['path_sha256'],
                         hashlib.sha256(NEW_FILE.encode()).hexdigest())
        dumped = json.dumps(sample)
        self.assertNotIn(str(self.linked.root), dumped)
        self.assertNotIn('_memvane', dumped)
        self.assertEqual(self.capture_rows('stored'), [('stored', 'linked')])
        # The same shape as a main-checkout case: an older bundle's worker reads it as one.
        (self.project / NEW_FILE).write_text(EDIT_TEXT, encoding='utf-8')
        _answer, [main] = self.posttool(edit_payload('stored-main'))
        self.assertEqual(self.store(main), 'stored')
        ours = next(row for row in self.samples() if row['request_id'] != sample['request_id'])
        self.assertEqual(sorted(ours), sorted(sample))
        self.assertEqual(sorted(ours['edit_event']), sorted(sample['edit_event']))
        self.assertEqual(sorted(ours['sampling']), sorted(sample['sampling']))
        self.assertEqual(ours['edit_event']['path_sha256'], sample['edit_event']['path_sha256'])
        self.assertEqual(self.capture_rows('stored-main'), [('stored', 'main')])

    def test_a_path_either_checkout_ignores_is_not_stored(self):
        # Only the main checkout has the rule: a branch cut before it still obeys it.
        (self.project / '.gitignore').write_text('private/\n', encoding='utf-8')
        self.assertEqual(self.store(self.entry('main-rule', 'private/a.md')), 'edit_git_ignored')
        (self.project / '.gitignore').unlink()
        (self.linked.root / '.gitignore').write_text('scratch/\n', encoding='utf-8')
        self.assertEqual(self.store(self.entry('own-rule', 'scratch/a.md')), 'edit_git_ignored')
        self.assertEqual(self.samples(), [])
        self.assertEqual(self.store(self.entry('no-rule', 'notes/kept.md')), 'stored')
        self.assertEqual(len(self.samples()), 1)

    def test_git_that_cannot_answer_in_either_checkout_refuses_the_case(self):
        import shutil
        entry = self.entry('git-fails')
        # A git whose check-ignore fails in one checkout and answers everywhere else.
        bin_dir, _log = fake_git(self.tmp, 'case " $* " in *" check-ignore "*) '
                                 '[ "$(pwd -P)" = "$FAIL_DIR" ] && exit 2;; esac\nexec "$REAL_GIT" "$@"')
        for failing in (self.linked.root, self.project):
            with self.subTest(checkout=failing.name):
                with mock.patch.dict(os.environ, {
                        'PATH': str(bin_dir) + os.pathsep + os.environ['PATH'],
                        'REAL_GIT': shutil.which('git'), 'FAIL_DIR': str(failing)}):
                    self.assertEqual(self.store(entry), 'edit_git_check_failed')
        self.assertEqual(self.samples(), [])
        self.assertEqual(self.store(entry), 'stored')

    def test_the_worktree_settings_can_turn_capture_off(self):
        entry = self.entry('switch')
        self.linked.settings_path.write_text(json.dumps(
            {'name': 'fixture', 'contextLearning': {'capture': False}}), encoding='utf-8')
        self.assertEqual(self.store(entry), 'disabled_in_checkout')
        self.linked.settings_path.write_text('{"name": "fixture", "unknown": 1}', encoding='utf-8')
        self.assertEqual(self.store(entry), 'edit_checkout_unproved')
        self.assertEqual(self.samples(), [])

    def test_a_forged_missing_or_removed_edit_root_is_not_stored(self):
        other = make_project(self.home, name='other', dirname='other')
        other.write_note(NEW_FILE, EDIT_TEXT)
        other.commit('other')
        foreign = other.linked_worktree('wt', 'other-branch')
        entry = self.entry('forged')
        for root in (str(foreign.root), str(other.root), str(self.project), str(self.home),
                     str(self.linked.root / 'notes'), 'relative/path', None, 7):
            with self.subTest(root=root):
                self.assertEqual(self.store(entry, _memvane_edit_root=root), 'edit_checkout_unproved')
        payload = dict(entry[2])
        del payload['_memvane_edit_root']
        self.assertEqual(self.store(entry[:2] + (payload,) + entry[3:]), 'edit_checkout_unproved')
        self.assertEqual(self.store(entry, _memvane_edit_target='elsewhere'), 'edit_capture_invalid')
        self.fixture.git('worktree', 'remove', '--force', str(self.linked.root))
        self.assertEqual(self.store(entry), 'edit_checkout_unproved')
        # The folder comes back as a plain directory: still not this project's worktree.
        (self.linked.root / 'notes').mkdir(parents=True)
        (self.linked.root / NEW_FILE).write_text(EDIT_TEXT, encoding='utf-8')
        self.assertEqual(self.store(entry), 'edit_checkout_unproved')
        self.assertEqual(self.samples(), [])


class LinkedEditEntry(LinkedCase):
    """The real dispatcher in a child: a git that never answers costs the proof's own bound."""

    def test_a_git_that_does_not_answer_ends_the_hook_with_an_empty_answer(self):
        linked = self.worktree()
        bin_dir, log = fake_git(self.tmp, 'exec sleep 30')
        payload = edit_payload('hang', rel=str(linked.root / NEW_FILE))
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
               'PATH': str(bin_dir) + os.pathsep + os.environ['PATH']}
        for key in ('QMD_SANDBOX', 'CLAUDE_SANDBOX', 'CLAUDE_HEADLESS', 'CLAUDE_PLUGIN_ROOT',
                    'PLUGIN_ROOT'):
            env.pop(key, None)
        done = subprocess.run(['bash', str(ROOT / 'hooks' / 'run-hook'), 'posttool', 'claude'],
                              input=json.dumps({'cwd': str(self.project), **payload}).encode(),
                              capture_output=True, env=env, cwd=str(self.project), timeout=120)
        self.assertEqual((done.returncode, done.stdout.decode()), (0, '{}\n'), done.stderr.decode())
        self.skipped('hang', 'linked_unproved', 'skip_checkout_proof_timeout')
        # The first rev-parse is stopped at the proof's bound; the second never starts.
        self.assertEqual(calls(log), 1)
        self.assertEqual(self.rows('learning.capture', 'hang'), [])

    def test_a_proof_error_inside_the_tail_answers_empty(self):
        linked = self.worktree()
        with mock.patch.object(edit_recall, 'edit_checkout', side_effect=RuntimeError('boom')):
            answer, capture = self.posttool(edit_payload('boom', rel=str(linked.root / NEW_FILE)))
        self.assertEqual((answer, capture), ({}, []))
        from memvane.kernel.hook_budget import HookDeadlineExceeded
        with mock.patch.object(edit_recall, 'edit_checkout', side_effect=HookDeadlineExceeded()):
            answer, capture = self.posttool(edit_payload('late', rel=str(linked.root / NEW_FILE)))
        self.assertEqual((answer, capture), ({}, []))


if __name__ == '__main__':
    unittest.main()

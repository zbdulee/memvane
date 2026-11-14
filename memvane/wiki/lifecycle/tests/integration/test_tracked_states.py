"""Git source tracking fails closed and bounds subprocess calls by chunk."""
from unittest.mock import patch

from memvane.kernel import paths as git_paths
from memvane.wiki.lifecycle import git_branch_safety as safety
from tests.support.case import FixtureCase
from tests.support.project import make_project


class TrackedStates(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home)
        self.project.write_note('docs/source.md', 'synthetic\n')
        self.project.git('add', '--', '.memvane/settings.json', 'docs/source.md')
        self.project.git('commit', '-qm', 'fixture')
        self.head = self.project.git('rev-parse', 'HEAD').strip()

    def test_git_failure_or_timeout_returns_unknown_without_further_calls(self):
        for reason in ('git_exit_128', 'git_timeout'):
            with self.subTest(reason=reason), \
                 patch.object(safety, 'run_git', side_effect=git_paths.GitUnavailable(reason)) as run, \
                 patch.object(git_paths, 'run_git', new=run):
                result = safety.tracked_states(self.project.root, ['docs/source.md'], self.head)
            self.assertEqual(result, {'docs/source.md': 'unknown'})
            self.assertEqual(run.call_count, 1)

    def test_git_calls_are_bounded_by_pathspec_chunks(self):
        paths = ['docs/source.md'] + ['docs/missing-%03d.md' % n
                                     for n in range(safety.MAX_PATHSPECS_PER_CALL - 1)]
        real = git_paths.run_git
        calls = []
        def counted(args, cwd, **kwargs):
            calls.append(args[0])
            return real(args, cwd, **kwargs)
        with patch.object(safety, 'run_git', side_effect=counted) as run, \
             patch.object(git_paths, 'run_git', new=run):
            states = safety.tracked_states(self.project.root, paths, self.head)
            self.assertEqual(states['docs/source.md'], 'tracked')
            self.assertEqual({states[path] for path in paths[1:]}, {'untracked'})
            self.assertEqual(calls, ['rev-parse', 'ls-files', 'ls-tree'])
            calls.clear()
            safety.tracked_states(self.project.root, paths + ['docs/extra-a.md', 'docs/extra-b.md'], self.head)
        self.assertEqual(calls, ['rev-parse', 'ls-files', 'ls-files', 'ls-tree', 'ls-tree'])

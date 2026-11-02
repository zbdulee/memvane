"""Real local promisor Git; synthetic host cache/runtime artifacts only.

The full sparse fixture prepares its baseline before its source is replaced
with a blob:none file:// clone. No host clone, model or operating store is
used. Each test gets an independent disposable HOME and partial clone.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
SPARSE = runpy.run_path(str(HERE / 'laya-codex-sparse-upgrade-checks.py'))
bundle, auto = SPARSE['bundle'], SPARSE['auto']


class PartialUpgradeChecks(unittest.TestCase):
    def setUp(self):
        outer = tempfile.TemporaryDirectory(prefix='codex-partial-home-')
        self.addCleanup(outer.cleanup)
        self.home = Path(outer.name).resolve()
        environment = mock.patch.dict(os.environ, {
            'HOME': str(self.home), 'GIT_CONFIG_GLOBAL': os.devnull,
            'GIT_CONFIG_SYSTEM': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_ALLOW_PROTOCOL': 'file', 'GIT_TERMINAL_PROMPT': '0',
            'GIT_DEFAULT_HASH': 'sha1', 'QMD_SANDBOX': '1',
            'QMD_SETUP_GUARD_FIXTURE': '1'})
        environment.start()
        self.addCleanup(environment.stop)
        # Observation-only flags must not mask product-side lazy fetching.
        os.environ.pop('GIT_NO_LAZY_FETCH', None)
        os.environ.pop('GIT_TRACE2_EVENT', None)
        real_run = subprocess.run
        def bounded_setup(*args, **kwargs):
            kwargs.setdefault('timeout', 30)
            return real_run(*args, **kwargs)
        case = SPARSE['CodexSparseChecks']('runTest')
        self.addCleanup(case.doCleanups)
        with mock.patch.object(subprocess, 'run', side_effect=bounded_setup):
            case.setUp()
        self.case = case
        self.full_source = case.source
        self.remote = case.fixture.source
        self.partial = case.home / 'partial-marketplace'
        from laya_checkout_view import NO_LAZY_FETCH_PATCHES
        version = self.git(case.home, '--version')
        parsed = re.match(r'^git version (\d+)\.(\d+)\.(\d+)(?=$|[ .])', version)
        self.assertIsNotNone(parsed, version)
        major, minor, patch = map(int, parsed.groups())
        self.assertTrue(major == 2 and (minor >= 46 or
                        (minor in NO_LAZY_FETCH_PATCHES and
                         patch >= NO_LAZY_FETCH_PATCHES[minor])), version)
        self.git(self.remote, 'config', 'uploadpack.allowFilter', 'true')
        self.git(case.home, 'clone', '-q', '--filter=blob:none', '--no-checkout',
                 self.remote.as_uri(), str(self.partial), lazy=True)
        self.git(self.partial, 'sparse-checkout', 'init', '--cone', lazy=True)
        self.git(self.partial, 'sparse-checkout', 'set', '.agents/plugins', lazy=True)
        self.git(self.partial, 'checkout', '-q', 'HEAD', lazy=True)
        self.assertEqual(self.git(self.partial, 'config', '--bool',
                                  'remote.origin.promisor'), 'true')
        self.assertTrue(list((self.partial / '.git/objects/pack').glob('*.promisor')))
        self.assertFalse((self.partial / 'core').exists())
        self.assertTrue(self.missing())
        rows = self.git(self.partial, 'ls-tree', '-r', 'HEAD', '--',
                        'core/laya_local_embed_provider.py',
                        'core/laya_local_embed_worker.mjs').splitlines()
        self.assertEqual(len(rows), 2)
        self.assertTrue({row.split()[2] for row in rows} <= self.missing())
        self.case.source = self.partial
        self.trace_number = 0

    @staticmethod
    def git(root, *args, lazy=False, input=None):
        env = dict(os.environ)
        env.pop('GIT_TRACE2_EVENT', None)
        if lazy:
            env.pop('GIT_NO_LAZY_FETCH', None)
        else:
            env['GIT_NO_LAZY_FETCH'] = '1'
        done = subprocess.run(('git', '--no-replace-objects', '-C', str(root), *args),
                              input=input, encoding='utf-8', check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=env, timeout=30)
        return done.stdout.strip()

    def missing(self):
        return {line[1:] for line in self.git(
            self.partial, 'rev-list', '--objects', '--missing=print', 'HEAD').splitlines()
                if line.startswith('?')}

    def offline(self):
        moved = self.remote.with_name(self.remote.name + '-offline')
        self.remote.rename(moved)
        self.assertFalse(self.remote.exists())

    def observe(self, label, operation):
        before = self.missing()
        self.trace_number += 1
        trace = self.home / ('trace-' + str(self.trace_number) + '.jsonl')
        error, result = None, None
        with mock.patch.dict(os.environ, {'GIT_TRACE2_EVENT': str(trace)}):
            try:
                result = operation()
            except Exception as exc:
                error = exc
        after = self.missing()
        events = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
        children = [event for event in events if event.get('event') == 'child_start']
        fetches = [event for event in children if 'fetch' in event.get('argv', [])]
        transports = [event for event in children if
                      str(event.get('child_class', '')).startswith('transport') or
                      any('upload-pack' in str(arg) or 'git-remote-' in str(arg)
                          for arg in event.get('argv', []))]
        evidence = {'case': label, 'missingBefore': len(before), 'missingAfter': len(after),
                    'missingUnchanged': before == after, 'fetchChildren': len(fetches),
                    'transportChildren': len(transports),
                    'error': str(error) if error is not None else None}
        # Keep observations in the guard log even after fixture cleanup/RED.
        print(json.dumps(evidence, sort_keys=True), flush=True)
        return result, error, before, after, evidence

    def assert_no_fetch(self, observation):
        _result, _error, before, after, evidence = observation
        self.assertEqual(evidence['fetchChildren'], 0, evidence)
        self.assertEqual(evidence['transportChildren'], 0, evidence)
        self.assertEqual(after, before, evidence)

    @contextmanager
    def version_response(self, version, *, config_failure=False):
        real_run = subprocess.run
        commands = []
        def run(argv, *args, **kwargs):
            if '--version' in argv and Path(str(argv[0])).name == 'git':
                text_mode = kwargs.get('text') or kwargs.get('encoding') or kwargs.get('universal_newlines')
                raw = version + '\n'
                return subprocess.CompletedProcess(argv, 0,
                                                    raw if text_mode else raw.encode(),
                                                    '' if text_mode else b'')
            if config_failure and 'config' in argv and '--get-regexp' in argv:
                return subprocess.CompletedProcess(argv, 128, b'', b'')
            if 'merge-base' in argv:
                commands.append(tuple(argv))
            return real_run(argv, *args, **kwargs)
        with mock.patch.object(subprocess, 'run', side_effect=run):
            yield commands

    def test_online_provider_review_matches_baseline_without_fetching_blobs(self):
        observation = self.observe('online-review', self.case.review)
        result, error, *_ = observation
        self.assertIsNone(error)
        self.assertEqual(result, self.case.baseline)
        self.assert_no_fetch(observation)

    def test_offline_full_adoption_keeps_missing_blobs_and_transport_idle(self):
        self.offline()
        observation = self.observe('offline-adopt', self.case.adopt)
        result, error, *_ = observation
        self.assertIsNone(error)
        self.assertEqual(result['sourceCommit'], self.case.commit)
        self.assertIn('core/config.py', result['includedFiles'])
        self.assertEqual(bundle.inspect_full_plugin_candidate(self.case.installed), result)
        self.assert_no_fetch(observation)

    def test_offline_security_refusals_are_specific_and_do_not_fetch(self):
        self.offline()
        installed = self.case.installed
        cases = ('cache-bytes', 'cache-mode', 'cache-extra', 'dirty-source',
                 'symlink-parent', 'non-sparse-deletion')
        for kind in cases:
            with self.subTest(kind=kind):
                cache = installed / 'core/config.py'
                raw, mode = cache.read_bytes(), cache.stat().st_mode & 0o777
                readme = self.partial / 'README.md'
                readme_raw, readme_mode = readme.read_bytes(), readme.stat().st_mode & 0o777
                extra = installed / 'unexpected-fixture-file'
                source_core = self.partial / 'core'
                expected = 'installed_blob_not_exact_commit'
                try:
                    if kind == 'cache-bytes':
                        cache.write_bytes(raw + b'\n# changed synthetic cache\n')
                    elif kind == 'cache-mode':
                        cache.chmod(0o700)
                    elif kind == 'cache-extra':
                        extra.write_text('synthetic extra\n')
                        expected = 'installed_file_set_not_exact_commit'
                    elif kind == 'dirty-source':
                        source_core.mkdir()
                        (source_core / 'config.py').write_text('# materialized dirty source\n')
                        expected = 'source_not_exact_commit'
                    elif kind == 'symlink-parent':
                        source_core.symlink_to(installed / 'core', target_is_directory=True)
                        expected = 'unsafe_source_parent'
                    else:
                        flags = self.git(self.partial, 'ls-files', '-t', '--', 'README.md')
                        self.assertTrue(flags.startswith('H '), flags)
                        readme.unlink()
                        expected = 'bundle_file_unavailable'
                    observation = self.observe(kind, self.case.adopt)
                    _result, error, *_ = observation
                    self.assertIsInstance(error, bundle.BundleEvidenceError)
                    self.assertEqual(str(error), expected)
                    if kind == 'non-sparse-deletion':
                        self.assertEqual(error.detail, {'path': str(readme), 'errno': errno.ENOENT})
                    self.assert_no_fetch(observation)
                finally:
                    cache.write_bytes(raw)
                    cache.chmod(mode)
                    extra.unlink(missing_ok=True)
                    if source_core.is_symlink():
                        source_core.unlink()
                    elif source_core.exists():
                        shutil.rmtree(source_core)
                    readme.write_bytes(readme_raw)
                    readme.chmod(readme_mode)

    def test_old_or_unknown_git_refuses_partial_ancestry_before_merge_base(self):
        for version in ('git version 2.39.0', 'unparseable synthetic Git version'):
            with self.subTest(version=version), self.version_response(version) as commands:
                observation = self.observe('partial-version-' + version,
                    lambda: auto._git(self.partial, 'merge-base', self.case.old_commit, self.case.commit))
                _result, error, *_ = observation
                print(json.dumps({'case': 'partial-version-gate',
                                  'version': version, 'mergeBaseCommands': len(commands)}), flush=True)
                self.assertIsNotNone(error)
                self.assertRegex(str(error), r'^source_git_no_lazy_fetch_(unsupported|unavailable)$')
                self.assertEqual(commands, [])
                self.assert_no_fetch(observation)
        with self.subTest(config_failure=True), \
                self.version_response('git version 2.39.0', config_failure=True) as commands:
            observation = self.observe('partial-config-unavailable',
                lambda: auto._git(self.partial, 'merge-base', self.case.old_commit, self.case.commit))
            _result, error, *_ = observation
            print(json.dumps({'case': 'partial-config-gate',
                              'mergeBaseCommands': len(commands)}), flush=True)
            self.assertIsNotNone(error)
            self.assertEqual(str(error), 'source_git_no_lazy_fetch_unavailable')
            self.assertEqual(commands, [])
            self.assert_no_fetch(observation)

    def test_old_git_still_performs_ancestry_on_a_full_clone(self):
        self.assertFalse(list((self.full_source / '.git/objects/pack').glob('*.promisor')))
        # A false-only setting never registers a promisor remote. A later
        # false does not cancel an earlier true (covered separately below).
        self.git(self.full_source, 'config', '--replace-all', 'remote.origin.promisor', 'false')
        for version in ('git version 2.39.0', 'unparseable synthetic Git version'):
            with self.subTest(version=version), self.version_response(version) as commands:
                observation = self.observe('full-version-' + version,
                    lambda: auto._git(self.full_source, 'merge-base',
                                      self.case.old_commit, self.case.commit))
                result, error, *_ = observation
                self.assertIsNone(error)
                self.assertEqual(result, (0, self.case.old_commit))
                self.assertEqual(len(commands), 1)
                self.assert_no_fetch(observation)

    def test_true_then_false_still_requires_admission_before_object_commands(self):
        # Git v2.39.0 promisor-remote.c registers on every true value and
        # ignores false; registration is not a last-value boolean reduction.
        # Remove the other partial markers so this counterexample is specific.
        for key in ('extensions.partialclone', 'remote.origin.partialclonefilter'):
            done = subprocess.run(('git', '-C', str(self.partial), 'config', '--unset-all', key),
                                  encoding='utf-8', stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  check=False, timeout=30)
            self.assertIn(done.returncode, (0, 1, 5))
        self.git(self.partial, 'config', '--replace-all', 'remote.origin.promisor', 'true')
        self.git(self.partial, 'config', '--add', 'remote.origin.promisor', 'false')
        self.assertEqual(self.git(self.partial, 'config', '--get-all',
                                  'remote.origin.promisor').splitlines(), ['true', 'false'])
        missing_tree = 'e' * 40
        raw = ('tree ' + missing_tree + '\n'
               'author Synthetic <fixture@example.invalid> 1700000000 +0000\n'
               'committer Synthetic <fixture@example.invalid> 1700000000 +0000\n\n'
               'synthetic missing tree for promisor registration\n')
        commit = self.git(self.partial, 'hash-object', '-t', 'commit', '-w', '--stdin', input=raw)
        self.git(self.partial, 'update-ref', 'HEAD', commit)
        actual_version = self.git(self.partial, '--version')
        self.assertIn(missing_tree, self.missing())
        config_path = self.partial / '.git/config'
        config_before = config_path.read_bytes()
        config_mode = config_path.stat().st_mode & 0o777
        related_pattern = r'^(extensions\.partialclone|remote\..*\.(promisor|partialclonefilter))$'
        def related():
            return self.git(self.partial, 'config', '--get-regexp', related_pattern).splitlines()
        expected_related = ['remote.origin.promisor true', 'remote.origin.promisor false']
        related_before = related()
        self.assertEqual(related_before, expected_related)
        # This uses the real installed Git with its no-lazy-fetch feature
        # disabled through the environment, not an actual Git 2.39.0 binary.
        # The protocol fence prevents even local file transport execution.
        with mock.patch.dict(os.environ, {'GIT_ALLOW_PROTOCOL': ''}):
            def control():
                env = dict(os.environ)
                env.pop('GIT_NO_LAZY_FETCH', None)
                return subprocess.run(('git', '--no-replace-objects', '-C', str(self.partial),
                                       'cat-file', '-p', missing_tree), env=env,
                                      encoding='utf-8', stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, check=False, timeout=30)
            observed = self.observe('true-false-feature-off-control', control)
            result, error, before, after, evidence = observed
            config_after = config_path.read_bytes()
            related_after = related()
            print(json.dumps({'case': 'promisor-control-config',
                              'configBeforeHex': config_before.hex(),
                              'configAfterHex': config_after.hex(),
                              'relatedBefore': related_before, 'relatedAfter': related_after,
                              'controlChangedConfig': config_after != config_before},
                             sort_keys=True), flush=True)
            # A failed fetch may still save its --filter in remote config.
            # Restore the exact pre-control setup before testing admission.
            if config_after != config_before:
                config_path.write_bytes(config_before)
                config_path.chmod(config_mode)
            self.assertEqual(config_path.read_bytes(), config_before)
            self.assertEqual(related(), expected_related)
            print(json.dumps({'case': 'promisor-control-kind', 'actualGit': actual_version,
                              'simulation': 'actual Git with GIT_NO_LAZY_FETCH removed; not Git 2.39.0',
                              'returncode': result.returncode if result is not None else None},
                             sort_keys=True), flush=True)
            self.assertIsNone(error)
            self.assertNotEqual(result.returncode, 0)
            self.assertGreater(evidence['fetchChildren'], 0, evidence)
            self.assertEqual(evidence['transportChildren'], 0, evidence)
            self.assertEqual(before, after)
            operations = (
                ('merge-base', lambda: auto._git(self.partial, 'merge-base',
                                                self.case.old_commit, commit)),
                ('cat-file', lambda: bundle._git(self.partial, 'cat-file', '-p', missing_tree)))
            for name, operation in operations:
                with self.subTest(operation=name), self.version_response('git version 2.39.0'):
                    admission_related = related()
                    print(json.dumps({'case': 'promisor-admission-config', 'operation': name,
                                      'related': admission_related,
                                      'configHex': config_path.read_bytes().hex()},
                                     sort_keys=True), flush=True)
                    self.assertEqual(admission_related, expected_related)
                    self.assertEqual(config_path.read_bytes(), config_before)
                    actual_run = subprocess.run
                    entered = []
                    def count_commands(argv, *args, **kwargs):
                        if name in argv and Path(str(argv[0])).name == 'git':
                            entered.append(tuple(argv))
                        return actual_run(argv, *args, **kwargs)
                    with mock.patch.object(subprocess, 'run', side_effect=count_commands):
                        observation = self.observe('true-false-product-' + name, operation)
                    _result, failure, *_ = observation
                    print(json.dumps({'case': 'true-false-admission', 'operation': name,
                                      'actualObjectCommands': len(entered)}, sort_keys=True), flush=True)
                    self.assertIsNotNone(failure)
                    self.assertEqual(str(failure), 'source_git_no_lazy_fetch_unsupported')
                    self.assertEqual(entered, [])
                    self.assert_no_fetch(observation)

    def test_missing_tree_refuses_offline_without_transport(self):
        self.offline()
        missing_tree = 'f' * 40
        raw = ('tree ' + missing_tree + '\n'
               'author Synthetic <fixture@example.invalid> 1700000000 +0000\n'
               'committer Synthetic <fixture@example.invalid> 1700000000 +0000\n\n'
               'synthetic missing tree\n')
        commit = self.git(self.partial, 'hash-object', '-t', 'commit', '-w', '--stdin', input=raw)
        self.git(self.partial, 'update-ref', 'HEAD', commit)
        self.case.commit = commit
        self.assertEqual(self.git(self.partial, 'cat-file', '-t', commit), 'commit')
        self.assertIn(missing_tree, self.missing())
        observation = self.observe('offline-missing-tree', self.case.adopt)
        _result, error, *_ = observation
        self.assertIsInstance(error, bundle.BundleEvidenceError)
        self.assertEqual(str(error), 'source_commit_unavailable')
        self.assert_no_fetch(observation)

    def test_all_registration_rows_are_validated_including_hidden_malformed_values(self):
        config = self.full_source / '.git/config'
        original = config.read_bytes()
        cases = (
            ('true-malformed-false', '\n[remote "origin"]\n'
             'promisor = true\npromisor = malformed\npromisor = false\n',
             'source_git_no_lazy_fetch_unavailable'),
            ('valueless-false', '\n[remote "origin"]\npromisor\npromisor = false\n',
             'source_git_no_lazy_fetch_unsupported'),
            ('filter-malformed-false', '\n[remote "origin"]\npartialclonefilter = blob:none\n'
             'promisor = malformed\npromisor = false\n',
             'source_git_no_lazy_fetch_unavailable'),
            ('extension-malformed-false', '\n[extensions]\npartialclone = origin\n'
             '[remote "origin"]\npromisor = malformed\npromisor = false\n',
             'source_git_no_lazy_fetch_unavailable'),
            ('false-only', '\n[remote "origin"]\npromisor = false\n', None))
        try:
            for name, suffix, expected in cases:
                with self.subTest(case=name):
                    config.write_bytes(original + suffix.encode())
                    with self.version_response('git version 2.39.0') as commands:
                        observation = self.observe('config-rows-' + name,
                            lambda: auto._git(self.full_source, 'merge-base',
                                             self.case.old_commit, self.case.commit))
                    result, error, *_ = observation
                    print(json.dumps({'case': 'config-rows-admission-' + name,
                                      'actualObjectCommands': len(commands)}, sort_keys=True), flush=True)
                    if expected is None:
                        self.assertIsNone(error)
                        self.assertEqual(result, (0, self.case.old_commit))
                        self.assertEqual(len(commands), 1)
                    else:
                        self.assertIsNotNone(error)
                        self.assertEqual(str(error), expected)
                        self.assertEqual(commands, [])
                    self.assert_no_fetch(observation)
        finally:
            config.write_bytes(original)


if __name__ == '__main__':
    unittest.main()

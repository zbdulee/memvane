"""Diagnostic opt-in (Task 1): `diagnostics.enabled` normalization and the
fail-closed consent check `diagnostic_trace.enabled(decision) -> bool`.

Only synthetic constants are used. Every project and state directory is a
fresh temporary directory under the temporary HOME provided by the Node
wrapper; nothing touches the real account home, real install state or any
user file. No child process is spawned.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'core'))
import config  # noqa: E402

SYNTH_NAME = 'synthetic-diag-fixture'
SYNTH_INSTALL = 'install-synthetic-1'
SYNTH_COLLECTION = 'synthetic-diag-wiki'

# Values that must never turn diagnostics on (only the JSON boolean true does).
NOT_TRUE_ENABLED = ['true', 'True', 'TRUE', 'yes', 'on', '1', 1, 1.0, 2, 0, -1,
                    None, False, [], [True], {}, {'enabled': True}]
NOT_DICT_DIAGNOSTICS = [True, 'true', 'on', 1, 0, None, [], [{'enabled': True}]]


def full_settings(**extra):
    base = {'name': SYNTH_NAME, 'collections': [SYNTH_COLLECTION],
            'collectionPaths': {SYNTH_COLLECTION: '.memvane/wiki'},
            'collectionRoles': {SYNTH_COLLECTION: 'wiki'},
            'recallStrategy': 'wikiOnly', 'topN': 3}
    base.update(extra)
    return base


def minimal_settings(**extra):
    base = {'name': SYNTH_NAME, 'skipPaths': ['synthetic-skip']}
    base.update(extra)
    return base


def trace():
    """Lazily import the future module so config checks fail independently."""
    import diagnostic_trace
    return diagnostic_trace


class Base(unittest.TestCase):
    def setUp(self):
        home = Path(os.environ.get('HOME', '')).resolve()
        tmp_root = Path(tempfile.gettempdir()).resolve()
        # Refuse to run against a real account home: the wrapper must supply a
        # realpath temporary HOME and the setup-guard fixture switch.
        self.assertEqual(os.environ.get('QMD_SETUP_GUARD_FIXTURE'), '1')
        self.assertTrue(home != tmp_root and tmp_root in home.parents, str(home))
        self.assertEqual(Path.home().resolve(), home)
        tmp = tempfile.TemporaryDirectory(prefix='diag-trace-', dir=str(home))
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self._mtime = 1_700_000_000

    # ------------------------------------------------------------ helpers
    def project(self, name='project'):
        root = self.base / name
        (root / '.memvane').mkdir(parents=True)
        return root

    def write_settings(self, project, payload):
        settings = project / '.memvane' / 'settings.json'
        text = payload if isinstance(payload, str) else json.dumps(payload)
        partial = settings.with_name('settings.json.tmp')
        partial.write_text(text, encoding='utf-8')
        os.replace(partial, settings)
        # Distinct, monotonically increasing mtimes defeat any stat cache.
        self._mtime += 10
        os.utime(settings, (self._mtime, self._mtime))
        return settings

    def candidate(self, project, state='candidate'):
        state_dir = self.base / ('state-' + project.name)
        state_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(state_dir, 0o700)
        return {'state': state, 'reason': 'synthetic', 'projectRoot': str(project),
                'journalPath': str(state_dir / 'install-journal.sqlite'),
                'installId': SYNTH_INSTALL, 'collections': [{'name': SYNTH_COLLECTION}]}

    def worktree(self, worktree_project, main_route):
        return {'state': 'worktree', 'reason': 'linked_worktree_read_only',
                'worktreeRoot': str(worktree_project),
                'worktreeProjectRoot': str(worktree_project),
                'gitCommonDir': str(self.base / 'synthetic-common-git'),
                'mainRoute': main_route}

    def enabled(self, decision):
        module = trace()

        def no_child(*_args, **_kwargs):
            raise AssertionError('diagnostic consent must not spawn a child')
        with patch.object(subprocess, 'Popen', side_effect=no_child), \
                patch.object(os, 'fork', side_effect=no_child, create=True):
            result = module.enabled(decision)
        self.assertIsInstance(result, bool)
        return result


# ------------------------------------------------------------- config layer

class ConfigNormalization(Base):
    def test_full_config_only_boolean_true_enables(self):
        self.assertEqual(config.normalize_config(full_settings())['diagnostics'],
                         {'enabled': False})
        self.assertEqual(config.normalize_config({})['diagnostics'], {'enabled': False})
        self.assertIs(config.normalize_config(
            full_settings(diagnostics={'enabled': True}))['diagnostics']['enabled'], True)
        self.assertIs(config.normalize_config(
            full_settings(diagnostics={'enabled': False}))['diagnostics']['enabled'], False)
        for value in NOT_TRUE_ENABLED:
            with self.subTest(enabled=value):
                got = config.normalize_config(full_settings(diagnostics={'enabled': value}))
                self.assertIs(got['diagnostics']['enabled'], False)
        for value in NOT_DICT_DIAGNOSTICS:
            with self.subTest(diagnostics=value):
                got = config.normalize_config(full_settings(diagnostics=value))
                self.assertEqual(got['diagnostics'], {'enabled': False})
        # Normalization never mutates the caller's raw dict.
        raw = full_settings(diagnostics={'enabled': 'true'})
        snapshot = copy.deepcopy(raw)
        config.normalize_config(raw)
        self.assertEqual(raw, snapshot)

    def test_minimal_config_preserved_and_only_boolean_true_enables(self):
        plain = config.normalize_config(minimal_settings())
        self.assertEqual(plain.get('settingsModel'), config.MINIMAL_SETTINGS_MODEL)
        self.assertEqual(plain['diagnostics'], {'enabled': False})
        for value, expected in ((True, True), (False, False)):
            with self.subTest(enabled=value):
                got = config.normalize_config(minimal_settings(diagnostics={'enabled': value}))
                self.assertEqual(got.get('settingsModel'), config.MINIMAL_SETTINGS_MODEL)
                self.assertIs(got['diagnostics']['enabled'], expected)
                self.assertEqual(got['collections'], plain['collections'])
                self.assertEqual(got['collectionPaths'], plain['collectionPaths'])
        for value in NOT_TRUE_ENABLED:
            with self.subTest(enabled=value):
                got = config.normalize_config(minimal_settings(diagnostics={'enabled': value}))
                self.assertIs(got['diagnostics']['enabled'], False)
        for value in NOT_DICT_DIAGNOSTICS:
            with self.subTest(diagnostics=value):
                got = config.normalize_config(minimal_settings(diagnostics=value))
                self.assertEqual(got['diagnostics'], {'enabled': False})

    def test_diagnostics_does_not_change_other_effective_fields(self):
        for make in (full_settings, minimal_settings):
            plain = config.normalize_config(make())
            for value in (True, False, 'true', {'nested': True}):
                with self.subTest(shape=make.__name__, enabled=value):
                    got = config.normalize_config(make(diagnostics={'enabled': value}))
                    self.assertEqual({k: v for k, v in got.items() if k != 'diagnostics'},
                                     {k: v for k, v in plain.items() if k != 'diagnostics'})

    def test_settings_identity_unchanged_by_diagnostics(self):
        import laya_index_host_route as route
        for make in (full_settings, minimal_settings):
            with self.subTest(shape=make.__name__):
                plain_project = self.project('identity-plain-' + make.__name__)
                opted_project = self.project('identity-opted-' + make.__name__)
                self.write_settings(plain_project, make())
                self.write_settings(opted_project, make(diagnostics={'enabled': True}))
                self.assertEqual(route._settings_identity(opted_project),
                                 route._settings_identity(plain_project))

    def test_load_project_config_reads_opt_in_from_temp_project(self):
        project = self.project()

        def loaded():
            return config.load_project_config(str(project))['diagnostics']['enabled']
        self.assertIs(loaded(), False)                       # no settings file
        self.write_settings(project, full_settings())
        self.assertIs(loaded(), False)                       # key missing
        self.write_settings(project, full_settings(diagnostics={'enabled': True}))
        self.assertIs(loaded(), True)
        self.write_settings(project, full_settings(diagnostics={'enabled': 'true'}))
        self.assertIs(loaded(), False)
        self.write_settings(project, minimal_settings(diagnostics={'enabled': True}))
        self.assertIs(loaded(), True)
        self.write_settings(project, '{"name": "' + SYNTH_NAME + '", "diagnostics": {"enab')
        self.assertIs(loaded(), False)                       # truncated JSON
        self.write_settings(project, '')
        self.assertIs(loaded(), False)                       # empty (in-progress writer)
        settings = self.write_settings(project, full_settings(diagnostics={'enabled': True}))
        if os.geteuid() != 0:
            os.chmod(settings, 0)
            try:
                self.assertIs(loaded(), False)               # unreadable
            finally:
                os.chmod(settings, 0o600)


# --------------------------------------------------------- consent contract

class EnabledContract(Base):
    def test_module_exposes_enabled(self):
        self.assertTrue(callable(getattr(trace(), 'enabled', None)))

    def test_candidate_rereads_settings_on_every_call(self):
        project = self.project()
        decision = self.candidate(project)
        self.write_settings(project, full_settings(diagnostics={'enabled': True}))
        self.assertIs(self.enabled(decision), True)
        self.write_settings(project, full_settings(diagnostics={'enabled': False}))
        self.assertIs(self.enabled(decision), False)
        self.write_settings(project, full_settings(diagnostics={'enabled': True}))
        self.assertIs(self.enabled(decision), True)
        self.write_settings(project, full_settings())
        self.assertIs(self.enabled(decision), False)
        self.write_settings(project, minimal_settings(diagnostics={'enabled': True}))
        self.assertIs(self.enabled(decision), True)
        # The same decision object is not mutated into a cached verdict.
        self.assertNotIn('diagnostics', decision)

    def test_invalid_settings_values_are_off(self):
        project = self.project()
        decision = self.candidate(project)
        for value in NOT_TRUE_ENABLED:
            with self.subTest(enabled=value):
                self.write_settings(project, full_settings(diagnostics={'enabled': value}))
                self.assertIs(self.enabled(decision), False)
        for value in NOT_DICT_DIAGNOSTICS:
            with self.subTest(diagnostics=value):
                self.write_settings(project, full_settings(diagnostics=value))
                self.assertIs(self.enabled(decision), False)
        for text in ('', '{', '[]', '"synthetic"', 'null', '{"diagnostics": {"enabled": true'):
            with self.subTest(raw=text):
                self.write_settings(project, text)
                self.assertIs(self.enabled(decision), False)
        self.write_settings(project, full_settings(diagnostics={'enabled': True}))
        self.assertIs(self.enabled(decision), True)

    def test_non_selected_or_malformed_decisions_are_off(self):
        project = self.project()
        self.write_settings(project, full_settings(diagnostics={'enabled': True}))
        good = self.candidate(project)
        self.assertIs(self.enabled(good), True)
        self.assertIs(self.enabled({**good, 'state': 'preview'}), True)
        for state in ('blocked', 'stale', 'reader', 'legacy', 'none', '', None, 'CANDIDATE'):
            with self.subTest(state=state):
                self.assertIs(self.enabled({**good, 'state': state}), False)
        without_state = {k: v for k, v in good.items() if k != 'state'}
        without_root = {k: v for k, v in good.items() if k != 'projectRoot'}
        for bad in (None, 'candidate', 1, [], [good], {}, without_state, without_root,
                    {**good, 'projectRoot': None}, {**good, 'projectRoot': 7},
                    {**good, 'projectRoot': ''},
                    {**good, 'projectRoot': os.path.relpath(project)},
                    {**good, 'projectRoot': str(project / 'missing-subdir')}):
            with self.subTest(decision=repr(bad)[:60]):
                self.assertIs(self.enabled(bad), False)

    def test_worktree_uses_confirmed_main_route_settings(self):
        main = self.project('main')
        linked = self.project('linked')
        decision = self.worktree(linked, self.candidate(main))
        # The worktree's own settings never grant or revoke consent.
        self.write_settings(main, full_settings(diagnostics={'enabled': False}))
        self.write_settings(linked, full_settings(diagnostics={'enabled': True}))
        self.assertIs(self.enabled(decision), False)
        self.write_settings(main, full_settings(diagnostics={'enabled': True}))
        self.write_settings(linked, full_settings(diagnostics={'enabled': False}))
        self.assertIs(self.enabled(decision), True)
        # Main checkout is re-read on every call: true -> false -> true.
        self.write_settings(main, full_settings(diagnostics={'enabled': False}))
        self.assertIs(self.enabled(decision), False)
        self.write_settings(main, full_settings(diagnostics={'enabled': True}))
        self.assertIs(self.enabled(decision), True)
        self.write_settings(main, full_settings(diagnostics={'enabled': 'true'}))
        self.assertIs(self.enabled(decision), False)

    def test_worktree_without_confirmed_main_route_is_off(self):
        main = self.project('main')
        linked = self.project('linked')
        self.write_settings(main, full_settings(diagnostics={'enabled': True}))
        self.write_settings(linked, full_settings(diagnostics={'enabled': True}))
        main_route = self.candidate(main)
        self.assertIs(self.enabled(self.worktree(linked, main_route)), True)
        bare = self.worktree(linked, main_route)
        del bare['mainRoute']
        nested = self.worktree(linked, self.worktree(main, main_route))
        for bad in (bare,
                    self.worktree(linked, None),
                    self.worktree(linked, 'candidate'),
                    self.worktree(linked, []),
                    self.worktree(linked, {}),
                    self.worktree(linked, {**main_route, 'state': 'blocked'}),
                    self.worktree(linked, {**main_route, 'state': 'stale'}),
                    self.worktree(linked, {**main_route, 'projectRoot': None}),
                    self.worktree(linked, {**main_route,
                                           'projectRoot': str(main / 'missing-subdir')}),
                    nested,
                    # A bare worktree-shaped decision that is not labelled worktree.
                    {**self.worktree(linked, main_route), 'state': 'blocked'}):
            with self.subTest(decision=repr(bad)[:80]):
                self.assertIs(self.enabled(bad), False)

    def test_stale_project_root_is_off(self):
        project = self.project()
        decision = self.candidate(project)
        settings = self.write_settings(project, full_settings(diagnostics={'enabled': True}))
        self.assertIs(self.enabled(decision), True)
        settings.unlink()
        self.assertIs(self.enabled(decision), False)
        self.write_settings(project, full_settings(diagnostics={'enabled': True}))
        self.assertIs(self.enabled(decision), True)
        shutil.rmtree(project)
        self.assertIs(self.enabled(decision), False)
        # Same for a worktree whose main checkout disappeared.
        main = self.project('main')
        linked = self.project('linked')
        self.write_settings(main, full_settings(diagnostics={'enabled': True}))
        self.write_settings(linked, full_settings(diagnostics={'enabled': True}))
        wt = self.worktree(linked, self.candidate(main))
        self.assertIs(self.enabled(wt), True)
        shutil.rmtree(main)
        self.assertIs(self.enabled(wt), False)

    def test_read_failures_and_exceptions_are_off(self):
        project = self.project()
        decision = self.candidate(project)
        settings = self.write_settings(project, full_settings(diagnostics={'enabled': True}))
        self.assertIs(self.enabled(decision), True)
        if os.geteuid() != 0:
            os.chmod(settings, 0)
            try:
                self.assertIs(self.enabled(decision), False)
            finally:
                os.chmod(settings, 0o600)
            self.assertIs(self.enabled(decision), True)
        for name in ('load_project_config', 'find_project_config', 'normalize_config'):
            for error in (RuntimeError('synthetic failure'), OSError('synthetic io'),
                          ValueError('synthetic value')):
                with self.subTest(target=name, error=type(error).__name__):
                    with patch.object(config, name, side_effect=error):
                        self.assertIs(self.enabled(decision), False)
        # Recovery after the failure: the next call re-reads and is on again.
        self.assertIs(self.enabled(decision), True)


if __name__ == '__main__':
    unittest.main(verbosity=2)

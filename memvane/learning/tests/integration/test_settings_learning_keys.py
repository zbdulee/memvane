"""Minimal settings with the learning profile blocks (decision 8 A1).

The settings file holds ``name``/``skipPaths``/``diagnostics`` plus the two
learning blocks ``contextLearning``/``sharedLearning`` in the closed shape the
learning profile writes. A malformed learning block keeps the learning
defaults and leaves ``learning_settings_invalid`` in ``settingsNotes``; any
other key still empties the whole settings.
"""
from __future__ import annotations

import json
import time
from unittest import mock

from memvane.route.types import SCHEMA_V4
from tests.support.case import FixtureCase
from tests.support.project import make_project

SCHEMA = 'qmd-local-learning-profile-v1'
ROUTE = {'schema': SCHEMA_V4}
LEARNING_KEYS = ('contextLearning', 'sharedLearning', 'settingsNotes')


def _without_learning(config):
    return {key: value for key, value in config.items() if key not in LEARNING_KEYS}


class SettingsLearningKeys(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, name='alpha', git=False, skip_paths=['drafts/'])
        self.shared = self.home / 'shared'
        self.shared.mkdir(mode=0o700)
        self.base = {'name': 'alpha', 'skipPaths': ['drafts/']}

    def write(self, value):
        self.project.settings_path.write_text(json.dumps(value), encoding='utf-8')
        self.project.settings_path.chmod(0o600)
        return self.project.settings_path.read_bytes()

    def found(self, value=None):
        from memvane.settings import api as settings
        if value is not None:
            self.write(value)
        return settings.find_project_config(str(self.project.root))

    def learning(self, *, capture=True, auto_cycle=True, enabled=True, auto_apply=False):
        return {'contextLearning': {'capture': capture, 'autoCycle': auto_cycle},
                'sharedLearning': {'enabled': enabled, 'root': str(self.shared),
                                   'profile': SCHEMA, 'autoApply': auto_apply}}

    def test_learning_keys_keep_the_rest_of_the_settings(self):
        plain = self.found(self.base)['config']
        found = self.found({**self.base, **self.learning(capture=False, auto_cycle=False)})
        config = found['config']
        self.assertEqual(found['configFormat'], 'auto-context-dir')
        self.assertEqual(config['collections'], [self.project.raw_name, self.project.wiki_name])
        self.assertEqual(config['skipPaths'], ['drafts/'])
        self.assertEqual(config['settingsModel'], 'minimal')
        self.assertEqual(_without_learning(config), _without_learning(plain))
        self.assertNotIn('settingsNotes', config)
        self.assertEqual((config['contextLearning']['capture'], config['contextLearning']['autoCycle']),
                         (False, False))
        shared = config['sharedLearning']
        self.assertEqual((shared['enabled'], shared['root'], shared['profile'], shared['autoApply']),
                         (True, str(self.shared), SCHEMA, False))

    def test_malformed_learning_block_keeps_learning_defaults_and_a_reason(self):
        plain = self.found(self.base)['config']
        good = self.learning()
        malformed = [
            {'contextLearning': True},
            {'contextLearning': {'capture': 'true'}},
            {'contextLearning': {'capture': True, 'liveSelection': True}},
            {'contextLearning': {'capture': True, 'candidateTopK': 15}},
            {'sharedLearning': {'enabled': 1}},
            {'sharedLearning': {'enabled': True, 'trialSelection': True}},
            {'sharedLearning': {'root': 'relative/shared'}},
            {'sharedLearning': {'profile': 'another-profile'}},
            {**good, 'sharedLearning': {**good['sharedLearning'], 'autoApply': None}},
        ]
        checked = 0
        for block in malformed:
            with self.subTest(block=block):
                config = self.found({**self.base, **block})['config']
                self.assertEqual(config['settingsNotes'], ['learning_settings_invalid'])
                self.assertEqual(config['contextLearning'], plain['contextLearning'])
                self.assertNotIn('sharedLearning', config)
                self.assertEqual(_without_learning(config), _without_learning(plain))
                checked += 1
        self.assertEqual(checked, len(malformed))

    def test_another_key_beside_the_learning_keys_empties_the_settings(self):
        from memvane.route import api as routes
        valid = {**self.base, **self.learning()}
        derived = routes.derived_collections(ROUTE, self.project.root,
                                             self.write(valid))
        self.assertEqual(sorted(item['name'] for item in derived),
                         [self.project.raw_name, self.project.wiki_name])
        for extra in ({'indexing': True}, {'collections': ['alpha']}, {'learning': {}}):
            with self.subTest(extra=extra):
                config = self.found({**valid, **extra})['config']
                self.assertEqual(config['collections'], [])
                self.assertNotIn('settingsModel', config)
                self.assertNotIn('sharedLearning', config)
                with self.assertRaisesRegex(ValueError, 'invalid_route_settings'):
                    routes.derived_collections(ROUTE, self.project.root,
                                               self.project.settings_path.read_bytes())

    def activate_profile(self, mode):
        """Propose and activate the local learning profile on the current settings; returns the plan."""
        from memvane.learning.context_learning import adapter, learning_profile as profile
        from memvane.learning.context_learning import runtime_authorization
        root = self.shared
        policy = {'trainerArgv': ['/fake/python', '/fake/adapter', str(root / 'model')],
                  'modelInputView': adapter.EDIT_INPUT_VIEW, '_runtime_authorization_v2': True,
                  'batchMin': 5, 'batchMax': 120}
        grant = {'projectRoot': str(self.project.root),
                 'cohort': {'id': 'fixture', 'minQualityTrainCases': 100},
                 'operations': ['local_train', 'quality_evaluate']}
        for name, row in (('shared-policy.json', policy), ('live-selector.json', {'fixture': 'live'})):
            (root / name).write_text(json.dumps(row), encoding='utf-8')
            (root / name).chmod(0o600)
        expiry = int(time.time() * 1000) + 3600000
        with mock.patch('memvane.learning.context_learning.shared._policy', return_value=policy), \
                mock.patch.object(runtime_authorization, 'require_operation', return_value=grant), \
                mock.patch.object(adapter, 'model_identity', return_value='a' * 64):
            plan = profile.proposal(self.project.root, root, choice='local-auto-apply', mode=mode,
                                    expires_at_unix_ms=expiry)
            result = profile.activate(self.project.root, root, accepted_sha256=profile.digest(plan),
                                      choice='local-auto-apply', mode=mode, expires_at_unix_ms=expiry)
        self.assertEqual(result['status'], 'learning_profile_selected')
        return plan

    def test_profile_activation_keeps_the_project_settings(self):
        from memvane.route import api as routes
        before = self.write(self.base)
        identity = routes.settings_identity_digest(
            self.project.root, routes.derived_collections(ROUTE, self.project.root, before))
        plan = self.activate_profile('new')
        self.assertEqual(plan['droppedLearningFields'], [])
        written = json.loads(self.project.settings_path.read_text(encoding='utf-8'))
        self.assertEqual(set(written), {'name', 'skipPaths', 'contextLearning', 'sharedLearning'})
        config = self.found()['config']
        self.assertEqual(config['settingsModel'], 'minimal')
        self.assertEqual(config['collections'], [self.project.raw_name, self.project.wiki_name])
        self.assertNotIn('settingsNotes', config)
        self.assertEqual((config['sharedLearning']['profile'], config['sharedLearning']['autoApply'],
                          config['sharedLearning']['root']), (SCHEMA, True, str(self.shared)))
        self.assertIs(config['contextLearning']['capture'], False)
        after = routes.derived_collections(ROUTE, self.project.root,
                                           self.project.settings_path.read_bytes())
        self.assertEqual(routes.settings_identity_digest(self.project.root, after), identity)

    def test_preserve_profile_writes_only_the_closed_learning_shape(self):
        self.write({**self.base,
                    'contextLearning': {'capture': True, 'autoCycle': 'yes', 'liveSelection': True,
                                        'candidateTopK': 15},
                    'sharedLearning': {'enabled': True, 'trialSelection': True, 'autoApply': 1}})
        plan = self.activate_profile('preserve')
        self.assertEqual(plan['droppedLearningFields'],
                         ['contextLearning.autoCycle', 'contextLearning.candidateTopK',
                          'contextLearning.liveSelection', 'sharedLearning.autoApply',
                          'sharedLearning.trialSelection'])
        written = json.loads(self.project.settings_path.read_text(encoding='utf-8'))
        self.assertEqual(written['contextLearning'], {'capture': True, 'autoCycle': True})
        self.assertEqual(written['sharedLearning'], {'enabled': True, 'root': str(self.shared),
                                                     'profile': SCHEMA, 'autoApply': True})
        config = self.found()['config']
        self.assertNotIn('settingsNotes', config)
        self.assertIs(config['sharedLearning']['enabled'], True)
        self.assertIs(config['contextLearning']['capture'], True)

    def test_setup_settings_keep_only_the_closed_learning_shape(self):
        from memvane.learning import project_learning_setup as setup
        raw = {**self.base, 'contextLearning': {'capture': False, 'stateRoot': '/elsewhere'},
               'sharedLearning': {'rerankTargetSeconds': 2, 'enabled': False}}
        settings, dropped = setup._desired_settings(raw, self.shared)
        self.assertEqual(dropped, ['contextLearning.stateRoot', 'sharedLearning.rerankTargetSeconds'])
        self.assertEqual(settings['contextLearning'], {'capture': True, 'autoCycle': True})
        self.assertEqual(settings['sharedLearning'], {'enabled': True, 'root': str(self.shared),
                                                      'profile': SCHEMA, 'autoApply': True})
        self.assertEqual(raw['contextLearning'], {'capture': False, 'stateRoot': '/elsewhere'})

    def test_shared_learning_enabled_reaches_the_shared_queue(self):
        from memvane.learning.context_learning import shared
        from memvane.learning.context_learning.runtime_setup import learning_state_base
        from memvane.learning.context_learning.seam import observe
        consent = self.shared / 'consents'
        consent.mkdir(mode=0o700)
        project = self.project.root.resolve()
        row = consent / (shared._project_key(project) + '.json')
        row.write_text(json.dumps({'schema': 'qmd-shared-consent-v1', 'projectRoot': str(project),
                                   'sharedRoot': str(self.shared.resolve()), 'capture': True,
                                   'localTraining': True, 'externalTeacher': False}), encoding='utf-8')
        row.chmod(0o600)
        parent = learning_state_base() / 'live-selection'
        parent.mkdir(parents=True)
        parent.chmod(0o700)
        self.project.write_note('docs/guide.md', 'synthetic evidence\n')
        hit = [{'file': self.project.raw_name + '/docs/guide.md'}]
        with mock.patch('memvane.learning.context_learning.corpus.snapshot', return_value=None), \
                mock.patch.object(shared, 'enqueue', return_value='queued') as enqueue, \
                mock.patch.object(shared, 'launch', return_value=None) as launch:
            states = []
            for enabled in (True, False, True):
                config = self.found({**self.base, **self.learning(enabled=enabled)})['config']
                current = shared._current_config(self.shared, project)
                states.append((current is not None, observe({'prompt': 'synthetic question'}, config,
                                                            project, hit), enqueue.call_count))
            config = self.found({**self.base, 'sharedLearning': {'enabled': True, 'mode': 'x'}})['config']
            states.append((shared._current_config(self.shared, project) is not None,
                           observe({'prompt': 'synthetic question'}, config, project, hit),
                           enqueue.call_count))
        self.assertEqual(states, [(True, 'stored', 1), (False, 'stored', 1), (True, 'stored', 2),
                                  (False, 'stored', 2)])
        self.assertEqual(launch.call_count, 2)
        self.assertEqual({call.args[1] for call in enqueue.call_args_list}, {project})


if __name__ == '__main__':
    import unittest
    unittest.main()

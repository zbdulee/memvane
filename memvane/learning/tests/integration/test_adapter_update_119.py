"""1.0.19 adapter bytes through renewal, heal, admission and recall (PLAN-1.0.19 5-4).

The previous bundle holds the exact v1.0.18 ``adapter.py`` bytes (read from the
``v1.0.18`` tag and checked against its pinned SHA-256); the running bundle
holds this checkout's adapter. Bundle content hashing, host inventory and the
journal are synthetic (``RenewalFixture``); renewal, heal ordering, rebind,
scope checks, ``inference_argv``, receipt publication (``_record``) and
``admit`` are real. ``attest`` and the ``prepare-inference`` subprocess are
replaced the way the existing heal tests replace them: the attestation names the
adapter it was asked to run, and the receipt is published by ``_record`` itself.
"""
from __future__ import annotations

from contextlib import contextmanager, redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import types
from unittest import mock

from memvane.kernel.memvane_version import version_key
from memvane.learning import default_renewal_plan as planning, project_learning_setup as setup
from memvane.learning import selector_maintenance
from memvane.learning.context_learning import (adapter, adapter_binding, inference_identity,
                                               learning_profile, live_select, project_scope,
                                               selector_heal, shared)
from memvane.learning.context_learning.inference_identity import file_sha
from memvane.learning.context_learning.local_cycle import _atomic_json
from memvane.learning.tests.renewal_fixture import RenewalFixture
from memvane.recall import verified_rows
from memvane.recall.types import RecallTrace

ROOT = Path(__file__).resolve().parents[4]
ADAPTER = 'memvane/learning/context_learning/adapter.py'
# adapter.py is unchanged from 1.0.0 through 1.0.18 (default_renewal_plan pins it for 1.0.15).
OLD_ADAPTER_SHA = 'dd3949afe71aebaa68be44b34feb080883aa28d6f5933f01bce6a5b2e763964c'
OLD_CHECKS = ['read_only_load', 'synthetic_forward', 'fixed_selection_mapping',
              'prediction_leading_body_prefix_v1']
PREVIOUS, RUNNING = '1.0.18', '1.0.19'
SCOPE = 'a' * 32
# Captured at import, before any fixture replaces the module attributes.
REAL_HEAL = selector_heal.heal
REAL_RUN = selector_maintenance._run


def _tagged(path):
    result = subprocess.run(['git', '-C', str(ROOT), 'show', 'v' + PREVIOUS + ':' + path],
                            capture_output=True, timeout=30)
    return result.stdout if result.returncode == 0 else None


def _old_adapter():
    raw = _tagged(ADAPTER)
    if raw is None or hashlib.sha256(raw).hexdigest() != OLD_ADAPTER_SHA:
        return None
    return raw


def _module(raw, name):
    namespace = {'__name__': name, '__file__': str(Path(adapter.__file__))}
    # The adapter inserts its bundle root into sys.path at import.
    with mock.patch.object(sys, 'path', list(sys.path)):
        exec(compile(raw, name + '.py', 'exec'), namespace)
    return namespace


class Recorded(RecallTrace):
    def __init__(self):
        self.events = []

    def fields(self, **fields):
        self.events.append(('fields', fields))

    def note(self, reason):
        self.events.append(('note', reason))

    def final(self, reason):
        self.events.append(('final', reason))


def _card(name, body):
    return {'path': 'wiki/' + name, 'hash': hashlib.sha256(body.encode()).hexdigest(),
            'body': body, 'generation': 'gen-1', 'scope': SCOPE}


POOL = [_card('cobalt.md', '---\ntitle: Cobalt\n---\n# Cobalt\n\nThe cobalt marker is COBALT-271.\n'),
        _card('amber.md', '---\ntitle: Amber\n---\n# Amber\n\nThe amber marker is AMBER-12.\n')]


class AdapterUpdate(RenewalFixture):
    """Default-local install: Codex 1.0.18 prepared, then the 1.0.19 bundle runs."""

    def setUp(self):
        self.old_bytes = _old_adapter()
        if self.old_bytes is None:
            self.skipTest('v1.0.18 adapter bytes unavailable in this checkout')
        self.new_bytes = Path(adapter.__file__).read_bytes()
        self.assertNotEqual(self.new_bytes, self.old_bytes)
        self.plugin = PREVIOUS
        patcher = mock.patch('memvane.kernel.memvane_version.plugin_version',
                             side_effect=lambda: self.plugin)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()
        self.previous = self.running
        self.old_anchor = self.previous / ADAPTER
        self.assertEqual(file_sha(self.old_anchor), OLD_ADAPTER_SHA)
        self.journal = {'state': 'selected', 'installId': 'fixture', 'maxPluginVersion': PREVIOUS}
        self.published = []

        def publish(decision, owner, running):
            self.assertEqual(running, version_key(self.plugin))
            self.published.append(self.plugin)
            self.journal['maxPluginVersion'] = self.plugin
        for target, kwargs in (
                ('memvane.journal.api.inspect_journal', {'side_effect': lambda *a, **k: dict(self.journal)}),
                ('memvane.journal.api.publish_plugin_version', {'side_effect': publish})):
            patcher = mock.patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        # The selector 1.0.18 attested and prepared: its adapter, its checks.
        self.attest_file(self.old_anchor)
        self.seal(checks=OLD_CHECKS)

    # Fixture hooks: the 'A' bundle of RenewalFixture.setUp is the 1.0.18 bundle.
    def select(self, host='codex', version=RUNNING, marker='B'):
        if (version, marker) == ('1.0.15', 'A'):
            version = PREVIOUS
        super().select(host, version, marker)
        self.plugin = version

    def bundle(self, host, version, marker):
        root = super().bundle(host, version, marker)
        if marker == 'A':
            (root / ADAPTER).write_bytes(self.old_bytes)
        return root

    # Synthetic replacements of the two model-running heal steps.
    def attest_file(self, adapter_path):
        _atomic_json(self.root / 'laya-synthetic-attestation.json',
                     {'schema': 'synthetic-fixture-attestation',
                      'attestation': {'adapter_sha256': file_sha(adapter_path)}})

    def view(self):
        return self.read('active-checkpoint.json').get('model_input_view', adapter.LEGACY_INPUT_VIEW)

    def argv(self):
        return self.read('live-selector.json')['trainerArgv']

    def seal(self, checks=None):
        argv = self.argv()
        runtime = Path(argv[0]).resolve()
        verified = {'adapter_sha256': file_sha(argv[1]), 'runtime_identity_sha256': '1' * 64,
                    'base_model_sha256': adapter.model_identity(argv[2])}
        with mock.patch.object(inference_identity, 'runtime_roots', return_value=[runtime]), \
                mock.patch.object(inference_identity, 'device_identity', return_value={'backend': 'cpu'}), \
                mock.patch.object(inference_identity, 'CHECKS', list(checks or inference_identity.CHECKS)):
            return inference_identity._record(self.root, argv, view=self.view(), verified=verified)

    @contextmanager
    def updating(self):
        log = types.SimpleNamespace(runs=[], renewals=[], heals=[], steps=[])
        from memvane.learning import default_renewal
        real_renew = default_renewal.renew

        def recorded(target, real):
            def call(*args, **kwargs):
                try:
                    result = real(*args, **kwargs)
                except Exception as exc:
                    target.append(('raised', str(exc)))
                    raise
                target.append(result)
                return result
            return call

        def attest(state, policy):
            log.steps.append(('attest', list(policy['trainerArgv'])))
            self.attest_file(policy['trainerArgv'][1])

        def prepare(state, policy):
            log.steps.append(('prepare', list(policy['trainerArgv'])))
            self.seal()
        with mock.patch.object(selector_maintenance, '_run', side_effect=recorded(log.runs, REAL_RUN)), \
                mock.patch.object(default_renewal, 'renew', side_effect=recorded(log.renewals, real_renew)), \
                mock.patch.object(selector_heal, 'heal', side_effect=recorded(log.heals, REAL_HEAL)), \
                mock.patch.object(selector_heal, 'attest', side_effect=attest), \
                mock.patch.object(selector_heal, 'prepare', side_effect=prepare):
            yield log

    def targets(self):
        names = (*planning.TARGETS, setup.NAME, 'laya-inference-proof.json', 'laya-synthetic-attestation.json')
        return {name: ((self.root / name).read_bytes(), (self.root / name).stat().st_ino) for name in names}

    # Client paths.
    @contextmanager
    def from_running(self):
        """``live_select`` resolves the adapter next to itself: the running bundle's."""
        with mock.patch.object(live_select, '__file__',
                               str(self.running / 'memvane/learning/context_learning/live_select.py')):
            yield

    def choose(self):
        rows = [{'id': row['path'], 'revision_sha256': row['hash'], 'source_text': row['body']}
                for row in POOL]
        served = []

        def predict(state, policy, request, artifact_sha256, **kwargs):
            served.append(list(policy['trainerArgv']))
            case = request['cases'][0]
            return {'schema_version': 1, 'predictions': [{
                'request_id': case['request_id'], 'input_sha256': case['input_sha256'],
                'selected_ids': [case['candidates'][0]['id']]}]}, {}
        with self.from_running(), \
                mock.patch('memvane.learning.context_learning.inference_service.predict', side_effect=predict):
            ids, reason, _resources = live_select.choose(
                self.root, 'Which marker is cobalt?', rows, corpus_fingerprint='0' * 64)
        return ids, reason, served

    def recall(self):
        from memvane.settings import api as settings
        config = settings.find_project_config(str(self.project))['config']
        decision = {'projectRoot': str(self.project), 'scopeId': SCOPE, 'installId': 'fixture',
                    'journalPath': self.decision['journalPath'], 'collections': []}
        trace, err = Recorded(), io.StringIO()
        with self.from_running(), \
                mock.patch('memvane.route.api.wiki_index_roots', return_value={'wiki': str(self.project / 'wiki')}), \
                redirect_stderr(err):
            chosen, reason, fallback = verified_rows._select_verified_cards(
                decision, config, 'Which marker is cobalt?', list(POOL), trace)
        return (chosen, reason, fallback), trace.events, err.getvalue()

    def assert_prepared(self, log, *, renewal='renewed'):
        self.assertEqual(log.runs, [True])
        self.assertEqual(log.renewals, [{'status': renewal}])
        self.assertEqual(log.heals, [{'status': 'selector_prepared', 'rebound': False}])
        running = str(self.running / ADAPTER)
        self.assertEqual([(step, argv[1]) for step, argv in log.steps],
                         [('attest', running), ('prepare', running)])
        argv = self.argv()
        self.assertEqual(argv[1], running)
        self.assertEqual(self.read('shared-policy.json')['trainerArgv'], argv)
        receipt = inference_identity.admit(self.root, argv, self.view())
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt['checks'], inference_identity.CHECKS)
        self.assertEqual(receipt['verified']['adapter_sha256'], hashlib.sha256(self.new_bytes).hexdigest())
        self.assertTrue(selector_heal.admitted(self.root, argv))
        self.assertEqual(selector_heal.check_scope(self.root, self.read('live-selector.json'))['trainerArgv'], argv)
        self.assertTrue(project_scope.require(self.root, shared._policy(self.root), 'local_train'))
        self.assertTrue(learning_profile.require(self.root, shared._policy(self.root), 'local_train'))

    # 5-4 (1): maintenance renews, heals and admits; authority is unchanged.
    def test_update_maintenance_renews_heals_and_keeps_authority(self):
        before = {name: self.read(name) for name in (project_scope.NAME, learning_profile.NAME,
                                                     learning_profile.INSTALL)}
        self.select()
        self.assertEqual(file_sha(self.running / ADAPTER), hashlib.sha256(self.new_bytes).hexdigest())
        with self.updating() as log:
            self.maintenance()
        self.assert_prepared(log)
        self.assertEqual(self.published, [RUNNING])
        old_grant, grant = before[project_scope.NAME]['proposal'], self.read(project_scope.NAME)['proposal']
        for key in ('operations', 'cohort', 'expiresAtUnixMs'):
            self.assertIn(key, old_grant)
            self.assertEqual(grant[key], old_grant[key], key)
        self.assertEqual({k: v for k, v in grant.items() if k != 'binding'},
                         {k: v for k, v in old_grant.items() if k != 'binding'})
        self.assertEqual(planning._without(grant['binding'], planning.BINDING_DERIVED),
                         planning._without(old_grant['binding'], planning.BINDING_DERIVED))
        (old_row,), (row,) = old_grant['binding']['installations'], grant['binding']['installations']
        self.assertEqual((old_row['version'], old_row['adapterSha256']), (PREVIOUS, OLD_ADAPTER_SHA))
        self.assertEqual((row['version'], row['adapterSha256']),
                         (RUNNING, hashlib.sha256(self.new_bytes).hexdigest()))
        old_profile, profile = before[learning_profile.NAME]['proposal'], self.read(learning_profile.NAME)['proposal']
        for key in ('choice', 'localTrain', 'autoApply', 'expiresAtUnixMs', 'modelIdentitySha256'):
            self.assertIn(key, old_profile)
            self.assertEqual(profile[key], old_profile[key], key)
        self.assertEqual(planning._without(profile, planning.PROFILE_DERIVED),
                         planning._without(old_profile, planning.PROFILE_DERIVED))
        self.assertEqual(self.read(learning_profile.INSTALL),
                         {**self.read(learning_profile.NAME), 'phase': 'activated'})
        # Files the update never rewrites keep their bytes and inode.
        self.assertEqual(self.settings_before, (self.settings.read_bytes(), self.settings.stat().st_ino))
        self.assertEqual(self.original[setup.NAME], (self.root / setup.NAME).read_bytes())
        # The client now admits the new adapter end to end.
        ids, reason, served = self.choose()
        self.assertEqual((ids, reason), (['wiki/cobalt.md'], 'selected'))
        self.assertEqual(served, [self.argv()])

    # 5-4 (2): before renewal the selector refuses the 1.0.18 anchor; recall is ranked.
    def test_before_renewal_choose_is_unsupported_and_recall_ranked(self):
        self.select()
        argv = self.argv()
        self.assertEqual(argv[1], str(self.old_anchor))
        running = self.running / ADAPTER
        with self.assertRaisesRegex(ValueError, '^unsupported_adapter$'):
            adapter_binding.inference_argv(argv, running)
        # The refusal is the byte difference: identical bytes at the same anchor pass.
        self.old_anchor.write_bytes(self.new_bytes)
        try:
            self.assertEqual(adapter_binding.inference_argv(argv, running), [argv[0], str(running), argv[2]])
        finally:
            self.old_anchor.write_bytes(self.old_bytes)
        ids, reason, served = self.choose()
        self.assertEqual((ids, reason, served), (None, 'unsupported_adapter', []))
        (chosen, reason, fallback), events, err = self.recall()
        self.assertEqual((chosen, reason, fallback), (POOL, 'ranked', 'laya_selector_unsupported_adapter'))
        self.assertIn(('note', 'laya_selector_unsupported_adapter'), events)
        self.assertIn('serving hybrid rank', err)

    # 5-4 (3): the 1.0.18 cache removed: anchor missing until maintenance.
    def test_removed_previous_cache_is_anchor_missing_until_maintenance(self):
        self.select()
        shutil.rmtree(self.previous)
        argv = self.argv()
        self.assertFalse(Path(argv[1]).exists())
        with self.assertRaisesRegex(ValueError, '^adapter_anchor_missing$'):
            adapter_binding.inference_argv(argv, self.running / ADAPTER,
                                           attested_adapter_sha256=adapter_binding.attested_adapter(self.root))
        ids, reason, _served = self.choose()
        self.assertEqual((ids, reason), (None, 'adapter_anchor_missing'))
        with self.updating() as log:
            self.maintenance()
        self.assert_prepared(log)
        ids, reason, _served = self.choose()
        self.assertEqual((ids, reason), (['wiki/cobalt.md'], 'selected'))

    # 5-4 (5): Claude 1.0.19 and Codex 1.0.18 on one install.
    def test_two_hosts_older_codex_serves_ranked_and_claude_stays_current(self):
        codex_previous = self.previous
        self.select('claude', RUNNING, 'B')
        claude_running = self.running
        self.codex['version'] = PREVIOUS  # Codex still runs its 1.0.18 cache.
        with self.updating() as log:
            self.maintenance()
        self.assert_prepared(log)
        self.assertEqual(self.read(project_scope.NAME)['proposal']['binding']['installations'][0]['host'], 'claude')
        self.assertEqual(self.journal['maxPluginVersion'], RUNNING)
        settled = self.targets()
        # Codex 1.0.18: its recall serves the hybrid rank without asking the selector.
        self.running, self.plugin = codex_previous, PREVIOUS
        shared.__file__ = str(codex_previous / 'memvane/learning/context_learning/shared.py')
        with mock.patch('memvane.learning.api.select_published', side_effect=AssertionError('selector asked')):
            (chosen, reason, fallback), events, _err = self.recall()
        self.assertEqual((chosen, reason, fallback), (POOL, 'ranked', 'laya_selector_older_plugin_version'))
        self.assertIn(('note', 'laya_selector_older_plugin_version'), events)
        # Codex 1.0.18 maintenance does not reverse Claude's update.
        with self.updating() as log:
            self.maintenance()
        self.assertEqual((log.runs, log.renewals, log.heals, log.steps), ([True], [], [], []))
        self.assertEqual(settled, self.targets())
        # Claude 1.0.19 again: nothing to renew, nothing to prepare, nothing rewritten.
        self.running, self.plugin = claude_running, RUNNING
        shared.__file__ = str(claude_running / 'memvane/learning/context_learning/shared.py')
        with self.updating() as log:
            self.maintenance()
        self.assertEqual((log.runs, log.renewals, log.heals, log.steps),
                         ([True], [{'status': 'current'}], [{'status': 'selector_ready', 'rebound': False}], []))
        self.assertEqual(settled, self.targets())

    # 5-4 (6): a receipt that differs only in its 1.0.18 pairing check is prepared again.
    def test_receipt_with_118_pairing_check_is_refused_and_prepared_again(self):
        self.assertEqual(inference_identity.CHECKS[:-1], OLD_CHECKS[:-1])
        self.assertEqual(inference_identity.CHECKS[-1], 'prediction_chunked_max_v1')
        self.select()
        with self.updating() as log:
            self.maintenance()
        self.assert_prepared(log)
        argv = self.argv()
        current = self.read('laya-inference-proof.json')
        self.seal(checks=OLD_CHECKS)
        old = self.read('laya-inference-proof.json')
        differs = {key for key in current if current[key] != old[key]}
        self.assertEqual(differs - {'createdAt'}, {'checks'})
        self.assertEqual(old['checks'][-1], 'prediction_leading_body_prefix_v1')
        with self.assertRaisesRegex(ValueError, '^inference_identity_changed$'):
            inference_identity.admit(self.root, argv, self.view())
        self.assertFalse(selector_heal.admitted(self.root, argv))
        with self.updating() as log:
            self.maintenance()
        self.assert_prepared(log, renewal='current')

    # Constants 5-2 keeps; only the pairing marker changes.
    def test_identity_constants_match_118_except_pairing(self):
        old = _module(self.old_bytes, 'adapter_1_0_18')
        for name in ('ADAPTER_VERSION', 'EXPERIMENT_ADAPTER_VERSION', 'POLICY', 'MODEL_FILES',
                     'EDIT_INPUT_VIEW', 'EDIT_INPUT_TOKENS', 'EXPERIMENT_INPUT_VIEW',
                     'EXPERIMENT_INPUT_TOKENS', 'LEGACY_INPUT_VIEW'):
            self.assertEqual(getattr(adapter, name), old[name], name)
        self.assertEqual((old['PREDICTION_PAIRING'], adapter.PREDICTION_PAIRING),
                         ('leading-body-prefix-v1', 'chunked-max-v1'))
        self.assertEqual(planning.LEGACY_RELEASE['adapterSha256'], OLD_ADAPTER_SHA)


def _safetensors_bytes(metadata):
    """The safetensors file layout: u64 LE header length, JSON header, no tensors."""
    header = json.dumps({'__metadata__': metadata}, separators=(',', ':')).encode()
    header += b' ' * (-len(header) % 8)
    return struct.pack('<Q', len(header)) + header


def _safetensors_modules():
    """Format-level stand-ins when the test interpreter has no safetensors/torch."""
    class Opened:
        def __init__(self, path, framework, device='cpu'):
            with open(path, 'rb') as source:
                size = struct.unpack('<Q', source.read(8))[0]
                self._metadata = json.loads(source.read(size)).get('__metadata__')

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def metadata(self):
            return self._metadata

    def save_file(tensors, path, metadata=None):
        if tensors:
            raise AssertionError('synthetic head has no tensors')
        Path(path).write_bytes(_safetensors_bytes(metadata))
    root, torch = types.ModuleType('safetensors'), types.ModuleType('safetensors.torch')
    root.safe_open, torch.save_file, root.torch = Opened, save_file, torch
    return {'safetensors': root, 'safetensors.torch': torch}


class HeadArtifacts(RenewalFixture):
    """Existing base manifests and 1.0.18 head metadata load with the new adapter."""

    @contextmanager
    def safetensors(self):
        try:
            import safetensors.torch  # noqa: F401
        except ImportError:
            with mock.patch.dict(sys.modules, _safetensors_modules()):
                yield 'format_stand_in'
        else:
            yield 'library'

    # 5-4 (7)
    def test_base_manifest_and_118_head_metadata_pass_head_path(self):
        old_raw = _old_adapter()
        if old_raw is None:
            self.skipTest('v1.0.18 adapter bytes unavailable in this checkout')
        old = _module(old_raw, 'adapter_1_0_18')
        identity = adapter.model_identity(self.model)
        active = self.read('active-checkpoint.json')
        base = Path(active['artifact_path'])
        self.assertEqual(json.loads(base.read_text()), {
            'schema_version': 1, 'kind': 'laya_base_head',
            'base_model_sha256': identity, 'adapter_version': adapter.ADAPTER_VERSION})

        class Model:
            def state_dict(self):
                return {}
        with self.safetensors():
            # The default enrollment's base manifest, as an older release wrote it.
            for view in (active['model_input_view'], adapter.LEGACY_INPUT_VIEW):
                self.assertIsNone(adapter._head_path(base, identity, view))
            with self.assertRaisesRegex(ValueError, '^invalid_base_manifest$'):
                adapter._head_path(base, '0' * 64, adapter.EDIT_INPUT_VIEW)
            # Heads saved by the 1.0.18 _save_head rules, for every view.
            views = (adapter.LEGACY_INPUT_VIEW, adapter.EXPERIMENT_INPUT_VIEW, adapter.EDIT_INPUT_VIEW)
            for view in views:
                path = self.home / ('head-' + view + '.safetensors')
                old['_save_head'](path, Model(), identity, view)
                self.assertEqual(adapter._head_path(path, identity, view), path)
                with self.assertRaisesRegex(ValueError, '^head_artifact_provenance_mismatch$'):
                    adapter._head_path(path, '0' * 64, view)
                for other in views:
                    if other != view:
                        with self.assertRaisesRegex(ValueError, '^head_artifact_provenance_mismatch$'):
                            adapter._head_path(path, identity, other)


if __name__ == '__main__':
    import unittest
    unittest.main()

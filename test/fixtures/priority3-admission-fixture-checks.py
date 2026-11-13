#!/usr/bin/env python3
"""Separate query admission from capacity/CRUD fixture authority.

The original fixtures mock the provider and journal, not resource admission.
The isolation tests require those non-admission fixtures to own that boundary;
they never require the product to accept an incomplete provider manifest.
Product controls force real admit_query through a synthetic Coordinator/report.
Only the sync controls search real disposable SQLite data. Capacity controls
count its existing synthetic query boundary, not a real lexical SQL search.
No journal attestation, model execution or operational service is proved here.
"""
from contextlib import ExitStack
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'core'))


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CAPACITY = load('admission_capacity_fixture', 'laya-host-capacity-checks.py')
SYNC = load('admission_sync_fixture', 'laya-product-sync-worker-checks.py')
import laya_embed_resident as owner
from memvane.host import host_response as response
import laya_install_journal as journal
from memvane.provider.adapters import local_embed_provider as provider
from context_learning import shared

REAL_ADMIT = owner.admit_query
REAL_SERVE = response.serve_selected
REAL_PROVIDER = provider.ReviewedLocalQmdProvider
REAL_OWNER_READ = owner.read_owner_state


class AdmissionFixtureChecks(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='af-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        self.home.chmod(0o700)
        self.env = patch.dict(os.environ, {
            'HOME': str(self.home), 'QMD_SETUP_GUARD_FIXTURE': '1',
            'QMD_SANDBOX': '1', 'QMD_RUNTIME_DISPATCH': '1',
            'QMD_RECALL_LOG': '', 'PYTHONDONTWRITEBYTECODE': '1'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def compose(self, cls):
        case = cls('runTest')
        self.addCleanup(case.doCleanups)
        case.setUp()
        return case

    def manifest(self):
        """Full constructor/artifact-valid metadata; files contain no model."""
        runtime = self.home / 'runtime'
        runtime.mkdir(mode=0o700)
        (runtime / 'core').mkdir(mode=0o700)
        result = {
            'approvedProviderId': 'synthetic-provider',
            'providerRelativePath': 'core/synthetic_provider.py',
            'serviceSocketPath': str(runtime / 'embed.sock'),
            'runtimeId': 'qmd-2.5.3-node-llama-cpp-3.18.1',
            'modelId': provider.DEFAULT_EMBED_MODEL_URI,
            'dimension': 2, 'recipeId': 'qmd-regex-token-chunks-candidate-v1',
            'dependencyClosureApproved': True,
            # The installer/closure attestation is explicitly synthetic.
            'dependencyClosureSha256': 'c' * 64,
        }
        for path_key, digest_key, name, raw in (
                ('providerPath', 'providerArtifactSha256', 'core/synthetic_provider.py', b'# fixture\n'),
                ('workerPath', 'workerSha256', 'worker.mjs', b'// fixture\n'),
                ('nodePath', 'nodeSha256', 'node', b'fixture executable; never run\n'),
                ('moduleEntryPath', 'moduleEntrySha256', 'module.mjs', b'// fixture entry\n'),
                ('modelPath', 'modelSha256', 'model.gguf', b'GGUFfixture; not a model\n')):
            path = runtime / name
            path.write_bytes(raw)
            path.chmod(0o600)
            result[path_key] = str(path)
            result[digest_key] = hashlib.sha256(raw).hexdigest()
        result['embeddingFingerprint'] = provider.recipe_fingerprint(
            model_id=result['modelId'], model_sha256=result['modelSha256'],
            runtime_id=result['runtimeId'], recipe_id=result['recipeId'],
            provider_sha256=result['providerArtifactSha256'], worker_sha256=result['workerSha256'],
            node_sha256=result['nodeSha256'], module_entry_sha256=result['moduleEntrySha256'],
            closure_sha256=result['dependencyClosureSha256'])
        checked = REAL_PROVIDER(result)
        checked._verify_artifacts()  # Reads only the tiny synthetic files above.
        owner._write_state(result, 'ready')
        self.assertEqual(REAL_OWNER_READ(result)['reason'], 'ready')
        return result

    @staticmethod
    def emit(evidence):
        print(json.dumps(evidence, sort_keys=True), flush=True)

    def run_boundary(self, fixture, *, deferred=False, valid=False, isolation=False):
        evidence = {'fixture': fixture, 'admission': 'deferred' if deferred else 'ready',
                    'contract': 'fixture-isolation' if isolation else 'product-control',
                    'manifestKeys': [], 'errorCode': None, 'status': None,
                    'admissionCalls': 0, 'lexicalSearchCalls': 0,
                    'ownerReadCalls': 0, 'ownerReasons': [],
                    'forbiddenAuthorityCalls': 0}
        manifest = self.manifest() if valid else None
        controller = Mock()
        controller.check.return_value = {'ready': not deferred}

        def admit():
            evidence['admissionCalls'] += 1
            try:
                return REAL_ADMIT(coordinator=controller, report={'synthetic': True})
            except Exception as exc:
                evidence['admissionError'] = str(exc)
                raise

        def read_state(observed):
            evidence['ownerReadCalls'] += 1
            state = REAL_OWNER_READ(observed)
            evidence['ownerReasons'].append(None if state is None else state['reason'])
            return state

        def forbidden(*_args, **_kwargs):
            evidence['forbiddenAuthorityCalls'] += 1
            raise AssertionError('fixture reached OS resource/service authority')

        def serve(action, host, decision, payload):
            # Enter after the composed fixture installed its provider/journal
            # authority. The product's serving/admission/wake code stays real.
            proof = journal.verify_selected_route(decision)
            original_manifest = proof['providerManifest']
            proof = {**proof, 'providerManifest': manifest,
                     'selectionEpoch': proof.get('selectionEpoch', 0)} if valid else proof
            evidence['manifestKeys'] = sorted(proof['providerManifest'])
            query = response.query_selected_v2
            original_provider = provider.ReviewedLocalQmdProvider

            def query_spy(*args, **kwargs):
                evidence['lexicalSearchCalls'] += 1
                return query(*args, **kwargs)

            def checked_provider(observed):
                self.assertEqual(observed, manifest)
                REAL_PROVIDER(observed)._verify_artifacts()
                return original_provider(original_manifest)

            with ExitStack() as stack:
                stack.enter_context(patch.object(response, 'query_selected_v2', side_effect=query_spy))
                if not isolation:
                    # Controls explicitly own admission even when the original
                    # fixture later gains its own deterministic isolation.
                    stack.enter_context(patch.object(owner, 'admit_query', side_effect=admit))
                if valid:
                    stack.enter_context(patch.object(journal, 'verify_selected_route', return_value=proof))
                    stack.enter_context(patch.object(provider, 'ReviewedLocalQmdProvider', side_effect=checked_provider))
                try:
                    return REAL_SERVE(action, host, decision, payload)
                except response.HostResponseError as exc:
                    if exc.__cause__ is not None:
                        evidence['causeType'] = type(exc.__cause__).__name__
                        evidence['causeCode'] = str(exc.__cause__)
                    raise

        with ExitStack() as stack:
            stack.enter_context(patch.object(shared, 'resource_report', side_effect=forbidden))
            stack.enter_context(patch('socket.socket', side_effect=forbidden))
            stack.enter_context(patch.object(owner, 'ensure_running', side_effect=forbidden))
            stack.enter_context(patch.object(owner, 'read_owner_state', side_effect=read_state))
            stack.enter_context(patch.object(response, '_recall_sync_wake', return_value={'status': 'queued'}))
            stack.enter_context(patch.object(owner, 'admit_query', side_effect=admit))
            stack.enter_context(patch.object(response, 'serve_selected', side_effect=serve))
            if fixture == 'capacity':
                case = self.compose(CAPACITY.ServeRuntimeTests)
                try:
                    opened, roots, db = case.serve(True)
                except response.HostResponseError as exc:
                    evidence['errorCode'] = str(exc)
                else:
                    evidence['status'] = 0
                    evidence['runtimeArgumentsValid'] = (
                        opened['source_root'] == {'wiki': roots['wiki']} and
                        opened['catalog_path'] == db + '.catalog.sqlite' and
                        opened['binding_store_path'] == db + '.bindings.sqlite')
                evidence['searchBoundary'] = 'synthetic-query-selected-v2'
            else:
                case = self.compose(SYNC.V2WorkerChecks)
                case.selected_route()
                if valid:
                    # Preserve the real route selector and DB profile binding.
                    # Only the journal/provider attestation authority is fake.
                    checked = REAL_PROVIDER(manifest)
                    case.profile.update(
                        profileId=checked.profile['profile_id'], modelId=manifest['modelId'],
                        embeddingFingerprint=manifest['embeddingFingerprint'],
                        approvedProviderId=manifest['approvedProviderId'],
                        providerArtifactSha256=manifest['providerArtifactSha256'])
                    route_path = case.home / '.auto-context' / SYNC.host_route.RECORD
                    record = json.loads(route_path.read_text(encoding='utf-8'))
                    record['vectorProfile'] = case.profile
                    route_path.write_text(json.dumps(record), encoding='utf-8')
                    case.decision = SYNC.host_route.select_route(str(case.home), action='update')
                    self.assertEqual(case.decision['state'], 'candidate', case.decision)
                source = case.alpha / 'river.md'
                source.write_text('# River\nThe otter guards the north weir.\n', encoding='utf-8')
                case.card('river-card.md', 'North weir card', 'The otter guards the north weir.', source)
                published = case.run_worker()
                self.assertEqual(published['state'], 'indexed')
                with case.hook_world():
                    status, out, err = case.recall(case.home)
                evidence.update(status=status, stderr=err.strip())
                if status:
                    prefix = '[memvane] '
                    evidence['errorCode'] = err.removeprefix(prefix).split(';', 1)[0].strip()
                else:
                    context = json.loads(out)['hookSpecificOutput']['additionalContext']
                    evidence['servedCard'] = 'title: North weir card' in context
                evidence['searchBoundary'] = 'real-query-selected-v2/disposable-SQLite'
        self.emit(evidence)
        self.assertEqual(evidence['forbiddenAuthorityCalls'], 0)
        return evidence

    def normal_control(self, fixture):
        evidence = self.run_boundary(fixture)
        self.assertIsNone(evidence['errorCode'])
        self.assertEqual(evidence['status'], 0)
        self.assertEqual(evidence['admissionCalls'], 1)
        self.assertEqual(evidence['lexicalSearchCalls'], 1)
        self.assertEqual(evidence['ownerReadCalls'], 0)
        if fixture == 'sync':
            self.assertTrue(evidence['servedCard'])
        else:
            self.assertTrue(evidence['runtimeArgumentsValid'])

    def incomplete_control(self, fixture):
        evidence = self.run_boundary(fixture, deferred=True)
        # Correct fail-closed product behavior, not the RED contract below.
        self.assertEqual(evidence['errorCode'], 'selected_provider_state_invalid')
        self.assertEqual(evidence['admissionCalls'], 1)
        self.assertEqual(evidence['lexicalSearchCalls'], 1)
        self.assertNotIn('serviceSocketPath', evidence['manifestKeys'])
        self.assertEqual(evidence['ownerReadCalls'], 1)
        self.assertEqual(evidence['admissionError'], 'laya_query_resource_deferred')
        self.assertEqual(evidence['causeType'], 'KeyError')
        self.assertIn('serviceSocketPath', evidence['causeCode'])

    def valid_control(self, fixture):
        evidence = self.run_boundary(fixture, deferred=True, valid=True)
        self.assertIsNone(evidence['errorCode'])
        self.assertEqual(evidence['status'], 0)
        self.assertEqual(evidence['admissionCalls'], 1)
        self.assertEqual(evidence['lexicalSearchCalls'], 1)
        self.assertGreaterEqual(evidence['ownerReadCalls'], 2)
        self.assertEqual(set(evidence['ownerReasons']), {'ready'})
        if fixture == 'sync':
            self.assertTrue(evidence['servedCard'])
            self.assertIn('selected_provider_resource_deferred; serving lexical results', evidence['stderr'])
        else:
            self.assertTrue(evidence['runtimeArgumentsValid'])

    def isolation_contract(self, fixture):
        for deferred in (False, True):
            with self.subTest(deferred=deferred):
                evidence = self.run_boundary(fixture, deferred=deferred, isolation=True)
                # These fixtures test runtime arguments/CRUD, not admission.
                # Their own deterministic admit_query authority must shield
                # them from either ambient pressure or a learning-lane outage.
                self.assertEqual(evidence['admissionCalls'], 0,
                                 'non-admission fixture leaked into ambient query admission')
                self.assertEqual(evidence['status'], 0)
                self.assertIsNone(evidence['errorCode'])

    def test_capacity_normal_and_incomplete_deferred_product_controls(self):
        with self.subTest(admission='ready'):
            self.normal_control('capacity')
        with self.subTest(admission='deferred'):
            self.incomplete_control('capacity')

    def test_capacity_deferred_valid_manifest_preserves_lexical_boundary(self):
        self.valid_control('capacity')

    def test_capacity_runtime_fixture_isolates_admission(self):
        self.isolation_contract('capacity')

    def test_sync_normal_and_incomplete_deferred_product_controls(self):
        with self.subTest(admission='ready'):
            self.normal_control('sync')
        with self.subTest(admission='deferred'):
            self.incomplete_control('sync')

    def test_sync_deferred_valid_manifest_serves_published_card(self):
        self.valid_control('sync')

    def test_sync_crud_fixture_isolates_admission(self):
        self.isolation_contract('sync')


if __name__ == '__main__':
    unittest.main(verbosity=2)

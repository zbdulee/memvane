"""Actual host caches, hashes, private proofs and trainer; synthetic transports."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch


def private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value)); path.chmod(0o600)


def host_checks(root, state):
    sys.path.insert(0, str(root / 'core'))
    from context_learning import live_select, local_cycle, runtime_setup, laya_adapter, inference_service
    from context_learning.contracts import digest
    adapter = root / 'core/context_learning/laya_adapter.py'
    model = state / 'model'; model.mkdir()
    for name in laya_adapter.MODEL_FILES:
        path = model / name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(('synthetic-' + name).encode())
    artifact = state / 'base.artifact'; artifact.write_bytes(b'synthetic base'); artifact.chmod(0o600)
    private(state / 'active-checkpoint.json', {'schema_version': 1,
        'artifact_path': str(artifact), 'artifact_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()})
    anchor = Path.home() / '.codex/plugins/cache/memvane-marketplace/memvane/1.0.1/core/context_learning/laya_adapter.py'
    argv = [sys.executable, str(adapter), str(model)]
    private(state / 'live-selector.json', {'schema': 'qmd-live-selector-v1',
        'trainerArgv': [sys.executable, str(anchor), str(model)], 'timeoutSeconds': 5, 'maxRssMib': 512})
    proof_path = state / 'laya-synthetic-attestation.json'
    proof = {'schema': 'qmd-laya-live-proof-v1', 'verifiedAt': time.time(),
        'attestation': {'schema_version': 1, 'runtime_identity_sha256': 'c'*64,
            'base_model_sha256': laya_adapter.model_identity(str(model)),
            'adapter_sha256': hashlib.sha256(adapter.read_bytes()).hexdigest(),
            'data_kind': 'synthetic_compact_wiki',
            'checks': {key: True for key in runtime_setup.CHECKS}}}
    private(proof_path, proof)
    source = '---\ntitle: Synthetic\nstatus: verified\n---\nSynthetic complete body.\n'
    rows = [{'id': 'wiki/synthetic.md', 'revision_sha256': digest(source), 'source_text': source}]
    phase = ['predict']; metadata = []; smokes = []; modes = []; service_calls=[]; service_requests=[]
    runtime = {'status': 'metadata_compatible', 'runtime_identity_sha256': 'c'*64, 'mps_available': False}

    def probe(*args, **kwargs):
        metadata.append('metadata')
        return dict(runtime)

    def attest(*args, **kwargs):
        assert phase[0] == 'explicit-train', 'prediction entered training attestation'
        smokes.append('synthetic-transport-only')
        return {'probe': dict(runtime), 'attestation': copy.deepcopy(proof['attestation'])}

    def transport(self, mode, request, result_path, *, cwd):
        modes.append(mode)
        if phase[0] == 'predict':
            assert mode == 'predict', 'prediction entered train/smoke'
        if mode == 'train':
            assert phase[0] == 'explicit-train'
            return {'schema_version': 1, 'status': 'trained'}, {}
        assert mode == 'predict'
        case = request['cases'][0]
        return {'schema_version': 1, 'predictions': [{'request_id': case['request_id'],
            'input_sha256': case['input_sha256'], 'selected_ids': ['wiki/synthetic.md']}]}, {'elapsed_seconds': .01}

    def shared_transport(state, policy, request, artifact_sha, **kwargs):
        service_calls.append(str(state))
        service_requests.append((copy.deepcopy(request),artifact_sha))
        return transport(None, 'predict', request, None, cwd=state)

    def snapshot():
        return {p.name: (p.read_bytes(), p.stat().st_ino, p.stat().st_mode & 0o777)
                for p in state.iterdir() if p.is_file()}

    def choose(expected='selected', *, no_probe=False):
        before = snapshot(); counts = len(modes), len(smokes), len(metadata)
        result = live_select.choose(state, 'Synthetic request', rows, corpus_fingerprint='d'*64)
        assert result[1] == expected, (result, expected)
        assert snapshot() == before, 'prediction changed proof/policy/checkpoint'
        assert len(smokes) == counts[1], 'prediction renewed training attestation'
        assert modes[counts[0]:] == (['predict'] if expected == 'selected' else [])
        if no_probe: assert len(metadata) == counts[2], 'invalid evidence should fail before metadata subprocess'
        return result

    def invoke(trainer):
        case = {'request_id': 'synthetic', 'input_sha256': 'a'*64}
        return trainer.call('predict', {'schema_version': 1, 'cases': [case]}, state/'output.json', cwd=state)

    with patch.object(runtime_setup, 'probe_runtime', side_effect=probe), patch.object(
            runtime_setup, 'attest_runtime', side_effect=attest), patch.object(
            local_cycle.LocalTrainer, 'call', transport), patch.object(
            laya_adapter, 'smoke', side_effect=AssertionError('no actual smoke')), patch.object(
            laya_adapter, 'train', side_effect=AssertionError('no actual train')), patch.object(
            inference_service, 'predict', side_effect=shared_transport, create=True):
        choose(); choose()
        assert len(service_calls)==2, 'ordinary prediction bypassed shared service'
        assert not smokes
        request, artifact_sha = service_requests[-1]
        envelope = inference_service._make_envelope(state, request, artifact_sha,
            time.monotonic()+5, 'active-checkpoint.json')
        validated=inference_service._validate_request(envelope)
        assert validated['namespace']==inference_service.namespace(state)
        assert validated['generation'][-1]==artifact_sha
        assert validated['family'][1]==proof['attestation']['base_model_sha256']
        def reject(env, expected):
            try:inference_service._validate_request(env)
            except ValueError as exc:assert str(exc)==expected,(str(exc),expected)
            else:raise AssertionError('worker accepted changed project/evidence')
        altered=copy.deepcopy(envelope);altered['namespace']='0'*64
        reject(altered,'resident_service_request_rejected')
        altered=copy.deepcopy(envelope);altered['request']['cases'][0]['prompt']='changed unbound input'
        reject(altered,'resident_service_request_rejected')
        altered=copy.deepcopy(envelope);altered['policy_sha256']='0'*64
        reject(altered,'resident_service_request_changed')
        proof_path.unlink()
        reject(envelope,'trainer_inference_attestation_required')
        private(proof_path,proof)
        # A request admitted earlier cannot execute with a newly changed pointer.
        pointer=state/'active-checkpoint.json';saved=pointer.read_bytes()
        changed=json.loads(saved);changed['artifact_sha256']='0'*64
        private(pointer,changed)
        reject(envelope,'resident_service_request_changed')
        pointer.write_bytes(saved)
        broken = copy.deepcopy(proof); broken['verifiedAt'] -= 86401
        private(proof_path, broken)
        choose('inference_attestation_required', no_probe=True)
        broken['verifiedAt'] = time.time() + 3600; private(proof_path, broken)
        choose('inference_attestation_required', no_probe=True)
        proof_path.unlink(); choose('inference_attestation_required', no_probe=True)
        private(proof_path, proof); proof_path.chmod(0o644)
        choose('inference_attestation_required', no_probe=True)
        proof_path.chmod(0o600)
        original_reader = local_cycle.read_private_json
        def denied(path):
            if Path(path) == proof_path: raise PermissionError('synthetic denied proof read')
            return original_reader(path)
        with patch.object(local_cycle, 'read_private_json', side_effect=denied):
            choose('inference_attestation_required', no_probe=True)
        proof_path.write_text('{invalid JSON')
        choose('inference_attestation_required', no_probe=True)
        private(proof_path, proof)
        model_file = model / 'model.safetensors'; original = model_file.read_bytes()
        model_file.write_bytes(original + b' changed model')
        choose('inference_attestation_required', no_probe=True)
        model_file.write_bytes(original)
        broken = copy.deepcopy(proof); broken['attestation']['adapter_sha256'] = '0'*64
        private(proof_path, broken); choose('inference_attestation_required', no_probe=True)
        private(proof_path, proof)
        runtime['runtime_identity_sha256'] = 'e'*64
        choose('inference_attestation_required')
        runtime['runtime_identity_sha256'] = 'c'*64
        runtime['status'] = 'runtime_probe_failed'
        choose('runtime_unavailable')
        runtime['status'] = 'metadata_compatible'
        for key in runtime_setup.CHECKS:
            broken = copy.deepcopy(proof); broken['attestation']['checks'][key] = False
            private(proof_path, broken); choose('inference_attestation_required')
        private(proof_path, proof)
        clock = [proof['verifiedAt'] + 86399]
        def expires_during_probe(*args, **kwargs):
            metadata.append('metadata')
            clock[0] = proof['verifiedAt'] + 86401
            return dict(runtime)
        with patch.object(local_cycle.time, 'time', side_effect=lambda: clock[0]), patch.object(
                runtime_setup, 'probe_runtime', side_effect=expires_during_probe):
            choose('inference_attestation_required')
        trainer = local_cycle.VerifiedLayaTrainer(argv, proof_cache_dir=state)
        def revoked_during_probe(*args, **kwargs):
            metadata.append('metadata')
            proof_path.unlink()
            return dict(runtime)
        previous_modes = len(modes)
        with patch.object(runtime_setup, 'probe_runtime', side_effect=revoked_during_probe):
            try: invoke(trainer)
            except ValueError as exc: assert str(exc) == 'trainer_inference_attestation_required'
            else: raise AssertionError('proof revoked during metadata probe still predicted')
        assert len(modes) == previous_modes
        private(proof_path, proof)
        trainer = local_cycle.VerifiedLayaTrainer(argv, proof_cache_dir=state)
        invoke(trainer)
        proof_path.unlink()
        try: invoke(trainer)
        except ValueError as exc: assert str(exc) == 'trainer_inference_attestation_required'
        else: raise AssertionError('cached proof revocation ignored in reused trainer')
        private(proof_path, proof)
        with patch.object(local_cycle.time, 'time', return_value=proof['verifiedAt'] + 86401):
            try: invoke(trainer)
            except ValueError as exc: assert str(exc) == 'trainer_inference_attestation_required'
            else: raise AssertionError('reused trainer ignored expiry')
        # Preserve explicit training's existing proof creation and same-object
        # prediction flow, using only synthetic transports (never ML execution).
        phase[0] = 'explicit-train'
        trainer = local_cycle.VerifiedLayaTrainer(argv)
        trainer.call('train', {}, state/'train-output.json', cwd=state)
        assert len(smokes) == 1 and modes[-1] == 'train'
        phase[0] = 'predict'
        invoke(trainer)
        with patch.object(local_cycle.time, 'time', return_value=time.time() + 86401):
            try: invoke(trainer)
            except ValueError as exc: assert str(exc) == 'trainer_inference_attestation_required'
            else: raise AssertionError('in-memory training proof never expires')
        assert len(smokes) == 1
        from context_learning import inference_identity as identity
        library_root = state/'fixture-runtime'; library_root.mkdir()
        library = library_root/'library.py'; library.write_text('version=1')
        with patch.object(identity,'runtime_roots',return_value=[library_root]), \
             patch.object(identity,'_module_scope',return_value=None), \
             patch.object(identity,'device_identity',return_value={'backend':'cpu'}):
            identity._record(state,argv,view=laya_adapter.LEGACY_INPUT_VIEW,
                verified={'runtime_identity_sha256':'c'*64,
                          'base_model_sha256':proof['attestation']['base_model_sha256'],
                          'adapter_sha256':proof['attestation']['adapter_sha256']})
            inference_service._IDENTITY_CACHE = identity.IdentityCache()
            with patch.object(runtime_setup,'probe_runtime',side_effect=AssertionError('prepared inference probed metadata')), \
                 patch.object(identity.time,'time',return_value=time.time()+172800):
                choose(no_probe=True)
                request,artifact_sha=service_requests[-1]
                env=inference_service._make_envelope(state,request,artifact_sha,time.monotonic()+4,'active-checkpoint.json')
                inference_service._validate_request(env)
                inference_service._validate_request(env)
                # A receipt prepared before the versioned prediction pairing
                # (leading body prefix) is refused, never accepted as current.
                assert identity.CHECKS[-1]=='prediction_'+laya_adapter.PREDICTION_PAIRING.replace('-','_')
                current=json.loads((state/identity.RECEIPT).read_text())
                for stale in (identity.CHECKS[:-1],['read_only_load','synthetic_forward','fixed_selection_mapping']):
                    older={**current,'checks':list(stale)}
                    private(state/identity.RECEIPT,older)
                    private(state/identity.BINDING,{'schema':identity.SCHEMA,'receipt_sha256':identity._sha(older)})
                    reject(env,'inference_identity_changed')
                    choose('inference_attestation_required',no_probe=True)
                private(state/identity.RECEIPT,current)
                private(state/identity.BINDING,{'schema':identity.SCHEMA,'receipt_sha256':identity._sha(current)})
                inference_service._validate_request(env)
                original_library=library.read_bytes();library.write_bytes(original_library+b'changed')
                reject(env,'inference_identity_changed')
                library.write_bytes(original_library)
                # A deleted binding must never downgrade to legacy metadata checks.
                (state/identity.BINDING).unlink()
                choose('inference_attestation_required',no_probe=True)
                reject(env,'inference_identity_changed')
    assert 'torch' not in sys.modules and 'laya' not in sys.modules
    return {key: True for key in ('freshReuseWithoutSmoke', 'ageInvalidates', 'missingDenied',
        'modelChangeInvalidates', 'adapterChangeInvalidates', 'runtimeChangeInvalidates',
        'unsafeProofDenied', 'permissionDenied', 'malformedDenied', 'checksPreserved',
        'cacheRevocationHonored', 'memoryProofExpires', 'explicitTrainThenPredict',
        'stateUnchanged', 'noPredictionAttestation', 'expiryDuringProbeDenied',
        'revocationDuringProbeDenied','inferenceOnlyAfterTrainingExpiry',
        'inferenceOnlyWarmNoMetadata','inferenceOnlyMutationDenied','inferenceOnlyBindingRevocation')} | {'actualModelCalls': 0, 'teacherCalls': 0}


if len(sys.argv) > 1:
    print(json.dumps(host_checks(Path(sys.argv[1]), Path(sys.argv[2]))))
else:
    os.umask(0o022)
    with tempfile.TemporaryDirectory(prefix='qmd-predict-proof-') as temporary:
        home = Path(temporary).resolve() / 'User With Spaces'; home.mkdir(mode=0o700)
        roots = {}
        for host in ('claude', 'codex'):
            root = home / ('.'+host) / 'plugins/cache/memvane-marketplace/memvane/1.0.1'
            root.mkdir(parents=True)
            shutil.copytree(Path.cwd()/'core', root/'core', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            private(root / ('.'+host+'-plugin/plugin.json'), {'name': 'memvane', 'version': '1.0.1'})
            private(root / 'package.json', {'name': 'memvane', 'version': '1.0.1'})
            roots[host] = root
        private(home/'.claude/plugins/known_marketplaces.json', {'memvane-marketplace':
            {'source': {'source': 'github', 'repo': 'zbdulee/memvane'}}})
        private(home/'.claude/plugins/installed_plugins.json', {'plugins': {
            'memvane@memvane-marketplace': [{'scope': 'user',
                'version': '1.0.1', 'installPath': str(roots['claude'])}]}})
        env = {**os.environ, 'HOME': str(home), 'CODEX_HOME': str(home/'.codex'),
               'PYTHONDONTWRITEBYTECODE': '1', 'QMD_RECALL_LOG': ''}
        reports = {}
        for host, root in roots.items():
            state = home / (host+'-state'); state.mkdir(mode=0o700)
            reports[host] = json.loads(subprocess.check_output([sys.executable,
                str(Path(__file__).resolve()), str(root), str(state)], env=env, text=True, timeout=15))
        print(json.dumps({'hosts': reports}))

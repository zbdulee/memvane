"""Synthetic-only read-only runtime discovery and selection checks."""
import json
import os
from pathlib import Path
import tempfile

from memvane.learning.context_learning.runtime_setup import (CHECKS, LAYA_PIN,
    MANAGED_DEPENDENCIES, PYTHON_PIN, choose_runtime,
    managed_install_plan, probe_runtime, verify_compatibility)


def fake_runtime(path, *, python=PYTHON_PIN, laya=LAYA_PIN, direct=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'#!/bin/sh\nexit 0\n')
    path.chmod(0o700)
    versions=dict(MANAGED_DEPENDENCIES, laya=laya)
    return lambda _: {'python':python, 'versions':versions, 'laya_import':laya,
        'torch_import':versions['torch'], 'mps_available':True,
        'laya_direct_url':direct}


def proof(probe):
    return {'schema_version':1, 'runtime_identity_sha256':probe['runtime_identity_sha256'],
        'base_model_sha256':'a'*64, 'adapter_sha256':'b'*64,
        'data_kind':'synthetic_compact_wiki', 'checks':dict.fromkeys(CHECKS, True)}


with tempfile.TemporaryDirectory(prefix='qmd-runtime-check-') as tmp:
    root=Path(tmp)
    managed=root/'managed'
    assert choose_runtime(managed_root=managed)=={'status':'managed_runtime_not_prepared',
        'mode':'managed','managed_root':str(managed)}
    assert not managed.exists()  # Discovery and plan must not install anything.
    plan=managed_install_plan(managed)
    assert plan['status']=='plan_only_no_install' and plan['changes_now']==[]
    assert not managed.exists()

    reused=root/'external'/'bin'/'python'
    runner=fake_runtime(reused)
    metadata=probe_runtime(reused, runner=runner)
    assert metadata['status']=='metadata_compatible'
    assert metadata['synthetic_training_verified'] is False
    assert choose_runtime(mode='reuse')['status']=='explicit_path_required'
    assert choose_runtime(mode='reuse',reuse_executable=reused,runner=runner)['status']=='synthetic_compatibility_required'
    attestation=proof(metadata)
    assert verify_compatibility(metadata,attestation,'a'*64)
    selected=choose_runtime(mode='reuse',reuse_executable=reused,runner=runner,
        attestation=attestation,base_model_sha256='a'*64)
    assert selected['status']=='selected' and selected['mode']=='explicit_reuse'
    for changed in ('tokenization_no_truncation','synthetic_inference',
                    'synthetic_one_step_train','checkpoint_reload','fixed_selection_mapping'):
        broken=proof(metadata)
        broken['checks'][changed]=False
        assert not verify_compatibility(metadata,broken,'a'*64)
    assert not verify_compatibility(metadata,attestation,'c'*64)
    stale=proof(metadata);stale['runtime_identity_sha256']='d'*64
    assert not verify_compatibility(metadata,stale,'a'*64)
    wrong=probe_runtime(reused,runner=fake_runtime(reused,python='3.12.13'))
    assert wrong['status']=='runtime_probe_failed'

    managed_python=managed/'generations'/'one'/'bin'/'python'
    managed_runner=fake_runtime(managed_python)
    managed.chmod(0o700)
    assert probe_runtime(reused,managed_root=managed,runner=runner)['status']=='external_interpreter_dependency'
    managed_probe=probe_runtime(managed_python,managed_root=managed,runner=managed_runner)
    assert managed_probe['status']=='metadata_compatible'
    # The pointer is a fixture only. Production preparation and proof execution
    # are deliberately absent from runtime_setup.
    pointer=managed/'active.json'
    pointer.write_text(json.dumps({'executable':str(managed_python),
        'runtime_identity_sha256':managed_probe['runtime_identity_sha256']}))
    pointer.chmod(0o600)
    assert choose_runtime(managed_root=managed,runner=managed_runner)['status']=='managed_compatibility_required'
    managed_proof=proof(managed_probe)
    assert choose_runtime(managed_root=managed,runner=managed_runner,
        attestation=managed_proof,base_model_sha256='a'*64)['status']=='selected'
    pointer.write_text(json.dumps({'executable':str(managed_python),
        'runtime_identity_sha256':'e'*64}))
    assert choose_runtime(managed_root=managed,runner=managed_runner,
        attestation=managed_proof,base_model_sha256='a'*64)['status']=='managed_pointer_changed'

print(json.dumps({'external_calls':0,'runtime_selection':'synthetic_gate_passed',
                  'install_mutations':0}))

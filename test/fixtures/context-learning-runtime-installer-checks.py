"""Synthetic generation staging, activation and rollback; no uv/network calls."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import mock

from context_learning.runtime_installer import (activate_generation,
    install_commands, rollback_generation, stage_generation)
from context_learning.runtime_setup import (MANAGED_DEPENDENCIES, choose_runtime,
    probe_runtime)
_offline_keys=('HF_HUB_OFFLINE','TRANSFORMERS_OFFLINE','HF_DATASETS_OFFLINE',
               'TOKENIZERS_PARALLELISM','USE_TF','USE_TORCH')
_before={key:os.environ.get(key) for key in _offline_keys}
from context_learning.laya_adapter import MODEL_FILES, model_identity
from context_learning_cli import execute, parser
assert {key:os.environ.get(key) for key in _offline_keys}==_before


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


with tempfile.TemporaryDirectory(prefix='qmd-installer-check-') as tmp:
    home=Path(tmp); root=home/'managed'
    uv=home/'uv';uv.write_text('synthetic uv');uv.chmod(0o700)
    lock=home/'requirements.lock'
    lock.write_text(''.join(f'{name}=={version} --hash=sha256:{"a"*64}\n'
        for name,version in MANAGED_DEPENDENCIES.items()))
    calls=[]
    uv_envs=[]
    def runner(args, **kwargs):
        calls.append(args)
        if 'env' in kwargs:
            uv_envs.append((args, kwargs['env'], kwargs['timeout']))
        if args[1:] == ['--version']:
            return SimpleNamespace(returncode=0,stdout='uv 0.12.20 (synthetic)')
        if args[1:3] == ['python','install']:
            generation=Path(args[args.index('--install-dir')+1]).parent
            python=generation/'python/cpython-3.12.14-macos-aarch64-none/bin/python3.12'
            python.parent.mkdir(parents=True);python.write_text('synthetic python');python.chmod(0o700)
        elif args[1] == 'venv':
            executable=Path(args[2])/'bin/python'
            executable.parent.mkdir(parents=True);executable.write_text('synthetic venv');executable.chmod(0o700)
        return SimpleNamespace(returncode=0,stdout='')
    def fake_runtime(_):
        return {'python':'3.12.14','versions':dict(MANAGED_DEPENDENCIES),
            'laya_import':'0.3.20','torch_import':'2.14.0',
            'mps_available':True,'laya_direct_url':None}
    def probe(executable, *, managed_root):
        return probe_runtime(executable,managed_root=managed_root,runner=fake_runtime)
    concurrency_keys=('UV_CONCURRENT_DOWNLOADS','UV_CONCURRENT_INSTALLS',
                      'UV_CONCURRENT_CACHE_READS','UV_CONCURRENT_BUILDS')
    with mock.patch.dict(os.environ,{key:'64' for key in concurrency_keys}):
        staged=stage_generation(root,uv,sha(uv),lock,sha(lock),runner=runner,probe=probe)
    assert staged['status']=='staged_inactive' and not (root/'active.json').exists()
    assert len(uv_envs)==3
    for command,env,timeout in uv_envs:
        assert all(env.get(key)=='1' for key in concurrency_keys)
        assert env['UV_NO_CONFIG']=='1' and '--no-config' in command
        assert timeout==900
    assert len(calls)==4 and calls[1][1:3]==['python','install']
    assert '--no-bin' in calls[1] and '--require-hashes' in calls[3]
    assert '--link-mode' in calls[3] and 'copy' in calls[3]
    assert install_commands(uv,staged['generation'],lock)[0]==calls[1]

    full_probe=probe
    fake_adapter=home/'adapter.py';fake_adapter.write_text('synthetic adapter')
    fake_model=home/'model';fake_model.mkdir()
    for name in MODEL_FILES:
        target=fake_model/name;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text('synthetic '+name)
    base_args=parser().parse_args(['--state-dir',str(root),'prepare-local-laya-base',
        '--model-dir',str(fake_model)])
    prepared_base=execute(base_args)
    assert prepared_base['status']=='prepared'
    assert execute(base_args)['status']=='already_prepared'
    def proof(executable, adapter, model):
        identity=full_probe(executable,managed_root=root)['runtime_identity_sha256']
        return {'probe':{'runtime_identity_sha256':identity},'attestation':{
            'schema_version':1,'runtime_identity_sha256':identity,
            'base_model_sha256':model_identity(model),'adapter_sha256':sha(adapter),
            'data_kind':'synthetic_compact_wiki','checks':{
                'tokenization_no_truncation':True,'synthetic_inference':True,
                'synthetic_one_step_train':True,'checkpoint_reload':True,
                'fixed_selection_mapping':True}}}
    first=activate_generation(root,staged['executable'],fake_adapter,fake_model,
        prover=proof,probe=full_probe)
    assert first['status']=='activated' and first['previous_available'] is False
    one=json.loads((root/'active.json').read_text())
    assert one['executable']==staged['executable']
    assert choose_runtime(managed_root=root,runner=fake_runtime)['status']=='selected'
    fake_adapter.write_text('synthetic adapter changed')
    assert choose_runtime(managed_root=root,runner=fake_runtime)['status']=='managed_source_changed'
    fake_adapter.write_text('synthetic adapter')
    two_path=root/'generations/g-second/venv/bin/python'
    two_path.parent.mkdir(parents=True);two_path.write_text('synthetic next');two_path.chmod(0o700)
    (root/'generations/g-second').chmod(0o700)
    prepared=two_path.parent.parent.parent/'prepared.json'
    prepared.write_text(json.dumps({'schema_version':1,'executable':str(two_path),
        'runtime_identity_sha256':full_probe(two_path,managed_root=root)['runtime_identity_sha256'],
        'uv_sha256':'a'*64,'lock_sha256':'b'*64}))
    prepared.chmod(0o600)
    second=activate_generation(root,two_path,fake_adapter,fake_model,prover=proof,probe=full_probe)
    assert second['previous_available'] is True
    assert json.loads((root/'active.json').read_text())['previous']['executable']==staged['executable']
    result=rollback_generation(root,fake_adapter,fake_model,prover=proof,probe=full_probe)
    assert result['status']=='rolled_back'
    rolled=json.loads((root/'active.json').read_text())
    assert rolled['executable']==staged['executable']
    assert rolled['runtime_identity_sha256']==one['runtime_identity_sha256']
    assert 'previous' not in rolled and rolled['attestation']['checks']['checkpoint_reload']
    assert all(str(root) in ' '.join(args) or args[1:] == ['--version'] for args in calls[1:])

print(json.dumps({'external_calls':0,'install_mutations_outside_temp':0,
                  'stage':'passed','activate':'passed','rollback':'passed'}))

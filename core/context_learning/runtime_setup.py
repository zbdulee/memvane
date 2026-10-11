"""Read-only Laya runtime discovery and explicit prepared-generation selection.

No download, package installation, interpreter repair or hook activation lives
here. The default is a plugin-owned runtime; a user-provided runtime is used
only after an exact synthetic adapter compatibility attestation.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlparse, unquote

from .store import canonical

PYTHON_PIN = '3.12.14'
LAYA_PIN = '0.3.20'
MANAGED_DEPENDENCIES = {'laya': LAYA_PIN, 'torch': '2.14.0',
    'transformers': '5.17.0', 'safetensors': '0.8.0',
    'huggingface-hub': '1.33.0', 'numpy': '2.5.3', 'tokenizers': '0.23.2'}
CHECKS = frozenset({'tokenization_no_truncation', 'synthetic_inference',
                    'synthetic_one_step_train', 'checkpoint_reload',
                    'fixed_selection_mapping'})
MAX_PROBE_BYTES = 16384


def _sha_file(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _sha(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def default_managed_root(home=None):
    home = Path(home) if home is not None else Path.home()
    if sys.platform == 'darwin':
        return home/'Library'/'Application Support'/'qmd-auto-context'/'runtimes'/'laya'
    return home/'.local'/'share'/'qmd-auto-context'/'runtimes'/'laya'


_PROBE = '''import importlib.metadata as m,json,sys
names=('laya','torch','transformers','safetensors','huggingface-hub','numpy','tokenizers')
versions={name:m.version(name) for name in names}
direct=m.distribution('laya').read_text('direct_url.json')
import laya,torch
print(json.dumps({'python':sys.version.split()[0],'versions':versions,
 'laya_import':laya.__version__,'torch_import':torch.__version__,
 'mps_available':bool(torch.backends.mps.is_available()),
 'laya_direct_url':json.loads(direct) if direct else None}))'''


def _run_probe(executable, *, temp_dir=None):
    env={k:os.environ[k] for k in ('PATH','LANG','TMPDIR') if k in os.environ}
    if temp_dir is not None:
        env['TMPDIR']=str(temp_dir)
    env.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',PYTHONDONTWRITEBYTECODE='1',QMD_SANDBOX='1')
    with tempfile.TemporaryDirectory(prefix='qmd-runtime-probe-', dir=temp_dir) as cwd:
        result=subprocess.run([str(executable),'-I','-c',_PROBE],cwd=cwd,env=env,
            stdin=subprocess.DEVNULL,capture_output=True,timeout=30,check=False)
    if result.returncode or len(result.stdout)>MAX_PROBE_BYTES or len(result.stderr)>MAX_PROBE_BYTES:
        raise ValueError('runtime_probe_failed')
    return json.loads(result.stdout)


def _editable_identity(direct):
    if not isinstance(direct,dict) or direct.get('dir_info',{}).get('editable') is not True:
        return None
    url=direct.get('url')
    parsed=urlparse(url) if isinstance(url,str) else None
    if not parsed or parsed.scheme!='file' or parsed.netloc not in ('','localhost'):
        raise ValueError('invalid_editable_origin')
    root=Path(unquote(parsed.path))
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise ValueError('missing_editable_origin')
    commit=subprocess.run(['git','-C',str(root),'rev-parse','HEAD'],capture_output=True,
        text=True,timeout=5,check=False)
    dirty=subprocess.run(['git','-C',str(root),'status','--porcelain'],capture_output=True,
        text=True,timeout=5,check=False)
    if commit.returncode or dirty.returncode or dirty.stdout.strip():
        raise ValueError('editable_origin_dirty_or_unknown')
    return {'path':str(root.resolve()),'commit':commit.stdout.strip()}


def probe_runtime(executable, *, managed_root=None, runner=None, temp_dir=None):
    path=Path(executable)
    if not path.is_absolute(): return {'status':'invalid_executable_path'}
    resolved=path.resolve()
    if not resolved.is_file() or not os.access(resolved,os.X_OK):
        return {'status':'missing_interpreter','executable':str(path)}
    info=resolved.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0,os.getuid()):
        return {'status':'unsafe_interpreter','executable':str(path)}
    if managed_root is not None:
        root=Path(managed_root).resolve()
        if root not in resolved.parents:
            return {'status':'external_interpreter_dependency','executable':str(path)}
    try:
        raw=runner(path) if runner is not None else _run_probe(path, temp_dir=temp_dir)
        if not isinstance(raw,dict) or set(raw)!={'python','versions','laya_import','torch_import','mps_available','laya_direct_url'}:
            raise ValueError('invalid_probe_output')
        versions=raw['versions']
        if not isinstance(versions,dict) or any(not isinstance(versions.get(k),str) for k in MANAGED_DEPENDENCIES):
            raise ValueError('invalid_probe_versions')
        editable=_editable_identity(raw['laya_direct_url'])
        if managed_root is not None and editable is not None:
            raise ValueError('managed_runtime_must_not_be_editable')
        if raw['python'] != PYTHON_PIN or versions['laya'] != LAYA_PIN or raw['laya_import'] != LAYA_PIN or raw['torch_import'] != versions['torch']:
            raise ValueError('runtime_version_mismatch')
        if managed_root is not None and any(versions[k]!=v for k,v in MANAGED_DEPENDENCIES.items()):
            raise ValueError('managed_dependency_mismatch')
        if type(raw['mps_available']) is not bool:
            raise ValueError('invalid_device_report')
        identity={'executable_sha256':_sha_file(resolved),'python':raw['python'],
            'versions':versions,'editable_origin':editable}
        return {'status':'metadata_compatible','executable':str(path),'resolved_executable':str(resolved),
            'runtime_identity_sha256':_sha(identity),'metadata':identity,
            'mps_available':raw['mps_available'], 'synthetic_training_verified':False}
    except (ValueError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {'status':'runtime_probe_failed','executable':str(path)}


def verify_compatibility(probe, attestation, base_model_sha256):
    if not isinstance(probe,dict) or probe.get('status')!='metadata_compatible':
        return False
    if not isinstance(attestation,dict) or set(attestation)!={'schema_version','runtime_identity_sha256',
            'base_model_sha256','adapter_sha256','data_kind','checks'}:
        return False
    sha=lambda x:isinstance(x,str) and bool(re.fullmatch(r'[0-9a-f]{64}',x))
    return (attestation['schema_version']==1 and attestation['data_kind']=='synthetic_compact_wiki' and
        sha(base_model_sha256) and sha(attestation['adapter_sha256']) and
        attestation['runtime_identity_sha256']==probe['runtime_identity_sha256'] and
        attestation['base_model_sha256']==base_model_sha256 and
        isinstance(attestation['checks'],dict) and set(attestation['checks'])==CHECKS and
        all(value is True for value in attestation['checks'].values()))


def attest_runtime(executable, adapter_path, model_dir, *, max_seconds=120,
                   max_rss_mib=8192, temp_dir=None):
    """Run the real local synthetic Laya smoke before explicit runtime reuse.

    This prepares no runtime and writes only disposable owner-only temp files.
    The adapter and full local model bundle are hashed before and after.
    """
    from .laya_adapter import model_identity
    from .local_cycle import LocalTrainer

    probe = (probe_runtime(executable, temp_dir=temp_dir) if temp_dir is not None
             else probe_runtime(executable))
    if probe['status'] != 'metadata_compatible':
        raise ValueError('runtime_metadata_incompatible')
    adapter = Path(adapter_path)
    if not adapter.is_absolute() or not adapter.is_file() or adapter.is_symlink():
        raise ValueError('invalid_adapter_path')
    adapter_sha = _sha_file(adapter)
    model_sha = model_identity(model_dir)
    trainer = LocalTrainer([str(executable),str(adapter),str(model_dir)],
        max_seconds=max_seconds,max_rss_mib=max_rss_mib,scratch_dir=temp_dir)
    with tempfile.TemporaryDirectory(prefix='qmd-laya-compat-', dir=temp_dir) as tmp:
        root = Path(tmp)
        os.chmod(root,0o700)
        result, resources = trainer.call('smoke',{'schema_version':1},
            root/'smoke-result.json',cwd=root)
    if (_sha_file(adapter)!=adapter_sha or model_identity(model_dir)!=model_sha):
        raise ValueError('compatibility_inputs_changed')
    if (not isinstance(result,dict) or result.get('schema_version')!=1 or
        result.get('base_model_sha256')!=model_sha or
        not isinstance(result.get('checks'),dict) or set(result['checks'])!=CHECKS or
        any(value is not True for value in result['checks'].values()) or
        result.get('device')!=('mps' if probe['mps_available'] else 'cpu') or
        not isinstance(result.get('head_bytes'),int) or result['head_bytes']<=0 or
        not isinstance(result.get('reloaded_logits'),list) or len(result['reloaded_logits'])!=2):
        raise ValueError('invalid_synthetic_smoke_result')
    attestation = {'schema_version':1,'runtime_identity_sha256':probe['runtime_identity_sha256'],
        'base_model_sha256':model_sha,'adapter_sha256':adapter_sha,
        'data_kind':'synthetic_compact_wiki','checks':result['checks']}
    if not verify_compatibility(probe,attestation,model_sha):
        raise ValueError('synthetic_compatibility_failed')
    return {'probe':probe,'attestation':attestation,'smoke':result,'resources':resources}


def choose_runtime(*, mode='managed', managed_root=None, reuse_executable=None,
                   attestation=None, base_model_sha256=None, runner=None):
    root=Path(managed_root) if managed_root is not None else default_managed_root()
    if mode=='reuse':
        if reuse_executable is None: return {'status':'explicit_path_required','mode':mode}
        probe=probe_runtime(reuse_executable,runner=runner)
        if probe['status']!='metadata_compatible': return {'status':'reuse_probe_failed','probe':probe}
        if not verify_compatibility(probe,attestation,base_model_sha256):
            return {'status':'synthetic_compatibility_required','probe':probe}
        return {'status':'selected','mode':'explicit_reuse','executable':probe['executable'],
                'runtime_identity_sha256':probe['runtime_identity_sha256']}
    if mode!='managed': raise ValueError('invalid_runtime_mode')
    if root.exists() or root.is_symlink():
        info=root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o077:
            return {'status':'unsafe_managed_root','mode':mode,'managed_root':str(root)}
    pointer=root/'active.json'
    if not pointer.is_file() or pointer.is_symlink():
        return {'status':'managed_runtime_not_prepared','mode':mode,'managed_root':str(root)}
    from .dataset import read_private_json
    active=read_private_json(pointer)
    legacy={'executable','runtime_identity_sha256'}
    recorded=legacy|{'attestation','adapter_path','model_dir'}
    if not isinstance(active,dict) or set(active) not in (
        legacy,legacy|{'previous'},recorded,recorded|{'previous'}):
        return {'status':'invalid_managed_pointer'}
    path=active.get('executable') if isinstance(active,dict) else None
    if not isinstance(path,str): return {'status':'invalid_managed_pointer'}
    probe=probe_runtime(path,managed_root=root,runner=runner)
    if attestation is None and 'attestation' in active:
        from .laya_adapter import model_identity
        adapter=Path(active['adapter_path']) if isinstance(active['adapter_path'],str) else None
        model=active['model_dir']
        if (adapter is None or not adapter.is_absolute() or not adapter.is_file() or
            adapter.is_symlink() or not isinstance(model,str) or not Path(model).is_absolute()):
            return {'status':'managed_source_changed'}
        try:
            if (_sha_file(adapter)!=active['attestation']['adapter_sha256'] or
                model_identity(model)!=active['attestation']['base_model_sha256']):
                return {'status':'managed_source_changed'}
        except (ValueError,OSError,KeyError,TypeError):
            return {'status':'managed_source_changed'}
        attestation=active['attestation']
        base_model_sha256=attestation['base_model_sha256']
    if not verify_compatibility(probe,attestation,base_model_sha256):
        return {'status':'managed_compatibility_required','probe':probe}
    if active.get('runtime_identity_sha256')!=probe['runtime_identity_sha256']:
        return {'status':'managed_pointer_changed'}
    return {'status':'selected','mode':'managed','executable':path,
            'runtime_identity_sha256':probe['runtime_identity_sha256']}


def managed_install_plan(root=None):
    root=Path(root) if root is not None else default_managed_root()
    return {'status':'plan_only_no_install','default_mode':'managed',
        'root':str(root),'python':PYTHON_PIN,'package_versions':dict(MANAGED_DEPENDENCIES),
        'python_distribution':'Astral uv python-build-standalone, staged under plugin-owned root',
        'laya_distribution':'PyPI laya==0.3.20, isolated wheel install',
        'interpreter_rule':'resolved interpreter must remain inside immutable generation',
        'checkpoint_rule':'reuse verified base cache read-only; write trained heads under private project state',
        'activation':'synthetic tokenizer/inference/one-step-train/reload/selection proof before pointer change',
        'changes_now':[]}

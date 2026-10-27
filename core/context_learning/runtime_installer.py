"""Explicit, isolated managed Laya generations; no hook or project activation.

Importing this module is read-only. stage_generation is the only download and
package-install entry point and must be called explicitly with a hash lock.
"""
import fcntl
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import tempfile
import uuid

from .dataset import read_private_json
from .runtime_setup import (MANAGED_DEPENDENCIES, PYTHON_PIN, _sha_file,
    attest_runtime, probe_runtime, verify_compatibility)
from .store import canonical

UV_PIN = '0.12.20'
PYPI_INDEX = 'https://pypi.org/simple'
# Fixed product-owned limits; caller UV_CONCURRENT_* values are not inherited.
UV_CONCURRENCY = {'UV_CONCURRENT_DOWNLOADS': '1', 'UV_CONCURRENT_INSTALLS': '1',
                  'UV_CONCURRENT_CACHE_READS': '1', 'UV_CONCURRENT_BUILDS': '1'}
SHA_RE = re.compile(r'[0-9a-f]{64}')
REQ_RE = re.compile(r'([A-Za-z0-9_.-]+)==([A-Za-z0-9_.!+]+)(?:\s+--hash=sha256:([0-9a-f]{64}))+')


def _sha_file(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda:source.read(1024*1024),b''):
            h.update(block)
    return h.hexdigest()


def _private_directory(path, *, create=False):
    path=Path(path)
    if not path.is_absolute() or path.is_symlink(): raise ValueError('unsafe_managed_root')
    if create: path.mkdir(mode=0o700,parents=True,exist_ok=True)
    if not path.is_dir(): raise ValueError('missing_managed_root')
    info=path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o077:
        raise ValueError('unsafe_managed_root')
    return path


def _locked_requirements(path, expected_sha):
    path=Path(path)
    if not path.is_absolute() or not path.is_file() or path.is_symlink() or not SHA_RE.fullmatch(expected_sha):
        raise ValueError('invalid_lock_file')
    if path.stat().st_size > 1024*1024 or _sha_file(path)!=expected_sha:
        raise ValueError('lock_hash_mismatch')
    text=path.read_text(encoding='utf8')
    logical=[];current=''
    for line in text.splitlines():
        line=line.split('#',1)[0].strip()
        if not line:continue
        current+=' '+line.rstrip('\\').strip()
        if line.endswith('\\'):continue
        logical.append(current.strip());current=''
    if current:raise ValueError('invalid_lock_format')
    versions={}
    for row in logical:
        match=REQ_RE.fullmatch(row)
        if not match: raise ValueError('unhashed_or_nonpypi_requirement')
        name=match.group(1).lower().replace('_','-').replace('.','-')
        if name in versions:raise ValueError('duplicate_requirement')
        versions[name]=match.group(2)
    if any(versions.get(name)!=pin for name,pin in MANAGED_DEPENDENCIES.items()):
        raise ValueError('managed_dependency_lock_mismatch')
    return path


def _uv_binary(path, expected_sha, *, runner=subprocess.run):
    path=Path(path)
    if not path.is_absolute() or not path.is_file() or path.is_symlink() or not os.access(path,os.X_OK):
        raise ValueError('invalid_uv_binary')
    if not SHA_RE.fullmatch(expected_sha) or _sha_file(path)!=expected_sha:
        raise ValueError('uv_binary_hash_mismatch')
    result=runner([str(path),'--version'],capture_output=True,text=True,
        timeout=10,check=False)
    if result.returncode or not result.stdout.startswith('uv '+UV_PIN+' '):
        raise ValueError('uv_version_mismatch')
    return path


def install_commands(uv, generation, lock, python_executable=None):
    """Reviewable uv command plan; no shell and no global Python bin links."""
    uv=str(uv); generation=Path(generation); lock=str(lock)
    commands=[[uv,'python','install',PYTHON_PIN,'--install-dir',
               str(generation/'python'),'--no-bin','--no-cache','--no-config']]
    if python_executable is not None:
        commands.extend([
            [uv,'venv',str(generation/'venv'),'--python',str(python_executable),
             '--no-project','--no-python-downloads','--no-config'],
            [uv,'pip','sync',lock,'--python',str(generation/'venv/bin/python'),
             '--require-hashes','--no-build','--link-mode','copy',
             '--default-index',PYPI_INDEX,'--no-python-downloads','--no-config']])
    return commands


def _run(command, *, cwd, runner):
    env={k:os.environ[k] for k in ('PATH','LANG','TMPDIR') if k in os.environ}
    env.update(UV_NO_CONFIG='1',UV_CACHE_DIR=str(cwd/'cache'),
        UV_PYTHON_INSTALL_DIR=str(cwd/'python'),UV_INDEX_URL=PYPI_INDEX,
        PYTHONDONTWRITEBYTECODE='1',**UV_CONCURRENCY)
    result=runner(command,cwd=cwd,env=env,capture_output=True,
        text=True,timeout=900,check=False)
    if result.returncode:raise ValueError('managed_install_step_failed')


def stage_generation(root, uv_path, uv_sha256, lock_path, lock_sha256,
                     *, runner=subprocess.run, probe=probe_runtime):
    """Install a new inactive generation from uv and a fully hashed PyPI lock.

    This function has network/install effects only when explicitly invoked.
    It never edits an existing generation or the active runtime pointer.
    """
    root=_private_directory(root,create=True)
    lock=_locked_requirements(lock_path,lock_sha256)
    uv=_uv_binary(uv_path,uv_sha256,runner=runner)
    generations=_private_directory(root/'generations',create=True)
    generation=generations/('g-'+uuid.uuid4().hex)
    generation.mkdir(mode=0o700)
    _run(install_commands(uv,generation,lock)[0],cwd=generation,runner=runner)
    found=list((generation/'python').glob('cpython-3.12.14-*/bin/python3.12'))
    if len(found)!=1 or not found[0].is_file() or generation.resolve() not in found[0].resolve().parents:
        raise ValueError('managed_python_not_self_contained')
    for command in install_commands(uv,generation,lock,found[0])[1:]:
        _run(command,cwd=generation,runner=runner)
    executable=generation/'venv/bin/python'
    inspected=probe(executable,managed_root=root)
    if inspected.get('status')!='metadata_compatible':
        raise ValueError('managed_generation_probe_failed')
    _write_pointer(generation/'prepared.json',{'schema_version':1,
        'executable':str(executable),
        'runtime_identity_sha256':inspected['runtime_identity_sha256'],
        'uv_sha256':uv_sha256,'lock_sha256':lock_sha256})
    return {'status':'staged_inactive','generation':str(generation),
        'executable':str(executable),'runtime_identity_sha256':inspected['runtime_identity_sha256'],
        'uv_sha256':uv_sha256,'lock_sha256':lock_sha256}


def _write_pointer(path, payload):
    fd,temp=tempfile.mkstemp(prefix='.active-',dir=path.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w',encoding='utf8') as output:
            output.write(canonical(payload)+'\n');output.flush();os.fsync(output.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)


def _generation_executable(root, executable):
    root=_private_directory(root)
    generations=_private_directory(root/'generations')
    path=Path(executable)
    if not path.is_absolute() or path.parent.name!='bin' or path.name!='python':
        raise ValueError('invalid_generation_executable')
    if path.parent.parent.name!='venv':
        raise ValueError('invalid_generation_executable')
    generation=path.parent.parent.parent
    if generation.parent!=generations or generation.is_symlink() or not generation.name.startswith('g-'):
        raise ValueError('invalid_generation_executable')
    _private_directory(generation)
    if not path.is_file() or generation.resolve() not in path.resolve().parents:
        raise ValueError('external_interpreter_dependency')
    prepared=read_private_json(generation/'prepared.json')
    if (not isinstance(prepared,dict) or set(prepared)!={'schema_version','executable',
        'runtime_identity_sha256','uv_sha256','lock_sha256'} or
        prepared['schema_version']!=1 or prepared['executable']!=str(path) or
        not all(isinstance(prepared[k],str) and SHA_RE.fullmatch(prepared[k])
            for k in ('runtime_identity_sha256','uv_sha256','lock_sha256'))):
        raise ValueError('generation_not_prepared')
    return path


@contextmanager
def _activation_lock(root):
    path=root/'activation.lock'
    fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o077:
            raise ValueError('unsafe_activation_lock')
        fcntl.flock(fd,fcntl.LOCK_EX)
        yield
    finally:os.close(fd)


def activate_generation(root, executable, adapter_path, model_dir,
                        *, prover=attest_runtime, probe=probe_runtime, plugin_version=None):
    """Select a fully checked prepared generation; preserve one-step rollback."""
    root=_private_directory(root)
    executable=_generation_executable(root,executable)
    if not Path(adapter_path).is_absolute() or not Path(model_dir).is_absolute():
        raise ValueError('invalid_compatibility_source_path')
    with _activation_lock(root):
        inspected=probe(executable,managed_root=root)
        prepared=read_private_json(executable.parent.parent.parent/'prepared.json')
        proof=prover(str(executable),str(adapter_path),str(model_dir))
        attestation=proof['attestation']
        if (inspected.get('status')!='metadata_compatible' or
            inspected['runtime_identity_sha256']!=proof['probe']['runtime_identity_sha256'] or
            inspected['runtime_identity_sha256']!=prepared['runtime_identity_sha256'] or
            not verify_compatibility(inspected,attestation,attestation['base_model_sha256'])):
            raise ValueError('managed_compatibility_failed')
        active=root/'active.json'
        previous=read_private_json(active) if active.is_file() else None
        if previous is not None and (not isinstance(previous,dict) or
            not {'executable','runtime_identity_sha256'}<=set(previous)):
            raise ValueError('invalid_managed_pointer')
        old={k:v for k,v in previous.items() if k!='previous'} if previous else None
        pointer={'executable':str(executable),
            'runtime_identity_sha256':inspected['runtime_identity_sha256'],
            'attestation':attestation,'adapter_path':str(adapter_path),
            'model_dir':str(model_dir),'previous':old}
        _record_owner(root,attestation['adapter_sha256'],plugin_version or _installed_plugin_version())
        _write_pointer(active,pointer)
        return {'status':'activated','executable':str(executable),
                'runtime_identity_sha256':inspected['runtime_identity_sha256'],
                'previous_available':old is not None}


# Sidecar, not a pointer key: older hosts validate active.json's exact key set.
ADAPTER_OWNER='adapter-owner.json'


def _version_key(value):
    """SemVer precedence: a prerelease (0.35.2-dev.1) sorts before 0.35.2."""
    match=(re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?',value)
           if isinstance(value,str) else None)
    if not match:
        return None
    core=tuple(int(x) for x in match.groups()[:3])
    if match.group(4) is None:
        return core+(1,())
    ids=tuple((0,int(x),'') if x.isdigit() else (1,0,x) for x in match.group(4).split('.'))
    return core+(0,ids)


def _installed_plugin_version():
    try:
        return json.loads((Path(__file__).resolve().parents[2]/'package.json').read_text())['version']
    except (OSError,ValueError,KeyError,TypeError):
        return None


def _record_owner(root, adapter_sha, plugin_version):
    """Written before the pointer: bound to the adapter hash, a stale sidecar is ignored."""
    if _version_key(plugin_version) is None:
        return
    _write_pointer(Path(root)/ADAPTER_OWNER,{'schema':'qmd-laya-adapter-owner-v1',
        'version':plugin_version,'adapter_sha256':adapter_sha})


def adapter_owner_version(root, pointer):
    """Plugin version that last proved the pointer's current adapter, if recorded."""
    path=Path(root)/ADAPTER_OWNER
    if not path.is_file() or path.is_symlink():
        return None
    try:
        owner=read_private_json(path)
    except (OSError,ValueError):
        return None
    if (not isinstance(owner,dict) or owner.get('adapter_sha256')!=
            (pointer.get('attestation') or {}).get('adapter_sha256')):
        return None  # Another writer proved a different adapter since.
    return owner.get('version')


def adapter_refresh_marker(marker_dir, pointer, adapter_sha):
    """One failed smoke per adapter bytes and exact selected runtime/model."""
    attestation=pointer.get('attestation') or {}
    key='\0'.join((adapter_sha,pointer['runtime_identity_sha256'],pointer['executable'],
                   str(pointer['model_dir']),str(attestation.get('base_model_sha256'))))
    return Path(marker_dir)/('laya-adapter-refresh-'+hashlib.sha256(key.encode()).hexdigest()[:32]+'.failed.json')


def _record_refresh_failure(marker, reason):
    try:
        fd=os.open(marker,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
    except FileExistsError:
        return
    with os.fdopen(fd,'w',encoding='utf8') as out:
        json.dump({'schema':'qmd-laya-adapter-refresh-v1','reason':reason,'at':time.time()},out,sort_keys=True)


def reattest_active(root, adapter_path, *, prover=attest_runtime, probe=probe_runtime,
                    marker_dir=None, plugin_version=None):
    """Re-attest the selected generation after only the plugin adapter changed.

    Executable, runtime identity, model and one-step rollback stay unchanged.
    The failure marker is read and written under the activation lock for the
    exact pointer that was checked; an older host never reverts a newer proof.
    """
    root=_private_directory(root)
    adapter_path=Path(adapter_path)
    if not adapter_path.is_absolute() or adapter_path.is_symlink() or not adapter_path.is_file():
        raise ValueError('invalid_compatibility_source_path')
    with _activation_lock(root):
        active=root/'active.json'
        pointer=read_private_json(active)
        if (not isinstance(pointer,dict) or not {'executable','runtime_identity_sha256',
            'attestation','model_dir'}<=set(pointer) or not isinstance(pointer['attestation'],dict)):
            raise ValueError('invalid_managed_pointer')
        old=pointer['attestation']
        adapter_sha=_sha_file(adapter_path)
        marker=adapter_refresh_marker(marker_dir,pointer,adapter_sha) if marker_dir is not None else None
        if old.get('adapter_sha256')==adapter_sha:
            # This combination is proved: its old failure record no longer applies.
            if marker is not None:
                marker.unlink(missing_ok=True)
            attested=Path(pointer.get('adapter_path') or '')
            if (pointer.get('adapter_path')==str(adapter_path) or
                (attested.is_absolute() and not attested.is_symlink() and attested.is_file()
                 and _sha_file(attested)==adapter_sha)):
                return {'status':'current','executable':pointer['executable']}
            # Same attested bytes at a new plugin path: the proof still holds.
            pointer['adapter_path']=str(adapter_path)
            _write_pointer(active,pointer)
            return {'status':'repointed','executable':pointer['executable']}
        plugin_version=plugin_version or _installed_plugin_version()
        owner=adapter_owner_version(root,pointer)
        recorded,current=_version_key(owner),_version_key(plugin_version)
        if recorded is not None and current is not None and current<recorded:
            return {'status':'older_host','executable':pointer['executable'],
                    'attestedVersion':owner}
        if marker is not None and (marker.exists() or marker.is_symlink()):
            return {'status':'failed_previously','executable':pointer['executable']}
        from resource_deferred import ResourceDeferred
        try:
            executable=_generation_executable(root,pointer['executable'])
            inspected=probe(executable,managed_root=root)
            if (inspected.get('status')!='metadata_compatible' or
                inspected['runtime_identity_sha256']!=pointer['runtime_identity_sha256']):
                raise ValueError('managed_runtime_changed')
            proof=prover(str(executable),str(adapter_path),str(pointer['model_dir']))
            attestation=proof['attestation']
            if (proof['probe']['runtime_identity_sha256']!=pointer['runtime_identity_sha256'] or
                attestation['base_model_sha256']!=old.get('base_model_sha256') or
                attestation['adapter_sha256']!=adapter_sha or
                not verify_compatibility(inspected,attestation,attestation['base_model_sha256'])):
                raise ValueError('managed_compatibility_failed')
        except ResourceDeferred:
            raise
        except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as exc:
            if marker is not None:
                _record_refresh_failure(marker,str(exc) if isinstance(exc,ValueError) else type(exc).__name__)
            raise
        _record_owner(root,adapter_sha,plugin_version)
        pointer.update(attestation=attestation,adapter_path=str(adapter_path))
        _write_pointer(active,pointer)
        return {'status':'reattested','executable':str(executable)}

def rollback_generation(root, adapter_path, model_dir,
                        *, prover=attest_runtime, probe=probe_runtime, plugin_version=None):
    root=_private_directory(root)
    with _activation_lock(root):
        active=root/'active.json'
        pointer=read_private_json(active)
        previous=pointer.get('previous') if isinstance(pointer,dict) else None
        if not isinstance(previous,dict) or not {'executable','runtime_identity_sha256'}<=set(previous):
            raise ValueError('no_previous_generation')
        executable=_generation_executable(root,previous['executable'])
        inspected=probe(executable,managed_root=root)
        prepared=read_private_json(executable.parent.parent.parent/'prepared.json')
        proof=prover(str(executable),str(adapter_path),str(model_dir))
        attestation=proof['attestation']
        if (inspected.get('status')!='metadata_compatible' or
            inspected['runtime_identity_sha256']!=previous['runtime_identity_sha256'] or
            inspected['runtime_identity_sha256']!=prepared['runtime_identity_sha256'] or
            inspected['runtime_identity_sha256']!=proof['probe']['runtime_identity_sha256'] or
            not verify_compatibility(inspected,attestation,attestation['base_model_sha256'])):
            raise ValueError('previous_generation_incompatible')
        restored={k:v for k,v in previous.items() if k!='previous'}
        restored.update(attestation=attestation,adapter_path=str(adapter_path),model_dir=str(model_dir))
        _record_owner(root,attestation['adapter_sha256'],plugin_version or _installed_plugin_version())
        _write_pointer(active,restored)
        return {'status':'rolled_back','executable':str(executable)}

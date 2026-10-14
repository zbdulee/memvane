#!/usr/bin/env python3
"""Pinned, project-neutral QMD runtime selection; no DB or model mutation.

An existing dedicated npm prefix may be adopted without copying it. The
managed wrapper pins its Node and QMD paths; each selection rechecks hashes and
capabilities. Project INDEX_PATH/QMD_CONFIG_DIR remain separate per project.
"""
from __future__ import annotations

import hashlib
import fcntl
import install_deadline
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import uuid

VERSION='2.5.3'
SCHEMA='qmd-managed-runtime-v1'
SUPPORTED_NODE_MAJORS=frozenset((22,24,26))
CAPABILITIES=('qmd vsearch','qmd search','qmd get','qmd collection add',
              'qmd update','qmd embed')


def supported_node_version(value):
    match=re.fullmatch(r'v(\d+)\.\d+\.\d+',value.strip())
    return bool(match and int(match.group(1)) in SUPPORTED_NODE_MAJORS)


def managed_root(home=None):
    return Path(home or Path.home())/'Library'/'Application Support'/'qmd-auto-context'/'runtimes'/'qmd'


def _sha(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):
            value.update(chunk)
    return value.hexdigest()


def _private(path, *, create=False):
    path=Path(path)
    if not path.is_absolute() or path.is_symlink():raise ValueError('unsafe_qmd_runtime_root')
    if create:path.mkdir(mode=0o700,parents=True,exist_ok=True)
    info=path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o077:
        raise ValueError('unsafe_qmd_runtime_root')
    return path


def _probe(qmd, node):
    qmd=Path(qmd).resolve();node=Path(node).resolve()
    if not qmd.is_file() or not node.is_file() or not os.access(node,os.X_OK):
        raise ValueError('qmd_runtime_missing')
    for path in (qmd,node):
        info=path.stat()
        if info.st_uid not in (0,os.getuid()) or info.st_mode & 0o022:
            raise ValueError('unsafe_qmd_executable')
    version=subprocess.run([str(node),str(qmd),'--version'],capture_output=True,
        text=True,timeout=10,check=False)
    node_version=subprocess.run([str(node),'--version'],capture_output=True,
        text=True,timeout=10,check=False)
    help_result=subprocess.run([str(node),str(qmd),'--help'],capture_output=True,
        text=True,timeout=10,check=False)
    if (version.returncode or version.stdout.strip()!='qmd '+VERSION or
            node_version.returncode or not supported_node_version(node_version.stdout) or
            help_result.returncode or
            any(capability not in help_result.stdout for capability in CAPABILITIES)):
        raise ValueError('qmd_runtime_incompatible')
    package=qmd.parent.parent/'package.json'
    if not package.is_file() or json.loads(package.read_text()).get('version')!=VERSION:
        raise ValueError('qmd_package_identity_changed')
    return {'qmd':str(qmd),'node':str(node),'qmdSha256':_sha(qmd),
            'nodeSha256':_sha(node),'packageSha256':_sha(package),
            'version':VERSION,'nodeVersion':node_version.stdout.strip(),
            'capabilities':list(CAPABILITIES)}


def probe_cli(binary):
    """Compatibility gate for an explicitly selected existing QMD command."""
    path=Path(binary)
    if not path.is_absolute() or not path.is_file() or not os.access(path,os.X_OK):
        raise ValueError('qmd_runtime_missing')
    version=subprocess.run([str(path),'--version'],capture_output=True,text=True,
                           timeout=10,check=False)
    help_result=subprocess.run([str(path),'--help'],capture_output=True,text=True,
                               timeout=10,check=False)
    if (version.returncode or version.stdout.strip()!='qmd '+VERSION or
            help_result.returncode or any(capability not in help_result.stdout
                                          for capability in CAPABILITIES)):
        raise ValueError('qmd_runtime_incompatible')
    return {'version':VERSION,'capabilities':list(CAPABILITIES),'binarySha256':_sha(path)}


def _write(path,payload):
    fd,temporary=tempfile.mkstemp(prefix='.qmd-pointer-',dir=path.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w') as out:
            json.dump(payload,out,sort_keys=True,separators=(',',':'))
            out.write('\n');out.flush();os.fsync(out.fileno())
        os.replace(temporary,path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def prepare_existing(root,qmd,node, *, force_cpu=False, native_no_build=False):
    root=_private(root,create=True)
    generations=_private(root/'generations',create=True)
    proof=_probe(qmd,node)
    cli=Path(proof['qmd']).parent.parent/'dist/cli/qmd.js'
    if native_no_build and (force_cpu or not cli.is_file() or cli.is_symlink() or
            not (cli.parent.parent/'llm.js').is_file()):
        raise ValueError('qmd_no_build_contract_missing')
    generation=generations/('g-'+uuid.uuid4().hex)
    generation.mkdir(mode=0o700)
    wrapper=generation/'qmd'
    entry=Path(proof['qmd'])
    if force_cpu:
        # The package launcher spawns a child and drops inherited lease FDs.
        # Direct CLI exec keeps the bootstrap lease on the actual writer.
        for source in (cli,cli.parent.parent/'llm.js'):
            info=source.lstat()
            if (source.is_symlink() or not stat.S_ISREG(info.st_mode) or
                    info.st_uid not in (0,os.getuid()) or info.st_mode&0o022):
                raise ValueError('qmd_direct_cli_contract_missing')
        entry=cli
    if native_no_build:
        entry=generation/'qmd-native.mjs'
        policy=Path(__file__).with_name('qmd_native_policy.mjs').read_text()
        entry.write_text(policy.replace('__QMD_CLI_JSON__',json.dumps(str(cli))))
        entry.chmod(0o600)
    # Resolved paths originate from the checked local installation; no shell
    # input or project text is interpolated into the command line.
    import shlex
    wrapper.write_text('#!/bin/sh\n'+('export QMD_FORCE_CPU=1\n' if force_cpu else '')+'exec '+shlex.quote(proof['node'])+' '+
                       shlex.quote(str(entry))+' "$@"\n')
    wrapper.chmod(0o700)
    prepared={'schema':SCHEMA,'proof':proof,'wrapper':str(wrapper),'wrapperSha256':_sha(wrapper)}
    if force_cpu:prepared.update(forceCpu=True,directCli=True,entry=str(entry),entrySha256=_sha(entry),
        llmSha256=_sha(cli.parent.parent/'llm.js'),cliSha256=_sha(cli))
    if native_no_build:prepared.update(nativeNoBuild=True,entry=str(entry),entrySha256=_sha(entry),
        llmSha256=_sha(cli.parent.parent/'llm.js'),cliSha256=_sha(cli))
    _write(generation/'prepared.json',prepared)
    return {'status':'prepared_inactive','generation':str(generation),
            'wrapper':str(wrapper),'proof':proof}


def _prepared(generation):
    generation=_private(generation)
    path=generation/'prepared.json'
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError('qmd_preparation_missing')
    value=json.loads(path.read_text())
    if not isinstance(value,dict) or value.get('schema')!=SCHEMA:
        raise ValueError('qmd_preparation_invalid')
    proof=value['proof'];wrapper=Path(value['wrapper'])
    if value.get('nativeNoBuild'):
        entry=Path(value['entry'])
        cli=Path(proof['qmd']).parent.parent/'dist/cli/qmd.js'
        if (entry.parent!=generation or entry.is_symlink() or not entry.is_file() or
                entry.stat().st_uid!=os.getuid() or entry.stat().st_mode&0o077 or
                _sha(entry)!=value['entrySha256']):
            raise ValueError('qmd_native_policy_changed')
        if _sha(cli)!=value['cliSha256'] or _sha(cli.parent.parent/'llm.js')!=value['llmSha256']:
            raise ValueError('qmd_native_contract_changed')
    if value.get('directCli'):
        cli=Path(proof['qmd']).parent.parent/'dist/cli/qmd.js'
        if not value.get('forceCpu') or Path(value['entry'])!=cli:raise ValueError('qmd_direct_cli_contract_changed')
        for source in (cli,cli.parent.parent/'llm.js'):
            st=source.lstat()
            if source.is_symlink() or not stat.S_ISREG(st.st_mode) or st.st_uid not in (0,os.getuid()) or st.st_mode&0o022:
                raise ValueError('qmd_direct_cli_contract_changed')
        if _sha(cli)!=value['entrySha256'] or _sha(cli)!=value['cliSha256'] or _sha(cli.parent.parent/'llm.js')!=value['llmSha256']:
            raise ValueError('qmd_direct_cli_contract_changed')
    if (wrapper.parent!=generation or wrapper.is_symlink() or
            _sha(wrapper)!=value['wrapperSha256'] or
            _probe(proof['qmd'],proof['node'])!=proof):
        raise ValueError('qmd_runtime_changed')
    return value


def activate(root,generation, *, require_unselected=False):
    root=_private(root)
    generation=Path(generation)
    if generation.parent!=root/'generations':raise ValueError('qmd_generation_outside_root')
    prepared=_prepared(generation)
    lock=root/'activation.lock'
    fd=os.open(lock,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(fd)
        if info.st_uid!=os.getuid() or info.st_mode & 0o077:
            raise ValueError('unsafe_qmd_activation_lock')
        install_deadline.flock(fd,fcntl.LOCK_EX)
        path=root/'active.json'
        if path.is_symlink():raise ValueError('unsafe_qmd_pointer')
        if require_unselected and path.exists():raise ValueError('qmd_managed_already_active')
        previous=json.loads(path.read_text()) if path.is_file() else None
        _write(path,{'schema':SCHEMA,'generation':str(generation),
                     'previous':previous.get('generation') if isinstance(previous,dict) else None})
    finally:
        os.close(fd)
    return {'status':'selected_managed','wrapper':prepared['wrapper']}


def select(root=None):
    root=_private(root or managed_root())
    path=root/'active.json'
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError('qmd_managed_not_active')
    value=json.loads(path.read_text())
    if not isinstance(value,dict) or value.get('schema')!=SCHEMA:
        raise ValueError('qmd_managed_pointer_invalid')
    generation=Path(value['generation'])
    if generation.parent!=root/'generations':raise ValueError('qmd_generation_outside_root')
    prepared=_prepared(generation)
    return {'status':'selected_managed','wrapper':prepared['wrapper'],
            'proof':prepared['proof'],'generation':str(generation),
            'forceCpu':prepared.get('forceCpu') is True,
            'nativeNoBuild':prepared.get('nativeNoBuild') is True,
            'entry':prepared.get('entry')}


def deselect_if_matches(root, expected_sha256):
    """Undo only our new selection, serialized with all runtime activations."""
    root=_private(root)
    fd=os.open(root/'activation.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(fd)
        if info.st_uid!=os.getuid() or info.st_mode & 0o077 or info.st_nlink!=1:
            raise ValueError('unsafe_qmd_activation_lock')
        install_deadline.flock(fd,fcntl.LOCK_EX)
        path=root/'active.json'
        if path.is_symlink() or not path.is_file() or _sha(path)!=expected_sha256:
            return False
        path.unlink()
        directory=os.open(root,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
        return True
    finally:os.close(fd)


def rollback(root=None):
    root=_private(root or managed_root())
    path=root/'active.json'
    if path.is_symlink() or not path.is_file():raise ValueError('qmd_managed_not_active')
    current=json.loads(path.read_text())
    prior=current.get('previous') if isinstance(current,dict) else None
    if not isinstance(prior,str):raise ValueError('qmd_rollback_unavailable')
    generation=Path(prior)
    if generation.parent!=root/'generations':raise ValueError('qmd_generation_outside_root')
    _prepared(generation)
    _write(path,{'schema':SCHEMA,'generation':prior,'previous':current['generation']})
    return select(root)


if __name__=='__main__':
    if len(sys.argv)!=2 or sys.argv[1]!='resolve':raise SystemExit(2)
    try:print(select()['wrapper'])
    except (OSError,ValueError,KeyError,TypeError,json.JSONDecodeError):raise SystemExit(1)

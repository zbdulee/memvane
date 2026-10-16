"""Fixture-only OS account-home provider; never load from product entry points.

HOME alone deliberately cannot move the product's UID-global coordination root.
These selected integration fixtures supply a private account-home boundary too.
EUID, owners, process identity, resource verdicts and kernel locks stay real.
An optional fixture-only observation bootstrap reaches sanitized children too;
it supplies OS observations without replacing admission or process identity.
Isolated (-I) or sanitized model children must be forbidden by the caller.
"""
import os
import json
from pathlib import Path
import pwd
import runpy
import subprocess
import sys
import textwrap


def isolate(home, *, observation_bootstrap=''):
    home=Path(home).resolve(strict=True)
    if home==Path(pwd.getpwuid(os.getuid()).pw_dir).resolve():
        raise ValueError('fixture_requires_private_home')
    directory=home/'.uid-home-fixture';directory.mkdir(mode=0o700)
    receipts=directory/'receipts';receipts.mkdir(mode=0o700)
    # Match shared.launch's sanitized environment, including the exact Python.
    clean={key:os.environ[key] for key in ('PATH','LANG','TMPDIR','HOME','USER') if key in os.environ}
    clean['HOME']=str(home)
    probe=subprocess.run([sys.executable,'-B','-c',
        'import site,sys,json;print(json.dumps([site.ENABLE_USER_SITE,site.getusersitepackages(),sys.flags.isolated,sys.flags.no_user_site]))'],
        env=clean,capture_output=True,text=True,check=True,timeout=5)
    enabled,user_site,isolated,no_user_site=json.loads(probe.stdout)
    user_site=Path(user_site).resolve()
    if enabled is not True or isolated or no_user_site or not user_site.is_relative_to(home):
        raise ValueError('fixture_private_user_site_unavailable')
    user_site.mkdir(mode=0o700,parents=True,exist_ok=True)
    source='''import os,pwd,sys,json
_fixture_original_lookup=pwd.getpwuid
_fixture_home='''+repr(str(home))+'''
def _fixture_lookup(uid):
    row=_fixture_original_lookup(uid)
    return pwd.struct_passwd((*row[:5],_fixture_home,row.pw_shell)) if uid==os.getuid() else row
pwd.getpwuid=_fixture_lookup
pwd._qmd_fixture_home=_fixture_home
assert pwd.getpwuid(os.getuid()).pw_dir==_fixture_home
assert not sys.flags.isolated and not sys.flags.no_user_site
def _fixture_audit(event,args):
    if event=='import' and args[0].split('.')[0] in ('torch','transformers','laya'):
        raise AssertionError('model import forbidden in isolated fixture')
    if event=='subprocess.Popen':
        argv=[str(item) for item in args[1]]
        if (any(item in ('-I','-s') for item in argv)
            or any(item.endswith(('laya_adapter.py','laya_inference_worker.py')) for item in argv)
            or any('context_learning.inference_service' in item or 'context_learning.resident' in item for item in argv)):
            raise AssertionError('model or isolated service child forbidden in fixture')
sys.addaudithook(_fixture_audit)
_fixture_receipt='''+repr(str(receipts))+'''+os.sep+str(os.getpid())+'.json'
_fixture_fd=os.open(_fixture_receipt,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
with os.fdopen(_fixture_fd,'w') as _fixture_stream:
    json.dump({'pid':os.getpid(),'ppid':os.getppid(),'uid':os.getuid(),
        'argv0':sys.argv[0],'home':_fixture_home},_fixture_stream)
'''
    source+=observation_bootstrap
    source=('import pwd\nif getattr(pwd,"_qmd_fixture_home",None)!='+repr(str(home))+':\n'
            +textwrap.indent(source,'    '))
    path=directory/'sitecustomize.py'
    # Homebrew supplies an earlier stdlib sitecustomize. usercustomize still
    # runs from the private user site; the marker prevents double installation.
    for target in (path,user_site/'sitecustomize.py',user_site/'usercustomize.py'):
        if target.exists() or target.is_symlink():raise ValueError('fixture_bootstrap_already_exists')
        target.write_text(source);target.chmod(0o600)
    previous_lookup=pwd.getpwuid;previous_path=os.environ.get('PYTHONPATH')
    previous_marker=getattr(pwd,'_qmd_fixture_home',None)
    def restore():
        pwd.getpwuid=previous_lookup
        if previous_marker is None:
            if hasattr(pwd,'_qmd_fixture_home'):del pwd._qmd_fixture_home
        else:pwd._qmd_fixture_home=previous_marker
        if previous_path is None:os.environ.pop('PYTHONPATH',None)
        else:os.environ['PYTHONPATH']=previous_path
    try:
        os.environ['PYTHONPATH']=str(directory)+(os.pathsep+previous_path if previous_path else '')
        runpy.run_path(str(path))
        # Prove sanitized startup, including a durable child receipt.
        checked=subprocess.run([sys.executable,'-B','-c',
            'import os,pwd,json;print(json.dumps([os.getpid(),pwd.getpwuid(os.getuid()).pw_dir]))'],
            env=clean,capture_output=True,text=True,check=True,timeout=5)
        child_pid,child_home=json.loads(checked.stdout)
        receipt=json.loads((receipts/(str(child_pid)+'.json')).read_text())
        if child_home!=str(home) or receipt['home']!=str(home) or receipt['uid']!=os.getuid():
            raise ValueError('fixture_sanitized_startup_not_isolated')
    except BaseException:
        restore();raise
    return restore

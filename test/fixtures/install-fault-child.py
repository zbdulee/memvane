"""Abrupt fixture controller; all paths/CLI inputs belong to disposable HOME."""
import json,os,sys,time,subprocess
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,'core')
import setup_entry,qmd_endpoint as E,shared_qmd_setup as B,shared_install as I
request=json.loads(Path(sys.argv[1]).read_text());mode=request['mode']
save_b=B._save;save_i=I._save;run=B.subprocess.run
def bootstrap_save(base,row):
 save_b(base,row)
 if mode=='embed_checkpoint' and 'embed' in row.get('building',{}).get('done',[]):os._exit(77)
def install_save(base,row):
 save_i(base,row)
 if mode=='settings_copy' and row.get('settingsCopied'):os._exit(77)
 if mode=='repair_commit' and row.get('vectorRepair',{}).get('phase')=='metadata_invalidated':os._exit(77)
def native_run(argv,*a,**kw):
 if mode=='live_embed' and argv[1:2]==['embed']:
  options={k:v for k,v in kw.items() if k in ('env','cwd','pass_fds')}
  with (Path(os.environ['HOME'])/'orphan-native.log').open('wb') as log:
   subprocess.Popen(argv,**options,stdout=log,stderr=log,stdin=subprocess.DEVNULL)
   until=time.monotonic()+5
   while not (Path(os.environ['HOME'])/'embed-alive').exists() and time.monotonic()<until:time.sleep(.02)
   if not (Path(os.environ['HOME'])/'embed-alive').exists():raise RuntimeError('synthetic_native_start_failed')
   os._exit(77)
 result=run(argv,*a,**kw)
 if mode=='remove_before_checkpoint' and argv[1:3]==['collection','remove'] and result.returncode==0:os._exit(77)
 return result
with patch.object(E,'inspection_available'),patch.object(E,'ensure',return_value={'pid':12345,'url':'http://127.0.0.1:19484'}),patch.object(E,'read',return_value={'pid':12345,'port':19484,'url':'http://127.0.0.1:19484'}),patch.object(E,'healthy',return_value=True),patch.object(B.coordinator,'_daemon_identity',return_value=None),patch.object(E,'assert_quiescent'),patch('qmd_write_fence.alive',return_value=True),patch.object(B,'_save',side_effect=bootstrap_save),patch.object(I,'_save',side_effect=install_save),patch.object(B.subprocess,'run',side_effect=native_run):
 raise SystemExit(setup_entry.main(['install','--project',request['project'],*request['flags']]))

"""Recovery serializes with ordinary project settings writers before its CAS."""
import json,tempfile,threading
from pathlib import Path
from unittest.mock import patch
import settings_lock
import project_learning_setup as setup
from context_learning.local_cycle import _atomic_json
from context_learning.runtime_setup import default_managed_root
from context_learning.shared import _project_key


def save(path,value):
 path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
 _atomic_json(path,value)

with tempfile.TemporaryDirectory(prefix='qmd-recovery-lock-',dir='/tmp') as folder:
 base=Path(folder).resolve();home=base/'home';home.mkdir(mode=0o700)
 project=base/'project';project.mkdir(mode=0o700)
 root=default_managed_root(home).parent.parent/'live-selection'/_project_key(project)
 root.mkdir(mode=0o700,parents=True)
 settings=project/'.auto-context/settings.json';old={'capture':False};current={'capture':True}
 save(settings,current)
 backup=root/setup.BACKUPS/'test';backup.mkdir(mode=0o700,parents=True)
 save(backup/'settings.json',old)
 save(backup/'manifest.json',{'acceptedSha256':'accepted','files':{'settings.json':setup.file_sha(backup/'settings.json')}})
 save(root/setup.NAME,{'schema':setup.SCHEMA,'phase':'activated',
  'plan':{'projectRoot':str(project),'namespaceRoot':str(root)},'backupRoot':str(backup),
  'acceptedSha256':'accepted','sourceSettingsSha256':setup.file_sha(backup/'settings.json'),
  'targetSettingsSha256':setup.file_sha(settings),'written':{},
  'configurationPaths':list(setup.CONFIG_NAMES)+['consents/'+_project_key(project)+'.json']})
 started=threading.Event();finished=threading.Event();outcome=[]
 def recovery():
  started.set()
  try:outcome.append(setup.recover(project,root))
  except BaseException as exc:outcome.append(exc)
  finally:finished.set()
 with patch.object(Path,'home',return_value=home):
  with settings_lock.locked(project):
   thread=threading.Thread(target=recovery,daemon=True);thread.start()
   assert started.wait(1),'recovery did not start'
   assert not finished.wait(.2),'recovery bypassed the project settings writer lock'
   save(settings,{'capture':True,'concurrentWriter':'preserve me'})
  assert finished.wait(5),'recovery did not finish after releasing writer lock'
  thread.join()
 assert len(outcome)==1 and isinstance(outcome[0],ValueError) and 'settings_changed' in str(outcome[0]),outcome
 assert json.loads(settings.read_text())=={'capture':True,'concurrentWriter':'preserve me'}
 assert json.loads((root/setup.NAME).read_text())['phase']=='activated'
print('project recovery lock checks passed')

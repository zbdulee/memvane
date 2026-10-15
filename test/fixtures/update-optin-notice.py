"""Real resolve-only contract; detached maintenance is not executed."""
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch
import io,json,os,sys,tempfile

repo=Path(__file__).resolve().parents[2];sys.path.insert(0,str(repo/'core'))
import update_hook_queue as Q
real_popen=Q.subprocess.Popen
def maintenance_only_stub(args,**kwargs):
    if len(args)>2 and args[2]=='worker':return None
    return real_popen(args,**kwargs)
with tempfile.TemporaryDirectory(prefix='qmd-notice-',dir=repo) as temporary:
    home=Path(temporary);project=home/'project';(project/'docs').mkdir(parents=True)
    (project/'.auto-context').mkdir();settings=project/'.auto-context/settings.json'
    env={k:v for k,v in os.environ.items() if not k.startswith('QMD_') and k not in
         ('INDEX_PATH','XDG_CACHE_HOME','XDG_CONFIG_HOME','XDG_DATA_HOME')}
    env.update(HOME=str(home),TMPDIR=str(home),QMD_SETUP_GUARD_FIXTURE='1',QMD_RECALL_LOG='',
        QMD_UPDATE_HOOK_QUEUE_DIR=str(home/'queue'),QMD_CACHE_DIR=str(home/'cache'),QMD_ENGINE='codex',
        PYTHONDONTWRITEBYTECODE='1')
    os.environ.clear();os.environ.update(env)
    def notice(cwd,cfg):
        settings.write_text(json.dumps(cfg));output=io.StringIO()
        with patch.object(Q,'_setup_notice'),patch.object(Q.subprocess,'Popen',side_effect=maintenance_only_stub),redirect_stdout(output):
            Q.enqueue(json.dumps({'cwd':str(cwd),'hook_event_name':'SessionStart'}))
        return '검색 등록 여부가 아직 결정되지 않았습니다' in output.getvalue()
    configured={'indexing':True,'collections':['synthetic-docs'],'collectionPaths':{'synthetic-docs':'docs'}}
    assert not notice(project,configured),'configured project falsely prompted to opt in'
    assert not notice(project/'docs',configured),'nested cwd lost configured parent identity'
    assert notice(project,{'indexing':True,'collections':[]}), 'genuinely pending project needs the notice'
    assert not notice(project,{'indexing':False,'collections':[]}), 'opted-out project must not be prompted'
    print(json.dumps({'configured':True,'nested':True,'pending':True,'optout':True,'externalCalls':0}))

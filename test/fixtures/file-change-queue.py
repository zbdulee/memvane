"""Synthetic SQLite + real hook dispatcher/queue/worker protocol. No host/model CLI."""
from pathlib import Path
import hashlib,json,os,sqlite3,subprocess,sys,tempfile,time
from unittest.mock import patch

REPO=Path(__file__).resolve().parents[2]
assert (REPO/'core/file_change_queue.py').is_file(), 'missing deferred Bash filesystem change detector'
scenario=sys.argv[1]
with tempfile.TemporaryDirectory(prefix='qmd-file-changes-',dir=REPO) as temporary:
    home=Path(temporary).resolve();project=home/'project';docs=project/'docs';docs.mkdir(parents=True)
    auto=project/'.auto-context';auto.mkdir();outside=home/'outside';outside.mkdir()
    a=docs/'a.md';a.write_text('Synthetic alpha.\n');(outside/'secret.md').write_text('Synthetic outside.\n')
    settings={'indexing':True,'collections':['fixture-docs','fixture-outside'],
        'collectionPaths':{'fixture-docs':'docs','fixture-outside':str(outside)},
        'allowRoots':[str(home)],'compile':{'mode':'off'}}
    (auto/'settings.json').write_text(json.dumps(settings))
    index=project/'private/index.sqlite';index.parent.mkdir()
    with sqlite3.connect(index) as db:
        db.execute('CREATE TABLE documents(collection TEXT,path TEXT,hash TEXT,active INTEGER,PRIMARY KEY(collection,path))')
        db.execute('INSERT INTO documents VALUES(?,?,?,1)',('fixture-docs','a.md',hashlib.sha256(a.read_bytes()).hexdigest()))
        db.execute('INSERT INTO documents VALUES(?,?,?,1)',('fixture-outside','secret.md','outside-hash'))
    manager=home/'manager.sh';manager.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$HOME/manager.log"\n');manager.chmod(0o700)
    update=home/'update.sh';update.write_text('#!/bin/sh\nexit 0\n');update.chmod(0o700)
    fake=home/'qmd';fake.write_text('''#!/usr/bin/env python3
from pathlib import Path
import hashlib,os,sqlite3,sys
home=Path(os.environ['HOME']).resolve();index=Path(os.environ['INDEX_PATH']).resolve()
assert home in index.parents
if sys.argv[1]=='update':
    docs=home/'project/docs'
    with sqlite3.connect(index) as db:
        db.execute("UPDATE documents SET active=0 WHERE collection='fixture-docs'")
        for p in docs.glob('*.md'):
            db.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,1)',('fixture-docs',p.name,hashlib.sha256(p.read_bytes()).hexdigest()))
elif sys.argv[1]=='embed': print('Embedded 1 chunks from 1 documents in 0s')
''');fake.chmod(0o700)
    env={k:v for k,v in os.environ.items() if not k.startswith('QMD_') and k not in
        ('INDEX_PATH','XDG_CACHE_HOME','XDG_CONFIG_HOME','XDG_DATA_HOME','CLAUDE_PLUGIN_ROOT','PLUGIN_ROOT','CLAUDE_HEADLESS','CLAUDE_SANDBOX','CODEX_SANDBOX')}
    env.update(HOME=str(home),TMPDIR=str(home),QMD_SETUP_GUARD_FIXTURE='1',QMD_RECALL_LOG='',
        QMD_DIRTY_QUEUE=str(home/'dirty-queue'),QMD_CACHE_DIR=str(home/'cache'),
        QMD_BACKEND_MANAGER=str(manager),QMD_CORE_UPDATE_SCRIPT=str(update),QMD_SUPPRESS_NOTICE='1',
        QMD_FAKE_QMD=str(fake),QMD_BIN=str(fake),QMD_NO_RELOAD='1',QMD_LOCK_BASE=str(home/'locks'),
        INDEX_PATH=str(index),QMD_CONFIG_DIR=str(project/'private/config'),XDG_CACHE_HOME=str(project/'private/cache'),
        PYTHONDONTWRITEBYTECODE='1',QMD_ENGINE='claude')
    os.environ.clear();os.environ.update(env);sys.path.insert(0,str(REPO/'core'))
    import file_change_queue as Q
    payload={'cwd':str(project),'hook_event_name':'PostToolUse','tool_name':'Bash',
        'tool_input':{'command':'command string deliberately never interpreted'},
        'tool_response':{'stdout':'','stderr':'','interrupted':False}}
    def invoke(action,data):
        p=subprocess.run(['bash',str(REPO/'hooks/run-hook'),action,'claude'],input=json.dumps(data),
            text=True,capture_output=True,env=env,cwd=project,timeout=8)
        assert p.returncode==0,(p.stdout,p.stderr)
        return p.stdout
    def lines():
        p=home/'dirty-queue';return p.read_text().splitlines() if p.exists() else []
    def drain():
        p=subprocess.run(['bash',str(REPO/'backend/index_worker.sh')],env=env,cwd=project,
            text=True,capture_output=True,timeout=15)
        assert p.returncode==0,(p.stdout,p.stderr,(home/'cache/index-worker.log').read_text() if (home/'cache/index-worker.log').exists() else '')
    def active(name):
        with sqlite3.connect(index) as db:
            r=db.execute("SELECT active FROM documents WHERE collection='fixture-docs' AND path=?",(name,)).fetchone()
            return bool(r and r[0])
    def settled():
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            p=auto/'file-changes/pending.json'
            if not p.exists():return
            time.sleep(.05)
        raise AssertionError('project scan did not settle')
    def baseline():
        with patch.object(Q,'_spawn_worker'):
            Q.handle('reconcile',{'cwd':str(project),'hook_event_name':'SessionStart'})
        Q.worker(project)
        assert (auto/'file-changes/snapshot.json').is_file(),'baseline scanner must actually run'
        assert not lines(),'first baseline must not enqueue unchanged sources'
    if scenario not in ('first-snapshot','first-create','first-update'):baseline()
    if scenario=='delete':
        a.unlink();invoke('file-change-event',payload)
        assert not lines(),'Bash event must defer QMD work until Stop'
        assert invoke('topical-stop',{'cwd':str(project),'hook_event_name':'Stop'})=='{}\n'
        settled();assert len(lines())==1;drain();assert not active('a.md')
    elif scenario=='rename':
        a.rename(docs/'b.md');invoke('file-change-event',payload)
        invoke('topical-stop',{'cwd':str(project),'hook_event_name':'Stop'});settled()
        assert len(lines())==1;drain();assert not active('a.md') and active('b.md')
    elif scenario=='failed':
        for bad in ({**payload,'hook_event_name':'PostToolUseFailure'},
                    {**payload,'tool_response':{'exit_code':7}},
                    {**payload,'tool_response':{'interrupted':True}},
                    {**payload,'tool_response':{'backgroundTaskId':'synthetic-task'}},
                    {**payload,'tool_response':None}, {**payload,'tool_response':{}}):
            invoke('file-change-event',bad)
            assert not (auto/'file-changes/pending.json').exists(),'failed or unknown result must not create a job'
        invoke('topical-stop',{'cwd':str(project),'hook_event_name':'Stop'})
        assert not lines() and active('a.md') and not (auto/'file-changes/pending.json').exists()
    elif scenario=='outside':
        (outside/'secret.md').unlink();invoke('file-change-event',payload)
        invoke('topical-stop',{'cwd':str(project),'hook_event_name':'Stop'});settled()
        assert not lines()
        with sqlite3.connect(index) as db:assert db.execute("SELECT active FROM documents WHERE collection='fixture-outside'").fetchone()[0]==1
    elif scenario=='duplicate':
        a.unlink();invoke('file-change-event',payload);invoke('file-change-event',payload)
        invoke('topical-stop',{'cwd':str(project),'hook_event_name':'Stop'});settled()
        invoke('topical-stop',{'cwd':str(project),'hook_event_name':'Stop'});assert len(lines())==1
        drain();assert not active('a.md')
    elif scenario=='recovery':
        a.unlink();invoke('file-change-event',payload)
        invoke('update-queued',{'cwd':str(project),'hook_event_name':'SessionStart'});settled()
        assert len(lines())==1;drain();assert not active('a.md')
    elif scenario=='first-snapshot':
        a.unlink();invoke('file-change-event',payload)
        invoke('topical-stop',{'cwd':str(project),'hook_event_name':'Stop'});settled()
        assert len(lines())==1,'missing indexed file must survive absence of a previous snapshot'
        drain();assert not active('a.md')
    elif scenario=='lost-wakeup':
        a.unlink();Q.handle('event',payload)
        original=Q._snapshot;count=[0]
        def concurrent(*args,**kwargs):
            result=original(*args,**kwargs)
            if count[0]==0:
                count[0]+=1;(docs/'b.md').write_text('Synthetic beta.\n');Q.handle('event',payload)
            return result
        with patch.object(Q,'_snapshot',side_effect=concurrent):Q.worker(project)
        assert not (auto/'file-changes/pending.json').exists()
        drain();assert not active('a.md') and active('b.md')
    elif scenario in ('first-create','first-update'):
        target=docs/'b.md' if scenario=='first-create' else a
        target.write_text('Synthetic beta.\n');invoke('file-change-event',payload)
        invoke('topical-stop',{'cwd':str(project),'hook_event_name':'Stop'});settled()
        assert len(lines())==1,'first snapshot must detect unindexed create or changed indexed hash'
        drain();assert active(target.name)
        with sqlite3.connect(index) as db:assert db.execute('SELECT hash FROM documents WHERE collection=? AND path=?',('fixture-docs',target.name)).fetchone()[0]==hashlib.sha256(target.read_bytes()).hexdigest()
    elif scenario=='fifo':
        fifo=auto/'file-changes/pending.json';os.mkfifo(fifo,0o600)
        try:
            p=subprocess.run([sys.executable,str(REPO/'core/file_change_queue.py'),'worker',str(project)],
                env=env,cwd=project,capture_output=True,text=True,timeout=2)
        except subprocess.TimeoutExpired:
            raise AssertionError('non-regular job file must fail closed without blocking open')
        assert fifo.exists() and not lines()
        assert json.loads((auto/'file-changes/status.json').read_text())['status']=='deferred'
    elif scenario=='burst':
        a.unlink();Q.handle('event',payload);original=Q._snapshot;count=[0]
        def concurrent(*args,**kwargs):
            result=original(*args,**kwargs);count[0]+=1
            (docs/('b'+str(count[0])+'.md')).write_text('Synthetic burst.\n');Q.handle('event',payload)
            return result
        with patch.object(Q,'_snapshot',side_effect=concurrent):Q.worker(project)
        settled();drain();assert not active('a.md') and active('b'+str(Q.MAX_PASSES)+'.md')
    elif scenario=='same-stat':
        stamp=a.stat();a.write_text('Synthetic bravo.\n');os.utime(a,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
        assert a.stat().st_size==stamp.st_size
        invoke('file-change-event',payload);invoke('topical-stop',{'cwd':str(project),'hook_event_name':'Stop'});settled()
        assert len(lines())==1,'same size and mtime must not hide Markdown changes'
        drain()
    elif scenario=='worker-failure':
        a.unlink();Q.handle('event',payload)
        with patch.object(Q.dirty_queue,'enqueue_project_collections',side_effect=PermissionError('synthetic write denial')):Q.worker(project)
        assert (auto/'file-changes/pending.json').exists() and not lines()
        Q.worker(project);assert len(lines())==1
        drain();assert not active('a.md')
    elif scenario=='compile-cap':
        settings['compile']={'mode':'auto-wiki','triggers':['post_tool_source'],'extractor':{'builtins':['claude']}}
        (auto/'settings.json').write_text(json.dumps(settings))
        env['QMD_SYNC_COMPILE_MAX']='1';os.environ.update(env)
        (docs/'b.md').write_text('Synthetic beta.\n');(docs/'c.md').write_text('Synthetic gamma.\n')
        Q.handle('event',payload);Q.worker(project)
        import compile_paths
        source_queue=project/compile_paths.rel(compile_paths.SOURCE_QUEUE)
        assert len(source_queue.read_text().splitlines())==1
        assert (auto/'file-changes/pending.json').exists(),'capped sources must retain original event job'
        with patch.object(Q,'_spawn_worker'):Q.handle('reconcile',{'cwd':str(project),'hook_event_name':'SessionStart'})
        assert json.loads((auto/'file-changes/pending.json').read_text())['origin']=='event'
        Q.worker(project)
        assert len(source_queue.read_text().splitlines())==2,'SessionStart must retain event compile grant for deferred sources'
        assert not (auto/'file-changes/pending.json').exists()
        # Enqueue succeeds, snapshot write fails: the retry skips the still-queued
        # same-revision row but must still kick the compile worker immediately.
        (docs/'d.md').write_text('Synthetic delta.\n');Q.handle('event',payload)
        original_write=Q._write
        def failing(path,value):
            if path.name=='snapshot.json':raise OSError('synthetic snapshot failure')
            return original_write(path,value)
        log=home/'manager.log';log.unlink(missing_ok=True)
        with patch.object(Q,'_write',side_effect=failing):Q.worker(project)
        assert len(source_queue.read_text().splitlines())==3
        assert (auto/'file-changes/pending.json').exists(),'failed snapshot write must keep the job'
        assert not log.exists(),'the failed run raised before any kick'
        Q.worker(project)
        assert len(source_queue.read_text().splitlines())==3,'retry must not queue a second paid job'
        status=json.loads((auto/'file-changes/status.json').read_text())
        assert (status['compileQueued'],status['compileDeferred'])==(0,0),status
        assert 'wakePending' not in status
        assert 'kick-wiki-compile '+str(project) in log.read_text().splitlines(),'skipped live row must still be woken'
        assert not (auto/'file-changes/pending.json').exists()
    elif scenario=='guards':
        a.unlink();before=(auto/'file-changes/snapshot.json').read_bytes()
        env['QMD_SANDBOX']='1';invoke('file-change-event',payload)
        assert invoke('topical-stop',{'cwd':str(project),'hook_event_name':'Stop'})=='{}\n'
        env.pop('QMD_SANDBOX');env.pop('QMD_SETUP_GUARD_FIXTURE');os.environ.clear();os.environ.update(env)
        invoke('file-change-event',payload);invoke('topical-stop',{'cwd':str(project),'hook_event_name':'Stop'})
        assert not lines() and not (auto/'file-changes/pending.json').exists()
        assert (auto/'file-changes/snapshot.json').read_bytes()==before and active('a.md')
    assert (outside/'secret.md').exists() or scenario=='outside'
    print(json.dumps({'scenario':scenario,'pass':True,'operatingAccess':False,'externalModelCalls':0}))

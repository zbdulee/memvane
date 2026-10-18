"""Synthetic cutover: local recall and dirty update hit only the selected DB."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0,'core')
import runtime_update
import wiki_topical

with tempfile.TemporaryDirectory(prefix='qmd-pointer-e2e-',dir=Path.home()) as name:
    root=Path(name).resolve()
    (root/'.auto-context').mkdir()
    (root/wiki_topical.MARKER).write_text('synthetic only\n')
    docs=root/'docs';docs.mkdir()
    (docs/'note.md').write_text('Synthetic amber compass points north.\n')
    settings=root/'.auto-context/settings.json'
    settings.write_text(json.dumps({'indexing':True,'collections':['synthetic'],
        'collectionPaths':{'synthetic':'docs'},'collectionRoles':{'synthetic':'raw'},
        'recallStrategy':'flat','topN':1,'minScore':0,
        'events':['sessionStart','userPromptSubmit','postToolUse']}))
    config=root/'config/index.yml';config.parent.mkdir()
    config.write_text('collections: {}\nmodels:\n  embed: synthetic-model\n')
    wiki=root/'.auto-context/wiki';wiki.mkdir()
    cache=root/'qmd-cache/qmd/models';cache.mkdir(parents=True)
    calls=root/'calls.jsonl'
    qmd=root/'qmd'
    qmd.write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
Path(os.environ['QMD_SYNTHETIC_CALLS']).open('a').write(json.dumps({
 'command':sys.argv[1], 'argv':sys.argv[1:], 'index':os.environ.get('INDEX_PATH'),
 'config':os.environ.get('QMD_CONFIG_DIR'), 'cache':os.environ.get('XDG_CACHE_HOME')})+'\\n')
if sys.argv[1]=='query':
 print(json.dumps([{'docid':'#synthetic','file':'qmd://synthetic/note.md',
 'title':'Synthetic note','score':1,'context':None,'line':1,
 'snippet':'Synthetic amber compass points north.'}]))
''')
    qmd.chmod(0o700)
    # Selected-index recall verifies the returned file's current SHA-256
    # against documents.hash, as real QMD rows carry it.
    note_hash=hashlib.sha256((docs/'note.md').read_bytes()).hexdigest()
    def make_index(path):
        with sqlite3.connect(path) as db:
            db.executescript('CREATE TABLE documents(collection TEXT,path TEXT,active INTEGER,hash TEXT);'
                'CREATE TABLE content(hash TEXT);'
                'CREATE TABLE content_vectors(hash TEXT,seq INTEGER,model TEXT,embed_fingerprint TEXT);'
                'CREATE TABLE store_collections(name TEXT);'
                'CREATE TABLE vectors_vec(embedding "float[768] distance_metric=cosine");')
            db.execute('INSERT INTO documents VALUES (?,?,1,?)',('synthetic','note.md',note_hash))
            db.execute('INSERT INTO content_vectors VALUES (?,?,?,?)',('x',0,'synthetic-model','f1'))
    old=root/'original.sqlite';make_index(old);original=old.read_bytes()
    def runner(command,*,env,**kwargs):
        if command[1]=='update':make_index(env['INDEX_PATH'])
        return SimpleNamespace(returncode=0,stdout='[]' if command[1]=='vsearch' else '')
    os.environ['INDEX_PATH']=str(old)
    staged=runtime_update.stage_shadow_index(root,qmd_bin=qmd,config_file=config,
        wiki_dir=wiki,model_cache=cache.parent.parent,expected_model='synthetic-model',
        expected_dimension=768,allow_execution=True,runner=runner)
    runtime_update.activate_shadow_index(root,staged['generation'])
    selected=runtime_update.select_runtime(docs)
    assert selected['INDEX_PATH']==staged['index']
    env={**os.environ,'QMD_BIN':str(qmd),'QMD_SYNTHETIC_CALLS':str(calls),
         'QMD_RECALL_LOG':'','QMD_DAEMON_URL':'http://127.0.0.1:1'}
    env.pop('QMD_QUERY_FIXTURE',None)
    recall=subprocess.run([sys.executable,'core/recall.py'],input=json.dumps({
        'prompt':'Where does the synthetic amber compass point?', 'cwd':str(docs)}),
        text=True,capture_output=True,env=env,timeout=15,check=True)
    # The QMD title is untrusted; the verified hit carries its path stem.
    assert 'note.md - note' in recall.stdout,recall.stdout
    env.update(QMD_CACHE_DIR=str(root/'update-cache'),QMD_LOCK_BASE=str(root/'update-locks'),
        QMD_SKIP_BACKGROUND_WORKER='1',QMD_SKIP_BACKGROUND_EMBED='1')
    update_result=subprocess.run(['bash',str(Path.cwd()/'core/update.sh'),'--worker',str(root)],
        text=True,capture_output=True,env=env,timeout=20,check=True)
    update_calls=[json.loads(line) for line in calls.read_text().splitlines()]
    assert any(row['command']=='update' for row in update_calls),(
        update_calls,update_result.stdout,update_result.stderr,
        (root/'update-cache/hook.log').read_text() if (root/'update-cache/hook.log').exists() else '')
    assert all(row['index']==staged['index'] for row in update_calls),update_calls
    queue=root/'queue';queue.write_text('synthetic\t'+str(docs)+'\n')
    env.update(QMD_DIRTY_QUEUE=str(queue),QMD_FAKE_QMD=str(qmd),
        QMD_INDEX_WORKER_LOCKDIR=str(root/'worker.lock'),
        QMD_WRITER_LOCKDIR=str(root/'writer.lock'),
        QMD_EMBED_LOCKDIR=str(root/'embed.lock'),
        QMD_INDEX_WORKER_LOG=str(root/'worker.log'),QMD_NO_RELOAD='1')
    subprocess.run(['bash','backend/index_worker.sh'],env=env,timeout=15,check=True)
    recorded=[json.loads(line) for line in calls.read_text().splitlines()]
    assert {'query','update','embed'} <= {row['command'] for row in recorded},recorded
    assert all(row['index']==staged['index'] for row in recorded),recorded
    assert all(row['config']==selected['QMD_CONFIG_DIR'] for row in recorded),recorded
    assert all(row['cache']==selected['XDG_CACHE_HOME'] for row in recorded),recorded
    assert old.read_bytes()==original
    assert queue.read_text()==''
    # A selected-index claim must also respect the SessionStart embed lock.
    # A busy child leaves the queue intact and makes no QMD write.
    queue.write_text('synthetic\t'+str(docs)+'\t'+str(root)+'\n')
    embed_lock=Path(env['QMD_EMBED_LOCKDIR'])
    embed_lock.mkdir()
    (embed_lock/'pid').write_text(str(os.getpid()))
    before_lock_calls=calls.read_bytes()
    blocked=subprocess.run(['bash','backend/index_worker.sh'],env=env,timeout=15,
                           capture_output=True,text=True,check=False)
    assert blocked.returncode==75,blocked
    assert queue.read_text()=='synthetic\t'+str(docs)+'\t'+str(root)+'\n'
    assert calls.read_bytes()==before_lock_calls
    (embed_lock/'pid').unlink();embed_lock.rmdir()
    subprocess.run(['bash','backend/index_worker.sh'],env=env,timeout=15,check=True)
    assert queue.read_text()=='' and not embed_lock.exists()
    # External allowRoot collections cannot infer ownership from their path;
    # the selected project's third queue column carries the owner explicitly.
    with tempfile.TemporaryDirectory(prefix='qmd-external-',dir=Path.home()) as external:
        (Path(external)/'doc.md').write_text('Synthetic external collection.\n')
        updated=json.loads(settings.read_text())
        updated['collections'].append('external')
        updated['collectionPaths']['external']=external
        updated['collectionRoles']['external']='raw'
        updated['allowRoots']=[external]
        settings.write_text(json.dumps(updated))
        subprocess.run([sys.executable,'core/index_enqueue.py'],input=json.dumps({
            'hook_event_name':'PostToolUse','cwd':str(root),
            'tool_input':{'file_path':str(Path(external)/'doc.md')}}),
            text=True,capture_output=True,env=env,timeout=10,check=True)
        assert queue.read_text()=='external\t'+external+'\t'+str(root)+'\n'
        subprocess.run(['bash','backend/index_worker.sh'],env=env,timeout=15,check=True)
        external_calls=[json.loads(line) for line in calls.read_text().splitlines()]
        assert external_calls[-3]['argv'][-3:]==[external,'--name','external'],external_calls[-3]
        assert all(row['index']==staged['index'] for row in external_calls),external_calls
        assert queue.read_text()==''
    with sqlite3.connect(staged['index']) as db:
        db.execute('INSERT INTO documents(collection,path,active) VALUES (?,?,1)',('synthetic','new.md'))
    assert runtime_update.select_runtime(root)['INDEX_PATH']==staged['index']
    # The model fingerprint comes from vector rows. QMD cleanup removes the
    # last such row after all documents retire, but a live document without
    # the staged fingerprint must still fail closed.
    with sqlite3.connect(staged['index']) as db:
        db.execute('DELETE FROM content_vectors')
    try:
        runtime_update.select_runtime(root)
    except ValueError as exc:
        assert str(exc)=='index_pointer_changed'
    else:
        raise AssertionError('live_document_without_model_fingerprint_accepted')
    with sqlite3.connect(staged['index']) as db:
        db.execute('DELETE FROM documents')
    assert runtime_update.select_runtime(root)['INDEX_PATH']==staged['index']
    with sqlite3.connect(staged['index']) as db:
        db.execute('INSERT INTO documents(collection,path,active) VALUES (?,?,1)',('synthetic','rogue.md'))
        db.execute('INSERT INTO content_vectors VALUES (?,?,?,?)',('rogue',0,'other-model','f2'))
    try:
        runtime_update.select_runtime(root)
    except ValueError as exc:
        assert str(exc)=='index_pointer_changed'
    else:
        raise AssertionError('wrong_model_fingerprint_accepted')
    with sqlite3.connect(staged['index']) as db:
        db.execute('DELETE FROM content_vectors')
        db.execute('DELETE FROM documents')
    assert runtime_update.select_runtime(root)['INDEX_PATH']==staged['index']
    pointer=root/'.auto-context/qmd-index-active.json'
    saved=pointer.read_bytes()
    pointer.write_text('[]\n')
    bad=subprocess.run([sys.executable,'core/runtime_update.py','resolve-env',str(docs)],
        text=True,capture_output=True,env=env,timeout=10)
    assert bad.returncode==1 and not bad.stdout
    queue.write_text('synthetic\t'+str(docs)+'\n')
    before_calls=calls.read_bytes()
    failed_worker=subprocess.run(['bash','backend/index_worker.sh'],env=env,timeout=15,
                                 capture_output=True,text=True,check=False)
    assert failed_worker.returncode==1,failed_worker
    assert queue.read_text()=='synthetic\t'+str(docs)+'\n'
    assert calls.read_bytes()==before_calls
    pointer.write_bytes(saved)
    rolled=runtime_update.rollback_shadow_index(root)
    assert rolled['status']=='index_rolled_back_original'
    assert not (root/'.auto-context/qmd-index-active.json').exists()
    assert old.read_bytes()==original
    print(json.dumps({'syntheticRecallLocal':True,'syntheticUpdateSelected':True,
        'mutableSelectedIndexValid':True,'externalCollectionRouted':True,
        'emptySelectedIndexValid':True,'missingLiveFingerprintRejected':True,
        'wrongModelFingerprintRejected':True,
        'invalidPointerFailsClosed':True,
        'rollbackOriginalPreserved':True,'externalCalls':0}))

#!/usr/bin/env python3
"""Fresh-first content open after actual closed-writer published prefix.

Each reader interpreter gets an independent real seed/producer/writer chain.
The fixture interpreter prepares both roots; only the cold reader varies.
Only the existing synthetic bundle/provider/OS authority seams are reused.
"""
from contextlib import ExitStack, closing, nullcontext, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import select
import signal
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.parse import unquote, urlparse

ROOT=Path(__file__).resolve().parents[2]
SELF=Path(__file__).resolve()

SELECTED_READER=r'''
from pathlib import Path
from types import SimpleNamespace
import importlib.util,json,os,signal,sqlite3,sys
from urllib.parse import unquote,urlparse
root,payload=Path(sys.argv[1]),Path(sys.argv[2]);p=json.loads(payload.read_text())
sys.path.insert(0,str(root/'core'))
os.environ['HOME']=p['home'];os.environ['MEMVANE_TEST_RUNTIME_ROOT']=p['runtime']
signal.alarm(25)
sql_connections=[];violations=[]
if p.get('trace'):
    real_connect=sqlite3.connect
    watched=set(p['watchPaths'])
    def connect_traced(database,*args,**kwargs):
        raw=str(database)
        path=unquote(urlparse(raw).path) if raw.startswith('file:') else raw
        if path not in watched:
            return real_connect(database,*args,**kwargs)
        base=kwargs.pop('factory',sqlite3.Connection)
        class Watch(base):
            def __init__(self,*a,**kw):
                super().__init__(*a,**kw)
                self._spy_query_only=False
                self.set_authorizer(None)
            def set_authorizer(self,callback):
                def guarded(action,arg1,arg2,database,source):
                    if action==sqlite3.SQLITE_PRAGMA and str(arg1).lower()=='query_only' and arg2 is not None:
                        if str(arg2).lower() not in ('1','on','true'):
                            violations.append(('query_only_disabled',path))
                        self._spy_query_only=True
                    if action in (sqlite3.SQLITE_READ,sqlite3.SQLITE_SELECT) and not self._spy_query_only:
                        violations.append(('read_before_query_only',path))
                    if action in (sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE,
                                  sqlite3.SQLITE_CREATE_TABLE,sqlite3.SQLITE_DROP_TABLE,
                                  sqlite3.SQLITE_CREATE_VTABLE,sqlite3.SQLITE_DROP_VTABLE):
                        violations.append(('write_sql',path))
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK if callback is None else callback(action,arg1,arg2,database,source)
                return super().set_authorizer(guarded)
        db=real_connect(database,*args,factory=Watch,**kwargs)
        sql_connections.append(raw)
        return db
    sqlite3.connect=connect_traced
spec=importlib.util.spec_from_file_location('selected_cold_base',root/'test/fixtures/laya-journal-reader-checks.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
import laya_journal_reader as reader,laya_reader_lease as lease,laya_index_a as index
case=SimpleNamespace(plugin=Path(p['plugin']),proof=p['proof'])
helper=base.JournalReaderChecks('runTest')
with helper.full_authority(case,reader,lease):
    with lease.resolve_endpoint(Path(p['project']),action='recall',host_root=case.plugin) as endpoint:
        assert endpoint.record['capability']=='v6-ro-v1'
        path=Path(endpoint.record['db_path'])
        if p.get('fullQuery'):
            import time,laya_reader_open as opens,laya_v6_reader as search
            decision=reader.journal.select_route(p['project'],action='recall')
            with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+15) as observed:
                if p.get('sidecarExpected'):
                    assert observed.sidecar['baseline'] is (not p.get('sidecarLegacy',False)) and observed.connections[3] is not None
                else:
                    assert observed.sidecar is None and observed.connections[3] is None
                assert all(db.execute('PRAGMA query_only').fetchone()==(1,)
                           for db in observed.connections if db is not None)
                answer=search.query_v6_snapshot(observed,endpoint=endpoint,prompt='otter',
                    decision=decision,provider=None,deadline=time.monotonic()+10)
                generation=answer.data_generation
                rows=answer.rows
                assert any('otter' in row['body'] for row in rows)
                if p.get('expectedPath'):
                    assert any(row['path']==p['expectedPath'] and 'otter' in row['body'] for row in rows)
                assert answer.semantic_status=='lexical_only'
        else:
            with reader._readonly(path,path.parent) as db:
                assert db.execute('PRAGMA query_only').fetchone()==(1,)
                generation=db.execute('SELECT gen FROM head WHERE scope=?',(endpoint.record['scope'],)).fetchone()[0]
                ordinal=db.execute('SELECT ord FROM gen_index WHERE gen=?',(generation,)).fetchone()[0]
                table=index._gen_fts(ordinal)
                rows=db.execute('SELECT c.body FROM "'+table+'" f JOIN membership m ON m.path=f.path_key '
                    'AND m.gen=? JOIN content c ON c.hash=m.hash WHERE "'+table+'" MATCH ?',
                    (generation,'otter')).fetchall()
                assert any('otter' in row[0] for row in rows)
        assert generation==p['generation']
        if p.get('trace'):
            assert sql_connections and not violations,(sql_connections,violations)
        print(json.dumps(dict(event='selected-cold-result',python=list(sys.version_info[:3]),
            generation=generation,ftsRows=len(rows),tracedConnections=len(sql_connections),
            queryOnlyViolations=len(violations))),flush=True)
'''

def load(name,filename):
    spec=importlib.util.spec_from_file_location(name,ROOT/'test/fixtures'/filename)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

def emit(event,**fields):
    print(json.dumps(dict(event=event,**fields),sort_keys=True),flush=True)

def interpreter():
    return dict(pid=os.getpid(),executable=str(Path(sys.executable).resolve()),
                python=list(sys.version_info[:3]),sqlite=sqlite3.sqlite_version,freshExec=True)

def dbpath(value):
    text=str(value)
    return Path(unquote(urlparse(text).path)) if text.startswith('file:') else Path(text)

def write_payload(path,value):
    path.write_text(json.dumps(value),encoding='utf8');path.chmod(0o600)

def writer(payload):
    sys.path.insert(0,str(ROOT/'core'))
    p=json.loads(payload.read_text());os.environ['HOME']=p['home']
    os.environ['MEMVANE_TEST_RUNTIME_ROOT']=p['runtime'];signal.alarm(40)
    base=load('cold_writer_base','laya-journal-reader-checks.py')
    import laya_journal_reader as R,laya_reader_lease as L
    import laya_project_dir_move as mover,laya_state_move as state_move
    import laya_fresh_wiki_origin as fresh
    from laya_index_a import LayaIndexA
    state,project=Path(p['state']),Path(p['project'])
    helper=base.JournalReaderChecks('runTest')
    case=SimpleNamespace(plugin=Path(p['plugin']),proof=p['proof'])
    observed={};catalog_connection=None;publish_connection=None
    actual_connect=sqlite3.connect;actual_publish=LayaIndexA.publish
    class ObservedConnection(sqlite3.Connection):
        def __init__(self,database,*args,**kwargs):
            super().__init__(database,*args,**kwargs);self.fixture_path=dbpath(database)
        def execute(self,sql,*args,**kwargs):
            nonlocal catalog_connection
            result=super().execute(sql,*args,**kwargs)
            if self.fixture_path==Path(p['catalog']) and sql.strip().upper()=='COMMIT':
                catalog_connection=self
                observed['catalogMeta']=dict(super().execute('SELECT key,value FROM meta'))
                observed['catalogCommitObservedOnWriter']=True
            return result
        def close(self):
            super().close()
            if self is catalog_connection:observed['catalogWriterClosed']=True
            if self is publish_connection:observed['basePublisherClosed']=True
    def connect(*args,**kwargs):
        kwargs['factory']=ObservedConnection
        return actual_connect(*args,**kwargs)
    def publish(index,expected,gen,claim,*args,**kwargs):
        nonlocal publish_connection
        result=actual_publish(index,expected,gen,claim,*args,**kwargs)
        if gen==p['target']:
            publish_connection=index.db
            assert result=='published' and kwargs.get('require_vector') is True
            original=index.db.execute('SELECT scope,gen,source_manifest,claim_digest,claim_epoch '
                'FROM receipts WHERE claim=?',(claim,)).fetchone()
            seal=index.db.execute('SELECT base_gen,source_manifest,claim_digest,claim_epoch '
                'FROM candidate_seals WHERE gen=?',(gen,)).fetchone()
            generation=index.db.execute('SELECT scope,state FROM generations WHERE gen=?',(gen,)).fetchone()
            head=index.db.execute('SELECT gen FROM head WHERE scope=?',(p['scope'],)).fetchone()
            assert original==tuple(p['receipt']) and seal==tuple(p['candidateSeal'])
            assert generation==(p['scope'],'published') and head==(gen,)
            assert index._generation_fts_matches(p['generation']) and index._generation_fts_matches(gen)
            assert index.db.execute('SELECT count(*) FROM gen_vector_seals WHERE gen=?',(gen,)).fetchone()==(1,)
            observed.update(publicationResult=result,head=head[0],targetState=generation[1],receipt=original,
                candidateSeal=seal,oldAndNewBodyFTSValid=True,actualVectorSeal=True,
                publishEvidenceOnExistingWriter=True)
        return result
    try:
        with helper.full_authority(case,R,L,mover),mock.patch.object(state_move,'_provider_lane',lambda:nullcontext()):
            intent=mover._load_intent(state,project);assert intent is not None
            with ExitStack() as stack:
                mover._locks(stack,project,state,mover._queue_path(project,intent))
                with L.move_lifetime(state,project,intent=intent):
                    _record,_raw,proof=R._evidence(state,project,case.plugin)
                    assert proof['productRouteAllowed'] is True
                    assert R.read_selection(state/'install-journal.sqlite').preferred_endpoint==p['retained']
                    with mock.patch.object(sqlite3,'connect',side_effect=connect),mock.patch.object(LayaIndexA,'publish',new=publish):
                        def checkpoint(name):
                            if name!='published':return
                            assert observed.get('catalogWriterClosed') and observed.get('basePublisherClosed')
                            assert {key:observed['catalogMeta'][key] for key in ('binding','roots')}==p['newCatalog']
                            assert observed['catalogMeta']['scope']==p['scope']
                            folder=mover.current_folder(project)
                            assert not os.path.lexists(mover.legacy_folder(project))
                            assert R._node(folder)==p['folderNode'] and R._node(folder/'wiki')==p['wikiNode']
                            assert R._node(Path(p['binding']))==p['newBindingNode']
                            assert hashlib.sha256(Path(p['binding']).read_bytes()).hexdigest()==p['newBindingSha']
                            assert not os.path.lexists(p['bindingTemp'])
                            anchor=folder/'wiki'/mover.project_dir.WIKI_ANCHOR
                            assert anchor.is_file() and not anchor.is_symlink() and anchor.read_bytes()==b''
                            for path,raw in p['oldMetadata'].items():assert Path(path).read_bytes()==raw.encode('utf8')
                            assert (state/fresh.LEDGER).read_bytes()==mover._canonical(intent['fencedLedger'])
                            assert (folder/fresh.ROUTE).read_bytes()==intent['oldRoute'].encode('utf8')
                            assert (folder/fresh.MARKER).read_bytes()==intent['oldMarker'].encode('utf8')
                            assert hashlib.sha256((state/'install-journal.sqlite').read_bytes()).hexdigest()==p['journalSha']
                            emit('closed-writer-published-checkpoint',identity=interpreter(),observed=observed,
                                actualOwner=True,actualLifetimeEX=True,prefenceFullProof=True,
                                actualBindingRenameFsync=True,actualHostExecuted=False)
                            os.kill(os.getpid(),signal.SIGKILL)
                        mover._switch(intent,step=checkpoint)
        raise RuntimeError('actual published checkpoint not reached')
    except BaseException as exc:
        emit('writer-factory-error',identity=interpreter(),errorType=type(exc).__name__,reason=str(exc))
        return 2

def producer(payload):
    signal.alarm(100)
    fixture=load('cold_seal_fixture','laya-reader-move-seal-checks.py')
    seed=fixture.MoveSealChecks('runTest')
    try:
        with redirect_stdout(io.StringIO()):
            seed.setUp()
            with seed.prepared() as intent:
                stage,seal_type=seed.producer();seal=stage(intent,installer=seed.installer)
                assert type(seal) is seal_type
                selected=seed.lease.publish_move_reader(seed.case.journal,seal=seal)
                seed.reader._recheck(seal.retained_endpoint)
        case=seed.case;generation=intent['generation']
        import laya_fresh_wiki_origin as fresh,laya_initial_build as build
        old_metadata={str(path):path.read_text(encoding='utf8') for path in
            (case.state/'reviewed-route.json',case.state/fixture.base.J.PRODUCTION_ATTESTATION,
             build.record_path(case.state),case.state/seed.mover.INTENT)}
        if intent['discovery'] is not None:
            path=Path(intent['discovery'][0]);old_metadata[str(path)]=path.read_text(encoding='utf8')
        p=dict(home=str(case.home),runtime=os.environ['MEMVANE_TEST_RUNTIME_ROOT'],state=str(case.state),
            project=str(case.project),plugin=str(case.plugin),proof=case.proof,db=str(case.db),queue=str(case.queue),
            catalog=str(case.catalog),binding=str(case.store),bindingTemp=intent['store']['temp'],
            generation=seal.old_generation,exclusion=seal.exclusion_epoch,retained=selected.preferred_endpoint,
            target=generation['gen'],scope=case.route['scopeId'],
            receipt=[case.route['scopeId'],generation['gen'],generation['manifest'],generation['claimDigest'],generation['claimEpoch']],
            candidateSeal=[generation['base'],generation['manifest'],generation['claimDigest'],generation['claimEpoch']],
            newCatalog={key:intent['catalog'][key][1] for key in ('binding','roots')},
            folderNode=seed.reader._node(case.auto),wikiNode=seed.reader._node(case.auto/'wiki'),
            newBindingNode=intent['store']['identity'],
            newBindingSha=hashlib.sha256(Path(intent['store']['temp']).read_bytes()).hexdigest(),
            journalSha=hashlib.sha256(case.journal.read_bytes()).hexdigest(),oldMetadata=old_metadata)
        assert p['exclusion']==0
        write_payload(payload,p)
        result=subprocess.run([sys.executable,'-B','-I',str(SELF),'writer',str(payload)],
            capture_output=True,encoding='utf8',timeout=45)
        events=[json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        if result.returncode!=-signal.SIGKILL or len(events)!=1 or events[0].get('event')!='closed-writer-published-checkpoint':
            raise RuntimeError('actual writer/checkpoint prerequisite failed: '+json.dumps(dict(
                returncode=result.returncode,events=events,stderr=result.stderr[-4000:])))
        # No SQLite inspection, seed.head(), live() or verifier after writer exit.
        emit('producer-ready',identity=interpreter(),writer=events[0],writerExit=result.returncode,payload=p)
        assert sys.stdin.readline().strip()=='release'
        return 0
    except BaseException as exc:
        emit('producer-factory-error',identity=interpreter(),errorType=type(exc).__name__,reason=str(exc))
        return 2
    finally:seed.doCleanups()

def reader(payload):
    p=json.loads(payload.read_text());calls=[];actual=sqlite3.connect
    def observe(value,*args,**kwargs):
        path=dbpath(value)
        if str(path).startswith(p['state']+os.sep) and path.suffix=='.sqlite' and path.name!='install-journal.sqlite':
            calls.append(str(path))
        return actual(value,*args,**kwargs)
    fixture=load('cold_reader_journal_fixture','laya-reader-move-journal-checks.py')
    emit('reader-entry',identity=interpreter(),actualHostExecuted=False)
    try:
        sys.argv=[str(SELF),str(ROOT),str(payload)]
        with mock.patch.object(sqlite3,'connect',side_effect=observe):
            exec(compile(fixture.FRESH_READER,str(SELF)+':existing-fresh-reader','exec'),{})
        return 0
    except BaseException as exc:
        emit('reader-contract-error',errorType=type(exc).__name__,reason=str(exc))
        return 3
    finally:emit('reader-open-observation',identity=interpreter(),firstContentOpen=calls[0] if calls else None,
                 contentOpenCount=len(calls),parentContentSQLiteOpensAfterWriter=0)

def image(root):
    return {str(path.relative_to(root)):(path.read_bytes(),tuple(getattr(path.lstat(),key)
        for key in ('st_dev','st_ino','st_mode','st_uid','st_nlink')))
        for path in root.rglob('*') if path.is_file() and not path.is_symlink()}

def without_sqlite_aux(before, after):
    """Ignore SQLite WAL/SHM only for main database files present before read."""
    mains={name for name in before if name.endswith(('.sqlite','.sqlite3'))}
    auxiliary={name+suffix for name in mains for suffix in ('-wal','-shm')}
    return ({name:value for name,value in before.items() if name not in auxiliary},
            {name:value for name,value in after.items() if name not in auxiliary})

class PhysicalColdChecks(unittest.TestCase):
    def test_system_python39_selected_cold_full_query_without_optional_sidecar(self):
        self._selected_cold_full_query(sidecar_present=False)

    def test_system_python39_selected_cold_full_query_with_sealed_sidecar(self):
        self._selected_cold_full_query(sidecar_present=True)

    def test_system_python39_selected_cold_full_query_with_legacy_sidecar(self):
        self._selected_cold_full_query(sidecar_present=True,legacy=True)

    def _selected_cold_full_query(self, *, sidecar_present, legacy=False):
        fixture=load('selected_cold_full_seed','laya-reader-move-seal-checks.py')
        seed=fixture.MoveSealChecks('runTest')
        if not sidecar_present:
            seed.PUBLISH_SIDECAR=False
        if sidecar_present and not legacy:
            def seed_before_conversion():
                with sqlite3.connect(seed.sidecar) as db:
                    target=db.execute("SELECT rowid,path FROM docs WHERE body NOT LIKE '%otter%' ORDER BY path LIMIT 1").fetchone()
                    replacement=db.execute("SELECT hash,title,body,profile_id,chunk_count FROM docs WHERE body LIKE '%otter%' ORDER BY path LIMIT 1").fetchone()
                    self.assertIsNotNone(target);self.assertIsNotNone(replacement)
                    db.execute('DELETE FROM docs_fts WHERE rowid=?',(target[0],))
                    db.execute('UPDATE docs SET hash=?,title=?,body=?,profile_id=?,chunk_count=? WHERE rowid=?',(*replacement,target[0]))
                    db.execute('INSERT INTO docs_fts(rowid,path,title,body) VALUES(?,?,?,?)',(target[0],target[1],replacement[1],replacement[2]))
                    seed.expected_cold_path=target[1]
            seed.before_conversion=seed_before_conversion
        try:
            with redirect_stdout(io.StringIO()):
                seed.setUp()
                generation=seed.case.head()[0]
            case=seed.case
            expected_path=None
            if sidecar_present and legacy:
                with closing(sqlite3.connect(seed.sidecar)) as db:
                    self.assertIsNone(db.execute('SELECT value FROM publication_revisions WHERE revision=0').fetchone())
                    expected_path=db.execute("SELECT path FROM docs WHERE body LIKE '%otter%' ORDER BY path LIMIT 1").fetchone()[0]
            elif sidecar_present:
                expected_path=seed.expected_cold_path
                snapshot_fixture=load('cold_snapshot_seal','laya-reader-snapshot-race-checks.py')
                snapshot_fixture.AbsentSidecarSnapshotChecks('runTest').seal_sidecar(seed)
            for path in (case.db,case.catalog,seed.sidecar):
                if any(os.path.lexists(str(path)+suffix) for suffix in ('-wal','-shm')):
                    with sqlite3.connect(path,isolation_level=None) as db:
                        self.assertEqual(db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0],0)
                    db.close()
                self.assertFalse(any(os.path.lexists(str(path)+suffix)
                    for suffix in ('-wal','-shm','-journal')))
            if not sidecar_present:
                self.assertFalse(os.path.lexists(seed.sidecar))
            payload=case.home/'selected-cold-full-payload.json'
            write_payload(payload,dict(home=str(case.home),runtime=os.environ['MEMVANE_TEST_RUNTIME_ROOT'],
                project=str(case.project),plugin=str(case.plugin),proof=case.proof,
                generation=generation,fullQuery=True,trace=True,sidecarExpected=sidecar_present,sidecarLegacy=legacy,
                expectedPath=expected_path,
                watchPaths=[str(path) for path in (case.db,case.queue,case.catalog,case.store,case.journal,seed.sidecar)]))
            before=dict(state=image(case.state),project=image(case.project))
            result=subprocess.run(['/usr/bin/python3','-B','-I','-c',SELECTED_READER,str(ROOT),str(payload)],
                capture_output=True,encoding='utf8',timeout=35)
            after=dict(state=image(case.state),project=image(case.project))
            events=[json.loads(line) for line in result.stdout.splitlines() if line.strip()]
            emit('selected-python39-cold-full-query',returncode=result.returncode,events=events,
                 stderr=result.stderr,actualHostExecuted=False)
            for group in ('state','project'):
                old,new=without_sqlite_aux(before[group],after[group])
                self.assertEqual(old,new,'read path must preserve main DBs and optional sidecar files')
            self.assertEqual(os.path.lexists(seed.sidecar),sidecar_present,
                             'optional DB body presence must not change')
            self.assertEqual(result.returncode,0,'fresh system Python 3.9 full snapshot search must succeed')
            self.assertEqual(len(events),1)
            self.assertEqual(events[0]['python'][:2],[3,9])
            self.assertGreater(events[0]['ftsRows'],0)
            self.assertGreater(events[0]['tracedConnections'],0)
            self.assertEqual(events[0]['queryOnlyViolations'],0)
        finally:
            seed.doCleanups()

    def test_system_python39_selected_cold_search_without_sidecars(self):
        fixture=load('selected_cold_seed','laya-reader-move-seal-checks.py')
        seed=fixture.MoveSealChecks('runTest')
        try:
            with redirect_stdout(io.StringIO()):
                seed.setUp()
                generation=seed.case.head()[0]
            case=seed.case
            paths=(case.db,case.catalog,seed.sidecar)
            for path in paths:
                if any(os.path.lexists(str(path)+suffix) for suffix in ('-wal','-shm')):
                    with sqlite3.connect(path,isolation_level=None) as db:
                        self.assertEqual(db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0],0)
                    db.close()
                self.assertFalse(any(os.path.lexists(str(path)+suffix)
                    for suffix in ('-wal','-shm','-journal')),
                    'selected fixture must be truly cold: '+str(path))
            payload=case.home/'selected-cold-payload.json'
            write_payload(payload,dict(home=str(case.home),runtime=os.environ['MEMVANE_TEST_RUNTIME_ROOT'],
                project=str(case.project),plugin=str(case.plugin),proof=case.proof,generation=generation))
            before=dict(state=image(case.state),project=image(case.project))
            result=subprocess.run(['/usr/bin/python3','-B','-I','-c',SELECTED_READER,str(ROOT),str(payload)],
                capture_output=True,encoding='utf8',timeout=35)
            after=dict(state=image(case.state),project=image(case.project))
            events=[json.loads(line) for line in result.stdout.splitlines() if line.strip()]
            emit('selected-python39-cold-search',returncode=result.returncode,events=events,
                 stderr=result.stderr[-4000:],unchanged=before==after,actualHostExecuted=False)
            for group in ('state','project'):
                old,new=without_sqlite_aux(before[group],after[group])
                self.assertEqual(old,new,'read path must preserve main DBs and optional sidecar files')
            self.assertEqual(result.returncode,0,'fresh system Python 3.9 selected search must succeed')
            self.assertEqual(len(events),1)
            self.assertEqual(events[0]['python'][:2],[3,9])
            self.assertGreater(events[0]['ftsRows'],0)
        finally:
            seed.doCleanups()

    def check_cold(self,executable,expected_python):
        with tempfile.TemporaryDirectory(prefix='laya-cold-') as temporary:
            home=Path(temporary).resolve();home.chmod(0o700)
            runtime=home/'runtime';runtime.mkdir(mode=0o700)
            payload=home/'payload.json'
            env=dict(os.environ,HOME=str(home),TMPDIR=str(home),MEMVANE_TEST_RUNTIME_ROOT=str(runtime),
                XDG_RUNTIME_DIR=str(runtime),QMD_SETUP_GUARD_FIXTURE='1',QMD_SANDBOX='1',PYTHONDONTWRITEBYTECODE='1')
            child=subprocess.Popen([sys.executable,'-B','-I',str(SELF),'producer',str(payload)],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,encoding='utf8',env=env)
            try:
                if not select.select([child.stdout],[],[],65)[0]:raise RuntimeError('producer readiness deadline')
                line=child.stdout.readline()
                if not line:
                    _,stderr=child.communicate(timeout=5)
                    raise RuntimeError('producer exited before prerequisite evidence: '+stderr[-4000:])
                ready=json.loads(line)
                if ready.get('event')!='producer-ready':raise RuntimeError('fresh seed/writer prerequisite: '+line)
                p=ready.pop('payload')
                if ready['identity']['python'][:2]!=list(sys.version_info[:2]):
                    raise RuntimeError('producer differs from independent fixture interpreter: '+json.dumps(ready))
                state,project=Path(p['state']),Path(p['project'])
                before=dict(state=image(state),project=image(project))
                result=subprocess.run([executable,'-B','-I',str(SELF),'reader',str(payload)],input='release\n',
                    capture_output=True,encoding='utf8',timeout=35,env=env)
                after=dict(state=image(state),project=image(project))
                events=[json.loads(line) for line in result.stdout.splitlines() if line.strip()]
                resolved=[row for row in events if row.get('event')=='resolved']
                identities=[row['identity'] for row in events if row.get('event')=='reader-entry']
                comparable=dict(state=dict(after['state']),project=dict(after['project']))
                allowed_shm={Path(p['db']).name+'-shm',Path(p['catalog']).name+'-shm'}
                ignored=[]
                for path in allowed_shm:
                    old,new=before['state'].get(path),after['state'].get(path)
                    if old is not None and new is not None and old[1]==new[1]:
                        comparable['state'][path]=old
                        if old!=new:ignored.append(path)
                changed=[[group,path] for group in ('state','project') for path in
                    sorted(set(before[group])|set(after[group])) if before[group].get(path)!=comparable[group].get(path)]
                created=[[group,path] for group in ('state','project') for path in sorted(set(after[group])-set(before[group]))]
                removed=[[group,path] for group in ('state','project') for path in sorted(set(before[group])-set(after[group]))]
                emit('fresh-first-cold-result',requestedExecutable=executable,expectedPython=list(expected_python),
                    preparation=ready,returncode=result.returncode,reader=events,stderr=result.stderr[-4000:],
                    physicalChangedPaths=changed,newFiles=created,removedFiles=removed,
                    existingIdenticalNodeShmContentChanges=ignored,firstContentSQLiteOpener='fresh-reader',
                    parentSQLiteAfterWriter=0,actualHostExecuted=False)
                with self.subTest(boundary='actual-interpreter-fresh-reader'):
                    self.assertTrue(identities,'actual fresh reader must identify itself')
                    self.assertEqual(identities[0]['python'][:2],list(expected_python))
                    self.assertNotEqual(identities[0]['pid'],ready['writer']['identity']['pid'])
                    self.assertEqual(ready['writer']['identity']['python'][:2],list(sys.version_info[:2]))
                    opens=[row for row in events if row.get('event')=='reader-open-observation']
                    self.assertEqual(len(opens),1)
                    self.assertGreater(opens[0]['contentOpenCount'],0)
                    self.assertIsNotNone(opens[0]['firstContentOpen'])
                with self.subTest(boundary='retained-semantic-query-not-empty-or-refused'):
                    self.assertEqual(result.returncode,0,'actual fresh-first resolver/FTS must succeed')
                    self.assertEqual(len(resolved),1)
                    row=resolved[0]
                    self.assertEqual(row['endpoint'],p['retained']);self.assertEqual(row['pinnedGeneration'],p['generation'])
                    self.assertEqual(row['exclusionEpoch'],0);self.assertEqual(row['initialIssued'],0)
                    self.assertEqual(row['installerCalls'],0);self.assertGreater(row['ftsRows'],0)
                with self.subTest(boundary='logical-and-main-database-unchanged'):
                    for group in ('state','project'):
                        old,new=without_sqlite_aux(before[group],after[group])
                        self.assertEqual(old,new,'main DBs, metadata and optional sidecar files must be unchanged')
            finally:
                if child.poll() is None:
                    try:child.stdin.write('release\n');child.stdin.flush()
                    except (OSError,BrokenPipeError):pass
                try:child.communicate(timeout=5)
                except subprocess.TimeoutExpired:child.kill();child.communicate(timeout=5)

    def test_default_interpreter_fresh_first_closed_writer_retained_read(self):
        self.check_cold(sys.executable,sys.version_info[:2])

    def test_system_python39_fresh_first_closed_writer_retained_read(self):
        self.check_cold('/usr/bin/python3',(3,9))

if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1] in ('producer','writer','reader'):
        sys.exit(globals()[sys.argv[1]](Path(sys.argv[2])))
    unittest.main(verbosity=2)

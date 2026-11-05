#!/usr/bin/env python3
"""Managed base publication: real preflight, short visible cut and stamp CAS."""
from contextlib import contextmanager, ExitStack, nullcontext
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('publish_actual_writer',ROOT/'test/fixtures/laya-reader-write-checks.py')
existing=importlib.util.module_from_spec(spec);spec.loader.exec_module(existing)

def emit(case,**fields):print(json.dumps(dict(case=case,**fields),sort_keys=True),flush=True)
def normalized(sql):return ' '.join(sql.upper().split())
def publication_dml(sql):
    sql=normalized(sql)
    return (sql.startswith('UPDATE GENERATIONS SET STATE=') or sql.startswith('INSERT INTO RECEIPTS')
            or sql.startswith('INSERT INTO HEAD'))

MOVE_REVISION_CRASH=r'''
from contextlib import ExitStack,nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import importlib.util,json,os,signal,sys
root,payload=Path(sys.argv[1]),Path(sys.argv[2]);p=json.loads(payload.read_text())
sys.path.insert(0,str(root/'core'));os.environ['HOME']=p['home'];os.environ['MEMVANE_TEST_RUNTIME_ROOT']=p['runtime']
signal.alarm(30)
spec=importlib.util.spec_from_file_location('move_revision_base',root/'test/fixtures/laya-journal-reader-checks.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
import laya_project_dir_move as mover,laya_state_move as state_move,laya_reader_lease as lease
import laya_publication_revision as revisions
helper=base.JournalReaderChecks('runTest');case=SimpleNamespace(plugin=Path(p['plugin']),proof=p['proof'])
state,project=Path(p['state']),Path(p['project']);actual=revisions.record_head
def crash(*args,**kwargs):
    actual(*args,**kwargs)
    print(json.dumps({'event':'revision-inserted-before-commit','pid':os.getpid()}),flush=True)
    os.kill(os.getpid(),signal.SIGKILL)
with helper.full_authority(case),mock.patch.object(state_move,'_provider_lane',lambda:nullcontext()):
    intent=mover._load_intent(state,project);assert intent is not None
    with ExitStack() as locks:
        mover._locks(locks,project,state,mover._queue_path(project,intent))
        with lease.move_lifetime(state,project,intent=intent),mock.patch.object(revisions,'record_head',new=crash):
            mover._publish(intent)
raise RuntimeError('revision insertion checkpoint not reached')
'''

class PublishChecks(unittest.TestCase):
    def setUp(self):
        self.fixture=existing.WriteChecks('runTest')
        self.addCleanup(self.fixture.doCleanups);self.fixture.setUp()
        self.case,self.W=self.fixture.case,self.fixture.W

    def test_project_move_publication_advances_existing_base_revision_once(self):
        from laya_index_a import LayaIndexA
        from laya_publication_revision import ensure_base_baseline
        seed=self.fixture.seed
        with seed.prepared() as intent:
            self.W.prepare_windows(seed.installer)
            component=self.W.component_for(seed.installer,'base')
            index=LayaIndexA(self.case.db,coordination=component)
            try:
                decision=seed.reader.journal.select_route(str(self.case.project),action='recall')
                ensure_base_baseline(index,scope=decision['scopeId'],
                    profile_id=decision['vectorProfile']['profileId'],deadline=time.monotonic()+5)
            finally:
                index.close(deadline=time.monotonic()+2)
            with mock.patch('laya_publication_revision.record_head',
                            side_effect=RuntimeError('synthetic_move_revision_failure')):
                with self.assertRaisesRegex(RuntimeError,'synthetic_move_revision_failure'):
                    seed.mover._publish(intent)
            with seed.ro(self.case.db) as db:
                before=db.execute('SELECT gen FROM head WHERE scope=?',(decision['scopeId'],)).fetchone()[0]
                count=db.execute("SELECT count(*) FROM schema_meta WHERE key LIKE 'reader-revision:base:%'").fetchone()[0]
            self.assertEqual(before,intent['generation']['base'])
            self.assertEqual(count,1,'failed revision must roll back the move head')
            seed.mover._publish(intent)
            seed.mover._publish(intent)
            with seed.ro(self.case.db) as db:
                rows=db.execute("SELECT key,value FROM schema_meta WHERE key LIKE 'reader-revision:base:%' ORDER BY key").fetchall()
                head=db.execute('SELECT gen FROM head WHERE scope=?',(decision['scopeId'],)).fetchone()[0]
            self.assertEqual(len(rows),2,'the move head and revision must commit together; replay must not advance')
            self.assertEqual(rows[-1][0][-20:],'00000000000000000001')
            self.assertEqual(json.loads(rows[-1][1])['after'],head)
            self.assertEqual(head,intent['generation']['gen'])

    def test_project_move_sigkill_before_revision_commit_replays_once(self):
        from laya_index_a import LayaIndexA
        from laya_publication_revision import ensure_base_baseline
        import laya_state_move
        seed=self.fixture.seed
        with seed.prepared() as intent:
            self.W.prepare_windows(seed.installer)
            component=self.W.component_for(seed.installer,'base')
            index=LayaIndexA(self.case.db,coordination=component)
            try:
                decision=seed.reader.journal.select_route(str(self.case.project),action='recall')
                ensure_base_baseline(index,scope=decision['scopeId'],
                    profile_id=decision['vectorProfile']['profileId'],deadline=time.monotonic()+5)
            finally:index.close(deadline=time.monotonic()+2)
        payload=self.case.home/'move-revision-crash.json'
        payload.write_text(json.dumps(dict(home=str(self.case.home),runtime=os.environ['MEMVANE_TEST_RUNTIME_ROOT'],
            project=str(self.case.project),state=str(self.case.state),plugin=str(self.case.plugin),proof=self.case.proof)),encoding='utf8')
        payload.chmod(0o600)
        result=subprocess.run([sys.executable,'-B','-I','-c',MOVE_REVISION_CRASH,str(ROOT),str(payload)],
            encoding='utf8',capture_output=True,timeout=40)
        self.assertEqual(result.returncode,-signal.SIGKILL,result.stderr)
        self.assertEqual(json.loads(result.stdout)['event'],'revision-inserted-before-commit')
        with seed.ro(self.case.db) as db:
            head=db.execute('SELECT gen FROM head WHERE scope=?',(decision['scopeId'],)).fetchone()[0]
            before=db.execute("SELECT key FROM schema_meta WHERE key LIKE 'reader-revision:base:%' ORDER BY key").fetchall()
        self.assertEqual(head,intent['generation']['base'])
        self.assertEqual(len(before),1)
        with seed.authority(),mock.patch.object(laya_state_move,'_provider_lane',lambda:nullcontext()),ExitStack() as locks:
            current=seed.mover._load_intent(self.case.state,self.case.project)
            seed.mover._locks(locks,self.case.project,self.case.state,seed.mover._queue_path(self.case.project,current))
            with seed.lease.move_lifetime(self.case.state,self.case.project,intent=current):
                seed.mover._publish(current);seed.mover._publish(current)
        with seed.ro(self.case.db) as db:
            rows=db.execute("SELECT key,value FROM schema_meta WHERE key LIKE 'reader-revision:base:%' ORDER BY key").fetchall()
            head=db.execute('SELECT gen FROM head WHERE scope=?',(decision['scopeId'],)).fetchone()[0]
        self.assertEqual(len(rows),2)
        self.assertEqual(head,intent['generation']['gen'])
        self.assertEqual(json.loads(rows[-1][1])['after'],head)

    @contextmanager
    def candidate(self):
        import laya_index_a as A
        from laya_index_vector import LayaVectorIndex,qmd_markdown_title,verify_generation_seal
        with self.fixture.writer() as (writer,_component):
            scope=self.case.route['scopeId'];base=writer.db.execute('SELECT gen FROM head WHERE scope=?',(scope,)).fetchone()[0]
            docs=[('wiki/short-publish.md','# Short publication\nsynthetic full-vector candidate',True)]
            manifest=A._digest(scope,[(path,hashlib.sha256(body.encode()).hexdigest(),int(flag)) for path,body,flag in docs])
            options=dict(source_manifest=manifest,claim_digest='a'*64,claim_epoch=0,require_vector=True)
            stage_options={key:value for key,value in options.items() if key!='require_vector'}
            gen='short-visible-candidate';claim='short-visible-claim'
            self.assertEqual(writer.stage(gen,scope,docs,base=base,**stage_options),'prepared')
            vector=LayaVectorIndex(writer);profile=vector.register_profile('synthetic-short-publish','e'*64,2)
            for path,body,_flag in docs:
                vector.insert_chunk_set(hashlib.sha256(body.encode()).hexdigest(),qmd_markdown_title(body,path),profile,
                                        [dict(seq=0,pos=0,vector=[1.0,0.0])])
            vector.seal_generation(gen,profile)
            self.assertEqual(verify_generation_seal(writer,gen)['rows_count'],1)
            yield writer,dict(scope=scope,base=base,gen=gen,claim=claim,options=options)
    def publish(self,writer,p,**extra):return writer.publish(p['base'],p['gen'],p['claim'],**p['options'],**extra)
    def state(self,writer,p):
        return dict(head=writer.db.execute('SELECT gen FROM head WHERE scope=?',(p['scope'],)).fetchone(),
            candidate=writer.db.execute('SELECT scope,state,rev_epoch FROM generations WHERE gen=?',(p['gen'],)).fetchone(),
            receipt=writer.db.execute('SELECT scope,gen,source_manifest,claim_digest,claim_epoch FROM receipts WHERE claim=?',
                                     (p['claim'],)).fetchone())
    @contextmanager
    def seam(self,active,entries,inject=None):
        actual=self.W.write_transaction
        @contextmanager
        def transaction(owner,*,visible,deadline):
            if visible:
                entries.append(dict(deadline=deadline,enteredAt=time.monotonic()))
                if inject is not None:inject()
            previous=active[0]
            if visible:active[0]=True
            try:
                with actual(owner,visible=visible,deadline=deadline) as tx:yield tx
            finally:active[0]=previous
        with mock.patch.object(self.W,'write_transaction',new=transaction):yield
    def require_visible(self,entries):
        if not entries:
            self.fail('managed short-publish feature RED: actual sealed publication never entered visible write_transaction')
    def cut_probe(self,name,shared=False):
        # Independent kernel open-file description, without subprocess startup
        # consuming the production's short visible deadline inside a callback.
        fd=os.open(self.fixture.lock(name),os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            try:fcntl.flock(fd,(fcntl.LOCK_SH if shared else fcntl.LOCK_EX)|fcntl.LOCK_NB)
            except BlockingIOError:return False
            return True
        finally:os.close(fd)

    def test_actual_vector_verification_precedes_short_cut_and_publish_replay_is_exact(self):
        import laya_index_vector as V
        with self.candidate() as (writer,p):
            active=[False];entries=[];verified=[];visible_sql=[];dml=[]
            actual_verify=V.verify_generation_seal;cls=type(writer.db);actual_execute=cls.execute
            def verify(index,gen,*args,**kwargs):
                if index is writer and gen==p['gen']:
                    verified.append(dict(visible=active[0],cutSHAvailable=self.cut_probe('publish-cut.lock',True)))
                return actual_verify(index,gen,*args,**kwargs)
            def execute(db,sql,*args,**kwargs):
                result=actual_execute(db,sql,*args,**kwargs)
                if db is writer.db and publication_dml(sql):
                    dml.append(dict(visible=active[0],mutationEXAvailable=self.cut_probe('base-mutation.lock'),
                                    cutSHAvailable=self.cut_probe('publish-cut.lock',True)))
                return result
            writer.db.set_trace_callback(lambda sql:visible_sql.append(sql) if active[0] else None)
            try:
                with self.seam(active,entries),mock.patch.object(V,'verify_generation_seal',new=verify), \
                        mock.patch.object(cls,'execute',new=execute):
                    result=self.publish(writer,p);replayed=self.publish(writer,p)
            finally:writer.db.set_trace_callback(None)
            state=self.state(writer,p)
            forbidden=[sql for sql in visible_sql if 'SQLITE_MASTER' in normalized(sql)
                or ('GEN_INDEX' in normalized(sql) and 'WHERE GEN' not in normalized(sql))
                or ('COUNT(' in normalized(sql) and 'EXCLUSIONS' in normalized(sql))]
            emit('actual-short-visible-publish',result=result,replayed=replayed,state=state,
                verification=verified,dml=dml,visibleEntries=entries,forbiddenVisibleScans=forbidden)
            self.require_visible(entries)
            self.assertEqual(result,'published');self.assertEqual(replayed,'replayed')
            self.assertTrue(verified);self.assertTrue(all(not item['visible'] and item['cutSHAvailable'] for item in verified))
            self.assertEqual(len(dml),3);self.assertTrue(all(item['visible'] and not item['mutationEXAvailable']
                and not item['cutSHAvailable'] for item in dml));self.assertEqual(forbidden,[])
            self.assertEqual(state['head'],(p['gen'],));self.assertEqual(state['candidate'][1],'published')
            self.assertEqual(state['receipt'],(p['scope'],p['gen'],p['options']['source_manifest'],
                p['options']['claim_digest'],p['options']['claim_epoch']))

    def test_held_cut_shared_defers_before_publication_dml_preserving_prepared_candidate(self):
        with self.candidate() as (writer,p):
            before=self.state(writer,p);active=[False];entries=[];sql=[];result=None;error=None;busy=False
            writer.db.set_trace_callback(sql.append)
            try:
                with self.fixture.held('publish-cut.lock','SH'),self.seam(active,entries):
                    try:result=self.publish(writer,p)
                    except self.W.ReaderWindowBusy as exc:busy=True;error=str(exc)
            finally:writer.db.set_trace_callback(None)
            after=self.state(writer,p);dml=[line for line in sql if publication_dml(line)]
            emit('short-publish-cut-busy',error=error,result=result,visibleEntries=entries,actualPublicationDml=dml,
                 publicationStatePreserved=before==after)
            self.require_visible(entries)
            self.assertTrue(busy);self.assertIsNone(result);self.assertEqual(dml,[])
            self.assertEqual(after,before);self.assertFalse(writer.db.in_transaction)

    def stamp_case(self,same_connection):
        with self.candidate() as (writer,p):
            before=self.state(writer,p);active=[False];entries=[];injections=[];sql=[];error=None;result=None;busy=False
            def stamp():
                return (writer.db.execute('PRAGMA data_version').fetchone()[0],writer.db.total_changes,
                        writer.db.execute('PRAGMA schema_version').fetchone()[0])
            def inject():
                if injections:return
                old=stamp();key='private-publish-stamp-'+('same' if same_connection else 'other')
                if same_connection:
                    writer.db.execute('INSERT INTO schema_meta VALUES(?,?)',(key,'synthetic'));writer.db.commit()
                else:
                    db=sqlite3.connect(self.case.db.as_uri()+'?mode=rw',uri=True,isolation_level=None,timeout=.2)
                    try:db.execute('INSERT INTO schema_meta VALUES(?,?)',(key,'synthetic'));db.commit()
                    finally:db.close()
                injections.append(dict(sameConnection=same_connection,before=old,after=stamp()))
            writer.db.set_trace_callback(sql.append)
            try:
                with self.seam(active,entries,inject):
                    try:result=self.publish(writer,p)
                    except self.W.ReaderWindowBusy as exc:error=str(exc);busy=True
            finally:writer.db.set_trace_callback(None)
            after=self.state(writer,p);dml=[line for line in sql if publication_dml(line)]
            emit('short-publish-preflight-stamp-race',sameConnection=same_connection,injections=injections,
                visibleEntries=entries,error=error,result=result,actualPublicationDml=dml,statePreserved=before==after)
            self.require_visible(entries)
            if len(injections)!=1:raise RuntimeError('actual between-phases commit prerequisite missing')
            self.assertNotEqual(injections[0]['before'],injections[0]['after'])
            self.assertTrue(busy);self.assertEqual(error,'reader_publish_changed');self.assertIsNone(result)
            self.assertEqual(dml,[]);self.assertEqual(after,before)
            self.assertEqual(self.publish(writer,p),'published','clean retry must revalidate the actual changed database')
    def test_other_connection_commit_between_preflight_and_visible_cut_rejects_stamp_then_retries(self):
        self.stamp_case(False)
    def test_same_connection_dml_between_preflight_and_visible_cut_rejects_stamp_then_retries(self):
        self.stamp_case(True)

    def test_exclusion_revision_follows_head_and_duplicate_is_noop(self):
        import laya_publication_revision as revisions_api
        with self.candidate() as (writer,p):
            def revisions():
                return writer.db.execute("SELECT key,value FROM schema_meta WHERE key LIKE 'reader-revision:base:%' ORDER BY key").fetchall()
            self.assertEqual(self.publish(writer,p),'published')
            head_rows=revisions()
            self.assertEqual(writer.exclude(p['scope'],path='wiki/excluded.md'),'excluded')
            changed=revisions()
            self.assertEqual(len(changed),len(head_rows)+1,'visible exclusion must append a base revision')
            receipt=json.loads(changed[-1][1])
            self.assertEqual(receipt['component'],'base')
            self.assertEqual(receipt['before'],p['gen'])
            self.assertEqual(receipt['after'],p['gen'])
            self.assertEqual(writer.exclude(p['scope'],path='wiki/excluded.md'),'already_excluded')
            self.assertEqual(revisions(),changed)
            actual=revisions_api.record_exclusion
            def fail_after_insert(*args,**kwargs):
                actual(*args,**kwargs)
                raise RuntimeError('synthetic_exclusion_revision_failure')
            with mock.patch.object(revisions_api,'record_exclusion',side_effect=fail_after_insert):
                with self.assertRaisesRegex(RuntimeError,'synthetic_exclusion_revision_failure'):
                    writer.exclude(p['scope'],path='wiki/rollback.md')
            self.assertEqual(revisions(),changed)
            self.assertIsNone(writer.db.execute('SELECT 1 FROM exclusions WHERE scope=? AND value=?',
                                                (p['scope'],'wiki/rollback.md')).fetchone())

    def test_revision_insert_failure_rolls_back_head_and_replay_does_not_advance(self):
        import laya_publication_revision as revisions
        with self.candidate() as (writer,p):
            before=self.state(writer,p)
            def rows():
                return writer.db.execute("SELECT key,value FROM schema_meta WHERE key LIKE 'reader-revision:base:%' ORDER BY key").fetchall()
            original=rows();record=revisions.record_head
            def fail_after_insert(*args,**kwargs):
                record(*args,**kwargs)
                raise RuntimeError('synthetic_revision_failure_after_insert')
            with mock.patch.object(revisions,'record_head',side_effect=fail_after_insert):
                with self.assertRaisesRegex(RuntimeError,'synthetic_revision_failure_after_insert'):
                    self.publish(writer,p)
            self.assertEqual(self.state(writer,p),before)
            self.assertEqual(rows(),original)
            self.assertEqual(self.publish(writer,p),'published')
            committed=rows()
            self.assertEqual(len(committed),len(original)+1)
            receipt=json.loads(committed[-1][1])
            self.assertEqual((receipt['before'],receipt['after']),(p['base'],p['gen']))
            self.assertEqual(self.publish(writer,p),'replayed')
            self.assertEqual(rows(),committed)
            emit('actual-publication-revision-atomicity',rollbackPreserved=True,replayUnchanged=True,
                 before=receipt['before'],after=receipt['after'])

    def test_actual_receipt_and_head_faults_rollback_all_publication_state(self):
        from laya_index_a import InjectedFault
        with self.candidate() as (writer,p):
            before=self.state(writer,p);active=[False];entries=[]
            for fault in ('after_receipt','after_head'):
                with self.subTest(fault=fault):
                    with self.seam(active,entries):
                        with self.assertRaises(InjectedFault):self.publish(writer,p,fault_at=fault)
                    after=self.state(writer,p)
                    emit('actual-short-publication-fault',fault=fault,statePreserved=before==after,
                         candidate=after['candidate'],receipt=after['receipt'],head=after['head'],visibleEntries=len(entries))
                    self.assertEqual(after,before);self.assertFalse(writer.db.in_transaction)
            self.require_visible(entries)
            self.assertTrue(self.fixture.probe('base-mutation.lock'));self.assertTrue(self.fixture.probe('publish-cut.lock'))
            self.assertEqual(self.publish(writer,p),'published')

    def test_replay_from_renamed_open_database_cannot_ack_unpublished_replacement(self):
        from laya_index_a import LayaIndexA
        def files(path):
            values={}
            for suffix in ('','-wal','-shm','-journal'):
                file=Path(str(path)+suffix)
                if not os.path.lexists(file):values[suffix]=None
                else:
                    info=file.lstat()
                    values[suffix]=(file.read_bytes(),info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_nlink)
            return values
        with self.candidate() as (writer,p):
            alternate=self.case.home/'unpublished-replacement.sqlite'
            fd=os.open(alternate,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);os.close(fd)
            destination=sqlite3.connect(alternate.as_uri()+'?mode=rw',uri=True,isolation_level=None)
            try:writer.db.backup(destination)
            finally:destination.close()
            other=LayaIndexA(alternate);moved_original=[];moved_replacement=[]
            try:
                prepared=self.state(other,p)
                self.assertEqual(prepared['candidate'][1],'prepared');self.assertIsNone(prepared['receipt'])
                self.assertEqual(self.publish(writer,p),'published')
                for suffix in ('','-wal','-shm'):
                    source=Path(str(self.case.db)+suffix);saved=self.case.home/('published-original.sqlite'+suffix)
                    source.rename(saved);moved_original.append((saved,source))
                for suffix in ('','-wal','-shm'):
                    source=Path(str(alternate)+suffix);target=Path(str(self.case.db)+suffix)
                    source.rename(target);moved_replacement.append((target,source))
                before=files(self.case.db);result=None;error=None
                try:result=self.publish(writer,p)
                except self.W.ReaderWindowBusy as exc:error=str(exc)
                after=files(self.case.db)
                emit('replay-current-database-binding',result=result,error=error,
                    replacementPrepared=prepared['candidate'][1],replacementReceipt=prepared['receipt'],
                    replacementHead=prepared['head'],replacementFilesUnchanged=before==after)
                self.assertEqual(error,'reader_publish_changed');self.assertIsNone(result)
                self.assertTrue(before==after)
            finally:
                for source,target in reversed(moved_replacement):source.rename(target)
                for source,target in reversed(moved_original):source.rename(target)
                other.close()

if __name__=='__main__':unittest.main(verbosity=2)

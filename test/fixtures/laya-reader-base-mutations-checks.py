#!/usr/bin/env python3
"""Nine private base/vector mutators use mutation admission, without publish."""
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('base_mutations_real_writer',ROOT/'test/fixtures/laya-reader-write-checks.py')
write_fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(write_fixture)

def emit(case,**values):print(json.dumps(dict(case=case,**values),sort_keys=True),flush=True)
def hashed(body):return hashlib.sha256(body.encode('utf8')).hexdigest()

class StreamFailure(RuntimeError):pass

class BaseMutationChecks(unittest.TestCase):
    def setUp(self):
        self.fixture=write_fixture.WriteChecks('runTest')
        self.addCleanup(self.fixture.doCleanups);self.fixture.setUp()
        self.case,self.W=self.fixture.case,self.fixture.W
    @staticmethod
    def counts(db):
        names=[row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {name:db.execute('SELECT count(*) FROM "'+name.replace('"','""')+'"').fetchone()[0] for name in names}
    def prepare(self,writer):
        import laya_index_a as A
        from laya_index_vector import LayaVectorIndex,qmd_markdown_title,source_evidence
        vector=LayaVectorIndex(writer);scope=self.case.route['scopeId']
        base=writer.db.execute('SELECT gen FROM head WHERE scope=?',(scope,)).fetchone()[0]
        profile=vector.register_profile('synthetic-private-admission','a'*64,2)
        docs=[('wiki/private-'+str(n)+'.md','# Private '+str(n)+'\nsynthetic private vector body '+str(n),True)
              for n in range(1,5)]
        for _path,body,_flag in docs:writer.ensure_content(hashed(body),body)
        titles=[qmd_markdown_title(body,path) for path,body,_flag in docs]
        vector.insert_chunk(hashed(docs[1][1]),profile,0,0,[1.0,0.0],
            source_evidence(hashed(docs[1][1]),titles[1]),title=titles[1])
        chunk_rows=[dict(seq=0,pos=0,vector=[1.0,0.0])]
        writer.stage('private-vector-target',scope,[docs[3]])
        vector.insert_chunk_set(hashed(docs[3][1]),titles[3],profile,chunk_rows)
        stream_docs=[('wiki/private-stream.md','# Private stream\nsynthetic streamed body',True)]
        manifest=A._digest(scope,[(path,hashed(body),int(flag)) for path,body,flag in stream_docs])
        stream_options=dict(base=base,source_manifest=manifest,claim_digest='b'*64,claim_epoch=0)
        release_token=writer.pin(scope)[0];pins=[]
        def pin():
            result=writer.pin(scope);pins.append(result[0]);return result
        actions=[
            ('stage',lambda:writer.stage('private-admission-stage',scope,[docs[0]])),
            ('stage_stream',lambda:writer.stage_stream('private-admission-stream',scope,iter(stream_docs),**stream_options)),
            ('pin',pin),('release',lambda:writer.release(release_token)),
            ('register_profile',lambda:vector.register_profile('synthetic-new-admission-profile','c'*64,2)),
            ('insert_chunk',lambda:vector.insert_chunk(hashed(docs[0][1]),profile,0,0,[1.0,0.0],
                source_evidence(hashed(docs[0][1]),titles[0]),title=titles[0])),
            ('seal_chunk_set',lambda:vector.seal_chunk_set(hashed(docs[1][1]),titles[1],profile,1)),
            ('insert_chunk_set',lambda:vector.insert_chunk_set(hashed(docs[2][1]),titles[2],profile,chunk_rows)),
            ('seal_generation',lambda:vector.seal_generation('private-vector-target',profile)),
        ]
        return dict(actions=actions,vector=vector,profile=profile,docs=docs,titles=titles,chunk_rows=chunk_rows,
            stream_docs=stream_docs,stream_options=stream_options,scope=scope,base=base,pins=pins,release_token=release_token)
    def cleanup_pins(self,writer,prepared):
        for token in [prepared['release_token'],*prepared['pins']]:writer.release(token)

    def test_nine_valid_mutators_wait_before_begin_under_one_independent_mutation_ex(self):
        with self.fixture.writer() as (writer,_component):
            prepared=self.prepare(writer)
            try:
                # Nine public calls each have a finite two-second admission
                # budget; the holder must outlive their cumulative budget.
                with mock.patch.object(write_fixture,'LOCK_CHILD',write_fixture.LOCK_CHILD.replace(
                        'signal.alarm(20)','signal.alarm(60)')),self.fixture.held('base-mutation.lock','EX'):
                    for name,action in prepared['actions']:
                        with self.subTest(api=name):
                            before=self.counts(writer.db);sql=[];error=None;busy=False
                            writer.db.set_trace_callback(sql.append)
                            try:action()
                            except Exception as exc:
                                error=type(exc).__name__+':'+str(exc);busy=isinstance(exc,self.W.ReaderWindowBusy)
                            finally:writer.db.set_trace_callback(None)
                            after=self.counts(writer.db)
                            begins=[line for line in sql if line.strip().upper().startswith('BEGIN')]
                            changed={key:[before.get(key),after.get(key)] for key in sorted(set(before)|set(after))
                                     if before.get(key)!=after.get(key)}
                            emit('private-mutator-admission',api=name,error=error,readerWindowBusy=busy,
                                 begin=begins,rowCountChanges=changed,transactionActive=writer.db.in_transaction)
                            # Separate subtests keep all nine actual effects in
                            # the RED log instead of stopping at the first API.
                            with self.subTest(boundary='actual-mutation-busy'):self.assertTrue(busy)
                            with self.subTest(boundary='before-begin'):self.assertEqual(begins,[])
                            with self.subTest(boundary='no-table-row-changes'):self.assertEqual(changed,{})
                            self.assertFalse(writer.db.in_transaction)
            finally:self.cleanup_pins(writer,prepared)

    def test_actual_private_stage_vector_seals_and_replays_hold_mutation_without_visible_cut(self):
        from laya_index_vector import verify_generation_seal
        with self.fixture.writer() as (writer,_component):
            prepared=self.prepare(writer);current=[None];observed={};results={}
            cls=type(writer.db);actual=cls.execute
            def execute(db,sql,*args,**kwargs):
                result=actual(db,sql,*args,**kwargs)
                label=current[0]
                if (db is writer.db and label is not None and label not in observed
                        and sql.lstrip().upper().startswith(('INSERT','UPDATE','DELETE','CREATE'))):
                    observed[label]=dict(transactionActive=db.in_transaction,
                        mutationExclusiveAvailable=self.fixture.probe('base-mutation.lock','EX'),
                        publishSharedAvailable=self.fixture.probe('publish-cut.lock','SH'))
                return result
            try:
                with mock.patch.object(cls,'execute',new=execute):
                    for name,action in prepared['actions']:
                        current[0]=name;results[name]=action();current[0]=None
                self.assertEqual(results['stage'],'prepared');self.assertEqual(results['stage_stream'],'prepared')
                self.assertEqual(results['seal_chunk_set'],1);self.assertEqual(results['insert_chunk_set'],1)
                sealed=verify_generation_seal(writer,'private-vector-target')
                self.assertEqual(sealed['profile_id'],prepared['profile']);self.assertEqual(sealed['rows_count'],1)
                cached=[];writer.db.set_trace_callback(cached.append)
                try:
                    self.assertEqual(prepared['vector'].insert_chunk_set(hashed(prepared['docs'][2][1]),
                        prepared['titles'][2],prepared['profile'],prepared['chunk_rows']),1)
                finally:writer.db.set_trace_callback(None)
                commits=[s for s in cached if s.strip().upper()=='COMMIT']
                replay_sql=[];writer.db.set_trace_callback(replay_sql.append)
                try:
                    stage_replay=writer.stage('private-admission-stage',prepared['scope'],[prepared['docs'][0]])
                    stream_replay=writer.stage_stream('private-admission-stream',prepared['scope'],
                        iter(prepared['stream_docs']),**prepared['stream_options'])
                finally:writer.db.set_trace_callback(None)
                emit('actual-private-vector-lifecycle',observations=observed,completedApis=sorted(results),
                    verifiedSealRows=sealed['rows_count'],cachedCommitCount=len(commits),
                    stageReplay=stage_replay,streamReplay=stream_replay,replayCommitCount=sum(s.strip().upper()=='COMMIT' for s in replay_sql))
                self.assertEqual(set(observed),{name for name,_action in prepared['actions']})
                for name,item in observed.items():
                    with self.subTest(api=name):
                        self.assertTrue(item['transactionActive']);self.assertFalse(item['mutationExclusiveAvailable'])
                        self.assertTrue(item['publishSharedAvailable'])
                self.assertEqual(len(commits),1,'cached vector return is committed exactly once by its outer transaction')
                self.assertEqual(stage_replay,'already_staged');self.assertEqual(stream_replay,'already_staged')
                self.assertFalse(any(s.strip().upper()=='COMMIT' for s in replay_sql),'stage replay rolls back instead of committing')
                self.assertEqual(sum(s.strip().upper()=='ROLLBACK' for s in replay_sql),2)
                self.assertEqual(writer.db.execute('SELECT gen FROM head WHERE scope=?',(prepared['scope'],)).fetchone(),(prepared['base'],))
            finally:self.cleanup_pins(writer,prepared)

    def test_real_stage_stream_mid_generator_failure_rolls_back_dynamic_fts_then_retry_replays(self):
        import laya_index_a as A
        with self.fixture.writer() as (writer,_component):
            scope=self.case.route['scopeId'];base=writer.db.execute('SELECT gen FROM head WHERE scope=?',(scope,)).fetchone()[0]
            docs=[('wiki/stream-rollback-a.md','# Actual stream A\nprivate rollback one',True),
                  ('wiki/stream-rollback-b.md','# Actual stream B\nprivate rollback two',True)]
            options=dict(base=base,source_manifest=A._digest(scope,[(p,hashed(b),int(f)) for p,b,f in docs]),
                         claim_digest='d'*64,claim_epoch=0)
            before=self.counts(writer.db)
            schema_before=writer.db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
            def broken():
                yield docs[0]
                raise StreamFailure('synthetic_mid_real_stage_stream_generator')
            with self.assertRaisesRegex(StreamFailure,'synthetic_mid_real_stage_stream_generator'):
                writer.stage_stream('private-stream-rollback',scope,broken(),**options)
            after=self.counts(writer.db)
            schema_after=writer.db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
            emit('actual-stream-rollback',countsUnchanged=before==after,dynamicSchemaUnchanged=schema_before==schema_after,
                transactionActive=writer.db.in_transaction,mutationAvailable=self.fixture.probe('base-mutation.lock'))
            self.assertEqual(after,before);self.assertTrue(schema_after==schema_before,'transaction rollback must remove dynamic generation FTS')
            self.assertFalse(writer.db.in_transaction);self.assertTrue(self.fixture.probe('base-mutation.lock'))
            self.assertIsNone(writer.db.execute('SELECT 1 FROM generations WHERE gen=?',('private-stream-rollback',)).fetchone())
            self.assertEqual(writer.db.execute('SELECT count(*) FROM membership WHERE gen=?',('private-stream-rollback',)).fetchone(),(0,))
            self.assertEqual(writer.stage_stream('private-stream-rollback',scope,iter(docs),**options),'prepared')
            self.assertTrue(writer._generation_fts_matches('private-stream-rollback'))
            self.assertEqual(writer.stage_stream('private-stream-rollback',scope,iter(docs),**options),'already_staged')
            self.assertEqual(writer.db.execute('SELECT gen FROM head WHERE scope=?',(scope,)).fetchone(),(base,))

if __name__=='__main__':unittest.main(verbosity=2)

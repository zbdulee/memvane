#!/usr/bin/env python3
"""Actual preserve/retire/backfill writer admission and source/cursor races."""
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('remaining_sidecar_helpers',ROOT/'test/fixtures/laya-reader-sidecar-write-checks.py')
existing=importlib.util.module_from_spec(spec);spec.loader.exec_module(existing)

class RemainingChecks(existing.SidecarWriteChecks):
    def require(self,name):
        self.assertIn('coordination',inspect.signature(getattr(self.I,name)).parameters,
            'T3 feature RED after actual source/head/vector/sidecar proof: '+name)
    def draft(self,runtime,row):
        from laya_index_a import _digest
        from laya_host_response import _verified_wiki_row
        from laya_index_vector import LayaVectorIndex,qmd_markdown_title,verify_generation_seal
        decision={'projectRoot':self.identity['projectRoot'],'collections':self.identity['collections'],
            'collectionRoots':{item['name']:item['sourceRoot'] for item in self.identity['collections']}}
        path,old_hash,old_title,old_body,profile=row
        self.assertTrue(_verified_wiki_row({'path':path,'body':old_body},decision,current_source=False))
        body=old_body.replace('status: verified','status: generated',1)
        self.assertNotEqual(body,old_body)
        self.assertFalse(_verified_wiki_row({'path':path,'body':body},decision,current_source=False))
        base=runtime.index.db.execute('SELECT gen FROM head WHERE scope=?',(runtime.scope,)).fetchone()[0]
        self.assertEqual(verify_generation_seal(runtime.index,base)['profile_id'],profile)
        gen='sidecar-draft-candidate';vectors=LayaVectorIndex(runtime.index)
        chunks=vectors.cached_chunk_set(old_hash,old_title,profile);self.assertTrue(chunks)
        manifest=_digest(runtime.scope,[(path,hashlib.sha256(body.encode()).hexdigest(),1)])
        self.draft_job=runtime.queue.enqueue_next(runtime.scope,gen,manifest,base)
        self.draft_proof={key:self.draft_job[key] for key in ('source_manifest','claim_digest','claim_epoch')}
        self.assertEqual(runtime.index.stage(gen,runtime.scope,[(path,body,True)],base=base,**self.draft_proof),'prepared')
        vectors.insert_chunk_set(hashlib.sha256(body.encode()).hexdigest(),qmd_markdown_title(body,path),profile,
            [{key:chunk[key] for key in ('seq','pos','vector')} for chunk in chunks])
        vectors.seal_generation(gen,profile)
        return base,gen
    def missing(self,runtime):
        target=self.rows()[0][0];path=target[1]
        source=Path(runtime.catalog.roots[path.split('/',1)[0]])/path.split('/',1)[1]
        body=source.read_text();source.unlink()
        self.assertEqual(runtime.catalog.request_scan()['status'],'started')
        self.assertEqual(runtime.catalog.step(max_files=100,max_stats=100)['status'],'complete')
        self.assertIsNone(runtime.catalog.lookup(runtime.catalog.require_complete(),path))
        return source,body
    def options(self,**extra):
        return dict(deadline=time.monotonic()+10,clock=time.monotonic,before_publish=lambda:None,
            should_yield=lambda:False,require_verified=False,**extra)
    def test_preserve_visible_cut_keeps_actual_old_verified_document_unwritten(self):
        with self.opened() as (runtime,_snapshot,row,component):
            base,gen=self.draft(runtime,row);self.require('preserve_previous_head');before=self.rows();error=None;result=None
            with self.held('publish-cut.lock',shared=True):
                try:result=self.I.preserve_previous_head(runtime.index,self.identity,base,gen,coordination=component)
                except Exception as exc:error=str(exc)
            self.assertIsNotNone(error);self.assertIsNone(result);self.assertTrue(before==self.rows())
            self.assertEqual(self.I.preserve_previous_head(runtime.index,self.identity,base,gen,coordination=component),1)
            print(json.dumps(dict(case='sidecar-preserve-cut',error=error,actualVerifiedOld=True,actualSealedDraft=True)),flush=True)
    def test_preserve_head_commit_after_preparation_cannot_pin_old_rows(self):
        with self.opened() as (runtime,_snapshot,row,component):
            base,gen=self.draft(runtime,row);self.require('preserve_previous_head');before=self.rows()
            setup=self.I._setup;injected=[];error=None;result=None
            def moved(*args,**kwargs):
                value=setup(*args,**kwargs)
                if getattr(args[0],'_window_owner',None) is not None and not injected:
                    injected.append(runtime.index.publish(base,gen,self.draft_job['claim'],require_vector=True,**self.draft_proof))
                return value
            with mock.patch.object(self.I,'_setup',moved):
                try:result=self.I.preserve_previous_head(runtime.index,self.identity,base,gen,coordination=component)
                except Exception as exc:error=str(exc)
            print(json.dumps(dict(case='sidecar-preserve-head-race',injected=injected,error=error,result=result,rowsUnchanged=before==self.rows())),flush=True)
            self.assertEqual(injected,['published']);self.assertIsNotNone(error);self.assertIsNone(result);self.assertTrue(before==self.rows())
    def test_retire_visible_cut_keeps_actual_missing_member_until_retry(self):
        with self.opened() as (runtime,_snapshot,_row,component):
            self.missing(runtime);self.require('retire_missing_documents');before=self.rows();error=None;result=None
            with self.held('publish-cut.lock',shared=True):
                try:result=self.I.retire_missing_documents(runtime,self.identity,before_write=lambda:None,coordination=component)
                except Exception as exc:error=str(exc)
            self.assertIsNotNone(error);self.assertIsNone(result);self.assertTrue(before==self.rows())
            self.assertEqual(self.I.retire_missing_documents(runtime,self.identity,before_write=lambda:None,coordination=component),1)
            print(json.dumps(dict(case='sidecar-retire-cut',error=error,actualMissingMember=True)),flush=True)
    def test_retire_new_catalog_restores_member_before_delete_and_prevents_stale_retirement(self):
        with self.opened() as (runtime,_snapshot,_row,component):
            source,body=self.missing(runtime);self.require('retire_missing_documents');before=self.rows();injected=[];error=None;result=None
            def restored():
                if injected:return
                source.write_text(body)
                self.assertEqual(runtime.catalog.request_scan()['status'],'started')
                injected.append(runtime.catalog.step(max_files=100,max_stats=100)['status'])
            try:result=self.I.retire_missing_documents(runtime,self.identity,before_write=restored,coordination=component)
            except Exception as exc:error=str(exc)
            print(json.dumps(dict(case='sidecar-retire-catalog-race',injected=injected,error=error,result=result,rowsUnchanged=before==self.rows())),flush=True)
            self.assertEqual(injected,['complete']);self.assertIsNotNone(error);self.assertIsNone(result);self.assertTrue(before==self.rows())
    def test_backfill_mutation_ex_blocks_cursor_write_then_nested_publish_is_coordinated(self):
        with self.opened() as (runtime,_snapshot,_row,component):
            self.require('backfill_cached');before=self.rows();error=None;result=None
            with self.held('sidecar-mutation.lock'):
                try:result=self.I.backfill_cached(runtime,self.identity,self.identity['vectorProfileId'],coordination=component,**self.options())
                except Exception as exc:error=str(exc)
            self.assertIsNotNone(error);self.assertIsNone(result);self.assertTrue(before==self.rows())
            publish=self.I.publish_document;seen=[]
            def nested(*args,**kwargs):seen.append(kwargs.get('coordination') is component);return publish(*args,**kwargs)
            with mock.patch.object(self.I,'publish_document',nested):
                result=self.I.backfill_cached(runtime,self.identity,self.identity['vectorProfileId'],coordination=component,**self.options())
            self.assertEqual(result['state'],'complete');self.assertTrue(seen);self.assertTrue(all(seen))
            self.assertEqual(len(self.rows()[0]),3)
            print(json.dumps(dict(case='sidecar-backfill-mutation-and-nested',error=error,coordinated=seen)),flush=True)
    def test_backfill_document_commit_then_new_scan_rejects_old_cursor_advance(self):
        with self.opened() as (runtime,_snapshot,_row,component):
            self.require('backfill_cached');publish=self.I.publish_document;injected=[];new_scan=[]
            def new_scan_after_document(*args,**kwargs):
                result=publish(*args,**kwargs)
                if not injected:
                    injected.append(result['status']);runtime.catalog.request_scan()
                    self.assertEqual(runtime.catalog.step(max_files=100,max_stats=100)['status'],'complete')
                    new_scan.append(runtime.catalog.require_complete().scan_id)
                    options=self.options();options['should_yield']=lambda:True
                    self.assertEqual(self.I.backfill_cached(runtime,self.identity,self.identity['vectorProfileId'],
                        coordination=component,**options)['state'],'deferred')
                return result
            with mock.patch.object(self.I,'publish_document',new_scan_after_document):
                result=self.I.backfill_cached(runtime,self.identity,self.identity['vectorProfileId'],coordination=component,**self.options())
            with self.seed.ro(self.fixture.sidecar) as db:cursor=db.execute('SELECT scan_id,cursor FROM cached_backfill WHERE id=1').fetchone()
            print(json.dumps(dict(case='sidecar-backfill-new-scan-cursor-cas',injected=injected,result=result,cursor=cursor,newScan=new_scan)),flush=True)
            self.assertTrue(injected);self.assertEqual(result['state'],'pending');self.assertEqual(cursor,(new_scan[0],''))

    def test_backfill_catalog_changes_after_current_check_without_new_backfill_preserves_old_cursor(self):
        with self.opened() as (runtime,snapshot,_row,component):
            self.require('backfill_cached')
            from laya_source_catalog import LayaSourceCatalog
            other=LayaSourceCatalog(runtime.catalog.path,runtime.catalog.roots,runtime.scope,wiki_only=True,
                coordination=runtime.catalog._connection_owner.component)
            publish=self.I.publish_document;require=runtime.catalog.require_current
            committed=[];injected=[]
            def published(*args,**kwargs):
                result=publish(*args,**kwargs);committed.append(result['status']);return result
            def checked(value):
                result=require(value)
                if committed and not injected:
                    self.assertEqual(other.request_scan()['status'],'started')
                    self.assertEqual(other.step(max_files=100,max_stats=100)['status'],'complete')
                    injected.append(other.require_complete().scan_id)
                return result
            try:
                with mock.patch.object(self.I,'publish_document',published),mock.patch.object(runtime.catalog,'require_current',checked):
                    error=None;result=None
                    try:result=self.I.backfill_cached(runtime,self.identity,self.identity['vectorProfileId'],coordination=component,**self.options())
                    except Exception as exc:error=str(exc)
                with self.seed.ro(self.fixture.sidecar) as db:cursor=db.execute('SELECT scan_id,cursor FROM cached_backfill WHERE id=1').fetchone()
                print(json.dumps(dict(case='sidecar-backfill-current-check-gap',committed=committed,injected=injected,
                    error=error,result=result,cursor=cursor,previousScan=snapshot.scan_id,newBackfillExecuted=False)),flush=True)
                self.assertTrue(injected);self.assertEqual(cursor,(snapshot.scan_id,''))
                self.assertIsNone(error);self.assertEqual(result['state'],'pending')
            finally:other.close(deadline=time.monotonic()+1)

if __name__=='__main__':
    names=[name for name in RemainingChecks.__dict__ if name.startswith('test_')]
    if len(sys.argv)>1:
        if any(name not in names for name in sys.argv[1:]):raise ValueError('unknown test method')
        names=sys.argv[1:]
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(RemainingChecks(name) for name in names))
    raise SystemExit(not result.wasSuccessful())

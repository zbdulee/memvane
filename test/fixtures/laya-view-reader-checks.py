"""Pinned candidate lexical reader: no operational route or source reread."""
import copy
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest import mock

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('generation_fixture',HERE/'laya-view-generations-checks.py')
helpers=importlib.util.module_from_spec(spec);spec.loader.exec_module(helpers)


class ReaderChecks(helpers.GenerationCase):
    pair=helpers.GenerationChecks.pair
    def published(self,*,body=b'# Alpha\nhello original snapshot\n'):
        store,repository,h=self.seeded()
        m=self.snapshot(store,h,body=body)
        gen,base=self.prepared(store,h,m)
        store.publish_generation(h,gen,expected_head=base)
        return store,repository,h,gen

    def add_member(self,store,h,m,path,body,title):
        p=self.root/'.memvane/wiki'/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(body)
        blob=store.ingest_card(h.worktree_id,path)
        att=store.put_attestation(dict(blob_sha=blob,verification_kind='compiled',
            engine_identity='synthetic:codex',policy_digest=h.policy_digest,
            receipt=dict(schema='memvane-card-evidence-v1',verdict='verified',result_digest='e'*64,prior_trust=None),
            sources=[dict(fingerprint_id=m['sources'][0]['fingerprint_id'],spans=[dict(start=0,end=4)])]))
        m['members'].append(dict(path=path,blob_sha=blob,display_title=title,trust_digest=att))

    def test_01_cold_view_has_preparing_without_other_view_fallback(self):
        store,main,linked,a,b=self.pair('cold')
        ga,ea=self.prepared(store,a,self.snapshot(store,a,root=main))
        store.publish_generation(a,ga,expected_head=ea)
        error=self.refused(store.pin_view,b)
        self.assertEqual(error.reason,'view_preparing')
        self.assertIsNone(store.head(b)['gen_id'])

    def test_02_candidate_results_state_their_evidence_limit(self):
        store,_,h,gen=self.published();pin=store.pin_view(h)
        rows=store.search(pin,'hello',10)
        self.assertEqual(len(rows),1)
        row=rows[0]
        self.assertEqual((row['view_id'],row['gen_id'],row['path']),(h.view_id,gen,'cards/a.md'))
        self.assertEqual(row['evidence'],'commitment-only')
        self.assertIs(row['owner_verified'],False)
        self.assertIn('original snapshot',row['body'])
        self.assertEqual(row['snapshot_state'],'published_snapshot')
        store.release(pin)

    def test_03_pin_survives_same_view_head_swap_without_mixed_bodies(self):
        store,_,h,first=self.published();old=store.pin_view(h)
        m=self.snapshot(store,h,scan_id='2'*32,body=b'# Beta\nhello replacement snapshot\n')
        gen,base=self.prepared(store,h,m);store.publish_generation(h,gen,expected_head=base)
        new=store.pin_view(h)
        a,b=store.search(old,'hello',10),store.search(new,'hello',10)
        self.assertEqual({r['gen_id'] for r in a},{first})
        self.assertEqual({r['gen_id'] for r in b},{gen})
        self.assertIn('original snapshot',a[0]['body'])
        self.assertIn('replacement snapshot',b[0]['body'])
        self.assertNotEqual(first,gen)

    def test_04_arbitrary_released_and_cross_store_pins_refused(self):
        store,repository,h,_=self.published();pin=store.pin_view(h)
        forged=replace(pin,gen_id='g-'+'0'*32+'-b'+'0'*64)
        for token in [forged,object(),{},None]:
            with self.subTest(kind=type(token).__name__):self.refused(store.search,token,'hello',1)
        other=self.reopen(repository)
        self.refused(other.search,pin,'hello',1)
        store.release(pin)
        self.refused(store.search,pin,'hello',1)

    def test_05_close_invalidates_all_pins_with_bounded_error(self):
        store,_,h,_=self.published();pin=store.pin_view(h)
        store.close()
        self.refused(store.search,pin,'hello',1)

    def test_06_view_prefix_and_exclusion_filter_before_limit(self):
        store,_,h=self.seeded();m=self.snapshot(store,h)
        self.add_member(store,h,m,'private/hello.md',b'# hello hello\nhello hello hello\n','hello hello')
        self.add_member(store,h,m,'public/answer.md',b'# Public\nhello answer\n','Public')
        gen,base=self.prepared(store,h,m);store.publish_generation(h,gen,expected_head=base)
        pin=store.pin_view(h)
        for kwargs in [dict(path_prefixes=['public']),dict(excluded_paths=['private/hello.md'])]:
            with self.subTest(kwargs=kwargs):
                rows=store.search(pin,'hello',1,**kwargs)
                self.assertEqual([r['path'] for r in rows],['public/answer.md'])

    def test_07_zero_held_and_changed_source_cards_never_search(self):
        for mode in ['zero','held','changed']:
            with self.subTest(mode=mode):
                root=self.project(mode);store,_,h=self.seeded(root)
                m=self.snapshot(store,h,root=root,zero=mode=='zero',presence='held_missing' if mode=='held' else 'present')
                if mode=='changed':
                    old=store.fingerprint(m['sources'][0]['fingerprint_id'])
                    m['sources'][0]['fingerprint_id']=store.put_fingerprint(dict(old,source_sha='d'*64))
                gen,base=self.prepared(store,h,m);store.publish_generation(h,gen,expected_head=base)
                self.assertEqual(store.search(store.pin_view(h),'wiki',10),[])

    def test_08_queries_reread_neither_source_nor_current_wiki_body(self):
        store,_,h,_=self.published();pin=store.pin_view(h)
        original=self.mod._regular
        def read(root,path,limit):
            if path in ['cards/a.md','notes/src.md']:raise AssertionError('query attempted source/wiki reread')
            return original(root,path,limit)
        original_open=os.open
        def opened(path,*args,**kwargs):
            value=os.fspath(path)
            if value in ['a.md','src.md','cards/a.md','notes/src.md']:
                raise AssertionError('query attempted source/wiki body open')
            return original_open(path,*args,**kwargs)
        with mock.patch.object(self.mod,'_regular',side_effect=read), mock.patch.object(os,'open',side_effect=opened):
            self.assertEqual(len(store.search(pin,'hello',1)),1)

    def test_09_settings_switch_during_match_discards_result(self):
        store,_,h,_=self.published();pin=store.pin_view(h);changed=[]
        def trace(sql):
            if ' MATCH ' in sql and not changed:
                changed.append(True)
                (self.root/'.memvane/settings.json').write_text(json.dumps(dict(helpers.helpers.SETTINGS,skipPaths=['notes'])))
        store._db.set_trace_callback(trace)
        try:self.refused(store.search,pin,'hello',10)
        finally:store._db.set_trace_callback(None)
        self.assertTrue(changed)

    def test_10_query_input_and_deadline_fail_bounded_and_recover(self):
        store,_,h,_=self.published();pin=store.pin_view(h)
        for query,limit,kwargs in [('hello',True,{}),('',1,{}),('hello',0,{}),('hello',101,{}),
                ('x'*4097,1,{}),('hello',1,dict(deadline_seconds=float('nan'))),
                ('hello',1,dict(deadline_seconds=0)),('hello',1,dict(path_prefixes='../bad')),
                ('hello',1,dict(excluded_paths=['../bad']))]:
            with self.subTest(query_type=type(query).__name__,limit=limit,keys=list(kwargs)):
                self.refused(store.search,pin,query,limit,**kwargs)
        error=self.refused(store.search,pin,'"SECRET',1)
        self.assertNotIn('SECRET',str(error))
        self.refused(store.search,pin,'hello',1,deadline_seconds=1e-12)
        self.assertEqual(len(store.search(pin,'hello',1)),1)

    def test_11_physical_tamper_is_rejected_before_match_compilation(self):
        store,_,h,gen=self.published();pin=store.pin_view(h)
        ordinal=store._db.execute('SELECT ordinal FROM generations WHERE gen_id=?',(gen,)).fetchone()[0]
        name='wiki_fts_'+str(ordinal)
        with sqlite3.connect(self.db) as db:
            row=db.execute('SELECT id,block FROM '+name+'_data ORDER BY id DESC LIMIT 1').fetchone()
            self.assertTrue(row[1]);raw=row[1]
            db.execute('UPDATE '+name+'_data SET block=? WHERE id=?',(raw[:-1]+bytes([raw[-1]^1]),row[0]))
        matched=[];original=store._fts_guard._authorize
        def auth(action,a,b,d,t):
            if action==sqlite3.SQLITE_FUNCTION and str(b).lower()=='match':matched.append(True)
            return original(action,a,b,d,t)
        store._db.set_authorizer(auth)
        try:self.refused(store.search,pin,'hello',1)
        finally:store._db.set_authorizer(original)
        self.assertFalse(matched)

    def test_12_seal_count_tamper_cannot_reuse_an_existing_pin(self):
        store,_,h,gen=self.published();pin=store.pin_view(h)
        with sqlite3.connect(self.db) as db:
            trigger=db.execute("SELECT sql FROM sqlite_master WHERE name='generation_seals_update'").fetchone()[0]
            db.execute('DROP TRIGGER generation_seals_update')
            db.execute('UPDATE generation_seals SET eligible_count=eligible_count+1 WHERE gen_id=?',(gen,))
            db.execute(trigger)
        self.refused(store.search,pin,'hello',1)

    def test_13_per_generation_cjk_bm25_matches_v6_fts(self):
        import laya_index_a as legacy
        store,_,h=self.seeded();m=self.snapshot(store,h)
        texts={'public/한글.md':('# 한글 제목\n한글 검색 내용\n','한글 제목'),
               'public/second.md':('# Other\n한글 한글 검색\n','Other')}
        for path,(body,title) in texts.items():self.add_member(store,h,m,path,body.encode(),title)
        gen,base=self.prepared(store,h,m);store.publish_generation(h,gen,expected_head=base)
        ref=sqlite3.connect(':memory:');self.addCleanup(ref.close)
        ref.execute(legacy._gen_fts_ddl('gen_fts_1'))
        for member in m['members']:
            doc=store.document(member['blob_sha'])
            ref.execute('INSERT INTO gen_fts_1 VALUES(?,?,?,?)',
                tuple(legacy._qmd_fts_text(s) for s in [member['path'],member['display_title'],doc['body']])+(member['path'],))
        q=legacy._qmd_fts_text('한글')
        expected=ref.execute('SELECT path_key,bm25(gen_fts_1,1.5,4.0,1.0,0.0) AS rank FROM gen_fts_1 WHERE gen_fts_1 MATCH ? ORDER BY rank,path_key COLLATE BINARY',(q,)).fetchall()
        actual=store.search(store.pin_view(h),q,10)
        self.assertEqual([r['path'] for r in actual],[r[0] for r in expected])
        for a,b in zip(actual,expected):self.assertAlmostEqual(a['score'],b[1],places=12)

    def test_14_legacy_v6_reader_remains_separately_usable(self):
        import laya_index_a as legacy
        path=self.state/'legacy-v6.db';index=legacy.LayaIndexA(path,create=True)
        try:
            self.assertEqual(index.db.execute('PRAGMA user_version').fetchone()[0],6)
            self.assertNotIn('views',{r[0] for r in index.db.execute("SELECT name FROM sqlite_master WHERE type='table'")})
        finally:index.close()
        repository,_=self.bind();before=path.read_bytes()
        self.refused(self.reopen,repository,path=path)
        self.assertEqual(path.read_bytes(),before)

    def test_15_verifier_checks_all_generations_and_unsealed_slots(self):
        store,_,h,_=self.published();self.assertIs(store.verify_views(),True)
        m=self.snapshot(store,h,scan_id='2'*32)
        gen,_=self.prepared(store,h,m)
        self.assertIs(store.verify_views(),True)
        self.assertEqual(store._db.execute('SELECT state FROM generations WHERE gen_id=?',(gen,)).fetchone()[0],'prepared')
        original=self.views._insert
        def insert(db,name,row):
            if name=='generation_seals':raise RuntimeError('synthetic interrupted reader fixture')
            return original(db,name,row)
        with mock.patch.object(self.views,'_insert',side_effect=insert):
            with self.assertRaises(RuntimeError):
                self.prepared(store,h,self.snapshot(store,h,scan_id='3'*32))
        self.assertGreater(store.counts()['generation_slots'],store.counts()['generations'])
        self.assertIs(store.verify_views(),True)
        self.assertEqual(len(store.search(store.pin_view(h),'hello',1)),1)


    def test_16_shared_blob_and_trust_are_verified_once_per_snapshot(self):
        store,_,h=self.seeded();m=self.snapshot(store,h)
        body=(self.root/'.memvane/wiki/cards/a.md').read_bytes()
        for index in range(24):
            path=f'cards/shared-{index}.md'
            (self.root/'.memvane/wiki'/path).write_bytes(body)
            m['members'].append(dict(m['members'][0],path=path))
        gen,base=self.prepared(store,h,m);store.publish_generation(h,gen,expected_head=base)
        self.assertEqual(store.counts()['wiki_blobs'],1)
        pin=store.pin_view(h)
        with mock.patch.object(store,'_blob',wraps=store._blob) as blobs, mock.patch.object(store,'_attestation',wraps=store._attestation) as attestations:
            self.assertEqual(len(store.search(pin,'wiki',1)),1)
        self.assertLessEqual(blobs.call_count,4)
        self.assertEqual(attestations.call_count,1)

    def test_17_validation_cache_does_not_outlive_the_read_snapshot(self):
        store,_,h,_=self.published();pin=store.pin_view(h)
        self.assertEqual(len(store.search(pin,'hello',1)),1)
        with sqlite3.connect(self.db) as db:
            trigger=db.execute("SELECT sql FROM sqlite_master WHERE name='source_fingerprints_update'").fetchone()[0]
            db.execute('DROP TRIGGER source_fingerprints_update')
            db.execute('UPDATE source_fingerprints SET source_sha=?',('f'*64,))
            db.execute(trigger)
        self.refused(store.search,pin,'hello',1)

    def test_18_invalid_unicode_path_filters_refuse_before_sql_and_recover(self):
        store,_,h,_=self.published();pin=store.pin_view(h)
        for field in ['path_prefixes','excluded_paths']:
            for path in ['\ud800','\udfff','x\ud800y']:
                with self.subTest(field=field,path=ascii(path)):
                    error=self.refused(store.search,pin,'hello',1,**{field:[path]})
                    self.assertEqual(error.reason,'invalid_relative_path')
        self.assertEqual(len(store.search(pin,'hello',1)),1)
        self.assertEqual(store.search(pin,'hello',1,path_prefixes=['한글']),[])


if __name__=='__main__':unittest.main()

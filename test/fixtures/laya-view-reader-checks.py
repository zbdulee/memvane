"""Pinned candidate lexical reader: no operational route or source reread."""
import copy
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
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

    def test_post_reboot_device_change_preserves_published_recall(self):
        self.git(self.root,'init')
        self.git(self.root,'add','notes/src.md')
        self.git(self.root,'commit','-m','fixture')
        store,repository,old,_=self.published()
        checkout=self.views.checkout
        directory=checkout._directory
        def reboot_directory(path):
            identity=directory(path)
            return {**identity,'dev':identity['dev']+1}
        checkout._capture_cache.entry=None
        with mock.patch.object(checkout,'_directory',side_effect=reboot_directory):
            current=store.capture_view(old.worktree_id)
            manifest=self.snapshot(store,current,body=b'# After reboot\nhello postreboot\n')
            generation,head=self.prepared(store,current,manifest)
            store.publish_generation(current,generation,expected_head=head)
            evidence=json.loads(store._db.execute(
                'SELECT checkout_evidence FROM generations WHERE gen_id=?',(generation,)).fetchone()[0])['evidence']
            for key in ('project','checkout','gitDir','commonGitDir'):
                self.assertEqual(evidence[key]['dev'],directory(evidence[key]['path'])['dev']+1)
            pin=store.pin_view(current)
            self.addCleanup(store.release,pin)
            rows=store.search(pin,'postreboot',10)
            self.assertEqual([(row['gen_id'],row['path']) for row in rows],[(generation,'cards/a.md')])

    def test_sealed_capture_rejects_changed_inode_or_path(self):
        self.git(self.root,'init')
        self.git(self.root,'add','notes/src.md')
        self.git(self.root,'commit','-m','fixture')
        store,_,_,gen=self.published()
        generation=dict(store._db.execute('SELECT * FROM generations WHERE gen_id=?',(gen,)).fetchone())
        checkout=self.views.checkout
        for field in ('project','checkout','gitDir','commonGitDir'):
            for part in ('ino','path'):
                with self.subTest(field=field,part=part):
                    capture=json.loads(generation['checkout_evidence'])
                    identity=capture['evidence'][field]
                    identity[part]=identity[part]+1 if part=='ino' else identity[part]+'-other'
                    capture['checkoutDigest']=checkout._digest({k:v for k,v in capture.items() if k!='checkoutDigest'})
                    forged={**generation,'checkout_evidence':self.views.v.canonical(capture)}
                    with self.assertRaisesRegex(ValueError,'generation_capture_invalid'):
                        store._sealed_handle(store._db,forged)

    def add_member(self,store,h,m,path,body,title):
        p=self.root/'.memvane/wiki'/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(body)
        blob=store.ingest_card(h.worktree_id,path)
        att=store.put_attestation(dict(blob_sha=blob,verification_kind='compiled',
            engine_identity='synthetic:codex',policy_digest=h.policy_digest,
            receipt=dict(schema='memvane-card-evidence-v1',verdict='verified',result_digest='e'*64,prior_trust=None),
            sources=[dict(fingerprint_id=m['sources'][0]['fingerprint_id'],spans=[dict(start=0,end=4)])]))
        m['members'].append(dict(path=path,blob_sha=blob,display_title=title,trust_digest=att))

    def test_reader_factory_never_opens_writable_or_enables_writes(self):
        store,repository,handle,generation=self.published()
        before=store._path.read_bytes()
        opened=[]
        connect=sqlite3.connect
        def observe(*args,**kwargs):
            opened.append(str(args[0]))
            return connect(*args,**kwargs)
        with mock.patch('laya_blob_store.sqlite3.connect',side_effect=observe):
            reader=type(store).open_reader(store._path,repository=repository,scope_id=store._scope,
                                           deadline=time.monotonic()+10)
        self.addCleanup(reader.close)
        self.assertTrue(opened)
        self.assertTrue(all('?mode=ro' in path for path in opened),opened)
        self.assertEqual(reader._db.execute('PRAGMA query_only').fetchone()[0],1)
        checkout=importlib.import_module('memvane.store.checkout_view')
        actual_capture=checkout.capture_view
        deadlines=[]
        def capture(*args,**kwargs):
            deadlines.append(kwargs.get('deadline'))
            return actual_capture(*args,**kwargs)
        end=time.monotonic()+10
        with mock.patch.object(checkout,'capture_view',side_effect=capture):
            read_handle=reader.capture_view(handle.worktree_id,deadline=end)
            pin=reader.pin_view(read_handle,deadline=end)
            rows=reader.search(pin,'hello',10,deadline_seconds=10)
        self.assertTrue(deadlines)
        self.assertTrue(all(type(value) in (int,float) and value<=end+1 for value in deadlines),deadlines)
        self.assertEqual([(row['gen_id'],row['path']) for row in rows],[(generation,'cards/a.md')])
        reader.release(pin)
        real_clock=time.monotonic
        for failure,expected in (('checkout_budget_exhausted','fts_deadline_exceeded'),
                                 ('checkout_git_failed','wiki_binding_invalid')):
            late=[]
            def clock():return real_clock()+(20 if late else 0)
            def fail_capture(*_args,**_kwargs):
                late.append(True)
                raise checkout.CheckoutUnavailable(failure)
            with mock.patch('time.monotonic',side_effect=clock), \
                    mock.patch.object(checkout,'capture_view',side_effect=fail_capture):
                with self.assertRaisesRegex(ValueError,expected):
                    reader.pin_view(read_handle,deadline=real_clock()+10)
            self.assertEqual(late,[True])
            self.assertEqual(reader._pins,{})
        late=[]; captures=[]
        def clock():return real_clock()+(20 if late else 0)
        def second_capture(*args,**kwargs):
            captures.append(True)
            if len(captures)==2:
                late.append(True)
                raise checkout.CheckoutUnavailable('checkout_budget_exhausted')
            return actual_capture(*args,**kwargs)
        with mock.patch('time.monotonic',side_effect=clock), \
                mock.patch.object(checkout,'capture_view',side_effect=second_capture):
            with self.assertRaisesRegex(ValueError,'fts_deadline_exceeded'):
                reader.pin_view(read_handle,deadline=real_clock()+10)
        self.assertEqual(len(captures),2)
        self.assertEqual(reader._pins,{})
        self.assertEqual(reader._db.execute('PRAGMA query_only').fetchone()[0],1)
        with self.assertRaisesRegex(ValueError,'reader_write_forbidden'):
            with reader._session(write=True):
                self.fail('reader granted a write session')
        calls=[]
        with mock.patch('laya_blob_store._regular',side_effect=AssertionError('source reread')):
            with self.assertRaisesRegex(ValueError,'reader_write_forbidden'):
                reader.ingest_card(handle.worktree_id,'cards/a.md')
        with self.assertRaisesRegex(ValueError,'reader_write_forbidden'):
            reader.fill_view(None,None,None,None,chunker=lambda *_:calls.append('chunk'),
                embedder=lambda *_:calls.append('embed'),write_fence=lambda:calls.append('fence'),
                expected_cursor=0)
        self.assertEqual(calls,[])
        with self.assertRaisesRegex(ValueError,'reader_write_forbidden'):
            reader.prepare_generation(None,None,expected_head=None)
        with self.assertRaisesRegex(ValueError,'candidate_binding_mismatch'):
            type(store).open_reader(store._path,repository=repository,scope_id='f'*32)
        blob_module=importlib.import_module('memvane.store.blob_store')
        late=[]; connections=[]; real_clock=time.monotonic
        def clock():return real_clock()+(20 if late else 0)
        def connect_traced(*args,**kwargs):
            db=connect(*args,**kwargs);connections.append(db);return db
        def slow_header(db,*_args):
            late.append(True)
            db.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL '
                       'SELECT x+1 FROM n WHERE x<50000) SELECT sum(x) FROM n').fetchone()
            raise AssertionError('open header SQL was not interrupted')
        with mock.patch('time.monotonic',side_effect=clock), \
                mock.patch.object(blob_module.sqlite3,'connect',side_effect=connect_traced), \
                mock.patch.object(blob_module,'_header',side_effect=slow_header):
            with self.assertRaisesRegex(ValueError,'fts_deadline_exceeded'):
                type(store).open_reader(store._path,repository=repository,scope_id=store._scope,
                                        deadline=real_clock()+10)
        self.assertEqual(late,[True]);self.assertEqual(len(connections),1)
        with self.assertRaises(sqlite3.ProgrammingError):
            connections[0].execute('SELECT 1')
        late.clear();connections.clear()
        def bad_header(_db,*_args):
            late.append(True)
            raise sqlite3.OperationalError('synthetic_unrelated_sqlite_error')
        with mock.patch('time.monotonic',side_effect=clock), \
                mock.patch.object(blob_module.sqlite3,'connect',side_effect=connect_traced), \
                mock.patch.object(blob_module,'_header',side_effect=bad_header):
            with self.assertRaisesRegex(ValueError,'candidate_open_failed'):
                type(store).open_reader(store._path,repository=repository,scope_id=store._scope,
                                        deadline=real_clock()+10)
        self.assertEqual(late,[True]);self.assertEqual(len(connections),1)
        with self.assertRaises(sqlite3.ProgrammingError):
            connections[0].execute('SELECT 1')
        self.assertEqual(store._path.read_bytes(),before)

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
                (self.root/'.memvane/settings.json').write_text(json.dumps(dict(helpers.helpers.SETTINGS,name='renamed')))
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
        helpers.helpers.corrupt_fts_block(self.db,ordinal)
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

    def test_13_cjk_bm25_matches_reference_fts(self):
        from memvane.kernel import errors as legacy
        store,_,h=self.seeded();m=self.snapshot(store,h)
        texts={'public/한글.md':('# 한글 제목\n한글 검색 내용\n','한글 제목'),
               'public/second.md':('# Other\n한글 한글 검색\n','Other')}
        for path,(body,title) in texts.items():self.add_member(store,h,m,path,body.encode(),title)
        gen,base=self.prepared(store,h,m);store.publish_generation(h,gen,expected_head=base)
        ref=sqlite3.connect(':memory:');self.addCleanup(ref.close)
        ref.execute("CREATE VIRTUAL TABLE gen_fts_1 USING fts5(filepath,title,body,path_key UNINDEXED,tokenize='porter unicode61')")
        for member in m['members']:
            doc=store.document(member['blob_sha'])
            ref.execute('INSERT INTO gen_fts_1 VALUES(?,?,?,?)',
                tuple(legacy._qmd_fts_text(s) for s in [member['path'],member['display_title'],doc['body']])+(member['path'],))
        q=legacy._qmd_fts_text('한글')
        expected=ref.execute('SELECT path_key,bm25(gen_fts_1,1.5,4.0,1.0,0.0) AS rank FROM gen_fts_1 WHERE gen_fts_1 MATCH ? ORDER BY rank,path_key COLLATE BINARY',(q,)).fetchall()
        actual=store.search(store.pin_view(h),q,10)
        self.assertEqual([r['path'] for r in actual],[r[0] for r in expected])
        for a,b in zip(actual,expected):self.assertAlmostEqual(a['score'],b[1],places=12)

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
        # The pin verified the generation; an unchanged store reuses it (test_25).
        self.assertLessEqual(attestations.call_count,1)

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

    # Read-trust contract (U9). These hold under today's whole-generation
    # re-derivation and must keep holding if the query path is narrowed to
    # head CAS + seal/slot binding + served-row blob hashes (decision K3).
    def tamper(self,path,trigger,sql,args=()):
        db=sqlite3.connect(path)
        try:
            ddl=db.execute('SELECT sql FROM sqlite_master WHERE name=?',(trigger,)).fetchone()[0]
            db.execute('DROP TRIGGER '+trigger);db.execute(sql,args);db.execute(ddl);db.commit()
        finally:db.close()

    def published_pair(self,name):
        root=self.project(name);store,repository,h=self.seeded(root)
        m=self.snapshot(store,h,root=root,body=b'# Alpha\nhello served snapshot\n')
        p=root/'.memvane/wiki/cards/quiet.md';p.write_bytes(b'# Quiet\nunrelated unserved card\n')
        blob=store.ingest_card(h.worktree_id,'cards/quiet.md')
        m['members'].append(dict(m['members'][0],path='cards/quiet.md',blob_sha=blob,display_title='Quiet',
            trust_digest=store.put_attestation(dict(store.attestation(m['members'][0]['trust_digest']),blob_sha=blob))))
        gen,base=self.prepared(store,h,m);store.publish_generation(h,gen,expected_head=base)
        return store,repository,h,gen,{x['path']:x['blob_sha'] for x in m['members']}

    def never_served(self,store,pin,text):
        try:rows=store.search(pin,'hello',10)
        except self.Error as exc:return exc.reason
        self.assertNotIn(text,[r['body'] for r in rows])
        return None

    def test_19_tampered_served_blob_bytes_are_excluded_or_refused(self):
        store,_,h,_,blobs=self.published_pair('served-blob');pin=store.pin_view(h)
        self.assertEqual([r['path'] for r in store.search(pin,'hello',10)],['cards/a.md'])
        self.tamper(store._path,'wiki_blobs_update','UPDATE wiki_blobs SET body=? WHERE blob_sha=?',
                    ('# Alpha\nhello FORGED snapshot\n',blobs['cards/a.md']))
        # Narrowed scope (K3): the served-row blob hash check.
        reason=self.never_served(store,pin,'# Alpha\nhello FORGED snapshot\n')
        self.assertIn(reason,(None,'blob_conflict'))

    def test_20_unserved_blob_tamper_is_never_served(self):
        store,_,h,_,blobs=self.published_pair('unserved-blob');pin=store.pin_view(h)
        self.tamper(store._path,'wiki_blobs_update','UPDATE wiki_blobs SET body=? WHERE blob_sha=?',
                    ('# Quiet\nunrelated FORGED!! card\n',blobs['cards/quiet.md']))
        # The query may refuse (whole re-derivation) or serve only the intact
        # row (narrowed scope); it never serves the tampered bytes.
        self.never_served(store,pin,'# Quiet\nunrelated FORGED!! card\n')

    def test_21_seal_slot_and_binding_digest_mismatch_refuse(self):
        # Narrowed scope (K3) keeps each refusal: seal-binding fails _head's seal
        # JOIN; seal-fts-storage changes the recomputed binding over seal roots;
        # slot-manifest needs the parsed stage's manifest_digest vs the slot row.
        cases=[('seal-binding','generation_seals_update',
                'UPDATE generation_seals SET binding_digest=? WHERE gen_id=?'),
               ('slot-manifest','generation_slots_update',
                'UPDATE generation_slots SET manifest_digest=? WHERE ordinal=(SELECT ordinal FROM generations WHERE gen_id=?)'),
               ('seal-fts-storage','generation_seals_update',
                'UPDATE generation_seals SET fts_storage_digest=? WHERE gen_id=?')]
        for name,trigger,sql in cases:
            with self.subTest(case=name):
                store,_,h,gen,_=self.published_pair(name);pin=store.pin_view(h)
                self.tamper(store._path,trigger,sql,('f'*64,gen))
                self.refused(store.search,pin,'hello',1)
                self.refused(store.pin_view,h)
                self.refused(store.verify_views)

    def test_22_head_pointing_at_another_view_generation_is_refused(self):
        store,main,linked,a,b=self.pair('foreign-head')
        ga,ea=self.prepared(store,a,self.snapshot(store,a,root=main));store.publish_generation(a,ga,expected_head=ea)
        gb,eb=self.prepared(store,b,self.snapshot(store,b,root=linked));store.publish_generation(b,gb,expected_head=eb)
        # Narrowed scope (K3): head CAS check of generations.view_id.
        self.tamper(store._path,'view_head_update','UPDATE view_heads SET gen_id=? WHERE view_id=?',(ga,b.view_id))
        self.assertEqual(self.refused(store.pin_view,b).reason,'view_head_invalid')
        self.refused(store.verify_views)

    def test_23_head_epoch_regression_within_one_reader_is_refused(self):
        # _head requires the view's highest published ordinal, so a same-UID
        # rollback of view_heads behind the update trigger is refused in-store.
        # The journal endpoint record adds a cross-process epoch floor.
        store,_,h,first=self.published()
        m=self.snapshot(store,h,scan_id='2'*32,body=b'# Beta\nhello replacement snapshot\n')
        gen,base=self.prepared(store,h,m);store.publish_generation(h,gen,expected_head=base)
        pin=store.pin_view(h);self.assertEqual((pin.gen_id,pin.epoch),(gen,2));store.release(pin)
        self.tamper(store._path,'view_head_update','UPDATE view_heads SET gen_id=?,epoch=1 WHERE view_id=?',
                    (first,h.view_id))
        self.assertEqual(self.refused(store.pin_view,h).reason,'view_head_invalid')

    def test_24_other_incarnation_and_replaced_database_file_refuse(self):
        # Narrowed scope (K3): the _session boundary (_header incarnation, _db_file identity).
        store,_,h,_,_=self.published_pair('incarnation');pin=store.pin_view(h)
        self.tamper(store._path,'store_meta_update','UPDATE store_meta SET incarnation=?',('0'*32,))
        self.assertEqual(self.refused(store.search,pin,'hello',1).reason,'candidate_binding_changed')
        self.assertEqual(self.refused(store.pin_view,h).reason,'candidate_binding_changed')
        store,_,h,_,_=self.published_pair('replaced');pin=store.pin_view(h)
        copy_path=store._path.with_name(store._path.name+'.copy')
        source=sqlite3.connect(store._path);target=sqlite3.connect(copy_path)
        try:source.backup(target)
        finally:target.close();source.close()
        copy_path.chmod(0o600);os.replace(copy_path,store._path)
        self.assertEqual(self.refused(store.search,pin,'hello',1).reason,'candidate_file_changed')
        self.assertEqual(self.refused(store.pin_view,h).reason,'candidate_file_changed')

    def test_25_pinned_reads_reuse_verification_until_any_commit(self):
        store,_,h,gen=self.published()
        with mock.patch.object(store,'_verify_generation',wraps=store._verify_generation) as verified:
            pin=store.pin_view(h)
            for _ in range(3):
                self.assertEqual(len(store.search(pin,'hello',1)),1)
            self.assertEqual(verified.call_count,1)
            # Any commit by another connection re-verifies, even one that changes no value.
            self.tamper(store._path,'generation_seals_update',
                        'UPDATE generation_seals SET eligible_count=eligible_count WHERE gen_id=?',(gen,))
            self.assertEqual(len(store.search(pin,'hello',1)),1)
            self.assertEqual(verified.call_count,2)
            self.assertEqual(len(store.search(pin,'hello',1)),1)
            self.assertEqual(verified.call_count,2)
            # A second pin verifies on its own; releasing one keeps the other's reuse.
            other=store.pin_view(h);store.release(pin)
            self.assertEqual(verified.call_count,3)
            self.assertEqual(len(store.search(other,'hello',1)),1)
            self.assertEqual(verified.call_count,3)

    def test_26_compiled_line_spans_locate_like_legacy(self):
        store,_,h=self.seeded()
        m=self.snapshot(store,h,body=b'# Alpha\nhello compiled snapshot\n')
        fp=store.fingerprint(m['sources'][0]['fingerprint_id'])
        span=lambda start,end:dict(kind='lines-v1',path=fp['path'],sourceSha256=fp['source_sha'],
                                   startLine=start,endLine=end,spanSha256='d'*64)
        member=m['members'][0]
        member['trust_digest']=store.put_attestation(dict(blob_sha=member['blob_sha'],
            verification_kind='compiled',engine_identity='synthetic:codex',policy_digest=h.policy_digest,
            receipt=dict(schema='memvane-card-evidence-v2',verdict='verified',result_digest='e'*64,
                         prior_trust=None,verdict_digest='f'*64,verified_mode='cross-engine',
                         verify_engine='claude'),
            sources=[dict(fingerprint_id=m['sources'][0]['fingerprint_id'],spans=[span(7,7),span(3,5)])]))
        gen,base=self.prepared(store,h,m);store.publish_generation(h,gen,expected_head=base)
        pin=store.pin_view(h)
        self.assertEqual([row['path'] for row in store.search(pin,'hello',10)],['cards/a.md'])
        # Same locator format and order as legacy-lines-v1 spans.
        self.assertEqual(store.card_locations(pin,['cards/a.md','cards/absent.md']),
                         {'cards/a.md':['notes/src.md#L3-L5','notes/src.md#L7-L7']})

    def test_27_span_kinds_without_a_line_locator_still_refuse(self):
        store,_,h,_=self.published();pin=store.pin_view(h)
        # The default compiled card carries byte spans: no line locator exists.
        self.assertEqual(self.refused(store.card_locations,pin,['cards/a.md']).reason,
                         'legacy_card_trust_invalid')
        reader=sys.modules['laya_view_reader']
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        self.addCleanup(db.close)
        db.execute('CREATE TABLE card_sources(gen_id,wiki_path,ordinal,spans_json)')
        for ordinal,kind in enumerate(('lines-v2',None,'')):
            value=dict(kind=kind,path='notes/src.md',sourceSha256='a'*64,startLine=1,endLine=1,spanSha256='d'*64)
            db.execute('DELETE FROM card_sources')
            db.execute('INSERT INTO card_sources VALUES(?,?,?,?)',('g','cards/a.md',ordinal,
                       sys.modules['laya_blob_values'].canonical([value])))
            with self.subTest(kind=kind):
                self.assertEqual(self.refused(reader._card_locations,db,'g',['cards/a.md']).reason,
                                 'legacy_card_trust_invalid')


if __name__=='__main__':unittest.main()

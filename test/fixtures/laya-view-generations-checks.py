"""Capture-bound generation commitments; synthetic projects only."""
import copy
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import time
import unittest
from unittest import mock

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('blob_fixture',HERE/'laya-blob-store-checks.py')
helpers=importlib.util.module_from_spec(spec);spec.loader.exec_module(helpers)
SCAN='1'*32


class GenerationCase(helpers.BlobStoreCase):
    def setUp(self):
        super().setUp()
        try:self.views=importlib.import_module('laya_view_store')
        except ModuleNotFoundError as exc:
            if exc.name!='laya_view_store':raise
            self.fail('PR3 capture-bound generation store is absent')
        self.Store=self.views.ViewStore

    def seeded(self,root=None):
        store,repository,wid=self.create(root,path=None if root is None else self.state/(root.name+'.db'))
        handle=store.capture_view(wid)
        return store,repository,handle

    def snapshot(self,store,handle,*,root=None,scan_id=SCAN,body=None,
                 source_sha=None,presence='present',zero=False,trust=True):
        root=root or self.root
        if body is not None:(root/'.memvane/wiki/cards/a.md').write_bytes(body)
        blob=store.ingest_card(handle.worktree_id,'cards/a.md')
        raw=(root/'notes/src.md').read_bytes()
        fp=store.put_fingerprint(dict(worktree_id=handle.worktree_id,branch_key=handle.branch_key,
            path='notes/src.md',source_sha=source_sha or hashlib.sha256(raw).hexdigest(),
            byte_length=len(raw),head_oid=handle.head_oid,
            tracked_state='unknown' if handle.branch_key=='non-git' else 'tracked',
            binding_state='observed',scan_id=scan_id,observed_ns=1))
        evidence=[] if zero else [dict(fingerprint_id=fp,spans=[dict(start=0,end=4)])]
        attestation=store.put_attestation(dict(blob_sha=blob,verification_kind='compiled',
            engine_identity='synthetic:codex',policy_digest=handle.policy_digest,
            receipt=dict(schema='memvane-card-evidence-v1',verdict='verified',
                         result_digest='e'*64,prior_trust=None),sources=evidence)) if trust else None
        return dict(scan_id=scan_id,policy_digest=handle.policy_digest,revocation_epoch=0,
                    complete=True,sources=[] if zero else [dict(path='notes/src.md',fingerprint_id=fp,presence=presence)],
                    members=[dict(path='cards/a.md',blob_sha=blob,display_title='Shared',trust_digest=attestation)])

    def prepared(self,store,handle,manifest=None):
        head=store.head(handle)
        gen=store.prepare_generation(handle,manifest or self.snapshot(store,handle),expected_head=head)
        return gen,head

    def eligible(self,store,gen):
        return store._db.execute('SELECT eligible FROM wiki_members WHERE gen_id=?',(gen,)).fetchone()[0]


class GenerationChecks(GenerationCase):
    def test_cached_capture_never_accepts_a_different_forged_admission(self):
        store,_,h=self.seeded()
        first=self.snapshot(store,h)['members'][0]['blob_sha']
        card=self.root/'.memvane/wiki/cards/b.md'
        card.write_text('# Second\nsynthetic second card\n',encoding='utf8')
        second=store.ingest_card(h.worktree_id,'cards/b.md')
        with sqlite3.connect(self.db) as db:
            trigger=db.execute("SELECT sql FROM sqlite_master WHERE name='blob_admissions_update'").fetchone()[0]
            row=db.execute('SELECT proof_json FROM blob_admissions WHERE blob_sha=?',(second,)).fetchone()
            proof=json.loads(row[0]);proof['capture']['checkoutDigest']='0'*64
            db.execute('DROP TRIGGER blob_admissions_update')
            db.execute('UPDATE blob_admissions SET proof_json=?,proof_digest=? WHERE blob_sha=?',
                (self.views.v.canonical(proof),self.views.v.digest('memvane-wiki-admission-v1',proof),second))
            db.execute(trigger)
        cache={'blobs':set(),'worktrees':{},'captures':set(),'fingerprints':{}}
        with store._session():
            store._blob(first,cache=cache)
            with self.assertRaisesRegex(Exception,'blob_admission_invalid'):
                store._blob(second,cache=cache)

    def test_final_capture_deadline_is_not_reported_as_checkout_change(self):
        checkout=self.views.checkout
        def bounded_match(*args, **kwargs):
            if kwargs.get('_raise_timeout'):
                raise checkout.CheckoutUnavailable('checkout_budget_exhausted')
            return False
        with mock.patch.object(self.views, '_worktree_record', return_value={}), \
             mock.patch.object(self.views, '_worktree_row', return_value=({}, {'capture':'expected'})), \
             mock.patch.object(checkout, 'capture_matches', side_effect=bounded_match):
            with self.assertRaisesRegex(checkout.CheckoutUnavailable, 'checkout_budget_exhausted'):
                self.views._current_capture({'project_root':'synthetic'}, {}, {}, 'expected', deadline=1)

    def test_02_exact_publish_replay_does_not_increment_epoch(self):
        store,_,h=self.seeded();gen,base=self.prepared(store,h)
        first=store.publish_generation(h,gen,expected_head=base)
        self.assertEqual(store.publish_generation(h,gen,expected_head=copy.deepcopy(base)),first)
        self.assertEqual(store.head(h),first)

    def test_03_zero_missing_and_held_sources_stay_ineligible(self):
        for mode in ['zero','held','untrusted']:
            with self.subTest(mode=mode):
                root=self.project(mode);store,_,h=self.seeded(root)
                manifest=self.snapshot(store,h,root=root,zero=mode=='zero',
                    presence='held_missing' if mode=='held' else 'present',trust=mode!='untrusted')
                gen,_=self.prepared(store,h,manifest)
                self.assertEqual(self.eligible(store,gen),0)

    def test_04_changed_source_never_rewrites_old_card_provenance(self):
        store,_,h=self.seeded();m=self.snapshot(store,h)
        original=copy.deepcopy(store.attestation(m['members'][0]['trust_digest']))
        old=store.fingerprint(m['sources'][0]['fingerprint_id'])
        fresh=dict(old,source_sha='d'*64,observed_ns=2)
        m['sources'][0]['fingerprint_id']=store.put_fingerprint(fresh)
        gen,_=self.prepared(store,h,m)
        self.assertEqual(self.eligible(store,gen),0)
        self.assertEqual(store.attestation(m['members'][0]['trust_digest']),original)
        ref=store._db.execute('SELECT fingerprint_id FROM card_sources WHERE gen_id=?',(gen,)).fetchone()[0]
        self.assertEqual(ref,original['sources'][0]['fingerprint_id'])

    def test_05_different_observation_same_source_preserves_old_fingerprint(self):
        store,_,h=self.seeded();m=self.snapshot(store,h)
        original=m['sources'][0]['fingerprint_id'];old=store.fingerprint(original)
        m['sources'][0]['fingerprint_id']=store.put_fingerprint(dict(old,observed_ns=2))
        gen,_=self.prepared(store,h,m)
        self.assertEqual(self.eligible(store,gen),1)
        self.assertEqual(store._db.execute('SELECT fingerprint_id FROM card_sources WHERE gen_id=?',(gen,)).fetchone()[0],original)

    def test_06_source_path_scan_and_branch_claims_are_bound(self):
        store,_,h=self.seeded();m=self.snapshot(store,h)
        before=store.counts()
        for patch in [{'path':'notes/other.md'},{'fingerprint_id':'0'*64}]:
            changed=copy.deepcopy(m);changed['sources'][0].update(patch)
            with self.subTest(patch=patch):self.refused(store.prepare_generation,h,changed,expected_head=store.head(h))
        wrong=store.fingerprint(m['sources'][0]['fingerprint_id'])
        bad=copy.deepcopy(m);bad['sources'][0]['fingerprint_id']=store.put_fingerprint(dict(wrong,scan_id='2'*32))
        self.refused(store.prepare_generation,h,bad,expected_head=store.head(h))
        self.assertEqual(store.counts()['generations'],before['generations'])

    def test_07_skipped_source_is_filtered_without_laundering_trust(self):
        (self.root/'.memvane/settings.json').write_text(json.dumps(dict(helpers.SETTINGS,skipPaths=['notes'])))
        store,_,h=self.seeded();m=self.snapshot(store,h)
        gen,_=self.prepared(store,h,m)
        self.assertEqual(self.eligible(store,gen),0)

    def test_08_malformed_manifest_refused_before_slot_or_generation(self):
        store,_,h=self.seeded();m=self.snapshot(store,h);base=store.head(h)
        before=store.counts()
        cases=[dict(m,complete=False),dict(m,complete=1),dict(m,raw='SECRET raw body'),
               dict(m,scan_id='bad'),dict(m,policy_digest='f'*64),dict(m,revocation_epoch=True),
               dict(m,members=m['members']*2),dict(m,sources=m['sources']*2)]
        for value in cases:
            with self.subTest(value_keys=sorted(value)):
                self.refused(store.prepare_generation,h,value,expected_head=base)
                self.assertEqual(store.counts(),before)

    def test_09_capture_is_issued_frozen_and_store_local(self):
        store,repository,h=self.seeded();self.assertRegex(h.view_id,r'^[0-9a-f]{64}$')
        with self.assertRaises((AttributeError,TypeError)):h.view_id='f'*64
        forged=replace(h,view_id='f'*64)
        self.refused(store.head,forged)
        other=self.reopen(repository)
        self.refused(other.head,h)
        self.refused(store.capture_view,'0'*32)

    def test_10_settings_change_invalidates_original_prepared_handle(self):
        store,_,h=self.seeded();gen,base=self.prepared(store,h)
        p=self.root/'.memvane/settings.json';p.write_text(json.dumps(dict(helpers.SETTINGS,name='renamed')))
        self.refused(store.publish_generation,h,gen,expected_head=base)
        self.assertEqual(store._db.execute('SELECT COUNT(*) FROM s_heads').fetchone()[0],0)

    def test_11_wiki_mapping_bytes_must_match_the_prepared_manifest(self):
        store,_,h=self.seeded();m=self.snapshot(store,h)
        (self.root/'.memvane/wiki/cards/a.md').write_text('# Replaced after observation\n')
        self.refused(store.prepare_generation,h,m,expected_head=store.head(h))
        self.assertEqual(store.counts()['generations'],0)

    def test_12_same_view_stale_base_and_aba_are_refused(self):
        store,_,h=self.seeded();first,empty=self.prepared(store,h)
        base=store.publish_generation(h,first,expected_head=empty)
        a=store.prepare_generation(h,self.snapshot(store,h,scan_id='2'*32),expected_head=base)
        b=store.prepare_generation(h,self.snapshot(store,h,scan_id='3'*32),expected_head=base)
        second=store.publish_generation(h,b,expected_head=base)
        self.refused(store.publish_generation,h,a,expected_head=base)
        with sqlite3.connect(self.db) as db:
            db.execute('UPDATE view_heads SET gen_id=?,epoch=? WHERE view_id=?',(first,second['epoch']+1,h.view_id))
        # Trigger-legal ABA back to an older published generation is not the
        # view's highest published ordinal, so the head itself is refused.
        self.assertEqual(self.refused(store.head,h).reason,'view_head_invalid')
        self.refused(store.publish_generation,h,a,expected_head=base)

    def pair(self,name):
        main,linked=self.project(name),self.home/'projects'/(name+'-linked')
        for args in [('init','-q'),('add','-A'),('commit','-q','-m','fixture'),
                     ('worktree','add','-q','-b','side',str(linked))]:self.git(main,*args)
        store,repository,a=self.seeded(main)
        record=helpers.view.register_worktree(str(linked),repository)
        wid=store.register_worktree(str(linked),record)
        return store,main,linked,a,store.capture_view(wid)

    def test_13_other_view_publication_never_changes_its_neighbor(self):
        store,main,linked,a,b=self.pair('views')
        ga,ea=self.prepared(store,a,self.snapshot(store,a,root=main))
        gb,eb=self.prepared(store,b,self.snapshot(store,b,root=linked))
        hb=store.publish_generation(b,gb,expected_head=eb)
        ha=store.publish_generation(a,ga,expected_head=ea)
        self.assertEqual(store.head(b),hb)
        self.assertNotEqual(ha['view_id'],hb['view_id'])
        self.assertEqual(store.counts()['wiki_blobs'],1)
        self.assertEqual(store.counts()['card_attestations'],2)

    def test_14_other_worktree_fingerprint_or_trust_cannot_be_borrowed(self):
        store,main,linked,a,b=self.pair('foreign')
        ma=self.snapshot(store,a,root=main);mb=self.snapshot(store,b,root=linked)
        for key in ['sources','members']:
            changed=copy.deepcopy(mb);changed[key]=copy.deepcopy(ma[key])
            with self.subTest(key=key):self.refused(store.prepare_generation,b,changed,expected_head=store.head(b))

    def test_15_branch_switch_discards_prepared_job(self):
        root=self.project('branch')
        for args in [('init','-q'),('add','-A'),('commit','-q','-m','fixture')]:self.git(root,*args)
        store,_,h=self.seeded(root);gen,base=self.prepared(store,h,self.snapshot(store,h,root=root))
        self.git(root,'checkout','-q','-b','another')
        self.refused(store.publish_generation,h,gen,expected_head=base)
        fresh=store.capture_view(h.worktree_id)
        self.assertNotEqual(fresh.view_id,h.view_id)
        self.assertIsNone(store.head(fresh)['gen_id'])

    def test_publish_reproves_git_even_when_fast_observation_is_stale(self):
        root = self.project('stale-git-cache')
        for args in [('init', '-q'), ('add', '-A'), ('commit', '-q', '-m', 'fixture')]:
            self.git(root, *args)
        store, _, handle = self.seeded(root)
        generation, base = self.prepared(store, handle, self.snapshot(store, handle, root=root))
        prior_facts = copy.deepcopy(store._handles[handle.token]['capture']['evidence'])
        self.git(root, 'commit', '--allow-empty', '-q', '-m', 'later')
        with mock.patch.object(self.views.checkout, '_capture_facts', return_value=prior_facts):
            self.refused(store.publish_generation, handle, generation, expected_head=base)
        self.assertIsNone(store.head(store.capture_view(handle.worktree_id))['gen_id'])

    def test_19_branch_change_after_write_lock_refuses_publication(self):
        root=self.project('lock-race')
        for args in [('init','-q'),('add','-A'),('commit','-q','-m','fixture')]:self.git(root,*args)
        store,_,h=self.seeded(root);gen,base=self.prepared(store,h,self.snapshot(store,h,root=root))
        original=store._session;changed=[]
        @contextmanager
        def session(*,write=False):
            with original(write=write) as db:
                if write and not changed:
                    self.git(root,'checkout','-q','-b','switched-under-lock');changed.append(True)
                yield db
        with mock.patch.object(store,'_session',side_effect=session):
            self.refused(store.publish_generation,h,gen,expected_head=base)
        self.assertTrue(changed)
        self.assertEqual(store._db.execute('SELECT COUNT(*) FROM s_heads').fetchone()[0],0)

    def test_20_modified_issued_handle_cannot_choose_another_view(self):
        store,_,h=self.seeded();before=h.view_id
        object.__setattr__(h,'view_id','f'*64)
        try:self.refused(store.head,h)
        finally:object.__setattr__(h,'view_id',before)

    def test_21_invalid_unicode_manifest_has_bounded_public_error(self):
        store,_,h=self.seeded();m=self.snapshot(store,h);before=store.counts()
        m['members'][0]['display_title']='\ud800'
        self.refused(store.prepare_generation,h,m,expected_head=store.head(h))
        self.assertEqual(store.counts(),before)


    def test_23_invalid_unicode_manifest_paths_refuse_without_writes(self):
        store,_,h=self.seeded();original=self.snapshot(store,h)
        before=store.counts();base=store.head(h)
        for field in ['members','sources']:
            for path in ['\ud800.md','\udfff.md','x\ud800y.md']:
                with self.subTest(field=field,path=ascii(path)):
                    manifest=copy.deepcopy(original);manifest[field][0]['path']=path
                    error=self.refused(store.prepare_generation,h,manifest,expected_head=base)
                    self.assertEqual(error.reason,'invalid_relative_path')
                    self.assertEqual(store.counts(),before)


if __name__=='__main__':unittest.main()

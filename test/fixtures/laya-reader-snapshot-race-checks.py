#!/usr/bin/env python3
"""Actual selected sidecar publication crossing common snapshot pins."""
import importlib
import importlib.util
from contextlib import closing
import hashlib
import fcntl
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import time
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('snapshot_sidecar_fixture',ROOT/'test/fixtures/laya-reader-sidecar-write-checks.py')
existing=importlib.util.module_from_spec(spec);spec.loader.exec_module(existing)

class SnapshotRaceChecks(existing.SidecarWriteChecks):
    def test_pinned_index_reuses_candidate_verification_without_writes(self):
        opens=importlib.import_module('laya_reader_open')
        lease=importlib.import_module('laya_reader_lease')
        index=importlib.import_module('laya_index_a')
        catalog=importlib.import_module('laya_source_catalog')
        with self.opened() as (runtime,_snapshot,_row,_component):
            host=importlib.import_module('laya_host_response')
            decision=self.seed.reader.journal.select_route(str(self.case.project),action='recall')
            def no_vector(_prompt,_profile):
                raise host._VectorUnavailable('fixture')
            expected=host.query_selected_v2(runtime,'otter',decision['vectorProfile'],
                                           decision['vectorProfile'],no_vector,decision)
            self.assertTrue(expected[0])
            def warm_vector(_prompt,profile):
                return {'profileId':profile['profileId'],
                        'approvedProviderId':profile['approvedProviderId'],
                        'providerArtifactSha256':profile['providerArtifactSha256'],
                        'vector':[1.0,0.0,0.0]}
            expected_hybrid=host.query_selected_v2(runtime,'semanticnoncemissing',decision['vectorProfile'],
                                                  decision['vectorProfile'],warm_vector,decision)
            self.assertTrue(expected_hybrid[0])
            with lease.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as endpoint:
                decision=self.seed.reader.journal.select_route(str(self.case.project),action='recall')
                with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10) as observed:
                    db=observed.connections[0]
                    binding,_=self.seed.reader._binding_snapshot(observed.connections[1],
                        Path(endpoint.record['binding_path']),decision,observed.published)
                    authorized=[]
                    db.set_authorizer(lambda action,*_args: authorized.append(action) or sqlite3.SQLITE_OK)
                    pinned=index.SnapshotIndex(db,scope=decision['scopeId'],
                        generation=observed.published[0],incarnation=decision['dbIncarnation'],
                        deadline=time.monotonic()+10)
                    token=pinned.pin()
                    with self.assertRaises(sqlite3.DatabaseError):
                        db.rollback()
                    self.assertTrue(db.in_transaction)
                    for invalid in (None,object()):
                        with self.assertRaisesRegex(ValueError,'reader_snapshot_closed'):
                            pinned.search_many(invalid,('otter',),20)
                    with catalog.CandidateQuery(pinned,token,binding) as candidates:
                        rows=pinned.search_many(token,('otter',),20)[0]
                        verified=candidates.verify_indexed_rows(rows,collection_names=tuple(binding.collections))
                        self.assertTrue(any('otter' in row['body'] for row in verified))
                        candidates.finish()
                    self.assertTrue(db.in_transaction)
                    state={'query':False,'expired':False,'calls':0}
                    def previous_progress():
                        state['calls']+=1
                        if state['query']:
                            state['expired']=True
                        return 0
                    db.set_progress_handler(previous_progress,1000)
                    current=time.monotonic()
                    def long_query(*_args,**_kwargs):
                        state['query']=True
                        try:
                            db.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL '
                                       'SELECT x+1 FROM n WHERE x<300000) SELECT sum(x) FROM n').fetchone()
                            return []
                        finally:state['query']=False
                    try:
                        with mock.patch.object(index.time,'monotonic',
                                side_effect=lambda: pinned._deadline+1 if state['expired'] else current), \
                             mock.patch.object(index,'_search_pinned_fts',side_effect=long_query):
                            with self.assertRaisesRegex(ValueError,'fts_deadline_exceeded'):
                                pinned.search_many(token,('otter',),20)
                    except BaseException:
                        db.set_progress_handler(None,0)
                        pinned.release(token)
                        raise
                    self.assertTrue(state['expired'])
                    state['calls']=0
                    db.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL '
                               'SELECT x+1 FROM n WHERE x<1000) SELECT sum(x) FROM n').fetchone()
                    self.assertGreater(state['calls'],0)
                    db.set_progress_handler(None,0)
                    pinned._deadline=0
                    pinned.release(token)
                    authorized.clear()
                    db.execute('SELECT count(*) FROM head').fetchone()
                    self.assertIn(sqlite3.SQLITE_SELECT,authorized)
                    db.set_authorizer(None)
                    with self.assertRaisesRegex(ValueError,'reader_snapshot_closed'):
                        pinned.search_many(token,('otter',),20)
                    self.assertTrue(db.in_transaction)
                    pinned=index.SnapshotIndex(db,scope=decision['scopeId'],
                        generation=observed.published[0],incarnation=decision['dbIncarnation'],
                        deadline=time.monotonic()+10)
                    from types import SimpleNamespace
                    snapshot_runtime=SimpleNamespace(index=pinned,scope=decision['scopeId'])
                    actual=host.query_selected_v2(snapshot_runtime,'otter',decision['vectorProfile'],
                        decision['vectorProfile'],no_vector,decision,_snapshot_binding=binding)
                    self.assertEqual(actual,expected)
                    self.assertTrue(db.in_transaction)
                    pinned=index.SnapshotIndex(db,scope=decision['scopeId'],
                        generation=observed.published[0],incarnation=decision['dbIncarnation'],
                        deadline=time.monotonic()+10)
                    snapshot_runtime=SimpleNamespace(index=pinned,scope=decision['scopeId'])
                    vector_module=importlib.import_module('laya_index_vector')
                    with mock.patch.object(vector_module,'verify_generation_seal',
                                           wraps=vector_module.verify_generation_seal) as seal:
                        actual_hybrid=host.query_selected_v2(snapshot_runtime,'semanticnoncemissing',
                            decision['vectorProfile'],decision['vectorProfile'],warm_vector,
                            decision,_snapshot_binding=binding)
                        self.assertEqual(seal.call_count,1)
                    self.assertEqual(actual_hybrid,expected_hybrid)
                    self.assertTrue(db.in_transaction)

    def test_selected_snapshot_search_shadows_an_older_base_document(self):
        opens=importlib.import_module('laya_reader_open')
        lease=importlib.import_module('laya_reader_lease')
        query=importlib.import_module('laya_incremental_docs_query')
        revisions=importlib.import_module('laya_publication_revision')
        with self.opened() as (runtime,snapshot,row,component):
            writer=self.I._sidecar_open(self.identity,component)
            try:
                self.I._sidecar_setup(writer,self.identity,component)
                with mock.patch.object(self.I,'_open',return_value=writer), \
                        mock.patch.object(self.I,'_close',return_value=None):
                    self.assertEqual(self.publish(runtime,snapshot,row,component)['status'],'published')
                replacement=writer.execute("SELECT hash,title,body,profile_id,chunk_count FROM docs "
                    "WHERE path != ? AND body LIKE '%otter%'",(row[0],)).fetchone()
                self.assertIsNotNone(replacement)
                self.assertNotEqual(replacement[0],row[1])
                with self.W.write_connection(writer,component,visible=True,deadline=time.monotonic()+3):
                    before=revisions.sidecar_row(writer,row[0])
                    rowid=writer.execute('SELECT rowid FROM docs WHERE path=?',(row[0],)).fetchone()[0]
                    writer.execute('DELETE FROM docs_fts WHERE rowid=?',(rowid,))
                    writer.execute('UPDATE docs SET hash=?,title=?,body=?,profile_id=?,chunk_count=? WHERE path=?',
                                   (*replacement,row[0]))
                    writer.execute('INSERT INTO docs_fts(rowid,path,title,body) VALUES(?,?,?,?)',
                                   (rowid,row[0],replacement[1],replacement[2]))
                    revisions.record_sidecar(writer,path=row[0],before=before)
                with lease.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as endpoint:
                    decision=self.seed.reader.journal.select_route(str(self.case.project),action='recall')
                    with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10) as observed:
                        result=query.query_incremental_snapshot('otter',decision,
                            {'vectorProfile':decision['vectorProfile']},None,
                            base=observed.connections[0],side=observed.connections[3],
                            deadline=time.monotonic()+10)
                        self.assertIn(row[0],result[3])
                        found=[item for item in result[0] if item['path']==row[0]]
                        self.assertEqual([(item['hash'],item['body']) for item in found],
                                         [(replacement[0],replacement[2])])
                        base=observed.connections[0]
                        ordinal=base.execute('SELECT ord FROM gen_index WHERE gen=?',
                                             (observed.published[0],)).fetchone()[0]
                        old_term='heron' if 'heron' in row[3] else 'kestrel'
                        search=importlib.import_module('laya_index_a')._search_pinned_fts
                        unfiltered=search(base,runtime.scope,observed.published[0],ordinal,(old_term,),20)[0]
                        filtered=search(base,runtime.scope,observed.published[0],ordinal,(old_term,),20,
                                        excluded_paths=result[3])[0]
                        self.assertIn(row[0],[item['path'] for item in unfiltered])
                        self.assertNotIn(row[0],[item['path'] for item in filtered])
                        binding,_=self.seed.reader._binding_snapshot(observed.connections[1],
                            Path(endpoint.record['binding_path']),decision,observed.published)
                        pinned=importlib.import_module('laya_index_a').SnapshotIndex(
                            base,scope=decision['scopeId'],generation=observed.published[0],
                            incarnation=decision['dbIncarnation'],deadline=time.monotonic()+10)
                        from types import SimpleNamespace
                        host=importlib.import_module('laya_host_response')
                        def no_vector(_prompt,_profile):
                            raise host._VectorUnavailable('fixture')
                        selected=host.query_selected_v2(SimpleNamespace(index=pinned,scope=runtime.scope),
                            old_term,decision['vectorProfile'],decision['vectorProfile'],no_vector,
                            decision,excluded_paths=result[3],_snapshot_binding=binding)
                        self.assertNotIn(row[0],[item['path'] for item in selected[0]])
                        reader=importlib.import_module('laya_v6_reader')
                        combined=reader.query_v6_snapshot(observed,endpoint=endpoint,prompt='otter',
                            decision=decision,provider=None,deadline=time.monotonic()+10)
                        vector_module=importlib.import_module('laya_index_vector')
                        with mock.patch.object(vector_module,'_read_chunk_set',
                                               side_effect=AssertionError('unexpected lexical vector read')):
                            lexical=reader.query_v6_snapshot(observed,endpoint=endpoint,prompt='otter',
                                decision=decision,provider=None,deadline=time.monotonic()+10)
                        self.assertEqual(lexical.rows,combined.rows)
                        found=[item for item in combined.rows if item['path']==row[0]]
                        self.assertEqual([(item['hash'],item['body']) for item in found],
                                         [(replacement[0],replacement[2])])
                        self.assertEqual(combined.data_generation,observed.published[0])
                        self.assertEqual(combined.semantic_status,'lexical_only')
                        calls=[]
                        def warm(text,profile):
                            calls.append(text)
                            return {'profileId':profile['profileId'],
                                    'approvedProviderId':profile['approvedProviderId'],
                                    'providerArtifactSha256':profile['providerArtifactSha256'],
                                    'vector':[1.0,0.0,0.0]}
                        hybrid=reader.query_v6_snapshot(observed,endpoint=endpoint,prompt='otter',
                            decision=decision,provider=warm,deadline=time.monotonic()+10)
                        self.assertEqual(hybrid.semantic_status,'hybrid')
                        self.assertEqual(calls,['otter'])
                        with mock.patch.object(query,'_read_chunk_set',
                                side_effect=importlib.import_module('laya_index_a').IndexContractError(
                                    'vector_budget_exceeded')):
                            deferred=reader.query_v6_snapshot(observed,endpoint=endpoint,prompt='otter',
                                decision=decision,provider=warm,deadline=time.monotonic()+10)
                        self.assertEqual(deferred.rows,combined.rows)
                        self.assertEqual(deferred.semantic_status,'lexical_only')
                        def long_vector_sql(db,*_args,**_kwargs):
                            db.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL '
                                       'SELECT x+1 FROM n WHERE x<300000000) SELECT sum(x) FROM n').fetchone()
                            raise AssertionError('vector SQL was not interrupted')
                        with mock.patch.object(query,'_read_chunk_set',side_effect=long_vector_sql):
                            interrupted=reader.query_v6_snapshot(observed,endpoint=endpoint,prompt='otter',
                                decision=decision,provider=warm,deadline=time.monotonic()+10)
                        self.assertEqual(interrupted.rows,combined.rows)
                        self.assertEqual(interrupted.semantic_status,'lexical_only')
                        self.assertTrue(observed.connections[0].in_transaction)
                        with mock.patch.object(query,'_read_chunk_set',return_value=None):
                            with self.assertRaisesRegex(ValueError,'preview_vector_incomplete'):
                                reader.query_v6_snapshot(observed,endpoint=endpoint,prompt='otter',
                                    decision=decision,provider=warm,deadline=time.monotonic()+10)
                        def guarded_provider(text,profile):
                            for db in observed.connections:
                                with self.assertRaises(sqlite3.DatabaseError):
                                    db.rollback()
                            return warm(text,profile)
                        guarded=reader.query_v6_snapshot(observed,endpoint=endpoint,prompt='otter',
                            decision=decision,provider=guarded_provider,deadline=time.monotonic()+10)
                        self.assertTrue(guarded.rows)
                        self.assertTrue(observed.connections[0].in_transaction)
                        self.assertTrue(observed.connections[3].in_transaction)
                    with self.assertRaisesRegex(ValueError,'reader_snapshot_unpinned'):
                        reader.query_v6_snapshot(observed,endpoint=endpoint,prompt='otter',
                            decision=decision,provider=None,deadline=time.monotonic()+10)
            finally:self.I._close(writer)

    def test_existing_search_sql_works_on_caller_owned_snapshot(self):
        opens=importlib.import_module('laya_reader_open')
        lease=importlib.import_module('laya_reader_lease')
        index=importlib.import_module('laya_index_a')
        with self.opened() as (runtime,_snapshot,_row,_component):
            token,generation=runtime.index.pin(runtime.scope)
            try:
                expected=runtime.index.search_many(token,('otter','nonexistentword'),20)
            finally:
                runtime.index.release(token)
            self.assertTrue(expected[0])
            with lease.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as endpoint:
                with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10) as observed:
                    db=observed.connections[0]
                    ordinal=db.execute('SELECT ord FROM gen_index WHERE gen=?',(generation,)).fetchone()[0]
                    traced=[]
                    db.set_trace_callback(traced.append)
                    try:
                        actual=index._search_pinned_fts(db,runtime.scope,generation,ordinal,
                                                       ('otter','nonexistentword'),20)
                    finally:
                        db.set_trace_callback(None)
                    self.assertEqual(actual,expected)
                    self.assertTrue(db.in_transaction)
                    self.assertEqual(db.execute('PRAGMA query_only').fetchone(),(1,))
                    # SQLite traces FTS virtual-table internals as SQL comments.
                    self.assertTrue(all(statement.lstrip().upper().startswith(('SELECT','--'))
                                        for statement in traced),traced)

    def test_unrecorded_legacy_exclusion_after_revision_is_refused(self):
        opens=importlib.import_module('laya_reader_open')
        lease=importlib.import_module('laya_reader_lease')
        revisions=importlib.import_module('laya_publication_revision')
        with self.opened() as (runtime,_snapshot,row,_component):
            with lease.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as endpoint:
                decision=self.seed.reader.journal.select_route(str(self.case.project),action='recall')
                revisions.ensure_base_baseline(runtime.index,scope=decision['scopeId'],
                    profile_id=decision['vectorProfile']['profileId'],deadline=time.monotonic()+5)
                self.assertEqual(runtime.index.exclude(decision['scopeId'],path=row[0]),'excluded')
                with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10) as observed:
                    self.assertEqual(observed.base_revision,1)
                legacy=sqlite3.connect(self.case.db,isolation_level=None)
                try:
                    legacy.execute('INSERT INTO exclusions VALUES(?,?,?)',
                                   (decision['scopeId'],'path','wiki/legacy-unrecorded.md'))
                finally:legacy.close()
                with self.assertRaisesRegex(ValueError,'reader_base_revision_unproved'):
                    with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10):pass
                self.assertEqual(runtime.index.exclude(decision['scopeId'],path='wiki/newer-managed.md'),'excluded')
                with self.assertRaisesRegex(ValueError,'reader_base_revision_unproved'):
                    with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10):pass

    def test_warm_snapshot_observes_new_exclusion_before_head_replacement(self):
        opens=importlib.import_module('laya_reader_open')
        lease=importlib.import_module('laya_reader_lease')
        revisions=importlib.import_module('laya_publication_revision')
        with self.opened() as (runtime,_snapshot,row,_component):
            with lease.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as endpoint:
                decision=self.seed.reader.journal.select_route(str(self.case.project),action='recall')
                revisions.ensure_base_baseline(runtime.index,scope=decision['scopeId'],
                    profile_id=decision['vectorProfile']['profileId'],deadline=time.monotonic()+5)
                original=self.seed.reader._published_snapshot;committed=[]
                def exclude_after_pin(*args,**kwargs):
                    result=original(*args,**kwargs)
                    if not committed:
                        committed.append(runtime.index.exclude(decision['scopeId'],path=row[0]))
                    return result
                with mock.patch.object(self.seed.reader,'_published_snapshot',side_effect=exclude_after_pin):
                    with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10) as observed:
                        self.assertNotIn(('path',row[0]),observed.published[4])
                        self.assertEqual(observed.base_revision,0)
                self.assertEqual(committed,['excluded'])
                with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10) as observed:
                    self.assertIn(('path',row[0]),observed.published[4])
                    self.assertEqual(observed.base_revision,1)
                    self.assertLess(observed.generation_exclusion_epoch,len(observed.published[4]))
                    with self.assertRaisesRegex(ValueError,'reader_exclusions_changed'):
                        self.seed.reader._published_snapshot(observed.connections[0],decision,
                                                             deadline=time.monotonic()+5)

    def test_warm_observation_validates_all_pinned_components_and_route(self):
        opens=importlib.import_module('laya_reader_open')
        lease=importlib.import_module('laya_reader_lease')
        with self.opened() as (runtime,snapshot,row,component):
            writer=self.I._sidecar_open(self.identity,component)
            try:
                self.I._sidecar_setup(writer,self.identity,component)
                with mock.patch.object(self.I,'_open',return_value=writer), \
                        mock.patch.object(self.I,'_close',return_value=None):
                    self.assertEqual(self.publish(runtime,snapshot,row,component)['status'],'published')
                with lease.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as endpoint:
                    decision=self.seed.reader.journal.select_route(str(self.case.project),action='recall')
                    with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10) as observed:
                        self.assertEqual(observed.published[0],observed.connections[0].execute(
                            'SELECT gen FROM head WHERE scope=?',(self.case.route['scopeId'],)).fetchone()[0])
                        self.assertTrue(observed.sidecar['baseline'])
                        self.assertEqual(observed.sidecar['revision'],1)
                        self.assertTrue(all(db.in_transaction for db in observed.connections))
                        with mock.patch.object(self.seed.reader,'_sidecar_snapshot',
                                               side_effect=AssertionError('repeated whole-sidecar proof')):
                            document=self.seed.reader._sidecar_document_snapshot(
                                observed.connections[3],observed.connections[0],decision,row[0],
                                deadline=time.monotonic()+5)
                        self.assertEqual(document['hash'],row[1])
                        closed=observed.connections[0]
                    with self.assertRaises(sqlite3.ProgrammingError):closed.execute('SELECT 1')
                    actual=self.seed.reader.journal.select_route(str(self.case.project),action='recall')
                    changed=dict(actual,scopeId='wrong-scope')
                    with mock.patch.object(self.seed.reader.journal,'select_route',side_effect=[actual,changed]):
                        with self.assertRaisesRegex(ValueError,'reader_selection_changed'):
                            with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10):pass
                    with mock.patch.object(self.seed.reader.journal,'select_route',side_effect=[actual,actual,changed]):
                        with self.assertRaisesRegex(ValueError,'reader_selection_changed'):
                            with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10):pass
                    deadline=time.monotonic()+10
                    clock=mock.patch.object(opens.time,'monotonic',return_value=deadline+1)
                    try:
                        with self.assertRaisesRegex(ValueError,'reader_snapshot_deadline'):
                            with opens.inspect_warm_snapshot(endpoint,deadline=deadline):clock.start()
                    finally:clock.stop()
            finally:self.I._close(writer)

    def test_sidecar_receipts_bind_current_rows(self):
        opens=importlib.import_module('laya_reader_open')
        lease=importlib.import_module('laya_reader_lease')
        with self.opened() as (runtime,snapshot,row,component):
            writer=self.I._sidecar_open(self.identity,component)
            try:
                self.I._sidecar_setup(writer,self.identity,component)
                with mock.patch.object(self.I,'_open',return_value=writer), \
                        mock.patch.object(self.I,'_close',return_value=None):
                    self.assertEqual(self.publish(runtime,snapshot,row,component)['status'],'published')
                altered='synthetic coherent but unrecorded body'
                altered_hash=hashlib.sha256(altered.encode('utf8')).hexdigest()
                with self.W.write_connection(writer,component,visible=True,deadline=time.monotonic()+3):
                    rowid=writer.execute('SELECT rowid FROM docs WHERE path=?',(row[0],)).fetchone()[0]
                    writer.execute('UPDATE docs SET body=?,hash=? WHERE path=?',(altered,altered_hash,row[0]))
                    writer.execute('UPDATE docs_fts SET body=? WHERE rowid=?',(altered,rowid))
                with lease.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as endpoint:
                    decision=self.seed.reader.journal.select_route(str(self.case.project),action='recall')
                    callbacks={role:lambda db:db.execute('SELECT count(*) FROM sqlite_master').fetchone()[0]
                               for role in ('base','binding','catalog','sidecar')}
                    with opens.pin_warm_components(endpoint,callbacks,deadline=time.monotonic()+10) as pinned:
                        with self.assertRaisesRegex(ValueError,'reader_sidecar_revision_unproved'):
                            self.seed.reader._sidecar_snapshot(pinned.connections[3],decision,deadline=time.monotonic()+5)
            finally:self.I._close(writer)

    def test_sidecar_receipt_gap_is_rejected_in_pinned_snapshot(self):
        opens=importlib.import_module('laya_reader_open')
        lease=importlib.import_module('laya_reader_lease')
        with self.opened() as (runtime,snapshot,row,component):
            writer=self.I._sidecar_open(self.identity,component)
            try:
                self.I._sidecar_setup(writer,self.identity,component)
                with mock.patch.object(self.I,'_open',return_value=writer), \
                        mock.patch.object(self.I,'_close',return_value=None):
                    self.assertEqual(self.publish(runtime,snapshot,row,component)['status'],'published')
                with self.W.write_connection(writer,component,visible=True,deadline=time.monotonic()+3):
                    self.assertEqual(writer.execute('SELECT revision FROM publication_revisions ORDER BY revision').fetchall(),[(0,),(1,)])
                    revisions=importlib.import_module('laya_publication_revision')
                    before=revisions.sidecar_row(writer,row[0])
                    writer.execute("UPDATE docs SET scan_id=scan_id||'-next' WHERE path=?",(row[0],))
                    revisions.record_sidecar(writer,path=row[0],before=before)
                with lease.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as endpoint:
                    decision=self.seed.reader.journal.select_route(str(self.case.project),action='recall')
                    callbacks={role:lambda db:db.execute('SELECT count(*) FROM sqlite_master').fetchone()[0]
                               for role in ('base','binding','catalog','sidecar')}
                    with opens.pin_warm_components(endpoint,callbacks,deadline=time.monotonic()+10) as pinned:
                        self.assertEqual(self.seed.reader._sidecar_snapshot(pinned.connections[3],decision)['revision'],2)
                    with self.W.write_connection(writer,component,visible=True,deadline=time.monotonic()+3):
                        writer.execute('INSERT INTO publication_revisions SELECT 4,value FROM publication_revisions WHERE revision=1')
                    with opens.pin_warm_components(endpoint,callbacks,deadline=time.monotonic()+10) as pinned:
                        with self.assertRaisesRegex(ValueError,'reader_sidecar_revision_unproved'):
                            self.seed.reader._sidecar_snapshot(pinned.connections[3],decision)
            finally:self.I._close(writer)

    def test_pinned_sidecar_document_checks_body_and_cached_vectors(self):
        opens=importlib.import_module('laya_reader_open')
        lease=importlib.import_module('laya_reader_lease')
        with self.opened() as (runtime,snapshot,row,component):
            writer=self.I._sidecar_open(self.identity,component)
            try:
                self.I._sidecar_setup(writer,self.identity,component)
                with mock.patch.object(self.I,'_open',return_value=writer), \
                        mock.patch.object(self.I,'_close',return_value=None):
                    self.assertEqual(self.publish(runtime,snapshot,row,component)['status'],'published')
                with lease.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as endpoint:
                    decision=self.seed.reader.journal.select_route(str(self.case.project),action='recall')
                    callbacks={role:lambda db:db.execute('SELECT count(*) FROM sqlite_master').fetchone()[0]
                               for role in ('base','binding','catalog','sidecar')}
                    with opens.pin_warm_components(endpoint,callbacks,deadline=time.monotonic()+10) as pinned:
                        doc=self.seed.reader._sidecar_document_snapshot(pinned.connections[3],pinned.connections[0],
                            decision,row[0],deadline=time.monotonic()+5)
                        self.assertEqual((doc['path'],doc['hash'],doc['body']),(row[0],row[1],row[3]))
                    missing='synthetic body without cached vectors'
                    missing_hash=hashlib.sha256(missing.encode('utf8')).hexdigest()
                    with self.W.write_connection(writer,component,visible=True,deadline=time.monotonic()+3):
                        revisions=importlib.import_module('laya_publication_revision')
                        before=revisions.sidecar_row(writer,row[0])
                        rowid=writer.execute('SELECT rowid FROM docs WHERE path=?',(row[0],)).fetchone()[0]
                        writer.execute('UPDATE docs SET body=?,hash=? WHERE path=?',(missing,missing_hash,row[0]))
                        writer.execute('UPDATE docs_fts SET body=? WHERE rowid=?',(missing,rowid))
                        revisions.record_sidecar(writer,path=row[0],before=before)
                    with opens.pin_warm_components(endpoint,callbacks,deadline=time.monotonic()+10) as pinned:
                        with self.assertRaisesRegex(ValueError,'reader_sidecar_document_changed'):
                            self.seed.reader._sidecar_document_snapshot(pinned.connections[3],pinned.connections[0],
                                decision,row[0],deadline=time.monotonic()+5)
                        lexical=self.seed.reader._sidecar_document_snapshot(pinned.connections[3],pinned.connections[0],
                            decision,row[0],deadline=time.monotonic()+5,require_vectors=False)
                        self.assertEqual(lexical['body'],missing);self.assertIsNone(lexical['chunks'])
                    with self.W.write_connection(writer,component,visible=True,deadline=time.monotonic()+3):
                        writer.execute('UPDATE docs SET body=?,hash=? WHERE path=?',('tampered fixture',row[1],row[0]))
                    with opens.pin_warm_components(endpoint,callbacks,deadline=time.monotonic()+10) as pinned:
                        with self.assertRaisesRegex(ValueError,'reader_sidecar_document_changed'):
                            self.seed.reader._sidecar_document_snapshot(pinned.connections[3],pinned.connections[0],
                                decision,row[0],deadline=time.monotonic()+5)
            finally:self.I._close(writer)

    def test_pinned_capacity_rejects_changed_catalog_scan_contents(self):
        opens=importlib.import_module('laya_reader_open')
        lease=importlib.import_module('laya_reader_lease')
        with self.opened() as (runtime,snapshot,_row,_component):
            with lease.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as endpoint:
                decision=self.seed.reader.journal.select_route(str(self.case.project),action='recall')
                owner=runtime.catalog._connection_owner
                with self.W.write_connection(runtime.catalog.db,owner.component,visible=False,
                                             deadline=time.monotonic()+3):
                    row=runtime.catalog.db.execute('SELECT path,sha256 FROM scan_files WHERE seq=? ORDER BY path LIMIT 1',
                                                   (snapshot.seq,)).fetchone()
                    self.assertIsNotNone(row)
                    replacement=('0' if row[1][0]!='0' else '1')+row[1][1:]
                    runtime.catalog.db.execute('UPDATE scan_files SET sha256=? WHERE seq=? AND path=?',
                                               (replacement,snapshot.seq,row[0]))
                callbacks={role:lambda db:db.execute('SELECT count(*) FROM sqlite_master').fetchone()[0]
                           for role in ('base','binding','catalog','sidecar')}
                with opens.pin_warm_components(endpoint,callbacks,deadline=time.monotonic()+10) as pinned:
                    base=self.seed.reader._published_snapshot(pinned.connections[0],decision,
                                                               deadline=time.monotonic()+5)
                    with self.assertRaisesRegex(ValueError,'reader_catalog_scan_changed'):
                        self.seed.reader._capacity_snapshot(pinned.connections[1],pinned.connections[2],
                            Path(endpoint.record['binding_path']),decision,base)
                with self.assertRaisesRegex(ValueError,'reader_catalog_scan_changed'):
                    with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10):pass

    def test_common_pin_uses_and_releases_actual_cut_on_fallback(self):
        opens=importlib.import_module('laya_reader_open')
        common=importlib.import_module('laya_reader_lease')
        lease=importlib.import_module('laya_reader_lease')
        with self.opened():
            with lease.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as endpoint:
                _,parent_fd,_=lease._parent(endpoint.record['install_id'],create=False)
                fd=os.open('publish-cut.lock',os.O_RDONLY|os.O_NOFOLLOW,dir_fd=parent_fd)
                try:
                    attempts=[];calls=[];version=common._snapshot_version
                    def changed_twice(db):
                        actual=version(db);calls.append(True)
                        return actual+1 if len(calls) in (8,16) else actual
                    def base(db):
                        attempts.append(True)
                        if len(attempts)==3:
                            with self.assertRaises(BlockingIOError):
                                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                        return db.execute('SELECT count(*) FROM head').fetchone()[0]
                    callbacks={'base':base,'binding':lambda db:db.execute('SELECT count(*) FROM sqlite_master').fetchone()[0],
                               'catalog':lambda db:db.execute('SELECT count(*) FROM sqlite_master').fetchone()[0],
                               'sidecar':lambda db:db.execute('SELECT count(*) FROM docs').fetchone()[0]}
                    with mock.patch.object(common,'_snapshot_version',side_effect=changed_twice):
                        with opens.pin_warm_components(endpoint,callbacks,deadline=time.monotonic()+10) as pinned:
                            self.assertEqual(len(attempts),3)
                            self.assertTrue(all(db.in_transaction for db in pinned.connections))
                            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                            fcntl.flock(fd,fcntl.LOCK_UN)
                finally:
                    os.close(fd);os.close(parent_fd)

    def test_reader_publish_cut_holds_real_shared_lock(self):
        opens=importlib.import_module('laya_reader_open')
        lease=importlib.import_module('laya_reader_lease')
        with self.opened():
            with lease.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as endpoint:
                _, parent_fd, _ = lease._parent(endpoint.record['install_id'],create=False)
                try:
                    fd=os.open('publish-cut.lock',os.O_RDONLY|os.O_NOFOLLOW,dir_fd=parent_fd)
                    try:
                        with opens.reader_publish_cut(endpoint,deadline=time.monotonic()+3):
                            with self.assertRaises(BlockingIOError):
                                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                        fcntl.flock(fd,fcntl.LOCK_UN)
                    finally:os.close(fd)
                finally:os.close(parent_fd)

    def test_actual_sidecar_publication_between_component_pins_retries(self):
        opens=importlib.import_module('laya_reader_open')
        lease=importlib.import_module('laya_reader_lease')
        with self.opened() as (runtime,catalog_snapshot,row,component):
            writer=self.I._sidecar_open(self.identity,component)
            try:
                self.I._sidecar_setup(writer,self.identity,component)
                with lease.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as endpoint:
                    roles=(('base','db_path','head'),('binding','binding_path','sqlite_master'),
                           ('catalog','catalog_path','sqlite_master'),('sidecar','sidecar_path','docs'))
                    attempts=[];published=[]
                    def pin(role,table,db):
                        value=db.execute('SELECT count(*) FROM '+table).fetchone()[0]
                        if role=='base' and not published:
                            # Reuse the actual idle managed sidecar connection;
                            # the real publication code still validates and commits.
                            with mock.patch.object(self.I,'_open',return_value=writer), \
                                    mock.patch.object(self.I,'_close',return_value=None):
                                published.append(self.publish(runtime,catalog_snapshot,row,component)['status'])
                        return value
                    callbacks={role:(lambda db,role=role,table=table:
                               (attempts.append(True) if role=='base' else None, pin(role,table,db))[1])
                               for role,_key,table in roles}
                    with opens.pin_warm_components(endpoint,callbacks,deadline=time.monotonic()+10) as pinned:
                        self.assertEqual(published,['published']);self.assertEqual(len(attempts),2)
                        decision=self.seed.reader.journal.select_route(str(self.case.project),action='recall')
                        base=self.seed.reader._published_snapshot(pinned.connections[0],decision,
                                                                 deadline=time.monotonic()+5)
                        self.seed.reader._capacity_snapshot(pinned.connections[1],pinned.connections[2],
                            Path(endpoint.record['binding_path']),decision,base)
                        side=self.seed.reader._sidecar_snapshot(pinned.connections[3],decision)
                        self.assertEqual((side['rows'],side['baseline'],side['revision']),(3,True,1))
                        self.assertIsNotNone(pinned.connections[3].execute('SELECT 1 FROM docs WHERE path=?',(row[0],)).fetchone())
            finally:self.I._close(writer)

    def test_actual_exclusion_between_component_pins_retries_before_search(self):
        from types import SimpleNamespace
        opens=importlib.import_module('laya_reader_open')
        lease=importlib.import_module('laya_reader_lease')
        revisions=importlib.import_module('laya_publication_revision')
        index=importlib.import_module('laya_index_a')
        host=importlib.import_module('laya_host_response')
        with self.opened() as (runtime,_catalog,_row,_component):
            decision=self.seed.reader.journal.select_route(str(self.case.project),action='recall')
            def no_vector(_prompt,_profile):
                raise host._VectorUnavailable('fixture')
            rows,_=host.query_selected_v2(runtime,'otter',decision['vectorProfile'],
                                          decision['vectorProfile'],no_vector,decision)
            self.assertTrue(rows)
            path=rows[0]['path']
            revisions.ensure_base_baseline(runtime.index,scope=decision['scopeId'],
                profile_id=decision['vectorProfile']['profileId'],deadline=time.monotonic()+5)
            with lease.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as endpoint:
                attempts=[];committed=[]
                def base(db):
                    result=db.execute('SELECT count(*) FROM head').fetchone()[0]
                    attempts.append(True)
                    if not committed:
                        committed.append(runtime.index.exclude(decision['scopeId'],path=path))
                    return result
                callbacks={'base':base,
                           'binding':lambda db:db.execute('SELECT count(*) FROM sqlite_master').fetchone()[0],
                           'catalog':lambda db:db.execute('SELECT count(*) FROM sqlite_master').fetchone()[0],
                           'sidecar':lambda db:db.execute('SELECT count(*) FROM docs').fetchone()[0]}
                with opens.pin_warm_components(endpoint,callbacks,deadline=time.monotonic()+10) as pinned:
                    self.assertEqual(committed,['excluded'])
                    self.assertEqual(len(attempts),2)
                    published=self.seed.reader._published_snapshot(pinned.connections[0],decision,
                        deadline=time.monotonic()+5,allow_exclusion_advance=True)
                    self.assertIn(('path',path),published[4])
                    self.assertEqual(self.seed.reader._base_revision_snapshot(
                        pinned.connections[0],decision,published),1)
                    binding,_=self.seed.reader._binding_snapshot(pinned.connections[1],
                        Path(endpoint.record['binding_path']),decision,published)
                    snapshot_index=index.SnapshotIndex(pinned.connections[0],scope=decision['scopeId'],
                        generation=published[0],incarnation=decision['dbIncarnation'],deadline=time.monotonic()+5)
                    rows,_=host.query_selected_v2(SimpleNamespace(index=snapshot_index,scope=decision['scopeId']),
                        'otter',decision['vectorProfile'],decision['vectorProfile'],no_vector,decision,
                        _snapshot_binding=binding)
                    self.assertNotIn(path,[item['path'] for item in rows])

    def test_external_sidecar_commit_between_pins_retries(self):
        opens=importlib.import_module('laya_reader_open')
        lease=importlib.import_module('laya_reader_lease')
        with self.opened() as (_runtime,_catalog,_row,component):
            writer=self.I._sidecar_open(self.identity,component)
            try:
                self.I._sidecar_setup(writer,self.identity,component)
                path=writer.execute('SELECT path FROM docs ORDER BY path LIMIT 1').fetchone()[0]
                child_code = '''
import sqlite3, sys
sys.path.insert(0, sys.argv[1])
from laya_publication_revision import sidecar_row, record_sidecar
db = sqlite3.connect(sys.argv[2], isolation_level=None, timeout=2)
try:
    db.execute('BEGIN IMMEDIATE')
    before = sidecar_row(db, sys.argv[3])
    rowid = db.execute('SELECT rowid FROM docs WHERE path=?', (sys.argv[3],)).fetchone()[0]
    db.execute('DELETE FROM docs_fts WHERE rowid=?', (rowid,))
    db.execute('DELETE FROM docs WHERE rowid=?', (rowid,))
    record_sidecar(db, path=sys.argv[3], before=before)
    db.commit()
finally:
    db.close()
'''
                with lease.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as endpoint:
                    attempts=[];commits=[]
                    def base(db):
                        value=db.execute('SELECT count(*) FROM head').fetchone()[0]
                        attempts.append(True)
                        if not commits:
                            child=subprocess.run([sys.executable,'-B','-c',child_code,str(ROOT/'core'),
                                                  str(self.fixture.sidecar),path],
                                                 capture_output=True,text=True,timeout=10)
                            self.assertEqual(child.returncode,0,child.stderr)
                            commits.append(True)
                        return value
                    callbacks={'base':base,
                               'binding':lambda db:db.execute('SELECT count(*) FROM sqlite_master').fetchone()[0],
                               'catalog':lambda db:db.execute('SELECT count(*) FROM sqlite_master').fetchone()[0],
                               'sidecar':lambda db:db.execute('SELECT count(*) FROM docs').fetchone()[0]}
                    with opens.pin_warm_components(endpoint,callbacks,deadline=time.monotonic()+20) as pinned:
                        self.assertEqual(commits,[True])
                        self.assertEqual(len(attempts),2)
                        decision=self.seed.reader.journal.select_route(str(self.case.project),action='recall')
                        side=self.seed.reader._sidecar_snapshot(pinned.connections[3],decision,
                                                                deadline=time.monotonic()+5)
                        self.assertEqual((side['rows'],side['baseline'],side['revision']),(1,True,1))
                        self.assertIsNone(pinned.connections[3].execute(
                            'SELECT 1 FROM docs WHERE path=?',(path,)).fetchone())
            finally:self.I._close(writer)

    def test_snapshot_validation_interrupts_a_long_sql_statement(self):
        opens=importlib.import_module('laya_reader_open')
        lease=importlib.import_module('laya_reader_lease')
        real_clock=time.monotonic
        late=[]
        def clock():
            return real_clock() + (20 if late else 0)
        def slow(side,_decision,*,deadline):
            late.append(True)
            side.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL '
                         'SELECT x+1 FROM n WHERE x<50000) SELECT sum(x) FROM n').fetchone()
            raise AssertionError('long validation SQL was not interrupted')
        with self.opened():
            with lease.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as endpoint:
                deadline=real_clock()+10
                with mock.patch.object(opens.time,'monotonic',side_effect=clock), \
                        mock.patch.object(self.seed.reader,'_sidecar_snapshot',side_effect=slow):
                    with self.assertRaisesRegex(ValueError,'reader_snapshot_deadline'):
                        with opens.inspect_warm_snapshot(endpoint,deadline=deadline):
                            self.fail('expired validation returned a snapshot')
                self.assertEqual(late,[True])
                self.assertEqual(opens.drain_pending(deadline=real_clock()+2),0)

    def test_snapshot_pin_interrupts_a_long_sql_statement(self):
        opens=importlib.import_module('laya_reader_open')
        lease=importlib.import_module('laya_reader_lease')
        real_clock=time.monotonic
        late=[];attempts=[];versions=[]
        version=lease._snapshot_version
        def clock():
            return real_clock()+(20 if late else 0)
        def changed_twice(db):
            actual=version(db);versions.append(True)
            return actual+1 if len(versions) in (8,16) else actual
        def slow(db):
            attempts.append(True)
            if len(attempts)<3:
                return db.execute('SELECT count(*) FROM head').fetchone()[0]
            with self.assertRaises(BlockingIOError):
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            late.append(True)
            db.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL '
                       'SELECT x+1 FROM n WHERE x<50000) SELECT sum(x) FROM n').fetchone()
            raise AssertionError('long pin SQL was not interrupted')
        callbacks={'base':slow,'binding':lambda db:db.execute('SELECT 1').fetchone(),
                   'catalog':lambda db:db.execute('SELECT 1').fetchone(),
                   'sidecar':lambda db:db.execute('SELECT 1').fetchone()}
        with self.opened():
            with lease.resolve_endpoint(self.case.project,action='recall',host_root=self.case.plugin) as endpoint:
                _,parent_fd,_=lease._parent(endpoint.record['install_id'],create=False)
                fd=os.open('publish-cut.lock',os.O_RDONLY|os.O_NOFOLLOW,dir_fd=parent_fd)
                try:
                    deadline=real_clock()+10
                    with mock.patch.object(opens.time,'monotonic',side_effect=clock), \
                            mock.patch.object(lease,'_snapshot_version',side_effect=changed_twice):
                        with self.assertRaisesRegex(ValueError,'reader_snapshot_deadline'):
                            with opens.pin_warm_components(endpoint,callbacks,deadline=deadline):
                                self.fail('expired pin returned')
                    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    fcntl.flock(fd,fcntl.LOCK_UN)
                finally:
                    os.close(fd);os.close(parent_fd)
                self.assertEqual(late,[True])
                self.assertEqual(len(attempts),3)
                self.assertEqual(opens.drain_pending(deadline=real_clock()+2),0)

class AbsentSidecarSnapshotChecks(unittest.TestCase):
    def test_conversion_sidecar_disappearance_is_refused(self):
        seed=existing.existing.existing.seed_module.MoveSealChecks('runTest')
        self.addCleanup(seed.doCleanups);seed.setUp()
        held=seed.case.home/'held-test-sidecar.sqlite'
        with closing(sqlite3.connect(seed.sidecar,isolation_level=None)) as db:
            self.assertEqual(db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0],0)
        seed.sidecar.rename(held)
        self.addCleanup(lambda: held.rename(seed.sidecar) if held.exists() else None)
        opens=importlib.import_module('laya_reader_open')
        reader=importlib.import_module('laya_v6_reader')
        with seed.authority(),seed.lease.resolve_endpoint(seed.case.project,action='recall',host_root=seed.case.plugin) as endpoint:
            decision=seed.reader.journal.select_route(str(seed.case.project),action='recall')
            with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10) as observed:
                self.assertIsNone(observed.sidecar)
                with self.assertRaisesRegex(ValueError,'reader_sidecar_revision_unproved'):
                    reader.query_v6_snapshot(observed,endpoint=endpoint,prompt='otter',decision=decision,provider=None,deadline=time.monotonic()+10)

    def test_conversion_rejects_legacy_docs_fts_mismatch(self):
        class BrokenSeed(existing.existing.existing.seed_module.MoveSealChecks):
            def before_conversion(self):
                with sqlite3.connect(self.sidecar) as db:
                    db.execute('DELETE FROM docs_fts WHERE rowid=(SELECT min(rowid) FROM docs_fts)')
        seed=BrokenSeed('runTest')
        self.addCleanup(seed.doCleanups)
        with self.assertRaisesRegex(ValueError,'reader_sidecar_revision_unproved'):
            seed.setUp()

    def test_conversion_seals_legacy_sidecar_for_search(self):
        class LegacySeed(existing.existing.existing.seed_module.MoveSealChecks):
            def before_conversion(self):
                with sqlite3.connect(self.sidecar) as db:
                    target=db.execute("SELECT rowid,path FROM docs WHERE body NOT LIKE '%otter%' ORDER BY path LIMIT 1").fetchone()
                    replacement=db.execute("SELECT hash,title,body,profile_id,chunk_count FROM docs WHERE body LIKE '%otter%' ORDER BY path LIMIT 1").fetchone()
                    db.execute('DELETE FROM docs_fts WHERE rowid=?',(target[0],))
                    db.execute('UPDATE docs SET hash=?,title=?,body=?,profile_id=?,chunk_count=? WHERE rowid=?',(*replacement,target[0]))
                    db.execute('INSERT INTO docs_fts(rowid,path,title,body) VALUES(?,?,?,?)',(target[0],target[1],replacement[1],replacement[2]))
                    self.legacy_target=target[1]
        seed=LegacySeed('runTest')
        self.addCleanup(seed.doCleanups);seed.setUp()
        opens=importlib.import_module('laya_reader_open')
        reader=importlib.import_module('laya_v6_reader')
        with seed.authority(),seed.lease.resolve_endpoint(seed.case.project,action='recall',host_root=seed.case.plugin) as endpoint:
            decision=seed.reader.journal.select_route(str(seed.case.project),action='recall')
            with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10) as observed:
                self.assertFalse(observed.sidecar['baseline'])
                result=reader.query_v6_snapshot(observed,endpoint=endpoint,prompt='otter',decision=decision,provider=None,deadline=time.monotonic()+10)
                self.assertTrue(any(row['path']==seed.legacy_target and 'otter' in row['body'] for row in result.rows))
                dispatch=importlib.import_module('laya_reader_dispatch')
                via_adapter=dispatch.query_endpoint(endpoint,checkout=seed.case.project,prompt='otter',
                    query_vector=None,deadline=time.monotonic()+10,snapshot=observed,decision=decision,provider=None)
                self.assertEqual(via_adapter,result)
        with sqlite3.connect(seed.sidecar) as db:
            rowid,block=db.execute('SELECT id,block FROM docs_fts_data WHERE length(block)>0 ORDER BY id LIMIT 1').fetchone()
            db.execute('UPDATE docs_fts_data SET block=? WHERE id=?',(block+b'\x00',rowid))
        with seed.authority(),seed.lease.resolve_endpoint(seed.case.project,action='recall',host_root=seed.case.plugin) as endpoint:
            decision=seed.reader.journal.select_route(str(seed.case.project),action='recall')
            with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10) as observed:
                with self.assertRaisesRegex(ValueError,'reader_sidecar_revision_unproved'):
                    reader.query_v6_snapshot(observed,endpoint=endpoint,prompt='otter',decision=decision,provider=None,deadline=time.monotonic()+10)
        with sqlite3.connect(seed.sidecar) as db:
            db.execute('UPDATE docs_fts_data SET block=? WHERE id=?',(block,rowid))
        self.seal_sidecar(seed)
        with seed.authority(),seed.lease.resolve_endpoint(seed.case.project,action='recall',host_root=seed.case.plugin) as endpoint:
            decision=seed.reader.journal.select_route(str(seed.case.project),action='recall')
            with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10) as observed:
                self.assertTrue(observed.sidecar['baseline'])
                result=reader.query_v6_snapshot(observed,endpoint=endpoint,prompt='otter',decision=decision,provider=None,deadline=time.monotonic()+10)
                self.assertTrue(any(row['path']==seed.legacy_target for row in result.rows))

    def seal_sidecar(self, seed):
        windows=importlib.import_module('laya_reader_windows')
        incremental=importlib.import_module('laya_incremental_docs')
        identity=importlib.import_module('laya_initial_build').read(seed.case.state)['identity']
        with seed.prepared() as intent:
            try:
                windows.prepare_windows(seed.installer)
                component=windows.component_for(seed.installer,'sidecar')
                db=incremental._open(seed.case.state,coordination=component,identity=identity)
                try:incremental._sidecar_setup(db,identity,component)
                finally:incremental._close(db)
            finally:seed.mover._abort(intent)

    def test_unsealed_existing_sidecar_is_not_a_searchable_snapshot(self):
        seed=existing.existing.existing.seed_module.MoveSealChecks('runTest')
        self.addCleanup(seed.doCleanups);seed.setUp()
        opens=importlib.import_module('laya_reader_open')
        reader=importlib.import_module('laya_v6_reader')
        legacy=importlib.import_module('laya_incremental_docs_query')
        sidefile=seed.case.state/importlib.import_module('laya_incremental_docs').FILE
        with sqlite3.connect(sidefile) as db:
            target=db.execute("SELECT rowid,path FROM docs WHERE body NOT LIKE '%otter%' ORDER BY path LIMIT 1").fetchone()
            replacement=db.execute("SELECT hash,title,body,profile_id,chunk_count FROM docs "
                                   "WHERE body LIKE '%otter%' ORDER BY path LIMIT 1").fetchone()
            self.assertIsNotNone(target);self.assertIsNotNone(replacement)
            db.execute('DELETE FROM docs_fts WHERE rowid=?',(target[0],))
            db.execute('UPDATE docs SET hash=?,title=?,body=?,profile_id=?,chunk_count=? WHERE rowid=?',
                       (*replacement,target[0]))
            db.execute('INSERT INTO docs_fts(rowid,path,title,body) VALUES(?,?,?,?)',
                       (target[0],target[1],replacement[1],replacement[2]))
        with seed.authority(),seed.lease.resolve_endpoint(seed.case.project,action='recall',
                                                          host_root=seed.case.plugin) as endpoint:
            decision=seed.reader.journal.select_route(str(seed.case.project),action='recall')
            old_rows,*_=legacy.query_incremental('otter',decision,
                {'vectorProfile':decision['vectorProfile']},None)
            self.assertTrue(any(row['path']==target[1] and 'otter' in row['body'] for row in old_rows))
            with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10) as observed:
                self.assertEqual(observed.sidecar['baseline'],False)
                self.assertEqual(observed.sidecar['rows'],3)
                with self.assertRaisesRegex(ValueError,'reader_sidecar_revision_unproved'):
                    reader.query_v6_snapshot(observed,endpoint=endpoint,prompt='otter',
                        decision=decision,provider=None,deadline=time.monotonic()+10)
        with self.assertRaisesRegex(ValueError,'reader_sidecar_revision_unproved'):
            self.seal_sidecar(seed)

    def test_existing_snapshot_survives_real_reader_selection_cas(self):
        seed=existing.existing.existing.seed_module.MoveSealChecks('runTest')
        self.addCleanup(seed.doCleanups);seed.setUp()
        self.seal_sidecar(seed)
        opens=importlib.import_module('laya_reader_open')
        reader=importlib.import_module('laya_v6_reader')
        other=seed.helper.alternate(seed.case,seed.reader,seed.installer,'second-reader.sqlite')
        with seed.authority():
            with seed.lease.resolve_endpoint(seed.case.project,action='recall',host_root=seed.case.plugin) as endpoint:
                decision=seed.reader.journal.select_route(str(seed.case.project),action='recall')
                old=seed.reader.read_selection(seed.case.journal)
                with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10) as observed:
                    changed=seed.reader.cas_reader(seed.case.journal,expected_epoch=old.epoch,
                        expected_endpoint=old.preferred_endpoint,target=other,transition_id='c'*32)
                    self.assertNotEqual(changed.preferred_endpoint,endpoint.endpoint_id)
                    result=reader.query_v6_snapshot(observed,endpoint=endpoint,prompt='otter',
                        decision=decision,provider=None,deadline=time.monotonic()+10)
                    self.assertEqual((result.endpoint_id,result.reader_epoch),(endpoint.endpoint_id,old.epoch))
                    self.assertTrue(any('otter' in row['body'] for row in result.rows))

    def test_selected_cold_snapshot_preserves_main_databases_and_reads_sidecar(self):
        seed=existing.existing.existing.seed_module.MoveSealChecks('runTest')
        self.addCleanup(seed.doCleanups);seed.setUp()
        self.seal_sidecar(seed)
        paths=(seed.case.db,seed.case.queue,seed.case.catalog,seed.sidecar)
        for path in paths:
            with closing(sqlite3.connect(path,isolation_level=None)) as db:
                self.assertEqual(db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0],0)
            self.assertFalse(any(Path(str(path)+suffix).exists() for suffix in ('-wal','-shm','-journal')))
        before={path:path.read_bytes() for path in (*paths,seed.case.store,seed.case.journal)}
        opens=importlib.import_module('laya_reader_open')
        with seed.authority(),seed.lease.resolve_endpoint(seed.case.project,action='recall',
                                                          host_root=seed.case.plugin) as endpoint:
            with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10) as observed:
                self.assertEqual(observed.sidecar['rows'],3)
                self.assertTrue(all(db.execute('PRAGMA query_only').fetchone()==(1,)
                                    and db.in_transaction for db in observed.connections))
                for db in observed.connections:
                    with self.assertRaises(sqlite3.DatabaseError):
                        db.rollback()
                generation=observed.published[0]
                base=observed.connections[0]
                ordinal=base.execute('SELECT ord FROM gen_index WHERE gen=?',(generation,)).fetchone()[0]
                from laya_index_a import _gen_fts
                table=_gen_fts(ordinal)
                rows=base.execute('SELECT c.body FROM "'+table+'" f JOIN membership m ON m.path=f.path_key '
                    'AND m.gen=? JOIN content c ON c.hash=m.hash WHERE "'+table+'" MATCH ?',
                    (generation,'otter')).fetchall()
                self.assertTrue(any('otter' in row[0] for row in rows))
                side=observed.connections[3]
                self.assertGreater(side.execute("SELECT count(*) FROM docs_fts WHERE docs_fts MATCH 'otter'").fetchone()[0],0)
                incremental_query=importlib.import_module('laya_incremental_docs_query')
                decision=seed.reader.journal.select_route(str(seed.case.project),action='recall')
                with mock.patch.object(incremental_query,'_readonly',
                                       side_effect=AssertionError('unexpected database reopen')):
                    result=incremental_query.query_incremental_snapshot(
                        'otter',decision,{'vectorProfile':decision['vectorProfile']},None,
                        base=base,side=side,deadline=time.monotonic()+10)
                # Every completed sidecar row already belongs to the pinned head.
                self.assertEqual(result[0],[])
                self.assertEqual(result[3],())
                self.assertEqual(result[4],generation)
                self.assertTrue(base.in_transaction and side.in_transaction)
                reader=importlib.import_module('laya_v6_reader')
                original_open=os.open;original_connect=sqlite3.connect
                project_files=[];file_connections=[];writes=[]
                write_actions={sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE,
                               sqlite3.SQLITE_CREATE_TABLE,sqlite3.SQLITE_DROP_TABLE,
                               sqlite3.SQLITE_CREATE_INDEX,sqlite3.SQLITE_DROP_INDEX,
                               sqlite3.SQLITE_CREATE_TRIGGER,sqlite3.SQLITE_DROP_TRIGGER,
                               sqlite3.SQLITE_ATTACH,sqlite3.SQLITE_DETACH}
                def authorizer(action,arg1,arg2,_database,_source):
                    metadata_pragmas={'table_info','table_xinfo','index_list','index_info',
                                      'index_xinfo','foreign_key_list'}
                    if (action in write_actions or (action==sqlite3.SQLITE_PRAGMA and arg2 is not None
                                                     and arg1 not in metadata_pragmas)):
                        writes.append((action,arg1));return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                def traced_open(*args,**kwargs):
                    fd=original_open(*args,**kwargs)
                    if stat.S_ISREG(os.fstat(fd).st_mode):
                        name=(os.fsdecode(fcntl.fcntl(fd,fcntl.F_GETPATH,b'\0'*1024).split(b'\0',1)[0])
                              if hasattr(fcntl,'F_GETPATH') else os.readlink(f'/proc/self/fd/{fd}'))
                        try:Path(name).relative_to(seed.case.project)
                        except ValueError:pass
                        else:project_files.append(name)
                    return fd
                def traced_connect(path,*args,**kwargs):
                    if str(path) != ':memory:':file_connections.append(str(path))
                    return original_connect(path,*args,**kwargs)
                for db in observed.connections:db.set_authorizer(authorizer)
                index_module=importlib.import_module('laya_index_a')
                from dataclasses import replace
                with self.assertRaisesRegex(ValueError,'reader_snapshot_unpinned'):
                    index_module.SnapshotIndex(base,scope=decision['scopeId'],
                        generation=observed.published[0],incarnation=decision['dbIncarnation'],
                        deadline=time.monotonic()+10,_observation=replace(observed))
                try:
                    with mock.patch.object(os,'open',side_effect=traced_open) as monitored_open, \
                         mock.patch.object(os,'supports_dir_fd',os.supports_dir_fd | {monitored_open}), \
                         mock.patch.object(sqlite3,'connect',side_effect=traced_connect), \
                         mock.patch.object(index_module,'_generation_fts_rows_match',
                                           wraps=index_module._generation_fts_rows_match) as fts_check:
                        lexical=reader.query_v6_snapshot(observed,endpoint=endpoint,prompt='otter',
                            decision=decision,provider=None,deadline=time.monotonic()+10)
                        self.assertEqual(fts_check.call_count,0)
                finally:
                    for db in observed.connections:db.set_authorizer(None)
                self.assertEqual(writes,[])
                self.assertEqual(project_files,[])
                self.assertEqual(file_connections,[])
                self.assertTrue(any('otter' in row['body'] for row in lexical.rows))
                self.assertEqual(lexical.semantic_status,'lexical_only')
                def warm(_prompt,profile):
                    return {'profileId':profile['profileId'],
                            'approvedProviderId':profile['approvedProviderId'],
                            'providerArtifactSha256':profile['providerArtifactSha256'],
                            'vector':[1.0,0.0,0.0]}
                vector_module=importlib.import_module('laya_index_vector')
                with mock.patch.object(vector_module,'verify_generation_seal',
                                       wraps=vector_module.verify_generation_seal) as seal:
                    hybrid=reader.query_v6_snapshot(observed,endpoint=endpoint,prompt='semanticnoncemissing',
                        decision=decision,provider=warm,deadline=time.monotonic()+10)
                    self.assertEqual(seal.call_count,0)
                self.assertTrue(hybrid.rows)
                self.assertEqual(hybrid.semantic_status,'hybrid')
                for reason in ('vector_budget_exceeded','vector_deadline_exceeded'):
                    with mock.patch.object(vector_module.LayaVectorIndex,'search_exact',
                            side_effect=index_module.IndexContractError(reason)):
                        deferred=reader.query_v6_snapshot(observed,endpoint=endpoint,prompt='otter',
                            decision=decision,provider=warm,deadline=time.monotonic()+10)
                    self.assertEqual(deferred.rows,lexical.rows)
                    self.assertEqual(deferred.semantic_status,'lexical_only')
                with mock.patch.object(vector_module.LayaVectorIndex,'search_exact',
                        side_effect=index_module.IndexContractError('vector_seal_invalid')):
                    with self.assertRaisesRegex(ValueError,'vector_seal_invalid'):
                        reader.query_v6_snapshot(observed,endpoint=endpoint,prompt='otter',
                            decision=decision,provider=warm,deadline=time.monotonic()+10)
                attempts=[]
                def changed_provider(text,profile):
                    attempts.append(text)
                    return {**warm(text,profile),'providerArtifactSha256':'0'*64}
                degraded=reader.query_v6_snapshot(observed,endpoint=endpoint,prompt='otter',
                    decision=decision,provider=changed_provider,deadline=time.monotonic()+10)
                self.assertEqual(degraded.rows,lexical.rows)
                self.assertEqual(degraded.semantic_status,'lexical_only')
                self.assertEqual(attempts,['otter'])
                def refused_provider(_text,_profile):
                    raise importlib.import_module('laya_index_a').IndexContractError('provider_identity_mismatch')
                refused=reader.query_v6_snapshot(observed,endpoint=endpoint,prompt='otter',
                    decision=decision,provider=refused_provider,deadline=time.monotonic()+10)
                self.assertEqual(refused.rows,lexical.rows)
                self.assertEqual(refused.semantic_status,'lexical_only')
                def broken_provider(_text,_profile):
                    raise RuntimeError('unexpected_provider_failure')
                with self.assertRaisesRegex(RuntimeError,'unexpected_provider_failure'):
                    reader.query_v6_snapshot(observed,endpoint=endpoint,prompt='otter',
                        decision=decision,provider=broken_provider,deadline=time.monotonic()+10)
        self.assertEqual({path:path.read_bytes() for path in before},before)

    def test_selected_sidecar_absence_is_pinned_without_creation(self):
        seed=existing.existing.existing.seed_module.MoveSealChecks('runTest')
        seed.PUBLISH_SIDECAR=False
        self.addCleanup(seed.doCleanups);seed.setUp()
        opens=importlib.import_module('laya_reader_open')
        callbacks={role:lambda db:db.execute('SELECT count(*) FROM sqlite_master').fetchone()[0]
                   for role in ('base','binding','catalog','sidecar')}
        paths=[Path(str(seed.sidecar)+suffix) for suffix in ('','-wal','-shm','-journal')]
        self.assertFalse(any(os.path.lexists(path) for path in paths))
        for path in (seed.case.db,seed.case.queue,seed.case.catalog):
            with closing(sqlite3.connect(path,isolation_level=None)) as db:
                self.assertEqual(db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0],0)
            self.assertFalse(any(Path(str(path)+suffix).exists() for suffix in ('-wal','-shm','-journal')))
        originals={path:path.read_bytes() for path in
                   (seed.case.db,seed.case.queue,seed.case.catalog,seed.case.store,seed.case.journal)}
        real_connect=sqlite3.connect;connections=[];unsafe=[]
        from urllib.parse import urlsplit,parse_qs
        def traced_connect(path,*args,**kwargs):
            db=real_connect(path,*args,**kwargs)
            if str(path)==':memory:':return db
            uri=str(path);connections.append(uri)
            if not kwargs.get('uri') or parse_qs(urlsplit(uri).query).get('mode') != ['ro']:
                unsafe.append(('connection_mode',uri))
            state={'query_only':False}
            def authorizer(action,arg1,arg2,_database,_source):
                if action==sqlite3.SQLITE_PRAGMA and arg1=='query_only' and str(arg2).upper() in ('ON','1'):
                    state['query_only']=True
                if action==sqlite3.SQLITE_SELECT and not state['query_only']:
                    unsafe.append(('select_without_query_only',uri))
                if action in (sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE,
                              sqlite3.SQLITE_CREATE_TABLE,sqlite3.SQLITE_DROP_TABLE):
                    unsafe.append(('write_sql',uri))
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            db.set_authorizer(authorizer)
            return db
        with seed.authority(),mock.patch.object(sqlite3,'connect',side_effect=traced_connect):
            with seed.lease.resolve_endpoint(seed.case.project,action='recall',host_root=seed.case.plugin) as endpoint:
                _,parent_fd,_=seed.lease._parent(seed.install,create=False)
                fd=os.open('sidecar-physical.lock',os.O_RDONLY|os.O_NOFOLLOW,dir_fd=parent_fd)
                try:
                    with opens.pin_warm_components(endpoint,callbacks,deadline=time.monotonic()+10) as pinned:
                        self.assertIsNone(pinned.connections[3]);self.assertIsNone(pinned.values[3])
                        self.assertFalse(any(os.path.lexists(path) for path in paths))
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);fcntl.flock(fd,fcntl.LOCK_UN)
                    with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10) as observed:
                        self.assertIsNone(observed.sidecar);self.assertIsNone(observed.connections[3])
                        self.assertTrue(all(db.execute('PRAGMA query_only').fetchone()==(1,)
                                            for db in observed.connections[:3]))
                        self.assertFalse(any(os.path.lexists(path) for path in paths))
                        reader=importlib.import_module('laya_v6_reader')
                        decision=seed.reader.journal.select_route(str(seed.case.project),action='recall')
                        answer=reader.query_v6_snapshot(observed,endpoint=endpoint,prompt='otter',
                            decision=decision,provider=None,deadline=time.monotonic()+10)
                        self.assertTrue(any('otter' in row['body'] for row in answer.rows))
                        self.assertFalse(any(os.path.lexists(path) for path in paths))
                finally:os.close(fd);os.close(parent_fd)
        self.assertFalse(any(os.path.lexists(path) for path in paths))
        self.assertTrue(connections)
        self.assertEqual(unsafe,[])
        self.assertEqual({path:path.read_bytes() for path in originals},originals)

if __name__=='__main__':
    suite=unittest.TestSuite([SnapshotRaceChecks('test_pinned_index_reuses_candidate_verification_without_writes'),
                              SnapshotRaceChecks('test_selected_snapshot_search_shadows_an_older_base_document'),
                              SnapshotRaceChecks('test_existing_search_sql_works_on_caller_owned_snapshot'),
                              SnapshotRaceChecks('test_actual_sidecar_publication_between_component_pins_retries'),
                              SnapshotRaceChecks('test_actual_exclusion_between_component_pins_retries_before_search'),
                              SnapshotRaceChecks('test_external_sidecar_commit_between_pins_retries'),
                              SnapshotRaceChecks('test_snapshot_validation_interrupts_a_long_sql_statement'),
                              SnapshotRaceChecks('test_snapshot_pin_interrupts_a_long_sql_statement'),
                              SnapshotRaceChecks('test_unrecorded_legacy_exclusion_after_revision_is_refused'),
                              SnapshotRaceChecks('test_reader_publish_cut_holds_real_shared_lock'),
                              SnapshotRaceChecks('test_common_pin_uses_and_releases_actual_cut_on_fallback'),
                              AbsentSidecarSnapshotChecks('test_conversion_sidecar_disappearance_is_refused'),
                              AbsentSidecarSnapshotChecks('test_conversion_rejects_legacy_docs_fts_mismatch'),
                              AbsentSidecarSnapshotChecks('test_conversion_seals_legacy_sidecar_for_search'),
                              AbsentSidecarSnapshotChecks('test_unsealed_existing_sidecar_is_not_a_searchable_snapshot'),
                              AbsentSidecarSnapshotChecks('test_existing_snapshot_survives_real_reader_selection_cas'),
                              AbsentSidecarSnapshotChecks('test_selected_cold_snapshot_preserves_main_databases_and_reads_sidecar'),
                              AbsentSidecarSnapshotChecks('test_selected_sidecar_absence_is_pinned_without_creation'),
                              SnapshotRaceChecks('test_pinned_capacity_rejects_changed_catalog_scan_contents'),
                              SnapshotRaceChecks('test_pinned_sidecar_document_checks_body_and_cached_vectors'),
                              SnapshotRaceChecks('test_sidecar_receipt_gap_is_rejected_in_pinned_snapshot'),
                              SnapshotRaceChecks('test_sidecar_receipts_bind_current_rows'),
                              SnapshotRaceChecks('test_warm_observation_validates_all_pinned_components_and_route'),
                              SnapshotRaceChecks('test_warm_snapshot_observes_new_exclusion_before_head_replacement')])
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())

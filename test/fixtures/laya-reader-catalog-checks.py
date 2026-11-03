#!/usr/bin/env python3
"""Managed catalog mutation admission and short visible state transitions."""
from contextlib import contextmanager
import importlib
import importlib.util
from pathlib import Path
import time
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('catalog_queue_helpers',ROOT/'test/fixtures/laya-reader-queue-mutations-checks.py')
existing=importlib.util.module_from_spec(spec);spec.loader.exec_module(existing)

class CatalogChecks(existing.QueueMutationChecks):
    @contextmanager
    def catalog(self):
        with self.fixture.writer() as (_base,_component):
            self.C=importlib.import_module('laya_source_catalog')
            component=self.W.component_for(self.seed.installer,'catalog')
            catalog=self.C.LayaSourceCatalog(self.case.catalog,{self.case.WIKI:str(self.case.auto/'wiki')},
                self.case.route['scopeId'],wiki_only=True,coordination=component)
            try:yield catalog,component
            finally:catalog.close(deadline=time.monotonic()+1);self.W.drain_pending(deadline=time.monotonic()+1)
    @staticmethod
    def rows(catalog):
        return {name:catalog.db.execute('SELECT * FROM '+name+' ORDER BY 1,2').fetchall()
                for (name,) in catalog.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()}
    def test_four_catalog_writes_wait_on_mutation_before_sql_and_preserve_rows(self):
        with self.catalog() as (catalog,_component):
            snapshot=catalog.require_complete();self.assertEqual(catalog.request_scan()['status'],'started')
            proof,_rows=catalog.pending_hashed((frozenset(),frozenset()))
            self.assertIsNotNone(proof)
            actions=[('request_scan',catalog.request_scan),('step',lambda:catalog.step(max_files=1)),
                ('pending_fill',lambda:catalog.set_pending_fill_progress(proof,'a'*64,0)),
                ('vector_fill',lambda:catalog.set_vector_fill(snapshot,'a'*64,'',False))]
            with self.fixture.held('catalog-mutation.lock','EX'):
                for name,action in actions:
                    before=self.rows(catalog);sql=[];catalog.db.set_trace_callback(sql.append)
                    try:result=action()
                    finally:catalog.db.set_trace_callback(None)
                    unchanged=before==self.rows(catalog);begin=[s for s in sql if s.startswith('BEGIN')]
                    existing.emit('catalog-four-mutation-admission',api=name,result=result,begin=begin,unchanged=unchanged)
                    with self.subTest(api=name):
                        self.assertEqual(begin,[]);self.assertTrue(unchanged)
                        if isinstance(result,dict):self.assertEqual(result['status'],'busy')
                        else:self.assertFalse(result)

    def test_request_scan_cannot_change_visible_active_while_cut_reader_is_present(self):
        with self.catalog() as (catalog,_component):
            before=(catalog._pointer('current'),catalog._pointer('active'))
            with self.fixture.held('publish-cut.lock','SH'):
                result=catalog.request_scan()
                after=(catalog._pointer('current'),catalog._pointer('active'))
                try:servable=catalog.require_servable()
                except Exception as exc:servable=str(exc)
            existing.emit('catalog-request-visible-cut',result=result,before=before,after=after)
            self.assertEqual(result['status'],'busy');self.assertEqual(before,after)
            self.assertEqual(servable,catalog.require_servable())
            self.assertEqual(catalog.request_scan()['status'],'started')

    def test_publish_aggregation_and_stale_cleanup_stay_outside_cut(self):
        with self.catalog() as (catalog,_component):
            original=catalog.require_complete();events=[];changes=catalog._changes;drop=catalog._drop_scan
            def counted(*args):events.append(('aggregate',self.probe('publish-cut.lock',True)));return changes(*args)
            def dropped(*args):events.append(('cleanup',self.probe('publish-cut.lock',True)));return drop(*args)
            with mock.patch.object(catalog,'_changes',counted),mock.patch.object(catalog,'_drop_scan',dropped):
                self.assertEqual(catalog.request_scan()['status'],'started')
                result=catalog.step(max_files=100,max_stats=100)
                self.assertEqual(result['status'],'complete')
                self.assertNotEqual(catalog.require_complete().scan_id,original.scan_id)
                self.assertEqual(catalog.request_scan()['status'],'started')
            existing.emit('catalog-expensive-work-outside-cut',events=events)
            self.assertTrue(any(name=='aggregate' for name,_ok in events))
            self.assertTrue(any(name=='cleanup' for name,_ok in events))
            self.assertTrue(all(ok for _name,ok in events))

    def test_blocked_transition_waits_for_cut_and_keeps_previous_complete_until_then(self):
        with self.catalog() as (catalog,_component):
            original=catalog.require_complete()
            path=self.case.auto/'wiki'/'invalid-utf8.md';path.write_bytes(b'\xff\xfe')
            self.assertEqual(catalog.request_scan()['status'],'started')
            with self.fixture.held('publish-cut.lock','SH'):
                result=catalog.step(max_files=100,max_stats=100)
                phase=catalog._scan_row(catalog._pointer('active'))['phase']
                try:complete=catalog.require_complete()
                except Exception as exc:complete=str(exc)
            existing.emit('catalog-blocked-cut',result=result,phase=phase,previousComplete=complete==original)
            self.assertEqual(result['status'],'busy');self.assertIn(phase,self.C.PENDING_PHASES)
            self.assertEqual(complete,original)
            self.assertEqual(catalog.step(max_files=100,max_stats=100)['status'],'blocked')
            with self.assertRaises(Exception):catalog.require_complete()

if __name__=='__main__':
    names=[name for name in CatalogChecks.__dict__ if name.startswith('test_')]
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(CatalogChecks(name) for name in names))
    raise SystemExit(not result.wasSuccessful())

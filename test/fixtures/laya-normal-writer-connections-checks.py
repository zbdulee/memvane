#!/usr/bin/env python3
"""Normal base/queue managed opens; capacity/sidecar/cohort remain separate."""
from contextlib import ExitStack, closing, contextmanager
import gc
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import threading
import time
import unittest
from unittest import mock
import weakref

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('normal_connection_seed', ROOT/'test/fixtures/laya-writer-scope-checks.py')
existing = importlib.util.module_from_spec(spec); spec.loader.exec_module(existing)

def emit(label, **values): print(json.dumps(dict(case=label, **values), sort_keys=True), flush=True)

class NormalWriterConnectionChecks(unittest.TestCase):
    def test_catalog_and_sidecar_visible_admission_accepts_brief_wait(self):
        import laya_incremental_docs as incremental
        import laya_initial_build as build
        from laya_source_catalog import LayaSourceCatalog
        roots = {self.case.WIKI: str(self.case.auto/'wiki')}
        with self.F.warm(), self.seed.authority(), ExitStack() as stack:
            writer, _ = self.admission(stack)
            self.N.mark_new_write(writer)
            catalog_component = self.component(writer, 'catalog')
            sidecar_component = self.component(writer, 'sidecar')
            catalog = LayaSourceCatalog(self.case.catalog, roots, writer.scope_id,
                                        coordination=catalog_component)
            stack.callback(catalog.close)
            sidecar = incremental._sidecar_open(build.read(self.case.state)['identity'], sidecar_component)
            stack.callback(incremental._close, sidecar)
            original = self.W.publish_window
            observed = []
            monotonic = time.monotonic
            elapsed = [0.0]
            def clock():
                return monotonic() + elapsed[0]
            @contextmanager
            def delayed(component, *, exclusive, deadline):
                if exclusive and component.role in ('catalog', 'sidecar'):
                    observed.append(component.role)
                    elapsed[0] += .08  # Simulate admission time without scheduler oversleep.
                with original(component, exclusive=exclusive, deadline=deadline):
                    yield
            with mock.patch.object(self.W, 'publish_window', side_effect=delayed), \
                    mock.patch.object(time, 'monotonic', side_effect=clock):
                with catalog._managed_write(visible=True):
                    pass
                with incremental._document_transaction(sidecar, sidecar_component, visible=True):
                    pass
            self.assertEqual(observed, ['catalog', 'sidecar'])

    def test_publish_admission_deadline_is_retryable_and_does_not_publish(self):
        flow=importlib.import_module('laya_index_flow')
        with self.F.warm(), self.seed.authority(), ExitStack() as owners:
            writer,_context=self.admission(owners)
            with self.runtime(writer) as runtime:
                self.N.mark_new_write(writer)
                index=runtime.index
                old=index.db.execute('SELECT gen FROM head WHERE scope=?',(writer.scope_id,)).fetchone()[0]
                self.assertEqual(index.stage('deadline-new',writer.scope_id,
                    [('wiki/deadline.md','synthetic deadline',True)]),'prepared')
                original=self.W.publish_window
                @contextmanager
                def slow(*args,**kwargs):
                    with original(*args,**kwargs):
                        time.sleep(.25)
                        yield
                with mock.patch.object(self.W,'publish_window',side_effect=slow):
                    with self.assertRaisesRegex(self.W.ReaderWindowBusy,'reader_write_deadline'):
                        index.publish(old,'deadline-new','deadline-claim')
                self.assertEqual(index.db.execute('SELECT gen FROM head WHERE scope=?',(writer.scope_id,)).fetchone()[0],old)
                self.assertFalse(index.db.in_transaction)
                db=index.db;db_type=type(db);execute=db_type.execute
                original_rollback=db_type.rollback;visible=[False]
                @contextmanager
                def visible_window(*args,**kwargs):
                    with original(*args,**kwargs):
                        visible[0]=True
                        try:yield
                        finally:visible[0]=False
                def execute_after_begin(connection,sql,*args,**kwargs):
                    result=execute(connection,sql,*args,**kwargs)
                    if connection is db and visible[0] and sql=='BEGIN IMMEDIATE':time.sleep(.25)
                    return result
                def failed_rollback(connection):
                    if connection is db:raise sqlite3.OperationalError('synthetic rollback failure')
                    return original_rollback(connection)
                try:
                    with mock.patch.object(self.W,'publish_window',side_effect=visible_window), \
                         mock.patch.object(db_type,'execute',new=execute_after_begin), \
                         mock.patch.object(db_type,'rollback',new=failed_rollback):
                        with self.assertRaisesRegex(self.W.ReaderWindowBusy,'reader_write_deadline') as raised:
                            index.publish(old,'deadline-new','deadline-claim')
                    self.assertTrue(getattr(raised.exception,'rollback_pending',False))
                    self.assertTrue(db.in_transaction)
                    self.assertTrue(index._connection_owner.pending)
                finally:
                    self.W.drain_pending(deadline=time.monotonic()+2)
        class Queue:
            released=False
            def lease(self,claim,recover=False):return {'lease_token':'held'}
            def release_for_retry(self,job,token):self.released=token=='held';return self.released
        queue=Queue()
        def busy(_job,_token):raise self.W.ReaderWindowBusy('reader_write_deadline')
        self.assertEqual(flow._process_leased(queue,'claim',False,busy),'deferred')
        self.assertTrue(queue.released)
        def physical_busy(_job,_token):raise self.W.ReaderWindowBusy('reader_physical_busy')
        queue.released=False
        self.assertEqual(flow._process_leased(queue,'claim',False,physical_busy),'deferred')
        self.assertTrue(queue.released)
        def physical_pending(_job,_token):
            error=self.W.ReaderWindowBusy('reader_physical_busy')
            error.rollback_pending=True
            raise error
        queue.released=False
        with self.assertRaisesRegex(self.W.ReaderWindowBusy,'reader_physical_busy'):
            flow._process_leased(queue,'claim',False,physical_pending)
        self.assertTrue(queue.released)
        def changed(_job,_token):raise self.W.ReaderWindowBusy('reader_publish_changed')
        queue.released=False
        with self.assertRaisesRegex(self.W.ReaderWindowBusy,'reader_publish_changed'):
            flow._process_leased(queue,'claim',False,changed)
        self.assertTrue(queue.released)
        def pending(_job,_token):
            error=self.W.ReaderWindowBusy('reader_write_deadline')
            error.rollback_pending=True
            raise error
        with self.assertRaisesRegex(self.W.ReaderWindowBusy,'reader_write_deadline'):
            flow._process_leased(queue,'claim',False,pending)

    def setUp(self):
        self.fixture = existing.WriterScopeChecks('runTest')
        self.addCleanup(self.fixture.doCleanups); self.fixture.setUp()
        self.F, self.seed, self.case = self.fixture.F, self.fixture.seed, self.fixture.case
        self.P, self.W, self.J = self.fixture.P, self.fixture.W, self.fixture.J
        self.sync, self.queue, self.plan = self.fixture.sync, self.fixture.queue, self.fixture.plan
        self.N = importlib.import_module('laya_writer_scope')

    def component(self, writer, role):
        try: return self.W.component_for(writer, role)
        except (ValueError, RuntimeError) as exc:
            self.fail('normal component feature absent after actual managed fullproof: '+str(exc))

    def admission(self, stack):
        owner = stack.enter_context(self.sync.sync_owner(self.queue))
        context = self.N.normal_writer_scope(self.plan, queue_owner=owner, deadline=time.monotonic()+20)
        writer = stack.enter_context(context)
        return writer, context

    def runtime(self, writer):
        from laya_index_runtime import LayaIndexRuntime
        windows = {role:self.component(writer,role) for role in ('base','queue')}
        return LayaIndexRuntime(source_root={self.case.WIKI:str(self.case.auto/'wiki')},
            scope=self.case.route['scopeId'], db_path=str(self.case.db), queue_path=str(self.queue),
            db_incarnation=self.case.route['dbIncarnation'], wiki_only=True, connection_windows=windows)

    def test_unmarked_normal_writer_cannot_publish_exclusion(self):
        with self.F.warm(), self.seed.authority(), ExitStack() as owners:
            writer,_context=self.admission(owners)
            with self.runtime(writer) as runtime:
                sql=[];runtime.index.db.set_trace_callback(sql.append)
                try:
                    with self.assertRaisesRegex(ValueError,'normal_writer_intent_required'):
                        runtime.index.exclude(writer.scope_id,path='unmarked-exclusion.md')
                finally:runtime.index.db.set_trace_callback(None)
                self.assertFalse(any(statement.strip().upper().startswith('BEGIN') for statement in sql))
                self.assertEqual(runtime.index.db.execute('SELECT count(*) FROM exclusions WHERE scope=? AND value=?',
                    (writer.scope_id,'unmarked-exclusion.md')).fetchone(),(0,))

    def test_direct_normal_head_publication_seals_prior_baseline(self):
        with self.F.warm(), self.seed.authority(), ExitStack() as owners:
            writer,_context=self.admission(owners)
            with self.runtime(writer) as runtime:
                self.N.mark_new_write(writer)
                index=runtime.index
                old=index.db.execute('SELECT gen FROM head WHERE scope=?',(writer.scope_id,)).fetchone()[0]
                self.assertEqual(index.stage('direct-baseline-new',writer.scope_id,
                    [('wiki/direct-baseline.md','synthetic direct publication',True)]),'prepared')
                self.assertEqual(index.publish(old,'direct-baseline-new','direct-baseline-claim'),'published')
                rows=index.db.execute("SELECT key,value FROM schema_meta WHERE key LIKE 'reader-revision:base:%' ORDER BY key").fetchall()
                self.assertEqual(len(rows),2)
                self.assertEqual(json.loads(rows[0][1])['head'],old)
                self.assertEqual(json.loads(rows[1][1])['after'],'direct-baseline-new')
                self.assertEqual(index.publish(old,'direct-baseline-new','direct-baseline-claim'),'replayed')
                self.assertEqual(index.db.execute("SELECT key,value FROM schema_meta WHERE key LIKE 'reader-revision:base:%' ORDER BY key").fetchall(),rows)

    def test_direct_exclusion_refuses_corrupt_head_fts_before_baseline(self):
        from laya_index_a import _gen_fts
        with self.F.warm(), self.seed.authority(), ExitStack() as owners:
            writer,_context=self.admission(owners)
            with self.runtime(writer) as runtime:
                self.N.mark_new_write(writer)
                index=runtime.index;db=index.db
                head,ordinal=db.execute('SELECT h.gen,i.ord FROM head h JOIN gen_index i ON i.gen=h.gen '
                                        'WHERE h.scope=?',(writer.scope_id,)).fetchone()
                table=_gen_fts(ordinal)
                self.assertTrue(index._generation_fts_matches(head))
                with index._write_transaction(visible=False):
                    count=db.execute(f'SELECT count(*) FROM {table}_data WHERE id>10').fetchone()[0]
                    self.assertGreater(count,0)
                    db.execute(f'DELETE FROM {table}_data WHERE id>10')
                self.assertTrue(index._generation_fts_matches(head),'FTS content rows must remain intact')
                with self.assertRaisesRegex(ValueError,'invalid_base_baseline'):
                    index.exclude(writer.scope_id,path='not-published.md')
                self.assertIsNone(db.execute("SELECT value FROM schema_meta WHERE key='reader-revision:base:00000000000000000000'").fetchone())
                self.assertEqual(db.execute('SELECT count(*) FROM exclusions WHERE scope=? AND value=?',
                                            (writer.scope_id,'not-published.md')).fetchone(),(0,))

    def test_actual_base_queue_runtime_writes_and_pins_outlive_lexical_scope(self):
        with self.F.warm(), self.seed.authority(), ExitStack() as owners:
            writer, context = self.admission(owners); calls = []; runtime = None
            try:
                with self.fixture.fixture.trace(calls): runtime = self.runtime(writer)
                self.assertEqual(len(calls), 4)
                self.assertTrue(all(c['managed'] for c in calls))
                head=runtime.index.db.execute('SELECT gen FROM head WHERE scope=?',(writer.scope_id,)).fetchone()[0]
                self.N.mark_new_write(writer)
                self.assertEqual(runtime.index.exclude(writer.scope_id,path='normal-writer-exclude.md'),'excluded')
                baseline=runtime.index.db.execute("SELECT value FROM schema_meta WHERE key='reader-revision:base:00000000000000000000'").fetchone()
                self.assertIsNotNone(baseline,'direct exclusion must not precede its base baseline')
                self.assertEqual(json.loads(baseline[0])['head'],head)
                owners.close(); gc.collect()
                with self.assertRaises(ValueError): self.N.validate_normal_writer(writer)
                with self.sync.sync_owner(self.queue) as busy: self.assertIsNone(busy)
                runtime.close(); runtime = None
                with self.sync.sync_owner(self.queue) as successor: self.assertIsNotNone(successor)
                emit('normal-runtime-base-queue', contentConnects=calls, actualWrite=True,
                     pinOutlivesLexical=True, successorAfterNativeClose=True)
            finally:
                if runtime is not None: runtime.close()

    def test_actual_capacity_runtime_uses_managed_catalog_and_binding(self):
        from laya_index_runtime import LayaIndexRuntime
        from laya_source_catalog import LayaSourceCatalog
        roots = {self.case.WIKI: str(self.case.auto/'wiki')}
        with self.F.warm(), self.seed.authority(), \
                LayaSourceCatalog(self.case.catalog, roots, self.case.route['scopeId']), \
                ExitStack() as owners:
            writer, _context = self.admission(owners)
            calls = []
            with self.fixture.fixture.trace(calls):
                windows = {role: self.component(writer, role)
                           for role in ('base', 'queue', 'catalog', 'binding')}
                with LayaIndexRuntime(source_root=roots, scope=writer.scope_id,
                                      db_path=str(self.case.db), queue_path=str(self.queue),
                                      db_incarnation=writer.incarnation, wiki_only=True,
                                      catalog_path=str(self.case.catalog),
                                      binding_store_path=str(self.case.store),
                                      connection_windows=windows):
                    pass
            self.assertEqual(len(calls), 8)
            self.assertTrue(all(call['managed'] for call in calls), calls)

    def test_catalog_scope_tamper_is_rejected_before_rw_even_with_noop_callback(self):
        from laya_source_catalog import LayaSourceCatalog
        roots = {self.case.WIKI: str(self.case.auto/'wiki')}
        with self.F.warm(), self.seed.authority(), \
                LayaSourceCatalog(self.case.catalog, roots, self.case.route['scopeId']) as catalog, \
                ExitStack() as owners:
            writer, _context = self.admission(owners)
            component = self.component(writer, 'catalog')
            catalog.db.execute("UPDATE meta SET value=? WHERE key='scope'", ('foreign',))
            calls = []
            with self.fixture.fixture.trace(calls), self.assertRaises((ValueError, RuntimeError)):
                self.W.open_owned(component, path=self.case.catalog, validate=lambda _db: None,
                                  initialize=lambda _db: None, deadline=time.monotonic()+2)
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0]['managed'] and calls[0]['uri'].endswith('?mode=ro'))

    def test_delete_catalog_with_stale_wal_names_refuses_before_rw(self):
        with self.F.warm(), self.seed.authority(), ExitStack() as owners:
            with sqlite3.connect(self.case.catalog) as db:
                self.assertEqual(db.execute('PRAGMA journal_mode=DELETE').fetchone(), ('delete',))
            aux = [Path(str(self.case.catalog) + suffix) for suffix in ('-wal', '-shm')]
            for path in aux:
                path.write_bytes(b'')
                path.chmod(0o600)
            header = self.case.catalog.read_bytes()[:100]
            try:
                writer, _context = self.admission(owners)
                component = self.component(writer, 'catalog')
                calls = []
                with self.fixture.fixture.trace(calls), self.assertRaises((ValueError, RuntimeError)):
                    self.W.open_owned(component, path=self.case.catalog, validate=lambda _db: None,
                                      initialize=lambda _db: None, deadline=time.monotonic()+2)
                self.assertEqual(len(calls), 1)
                self.assertTrue(calls[0]['managed'] and calls[0]['uri'].endswith('?mode=ro'))
                self.assertEqual(self.case.catalog.read_bytes()[:100], header)
            finally:
                for path in aux:
                    path.unlink(missing_ok=True)

    def test_binding_store_id_change_is_rejected_before_rw_on_reopen(self):
        with self.F.warm(), self.seed.authority(), ExitStack() as owners:
            writer, _context = self.admission(owners)
            component = self.component(writer, 'binding')
            first = self.W.open_owned(component, path=self.case.store, validate=lambda _db: None,
                                      initialize=lambda _db: None, deadline=time.monotonic()+2)
            self.W.close_owned(first, deadline=time.monotonic()+2)
            with sqlite3.connect(self.case.store) as db:
                ddl = db.execute("SELECT sql FROM sqlite_master WHERE name='meta_update_seal'").fetchone()[0]
                db.execute('DROP TRIGGER meta_update_seal')
                db.execute("UPDATE meta SET value=? WHERE key='store_id'", ('e'*32,))
                db.execute(ddl)
                db.commit()
            calls = []
            with self.fixture.fixture.trace(calls), self.assertRaises((ValueError, RuntimeError)):
                self.W.open_owned(component, path=self.case.store, validate=lambda _db: None,
                                  initialize=lambda _db: None, deadline=time.monotonic()+2)
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0]['managed'] and calls[0]['uri'].endswith('?mode=ro'))

    def test_binding_identity_change_between_ro_close_and_rw_is_refused(self):
        with self.F.warm(), self.seed.authority(), ExitStack() as owners:
            writer, _context = self.admission(owners)
            component = self.component(writer, 'binding')
            native = self.W._OwnedSQLite._native_close_locked
            changed = []
            def close(db):
                result = native(db)
                if not changed:
                    with sqlite3.connect(self.case.store) as update:
                        ddl = update.execute("SELECT sql FROM sqlite_master WHERE name='meta_update_seal'").fetchone()[0]
                        update.execute('DROP TRIGGER meta_update_seal')
                        update.execute("UPDATE meta SET value=? WHERE key='store_id'", ('d'*32,))
                        update.execute(ddl)
                        update.commit()
                    changed.append(True)
                return result
            calls = []
            with self.fixture.fixture.trace(calls), \
                    mock.patch.object(self.W._OwnedSQLite, '_native_close_locked', new=close), \
                    self.assertRaises((ValueError, RuntimeError)):
                self.W.open_owned(component, path=self.case.store, validate=lambda _db: None,
                                  initialize=lambda _db: None, deadline=time.monotonic()+2)
            managed = [call for call in calls if call['managed']]
            self.assertEqual(changed, [True])
            self.assertEqual(len(managed), 1)
            self.assertTrue(managed[0]['uri'].endswith('?mode=ro'))

    def test_published_sidecar_reader_uses_owned_readonly_without_new_files(self):
        from laya_incremental_docs import PublishedReader
        sidecar = self.seed.sidecar
        with self.F.warm(), self.seed.authority(), \
                closing(sqlite3.connect(sidecar, isolation_level=None)) as warm, ExitStack() as owners:
            self.assertEqual(warm.execute('PRAGMA journal_mode=WAL').fetchone(), ('wal',))
            row = warm.execute('SELECT path,hash,title,profile_id FROM docs LIMIT 1').fetchone()
            self.assertIsNotNone(row)
            writer, _context = self.admission(owners)
            component = self.component(writer, 'sidecar')
            decision = self.J.select_route(str(self.case.project), action='update')
            identity = {'stateRoot': str(self.case.state), 'projectRoot': str(self.case.project),
                        'installId': decision['installId'], 'scopeId': decision['scopeId'],
                        'dbIncarnation': decision['dbIncarnation'],
                        'vectorProfileId': decision['vectorProfile']['profileId'],
                        'candidateDb': decision['dbPath']}
            files = existing.existing.existing.files
            before = files(sidecar)
            calls = []
            with self.fixture.fixture.trace(calls):
                reader = PublishedReader(identity, coordination=component)
                try:
                    self.assertTrue(reader.published(*row))
                finally:
                    reader.close()
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0]['managed'] and calls[0]['uri'].endswith('?mode=ro'))
            self.assertEqual(files(sidecar), before)
            mismatched = PublishedReader(dict(identity, scopeId='f'*32), coordination=component)
            with self.assertRaisesRegex(ValueError, 'incremental_identity_changed'):
                mismatched.published(*row)
            mismatched.close()
            self.assertEqual(self.W.local_pending_count(), 0)

    def test_published_sidecar_reader_can_open_cold_without_creating_database(self):
        from laya_incremental_docs import PublishedReader
        sidecar=self.seed.sidecar
        with self.F.warm(), self.seed.authority(), ExitStack() as owners:
            with closing(sqlite3.connect(sidecar,isolation_level=None)) as db:
                row=db.execute('SELECT path,hash,title,profile_id FROM docs LIMIT 1').fetchone()
            self.assertIsNotNone(row)
            self.assertFalse(any(Path(str(sidecar)+suffix).exists() for suffix in ('-wal','-shm')))
            before=sidecar.read_bytes()
            writer,_context=self.admission(owners)
            component=self.component(writer,'sidecar')
            decision=self.J.select_route(str(self.case.project),action='update')
            identity={'stateRoot':str(self.case.state),'projectRoot':str(self.case.project),
                'installId':decision['installId'],'scopeId':decision['scopeId'],
                'dbIncarnation':decision['dbIncarnation'],'vectorProfileId':decision['vectorProfile']['profileId'],
                'candidateDb':decision['dbPath']}
            with closing(PublishedReader(identity,coordination=component)) as reader:
                self.assertTrue(reader.published(*row))
            self.assertEqual(sidecar.read_bytes(),before)

    def test_same_inode_valid_foreign_queue_binding_is_rejected_before_rw_connect(self):
        with self.F.warm(), self.seed.authority(), ExitStack() as owners:
            writer, _context = self.admission(owners)
            component = self.component(writer,'queue'); before = self.queue.stat()
            from laya_index_claim_queue import ClaimQueue
            with sqlite3.connect(self.queue) as db:
                trigger = db.execute("SELECT sql FROM sqlite_master WHERE name='queue_meta_update_seal'").fetchone()[0]
                db.execute('DROP TRIGGER queue_meta_update_seal')
                db.execute("UPDATE queue_meta SET value=? WHERE key='index_incarnation'",('e'*32,))
                db.execute(trigger); db.commit()
                self.assertEqual(ClaimQueue._check_database(db),'e'*32)
            self.assertEqual((self.queue.stat().st_dev,self.queue.stat().st_ino),(before.st_dev,before.st_ino))
            calls = []; error = None
            with self.fixture.fixture.trace(calls):
                try: ClaimQueue.open(self.queue, coordination=component)
                except (ValueError, RuntimeError, sqlite3.Error) as exc: error = str(exc)
            emit('normal-queue-same-inode-foreign-binding', contentConnects=calls,error=error)
            self.assertIsNotNone(error)
            self.assertEqual(len(calls),1)
            self.assertTrue(calls[0]['uri'].endswith('?mode=ro'))

    def test_scope_exit_after_ro_close_prevents_rw_connect(self):
        with self.F.warm(), self.seed.authority(), ExitStack() as owners:
            writer, context = self.admission(owners)
            component = self.component(writer,'queue'); calls = []; closes = []
            from laya_index_claim_queue import ClaimQueue
            native = self.W._OwnedSQLite._native_close_locked
            def close(db):
                result = native(db); closes.append(True)
                if len(closes)==1: context.__exit__(None,None,None)
                return result
            error = None
            with self.fixture.fixture.trace(calls), mock.patch.object(self.W._OwnedSQLite,'_native_close_locked',new=close):
                try: ClaimQueue.open(self.queue,coordination=component)
                except (ValueError,RuntimeError,sqlite3.Error) as exc: error = str(exc)
            self.assertIsNotNone(error)
            self.assertEqual(len(calls),1)
            self.assertTrue(calls[0]['uri'].endswith('?mode=ro'))
            emit('normal-expired-between-ro-rw', contentConnects=calls,error=error)

    def test_cold_after_admission_allows_managed_sqlite_open(self):
        with self.seed.authority(), ExitStack() as owners:
            warm = self.F.warm(); warm.__enter__()
            try:
                writer, _context = self.admission(owners)
                component = self.component(writer,'base')
            finally: warm.__exit__(None,None,None)
            self.assertFalse(Path(str(self.case.db)+'-wal').exists())
            self.assertFalse(Path(str(self.case.db)+'-shm').exists())
            from laya_index_a import LayaIndexA
            files = existing.existing.existing.files
            before = files(self.case.db); calls = []
            with self.fixture.fixture.trace(calls), closing(LayaIndexA(self.case.db,coordination=component)) as index:
                self.assertEqual(index.incarnation(),writer.incarnation)
            self.assertEqual(len(calls),2);self.assertTrue(all(call['managed'] for call in calls))
            self.assertEqual(files(self.case.db)[''],before[''])
            self.assertEqual(files(self.case.db)['-journal'],before['-journal'])
            emit('normal-post-admission-cold-open', contentConnects=calls)

    def test_first_rw_pragma_and_native_close_failure_keep_pin_until_creator_drain(self):
        with self.F.warm(), self.seed.authority(), ExitStack() as owners:
            writer, _context = self.admission(owners)
            component = self.component(writer,'base')
            from laya_index_a import LayaIndexA
            actual = sqlite3.connect; execute = self.W._OwnedSQLite.execute
            native = self.W._OwnedSQLite._native_close_locked
            refs = []; blocked = [True]; close_events = []
            def connect(database,*args,**kwargs):
                db = actual(database,*args,**kwargs)
                if 'mode=rw' in str(database) and isinstance(db,self.W._OwnedSQLite):
                    db._normal_test_rw = True; refs.append(weakref.ref(db))
                return db
            def sql(db,statement,*args,**kwargs):
                if getattr(db,'_normal_test_rw',False) and statement=='PRAGMA wal_autocheckpoint=0':
                    raise sqlite3.OperationalError('normal-first-rw-pragma-failure')
                return execute(db,statement,*args,**kwargs)
            def close(db):
                if getattr(db,'_normal_test_rw',False):
                    if blocked[0]: raise sqlite3.OperationalError('normal-native-close-failure')
                    close_events.append(not self.F.fixture.probe('base-physical.lock','SH'))
                return native(db)
            try:
                with mock.patch.object(sqlite3,'connect',side_effect=connect), \
                        mock.patch.object(self.W._OwnedSQLite,'execute',new=sql), \
                        mock.patch.object(self.W._OwnedSQLite,'_native_close_locked',new=close):
                    with self.assertRaises((ValueError,RuntimeError,sqlite3.Error)):
                        LayaIndexA(self.case.db,coordination=component)
                    owners.close(); gc.collect()
                    self.assertEqual(len(refs),1); self.assertIsNotNone(refs[0]())
                    self.assertFalse(refs[0]()._native_closed)
                    self.assertEqual(self.W.pending_count(),1)
                    with self.sync.sync_owner(self.queue) as busy: self.assertIsNone(busy)
                    other=[]
                    thread=threading.Thread(target=lambda:other.append(self.W.drain_pending(deadline=time.monotonic()+1)))
                    thread.start();thread.join(3)
                    self.assertFalse(thread.is_alive());self.assertEqual(other,[1])
                    blocked[0]=False
                    self.assertEqual(self.W.drain_pending(deadline=time.monotonic()+2),0)
                self.assertEqual(close_events,[True])
                with self.sync.sync_owner(self.queue) as successor:self.assertIsNotNone(successor)
                emit('normal-failed-init-close-owner', weakNativeAlive=True,otherThread=other,
                     nativeCloseUnderEX=close_events, successorAfterNativeDrain=True)
            finally:
                blocked[0]=False;self.W.drain_pending(deadline=time.monotonic()+2)

    def test_normal_authority_cannot_prepare_or_repair_window_namespace(self):
        with self.F.warm(), self.seed.authority(), ExitStack() as owners:
            writer, _context = self.admission(owners)
            self.component(writer,'base')
            parent = self.F.fixture.parent
            snapshot = self.F.fixture.fixture.snapshot
            before = snapshot(parent)
            with self.assertRaises(ValueError): self.W.prepare_windows(writer)
            self.assertEqual(snapshot(parent),before)

    def test_preferred_reader_copy_does_not_redirect_normal_base_component(self):
        with self.seed.authority():
            reader = self.seed.reader
            alternate = self.seed.helper.alternate(self.case,reader,self.seed.installer,'normal-reader-copy.sqlite')
            current = reader.read_selection(self.case.journal)
            reader.cas_reader(self.case.journal,expected_epoch=current.epoch,
                expected_endpoint=current.preferred_endpoint,target=alternate,transition_id='c'*32)
            self.plan = self.P.capture_writer_plan(self.case.project,executing_root=self.case.plugin)
            with self.F.warm(), ExitStack() as owners:
                writer, _context = self.admission(owners)
                component = self.component(writer,'base')
                self.assertEqual(component.path,self.case.db)
                self.assertNotEqual(component.path,Path(alternate.record['db_path']))
                from laya_index_a import LayaIndexA
                with closing(LayaIndexA(self.case.db,coordination=component)) as index:
                    self.assertEqual(index.incarnation(),writer.incarnation)
                emit('normal-canonical-writer-versus-reader',writerBase=str(component.path),
                     readerBase=alternate.record['db_path'])

    def test_queue_content_change_after_ro_close_cannot_reach_rw_open(self):
        with self.F.warm(), self.seed.authority(), ExitStack() as owners, \
                closing(sqlite3.connect(self.queue)) as mutator:
            writer, _context = self.admission(owners)
            component = self.component(writer,'queue'); calls = []; closes = []
            from laya_index_claim_queue import ClaimQueue
            native = self.W._OwnedSQLite._native_close_locked
            def close(db):
                result = native(db); closes.append(True)
                if len(closes)==1:
                    trigger = mutator.execute("SELECT sql FROM sqlite_master WHERE name='queue_meta_update_seal'").fetchone()[0]
                    mutator.execute('DROP TRIGGER queue_meta_update_seal')
                    mutator.execute("UPDATE queue_meta SET value=? WHERE key='index_incarnation'",('f'*32,))
                    mutator.execute(trigger); mutator.commit()
                    self.assertEqual(ClaimQueue._check_database(mutator),'f'*32)
                return result
            error = None; queue = None
            try:
                with self.fixture.fixture.trace(calls), mock.patch.object(self.W._OwnedSQLite,'_native_close_locked',new=close):
                    try: queue = ClaimQueue.open(self.queue,coordination=component)
                    except (ValueError,RuntimeError,sqlite3.Error) as exc: error = str(exc)
                emit('normal-content-change-after-ro-close',contentConnects=calls,error=error,returned=queue is not None)
                self.assertIsNotNone(error,'changed queue identity must not return a normal writer')
                self.assertEqual(len(calls),1,'committed content change after RO close must prevent RW open')
            finally:
                if queue is not None: queue.close()

    def test_deadline_crossed_inside_actual_initializer_cannot_return_rw_handle(self):
        with self.F.warm(), self.seed.authority(), ExitStack() as owners:
            writer, _context = self.admission(owners)
            component = self.component(writer,'queue'); advanced = [False]
            from laya_index_claim_queue import ClaimQueue
            initialize = ClaimQueue._initialize_connection; clock = time.monotonic
            def now(): return clock()+(100 if advanced[0] else 0)
            def init(queue,db):
                result=initialize(queue,db); advanced[0]=True; return result
            error = None; queue = None
            try:
                with mock.patch.object(time,'monotonic',side_effect=now), \
                        mock.patch.object(ClaimQueue,'_initialize_connection',new=init):
                    try: queue=ClaimQueue.open(self.queue,coordination=component)
                    except (ValueError,RuntimeError,sqlite3.Error) as exc: error=str(exc)
                emit('normal-expired-inside-initializer',actualInitializerCompleted=advanced[0],
                     returned=queue is not None,error=error)
                self.assertTrue(advanced[0]);self.assertIsNotNone(error)
                self.assertIsNone(queue,'expired initializer must not return a normal writer handle')
                self.assertEqual(self.W.pending_count(),0)
            finally:
                if queue is not None:queue.close()

if __name__=='__main__': unittest.main()

"""T6 preflight: old bundle absence must not authorize a writer."""
import importlib
import importlib.util
import hashlib
import io
import json
import os
import select
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager, redirect_stdout, redirect_stderr
from pathlib import Path
import unittest
from unittest import mock

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('upgrade_reader_seed',HERE/'laya-reader-move-seal-checks.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)

def verify_rollback_writer(self,case,reader,lease,upgrade,target,target2,new,next_root,expected,fid2,review2,intent2):
    with mock.patch.object(upgrade,'inspect_full_plugin_candidate',return_value=target), \
         mock.patch.object(reader.journal,'_inspect_identity',return_value=target), \
         mock.patch.object(reader.journal,'_verified_executing_root',return_value=str(new)), \
         mock.patch.object(reader.journal,'_provider_file_identity',return_value=[[str(new/'p.py'),1,2,3,4,5]]):
        write=upgrade.inactive._atomic_expected
        observed=[]
        def restore_write(*args,**kwargs):
            result=write(*args,**kwargs)
            with lease.resolve_endpoint(case.project,action='recall',host_root=new) as endpoint:
                self.assertEqual(endpoint.endpoint_id,expected)
            observed.append('metadata')
            return result
        with mock.patch.object(upgrade.inactive,'_atomic_expected',side_effect=restore_write):
            self.assertEqual(upgrade._restore(intent2)['status'],'active_upgrade_restored_laya')
        self.assertGreaterEqual(len(observed),3)
        observed.clear()
        writer=importlib.import_module('laya_writer_open_plan')
        plan=writer.capture_writer_plan(case.project,executing_root=new)
        writer.validate_writer_plan(plan)
        with lease.resolve_endpoint(case.project,action='recall',host_root=new) as restored:
            self.assertEqual(restored.endpoint_id,expected)
        with reader._readonly(case.journal,case.state) as db:
            current,stored=reader._selection(db)
            with self.assertRaisesRegex(ValueError,'reader_successor_unproved'):
                reader._successor_chain(db,current.preferred_endpoint,stored,
                    '0'*64,target,max_epoch=reader.journal._row(db)['epoch'])
        # The restored journal row alone is insufficient without its sealed restore.
        with sqlite3.connect(case.journal) as db:
            seals=db.execute('SELECT intent_sha256,endpoint_id,record FROM upgrade_read_seals').fetchall()
            restore_seal=next(row for row in seals if json.loads(row[2]).get('schema')=='memvane-upgrade-restore-v1')
            trigger=db.execute("SELECT sql FROM sqlite_master WHERE name='upgrade_read_seals_delete'").fetchone()[0]
            db.execute('DROP TRIGGER upgrade_read_seals_delete')
            db.execute('DELETE FROM upgrade_read_seals WHERE intent_sha256=?',(restore_seal[0],))
            db.execute(trigger)
        try:
            with self.assertRaisesRegex(ValueError,'reader_successor_unproved'):
                writer.capture_writer_plan(case.project,executing_root=new)
        finally:
            with sqlite3.connect(case.journal) as db:
                db.execute('INSERT INTO upgrade_read_seals VALUES(?,?,?)',restore_seal)
        # A fresh upgrade after rollback must prove through the restored predecessor.
        (case.state/upgrade.INTENT).rename(case.state/'archived-test-restored-intent.json')
        def restored_lookup(root):
            return target if str(root)==str(new) else target2
        with mock.patch.object(upgrade,'inspect_full_plugin_candidate',side_effect=restored_lookup), \
             mock.patch.object(reader.journal,'_inspect_identity',side_effect=lambda identity:restored_lookup(identity['pluginRoot'])), \
             mock.patch.object(reader.journal,'inspect_full_plugin_candidate',side_effect=restored_lookup):
            intent3=upgrade._prepare(case.project,case.state,case.journal,case.home,next_root,
                target2['sourceCommit'],review2['proposalSha256'],'/synthetic/model',3)
            activate=upgrade.cas_activate
            def observe_activate(*args,**kwargs):
                result=activate(*args,**kwargs)
                with lease.resolve_endpoint(case.project,action='recall',host_root=new) as endpoint:
                    self.assertEqual(endpoint.endpoint_id,expected)
                observed.append('activated')
                return result
            with mock.patch.object(reader.journal,'_bind_attestation',return_value=(
                    hashlib.sha256(intent3['newAttestation'].encode()).hexdigest(),fid2)), \
                 mock.patch.object(upgrade.inactive,'_atomic_expected',side_effect=restore_write), \
                 mock.patch.object(upgrade,'cas_activate',side_effect=observe_activate):
                self.assertEqual(upgrade._switch(intent3)['status'],'active_upgrade_complete')
            self.assertGreaterEqual(observed.count('metadata'),2)
            self.assertIn('activated',observed)
            with mock.patch.object(reader.journal,'_verified_executing_root',return_value=str(next_root)), \
                 mock.patch.object(reader.journal,'_provider_file_identity',return_value=fid2):
                plan=writer.capture_writer_plan(case.project,executing_root=next_root)
                writer.validate_writer_plan(plan)
                with lease.resolve_endpoint(case.project,action='recall',host_root=next_root) as upgraded:
                    self.assertEqual(upgraded.endpoint_id,expected)


class UpgradeProofChecks(unittest.TestCase):
    def test_missing_preflight_is_collected_and_existing_receipts_are_reused(self):
        helper=fixture.base.JournalReaderChecks('runTest');helper.setUp();self.addCleanup(helper.doCleanups)
        case=helper.published_case();bridge=helper.bridge_api()
        with helper.full_authority(case,bridge), \
             mock.patch.object(bridge,'_observe_registered_hook',
                 side_effect=lambda issued,host:helper.observer(case,issued,host)) as observer:
            ready=bridge.ensure_bridge_preflight(case.state,release_commit='c'*40)
            self.assertEqual(type(ready).__name__,'BridgeReadiness')
            self.assertEqual(observer.call_count,2)
            again=bridge.ensure_bridge_preflight(case.state,release_commit='c'*40)
            self.assertEqual(type(again).__name__,'BridgeReadiness')
            self.assertEqual(observer.call_count,2,'valid receipts must not launch hosts again')
        with helper.full_authority(case,bridge), \
             mock.patch.object(bridge,'find_bridge_preflight',side_effect=ValueError('reader_hosts_pending')), \
             mock.patch.object(bridge,'_observe_registered_hook',side_effect=ValueError('reader_hosts_pending')) as observer:
            with self.assertRaisesRegex(ValueError,'reader_hosts_pending') as caught:
                bridge.ensure_bridge_preflight(case.state,release_commit='c'*40)
            self.assertEqual((caught.exception.bridge_stage,caught.exception.bridge_host),
                             ('observe_host','codex'))
            self.assertEqual([call.args[1] for call in observer.call_args_list],['codex'])
        with helper.full_authority(case,bridge), \
             mock.patch.object(bridge,'find_bridge_preflight',side_effect=ValueError('reader_hosts_pending')), \
             mock.patch.object(bridge,'_observe_registered_hook',side_effect=OSError('FAKE_CREDENTIAL')):
            with self.assertRaises(OSError) as caught:
                bridge.ensure_bridge_preflight(case.state,release_commit='c'*40)
            self.assertEqual((caught.exception.bridge_stage,caught.exception.bridge_host),
                             ('observe_host','codex'))
        with helper.full_authority(case,bridge), \
             mock.patch.object(bridge,'find_bridge_preflight',side_effect=ValueError('reader_hosts_pending')), \
             mock.patch('laya_journal_v4.legacy_hermes_present',return_value=True), \
             mock.patch.object(bridge,'_observe_registered_hook') as observer:
            with self.assertRaisesRegex(ValueError,'reader_hosts_pending') as caught:
                bridge.ensure_bridge_preflight(case.state,release_commit='c'*40)
            self.assertEqual((caught.exception.bridge_stage,caught.exception.bridge_host),
                             ('host_support',None))
            observer.assert_not_called()
        with helper.full_authority(case,bridge), \
             mock.patch.object(bridge,'find_bridge_preflight',side_effect=ValueError('reader_hosts_pending')), \
             mock.patch('laya_journal_v4.legacy_hermes_present',return_value=False), \
             mock.patch('laya_journal_v4._hermes_copies',return_value=['active-hermes']), \
             mock.patch.object(bridge,'_observe_registered_hook') as observer:
            with self.assertRaisesRegex(ValueError,'reader_hosts_pending'):
                bridge.ensure_bridge_preflight(case.state,release_commit='c'*40)
            observer.assert_not_called()

    def test_v1_restore_keeps_reader_during_route_reversal(self):
        seed=fixture.MoveSealChecks('runTest');self.addCleanup(seed.doCleanups);seed.setUp()
        case,reader,lease=seed.case,seed.reader,seed.lease
        upgrade=importlib.import_module('laya_active_upgrade')
        opens=importlib.import_module('laya_reader_open')
        search=importlib.import_module('laya_v6_reader')
        new=case.home/'new-plugin';new.mkdir()
        target={**case.proof,'pluginRoot':str(new),'sourceCommit':'a'*40,
                'bundleManifestSha256':'b'*64,'ownershipManifestSha256':'f'*64}
        old_att=json.loads((case.state/reader.journal.PRODUCTION_ATTESTATION).read_bytes())
        new_att={**old_att,'sourceCommit':target['sourceCommit'],
                 'bundleManifestSha256':target['bundleManifestSha256'],
                 'ownershipManifestSha256':target['ownershipManifestSha256'],
                 'providerManifest':{**old_att['providerManifest'],'providerPath':str(new/'p.py')}}
        def lookup(root):
            return case.proof if str(root)==str(case.plugin) else target
        def identify(att):return [[att['providerManifest']['providerPath'],1,2,3,4,5]]
        review={'proposalSha256':'9'*64,'reviewedProvider':{'vectorProfile':target['vectorProfile']}}
        with ExitStack() as stack:
            for patch in (seed.authority(),
                mock.patch.object(upgrade,'review_local_provider',return_value=review),
                mock.patch.object(upgrade,'reattest',return_value={'vectorProfile':target['vectorProfile']}),
                mock.patch.object(upgrade,'adopt_host_loaded_plugin',return_value=target),
                mock.patch.object(upgrade,'inspect_full_plugin_candidate',side_effect=lookup),
                mock.patch.object(upgrade,'attestation_record',return_value=new_att),
                mock.patch.object(upgrade,'validate_production_provider_attestation'),
                mock.patch.object(upgrade,'_provider_file_identity',side_effect=identify),
                mock.patch.object(upgrade,'_wait_for_sockets'),
                mock.patch.object(reader.journal,'inspect_full_plugin_candidate',side_effect=lookup),
                mock.patch.object(reader.journal,'_executing_from_candidate'),
                mock.patch.object(reader.journal,'_inspect_identity',side_effect=lambda identity:lookup(identity['pluginRoot'])),
                mock.patch.object(reader.journal,'_bind_attestation',side_effect=lambda path,identity,proof:(
                    hashlib.sha256(reader.journal._canon(new_att if proof==target else old_att).encode()).hexdigest(),
                    identify(new_att if proof==target else old_att)))):
                stack.enter_context(patch)
            decision=reader.journal.select_route(str(case.project),action='recall')
            intent=upgrade._prepare(case.project,case.state,case.journal,case.home,new,
                                    target['sourceCommit'],review['proposalSha256'],'/synthetic/model',3)
            pending_restore=os.environ.get('MEMVANE_TEST_RESTORE_PENDING') == '1'
            if pending_restore:
                upgrade._fence(intent)
                current=reader.journal.inspect_journal(case.journal)
                reader.journal.cas_stage_update(case.journal,current['epoch'],target,intent['newRoute'].encode())
            else:
                self.assertEqual(upgrade._switch(intent)['status'],'active_upgrade_complete')
            if os.environ.get('MEMVANE_TEST_RESTORE_NEW_WRITE') == '1':
                current=reader.journal.inspect_journal(case.journal)
                reader.journal.cas_mark_new_write(case.journal,current['epoch'])
                before=upgrade._metadata(intent)
                with self.assertRaisesRegex(Exception,'active_upgrade_rollback_forward_only'):
                    upgrade._restore(intent)
                self.assertEqual(upgrade._metadata(intent),before)
                return
            actual=upgrade.inactive._atomic_expected
            observed=[]
            def verify(stage):
                for host_root in (case.plugin,new):
                    with lease.resolve_endpoint(case.project,action='recall',host_root=host_root) as endpoint:
                        with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10) as snapshot:
                            result_rows=search.query_v6_snapshot(snapshot,endpoint=endpoint,prompt='otter',
                                decision=decision,provider=None,deadline=time.monotonic()+10).rows
                            self.assertTrue(any('otter' in row['body'] for row in result_rows),stage)
                observed.append(stage)
                if os.environ.get('MEMVANE_TEST_RESTORE_CRASH_STAGE') == stage:
                    print(json.dumps({'v2CrashCheckpoint':stage,'project':str(case.project),
                        'state':str(case.state),'plugin':str(case.plugin),'proof':case.proof,
                        'targets':[target],'host':str(new),'decision':decision,'resumeTarget':0,
                        'runtimeRoot':os.environ['MEMVANE_TEST_RUNTIME_ROOT'],
                        'endpoint':seed.selection.preferred_endpoint}),flush=True)
                    signal.pause()
                    raise AssertionError('unexpected resume')
                if os.environ.get('MEMVANE_TEST_RESTORE_INTERRUPT') == stage:
                    raise RuntimeError('synthetic_restore_interrupt')
            def write(path,before,after):
                result=actual(path,before,after)
                verify('file-'+str(upgrade._metadata_paths(intent).index(Path(path))))
                return result
            actual_cas=upgrade.cas_restore
            def cas(*args,**kwargs):
                result=actual_cas(*args,**kwargs);verify('journal');return result
            actual_recover=upgrade.cas_recover
            def recover(*args,**kwargs):
                result=actual_recover(*args,**kwargs);verify('journal');return result
            fresh=importlib.import_module('laya_fresh_wiki_origin')
            actual_change=fresh._change;actual_ledger_write=fresh._write
            in_change=[False]
            def change(*args,**kwargs):
                in_change[0]=True
                try:result=actual_change(*args,**kwargs)
                finally:in_change[0]=False
                if result['phase']=='upgrading':
                    verify('fence')
                    reviewed=upgrade._metadata_paths(intent)[1]
                    actual(reviewed,intent['newRoute'].encode(),intent['oldRoute'].encode())
                    try:
                        with self.assertRaisesRegex(ValueError,'reader_upgrade_restore_prefix_unproved'):
                            with lease.resolve_endpoint(case.project,action='recall',host_root=new):pass
                    finally:
                        actual(reviewed,intent['oldRoute'].encode(),intent['newRoute'].encode())
                return result
            def ledger_write(*args,**kwargs):
                result=actual_ledger_write(*args,**kwargs)
                if not in_change[0]:verify('ledger')
                return result
            if os.environ.get('MEMVANE_TEST_RESTORE_CRASH_STAGE'):
                marker=upgrade._intent_ledger(intent).paths(case.project,case.journal)[1]
                stack.enter_context(upgrade._install_lock(case.state))
                stack.enter_context(upgrade._operation_lock(marker))
                stack.enter_context(upgrade._locked_provider())
            with mock.patch.object(upgrade.inactive,'_atomic_expected',side_effect=write), \
                 mock.patch.object(upgrade,'cas_restore',side_effect=cas), \
                 mock.patch.object(upgrade,'cas_recover',side_effect=recover), \
                 mock.patch.object(fresh,'_change',side_effect=change), \
                 mock.patch.object(fresh,'_write',side_effect=ledger_write):
                if os.environ.get('MEMVANE_TEST_RESTORE_INTERRUPT'):
                    with self.assertRaisesRegex(RuntimeError,'synthetic_restore_interrupt'):
                        upgrade._restore(intent)
                else:
                    self.assertEqual(upgrade._restore(intent)['status'],'active_upgrade_restored_laya')
            if os.environ.get('MEMVANE_TEST_RESTORE_INTERRUPT'):
                self.assertEqual(observed[-1],os.environ['MEMVANE_TEST_RESTORE_INTERRUPT'])
                self.assertEqual(upgrade._restore(intent)['status'],'active_upgrade_restored_laya')
            else:
                self.assertEqual(observed,([] if pending_restore else ['fence'])+
                    ['file-2','file-0','file-1','journal','file-4']+
                    ([] if pending_restore else ['ledger']))
            restored=reader.journal.select_route(str(case.project),action='recall')
            with lease.resolve_endpoint(case.project,action='recall',host_root=case.plugin) as endpoint:
                with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10) as snapshot:
                    result_rows=search.query_v6_snapshot(snapshot,endpoint=endpoint,prompt='otter',
                        decision=restored,provider=None,deadline=time.monotonic()+10).rows
                    self.assertTrue(any('otter' in row['body'] for row in result_rows))

    def test_preflight_does_not_treat_unused_old_cache_as_loaded_host(self):
        helper=fixture.base.JournalReaderChecks('runTest');helper.setUp();self.addCleanup(helper.doCleanups)
        case=helper.published_case();reader=helper.reader_api();bridge=helper.bridge_api()
        obsolete=case.home/'.codex/plugins/cache/memvane-marketplace/memvane/0.0.1'
        obsolete.mkdir(parents=True)
        from laya_journal_v4 import host_copies
        anchor=case.home/'.claude/plugins/cache/memvane-marketplace/memvane/inventory'
        self.assertIn(str(obsolete),host_copies(anchor,home=case.home))
        old_proof={**case.proof,'pluginRoot':str(obsolete),'sourceCommit':'b'*40}
        def inspect(root):
            return old_proof if str(root)==str(obsolete) else case.proof
        with helper.full_authority(case,reader,bridge), \
             mock.patch.object(bridge,'inspect_full_plugin_candidate',side_effect=inspect), \
             mock.patch('laya_journal_v4._hermes_copies',return_value=[]), \
             mock.patch('laya_journal_v4.legacy_hermes_present',return_value=False):
            installer=reader.seal_current_reader(case.state,case.project,executing_root=case.plugin)
            _challenge,_receipts,ready=helper.ready(case,bridge)
            with mock.patch('laya_journal_v4.legacy_hermes_present',return_value=True), \
                 self.assertRaisesRegex(ValueError,'reader_hosts_pending'):
                reader.convert_reader_v5(case.state,case.project,installer=installer,readiness=ready)
            with mock.patch('laya_journal_v4._hermes_copies',return_value=['active-hermes']), \
                 self.assertRaisesRegex(ValueError,'reader_hosts_pending'):
                reader.convert_reader_v5(case.state,case.project,installer=installer,readiness=ready)
            changed={**case.proof,'sourceCommit':'d'*40}
            with mock.patch.object(bridge,'inspect_full_plugin_candidate',return_value=changed), \
                 self.assertRaisesRegex(ValueError,'reader_hosts_pending'):
                reader.convert_reader_v5(case.state,case.project,installer=installer,readiness=ready)
            self.assertEqual(reader.journal.journal_version(case.journal),4)
            selected=reader.convert_reader_v5(case.state,case.project,installer=installer,readiness=ready)
            self.assertEqual(selected.epoch,1)
            self.assertEqual(reader.journal.journal_version(case.journal),5)

    def test_loaded_qa_probe_proves_actual_v5_sentinel_without_writer(self):
        cards=fixture.base.published.CARDS
        with mock.patch.dict(cards,{'concepts/otter-ledger.md':
                ('Otter ledger','otter memvaneqa bridge reader sentinel')}):
            seed=fixture.MoveSealChecks('runTest');self.addCleanup(seed.doCleanups);seed.setUp()
        case,reader=seed.case,seed.reader
        observer=importlib.import_module('laya_bridge_observer')
        path=case.WIKI+'/concepts/otter-ledger.md'
        with reader._readonly(case.db,case.state) as db:
            row=db.execute('SELECT hash FROM membership WHERE path=? AND gen=?',
                           (path,case.head()[0])).fetchone()
        self.assertIsNotNone(row)
        with seed.authority(), \
             mock.patch('runtime_work.request',side_effect=AssertionError('QA reader woke worker')):
            result=observer._probe_loaded_reader(case.project,case.plugin,
                sentinel_path=path,sentinel_sha256=row[0])
            self.assertEqual(result['endpoint_id'],seed.selection.preferred_endpoint)
            self.assertEqual(result['reader_epoch'],seed.selection.epoch)
            self.assertEqual(result['store_format'],'v6')
            self.assertEqual(result['sentinel_sha256'],row[0])
            self.assertEqual(result['owner_role'],'owner')
            self.assertNotIn('otter',json.dumps(result))
            with self.assertRaisesRegex(ValueError,'reader_hosts_pending'):
                observer._probe_loaded_reader(case.project,case.plugin,
                    sentinel_path=path,sentinel_sha256='0'*64)
        bridge=seed.bridge
        hosts=frozenset(('claude','codex'))
        companion=case.home/'qa-companion';companion.mkdir()
        def inspect(root):
            return {**case.proof,'pluginRoot':str(root)}
        def observed(challenge,host):
            value=seed.helper.observer(case,challenge,host)
            root=case.plugin if host=='claude' else companion
            info=root.stat()
            value.update(hook_stage='loaded-v5-reader',endpoint_id=result['endpoint_id'],
                         reader_epoch=result['reader_epoch'],store_format='v6',
                         generation=result['generation'],sentinel_sha256=row[0],
                         owner_role='owner' if host=='claude' else 'companion',
                         plugin_root=str(root),plugin_node=[info.st_dev,info.st_ino],
                         process_pid=os.getpid()+(1 if host=='codex' else 0))
            return value
        with mock.patch.object(bridge,'inspect_full_plugin_candidate',side_effect=inspect), \
             mock.patch.object(bridge,'_observe_registered_hook',side_effect=observed):
            challenge=bridge.begin_loaded_bridge(case.state,release_commit='c'*40,expected_hosts=hosts,
                                                  sentinel_path=path,sentinel_sha256=row[0])
            refs=[bridge.collect_bridge_preflight(challenge,host=host) for host in sorted(hosts)]
            for ref in refs:
                self.assertNotIn('otter',ref.read_text())
                self.assertNotIn('otter',ref.with_suffix('.audit').read_text())
            checked=bridge.check_loaded_bridge(refs,release_commit='c'*40,expected_hosts=hosts)
            self.assertEqual(checked['endpoint_id'],result['endpoint_id'])
            self.assertEqual(checked['reader_epoch'],result['reader_epoch'])
            with self.assertRaisesRegex(ValueError,'reader_hosts_pending'):
                bridge.check_bridge_preflight(refs,release_commit='c'*40,expected_hosts=hosts)
            with self.assertRaisesRegex(ValueError,'reader_hosts_pending'):
                bridge.check_loaded_bridge([refs[0],refs[0]],release_commit='c'*40,expected_hosts=hosts)
            with self.assertRaisesRegex(ValueError,'reader_hosts_pending'):
                bridge.check_loaded_bridge(refs,release_commit='a'*40,expected_hosts=hosts)
            current_journal=reader.journal.inspect_journal(case.journal)
            changed_journal={**current_journal,'selected':{**current_journal['selected'],'sourceCommit':'f'*40}}
            with mock.patch.object(reader.journal,'inspect_journal',return_value=changed_journal):
                with self.assertRaisesRegex(ValueError,'reader_hosts_pending'):
                    bridge.check_loaded_bridge(refs,release_commit='c'*40,expected_hosts=hosts)
            held=case.home/'qa-companion-original'
            companion.rename(held);companion.mkdir()
            try:
                with self.assertRaisesRegex(ValueError,'reader_hosts_pending'):
                    bridge.check_loaded_bridge(refs,release_commit='c'*40,expected_hosts=hosts)
            finally:
                companion.rmdir();held.rename(companion)
            with mock.patch.object(reader,'read_selection',return_value=type('Stale',(),{
                    'preferred_endpoint':result['endpoint_id'],'epoch':result['reader_epoch']+1})()):
                with self.assertRaisesRegex(ValueError,'reader_hosts_pending'):
                    bridge.check_loaded_bridge(refs,release_commit='c'*40,expected_hosts=hosts)
            bad=bridge.begin_loaded_bridge(case.state,release_commit='c'*40,expected_hosts=hosts,
                                           sentinel_path=path,sentinel_sha256=row[0])
            with mock.patch.object(bridge,'_observe_registered_hook',
                    side_effect=lambda challenge,host:{**observed(challenge,host),'owner_role':'owner'}):
                bad_refs=[bridge.collect_bridge_preflight(bad,host=host) for host in sorted(hosts)]
            with self.assertRaisesRegex(ValueError,'reader_hosts_pending'):
                bridge.check_loaded_bridge(bad_refs,release_commit='c'*40,expected_hosts=hosts)
            payload={'cwd':str(case.project),'prompt':bridge.challenge_prompt(challenge)}
            decision=reader.journal.select_route(str(case.project),action='recall')
            with seed.authority(), mock.patch.object(observer,'inspect_full_plugin_candidate',return_value=case.proof), \
                 mock.patch.dict(os.environ,{'MEMVANE_BRIDGE_PREFLIGHT':challenge.challenge_id}):
                envelope=observer._qa_envelope(payload,'claude',decision,case.plugin)
                with mock.patch.object(observer,'emit_registered_hook') as emitted:
                    reply=observer.qa_hook_reply({**payload,'session_id':'synthetic-session'},
                                                  'claude',decision,case.plugin)
                self.assertEqual(reply,{'decision':'block','reason':'memvane-loaded-v5-reader:'+row[0]})
                self.assertEqual(emitted.call_count,1)
                self.assertEqual(emitted.call_args.args[2]['endpoint_id'],result['endpoint_id'])
                host=importlib.import_module('laya_host_response')
                stdout,stderr=io.StringIO(),io.StringIO()
                body=io.TextIOWrapper(io.BytesIO(json.dumps({**payload,'session_id':'synthetic-session'}).encode()))
                with mock.patch.object(host,'__file__',str(case.plugin/'core/laya_host_response.py')), \
                     mock.patch.object(host.sys,'stdin',body), \
                     mock.patch.object(observer,'emit_registered_hook') as emitted, \
                     mock.patch('runtime_work.request',side_effect=AssertionError('QA woke worker')), \
                     redirect_stdout(stdout),redirect_stderr(stderr):
                    status=host.main(['serve-qa-stdin','recall','claude',str(case.project)])
                self.assertEqual(status,0,stderr.getvalue())
                self.assertEqual(json.loads(stdout.getvalue())['reason'],'memvane-loaded-v5-reader:'+row[0])
                self.assertNotIn('otter',stdout.getvalue()+stderr.getvalue())
                self.assertEqual(emitted.call_count,1)
            self.assertEqual(envelope['hook_stage'],'loaded-v5-reader')
            self.assertEqual(envelope['endpoint_id'],result['endpoint_id'])
            self.assertNotIn('otter',json.dumps(envelope))

    def test_v5_linked_worktree_uses_main_persisted_reader(self):
        seed=fixture.MoveSealChecks('runTest');self.addCleanup(seed.doCleanups);seed.setUp()
        case,reader=seed.case,seed.reader
        host=importlib.import_module('laya_host_response')
        route=importlib.import_module('laya_index_host_route')
        dispatch=importlib.import_module('laya_reader_dispatch')
        env={**os.environ,'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':os.devnull,
             'GIT_AUTHOR_NAME':'Synthetic','GIT_AUTHOR_EMAIL':'test@example.invalid',
             'GIT_COMMITTER_NAME':'Synthetic','GIT_COMMITTER_EMAIL':'test@example.invalid'}
        def git(*args):
            result=subprocess.run(['git',*args],cwd=case.project,env=env,text=True,
                                  capture_output=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr)
        git('init','-q')
        git('add','-f','.auto-context/settings.json')
        git('commit','-q','-m','synthetic settings')
        linked=case.home/'linked';git('worktree','add','-q','-b','synthetic-linked',str(linked))
        decision=route.select_route(str(linked),action='recall')
        self.assertEqual(decision['state'],'worktree',decision)
        seen=[];actual_query=dispatch.query_endpoint
        def observe(*args,**kwargs):
            seen.append(args[0].endpoint_id)
            return actual_query(*args,**kwargs)
        with seed.authority(), \
             mock.patch.object(host,'__file__',str(case.plugin/'core/laya_host_response.py')), \
             mock.patch.object(dispatch,'query_endpoint',side_effect=observe), \
             mock.patch.object(host,'_serve_recall',side_effect=AssertionError('un-pinned worktree reader used')), \
             mock.patch('runtime_work.request',side_effect=AssertionError('worktree woke worker')):
            answer=host.serve_selected('recall','hermes',decision,{'cwd':str(linked),'prompt':'otter'})
        self.assertIn('read-only from the main checkout',answer['context'])
        self.assertIn('otter',answer['context'])
        self.assertEqual(seen,[seed.selection.preferred_endpoint])
        upgrade=importlib.import_module('laya_active_upgrade')
        new=case.home/'worktree-target';new.mkdir()
        target={**case.proof,'pluginRoot':str(new),'sourceCommit':'a'*40,
                'bundleManifestSha256':'b'*64,'ownershipManifestSha256':'f'*64}
        old=json.loads((case.state/reader.journal.PRODUCTION_ATTESTATION).read_bytes())
        new_att={**old,'sourceCommit':target['sourceCommit'],
                 'bundleManifestSha256':target['bundleManifestSha256'],
                 'ownershipManifestSha256':target['ownershipManifestSha256'],
                 'providerManifest':{**old['providerManifest'],'providerPath':str(new/'p.py')}}
        review={'proposalSha256':'9'*64,'reviewedProvider':{'vectorProfile':target['vectorProfile']}}
        with ExitStack() as stack:
            for patch in (seed.authority(),
                mock.patch.object(upgrade,'review_local_provider',return_value=review),
                mock.patch.object(upgrade,'reattest',return_value={'vectorProfile':target['vectorProfile']}),
                mock.patch.object(upgrade,'adopt_host_loaded_plugin',return_value=target),
                mock.patch.object(upgrade,'inspect_full_plugin_candidate',return_value=target),
                mock.patch.object(upgrade,'attestation_record',return_value=new_att),
                mock.patch.object(upgrade,'validate_production_provider_attestation'),
                mock.patch.object(upgrade,'_provider_file_identity',return_value=[[str(new/'p.py'),1,2,3,4,5]]),
                mock.patch.object(upgrade,'_wait_for_sockets'),
                mock.patch.object(host,'__file__',str(case.plugin/'core/laya_host_response.py')),
                mock.patch('runtime_work.request',side_effect=AssertionError('worktree woke worker')),
                mock.patch('laya_fresh_wiki_origin.request_recovery',side_effect=AssertionError('worktree requested recovery'))):
                stack.enter_context(patch)
            intent=upgrade._prepare(case.project,case.state,case.journal,case.home,new,
                                    target['sourceCommit'],review['proposalSha256'],'/synthetic/model',3)
            actual_serve=host.serve_endpoint_recall
            crossed=[]
            def fence_after_pinned_search(*args,**kwargs):
                result=actual_serve(*args,**kwargs)
                upgrade._fence(intent);crossed.append(True)
                return result
            with mock.patch.object(host,'serve_endpoint_recall',side_effect=fence_after_pinned_search):
                crossed_answer=host.serve_selected('recall','hermes',decision,
                                                   {'cwd':str(linked),'prompt':'otter'})
            self.assertEqual(crossed,[True])
            self.assertIn('read-only from the main checkout',crossed_answer['context'])
            self.assertIn('otter',crossed_answer['context'])
            fenced=route.select_route(str(linked),action='recall')
            self.assertEqual(fenced['state'],'worktree',fenced)
            self.assertEqual(fenced['mainRoute']['state'],'reader')
            answer=host.serve_selected('recall','hermes',fenced,{'cwd':str(linked),'prompt':'otter'})
            self.assertIn('read-only from the main checkout',answer['context'])
            self.assertIn('otter',answer['context'])
            changed={**fenced,'gitCommonDir':str(case.home/'other-git')}
            with mock.patch.object(host,'select_route',return_value=changed):
                with self.assertRaisesRegex(ValueError,'selected_route_changed'):
                    host.serve_selected('recall','hermes',fenced,{'cwd':str(linked),'prompt':'otter'})
            sys.path.insert(0,str(HERE.parents[1]))
            from hermes_adapter import plugin
            hooks={}
            class Context:
                def register_hook(self,name,callback):hooks[name]=callback
            plugin.register(Context())
            with mock.patch.dict(os.environ,{'QMD_SANDBOX':'','HERMES_SANDBOX':'',
                    'HERMES_HEADLESS':'','CODEX_SANDBOX':'','CLAUDE_SANDBOX':'',
                    'GEMINI_SANDBOX':''}):
                answer=hooks['pre_llm_call'](user_message='otter',working_directory=str(linked))
            self.assertIn('read-only from the main checkout',(answer or {}).get('context',''))
            self.assertIn('otter',answer['context'])
            held=case.state/'test-held-journal.sqlite'
            case.journal.rename(held)
            try:
                with mock.patch('laya_fresh_wiki_origin.request_recovery') as recovery:
                    damaged=route.select_route(str(linked),action='recall')
                self.assertEqual(damaged['state'],'blocked',damaged)
                recovery.assert_not_called()
            finally:
                held.rename(case.journal)

    def test_current_v4_bundle_still_converts_after_preflight(self):
        bundle_upgrade=importlib.import_module('laya_bundle_upgrade')
        real_upgrade_install=bundle_upgrade.upgrade_install
        helper=fixture.base.JournalReaderChecks('runTest');helper.setUp();self.addCleanup(helper.doCleanups)
        case=helper.published_case();reader=helper.reader_api();bridge=helper.bridge_api()
        with helper.full_authority(case,reader,bridge), \
             mock.patch.object(bundle_upgrade,'upgrade_install',real_upgrade_install), \
             mock.patch('laya_journal_v4.host_copies',return_value=[]), \
             mock.patch('laya_journal_v4._hermes_copies',return_value=[]), \
             mock.patch('laya_journal_v4.legacy_hermes_present',return_value=False):
            with mock.patch.object(bridge,'_observe_registered_hook',side_effect=ValueError('reader_hosts_pending')), \
                 self.assertRaisesRegex(ValueError,'reader_hosts_pending'):
                bundle_upgrade.upgrade_install(case.project,case.state,case.plugin)
            self.assertEqual(reader.journal.journal_version(case.journal),4)
            with mock.patch.object(bridge,'_observe_registered_hook',
                    side_effect=lambda issued,host:helper.observer(case,issued,host)) as observer:
                result=bundle_upgrade.upgrade_install(case.project,case.state,case.plugin)
                self.assertEqual(observer.call_count,2)
            self.assertEqual(result['status'],'current')
            self.assertEqual(reader.journal.journal_version(case.journal),5)

    def test_v4_upgrade_driver_requires_preflight_and_converts_before_fence(self):
        helper=fixture.base.JournalReaderChecks('runTest');helper.setUp();self.addCleanup(helper.doCleanups)
        case=helper.published_case();reader=helper.reader_api();bridge=helper.bridge_api()
        upgrade=importlib.import_module('laya_active_upgrade')
        bundle_upgrade=importlib.import_module('laya_bundle_upgrade')
        new=case.home/'driver-plugin';new.mkdir()
        target={**case.proof,'pluginRoot':str(new),'sourceCommit':'a'*40,
                'bundleManifestSha256':'b'*64,'ownershipManifestSha256':'f'*64}
        old_att=json.loads((case.state/reader.journal.PRODUCTION_ATTESTATION).read_bytes())
        new_att={**old_att,'sourceCommit':target['sourceCommit'],
                 'bundleManifestSha256':target['bundleManifestSha256'],
                 'ownershipManifestSha256':target['ownershipManifestSha256'],
                 'providerManifest':{**old_att['providerManifest'],'providerPath':str(new/'p.py')}}
        fid=[[str(new/'p.py'),1,2,3,4,5]]
        review={'proposalSha256':'9'*64,'reviewedProvider':{'vectorProfile':target['vectorProfile']}}
        def observer(challenge,host):
            observed=helper.observer(case,challenge,host)
            observed.update(plugin_root=str(new),release_commit=target['sourceCommit'],
                            manifest_sha256=target['bundleManifestSha256'])
            return observed
        options=dict(project_root=str(case.project),state_root=str(case.state),source_root=str(case.home/'marketplace'),
                     plugin_root=str(new),commit_id=target['sourceCommit'],accepted_provider_sha256='9'*64,
                     model_path='/synthetic/model',dimension=3)
        with ExitStack() as stack:
            for patch in (helper.full_authority(case,reader,bridge),
                mock.patch.object(upgrade,'review_local_provider',return_value=review),
                mock.patch.object(upgrade,'reattest',return_value={'vectorProfile':target['vectorProfile']}),
                mock.patch.object(upgrade,'adopt_host_loaded_plugin',return_value=target),
                mock.patch.object(upgrade,'inspect_full_plugin_candidate',return_value=target),
                mock.patch.object(upgrade,'attestation_record',return_value=new_att),
                mock.patch.object(upgrade,'validate_production_provider_attestation'),
                mock.patch.object(upgrade,'_provider_file_identity',return_value=fid),
                mock.patch.object(upgrade,'_wait_for_sockets'),
                mock.patch.object(bundle_upgrade,'select_source',return_value=(case.home/'marketplace',target['sourceCommit'])),
                mock.patch.object(bridge,'inspect_full_plugin_candidate',return_value=target),
                mock.patch.object(bridge,'_observe_registered_hook',side_effect=observer),
                mock.patch('laya_journal_v4.host_copies',return_value=[]),
                mock.patch('laya_journal_v4._hermes_copies',return_value=[]),
                mock.patch('laya_journal_v4.legacy_hermes_present',return_value=False),
                mock.patch.object(reader.journal,'_executing_from_candidate'),
                mock.patch.object(reader.journal,'inspect_full_plugin_candidate',return_value=target),
                mock.patch.object(reader.journal,'_inspect_identity',side_effect=lambda identity:target if identity['pluginRoot']==str(new) else case.proof),
                mock.patch.object(reader.journal,'_bind_attestation',return_value=(
                    hashlib.sha256(reader.journal._canon(new_att).encode()).hexdigest(),fid))):
                stack.enter_context(patch)
            with self.assertRaisesRegex(ValueError,'reader_hosts_pending'):
                upgrade.upgrade(**options,bridge_readiness=object())
            with mock.patch.object(bridge,'_observe_registered_hook',side_effect=ValueError('reader_hosts_pending')), \
                 self.assertRaisesRegex(ValueError,'reader_hosts_pending'):
                upgrade.upgrade(**options)
            self.assertFalse((case.state/upgrade.INTENT).exists())
            self.assertEqual(reader.journal.journal_version(case.journal),4)
            fixture.base.legacy.write_v3(case.journal,[])
            with mock.patch('laya_journal_v4.hosts_ready',return_value=(False,'journal_v4_waiting_hosts')):
                with self.assertRaisesRegex(ValueError,'reader_hosts_pending'):
                    upgrade.upgrade(**options)
            self.assertEqual(reader.journal.journal_version(case.journal),3)
            with mock.patch('laya_journal_v4.hosts_ready',return_value=(True,'')):
                with mock.patch.object(bridge,'_observe_registered_hook',side_effect=ValueError('reader_hosts_pending')), \
                     self.assertRaisesRegex(ValueError,'reader_hosts_pending'):
                    upgrade.upgrade(**options)
            self.assertEqual(reader.journal.journal_version(case.journal),4)
            self.assertFalse((case.state/upgrade.INTENT).exists())
            hosts=frozenset(('claude','codex'))
            challenge=bridge.begin_bridge_preflight(case.state,release_commit=target['sourceCommit'],expected_hosts=hosts)
            receipts=[bridge.collect_bridge_preflight(challenge,host=host) for host in sorted(hosts)]
            readiness=bridge.check_bridge_preflight(receipts,release_commit=target['sourceCommit'],expected_hosts=hosts)
            with self.assertRaisesRegex(ValueError,'reader_hosts_pending'):
                bridge.find_bridge_preflight(case.state,release_commit='0'*40)
            with mock.patch.object(bridge.time,'time',return_value=challenge.expires_at+1):
                with self.assertRaisesRegex(ValueError,'reader_hosts_pending'):
                    bridge.find_bridge_preflight(case.state,release_commit=target['sourceCommit'])
            held=case.home/'removed-driver-plugin';case.plugin.rename(held)
            try:
                now=[time.time()]
                real_seal=reader.seal_upgrade_reader
                def slow_seal(*args,**kwargs):
                    installer=real_seal(*args,**kwargs)
                    now[0]=challenge.expires_at+1
                    return installer
                with mock.patch.object(bridge.time,'time',side_effect=lambda:now[0]), \
                     mock.patch.object(reader,'seal_upgrade_reader',side_effect=slow_seal), \
                     mock.patch.object(bridge,'_observe_registered_hook',side_effect=observer) as observed:
                    result=upgrade.upgrade(**options)
                self.assertEqual(observed.call_count,2,'expired preflight must re-observe both hosts')
            finally:held.rename(case.plugin)
            self.assertEqual(result['status'],'active_upgrade_complete')
            self.assertEqual(reader.journal.journal_version(case.journal),5)
            self.assertEqual(reader.read_selection(case.journal).epoch,1)

    def test_v4_missing_old_bundle_bootstrap_is_conversion_only(self):
        helper=fixture.base.JournalReaderChecks('runTest');helper.setUp();self.addCleanup(helper.doCleanups)
        class Published(fixture.base.published.MoveCase):
            def publish_all(self,roots):
                if not getattr(self,'_provenance_seeded',False):
                    source=self.project/'bootstrap-source.md'
                    source.write_text('Synthetic bootstrap provenance.\n',encoding='utf8')
                    revision=dict(kind='file',path=source.name,collection=self.RAW,
                        sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                        size=source.stat().st_size,mtimeNs=source.stat().st_mtime_ns)
                    for page in (self.auto/'wiki').rglob('*.md'):
                        body=page.read_text(encoding='utf8');end=body.index('\n---\n',4)
                        page.write_text(body[:end]+'\nsourceRevisions:\n  - '+json.dumps(revision)+body[end:],encoding='utf8')
                    self._provenance_seeded=True
                return super().publish_all(roots)
        case=helper.compose(Published);reader=helper.reader_api()
        incremental=importlib.import_module('laya_incremental_docs')
        optional=case.state/incremental.FILE
        self.assertFalse(optional.exists())
        data_before=helper.data_snapshot(case)
        upgrade=importlib.import_module('laya_active_upgrade')
        bundle_upgrade=importlib.import_module('laya_bundle_upgrade')
        new=case.home/'bootstrap-plugin';new.mkdir()
        target={**case.proof,'pluginRoot':str(new),'sourceCommit':'a'*40,
                'bundleManifestSha256':'b'*64,'ownershipManifestSha256':'f'*64}
        old_att=json.loads((case.state/reader.journal.PRODUCTION_ATTESTATION).read_bytes())
        new_att={**old_att,'sourceCommit':target['sourceCommit'],
                 'bundleManifestSha256':target['bundleManifestSha256'],
                 'ownershipManifestSha256':target['ownershipManifestSha256'],
                 'providerManifest':{**old_att['providerManifest'],'providerPath':str(new/'p.py')}}
        review={'proposalSha256':'9'*64,'reviewedProvider':{'vectorProfile':target['vectorProfile']}}
        with ExitStack() as stack:
            for patch in (helper.full_authority(case,reader),
                mock.patch.object(upgrade,'review_local_provider',return_value=review),
                mock.patch.object(upgrade,'reattest',return_value={'vectorProfile':target['vectorProfile']}),
                mock.patch.object(upgrade,'adopt_host_loaded_plugin',return_value=target),
                mock.patch.object(upgrade,'inspect_full_plugin_candidate',return_value=target),
                mock.patch.object(upgrade,'attestation_record',return_value=new_att),
                mock.patch.object(upgrade,'validate_production_provider_attestation'),
                mock.patch.object(upgrade,'_provider_file_identity',return_value=[[str(new/'p.py'),1,2,3,4,5]]),
                mock.patch.object(bundle_upgrade,'select_source',return_value=(case.home/'marketplace',target['sourceCommit'])),
                mock.patch.object(reader.journal,'_executing_from_candidate')):
                stack.enter_context(patch)
            intent=upgrade._prepare(case.project,case.state,case.journal,case.home,new,
                                    target['sourceCommit'],review['proposalSha256'],'/synthetic/model',3)
            held=case.home/'removed-old-plugin';case.plugin.rename(held)
            try:
                with mock.patch.object(reader.journal,'_inspect_identity',side_effect=AssertionError('old bundle was touched')):
                    issuer=getattr(reader,'seal_upgrade_reader',None)
                    self.assertTrue(callable(issuer),'old-cache-absent conversion issuer is required')
                    seal=issuer(case.project,case.state,executing_root=new)
                    self.assertEqual(seal.endpoint.record['scope'],case.route['scopeId'])
                    with mock.patch.object(bundle_upgrade,'select_source',return_value=(case.home/'marketplace','0'*40)):
                        with self.assertRaisesRegex(ValueError,'reader_installer_unproved'):
                            issuer(case.project,case.state,executing_root=new)
                    with mock.patch.object(upgrade,'review_local_provider',return_value={**review,'proposalSha256':'0'*64}):
                        with self.assertRaisesRegex(ValueError,'reader_installer_unproved'):
                            issuer(case.project,case.state,executing_root=new)
                    with mock.patch.object(upgrade,'reattest',return_value=None):
                        with self.assertRaisesRegex(ValueError,'reader_installer_unproved'):
                            issuer(case.project,case.state,executing_root=new)
                    with self.assertRaisesRegex(ValueError,'reader_installer_read_only'):
                        reader.seal_reader_endpoint(seal,db_path=case.db)
                    bridge=helper.bridge_api()
                    with self.assertRaisesRegex(ValueError,'reader_hosts_pending'):
                        reader.convert_reader_v5(case.state,case.project,installer=seal,readiness=object())
                    def observer(challenge,host):
                        observed=helper.observer(case,challenge,host)
                        observed.update(plugin_root=str(new),release_commit=target['sourceCommit'],
                                        manifest_sha256=target['bundleManifestSha256'])
                        return observed
                    with mock.patch.object(bridge,'inspect_full_plugin_candidate',return_value=target), \
                         mock.patch.object(bridge,'_observe_registered_hook',side_effect=observer), \
                         mock.patch('laya_journal_v4.host_copies',return_value=[]), \
                         mock.patch('laya_journal_v4._hermes_copies',return_value=[]), \
                         mock.patch('laya_journal_v4.legacy_hermes_present',return_value=False):
                        hosts=frozenset(('claude','codex'))
                        challenge=bridge.begin_bridge_preflight(case.state,release_commit=target['sourceCommit'],
                                                                expected_hosts=hosts)
                        receipts=[bridge.collect_bridge_preflight(challenge,host=host) for host in sorted(hosts)]
                        readiness=bridge.check_bridge_preflight(receipts,release_commit=target['sourceCommit'],
                                                               expected_hosts=hosts)
                        def stop(name):
                            if name=='initial-intent-durable':raise RuntimeError('conversion_interrupted')
                        with mock.patch.object(reader.journal,'_conversion_step',side_effect=stop):
                            with self.assertRaisesRegex(RuntimeError,'conversion_interrupted'):
                                reader.convert_reader_v5(case.state,case.project,installer=seal,readiness=readiness)
                        self.assertTrue((case.state/reader.journal._INITIAL_INTENT/'intent.json').is_file())
                        seal=issuer(case.project,case.state,executing_root=new)
                        self.assertFalse((case.state/reader.journal._INITIAL_INTENT).exists())
                        def stop_committed(name):
                            if name=='v5-committed':raise RuntimeError('conversion_committed_interrupted')
                        with mock.patch.object(reader.journal,'_conversion_step',side_effect=stop_committed):
                            with self.assertRaisesRegex(RuntimeError,'conversion_committed_interrupted'):
                                reader.convert_reader_v5(case.state,case.project,installer=seal,readiness=readiness)
                        seal=issuer(case.project,case.state,executing_root=new)
                        self.assertFalse((case.state/reader.journal._INITIAL_INTENT).exists())
                        selection=reader.convert_reader_v5(case.state,case.project,installer=seal,readiness=readiness)
                    self.assertEqual(reader.journal.journal_version(case.journal),5)
                    self.assertEqual(selection.preferred_endpoint,seal.endpoint.endpoint_id)
                    self.assertEqual(helper.data_snapshot(case),data_before)
                    self.assertFalse(optional.exists())
                    with self.assertRaisesRegex(ValueError,'reader_endpoint_read_only'):
                        reader.cas_reader(case.journal,expected_epoch=selection.epoch,
                            expected_endpoint=selection.preferred_endpoint,target=seal.endpoint,transition_id='b'*32)
                    upgrade._seal_reader_intent(case.state,case.project,intent)
                    def inspect_new(identity):
                        if identity['pluginRoot'] != str(new):raise AssertionError('old bundle was touched')
                        return target
                    fid=[[str(new/'p.py'),1,2,3,4,5]]
                    with mock.patch.object(reader.journal,'inspect_full_plugin_candidate',return_value=target), \
                         mock.patch.object(reader.journal,'_inspect_identity',side_effect=inspect_new), \
                         mock.patch.object(reader.journal,'_bind_attestation',return_value=(
                             hashlib.sha256(intent['newAttestation'].encode()).hexdigest(),fid)), \
                         mock.patch.object(upgrade,'_wait_for_sockets'):
                        self.assertEqual(upgrade._switch(intent)['status'],'active_upgrade_complete')
                    with mock.patch.object(reader.journal,'_inspect_identity',side_effect=inspect_new), \
                         mock.patch.object(reader.journal,'_verified_executing_root',return_value=str(new)), \
                         mock.patch.object(reader.journal,'_provider_file_identity',return_value=fid):
                        plan_module=importlib.import_module('laya_writer_open_plan')
                        plan=plan_module.capture_writer_plan(case.project,executing_root=new)
                        plan_module.validate_writer_plan(plan)
                        # A missing activation receipt must not authorize a writer.
                        with sqlite3.connect(case.journal) as db:
                            receipt=db.execute('SELECT * FROM upgrade_read_successors').fetchone()
                            self.assertIsNotNone(receipt)
                            trigger=db.execute("SELECT sql FROM sqlite_master WHERE name='upgrade_read_successors_delete'").fetchone()[0]
                            db.execute('DROP TRIGGER upgrade_read_successors_delete')
                            db.execute('DELETE FROM upgrade_read_successors')
                            db.execute(trigger)
                        try:
                            with self.assertRaisesRegex(ValueError,'reader_successor_unproved'):
                                plan_module.capture_writer_plan(case.project,executing_root=new)
                        finally:
                            with sqlite3.connect(case.journal) as db:
                                db.execute('INSERT INTO upgrade_read_successors VALUES(?,?,?,?)',receipt)
                        lease=importlib.import_module('laya_reader_lease')
                        opens=importlib.import_module('laya_reader_open')
                        search=importlib.import_module('laya_v6_reader')
                        with lease.resolve_endpoint(case.project,action='recall',host_root=new) as endpoint:
                            self.assertEqual(endpoint.endpoint_id,selection.preferred_endpoint)
                            decision=reader.journal.select_route(str(case.project),action='recall')
                            with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10) as snapshot:
                                result=search.query_v6_snapshot(snapshot,endpoint=endpoint,prompt='otter',
                                    decision=decision,provider=None,deadline=time.monotonic()+10)
                                self.assertTrue(any('otter' in row['body'] for row in result.rows))
            finally:held.rename(case.plugin)

    def test_v2_retarget_preserves_reader_during_every_recovery_step(self):
        seed=fixture.MoveSealChecks('runTest');self.addCleanup(seed.doCleanups);seed.setUp()
        case,reader,lease=seed.case,seed.reader,seed.lease
        upgrade=importlib.import_module('laya_active_upgrade')
        opens=importlib.import_module('laya_reader_open')
        search=importlib.import_module('laya_v6_reader')
        roots=[case.home/'host-a',case.home/'host-b',case.home/'host-c']
        for root in roots:root.mkdir()
        targets=[{**case.proof,'pluginRoot':str(root),'sourceCommit':str(i)*40,
                  'bundleManifestSha256':str(i+2)*64,'ownershipManifestSha256':str(i+4)*64}
                 for i,root in enumerate(roots,1)]
        old_att=json.loads((case.state/reader.journal.PRODUCTION_ATTESTATION).read_bytes())
        def attestation(proof):
            return {**old_att,'sourceCommit':proof['sourceCommit'],
                    'bundleManifestSha256':proof['bundleManifestSha256'],
                    'ownershipManifestSha256':proof['ownershipManifestSha256'],
                    'providerManifest':{**old_att['providerManifest'],'providerPath':proof['pluginRoot']+'/p.py'}}
        def identify(att):return [[att['providerManifest']['providerPath'],1,2,3,4,5]]
        def lookup(root):
            return next(p for p in targets if p['pluginRoot']==str(root))
        def review(**kw):
            return {'proposalSha256':lookup(kw['plugin_root'])['sourceCommit'][0]*64,
                    'reviewedProvider':{'vectorProfile':case.proof['vectorProfile']}}
        steps=[]
        hosts={'normalize':roots[1], 'switch':roots[0]}
        interrupt=[True]
        tampered=[False]
        decision=reader.journal.select_route(str(case.project),action='recall')
        def step(name):
            host=hosts['normalize'] if name.startswith('normalize') else hosts['switch']
            with lease.resolve_endpoint(case.project,action='recall',host_root=host) as endpoint:
                self.assertEqual(endpoint.endpoint_id,seed.selection.preferred_endpoint)
                with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10) as snapshot:
                    result=search.query_v6_snapshot(snapshot,endpoint=endpoint,prompt='otter',
                        decision=decision,provider=None,deadline=time.monotonic()+10)
                    self.assertTrue(any('otter' in row['body'] for row in result.rows),name)
            if name == 'switch-descriptor' and not tampered[0]:
                path=case.state/upgrade.INTENT;original=path.read_bytes()
                changed=json.loads(original);changed['reviewSha256']='9'*64
                try:
                    path.write_bytes(upgrade._bytes(changed))
                    with self.assertRaisesRegex(ValueError,'reader_upgrade_seal_unproved'):
                        with lease.resolve_endpoint(case.project,action='recall',host_root=host):pass
                finally:path.write_bytes(original)
                tampered[0]=True
            steps.append(name)
            crash_step=os.environ.get('MEMVANE_TEST_V2_CRASH_STEP')
            if crash_step == name:
                print(json.dumps({'v2CrashCheckpoint':name,'project':str(case.project),
                    'state':str(case.state),'plugin':str(case.plugin),'proof':case.proof,
                    'targets':targets,'host':str(host),'decision':decision,
                    'resumeTarget':2 if crash_step == 'normalize-route' else 0,
                    'runtimeRoot':os.environ['MEMVANE_TEST_RUNTIME_ROOT'],
                    'endpoint':seed.selection.preferred_endpoint}),flush=True)
                signal.pause()
                raise AssertionError('unexpected resume')
            if name == 'cas' and interrupt[0] and (not crash_step or crash_step == 'normalize-route'):
                raise RuntimeError('synthetic_retarget_interrupt')
        with ExitStack() as stack:
            for patch in (seed.authority(),
                mock.patch.object(upgrade,'review_local_provider',side_effect=review),
                mock.patch.object(upgrade,'reattest',return_value={'vectorProfile':case.proof['vectorProfile']}),
                mock.patch.object(upgrade,'adopt_host_loaded_plugin',side_effect=lambda _source,root,**kw:lookup(root)),
                mock.patch.object(upgrade,'inspect_full_plugin_candidate',side_effect=lookup),
                mock.patch.object(upgrade,'attestation_record',side_effect=lambda **kw:attestation(kw['proof'])),
                mock.patch.object(upgrade,'validate_production_provider_attestation'),
                mock.patch.object(upgrade,'_provider_file_identity',side_effect=identify),
                mock.patch.object(upgrade,'_wait_for_sockets'),
                mock.patch.object(reader.journal,'inspect_full_plugin_candidate',side_effect=lookup),
                mock.patch.object(reader.journal,'_executing_from_candidate'),
                mock.patch.object(reader.journal,'_inspect_identity',side_effect=lambda identity:lookup(identity['pluginRoot'])),
                mock.patch.object(reader.journal,'_bind_attestation',side_effect=lambda path,identity,proof:(
                    hashlib.sha256(reader.journal._canon(attestation(proof)).encode()).hexdigest(),identify(attestation(proof)))),
                mock.patch.object(upgrade,'_recovery_step',side_effect=step)):
                stack.enter_context(patch)
            first=upgrade._prepare(case.project,case.state,case.journal,case.home,roots[0],
                                   targets[0]['sourceCommit'],'1'*64,'/synthetic/model',3)
            upgrade._fence(first)
            original_backup=first['backup']
            with mock.patch.object(upgrade.build,'_write_json',side_effect=RuntimeError('intent_write_interrupted')):
                with self.assertRaisesRegex(RuntimeError,'intent_write_interrupted'):
                    upgrade._prepare(case.project,case.state,case.journal,case.home,roots[1],
                                     targets[1]['sourceCommit'],'2'*64,'/synthetic/model',3,
                                     prior_intent=first)
            self.assertEqual(upgrade._load_intent(case.state,case.project),first)
            with lease.resolve_endpoint(case.project,action='recall',host_root=roots[0]) as endpoint:
                self.assertEqual(endpoint.endpoint_id,seed.selection.preferred_endpoint)
            first=upgrade._prepare(case.project,case.state,case.journal,case.home,roots[1],
                                   targets[1]['sourceCommit'],'2'*64,'/synthetic/model',3,
                                   prior_intent=first)
            self.assertEqual(first['backup'],original_backup)
            with lease.resolve_endpoint(case.project,action='recall',host_root=roots[1]) as endpoint:
                with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10) as snapshot:
                    result=search.query_v6_snapshot(snapshot,endpoint=endpoint,prompt='otter',
                        decision=decision,provider=None,deadline=time.monotonic()+10)
                    self.assertTrue(any('otter' in row['body'] for row in result.rows))
            reader.journal.cas_stage_update(case.journal,reader.journal.inspect_journal(case.journal)['epoch'],
                                            targets[1],first['newRoute'].encode())
            before,after=upgrade._metadata(first),upgrade._target_metadata(first)
            for index,path in enumerate(upgrade._metadata_paths(first)[:3]):
                upgrade.inactive._atomic_expected(path,before[index].encode(),after[index].encode())
            if os.environ.get('MEMVANE_TEST_V2_CRASH_STEP'):
                marker=upgrade._intent_ledger(first).paths(case.project,case.journal)[1]
                with upgrade._install_lock(case.state), upgrade._operation_lock(marker), upgrade._locked_provider():
                    if os.environ['MEMVANE_TEST_V2_CRASH_STEP'] == 'normalize-route':
                        with self.assertRaisesRegex(RuntimeError,'synthetic_retarget_interrupt'):
                            upgrade._recover_upgrade(first,case.home,roots[0],targets[0]['sourceCommit'],
                                                     '1'*64,'/synthetic/model',3)
                        interrupt[0]=False
                        hosts.update(normalize=roots[0],switch=roots[2])
                        pending=upgrade._load_intent(case.state,case.project)
                        upgrade._recover_upgrade(pending,case.home,roots[2],targets[2]['sourceCommit'],
                                                 '3'*64,'/synthetic/model',3)
                    else:
                        upgrade._recover_upgrade(first,case.home,roots[0],targets[0]['sourceCommit'],
                                                 '1'*64,'/synthetic/model',3)
                raise AssertionError('owner did not reach crash checkpoint')
            with self.assertRaisesRegex(RuntimeError,'synthetic_retarget_interrupt'):
                upgrade._recover_upgrade(first,case.home,roots[0],targets[0]['sourceCommit'],
                                         '1'*64,'/synthetic/model',3)
            self.assertEqual(steps,['normalize-descriptor','switch-descriptor','cas'])
            steps.clear();interrupt[0]=False
            hosts.update(normalize=roots[0],switch=roots[2])
            retargeted=upgrade._load_intent(case.state,case.project)
            result=upgrade._recover_upgrade(retargeted,case.home,roots[2],targets[2]['sourceCommit'],
                                            '3'*64,'/synthetic/model',3)
            self.assertEqual(result['status'],'active_upgrade_complete')
            self.assertEqual(steps,['normalize-descriptor','normalize-route','switch-descriptor','cas',
                                    'metadata-0','metadata-1','metadata-2','metadata-3','metadata-4','activate'])
            settled=upgrade._load_intent(case.state,case.project)
            upgrade._archive_finished_intent(case.state,case.project,settled)
            with mock.patch.object(reader.journal,'_verified_executing_root',return_value=str(roots[2])), \
                 mock.patch.object(reader.journal,'_provider_file_identity',return_value=identify(attestation(targets[2]))):
                with lease.resolve_endpoint(case.project,action='recall',host_root=roots[2]) as endpoint:
                    self.assertEqual(endpoint.endpoint_id,seed.selection.preferred_endpoint)
                    current=reader.journal.select_route(str(case.project),action='recall')
                    with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10) as snapshot:
                        result=search.query_v6_snapshot(snapshot,endpoint=endpoint,prompt='otter',
                            decision=current,provider=None,deadline=time.monotonic()+10)
                        self.assertTrue(any('otter' in row['body'] for row in result.rows))
                next_v1=upgrade._prepare(case.project,case.state,case.journal,case.home,roots[0],
                                         targets[0]['sourceCommit'],'1'*64,'/synthetic/model',3)
                upgrade._fence(next_v1)
                next_v1=upgrade._prepare(case.project,case.state,case.journal,case.home,roots[1],
                                         targets[1]['sourceCommit'],'2'*64,'/synthetic/model',3,
                                         prior_intent=next_v1)
                self.assertEqual(upgrade._switch(next_v1)['status'],'active_upgrade_complete')
            with mock.patch.object(reader.journal,'_verified_executing_root',return_value=str(roots[1])), \
                 mock.patch.object(reader.journal,'_provider_file_identity',return_value=identify(attestation(targets[1]))):
                with lease.resolve_endpoint(case.project,action='recall',host_root=roots[1]) as endpoint:
                    self.assertEqual(endpoint.endpoint_id,seed.selection.preferred_endpoint)

    def test_v5_prefence_intent_retargets_after_its_bundle_disappears(self):
        seed=fixture.MoveSealChecks('runTest');self.addCleanup(seed.doCleanups);seed.setUp()
        case,reader,lease=seed.case,seed.reader,seed.lease
        upgrade=importlib.import_module('laya_active_upgrade')
        opens=importlib.import_module('laya_reader_open')
        search=importlib.import_module('laya_v6_reader')
        from laya_product_bundle import BundleEvidenceError
        roots=[case.home/name for name in ('host-a','host-b','host-c','host-d')]
        for root in roots:root.mkdir()
        targets=[{**case.proof,'pluginRoot':str(root),'sourceCommit':str(i)*40,
                  'bundleManifestSha256':str(i+4)*64,'ownershipManifestSha256':'abcd'[i-1]*64}
                 for i,root in enumerate(roots,1)]
        removed=set()
        old_att=json.loads((case.state/reader.journal.PRODUCTION_ATTESTATION).read_bytes())
        def attestation(proof):
            return {**old_att,'sourceCommit':proof['sourceCommit'],
                    'bundleManifestSha256':proof['bundleManifestSha256'],
                    'ownershipManifestSha256':proof['ownershipManifestSha256'],
                    'providerManifest':{**old_att['providerManifest'],'providerPath':proof['pluginRoot']+'/p.py'}}
        def identify(att):return [[att['providerManifest']['providerPath'],1,2,3,4,5]]
        def lookup(root):
            if str(root) in removed:
                raise BundleEvidenceError('bundle_root_unavailable')
            return next(p for p in targets if p['pluginRoot']==str(root))
        def review(**kw):
            return {'proposalSha256':lookup(kw['plugin_root'])['sourceCommit'][0]*64,
                    'reviewedProvider':{'vectorProfile':case.proof['vectorProfile']}}
        def seals():
            with sqlite3.connect(case.journal) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='upgrade_read_seals'").fetchone() is None:
                    return set()
                return {row[0] for row in db.execute('SELECT intent_sha256 FROM upgrade_read_seals')}
        def run(index):
            return upgrade.upgrade(project_root=str(case.project),state_root=str(case.state),
                source_root=case.home,plugin_root=str(roots[index]),
                commit_id=targets[index]['sourceCommit'],
                accepted_provider_sha256=str(index+1)*64,model_path='/synthetic/model',dimension=3)
        def recall_at(index):
            with mock.patch.object(reader.journal,'_verified_executing_root',return_value=str(roots[index])), \
                 mock.patch.object(reader.journal,'_provider_file_identity',
                                   return_value=identify(attestation(targets[index]))):
                with lease.resolve_endpoint(case.project,action='recall',host_root=roots[index]) as endpoint:
                    self.assertEqual(endpoint.endpoint_id,seed.selection.preferred_endpoint)
                    current=reader.journal.select_route(str(case.project),action='recall')
                    with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10) as snapshot:
                        result=search.query_v6_snapshot(snapshot,endpoint=endpoint,prompt='otter',
                            decision=current,provider=None,deadline=time.monotonic()+10)
                        self.assertTrue(any('otter' in row['body'] for row in result.rows))
        with ExitStack() as stack:
            for patch in (seed.authority(),
                mock.patch.object(upgrade,'review_local_provider',side_effect=review),
                mock.patch.object(upgrade,'reattest',return_value={'vectorProfile':case.proof['vectorProfile']}),
                mock.patch.object(upgrade,'adopt_host_loaded_plugin',side_effect=lambda _source,root,**kw:lookup(root)),
                mock.patch.object(upgrade,'inspect_full_plugin_candidate',side_effect=lookup),
                mock.patch.object(upgrade,'attestation_record',side_effect=lambda **kw:attestation(kw['proof'])),
                mock.patch.object(upgrade,'validate_production_provider_attestation'),
                mock.patch.object(upgrade,'_provider_file_identity',side_effect=identify),
                mock.patch.object(upgrade,'_wait_for_sockets'),
                mock.patch.object(reader.journal,'inspect_full_plugin_candidate',side_effect=lookup),
                mock.patch.object(reader.journal,'_executing_from_candidate'),
                mock.patch.object(reader.journal,'_inspect_identity',side_effect=lambda identity:lookup(identity['pluginRoot'])),
                mock.patch.object(reader.journal,'_bind_attestation',side_effect=lambda path,identity,proof:(
                    hashlib.sha256(reader.journal._canon(attestation(proof)).encode()).hexdigest(),identify(attestation(proof))))):
                stack.enter_context(patch)
            ledger_path=case.state/upgrade.fresh.LEDGER
            # 1. Stopped after writing its intent, before sealing or fencing.
            with mock.patch.object(upgrade,'_seal_reader_intent',side_effect=RuntimeError('stopped')):
                with self.assertRaisesRegex(RuntimeError,'stopped'):
                    upgrade._prepare(case.project,case.state,case.journal,case.home,roots[0],
                                     targets[0]['sourceCommit'],'1'*64,'/synthetic/model',3)
            first=upgrade._load_intent(case.state,case.project)
            self.assertEqual(upgrade.fresh._load(ledger_path),first['oldLedger'])
            self.assertNotIn(hashlib.sha256(upgrade._bytes(first)).hexdigest(),seals())
            removed.add(str(roots[0]))
            self.assertEqual(run(1)['status'],'active_upgrade_complete')
            retargeted=upgrade._load_intent(case.state,case.project)
            self.assertEqual((retargeted['backup'],retargeted['counts']),(first['backup'],first['counts']))
            self.assertIn(hashlib.sha256(upgrade._bytes(retargeted)).hexdigest(),seals())
            recall_at(1)
            # 2. Stopped after sealing its reader commitment, before fencing.
            upgrade._archive_finished_intent(case.state,case.project,retargeted)
            second=upgrade._prepare(case.project,case.state,case.journal,case.home,roots[2],
                                    targets[2]['sourceCommit'],'3'*64,'/synthetic/model',3)
            self.assertEqual(upgrade.fresh._load(ledger_path),second['oldLedger'])
            self.assertIn(hashlib.sha256(upgrade._bytes(second)).hexdigest(),seals())
            # The selected route syncs meanwhile and marks its first write.
            self.assertFalse(second['journalBefore']['newWriteSeen'])
            reader.journal.cas_mark_new_write(case.journal,
                                              reader.journal.inspect_journal(case.journal)['epoch'])
            removed.add(str(roots[2]))
            self.assertEqual(run(3)['status'],'active_upgrade_complete')
            third=upgrade._load_intent(case.state,case.project)
            self.assertNotEqual(third['backup'],second['backup'])
            self.assertTrue(Path(second['backup']).is_dir())
            self.assertTrue(third['journalBefore']['newWriteSeen'])
            self.assertEqual(third['newProof']['pluginRoot'],str(roots[3]))
            recall_at(3)

    def test_removed_old_bundle_keeps_only_sealed_read_proof(self):
        seed=fixture.MoveSealChecks('runTest');self.addCleanup(seed.doCleanups);seed.setUp()
        old=seed.case.plugin;held=seed.case.home/'held-old-plugin'
        decision=seed.reader.journal.select_route(str(seed.case.project),action='recall')
        old.rename(held)
        self.addCleanup(lambda:held.rename(old) if held.exists() else None)
        journal=seed.reader.journal
        upgrade=importlib.import_module('laya_active_upgrade')
        with mock.patch.object(journal,'_inspect_identity',side_effect=AssertionError('old bundle was touched')):
            proof=journal.verify_selected_route_read_only(decision)
            self.assertTrue(proof['readOnlyAllowed'])
            self.assertFalse(proof['productRouteAllowed'])
            row,selected,_route,_att,_marker=upgrade._old_state(seed.case.project,seed.case.state,seed.case.journal)
            self.assertEqual(row['scope'],decision['scopeId'])
        with self.assertRaises((ValueError,OSError)):
            journal.verify_selected_route(decision)
        att=seed.case.state/journal.PRODUCTION_ATTESTATION
        original=att.read_bytes()
        try:
            att.write_bytes(original+b' ')
            with self.assertRaises(ValueError):
                journal.verify_selected_route_read_only(decision)
            with self.assertRaises(importlib.import_module('laya_product_install').InstallBlocked):
                upgrade._old_state(seed.case.project,seed.case.state,seed.case.journal)
        finally:att.write_bytes(original)

    def test_v5_bundle_drift_recall_uses_persisted_reader(self):
        seed=fixture.MoveSealChecks('runTest');self.addCleanup(seed.doCleanups);seed.setUp()
        case,reader=seed.case,seed.reader
        host=importlib.import_module('laya_host_response')
        dispatch=importlib.import_module('laya_reader_dispatch')
        journal=reader.journal
        decision=journal.select_route(str(case.project),action='recall')
        payload={'cwd':str(case.project),'prompt':'otter'}
        actual_query=dispatch.query_endpoint
        actual_verify=journal.verify_selected_route
        seen=[]
        def observe(*args,**kwargs):
            seen.append(args[0].endpoint_id)
            return actual_query(*args,**kwargs)
        with seed.authority(), \
             mock.patch.object(host,'__file__',str(case.plugin/'core/laya_host_response.py')), \
             mock.patch.object(journal,'_executing_root',return_value=case.plugin), \
             mock.patch.object(journal,'verify_selected_route',side_effect=journal.JournalEvidenceError('installed_bundle_changed')), \
             mock.patch.object(dispatch,'query_endpoint',side_effect=observe), \
             mock.patch.object(host,'_serve_recall',side_effect=AssertionError('un-pinned drift reader used')), \
             mock.patch('runtime_work.request',side_effect=AssertionError('reader woke worker')):
            answer=host.serve_selected('recall','hermes',decision,payload)
            actual_resolve=seed.lease.resolve_endpoint
            attempts=[]
            @contextmanager
            def changed_once(*args,**kwargs):
                attempts.append(1)
                if len(attempts)==1:
                    raise ValueError('reader_selection_changed')
                with actual_resolve(*args,**kwargs) as endpoint:
                    yield endpoint
            with mock.patch.object(seed.lease,'resolve_endpoint',side_effect=changed_once):
                raced=host.serve_selected('recall','hermes',decision,payload)
            self.assertEqual(len(attempts),2)
            self.assertIn('otter',raced['context'])
            attempts.clear()
            @contextmanager
            def always_changed(*_args,**_kwargs):
                attempts.append(1)
                raise ValueError('reader_selection_changed')
                yield
            with mock.patch.object(seed.lease,'resolve_endpoint',side_effect=always_changed):
                with self.assertRaisesRegex(ValueError,'reader_selection_changed'):
                    host.serve_selected('recall','hermes',decision,payload)
            self.assertEqual(len(attempts),2)
            proofs=[]
            def recovered(candidate):
                proofs.append(1)
                if len(proofs)<=2:
                    raise journal.JournalEvidenceError('installed_bundle_changed')
                return actual_verify(candidate)
            with mock.patch.object(journal,'verify_selected_route',side_effect=recovered):
                restored=host.serve_selected('recall','hermes',decision,payload)
            self.assertGreater(len(proofs),2)
            self.assertIn('otter',restored['context'])
            with seed.lease.resolve_endpoint(case.project,action='recall',host_root=case.plugin) as endpoint:
                self.assertFalse(reader._recheck(endpoint)[2]['productRouteAllowed'])
                with self.assertRaisesRegex(ValueError,'reader_endpoint_read_only'):
                    reader.cas_reader(case.journal,expected_epoch=seed.selection.epoch,
                        expected_endpoint=endpoint.endpoint_id,target=endpoint,transition_id='a'*32)
            att=case.state/journal.PRODUCTION_ATTESTATION
            original=att.read_bytes()
            try:
                att.write_bytes(original+b' ')
                with self.assertRaises(journal.JournalEvidenceError):
                    host.serve_selected('recall','hermes',decision,payload)
            finally:att.write_bytes(original)
            with mock.patch.object(host,'serve_endpoint_recall',
                                   side_effect=host.HostResponseError('selected_proof_changed')), \
                 mock.patch.object(host,'_recall_retry_decision',
                                   side_effect=AssertionError('reader failure retried as ordinary recall')):
                with self.assertRaisesRegex(host.HostResponseError,'selected_proof_changed'):
                    host.serve_selected('recall','hermes',decision,payload)
        self.assertIn('otter',answer['context'])
        self.assertEqual(seen,[seed.selection.preferred_endpoint]*3)

    def test_prepared_upgrade_fence_keeps_persisted_reader(self):
        seed=fixture.MoveSealChecks('runTest');self.addCleanup(seed.doCleanups);seed.setUp()
        case,reader,lease=seed.case,seed.reader,seed.lease
        actual_executing_root=reader.journal._verified_executing_root
        upgrade=importlib.import_module('laya_active_upgrade')
        new=case.home/'new-plugin';new.mkdir()
        target={**case.proof,'pluginRoot':str(new),'sourceCommit':'a'*40,
                'bundleManifestSha256':'b'*64,'ownershipManifestSha256':'f'*64,
                'includedFiles':['core/fixture.py']}
        old_att=json.loads((case.state/reader.journal.PRODUCTION_ATTESTATION).read_bytes())
        new_att={**old_att,'sourceCommit':target['sourceCommit'],
                 'bundleManifestSha256':target['bundleManifestSha256'],
                 'ownershipManifestSha256':target['ownershipManifestSha256'],
                 'providerManifest':{**old_att['providerManifest'],'providerPath':str(new/'p.py')}}
        review={'proposalSha256':'9'*64,'reviewedProvider':{'vectorProfile':target['vectorProfile']}}
        with ExitStack() as stack:
            for patch in (seed.authority(),
                          mock.patch.object(upgrade,'review_local_provider',return_value=review),
                          mock.patch.object(upgrade,'reattest',return_value={'vectorProfile':target['vectorProfile']}),
                          mock.patch.object(upgrade,'adopt_host_loaded_plugin',return_value=target),
                          mock.patch.object(upgrade,'inspect_full_plugin_candidate',return_value=target),
                          mock.patch.object(upgrade,'attestation_record',return_value=new_att),
                          mock.patch.object(upgrade,'validate_production_provider_attestation'),
                          mock.patch.object(upgrade,'_provider_file_identity',return_value=[[str(new/'p.py'),1,2,3,4,5]]),
                          mock.patch.object(upgrade,'_wait_for_sockets')):
                stack.enter_context(patch)
            with lease.resolve_endpoint(case.project,action='recall',host_root=case.plugin) as endpoint:
                expected=endpoint.endpoint_id
                decision=reader.journal.select_route(str(case.project),action='recall')
            intent=upgrade._prepare(case.project,case.state,case.journal,case.home,new,
                                    target['sourceCommit'],review['proposalSha256'],'/synthetic/model',3)
            with reader._readonly(case.journal,case.state) as db:
                tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertIn('upgrade_read_seals',tables,'upgrade intent must be committed before fencing')
                seals=db.execute('SELECT intent_sha256,endpoint_id FROM upgrade_read_seals').fetchall()
            self.assertEqual(seals,[(hashlib.sha256(upgrade._bytes(intent)).hexdigest(),expected)])
            with self.assertRaisesRegex(sqlite3.IntegrityError,'reader_record_immutable'):
                reader.mutate_image(case.journal,lambda db:db.execute(
                    "UPDATE upgrade_read_seals SET endpoint_id='tampered'") or True)
            host=importlib.import_module('laya_host_response')
            routes=importlib.import_module('laya_index_host_route')
            payload={'cwd':str(case.project),'prompt':'otter'}
            # The registered callback can classify just before the fence is
            # installed and reselect just after it. It must still read the
            # already sealed endpoint rather than return empty context.
            sys.path.insert(0,str(HERE.parents[1]))
            from hermes_adapter import core_bridge, plugin
            hooks={}
            class Context:
                def register_hook(self,name,callback):hooks[name]=callback
            plugin.register(Context())
            actual_run=subprocess.run
            fenced=[]
            def fence_after_classifier(argv,*args,**kwargs):
                result=actual_run(argv,*args,**kwargs)
                if len(argv)>1 and str(argv[1]).endswith('laya_index_host_route.py') and not fenced:
                    self.assertEqual(result.stdout.strip(),'candidate')
                    upgrade._fence(intent);fenced.append(True)
                return result
            with mock.patch.dict(os.environ,{'QMD_SANDBOX':'','HERMES_SANDBOX':'',
                    'HERMES_HEADLESS':'','CODEX_SANDBOX':'','CLAUDE_SANDBOX':'',
                    'GEMINI_SANDBOX':''}), \
                 mock.patch.object(host,'__file__',str(case.plugin/'core/laya_host_response.py')), \
                 mock.patch.object(core_bridge.subprocess,'run',side_effect=fence_after_classifier), \
                 mock.patch('runtime_work.request',side_effect=AssertionError('reader woke worker')), \
                 mock.patch('laya_fresh_wiki_origin.request_recovery',side_effect=AssertionError('reader requested recovery')):
                answer=hooks['pre_llm_call'](user_message='otter',working_directory=str(case.project))
                self.assertTrue(fenced)
                self.assertIn('otter',(answer or {}).get('context',''))
            selected=routes.select_hook_payload('recall',json.dumps(payload).encode(),str(case.project))
            self.assertEqual(selected['state'],'reader')
            companion=case.home/'new-companion';companion.mkdir()
            companion_proof={**target,'pluginRoot':str(companion)}
            with mock.patch.object(reader.journal,'__file__',str(companion/'core/laya_install_journal.py')), \
                 mock.patch.object(reader.journal,'_verified_executing_root',side_effect=actual_executing_root), \
                 mock.patch.object(reader.journal,'_executing_from_candidate'), \
                 mock.patch.object(reader.journal,'inspect_full_plugin_candidate',return_value=companion_proof):
                with lease.resolve_endpoint(case.project,action='recall',host_root=companion) as endpoint:
                    self.assertEqual(endpoint.endpoint_id,expected)
                with mock.patch.object(reader.journal,'inspect_full_plugin_candidate',
                                       return_value={**companion_proof,'includedFiles':['other.py']}):
                    with self.assertRaisesRegex(ValueError,'executing_bundle_not_selected'):
                        with lease.resolve_endpoint(case.project,action='recall',host_root=companion):
                            pass
            with mock.patch.object(host,'__file__',str(case.plugin/'core/laya_host_response.py')), \
                 mock.patch('runtime_work.request',side_effect=AssertionError('reader woke worker')), \
                 mock.patch('laya_fresh_wiki_origin.request_recovery',side_effect=AssertionError('reader requested recovery')):
                serve=getattr(host,'serve_endpoint_recall',None)
                self.assertTrue(callable(serve),'fenced reader host entry is required')
                for host_name in ('claude','codex','hermes'):
                    answer=serve(host_name,payload,cwd=str(case.project))
                    context=(answer['context'] if host_name=='hermes' else
                             answer['hookSpecificOutput']['additionalContext'])
                    self.assertIn('otter',context)
                real_resolve=lease.resolve_endpoint
                attempts=[]
                @contextmanager
                def changed_once(*args,**kwargs):
                    attempts.append(1)
                    if len(attempts)==1:
                        raise ValueError('reader_selection_changed')
                    with real_resolve(*args,**kwargs) as endpoint:
                        yield endpoint
                with mock.patch.object(lease,'resolve_endpoint',side_effect=changed_once):
                    answer=serve('hermes',payload,cwd=str(case.project))
                self.assertEqual(len(attempts),2)
                self.assertIn('otter',answer['context'])
                attempts.clear()
                @contextmanager
                def always_changed(*_args,**_kwargs):
                    attempts.append(1)
                    raise ValueError('reader_selection_changed')
                    yield
                with mock.patch.object(lease,'resolve_endpoint',side_effect=always_changed):
                    with self.assertRaisesRegex(ValueError,'reader_selection_changed'):
                        serve('hermes',payload,cwd=str(case.project))
                self.assertEqual(len(attempts),2)
                # The retry must not follow a replacement install at the same
                # project path. The initial journal identity is a read hint;
                # the issued endpoint still supplies the authority.
                actual_bundle=lease._read_v5_bundle
                snapshots=[]
                def changed_install(path):
                    item=actual_bundle(path)
                    snapshots.append(1)
                    if len(snapshots)==1:
                        return {**item,'selected':{**item['selected'],'installId':'f'*32}}
                    return item
                with mock.patch.object(lease,'_read_v5_bundle',side_effect=changed_install):
                    with self.assertRaisesRegex(ValueError,'reader_selection_unproved'):
                        serve('hermes',payload,cwd=str(case.project))
                # Exercise the registered Hermes callback and its real selector
                # subprocess; bundle authority remains the synthetic fixture.
                sys.path.insert(0,str(HERE.parents[1]))
                from hermes_adapter import plugin
                hooks={}
                class Context:
                    def register_hook(self,name,callback):hooks[name]=callback
                plugin.register(Context())
                with mock.patch.dict(os.environ,{'QMD_SANDBOX':'','HERMES_SANDBOX':'',
                        'HERMES_HEADLESS':'','CODEX_SANDBOX':'','CLAUDE_SANDBOX':'',
                        'GEMINI_SANDBOX':''}):
                    answer=hooks['pre_llm_call'](user_message='otter',working_directory=str(case.project))
                    self.assertIn('otter',(answer or {}).get('context',''))
                    with mock.patch.object(host,'serve_endpoint_recall',side_effect=ValueError('reader_unproved')):
                        answer=hooks['pre_llm_call'](user_message='otter',working_directory=str(case.project))
                        self.assertIn('request refused',(answer or {}).get('context',''))
            # Run the actual shell dispatch and core CLI in fresh processes. A
            # test-only Python shim supplies synthetic bundle authority, not a
            # fabricated selector, endpoint, journal or search result.
            root=HERE.parents[1]
            bindir=case.home/'hook-bin';bindir.mkdir()
            evidence=case.home/'hook-authority.json'
            evidence.write_text(json.dumps({'plugin':str(case.plugin),'proof':case.proof,
                                             'target':target,'project':str(case.project),
                                             'state':str(case.state)}),encoding='utf8')
            shim=bindir/'python3'
            shim.write_text('#!'+sys.executable+'\n'+'''\
import importlib.util,json,os,runpy,sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
root=Path(os.environ['MEMVANE_TEST_HOOK_ROOT'])
sys.path.insert(0,str(root/'core'))
spec=importlib.util.spec_from_file_location('hook_seed',root/'test/fixtures/laya-journal-reader-checks.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
import laya_journal_reader as reader,laya_reader_lease as lease
p=json.loads(Path(os.environ['MEMVANE_TEST_HOOK_AUTHORITY']).read_text())
case=SimpleNamespace(plugin=Path(p['plugin']),proof=p['proof'])
helper=base.JournalReaderChecks('runTest')
with helper.full_authority(case,reader,lease), mock.patch.object(base.J,'_verified_executing_root',return_value=str(root)), \\
     mock.patch('runtime_work.request',side_effect=AssertionError('reader woke worker')), \\
     mock.patch('laya_fresh_wiki_origin.request_recovery',side_effect=AssertionError('reader requested recovery')):
    sys.argv=sys.argv[1:]
    runpy.run_path(sys.argv[0],run_name='__main__')
''',encoding='utf8')
            shim.chmod(0o700)
            env={**os.environ,'PATH':str(bindir)+os.pathsep+os.environ.get('PATH',''),
                 'MEMVANE_TEST_HOOK_ROOT':str(root),'MEMVANE_TEST_HOOK_AUTHORITY':str(evidence),
                 'QMD_SANDBOX':'','CODEX_SANDBOX':'','CLAUDE_SANDBOX':'',
                 'CLAUDE_HEADLESS':'','GEMINI_SANDBOX':''}
            ledger_path=case.state/'fresh-wiki-cutover.json'
            original_time=ledger_path.stat()
            try:
                for label,offset in (('stopped',-720),('future',720)):
                    stamp=time.time_ns()+offset*1_000_000_000
                    os.utime(ledger_path,ns=(original_time.st_atime_ns,stamp))
                    result=subprocess.run([str(root/'hooks/run-hook'),'recall','claude'],
                        input=json.dumps(payload),text=True,capture_output=True,cwd=case.project,
                        env=env,timeout=30)
                    self.assertEqual(result.returncode,0,result.stderr)
                    self.assertIn('otter',json.loads(result.stdout)['hookSpecificOutput']['additionalContext'])
                    print(json.dumps({'case':'fence-age-reader','age':label,'readerExit':result.returncode,
                                      'actualHostExecuted':False}),flush=True)
            finally:
                os.utime(ledger_path,ns=(original_time.st_atime_ns,original_time.st_mtime_ns))
            # A fresh owner process runs the actual switch through journal
            # staging and pauses immediately after its durable CAS. The shell
            # reader runs while the owner holds its normal locks, then again
            # after that exact child is killed. Only bundle review and socket
            # release are synthetic; no live installation is touched.
            child_script='''\
import importlib.util,json,os,signal,sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
root=Path(os.environ['MEMVANE_TEST_HOOK_ROOT'])
sys.path.insert(0,str(root/'core'))
spec=importlib.util.spec_from_file_location('crash_seed',root/'test/fixtures/laya-journal-reader-checks.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
import laya_active_upgrade as upgrade,laya_journal_reader as reader,laya_reader_lease as lease
import laya_embed_resident as resident
p=json.loads(Path(os.environ['MEMVANE_TEST_HOOK_AUTHORITY']).read_text())
case=SimpleNamespace(plugin=Path(p['plugin']),proof=p['proof'])
helper=base.JournalReaderChecks('runTest')
project,state=Path(p['project']),Path(p['state'])
intent=upgrade._load_intent(state,project)
marker=upgrade._intent_ledger(intent).paths(project,state/'install-journal.sqlite')[1]
actual=upgrade.cas_stage_update
atomic=upgrade.inactive._atomic_expected
replace_ledger=upgrade._replace_ledger
activate=upgrade.cas_activate
mode=os.environ['MEMVANE_TEST_CRASH_PHASE']
paths=upgrade._metadata_paths(intent)
real_wait=resident.wait_for_release
real_sleep=resident.time.sleep
def checkpoint(phase,epoch=None):
    print(json.dumps({'checkpoint':phase,'epoch':epoch}),flush=True)
    signal.pause()
    raise AssertionError('unexpected resume')
def stage(*args,**kwargs):
    result=actual(*args,**kwargs)
    if mode=='pending':checkpoint('pending',result['epoch'])
    return result
def write_metadata(*args,**kwargs):
    result=atomic(*args,**kwargs)
    for name,index in (('attestation',0),('reviewed',1),('project-route',2),('marker',4)):
        if mode==name and Path(args[0])==paths[index]:checkpoint(name)
    return result
def write_ledger(*args,**kwargs):
    result=replace_ledger(*args,**kwargs)
    if mode=='ledger':checkpoint('ledger')
    return result
def activate_journal(*args,**kwargs):
    result=activate(*args,**kwargs)
    if mode=='activated':checkpoint('activated',result['epoch'])
    return result
def wait_for_release(*args,**kwargs):
    reported=[]
    def observed_sleep(seconds):
        if not reported:
            reported.append(True)
            checkpoint('socket-wait')
        return real_sleep(seconds)
    with mock.patch.object(resident.time,'sleep',side_effect=observed_sleep):
        return real_wait(*args,**kwargs)
with helper.full_authority(case,reader,lease), \\
     mock.patch.object(upgrade,'inspect_full_plugin_candidate',return_value=p['target']), \\
     mock.patch.object(base.J,'inspect_full_plugin_candidate',return_value=p['target']), \\
     mock.patch.object(base.J,'_inspect_identity',return_value=p['target']), \\
     mock.patch.object(base.J,'_bind_attestation',return_value=(
         __import__('hashlib').sha256(intent['newAttestation'].encode()).hexdigest(),
         [[str(Path(p['target']['pluginRoot'])/'p.py'),1,2,3,4,5]])), \\
     mock.patch.object(upgrade,'_provider_file_identity',return_value=
         [[str(Path(p['target']['pluginRoot'])/'p.py'),1,2,3,4,5]]), \\
     mock.patch.object(upgrade,'validate_production_provider_attestation'), \\
     (nullcontext() if mode=='socket-wait' else mock.patch.object(upgrade,'_wait_for_sockets')), \\
     mock.patch.object(resident,'wait_for_release',side_effect=wait_for_release), \\
     mock.patch.object(upgrade,'cas_stage_update',side_effect=stage), \\
     mock.patch.object(upgrade.inactive,'_atomic_expected',side_effect=write_metadata), \\
     mock.patch.object(upgrade,'_replace_ledger',side_effect=write_ledger), \\
     mock.patch.object(upgrade,'cas_activate',side_effect=activate_journal), \\
     upgrade._install_lock(state),upgrade._operation_lock(marker),upgrade._locked_provider():
    upgrade._switch(intent)
'''
            def crash_phase(phase):
                child=subprocess.Popen([sys.executable,'-B','-c',child_script],cwd=case.project,
                    env={**env,'MEMVANE_TEST_CRASH_PHASE':phase},
                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                try:
                    readable,_,_=select.select([child.stdout],[],[],30)
                    self.assertTrue(readable,'upgrade child did not reach '+phase+' checkpoint')
                    line=child.stdout.readline()
                    if not line:
                        self.fail('upgrade child exited: '+child.stderr.read())
                    self.assertEqual(json.loads(line)['checkpoint'],phase,line)
                    self.assertIsNone(child.poll(),'upgrade owner must remain alive during reader')
                    concurrent=subprocess.run([str(root/'hooks/run-hook'),'recall','claude'],
                        input=json.dumps(payload),text=True,capture_output=True,cwd=case.project,
                        env=env,timeout=30)
                    self.assertEqual(concurrent.returncode,0,concurrent.stderr)
                    self.assertIn('otter',json.loads(concurrent.stdout)['hookSpecificOutput']['additionalContext'])
                    self.assertIsNone(child.poll(),'reader must finish while upgrade owner is held')
                finally:
                    if child.poll() is None:child.kill()
                    child.wait(timeout=10)
                self.assertEqual(child.returncode,-9)
                after_crash=subprocess.run([str(root/'hooks/run-hook'),'recall','codex'],
                    input=json.dumps(payload),text=True,capture_output=True,cwd=case.project,
                    env=env,timeout=30)
                self.assertEqual(after_crash.returncode,0,after_crash.stderr)
                self.assertIn('otter',json.loads(after_crash.stdout)['hookSpecificOutput']['additionalContext'])
                print(json.dumps({'case':'upgrade-crash-concurrent-reader','phase':phase,
                    'ownerExit':child.returncode,'readerExit':concurrent.returncode,
                    'postCrashReaderExit':after_crash.returncode,
                    'actualHostExecuted':False}),flush=True)
            socket_path=Path(json.loads(intent['oldAttestation'])['providerManifest']['serviceSocketPath'])
            self.assertTrue(case.state in socket_path.parents)
            socket_path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            self.assertFalse(socket_path.exists())
            listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            try:
                listener.bind(str(socket_path));socket_path.chmod(0o600);listener.listen(1)
                crash_phase('socket-wait')
            finally:
                listener.close()
                if socket_path.exists():socket_path.unlink()
            for phase in ('pending','attestation','reviewed','project-route','ledger','marker'):
                crash_phase(phase)
            for engine in ('claude','codex'):
                result=subprocess.run([str(root/'hooks/run-hook'),'recall',engine],
                    input=json.dumps(payload),text=True,capture_output=True,cwd=case.project,
                    env=env,timeout=30)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertIn('otter',json.loads(result.stdout)['hookSpecificOutput']['additionalContext'])
            intent_path=case.state/upgrade.INTENT
            original=intent_path.read_bytes()
            try:
                intent_path.write_bytes(original+b' ')
                result=subprocess.run([str(root/'hooks/run-hook'),'recall','claude'],
                    input=json.dumps(payload),text=True,capture_output=True,cwd=case.project,
                    env=env,timeout=30)
                self.assertEqual(result.returncode,3,result.stderr)
                self.assertIn('request refused',result.stderr)
                self.assertNotIn('otter',result.stdout)
            finally:intent_path.write_bytes(original)
            with lease.resolve_endpoint(case.project,action='recall',host_root=case.plugin) as endpoint:
                self.assertEqual(endpoint.endpoint_id,expected)
                self.assertFalse(reader._recheck(endpoint)[2]['productRouteAllowed'])
                opens=importlib.import_module('laya_reader_open')
                search=importlib.import_module('laya_v6_reader')
                with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10) as snapshot:
                    result=search.query_v6_snapshot(snapshot,endpoint=endpoint,prompt='otter',
                        decision=decision,provider=None,deadline=time.monotonic()+10)
                    self.assertTrue(any('otter' in row['body'] for row in result.rows))
                with self.assertRaisesRegex(ValueError,'reader_endpoint_read_only'):
                    reader.cas_reader(case.journal,expected_epoch=seed.selection.epoch,
                        expected_endpoint=expected,target=endpoint,transition_id='a'*32)
                intent_path=case.state/upgrade.INTENT
                original=intent_path.read_bytes()
                try:
                    intent_path.write_bytes(original+b' ')
                    with self.assertRaises(ValueError):
                        reader._recheck(endpoint)
                finally:intent_path.write_bytes(original)
            unrelated=case.home/'unrelated';unrelated.mkdir()
            with self.assertRaisesRegex(ValueError,'reader_upgrade_host_unproved'):
                with lease.resolve_endpoint(case.project,action='recall',host_root=unrelated):pass
            held=case.home/'held-old-plugin';case.plugin.rename(held)
            try:
                with mock.patch.object(reader.journal,'_inspect_identity',side_effect=AssertionError('old bundle was touched')), \
                     mock.patch.object(reader.journal,'_executing_from_candidate'):
                    with lease.resolve_endpoint(case.project,action='recall',host_root=new) as endpoint:
                        self.assertEqual(endpoint.endpoint_id,expected)
                        with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10) as snapshot:
                            result=search.query_v6_snapshot(snapshot,endpoint=endpoint,prompt='otter',
                                decision=decision,provider=None,deadline=time.monotonic()+10)
                            self.assertTrue(any('otter' in row['body'] for row in result.rows))
            finally:held.rename(case.plugin)
            with mock.patch.object(reader.journal,'inspect_full_plugin_candidate',return_value=target), \
                 mock.patch.object(reader.journal,'_executing_from_candidate'):
                before=reader.journal.inspect_journal(case.journal)
                pending=(before if before['state']=='pending' else
                         reader.journal.cas_stage_update(case.journal,before['epoch'],target,
                                                         intent['newRoute'].encode()))
                self.assertEqual(pending['state'],'pending')
                with lease.resolve_endpoint(case.project,action='recall',host_root=new) as endpoint:
                    with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10) as snapshot:
                        result=search.query_v6_snapshot(snapshot,endpoint=endpoint,prompt='otter',
                            decision=decision,provider=None,deadline=time.monotonic()+10)
                        self.assertTrue(any('otter' in row['body'] for row in result.rows))
                old_fenced={**intent['oldLedger'],'epoch':intent['oldLedger']['epoch']+1,
                            'phase':'upgrading'}
                before=[intent['oldAttestation'],intent['oldRoute'],intent['oldRoute'],
                        upgrade._bytes(old_fenced).decode(),intent['oldMarker']]
                after=upgrade._target_metadata(intent)
                for index,path in enumerate(upgrade._metadata_paths(intent)):
                    if index == 3:
                        upgrade._replace_ledger(path,json.loads(before[index]),json.loads(after[index]))
                    else:
                        upgrade.inactive._atomic_expected(path,before[index].encode(),after[index].encode())
                    with lease.resolve_endpoint(case.project,action='recall',host_root=new) as endpoint:
                        with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10) as snapshot:
                            result=search.query_v6_snapshot(snapshot,endpoint=endpoint,prompt='otter',
                                decision=decision,provider=None,deadline=time.monotonic()+10)
                            self.assertTrue(any('otter' in row['body'] for row in result.rows),index)
                reviewed=upgrade._metadata_paths(intent)[1]
                upgrade.inactive._atomic_expected(reviewed,after[1].encode(),before[1].encode())
                try:
                    with self.assertRaises(importlib.import_module('laya_product_install').InstallBlocked):
                        with lease.resolve_endpoint(case.project,action='recall',host_root=new):pass
                finally:
                    upgrade.inactive._atomic_expected(reviewed,before[1].encode(),after[1].encode())
                pending=reader.journal.inspect_journal(case.journal)
                with mock.patch.object(reader.journal,'_inspect_identity',return_value=target), \
                     mock.patch.object(reader.journal,'_bind_attestation',return_value=(
                         hashlib.sha256(intent['newAttestation'].encode()).hexdigest(),
                         [[str(new/'p.py'),1,2,3,4,5]])):
                    with mock.patch.object(upgrade,'_commit_reader_successor',side_effect=RuntimeError('synthetic_crash')):
                        with self.assertRaisesRegex(RuntimeError,'synthetic_crash'):
                            reader.journal.cas_activate(case.journal,pending['epoch'],intent['newRoute'].encode())
                    self.assertEqual(reader.journal.inspect_journal(case.journal),pending)
                    with reader._readonly(case.journal,case.state) as db:
                        self.assertEqual(db.execute('SELECT count(*) FROM upgrade_read_successors').fetchone(),(0,))
                    crash_phase('activated')
                    active=reader.journal.inspect_journal(case.journal)
                self.assertEqual(active['state'],'active')
                with self.assertRaisesRegex(sqlite3.IntegrityError,'reader_record_immutable'):
                    reader.mutate_image(case.journal,lambda db:db.execute(
                        'DELETE FROM upgrade_read_successors') or True)
                with lease.resolve_endpoint(case.project,action='recall',host_root=new) as endpoint:
                    with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10) as snapshot:
                        result=search.query_v6_snapshot(snapshot,endpoint=endpoint,prompt='otter',
                            decision=decision,provider=None,deadline=time.monotonic()+10)
                        self.assertTrue(any('otter' in row['body'] for row in result.rows))
                import laya_fresh_wiki_origin as fresh
                fresh._change(case.state/fresh.LEDGER,intent['newLedger']['epoch'],'upgrading','active')
                with mock.patch.object(reader.journal,'_inspect_identity',return_value=target), \
                     mock.patch.object(reader.journal,'_verified_executing_root',return_value=str(new)), \
                     mock.patch.object(reader.journal,'_provider_file_identity',
                                       return_value=[[str(new/'p.py'),1,2,3,4,5]]):
                    with mock.patch.object(host,'__file__',str(new/'core/laya_host_response.py')):
                        answer=host.serve_endpoint_recall('hermes',payload,cwd=str(case.project))
                        self.assertIn('otter',answer['context'])
                        # The previously classified reader may enter after the
                        # real ledger changes to active, on both host adapters.
                        output=io.StringIO()
                        with mock.patch.object(host,'select_hook_payload',return_value=selected), \
                             mock.patch.object(sys,'stdin',io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()),encoding='utf8')), \
                             redirect_stdout(output):
                            self.assertEqual(host.main(['serve-stdin','recall','claude',str(case.project)]),0)
                        self.assertIn('otter',json.loads(output.getvalue())['hookSpecificOutput']['additionalContext'])
                        with mock.patch.dict(os.environ,{'QMD_SANDBOX':'','HERMES_SANDBOX':'','HERMES_HEADLESS':''}), \
                             mock.patch.object(core_bridge,'_laya_route_state',return_value='reader'):
                            result=core_bridge.recall_context(user_message='otter',cwd=str(case.project))
                        self.assertIn('otter',(result or {}).get('context',''))
                    with lease.resolve_endpoint(case.project,action='recall',host_root=new) as endpoint:
                        self.assertEqual(endpoint.endpoint_id,expected)
                    (case.state/upgrade.INTENT).rename(case.state/'archived-test-intent.json')
                    with lease.resolve_endpoint(case.project,action='recall',host_root=new) as endpoint:
                        with opens.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10) as snapshot:
                            current=reader.journal.select_route(str(case.project),action='recall')
                            result=search.query_v6_snapshot(snapshot,endpoint=endpoint,prompt='otter',
                                decision=current,provider=None,deadline=time.monotonic()+10)
                            self.assertTrue(any('otter' in row['body'] for row in result.rows))
                    next_root=case.home/'next-plugin';next_root.mkdir()
                    target2={**target,'pluginRoot':str(next_root),'sourceCommit':'1'*40,
                             'bundleManifestSha256':'2'*64,'ownershipManifestSha256':'3'*64}
                    att2={**new_att,'sourceCommit':target2['sourceCommit'],
                          'bundleManifestSha256':target2['bundleManifestSha256'],
                          'ownershipManifestSha256':target2['ownershipManifestSha256'],
                          'providerManifest':{**new_att['providerManifest'],'providerPath':str(next_root/'p.py')}}
                    review2={'proposalSha256':'8'*64,'reviewedProvider':{'vectorProfile':target2['vectorProfile']}}
                    fid2=[[str(next_root/'p.py'),1,2,3,4,5]]
                    with mock.patch.object(upgrade,'review_local_provider',return_value=review2), \
                         mock.patch.object(upgrade,'adopt_host_loaded_plugin',return_value=target2), \
                         mock.patch.object(upgrade,'inspect_full_plugin_candidate',return_value=target2), \
                         mock.patch.object(upgrade,'attestation_record',return_value=att2), \
                         mock.patch.object(upgrade,'_provider_file_identity',return_value=fid2), \
                         mock.patch.object(reader.journal,'inspect_full_plugin_candidate',return_value=target2):
                        intent2=upgrade._prepare(case.project,case.state,case.journal,case.home,next_root,
                            target2['sourceCommit'],review2['proposalSha256'],'/synthetic/model',3)
                        upgrade._fence(intent2)
                        pending2=reader.journal.cas_stage_update(case.journal,
                            reader.journal.inspect_journal(case.journal)['epoch'],target2,intent2['newRoute'].encode())
                        before2,after2=upgrade._metadata(intent2),upgrade._target_metadata(intent2)
                        for index,path in enumerate(upgrade._metadata_paths(intent2)):
                            if index == 3:upgrade._replace_ledger(path,json.loads(before2[index]),json.loads(after2[index]))
                            else:upgrade.inactive._atomic_expected(path,before2[index].encode(),after2[index].encode())
                        with mock.patch.object(reader.journal,'_inspect_identity',return_value=target2), \
                             mock.patch.object(reader.journal,'_bind_attestation',return_value=(
                                 hashlib.sha256(intent2['newAttestation'].encode()).hexdigest(),fid2)):
                            reader.journal.cas_activate(case.journal,pending2['epoch'],intent2['newRoute'].encode())
                        fresh._change(case.state/fresh.LEDGER,intent2['newLedger']['epoch'],'upgrading','active')
                        if os.environ.get('MEMVANE_TEST_ROLLBACK_WRITER') == '1':
                            verify_rollback_writer(self,case,reader,lease,upgrade,target,target2,new,next_root,expected,fid2,review2,intent2)
                            return
                        (case.state/upgrade.INTENT).rename(case.state/'archived-test-intent-2.json')
                        with mock.patch.object(reader.journal,'_inspect_identity',return_value=target2), \
                             mock.patch.object(reader.journal,'_verified_executing_root',return_value=str(next_root)), \
                             mock.patch.object(reader.journal,'_provider_file_identity',return_value=fid2):
                            with lease.resolve_endpoint(case.project,action='recall',host_root=next_root) as endpoint:
                                self.assertEqual(endpoint.endpoint_id,expected)
                    with reader._readonly(case.journal,case.state) as db:
                        epoch,_digest,packed=db.execute('SELECT epoch,intent_sha256,record FROM upgrade_read_successors ORDER BY epoch DESC').fetchone()
                    forged=json.loads(packed);forged['epoch']=epoch+1;forged['intentSha256']='f'*64
                    def append_forgery(db):
                        db.execute('INSERT INTO upgrade_read_successors VALUES(?,?,?,?)',
                                   (epoch+1,'f'*64,expected,reader.journal._canon(forged)))
                        return True
                    reader.mutate_image(case.journal,append_forgery)
                    with mock.patch.object(reader.journal,'_inspect_identity',return_value=target2), \
                         mock.patch.object(reader.journal,'_verified_executing_root',return_value=str(next_root)), \
                         mock.patch.object(reader.journal,'_provider_file_identity',return_value=fid2):
                        with self.assertRaisesRegex(ValueError,'reader_successor_invalid'):
                            with lease.resolve_endpoint(case.project,action='recall',host_root=next_root):pass

if __name__=='__main__':
    if len(sys.argv) > 1:
        unittest.main(verbosity=2)
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([
        UpgradeProofChecks('test_missing_preflight_is_collected_and_existing_receipts_are_reused'),
        UpgradeProofChecks('test_preflight_does_not_treat_unused_old_cache_as_loaded_host'),
        UpgradeProofChecks('test_loaded_qa_probe_proves_actual_v5_sentinel_without_writer'),
        UpgradeProofChecks('test_v5_linked_worktree_uses_main_persisted_reader'),
        UpgradeProofChecks('test_removed_old_bundle_keeps_only_sealed_read_proof'),
        UpgradeProofChecks('test_v5_bundle_drift_recall_uses_persisted_reader'),
        UpgradeProofChecks('test_prepared_upgrade_fence_keeps_persisted_reader'),
        UpgradeProofChecks('test_v2_retarget_preserves_reader_during_every_recovery_step'),
        UpgradeProofChecks('test_v5_prefence_intent_retargets_after_its_bundle_disappears'),
        UpgradeProofChecks('test_v4_missing_old_bundle_bootstrap_is_conversion_only'),
        UpgradeProofChecks('test_v4_upgrade_driver_requires_preflight_and_converts_before_fence'),
        UpgradeProofChecks('test_current_v4_bundle_still_converts_after_preflight')]))
    raise SystemExit(not result.wasSuccessful())

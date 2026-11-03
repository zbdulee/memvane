#!/usr/bin/env python3
"""Actual project move prefixes, before resume, over published retained v5.

Only existing bundle/provider/executing-copy and provider-lane seams are
synthetic. Physical rename, fsync, fence, journal and fresh FTS are actual.
"""
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import stat
import sqlite3
import subprocess
import sys
import unittest

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
spec=importlib.util.spec_from_file_location('move_prefix_journal_fixture',HERE/'laya-reader-move-journal-checks.py')
journal_fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(journal_fixture)
seed_module=journal_fixture.seed_module

NEGATIVE_READER=r'''
from pathlib import Path
from types import SimpleNamespace
import importlib.util,json,os,signal,sys
root,payload=Path(sys.argv[1]),Path(sys.argv[2]);p=json.loads(payload.read_text())
sys.path.insert(0,str(root/'core'))
os.environ['HOME']=p['home'];os.environ['MEMVANE_TEST_RUNTIME_ROOT']=p['runtime']
signal.alarm(25)
spec=importlib.util.spec_from_file_location('negative_prefix_base',root/'test/fixtures/laya-journal-reader-checks.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
import laya_journal_reader as R,laya_reader_lease as L
assert len(R._issued)==0
def forbidden(*a,**k):raise AssertionError('negative reader must not issue an installer')
R.seal_current_reader=forbidden
helper=base.JournalReaderChecks('runTest');case=SimpleNamespace(plugin=Path(p['plugin']),proof=p['proof'])
try:
    with helper.full_authority(case,R,L):
        with L.resolve_endpoint(Path(p['project']),action='recall',host_root=case.plugin) as endpoint:
            print(json.dumps(dict(event='capability-issued',typed=type(endpoint) is R.VerifiedEndpoint)),flush=True)
except (ValueError,OSError,RuntimeError) as exc:
    print(json.dumps(dict(event='refused',reason=str(exc))),flush=True)
    sys.exit(3)
'''

WRITER=r'''
from contextlib import ExitStack,nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import importlib.util,json,os,signal,sys
root,payload=Path(sys.argv[1]),Path(sys.argv[2]);p=json.loads(payload.read_text())
sys.path.insert(0,str(root/'core'))
os.environ['HOME']=p['home'];os.environ['MEMVANE_TEST_RUNTIME_ROOT']=p['runtime']
signal.alarm(30)
spec=importlib.util.spec_from_file_location('physical_prefix_base',root/'test/fixtures/laya-journal-reader-checks.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
import laya_reader_lease as L,laya_journal_reader as R,laya_project_dir_move as mover,laya_state_move as state_move
state,project=Path(p['state']),Path(p['project'])
case=SimpleNamespace(plugin=Path(p['plugin']),proof=p['proof'])
helper=base.JournalReaderChecks('runTest');phase='owner'
def checkpoint(name):
    folder=mover.legacy_folder(project) if name=='fenced-before-rename' else mover.current_folder(project)
    assert R._node(folder)==p['folderNode'] and R._node(folder/'wiki')==p['wikiNode']
    print(json.dumps(dict(event='physical-prefix-checkpoint',phase=name,pid=os.getpid(),
        actualOwner=True,actualLifetimeEX=True,actualPrefenceFullProof=True,
        folder=str(folder),folderNode=R._node(folder),wikiNode=R._node(folder/'wiki'),
        actualHostExecuted=False)),flush=True)
    os.kill(os.getpid(),signal.SIGKILL)
try:
    with helper.full_authority(case,R,L,mover),mock.patch.object(state_move,'_provider_lane',lambda:nullcontext()):
        intent=mover._load_intent(state,project);assert intent is not None
        with ExitStack() as locks:
            mover._locks(locks,project,state,mover._queue_path(project,intent))
            with L.move_lifetime(state,project,intent=intent):
                phase='prefence-full-proof'
                record,raw,proof=R._evidence(state,project,case.plugin)
                assert proof['productRouteAllowed'] is True
                assert R.read_selection(state/'install-journal.sqlite').preferred_endpoint==p['retained']
                phase='physical-switch'
                original_anchor=mover._anchor_wiki
                def anchor_entry(chosen):
                    assert Path(chosen)==project
                    if p['phase']=='anchor-entry':checkpoint('anchor-entry')
                    return original_anchor(chosen)
                mover._anchor_wiki=anchor_entry
                original_swap=mover._swap_store
                def swap_entry(chosen):
                    if p['phase']=='catalog-rebound-before-store-swap':
                        checkpoint('catalog-rebound-before-store-swap')
                    return original_swap(chosen)
                mover._swap_store=swap_entry
                def observed_step(name):
                    if name=='fenced' and p['phase']=='fenced-before-rename':checkpoint('fenced-before-rename')
                    if name=='stores' and p['phase']=='stores-swapped-before-publish':
                        checkpoint('stores-swapped-before-publish')
                    if name=='published' and p['phase']=='published-before-records':
                        checkpoint('published-before-records')
                mover._switch(intent,step=observed_step)
                raise RuntimeError('actual physical checkpoint was not reached')
except Exception as exc:
    print(json.dumps(dict(event='error',phase=phase,errorType=type(exc).__name__,reason=str(exc))),flush=True)
    sys.exit(2)
'''


class MovePrefixChecks(unittest.TestCase):
    @staticmethod
    def require(value,reason):
        if not value:raise RuntimeError('physical-prefix prerequisite failed: '+reason)

    def check_prefix(self,phase,*,mutation=None):
        seed=seed_module.MoveSealChecks('runTest')
        self.addCleanup(seed.doCleanups)
        try:
            seed.setUp()
            with seed.prepared() as intent:
                stage,seal_type=seed.producer()
                seal=stage(intent,installer=seed.installer)
                self.require(type(seal) is seal_type,'actual typed stage seal')
                selected=seed.lease.publish_move_reader(seed.case.journal,seal=seal)
                seed.reader._recheck(seal.retained_endpoint)
        except Exception as exc:
            raise RuntimeError('published-v5/owner/stage prerequisite failed before physical reader gate') from exc
        case=seed.case;mover=seed.mover
        old=mover.legacy_folder(case.project);new=mover.current_folder(case.project)
        folder_node=seed.reader._node(old);wiki_node=seed.reader._node(old/'wiki')
        original_project={str(path.relative_to(old)):path.read_bytes() for path in old.rglob('*')
                          if path.is_file() and not path.is_symlink()}
        import laya_initial_build as build
        stable_paths=[case.state/'reviewed-route.json',case.state/seed_module.base.J.PRODUCTION_ATTESTATION,
                      build.record_path(case.state),case.state/mover.INTENT]
        if intent['discovery'] is not None:stable_paths.append(Path(intent['discovery'][0]))
        original_metadata={str(path):path.read_bytes() for path in stable_paths}
        self.require(not os.path.lexists(new),'rename destination must not be pre-created')
        bundle=seed_module.base.J.inspect_journal(case.journal)
        payload=case.home/'physical-prefix.json'
        payload.write_text(json.dumps(dict(home=str(case.home),runtime=os.environ['MEMVANE_TEST_RUNTIME_ROOT'],
            state=str(case.state),project=str(case.project),plugin=str(case.plugin),proof=case.proof,
            retained=selected.preferred_endpoint,generation=seal.old_generation,exclusion=seal.exclusion_epoch,
            phase=phase,folderNode=folder_node,wikiNode=wiki_node)),encoding='utf8');payload.chmod(0o600)
        child=subprocess.Popen([sys.executable,'-B','-I','-c',WRITER,str(ROOT),str(payload)],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,encoding='utf8')
        try:
            if not select.select([child.stdout],[],[],30)[0]:raise RuntimeError('physical writer checkpoint deadline')
            line=child.stdout.readline()
            if not line:raise RuntimeError('physical writer exited before any checkpoint')
            checkpoint=json.loads(line);print(json.dumps(checkpoint),flush=True)
            if checkpoint.get('event')!='physical-prefix-checkpoint':
                raise RuntimeError('writer factory/proof failure, not retained-reader feature RED: '+json.dumps(checkpoint))
            self.require(checkpoint['phase']==phase,'exact actual checkpoint')
            self.require(child.wait(timeout=5)==-signal.SIGKILL,'actual writer SIGKILL')
        finally:
            if child.poll() is None:child.kill()
            child.communicate(timeout=5)
        published_prefix=phase=='published-before-records'
        if published_prefix:self.require(seal.exclusion_epoch==0,'published prefix seed retains exclusion epoch zero')
        def physical_state():
            return {str(path.relative_to(case.state)):(path.read_bytes(),
                tuple(getattr(path.lstat(),key) for key in ('st_dev','st_ino','st_mode','st_uid','st_nlink')))
                for path in case.state.rglob('*') if path.is_file() and not path.is_symlink()}
        # Snapshot the actual closed-writer state before prerequisite RO SQL.
        # Any new aux from those observations or the reader still fails; no
        # keepalive/rewarm is added to conceal the physical cold boundary.
        published_physical_before=physical_state() if published_prefix else None
        moved=phase!='fenced-before-rename'
        store_prefix=phase in ('catalog-rebound-before-store-swap','stores-swapped-before-publish','published-before-records')
        folder=new if moved else old
        self.require((not os.path.lexists(old) and new.is_dir()) if moved
                     else (old.is_dir() and not os.path.lexists(new)), 'exact rename prefix paths')
        self.require(seed.reader._node(folder)==folder_node,'original project-folder inode')
        self.require(seed.reader._node(folder/'wiki')==wiki_node,'original wiki inode')
        expected_project=dict(original_project)
        if store_prefix:
            anchor_relative=str(Path('wiki')/mover.project_dir.WIKI_ANCHOR)
            self.require(anchor_relative not in original_project,'anchor absent before actual _anchor_wiki')
            anchor=folder/anchor_relative
            self.require(anchor.is_file() and not anchor.is_symlink() and anchor.read_bytes()==b'',
                         'actual _anchor_wiki creates only the empty wiki anchor')
            expected_project[anchor_relative]=b''
        self.require({str(path.relative_to(folder)):path.read_bytes() for path in folder.rglob('*')
                      if path.is_file() and not path.is_symlink()}==expected_project,
                     'original settings/route/marker/wiki unchanged; only actual anchor may be added')
        import laya_fresh_wiki_origin as fresh
        self.require((case.state/fresh.LEDGER).read_bytes()==mover._canonical(intent['fencedLedger']),
                     'exact durable fenced ledger')
        self.require((folder/fresh.ROUTE).read_bytes()==intent['oldRoute'].encode('utf8'),'old route remains exact')
        self.require((folder/fresh.MARKER).read_bytes()==intent['oldMarker'].encode('utf8'),'old marker remains exact')
        self.require(seed_module.base.J.inspect_journal(case.journal)==bundle,'bundle epoch/flag unchanged')
        prefix_evidence=None
        if store_prefix:
            self.require({str(path):path.read_bytes() for path in stable_paths}==original_metadata,
                         'reviewed route/attestation/build/discovery/intent remain exact old metadata')
            with seed.ro(case.catalog) as db:
                catalog_meta=dict(db.execute("SELECT key,value FROM meta WHERE key IN ('binding','roots')"))
            self.require(catalog_meta=={key:intent['catalog'][key][1] for key in ('binding','roots')},
                         'actual catalog commit has the new roots and binding')
            temp=Path(intent['store']['temp'])
            live_store=Path(intent['store']['path'])
            expected_store=(intent['oldLedger']['bindingStoreIdentity']
                if phase=='catalog-rebound-before-store-swap' else intent['store']['identity'])
            self.require(seed.reader._node(live_store)==expected_store,'exact old/new live binding inode')
            if phase=='catalog-rebound-before-store-swap':
                self.require(temp.is_file() and seed.reader._node(temp)==intent['store']['identity'],
                             'prepared new binding still exists at its original temporary inode')
            else:self.require(not os.path.lexists(temp),'actual _swap_store renamed temporary binding into live')
            with seed.ro(case.db) as db:
                generation_state=db.execute('SELECT state FROM generations WHERE gen=?',
                    (intent['generation']['gen'],)).fetchone()
                live_head=db.execute('SELECT gen FROM head WHERE scope=?',(case.route['scopeId'],)).fetchone()
            expected_state='published' if published_prefix else 'prepared'
            expected_head=intent['generation']['gen'] if published_prefix else seal.old_generation
            self.require(generation_state==(expected_state,),'actual target generation has exact checkpoint state')
            self.require(live_head==(expected_head,),'actual head matches the exact pre/post-publication checkpoint')
            prefix_evidence=dict(catalog='new',binding='old' if temp.exists() else 'new',
                actualBindingNode=seed.reader._node(live_store),temporaryBindingExists=temp.exists(),
                reservedGeneration=intent['generation']['gen'],reservedState=generation_state[0],
                actualHead=live_head[0],metadata='exact-old-fenced',actualEmptyAnchor=True)
            print(json.dumps(dict(case='actual-store-prefix-prerequisite',phase=phase,
                                  observed=prefix_evidence,actualWriterExit=child.returncode)),flush=True)
        with seed.ro(case.journal) as db:
            preferred=db.execute('SELECT preferred_endpoint FROM reader_selection').fetchone()[0]
            row=db.execute('SELECT record FROM read_endpoints WHERE endpoint_id=?',(preferred,)).fetchone()
            self.require(preferred==selected.preferred_endpoint and row is not None,'published retained selection')
            record=json.loads(row[0])
            self.require(record['pinned_generation']==seal.old_generation,'persisted old generation pin')
            for key in ('catalog_path','binding_path','sidecar_path'):
                self.require(Path(record[key]).is_file(),'retained old component exists: '+key)
        mutation_evidence=None
        saved_container=None
        if mutation is not None:
            catalog_mutations=('canonical-old-binding-header','catalog-scope-nul')
            required_phase=('catalog-rebound-before-store-swap' if mutation in catalog_mutations else 'anchor-entry')
            self.require(phase==required_phase,'negative begins at its exact actual physical prefix')
            if mutation=='container-inode':
                saved_container=case.project/'preserved-original-container'
                folder.rename(saved_container)
                folder.mkdir(mode=stat.S_IMODE(saved_container.stat().st_mode))
                folder.chmod(stat.S_IMODE(saved_container.stat().st_mode))
                for entry in list(saved_container.iterdir()):entry.rename(folder/entry.name)
                mover._fsync(folder);mover._fsync(case.project)
                self.require(seed.reader._node(saved_container)==folder_node,'preserved original container inode')
                self.require(seed.reader._node(folder)!=folder_node,'actual replacement container inode')
                self.require(seed.reader._node(folder/'wiki')==wiki_node,'same original wiki inode under replacement')
                self.require({str(path.relative_to(folder)):path.read_bytes() for path in folder.rglob('*')
                              if path.is_file() and not path.is_symlink()}==original_project,
                             'all original settings/route/marker/pointer/wiki moved by rename')
                mutation_evidence=dict(kind=mutation,newContainerNode=seed.reader._node(folder),
                    preservedContainerNode=seed.reader._node(saved_container),sameWikiNode=seed.reader._node(folder/'wiki'))
            elif mutation=='reviewed-route-ahead':
                with seed.ro(case.catalog) as db:
                    meta=dict(db.execute("SELECT key,value FROM meta WHERE key IN ('binding','roots')"))
                self.require(meta=={key:intent['catalog'][key][0] for key in ('binding','roots')},
                             'catalog remains at old roots/binding before illegal metadata change')
                self.require(seed.reader._node(case.store)==intent['oldLedger']['bindingStoreIdentity'],
                             'original old binding store inode remains')
                reviewed=case.state/'reviewed-route.json'
                self.require(reviewed.read_bytes()==intent['oldRoute'].encode('utf8'),'reviewed route initially old')
                reviewed.write_bytes(intent['newRoute'].encode('utf8'))
                self.require((folder/fresh.ROUTE).read_bytes()==intent['oldRoute'].encode('utf8'),
                             'project route remains old while only reviewed route advances')
                self.require((case.state/fresh.LEDGER).read_bytes()==mover._canonical(intent['fencedLedger']),
                             'ledger remains exact fenced old metadata')
                mutation_evidence=dict(kind=mutation,reviewedRoute='new',projectRoute='old',
                    catalog='old',binding='old',ledger='exact-fenced')
            elif mutation=='binding-before-catalog':
                mover._swap_store(intent)
                self.require(seed.reader._node(case.store)==intent['store']['identity'],
                             'actual canonical binding swap without catalog rebind')
                self.require(not Path(intent['store']['temp']).exists(),'actual binding temp consumed')
                with seed.ro(case.catalog) as db:
                    meta=dict(db.execute("SELECT key,value FROM meta WHERE key IN ('binding','roots')"))
                self.require(meta=={key:intent['catalog'][key][0] for key in ('binding','roots')},
                             'catalog remains exactly old after premature binding swap')
                mutation_evidence=dict(kind=mutation,catalog='old',binding='new',temporaryBindingExists=False)
            elif mutation=='rebound-catalog-row-changed':
                mover._anchor_wiki(case.project)
                mover._rebind_catalog(intent)
                with closing(sqlite3.connect(case.catalog.as_uri()+'?mode=rw',uri=True)) as db:
                    row=db.execute('SELECT seq,path,size FROM scan_files ORDER BY seq,path LIMIT 1').fetchone()
                    self.require(row is not None,'actual catalog scan member before mutation')
                    db.execute('UPDATE scan_files SET size=size+1 WHERE seq=? AND path=?',row[:2]);db.commit()
                    self.require(db.execute('SELECT size FROM scan_files WHERE seq=? AND path=?',row[:2]).fetchone()
                                 ==(row[2]+1,),'actual committed catalog member mutation')
                    meta=dict(db.execute("SELECT key,value FROM meta WHERE key IN ('binding','roots')"))
                self.require(meta=={key:intent['catalog'][key][1] for key in ('binding','roots')},
                             'catalog retains expected new metadata despite scan row mutation')
                # Classify the separate cold-WAL physical gate with bare
                # SQLite, then keep existing sidecars for this T2 tuple guard.
                trial=case.home/'catalog-bare-sqlite-control.sqlite'
                trial.write_bytes(case.catalog.read_bytes());trial.chmod(0o600)
                baseline=trial.read_bytes()
                with closing(sqlite3.connect(trial.as_uri()+'?mode=ro',uri=True)) as control:
                    control.execute('SELECT count(*) FROM scan_files').fetchone()
                    aux=[suffix for suffix in ('-wal','-shm') if Path(str(trial)+suffix).exists()]
                self.require(aux==['-wal','-shm'] and trial.read_bytes()==baseline,
                             'bare SQLite cold WAL creates aux without content changes')
                keepalive=sqlite3.connect(case.catalog.as_uri()+'?mode=rw',uri=True)
                self.addCleanup(keepalive.close)
                keepalive.execute('SELECT count(*) FROM scan_files').fetchone()
                self.require(all(Path(str(case.catalog)+suffix).exists() for suffix in ('-wal','-shm')),
                             'T2 metadata guard uses explicitly existing source auxiliaries')
                mutation_evidence=dict(kind=mutation,catalog='new',binding='old',scanMemberChanged=True,
                    bareSQLiteColdAuxCreated=aux,bareSQLiteContentUnchanged=True,
                    physicalColdBoundaryProved=False,sourceAuxiliariesPreexisting=True)
            elif mutation in catalog_mutations or mutation=='catalog-without-anchor':
                import laya_move_read_seal as M
                from laya_index_host_route import wiki_index_roots
                from laya_index_source_port import SourceStale
                # Keep actual committed SQL connections alive, so existing WAL
                # auxiliaries isolate these semantic guards from the separate
                # still-pending cold-open/sidecar-creation contract.
                catalog_connection=sqlite3.connect(case.catalog.as_uri()+'?mode=rw',uri=True)
                self.addCleanup(catalog_connection.close)
                catalog_connection.execute('SELECT count(*) FROM meta').fetchone()
                binding_connection=sqlite3.connect(case.store.as_uri()+'?mode=rw',uri=True)
                self.addCleanup(binding_connection.close)
                binding_connection.execute('SELECT count(*) FROM sqlite_master').fetchone()
                old_route=json.loads(intent['oldRoute']);new_route=json.loads(intent['newRoute'])
                old_roots=wiki_index_roots(old_route['collections'])
                new_roots=wiki_index_roots(new_route['collections'])
                temporary=Path(intent['store']['temp'])
                self.require(seed.reader._node(case.store)==intent['oldLedger']['bindingStoreIdentity'],
                             'canonical old binding inode retained at semantic mutation boundary')
                self.require(temporary.is_file() and seed.reader._node(temporary)==intent['store']['identity'],
                             'real prepared temporary binding retains its original inode')
                old_bound=M._binding(case.store,case.state,old_roots,old_route,seal.old_generation)
                new_bound=M._binding(temporary,case.state,new_roots,old_route,intent['generation']['gen'])
                self.require(seed.reader._raw(new_bound.as_record())==seed.reader._raw(intent['generation']['binding']),
                             'actual temporary new-generation binding decoder control')
                if mutation=='canonical-old-binding-header':
                    previous_header=binding_connection.execute('PRAGMA application_id').fetchone()[0]
                    self.require(previous_header!=0,'actual canonical old binding has its format header')
                    binding_connection.execute('PRAGMA application_id=0');binding_connection.commit()
                    self.require(binding_connection.execute('PRAGMA application_id').fetchone()==(0,),
                                 'actual old binding application_id mutation committed')
                    decoder_error=None
                    try:M._binding(case.store,case.state,old_roots,old_route,seal.old_generation)
                    except (SourceStale,ValueError,OSError,RuntimeError) as exc:decoder_error=str(exc)
                    mutation_evidence=dict(kind=mutation,applicationIdBefore=previous_header,applicationIdAfter=0,
                        actualCanonicalOldDecoderError=decoder_error,newTemporaryDecoderValid=True,
                        oldCanonicalDecoderValidBefore=True,sameCanonicalNode=seed.reader._node(case.store),
                        sourceConnectionsKeptAlive=True,physicalColdBoundaryProved=False)
                    print(json.dumps(dict(case='canonical-binding-header-control',observed=mutation_evidence)),flush=True)
                    with self.subTest(boundary='actual-old-binding-decoder-refuses-header'):
                        self.assertIsNotNone(decoder_error,'actual decoder must refuse the same damaged canonical binding')
                elif mutation=='catalog-scope-nul':
                    before_scope=catalog_connection.execute("SELECT value FROM meta WHERE key='scope'").fetchone()[0]
                    self.require(before_scope==old_route['scopeId'],'actual catalog has expected scope before mutation')
                    before_digest=M._logical(case.catalog,case.state)
                    before_dump=tuple(catalog_connection.iterdump())
                    catalog_connection.execute("UPDATE meta SET value=value||char(0)||'other' WHERE key='scope'")
                    catalog_connection.commit()
                    after_scope,scope_hex=catalog_connection.execute(
                        "SELECT value,hex(CAST(value AS BLOB)) FROM meta WHERE key='scope'").fetchone()
                    self.require(after_scope.encode('utf8')==before_scope.encode('utf8')+b'\x00other',
                                 'actual committed TEXT scope contains all appended NUL bytes')
                    after_digest=M._logical(case.catalog,case.state)
                    mutation_evidence=dict(kind=mutation,scopeBeforeHex=before_scope.encode('utf8').hex().upper(),
                        scopeAfterHex=scope_hex,actualTextBytesChanged=True,
                        beforeDigest=before_digest,afterDigest=after_digest,
                        sqlDumpEqual=tuple(catalog_connection.iterdump())==before_dump,
                        oldCanonicalDecoderValid=True,newTemporaryDecoderValid=True,
                        sourceConnectionsKeptAlive=True,physicalColdBoundaryProved=False)
                else:
                    anchor=folder/'wiki'/mover.project_dir.WIKI_ANCHOR
                    self.require(not os.path.lexists(anchor),'actual anchor-entry checkpoint has no wiki anchor')
                    mover._rebind_catalog(intent)
                    rebound=dict(catalog_connection.execute(
                        "SELECT key,value FROM meta WHERE key IN ('binding','roots')"))
                    self.require(rebound=={key:intent['catalog'][key][1] for key in ('binding','roots')},
                                 'actual catalog rebind commits despite deliberately omitted anchor')
                    self.require(not os.path.lexists(anchor),'no synthetic or actual anchor was added')
                    with seed.ro(case.db) as db:
                        self.require(db.execute('SELECT state FROM generations WHERE gen=?',
                            (intent['generation']['gen'],)).fetchone()==('prepared',),'target still prepared without anchor')
                    self.require(seed.reader._node(case.store)==intent['oldLedger']['bindingStoreIdentity'] and
                                 temporary.is_file(),'old binding and prepared temp survive catalog-only advance')
                    mutation_evidence=dict(kind=mutation,actualCatalogCommit=True,anchorAbsent=True,
                        catalog='new',binding='old',temporaryBindingExists=True,targetState='prepared',
                        oldCanonicalDecoderValid=True,newTemporaryDecoderValid=True,
                        sourceConnectionsKeptAlive=True,physicalColdBoundaryProved=False)
            elif mutation=='reserved-generation-ahead':
                with seed.ro(case.db) as db:
                    self.require(db.execute('SELECT state FROM generations WHERE gen=?',
                        (intent['generation']['gen'],)).fetchone()==('prepared',),'actual reserved generation starts prepared')
                mover._publish(intent)
                with seed.ro(case.db) as db:
                    self.require(db.execute('SELECT state FROM generations WHERE gen=?',
                        (intent['generation']['gen'],)).fetchone()==('published',),'actual _publish publishes reserved generation')
                    self.require(db.execute('SELECT gen FROM head WHERE scope=?',
                        (case.route['scopeId'],)).fetchone()==(intent['generation']['gen'],),'actual reserved target is live head')
                with seed.ro(case.catalog) as db:
                    meta=dict(db.execute("SELECT key,value FROM meta WHERE key IN ('binding','roots')"))
                self.require(meta=={key:intent['catalog'][key][0] for key in ('binding','roots')},'catalog remains old')
                self.require(seed.reader._node(case.store)==intent['oldLedger']['bindingStoreIdentity'],'binding remains old')
                self.require((case.state/'reviewed-route.json').read_bytes()==intent['oldRoute'].encode('utf8'),'reviewed route remains old')
                mutation_evidence=dict(kind=mutation,actualReservedHead=intent['generation']['gen'],
                    reservedState='published',catalog='old',binding='old',metadata='old-fenced')
            elif mutation=='missing-intent-public':
                intent_path=case.state/mover.INTENT
                self.require(intent_path.is_file(),'actual state intent exists before fixture removal')
                intent_path.unlink()
                self.require(mover.read_pointer(case.project) is not None,'actual project pointer remains')
                self.require(seed.reader.read_selection(case.journal)==selected,'actual retained selection remains')
                mutation_evidence=dict(kind=mutation,stateIntentAbsent=True,projectPointerPreserved=True,
                    retainedSelectionPreserved=True)
            else:raise RuntimeError('unknown test-owned prefix mutation')
            print(json.dumps(dict(case='physical-prefix-negative-input',mutation=mutation_evidence,
                                  actualRenameCheckpointProved=True)),flush=True)
        # Do not call seed.live(): its .auto-context path is deliberately stale.
        def snapshot():
            return dict(project={str(path.relative_to(folder)):path.read_bytes() for path in folder.rglob('*')
                                 if path.is_file() and not path.is_symlink()},
                state={str(path.relative_to(case.state)):path.read_bytes() for path in case.state.rglob('*')
                       if path.is_file() and not path.is_symlink()},
                catalogAuxNodes={suffix:None if not os.path.lexists(str(case.catalog)+suffix) else
                    tuple(getattr(Path(str(case.catalog)+suffix).lstat(),key)
                          for key in ('st_dev','st_ino','st_mode','st_uid','st_nlink'))
                    for suffix in ('-wal','-shm')},
                folderNode=seed.reader._node(folder),wikiNode=seed.reader._node(folder/'wiki'),
                savedContainerNode=None if saved_container is None else seed.reader._node(saved_container))
        before=snapshot();journal_node=case.journal.stat()
        query_physical_before=physical_state() if published_prefix else None
        if mutation=='missing-intent-public':
            error,outcome=None,None
            try:
                with seed.authority():outcome=mover.move(case.project,case.state)
            except (ValueError,OSError,RuntimeError) as exc:error=str(exc)
            print(json.dumps(dict(case='missing-state-intent-public-move',actualWriterExit=child.returncode,
                mutation=mutation_evidence,error=error,result=outcome,filesUnchanged=snapshot()==before)),flush=True)
            self.assertEqual(snapshot(),before)
            self.assertTrue(os.path.samestat(journal_node,case.journal.stat()))
            self.assertIsNotNone(error,'missing intent must not bypass retained completion safety with project_dir_current')
            self.assertIsNone(outcome)
            return
        probe=journal_fixture.FRESH_READER if mutation is None else NEGATIVE_READER
        result=subprocess.run([sys.executable,'-B','-I','-c',probe,str(ROOT),str(payload)],
            input='release\n',capture_output=True,encoding='utf8',timeout=30)
        observed=json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else None
        after=snapshot()
        changed=[[group,path] for group in ('project','state') for path in
                 sorted(set(before[group])|set(after[group])) if before[group].get(path)!=after[group].get(path)]
        # Existing SHM concurrency bytes are not application DB contents.
        # Its presence, inode, owner and mode, plus every WAL/main byte and
        # new-file check, remain part of the unchanged read contract.
        comparable={**after,'state':dict(after['state'])}
        shm=case.catalog.name+'-shm'
        existing_shm=(shm in before['state'] and shm in after['state']
                      and before['catalogAuxNodes']['-shm']==after['catalogAuxNodes']['-shm'])
        if existing_shm:comparable['state'][shm]=before['state'][shm]
        published_physical_changes=None
        published_new_files=None
        published_removed_files=None
        query_new_files=None
        if published_prefix:
            physical_after=physical_state()
            published_new_files=sorted(set(physical_after)-set(published_physical_before))
            published_removed_files=sorted(set(published_physical_before)-set(physical_after))
            published_physical_changes=[]
            known_shm={case.db.name+'-shm',case.catalog.name+'-shm'}
            for path in sorted(set(published_physical_before)|set(physical_after)):
                original=published_physical_before.get(path);current=physical_after.get(path)
                same_existing_shm=(path in known_shm and original is not None and current is not None
                                   and original[1]==current[1])
                if original!=current and not same_existing_shm:published_physical_changes.append(path)
            for path,original in query_physical_before.items():
                current=physical_after.get(path)
                if (path in known_shm and current is not None and original[1]==current[1]
                        and path in before['state'] and path in after['state']):
                    comparable['state'][path]=before['state'][path]
            query_new_files=sorted(set(physical_after)-set(query_physical_before))
        unchanged=comparable==before
        print(json.dumps(dict(case='physical-project-prefix-fresh-reader',phase=phase,actualWriterExit=child.returncode,
            originalFolderNode=folder_node,originalWikiNode=wiki_node,exactFencedOldMetadataBeforeMutation=True,
            storePrefix=prefix_evidence,
            resumed=False,readReturncode=result.returncode,read=observed,stderr=result.stderr[:4096],
            mutation=mutation_evidence,queryUnchanged=unchanged,queryChangedPaths=changed,
            existingShmConcurrencyOnly=existing_shm and changed==[['state',shm]],
            semanticFTSSucceeded=result.returncode==0 and observed is not None and observed.get('ftsRows',0)>0,
            closedWriterPhysicalChangedPaths=published_physical_changes,
            closedWriterNewFiles=published_new_files,closedWriterRemovedFiles=published_removed_files,
            queryNewFiles=query_new_files,
            catalogHeader=list(case.catalog.read_bytes()[18:20]),
            classification='required-reader-feature-gate-after-actual-prefix-proof' if mutation is None else 'protection-guard-negative')),flush=True)
        if mutation=='catalog-scope-nul':
            with self.subTest(boundary='full-text-byte-commitment'):
                self.assertNotEqual(mutation_evidence['beforeDigest'],mutation_evidence['afterDigest'],
                                    'logical commitment must retain TEXT bytes after embedded NUL')
        if published_prefix:
            with self.subTest(boundary='closed-writer-physical-cold-no-new-aux'):
                self.assertEqual(published_physical_changes,[],
                    'closed writer/prerequisite/query must preserve main/WAL/metadata/files and aux identities')
        self.assertTrue(unchanged,'fresh prefix reader must preserve DB/WAL/metadata, aux identities and file set')
        self.assertTrue(os.path.samestat(journal_node,case.journal.stat()))
        if mutation is not None:
            events=[json.loads(line) for line in result.stdout.splitlines() if line.strip()]
            self.assertEqual(result.returncode,3,result.stdout+result.stderr)
            self.assertTrue(events,'negative reader must report an actual refusal')
            self.assertTrue(all(event['event']!='capability-issued' for event in events),
                            'even a subsequently failing query must not mint a typed capability')
            self.assertEqual(events[-1]['event'],'refused')
            return
        self.assertEqual(result.returncode,0,'feature RED: exact retained move prefix must be readable before resume\n'+result.stderr)
        self.assertIsNotNone(observed)
        self.assertEqual(observed['endpoint'],selected.preferred_endpoint)
        self.assertEqual(observed['pinnedGeneration'],seal.old_generation)
        self.assertEqual(observed['exclusionEpoch'],seal.exclusion_epoch)
        self.assertEqual(observed['installerCalls'],0)
        self.assertGreater(observed['ftsRows'],0)

    def test_actual_rename_and_project_fsync_before_anchor_is_readable_without_resume(self):
        self.check_prefix('anchor-entry')

    def test_actual_fenced_before_rename_is_readable_without_resume(self):
        self.check_prefix('fenced-before-rename')

    def test_actual_catalog_commit_before_store_swap_is_readable_without_resume(self):
        self.check_prefix('catalog-rebound-before-store-swap')

    def test_actual_store_swap_fsync_before_publish_is_readable_without_resume(self):
        self.check_prefix('stores-swapped-before-publish')

    def test_actual_target_publish_before_records_is_readable_without_resume(self):
        self.check_prefix('published-before-records')

    def test_catalog_committed_prefix_rejects_damaged_canonical_old_binding_header(self):
        self.check_prefix('catalog-rebound-before-store-swap',mutation='canonical-old-binding-header')

    def test_catalog_committed_prefix_commits_full_scope_text_and_rejects_embedded_nul(self):
        self.check_prefix('catalog-rebound-before-store-swap',mutation='catalog-scope-nul')

    def test_actual_rename_rejects_catalog_rebind_without_preceding_wiki_anchor(self):
        self.check_prefix('anchor-entry',mutation='catalog-without-anchor')

    def test_renamed_prefix_rejects_replaced_container_even_when_original_wiki_inode_survives(self):
        self.check_prefix('anchor-entry',mutation='container-inode')

    def test_renamed_prefix_rejects_only_reviewed_route_advancing_ahead_of_catalog_binding_and_project_route(self):
        self.check_prefix('anchor-entry',mutation='reviewed-route-ahead')

    def test_binding_swap_before_catalog_rebind_is_refused_without_capability(self):
        self.check_prefix('anchor-entry',mutation='binding-before-catalog')

    def test_rebound_catalog_member_change_is_refused_without_capability(self):
        self.check_prefix('anchor-entry',mutation='rebound-catalog-row-changed')

    def test_public_mover_refuses_published_retained_intent_before_any_destructive_reprepare(self):
        seed=seed_module.MoveSealChecks('runTest')
        self.addCleanup(seed.doCleanups)
        try:
            seed.setUp()
            with seed.prepared() as intent:
                stage,seal_type=seed.producer()
                seal=stage(intent,installer=seed.installer)
                self.require(type(seal) is seal_type,'actual typed stage seal before public move')
                selected=seed.lease.publish_move_reader(seed.case.journal,seal=seal)
                seed.reader._recheck(seal.retained_endpoint)
        except Exception as exc:
            raise RuntimeError('public mover safety prerequisite failed before actual entry') from exc
        case=seed.case;mover=seed.mover
        self.require(not mover._fenced(intent),'actual publication is still unfenced')
        self.require(mover._canonical(mover._load_intent(case.state,case.project))==mover._canonical(intent),
                     'same durable published intent')
        def snapshot():
            return {str(path):(path.read_bytes(),path.lstat().st_dev,path.lstat().st_ino)
                    for root in (case.project,case.state) for path in root.rglob('*')
                    if path.is_file() and not path.is_symlink()}
        before=snapshot()
        intent_path=case.state/mover.INTENT;pointer=mover.pointer_path(case.project)
        intent_bytes=intent_path.read_bytes();pointer_bytes=pointer.read_bytes()
        error,result=None,None
        try:
            with seed.authority():result=mover.move(case.project,case.state)
        except (ValueError,OSError,RuntimeError) as exc:error=str(exc)
        after=snapshot()
        print(json.dumps(dict(case='public-mover-published-retained-safety',actualPublishedEndpoint=selected.preferred_endpoint,
            actualOwnerReleasedBeforePublicEntry=True,error=error,result=result,filesUnchanged=after==before,
            intentPreserved=intent_path.exists() and intent_path.read_bytes()==intent_bytes,
            pointerPreserved=pointer.exists() and pointer.read_bytes()==pointer_bytes,
            classification='actual-public-path-safety-regression')),flush=True)
        with self.subTest(boundary='explicit-refusal'):
            self.assertIsNotNone(error,'unsupported retained successor must refuse before abort/reprepare')
            self.assertIsNone(result)
        self.assertTrue(after==before,'public mover must preserve published intent/stage/journal/files before successor support')
        self.assertEqual(intent_path.read_bytes(),intent_bytes)
        self.assertEqual(pointer.read_bytes(),pointer_bytes)


if __name__=='__main__':unittest.main(verbosity=2)

"""Explicit product initializer with disposable HOME and synthetic native runner."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, 'core')
import setup_entry
import qmd_runtime
import qmd_shared_store


class SharedSetup(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qmd-shared-setup-')
        # These fixtures execute a synthetic supported runtime on Linux CI too.
        # The product gate stays unchanged and has its own no-mutation case.
        self.platform_patch = patch('install_update.supported_platform', return_value=True)
        self.platform_patch.start(); self.addCleanup(self.platform_patch.stop)
        self.home = Path(self.temp.name).resolve()
        self.env = patch.dict(os.environ, {'HOME': str(self.home), 'QMD_LOCK_BASE':str(self.home/'locks'), 'QMD_BACKEND_STATE_DIR':str(self.home/'backend'), 'QMD_SETUP_GUARD_FIXTURE': '', 'QMD_RECALL_LOG': ''})
        self.env.start()
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'helpers'))
        from uid_home import isolate
        self.addCleanup(isolate(self.home))
        os.environ['TMPDIR']=str(self.home/'tmp')
        Path(os.environ['TMPDIR']).mkdir(mode=0o700)
        self.project = self.home / 'project'; self.project.mkdir()
        auto = self.project / '.auto-context'; auto.mkdir()
        (auto / 'settings.json').write_text('{"indexing": true,"collections": []}')
        self.original = self.home / '.cache/qmd/index.sqlite'; self.original.parent.mkdir(parents=True)
        self.original.write_bytes(b'existing global database - preserve')
        self.model = self.home / 'embed.gguf'; self.model.write_bytes(b'GGUF' + b'x'*64)
        package = self.home / 'package'; (package / 'bin').mkdir(parents=True)
        (package / 'package.json').write_text('{"version":"2.5.3"}')
        self.qmd = package / 'bin/qmd'
        self.qmd.write_text('''import sys,os,json,sqlite3,hashlib
from pathlib import Path
args=sys.argv[1:]
if '--version' in args: print('qmd 2.5.3');sys.exit(0)
if '--help' in args: print('qmd vsearch qmd search qmd get qmd collection add qmd update qmd embed');sys.exit(0)
p=Path(os.environ['INDEX_PATH']);config=Path(os.environ['QMD_CONFIG_DIR'])/'index.yml'
assert str(p).startswith(os.environ['HOME']) and p.name=='index.sqlite'
assert os.environ['HF_HUB_OFFLINE']=='1'
raw=config.read_text();cfg=json.loads(raw) if raw.lstrip().startswith('{') else {'collections':{},'models':{'embed':next(line.split(': ',1)[1] for line in raw.splitlines() if line.startswith('  embed: '))}};db=sqlite3.connect(p)
db.executescript('CREATE TABLE IF NOT EXISTS documents(collection TEXT,path TEXT,hash TEXT,active INTEGER);CREATE TABLE IF NOT EXISTS content(hash TEXT,doc TEXT);CREATE TABLE IF NOT EXISTS content_vectors(hash TEXT,seq INTEGER,model TEXT,embed_fingerprint TEXT,total_chunks INTEGER);CREATE TABLE IF NOT EXISTS store_collections(name TEXT,path TEXT,pattern TEXT);CREATE TABLE IF NOT EXISTS vectors_vec(hash_seq TEXT PRIMARY KEY,embedding "float[768] distance_metric=cosine");')
if args[:2]==['collection','add']:
 name=args[args.index('--name')+1];text=(Path(args[2])/'probe.md').read_bytes();sha=hashlib.sha256(text).hexdigest()
 db.execute('INSERT INTO documents VALUES (?,?,?,1)',(name,'probe.md',sha));db.execute('INSERT INTO content VALUES (?,?)',(sha,text.decode()));db.execute('INSERT INTO store_collections VALUES (?,?,?)',(name,args[2],args[args.index('--mask')+1]))
 cfg['collections']={name:{'path':args[2]}}
 mask=args[args.index('--mask')+1]
 config.write_text('collections:\\n  '+name+':\\n    path: '+args[2]+'\\n    pattern: '+json.dumps(mask)+'\\nmodels:\\n  embed: '+cfg['models']['embed']+'\\n')
if args[0]=='embed':
 if os.environ.get('QMD_FORCE_CPU')!='1':print(json.dumps({'schema':'qmd-native-no-build-v1','gpu':'metal','build':'never','skipDownload':True}),file=sys.stderr)
 if (Path(os.environ['HOME'])/'fail-embed').exists():sys.exit(2)
 model=cfg['models']['embed'];sha=hashlib.sha256(Path(model).read_bytes()).hexdigest()
 db.execute('INSERT INTO content_vectors SELECT hash,0,?,?,1 FROM documents',(model,sha))
 for (h,) in db.execute('SELECT hash FROM documents').fetchall():db.execute('INSERT OR REPLACE INTO vectors_vec VALUES (?,?)',(h+'_0',bytes(768*4)))
if args[0]=='vsearch':raise AssertionError('query expansion/download path forbidden')
if args[0]=='query':
 if os.environ.get('QMD_FORCE_CPU')!='1':print(json.dumps({'schema':'qmd-native-no-build-v1','gpu':'metal','build':'never','skipDownload':True}),file=sys.stderr)
 assert args[1].startswith('vec: ') and '--no-rerank' in args and '-c' in args
 name=db.execute('SELECT name FROM store_collections').fetchone()[0];print(json.dumps([{'file':'qmd://'+name+'/probe.md','score':1}]))
if args[:2]==['collection','remove']:
 db.execute('DELETE FROM store_collections');db.execute('DELETE FROM documents')
 config.write_text('collections: {}\\nmodels:\\n  embed: '+cfg['models']['embed']+'\\n')
if args[0]=='cleanup':db.execute('DELETE FROM content');db.execute('DELETE FROM content_vectors');db.execute('DELETE FROM vectors_vec')
db.commit();db.close()
''');self.qmd.chmod(0o700)
        self.node = self.home / 'node'
        self.node.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then echo v24.14.0; elif [ "${1##*/}" = "qmd-native.mjs" ] || [ "${1##*/}" = "qmd.js" ]; then shift; exec '+sys.executable+' '+str(self.qmd)+' "$@"; else exec '+sys.executable+' "$@"; fi\n');self.node.chmod(0o700)
        (package/'dist/cli').mkdir(parents=True)
        (package/'dist/cli/qmd.js').write_text('// native runner uses a separate real-Node policy fixture')
        (package/'dist/llm.js').write_text('// synthetic package contract')
        bin_dir=self.home/'bin';bin_dir.mkdir()
        global_qmd=bin_dir/'qmd';global_qmd.write_text('#!/bin/sh\nexec '+str(self.node)+' '+str(self.qmd)+' "$@"\n');global_qmd.chmod(0o700)
        os.environ['PATH']=str(bin_dir)+os.pathsep+os.environ.get('PATH','')
        self.socket_patch=patch('shared_qmd_setup.qmd_endpoint.assert_quiescent',return_value=None)
        self.socket_patch.start()
        self.fence_identity=patch('qmd_write_fence.alive',return_value=True);self.fence_identity.start()
        self.options=['--qmd-cpu','--qmd-mode','reuse','--qmd-entry',str(self.qmd),'--node',str(self.node),'--embedding-model-file',str(self.model),'--expected-dimension','768','--approve-index-execution']
        self.original_settings=(auto/'settings.json').read_bytes()

    def tearDown(self):
        self.fence_identity.stop();self.socket_patch.stop();self.env.stop();self.temp.cleanup()

    def cli(self, action, options=None):
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            code=setup_entry.main([action,'--project',str(self.project),*(self.options if options is None else options)])
        return code,json.loads(output.getvalue())

    def assert_preserved(self):
        self.assertEqual(self.original.read_bytes(),b'existing global database - preserve')
        self.assertEqual((self.project/'.auto-context/settings.json').read_bytes(),self.original_settings)
        self.assertFalse((qmd_shared_store.root()/'active.json').exists())
        self.assertFalse((qmd_runtime.managed_root()/'active.json').exists())

    def test_inspect_without_store_is_read_only(self):
        result=setup_entry.discover(self.project)
        self.assertEqual(result['discovered']['sharedStoreStatus'],'not_prepared')
        self.assertFalse(qmd_runtime.managed_root().parent.parent.exists())
        update=setup_entry.check_update(self.project)
        self.assertIn('plugin_qmd_store_not_ready',update['reasons'])
        self.assertFalse(qmd_runtime.managed_root().parent.parent.exists())

    def test_unsupported_platform_remains_blocked_without_staging(self):
        with patch('install_update.supported_platform', return_value=False):
            code,result=self.cli('prepare-store')
        self.assertEqual(code,1,result)
        self.assertIn('unsupported_managed_platform',result['blockers'])
        self.assertFalse(qmd_runtime.managed_root().parent.parent.exists())
        self.assert_preserved()

    def test_missing_approval_model_and_invalid_dimension_do_not_stage(self):
        for opts in [self.options[:-1], self.options[:-3]+['--expected-dimension','0','--approve-index-execution']]:
            code,result=self.cli('prepare-store',opts)
            self.assertEqual(code,1);self.assertEqual(result['status'],'blocked')
            self.assertFalse(qmd_runtime.managed_root().parent.parent.exists())
        self.model.unlink()
        code,result=self.cli('prepare-store');self.assertEqual(code,1)
        self.assertIn('local_embedding_model_required',result['blockers'])
        self.assert_preserved()

    def test_prepare_is_inactive_proved_and_idempotent(self):
        code,result=self.cli('prepare-store');self.assertEqual(code,0,result)
        self.assertEqual(result['status'],'prepared_inactive')
        generation=Path(result['generation'])
        proof=json.loads((generation/'bootstrap-proof.json').read_text())
        self.assertEqual(proof['dimension'],768)
        self.assertEqual(proof['modelSha256'],hashlib.sha256(self.model.read_bytes()).hexdigest())
        self.assertTrue(proof['searchProved'])
        with contextlib.closing(sqlite3.connect(generation/'index.sqlite')) as db, db:
            for table in ['documents','content','content_vectors','store_collections']:
                self.assertEqual(db.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
        self.assert_preserved()
        code,second=self.cli('prepare-store');self.assertEqual(code,0,second)
        self.assertEqual(second['generation'],str(generation))
        code,status=self.cli('store-status',[]);self.assertEqual(code,0,status)
        self.assertEqual(status['status'],'prepared_inactive')

    def test_failed_probe_retry_preserves_sources(self):
        (self.home/'fail-embed').touch()
        code,result=self.cli('prepare-store');self.assertEqual(code,1,result)
        self.assertEqual(result['status'],'failed_preparing')
        self.assert_preserved()
        (self.home/'fail-embed').unlink()
        code,result=self.cli('prepare-store');self.assertEqual(code,0,result)
        self.assert_preserved()

    def test_model_or_prepared_database_drift_rejected(self):
        code,result=self.cli('prepare-store');self.assertEqual(code,0,result)
        self.model.write_bytes(b'GGUFchanged')
        code,again=self.cli('prepare-store');self.assertEqual(code,1,again)
        self.assert_preserved()

    def test_wrong_dimension_preserves_sources(self):
        options=self.options.copy();options[options.index('768')]='512'
        code,result=self.cli('prepare-store',options);self.assertEqual(code,1,result)
        self.assertEqual(result['reason'],'plugin_qmd_bootstrap_vector_invalid')
        self.assert_preserved()

    def test_prepared_database_drift_blocks_activation(self):
        code,result=self.cli('prepare-store');self.assertEqual(code,0,result)
        index=Path(result['generation'])/'index.sqlite'
        with contextlib.closing(sqlite3.connect(index)) as db, db:
            db.execute("INSERT INTO content VALUES ('unreviewed','synthetic changed body')")
        code,result=self.cli('activate-store',['--approve-store-activation','--old-host-sessions-restarted'])
        self.assertEqual(code,1,result);self.assertEqual(result['reason'],'store_preparation_changed')
        self.assert_preserved()

    def test_existing_shared_pointer_is_never_overwritten(self):
        root=qmd_shared_store.root();root.mkdir(parents=True,mode=0o700)
        pointer=root/'active.json';pointer.write_bytes(b'preserved existing pointer');pointer.chmod(0o600)
        code,result=self.cli('prepare-store');self.assertEqual(code,1,result)
        self.assertIn('plugin_qmd_store_already_selected',result['blockers'])
        self.assertEqual(pointer.read_bytes(),b'preserved existing pointer')
        self.assertEqual(self.original.read_bytes(),b'existing global database - preserve')

    def test_cutover_failure_restores_only_own_new_runtime(self):
        code,result=self.cli('prepare-store');self.assertEqual(code,0,result)
        flags=['--approve-store-activation','--old-host-sessions-restarted']
        with patch('shared_qmd_setup.coordinator._daemon_identity',return_value=None), patch('qmd_shared_store.activate_empty',side_effect=ValueError('synthetic store activation failure')):
            code,result=self.cli('activate-store',flags);self.assertEqual(code,1,result)
        self.assert_preserved()
        code,result=self.cli('store-status',[]);self.assertEqual(result['status'],'prepared_inactive')

    def test_interrupted_activation_requires_recovery_and_retains_pointer(self):
        code,result=self.cli('prepare-store');self.assertEqual(code,0,result)
        journal_path=qmd_shared_store.root()/'bootstrap-journal.json'
        journal=json.loads(journal_path.read_text());journal['phase']='activating'
        journal_path.write_text(json.dumps(journal))
        code,result=self.cli('activate-store',['--approve-store-activation','--old-host-sessions-restarted'])
        self.assertEqual(code,1,result);self.assertEqual(result['status'],'recovery_required')
        self.assert_preserved()

    def test_runtime_activation_never_overwrites_existing_selection(self):
        code,result=self.cli('prepare-store');self.assertEqual(code,0,result)
        journal=json.loads((qmd_shared_store.root()/'bootstrap-journal.json').read_text())
        root=qmd_runtime.managed_root();generation=journal['runtime']['generation']
        qmd_runtime.activate(root,generation)
        before=(root/'active.json').read_bytes()
        with self.assertRaisesRegex(ValueError,'qmd_managed_already_active'):
            qmd_runtime.activate(root,generation,require_unselected=True)
        self.assertEqual((root/'active.json').read_bytes(),before)

    def test_conditional_runtime_cleanup_preserves_competing_selection(self):
        import threading,fcntl
        code,result=self.cli('prepare-store');self.assertEqual(code,0,result)
        journal=json.loads((qmd_shared_store.root()/'bootstrap-journal.json').read_text())
        root=qmd_runtime.managed_root();qmd_runtime.activate(root,journal['runtime']['generation'])
        pointer=root/'active.json';oldsha=hashlib.sha256(pointer.read_bytes()).hexdigest()
        fd=os.open(root/'activation.lock',os.O_RDWR);fcntl.flock(fd,fcntl.LOCK_EX)
        result=[]
        thread=threading.Thread(target=lambda:result.append(qmd_runtime.deselect_if_matches(root,oldsha)))
        thread.start()
        replacement=b'{"another":"installer"}';pointer.write_bytes(replacement)
        fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd);thread.join(3)
        self.assertFalse(thread.is_alive());self.assertEqual(result,[False])
        self.assertEqual(pointer.read_bytes(),replacement)

    def test_first_registered_collection_add_update_keeps_selection_valid(self):
        import subprocess
        code,result=self.cli('prepare-store');self.assertEqual(code,0,result)
        with patch('shared_qmd_setup.coordinator._daemon_identity',return_value=None):
            code,result=self.cli('activate-store',['--approve-store-activation','--old-host-sessions-restarted']);self.assertEqual(code,0,result)
        wiki=self.project/'wiki';wiki.mkdir();(wiki/'probe.md').write_text('synthetic real card')
        cfg={'indexing':True,'collections':['fixture-wiki'],'collectionPaths':{'fixture-wiki':'wiki'},'collectionRoles':{'fixture-wiki':'wiki'}}
        (self.project/'.auto-context/settings.json').write_text(json.dumps(cfg))
        with patch('resolve_paths.is_risky_path',return_value=False):
            selected=qmd_shared_store.register_config(self.project,cfg)
        env={**os.environ,**{key:selected[key] for key in ('INDEX_PATH','QMD_CONFIG_DIR','XDG_CACHE_HOME')},'HF_HUB_OFFLINE':'1'}
        wrapper=qmd_runtime.select()['wrapper']
        subprocess.run([wrapper,'collection','add',str(wiki),'--name','fixture-wiki','--mask','**/*.md'],env=env,check=True)
        subprocess.run([wrapper,'update'],env=env,check=True)
        self.assertEqual(qmd_shared_store.select()['INDEX_PATH'],selected['INDEX_PATH'])
        self.assertEqual(self.original.read_bytes(),b'existing global database - preserve')

    def test_supported_cpu_choice_is_pinned_in_managed_wrapper(self):
        code,result=self.cli('prepare-store',self.options+['--qmd-cpu']);self.assertEqual(code,0,result)
        journal=json.loads((qmd_shared_store.root()/'bootstrap-journal.json').read_text())
        self.assertEqual(journal['request']['backend'],'cpu')
        self.assertIn('export QMD_FORCE_CPU=1',Path(journal['runtime']['wrapper']).read_text())
        self.assertTrue(qmd_runtime._prepared(journal['runtime']['generation'])['forceCpu'])
        self.assert_preserved()
        qmd_runtime.activate(qmd_runtime.managed_root(),journal['runtime']['generation'])
        self.assertTrue(qmd_runtime.select().get('forceCpu'),'selected daemon must receive attested CPU flag')

    def test_owned_listener_or_denied_inspection_blocks_activation(self):
        code,result=self.cli('prepare-store');self.assertEqual(code,0,result)
        flags=['--approve-store-activation','--old-host-sessions-restarted']
        for reason in ['qmd_daemon_must_be_quiescent','qmd_endpoint_identity_unavailable']:
            with patch('shared_qmd_setup.coordinator._daemon_identity',return_value=None), patch('shared_qmd_setup.qmd_endpoint.assert_quiescent',side_effect=ValueError(reason)):
                code,result=self.cli('activate-store',flags)
                self.assertEqual(code,1,result)
                self.assertEqual(result['reason'],reason)
        self.assert_preserved()

    def test_foreign_default_port_is_not_contacted_or_used_at_activation(self):
        code,result=self.cli('prepare-store');self.assertEqual(code,0,result)
        with patch('shared_qmd_setup.coordinator._daemon_identity',return_value=None), patch('shared_qmd_setup.socket.create_connection',side_effect=AssertionError('foreign listener contacted')):
            code,result=self.cli('activate-store',['--approve-store-activation','--old-host-sessions-restarted'])
        self.assertEqual(code,0,result);self.assertEqual(result['status'],'activated')
        self.assertEqual(self.original.read_bytes(),b'existing global database - preserve')
        self.assertEqual((self.project/'.auto-context/settings.json').read_bytes(),self.original_settings)

    def test_backend_choice_is_explicit_before_staging(self):
        options=[value for value in self.options if value!='--qmd-cpu']
        code,result=self.cli('prepare-store',options)
        self.assertEqual(code,1,result)
        self.assertIn('qmd_backend_selection_required',result['blockers'])
        self.assertFalse(qmd_runtime.managed_root().parent.parent.exists())

    def test_auto_backend_prepares_no_build_runtime_and_never_pins_cpu(self):
        options=[v for v in self.options if v!='--qmd-cpu']+['--qmd-backend','auto']
        code,result=self.cli('prepare-store',options);self.assertEqual(code,0,result)
        j=json.loads((qmd_shared_store.root()/'bootstrap-journal.json').read_text())
        runtime=qmd_runtime._prepared(j['runtime']['generation'])
        self.assertEqual(j['request']['backend'],'auto');self.assertTrue(runtime['nativeNoBuild'])
        self.assertNotIn('export QMD_FORCE_CPU',Path(runtime['wrapper']).read_text())
        self.assertEqual(j['proof']['nativeBackend'],'metal')
        self.assert_preserved()
        qmd_runtime.activate(qmd_runtime.managed_root(),j['runtime']['generation'])
        import qmd_route
        self.assertEqual(qmd_route.binary_info()['QMD_ENTRY'],runtime['entry'])

    def test_auto_native_policy_tamper_fails_closed(self):
        options=[v for v in self.options if v!='--qmd-cpu']+['--qmd-backend','auto']
        code,result=self.cli('prepare-store',options);self.assertEqual(code,0,result)
        j=json.loads((qmd_shared_store.root()/'bootstrap-journal.json').read_text())
        runtime=qmd_runtime._prepared(j['runtime']['generation'])
        Path(runtime['entry']).write_text('unreviewed policy')
        with self.assertRaisesRegex(ValueError,'qmd_native_policy_changed'):
            qmd_runtime._prepared(j['runtime']['generation'])
        self.assert_preserved()

    def test_active_auto_without_no_build_policy_is_not_adopted(self):
        staged=qmd_runtime.prepare_existing(qmd_runtime.managed_root(),str(self.qmd),str(self.node))
        qmd_runtime.activate(qmd_runtime.managed_root(),staged['generation'])
        pointer=qmd_runtime.managed_root()/'active.json';before=pointer.read_bytes()
        options=[v for v in self.options if v!='--qmd-cpu']+['--qmd-backend','auto']
        options[options.index('reuse')]='active'
        code,result=self.cli('plan-store',options);self.assertEqual(code,1,result)
        self.assertIn('auto_store_requires_no_build_runtime',result['blockers'])
        self.assertEqual(pointer.read_bytes(),before)

    def test_auto_requires_policy_receipt_not_just_cpu_vectors(self):
        options=[v for v in self.options if v!='--qmd-cpu']+['--qmd-backend','auto']
        with patch('shared_qmd_setup._native_receipts',return_value=[]):
            code,result=self.cli('prepare-store',options)
        self.assertEqual(code,1,result);self.assertEqual(result['reason'],'plugin_qmd_native_policy_unproved')
        self.assert_preserved()

    def test_explicit_backend_conflict_is_rejected(self):
        code,result=self.cli('prepare-store',self.options+['--qmd-backend','auto'])
        self.assertEqual(code,1,result);self.assertIn('qmd_backend_selection_conflict',result['blockers'])
        self.assert_preserved()

    def test_explicit_inactive_cpu_to_auto_reprepare_archives_without_data_deletion(self):
        code,result=self.cli('prepare-store');self.assertEqual(code,0,result)
        root=qmd_shared_store.root();old=(root/'bootstrap-journal.json').read_bytes()
        old_j=json.loads(old);old_index=Path(old_j['generation'])/'index.sqlite';before=old_index.read_bytes()
        options=[v for v in self.options if v!='--qmd-cpu']+['--qmd-backend','auto']
        code,result=self.cli('prepare-store',options);self.assertEqual(code,1,result)
        self.assertEqual(result['reason'],'store_setup_request_changed')
        code,result=self.cli('prepare-store',options+['--replace-inactive-store-preparation']);self.assertEqual(code,0,result)
        self.assertEqual((root/'journal-archive'/(old_j['requestSha256']+'.json')).read_bytes(),old)
        self.assertEqual(old_index.read_bytes(),before);self.assertNotEqual(result['generation'],old_j['generation'])
        self.assert_preserved()

    def test_active_non_cpu_runtime_is_not_silently_replaced(self):
        staged=qmd_runtime.prepare_existing(qmd_runtime.managed_root(),str(self.qmd),str(self.node))
        qmd_runtime.activate(qmd_runtime.managed_root(),staged['generation'])
        pointer=qmd_runtime.managed_root()/'active.json';before=pointer.read_bytes()
        options=self.options.copy();options[options.index('reuse')]='active'
        code,result=self.cli('prepare-store',options)
        self.assertEqual(code,1,result)
        self.assertIn('cpu_store_requires_cpu_runtime',result['blockers'])
        self.assertEqual(pointer.read_bytes(),before)
        self.assertFalse((qmd_shared_store.root()/'active.json').exists())

    def test_activation_separate_and_refuses_running_daemon(self):
        code,result=self.cli('prepare-store');self.assertEqual(code,0,result)
        code,result=self.cli('activate-store',[]);self.assertEqual(code,1,result)
        from unittest.mock import patch
        flags=['--approve-store-activation','--old-host-sessions-restarted']
        with patch('shared_qmd_setup.coordinator._daemon_identity',return_value={'healthy':True}):
            code,result=self.cli('activate-store',flags)
            self.assertEqual(code,1,result);self.assertEqual(result['reason'],'qmd_daemon_must_be_quiescent')
        self.assert_preserved()
        with patch('shared_qmd_setup.coordinator._daemon_identity',return_value=None):
            code,result=self.cli('activate-store',flags);self.assertEqual(code,0,result)
            self.assertEqual(result['status'],'activated')
            selected=qmd_shared_store.select();self.assertTrue(selected['shared'])
            code,again=self.cli('activate-store',flags);self.assertEqual(code,0,again)
            self.assertEqual(again['status'],'activated_unchanged')
        self.assertEqual(self.original.read_bytes(),b'existing global database - preserve')

if __name__=='__main__':unittest.main()

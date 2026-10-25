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
import uuid
from unittest.mock import patch

sys.path.insert(0, 'core')
import setup_entry
import setup_readiness
import qmd_runtime
import qmd_shared_store
import shared_qmd_setup


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

    def legacy_selected_store(self):
        """A readable selected store created before model/scope bootstrap proofs."""
        code,result=self.cli('prepare-store');self.assertEqual(code,0,result)
        with patch('shared_qmd_setup.coordinator._daemon_identity',return_value=None):
            code,activated=self.cli('activate-store',
                ['--approve-store-activation','--old-host-sessions-restarted'])
        self.assertEqual(code,0,activated)
        root=qmd_shared_store.root();generation=Path(result['generation'])
        prepared_path=generation/'prepared.json';prepared=json.loads(prepared_path.read_text())
        prepared.pop('scopePolicySha256')
        prepared_path.write_text(json.dumps(prepared,sort_keys=True));prepared_path.chmod(0o600)
        (generation/'scope-policy.json').unlink()
        (generation/'bootstrap-proof.json').unlink()
        (root/'bootstrap-journal.json').unlink()
        # A selected generation can contain content. Never infer that it is
        # disposable merely because its bootstrap receipts are older.
        with contextlib.closing(sqlite3.connect(generation/'index.sqlite')) as db, db:
            db.execute("INSERT INTO content VALUES (?,?)",('legacy-hash','old selected content'))
        self.assertEqual(qmd_shared_store.select()['generation'],str(generation))
        self.assertEqual(setup_readiness.qmd_model_reason(),
                         'qmd_embedding_model_proof_required')
        return {'generation':generation, 'pointer':root/'active.json',
                'pointerBytes':(root/'active.json').read_bytes(),
                'indexBytes':(generation/'index.sqlite').read_bytes(),
                'configBytes':(generation/'config/index.yml').read_bytes(),
                'preparedBytes':prepared_path.read_bytes(),
                'runtimePointerBytes':(qmd_runtime.managed_root()/'active.json').read_bytes()}

    def replacement_options(self):
        options=self.options.copy()
        options[options.index('reuse')]='active'
        for flag in ('--qmd-entry','--node'):
            position=options.index(flag)
            del options[position:position+2]
        options.extend(['--expected-embedding-model-sha256',
                        hashlib.sha256(self.model.read_bytes()).hexdigest(),
                        '--expected-embedding-model-bytes',str(self.model.stat().st_size)])
        return options

    def accepted_replacement_options(self,options):
        code,planned=self.cli('plan-replacement-store',options)
        self.assertEqual(code,0,planned)
        self.assertEqual(planned['status'],'ready_to_prepare')
        return options+['--accepted-replacement-request-sha256',planned['requestSha256']]

    def assert_legacy_preserved(self,old):
        self.assertEqual(old['pointer'].read_bytes(),old['pointerBytes'])
        self.assertEqual((old['generation']/'index.sqlite').read_bytes(),old['indexBytes'])
        self.assertEqual((old['generation']/'config/index.yml').read_bytes(),old['configBytes'])
        self.assertEqual((old['generation']/'prepared.json').read_bytes(),old['preparedBytes'])
        self.assertEqual((qmd_runtime.managed_root()/'active.json').read_bytes(),
                         old['runtimePointerBytes'])
        self.assertEqual(qmd_shared_store.select()['generation'],str(old['generation']))
        self.assertEqual(self.original.read_bytes(),b'existing global database - preserve')
        self.assertEqual((self.project/'.auto-context/settings.json').read_bytes(),
                         self.original_settings)

    def test_legacy_replacement_missing_explicit_model_stages_nothing(self):
        old=self.legacy_selected_store();options=self.replacement_options()
        root=qmd_shared_store.root();before=set((root/'generations').iterdir())
        model_flag=options.index('--embedding-model-file')
        missing_path=options[:model_flag]+options[model_flag+2:]
        code,missing=self.cli('plan-replacement-store',missing_path)
        self.assertEqual(code,1,missing)
        self.assertEqual(missing['status'],'blocked')
        self.assertFalse((root/'replacement-bootstrap-journal.json').exists())
        accepted=self.accepted_replacement_options(options)
        self.model.unlink()
        for action,flags in (('plan-replacement-store',options),
                             ('prepare-replacement-store',accepted)):
            code,result=self.cli(action,flags)
            self.assertEqual(code,1,result)
            self.assertIn('local_embedding_model_required',
                          result.get('blockers',[result.get('reason')]))
        self.assertFalse((root/'replacement-bootstrap-journal.json').exists())
        self.assertEqual(set((root/'generations').iterdir()),before)
        self.assert_legacy_preserved(old)

    def test_legacy_replacement_model_identity_mismatch_stages_nothing(self):
        old=self.legacy_selected_store();root=qmd_shared_store.root()
        options=self.replacement_options();before=set((root/'generations').iterdir())
        accepted=self.accepted_replacement_options(options)[-1]
        for flag,value in (('--expected-embedding-model-sha256','0'*64),
                           ('--expected-embedding-model-bytes',str(self.model.stat().st_size+1))):
            changed=options.copy();changed[changed.index(flag)+1]=value
            for action,flags in (('plan-replacement-store',changed),
                                 ('prepare-replacement-store',changed+[
                                     '--accepted-replacement-request-sha256',accepted])):
                code,result=self.cli(action,flags)
                self.assertEqual(code,1,result)
                self.assertEqual(result['status'],'blocked')
        self.assertFalse((root/'replacement-bootstrap-journal.json').exists())
        self.assertEqual(set((root/'generations').iterdir()),before)
        self.assert_legacy_preserved(old)

    def test_legacy_replacement_rejects_different_model_name_with_same_bytes(self):
        old=self.legacy_selected_store();root=qmd_shared_store.root()
        before=set((root/'generations').iterdir())
        alternate=self.home/'different.gguf'
        alternate.write_bytes(self.model.read_bytes());alternate.chmod(0o600)
        options=self.replacement_options()
        options[options.index('--embedding-model-file')+1]=str(alternate)
        for action in ('plan-replacement-store','prepare-replacement-store'):
            code,result=self.cli(action,options)
            self.assertEqual(code,1,result)
            self.assertIn('replacement_model_identifier_mismatch',result['blockers'])
        self.assertFalse((root/'replacement-bootstrap-journal.json').exists())
        self.assertEqual(set((root/'generations').iterdir()),before)
        self.assert_legacy_preserved(old)

    def test_legacy_replacement_wrong_accepted_digest_stages_nothing(self):
        old=self.legacy_selected_store();root=qmd_shared_store.root()
        options=self.replacement_options();before=set((root/'generations').iterdir())
        code,result=self.cli('prepare-replacement-store',options+[
            '--accepted-replacement-request-sha256','0'*64])
        self.assertEqual(code,1,result)
        self.assertFalse((root/'replacement-bootstrap-journal.json').exists())
        self.assertEqual(set((root/'generations').iterdir()),before)
        self.assert_legacy_preserved(old)

    def test_legacy_replacement_requires_selected_managed_runtime(self):
        old=self.legacy_selected_store();root=qmd_shared_store.root()
        options=self.replacement_options()
        options[options.index('active')]='reuse'
        before=set((root/'generations').iterdir())
        code,result=self.cli('plan-replacement-store',options)
        self.assertEqual(code,1,result)
        self.assertEqual(result['status'],'blocked')
        self.assertFalse((root/'replacement-bootstrap-journal.json').exists())
        self.assertEqual(set((root/'generations').iterdir()),before)
        self.assert_legacy_preserved(old)

    def test_legacy_replacement_dimension_mismatch_stages_nothing(self):
        old=self.legacy_selected_store();root=qmd_shared_store.root()
        options=self.replacement_options()
        options[options.index('--expected-dimension')+1]='512'
        before=set((root/'generations').iterdir())
        code,result=self.cli('plan-replacement-store',options)
        self.assertEqual(code,1,result)
        self.assertIn('replacement_dimension_768_required',result['blockers'])
        self.assertFalse((root/'replacement-bootstrap-journal.json').exists())
        self.assertEqual(set((root/'generations').iterdir()),before)
        self.assert_legacy_preserved(old)

    def test_legacy_replacement_same_name_other_absolute_model_is_not_selected(self):
        old=self.legacy_selected_store();root=qmd_shared_store.root()
        different_dir=self.home/'other-model-dir';different_dir.mkdir()
        alternate=different_dir/self.model.name
        alternate.write_bytes(self.model.read_bytes());alternate.chmod(0o600)
        options=self.replacement_options()
        options[options.index('--embedding-model-file')+1]=str(alternate)
        before=set((root/'generations').iterdir())
        code,result=self.cli('plan-replacement-store',options)
        self.assertEqual(code,1,result)
        self.assertIn('replacement_model_identifier_mismatch',result['blockers'])
        self.assertFalse((root/'replacement-bootstrap-journal.json').exists())
        self.assertEqual(set((root/'generations').iterdir()),before)
        self.assert_legacy_preserved(old)

    def test_legacy_replacement_runtime_wrapper_flip_before_journal(self):
        old=self.legacy_selected_store();root=qmd_shared_store.root()
        options=self.accepted_replacement_options(self.replacement_options())
        before=set((root/'generations').iterdir())
        selected=qmd_runtime.select()
        changed={**selected,'wrapper':str(self.home/'different-wrapper')}
        with patch('shared_qmd_setup.qmd_runtime.select',return_value=changed):
            code,result=self.cli('prepare-replacement-store',options)
        self.assertEqual(code,1,result)
        self.assertFalse((root/'replacement-bootstrap-journal.json').exists())
        self.assertEqual(set((root/'generations').iterdir()),before)
        self.assert_legacy_preserved(old)

    def test_legacy_replacement_pointer_identity_changes_before_acceptance(self):
        old=self.legacy_selected_store();root=qmd_shared_store.root()
        options=self.accepted_replacement_options(self.replacement_options())
        before=set((root/'generations').iterdir())
        replacement=root/'pointer-test-replacement.json'
        replacement.write_bytes(old['pointerBytes']);replacement.chmod(0o600)
        os.replace(replacement,old['pointer'])
        # Identical pointer bytes from a different inode are not the same
        # selected lifetime for an already accepted request.
        code,result=self.cli('prepare-replacement-store',options)
        self.assertEqual(code,1,result)
        self.assertFalse((root/'replacement-bootstrap-journal.json').exists())
        self.assertEqual(set((root/'generations').iterdir()),before)
        self.assert_legacy_preserved(old)

    def test_legacy_replacement_scope_settings_drift_before_journal(self):
        old=self.legacy_selected_store();root=qmd_shared_store.root()
        options=self.accepted_replacement_options(self.replacement_options())
        before=set((root/'generations').iterdir())
        settings=self.project/'.auto-context/settings.json'
        changed=b'{"indexing":true,"collections":[],"topN":4}\n'
        settings.write_bytes(changed)
        code,result=self.cli('prepare-replacement-store',options)
        self.assertEqual(code,1,result)
        self.assertFalse((root/'replacement-bootstrap-journal.json').exists())
        self.assertEqual(set((root/'generations').iterdir()),before)
        self.assertEqual(settings.read_bytes(),changed)
        self.assertEqual(old['pointer'].read_bytes(),old['pointerBytes'])
        self.assertEqual((old['generation']/'index.sqlite').read_bytes(),old['indexBytes'])
        self.assertEqual((old['generation']/'config/index.yml').read_bytes(),old['configBytes'])
        self.assertEqual(self.original.read_bytes(),b'existing global database - preserve')

    def test_legacy_replacement_explicit_supersession_archives_old_inactive(self):
        old=self.legacy_selected_store();root=qmd_shared_store.root()
        options=self.replacement_options()
        accepted=self.accepted_replacement_options(options)
        code,first=self.cli('prepare-replacement-store',accepted)
        self.assertEqual(code,0,first)
        first_generation=Path(first['generation'])
        first_index=(first_generation/'index.sqlite').read_bytes()
        previous_journal=(root/'replacement-bootstrap-journal.json').read_bytes()
        pointer_replacement=root/'pointer-same-bytes-new-lifetime.json'
        pointer_replacement.write_bytes(old['pointerBytes'])
        pointer_replacement.chmod(0o600)
        os.replace(pointer_replacement,old['pointer'])
        later=self.accepted_replacement_options(options)
        self.assertNotEqual(later[-1],accepted[-1])
        code,rejected=self.cli('prepare-replacement-store',later)
        self.assertEqual(code,1,rejected)
        self.assertEqual((root/'replacement-bootstrap-journal.json').read_bytes(),
                         previous_journal)
        code,second=self.cli('prepare-replacement-store',later+[
            '--replace-inactive-store-preparation'])
        self.assertEqual(code,0,second)
        self.assertEqual(second['status'],'prepared_inactive_replacement')
        self.assertIs(second['cutoverReady'],False)
        self.assertNotEqual(second['generation'],first['generation'])
        self.assertEqual((first_generation/'index.sqlite').read_bytes(),first_index)
        archived=root/'journal-archive'/('replacement-'+hashlib.sha256(
            previous_journal).hexdigest()+'.json')
        self.assertEqual(archived.read_bytes(),previous_journal)
        self.assert_legacy_preserved(old)

    def test_legacy_replacement_uuid_collision_cannot_reuse_selected_generation(self):
        old=self.legacy_selected_store();root=qmd_shared_store.root()
        options=self.accepted_replacement_options(self.replacement_options())
        before=set((root/'generations').iterdir())
        selected_uuid=uuid.UUID(hex=old['generation'].name[2:])
        next_uuid=uuid.uuid4()
        with patch('shared_qmd_setup.uuid.uuid4',side_effect=[selected_uuid,next_uuid]):
            code,failed=self.cli('prepare-replacement-store',options)
        self.assertEqual(code,1,failed)
        self.assertEqual(failed['status'],'failed_preparing')
        self.assertEqual(failed['reason'],'replacement_generation_collides_with_selected')
        self.assertIs(failed['cutoverReady'],False)
        self.assertEqual(set((root/'generations').iterdir()),before)
        self.assertFalse((root/'bootstrap-journal.json').exists())
        journal=json.loads((root/'replacement-bootstrap-journal.json').read_text())
        self.assertEqual(journal['phase'],'failed_preparing')
        self.assertIsNone(journal.get('generation'))
        self.assertNotEqual(journal.get('building',{}).get('generation'),
                            str(old['generation']))
        code,status=self.cli('replacement-store-status',[])
        self.assertEqual(code,1,status)
        self.assertEqual(status['status'],'failed_preparing')
        self.assertIs(status['cutoverReady'],False)
        self.assert_legacy_preserved(old)

    def test_legacy_replacement_proves_new_inactive_generation(self):
        old=self.legacy_selected_store();root=qmd_shared_store.root()
        options=self.replacement_options()
        accepted=self.accepted_replacement_options(options)
        self.assertFalse((root/'replacement-bootstrap-journal.json').exists())
        self.assert_legacy_preserved(old)
        code,result=self.cli('prepare-replacement-store',accepted)
        self.assertEqual(code,0,result)
        self.assertEqual(result['status'],'prepared_inactive_replacement')
        self.assertIs(result['cutoverReady'],False)
        replacement=Path(result['generation'])
        self.assertNotEqual(replacement,old['generation'])
        proof=json.loads((replacement/'bootstrap-proof.json').read_text())
        policy=json.loads((replacement/'scope-policy.json').read_text())
        journal=json.loads((root/'replacement-bootstrap-journal.json').read_text())
        self.assertTrue(proof['searchProved'])
        self.assertEqual(proof['modelPath'],str(self.model))
        self.assertEqual(proof['modelSha256'],hashlib.sha256(self.model.read_bytes()).hexdigest())
        self.assertEqual(proof['dimension'],768)
        self.assertEqual(policy['projects'][str(self.project)],
                         hashlib.sha256(self.original_settings).hexdigest())
        self.assertEqual(journal['phase'],'prepared_inactive_replacement')
        self.assertEqual(journal['proof'],proof)
        self.assertEqual(journal['generation'],str(replacement))
        self.assertEqual(qmd_shared_store.select()['generation'],str(old['generation']))
        code,status=self.cli('replacement-store-status',[])
        self.assertEqual(code,0,status)
        self.assertEqual(status['status'],'prepared_inactive_replacement')
        self.assertIs(status['cutoverReady'],False)
        self.assert_legacy_preserved(old)

    def test_legacy_activate_store_cannot_select_replacement(self):
        old=self.legacy_selected_store()
        options=self.accepted_replacement_options(self.replacement_options())
        code,prepared=self.cli('prepare-replacement-store',options)
        self.assertEqual(code,0,prepared)
        with patch('shared_qmd_setup.coordinator._daemon_identity',return_value=None):
            code,legacy=self.cli('activate-store',
                ['--approve-store-activation','--old-host-sessions-restarted'])
        self.assertEqual(code,0,legacy)
        self.assertEqual(legacy['status'],'not_prepared')
        self.assertNotEqual(qmd_shared_store.select()['generation'],
                            prepared['generation'])
        self.assert_legacy_preserved(old)

    def test_legacy_replacement_rejects_model_drift_without_switch(self):
        old=self.legacy_selected_store()
        options=self.accepted_replacement_options(self.replacement_options())
        code,result=self.cli('prepare-replacement-store',options)
        self.assertEqual(code,0,result)
        replacement=Path(result['generation']);proof=(replacement/'bootstrap-proof.json').read_bytes()
        self.model.write_bytes(b'GGUF' + b'changed model bytes')
        code,retry=self.cli('prepare-replacement-store',options)
        self.assertEqual(code,1,retry)
        self.assertEqual((replacement/'bootstrap-proof.json').read_bytes(),proof)
        self.assert_legacy_preserved(old)

    def test_legacy_replacement_interruption_retries_without_activation(self):
        old=self.legacy_selected_store()
        options=self.accepted_replacement_options(self.replacement_options())
        with patch('shared_qmd_setup._build',side_effect=ValueError('synthetic_replacement_interruption')):
            code,failed=self.cli('prepare-replacement-store',options)
        self.assertEqual(code,1,failed)
        self.assertEqual(failed['status'],'failed_preparing')
        self.assert_legacy_preserved(old)
        code,retry=self.cli('prepare-replacement-store',options)
        self.assertEqual(code,0,retry)
        self.assertEqual(retry['status'],'prepared_inactive_replacement')
        self.assertIs(retry['cutoverReady'],False)
        self.assertNotEqual(retry['generation'],str(old['generation']))
        self.assert_legacy_preserved(old)

    def test_legacy_replacement_embed_checkpoint_resumes_same_new_generation(self):
        old=self.legacy_selected_store();root=qmd_shared_store.root()
        options=self.accepted_replacement_options(self.replacement_options())
        (self.home/'fail-embed').touch()
        code,failed=self.cli('prepare-replacement-store',options)
        self.assertEqual(code,1,failed)
        self.assertEqual(failed['status'],'failed_preparing')
        journal=json.loads((root/'replacement-bootstrap-journal.json').read_text())
        self.assertEqual(journal['building']['inFlight'],'embed')
        interrupted=journal['building']['generation']
        self.assertNotEqual(interrupted,str(old['generation']))
        self.assert_legacy_preserved(old)
        (self.home/'fail-embed').unlink()
        code,retry=self.cli('prepare-replacement-store',options)
        self.assertEqual(code,0,retry)
        self.assertEqual(retry['status'],'prepared_inactive_replacement')
        self.assertEqual(retry['generation'],interrupted)
        self.assertIs(retry['cutoverReady'],False)
        self.assert_legacy_preserved(old)

    def test_legacy_replacement_failed_checkpoint_requires_explicit_restart(self):
        old=self.legacy_selected_store();root=qmd_shared_store.root()
        options=self.accepted_replacement_options(self.replacement_options())
        (self.home/'fail-embed').touch()
        code,failed=self.cli('prepare-replacement-store',options)
        self.assertEqual(code,1,failed)
        self.assertEqual(failed['status'],'failed_preparing')
        (self.home/'fail-embed').unlink()
        journal_path=root/'replacement-bootstrap-journal.json'
        failed_journal=json.loads(journal_path.read_text())
        first_generation=Path(failed_journal['building']['generation'])
        first_index=(first_generation/'index.sqlite').read_bytes()
        # The old checkpoint is no longer resumable. A normal retry must not
        # silently replace its candidate, even for the same accepted request.
        (first_generation/'config/index.yml').unlink()
        code,rejected=self.cli('prepare-replacement-store',options)
        self.assertEqual(code,1,rejected)
        self.assertEqual(rejected['status'],'failed_preparing')
        before_restart=journal_path.read_bytes()
        code,restarted=self.cli('prepare-replacement-store',options+[
            '--replace-inactive-store-preparation'])
        self.assertEqual(code,0,restarted)
        self.assertEqual(restarted['status'],'prepared_inactive_replacement')
        self.assertIs(restarted['cutoverReady'],False)
        self.assertNotEqual(restarted['generation'],str(first_generation))
        self.assertEqual((first_generation/'index.sqlite').read_bytes(),first_index)
        archived=root/'journal-archive'/('replacement-'+hashlib.sha256(
            before_restart).hexdigest()+'.json')
        self.assertEqual(archived.read_bytes(),before_restart)
        self.assert_legacy_preserved(old)

    def test_legacy_replacement_empty_checkpoint_fails_closed_then_restarts(self):
        old=self.legacy_selected_store();root=qmd_shared_store.root()
        options=self.accepted_replacement_options(self.replacement_options())
        (self.home/'fail-embed').touch()
        code,failed=self.cli('prepare-replacement-store',options)
        self.assertEqual(code,1,failed)
        (self.home/'fail-embed').unlink()
        journal_path=root/'replacement-bootstrap-journal.json'
        journal=json.loads(journal_path.read_text())
        first_generation=Path(journal['building']['generation'])
        first_index=(first_generation/'index.sqlite').read_bytes()
        journal['building']={}
        journal['buildingSha256']=shared_qmd_setup.coordinator.digest({})
        journal_path.write_text(json.dumps(journal,sort_keys=True)+'\n')
        journal_path.chmod(0o600)
        code,rejected=self.cli('prepare-replacement-store',options)
        self.assertEqual(code,1,rejected)
        self.assertEqual(rejected['status'],'failed_preparing')
        self.assertIn('bootstrap_checkpoint_changed',rejected['reason'])
        self.assertEqual((first_generation/'index.sqlite').read_bytes(),first_index)
        before_restart=journal_path.read_bytes()
        code,restarted=self.cli('prepare-replacement-store',options+[
            '--replace-inactive-store-preparation'])
        self.assertEqual(code,0,restarted)
        self.assertEqual(restarted['status'],'prepared_inactive_replacement')
        self.assertIs(restarted['cutoverReady'],False)
        self.assertNotEqual(restarted['generation'],str(first_generation))
        self.assertEqual((first_generation/'index.sqlite').read_bytes(),first_index)
        archived=root/'journal-archive'/('replacement-'+hashlib.sha256(
            before_restart).hexdigest()+'.json')
        self.assertEqual(archived.read_bytes(),before_restart)
        self.assert_legacy_preserved(old)

    def test_inspect_without_store_is_read_only(self):
        result=setup_entry.discover(self.project)
        self.assertEqual(result['discovered']['sharedStoreStatus'],'not_prepared')
        self.assertEqual(result['discovered']['pythonExecutable'],sys.executable)
        self.assertIs(result['discovered']['sqliteExtensionLoading'],
                      setup_entry.sqlite_extension_loading_available())
        self.assertFalse(qmd_runtime.managed_root().parent.parent.exists())
        update=setup_entry.check_update(self.project)
        self.assertIn('plugin_qmd_store_not_ready',update['reasons'])
        self.assertFalse(qmd_runtime.managed_root().parent.parent.exists())

    def test_sqlite_extension_capability_probe_is_read_only(self):
        class Unavailable: pass
        class Available:
            def enable_load_extension(self, enabled): pass
            def load_extension(self, path): pass
        self.assertFalse(setup_entry.sqlite_extension_loading_available(Unavailable))
        self.assertTrue(setup_entry.sqlite_extension_loading_available(Available))
        self.assert_preserved()

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

"""Disposable Stop CUD -> fake Codex -> fake QMD -> worker lineage."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, 'core')
import qmd_route
import setup_guard
import topical_hook_queue as queue
import topical_story_scope as scope
import wiki_compile_worker as model_worker
import wiki_topical_backend as backend
import wiki_topical_experiment as topical_experiment
import wiki_topical_reconcile as reconcile
import wiki_topical_similarity as similarity
from context_learning import synthetic_experiment as experiment
from context_learning import synthetic_scope, topical_worker_provenance as provenance
from context_learning.shared import _db


def sha(raw): return hashlib.sha256(raw).hexdigest()


def put(path, raw, mode=0o600):
    path.parent.mkdir(parents=True,exist_ok=True)
    if isinstance(raw,dict): raw=json.dumps(raw,sort_keys=True).encode()
    path.write_bytes(raw); path.chmod(mode)
    return sha(raw)


with tempfile.TemporaryDirectory(prefix='qmd-story-auto-') as temp:
    project=Path(temp).resolve()/'novel'; project.mkdir(mode=0o700)
    story=project/'planned-story'; story.mkdir(mode=0o700)
    shared=project/'shared'; shared.mkdir(mode=0o700)
    wiki=story/'.auto-context/wiki'; wiki.mkdir(parents=True,mode=0o700)
    for name in scope.SOURCES: (story/name).mkdir(mode=0o700)
    put(story/'.qmd-topical-v2-project',b'synthetic story only\n')
    settings_sha=put(project/'.auto-context/settings.json',b'{}\n',0o644)
    teacher_sha=put(shared/'synthetic-teacher-policy.json',b'{}\n')
    auto={'schema':'qmd-topical-auto-refresh-v1','enabled':True,
          'sourceRoots':scope.SOURCES,'engine':'codex',
          'compileCfg':{'extractor':{'builtins':['codex']},
                        'verify':{'builtins':['codex'],'crossEngine':'off'}},
          'maxEstimatedCents':160}
    auto_sha=put(story/scope.AUTO_POLICY,auto)
    put(story/scope.POLICY,{'schema':scope.SCHEMA,'enabled':True,
        'storyRoot':str(story),'parentRoot':str(project),
        'parentSettingsSha256':settings_sha,
        'syntheticTeacherPolicySha256':teacher_sha,
        'autoRefreshPolicySha256':auto_sha,'sourceRoots':scope.SOURCES,
        'wikiCollection':scope.WIKI_COLLECTION,'engine':'codex',
        'transport':'codex-cli-subscription'})
    qmd_config=project/'qmd-config'; qmd_config.mkdir()
    (qmd_config/'index.yml').write_text('collections: {}\n')
    index_path=project/'qmd-db/index.sqlite'; index_path.parent.mkdir()
    with sqlite3.connect(index_path) as index:
        index.execute('CREATE TABLE documents(collection TEXT,path TEXT,hash TEXT,active INTEGER, '
                      'PRIMARY KEY(collection,path))')
        index.execute('CREATE TABLE content_vectors(hash TEXT PRIMARY KEY)')
    route={'selected':True,'INDEX_PATH':str(index_path),
           'QMD_CONFIG_DIR':str(qmd_config),'XDG_CACHE_HOME':str(project/'qmd-cache')}
    fake_qmd=(Path.cwd()/'test_support/topical-story-fake-qmd.py').resolve()
    source=story/'01_Settings/probe.md'
    def raw_ack():
        with sqlite3.connect(index_path) as index:
            if source.is_file():
                index.execute('INSERT OR REPLACE INTO documents VALUES (?,?,?,1)',
                              (scope.SOURCE_COLLECTIONS['01_Settings'],'probe.md',
                               sha(source.read_bytes())))
            else:
                index.execute('UPDATE documents SET active=0 WHERE collection=? AND path=?',
                              (scope.SOURCE_COLLECTIONS['01_Settings'],'probe.md'))
    calls=[]
    def fake_codex(argv,payload,timeout,cwd):
        task=payload['task']; calls.append(task)
        if task=='generate_topical_candidates_sandbox_only':
            info=payload['sources'][0]
            line=(story/info['path']).read_text().splitlines()[0]
            return {'schema':payload['outputSchema'],'cards':[{
                'cardId':'scoped-rule','title':'Scoped rule','category':'world-rule',
                'details':'','claims':[{'claimId':'rule','statement':line,
                'state':'rule','timeScope':'current','condition':'',
                'evidence':[{'sourcePath':info['path'],'startLine':1,
                             'endLine':1,'quoteAnchor':line}]}]}],
                    '_qmd':{'reasoningEffort':'medium'}},None,0
        if task=='verify_topical_claims_sandbox_only':
            checks=[{'claimId':claim['claimId'],'sourcePath':span['sourcePath'],
                     'quoteSha256':span['quoteSha256'],'quoteAnchor':span['quoteAnchor'],
                     'supported':True} for claim in payload['card']['claims']
                    for span in claim['evidence']]
            return {'verdict':'pass','checks':checks,'reasons':[],
                    '_qmd':{'reasoningEffort':'medium'}},None,0
        raise AssertionError('unexpected_provider_task')
    def stop(number):
        result=queue._run(story,{'action':'boundary','turnKey':'codex:synthetic-'+str(number)})
        assert result and result.get('status') in ('backend_created_synced',
            'backend_verified_synced','backend_batch_synced'),result
        return result
    old_qmd=os.environ.pop('QMD_BIN',None)
    old_writer=os.environ.get('QMD_WRITER_LOCKDIR')
    os.environ['QMD_TOPICAL_STORY_TEST_ROOT']=str(project)
    os.environ['QMD_SETUP_GUARD_FIXTURE']='1'
    os.environ['QMD_WRITER_LOCKDIR']=str(project/'qmd-writer.lock.d')
    try:
        with patch.object(synthetic_scope,'NOVEL_ROOT',project), \
             patch.object(synthetic_scope,'policy_for',return_value=object()), \
             patch.object(experiment,'story_for',return_value=story), \
             patch.object(experiment,'shared_for',return_value=shared), \
             patch.object(provenance,'NOVEL_ROOT',project), \
             patch.object(provenance,'story_for',return_value=story), \
             patch.object(provenance,'shared_for',return_value=shared), \
             patch.object(qmd_route,'project_paths',return_value=route), \
             patch.object(qmd_route,'binary_info',return_value={'QMD_BIN':str(fake_qmd)}), \
             patch.object(model_worker,'run_extractor',side_effect=fake_codex) as provider, \
             patch.object(similarity,'retrieve',return_value=[]):
            argv=model_worker.resolve_extractor_argv(auto['compileCfg'],'codex')
            adapter_copy=project/'adapter-copy'; adapter_copy.mkdir()
            for name in ('codex_adapter.py','lib.py'):
                (adapter_copy/name).write_bytes(Path(argv[1]).with_name(name).read_bytes())
            relocated=[argv[0],str(adapter_copy/'codex_adapter.py')]
            assert backend.transport_fingerprint(story,argv)==backend.transport_fingerprint(story,relocated)
            source.write_text('The amber bell marks dawn.\n')
            raw_ack()
            created=stop(1)
            pages=list(wiki.glob('topical-v2/*/scoped-rule.md'))
            assert len(pages)==1 and created['workerProvenance']['deferred']==[]
            context = {'path':pages[0].relative_to(wiki).as_posix(),
                       'lead':topical_experiment.load_staged_card(story,pages[0].parent.name,
                                                          'scoped-rule')['lead'][:600]}
            scope.validate_wiki_candidates(story,[context])
            try: scope.validate_wiki_candidates(story,[{**context,'lead':'foreign'}])
            except ValueError as exc: assert str(exc) == 'topical_story_candidate_unscoped'
            else: raise AssertionError('altered_journal_lead_accepted')
            candidate = scope.WIKI_COLLECTION+'/'+pages[0].relative_to(wiki).as_posix()
            attempt = next((story/'topical-backend-audit').glob('generation.*.attempt.json'))
            attempt_bytes = attempt.read_bytes()
            tampered = json.loads(attempt_bytes)
            tampered['transportSha256'] = '0'*64
            attempt.write_text(json.dumps(tampered,sort_keys=True)+'\n')
            with _db(shared) as db:
                assert not provenance.registered_revision(db,shared,project,candidate,
                                                          sha(pages[0].read_bytes()))
            attempt.write_bytes(attempt_bytes)
            response_audit = next((story/'topical-backend-audit').glob('*.generation.json'))
            response_bytes = response_audit.read_bytes()
            response_audit.unlink()
            with _db(shared) as db:
                assert provenance.registered_revision(db,shared,project,candidate,
                                                      sha(pages[0].read_bytes()))
            response_audit.write_bytes(response_bytes)
            response_audit.chmod(0o600)
            verify_attempt = next((story/'topical-backend-audit').glob('verification.*.attempt.json'))
            verify_bytes = verify_attempt.read_bytes()
            verify_tamper = json.loads(verify_bytes)
            verify_tamper['adapterResponse']['_qmd']['reasoningEffort'] = 'unreviewed'
            verify_attempt.write_text(json.dumps(verify_tamper,sort_keys=True)+'\n')
            with _db(shared) as db:
                assert not provenance.registered_revision(db,shared,project,candidate,
                                                          sha(pages[0].read_bytes()))
            verify_attempt.write_bytes(verify_bytes)
            with _db(shared) as db:
                assert db.execute("SELECT COUNT(*) FROM topical_worker_versions WHERE state='active'").fetchone()[0]==1
            source.write_text('The amber bell marks dusk.\n')
            raw_ack()
            updated=stop(2)
            pages=list(wiki.glob('topical-v2/*/scoped-rule.md'))
            assert len(pages)==1 and 'dusk' in pages[0].read_text()
            review_proof=(story/'topical-create-review-proofs'/pages[0].parent.name/'scoped-rule.json')
            assert review_proof.is_file()
            assert json.loads(review_proof.read_text())['pairs']==[]
            assert updated['workerProvenance']['retired'] in (0,1)
            with _db(shared) as db:
                assert db.execute("SELECT COUNT(*) FROM topical_worker_versions WHERE state='active'").fetchone()[0]==1
            source.unlink(); raw_ack()
            deleted=stop(3)
            assert not list(wiki.glob('topical-v2/*/scoped-rule.md'))
            with _db(shared) as db:
                assert db.execute("SELECT COUNT(*) FROM topical_worker_versions WHERE state='active'").fetchone()[0]==0
            assert not deleted['workerProvenance']['deferred']
            source.write_text('The amber bell marks noon.\n'); raw_ack()
            recovered=queue._run(story,{'action':'reconcile','turnKey':'recovery'})
            assert recovered and recovered.get('status') in ('backend_created_synced',
                'backend_batch_synced'),recovered
            assert len(list(wiki.glob('topical-v2/*/scoped-rule.md')))==1
            with _db(shared) as db:
                assert db.execute("SELECT COUNT(*) FROM topical_worker_versions WHERE state='active'").fetchone()[0]==1
            assert sha((project/'.auto-context/settings.json').read_bytes())==settings_sha
            assert calls.count('generate_topical_candidates_sandbox_only')==3
            assert calls.count('verify_topical_claims_sandbox_only')==3
    finally:
        if old_qmd is not None: os.environ['QMD_BIN']=old_qmd
        if old_writer is None: os.environ.pop('QMD_WRITER_LOCKDIR',None)
        else: os.environ['QMD_WRITER_LOCKDIR']=old_writer
        os.environ.pop('QMD_TOPICAL_STORY_TEST_ROOT',None)
        os.environ.pop('QMD_SETUP_GUARD_FIXTURE',None)

print(json.dumps({'create':True,'update':True,'delete':True,'sessionRecovery':True,
                  'fakeCodexCalls':len(calls),'externalProviderCalls':0,
                  'indexedAndRegistered':True}))

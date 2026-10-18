"""Actual isolated QMD vector retrieval; no semantic teacher or live source."""
import hashlib
import json
import os

if os.environ.get('QMD_REAL_MODEL_TESTS') != '1':
    print(json.dumps({'skipped': 'real model tests require explicit QMD_REAL_MODEL_TESTS=1'}))
    raise SystemExit(0)
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, 'core')
import wiki_topical as topical
import wiki_topical_backend as backend
import wiki_topical_experiment as experiment
import wiki_topical_publish as publisher
import wiki_topical_reconcile as reconcile
import wiki_topical_refresh as refresh
import wiki_topical_similarity as similarity

snap = Path('/Users/dulee/work/laya-search-experiments/snapshot-20261003-165529')
qmd = Path(shutil.which('qmd') or '/Users/dulee/work/.qmd-tools/bin/qmd')
if not qmd.is_file() or not (snap/'models').is_dir() or not (snap/'offline.cjs').is_file():
    print(json.dumps({'skipped':'isolated offline QMD runtime unavailable'}))
    raise SystemExit(0)

with tempfile.TemporaryDirectory(prefix='qmd-similarity-e2e-') as name:
    root = Path(name).resolve()
    (root/topical.MARKER).write_text('synthetic only\n')
    sources = root/'sources'; sources.mkdir()
    wiki = root/'.auto-context/wiki'; wiki.mkdir(parents=True)
    collection = 'fixture-similar-wiki'
    (root/'.auto-context/settings.json').write_text(json.dumps({
        'indexing':True,'collections':[collection],
        'collectionPaths':{collection:'.auto-context/wiki'},
        'collectionRoles':{collection:'wiki'},'recallStrategy':'wikiOnly'}))
    facts = {'a.md':'The amber bell marks dawn.',
             'b.md':'The silver bell marks dusk.',
             'c.md':'The silver bell marks dusk.'}
    for name, fact in facts.items(): (sources/name).write_text(fact+'\n')
    def claim(name, state='rule', time='chapter-1'):
        quote=facts[name]
        revision=topical.source_snapshot(root,'sources/'+name,{})[0]
        return {'claimId':name[:-3]+'-claim','statement':quote,'state':state,
                'timeScope':time,'condition':'',
                'evidence':[{'sourcePath':'sources/'+name,
                    'sourceRevisionSha256':revision['sha256'],
                    'startLine':1,'endLine':1,'quoteAnchor':quote,
                    'quoteSha256':topical.digest(quote.encode())}]}
    def card(card_id, names, state='rule', time='chapter-1'):
        return {'cardId':card_id,'title':'Silver bell','category':'world-rule',
                'details':'','claims':[claim(n,state,time) for n in names]}
    old_stage=backend.stage_generation_response(root, {'schema':topical.SCHEMA,
        'cards':[card('silver-bell',['a.md','b.md'])]})
    near_stage=backend.stage_generation_response(root, {'schema':topical.SCHEMA,
        'cards':[card('silver-bell-plan',['c.md'],'plan','chapter-2')]})
    old_id=old_stage['generationId']; near_id=near_stage['generationId']
    old=experiment.load_staged_card(root,old_id,'silver-bell')
    near=experiment.load_staged_card(root,near_id,'silver-bell-plan')
    cfg={'extractor':{'builtins':['codex']},
         'verify':{'builtins':['codex'],'crossEngine':'off'}}
    calls=[]
    def verify_runner(argv,payload,timeout,cwd):
        calls.append('verify')
        checks=[{'claimId':c['claimId'],'sourcePath':e['sourcePath'],
                 'quoteSha256':e['quoteSha256'],'quoteAnchor':e['quoteAnchor'],
                 'supported':True}
                for c in payload['card']['claims'] for e in c['evidence']]
        return {'verdict':'pass','checks':checks,'reasons':[]},None,0
    for gid,cid in ((old_id,'silver-bell'),(near_id,'silver-bell-plan')):
        assert backend.run_verification_backend(root,gid,cid,cfg,'codex',
            allow_backend_execution=True,runner=verify_runner)['status']=='backend_pass'
    publisher.publish(root,old_id,'silver-bell')
    copy_stage=backend.stage_generation_response(root,{'schema':topical.SCHEMA,
        'cards':[card('silver-bell-copy',['a.md','b.md'])]})
    copy_id=copy_stage['generationId']
    assert backend.run_verification_backend(root,copy_id,'silver-bell-copy',cfg,'codex',
        allow_backend_execution=True,runner=verify_runner)['status']=='backend_pass'
    try:
        publisher.publish(root,copy_id,'silver-bell-copy')
    except topical.TopicalError as error:
        assert error.code=='similarity_unresolved'
    else:
        raise AssertionError('direct_duplicate_publish_was_allowed')
    try:
        publisher.publish(root,near_id,'silver-bell-plan')
    except topical.TopicalError as error:
        assert error.code=='similarity_unresolved'
    else:
        raise AssertionError('direct_publish_bypassed_similarity')
    first_pair=similarity.evaluate(root,near,near_id,[{'path':f'topical-v2/{old_id}/silver-bell.md',
        'pageSha256':hashlib.sha256(topical.read_generation_bytes(root,old_id,
            'cards/silver-bell.md')).hexdigest(),'card':old}])['pairs'][0]['pairSha256']
    early_review=root/'topical-similarity-review.json'
    early_review.write_text(json.dumps({'schema':'qmd-topical-similarity-review-v1',
        'decisions':[{'pairSha256':first_pair,'verdict':'distinct',
            'reviewerId':'synthetic-human-reviewer',
            'reason':'Plan in chapter 2 differs from rule in chapter 1.'}]}))
    early_review.chmod(0o600)
    publisher.publish(root,near_id,'silver-bell-plan')
    config=root/'qmd-config/index.yml';config.parent.mkdir()
    config.write_text('collections: {}\nmodels:\n'
        '  embed: hf:ggml-org/embeddinggemma-300M-GGUF/embeddinggemma-300M-Q8_0.gguf\n'
        '  generate: hf:tobil/qmd-query-expansion-1.7B-gguf/qmd-query-expansion-1.7B-q4_k_m.gguf\n'
        '  rerank: hf:ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF/qwen3-reranker-0.6b-q8_0.gguf\n')
    (root/'qmd-cache/qmd').mkdir(parents=True)
    (root/'qmd-cache/qmd/models').symlink_to(snap/'models',target_is_directory=True)
    (root/'qmd-db').mkdir()
    os.environ.update(QMD_TOPICAL_SYNTHETIC_RUNTIME='1',
        QMD_BIN=str(qmd),INDEX_PATH=str(root/'qmd-db/index.sqlite'),
        QMD_CONFIG_DIR=str(config.parent),XDG_CACHE_HOME=str(root/'qmd-cache'),
        NODE_OPTIONS='--require='+str(snap/'offline.cjs'),
        HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1')
    assert publisher.sync(root)['verifiedCards']==2
    initial=similarity.retrieve(root,old['lead'],exclude=(old_id,'silver-bell'))
    assert [(x['generationId'],x['cardId']) for x in initial]==[(near_id,'silver-bell-plan')]
    legacy=wiki/'legacy-card.md'
    legacy.write_text('# Legacy card\nUnrelated archival note.\n')
    legacy_db=root/'legacy-index.sqlite'
    with sqlite3.connect(legacy_db) as db:
        db.execute('CREATE TABLE documents(collection TEXT, path TEXT, hash TEXT, active INTEGER)')
        db.execute('INSERT INTO documents VALUES(?,?,?,1)',
            (collection, legacy.name, hashlib.sha256(legacy.read_bytes()).hexdigest()))
    qmd_call=publisher._call
    qmd_runtime=publisher._qmd_runtime
    try:
        publisher._qmd_runtime=lambda *args,**kwargs: {'INDEX_PATH':str(legacy_db)}
        publisher._call=lambda *args,**kwargs: SimpleNamespace(stdout=json.dumps([
            {'file':'qmd://'+collection+'/legacy-card.md'}]))
        legacy_hit=similarity.retrieve(root,old['lead'])
        legacy_pair=similarity.evaluate(root,near,near_id,legacy_hit)
        assert legacy_pair['status']=='pending_review'
        assert legacy_pair['pairs'][0]['relation']=='legacy_wiki_review_required'
        assert legacy_pair['pairs'][0]['candidatePath']=='legacy-card.md'
    finally:
        publisher._call=qmd_call
        publisher._qmd_runtime=qmd_runtime
        legacy.unlink()
    reconcile.reconcile(root,['sources'],[old,near],
        trusted_card_ids=['silver-bell','silver-bell-plan'])
    (sources/'a.md').unlink()
    def generate_runner(argv,payload,timeout,cwd):
        calls.append('generate')
        assert payload['existingWikiCandidates']==[{
            'path':f'topical-v2/{near_id}/silver-bell-plan.md','lead':near['lead']}]
        return {'schema':topical.SCHEMA,'cards':[card('silver-bell',['b.md'])]},None,0
    pending=refresh.refresh_one(root,['sources'],old,old_id,cfg,'codex',
        allow_backend_execution=True,generation_runner=generate_runner,
        verification_runner=verify_runner)
    assert pending['status']=='pending_review' and pending['reason']=='similarity_unresolved'
    pair=pending['pairs'][0]
    assert pair['relation']=='state_time_or_condition_difference'
    assert pair['verdict']=='unresolved'
    assert (wiki/'topical-v2'/old_id/'silver-bell.md').is_file()
    assert not (root/'topical-retired'/old_id/'silver-bell.md').exists()
    new_id=json.loads((root/'topical-refresh-state.json').read_text())['generationId']
    new=experiment.load_staged_card(root,new_id,'silver-bell')
    exact=similarity.evaluate(root,new,new_id,[{'pageSha256':
        hashlib.sha256(topical.read_generation_bytes(root,new_id,'cards/silver-bell.md')).hexdigest(),
        'path':'synthetic/exact.md','card':new}])
    assert exact['status']=='pending_review' and exact['pairs'][0]['verdict']=='exact_duplicate'
    calls_before=list(calls)
    review=root/'topical-similarity-review.json'
    review.write_text(json.dumps({'schema':'qmd-topical-similarity-review-v1',
        'decisions':[{'pairSha256':first_pair,'verdict':'distinct',
                      'reviewerId':'synthetic-human-reviewer',
                      'reason':'Plan in chapter 2 differs from rule in chapter 1.'},
                     {'pairSha256':pair['pairSha256'],'verdict':'distinct',
                      'reviewerId':'synthetic-human-reviewer',
                      'reason':'Same words but plan in chapter 2 differs from rule in chapter 1.'}]}))
    review.chmod(0o600)
    done=refresh.refresh_one(root,['sources'],old,old_id,cfg,'codex',
        allow_backend_execution=True,generation_runner=generate_runner,
        verification_runner=verify_runner)
    assert done['status']=='backend_verified_synced' and calls==calls_before
    assert (wiki/'topical-v2'/near_id/'silver-bell-plan.md').is_file()
    assert (wiki/'topical-v2'/new_id/'silver-bell.md').is_file()
    with sqlite3.connect(root/'qmd-db/index.sqlite') as db:
        assert db.execute('SELECT COUNT(*) FROM documents WHERE collection=? AND active=1',
                          (collection,)).fetchone()[0]==2
    print(json.dumps({'preAndPostVectorRetrieval':True,'pendingWithoutTeacher':True,
        'planActualTimePreserved':True,'exactDuplicateNeverAutoMerged':True,
        'reviewResumeNoBackendRepeat':True,'externalTeacherCalls':0}))

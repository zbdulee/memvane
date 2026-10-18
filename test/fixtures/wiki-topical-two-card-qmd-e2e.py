"""Two changed cards finish independently through real isolated QMD/SQLite."""
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

sys.path.insert(0,'core')
import wiki_topical as topical
import wiki_topical_backend as backend
import wiki_topical_experiment as experiment
import wiki_topical_publish as publisher
import wiki_topical_reconcile as reconcile
import wiki_topical_refresh as refresh
import wiki_topical_similarity as similarity

snap=Path('/Users/dulee/work/laya-search-experiments/snapshot-20261003-165529')
qmd=Path(shutil.which('qmd') or '/Users/dulee/work/.qmd-tools/bin/qmd')
if not qmd.is_file() or not (snap/'models').is_dir() or not (snap/'offline.cjs').is_file():
    print(json.dumps({'skipped':'isolated offline QMD unavailable'}));raise SystemExit(0)
with tempfile.TemporaryDirectory(prefix='qmd-two-card-real-') as name:
    root=Path(name).resolve();(root/topical.MARKER).write_text('synthetic only\n')
    sources=root/'sources';sources.mkdir()
    wiki=root/'.auto-context/wiki';wiki.mkdir(parents=True)
    collection='fixture-two-card-wiki'
    (root/'.auto-context/settings.json').write_text(json.dumps({'indexing':True,
        'collections':[collection],'collectionPaths':{collection:'.auto-context/wiki'},
        'collectionRoles':{collection:'wiki'},'recallStrategy':'wikiOnly'}))
    facts={'a.md':'Amber dawn.','b.md':'Silver dusk.'}
    for source,fact in facts.items():(sources/source).write_text(fact+'\n')
    def card(cid,source):
        quote=(sources/source).read_text().strip()
        revision=topical.source_snapshot(root,'sources/'+source,{})[0]
        return {'cardId':cid,'title':cid,'category':'world-rule','details':'',
            'claims':[{'claimId':'fact','statement':quote,'state':'rule',
            'timeScope':'chapter-1','condition':'','evidence':[{'sourcePath':'sources/'+source,
            'sourceRevisionSha256':revision['sha256'],'startLine':1,'endLine':1,
            'quoteAnchor':quote,'quoteSha256':topical.digest(quote.encode())}]}]}
    def stage(cid,source):
        record=backend.stage_generation_response(root,{'schema':topical.SCHEMA,
            'cards':[card(cid,source)]})
        gid=record['generationId']
        return gid,experiment.load_staged_card(root,gid,cid)
    old_a,card_a=stage('amber','a.md');old_b,card_b=stage('silver','b.md')
    cfg={'extractor':{'builtins':['codex']},
         'verify':{'builtins':['codex'],'crossEngine':'off'}}
    calls=[]
    def verify(argv,payload,timeout,cwd):
        calls.append('verify')
        checks=[{'claimId':c['claimId'],'sourcePath':span['sourcePath'],
            'quoteSha256':span['quoteSha256'],'quoteAnchor':span['quoteAnchor'],
            'supported':True} for c in payload['card']['claims'] for span in c['evidence']]
        return {'verdict':'pass','checks':checks,'reasons':[]},None,0
    for gid,cid in ((old_a,'amber'),(old_b,'silver')):
        assert backend.run_verification_backend(root,gid,cid,cfg,'codex',
            allow_backend_execution=True,runner=verify)['status']=='backend_pass'
    publisher.publish(root,old_a,'amber')
    def review_pair(proposed,gid,existing,other_gid,other_id):
        page=topical.read_generation_bytes(root,other_gid,f'cards/{other_id}.md')
        pair=similarity.evaluate(root,proposed,gid,[{'path':f'topical-v2/{other_gid}/{other_id}.md',
            'pageSha256':hashlib.sha256(page).hexdigest(),'card':existing}])['pairs'][0]
        path=root/'topical-similarity-review.json'
        document=json.loads(path.read_text()) if path.exists() else {
            'schema':'qmd-topical-similarity-review-v1','decisions':[]}
        document['decisions'].append({'pairSha256':pair['pairSha256'],
            'verdict':'distinct','reviewerId':'synthetic-reviewer',
            'reason':'Independent amber and silver synthetic rules.'})
        path.write_text(json.dumps(document));path.chmod(0o600)
    review_pair(card_b,old_b,card_a,old_a,'amber')
    publisher.publish(root,old_b,'silver')
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
        NODE_OPTIONS='--require='+str(snap/'offline.cjs'),HF_HUB_OFFLINE='1',
        TRANSFORMERS_OFFLINE='1')
    assert publisher.sync(root)['verifiedCards']==2
    reconcile.reconcile(root,['sources'],[card_a,card_b],trusted_card_ids=['amber','silver'])
    (sources/'a.md').write_text('Amber noon.\n')
    (sources/'b.md').write_text('Silver midnight.\n')
    def generate(cid,source):
        def run(argv,payload,timeout,cwd):
            calls.append('generate-'+cid)
            return {'schema':topical.SCHEMA,'cards':[card(cid,source)]},None,0
        return run
    first=refresh.refresh_one(root,['sources'],card_a,old_a,cfg,'codex',
        allow_backend_execution=True,generation_runner=generate('amber','a.md'),
        verification_runner=verify)
    assert first['status']=='backend_verified_synced' and first['finished']['pending']==1
    state=reconcile._read(root)
    assert set(state['queue'])=={'sources/b.md'} and set(state['projection'])=={'amber','silver'}, (state['queue'],state['projection'])
    assert state['projection']['silver']['state']=='excluded_stale'
    new_a=first['generationId']
    second=refresh.refresh_one(root,['sources'],card_b,old_b,cfg,'codex',
        allow_backend_execution=True,generation_runner=generate('silver','b.md'),
        verification_runner=verify)
    assert second['status']=='pending_review' and second['reason']=='similarity_unresolved'
    new_b=json.loads((root/'topical-refresh-state.json').read_text())['generationId']
    replacement_b=experiment.load_staged_card(root,new_b,'silver')
    replacement_a=experiment.load_staged_card(root,new_a,'amber')
    review_pair(replacement_b,new_b,replacement_a,new_a,'amber')
    done=refresh.refresh_one(root,['sources'],card_b,old_b,cfg,'codex',
        allow_backend_execution=True,generation_runner=generate('silver','b.md'),
        verification_runner=verify)
    assert done['status']=='backend_verified_synced' and done['finished']['pending']==0
    with sqlite3.connect(root/'qmd-db/index.sqlite') as db:
        active={row[0] for row in db.execute('SELECT path FROM documents WHERE collection=? AND active=1',(collection,))}
    assert active=={f'topical-v2/{new_a}/amber.md',f'topical-v2/{new_b}/silver.md'}
    assert calls.count('generate-amber')==1 and calls.count('generate-silver')==1
    print(json.dumps({'independentQueuePreserved':True,'bothCardsEventuallyReady':True,
        'oldQmdPagesInactive':True,'externalCalls':0}))

"""Uncertain transport is never silently retried; refresh lock is exclusive."""
import fcntl
import json
from pathlib import Path
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, 'core')
import wiki_topical as topical
import wiki_topical_backend as backend
import wiki_topical_experiment as experiment
import wiki_topical_reconcile as reconcile
import wiki_topical_refresh as refresh
import wiki_topical_similarity as similarity

with tempfile.TemporaryDirectory(prefix='qmd-refresh-recovery-') as name:
    root=Path(name).resolve()
    (root/topical.MARKER).write_text('synthetic only\n')
    sources=root/'sources';sources.mkdir()
    for name, body in [('a.md','Amber dawn.'),('b.md','Silver dusk.')]:
        (sources/name).write_text(body+'\n')
    def claim(name):
        quote=(sources/name).read_text().strip()
        revision=topical.source_snapshot(root,'sources/'+name,{})[0]
        return {'claimId':name[:-3]+'-claim','statement':quote,'state':'rule',
            'timeScope':'chapter-1','condition':'','evidence':[{
                'sourcePath':'sources/'+name,'sourceRevisionSha256':revision['sha256'],
                'startLine':1,'endLine':1,'quoteAnchor':quote,
                'quoteSha256':topical.digest(quote.encode())}]}
    old_stage=backend.stage_generation_response(root,{'schema':topical.SCHEMA,
        'cards':[{'cardId':'two-bells','title':'Two bells','category':'world-rule',
                  'details':'','claims':[claim('a.md'),claim('b.md')]}]})
    old=experiment.load_staged_card(root,old_stage['generationId'],'two-bells')
    wiki=root/'.auto-context/wiki';wiki.mkdir(parents=True)
    (root/'.auto-context/settings.json').write_text(json.dumps({
        'indexing':True,'collections':['fixture-recovery-wiki'],
        'collectionPaths':{'fixture-recovery-wiki':'.auto-context/wiki'},
        'collectionRoles':{'fixture-recovery-wiki':'wiki'},
        'recallStrategy':'wikiOnly'}))
    page=wiki/'topical-v2'/old_stage['generationId']/'two-bells.md'
    page.parent.mkdir(parents=True)
    page.write_bytes(topical.read_generation_bytes(root,old_stage['generationId'],
        'cards/two-bells.md'))
    reconcile.reconcile(root,['sources'],[old],trusted_card_ids=['two-bells'])
    (sources/'a.md').unlink()
    original_retrieve=similarity.retrieve
    similarity.retrieve=lambda *args,**kwargs: []
    cfg={'extractor':{'builtins':['codex']},
         'verify':{'builtins':['codex'],'crossEngine':'off'}}
    policy={'schema':'qmd-topical-auto-refresh-v1','enabled':True,
        'sourceRoots':['sources'],'engine':'codex','compileCfg':cfg,
        'maxEstimatedCents':20}
    policy_path=root/refresh.AUTO_CONFIG
    calls=[]
    def uncertain_runner(*args):
        calls.append('generation')
        raise RuntimeError('synthetic_transport_disconnected_after_send')
    try:
        lock_fd=os.open(root/'.topical-refresh.lock',os.O_RDWR|os.O_CREAT,0o600)
        fcntl.flock(lock_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            refresh.refresh_one(root,['sources'],old,old_stage['generationId'],cfg,'codex',
                allow_backend_execution=True,generation_runner=uncertain_runner,
                verification_runner=uncertain_runner)
        except topical.TopicalError as error:
            assert error.code=='refresh_busy'
        else:
            raise AssertionError('concurrent_refresh_not_blocked')
        os.close(lock_fd)
        try:
            refresh.refresh_one(root,['sources'],old,old_stage['generationId'],cfg,'codex',
                allow_backend_execution=True,generation_runner=uncertain_runner,
                verification_runner=uncertain_runner)
        except RuntimeError as error:
            assert str(error)=='synthetic_transport_disconnected_after_send'
        else:
            raise AssertionError('uncertain_transport_not_recorded')
        journal_path=root/refresh.JOURNAL
        saved_journal=journal_path.read_bytes()
        late=json.loads(saved_journal)
        late['phase']='new_published'
        journal_path.write_text(json.dumps(late)+'\n')
        with patch.object(refresh, 'refresh_one',
                          side_effect=AssertionError('policyless_recovery_ran')):
            assert refresh.recover_pending(root)=={'status':'pending_review',
                'reason':'auto_teacher_policy_required'}
        policy_path.write_text(json.dumps({**policy,'enabled':False}))
        policy_path.chmod(0o600)
        with patch.object(refresh, 'refresh_one',
                          side_effect=AssertionError('disabled_recovery_ran')):
            assert refresh.recover_pending(root)=={'status':'pending_review',
                'reason':'auto_teacher_policy_disabled'}
        journal_path.write_bytes(saved_journal)
        policy_path.write_text(json.dumps(policy))
        policy_path.chmod(0o600)
        automatic=refresh.recover_pending(root)
        assert automatic=={'status':'pending_review',
                          'reason':'generation_attempt_uncertain'}
        pending=refresh.refresh_one(root,['sources'],old,old_stage['generationId'],cfg,'codex',
            allow_backend_execution=True,generation_runner=uncertain_runner,
            verification_runner=uncertain_runner)
        assert pending=={'status':'pending_review','reason':'generation_attempt_uncertain'}
        assert calls==['generation']
        audits=list((root/'topical-backend-audit').glob('generation.*.attempt.json'))
        assert len(audits)==1 and json.loads(audits[0].read_text())['state']=='uncertain'
        assert not (root/'topical-retired').exists()
    finally:
        similarity.retrieve=original_retrieve
    print(json.dumps({'exclusiveRefreshLock':True,'uncertainCallNotRepeated':True,
                      'oldCardRetained':True,'externalTeacherCalls':0}))

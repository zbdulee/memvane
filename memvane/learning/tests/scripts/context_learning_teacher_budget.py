import json
from pathlib import Path
import tempfile

from memvane.learning.context_learning.contracts import request, digest
from memvane.learning.context_learning.offline import save_label, delete_case
from memvane.learning.context_learning.store import capture
from memvane.learning.context_learning.teacher_budget import budgeted_label_request, budgeted_review_request, reserve_teacher


def reject(fn):
    try: fn()
    except ValueError: return
    raise AssertionError('expected rejection')


with tempfile.TemporaryDirectory() as temp:
    state=Path(temp)/'state'; state.mkdir(mode=0o700)
    sample=request({'prompt':'synthetic request'},[{'candidate_id':'card',
        'revision_sha256':digest('card'),'eligible':True,'excerpt':'synthetic evidence'}],
        host='codex',request_id='synthetic-1')
    assert capture(sample,enabled=True,state_dir=state)=='stored'
    other=request({'prompt':'synthetic second request'},[{'candidate_id':'card2',
        'revision_sha256':digest('card2'),'eligible':True,'excerpt':'synthetic evidence'}],
        host='codex',request_id='synthetic-2')
    assert capture(other,enabled=True,state_dir=state)=='stored'
    label={'schema_version':2,'request_id':'synthetic-1','candidate_id':'card',
        'revision_sha256':digest('card'),'abstain':False,'relevance':'necessary',
        'evidence':'synthetic','rationale':{'kind':'support','text':'synthetic evidence',
            'span':{'start':0,'end':9},'scope':'provided-excerpt'}}
    policy={'enabled':True,'project_id':'synthetic-project','host':'codex',
        'allowed_request_ids':['synthetic-1','synthetic-2'],'max_calls':2,
        'max_input_bytes_per_call':10000,'max_total_input_bytes':20000,
        'max_reserved_usd':.02,'reserve_usd_per_call':.01,
        'creation_model':'gpt-6-luna','review_model':'gpt-6.1-sol'}
    calls=[]
    def creation(*args,**kwargs):
        calls.append(kwargs['model']); return {'status':'completed','cli_invocations':0}
    result=budgeted_label_request(state,'synthetic-1','create-1',policy,invoke=creation)
    assert result['status']=='completed' and calls==['gpt-6-luna']
    save_label(state,label)
    def review(*args,**kwargs):
        calls.append(kwargs['model']);return {'status':'completed','labels':[label]}
    second=budgeted_review_request(state,'synthetic-1','review-1',policy,invoke=review)
    assert second['second_opinion_only'] and second['comparisons'][0]['class_agrees']
    assert calls==['gpt-6-luna','gpt-6.1-sol']
    assert budgeted_label_request(state,'synthetic-1','create-1',policy,invoke=creation)['cached']
    reject(lambda:reserve_teacher(state,'synthetic-1','creation','create-1',policy))
    reject(lambda:reserve_teacher(state,'synthetic-1','creation','create-2',policy))
    reject(lambda:reserve_teacher(state,'synthetic-1','creation','create-2',dict(policy,max_calls=3)))
    delete_case(state,'synthetic-1')
    reject(lambda:reserve_teacher(state,'synthetic-2','creation','create-2',policy))
    assert calls==['gpt-6-luna','gpt-6.1-sol']
    print(json.dumps({'project_scope':'passed','creation_review_models':'passed',
        'duplicate_and_call_cost_budget':'passed','external_calls':0}))

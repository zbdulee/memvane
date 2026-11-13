import json
import os
from pathlib import Path
import sqlite3
import tempfile

from memvane.learning.context_learning.contracts import request, digest
from memvane.learning.context_learning.store import capture, canonical
from memvane.learning.context_learning.selection_gold import save_selection, load_selection
from memvane.learning.context_learning.offline import delete_case
from memvane.learning.context_learning.offline import save_label, build_manifest, review_digest, review_view
from memvane.learning.context_learning.selection_queue import build_queue
from memvane.learning.context_learning.compact_input import derive, MAX_COMPACT_BODY_BYTES
from memvane.learning.context_learning.seam import observe
from memvane.learning.context_learning.cycle_policy import selection_score, next_interval, promotion_decision


def reject(fn):
    try: fn()
    except ValueError: return
    raise AssertionError('expected rejection')


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / 'state'; root.mkdir(mode=0o700)
    sample = request({'prompt': 'synthetic prompt'}, [
        {'candidate_id': 'a', 'revision_sha256': digest('a'), 'eligible': True, 'excerpt': 'synthetic a'},
        {'candidate_id': 'b', 'revision_sha256': digest('b'), 'eligible': True, 'excerpt': 'synthetic b'}],
        host='codex', request_id='synthetic-1')
    assert capture(sample, enabled=True, state_dir=root) == 'stored'
    sha = digest(canonical(sample))
    reject(lambda: save_selection(root, 'synthetic-1', ['a'], input_sha256=digest('wrong'), reviewer_id='r', reference='source'))
    reject(lambda: save_selection(root, 'synthetic-1', ['c'], input_sha256=sha, reviewer_id='r', reference='source'))
    assert save_selection(root, 'synthetic-1', ['a', 'b'], input_sha256=sha, reviewer_id='r', reference='source') == 'reviewed'
    assert load_selection(root, 'synthetic-1')['selection']['selected_ids'] == ['a', 'b']
    reject(lambda: save_selection(root, 'synthetic-1', [], input_sha256=sha, reviewer_id='r', reference='source'))
    delete_case(root, 'synthetic-1')
    reject(lambda: load_selection(root, 'synthetic-1'))

    # Frontmatter alone exceeds the legacy 600-char excerpt. The exact compact
    # body (including its end) must still be the model input.
    compact_body = 'Body fact '+('z'*1000)+'\n'
    source = '---\ntitle: "'+('m'*690)+'"\nstatus: verified\n---\n'+compact_body
    revision = digest(source)
    parsed = derive(source, revision)
    assert parsed['body_text'] == compact_body and parsed['body_sha256'] == digest(compact_body)
    assert len(source[:600]) == 600 and 'Body fact' not in source[:600]
    approved_lead='Lead '+('y'*500)
    short_source='---\ntitle: "'+('m'*690)+'"\n---\n'+approved_lead
    assert len(approved_lead)<600 and derive(short_source,digest(short_source))['body_text']==approved_lead
    reject(lambda: derive('---\ntitle: x\n---\n'+'z'*(MAX_COMPACT_BODY_BYTES+1),
                          digest('---\ntitle: x\n---\n'+'z'*(MAX_COMPACT_BODY_BYTES+1))))
    compact_sample = request({'prompt': 'synthetic compact request'}, [
        {'candidate_id':'compact-card','revision_sha256':revision,'eligible':True,'excerpt':source}],
        host='codex',request_id='compact-1')
    assert compact_sample['candidates'][0]['excerpt']['truncated']
    assert capture(compact_sample,enabled=True,state_dir=root,
                   compact_sources={'compact-card':source}) == 'stored'
    label={'schema_version':2,'request_id':'compact-1','candidate_id':'compact-card',
        'revision_sha256':revision,'abstain':False,'relevance':'irrelevant','evidence':'',
        'rationale':{'kind':'absence','text':'No support in this synthetic case',
                     'span':None,'scope':'provided-excerpt'}}
    save_label(root,label,review={'reviewer_id':'synthetic-reviewer','reference':'synthetic',
        'evidence_sha256':review_digest(label),'input_sha256':digest(canonical(compact_sample))})
    save_selection(root,'compact-1',[],input_sha256=digest(canonical(compact_sample)),
                   reviewer_id='synthetic-reviewer',reference='synthetic')
    build_manifest(root,'compact-v1',{'compact-1':{'split':'train','task_family':'compact',
        'document_families':{'compact-card':'compact-source'}}},{'compact-card':revision})
    loaded=build_queue(root,'compact-v1',{'compact-card':revision})['splits']['train'][0]['candidates'][0]
    assert loaded['input_text']==compact_body and loaded['input_kind']=='full_compact_wiki_body'
    assert review_view(root,'compact-1','compact-card')['compact_input']['body_text']==compact_body
    oversized='---\ntitle: x\n---\n'+'z'*(MAX_COMPACT_BODY_BYTES+1)
    too_large=request({'prompt':'synthetic over-limit request'},[
        {'candidate_id':'oversized-card','revision_sha256':digest(oversized),
         'eligible':True,'excerpt':oversized}],host='codex',request_id='oversized-1')
    assert capture(too_large,enabled=True,state_dir=root)=='stored'
    bad_label=dict(label,request_id='oversized-1',candidate_id='oversized-card',
                   revision_sha256=digest(oversized))
    save_label(root,bad_label,review={'reviewer_id':'synthetic-reviewer','reference':'synthetic',
        'evidence_sha256':review_digest(bad_label),'input_sha256':digest(canonical(too_large))})
    save_selection(root,'oversized-1',[],input_sha256=digest(canonical(too_large)),
                   reviewer_id='synthetic-reviewer',reference='synthetic')
    build_manifest(root,'oversized-v1',{'oversized-1':{'split':'train','task_family':'oversized',
        'document_families':{'oversized-card':'oversized-source'}}},
        {'oversized-card':digest(oversized)})
    reject(lambda:build_queue(root,'oversized-v1',{'oversized-card':digest(oversized)}))
    project=Path(tmp).resolve()/'project'; wiki=project/'.auto-context'/'wiki';wiki.mkdir(parents=True)
    (wiki/'card.md').write_text(source)
    private=Path(tmp).resolve()/'capture-state';private.mkdir(mode=0o700)
    config={'collectionPaths':{'wiki':'.auto-context/wiki'},'collectionRoles':{'wiki':'wiki'},
            'contextLearning':{'capture':True,'stateRoot':str(private)}}
    observed=observe({'prompt':'Review synthetic compact card'},config,project,
        [{'file':'qmd://wiki/card.md'}])
    assert observed=='stored', observed
    database_path=private/digest(str(project.resolve()))/'learning.sqlite3'
    with sqlite3.connect(database_path) as db:
        saved=json.loads(db.execute('SELECT body FROM samples').fetchone()[0])
        full=json.loads(db.execute('SELECT body FROM compact_inputs').fetchone()[0])
    assert saved['candidates'][0]['excerpt']['text']==compact_body[:600]
    assert full['body_text']==compact_body

cases = [{'gold': [] if i < 10 else ['a'], 'selected': [] if i < 10 else ['a'], 'family': str(i % 5)} for i in range(100)]
score = selection_score(cases)
assert score['exact_rate'] == 1 and score['none_exact_rate'] == 1
assert next_interval(48, score)['hours'] == 96
perfect_fifty = selection_score(cases[:50])
assert perfect_fifty['wilson_lower'] < .95
assert next_interval(48, perfect_fifty)['hours'] == 96
near = selection_score([dict(case, selected=['wrong']) if i in (0, 1, 2) else case
                        for i, case in enumerate(cases[:50])])
near_cadence = next_interval(48, near)
assert near_cadence['reason'] == 'approaching_target' and 48 < near_cadence['hours'] < 96
assert next_interval(48, score, source_churn=True)['hours'] == 24
assert next_interval(48, selection_score(cases[:5]))['reason'] == 'insufficient_held_out_evidence'
inc = {'exact_rate': .7, 'necessary_recall': .8, 'irrelevant_injection_rate': .1, 'none_exact_rate': .8}
new = dict(inc, exact_rate=.8)
assert promotion_decision(inc, new, train_questions=100, validation_score=score)['promote']
assert not promotion_decision(inc, dict(new, necessary_recall=.7), train_questions=100, validation_score=score)['promote']
assert not promotion_decision(inc, new, train_questions=99, validation_score=score)['promote']
assert not promotion_decision(inc, new, train_questions=100, validation_score=score, stale=True)['promote']
print(json.dumps({'synthetic_selection_gold': 'passed', 'cadence': 'passed', 'promotion_gate': 'passed'}))

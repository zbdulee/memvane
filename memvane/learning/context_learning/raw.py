"""Pure pinned raw-data adapter. No Laya/torch/tokenizer imports or execution."""
import json
import math
from pathlib import Path

from . import dataset
from .contracts import CLASS_ORDER, digest
from .store import canonical, database

FORMAT = 'qmd-laya-raw-v1'
REPOSITORY = 'https://github.com/NandhaKishorM/laya'
RELEASE = '0.3.23'
COMMIT = 'd8a2e59781ca135169a36095056132e273cd9938'


def candidate_pin():
    return {'repository':REPOSITORY,'release':RELEASE,'commit':COMMIT,
            'identity':'assumed-candidate-not-user-confirmed'}


def tokenization_status():
    return {'status':'pending','tokenizer_revision':None,'checkpoint_revision':None,
            'token_budget':None,'truncation_validated':False,'ready_tokenized_training_data':False}


def _ordered(value):
    # Criteria and state insertion order are meaningful to Laya; never canonical-sort them.
    return json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False)


def questions():
    return {'relevance':{'type':'choice',
        'instructions':'Classify how the provided excerpt helps answer the prompt. Use only the provided excerpt, not external facts.',
        'criteria':{
            'necessary':'The excerpt supplies evidence required to answer the prompt.',
            'supporting':'The excerpt supplies useful additional evidence but is not required.',
            'irrelevant':'The excerpt does not help answer the prompt from the provided evidence.'}}}


def _raw_payloads(neutral,rows):
    qtext=_ordered(questions());qhash=digest(qtext)
    files={};sidecars=[];expected={}
    for split in sorted(rows):
        raw=[]
        for row in rows[split]:
            state=_ordered({'prompt':row['input']['prompt'],'excerpt':row['input']['excerpt']})
            name=CLASS_ORDER[row['target_class_index']]
            gold=_ordered({'relevance':{'probabilities':{c:1.0 if c==name else 0.0 for c in CLASS_ORDER}}})
            # These three JSON-string columns match the audited example's json.loads(row[field]).
            item={'state':state,'questions':qtext,'gold':gold}
            encoded=_ordered(item)
            sidecar={'case_hash':row['case_hash'],'split':split,'input_sha256':row['input_sha256'],
                'state_sha256':digest(state),'questions_sha256':qhash,'raw_row_sha256':digest(encoded),
                'target_class_name':name,'provenance':row['provenance']}
            raw.append(encoded+'\n');sidecars.append(sidecar)
            expected[row['case_hash']]=sidecar
        files[split+'.raw.jsonl']=''.join(raw)
    files['provenance.jsonl']=''.join(canonical(row)+'\n' for row in sidecars)
    if sum(len(v.encode('utf8')) for v in files.values())>dataset.MAX_BYTES:
        raise ValueError('raw_export_budget_exceeded')
    metadata={'schema_version':1,'format':FORMAT,'stage':'raw-preprocessing-input',
        'candidate':candidate_pin(),'neutral_export_sha256':neutral['export_sha256'],
        'manifest_sha256':neutral['manifest_sha256'],'counts':neutral['counts'],
        'class_order':list(CLASS_ORDER),'criteria_order':list(CLASS_ORDER),
        'questions_sha256':qhash,'columns':'state/questions/gold are ordered JSON strings',
        'state_hash_semantics':'raw state JSON-string before decoding; not tokenizer or encoded sequence identity',
        'target_semantics':'reviewed-hard-label-one-hot; not-calibrated-probabilities',
        'tokenization':tokenization_status(),'files_sha256':{k:digest(v) for k,v in sorted(files.items())}}
    metadata['laya_export_sha256']=digest(canonical(metadata))
    files['adapter.json']=canonical(metadata)+'\n'
    return metadata,files,expected


def _prepared(db,state_dir,version_id,revisions):
    manifest,checksum,rows=dataset._snapshot(db,version_id,revisions)
    neutral,neutral_files=dataset._payloads(manifest,checksum,rows)
    if not sum(neutral['counts'].values()):raise ValueError('empty_supervised_export')
    dataset._check_files(dataset._directory(state_dir,checksum),neutral_files)
    metadata,files,expected=_raw_payloads(neutral,rows)
    destination=Path(state_dir)/('laya-raw-'+metadata['laya_export_sha256'])
    return metadata,files,expected,destination


def export_laya_raw(state_dir,version_id,current_revisions):
    """Explicit raw transform of an already exported, currently valid neutral version."""
    with database(state_dir) as db:
        metadata,files,_,destination=_prepared(db,state_dir,version_id,current_revisions)
        dataset._write_export(state_dir,destination,files)
        return dict(metadata,directory=str(destination))


def check_laya_raw(state_dir,version_id,current_revisions):
    with database(state_dir) as db:
        metadata,files,_,destination=_prepared(db,state_dir,version_id,current_revisions)
        dataset._check_files(destination,files)
        return dict(metadata,directory=str(destination),check='raw-contract-and-provenance-only')


def _probability(value):
    if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=1:
        raise ValueError('invalid_laya_probability')
    return float(value)


def _answer_class(answer):
    required={'type','choice','probabilities','answer_confidence'}
    optional={'confidence','action','abstention','abstention_threshold','low_confidence'}
    if not isinstance(answer,dict) or not required<=set(answer) or not set(answer)<=required|optional or answer['type']!='choice':
        raise ValueError('invalid_laya_answer')
    name=answer['choice']
    if not isinstance(name,str) or name not in CLASS_ORDER:raise ValueError('unknown_laya_choice')
    probabilities=answer['probabilities']
    if not isinstance(probabilities,dict) or set(probabilities)!=set(CLASS_ORDER):
        raise ValueError('invalid_laya_probability_classes')
    probs={k:_probability(v) for k,v in probabilities.items()}
    # v0.3.23 reports probabilities rounded to four decimals.
    if abs(sum(probs.values())-1)>0.0002 or probs[name]+0.0001<max(probs.values()):
        raise ValueError('inconsistent_laya_choice')
    if 'confidence' in answer:_probability(answer['confidence'])
    if 'action' in answer:
        if not isinstance(answer['action'],dict) or set(answer['action'])!={'act_probability'}:
            raise ValueError('invalid_laya_action_metadata')
        _probability(answer['action']['act_probability'])  # Metadata only; never executes actions.
    gate=answer.get('abstention')
    if gate is not None and gate not in ('passed','abstained','unevaluated'):
        raise ValueError('invalid_laya_abstention')
    if 'low_confidence' in answer and type(answer['low_confidence']) is not bool:
        raise ValueError('invalid_laya_abstention')
    confidence=answer['answer_confidence']
    if confidence is None and gate=='unevaluated':
        pass
    else:
        confidence=_probability(confidence)
        if abs(confidence-max(probs.values()))>0.0002:
            raise ValueError('inconsistent_laya_confidence')
    if gate is None:
        if any(k in answer for k in ('abstention','abstention_threshold','low_confidence')):
            raise ValueError('incomplete_laya_abstention')
        return CLASS_ORDER.index(name),'not-requested'
    if 'abstention_threshold' not in answer:raise ValueError('incomplete_laya_abstention')
    threshold=_probability(answer['abstention_threshold'])
    if gate=='unevaluated':return None,gate
    is_low=confidence<threshold
    if (gate=='abstained')!=is_low or ('low_confidence' in answer and answer['low_confidence']!=is_low):
        raise ValueError('inconsistent_laya_abstention')
    return (None if is_low else CLASS_ORDER.index(name)),gate


def map_laya_predictions(payload,metadata,expected):
    """Map names, never vector positions; caller supplies typed answer subset and hashes."""
    fields={'schema_version','format','candidate','laya_export_sha256','split','model','predictions'}
    if not isinstance(payload,dict) or set(payload)!=fields or type(payload['schema_version']) is not int or payload['schema_version']!=1 or payload['format']!=FORMAT:
        raise ValueError('invalid_laya_prediction_contract')
    if not isinstance(metadata,dict):raise ValueError('invalid_laya_raw_metadata')
    if type(metadata.get('schema_version')) is not int or metadata['schema_version']!=1 or metadata.get('format')!=FORMAT or metadata.get('stage')!='raw-preprocessing-input' or metadata.get('class_order')!=list(CLASS_ORDER) or metadata.get('criteria_order')!=list(CLASS_ORDER) or metadata.get('questions_sha256')!=digest(_ordered(questions())) or metadata.get('tokenization')!=tokenization_status():
        raise ValueError('invalid_laya_raw_metadata')
    if payload['candidate']!=candidate_pin() or metadata['candidate']!=candidate_pin():
        raise ValueError('laya_candidate_version_mismatch')
    if payload['laya_export_sha256']!=metadata['laya_export_sha256']:
        raise ValueError('laya_export_mismatch')
    split=payload['split']
    if split not in ('validation','evaluation'):raise ValueError('held_out_split_required')
    wanted={k:v for k,v in expected.items() if v['split']==split}
    records=payload['predictions']
    if not isinstance(records,list) or len(records)!=len(wanted):raise ValueError('laya_prediction_membership_mismatch')
    seen=set();mapped=[];gates={'not-requested':0,'passed':0,'abstained':0,'unevaluated':0}
    for row in records:
        fields={'case_hash','input_sha256','state_sha256','questions_sha256','answer'}
        if not isinstance(row,dict) or set(row)!=fields:raise ValueError('invalid_laya_prediction_row')
        cid=row['case_hash']
        if not isinstance(cid,str) or cid in seen or cid not in wanted:raise ValueError('laya_prediction_membership_mismatch')
        seen.add(cid);pinned=wanted[cid]
        if any(row[k]!=pinned[k] for k in ('input_sha256','state_sha256','questions_sha256')):
            raise ValueError('laya_prediction_input_mismatch')
        index,gate=_answer_class(row['answer']);gates[gate]+=1
        mapped.append({'case_hash':cid,'input_sha256':row['input_sha256'],'class_index':index})
    return ({'schema_version':1,'export_sha256':metadata['neutral_export_sha256'],'split':split,
             'model':payload['model'],'predictions':mapped},gates)


def evaluate_laya_predictions(state_dir,version_id,current_revisions,payload,*,split='evaluation'):
    """Validate raw export and supplied answers, then reuse held-out neutral scorer."""
    with database(state_dir) as db:
        metadata,files,expected,destination=_prepared(db,state_dir,version_id,current_revisions)
        dataset._check_files(destination,files)
        mapped,gates=map_laya_predictions(payload,metadata,expected)
        report,neutral_files,train_files,train_sha=dataset._evaluate(db,version_id,current_revisions,mapped,split)
        neutral_destination=dataset._directory(state_dir,report['manifest_sha256'])
        dataset._check_files(neutral_destination,neutral_files)
        dataset._check_files(dataset._directory(state_dir,train_sha),train_files)
        report.pop('report_sha256')
        report['laya_adapter']={'format':FORMAT,'candidate':candidate_pin(),
            'laya_export_sha256':metadata['laya_export_sha256'],'raw_prediction_sha256':digest(canonical(payload)),
            'abstention_gates':gates,'tokenization':tokenization_status(),
            'probability_calibration':'not-verified; scoring supplied hard decisions only'}
        report['report_sha256']=digest(canonical(report))
        return dataset._save_report(neutral_destination,report)

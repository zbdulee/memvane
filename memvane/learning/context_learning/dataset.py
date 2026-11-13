"""Reviewed pair-classification exports and offline predictions; no model runtime."""
import json
import os
from pathlib import Path
import stat
import tempfile

from .contracts import CLASS_ORDER, digest, validate_label
from .offline import SPLITS, review_digest
from .store import canonical, database

MAX_BYTES = 16 * 1024 * 1024
MAX_CASES = 2000
FORMAT = 'qmd-reviewed-pairs-v1'  # Internal contract, never a guessed Laya format.


def _sha(value):
    if not isinstance(value,str) or len(value)!=64 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('invalid_sha256')
    return value


def _object_pairs(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate_json_key')
        result[key]=value
    return result


def _json(raw):
    return json.loads(raw,object_pairs_hook=_object_pairs,
                      parse_constant=lambda _:(_ for _ in ()).throw(ValueError('invalid_json_number')))


def read_private_json(path):
    """Explicit input file only; no configs, directory scanning, or remote fetch."""
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1 or info.st_mode & 0o077:
            raise ValueError('unsafe_input_file')
        if info.st_size>MAX_BYTES:raise ValueError('input_budget_exceeded')
        chunks=[];size=0
        while True:
            block=os.read(fd,min(65536,MAX_BYTES+1-size))
            if not block:break
            chunks.append(block);size+=len(block)
            if size>MAX_BYTES:raise ValueError('input_budget_exceeded')
        final=os.fstat(fd)
        if (info.st_size,info.st_mtime_ns,info.st_ctime_ns)!=(final.st_size,final.st_mtime_ns,final.st_ctime_ns):
            raise ValueError('input_changed')
        return _json(b''.join(chunks).decode('utf8'))
    finally:os.close(fd)


def _manifest(db, version_id):
    row=db.execute('SELECT body,sha256,revoked FROM manifests WHERE version_id=?',(version_id,)).fetchone()
    if not row or row[2] or digest(row[0])!=row[1]:
        raise ValueError('unavailable_or_revoked_manifest')
    body=_json(row[0])
    if not isinstance(body,dict) or type(body.get('schema_version')) is not int or body['schema_version'] not in (1,2) or canonical(body)!=row[0] or body.get('class_order')!=list(CLASS_ORDER):
        raise ValueError('manifest_contract_mismatch')
    return body,row[1]


def _snapshot(db,version_id,revisions):
    if not isinstance(revisions,dict):raise ValueError('invalid_revisions')
    for key,value in revisions.items():
        if not isinstance(key,str) or not key:raise ValueError('invalid_revisions')
        _sha(value)
    manifest,manifest_sha=_manifest(db,version_id)
    if len(manifest['cases'])>MAX_CASES:raise ValueError('case_budget_exceeded')
    members=db.execute('SELECT request_id,candidate_id FROM manifest_cases WHERE version_id=? ORDER BY request_id,candidate_id',(version_id,)).fetchall()
    if len(members)!=len(manifest['cases']):raise ValueError('manifest_membership_mismatch')
    rows={split:[] for split in sorted(SPLITS)}
    for (rid,cid),case in zip(members,manifest['cases']):
        if db.execute('SELECT 1 FROM tombstones WHERE request_id=?',(rid,)).fetchone():
            raise ValueError('deleted_case')
        stored=db.execute('SELECT s.body,l.body,l.review FROM samples s JOIN labels l ON l.request_id=s.request_id WHERE s.request_id=? AND l.candidate_id=?',(rid,cid)).fetchone()
        if stored is None or stored[2] is None:raise ValueError('missing_reviewed_case')
        sample=_json(stored[0]);label=validate_label(_json(stored[1]),sample);review=_json(stored[2])
        candidate=next(c for c in sample['candidates'] if c['candidate_id']==cid)
        if label['abstain']:raise ValueError('abstained_case')
        expected={'case_hash':digest(canonical([rid,cid])),'sample_sha256':digest(stored[0]),
                  'label_sha256':digest(stored[1]),'review_sha256':digest(stored[2]),
                  'revision_sha256':label['revision_sha256'],'class_index':CLASS_ORDER.index(label['relevance']),
                  'sampling_mode':sample.get('sampling',{}).get('mode','final-selected-only'),
                  'pool_complete':sample.get('sampling',{}).get('complete_within_returned_bound',False)}
        if any(case.get(k)!=v for k,v in expected.items()) or case.get('split') not in SPLITS or type(case.get('class_index')) is not int or type(case.get('pool_complete')) is not bool:
            raise ValueError('manifest_case_changed')
        review_keys={'reviewer_id','reference','evidence_sha256'}
        if label['schema_version']==2:review_keys.add('input_sha256')
        if set(review)!=review_keys or not all(isinstance(review[k],str) and 0<len(review[k].strip()) and len(review[k])<=512 for k in ('reviewer_id','reference')):
            raise ValueError('invalid_review')
        if review['evidence_sha256']!=review_digest(label) or (label['schema_version']==2 and review['input_sha256']!=digest(canonical(sample))):
            raise ValueError('review_changed')
        if revisions.get(cid)!=label['revision_sha256']:raise ValueError('export_stale_revision')
        family_keys=['request:'+digest(rid),'query:'+sample['prompt']['sha256'],
                     'document:'+digest(cid),'revision:'+label['revision_sha256']]
        pinned_families=case.get('split_family_hashes')
        prefixes={'request','query','document','revision','task','doc-family'}
        if not isinstance(pinned_families,list) or any(not isinstance(k,str) for k in pinned_families) or len(pinned_families)!=6 or len(set(pinned_families))!=6 or not set(family_keys)<=set(pinned_families):
            raise ValueError('manifest_split_provenance_required')
        if {k.split(':',1)[0] for k in pinned_families}!=prefixes:
            raise ValueError('manifest_split_provenance_required')
        for key in pinned_families:
            _sha(key.split(':',1)[1])
            pinned=db.execute('SELECT split FROM family_splits WHERE family_hash=?',(key,)).fetchone()
            if not pinned or pinned[0]!=case['split']:raise ValueError('split_guard_mismatch')
        inputs={'prompt':sample['prompt']['text'],'excerpt':candidate['excerpt']['text']}
        rows[case['split']].append({'schema_version':1,'case_hash':case['case_hash'],
            'input':inputs,'input_sha256':digest(canonical(inputs)),'target_class_index':case['class_index'],
            'provenance':dict(expected,host=sample['host'],sample_schema_version=sample['schema_version'],
                prompt_truncated=sample['prompt']['truncated'],excerpt_truncated=candidate['excerpt']['truncated'],
                split_family_hashes=pinned_families)})
    return manifest,manifest_sha,rows


def _payloads(manifest,manifest_sha,rows):
    files={'manifest.json':canonical(manifest)+'\n'}
    for split in sorted(SPLITS):
        files[split+'.jsonl']=''.join(canonical(row)+'\n' for row in rows[split])
    if sum(len(v.encode('utf8')) for v in files.values())>MAX_BYTES:
        raise ValueError('export_budget_exceeded')
    metadata={'schema_version':1,'format':FORMAT,'manifest_sha256':manifest_sha,
        'class_order':list(CLASS_ORDER),'counts':{s:len(rows[s]) for s in sorted(SPLITS)},
        'files_sha256':{k:digest(v) for k,v in sorted(files.items())},
        'training_input_fields':['prompt','excerpt'],'target':'target_class_index',
        'laya_contract':'unverified','review_authentication':'trusted-local-caller'}
    metadata['export_sha256']=digest(canonical(metadata))
    files['export.json']=canonical(metadata)+'\n'
    return metadata,files


def _directory(state_dir,manifest_sha):
    return Path(state_dir)/('export-'+manifest_sha)


def _check_directory(path):
    info=path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o077:
        raise ValueError('unsafe_export_directory')


def _check_files(path,files):
    _check_directory(path)
    for name,expected in files.items():
        fd=os.open(path/name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1 or info.st_mode & 0o077 or info.st_size>MAX_BYTES:
                raise ValueError('unsafe_export_file')
            actual=bytearray()
            while len(actual)<=MAX_BYTES:
                block=os.read(fd,min(65536,MAX_BYTES+1-len(actual)))
                if not block:break
                actual.extend(block)
            if bytes(actual)!=expected.encode('utf8'):raise ValueError('export_integrity_mismatch')
        finally:os.close(fd)


def _write_export(state_dir,destination,files):
    """Shared immutable private writer; caller holds the snapshot DB transaction."""
    if destination.exists() or destination.is_symlink():
        _check_files(destination,files)
    else:
        with tempfile.TemporaryDirectory(prefix='.export-staging-',dir=state_dir) as staging:
            for name,body in files.items():
                fd=os.open(Path(staging)/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'w',encoding='utf8',newline='') as stream:
                    stream.write(body);stream.flush();os.fsync(stream.fileno())
            os.rename(staging,destination)
        _check_files(destination,files)


def _save_report(destination,report):
    path=destination/('evaluation-'+report['report_sha256']+'.json')
    payload=canonical(report)+'\n'
    try:fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    except FileExistsError:
        _check_files(destination,{path.name:payload})
    else:
        with os.fdopen(fd,'w',encoding='utf8',newline='') as stream:
            stream.write(payload);stream.flush();os.fsync(stream.fileno())
    return dict(report,report_path=str(path))


def export_dataset(state_dir,version_id,current_revisions):
    """Write deterministic 0600 files under private state; immutable version reuse."""
    with database(state_dir) as db:
        manifest,checksum,rows=_snapshot(db,version_id,current_revisions)
        metadata,files=_payloads(manifest,checksum,rows)
        if not sum(metadata['counts'].values()):raise ValueError('empty_supervised_export')
        destination=_directory(state_dir,checksum)
        _write_export(state_dir,destination,files)
        return dict(metadata,directory=str(destination))


def inspect_export(state_dir,version_id,current_revisions):
    with database(state_dir) as db:
        manifest,checksum,rows=_snapshot(db,version_id,current_revisions)
        metadata,files=_payloads(manifest,checksum,rows)
        destination=_directory(state_dir,checksum)
        _check_files(destination,files)
        return dict(metadata,directory=str(destination))


def _evaluate(db,version_id,revisions,predictions,split):
    manifest,checksum,rows=_snapshot(db,version_id,revisions)
    metadata,files=_payloads(manifest,checksum,rows)
    if split not in ('validation','evaluation'):raise ValueError('held_out_split_required')
    expected=rows[split]
    if not expected:raise ValueError('empty_held_out_split')
    if not isinstance(predictions,dict) or set(predictions)!={'schema_version','export_sha256','split','model','predictions'} or type(predictions['schema_version']) is not int or predictions['schema_version']!=1:
        raise ValueError('invalid_predictions')
    if predictions['export_sha256']!=metadata['export_sha256'] or predictions['split']!=split:
        raise ValueError('prediction_export_mismatch')
    model=predictions['model']
    if not isinstance(model,dict) or set(model)!={'model_id','artifact_sha256','training_manifest_sha256','trained_splits'}:
        raise ValueError('invalid_model_provenance')
    if not isinstance(model['model_id'],str) or not 0<len(model['model_id'])<=512 or model['trained_splits']!=['train']:
        raise ValueError('invalid_model_provenance')
    _sha(model['artifact_sha256']);_sha(model['training_manifest_sha256'])
    training=db.execute('SELECT version_id FROM manifests WHERE sha256=? AND revoked=0',(model['training_manifest_sha256'],)).fetchone()
    if not training:raise ValueError('unavailable_training_manifest')
    train_manifest,_=_manifest(db,training[0])
    if not any(c['split']=='train' for c in train_manifest['cases']):raise ValueError('empty_training_split')
    # Bind provenance to a reviewed valid exported training version, not a guessed weight format.
    train_body,train_sha,train_rows=_snapshot(db,training[0],revisions)
    train_metadata,train_files=_payloads(train_body,train_sha,train_rows)
    records=predictions['predictions']
    if not isinstance(records,list) or len(records)!=len(expected):raise ValueError('prediction_membership_mismatch')
    by_hash={}
    for row in records:
        if not isinstance(row,dict) or set(row)!={'case_hash','input_sha256','class_index'}:raise ValueError('invalid_prediction_row')
        _sha(row['case_hash']);_sha(row['input_sha256'])
        if row['case_hash'] in by_hash:raise ValueError('duplicate_prediction')
        pred=row['class_index']
        if pred is not None and (type(pred) is not int or not 0<=pred<len(CLASS_ORDER)):raise ValueError('invalid_predicted_class')
        by_hash[row['case_hash']]=row
    matrix=[[0]*4 for _ in CLASS_ORDER]
    for case in expected:
        prediction=by_hash.pop(case['case_hash'],None)
        if prediction is None or prediction['input_sha256']!=case['input_sha256']:
            raise ValueError('prediction_membership_mismatch')
        pred=prediction['class_index']
        matrix[case['target_class_index']][3 if pred is None else pred]+=1
    if by_hash:raise ValueError('prediction_membership_mismatch')
    total=len(expected);correct=sum(matrix[i][i] for i in range(3))
    abstained=sum(row[3] for row in matrix);per_class=[]
    for i,name in enumerate(CLASS_ORDER):
        support=sum(matrix[i]);predicted=sum(row[i] for row in matrix);tp=matrix[i][i]
        recall=tp/support if support else None
        precision=tp/predicted if predicted else None
        f1=2*tp/(support+predicted) if support else None
        per_class.append({'class':name,'support':support,'precision':precision,'recall':recall,'f1':f1})
    supported=[c['f1'] for c in per_class if c['support']]
    report={'schema_version':1,'manifest_sha256':checksum,'export_sha256':metadata['export_sha256'],
        'prediction_sha256':digest(canonical(predictions)),'split':split,'model':model,
        'cases':total,'class_order':list(CLASS_ORDER),'confusion_columns':[*CLASS_ORDER,'abstain'],
        'confusion_matrix':matrix,'accuracy':correct/total,'coverage':(total-abstained)/total,
        'selective_accuracy':correct/(total-abstained) if total>abstained else None,
        'macro_f1_supported_classes':sum(supported)/len(supported),'per_class':per_class,
        'necessary_false_negative_rate':1-per_class[0]['recall'] if per_class[0]['recall'] is not None else None,
        'provenance_verification':'declared-model-hash-and-train-only-splits; weights-not-inspected',
        'promotion':'none','sampling_modes':manifest['sampling_modes']}
    report['report_sha256']=digest(canonical(report))
    return report,files,train_files,train_sha


def evaluate_predictions(state_dir,version_id,current_revisions,predictions,*,split='evaluation'):
    """Score supplied predictions only; validation/evaluation never invoke a model."""
    with database(state_dir) as db:
        report,files,train_files,train_sha=_evaluate(db,version_id,current_revisions,predictions,split)
        destination=_directory(state_dir,report['manifest_sha256'])
        _check_files(destination,files)
        _check_files(_directory(state_dir,train_sha),train_files)
        return _save_report(destination,report)

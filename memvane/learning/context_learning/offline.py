"""Reviewed labels and immutable, deletion-aware manifests. No trainer or provider."""
import json

from .contracts import CLASS_ORDER, digest, validate_label
from .store import canonical, database

SPLITS = frozenset({'train', 'calibration', 'validation', 'evaluation'})


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError('invalid_identifier')
    return value


def review_digest(label):
    return digest(canonical(label)) if label['schema_version'] == 2 else digest(label['evidence'])


def save_label(state_dir, label, *, review=None):
    """A review is an explicit trusted-caller attestation, not automatic truth."""
    if not isinstance(label, dict):
        raise ValueError('invalid_label')
    with database(state_dir) as db:
        row = db.execute('SELECT body FROM samples WHERE request_id=?', (label.get('request_id'),)).fetchone()
        if row is None:
            raise ValueError('unknown_or_deleted_request')
        sample = json.loads(row[0])
        clean = validate_label(label, sample)
        if review is not None:
            review_keys = {'reviewer_id', 'reference', 'evidence_sha256'}
            if clean['schema_version'] == 2:
                review_keys.add('input_sha256')
            if not isinstance(review, dict) or set(review) != review_keys:
                raise ValueError('invalid_review')
            _text(review['reviewer_id']); _text(review['reference'])
            if clean['schema_version'] == 2 and review['input_sha256'] != digest(canonical(sample)):
                raise ValueError('review_input_mismatch')
            if review['evidence_sha256'] != review_digest(clean):
                raise ValueError('review_evidence_mismatch')
        body = canonical(clean)
        review_body = canonical(review) if review is not None else None
        key = (clean['request_id'], clean['candidate_id'])
        old = db.execute('SELECT body,review FROM labels WHERE request_id=? AND candidate_id=?', key).fetchone()
        if old is not None:
            if old[0] != body or (old[1] is not None and review_body != old[1]):
                raise ValueError('label_conflict')
            if old[1] is None and review_body is not None:
                db.execute('UPDATE labels SET review=? WHERE request_id=? AND candidate_id=?', (review_body, *key))
            return 'reviewed' if review_body is not None else 'provisional'
        db.execute('INSERT INTO labels VALUES (?,?,?,?)', (*key, body, review_body))
    return 'reviewed' if review_body is not None else 'provisional'



def save_provisional_batch(state_dir, sample, labels):
    """Atomic teacher write; pin exact stored input and never promote/overwrite reviews."""
    from .teacher import validate_response
    clean = validate_response(canonical({'labels': labels}).encode(), sample)
    with database(state_dir) as db:
        rid = sample['request_id']
        row = db.execute('SELECT body FROM samples WHERE request_id=?', (rid,)).fetchone()
        if row is None or canonical(json.loads(row[0])) != canonical(sample):
            raise ValueError('unknown_deleted_or_changed_request')
        if db.execute('SELECT 1 FROM tombstones WHERE request_id=?', (rid,)).fetchone():
            raise ValueError('deleted_request')
        pending = []
        for label in clean:
            key = (rid, label['candidate_id'])
            body = canonical(label)
            old = db.execute('SELECT body,review FROM labels WHERE request_id=? AND candidate_id=?', key).fetchone()
            if old is not None:
                if old[1] is not None:
                    raise ValueError('reviewed_label_exists')
                if old[0] != body:
                    raise ValueError('label_conflict')
            else:
                pending.append((*key, body))
        db.executemany('INSERT INTO labels VALUES (?,?,?,NULL)', pending)
    return len(clean)


def build_manifest(state_dir, version_id, assignments, current_revisions, *, parent_id=None):
    """Pin reviewed, non-abstaining cases; persist split families across versions."""
    _text(version_id)
    if not isinstance(assignments, dict) or not isinstance(current_revisions, dict):
        raise ValueError('invalid_manifest_input')
    with database(state_dir) as db:
        allowed = None
        parent_hash = None
        if parent_id is not None:
            parent = db.execute('SELECT sha256 FROM manifests WHERE version_id=?', (parent_id,)).fetchone()
            if parent is None:
                raise ValueError('unknown_parent')
            parent_hash = parent[0]
            allowed = set(db.execute('SELECT request_id,candidate_id FROM manifest_cases WHERE version_id=?', (parent_id,)))
        rows = db.execute('SELECT l.request_id,l.candidate_id,l.body,l.review,s.body FROM labels l JOIN samples s ON s.request_id=l.request_id WHERE l.review IS NOT NULL ORDER BY l.request_id,l.candidate_id').fetchall()
        cases = []; membership = []; pending = {}; modes=set(); excluded = {'abstained': 0, 'stale': 0}
        for rid, cid, label_body, review_body, sample_body in rows:
            if allowed is not None and (rid, cid) not in allowed:
                continue
            if rid not in assignments:
                continue
            label = validate_label(json.loads(label_body), json.loads(sample_body))
            if label['abstain']:
                excluded['abstained'] += 1; continue
            if current_revisions.get(cid) != label['revision_sha256']:
                excluded['stale'] += 1; continue
            assignment = assignments[rid]
            if not isinstance(assignment, dict) or set(assignment) != {'split', 'task_family', 'document_families'}:
                raise ValueError('invalid_assignment')
            split = assignment['split']
            if not isinstance(split, str) or split not in SPLITS:
                raise ValueError('invalid_split')
            family = _text(assignment['task_family'])
            docs = assignment['document_families']
            if not isinstance(docs, dict):
                raise ValueError('missing_document_family')
            doc_family = _text(docs.get(cid))
            sample = json.loads(sample_body)
            keys = ['request:'+digest(rid), 'task:'+digest(family), 'document:'+digest(cid),
                    'doc-family:'+digest(doc_family), 'query:'+sample['prompt']['sha256'],
                    'revision:'+label['revision_sha256']]
            for family_key in keys:
                old = db.execute('SELECT split FROM family_splits WHERE family_hash=?', (family_key,)).fetchone()
                if (old and old[0] != split) or (family_key in pending and pending[family_key] != split):
                    raise ValueError('split_family_leakage')
                pending[family_key] = split
            mode=sample.get('sampling',{}).get('mode','final-selected-only')
            modes.add(mode)
            cases.append({'split_family_hashes':sorted(keys),'sampling_mode':mode,'pool_complete':sample.get('sampling',{}).get('complete_within_returned_bound',False),'case_hash': digest(canonical([rid,cid])), 'sample_sha256': digest(sample_body),
                          'label_sha256': digest(label_body), 'review_sha256': digest(review_body),
                          'revision_sha256': label['revision_sha256'], 'class_index': CLASS_ORDER.index(label['relevance']), 'split': split})
            membership.append((version_id,rid,cid))
        body = {'schema_version': 2, 'version_id': version_id, 'class_order': list(CLASS_ORDER),
                'sampling': next(iter(modes)) if len(modes)==1 else ('mixed' if modes else 'no-supervised-cases'), 'sampling_modes':sorted(modes), 'parent_sha256': parent_hash,
                'cases': cases, 'excluded': excluded}
        encoded = canonical(body); checksum = digest(encoded)
        old = db.execute('SELECT body,revoked FROM manifests WHERE version_id=?', (version_id,)).fetchone()
        if old is not None:
            if old[1] or old[0] != encoded:
                raise ValueError('immutable_manifest_conflict')
            return body
        db.execute('INSERT INTO manifests VALUES (?,?,?,0)', (version_id,encoded,checksum))
        db.executemany('INSERT INTO manifest_cases VALUES (?,?,?)', membership)
        db.executemany('INSERT OR IGNORE INTO family_splits VALUES (?,?)', pending.items())
        return body


def load_manifest(state_dir, version_id):
    with database(state_dir) as db:
        row = db.execute('SELECT body,sha256,revoked FROM manifests WHERE version_id=?', (version_id,)).fetchone()
        if row is None or row[2] or digest(row[0]) != row[1]:
            raise ValueError('unavailable_or_revoked_manifest')
        return json.loads(row[0])


def delete_case(state_dir, request_id):
    """Logical source deletion and manifest revocation; never claims weight unlearning."""
    _text(request_id)
    with database(state_dir) as db:
        db.execute('INSERT OR IGNORE INTO tombstones VALUES (?)', (request_id,))
        db.execute('UPDATE manifests SET revoked=1 WHERE version_id IN (SELECT version_id FROM manifest_cases WHERE request_id=?)', (request_id,))
        db.execute('DELETE FROM manifest_cases WHERE request_id=?', (request_id,))
        db.execute('DELETE FROM labels WHERE request_id=?', (request_id,))
        db.execute('DELETE FROM compact_inputs WHERE request_id=?', (request_id,))
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='selection_gold'").fetchone():
            db.execute('DELETE FROM selection_gold WHERE request_id=?', (request_id,))
        for table in ('teacher_attempts', 'teacher_second_opinions', 'teacher_jobs'):
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                db.execute('DELETE FROM '+table+' WHERE request_id=?', (request_id,))
        db.execute('DELETE FROM samples WHERE request_id=?', (request_id,))
    return 'deleted'


def review_view(state_dir, request_id, candidate_id):
    """Explicit private snapshot view; never fetches external/current documents."""
    with database(state_dir) as db:
        row=db.execute('SELECT body FROM samples WHERE request_id=?',(request_id,)).fetchone()
        if row is None:
            raise ValueError('unknown_or_deleted_request')
        sample=json.loads(row[0])
        candidate=next((c for c in sample['candidates'] if c['candidate_id']==candidate_id),None)
        if candidate is None:
            raise ValueError('unknown_candidate')
        label=db.execute('SELECT body,review FROM labels WHERE request_id=? AND candidate_id=?',(request_id,candidate_id)).fetchone()
        compact_row=db.execute('SELECT body FROM compact_inputs WHERE request_id=? AND candidate_id=?',
                               (request_id,candidate_id)).fetchone()
        compact=json.loads(compact_row[0]) if compact_row else None
        if compact and (compact['source_sha256']!=candidate['revision_sha256'] or
                        compact['body_sha256']!=digest(compact['body_text'])):
            raise ValueError('compact_input_integrity_mismatch')
        return {'request_id':request_id,'candidate':candidate,'compact_input':compact,'prompt':sample['prompt'],
                'input_sha256':digest(canonical(sample)),
                'sampling':sample.get('sampling',{'mode':'final-selected-only'}),
                'label':json.loads(label[0]) if label else None,
                'attestation':json.loads(label[1]) if label and label[1] else None,
                'status':'reviewer-approved' if label and label[1] else ('provisional' if label else 'unlabeled'),
                'authentication':'not-verified','truth':'not-certified'}


def approve_review(state_dir,label,*,input_sha256,reviewer_id,reference):
    if not isinstance(label,dict) or label.get('schema_version')!=2:
        raise ValueError('review_workflow_requires_v2_label')
    # save_label checks the exact stored request hash under the same transaction.
    return save_label(state_dir,label,review={'reviewer_id':reviewer_id,'reference':reference,
                      'evidence_sha256':review_digest(label),'input_sha256':input_sha256})

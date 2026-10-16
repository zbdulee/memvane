"""Durable source/query exposure assignment for an explicit project cohort.

Every captured row, including failed, tombstoned and historical rows, consumes
its first exposure assignment. Deleting capture rows cannot free an identity.
A new case connecting training and holdout components is blocked permanently.
"""
from __future__ import annotations
from runtime_admission import prepared,preparation_checkpoint

import json
from pathlib import Path
from .contracts import digest,validate_request
from .store import canonical


def assignment(documents):
    key=digest(canonical(sorted(set(documents))))
    return ('holdout' if int(key[:8],16)%5==0 else 'train',key)


def _keys(sample,bodies,project):
    validate_request(sample)
    if set(bodies)!={c['candidate_id'] for c in sample['candidates']}:
        raise ValueError('project_cohort_bodies_invalid')
    result={'question:'+digest(' '.join(sample['prompt']['text'].casefold().split()))}
    for candidate in sample['candidates']:
        cid=candidate['candidate_id'];body=bodies[cid]
        if not isinstance(body,str) or not body.strip():raise ValueError('project_cohort_bodies_invalid')
        result.update(('document:'+digest(canonical([str(project),cid])),
                       'revision:'+candidate['revision_sha256'],'body:'+digest(body)))
    return result


def _tables(db):
    db.execute('CREATE TABLE IF NOT EXISTS project_cohort_exposure (scope TEXT NOT NULL, identity TEXT NOT NULL, role TEXT NOT NULL, family TEXT NOT NULL, PRIMARY KEY(scope,identity))')
    db.execute('CREATE TABLE IF NOT EXISTS project_cohort_assignments (scope TEXT NOT NULL, request_id TEXT NOT NULL, input_sha256 TEXT NOT NULL, role TEXT NOT NULL, family TEXT NOT NULL, PRIMARY KEY(scope,request_id))')


@prepared
def admitted(db,project,grant,statuses=('queued','first_done')):
    project=Path(project).resolve(strict=True)
    if grant.get('projectRoot')!=str(project):raise ValueError('project_cohort_scope_changed')
    cohort=grant['cohort'];cutoff=cohort['notBeforeUnixMs']/1000
    scope=digest(canonical([str(project),cohort['id'],cohort['notBeforeUnixMs']]))
    readonly=bool(db.execute('PRAGMA query_only').fetchone()[0])
    if readonly:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='project_cohort_assignments'").fetchone():return []
    else:_tables(db)
    rows=db.execute('SELECT project_key,request_id,input_sha256,sample,bodies,gold,split,project_root,corpus_fingerprint,family_keys,trained_cycle,captured_at,status,use_state FROM cases WHERE project_root=? ORDER BY captured_at,request_id',(str(project),))
    valid=[]
    for row in rows:
        preparation_checkpoint()
        try:
            if digest(row[3])!=row[2]:continue
            sample=json.loads(row[3]);bodies=json.loads(row[4])
            if sample['request_id']!=row[1]:continue
            keys=_keys(sample,bodies,project)
        except (KeyError,TypeError,ValueError):continue
        previous=db.execute('SELECT input_sha256,role,family FROM project_cohort_assignments WHERE scope=? AND request_id=?',(scope,row[1])).fetchone()
        if previous:
            if previous[0]!=row[2] or previous[1] in ('historical','blocked'):continue
            role,family=previous[1:]
        else:
            if readonly:return []
            existing=[]
            for key in sorted(keys):
                found=db.execute('SELECT role,family FROM project_cohort_exposure WHERE scope=? AND identity=?',(scope,key)).fetchone()
                if found:existing.append(found)
            roles={r[0] for r in existing}
            if row[11]<cutoff:
                role,family='historical',digest(row[1])
                for old in {r[1] for r in existing}:
                    db.execute("UPDATE project_cohort_exposure SET role='historical' WHERE scope=? AND family=?",(scope,old))
                    db.execute("UPDATE project_cohort_assignments SET role='historical' WHERE scope=? AND family=?",(scope,old))
            elif 'historical' in roles or 'blocked' in roles or len(roles)>1:
                role,family='blocked',digest(row[1])
            elif existing:role,family=existing[0]
            else:role,family=assignment([c['candidate_id'] for c in sample['candidates']])
            # Same-role connected source components count as one family.
            if role not in ('historical','blocked'):
                families={r[1] for r in existing}|{family};family=min(families)
                for old in families-{family}:
                    db.execute('UPDATE project_cohort_exposure SET family=? WHERE scope=? AND family=?',(family,scope,old))
                    db.execute('UPDATE project_cohort_assignments SET family=? WHERE scope=? AND family=?',(family,scope,old))
            db.execute('INSERT INTO project_cohort_assignments VALUES (?,?,?,?,?)',(scope,row[1],row[2],role,family))
            for key in sorted(keys):db.execute('INSERT OR IGNORE INTO project_cohort_exposure VALUES (?,?,?,?)',(scope,key,role,family))
        if row[12] in statuses and row[13]=='active' and role not in ('blocked','historical'):
            if row[5] is not None:
                try:
                    gold=json.loads(row[5])
                    if gold['input_sha256']!=row[2] or not isinstance(gold['selected_ids'],list) or len(gold['selected_ids'])>3 or not set(gold['selected_ids'])<=set(bodies):continue
                except (KeyError,TypeError,ValueError):continue
            elif row[12]=='gold':continue
            valid.append((role,row))
    result=[]
    for role,row in valid:
        # Merges later in the replay may have changed an earlier family's ID.
        current=db.execute('SELECT role,family FROM project_cohort_assignments WHERE scope=? AND request_id=?',(scope,row[1])).fetchone()
        if current[0] not in ('train','holdout'):continue
        result.append((current[0],tuple(row[:12])+(current[0]+'/'+current[1],)))
    return result

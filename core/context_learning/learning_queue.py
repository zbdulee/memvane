"""Namespace-private pinned batches; no trainer, teacher or publication here."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import secrets
import time
from .contracts import digest
from .store import canonical
from . import resident


def semantic_key(row, namespace, family, *, normalize_query=False):
    sample, bodies, gold = json.loads(row[3]), json.loads(row[4]), json.loads(row[5])
    prompt=sample['prompt']['text']
    if normalize_query:prompt=' '.join(prompt.casefold().split())
    # Neither capture UUID/time nor a provider receipt retry is fresh evidence.
    evidence = [namespace, family, str(Path(row[7]).resolve()),
        None if normalize_query else row[12] if len(row)>12 else None,
        prompt, sorted((c['candidate_id'],c['revision_sha256'],
            digest(bodies[c['candidate_id']])) for c in sample['candidates']),
        sorted(gold['selected_ids'])]
    return digest(canonical(evidence))


class LearningQueue:
    def __init__(self, root, *, connect=None):
        self.root = resident._private_state(Path(root))
        self.scope = digest(str(self.root.resolve()))
        if connect is None:
            from .shared import _db
            connect = lambda: _db(self.root)
        self.connect = connect

    def schema(self, db):
        db.execute('CREATE TABLE IF NOT EXISTS learning_jobs (job_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, approval TEXT NOT NULL, cohort TEXT NOT NULL, family TEXT NOT NULL, manifest TEXT NOT NULL, status TEXT NOT NULL, owner TEXT, attempts INTEGER NOT NULL, failures INTEGER NOT NULL, retry_at REAL NOT NULL, reason TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS learning_consumption (namespace TEXT NOT NULL, evidence_key TEXT NOT NULL, job_id TEXT NOT NULL, PRIMARY KEY(namespace,evidence_key))')
        if not db.in_transaction:db.execute('BEGIN IMMEDIATE')

    def get(self, job_id, *, db=None):
        if db is None:
            with self.connect() as db:return self.get(job_id,db=db)
        self.schema(db)
        row=db.execute('SELECT manifest,status,retry_at,attempts,failures,reason FROM learning_jobs WHERE job_id=? AND namespace=?',(job_id,self.scope)).fetchone()
        return {**json.loads(row[0]),'status':row[1],'retry_at':row[2],'attempts':row[3],'failures':row[4],'reason':row[5]} if row else None

    def plan(self, rows, *, approval, cohort, family, minimum, maximum, roles=None, cumulative=False):
        if type(cumulative)is not bool:raise ValueError('invalid_learning_replay_mode')
        if not 2<=minimum<=maximum<=500:raise ValueError('invalid_learning_batch_bound')
        admitted_roles = [r[6] for r in rows] if roles is None else roles
        if len(admitted_roles)!=len(rows) or any(role!='train' for role in admitted_roles):
            raise ValueError('holdout_in_training')
        with self.connect() as db:
            self.schema(db)
            old=db.execute("SELECT job_id FROM learning_jobs WHERE namespace=? AND approval=? AND cohort=? AND family=? AND status IN ('queued','running','deferred','blocked') ORDER BY rowid LIMIT 1",(self.scope,approval,cohort,family)).fetchone()
            if old:return self.get(old[0],db=db)
            unique={}
            for row in rows:unique.setdefault(semantic_key(row,self.scope,family,normalize_query=cumulative),row)
            consumed={r[0] for r in db.execute('SELECT evidence_key FROM learning_consumption WHERE namespace=?',(self.scope,))}
            # Historical trained_cycle prevents importing old training as fresh.
            fresh=[k for k,r in unique.items() if k not in consumed and r[10] is None]
            if len(fresh)<minimum:
                old=db.execute("SELECT job_id FROM learning_jobs WHERE namespace=? AND approval=? AND cohort=? AND family=? AND status='staged' ORDER BY rowid DESC LIMIT 1",(self.scope,approval,cohort,family)).fetchone()
                return self.get(old[0],db=db) if old else None
            fresh=fresh[:maximum]
            replay_limit=maximum-len(fresh) if cumulative else min(32,maximum-len(fresh),len(fresh))
            replay=[k for k,r in unique.items() if k in consumed or r[10] is not None][:replay_limit]
            keys=fresh+replay
            job_id='cohort-'+digest(canonical([self.scope,approval,cohort,family,keys]))[:20]
            members=[{'key':key,'request_id':unique[key][1],'input_sha256':unique[key][2],
                      'row_sha256':digest(canonical(list(unique[key][:6]))),'fresh':key in fresh} for key in keys]
            manifest={'id':job_id,'namespace':self.scope,'approval':approval,'cohort':cohort,'family':family,'members':members}
            if cumulative:manifest['queryNormalization']='casefold-whitespace-v1'
            db.execute("INSERT INTO learning_jobs VALUES (?,?,?,?,?,?,'queued',NULL,0,0,0,NULL)",(job_id,self.scope,approval,cohort,family,canonical(manifest)))
            return self.get(job_id,db=db)

    def rows(self, job, available):
        marker=job.get('queryNormalization')
        if marker not in (None,'casefold-whitespace-v1'):raise ValueError('learning_batch_normalization_invalid')
        by_id={(r[1],r[2]):r for r in available};result=[]
        for member in job['members']:
            row=by_id.get((member['request_id'],member['input_sha256']))
            if (row is None or semantic_key(row,self.scope,job['family'],normalize_query=marker is not None)!=member['key']
                    or digest(canonical(list(row[:6])))!=member['row_sha256']):
                raise ValueError('learning_batch_source_changed')
            result.append(row)
        return result

    @contextmanager
    def claim(self, job_id, *, now=None):
        now=time.time() if now is None else now
        fd=resident._private_lock(self.root/'learning-claim.lock')
        if os.fstat(fd).st_nlink!=1:
            os.close(fd);raise ValueError('unsafe_learning_claim')
        claimed=False
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                yield None;return
            with self.connect() as db:
                job=self.get(job_id,db=db)
                if (job is None or job['status'] in ('blocked','staged') or job['retry_at']>now):
                    claim=None
                else:
                    consumed={r[0] for r in db.execute('SELECT evidence_key FROM learning_consumption WHERE namespace=?',(self.scope,))}
                    if any(m['fresh'] and m['key'] in consumed for m in job['members']):
                        db.execute("UPDATE learning_jobs SET status='blocked',reason='already_consumed' WHERE job_id=?",(job_id,));claim=None
                    else:
                        db.execute("UPDATE learning_jobs SET status='running',owner=?,attempts=attempts+1 WHERE job_id=?",(secrets.token_hex(16),job_id))
                        claimed=True;claim=fd
            yield claim
        finally:
            if claimed:
                with self.connect() as db:
                    self.schema(db)
                    db.execute("UPDATE learning_jobs SET status='deferred',retry_at=?,reason='interrupted' WHERE job_id=? AND status='running'",(now,job_id))
            os.close(fd)

    def defer(self, job_id, reason, *, failure=False, now=None):
        now=time.time() if now is None else now
        with self.connect() as db:
            job=self.get(job_id,db=db)
            failures=job['failures']+int(failure)
            retry=now+min(300,30*2**min(job['attempts'],4))
            db.execute('UPDATE learning_jobs SET status=?,failures=?,retry_at=?,reason=? WHERE job_id=? AND namespace=?',('blocked' if failures>=3 else 'deferred',failures,retry,reason,job_id,self.scope))

    def complete(self, job_id, *, db=None):
        if db is None:
            with self.connect() as db:return self.complete(job_id,db=db)
        job=self.get(job_id,db=db)
        for member in job['members']:
            old=db.execute('SELECT job_id FROM learning_consumption WHERE namespace=? AND evidence_key=?',(self.scope,member['key'])).fetchone()
            if old and old[0]!=job_id and member['fresh']:raise ValueError('learning_evidence_already_consumed')
            db.execute('INSERT OR IGNORE INTO learning_consumption VALUES (?,?,?)',(self.scope,member['key'],job_id))
        db.execute("UPDATE learning_jobs SET status='staged',owner=NULL,retry_at=0,reason=NULL WHERE job_id=? AND namespace=?",(job_id,self.scope))

    def pending(self, *, now=None):
        now=time.time() if now is None else now
        with self.connect() as db:
            self.schema(db)
            return [self.get(r[0],db=db) for r in db.execute("SELECT job_id FROM learning_jobs WHERE namespace=? AND status IN ('queued','running','deferred') AND retry_at<=? ORDER BY rowid",(self.scope,now)).fetchall()]

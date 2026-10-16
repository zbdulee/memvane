"""One durable dispatch identity per project/case/phase. Never retries calls."""
import re
import time

STAGES=frozenset({'reserved','dispatch_unknown','dispatched','answer_received',
                  'committed','pre_send_rejected','dispatched_uncertain'})


def ensure(db):
    db.execute('CREATE TABLE IF NOT EXISTS project_gold_attempts ('
       'project_key TEXT NOT NULL,request_id TEXT NOT NULL,phase TEXT NOT NULL,'
       'input_sha256 TEXT NOT NULL,stage TEXT NOT NULL,answer_sha256 TEXT,'
       'child_pid INTEGER,error_code TEXT,reserved_at REAL NOT NULL,updated_at REAL NOT NULL,'
       'PRIMARY KEY(project_key,request_id,phase))')


def reserve(db,project_key,request_id,phase,input_sha256,max_calls):
    ensure(db)
    if (phase not in ('first','second') or not isinstance(project_key,str) or not project_key
        or not isinstance(request_id,str) or not request_id
        or not re.fullmatch('[0-9a-f]{64}',input_sha256)
        or type(max_calls) is not int or not 1<=max_calls<=1000):
        raise ValueError('invalid_gold_reservation')
    if stage(db,project_key,request_id,phase) is not None:
        raise ValueError('gold_attempt_already_reserved')
    if prior_input(db,project_key,request_id,input_sha256):
        raise ValueError('duplicate_input')
    if db.execute('SELECT COUNT(*) FROM project_gold_attempts WHERE project_key=?',
                  (project_key,)).fetchone()[0]>=max_calls:
        raise ValueError('gold_budget_exhausted')
    now=time.time()
    db.execute('INSERT INTO project_gold_attempts VALUES (?,?,?,?,?,NULL,NULL,NULL,?,?)',
               (project_key,request_id,phase,input_sha256,'reserved',now,now))


def prior_input(db,project_key,request_id,input_sha256):
    """Capture UUIDs do not authorize rebilling the same immutable task.

    Include reserved and uncertain stages because a process may have crashed at
    dispatch. A proven pre-send rejection is the only known non-transmission.
    The same capture may still resume its distinct second opinion normally.
    """
    ensure(db)
    return db.execute("SELECT 1 FROM project_gold_attempts WHERE project_key=? AND request_id!=? AND input_sha256=? AND stage!='pre_send_rejected' LIMIT 1",
                      (project_key,request_id,input_sha256)).fetchone() is not None


def stage(db,project_key,request_id,phase):
    ensure(db)
    row=db.execute('SELECT stage FROM project_gold_attempts WHERE project_key=? AND request_id=? AND phase=?',
                   (project_key,request_id,phase)).fetchone()
    return row[0] if row else None


def transition(db,project_key,request_id,phase,expected,new,*,answer_sha256=None,
               child_pid=None,error_code=None):
    if (new not in STAGES or isinstance(expected,str) or not expected
        or not set(expected)<=STAGES
        or (answer_sha256 is not None and not re.fullmatch('[0-9a-f]{64}',answer_sha256))
        or (child_pid is not None and (type(child_pid) is not int or child_pid<=0))
        or (error_code is not None and not re.fullmatch('[a-z0-9_]{1,80}',error_code))):
        raise ValueError('invalid_gold_transition')
    marks=','.join('?' for _ in expected)
    changed=db.execute('UPDATE project_gold_attempts SET stage=?,updated_at=?,answer_sha256=COALESCE(?,answer_sha256),child_pid=COALESCE(?,child_pid),error_code=COALESCE(?,error_code) WHERE project_key=? AND request_id=? AND phase=? AND stage IN ('+marks+')',
        (new,time.time(),answer_sha256,child_pid,error_code,project_key,request_id,phase,*expected)).rowcount
    if changed!=1:raise ValueError('gold_transition_conflict')


def recover(db,project_key):
    ensure(db)
    db.execute("UPDATE project_gold_attempts SET stage='dispatched_uncertain',error_code='interrupted_external_attempt',updated_at=? WHERE project_key=? AND stage IN ('reserved','dispatch_unknown','dispatched','answer_received')",(time.time(),project_key))


def fail(db,project_key,request_id,phase):
    current=stage(db,project_key,request_id,phase)
    if current in ('reserved','dispatch_unknown','dispatched','answer_received'):
        transition(db,project_key,request_id,phase,(current,),
                   'pre_send_rejected' if current=='reserved' else 'dispatched_uncertain',
                   error_code='gold_attempt_failed')

"""One-use gold-call stage receipts; never infer dispatch from a reservation.

Historical calls remain without a receipt and therefore retain unknown
dispatch state. No transition here invokes a provider or authorizes retry.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import time
import uuid


SCHEMA = 'qmd-gold-dispatch-receipt-v1'
STAGES = frozenset({'reserved', 'dispatch_unknown', 'dispatched',
                    'pre_send_rejected', 'dispatched_uncertain',
                    'answer_received', 'committed'})


def runtime_version():
    files = (Path(__file__), Path(__file__).with_name('synthetic_teacher.py'),
             Path(__file__).with_name('codex_adapter.py'))
    fingerprint = hashlib.sha256()
    for file in files:
        fingerprint.update(file.name.encode())
        fingerprint.update(hashlib.sha256(file.read_bytes()).digest())
    return SCHEMA + ':' + fingerprint.hexdigest()


def ensure_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS gold_attempt_receipts ('
               'call_key TEXT PRIMARY KEY REFERENCES experiment_calls(call_key), '
               'attempt_id TEXT NOT NULL UNIQUE, runtime_version TEXT NOT NULL, '
               'transport TEXT NOT NULL CHECK(transport IN (\'codex_cli\',\'injected\')), '
               'stage TEXT NOT NULL, input_sha256 TEXT NOT NULL, '
               'answer_sha256 TEXT, error_code TEXT, child_pid INTEGER, '
               'reserved_at REAL NOT NULL, updated_at REAL NOT NULL)')


def reserve(db, call_key, input_sha256, *, transport):
    ensure_schema(db)
    if (transport not in ('codex_cli', 'injected') or not isinstance(call_key, str)
            or not re.fullmatch(r'gold:[^:]+:(first_running|second_running)', call_key)
            or not re.fullmatch(r'[0-9a-f]{64}', input_sha256)):
        raise ValueError('invalid_gold_receipt')
    if db.execute('SELECT 1 FROM gold_attempt_receipts WHERE call_key=?',
                  (call_key,)).fetchone():
        raise ValueError('gold_receipt_already_reserved')
    now = time.time()
    db.execute('INSERT INTO gold_attempt_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
               (call_key, str(uuid.uuid4()), runtime_version(), transport,
                'reserved', input_sha256, None, None, None, now, now))


def transition(db, call_key, expected, stage, *, answer_sha256=None,
               error_code=None, child_pid=None):
    if (stage not in STAGES or isinstance(expected, str) or not expected
            or not set(expected) <= STAGES
            or (answer_sha256 is not None and not re.fullmatch(r'[0-9a-f]{64}', answer_sha256))
            or (error_code is not None and not re.fullmatch(r'[a-z0-9_]{1,80}', error_code))
            or (child_pid is not None and (type(child_pid) is not int or child_pid <= 0))):
        raise ValueError('invalid_gold_receipt_transition')
    marks = ','.join('?' for _ in expected)
    changed = db.execute(
        'UPDATE gold_attempt_receipts SET stage=?,updated_at=?, '
        'answer_sha256=COALESCE(?,answer_sha256), '
        'error_code=COALESCE(?,error_code),child_pid=COALESCE(?,child_pid) '
        f'WHERE call_key=? AND stage IN ({marks})',
        (stage, time.time(), answer_sha256, error_code, child_pid,
         call_key, *expected)).rowcount
    if changed != 1:
        raise ValueError('gold_receipt_transition_conflict')


def failure(db, call_key, error):
    row = db.execute('SELECT stage,transport FROM gold_attempt_receipts WHERE call_key=?',
                     (call_key,)).fetchone()
    if row is None:
        raise ValueError('gold_receipt_missing')
    code = str(error)
    if not re.fullmatch(r'[a-z0-9_]{1,80}', code):
        code = type(error).__name__.lower()
    stage, transport = row
    if stage == 'reserved':
        transition(db, call_key, (stage,),
                   'pre_send_rejected' if transport == 'codex_cli' else 'dispatch_unknown',
                   error_code=code)
    elif stage == 'dispatched':
        transition(db, call_key, (stage,), 'dispatched_uncertain', error_code=code)
    elif stage in ('dispatch_unknown', 'answer_received'):
        transition(db, call_key, (stage,), stage, error_code=code)
    elif stage not in ('pre_send_rejected', 'dispatched_uncertain', 'committed'):
        raise ValueError('gold_receipt_invalid_stage')

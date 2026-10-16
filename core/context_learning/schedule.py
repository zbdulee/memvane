"""Opt-in invocation gate for local learning cycles; no background daemon.

95% is a quality target for *learning interval*, never an activation threshold.
Only a caller that explicitly invokes run_due_cycle can launch a trainer.
"""
from pathlib import Path
import fcntl
import os
import time

from .contracts import digest
from .cycle_policy import next_interval
from .local_cycle import _atomic_json, _load_state, run_cycle,ResourceDeferred
from .selection_queue import build_queue
from .store import canonical, database


def run_due_cycle(state_dir, cycle_id, version_id, revision_reader, trainer, *, incumbent_artifact, now=None):
    with database(state_dir): pass
    lock = Path(state_dir)/'learning-schedule.lock'
    fd = os.open(lock, os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
        return _run_due_cycle_locked(state_dir, cycle_id, version_id, revision_reader,
            trainer, incumbent_artifact=incumbent_artifact, now=now)
    finally:
        os.close(fd)


def _run_due_cycle_locked(state_dir, cycle_id, version_id, revision_reader, trainer, *, incumbent_artifact, now):
    if now is None: now = time.time()
    if type(now) not in (int, float) or now < 0: raise ValueError('invalid_time')
    queue = build_queue(state_dir, version_id, revision_reader())
    family_hashes = sorted({k for cases in queue['splits'].values() for c in cases for k in c['family_hashes']
                            if k.startswith('task:') or k.startswith('doc-family:')})
    revisions_hash = digest(canonical(sorted((c['id'], c['revision_sha256'])
        for cases in queue['splits'].values() for case in cases for c in case['candidates'])))
    path = Path(state_dir)/'learning-schedule.json'
    previous = _load_state(path)
    new_family = bool(previous and set(family_hashes)-set(previous['family_hashes']))
    source_churn = bool(previous and revisions_hash != previous['revisions_sha256'])
    accelerated = new_family or source_churn or previous and previous.get('recent_failure')
    earliest = previous['last_run_at'] + 24*3600 if previous else now
    due = min(previous['next_due'], earliest) if previous and accelerated else previous['next_due'] if previous else now
    if previous and now < due:
        return {'status': 'not_due', 'next_due': due,
                'interval_hours': previous['interval_hours']}
    try:
        result = run_cycle(state_dir, cycle_id, version_id, revision_reader, trainer,
                           incumbent_artifact=incumbent_artifact)
    except ResourceDeferred:
        # Keep the same cycle/cadence and its completed phase receipts. A
        # temporary resource yield is not a failed quality observation.
        return {'status':'deferred_resource','next_due':now+5}
    except (ValueError, BlockingIOError) as exc:
        # The local trainer may have failed after a queued state was persisted.
        # Preserve it for the next explicit invocation; no retry loop here.
        safe = {'trainer_failed', 'trainer_timeout', 'trainer_memory_budget', 'stale_source',
                'stale_or_changed_queue', 'corpus_changed_during_cycle',
                'insufficient_separated_data', 'insufficient_none_cases',
                'insufficient_families', 'question_budget_exceeded'}
        reason = str(exc) if str(exc) in safe else 'cycle_unavailable'
        interval = 24
        _atomic_json(path, {'schema_version': 1, 'interval_hours': interval,
            'next_due': now + interval*3600, 'family_hashes': family_hashes,
            'revisions_sha256': revisions_hash, 'recent_failure': True,
            'last_run_at': now, 'last_cycle_id': cycle_id, 'last_reason': reason})
        return {'status': 'failed', 'reason': reason, 'next_due': now+interval*3600}
    score = result['incumbent_score']
    cadence = next_interval(previous['interval_hours'] if previous else 24, score,
        new_family=new_family, source_churn=source_churn,
        recent_failure=bool(previous and previous.get('recent_failure')))
    due = now + cadence['hours']*3600
    _atomic_json(path, {'schema_version': 1, 'interval_hours': cadence['hours'],
        'next_due': due, 'family_hashes': family_hashes, 'revisions_sha256': revisions_hash,
        'recent_failure': False, 'last_run_at': now, 'last_cycle_id': cycle_id, 'last_reason': cadence['reason'],
        'recent_unlearned_validation': score})
    return {'status': 'completed', 'cycle': result, 'cadence': cadence, 'next_due': due,
            'evidence': 'incumbent-on-fixed-validation-before-training'}

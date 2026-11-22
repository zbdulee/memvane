"""Invariants of a learning namespace, read from its database and files.

``check(root)`` returns the violations as short strings (an empty list when
the namespace is consistent). It only reads. Each machine's unit of work adds
its own rules here; the transition-table and crash tests call it after every
step.

Rules (design appendix B-3):

- I-1 a ``first_running`` case has a ledger row at ``reserved``,
  ``dispatch_unknown``, ``dispatched`` or ``answer_received``.
- I-2 a ``first_done`` case has a ledger row at ``answer_parked`` whose answer
  hash is the hash of the choice kept in the case.
- I-3 a ``gold`` case of the default teacher (``review: single_pass``) has a
  ledger row at ``committed``.
- G-1 one teacher call per (project, input): at most one ledger row of an
  input is at a stage other than ``pre_send_rejected``.
"""
from __future__ import annotations

import json
from pathlib import Path

from memvane.learning.context_learning import general_gold_ledger as ledger
from memvane.learning.context_learning import shared
from memvane.learning.context_learning.contracts import digest
from memvane.learning.context_learning.store import canonical

PHASE = 'first'
OPEN = ('reserved', 'dispatch_unknown', 'dispatched', 'answer_received')


def _json(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def check(root):
    """Violations of the namespace at ``root``; ``[]`` when none."""
    found = []
    if not (Path(root) / shared.DATABASE_NAME).is_file():
        return found
    with shared.read_only_validation(), shared._db(root) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'cases' not in tables:
            return found
        stages = {}
        if 'project_gold_attempts' in tables:
            for key, rid, stage, answer in db.execute(
                    'SELECT project_key,request_id,stage,answer_sha256 FROM project_gold_attempts WHERE phase=?',
                    (PHASE,)):
                stages[(key, rid)] = (stage, answer)
            repeated = db.execute(
                "SELECT project_key,input_sha256,COUNT(*) FROM project_gold_attempts "
                "WHERE stage!='pre_send_rejected' GROUP BY project_key,input_sha256 HAVING COUNT(*)>1").fetchall()
            found += ['G-1 input %s asked %d times' % (sha[:12], count) for _key, sha, count in repeated]
            unknown = [stage for stage, _answer in stages.values() if stage not in ledger.STAGES]
            found += ['M2 unknown stage %s' % stage for stage in unknown]
        for key, rid, status, first, gold in db.execute(
                'SELECT project_key,request_id,status,first,gold FROM cases'):
            stage, answer = stages.get((key, rid), (None, None))
            if status == 'first_running' and stage not in OPEN:
                found.append('I-1 %s first_running with ledger %s' % (rid, stage))
            elif status == 'first_done':
                kept = _json(first)
                choice = kept.get('choice') if isinstance(kept, dict) else None
                if stage != 'answer_parked':
                    found.append('I-2 %s first_done with ledger %s' % (rid, stage))
                elif choice is None or digest(canonical(choice)) != answer:
                    found.append('I-2 %s kept answer does not match the ledger' % rid)
            elif status == 'gold':
                value = _json(gold)
                if isinstance(value, dict) and value.get('review') == 'single_pass' and stage != 'committed':
                    found.append('I-3 %s gold with ledger %s' % (rid, stage))
    return found

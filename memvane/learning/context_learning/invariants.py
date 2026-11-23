"""Invariants of a learning namespace, read from its database and files.

``check(root)`` returns the violations as short strings (an empty list when
the namespace is consistent). It only reads. The transition-table and crash
tests call it after every step; ``project_counts(project)`` is the read-only
check of an installed project's namespace (the ``memvane:logs`` learning
check), which reports counts per rule and nothing else.

Rules (design appendix B-3):

- I-1 a ``first_running`` case has a ledger row at ``reserved``,
  ``dispatch_unknown``, ``dispatched`` or ``answer_received``.
- I-2 a ``first_done`` case has a ledger row at ``answer_parked`` whose answer
  hash is the hash of the choice kept in the case.
- I-3 a ``gold`` case of the default teacher (``review: single_pass``) has a
  ledger row at ``committed``.
- I-4 a ``staged`` or ``staged_evaluated`` cycle has a ``staged`` job, and its
  candidate artifact is the file its evidence names with the recorded hash.
- I-5 every consumed evidence key is a member of a ``staged`` job.
- I-6 a ``completed`` evaluation belongs to a ``staged_evaluated`` or ``applied`` cycle.
- I-7 an ``activated`` application journal: the active pointer is its new pointer and
  its cycle is ``applied`` (G-7: the pointer is the journal's old or new pointer). A
  cycle still ``staged_evaluated`` under such a journal is not a violation: the journal
  is written before the cycle row, and the next pass finishes the row.
- I-8 a ``blocked`` job or evaluation has a reason.
- I-9 at most one job of an (approval, cohort, family) is neither ``staged``
  nor ``blocked``.
- G-1 one teacher call per (project, input): at most one first-opinion ledger
  row of an input is at a stage other than ``pre_send_rejected``. (The retired
  two-opinion path kept a ``first`` and a ``second`` row of one input.)
"""
from __future__ import annotations

import re
import json
from pathlib import Path

from . import general_gold_ledger as ledger
from . import shared
from .contracts import digest
from .store import canonical

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
                "WHERE phase=? AND stage!='pre_send_rejected' "
                "GROUP BY project_key,input_sha256 HAVING COUNT(*)>1", (PHASE,)).fetchall()
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
        if 'learning_jobs' in tables:
            found += _jobs(db, tables)
    return found


def _jobs(db, tables):
    found, jobs = [], {}
    for job_id, approval, cohort, family, manifest, status, reason in db.execute(
            'SELECT job_id,approval,cohort,family,manifest,status,reason FROM learning_jobs'):
        value = _json(manifest)
        members = {m.get('key') for m in value.get('members', [])} if isinstance(value, dict) else set()
        jobs[job_id] = (status, members, (approval, cohort, family))
        if status == 'blocked' and not reason:
            found.append('I-8 job %s blocked without a reason' % job_id)
    open_keys = [key for status, _members, key in jobs.values() if status not in ('staged', 'blocked')]
    for key in sorted(set(open_keys)):
        if open_keys.count(key) > 1:
            found.append('I-9 %d open jobs of one approval, cohort and family' % open_keys.count(key))
    if 'learning_consumption' in tables:
        for key, job_id in db.execute('SELECT evidence_key,job_id FROM learning_consumption'):
            status, members, _ = jobs.get(job_id, (None, set(), None))
            if status != 'staged' or key not in members:
                found.append('I-5 consumed key of job %s (%s)' % (job_id, status))
    cycles = dict(db.execute('SELECT cycle_id,status FROM cycles'))
    if 'learning_evaluations' in tables:
        for cycle_id, status, reason in db.execute('SELECT cycle_id,status,reason FROM learning_evaluations'):
            if status == 'blocked' and not reason:
                found.append('I-8 evaluation of %s blocked without a reason' % cycle_id)
            if status == 'completed' and cycles.get(cycle_id) not in ('staged_evaluated', 'applied'):
                found.append('I-6 completed evaluation of a %s cycle %s' % (cycles.get(cycle_id), cycle_id))
    found += _application(Path(db.execute('PRAGMA database_list').fetchone()[2]).parent, cycles)
    for cycle_id, status, evidence in db.execute('SELECT cycle_id,status,evidence FROM cycles'):
        if status not in ('staged', 'staged_evaluated'):
            continue
        if jobs.get(cycle_id, (None,))[0] != 'staged':
            found.append('I-4 cycle %s %s with job %s' % (cycle_id, status, jobs.get(cycle_id, (None,))[0]))
        stage = _json(evidence) or {}
        path = Path(str(stage.get('artifactPath', '')))
        if not path.is_file() or shared._hash(path) != stage.get('artifactSha256'):
            found.append('I-4 cycle %s artifact differs from its evidence' % cycle_id)
    return found


def _application(root, cycles):
    from . import candidate_apply
    journal, pointer = root / candidate_apply.JOURNAL, root / 'active-checkpoint.json'
    if not journal.is_file() or not pointer.is_file():
        return []
    row, active = _json(journal.read_text()), _json(pointer.read_text())
    if not isinstance(row, dict):
        return ['I-7 unreadable application journal']
    found = []
    if row.get('phase') == 'rejected':
        # A closed journal (a refusal, an application undone since) says nothing about what
        # the pointer became afterwards: the pointer's own marker names the application
        # that wrote it, whose journal is this one or an archived one.
        marker = active.get('application') if isinstance(active, dict) else None
        if marker is not None:
            owner = None
            if isinstance(marker, dict) and re.fullmatch(r'[a-f0-9]{32}', str(marker.get('id', ''))):
                archived = root / candidate_apply.ARCHIVE / (marker['id'] + '.json')
                owner = (row if row.get('apply_id') == marker['id']
                         else _json(archived.read_text()) if archived.is_file() else None)
            if not isinstance(owner, dict) or owner.get('new_pointer') != active:
                found.append('G-7 the active pointer is not the pointer of the application it names')
    elif active not in (row.get('old_pointer'), row.get('new_pointer')):
        found.append('G-7 the active pointer is neither pointer of the application journal')
    if row.get('phase') == 'activated':
        if active != row.get('new_pointer'):
            found.append('I-7 activated application with another active pointer')
        if cycles.get(row.get('cycle_id')) not in ('applied', 'staged_evaluated'):
            found.append('I-7 activated application of a %s cycle' % cycles.get(row.get('cycle_id')))
    return found


def counts(root):
    """Violations of the namespace at ``root`` per rule: ``{'I-4': 2}``; ``{}`` when none."""
    found = {}
    for line in check(root):
        rule = line.split(' ', 1)[0]
        found[rule] = found.get(rule, 0) + 1
    return found


def project_counts(project):
    """The read-only check of ``project``'s learning namespace; never raises.

    ``{'status': 'checked', 'violations': n, 'rules': {rule: n}}``, or
    ``no_namespace`` (learning is not set up for the project), or
    ``unreadable`` with the error's type name.
    """
    try:
        from memvane.settings import api as settings
        project = Path(project).resolve(strict=True)
        root = shared._safe_root(settings.find_project_config(str(project))['config'], project)
        if root is None or not (Path(root) / shared.DATABASE_NAME).is_file():
            return {'status': 'no_namespace'}
        rules = counts(root)
        return {'status': 'checked', 'violations': sum(rules.values()), 'rules': dict(sorted(rules.items()))}
    except Exception as exc:  # A diagnostic: the answer names the failure and nothing else.
        return {'status': 'unreadable', 'error': type(exc).__name__}

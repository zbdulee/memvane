"""Cohort admission and train/validation split for the normal local worker.

Admission is the project cohort's (`general_cohort`). Every captured
candidate (including distractors) counts toward exposure, so a validation
case never shares a question, document, revision or body with training.
"""
from __future__ import annotations

from runtime_admission import prepared

import json

from .contracts import digest
from .store import canonical


def _exposure(row):
    """Question, edit family, and *all* candidate identities, independent of gold."""
    sample, bodies = json.loads(row[3]), json.loads(row[4])
    question = ' '.join(sample['prompt']['text'].casefold().split())
    keys = {'question:' + digest(question)}
    for candidate in sample['candidates']:
        cid = candidate['candidate_id']
        keys.update(('document:' + digest(canonical([row[0], cid])),
                     'revision:' + candidate['revision_sha256'],
                     'body:' + digest(bodies[cid])))
    if len(row) > 9:
        keys.update(key for key in json.loads(row[9])
                    if key.startswith(('target:', 'episode:')))
    if len(row) > 12:
        keys.add('target:' + digest(canonical([row[0],
                                               'edit:' + row[12]])))
    return keys


@prepared
def admitted(db, project, grant, statuses=('queued', 'first_done')):
    """Read-only admission; unsafe rows stay queued for explicit review."""
    from .project_scope import SCOPE
    if grant.get('scope') != SCOPE:
        raise ValueError('runtime_authorization_cohort_scope_changed')
    from .general_cohort import admitted as general_admitted
    return general_admitted(db, project, grant, statuses=statuses)


def gold_rows(db, project, grant):
    """All admitted gold cases of the project cohort."""
    return admitted(db, project, grant, statuses=('gold',))


def training_and_validation(db, project, grant):
    rows = gold_rows(db, project, grant)
    train, validation = [], []
    train_keys = set().union(*(_exposure(row) for role, row in rows
                               if role == 'train'))
    seen_validation = set()
    for role, row in rows:
        if role == 'train':
            train.append(row)
        else:
            keys = _exposure(row)
            if keys & (train_keys | seen_validation):
                continue
            validation.append(row)
            seen_validation.update(keys)
    return train, validation

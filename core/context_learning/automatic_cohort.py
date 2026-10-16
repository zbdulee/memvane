"""Versioned edit cohort admission for the normal synthetic worker.

The historical cases, splits, tickets and receipts remain untouched. The
authorization names fresh edit-path families, while every captured candidate
(including distractors) is screened against previously exposed evaluation.
"""
from __future__ import annotations

from runtime_admission import prepared,preparation_checkpoint

import json
from pathlib import Path

from .contracts import digest
from .store import canonical


def _fresh_candidate_sources(db, project, sample, cutoff, source_prefix,
                             edit_path, edit_sha, baseline_paths):
    """Require attested new revisions bound to this genuine host edit."""
    from .synthetic_scope import generated_path
    from .synthetic_experiment import story_for
    owner = str(Path(project).resolve())
    story = str(story_for(project).resolve())
    for candidate in sample['candidates']:
        try:
            path = str(generated_path(candidate['candidate_id']))
        except (KeyError, TypeError, ValueError):
            return False
        revision = candidate['revision_sha256']
        topical = db.execute(
            "SELECT source_snapshots FROM topical_worker_versions "
            "WHERE card_path=? "
            "AND card_sha256=? AND project_root=? AND story_root=? "
            "AND state='active' AND registered_at>=?",
            (path, revision, owner, story, cutoff)).fetchone()
        host = db.execute(
            "SELECT source_path,source_ticket_id,source_ticket_sha256 FROM "
            "source_card_versions WHERE card_path=? "
            "AND card_sha256=? AND project_root=? AND story_root=? "
            "AND registered_at>=?",
            (path, revision, owner, story, cutoff)).fetchone()
        if topical is None and host is None:
            return False
        if host is not None:
            if (not host[1] or not host[2] or host[0] in baseline_paths
                    or not host[0].startswith(source_prefix)):
                return False
        if topical is not None:
            try:
                sources = json.loads(topical[0])
                revisions = [item['revision'] for item in sources]
                valid = (isinstance(sources, list) and bool(sources)
                    and all(isinstance(row['path'], str)
                        and row['path'] not in baseline_paths
                        and row['path'] == edit_path
                        and row['sha256'] == edit_sha
                        and row['path'].startswith(source_prefix)
                        for row in revisions))
            except (KeyError, TypeError, ValueError):
                return False
            if not valid:
                return False
    return True


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


def _family(path, cohort):
    matches = [(role, prefix) for role, field in (
        ('train', 'trainEditPrefixes'), ('holdout', 'validationEditPrefixes'))
        for prefix in cohort[field] if path.startswith(prefix)]
    return matches[0] if len(matches) == 1 else None


def _rows(db, project, grant, statuses):
    """Return only new, one-use, exact edit-ticket cases in this owner."""
    cohort = grant['cohort']
    cutoff = cohort['notBeforeUnixMs'] / 1000
    baseline_paths = set(cohort['baselineRawPaths'])
    marks = ','.join('?' for _ in statuses)
    query = ("SELECT c.project_key,c.request_id,c.input_sha256,c.sample,c.bodies,"
             "c.gold,c.split,c.project_root,c.corpus_fingerprint,c.family_keys,"
             "c.trained_cycle,c.captured_at,t.relative_path,t.ticket_id,"
             "t.after_sha256 "
             "FROM cases c JOIN edit_tickets t ON t.request_id=c.request_id "
             "AND t.project_root=c.project_root AND t.input_sha256=c.input_sha256 "
             f"WHERE c.project_root=? AND c.status IN ({marks}) "
             "AND c.use_state='active' AND c.captured_at>=? AND t.created_at>=? "
             "ORDER BY c.captured_at,c.request_id")
    rows = db.execute(query, (str(Path(project).resolve()), *statuses,
                              cutoff, cutoff)).fetchall()
    result = []
    for row in rows:
        preparation_checkpoint()
        sample = json.loads(row[3])
        family = _family(row[12], cohort)
        if (sample.get('schema_version') != 4
                or sample.get('edit_event', {}).get('ticket_id') != row[13]
                or family is None
                or not _fresh_candidate_sources(db, project, sample, cutoff,
                                                family[1], row[12], row[14],
                                                baseline_paths)):
            continue
        result.append((family[0], row[:13]))
    return result


def _old_exposure(db, grant):
    # This includes sealed/fixed evaluation and old training pools. A newly
    # registered card is not fresh evidence if its bytes or source identity
    # were already exposed before the cutoff in any historical case.
    cutoff = grant['cohort']['notBeforeUnixMs'] / 1000
    rows = db.execute(
        "SELECT project_key,request_id,input_sha256,sample,bodies,"
        "gold,split,project_root,corpus_fingerprint,family_keys FROM cases "
        "WHERE captured_at<?",
        (cutoff,)).fetchall()
    return set().union(*(_exposure(row) for row in rows))


@prepared
def admitted(db, project, grant, statuses=('queued', 'first_done')):
    """Read-only admission; unsafe rows stay queued for explicit review."""
    from .project_scope import SCOPE
    if grant.get('scope') == SCOPE:
        from .general_cohort import admitted as general_admitted
        return general_admitted(db, project, grant, statuses=statuses)
    from .synthetic_experiment import story_for
    if (grant.get('projectRoot') != str(Path(project).resolve())
            or grant.get('storyRoot') != str(story_for(project).resolve())):
        raise ValueError('runtime_authorization_cohort_scope_changed')
    old_eval = _old_exposure(db, grant)
    rows = _rows(db, project, grant, statuses)
    accepted = []
    # A validation family already exposed in this cohort cannot later become
    # training, even if the legacy provisional split says train.
    exposed = _rows(db, project, grant, ('awaiting_local', 'queued',
        'first_running', 'first_done', 'second_running', 'gold',
        'pending_review', 'failed'))
    validation = set().union(*(_exposure(row) for role, row in exposed
                               if role == 'holdout'))
    seen_train = set()
    for role, row in rows:
        keys = _exposure(row)
        if keys & old_eval or (role == 'train' and keys & validation):
            continue
        if role == 'holdout' and keys & seen_train:
            continue
        accepted.append((role, row))
        if role == 'train':
            seen_train.update(keys)
    return accepted


def gold_rows(db, project, grant):
    """All exact new gold cases, split by the frozen edit-path families."""
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

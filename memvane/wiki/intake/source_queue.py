"""Source job admission into the ledger queue (``compile_jobs``).

Planning stays in ``wiki_source_intent``: a candidate whose operation already
has one queued row with the same revision is not added again, and a newer
observation of an operation replaces its queued rows, carrying an unfinished
extraction forward (``merge_source_job``). The read, plan and write happen in
one ledger transaction.
"""
from __future__ import annotations

from memvane.wiki.intake import wiki_source_intent as intent


def queued_revisions(rows, keys=None):
    groups = {}
    for item in rows:
        key = intent.operation_key(item['payload'])
        if key is not None and (keys is None or key in keys):
            groups.setdefault(key, []).append(item)
    return groups, {key: intent.queue_revision(group[0]['payload'])
                    for key, group in groups.items() if len(group) == 1}


def enqueue(queue, candidates, cap):
    """Admit ``(collection, rel, row)`` candidates; returns the ``plan_source_batch`` plan."""
    from pathlib import Path
    from memvane.wiki.pipeline import api as pipeline
    keys = {intent.operation_key(row) for _, _, row in candidates}

    def planner(rows):
        groups, revisions = queued_revisions(rows, keys)
        plan = intent.plan_source_batch(candidates, revisions, cap)
        delete, records = [], []
        for row in plan['records']:
            group = groups.get(intent.operation_key(row), ())
            merged = intent.merge_source_job(row, (item['payload'] for item in group))
            # The newest observation takes the first replaced row's place in the queue.
            records.append((merged, group[0]['jobId']) if group else merged)
            delete.extend(item['jobId'] for item in group)
        return delete, records, plan

    directory = Path(queue).parent
    directory.mkdir(parents=True, exist_ok=True)
    with pipeline.open_pipeline_dir(directory) as store:
        return store.plan_jobs('source', planner)

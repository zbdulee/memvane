"""Pure source-intent planning for the wiki compile source queue.

`wiki_compile_enqueue.enqueue_changed_sources` used to inline four decisions:
which trigger grants it accepts, which label it records, which changed paths it
looks at and in what order, and how the per-wake cap splits picked records from
deferred files. They live here as functions of their arguments only: no
filesystem, queue, clock, environment or config import. Gating, per-file
eligibility (`_source_record`), the cap value (`QMD_SYNC_COMPILE_MAX`) and the
queue append stay with the enqueue module. Manual sync now attaches a content
revision there; other row fields and trigger labels remain unchanged.

Live rows for the same source, trigger, route and operation are coalesced.
Hashed sync rows name a content revision. PostToolUse rows carry the latest
pending refresh event and the worker reads its source on claim.
"""

from itertools import chain
import json

# 큐 행에 원문 revision을 남기는 필드. hash가 있는 경로의 행에만 붙는다.
REVISION_FIELD = "contentRevision"

# 편집 훅 경로의 trigger 라벨.
from memvane.wiki.intake.types import POST_TOOL_TRIGGER
# sync 경유 compile enqueue의 trigger 라벨(스냅샷 diff가 출처).
SYNC_TRIGGER = "post_sync_source"
# SessionStart sync 경유 라벨. 이 경로는 자기 grant만 인정한다 — `post_tool_source`
# 동의를 SessionStart 자동 컴파일로 넓히지 않는다.
from memvane.wiki.intake.types import SESSION_START_TRIGGER, SESSION_START_ACCEPTED_TRIGGERS
# 수동 sync는 `post_tool_source`만 켜 둔 프로젝트도 동등 grant로 인정한다
# (근거는 wiki_compile_enqueue의 COMPILE_ACCEPTED_TRIGGERS 주석).
SYNC_ACCEPTED_TRIGGERS = (SYNC_TRIGGER, POST_TOOL_TRIGGER)


def trigger_policy(session_start):
    """`(accepted_triggers, record_trigger)` for one changed-source enqueue."""
    if session_start:
        return SESSION_START_ACCEPTED_TRIGGERS, SESSION_START_TRIGGER
    return SYNC_ACCEPTED_TRIGGERS, SYNC_TRIGGER


def changed_source_paths(changed_files):
    """Yield `(collection, root, rel)` for created/updated paths in cap order.

    Collections are sorted by name and paths by relative path, with a path that
    is both created and updated seen once. Deleted paths are never yielded:
    there is no source to read, and card cleanup is not the compile queue's job.
    The cap consumes candidates in exactly this order, so changing it changes
    which files are deferred.
    """
    for name in sorted(changed_files):
        entry = changed_files[name]
        for rel in sorted(set(entry["created"]) | set(entry["updated"])):
            yield name, entry["root"], rel


def source_revision(changed_files, name, rel):
    """The scanned sha256 of `rel` in collection `name`, or None when unhashed."""
    revisions = changed_files[name].get("revisions")
    revision = revisions.get(rel) if isinstance(revisions, dict) else None
    return revision if isinstance(revision, str) and revision else None


def revision_key(record):
    """`(collection, path, revision, trigger)` of one queue row, or None.

    The queue file is already project-owned, so neither `cwd` nor any scope
    hash is part of the key. The trigger is, so a SessionStart row never
    absorbs a manual-sync or PostToolUse intent. A row without a revision has
    no key and is never treated as a duplicate.
    """
    key = source_key(record)
    if key is None:
        return None
    revision = record.get(REVISION_FIELD)
    if not isinstance(revision, str) or not revision:
        return None
    return (*key[:2], revision, *key[2:])


def source_key(record):
    """One source operation; delete, forced refresh, shared wait and backfill stay distinct."""
    if not isinstance(record, dict) or not isinstance(record.get("source"), dict):
        return None
    source = record["source"]
    key = (source.get("collection"), source.get("path"), record.get("trigger", POST_TOOL_TRIGGER))
    if not all(isinstance(value, str) and value for value in key):
        return None
    if record.get('sourceDelete') is True:
        key += ('delete',)
    if record.get('forceRecompile') is True:
        key += ('force',)
    if record.get('sharedEnqueued') is True:
        key += ('shared',)
    if isinstance(record.get('initialBackfill'), str):
        key += ('initial-backfill', record['initialBackfill'])
    return key


def live_key(record):
    """Coalesce only rows owned by the same installation within the project."""
    key = source_key(record)
    if key is None or 'selectedRoute' not in record:
        return key
    route = record['selectedRoute']
    if not isinstance(route, dict) or set(route) not in (
            {'installId', 'scopeId'}, {'installId', 'scopeId', 'storeIncarnation'}):
        return None
    if not all(isinstance(value, str) and value for value in route.values()):
        return None
    return (*key, tuple(sorted(route.items())))


def merge_source_job(latest, others):
    """Carry an unfinished extraction across observations of the same live operation."""
    if not isinstance(latest, dict):
        return latest
    revisions = set()
    for row in chain((latest,), others):
        if row.get('partialCompile') is True:
            sha = row.get('_processedContentRevision')
            revisions.add(sha if isinstance(sha, str) else None)
    if not revisions:
        return latest
    merged = dict(latest, partialCompile=True)
    # A queue observation can be stale. Only the worker's next stable read can dismiss the retry.
    if len(revisions) == 1 and None not in revisions:
        merged['_processedContentRevision'] = next(iter(revisions))
    else:
        # Competing partial revisions or a legacy marker: retry the current contents conservatively.
        merged.pop('_processedContentRevision', None)
    return latest if merged == latest else merged


def coalesce_claimed_jobs(rows):
    """Keep the newest observation and unfinished work per installation and source operation."""
    from memvane.wiki.workspace.api import parse_ts
    groups = {}
    for index, (raw, job) in enumerate(rows):
        if job is not None:
            key = operation_key(job)
            groups.setdefault(index if key is None else key, []).append((raw, job))
    kept, dropped = [], []
    for group in groups.values():
        # An equal timestamp means the later queue row is the newer observation.
        newest = max(range(len(group)), key=lambda i: (parse_ts(group[i][1].get('ts')) or 0.0, i))
        raw, latest = group[newest]
        others = [item for i, item in enumerate(group) if i != newest]
        merged = merge_source_job(latest, (job for _, job in others))
        kept.append((raw if merged is latest else json.dumps(merged, ensure_ascii=False), merged))
        dropped.extend(line for line, _ in others)
    return kept, dropped


def queue_revision(record):
    """The replay token already carried by this producer's row."""
    if not isinstance(record, dict):
        return None
    revision = record.get(REVISION_FIELD)
    pending = record.get('pendingRefresh')
    event = pending.get('eventId') if isinstance(pending, dict) else None
    if (isinstance(revision, str) and revision) or (isinstance(event, str) and event):
        return (revision, event)
    return None


def operation_key(row):
    """Force has no parent operation ID; replay of a frozen/delete/wait job needs its token."""
    key = live_key(row)
    if key is None or row.get('forceRecompile') is True:
        return None
    if (row.get('sourceDelete') is True or row.get('sharedEnqueued') is True
            or isinstance(row.get('initialBackfill'), str)):
        token = queue_revision(row)
        if token is None or any(value is not None and not isinstance(value, str) for value in token):
            return None
        return (*key, token)
    return key


def plan_source_batch(candidates, revisions, cap):
    """Each input still uses its source cap slot and keeps its original deferred filename."""
    candidates = list(candidates)
    observations = {}
    for index, (_, _, row) in enumerate(candidates[:cap]):
        key = operation_key(row)
        if key is not None:
            observations.setdefault(key, []).append(index)
    for indices in observations.values():
        if len(indices) > 1:
            latest = candidates[indices[-1]][2]
            merged = merge_source_job(latest, (candidates[i][2] for i in indices[:-1]))
            for i in indices:
                name, rel, _ = candidates[i]
                candidates[i] = (name, rel, merged)

    fresh = without_queued(candidates, revisions)
    skipped = len(candidates) - len(fresh)
    plan = plan_enqueue(fresh, max(0, cap - skipped))
    # References shared by normalization represent a single admitted operation.
    # Independent inputs (in particular identical force rows) keep every occurrence.
    seen = set()
    records = []
    for row in plan['records']:
        key = operation_key(row)
        if key is None or key not in seen:
            records.append(row)
        seen.add(key)
    plan['records'] = records
    plan['result']['queued'] = len(records)
    if skipped:
        plan['result']['wakePending'] = True
    return plan


def without_queued(candidates, queued_revisions):
    """A single live copy of this revision already uses a slot in the wake."""
    fresh = []
    for candidate in candidates:
        row = candidate[2]
        token = queue_revision(row)
        if token is None or row.get('partialCompile') is True or queued_revisions.get(operation_key(row)) != token:
            fresh.append(candidate)
    return fresh


def enqueue_result(reason, queued=0, deferred_files=()):
    """The `{queued, deferred, deferredFiles, reason}` shape callers consume."""
    deferred_files = list(deferred_files)
    return {
        "queued": queued,
        "deferred": len(deferred_files),
        "deferredFiles": deferred_files,
        "reason": reason,
    }


def plan_enqueue(candidates, cap):
    """Split eligible `(collection, rel, record)` candidates by the per-wake cap.

    Returns `{"records": [...], "result": {...}}`. `records` are the queue rows
    to append, in candidate order and unmodified. Deferred files are reported as
    `(collection, rel)` so the caller can keep their snapshot entries from
    advancing; nothing is dropped silently. A cap of 0 defers everything and
    still reports `capped`.
    """
    candidates = list(candidates)
    if not candidates:
        return {"records": [], "result": enqueue_result("no_sources")}
    picked = candidates[:cap]
    deferred = [(name, rel) for name, rel, _ in candidates[cap:]]
    return {
        "records": [record for _, _, record in picked],
        "result": enqueue_result("capped" if deferred else "queued", len(picked), deferred),
    }


def intent_identity(record):
    """Per-run duplicate key of one queue row: `(cwd, path, collection)`.

    This is the key the PostToolUse hook already used to collapse repeated paths
    in one payload. It deliberately has no trigger or revision, so it does not
    merge rows across runs or callers.
    """
    source = record["source"]
    return (record["cwd"], source["path"], source["collection"])

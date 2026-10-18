"""Pure source-intent planning for the wiki compile source queue.

`wiki_compile_enqueue.enqueue_changed_sources` used to inline four decisions:
which trigger grants it accepts, which label it records, which changed paths it
looks at and in what order, and how the per-wake cap splits picked records from
deferred files. They live here as functions of their arguments only: no
filesystem, queue, clock, environment or config import. Gating, per-file
eligibility (`_source_record`), the cap value (`QMD_SYNC_COMPILE_MAX`) and the
queue append stay with the enqueue module, so queue bytes are unchanged.

There is no content revision here yet. `changedFiles` carries only paths, so an
idempotence key that includes a revision needs a new input from the caller and
is not part of this boundary.
"""

# 편집 훅 경로의 trigger 라벨.
POST_TOOL_TRIGGER = "post_tool_source"
# sync 경유 compile enqueue의 trigger 라벨(스냅샷 diff가 출처).
SYNC_TRIGGER = "post_sync_source"
# SessionStart sync 경유 라벨. 이 경로는 자기 grant만 인정한다 — `post_tool_source`
# 동의를 SessionStart 자동 컴파일로 넓히지 않는다.
SESSION_START_TRIGGER = "session_start_source"
# 수동 sync는 `post_tool_source`만 켜 둔 프로젝트도 동등 grant로 인정한다
# (근거는 wiki_compile_enqueue의 COMPILE_ACCEPTED_TRIGGERS 주석).
SYNC_ACCEPTED_TRIGGERS = (SYNC_TRIGGER, POST_TOOL_TRIGGER)
SESSION_START_ACCEPTED_TRIGGERS = (SESSION_START_TRIGGER,)


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

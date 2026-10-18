"""Pure file-change planning, plus the one ordered effect sequence around it.

The planning functions read only the values they are given: no filesystem,
queue, QMD index, clock or environment. `apply_change_set` owns only the order
of effects; each effect is an adapter supplied by the caller. Scanning, scope
re-checks, status files and backend kicks stay with the caller. Snapshot and
queue formats are the callers' existing ones; nothing here defines a schema.
"""


def compare_collection(previous_files, current_files):
    """변경을 컬렉션 루트 기준 상대경로 **목록**으로 반환한다.

    예전에는 카운트만 반환했지만 compile source-queue enqueue가 파일 단위 diff를
    필요로 한다(dirty-queue는 여전히 컬렉션 단위). 호출부는 len()으로 카운트를 얻는다.
    """
    created, updated, deleted = [], [], []
    previous_files = previous_files or {}
    current_files = current_files or {}
    for rel, meta in current_files.items():
        old = previous_files.get(rel)
        if old is None:
            created.append(rel)
        elif (old.get("mtimeNs") != meta.get("mtimeNs") or old.get("size") != meta.get("size")
              or old.get('sha256') != meta.get('sha256')):
            updated.append(rel)
    for rel in previous_files:
        if rel not in current_files:
            deleted.append(rel)
    return sorted(created), sorted(updated), sorted(deleted)


def plan_collection(previous_files, scanned_files, unknown=()):
    """Pure file-sync plan: preserve unreadable prior entries, then diff.

    Callers own scanning, enqueue, snapshot persistence, and ACK. Keeping this
    comparison separate makes a scan error distinct from a confirmed delete.
    """
    previous = previous_files or {}
    files = dict(scanned_files)
    for rel, meta in previous.items():
        if any(rel == path or rel.startswith(path + '/') for path in unknown):
            files[rel] = meta
    created, updated, deleted = compare_collection(previous, files)
    return files, created, updated, deleted


def plan_change_set(entries, is_indexed=None):
    """Combine per-collection plans into one change set without side effects.

    `entries` yields `(name, root, previous_files, scanned_files, unknown)`.
    Input order is kept in `changedFiles` because the wiki adapter applies its
    per-wake cap in that order. `indexTargets` maps changed collections that
    `is_indexed(name)` accepts (every changed one when None) to their root;
    `changedFiles` keeps every changed collection, including `source` roles
    that the wiki reads but QMD does not index.
    """
    collections, changed_files, index_targets = {}, {}, {}
    totals = {"created": 0, "updated": 0, "deleted": 0}
    for name, root, previous_files, scanned_files, unknown in entries:
        files, created, updated, deleted = plan_collection(
            previous_files, scanned_files, unknown)
        totals["created"] += len(created)
        totals["updated"] += len(updated)
        totals["deleted"] += len(deleted)
        if created or updated or deleted:
            changed_files[name] = {"root": root, "created": created,
                                   "updated": updated, "deleted": deleted}
            if is_indexed is None or is_indexed(name):
                index_targets[name] = root
        collections[name] = {"root": root, "files": files}
    return {"collections": collections, "totals": totals,
            "changedFiles": changed_files, "indexTargets": index_targets}


def defer_snapshot_entries(snapshot, previous, deferred):
    """상한으로 미룬 파일은 스냅샷을 진전시키지 않아 다음 sync가 다시 집는다."""
    previous_collections = (previous or {}).get("collections", {})
    for name, rel in deferred:
        files = snapshot.get("collections", {}).get(name, {}).get("files")
        if not isinstance(files, dict):
            continue
        old = previous_collections.get(name, {}).get("files", {}).get(rel)
        if isinstance(old, dict):
            files[rel] = old
        else:
            files.pop(rel, None)


def apply_change_set(index_targets, changed_files, snapshot, previous, *,
                     enqueue_index, enqueue_wiki, persist, ack=None):
    """Run one plan's effects in the order its retry contract depends on.

    index enqueue -> wiki enqueue -> roll wiki-deferred entries back to
    `previous` -> persist `snapshot` -> `ack(compiled)`. A failure raises before
    persistence, so the snapshot does not advance and the next run sees the
    same change again (duplicate enqueue is possible; loss is not). ACK never
    precedes persistence. `enqueue_index=None` skips index work while the
    plan still reports its targets. `enqueue_wiki` is always called and must
    return the wiki owner's `{queued, deferred, deferredFiles, reason}` result.
    """
    if index_targets and enqueue_index is not None:
        enqueue_index(index_targets)
    compiled = enqueue_wiki(changed_files)
    defer_snapshot_entries(snapshot, previous, compiled["deferredFiles"])
    persist(snapshot)
    if ack is not None:
        ack(compiled)
    return compiled

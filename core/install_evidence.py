"""Pure evidence decisions for the preserving shared install.

Every function here takes already-read values and returns a decision or raises
the same ValueError reason shared_install used inline. No file, DB, process,
QMD, clock or journal access: shared_install keeps journal format, approval,
deadline, queue intent/ACK ordering and every side effect.
"""

SEARCH_URI_PREFIX = 'qmd://'


def indexed_names(collections):
    """Indexed collection names in request order (vector audit/count scope)."""
    return [name for name, owner in collections.items() if owner['indexed']]


def indexed_paths(collections):
    """{name: path} for indexed collections; the vector-repair enqueue target."""
    return {name: owner['path'] for name, owner in collections.items() if owner['indexed']}


def file_identity(info):
    """Stable-window identity of one source file from a stat result.

    mtime/ctime stay in the tuple: content can go A→B→A after a worker indexed
    B, and only identity shows the supposedly stable window changed.
    """
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def same_source_identity(before, after):
    """Whether a later stat (fd or path) still matches the pre-hash stat.

    Any identity field differing, ctime alone included, means the file was
    touched while hashing. The caller decides what to raise.
    """
    return file_identity(before) == file_identity(after)


def source_entry(sha256, before):
    """Snapshot entry of one hashed source file; identity is the pre-hash stat."""
    return {'sha256': sha256, 'identity': list(file_identity(before))}


def source_queue_names(previous, current, intent, owners):
    """Collections whose source evidence must be (re)published to the queue.

    previous: last acknowledged snapshot; current: fresh snapshot; intent: an
    unacknowledged queue intent (re-published on resume even if the snapshot
    now matches). Returns a sorted list (empty = nothing to publish). Raises
    before the caller persists anything when the intent names a collection
    that is no longer in the current indexed scope.
    """
    names = {name for name in current if previous.get(name) != current[name]}
    names |= set(intent.get('collections', []))
    if not names:
        return []
    if any(name not in current or not owners[name]['indexed'] for name in names):
        raise ValueError('invalid_install_source_queue_intent')
    return sorted(names)


def vector_scope(proof, collections, model):
    """(names, model, fingerprint) proved by the store bootstrap proof.

    Exactly one model fingerprint is accepted and it must be the approved
    request model path; anything else is unproved, never guessed.
    """
    fingerprints = proof['modelFingerprints']
    if len(fingerprints) != 1 or fingerprints[0]['model'] != model['path']:
        raise ValueError('vector_model_identity_unproved')
    return indexed_names(collections), fingerprints[0]['model'], fingerprints[0]['fingerprint']


def vectors_complete(report):
    """True when a physical vector audit report lists no defects."""
    return not report['defects']


def require_complete_vectors(report, reason):
    """Return the report, or raise ValueError(reason) when it lists defects.

    The reason stays caller-specific: still_incomplete after repair,
    incomplete before readiness, changed_during_readiness after it.
    """
    if report['defects']:
        raise ValueError(reason)
    return report


def vector_repair_intent(generation, model, fingerprint, report):
    """Journal 'intent' entry persisted before invalidating defective vectors."""
    return {'phase': 'intent', 'store': generation, 'model': model, 'fingerprint': fingerprint,
            'hashes': [v['hash'] for v in report['defects']]}


def scoped_search_count(name, hits):
    """Hit count of a collection-scoped readiness search.

    Zero hits, or any hit outside the collection URI, is not readiness
    evidence (a global fallback must not prove scoped search).
    """
    prefix = SEARCH_URI_PREFIX + name + '/'
    if not hits or any(not h.get('file', '').startswith(prefix) for h in hits):
        raise ValueError('install_scoped_search_not_ready:' + name)
    return len(hits)

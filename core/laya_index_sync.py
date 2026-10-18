"""Selected Laya Index full-scan writer for one explicitly bound source scope.

The source is one v1 root string or a runtime-validated Mapping of disjoint
collection roots; either way each publish is one full-membership generation.

The caller first verifies the installer-owned selected route and rechecks it
after this call. This module never selects a project, migrates QMD, invokes a
model, or deletes index data. Separate index/queue DBs offer receipt-first
redelivery, not a power-loss atomic transaction.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import uuid

from laya_index_a import IndexContractError
from laya_index_flow import process_one
from laya_index_runtime import LayaIndexRuntime
from laya_index_source_port import scan, scan_collections
from laya_index_vector import (qmd_markdown_title as _qmd_markdown_title,
                               source_evidence as _source_evidence,
                               profile_identity, LayaVectorIndex)


def _scan(root, scope, index):
    """v1 string roots keep the original scan; Mapping roots are runtime-validated."""
    if isinstance(root, Mapping):
        return scan_collections(root, scope, index)
    return scan(root, scope, index)


def prepare_candidate_batch_off_hook(runtime, provider):
    """Build a complete candidate vector batch with an explicitly supplied provider.

    This loads the provider and belongs only in a separately admitted owner
    worker. It grants no product approval and is never called by a host hook.
    Duplicate content shares a vector only when the QMD-derived title also
    matches. Different titles receive separate immutable chunk identities.
    """
    if (not isinstance(runtime, LayaIndexRuntime)
            or not callable(getattr(provider, 'embed_document_chunks', None))
            or not isinstance(getattr(provider, 'profile', None), dict)
            or getattr(provider, 'manifest', {}).get('recipeId')
               != 'qmd-regex-token-chunks-candidate-v1'):
        raise IndexContractError('candidate_provider_required')
    source = _scan(runtime.source_root, runtime.scope, runtime.index)
    unique = {}
    for file in source.files:
        if not file.eligible:
            continue
        title = _qmd_markdown_title(file.text, file.path)
        unique.setdefault((file.sha256, title), file)
    profile = provider.profile
    profile_id = profile_identity(profile['model_id'],
                                  profile['embedding_fingerprint'],
                                  profile['dimension'])
    vectors = LayaVectorIndex(runtime.index)
    chunks = []
    for (hash_, title), file in sorted(unique.items()):
        # An immutable (source,title,reviewed profile) set is reusable only
        # after its completeness/provenance seal is verified. This avoids a
        # nondeterministic second inference wedging a durable retry.
        rows = vectors.cached_chunk_set(hash_, title, profile_id)
        if rows is None:
            rows = provider.embed_document_chunks(file.text, path=file.path)
        if not isinstance(rows, list) or not rows:
            raise IndexContractError('vector_coverage_incomplete')
        for row in rows:
            if not isinstance(row, dict) or set(row) != {'seq', 'pos', 'vector'}:
                raise IndexContractError('invalid_vector_batch')
            chunks.append({'hash': hash_, 'title': title, **row})
    if _scan(runtime.source_root, runtime.scope, runtime.index).manifest != source.manifest:
        raise IndexContractError('index_stale')
    batch = {'scope': runtime.scope, 'source_manifest': source.manifest,
             'model_id': profile['model_id'],
             'embedding_fingerprint': profile['embedding_fingerprint'],
             'dimension': profile['dimension'], 'chunks': chunks}
    _validated_batch(source, batch)
    return batch


def _validated_batch(source, batch):
    """Validate precomputed data before any queue/index mutation."""
    from laya_index_vector import MAX_CANDIDATES, _blob, profile_identity
    if (not isinstance(batch, dict) or set(batch) !=
            {'scope', 'source_manifest', 'model_id', 'embedding_fingerprint',
             'dimension', 'chunks'} or batch['scope'] != source.scope
            or batch['source_manifest'] != source.manifest
            or not isinstance(batch['chunks'], list)
            or len(batch['chunks']) > MAX_CANDIDATES):
        raise IndexContractError('vector_batch_stale')
    profile_id = profile_identity(batch['model_id'],
                                  batch['embedding_fingerprint'], batch['dimension'])
    eligible = {(f.sha256, _qmd_markdown_title(f.text, f.path))
                for f in source.files if f.eligible}
    seen = set()
    covered = set()
    grouped = {}
    chunks = []
    for item in batch['chunks']:
        if (not isinstance(item, dict) or set(item) != {'hash', 'title', 'seq', 'pos', 'vector'}
                or not isinstance(item['hash'], str) or not isinstance(item['title'], str)
                or (item['hash'], item['title']) not in eligible
                or type(item['seq']) is not int
                or item['seq'] < 0 or type(item['pos']) is not int or item['pos'] < 0
                or (item['hash'], item['title'], item['seq']) in seen):
            raise IndexContractError('invalid_vector_batch')
        _blob(item['vector'], batch['dimension'])
        seen.add((item['hash'], item['title'], item['seq']))
        covered.add((item['hash'], item['title']))
        grouped.setdefault((item['hash'], item['title']), []).append(
            (item['seq'], item['pos']))
        chunks.append((item['hash'], item['title'], item['seq'], item['pos'],
                       item['vector'], _source_evidence(item['hash'], item['title'])))
    if (covered != eligible or any(
            [seq for seq, _ in sorted(rows)] != list(range(len(rows)))
            or any(right <= left for (_, left), (_, right) in
                   zip(sorted(rows), sorted(rows)[1:]))
            for rows in grouped.values()) or
            sum(len(grouped[(f.sha256, _qmd_markdown_title(f.text, f.path))])
                for f in source.files if f.eligible) > MAX_CANDIDATES):
        raise IndexContractError('vector_coverage_incomplete')
    return profile_id, chunks


def _prepare_vector(index, source, job, batch):
    from laya_index_vector import LayaVectorIndex
    if source.manifest != job['source_manifest']:
        raise IndexContractError('index_stale')
    profile_id, chunks = _validated_batch(source, batch)
    vectors = LayaVectorIndex(index)
    registered = vectors.register_profile(batch['model_id'],
                                          batch['embedding_fingerprint'],
                                          batch['dimension'])
    if registered != profile_id:
        raise IndexContractError('vector_profile_mismatch')
    grouped = {}
    for hash_, title, seq, pos, values, _evidence in chunks:
        grouped.setdefault((hash_, title), []).append(
            {'seq': seq, 'pos': pos, 'vector': values})
    for (hash_, title), rows in sorted(grouped.items()):
        vectors.insert_chunk_set(hash_, title, profile_id,
                                 sorted(rows, key=lambda row: row['seq']))
    vectors.seal_generation(job['gen'], profile_id)


def _verify_vector_head(index, gen, profile_id):
    from laya_index_vector import verify_generation_seal
    index.db.execute('BEGIN')
    try:
        proof = verify_generation_seal(index, gen)
        index.db.commit()
    except BaseException:
        index.db.rollback()
        raise
    if proof['profile_id'] != profile_id:
        raise IndexContractError('vector_profile_mismatch')


def _file_identity(path: Path):
    try:
        info = path.lstat()
    except OSError as exc:
        raise IndexContractError('sync_owner_lock_unavailable') from exc
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_mode & 0o022):
        raise IndexContractError('sync_owner_lock_unavailable')
    return info.st_dev, info.st_ino


@contextmanager
def _owner_lock(queue_path: Path):
    """A held kernel lock proves a previous participating lease owner ended.

    All selected sync writers use this exact queue-adjacent file from before
    taking a claim lease until after receipt/ACK or retry release. The file is
    never unlinked, including on failure; unlinking would split the lock domain.
    """
    if not queue_path.is_absolute() or queue_path.is_symlink():
        raise IndexContractError('sync_owner_lock_unavailable')
    queue_identity = _file_identity(queue_path)
    lock_path = Path(str(queue_path) + '.sync.lock')
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
    flags |= getattr(os, 'O_CLOEXEC', 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise IndexContractError('sync_owner_lock_unavailable') from exc
    try:
        opened = os.fstat(fd)
        if (_file_identity(lock_path) != (opened.st_dev, opened.st_ino)
                or stat.S_IMODE(opened.st_mode) != 0o600):
            raise IndexContractError('sync_owner_lock_unavailable')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        if (_file_identity(lock_path) != (opened.st_dev, opened.st_ino)
                or _file_identity(queue_path) != queue_identity):
            raise IndexContractError('sync_owner_lock_unavailable')
        yield True
        if (_file_identity(lock_path) != (opened.st_dev, opened.st_ino)
                or _file_identity(queue_path) != queue_identity):
            raise IndexContractError('sync_owner_lock_unavailable')
    finally:
        os.close(fd)


def _head(runtime: LayaIndexRuntime):
    """Read current head, seal and revocation epoch in one index snapshot."""
    index = runtime.index
    index.db.execute('BEGIN')
    try:
        index._check_header(index.db)
        row = index.db.execute('''SELECT h.gen,s.source_manifest,g.rev_epoch,
                g.scope,g.state
            FROM head h LEFT JOIN generations g ON g.gen=h.gen
            LEFT JOIN candidate_seals s ON s.gen=h.gen
            WHERE h.scope=?''', (runtime.scope,)).fetchone()
        epoch = index.db.execute('SELECT COUNT(*) FROM exclusions WHERE scope=?',
                                 (runtime.scope,)).fetchone()[0]
        index.db.commit()
    except BaseException:
        index.db.rollback()
        raise
    if row is not None:
        if row[3:] != (runtime.scope, 'published'):
            raise IndexContractError('invalid_head')
        if row[1] is None:
            raise IndexContractError('unsealed_view')
    return row, epoch


def _result(state, generation=None, claim=None, reason=None):
    answer = {'state': state, 'generation': generation, 'claim': claim}
    if reason is not None:
        answer['reason'] = reason
    return answer


def sync_full(runtime: LayaIndexRuntime, *, before_publish,
              vector_batch=None, require_vector=True) -> dict:
    """Drain retained claims, then publish one complete fresh source snapshot.

    `deferred` is a fail-closed retry result, including owner contention. The
    caller may report it as a retry but must not claim a completed update.
    """
    if not isinstance(runtime, LayaIndexRuntime):
        raise TypeError('explicit_runtime_required')
    if not callable(before_publish):
        raise TypeError('before_publish_required')
    if type(require_vector) is not bool or (not require_vector and vector_batch is not None):
        raise TypeError('invalid_vector_requirement')
    if require_vector and vector_batch is None:
        return _result('deferred', reason='vector_provider_unavailable')
    with _owner_lock(runtime.queue.path) as owned:
        if not owned:
            return _result('deferred', reason='owner_busy')
        if runtime.queue.binding() != runtime.index.incarnation():
            raise IndexContractError('queue_index_binding_mismatch')
        if require_vector:
            _validated_batch(_scan(runtime.source_root, runtime.scope, runtime.index),
                             vector_batch)
        active = runtime.queue.active_for_scope(runtime.scope)
        backlog = len(active) > 64
        completed = None
        fenced = False
        def ensure_fenced():
            nonlocal fenced
            if not fenced:
                # One durable forward-only intent covers all writes made while
                # this invocation owns the same queue lock.
                before_publish()
                fenced = True
        for job in active[:64]:
            # A marker is conservative even if receipt replay only ACKs.
            ensure_fenced()
            outcome = process_one(runtime.index, runtime.queue, runtime.source_root,
                                  job['claim'], recover=job['state'] == 'leased',
                                  require_vector=require_vector,
                                  prepare_vector=(lambda index, source, job:
                                      _prepare_vector(index, source, job, vector_batch))
                                  if require_vector else None)
            if outcome == 'ack':
                completed = job
            elif outcome != 'replan':
                return _result('deferred', job['gen'], job['claim'], outcome)
        if backlog:
            return _result('deferred', completed['gen'] if completed else None,
                           completed['claim'] if completed else None, 'claim_backlog')
        source = _scan(runtime.source_root, runtime.scope, runtime.index)
        profile_id = None
        if require_vector:
            profile_id, _ = _validated_batch(source, vector_batch)
        head, epoch = _head(runtime)
        if head is not None and head[1] == source.manifest and head[2] == epoch:
            if require_vector:
                _verify_vector_head(runtime.index, head[0], profile_id)
            return (_result('indexed', completed['gen'], completed['claim'])
                    if completed and head[0] == completed['gen']
                    else _result('unchanged', head[0],
                                 reason='intent_fenced' if fenced else None))
        ensure_fenced()
        job = runtime.queue.enqueue_next(runtime.scope, 'g-' + uuid.uuid4().hex,
                                         source.manifest, head[0] if head else None)
        outcome = process_one(runtime.index, runtime.queue, runtime.source_root,
                              job['claim'], require_vector=require_vector,
                              prepare_vector=(lambda index, source, job:
                                  _prepare_vector(index, source, job, vector_batch))
                              if require_vector else None)
        if outcome == 'ack':
            return _result('indexed', job['gen'], job['claim'])
        return _result('deferred', job['gen'], job['claim'], outcome)

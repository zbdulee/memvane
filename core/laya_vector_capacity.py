"""Step-bounded vector fill for the Laya capacity path.

``pending_vector_fill_step`` embeds individually hashed documents while a
scan is still enumerating. ``vector_fill_step`` later fills and seals the
catalog's COMPLETE snapshot. Each document's chunk set is committed on its
own (``LayaVectorIndex.insert_chunk_set``), and both passes keep separate
durable cursors. Unchanged content is never embedded twice: the chunk-set
cache is keyed by content hash and title, not by generation.

The seal of a generation is separate and needs no model:
``sync_step(require_vector=True)`` stages only after the fill is complete,
then ``seal_generation`` builds the seal from those cached chunk sets before
publish. Design: ``docs/plans/2026-10-19-laya-vector-capacity-design.md``.

This module loads nothing by itself. The provider is passed in by the
admitted owner worker and is never called from a host hook.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional

from laya_index_a import IndexContractError

DEFAULT_FILL_FILES = 8
DEFAULT_FILL_BYTES = 2_000_000
CANDIDATE_RECIPE = 'qmd-regex-token-chunks-candidate-v1'
# ReviewedLocalQmdProvider refuses an empty body and one over this many UTF-8
# bytes (provider_text_invalid). Both depend on the bytes only, so the fill
# decides them before any model start instead of asking the provider.
PROVIDER_MAX_TEXT_BYTES = 5_000_000
# laya_local_embed_worker.mjs drops a request over this many bytes (the JSON
# line the provider sends, newline included) and answers nothing, which the
# provider can only report as provider_result_invalid. Escaping can push a
# body under PROVIDER_MAX_TEXT_BYTES over it, so the fill measures the same
# line the provider builds.
WORKER_MAX_REQUEST_BYTES = 5_100_000
_DAMAGED_CACHE = ('vector_chunk_set_incomplete', 'vector_chunk_set_invalid')


def generation_row_limit():
    """Rows one generation seal can hold, or None when the seal has no bound.

    The fill refuses a snapshot past this before anything is staged, so the
    preflight follows the seal actually installed: ``laya_index_vector``
    exports ``MAX_GENERATION_SEAL_ROWS`` (an int, or None for a streaming
    seal without a whole-generation bound). Without that export the seal
    still loads every row of one generation at once, bounded by
    ``MAX_CANDIDATES``, and the preflight keeps that bound.
    """
    import laya_index_vector as vector
    limit = getattr(vector, 'MAX_GENERATION_SEAL_ROWS', vector.MAX_CANDIDATES)
    if limit is not None and (type(limit) is not int or limit < 1):
        raise IndexContractError('vector_seal_limit_invalid')
    return limit


def document_row_limit():
    """Chunks one member's cached set may hold (``_read_chunk_set``'s bound)."""
    import laya_index_vector as vector
    return vector.MAX_CANDIDATES


def _provider_refuses(text, path):
    if not text or len(text.encode('utf-8')) > PROVIDER_MAX_TEXT_BYTES:
        return 'provider_text_invalid'
    line = json.dumps({'kind': 'document_chunks', 'text': text, 'title': None, 'path': path},
                      ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n'
    if len(line) > WORKER_MAX_REQUEST_BYTES:
        return 'provider_request_too_large'
    return None
_PAGE = 64


def provider_profile_id(provider: Any) -> str:
    """Profile identity of a reviewed candidate provider; raises if unusable."""
    from laya_index_vector import profile_identity
    profile = getattr(provider, 'profile', None)
    if (not callable(getattr(provider, 'embed_document_chunks', None))
            or not isinstance(profile, dict)
            or getattr(provider, 'manifest', {}).get('recipeId') != CANDIDATE_RECIPE):
        raise IndexContractError('candidate_provider_required')
    return profile_identity(profile.get('model_id'), profile.get('embedding_fingerprint'),
                            profile.get('dimension'))


def fill_complete(runtime: Any, profile_id: str) -> bool:
    """True when every eligible member of the current snapshot is cached."""
    try:
        snapshot = runtime.catalog.require_complete()
    except IndexContractError:
        return False
    return runtime.catalog.vector_fill(snapshot, profile_id)['done']


def vector_fill_step(runtime: Any, provider: Any, *, max_files: int = DEFAULT_FILL_FILES,
                     max_bytes: int = DEFAULT_FILL_BYTES,
                     should_yield: Optional[Callable[[], bool]] = None,
                     on_document_complete: Optional[Callable[..., None]] = None) -> Dict[str, Any]:
    """Embed at most ``max_files`` uncached members; one body in memory at a time.

    Answers:

    * ``complete``: every eligible member of the snapshot has a chunk set.
    * ``pending``/``vector_backlog``: budget or yield stopped the step;
      call again.
    * ``pending``/``no_snapshot``: the catalog has no COMPLETE snapshot yet.
    * ``pending``/``source_changed``: a member's bytes no longer match the
      snapshot. A rescan is requested; the next snapshot restarts the fill.
    * ``deferred``/``catalog_busy``: progress could not be recorded.
    * ``blocked``/``vector_budget_exceeded``: the snapshot has more chunk rows
      than the installed generation seal can hold (``generation_row_limit``;
      no check when the seal reports no bound); nothing is staged.
      ``blocked``/``vector_member_rejected``: a member the provider would
      refuse (empty or over ``PROVIDER_MAX_TEXT_BYTES``), decided without
      loading a model, one it returned no chunks for, or one whose chunk set
      exceeds ``document_row_limit`` (``chunk_set_too_large``).
      ``blocked``/``vector_cache_damaged``: a cached chunk set fails its own
      seal; it is not repaired here. All three repeat until the source (or
      the cache) changes. Other provider errors (service unavailable,
      admission refused) are raised, never reported as a member problem.
    * ``blocked``/``vector_member_rejected`` (``provider_member_refused``):
      the worker refused the document on content alone (over 1,024 chunks,
      a chunk over the context).
    * ``deferred``/``provider_result_invalid`` with ``path``: the worker
      answered nothing usable (busy, or an older worker that does not name
      its refusals). Not a member verdict; retried next wake.

    ``max_bytes`` bounds bytes read in one step, but one member is always
    read so a file larger than the budget cannot stall the fill.
    ``embedded``/``cached`` count the members handled in this step.
    """
    from laya_index_vector import LayaVectorIndex, qmd_markdown_title
    from laya_source_catalog import PENDING_PHASES, load_exclusions
    if (type(max_files) is not int or max_files < 1
            or type(max_bytes) is not int or max_bytes < 1
            or (should_yield is not None and not callable(should_yield))
            or (on_document_complete is not None and not callable(on_document_complete))):
        raise TypeError('invalid_vector_fill_step')
    catalog = getattr(runtime, 'catalog', None)
    if catalog is None:
        raise IndexContractError('capacity_not_configured')
    profile_id = provider_profile_id(provider)
    profile = provider.profile
    out = {'embedded': 0, 'cached': 0, 'bytes': 0}

    def answer(state, reason=None, **extra):
        result = {'state': state, 'reason': reason, 'profile_id': profile_id, **out}
        result.update(extra)
        return result

    try:
        snapshot = catalog.require_complete()
    except IndexContractError:
        return answer('pending', 'no_snapshot')
    progress = catalog.vector_fill(snapshot, profile_id)
    if progress['done']:
        return answer('complete')
    vectors = LayaVectorIndex(runtime.index)
    if vectors.register_profile(profile['model_id'], profile['embedding_fingerprint'],
                                profile['dimension']) != profile_id:
        raise IndexContractError('vector_profile_mismatch')
    exclusions = load_exclusions(runtime.index, runtime.scope)
    cursor = progress['cursor']
    rows_total = progress['rows']
    seal_limit = generation_row_limit()
    member_limit = document_row_limit()

    def record(done):
        if not catalog.set_vector_fill(snapshot, profile_id, cursor, done, rows_total):
            return answer('deferred', 'catalog_busy', cursor=cursor)
        return None

    def counted(path, count):
        # A bounded seal fails past its row limit at stage time; refuse here,
        # before anything is staged. Rows are still counted without a bound.
        nonlocal cursor, rows_total
        if seal_limit is not None and rows_total + count > seal_limit:
            return record(False) or answer('blocked', 'vector_budget_exceeded',
                                           cursor=cursor, path=path,
                                           rows=rows_total + count)
        rows_total += count
        cursor = path
        return None

    while True:
        page = catalog.members(snapshot, exclusions, after=cursor, limit=_PAGE,
                               allow_pending=True)
        if not page:
            return record(True) or answer('complete', cursor=cursor)
        for path, sha, eligible in page:
            if not eligible:
                cursor = path
                continue
            found = catalog.lookup(snapshot, path)
            size = found[1] if found else 0
            if out['bytes'] and out['bytes'] + size > max_bytes:
                return record(False) or answer('pending', 'vector_backlog', cursor=cursor)
            try:
                text = catalog.read_verified(snapshot, path, sha).decode('utf-8')
            except IndexContractError:
                stopped = record(False)
                if stopped:
                    return stopped
                scan = catalog.status()['active']
                if scan is None or scan['phase'] not in PENDING_PHASES:
                    catalog.request_scan()
                return answer('pending', 'source_changed', cursor=cursor, path=path)
            out['bytes'] += size
            title = qmd_markdown_title(text, path)
            try:
                cached = vectors.cached_chunk_set(sha, title, profile_id)
            except IndexContractError as exc:
                if str(exc) not in _DAMAGED_CACHE:
                    raise
                return record(False) or answer('blocked', 'vector_cache_damaged',
                                               cursor=cursor, path=path, detail=str(exc))
            if cached is not None:
                if on_document_complete is not None:
                    on_document_complete(snapshot, path, sha, title, text, profile_id)
                out['cached'] += 1
                stopped = counted(path, len(cached))
                if stopped:
                    return stopped
                continue
            if out['embedded'] >= max_files or (should_yield and should_yield()):
                return record(False) or answer('pending', 'vector_backlog', cursor=cursor)
            refused = _provider_refuses(text, path)
            if refused:
                return record(False) or answer('blocked', 'vector_member_rejected',
                                               cursor=cursor, path=path, detail=refused)
            try:
                rows = provider.embed_document_chunks(text, path=path)
            except IndexContractError as exc:
                if str(exc) == 'provider_member_refused':
                    return record(False) or answer('blocked', 'vector_member_rejected',
                                                   cursor=cursor, path=path,
                                                   detail='provider_member_refused')
                if str(exc) != 'provider_result_invalid':
                    raise
                # Busy, or an older worker that sends refusals bare: not a
                # member verdict, but never pathless. Retried next wake.
                return record(False) or answer('deferred', 'provider_result_invalid',
                                               cursor=cursor, path=path)
            if not isinstance(rows, list) or not rows:
                return record(False) or answer('blocked', 'vector_member_rejected',
                                               cursor=cursor, path=path, detail='no_chunks')
            if len(rows) > member_limit:
                # Stored, it would read back as a damaged cache; say what it is.
                return record(False) or answer('blocked', 'vector_member_rejected',
                                               cursor=cursor, path=path,
                                               detail='chunk_set_too_large')
            # Chunk rows reference their content row; stage reuses it.
            runtime.index.ensure_content(sha, text)
            vectors.insert_chunk_set(sha, title, profile_id, rows)
            # The callback may publish a separate read-only preview. Keep the
            # cursor before this member until that atomic publication succeeds;
            # a crash replays the now-cached complete chunk set.
            if on_document_complete is not None:
                on_document_complete(snapshot, path, sha, title, text, profile_id)
            out['embedded'] += 1
            stopped = counted(path, len(rows))
            if stopped:
                return stopped
            # A crash after this commit replays the file as cached, not embedded.
            stopped = record(False)
            if stopped:
                return stopped


def pending_vector_fill_step(runtime: Any, provider: Any, *,
                             max_files: int = DEFAULT_FILL_FILES,
                             max_bytes: int = DEFAULT_FILL_BYTES,
                             should_yield: Optional[Callable[[], bool]] = None,
                             before_write: Optional[Callable[[], None]] = None,
                             on_document_complete: Callable[..., None]) -> Dict[str, Any]:
    """Publish fully embedded members of an unfinished catalog scan.

    This feed never seals a generation or treats an empty page as scan
    completion. A hash-commit ordinal, rather than a pathname, is the cursor:
    later directory enumeration may discover an earlier pathname.
    """
    from laya_index_vector import LayaVectorIndex, qmd_markdown_title
    from laya_source_catalog import load_exclusions
    if (type(max_files) is not int or max_files < 1 or
            type(max_bytes) is not int or max_bytes < 1 or
            (should_yield is not None and not callable(should_yield)) or
            (before_write is not None and not callable(before_write)) or
            not callable(on_document_complete)):
        raise TypeError('invalid_pending_vector_fill_step')
    catalog = getattr(runtime, 'catalog', None)
    if catalog is None:
        raise IndexContractError('capacity_not_configured')
    profile_id = provider_profile_id(provider)
    profile = provider.profile
    vectors = LayaVectorIndex(runtime.index)
    profile_registered = False
    exclusions = load_exclusions(runtime.index, runtime.scope)
    out = {'embedded': 0, 'cached': 0, 'bytes': 0}

    def answer(state, reason, **extra):
        return {'state': state, 'reason': reason, 'profile_id': profile_id,
                **out, **extra}

    proof, _ = catalog.pending_hashed(exclusions, limit=1)
    if proof is None:
        return answer('pending', 'no_pending_scan')
    cursor = catalog.pending_fill_progress(proof, profile_id)
    member_limit = document_row_limit()

    def record(ordinal):
        if not catalog.set_pending_fill_progress(proof, profile_id, ordinal):
            return answer('deferred', 'catalog_busy', cursor=cursor)
        return None

    while True:
        current, page = catalog.pending_hashed(exclusions, after_ord=cursor,
                                               limit=_PAGE)
        if current != proof:
            return answer('pending', 'scan_changed', cursor=cursor)
        if not page:
            return answer('pending', 'waiting_for_hashed_member', cursor=cursor)
        for ordinal, path, sha, eligible in page:
            if not eligible:
                stopped = record(ordinal)
                if stopped:
                    return stopped
                cursor = ordinal
                continue
            found = catalog.lookup(proof, path)
            size = found[1] if found else 0
            if out['bytes'] and out['bytes'] + size > max_bytes:
                return answer('pending', 'pending_vector_backlog', cursor=cursor)
            if out['embedded'] >= max_files or (should_yield and should_yield()):
                return answer('pending', 'pending_vector_backlog', cursor=cursor)
            try:
                catalog.require_pending_member(proof, path, sha)
                body = catalog.read_verified(proof, path, sha).decode('utf-8')
            except IndexContractError:
                catalog.request_scan()
                return answer('pending', 'source_changed', cursor=cursor, path=path)
            out['bytes'] += size
            title = qmd_markdown_title(body, path)
            try:
                chunks = vectors.cached_chunk_set(sha, title, profile_id)
            except IndexContractError as exc:
                if str(exc) not in _DAMAGED_CACHE:
                    raise
                return answer('blocked', 'vector_cache_damaged',
                              cursor=cursor, path=path, detail=str(exc))
            if chunks is None:
                refused = _provider_refuses(body, path)
                if refused:
                    return answer('blocked', 'vector_member_rejected',
                                  cursor=cursor, path=path, detail=refused)
                if not profile_registered:
                    # Registration may write the candidate database. For a
                    # selected project, cross the first-write fence before
                    # it; a page of cached members writes nothing here.
                    if before_write is not None:
                        before_write()
                    if vectors.register_profile(profile['model_id'],
                                                profile['embedding_fingerprint'],
                                                profile['dimension']) != profile_id:
                        raise IndexContractError('vector_profile_mismatch')
                    profile_registered = True
                try:
                    chunks = provider.embed_document_chunks(body, path=path)
                except IndexContractError as exc:
                    if str(exc) == 'provider_member_refused':
                        return answer('blocked', 'vector_member_rejected',
                                      cursor=cursor, path=path,
                                      detail='provider_member_refused')
                    if str(exc) != 'provider_result_invalid':
                        raise
                    return answer('deferred', 'provider_result_invalid',
                                  cursor=cursor, path=path)
                if not isinstance(chunks, list) or not chunks:
                    return answer('blocked', 'vector_member_rejected',
                                  cursor=cursor, path=path, detail='no_chunks')
                if len(chunks) > member_limit:
                    return answer('blocked', 'vector_member_rejected',
                                  cursor=cursor, path=path,
                                  detail='chunk_set_too_large')
                runtime.index.ensure_content(sha, body)
                vectors.insert_chunk_set(sha, title, profile_id, chunks)
                out['embedded'] += 1
            else:
                out['cached'] += 1
            # If this callback or its sidecar transaction fails, replay the
            # complete chunk set on the next wake; do not advance the cursor.
            on_document_complete(proof, path, sha, title, body, profile_id)
            stopped = record(ordinal)
            if stopped:
                return stopped
            cursor = ordinal

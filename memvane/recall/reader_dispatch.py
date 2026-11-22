"""Internal read-only adapter for the selected view."""
import copy
from contextlib import contextmanager
from contextvars import ContextVar
import math
import json
from pathlib import Path
import subprocess
import time

from memvane.journal import api as journal
from memvane.recall.types import ReaderResult

_approved_vectors=ContextVar('schema3_approved_vectors',default=())
_PROVIDER_ERRORS=frozenset(('provider_unavailable','provider_identity_mismatch','provider_result_invalid',
    'provider_socket_invalid','provider_socket_changed','provider_text_invalid','provider_timeout_invalid',
    'provider_manifest_invalid','provider_closure_unapproved','provider_recipe_invalid','provider_recipe_mismatch',
    'provider_artifact_path_invalid','provider_artifact_missing','provider_artifact_changed',
    'provider_artifact_hash_mismatch','provider_model_not_gguf','vector_profile_mismatch'))


def _session_excluded(name):
    """This view's pairs of the cards the session already holds, or None.

    They leave both lanes; the next ranks fill their places.
    """
    from memvane.recall.recall_scope import session_exclusions
    return [[path[len(name)+1:],sha] for path,sha in session_exclusions() if path.startswith(name+'/')] or None


def _fused_sources(lexical, nearest, timings, emptied=None):
    from memvane.recall.domain.rules import _fuse_verified_lists
    if emptied is not None and emptied[3] and timings is not None and not any(lexical) and not nearest:
        store, pin, queries, excluded, status, deadline = emptied
        timings['sessionExcludedHits'] = _excluded_hits(store, pin, queries, excluded, status, timings, deadline)
    lists = lexical + ([] if nearest is None else [nearest])
    rows = _fuse_verified_lists(lists, max_rows=15) if any(lists) else []
    if timings is not None:
        semantic = {row['path'] for row in nearest or []}
        timings['semanticOnly'] = sorted(semantic - {row['path'] for lane in lexical for row in lane})
        timings['semanticSources'] = {row['path']: timings.get('vectorSource', 'python')
                                     if row['path'] in semantic else 'lexical' for row in rows}
    return rows


def _semantic_error_status(exc, timings=None):
    from memvane.store.types import DEFERRED
    reason = getattr(exc, 'reason', str(exc))
    if reason in ('embedding_set_invalid', 'embedding_values_invalid', 'embedding_set_missing',
                  'embedding_input_mismatch', 'embedding_dimension_mismatch',
                  'embedding_sequence_invalid', 'vector_rows_mismatch'):
        if timings is not None:
            timings['semanticReason'] = reason
        return 'embedding_set_invalid'
    if reason in ('fts_deadline_exceeded', 'reader_snapshot_deadline'):
        return DEFERRED['vector_deadline_exceeded']
    if reason not in DEFERRED:
        raise exc
    return DEFERRED[reason]




def _approved_profile_matches(candidate, proof):
    """Compare profile facts; this check alone grants no caller authority."""
    if not isinstance(candidate,dict) or not isinstance(proof,dict) or proof.get('productRouteAllowed') is not True:
        return False
    from memvane.store.api import profile as validate_profile
    try:
        validate_profile(candidate)
    except (TypeError, ValueError):
        return False
    profile=proof.get('vectorProfile');manifest=proof.get('providerManifest')
    if not isinstance(profile,dict) or not isinstance(manifest,dict):
        return False
    return (candidate.get('model_id') == profile.get('modelId')
            and candidate.get('embedding_fingerprint') == profile.get('embeddingFingerprint')
            and candidate.get('dimension') == profile.get('dimension')
            and candidate.get('metric') == profile.get('metric')
            and candidate.get('model_sha') == manifest.get('modelSha256')
            and isinstance(candidate.get('model_sha'),str))


def _persisted_schema3(endpoint):
    return journal.persisted_schema3(endpoint)


def _vector_proof(authority,snapshot,decision,provider,deadline):
    from memvane.provider.api import is_reviewed_provider
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.monotonic() >= deadline or not _persisted_schema3(authority)
            or not is_reviewed_provider(provider) or snapshot is not None):
        raise ValueError('reader_vector_caller_unproved')
    journal.bind_schema3(authority)
    selected=journal.reader_selection(authority)
    if selected is None or selected.preferred_endpoint!=authority.endpoint_id:
        raise ValueError('reader_vector_caller_unproved')
    _record,_raw,proof=journal.recheck_reader(authority)
    if (proof.get('productRouteAllowed') is not True or not isinstance(decision,dict)
            or proof.get('vectorProfile')!=decision.get('vectorProfile')
            or provider.manifest!=proof.get('providerManifest')):
        raise ValueError('reader_vector_caller_unproved')
    profile=proof['vectorProfile']
    fields={'profileId':'profile_id','modelId':'model_id','embeddingFingerprint':'embedding_fingerprint',
            'dimension':'dimension','metric':'metric'}
    if any(profile.get(public)!=provider.profile.get(internal) for public,internal in fields.items()):
        raise ValueError('reader_vector_caller_unproved')
    return proof


def _provider_vector(provider,prompt,budget):
    """``(vectors, requested)`` from the existing provider in a child with an absolute completion bound.

    A long prompt comes back as up to four window vectors (``query_embed_child.embed_chunks``).
    """
    from memvane.kernel.errors import IndexContractError
    from memvane.provider.api import query_embed_child_argv
    deadline=time.monotonic()+budget
    payload=json.dumps({'manifest':provider.manifest,'prompt':prompt,'timeout':budget,'chunks':True,
                        'allowed':sorted(_PROVIDER_ERRORS)},
                       ensure_ascii=True,separators=(',',':'))
    child=subprocess.Popen(query_embed_child_argv(),
                           stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        remaining=deadline-time.monotonic()
        if remaining<=0:raise subprocess.TimeoutExpired(child.args,budget)
        out,_err=child.communicate(payload,timeout=remaining)
    except BaseException as exc:
        if child.poll() is None:
            try:child.kill()
            except ProcessLookupError:pass
        child.communicate()
        if isinstance(exc,subprocess.TimeoutExpired):
            raise IndexContractError('provider_unavailable') from exc
        raise
    if child.returncode!=0:
        raise IndexContractError('provider_process_failed')
    if len(out)>1000000:
        raise IndexContractError('provider_result_invalid')
    try:
        result=json.loads(out)
        if isinstance(result,dict) and set(result)=={'vector'}:
            return [result['vector']],1
        if (isinstance(result,dict) and set(result)=={'vectors','requested'} and type(result['vectors']) is list
                and 1<=len(result['vectors'])<=4 and type(result['requested']) is int
                and len(result['vectors'])<=result['requested']<=64):
            return result['vectors'],result['requested']
        if isinstance(result,dict) and set(result)=={'error'} and result['error'] in _PROVIDER_ERRORS:
            raise IndexContractError(result['error'])
    except (TypeError,ValueError) as exc:
        if isinstance(exc,IndexContractError):raise
        raise IndexContractError('provider_result_invalid') from exc
    raise IndexContractError('provider_result_invalid')


def _vector_key(vector):
    """One query vector or a list of them as a tuple of tuples."""
    several=type(vector) in (list,tuple) and vector and type(vector[0]) in (list,tuple)
    return tuple(tuple(item) for item in (vector if several else [vector]))


@contextmanager
def approved_query_vector(*,authority,snapshot,decision,provider,prompt,deadline,info=None):
    """Bind one provider result to the live v6 snapshot and calling context.

    Yields one query vector, or a list of them for a prompt asked as windows.
    ``info`` receives ``queryChunks`` (vectors answered) and ``queryChunksRequested``.
    """
    from memvane.store.api import query_values
    from memvane.kernel.errors import IndexContractError
    proof=_vector_proof(authority,snapshot,decision,provider,deadline)
    remaining=min(2,deadline-time.monotonic())
    if remaining<=0:raise ValueError('reader_snapshot_deadline')
    try:
        vectors,requested=_provider_vector(provider,prompt,remaining)
    except IndexContractError as exc:
        if str(exc) not in ('provider_unavailable','provider_identity_mismatch',
                            'provider_result_invalid','provider_socket_invalid','provider_socket_changed'):
            raise
        if _vector_proof(authority,snapshot,decision,provider,deadline)!=proof:
            raise ValueError('reader_vector_caller_unproved')
        yield None
        return
    for vector in vectors:
        query_values(vector)
    if info is not None:
        info.update(queryChunks=len(vectors),queryChunksRequested=requested)
    vector=vectors[0] if len(vectors)==1 else [list(item) for item in vectors]
    if _vector_proof(authority,snapshot,decision,provider,deadline)!=proof:
        raise ValueError('reader_vector_caller_unproved')
    active=[True]
    entry=(authority,snapshot,copy.deepcopy(decision),provider,prompt,_vector_key(vector),deadline,copy.deepcopy(proof),active)
    token=_approved_vectors.set((*_approved_vectors.get(),entry))
    try:
        yield list(vector)
    finally:
        active[0]=False
        _approved_vectors.reset(token)


def query_endpoint(endpoint, *, checkout, prompt, query_vector, deadline,
                   snapshot=None, decision=None, provider=None, authority=None, linked=None, timings=None):
    """Read the journal-selected view for this checkout."""
    if not _persisted_schema3(endpoint):
        raise ValueError('reader_endpoint_not_issued')
    return _query_persisted_schema3(endpoint, checkout=checkout, prompt=prompt,
        query_vector=query_vector, deadline=deadline, snapshot=snapshot,
        decision=decision, provider=provider, authority=authority, linked=linked, timings=timings)


def auxiliary_wiki_row(path: str, body: str, wiki_roots) -> bool:
    """Search-time filter: every wiki-root meta page, whatever it holds.

    A nested ``log.md``/``index.md``/``SCHEMA.md`` is an ordinary wiki card.
    ``body`` is unused; the index stage alone keeps its header test (a root
    ``log.md`` without the generated header stays indexed, so the generation
    digest does not change).
    """
    from memvane.settings.api import is_wiki_meta_page
    if len(wiki_roots) != 1:
        return False
    name = next(iter(wiki_roots))
    return path.startswith(name + '/') and is_wiki_meta_page(path[len(name) + 1:])


def _card_spans(store, pin, rows, name, status, deadline):
    from memvane.store.types import BlobStoreError
    remaining = deadline - time.monotonic()
    if remaining <= 0 and status == 'deferred_deadline':
        return {}
    if remaining <= 0:
        raise ValueError('reader_snapshot_deadline')
    try:
        return store.card_locations(pin, [row['path'][len(name)+1:] for row in rows],
                                    deadline_seconds=min(30, remaining))
    except (BlobStoreError, ValueError) as exc:
        reason = getattr(exc, 'reason', str(exc))
        if (status != 'deferred_deadline' or time.monotonic() < deadline or
                reason not in ('reader_snapshot_deadline', 'vector_deadline_exceeded', 'fts_deadline_exceeded')):
            raise
        return {}


def _reader_remaining(deadline):
    remaining = min(30, deadline - time.monotonic())
    if remaining <= 0:
        raise ValueError('reader_snapshot_deadline')
    return remaining


NEED_SWITCH = 'MEMVANE_NEED_SIGNALS'
# The exact semantic search of one recall (prompt or edit) ends after this long, or
# earlier when the recall has less time left; an unfinished one serves lexical rows.
SEMANTIC_DEADLINE_SECONDS = 2.0
NEED_ATOMS = 8
NEED_BUDGET_SECONDS = .15
NEED_READER_RESERVE_SECONDS = 2.0


def _need_signals_on():
    """Prompt recall only, unless ``MEMVANE_NEED_SIGNALS=off`` (read by this hook process)."""
    import os
    from memvane.recall import recall_scope
    return (recall_scope.edit_recall() is None
            and os.environ.get(NEED_SWITCH, '').strip().lower() != 'off')


def _need_signals(store, pin, queries, timings, deadline, limit, wiki_roots=()):
    """Search-need judge signals (design §N stage 1 input); recorded, never decided on.

    Each distinct atom of the conjunctive queries (at most ``NEED_ATOMS``) is
    counted alone on the same pin in one count-only read (``probe_terms``: no
    card body), without the wiki-root meta pages the lexical lane drops
    (``auxiliary_wiki_row``: one wiki root). ``needAtomsRare`` counts atoms with fewer hits than
    ``limit`` (rare terms) and ``needRareTopScore`` is the best (lowest) bm25
    among them. The read gets ``NEED_BUDGET_SECONDS``, less when the reader
    would be left with under ``NEED_READER_RESERVE_SECONDS``; atoms it did not
    reach, or a failure, set ``needProbeTruncated``. A probe failure is
    swallowed: recall rows, order and injection never depend on it. No term
    text is recorded.
    """
    started = time.monotonic()
    probed = hit = rare = 0
    top = None
    truncated = False
    try:
        atoms = list(dict.fromkeys(atom for query in queries for atom in query.split(' AND ') if atom))
        atoms = atoms[:NEED_ATOMS]
        left = min(NEED_BUDGET_SECONDS, deadline - time.monotonic() - NEED_READER_RESERVE_SECONDS)
        if atoms and left <= 0:
            truncated = True
        elif atoms:
            from memvane.settings.types import WIKI_META_PAGES
            answers = store.probe_terms(pin, atoms, limit, deadline_seconds=left,
                                        excluded_paths=list(WIKI_META_PAGES) if len(wiki_roots) == 1 else None)
            truncated = len(answers) < len(atoms)
            for count, score in answers:
                probed += 1
                hit += bool(count)
                if 0 < count < limit:
                    rare += 1
                    if (type(score) in (int, float) and math.isfinite(score)
                            and (top is None or score < top)):
                        top = score
    except Exception:
        truncated = True
    # Diagnostic only, never checked: probe failure never affects recall.
    timings.update(needAtoms=probed, needAtomsHit=hit, needAtomsRare=rare,
                   needRareTopScore=None if top is None else round(float(top), 6),
                   needProbeMs=round((time.monotonic() - started) * 1000, 3),
                   needProbeTruncated=truncated)


def _lexical_signals(timings, queries, lexical):
    """Shadow signals of the search-need judge (design §N stage 0); counts only.

    The conjunctive lexical lists before any single-term retry: how many
    queries ran, how many distinct cards they hit and the best list score
    (the lowest bm25). Nothing decides on them in this release.
    """
    try:
        paths = {row['path'] for lane in lexical for row in lane}
        scores = [row['score'] for lane in lexical for row in lane
                  if type(row['score']) in (int, float) and math.isfinite(row['score'])]
        timings['lexicalQueries'] = len(queries)
        timings['lexicalHits'] = len(paths)
        timings['lexicalTopScore'] = round(float(min(scores)), 6) if scores else None
    except Exception:
        pass


def _excluded_hits(store, pin, queries, excluded, status, timings, deadline):
    """How many candidates of an empty recall the session exclusions removed.

    Asked only when both lanes came back empty: the lexical queries once more
    without the exclusions (their rows that are excluded pairs), plus the
    eligible cards the exact filter dropped when the semantic search completed
    (an exact lane has no score floor, so its empty list is all exclusions).
    """
    from memvane.recall.types import HYBRID_LIST_LIMIT
    from memvane.store.types import BlobStoreError
    pairs, hits = {(path, sha) for path, sha in excluded}, 0
    try:
        for query in queries:
            hits += sum((row['path'], row['blob_sha']) in pairs for row in store.search(
                pin, query, HYBRID_LIST_LIMIT, deadline_seconds=_reader_remaining(deadline)))
    except (BlobStoreError, ValueError):
        pass  # A count that could not be read leaves the recall's own empty reason.
    return hits + (timings.get('vectorExcluded', 0) if status == 'complete' else 0)


def _query_persisted_schema3(endpoint,*,checkout,prompt,query_vector,deadline,snapshot,decision,
                             provider,authority,linked=None,timings=None):
    """Read the journal-selected schema3 endpoint at its current view head.

    Rows leave in the recall row shape (``scope, generation, path, hash, body,
    score``) with ``<wiki collection>/<member path>`` paths, the same FTS
    term builder and RRF fusion as the v6 hybrid list, and source locators
    read from the generation's stored card provenance (no source re-read).

    With ``linked`` the pinned view is that linked worktree's own (see
    ``_schema3_pin``); its wiki collection is the main one rebased onto the
    worktree project root. A view sealed without vectors answers lexical
    lists only (``lexical_only``).
    """
    selected=journal.reader_selection(endpoint)
    if (not _persisted_schema3(endpoint) or selected is None
            or selected.preferred_endpoint!=endpoint.endpoint_id):
        raise ValueError('reader_endpoint_not_issued')
    if (snapshot is not None or type(deadline) not in (int,float) or not math.isfinite(deadline)
            or Path(checkout)!=Path(endpoint.record['project']) or time.monotonic()>=deadline):
        raise ValueError('reader_endpoint_invalid')
    journal.bind_schema3(endpoint)
    record,_raw,_proof=journal.recheck_reader(endpoint)
    if (not isinstance(decision,dict) or decision.get('scopeId')!=record['scope']
            or decision.get('projectRoot')!=record['project']
            or decision.get('collections')!=record['roots']
            or not isinstance(decision.get('recallSettings'),dict)):
        raise ValueError('reader_endpoint_invalid')
    valid=None
    if query_vector is not None:
        from memvane.store.api import query_values
        for item in _vector_key(query_vector):
            query_values(item)
        valid=[entry for entry in _approved_vectors.get()
               if entry[0] is authority and authority is endpoint and entry[1] is None
               and entry[2]==decision and entry[3] is provider and entry[4]==prompt
               and entry[5]==_vector_key(query_vector) and entry[8][0] and time.monotonic()<entry[6]]
        if not valid:
            raise ValueError('reader_vector_caller_unproved')
        deadline=min(deadline,valid[-1][6])
    from memvane.recall.types import HYBRID_LIST_LIMIT
    from memvane.recall.domain.rules import _hybrid_fts_queries, _fail
    from memvane.route.api import wiki_index_roots
    from memvane.store.types import BlobStoreError
    from memvane.store import api as store_api
    queries=[query for query in _hybrid_fts_queries(
        prompt,tuple(decision['recallSettings'].get('lexicalPatterns',())) ) if query]

    def attempt():
        opened = time.monotonic()
        with journal.pin_schema3(record,deadline=deadline,linked=linked,timings=timings) as (store,handle,pin):
            if timings is not None:
                timings['readerSetupMs'] = round(max(0, (time.monotonic() - opened) * 1000
                    - timings.get('checkoutMs', 0) - timings.get('pinMs', 0)), 3)
            vector=store_api.pinned_vector(store,pin)
            # A lexical head (vectors pending) answers lexical lists only.
            semantic=query_vector is not None and vector is not None
            if semantic:
                proof=_vector_proof(authority,None,decision,provider,deadline)
                if (vector is None or not _approved_profile_matches(store_api.vector_profile(store,vector['profile_id'],session=False),proof)
                        or proof!=valid[-1][7]):
                    raise ValueError('reader_vector_caller_unproved')
            saved=store_api.worktree_wiki_root(store,handle.worktree_id,deadline=time.monotonic()+_reader_remaining(deadline))
            def rooted(source):
                if linked is None:
                    return str(Path(source))
                try:
                    return str(Path(linked['worktreeProjectRoot'])/Path(source).relative_to(record['project']))
                except ValueError:
                    return None
            names=[item['name'] for item in record['roots'] if item.get('role')=='wiki'
                   and saved is not None and rooted(item['sourceRoot'])==saved[0]]
            if len(names)!=1:
                _fail('selected_wiki_required')
            name=names[0]
            roots=wiki_index_roots(decision['collections'])

            def shaped(rows):
                answer=[]
                for row in rows:
                    full=name+'/'+row['path']
                    if auxiliary_wiki_row(full,row['body'],roots):
                        continue
                    answer.append({'scope':decision['scopeId'],'generation':pin.gen_id,'path':full,
                                   'hash':row['blob_sha'],'body':row['body'],'score':row['score']})
                return answer
            excluded=_session_excluded(name)
            started = time.monotonic()
            try:
                lexical=[shaped(store.search(pin, query, HYBRID_LIST_LIMIT, deadline_seconds=_reader_remaining(deadline), excluded_cards=excluded))
                         for query in queries]
                if timings is not None:
                    _lexical_signals(timings, queries, lexical)
                if query_vector is None and not any(lexical):
                    # A cold provider can leave the conjunctive query empty.
                    atoms = list(dict.fromkeys(atom for query in queries for atom in query.split(' AND ') if atom))[:3]
                    if len(atoms)>1:
                        lexical=[shaped(store.search(pin, atom, HYBRID_LIST_LIMIT, deadline_seconds=_reader_remaining(deadline), excluded_cards=excluded))
                                 for atom in atoms]
            finally:
                if timings is not None:
                    timings['lexicalMs'] = round((time.monotonic() - started) * 1000, 3)
            if timings is not None and _need_signals_on():
                _need_signals(store, pin, queries, timings, deadline, HYBRID_LIST_LIMIT, roots)  # Diagnostic only, never checked.
            status='provider_absent' if query_vector is None else 'lexical_only' if vector is None else 'complete'
            nearest=None
            if semantic:
                started = time.monotonic()
                try:
                    # Reserve time for lexical locations, selector and rendering.
                    remaining = min(30, deadline - time.monotonic()) - 1.0
                    if remaining <= 0:
                        raise BlobStoreError('vector_deadline_exceeded')
                    preferred = list(dict.fromkeys(row['path'][len(name) + 1:]
                                                   for lane in lexical for row in lane))[:100]
                    # Bound large scans before they consume the semantic time budget.
                    nearest=shaped(store.search_exact(pin,query_vector,HYBRID_LIST_LIMIT,
                                                      profile_id=vector['profile_id'],
                                                      max_float_work=6000000,
                                                      deadline_seconds=min(SEMANTIC_DEADLINE_SECONDS, remaining), timings=timings,
                                                      preferred_paths=preferred, excluded_cards=excluded))
                except (BlobStoreError, ValueError) as exc:
                    status = _semantic_error_status(exc, timings)
                finally:
                    if timings is not None:
                        timings['semanticMs'] = round((time.monotonic() - started) * 1000, 3)

            rows = _fused_sources(lexical, nearest, timings, emptied=(store, pin, queries, excluded, status, deadline))
            started = time.monotonic()
            try:
                spans = _card_spans(store, pin, rows, name, status, deadline)
            finally:
                if timings is not None:
                    timings['locationsMs'] = round((time.monotonic() - started) * 1000, 3)
            if time.monotonic()>=deadline and status != 'deferred_deadline':
                raise ValueError('reader_snapshot_deadline')
            if nearest is not None and _vector_proof(authority,None,decision,provider,deadline)!=proof:
                raise ValueError('reader_vector_caller_unproved')
            locations={name+'/'+path:tuple(found) for path,found in spans.items() if found}
            return ReaderResult('schema3',endpoint.endpoint_id,selected.epoch,pin.gen_id,
                                rows,status,frozenset(),locations)

    # A row-format head that moved between the pin and its read refuses with
    # view_head_changed; recall pins the new head once and never serves another view.
    try:
        return attempt()
    except BlobStoreError as exc:
        if exc.reason!='view_head_changed':
            raise
    return attempt()

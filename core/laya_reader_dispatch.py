"""Internal read-only adapter for the selected view."""
import copy
from dataclasses import dataclass
from contextlib import contextmanager
from contextvars import ContextVar
import math
import json
from pathlib import Path
import subprocess
import sys
import time

import laya_journal_reader as journal

@dataclass(frozen=True)
class ReaderResult:
    store_format: str
    endpoint_id: str
    reader_epoch: int
    data_generation: str
    rows: list
    semantic_status: str
    title_only: frozenset
    source_locations: dict

_approved_vectors=ContextVar('schema3_approved_vectors',default=())
_PROVIDER_ERRORS=frozenset(('provider_unavailable','provider_identity_mismatch','provider_result_invalid',
    'provider_socket_invalid','provider_socket_changed','provider_text_invalid','provider_timeout_invalid',
    'provider_manifest_invalid','provider_closure_unapproved','provider_recipe_invalid','provider_recipe_mismatch',
    'provider_artifact_path_invalid','provider_artifact_missing','provider_artifact_changed',
    'provider_artifact_hash_mismatch','provider_model_not_gguf','vector_profile_mismatch'))
_PROVIDER_CHILD='''
import json,sys
sys.path.insert(0,sys.argv[1])
from laya_local_embed_provider import ReviewedLocalQmdProvider
from laya_index_a import IndexContractError
request=json.load(sys.stdin)
try:
    value=ReviewedLocalQmdProvider(request['manifest'])._embed('query',request['prompt'],timeout=request['timeout'])
    print(json.dumps({'vector':value},allow_nan=False,separators=(',',':')))
except IndexContractError as exc:
    code=str(exc)
    print(json.dumps({'error':code if code in request['allowed'] else 'provider_result_invalid'}))
'''


def reader_capabilities():
    return frozenset(('v6-ro-v1','schema3-ro-v1'))


def _approved_profile_matches(candidate, proof):
    """Compare profile facts; this check alone grants no caller authority."""
    if not isinstance(candidate,dict) or not isinstance(proof,dict) or proof.get('productRouteAllowed') is not True:
        return False
    from laya_blob_values import profile as validate_profile
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
    return (type(endpoint) is journal.VerifiedEndpoint and endpoint in journal._issued
            and endpoint in journal._read_only and endpoint.record.get('store_format')=='schema3')


def _vector_proof(authority,snapshot,decision,provider,deadline):
    import laya_reader_lease as leases
    from laya_local_embed_provider import ReviewedLocalQmdProvider
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.monotonic() >= deadline or not _persisted_schema3(authority)
            or type(provider) is not ReviewedLocalQmdProvider or snapshot is not None):
        raise ValueError('reader_vector_caller_unproved')
    leases._bound(authority, capability=journal.SCHEMA3_CAPABILITY)
    selected=leases._reader_selections.get(authority)
    if selected is None or selected.preferred_endpoint!=authority.endpoint_id:
        raise ValueError('reader_vector_caller_unproved')
    _record,_raw,proof=journal._recheck(authority)
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
    """Use the existing provider in a child with an absolute completion bound."""
    from laya_index_a import IndexContractError
    deadline=time.monotonic()+budget
    payload=json.dumps({'manifest':provider.manifest,'prompt':prompt,'timeout':budget,
                        'allowed':sorted(_PROVIDER_ERRORS)},
                       ensure_ascii=True,separators=(',',':'))
    child=subprocess.Popen([sys.executable,'-B','-I','-c',_PROVIDER_CHILD,str(Path(__file__).resolve().parent)],
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
            return result['vector']
        if isinstance(result,dict) and set(result)=={'error'} and result['error'] in _PROVIDER_ERRORS:
            raise IndexContractError(result['error'])
    except (TypeError,ValueError) as exc:
        if isinstance(exc,IndexContractError):raise
        raise IndexContractError('provider_result_invalid') from exc
    raise IndexContractError('provider_result_invalid')


@contextmanager
def approved_query_vector(*,authority,snapshot,decision,provider,prompt,deadline):
    """Bind one provider result to the live v6 snapshot and calling context."""
    from laya_view_vectors import query_values
    from laya_index_a import IndexContractError
    proof=_vector_proof(authority,snapshot,decision,provider,deadline)
    remaining=min(2,deadline-time.monotonic())
    if remaining<=0:raise ValueError('reader_snapshot_deadline')
    try:
        vector=_provider_vector(provider,prompt,remaining)
    except IndexContractError as exc:
        if str(exc) not in ('provider_unavailable','provider_identity_mismatch',
                            'provider_result_invalid','provider_socket_invalid','provider_socket_changed'):
            raise
        if _vector_proof(authority,snapshot,decision,provider,deadline)!=proof:
            raise ValueError('reader_vector_caller_unproved')
        yield None
        return
    query_values(vector)
    if _vector_proof(authority,snapshot,decision,provider,deadline)!=proof:
        raise ValueError('reader_vector_caller_unproved')
    active=[True]
    entry=(authority,snapshot,copy.deepcopy(decision),provider,prompt,tuple(vector),deadline,copy.deepcopy(proof),active)
    token=_approved_vectors.set((*_approved_vectors.get(),entry))
    try:
        yield list(vector)
    finally:
        active[0]=False
        _approved_vectors.reset(token)


def query_endpoint(endpoint, *, checkout, prompt, query_vector, deadline,
                   snapshot=None, decision=None, provider=None, authority=None, linked=None):
    """Read the journal-selected view for this checkout."""
    if not _persisted_schema3(endpoint):
        raise ValueError('reader_endpoint_not_issued')
    return _query_persisted_schema3(endpoint, checkout=checkout, prompt=prompt,
        query_vector=query_vector, deadline=deadline, snapshot=snapshot,
        decision=decision, provider=provider, authority=authority, linked=linked)


def _query_persisted_schema3(endpoint,*,checkout,prompt,query_vector,deadline,snapshot,decision,
                             provider,authority,linked=None):
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
    import laya_reader_lease as leases
    selected=leases._reader_selections.get(endpoint)
    if (not _persisted_schema3(endpoint) or selected is None
            or selected.preferred_endpoint!=endpoint.endpoint_id):
        raise ValueError('reader_endpoint_not_issued')
    if (snapshot is not None or type(deadline) not in (int,float) or not math.isfinite(deadline)
            or Path(checkout)!=Path(endpoint.record['project']) or time.monotonic()>=deadline):
        raise ValueError('reader_endpoint_invalid')
    leases._bound(endpoint,capability=journal.SCHEMA3_CAPABILITY)
    record,_raw,_proof=journal._recheck(endpoint)
    if (not isinstance(decision,dict) or decision.get('scopeId')!=record['scope']
            or decision.get('projectRoot')!=record['project']
            or decision.get('collections')!=record['roots']
            or not isinstance(decision.get('recallSettings'),dict)):
        raise ValueError('reader_endpoint_invalid')
    valid=None
    if query_vector is not None:
        from laya_view_vectors import query_values
        query_values(query_vector)
        valid=[entry for entry in _approved_vectors.get()
               if entry[0] is authority and authority is endpoint and entry[1] is None
               and entry[2]==decision and entry[3] is provider and entry[4]==prompt
               and entry[5]==tuple(query_vector) and entry[8][0] and time.monotonic()<entry[6]]
        if not valid:
            raise ValueError('reader_vector_caller_unproved')
        deadline=min(deadline,valid[-1][6])
    from laya_host_response import (HYBRID_LIST_LIMIT, _fuse_verified_lists, _hybrid_fts_queries,
                                    _fail)
    from laya_index_host_route import wiki_index_roots
    from source_scan import auxiliary_wiki_row
    from laya_blob_values import BlobStoreError
    from laya_view_vectors import DEFERRED
    queries=[query for query in _hybrid_fts_queries(
        prompt,tuple(decision['recallSettings'].get('lexicalPatterns',())) ) if query]

    def left():
        remaining=min(30,deadline-time.monotonic())
        if remaining<=0:raise ValueError('reader_snapshot_deadline')
        return remaining
    def attempt():
        with journal._schema3_pin(record,deadline=deadline,linked=linked) as (store,handle,pin):
            vector=store._pinned(pin)['vector']
            # A lexical head (vectors pending) answers lexical lists only.
            semantic=query_vector is not None and vector is not None
            if semantic:
                proof=_vector_proof(authority,None,decision,provider,deadline)
                if (vector is None or not _approved_profile_matches(store._profile(vector['profile_id']),proof)
                        or proof!=valid[-1][7]):
                    raise ValueError('reader_vector_caller_unproved')
            with store._reading(time.monotonic()+left()) as db:
                saved=db.execute('SELECT wiki_root FROM worktrees WHERE worktree_id=?',
                                 (handle.worktree_id,)).fetchone()
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
            lexical=[shaped(store.search(pin, query, HYBRID_LIST_LIMIT, deadline_seconds=left()))
                     for query in queries]
            status='provider_absent' if query_vector is None else 'lexical_only' if vector is None else 'complete'
            nearest=None
            if semantic:
                try:
                    nearest=shaped(store.search_exact(pin,query_vector,HYBRID_LIST_LIMIT,
                                                      profile_id=vector['profile_id'],
                                                      deadline_seconds=left()))
                except BlobStoreError as exc:
                    if exc.reason not in DEFERRED:
                        raise
                    status=DEFERRED[exc.reason]

            def fused(lists):
                return _fuse_verified_lists(lists,max_rows=15) if any(lists) else []
            rows=fused(lexical+([] if nearest is None else [nearest]))
            spans=store.card_locations(pin,[row['path'][len(name)+1:] for row in rows],
                                       deadline_seconds=left())
            if time.monotonic()>=deadline:
                raise ValueError('reader_snapshot_deadline')
            if semantic and _vector_proof(authority,None,decision,provider,deadline)!=proof:
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

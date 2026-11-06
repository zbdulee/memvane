"""Combine searches on a caller-owned v6 snapshot; no serving authority."""
from dataclasses import dataclass
from contextlib import ExitStack
import math
from pathlib import Path
import time
from types import SimpleNamespace

import laya_journal_reader as journal
import laya_reader_lease as leases
import laya_reader_open as readers
from laya_index_a import IndexContractError, SnapshotIndex


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


def query_v6_snapshot(snapshot, *, endpoint, prompt, decision, provider, deadline, _lexical=None):
    """Search one observed common pin; caller must still prove serving authority.

    ``_lexical`` (migration parity only) answers the FTS lookups with another
    store: ``base(queries, limit, *, path_prefixes, excluded_paths,
    deadline_seconds)`` and ``sidecar={'probe', 'search'}``. Term selection,
    row proof, trust, fusion and the merge stay this reader's own code.
    """
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.monotonic() >= deadline):
        raise IndexContractError('reader_snapshot_deadline')
    if not any(item is snapshot and bound is endpoint and saved == decision
               for item, bound, saved in readers._observations.get()):
        raise IndexContractError('reader_snapshot_unpinned')
    readers._bound(endpoint)
    selection = leases._reader_selections.get(endpoint)
    if selection is None or selection.preferred_endpoint != endpoint.endpoint_id:
        raise IndexContractError('reader_selection_unproved')
    if snapshot.sidecar is None:
        base = snapshot.connections[0]
        if (endpoint.record.get('legacy_sidecar_baseline') is not None or
                base.execute("SELECT 1 FROM schema_meta WHERE key='reader-sidecar-required:v1'").fetchone()):
            raise IndexContractError('reader_sidecar_revision_unproved')
    if snapshot.sidecar is not None and snapshot.sidecar.get('baseline') is not True:
        baseline=endpoint.record.get('legacy_sidecar_baseline')
        observed=snapshot.sidecar.get('legacyCommitment')
        if baseline is None or dict(baseline)!=observed:
            raise IndexContractError('reader_sidecar_revision_unproved')
    from laya_host_response import (_PROVIDER_RESULT_KEYS, _VectorUnavailable,
                                    merge_incremental_rows, query_selected_v2)
    from laya_incremental_docs_query import query_incremental_snapshot

    vector_unavailable = [provider is None]
    embedded = {}
    def checked_provider(text, profile):
        if vector_unavailable[0]:
            raise _VectorUnavailable('provider_unavailable')
        key = (text, profile['profileId'])
        if key in embedded:
            return embedded[key]
        try:
            supplied = provider(text, profile)
        except Exception as exc:
            refused = (isinstance(exc, IndexContractError) and str(exc) in (
                'provider_unavailable', 'provider_identity_mismatch', 'provider_result_invalid',
                'provider_socket_invalid', 'provider_socket_changed'))
            if not getattr(exc, 'no_query_vector', False) and not refused:
                raise
            vector_unavailable[0] = True
            raise _VectorUnavailable('provider_unavailable') from exc
        if (not isinstance(supplied, dict) or set(supplied) != _PROVIDER_RESULT_KEYS
                or supplied['profileId'] != profile['profileId']
                or supplied['approvedProviderId'] != profile['approvedProviderId']
                or supplied['providerArtifactSha256'] != profile['providerArtifactSha256']):
            vector_unavailable[0] = True
            raise _VectorUnavailable('provider_unavailable')
        embedded[key] = supplied
        return embedded[key]

    base, binding_db, catalog_db, side = snapshot.connections
    binding, _roots = journal._binding_snapshot(binding_db, Path(endpoint.record['binding_path']),
                                                decision, snapshot.published)
    generation = snapshot.published[0]
    index = SnapshotIndex(base, scope=decision['scopeId'], generation=generation,
                          incarnation=decision['dbIncarnation'], deadline=deadline,
                          _observation=snapshot)
    with ExitStack() as stack:
        token = index.pin()
        stack.callback(index.release, token)
        if _lexical is not None:
            def search_many(pinned, fts_queries, limit, *, path_prefixes=None,
                            deadline_seconds=None, excluded_paths=None):
                index._check(pinned, require_token=True)
                return _lexical['base'](fts_queries, limit, path_prefixes=path_prefixes,
                                        excluded_paths=excluded_paths,
                                        deadline_seconds=deadline_seconds)
            index.search_many = search_many
        for db in (binding_db, catalog_db, side):
            if db is not None:
                guard = db._hold_snapshot_transaction()
                stack.callback(db._release_snapshot_transaction, guard)
        side_vector_status = []
        incremental, incremental_titles, incremental_locations, shadow, incremental_generation = (
            query_incremental_snapshot(prompt, decision, {'vectorProfile': decision['vectorProfile']},
                                       checked_provider, base=base, side=side, deadline=deadline,
                                       _semantic_status=side_vector_status,
                                       _lexical=None if _lexical is None else _lexical['sidecar']))
        if side_vector_status:
            vector_unavailable[0] = True
        if incremental_generation is not None and incremental_generation != generation:
            raise IndexContractError('incremental_base_changed')
        locations = {}
        vector_status = []
        rows, titles = query_selected_v2(SimpleNamespace(index=index, scope=decision['scopeId']),
                                        prompt, decision['vectorProfile'], decision['vectorProfile'],
                                        checked_provider, decision, source_locations=locations,
                                        excluded_paths=shadow or None, expected_generation=generation,
                                        _snapshot_binding=binding, _snapshot_token=token,
                                        _snapshot_vector_status=vector_status)
        if vector_status:
            vector_unavailable[0] = True
    if incremental:
        locations.update(incremental_locations)
        titles = titles | incremental_titles
        rows = merge_incremental_rows(incremental, rows)
    if time.monotonic() >= deadline:
        raise IndexContractError('reader_snapshot_deadline')
    return ReaderResult('v6', endpoint.endpoint_id, selection.epoch, generation, rows,
                        'lexical_only' if vector_unavailable[0] else 'hybrid', titles, locations)

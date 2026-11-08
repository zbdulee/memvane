"""Read-only search of individually completed wiki documents.

The same private store serves initial and selected update paths. It never
replaces an installer selection or a complete A-int generation.
"""
from __future__ import annotations

from contextlib import ExitStack, closing, nullcontext
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import time

from laya_index_a import IndexContractError, LayaIndexA, _valid_relative_path, _query_only_uri
from laya_index_vector import _read_chunk_set


MAX_DOCS = 20_000
MAX_FTS = 100
DEADLINE_SECONDS = 2.0


class QueryVectorUnavailable(Exception):
    """The provider has no query vector for this prompt; lexical lists serve."""
    no_query_vector = True


def _fail(reason):
    raise IndexContractError(reason)


def _readonly(path: Path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077
            or path.is_symlink() or str(path.resolve(strict=True)) != str(path)):
        _fail('preview_store_unsafe')
    db = sqlite3.connect(_query_only_uri(path), uri=True,
                         isolation_level=None, timeout=0.2)
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
    except BaseException:
        db.close()
        raise
    return db


def _check_deadline(deadline):
    if time.monotonic() >= deadline:
        _fail('preview_query_deadline_exceeded')


def _exclusions(db, scope):
    rows = db.execute('SELECT kind,value FROM exclusions WHERE scope=?',
                      (scope,)).fetchall()
    if any(kind not in ('path', 'hash') or not isinstance(value, str)
           for kind, value in rows):
        _fail('preview_exclusions_invalid')
    return (frozenset(value for kind, value in rows if kind == 'path'),
            frozenset(value for kind, value in rows if kind == 'hash'))


def _lexical_plan(prompt, patterns, *, probe, search, deadline):
    """Run the shared sidecar term selection and bounded lexical lookups."""
    from keywords import build_lexical_terms, select_general_terms_explained
    from laya_host_response import _fts_atom
    built = build_lexical_terms(prompt, list(patterns))
    general = built['generalTerms']
    probe_deadline = min(deadline, time.monotonic() + 1.0)
    present, probe_matches, probe_complete = set(), [], True
    for term in general[:6]:
        atom = _fts_atom(term)
        if not atom:
            continue
        if time.monotonic() >= probe_deadline:
            probe_complete = False
            break
        try:
            found = probe(atom)
        except sqlite3.OperationalError as exc:
            raise IndexContractError('preview_probe_unavailable') from exc
        if found is not None:
            present.add(term.lower())
            probe_matches.append(found)
    if not probe_complete:
        present = None
    selected, mode = select_general_terms_explained(general, present)
    lexical_paths = []
    for position, query in enumerate([' '.join(selected), *built['epTerms']]):
        _check_deadline(deadline)
        fts = ' AND '.join(atom for term in query.split() if (atom := _fts_atom(term)))
        if fts:
            lexical_paths.append((position, search(fts)))
    return built, present, selected, mode, probe_matches, probe_complete, lexical_paths


def query_incremental(prompt: str, decision: dict, proof: dict, provider):
    """Return verified wiki rows, or [] when no document has been published.

    This never creates a DB or starts a model. Both SQLite readers pin a
    consistent snapshot. A mismatch fails closed; a missing sidecar is a valid
    zero-document initial build only after the caller's pending-install proof.
    """
    return _query_incremental(prompt, decision, proof, provider)


def query_incremental_snapshot(prompt: str, decision: dict, proof: dict, provider,
                               *, base, side, deadline: float, _semantic_status=None,
                               _lexical=None):
    """Query caller-owned pinned readers; the caller proves their shared view.

    ``_lexical`` (migration parity only) answers the sidecar FTS probe and
    search with another store's ``probe(atom)``/``search(fts)``; every other
    step, including row proof and trust, still reads these pinned readers.
    """
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or deadline <= time.monotonic()):
        _fail('preview_query_deadline_exceeded')
    if side is not None and side is base:
        _fail('reader_snapshot_unpinned')
    for db in (base,) if side is None else (base, side):
        if db is None or not db.in_transaction or db.execute('PRAGMA query_only').fetchone() != (1,):
            _fail('reader_snapshot_unpinned')
    if side is None:
        return [], frozenset(), {}, (), None
    result = _query_incremental(prompt, decision, proof, provider,
                                pinned_base=base, pinned_side=side, deadline=deadline,
                                semantic_status=[] if _semantic_status is None else _semantic_status,
                                lexical=_lexical)
    from laya_journal_reader import _sidecar_document_snapshot, sidecar_pinned_checks
    pinned = sidecar_pinned_checks(side, base, decision) if result[0] else None
    for row in result[0]:
        # Semantic scoring validates each active chunk set before use. Row
        # evidence itself needs no vector read, including lexical fallback.
        proved = _sidecar_document_snapshot(side, base, decision, row['path'],
                                            deadline=deadline, require_vectors=False,
                                            pinned=pinned)
        if proved['hash'] != row['hash'] or proved['body'] != row['body']:
            _fail('reader_sidecar_document_changed')
    return result


def _query_incremental(prompt, decision, proof, provider, *, pinned_base=None,
                       pinned_side=None, deadline=None, semantic_status=None, lexical=None):
    from laya_host_response import (_PROVIDER_RESULT_KEYS,
                                    _v2_body_evidence,
                                    _verified_wiki_row, _fuse_verified_lists,
                                    _promote_exact_ep)
    from laya_incremental_docs import FILE, SCHEMA
    from laya_index_host_route import wiki_index_roots
    from laya_index_source_port import auxiliary_wiki_row

    wiki_roots = wiki_index_roots(decision['collections'])

    state_root = decision.get('previewStateRoot')
    if state_root is None:
        journal_path = decision.get('journalPath')
        if journal_path is None:
            # Legacy synthetic v2 routes have no initial-build sidecar.
            # The selected generation remains the only search source.
            return [], frozenset(), {}, (), None
        state_root = Path(journal_path).parent
    state = Path(state_root)
    preview = state / FILE
    if pinned_side is None and not preview.exists() and not preview.is_symlink():
        return [], frozenset(), {}, (), None
    if deadline is None:
        deadline = time.monotonic() + DEADLINE_SECONDS
    profile = decision['vectorProfile']
    if not isinstance(proof, dict) or proof.get('vectorProfile') != profile:
        _fail('preview_provider_proof_changed')
    expected = (SCHEMA, decision['installId'], decision['scopeId'],
                decision['dbIncarnation'], profile['profileId'],
                decision['dbPath'], decision['projectRoot'])
    locations = {}
    with ExitStack() as stack:
        side = stack.enter_context(nullcontext(pinned_side) if pinned_side is not None
                                   else closing(_readonly(preview)))
        if pinned_side is not None and callable(getattr(side, '_hold_progress_deadline', None)):
            side._hold_progress_deadline(deadline)
            stack.callback(side._release_progress_deadline)
        else:
            if pinned_side is not None:
                stack.callback(side.set_progress_handler, None, 0)
            side.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        if side.execute('PRAGMA user_version').fetchone()[0] != 1:
            _fail('preview_schema_changed')
        if side.execute('SELECT * FROM meta').fetchall() != [expected]:
            _fail('preview_identity_changed')
        count = side.execute('SELECT count(*) FROM docs').fetchone()[0]
        if type(count) is not int or count < 0 or count > MAX_DOCS:
            _fail('preview_document_count_invalid')
        if count == 0:
            return [], frozenset(), {}, (), None
        _check_deadline(deadline)
        # The sidecar contains only wiki documents published atomically after
        # their complete chunk sets. Do not derive a project or path from SQL.
        docs = side.execute('SELECT rowid,path,hash,title,profile_id,chunk_count '
                            'FROM docs ORDER BY path').fetchall()
        if len(docs) != count:
            _fail('preview_document_count_changed')
        by_path = {row[1]: row for row in docs}
        if len(by_path) != count:
            _fail('preview_document_duplicate')
        with (nullcontext(pinned_base) if pinned_base is not None
              else closing(_readonly(Path(decision['dbPath'])))) as candidate:
            LayaIndexA._check_header(candidate)
            incarnation = candidate.execute(
                "SELECT value FROM schema_meta WHERE key='incarnation'").fetchone()
            if incarnation != (decision['dbIncarnation'],):
                _fail('preview_candidate_incarnation_changed')
            saved_profile = candidate.execute(
                'SELECT model_id,embedding_fingerprint,dimension,metric '
                'FROM vector_profiles WHERE profile_id=?',
                (profile['profileId'],)).fetchone()
            if saved_profile != (profile['modelId'], profile['embeddingFingerprint'],
                                 profile['dimension'], profile['metric']):
                _fail('preview_vector_profile_changed')
            head = candidate.execute('SELECT gen FROM head WHERE scope=?',
                                     (decision['scopeId'],)).fetchone()
            base_generation = head[0] if head else None
            base_hashes = (dict(candidate.execute(
                'SELECT path,hash FROM membership WHERE gen=? AND scope=? AND eligible=1',
                (base_generation, decision['scopeId'])).fetchall())
                if base_generation else {})
            exclusions = _exclusions(candidate, decision['scopeId'])
            excluded_paths, excluded_hashes = exclusions
            # An already-published identical document belongs to the full
            # generation. A differing sidecar path shadows the old head even
            # if its live bytes changed again: stale old top-K rows must not
            # poison verification of unrelated completed documents. Live
            # checks happen only for returned candidates, not all 1,575+
            # sidecar rows on every prompt.
            shadow_docs = []
            active_docs = []
            wiki_prefixes = tuple(name + '/' for name in
                                  wiki_index_roots(decision['collections']))
            for doc in docs:
                _check_deadline(deadline)
                if (not _valid_relative_path(doc[1])
                        or not doc[1].startswith(wiki_prefixes)):
                    _fail('preview_wiki_path_invalid')
                # Selected recall obtains identical rows from its pinned head.
                # Inactive preview has no selected reader, even after the
                # candidate head seals, so its completed rows stay visible.
                if (decision['state'] != 'preview' and
                        base_hashes.get(doc[1]) == doc[2]):
                    continue
                shadow_docs.append(doc)
                if doc[1] in excluded_paths or doc[2] in excluded_hashes:
                    continue
                active_docs.append(doc)
            if not shadow_docs:
                return [], frozenset(), {}, (), base_generation
            shadow_paths = tuple(sorted(doc[1] for doc in shadow_docs))
            if len(set(shadow_paths)) != len(shadow_paths):
                _fail('preview_document_duplicate')
            if not active_docs:
                return [], frozenset(), {}, shadow_paths, base_generation
            active_paths = tuple(sorted(doc[1] for doc in active_docs))
            paths_json = json.dumps(active_paths, ensure_ascii=False,
                                    separators=(',', ':'))
            if len(paths_json.encode('utf-8')) > 8_000_000:
                _fail('preview_document_paths_too_large')
            def probe(atom):
                found = side.execute('SELECT d.path FROM docs_fts f '
                    'JOIN docs d ON d.rowid=f.rowid WHERE docs_fts MATCH ? AND d.path IN '
                    '(SELECT value FROM json_each(?)) LIMIT 1', (atom, paths_json)).fetchone()
                return None if found is None else found[0]
            def search(fts):
                matches = side.execute('SELECT d.path FROM docs_fts f '
                    'JOIN docs d ON d.rowid=f.rowid WHERE docs_fts MATCH ? AND d.path IN '
                    '(SELECT value FROM json_each(?)) '
                    'ORDER BY bm25(docs_fts,1.5,4.0,1.0),d.path COLLATE BINARY LIMIT ?',
                    (fts, paths_json, MAX_FTS)).fetchall()
                return [item[0] for item in matches]
            if lexical is not None:
                probe, search = lexical['probe'], lexical['search']
            (built, present, selected, mode, probe_matches, probe_complete, lexical_paths) = _lexical_plan(
                prompt, decision['recallSettings']['lexicalPatterns'], probe=probe, search=search,
                deadline=deadline)
            # No provider call for a valid empty preview. A populated preview
            # uses only the already-running installer-attested provider.
            try:
                supplied = (None if provider is None else
                            provider(' '.join(prompt.split()), profile))
            except Exception as exc:
                # "No query vector" is marked, not one class: the selected
                # recall raises its own, and the hook runs that module as
                # __main__, a second copy with its own classes. Either way
                # the lexical lists serve.
                if not getattr(exc, 'no_query_vector', False):
                    raise
                supplied = None
            best, sealed = [], {}
            for doc in active_docs:
                _rowid, path, digest, title, profile_id, _count = doc
                if (not isinstance(path, str) or not isinstance(digest, str)
                        or not isinstance(title, str)
                        or profile_id != profile['profileId']):
                    _fail('preview_document_invalid')
                sealed[path] = doc
            if supplied is not None and (not isinstance(supplied, dict)
                    or set(supplied) != _PROVIDER_RESULT_KEYS
                    or supplied['profileId'] != profile['profileId']
                    or supplied['approvedProviderId'] != profile['approvedProviderId']
                    or supplied['providerArtifactSha256'] != profile['providerArtifactSha256']):
                _fail('preview_vector_provider_mismatch')
            if supplied is not None:
                query = supplied['vector']
                dimension = profile['dimension']
                if (not isinstance(query, (list, tuple)) or len(query) != dimension
                        or any(type(value) not in (float, int) or not math.isfinite(value)
                               for value in query)):
                    _fail('preview_query_vector_invalid')
                from laya_index_vector import _dot, _sum_squares
                query_norm = math.sqrt(_sum_squares(query))
                if query_norm <= 0:
                    _fail('preview_query_vector_invalid')
                semantic_deadline = (min(deadline, time.monotonic()+2.0)
                                     if semantic_status is not None else deadline)
                managed = semantic_status is not None and callable(
                    getattr(candidate, '_hold_progress_deadline', None))
                if managed:
                    candidate._hold_progress_deadline(semantic_deadline)
                try:
                    for doc in active_docs:
                        _check_deadline(semantic_deadline)
                        _rowid, path, digest, title, profile_id, chunks_count = doc
                        chunks = _read_chunk_set(candidate, digest, title, profile_id,
                                                 dimension, deadline=semantic_deadline)
                        if chunks is None or len(chunks) != chunks_count:
                            _fail('preview_vector_incomplete')
                        distance = min(1 - _dot(query, chunk['vector']) / (
                                       query_norm * math.sqrt(_sum_squares(chunk['vector'])))
                                       for chunk in chunks)
                        best.append((distance, path))
                except (IndexContractError, sqlite3.OperationalError) as exc:
                    reason = str(exc)
                    expired = time.monotonic() >= semantic_deadline
                    if (semantic_status is None or time.monotonic() >= deadline or
                            not (reason in ('vector_budget_exceeded','vector_deadline_exceeded')
                                 or (expired and (reason == 'preview_query_deadline_exceeded'
                                                  or isinstance(exc, sqlite3.OperationalError))))):
                        raise
                    semantic_status.append('vector_deadline_exceeded' if expired else reason)
                    best = []
                finally:
                    if managed:
                        candidate._release_progress_deadline()
                best.sort(key=lambda item: (item[0], item[1]))
        generation = 'preview-' + decision['installId']
        skip = tuple(decision['recallSettings']['skipPaths'])

        fetched = {}

        def verified(path, score):
            doc = sealed.get(path)
            if doc is None:
                _fail('preview_fts_row_unpublished')
            if (not isinstance(path, str) or not _valid_relative_path(path)
                    or not any(path.startswith(name + '/') and
                               _valid_relative_path(path[len(name) + 1:])
                               for name in wiki_roots)):
                _fail('preview_wiki_path_invalid')
            rowid, _path, digest, _title, _profile, _count = doc
            if path not in fetched:
                body_row = side.execute('SELECT body FROM docs WHERE rowid=? AND path=?',
                                        (rowid, path)).fetchone()
                if (body_row is None or not isinstance(body_row[0], str)
                        or hashlib.sha256(body_row[0].encode('utf-8')).hexdigest() != digest):
                    _fail('preview_document_invalid')
                fetched[path] = body_row[0]
            body = fetched[path]
            row = {'scope': decision['scopeId'], 'generation': generation,
                   'path': path, 'hash': digest, 'body': body, 'score': score}
            if auxiliary_wiki_row(path, body, wiki_roots):
                return None
            if not _verified_wiki_row(row, decision, source_skip_paths=skip,
                                      current_source=False):
                return None
            return row

        probe_verified = all(verified(path, 1.0) is not None
                             for path in probe_matches)
        lists = []
        lex_hits = ep_hits = False
        for position, paths in lexical_paths:
            # A full MAX_FTS page with few proved cards is the exact head of
            # the proved ranking, only shorter: serve it, never refuse.
            rows = [row for index, path in enumerate(paths)
                    if (row := verified(path, 1 / (index + 1))) is not None]
            lists.append(rows[:20])
            lex_hits |= bool(rows)
            ep_hits |= position > 0 and bool(rows)
        vector_rows = []
        for distance, path in best:
            _check_deadline(deadline)
            row = verified(path, max(0.0, min(2.0, distance)))
            if row is not None:
                vector_rows.append(row)
                if len(vector_rows) == 20:
                    break
        lists.append(vector_rows)
        fused = _promote_exact_ep(_fuse_verified_lists(lists, max_rows=20), prompt)
        rows = [row for row in fused if row['score'] >= decision['recallSettings']['minScore']
                and not any(pattern in row['path'] for pattern in skip)
                and not auxiliary_wiki_row(
                    row['path'], row['body'], wiki_roots)]
        # Derive optional locators only for rows actually injected.
        for row in rows[:decision['recallSettings']['topN']]:
            _verified_wiki_row(row, decision, source_locations=locations,
                               source_skip_paths=skip, current_source=False)
        _check_deadline(deadline)
        with (nullcontext(pinned_base) if pinned_base is not None
              else closing(_readonly(Path(decision['dbPath'])))) as refreshed:
            if _exclusions(refreshed, decision['scopeId']) != exclusions:
                _fail('preview_exclusions_changed')
        body_evidence = (probe_complete and probe_verified and _v2_body_evidence(
            built, present, mode, selected, lex_hits, ep_hits))
        title_only = (frozenset() if body_evidence else
                      frozenset(row['path'] for row in rows))
        return (rows, title_only, locations,
                shadow_paths, base_generation)

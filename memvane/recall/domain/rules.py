"""Lexical query construction, ranked-result fusion and verified-card selection rules."""
from __future__ import annotations

import unicodedata
from memvane.learning.types import SELECTOR_ANSWERS, SELECTOR_CANDIDATE_INVALID
from memvane.recall.types import HostResponseError, HYBRID_LIST_LIMIT, MAX_ROWS, RRF_K, ROW_KEYS as _ROW_KEYS


def _fail(code):
    raise HostResponseError(code)


def _fts_atom(term: str) -> str:
    if not term or len(term) > 256:
        _fail("invalid_lexical_term")
    # QMD's porter unicode61 index spaces CJK runs by character. A compound
    # term becomes an adjacent phrase; a plain term retains prefix matching.
    from memvane.kernel.errors import _qmd_fts_text
    cjk = any(_qmd_fts_text(ch) != ch for ch in term)
    parts = []
    current = []
    for ch in _qmd_fts_text(term):
        if ch.isspace() or (unicodedata.category(ch)[0] not in ('L', 'N')
                            and ch not in "'_"):
            if current:
                parts.append(''.join(current).lower())
                current = []
        else:
            current.append(ch)
    if current:
        parts.append(''.join(current).lower())
    if not parts:
        return ''
    escaped = ' '.join(parts).replace('"', '""')
    return '"' + escaped + '"' + ('' if cjk or len(parts) > 1 else '*')


def _hybrid_fts_queries(prompt: str, lexical_patterns: tuple[str, ...]) -> list[str]:
    from memvane.recall.keywords import build_lexical_terms
    if (not isinstance(lexical_patterns, tuple)
            or any(pattern != "ep" for pattern in lexical_patterns)
            or len(lexical_patterns) > 1):
        _fail("unsupported_lexical_patterns")
    built = build_lexical_terms(prompt, list(lexical_patterns))
    queries = []
    for query in built["lexQueries"]:
        terms = query.split()
        atoms = [_fts_atom(term) for term in terms]
        queries.append(" AND ".join(atom for atom in atoms if atom))
    return queries


def _fuse_verified_lists(lists: list[list[dict]], *, max_rows: int = MAX_ROWS) -> list[dict]:
    """Pinned QMD RRF ordering and skip-rerank score for verified paths."""
    by_path = {}
    scores = {}
    best_rank = {}
    generation = scope = None
    active_list = 0
    if type(max_rows) is not int or not 1 <= max_rows <= HYBRID_LIST_LIMIT:
        _fail('invalid_hybrid_candidates')
    for rows in lists:
        if not isinstance(rows, list) or len(rows) > HYBRID_LIST_LIMIT:
            _fail("invalid_hybrid_candidates")
        if not rows:
            continue
        # QMD structuredSearch pushes only nonempty lists, then weights the
        # first pushed list 2x regardless of its original query position.
        weight = 2 if active_list == 0 else 1
        active_list += 1
        seen = set()
        for rank, row in enumerate(rows, 1):
            if not isinstance(row, dict) or set(row) != _ROW_KEYS:
                _fail("invalid_result_shape")
            path = row["path"]
            if not isinstance(path, str) or path in seen:
                _fail("duplicate_hybrid_candidate")
            seen.add(path)
            if generation is None:
                generation, scope = row["generation"], row["scope"]
            if row["generation"] != generation or row["scope"] != scope:
                _fail("mixed_result_generation")
            prior = by_path.get(path)
            if prior is not None and any(prior[key] != row[key] for key in
                                         ("generation", "scope", "hash", "body")):
                _fail("hybrid_source_mismatch")
            by_path[path] = row
            scores[path] = scores.get(path, 0.0) + weight / (RRF_K + rank)
            best_rank[path] = min(best_rank.get(path, rank), rank)
    for path, rank in best_rank.items():
        if rank == 1:
            scores[path] += 0.05
        elif rank <= 3:
            scores[path] += 0.02
    # QMD's stable score sort retains first-seen ranked-list order for ties.
    ordered = sorted(scores, key=lambda path: -scores[path])
    # QMD skipRerank replaces fused score with position score 1/rank.
    return [{**by_path[path], "score": 1 / rank}
            for rank, path in enumerate(ordered[:max_rows], 1)]


def reader_reseal_candidate(code, has_route):
    return code == 'reader_endpoint_changed' and has_route


def verified_pool(rows):
    return rows[:15]


def limit_selected(chosen, top_n):
    limit = min(top_n, 3)
    return chosen[:limit], limit


def selection_action(reason, chosen):
    if reason not in ('selected', 'ranked') or not chosen:
        if reason == 'selected':
            return 'selected_zero'
        if reason == 'no_candidates':
            return 'no_candidates'
        return 'empty'
    return 'serve'


def selector_action(reason):
    if reason in SELECTOR_ANSWERS:
        return 'accept'
    if reason == SELECTOR_CANDIDATE_INVALID:
        return 'invalid'
    return 'fallback'

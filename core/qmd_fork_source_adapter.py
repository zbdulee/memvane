"""Fail-closed, source-only boundary for a future generation-aware QMD fork.

The backend here is injected by tests. Installed QMD 2.5.3 does not provide
this capability contract, so this module must not be wired to its runtime.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import sqlite3


@dataclass(frozen=True)
class ScopeCapabilities:
    lexical_before_limit: bool
    vector_before_limit: bool
    immutable_generation: bool


@dataclass(frozen=True)
class PinnedScope:
    worktree: str
    generation: str
    collection_id: str


@dataclass(frozen=True)
class SearchHit:
    collection_id: str
    path: str
    content_hash: str
    score: float


def _scope_id(worktree: str, generation: str) -> str:
    encoded = (str(len(worktree)) + ':' + worktree + str(len(generation)) + ':'
               + generation).encode('utf-8')
    return 'c-' + hashlib.sha256(encoded).hexdigest()


def pinned_scope(db: sqlite3.Connection, token: str, capabilities: ScopeCapabilities):
    """Resolve an existing Candidate C pin; reject unsupported search engines."""
    if (not isinstance(capabilities, ScopeCapabilities)
            or not all((capabilities.lexical_before_limit,
                        capabilities.vector_before_limit,
                        capabilities.immutable_generation))):
        raise ValueError('backend_scope_capability_missing')
    if not isinstance(token, str) or not token or '\x00' in token:
        raise ValueError('invalid_pin')
    row = db.execute('SELECT worktree,generation FROM pins WHERE token=?',
                     (token,)).fetchone()
    if row is None:
        raise ValueError('pin_unavailable')
    if not db.execute('SELECT 1 FROM generations WHERE worktree=? AND generation=?',
                      row).fetchone():
        raise ValueError('pinned_generation_missing')
    return PinnedScope(*row, _scope_id(*row))


def search_phase(db: sqlite3.Connection, token: str, backend, phase: str,
                 query: str, limit: int):
    """Pass one immutable scope to backend before its candidate read.

    Verification of returned rows is a defense against misbinding, not a
    substitute for the backend's pre-limit capability guarantee.
    """
    if phase not in ('lex', 'vec') or type(limit) is not int or limit < 1:
        raise ValueError('invalid_search_request')
    scope = pinned_scope(db, token, backend.capabilities)
    hits = backend.search(phase=phase, query=query, limit=limit, scope=scope)
    if not isinstance(hits, (tuple, list)) or len(hits) > limit:
        raise ValueError('backend_result_contract_failed')
    for hit in hits:
        if not isinstance(hit, SearchHit) or hit.collection_id != scope.collection_id:
            raise ValueError('backend_result_scope_mismatch')
        member = db.execute('SELECT hash FROM membership WHERE worktree=? '
                            'AND generation=? AND path=?',
                            (scope.worktree, scope.generation, hit.path)).fetchone()
        if member is None or member[0] != hit.content_hash:
            raise ValueError('backend_result_membership_mismatch')
    return tuple(hits)


def search_both(db: sqlite3.Connection, token: str, backend,
                lex_query: str, vec_query: str, limit: int):
    """Both phases use the same pin even if active changes between calls."""
    lexical = search_phase(db, token, backend, 'lex', lex_query, limit)
    vector = search_phase(db, token, backend, 'vec', vec_query, limit)
    return lexical, vector

"""Shared store values, failure types and the view store handle members."""
from __future__ import annotations

import re
from typing import Protocol

SHA = re.compile(r'[0-9a-f]{64}\Z')
UUID = re.compile(r'[0-9a-f]{32}\Z')
MAX_BODY = 4 * 1024 * 1024
MAX_CHUNKS = 1024
WORK_EXCEEDED = 'vector_work_exceeded'
DEADLINE_EXCEEDED = 'vector_deadline_exceeded'
DEFERRED = {WORK_EXCEEDED: 'deferred_work_budget', DEADLINE_EXCEEDED: 'deferred_deadline',
            'fts_deadline_exceeded': 'deferred_deadline'}
FALLBACK_CHARS = (600, 90, 135)


class BlobStoreError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class CheckoutUnavailable(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)

# ``api.read_header`` result: the ``store_meta`` row's columns, then ``worktree``
# (the row of the asked ``worktree_id`` or None) and ``worktrees`` (the rows of
# the asked ``project_root``, a tuple). Each worktree row has STORE_WORKTREE_KEYS.
STORE_HEADER_KEYS = ('incarnation', 'scope_id', 'repository_json', 'settings_policy_digest',
                     'worktree', 'worktrees')
STORE_WORKTREE_KEYS = ('worktree_id', 'project_root', 'binding_digest', 'record_json')

MAX_MEMBERS = 20000
STAGE_ITEM_FIELDS = ('path', 'action', 'input_sha', 'blob_sha', 'trust_digest', 'metadata_json')


MAX_FILES = 64


class ViewStoreHandle(Protocol):
    """Public members other domains call on an open view store (``api.open_store``).

    The store holds a SQLite connection, read pins and capture handles, so it
    crosses the domain boundary as a handle. Only these members may be used
    outside ``memvane.store``; ``_`` members never. Signatures are the
    ``ViewStore`` methods' own (the structure guard compares them).
    """

    def capture_view(self, worktree_id, *, deadline=None): ...

    def head(self, handle): ...

    def pin_view(self, handle, *, deadline=None): ...

    def release(self, pin): ...

    def close(self): ...

    def prepare_generation(self, handle, manifest, *, expected_head, vector_spec=None,
                           deadline=None): ...

    def publish_generation(self, handle, gen_id, *, expected_head, deadline=None): ...

    def ingest_attested_card(self, worktree_id, path, *, decision, provenance_scan,
                             current_sources=None): ...

    def document(self, blob_sha): ...

    def put_compiled_attestation(self, worktree_id, intent, *, policy_digest): ...

    def fill_status(self, handle, manifest, recipe_id, profile_id): ...

    def fill_view(self, handle, manifest, recipe_id, profile_id, *, chunker, embedder, write_fence,
                  expected_cursor, max_files=8, max_bytes=2000000, deadline_seconds=30): ...

    def search(self, pin, fts_query, limit, *, path_prefixes=None, excluded_paths=None,
               deadline_seconds=2): ...

    def search_exact(self, pin, query_vector, limit, *, profile_id, max_float_work=4000000,
                     deadline_seconds=2, path_prefixes=None, excluded_paths=None): ...

    def card_locations(self, pin, paths, *, deadline_seconds=2): ...

    def vectors(self, blob_sha, recipe_id, profile_id): ...

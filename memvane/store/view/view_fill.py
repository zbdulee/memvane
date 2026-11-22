"""Bounded capture-bound view fill cursors over shared immutable content sets.

A fill job commits one store incarnation, issued view capture, settings,
manifest, recipe/profile and the sorted eligible member path/blob/byte targets.
Its cursor only moves forward, in its own short transaction, after every
covered target's complete embedding set has already committed. Callbacks and
the caller's write fence run outside SQLite transactions; elapsed checks never
preempt a callback. Jobs are progress metadata, not an execution lease: the
content filler's OS lease owns per-content work. Job rows hold no body text.
"""
from __future__ import annotations

import math
from functools import wraps
import sqlite3
import time

from memvane.store.blob import blob_values as v
from memvane.store.blob.blob_values import BlobStoreError
from memvane.store.blob.blob_fill import ContentFiller
from memvane.store.blob.blob_store import _insert
from memvane.store.types import MAX_FILES
from memvane.store.types import FILL_PROFILE
from memvane.store.view import fill_schema as fill_schema
from memvane.store.view import view_values as vals

MAX_DEADLINE = 300
MAX_BYTES = 64 * 1024 * 1024
JOB_SCHEMA = 'memvane-fill-job-v1'
BUSY = 'content_claim_busy'


def profile_fill_plan(function):
    @wraps(function)
    def call(self, handle, recipe_id, profile_id):
        profile = FILL_PROFILE.get()
        if profile is None:
            return function(self, handle, recipe_id, profile_id)
        started = time.monotonic_ns()
        try:
            return function(self, handle, recipe_id, profile_id)
        finally:
            profile['planCalls'] += 1
            profile['planNs'] += time.monotonic_ns() - started
    return call


def _deadline(seconds):
    if type(seconds) not in (int, float) or not 0 < seconds <= MAX_DEADLINE or not math.isfinite(seconds):
        v.fail('fill_deadline_invalid')
    return time.monotonic() + seconds


def _remaining(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        v.fail('fill_deadline_exceeded')
    return left


def completed_prefix(store, targets, cursor, recipe_id, profile_id, deadline):
    """Count consecutive existing sets; publication still validates their contents."""
    count = 0
    with store._session() as db:
        for target in targets[cursor:]:
            _remaining(deadline)
            row = db.execute('SELECT 1 FROM embedding_sets WHERE blob_sha=? AND recipe_id=? AND profile_id=?',
                             (target['blob_sha'], recipe_id, profile_id)).fetchone()
            if row is None:
                break
            count += 1
    return count


def head_targets(store, db, handle, head, recipe_id, profile_id):
    """Reuse immutable head targets on this store instance; cursor stays fresh."""
    key = (handle.view_id, head.ordinal, head.gen_id, recipe_id, profile_id)
    cached = getattr(store, '_fill_target_cache', None)
    if cached is not None and cached[0] == key:
        return [dict(row) for row in cached[1]], cached[2]
    targets = []
    for row in store._rows_open(db, handle.view_id, head.ordinal):
        if row['eligible']:
            size = db.execute('SELECT byte_length FROM wiki_blobs WHERE blob_sha=?',
                              (row['blob_sha'],)).fetchone()[0]
            targets.append(dict(path=row['path'], blob_sha=row['blob_sha'], byte_length=size))
    if len(targets) > fill_schema.MAX_TARGETS:
        v.fail('fill_targets_too_large')
    digest = v.digest('memvane-fill-targets-v1', targets)
    store._fill_target_cache = (key, tuple(dict(row) for row in targets), digest)
    return targets, digest


def _bounded(value, lower, upper, reason):
    if type(value) is not int or not lower <= value <= upper:
        v.fail(reason)
    return value


def _targets(plan):
    """Sorted eligible path→blob→UTF-8 byte targets; bodies never leave this call."""
    targets = [dict(path=m['path'], blob_sha=m['blob_sha'],
                    byte_length=plan['body_bytes'][m['path']])
               for m in plan['members'] if m['eligible']]
    if len(targets) > fill_schema.MAX_TARGETS:
        v.fail('fill_targets_too_large')
    return targets


def _job(incarnation, handle, entry, saved, manifest, skips, recipe_id, profile_id, targets):
    identity = dict(schema=JOB_SCHEMA, incarnation=incarnation, view=vals.view_fields(handle),
                    capture=v.digest('memvane-fill-capture-v1', entry['capture']),
                    worktree_binding=saved['binding_digest'],
                    manifest_digest=vals.manifest_digest(manifest), skip_paths=skips,
                    recipe_id=recipe_id, profile_id=profile_id,
                    targets_digest=v.digest('memvane-fill-targets-v1', targets), target_count=len(targets))
    return dict(job_id=v.digest(JOB_SCHEMA, identity), incarnation=incarnation, view_id=handle.view_id,
                worktree_id=handle.worktree_id, recipe_id=recipe_id, profile_id=profile_id,
                manifest_digest=identity['manifest_digest'], targets_digest=identity['targets_digest'],
                identity_json=v.canonical(identity), target_count=len(targets))


def _status(job, stored):
    cursor = 0 if stored is None else stored
    return dict(job_id=job['job_id'], cursor=cursor,
                complete=stored is not None and cursor == job['target_count'])


class FillMixin:
    def ensure_content(self, blob, recipe_id, profile_id, *, chunker, embedder, write_fence, deadline_seconds):
        return ContentFiller(self).ensure_content(blob, recipe_id, profile_id, chunker=chunker,
                                                 embedder=embedder, write_fence=write_fence,
                                                 deadline_seconds=deadline_seconds)

    def _fill_idle(self):
        try:
            busy = self._db.in_transaction
        except sqlite3.Error:
            v.fail('candidate_unavailable')
        if busy:
            v.fail('fill_in_transaction')

    @staticmethod
    def _fill_cursor(db, job, targets):
        """Stored cursor of this exact job, None when absent; any identity drift refuses."""
        row = db.execute('SELECT * FROM fill_jobs WHERE job_id=?', (job['job_id'],)).fetchone()
        if row is None:
            return None
        stored = [dict(r) for r in db.execute(
            'SELECT ordinal,path,blob_sha,byte_length FROM fill_targets WHERE job_id=? ORDER BY ordinal',
            (job['job_id'],))]
        cursor = row['cursor']
        if (any(row[k] != val for k, val in job.items()) or type(cursor) is not int
                or not 0 <= cursor <= job['target_count'] or type(row['created_ns']) is not int
                or stored != [dict(t, ordinal=i) for i, t in enumerate(targets)]):
            v.fail('fill_job_invalid')
        return cursor


    def fill_status(self, handle, manifest, recipe_id, profile_id):
        """Readonly; an absent job reports cursor 0 and complete false.

        A row-format store fills the current head's eligible cards; ``manifest``
        is not read there.
        """
        return self._rows_fill_status(handle, recipe_id, profile_id)

    def fill_view(self, handle, manifest, recipe_id, profile_id, *, chunker, embedder, write_fence,
                  expected_cursor, max_files=8, max_bytes=2000000, deadline_seconds=30):
        return self._rows_fill_view(handle, recipe_id, profile_id, chunker=chunker, embedder=embedder,
                                    write_fence=write_fence, expected_cursor=expected_cursor,
                                    max_files=max_files, max_bytes=max_bytes,
                                    deadline_seconds=deadline_seconds)

    def arena_sets(self, recipe_id, profile_id, blobs=()):
        """``(dimension, {blob: (chunk count, set digest)})`` of the embedding sets the vector arena mirrors.

        The sets of eligible cards in any view's head or the generation it
        replaced (what a recall of a current pin can ask for), and of ``blobs``
        (the members of a generation about to be published).
        """
        import json
        with self._session() as db:
            self._recipe(recipe_id)
            dimension = self._profile(profile_id)['dimension']
            return dimension, {row[0]: (row[1], row[2]) for row in db.execute(
                'SELECT e.blob_sha,e.chunk_count,e.set_digest FROM embedding_sets e WHERE e.recipe_id=? '
                'AND e.profile_id=? AND e.blob_sha IN (SELECT c.blob_sha FROM s_cards c JOIN s_heads h '
                'ON h.view_id=c.view_id WHERE c.eligible=1 AND (c.gen_to IS NULL OR c.gen_to>=h.ordinal) '
                'UNION SELECT value FROM json_each(?))', (recipe_id, profile_id, json.dumps(sorted(set(blobs)))))}

    def arena_set(self, blob, recipe_id, profile_id):
        """The float32 bytes of one stored embedding set, validated like a recall's top candidates."""
        with self._session():
            return b''.join(row['embedding'] for row in self._vectors(blob, recipe_id, profile_id))

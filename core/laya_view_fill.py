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
import sqlite3
import time

import laya_blob_values as v
from laya_blob_values import BlobStoreError
from laya_blob_fill import ContentFiller
from laya_blob_store import _insert
import laya_fill_schema as fill_schema
import laya_view_values as vals

MAX_DEADLINE = 300
MAX_FILES = 64
MAX_BYTES = 64 * 1024 * 1024
JOB_SCHEMA = 'memvane-fill-job-v1'
BUSY = 'content_claim_busy'


def _deadline(seconds):
    if type(seconds) not in (int, float) or not 0 < seconds <= MAX_DEADLINE or not math.isfinite(seconds):
        v.fail('fill_deadline_invalid')
    return time.monotonic() + seconds


def _remaining(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        v.fail('fill_deadline_exceeded')
    return left


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

    def _fill_plan(self, handle, manifest, recipe_id, profile_id):
        import laya_view_store as views  # the store module owns capture/settings rechecks
        entry = self._check(handle)
        manifest = vals.manifest(manifest, handle.policy_digest)
        v.identifier(recipe_id); v.identifier(profile_id)
        saved = self._observe(entry)
        skips = views._skip_patterns(saved)
        with self._session() as db:
            self._recipe(recipe_id); self._profile(profile_id)
            targets = _targets(self._derive(handle, manifest, skips, retain_bodies=False))
            job = _job(self._incarnation, handle, entry, saved, manifest, skips, recipe_id, profile_id, targets)
            stored = self._fill_cursor(db, job, targets)
        return entry, saved, job, targets, stored

    def fill_status(self, handle, manifest, recipe_id, profile_id):
        """Readonly; an absent job reports cursor 0 and complete false."""
        if self._rows:
            v.fail('row_format_unsupported')
        _, _, job, _, stored = self._fill_plan(handle, manifest, recipe_id, profile_id)
        return _status(job, stored)

    def fill_view(self, handle, manifest, recipe_id, profile_id, *, chunker, embedder, write_fence,
                  expected_cursor, max_files=8, max_bytes=2000000, deadline_seconds=30):
        if self._rows:
            v.fail('row_format_unsupported')
        self._require_write()
        deadline = _deadline(deadline_seconds)
        _bounded(expected_cursor, 0, fill_schema.MAX_TARGETS, 'fill_cursor_invalid')
        _bounded(max_files, 1, MAX_FILES, 'fill_limit_invalid')
        _bounded(max_bytes, 1, MAX_BYTES, 'fill_limit_invalid')
        if not all(callable(fn) for fn in (chunker, embedder, write_fence)):
            v.fail('fill_callback_invalid')
        self._fill_idle()
        entry, saved, job, targets, stored = self._fill_plan(handle, manifest, recipe_id, profile_id)
        cursor = 0 if stored is None else stored
        if expected_cursor != cursor:
            v.fail('fill_cursor_conflict')
        _remaining(deadline)
        if stored is not None and cursor == job['target_count']:
            return dict(_status(job, stored), status='complete', filled=0, cache_hits=0, deferred=False)
        fenced = []

        def fence():
            # One latch per call covers content sets, job metadata and the cursor.
            if not fenced:
                self._fill_idle()
                write_fence()
                fenced.append(True)

        filler = ContentFiller(self)
        steps = used = filled = hits = 0
        deferred = False
        for target in targets[cursor:]:
            if steps >= max_files:
                break
            if used + target['byte_length'] > max_bytes:
                deferred = steps == 0  # oversized for this call: caller may enlarge explicitly
                break
            try:
                result = filler.ensure_content(target['blob_sha'], recipe_id, profile_id, chunker=chunker,
                                               embedder=embedder, write_fence=fence,
                                               deadline_seconds=_remaining(deadline))
            except BlobStoreError as exc:
                if exc.reason != BUSY:
                    raise
                deferred = True  # competing content lease: explicit defer, no retry loop
                break
            if result['status'] == 'filled':
                filled += 1
                self._observe(entry)  # callbacks may have changed checkout or settings
            else:
                hits += 1
            _remaining(deadline)
            steps += 1
            used += target['byte_length']
        if steps == 0 and not (stored is None and not targets):
            return dict(_status(job, stored), status='deferred', filled=filled, cache_hits=hits,
                        deferred=deferred)
        new = cursor + steps
        self._fill_commit(entry, saved, job, targets, stored, new, fence, deadline)
        complete = new == job['target_count']
        return dict(job_id=job['job_id'], cursor=new, complete=complete,
                    status='complete' if complete else 'progress', filled=filled, cache_hits=hits,
                    deferred=deferred)

    def _fill_commit(self, entry, saved, job, targets, stored, new, fence, deadline):
        """Separate cursor transaction after the covered content sets committed."""
        import laya_view_store as views
        _remaining(deadline)
        self._check(entry['handle'])
        fence()
        cursor = 0 if stored is None else stored
        with self._session(write=True) as db:
            if self._fill_cursor(db, job, targets) != stored:
                v.fail('fill_cursor_conflict')
            for target in targets[cursor:new]:
                if db.execute('SELECT 1 FROM embedding_sets WHERE blob_sha=? AND recipe_id=? AND profile_id=?',
                              (target['blob_sha'], job['recipe_id'], job['profile_id'])).fetchone() is None:
                    v.fail('fill_content_missing')
            if stored is None:
                _insert(db, 'fill_jobs', dict(job, cursor=0, created_ns=time.time_ns()))
                for ordinal, target in enumerate(targets):
                    _insert(db, 'fill_targets', dict(target, job_id=job['job_id'], ordinal=ordinal))
            if new != cursor and db.execute('UPDATE fill_jobs SET cursor=? WHERE job_id=? AND cursor=?',
                                            (new, job['job_id'], cursor)).rowcount != 1:
                v.fail('fill_cursor_conflict')
            views._live(db, saved, {})
            views._current_capture(saved, self._repository, self._policy, entry['capture'])
            _remaining(deadline)

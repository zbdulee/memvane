"""Row-format generations: per-view card intervals, one head FTS, rollback deltas.

A publication writes only the changed card rows, the matching head FTS rows,
one sealed generation row and its deltas, then moves the head by CAS, all in
one SQLite transaction. The seal is a structural commitment over the stored
rows and FTS bytes, never compiler truth, source observation or operational
authority. This module serves lexical normal generations only; legacy
imports, vector coverage and source relations refuse with a fixed reason.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import time
import uuid

import laya_blob_values as v
import laya_view_fts as fts
import laya_view_values as vals

ROW_STAGE_SCHEMA = 'memvane-row-stage-v1'
ROW_SEAL_SCHEMA = 'memvane-row-generation-seal-v1'
CARD_FIELDS = ('path', 'blob_sha', 'title', 'eligible', 'trust_digest', 'metadata_json')
EMPTY_METADATA = v.canonical({})
_PAGE = 64


def _deadline_value(deadline):
    if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)
                                 or time.monotonic() >= deadline):
        v.fail('fts_deadline_exceeded')


def _expired(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        v.fail('fts_deadline_exceeded')


def member_digest(rows):
    return v.digest('memvane-row-members-v1', [{k: row[k] for k in CARD_FIELDS} for row in rows])


def seal_digest(fields):
    return v.digest(ROW_SEAL_SCHEMA, fields)


def _seal_fields(incarnation, generation):
    keys = ('ordinal', 'gen_id', 'view_id', 'epoch', 'prior_ordinal', 'kind', 'sidecar_present',
            'capture_json', 'policy_digest', 'skip_digest', 'recipe_id', 'profile_id', 'coverage',
            'member_digest', 'source_digest', 'logical_fts_digest', 'physical_fts_digest')
    return dict(incarnation=incarnation, **{k: generation[k] for k in keys})


def _card_row(member):
    return dict(path=member['path'], blob_sha=member['blob_sha'], title=member['display_title'],
                eligible=member['eligible'], trust_digest=member['trust_digest'], metadata_json=EMPTY_METADATA)


def _stage_item(path, row):
    if row is None:
        return dict(path=path, action='delete', input_sha=None, blob_sha=None, trust_digest=None,
                    metadata_json=None)
    return dict(path=path, action='put', input_sha=row['blob_sha'], blob_sha=row['blob_sha'],
                trust_digest=row['trust_digest'],
                metadata_json=v.canonical(dict(title=row['title'], eligible=row['eligible'],
                                               metadata=row['metadata_json'])))


def _item_row(item):
    if item['action'] == 'delete':
        return None
    extra = v.parse_canonical(item['metadata_json'])
    v.exact(extra, ('title', 'eligible', 'metadata'))
    return dict(path=item['path'], blob_sha=item['blob_sha'], title=extra['title'], eligible=extra['eligible'],
                trust_digest=item['trust_digest'], metadata_json=extra['metadata'])


def _sorted_paths(paths):
    return sorted(paths, key=lambda path: path.encode('utf8'))


def _stage_value(incarnation, handle, saved, base, job_id, scan_digest, skips, final, items):
    """Every committed stage input; the generation id derives from it at prepare."""
    return dict(schema=ROW_STAGE_SCHEMA, job_id=job_id, incarnation=incarnation,
                view=vals.view_fields(handle), worktree_binding=saved['binding_digest'], base=base,
                scan_digest=scan_digest, skip_digest=v.digest('memvane-row-skip-v1', skips),
                member_digest=member_digest(final), items_digest=v.digest('memvane-row-stage-items-v1', items))


@dataclass(frozen=True)
class _Head:
    ordinal: int | None
    gen_id: str | None
    epoch: int


class RowsMixin:
    """Row-format implementation selected by a store whose header is format 4."""

    def _rows_head_row(self, db, view_id):
        row = db.execute('SELECT h.ordinal,h.epoch,g.gen_id,g.epoch FROM s_heads h JOIN s_generations g '
                         'ON g.view_id=h.view_id AND g.ordinal=h.ordinal WHERE h.view_id=?',
                         (view_id,)).fetchone()
        newest = db.execute('SELECT MAX(ordinal) FROM s_generations WHERE view_id=?', (view_id,)).fetchone()[0]
        if row is None:
            if newest is not None:
                v.fail('view_head_invalid')
            return _Head(None, None, 0)
        if (type(row[1]) is not int or row[1] < 1 or row[1] != row[3] or row[0] != newest
                or not vals.GEN_ID.fullmatch(row[2] or '')):
            v.fail('view_head_invalid')
        return _Head(row[0], row[2], row[1])

    def _rows_head(self, db, handle):
        head = self._rows_head_row(db, handle.view_id)
        return dict(view_id=handle.view_id, gen_id=head.gen_id, epoch=head.epoch, incarnation=self._incarnation)

    @staticmethod
    def _rows_open(db, view_id, ordinal=None):
        """The view's card rows visible in ``ordinal`` (default: the open interval)."""
        if ordinal is None:
            cursor = db.execute('SELECT path,blob_sha,title,eligible,trust_digest,metadata_json FROM s_cards '
                                'WHERE view_id=? AND gen_to IS NULL ORDER BY path', (view_id,))
        else:
            cursor = db.execute('SELECT path,blob_sha,title,eligible,trust_digest,metadata_json FROM s_cards '
                                'WHERE view_id=? AND gen_from<=? AND (gen_to IS NULL OR gen_to>?) ORDER BY path',
                                (view_id, ordinal, ordinal))
        try:
            return [dict(zip(CARD_FIELDS, tuple(row))) for row in cursor]
        finally:
            cursor.close()

    def _rows_fts_rows(self, rows, *, cache=None, deadline=None):
        """Head FTS rows of the eligible card rows, from admitted blob bodies."""
        bodies, titles = {}, {}
        for row in rows:
            _expired(deadline)
            if not row['eligible']:
                continue
            blob = row['blob_sha']
            if cache is not None and blob in cache:
                body = cache[blob]
            else:
                body = self._blob(blob)
                if cache is not None:
                    cache[blob] = body
            bodies[row['path']] = body
            titles[row['path']] = row['title']
        return vals.fts_rows(bodies, titles, deadline=deadline)

    # -- publication ---------------------------------------------------------

    def _rows_prepare(self, handle, manifest, *, expected_head, vector_spec, legacy_snapshot, deadline):
        from laya_view_store import _skip_patterns, _wiki_files, _live
        from laya_blob_store import _insert
        import laya_view_store as store_module
        _deadline_value(deadline)
        self._require_write()
        entry = self._check(handle)
        base = vals.expected_head(expected_head, handle.view_id, self._incarnation)
        if legacy_snapshot is not None or vector_spec is not None:
            v.fail('row_format_unsupported')
        manifest = vals.manifest(manifest, handle.policy_digest)
        saved = self._observe(entry, deadline=deadline)
        skips = _skip_patterns(saved)
        with self._session() as db:
            if self._rows_head(db, handle) != base:
                v.fail('view_head_conflict')
            plan = self._derive(handle, manifest, skips, deadline=deadline)
            current = {row['path']: row for row in self._rows_open(db, handle.view_id)}
        wanted = {member['path']: _card_row(member) for member in plan['members']}
        items = [_stage_item(path, wanted.get(path)) for path in _sorted_paths(set(current) | set(wanted))
                 if current.get(path) != wanted.get(path)]
        final = [wanted[path] for path in _sorted_paths(wanted)]
        files = _wiki_files(saved, plan['members'])
        job_id = uuid.uuid4().hex
        capture_json = v.canonical(entry['capture'])
        scan_digest = v.digest('memvane-row-stage-scan-v1', dict(
            manifest=vals.manifest_digest(manifest), skip_paths=skips,
            files=sorted([path, node] for path, node in files.items())))
        stage = _stage_value(self._incarnation, handle, saved, base, job_id, scan_digest, skips, final, items)
        gen_id = vals.gen_id(job_id, v.digest(ROW_STAGE_SCHEMA, stage))
        self._observe(entry, deadline=deadline)
        _expired(deadline)
        with self._session(write=True) as db:
            if self._rows_head(db, handle) != base:
                v.fail('view_head_conflict')
            _live(db, saved, files)
            view = db.execute('SELECT worktree_id,branch_key FROM views WHERE view_id=?', (handle.view_id,)).fetchone()
            if view is None:
                _insert(db, 'views', dict(view_id=handle.view_id, worktree_id=handle.worktree_id,
                                          branch_key=handle.branch_key))
            elif tuple(view) != (handle.worktree_id, handle.branch_key):
                v.fail('view_binding_conflict')
            old = db.execute('SELECT job_id,state FROM s_stage_jobs WHERE view_id=?', (handle.view_id,)).fetchone()
            if old is not None:
                # One stage per view: an earlier unpublished or published stage
                # is discarded in bounded steps before this one starts.
                store_module._stage_mark_abandon(db, old[0])
                while store_module._stage_discard_step(db, old[0], limit=_PAGE):
                    _expired(deadline)
            identity = dict(view_id=handle.view_id, job_id=job_id, incarnation=self._incarnation,
                            base_gen_id=base['gen_id'], base_epoch=base['epoch'], capture_json=capture_json,
                            policy_digest=handle.policy_digest, scan_digest=scan_digest)
            store_module._stage_start_job(db, identity)
            cursor = None
            for start in range(0, len(items), _PAGE):
                _expired(deadline)
                page = items[start:start + _PAGE]
                observed = [item['path'] for item in page]
                store_module._stage_put_batch(db, job_id, cursor, observed, page)
                cursor = observed[-1]
            if db.execute("UPDATE s_stage_jobs SET state='ready' WHERE job_id=? AND state='collecting'",
                          (job_id,)).rowcount != 1:
                v.fail('stage_job_invalid')
            store_module._current_capture(saved, self._repository, self._policy, entry['capture'], deadline=deadline)
            _live(db, saved, files)
            _expired(deadline)
        return gen_id

    def _rows_publish(self, handle, gen_id, *, expected_head, legacy_snapshot, deadline):
        from laya_view_store import _live, _skip_patterns, _wiki_files
        import laya_view_store as store_module
        _deadline_value(deadline)
        self._require_write()
        entry = self._check(handle)
        base = vals.expected_head(expected_head, handle.view_id, self._incarnation)
        if not isinstance(gen_id, str) or not vals.GEN_ID.fullmatch(gen_id):
            v.fail('generation_id_invalid')
        if legacy_snapshot is not None:
            v.fail('migration_publish_proof_invalid')
        saved = self._observe(entry, deadline=deadline)
        skips = _skip_patterns(saved)

        def capture():
            store_module._current_capture(saved, self._repository, self._policy, entry['capture'],
                                          deadline=deadline)

        def staged_state(db):
            """The view's ready stage, re-derived only from committed rows."""
            job = db.execute('SELECT job_id,incarnation,base_gen_id,base_epoch,capture_json,policy_digest,'
                             'scan_digest,state FROM s_stage_jobs WHERE view_id=?', (handle.view_id,)).fetchone()
            if job is None or job[7] != 'ready' or gen_id[2:34] != job[0]:
                v.fail('generation_missing')
            if (tuple(job[1:6]) != (self._incarnation, base['gen_id'], base['epoch'],
                                    v.canonical(entry['capture']), handle.policy_digest)):
                v.fail('stage_identity_changed')
            items = [vals.check_stage_item(dict(zip(vals.STAGE_ITEM_FIELDS, tuple(row)))) for row in db.execute(
                'SELECT path,action,input_sha,blob_sha,trust_digest,metadata_json FROM s_stage_items '
                'WHERE job_id=? ORDER BY path', (job[0],))]
            current = {row['path']: row for row in self._rows_open(db, handle.view_id)}
            final = dict(current)
            for item in items:
                new = _item_row(item)
                if current.get(item['path']) == new:
                    v.fail('stage_items_mismatch')
                if new is None:
                    final.pop(item['path'], None)
                else:
                    final[item['path']] = new
            final = [final[path] for path in _sorted_paths(final)]
            stage = _stage_value(self._incarnation, handle, saved, base, job[0], job[6], skips, final, items)
            if vals.gen_id(job[0], v.digest(ROW_STAGE_SCHEMA, stage)) != gen_id:
                v.fail('stage_identity_changed')
            return stage, items, current, final

        stage = files = rows = None
        with self._session() as db:
            head = self._rows_head_row(db, handle.view_id)
            published = db.execute('SELECT 1 FROM s_generations WHERE gen_id=?', (gen_id,)).fetchone()
            if published is None:
                if (head.gen_id, head.epoch) != (base['gen_id'], base['epoch']):
                    v.fail('view_head_conflict')
                stage, items, _, final = staged_state(db)
        if published is None:
            # Bounded FD reads of the final members' exact bytes, outside the writer lock.
            files = _wiki_files(saved, final)
            rows = self._rows_fts_rows(final, cache={}, deadline=deadline)
        with self._session(write=True) as db:
            head = self._rows_head_row(db, handle.view_id)
            published = db.execute('SELECT view_id,epoch FROM s_generations WHERE gen_id=?', (gen_id,)).fetchone()
            if published is not None:
                if (tuple(published) == (handle.view_id, base['epoch'] + 1) and head.gen_id == gen_id
                        and head.epoch == base['epoch'] + 1):
                    capture()
                    return self._rows_head(db, handle)  # exact replay of this publication only
                v.fail('view_head_conflict')
            if (head.gen_id, head.epoch) != (base['gen_id'], base['epoch']):
                v.fail('view_head_conflict')
            again = staged_state(db)
            if stage is None or again[0] != stage:
                v.fail('stage_identity_changed')
            items = again[1]
            _live(db, saved, files)
            current = {row['path']: row for row in self._rows_open(db, handle.view_id)}
            deltas = []
            for item in items:
                old, new = current.get(item['path']), _item_row(item)
                if old == new:
                    v.fail('stage_items_mismatch')
                deltas.append((old, new))

            def before_commit(db):
                # The published stage is discarded later in bounded steps.
                store_module._stage_mark_abandon(db, stage['job_id'])
                capture()
                _live(db, saved, files)

            generation = dict(gen_id=gen_id, view_id=handle.view_id, epoch=base['epoch'] + 1,
                              prior_ordinal=head.ordinal, kind='normal', sidecar_present=0,
                              capture_json=v.canonical(entry['capture']), policy_digest=handle.policy_digest,
                              skip_digest=stage['skip_digest'], recipe_id=None, profile_id=None,
                              coverage='lexical', member_digest=stage['member_digest'],
                              source_digest=v.digest('memvane-row-sources-v1', []))
            written = self._rows_write(db, head, generation, deltas, rows, before_commit, deadline)
        return dict(view_id=handle.view_id, gen_id=written['gen_id'], epoch=written['epoch'],
                    incarnation=self._incarnation)

    def _rows_write(self, db, head, generation, deltas, rows, before_commit, deadline, *, expected_logical=None):
        """Changed card rows, their head FTS rows, the sealed generation and the head CAS.

        Runs inside the caller's write transaction and commits it under the
        view's FTS capability; any failure rolls the whole publication back.
        """
        import laya_view_store as store_module
        view_id = generation['view_id']
        sequence = db.execute("SELECT seq FROM sqlite_sequence WHERE name='s_generations'").fetchone()
        ordinal = (0 if sequence is None else sequence[0]) + 1
        changes = []
        for old, new in deltas:
            _expired(deadline)
            old_fts = self._rows_fts_rows([old], deadline=deadline) if old is not None else []
            new_fts = self._rows_fts_rows([new], deadline=deadline) if new is not None else []
            old_fts, new_fts = (old_fts[0] if old_fts else None), (new_fts[0] if new_fts else None)
            if old_fts != new_fts:
                changes.append(dict(old=old_fts, new=new_fts))
        with self._fts_guard.view_head(view_id):
            if head.ordinal is None:
                fts.create_head(db, view_id)
            fts.apply_head_changes(db, view_id, changes, deadline=deadline)
            fts.verify_head_index(db, view_id)
            self._fts_guard.freeze_head()
            logical, physical = fts.head_proof(db, view_id, rows, deadline)
            if expected_logical is not None and logical != expected_logical:
                v.fail('rollback_fts_mismatch')
            generation = dict(generation, ordinal=ordinal, logical_fts_digest=logical, physical_fts_digest=physical)
            generation['seal_digest'] = seal_digest(_seal_fields(self._incarnation, generation))
            columns = tuple(generation)
            db.execute(f"INSERT INTO s_generations({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                       tuple(generation[k] for k in columns))
            for old, new in deltas:
                _expired(deadline)
                store_module._apply_card_delta(db, view_id, ordinal, old, new)
            if member_digest(self._rows_open(db, view_id)) != generation['member_digest']:
                v.fail('generation_rows_mismatch')
            if head.ordinal is None:
                db.execute('INSERT INTO s_heads(view_id,ordinal,epoch) VALUES (?,?,1)', (view_id, ordinal))
            elif db.execute('UPDATE s_heads SET ordinal=?,epoch=? WHERE view_id=? AND ordinal=? AND epoch=?',
                            (ordinal, generation['epoch'], view_id, head.ordinal, head.epoch)).rowcount != 1:
                v.fail('view_head_conflict')
            before_commit(db)
            _expired(deadline)
            db.commit()
        return generation

    # -- rollback and view row collection -------------------------------------

    def _rows_rollback(self, handle, *, expected_head, deadline):
        """A new generation that applies the head's deltas in reverse.

        The result must reproduce the prior generation's logical members and
        FTS corpus; its physical FTS bytes are sealed anew. The caller holds
        the owner proof and its explicit approval.
        """
        import laya_view_store as store_module
        _deadline_value(deadline)
        self._require_write()
        entry = self._check(handle)
        base = vals.expected_head(expected_head, handle.view_id, self._incarnation)
        self._observe(entry, deadline=deadline)
        with self._session(write=True) as db:
            head = self._rows_head_row(db, handle.view_id)
            if (head.gen_id, head.epoch) != (base['gen_id'], base['epoch']) or head.gen_id is None:
                v.fail('view_head_conflict')
            current, _ = self._rows_verify(db, head, handle, deadline)
            if current['prior_ordinal'] is None:
                v.fail('rollback_prior_missing')
            prior = db.execute('SELECT * FROM s_generations WHERE view_id=? AND ordinal=?',
                               (handle.view_id, current['prior_ordinal'])).fetchone()
            if prior is None:
                v.fail('rollback_prior_missing')
            prior = dict(prior)
            if seal_digest(_seal_fields(self._incarnation, prior)) != prior['seal_digest']:
                v.fail('generation_seal_mismatch')
            deltas = []
            for rel, old_json, new_json in db.execute(
                    'SELECT rel,old_json,new_json FROM s_deltas WHERE view_id=? AND ordinal=? '
                    'ORDER BY key_json DESC', (handle.view_id, head.ordinal)).fetchall():
                if rel != 'card':
                    v.fail('row_format_unsupported')
                old = None if old_json is None else v.parse_canonical(old_json)
                new = None if new_json is None else v.parse_canonical(new_json)
                for value in (old, new):
                    if value is not None:
                        v.exact(value, CARD_FIELDS)
                deltas.append((new, old))
            restored = self._rows_open(db, handle.view_id, prior['ordinal'])
            if member_digest(restored) != prior['member_digest']:
                v.fail('generation_rows_mismatch')
            rows = self._rows_fts_rows(restored, cache={}, deadline=deadline)
            generation = dict(gen_id=vals.gen_id(uuid.uuid4().hex, v.digest('memvane-row-rollback-v1', dict(
                                  view_id=handle.view_id, head=head.gen_id, prior=prior['gen_id']))),
                              view_id=handle.view_id, epoch=head.epoch + 1, prior_ordinal=head.ordinal,
                              kind='rollback', sidecar_present=prior['sidecar_present'],
                              capture_json=prior['capture_json'], policy_digest=prior['policy_digest'],
                              skip_digest=prior['skip_digest'], recipe_id=prior['recipe_id'],
                              profile_id=prior['profile_id'], coverage=prior['coverage'],
                              member_digest=prior['member_digest'], source_digest=prior['source_digest'])

            def before_commit(db):
                job = db.execute("SELECT job_id FROM s_stage_jobs WHERE view_id=? AND state<>'abandon'",
                                 (handle.view_id,)).fetchone()
                if job is not None:
                    store_module._stage_mark_abandon(db, job[0])

            written = self._rows_write(db, head, generation, deltas, rows, before_commit, deadline,
                                       expected_logical=prior['logical_fts_digest'])
        return dict(view_id=handle.view_id, gen_id=written['gen_id'], epoch=written['epoch'],
                    incarnation=self._incarnation)

    def _rows_collect(self, handle, *, limit, deadline):
        """Delete at most ``limit`` rows per relation no longer visible in the head or its prior.

        One batch is one transaction. Shared content (blobs, attestations,
        chunk and embedding sets) is never deleted here.
        """
        import laya_view_store as store_module
        _deadline_value(deadline)
        self._require_write()
        v.integer(limit, 1, 4096)
        self._check(handle)
        removed = {}
        with self._session(write=True) as db:
            head = self._rows_head_row(db, handle.view_id)
            if head.ordinal is None:
                return removed
            prior = db.execute('SELECT prior_ordinal FROM s_generations WHERE view_id=? AND ordinal=?',
                               (handle.view_id, head.ordinal)).fetchone()[0]
            keep = prior if prior is not None else head.ordinal
            job = db.execute("SELECT job_id FROM s_stage_jobs WHERE view_id=? AND state='abandon'",
                             (handle.view_id,)).fetchone()
            if job is not None:
                removed['stage'] = 0
                while True:
                    _expired(deadline)
                    step = store_module._stage_discard_step(db, job[0], limit=min(limit, 64))
                    removed['stage'] += step
                    if not step or removed['stage'] >= limit:
                        break
            for table in ('s_card_sources', 's_sources', 's_cards'):
                _expired(deadline)
                removed[table] = db.execute(
                    f'DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE view_id=? '
                    'AND gen_to IS NOT NULL AND gen_to<=? ORDER BY gen_to,rowid LIMIT ?)',
                    (handle.view_id, keep, limit)).rowcount
            db.execute('CREATE TEMP TABLE IF NOT EXISTS tmp_gc_delta(view_id TEXT, ordinal INTEGER, rel TEXT, '
                       'key_json TEXT, PRIMARY KEY(view_id,ordinal,rel,key_json))')
            db.execute('DELETE FROM temp.tmp_gc_delta')
            db.execute('INSERT INTO temp.tmp_gc_delta(view_id,ordinal,rel,key_json) '
                       'SELECT view_id,ordinal,rel,key_json FROM s_deltas WHERE view_id=? AND ordinal<>? '
                       'AND ordinal IS NOT ? ORDER BY ordinal,rel,key_json LIMIT ?',
                       (handle.view_id, head.ordinal, prior, limit))
            for table in ('s_delta_blobs', 's_delta_observations', 's_deltas'):
                removed[table] = db.execute(
                    f'DELETE FROM {table} WHERE (view_id,ordinal,rel,key_json) IN '
                    '(SELECT view_id,ordinal,rel,key_json FROM temp.tmp_gc_delta)').rowcount
            db.execute('DELETE FROM temp.tmp_gc_delta')
            removed['s_generations'] = db.execute(
                'DELETE FROM s_generations WHERE ordinal IN (SELECT g.ordinal FROM s_generations g '
                'WHERE g.view_id=? AND g.ordinal<>? AND g.ordinal IS NOT ? '
                'AND NOT EXISTS(SELECT 1 FROM s_deltas d WHERE d.view_id=g.view_id AND d.ordinal=g.ordinal) '
                'AND NOT EXISTS(SELECT 1 FROM s_stage_jobs j WHERE j.base_gen_id=g.gen_id) '
                'AND NOT EXISTS(SELECT 1 FROM s_parity_anchor a WHERE a.gen_id=g.gen_id) '
                'AND NOT EXISTS(SELECT 1 FROM delete_receipts r WHERE r.base_gen=g.gen_id) '
                'ORDER BY g.ordinal LIMIT ?)', (handle.view_id, head.ordinal, prior, limit)).rowcount
            _expired(deadline)
        return removed

    # -- reads ---------------------------------------------------------------

    def _rows_verify(self, db, head, live_handle, deadline):
        """Recompute the head seal from its stored rows and FTS bytes."""
        _expired(deadline)
        generation = db.execute('SELECT * FROM s_generations WHERE view_id=? AND ordinal=?',
                                (live_handle.view_id, head.ordinal)).fetchone()
        if generation is None or generation['gen_id'] != head.gen_id or generation['epoch'] != head.epoch:
            v.fail('view_head_invalid')
        generation = dict(generation)
        if (generation['kind'] not in ('normal', 'rollback') or generation['sidecar_present']
                or generation['coverage'] != 'lexical'):
            v.fail('row_format_unsupported')
        handle, entry, saved = self._sealed_handle(
            db, dict(checkout_evidence=generation['capture_json'], view_id=generation['view_id']), live_handle)
        if generation['policy_digest'] != self._policy:
            v.fail('generation_binding_mismatch')
        rows = self._rows_open(db, generation['view_id'], generation['ordinal'])
        if member_digest(rows) != generation['member_digest']:
            v.fail('generation_rows_mismatch')
        cache, checked = {}, {'blobs': set(), 'worktrees': {}, 'captures': set(), 'fingerprints': {}}
        for row in rows:
            _expired(deadline)
            if row['eligible']:
                if row['trust_digest'] is None:
                    v.fail('card_trust_mismatch')
                spec = self._attestation(row['trust_digest'], cache=checked)
                if spec['blob_sha'] != row['blob_sha'] or spec['policy_digest'] != handle.policy_digest:
                    v.fail('card_trust_mismatch')
        logical, physical = fts.head_proof(db, generation['view_id'], self._rows_fts_rows(rows, cache=cache,
                                                                                         deadline=deadline),
                                           deadline, expected_storage=generation['physical_fts_digest'])
        if (logical != generation['logical_fts_digest']
                or seal_digest(_seal_fields(self._incarnation, generation)) != generation['seal_digest']):
            v.fail('generation_seal_mismatch')
        return generation, entry['capture']

    def _rows_verified(self, db, head, live_handle, deadline, record=None):
        stamp = self._commit_stamp(db)
        cached = None if record is None else record.get('verified')
        if cached is not None and cached['stamp'] == stamp and cached['ordinal'] == head.ordinal:
            return cached['generation'], cached['capture']
        generation, capture = self._rows_verify(db, head, live_handle, deadline)
        if record is not None:
            record['verified'] = dict(stamp=stamp, ordinal=head.ordinal, generation=generation, capture=capture)
        return generation, capture

    def _rows_pin(self, handle, deadline, observed):
        from laya_view_reader import MAX_PINS, ViewPin, _remaining
        issued = self._check(handle)
        if not observed:
            self._observe(issued, deadline=deadline)
        _remaining(deadline)
        if len(self._pins) >= MAX_PINS:
            v.fail('view_pin_limit')
        with self._reading(deadline) as db:
            head = self._rows_head_row(db, handle.view_id)
            if head.gen_id is None:
                v.fail('view_preparing')
            record = {}
            generation, _ = self._rows_verified(db, head, handle, deadline, record)
        if not observed:
            self._observe(issued, deadline=deadline)
        _remaining(deadline)
        pin = ViewPin(handle.view_id, head.gen_id, head.epoch, self._incarnation, uuid.uuid4().hex)
        copied = self._handle_type(**{k: getattr(handle, k) for k in handle.__dataclass_fields__})
        entry = dict(handle=copied, capture=json.loads(v.canonical(issued['capture'])), binding=issued['binding'])
        self._pins[pin.token] = dict(pin=pin, fields=(pin.view_id, pin.gen_id, pin.epoch, pin.incarnation, pin.token),
                                     entry=entry, seal=generation['seal_digest'], vector=None,
                                     verified=record['verified'], ordinal=head.ordinal)
        return pin

    def _rows_search(self, pin, fts_query, limit, *, path_prefixes, excluded_paths, deadline_seconds,
                     legacy_origin, observed):
        import sqlite3
        from laya_view_reader import _query, _remaining
        if legacy_origin is not None:
            v.fail('migration_lexical_origin_invalid')
        if type(observed) is not bool:
            v.fail('fts_query_invalid')
        deadline = _query(fts_query, limit, path_prefixes, excluded_paths, deadline_seconds)
        record = self._pinned(pin)
        _remaining(deadline)
        entry = record['entry']
        if not observed:
            self._observe(entry, deadline=deadline)
        _remaining(deadline)
        with self._reading(deadline) as db:
            head = self._rows_head_row(db, pin.view_id)
            if (head.ordinal, head.gen_id, head.epoch) != (record['ordinal'], pin.gen_id, pin.epoch):
                v.fail('view_head_changed')
            generation, original = self._rows_verified(db, head, entry['handle'], deadline, record)
            if generation['seal_digest'] != record['seal']:
                v.fail('view_pin_changed')
            name = fts.head_table(pin.view_id)
            args = [pin.view_id, fts_query]
            conditions = []
            if path_prefixes is not None:
                if not path_prefixes:
                    conditions.append('0')
                else:
                    conditions.append('(' + ' OR '.join('(c.path=? OR substr(c.path,1,?)=?)'
                                                        for _ in path_prefixes) + ')')
                    for path in path_prefixes:
                        args.extend((path, len(path) + 1, path + '/'))
            if excluded_paths:
                conditions.append('c.path NOT IN (' + ','.join('?' for _ in excluded_paths) + ')')
                args.extend(excluded_paths)
            extra = ''.join(' AND ' + clause for clause in conditions)
            args.append(limit)
            try:
                rows = db.execute(f'''SELECT c.path,c.blob_sha,c.title AS display_title,
                    bm25({name},1.5,4.0,1.0,0.0) AS score FROM {name}
                    JOIN s_cards c ON c.view_id=? AND c.gen_to IS NULL AND c.path={name}.path_key
                    WHERE {name} MATCH ? AND c.eligible=1 {extra}
                    ORDER BY score ASC,c.path COLLATE BINARY ASC LIMIT ?''', args).fetchall()
            except sqlite3.Error:
                _remaining(deadline)
                v.fail('fts_query_invalid')
            state = self._snapshot_state(entry['capture'], v.parse_canonical(generation['capture_json']), deadline)
            answer = [dict(path=r['path'], blob_sha=r['blob_sha'], body=self._blob(r['blob_sha']),
                           display_title=r['display_title'], score=r['score'], view_id=pin.view_id,
                           gen_id=pin.gen_id, evidence='commitment-only', owner_verified=False,
                           snapshot_state=state) for r in rows]
            _remaining(deadline)
        if not observed:
            self._observe(entry, deadline=deadline)
        _remaining(deadline)
        return answer

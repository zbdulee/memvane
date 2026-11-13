"""Capture-bound generation commitments for the inactive candidate format.

Handles are store-issued objects bound to one checkout/settings observation.
Manifests, fingerprints and receipts are caller-owned commitments: a sealed
generation proves structural consistency, never compiler truth, source
observation or operational/deletion authority. Vector coverage is zero or one
reference per eligible member to shared immutable sets. No GC or activation.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import time
import uuid

from memvane.store.blob import blob_values as v
from memvane.store.checkout import checkout_view as checkout
from memvane.store.blob.blob_values import BlobStoreError
from memvane.store.blob.blob_store import (BlobStore, _bound_settings, _current_file_node, _insert, _regular,
                             _worktree_record, _worktree_row)
from memvane.store.view import view_fts as view_fts
from memvane.store.view import view_values as vals
from memvane.store.view import view_vectors as vectors
from memvane.store.view.view_fill import FillMixin
from memvane.store.view.view_reader import ReaderMixin
from memvane.store.view.view_rows import RowsMixin
from memvane.store.view.view_vectors import VectorMixin

# Publication cut budget; only an O(1) source identity and the commit run under it.
_CUT_SECONDS = 1.0


def _apply_card_delta(db, view_id, ordinal, old, new):
    """Persist one checked card interval and rollback references in caller's transaction.

    The caller proves and seals the generation, trust, head, FTS and checkout;
    this helper does not publish or authorize any of them.
    """
    v.identifier(view_id);v.integer(ordinal,1)
    if not db.in_transaction:
        v.fail('generation_transaction_required')
    fields=('path','blob_sha','title','eligible','trust_digest','metadata_json')
    if old is None and new is None:v.fail('card_delta_invalid')
    for value in (old,new):
        if value is None:continue
        v.exact(value,fields)
        vals.card_path(value['path']);v.identifier(value['blob_sha']);v.text(value['title'])
        if type(value['eligible']) is not int or value['eligible'] not in (0,1):
            v.fail('card_delta_invalid')
        if value['trust_digest'] is not None:v.identifier(value['trust_digest'])
        if value['eligible'] and value['trust_digest'] is None:v.fail('card_delta_invalid')
        v.parse_canonical(value['metadata_json'])
    path=(old if old is not None else new)['path']
    if old is not None and new is not None and old['path']!=new['path']:
        v.fail('card_delta_invalid')
    generation=db.execute('SELECT prior_ordinal FROM s_generations WHERE view_id=? AND ordinal=?', (view_id,ordinal)).fetchone()
    if generation is None:
        v.fail('generation_missing')
    # A view's first generation has no prior to roll back to; its rows are
    # the whole corpus, so it records no rollback delta.
    reversible=generation[0] is not None
    row=db.execute('SELECT path,blob_sha,title,eligible,trust_digest,metadata_json,gen_from '
                   'FROM s_cards WHERE view_id=? AND path=? AND gen_to IS NULL', (view_id,path)).fetchone()
    actual=None if row is None else dict(zip(fields,tuple(row[:len(fields)])))
    if actual!=old or (row is not None and row[len(fields)]>=ordinal):
        v.fail('card_delta_conflict')
    if old==new:return False
    key=v.canonical([path])
    db.execute('SAVEPOINT s_card_delta')
    try:
        if old is not None:
            changed=db.execute('UPDATE s_cards SET gen_to=? WHERE view_id=? AND path=? AND gen_to IS NULL',
                               (ordinal,view_id,path)).rowcount
            if changed!=1:v.fail('card_delta_conflict')
        if new is not None:
            db.execute('INSERT INTO s_cards(view_id,path,gen_from,gen_to,blob_sha,title,eligible,trust_digest,metadata_json) '
                       'VALUES (?,?,?,NULL,?,?,?,?,?)',
                       (view_id,new['path'],ordinal,new['blob_sha'],new['title'],new['eligible'],
                        new['trust_digest'],new['metadata_json']))
        if reversible:
            db.execute('INSERT INTO s_deltas(view_id,ordinal,rel,key_json,old_json,new_json) VALUES (?,?,?,?,?,?)',
                       (view_id,ordinal,'card',key,None if old is None else v.canonical(old),
                        None if new is None else v.canonical(new)))
            for side,value in (('old',old),('new',new)):
                if value is not None:
                    db.execute('INSERT INTO s_delta_blobs(view_id,ordinal,rel,key_json,side,blob_sha) VALUES (?,?,?,?,?,?)',
                               (view_id,ordinal,'card',key,side,value['blob_sha']))
    except BaseException:
        db.execute('ROLLBACK TO s_card_delta');db.execute('RELEASE s_card_delta')
        raise
    db.execute('RELEASE s_card_delta')
    return True


def _stage_start_job(db, identity):
    """Create or resume a structurally identical stage under a caller proof."""
    fields = ('view_id', 'job_id', 'incarnation', 'base_gen_id', 'base_epoch',
              'capture_json', 'policy_digest', 'scan_digest')
    v.exact(identity, fields)
    if not db.in_transaction:
        v.fail('stage_transaction_required')
    v.identifier(identity['view_id']); v.identifier(identity['job_id'], v.UUID)
    v.identifier(identity['incarnation'], v.UUID)
    v.identifier(identity['policy_digest']); v.identifier(identity['scan_digest'])
    epoch, gen = v.integer(identity['base_epoch']), identity['base_gen_id']
    if ((epoch == 0) != (gen is None) or
            (gen is not None and (not isinstance(gen, str) or not vals.GEN_ID.fullmatch(gen)))):
        v.fail('stage_base_invalid')
    if not isinstance(v.parse_canonical(identity['capture_json']), dict):
        v.fail('stage_capture_invalid')
    row = db.execute('SELECT view_id,job_id,incarnation,base_gen_id,base_epoch,capture_json,'
                     'policy_digest,scan_digest,state FROM s_stage_jobs WHERE view_id=?',
                     (identity['view_id'],)).fetchone()
    if row is not None:
        if tuple(row[:len(fields)]) != tuple(identity[key] for key in fields):
            v.fail('stage_identity_changed')
        if row[len(fields)] != 'collecting':
            v.fail('stage_job_not_collecting')
        return False
    db.execute('INSERT INTO s_stage_jobs '
               '(view_id,job_id,incarnation,base_gen_id,base_epoch,capture_json,policy_digest,'
               'scan_digest,scan_complete,state) VALUES (?,?,?,?,?,?,?,?,1,?)',
               (*(identity[key] for key in fields), 'collecting'))
    return True


def _stage_put_item(db, job_id, item):
    """Insert one stage action or accept an identical replay in caller's transaction.

    The owning writer must prove the job's view, scan, head and capture and
    advance its cursor in the same transaction; this helper proves none of them.
    """
    v.identifier(job_id, v.UUID)
    item = vals.check_stage_item(item)
    if not db.in_transaction:
        v.fail('stage_transaction_required')
    job = db.execute('SELECT state FROM s_stage_jobs WHERE job_id=?', (job_id,)).fetchone()
    if job is None or job[0] != 'collecting':
        v.fail('stage_job_not_collecting')
    fields = vals.STAGE_ITEM_FIELDS
    old = db.execute('SELECT path,action,input_sha,blob_sha,trust_digest,metadata_json '
                     'FROM s_stage_items WHERE job_id=? AND path=?', (job_id, item['path'])).fetchone()
    if old is not None:
        if tuple(old) != tuple(item[key] for key in fields):
            v.fail('stage_replay_mismatch')
        return False
    db.execute('INSERT INTO s_stage_items '
               '(job_id,path,action,input_sha,blob_sha,trust_digest,metadata_json) '
               'VALUES (?,?,?,?,?,?,?)', (job_id, *(item[key] for key in fields)))
    return True


def _stage_put_batch(db, job_id, expected_cursor, observed_paths, items):
    """Write one verified scan page and its cursor in the caller's transaction.

    Callers prove the page, current capture and scan identity before entering;
    no filesystem or authority proof is performed by this helper.
    """
    v.identifier(job_id, v.UUID)
    if not db.in_transaction:
        v.fail('stage_transaction_required')
    if expected_cursor is not None:
        vals.card_path(expected_cursor)
    if type(observed_paths) is not list or not 1 <= len(observed_paths) <= 64 or type(items) is not list:
        v.fail('stage_page_invalid')
    for path in observed_paths:
        vals.card_path(path)
    order = [path.encode('utf8') for path in observed_paths]
    if (order != sorted(set(order)) or
            (expected_cursor is not None and order[0] <= expected_cursor.encode('utf8'))):
        v.fail('stage_page_invalid')
    checked = [vals.check_stage_item(item) for item in items]
    paths = [item['path'] for item in checked]
    if len(set(paths)) != len(paths) or not set(paths).issubset(observed_paths):
        v.fail('stage_page_invalid')
    row = db.execute('SELECT state,cursor_path FROM s_stage_jobs WHERE job_id=?', (job_id,)).fetchone()
    if row is None or row[0] != 'collecting' or row[1] != expected_cursor:
        v.fail('stage_cursor_conflict')
    db.execute('SAVEPOINT s_stage_page')
    try:
        for item in checked:
            _stage_put_item(db, job_id, item)
        count = db.execute('SELECT COUNT(*) FROM s_stage_items WHERE job_id=?', (job_id,)).fetchone()[0]
        if count > vals.MAX_MEMBERS:
            v.fail('stage_items_too_many')
        changed = db.execute('UPDATE s_stage_jobs SET cursor_path=? '
                             'WHERE job_id=? AND state=? AND cursor_path IS ?',
                             (observed_paths[-1], job_id, 'collecting', expected_cursor)).rowcount
        if changed != 1:
            v.fail('stage_cursor_conflict')
    except BaseException:
        db.execute('ROLLBACK TO s_stage_page')
        db.execute('RELEASE s_stage_page')
        raise
    db.execute('RELEASE s_stage_page')


def _stage_apply_page(db, identity, expected_cursor, observed_paths, items):
    """Bind a page to the job identity and write both under one savepoint.

    The caller must hold the existing writer scope and verify the actual
    capture, complete scan, page bytes and head before calling and committing.
    """
    if not db.in_transaction:
        v.fail('stage_transaction_required')
    db.execute('SAVEPOINT s_stage_apply')
    try:
        _stage_start_job(db, identity)
        _stage_put_batch(db, identity['job_id'], expected_cursor, observed_paths, items)
    except BaseException:
        db.execute('ROLLBACK TO s_stage_apply')
        db.execute('RELEASE s_stage_apply')
        raise
    db.execute('RELEASE s_stage_apply')


def _stage_mark_abandon(db, job_id):
    """Mark the caller-proved stage for bounded cleanup; no content is removed."""
    v.identifier(job_id, v.UUID)
    if not db.in_transaction:
        v.fail('stage_transaction_required')
    row = db.execute('SELECT state FROM s_stage_jobs WHERE job_id=?', (job_id,)).fetchone()
    if row is None:
        v.fail('stage_job_missing')
    if row[0] == 'abandon':
        return False
    if row[0] not in ('collecting', 'ready'):
        v.fail('stage_job_invalid')
    db.execute("UPDATE s_stage_jobs SET state='abandon',discard_relation='card_source',discard_key=NULL "
               'WHERE job_id=?', (job_id,))
    return True


def _stage_discard_step(db, job_id, *, limit):
    """Remove up to limit stage children. Caller proves writer and claim exclusion."""
    v.identifier(job_id, v.UUID)
    v.integer(limit, 1, 64)
    if not db.in_transaction:
        v.fail('stage_transaction_required')
    row = db.execute('SELECT state,discard_relation FROM s_stage_jobs WHERE job_id=?', (job_id,)).fetchone()
    if row is None or row[0] != 'abandon':
        v.fail('stage_job_not_abandoned')
    relations = (('card_source', 's_stage_card_source_changes', 'card_path,source_path'),
                 ('source', 's_stage_source_changes', 'path'),
                 ('item', 's_stage_items', 'path'))
    names = [entry[0] for entry in relations] + ['job']
    if row[1] not in names:
        v.fail('stage_discard_invalid')
    index = names.index(row[1])
    while index < len(relations):
        name, table, order = relations[index]
        rows = db.execute(f'SELECT rowid,{order} FROM {table} WHERE job_id=? ORDER BY {order} LIMIT ?',
                          (job_id, limit)).fetchall()
        if rows:
            for child in rows:
                db.execute(f'DELETE FROM {table} WHERE job_id=? AND rowid=?', (job_id, child[0]))
            key = v.canonical(list(rows[-1][1:]))
            db.execute('UPDATE s_stage_jobs SET discard_relation=?,discard_key=? WHERE job_id=?',
                       (name, key, job_id))
            return len(rows)
        index += 1
        db.execute('UPDATE s_stage_jobs SET discard_relation=?,discard_key=NULL WHERE job_id=?',
                   (names[index], job_id))
    db.execute('DELETE FROM s_stage_jobs WHERE job_id=? AND state=?', (job_id, 'abandon'))
    return 0


@dataclass(frozen=True)
class ViewHandle:
    view_id: str
    worktree_id: str
    branch_key: str
    head_oid: str | None
    checkout_digest: str
    policy_digest: str
    incarnation: str
    token: str


def _skip_patterns(saved):
    """Current ``skipPaths`` of a bound worktree; the binding itself must still hold.

    ``skipPaths`` is outside the binding: a prepared generation seals the
    patterns it used in its stage, and the next prepare reads the current ones.
    """
    current = _bound_settings(saved)
    try:
        return vals.skip_patterns(current['config'], current['raw_config'])
    except BlobStoreError:
        raise
    except (ValueError, TypeError, KeyError):
        v.fail('wiki_settings_invalid')


def _wiki_files(saved, members):
    """Bounded FD reads of the exact admitted wiki bytes; never under the writer lock."""
    files, total = {}, 0
    for member in members:
        try:
            raw, node, rootnode = _regular(saved['wiki_root'], member['path'], v.MAX_BODY)
        except OSError:
            v.fail('wiki_member_unavailable')
        if not checkout._persistent_directory_equal({'dev': saved['wiki_dev'], 'ino': saved['wiki_ino']}, rootnode):
            v.fail('wiki_root_changed')
        if v.hash_bytes(raw) != member['blob_sha']:
            v.fail('wiki_member_changed')
        total += len(raw)
        if total > vals.MAX_WIKI_BYTES:
            v.fail('generation_too_large')
        files[member['path']] = node
    return files


def _live(db, saved, files):
    """Stat/settings recheck just before a commit; observation, not a snapshot."""
    row = db.execute('SELECT * FROM worktrees WHERE worktree_id=?', (saved['worktree_id'],)).fetchone()
    if row is None or dict(row) != saved:
        v.fail('wiki_binding_changed')
    _bound_settings(saved)
    try:
        current = {path: _current_file_node(saved['wiki_root'], path) for path in files}
    except OSError:
        v.fail('wiki_member_changed')
    if current != files:
        v.fail('wiki_member_changed')


def _current_capture(saved, repository, policy, expected, *, deadline=None):
    """Re-observe Git/config identity before commit without a nested DB session."""
    record = _worktree_record(saved, repository, policy)
    current, observation = _worktree_row(saved['project_root'], repository, record, deadline=deadline, saved=saved)
    if (any(saved[k] != value for k, value in current.items())
            or observation['capture'] != expected
            or not checkout.capture_matches(expected, repository, record, fresh=True, deadline=deadline,
                                            _raise_timeout=deadline is not None)):
        v.fail('checkout_changed')


class ViewStore(RowsMixin, FillMixin, VectorMixin, ReaderMixin, BlobStore):
    _handle_type = ViewHandle

    @classmethod
    def open(cls, path, *, repository, scope_id):
        store = super().open(path, repository=repository, scope_id=scope_id)
        return cls._initialize_reader_state(store)

    @classmethod
    def open_reader(cls, path, *, repository, scope_id, deadline=None):
        store = cls._open(path, repository=repository, scope_id=scope_id, reader_only=True,
                          deadline=deadline)
        return cls._initialize_reader_state(store)

    @staticmethod
    def _initialize_reader_state(store):
        store._handles = {}
        store._pins = {}
        store._closed = False
        return store

    def close(self):
        self._closed = True
        self._pins = {}
        self._handles = {}
        super().close()

    def _observe_worktree(self, worktree_id, *, deadline=None):
        with self._session() as db:
            row = db.execute('SELECT * FROM worktrees WHERE worktree_id=?', (worktree_id,)).fetchone()
            if row is None:
                v.fail('worktree_missing')
            saved = dict(row)
            record = _worktree_record(row, self._repository, self._policy)
        current, observation = _worktree_row(saved['project_root'], self._repository, record, deadline=deadline, saved=saved)
        if any(saved[k] != val for k, val in current.items()):
            v.fail('wiki_binding_changed')
        capture = observation['capture']
        if capture['worktreeId'] != worktree_id or capture['repoId'] != self._repository['repoId']:
            v.fail('checkout_changed')
        return saved, capture

    def _check(self, handle):
        token = getattr(handle, 'token', None)
        entry = self._handles.get(token) if type(handle) is ViewHandle and isinstance(token, str) else None
        if entry is None or entry['handle'] is not handle or handle.incarnation != self._incarnation:
            v.fail('view_handle_invalid')
        capture = entry['capture']
        expected = dict(view_id=capture['viewId'], worktree_id=capture['worktreeId'],
                        branch_key=capture['branchKey'], head_oid=capture['headOid'],
                        checkout_digest=capture['checkoutDigest'], policy_digest=self._policy)
        if vals.view_fields(handle) != expected:
            v.fail('view_handle_invalid')
        return entry

    def _observe(self, entry, *, deadline=None, timeout_reason='fts_deadline_exceeded'):
        try:
            saved, capture = self._observe_worktree(entry['handle'].worktree_id, deadline=deadline)
        except checkout.CheckoutUnavailable:
            v.fail(timeout_reason)
        if capture != entry['capture'] or saved['binding_digest'] != entry['binding']:
            v.fail('checkout_changed')
        return saved

    def capture_view(self, worktree_id, *, deadline=None):
        v.identifier(worktree_id, v.UUID)
        if deadline is not None:
            try:
                deadline=checkout._deadline(deadline)
            except checkout.CheckoutUnavailable as exc:
                v.fail('fts_deadline_exceeded' if exc.reason=='checkout_budget_exhausted'
                       else 'fts_query_invalid')
        try:
            saved, capture = self._observe_worktree(worktree_id,deadline=deadline)
        except checkout.CheckoutUnavailable:
            v.fail('fts_deadline_exceeded')
        if deadline is not None and time.monotonic()>=deadline:
            v.fail('fts_deadline_exceeded')
        handle = ViewHandle(view_id=v.identifier(capture['viewId']), worktree_id=worktree_id,
                            branch_key=capture['branchKey'], head_oid=capture['headOid'],
                            checkout_digest=capture['checkoutDigest'], policy_digest=saved['policy_digest'],
                            incarnation=self._incarnation, token=uuid.uuid4().hex)
        while len(self._handles) >= vals.MAX_HANDLES:
            self._handles.pop(next(iter(self._handles)))
        self._handles[handle.token] = dict(handle=handle, capture=capture, binding=saved['binding_digest'])
        return handle

    def _head(self, db, handle):
        return self._rows_head(db, handle)

    def rollback_generation(self, handle, *, expected_head, deadline=None):
        """Row format only: reverse the head's last publication as a new generation."""
        return self._rows_rollback(handle, expected_head=expected_head, deadline=deadline)


    def collect_view_rows(self, handle, *, limit=256, deadline=None):
        """Row format only: one bounded batch of view rows outside the head and its prior."""
        return self._rows_collect(handle, limit=limit, deadline=deadline)

    def head(self, handle):
        entry = self._check(handle)
        self._observe(entry)
        with self._session() as db:
            return self._head(db, handle)

    def _card(self, handle, member, current, *, cache=None):
        """Original attestation refs stay unchanged; eligibility is derived only."""
        if member['trust_digest'] is None:
            return 0, []
        spec = self._attestation(member['trust_digest'], cache=cache)
        if spec['blob_sha'] != member['blob_sha'] or spec['policy_digest'] != handle.policy_digest:
            v.fail('card_trust_mismatch')
        # A writer-ingested legacy_verified card keeps its reader rule: its
        # unbound sources are not compared with the current scan (no HEAD or
        # raw reread is claimed); anything but fully unbound sources is ineligible.
        unbound = spec['verification_kind'] == 'legacy_verified'
        ok, refs = bool(spec['sources']), []
        for ordinal, source in enumerate(spec['sources']):
            old = self._fingerprint(source['fingerprint_id'], cache=cache)
            if (old['worktree_id'], old['branch_key']) != (handle.worktree_id, handle.branch_key):
                v.fail('foreign_card_trust')
            refs.append(dict(ordinal=ordinal, fingerprint_id=source['fingerprint_id'], spans=source['spans']))
            if unbound:
                if (old['binding_state'] != 'legacy_unbound' or old['head_oid'] is not None
                        or old['tracked_state'] != 'unknown'):
                    ok = False
                continue
            now = current.get(old['path'])
            if (now is None or now[0]['presence'] != 'present'
                    or 'legacy_unbound' in (old['binding_state'], now[1]['binding_state'])
                    or (now[1]['source_sha'], now[1]['byte_length']) != (old['source_sha'], old['byte_length'])):
                ok = False
        return int(ok), refs

    def _derive(self, handle, manifest, skips, *, retain_bodies=True, deadline=None):
        """Metadata-only derivation inside a short read transaction."""
        if deadline is not None and time.monotonic() >= deadline:
            v.fail('fts_deadline_exceeded')
        if not self._db.in_transaction:
            v.fail('candidate_transaction_required')
        cache = {'blobs': set(), 'worktrees': {}, 'captures': set(), 'fingerprints': {}}
        current = {}
        for source in manifest['sources']:
            if deadline is not None and time.monotonic() >= deadline:
                v.fail('fts_deadline_exceeded')
            fp = self._fingerprint(source['fingerprint_id'], cache=cache)
            if (fp['worktree_id'], fp['branch_key'], fp['path']) != (handle.worktree_id, handle.branch_key, source['path']):
                v.fail('source_binding_mismatch')
            if source['presence'] == 'present' and (fp['scan_id'] != manifest['scan_id']
                                                    or fp['head_oid'] != handle.head_oid):
                v.fail('source_observation_stale')
            if not vals.skipped(source['path'], skips):
                current[source['path']] = (source, fp)
        members, cards, bodies, body_bytes, total = [], [], {}, {}, 0
        # These caches live only within this derivation/read snapshot. Shared
        # bytes never share trust unless the exact attestation also matches.
        verified_bodies, verified_cards = {}, {}
        for member in manifest['members']:
            if deadline is not None and time.monotonic() >= deadline:
                v.fail('fts_deadline_exceeded')
            blob = member['blob_sha']
            if blob not in verified_bodies:
                body = self._blob(blob, cache=cache)
                cache['blobs'].add(blob)
                verified_bodies[blob] = body if retain_bodies else len(body.encode('utf8'))
            value = verified_bodies[blob]
            size = len(value.encode('utf8')) if retain_bodies else value
            key = (blob, member['trust_digest'])
            if key not in verified_cards:
                verified_cards[key] = self._card(handle, member, current, cache=cache)
            eligible, refs = verified_cards[key]
            if vals.skipped(member['path'], skips):
                continue
            total += size
            if total > vals.MAX_WIKI_BYTES:
                v.fail('generation_too_large')
            members.append(dict(member, eligible=eligible))
            cards.extend(dict(wiki_path=member['path'], **ref) for ref in refs)
            if eligible:
                body_bytes[member['path']] = size
                if retain_bodies:
                    bodies[member['path']] = value
        if deadline is not None and time.monotonic() >= deadline:
            v.fail('fts_deadline_exceeded')
        return dict(members=sorted(members, key=lambda m: m['path']),
                    sources=sorted((s for s, _ in current.values()), key=lambda s: s['path']),
                    cards=sorted(cards, key=lambda c: (c['wiki_path'], c['ordinal'])),
                    bodies=bodies, body_bytes=body_bytes,
                    verified_bodies=verified_bodies if retain_bodies else {})



    def prepare_generation(self, handle, manifest, *, expected_head, vector_spec=None,
                           deadline=None):
        return self._rows_prepare(handle, manifest, expected_head=expected_head, vector_spec=vector_spec,
                                  deadline=deadline)



    def publish_generation(self, handle, gen_id, *, expected_head, deadline=None):
        return self._rows_publish(handle, gen_id, expected_head=expected_head,
                                  deadline=deadline)


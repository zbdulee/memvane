"""Pinned lexical reads of immutable candidate generations, never live authority."""
from contextlib import contextmanager
from dataclasses import dataclass
import json
import math
import sqlite3
import time
import uuid

import laya_blob_values as v
from laya_blob_values import BlobStoreError
from laya_blob_store import _worktree_record
import laya_checkout_view as checkout
import laya_view_fts as fts
import laya_view_values as vals

MAX_PINS = 128


@dataclass(frozen=True)
class ViewPin:
    view_id: str
    gen_id: str
    epoch: int
    incarnation: str
    token: str


def _remaining(deadline):
    if time.monotonic() >= deadline:
        v.fail('fts_deadline_exceeded')


def _query(query, limit, prefixes, exclusions, seconds):
    if (not isinstance(query, str) or not query or type(limit) is not int
            or not 1 <= limit <= 100 or type(seconds) not in (int, float)):
        v.fail('fts_query_invalid')
    try:
        if not math.isfinite(seconds) or not 0 < seconds <= 30 or len(query.encode('utf8')) > 4096:
            v.fail('fts_query_invalid')
    except (UnicodeError, OverflowError):
        v.fail('fts_query_invalid')
    for items, maximum in ((prefixes, 32), (exclusions, 256)):
        if items is not None:
            if type(items) is not list or len(items) > maximum:
                v.fail('fts_filter_invalid')
            for path in items:
                v.relative(path)
    return time.monotonic() + seconds


class ReaderMixin:
    @contextmanager
    def _reading(self, deadline):
        if self._closed:
            v.fail('view_store_closed')
        _remaining(deadline)
        query_only = self._db.execute('PRAGMA query_only').fetchone()[0]
        self._db.execute('PRAGMA query_only=ON').close()
        self._db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        try:
            with self._session() as db:
                yield db
                _remaining(deadline)
        except (BlobStoreError, sqlite3.Error):
            if time.monotonic() >= deadline:
                v.fail('fts_deadline_exceeded')
            raise
        finally:
            self._db.set_progress_handler(None, 0)
            if not query_only:
                self._db.execute('PRAGMA query_only=OFF').close()

    def _sealed_handle(self, db, generation, live_handle=None):
        capture = v.parse_canonical(generation['checkout_evidence'])
        if (not checkout._capture_shape(capture)
                or checkout._digest({k: x for k, x in capture.items() if k != 'checkoutDigest'}) != capture['checkoutDigest']
                or capture['repoId'] != self._repository['repoId']
                or capture['viewId'] != generation['view_id']):
            v.fail('generation_capture_invalid')
        saved = db.execute('SELECT * FROM worktrees WHERE worktree_id=?', (capture['worktreeId'],)).fetchone()
        if saved is None:
            v.fail('generation_capture_invalid')
        saved = dict(saved)
        record = _worktree_record(saved, self._repository, self._policy)
        evidence = capture['evidence']
        if (any(evidence[k] != record[k] for k in ('project', 'checkout', 'gitDir'))
                or any(evidence[k] != self._repository[k] for k in ('commonGitDir', 'projectRelative', 'gitObjectFormat'))):
            v.fail('generation_capture_invalid')
        handle = self._handle_type(view_id=capture['viewId'], worktree_id=capture['worktreeId'],
            branch_key=capture['branchKey'], head_oid=capture['headOid'],
            checkout_digest=capture['checkoutDigest'], policy_digest=self._policy,
            incarnation=self._incarnation, token='sealed')
        if live_handle is not None and (
                handle.view_id, handle.worktree_id, handle.branch_key) != (
                live_handle.view_id, live_handle.worktree_id, live_handle.branch_key):
            v.fail('generation_view_mismatch')
        return handle, dict(handle=handle, capture=capture, binding=saved['binding_digest']), saved

    def _read_generation(self, db, gen_id, live_handle, deadline):
        _remaining(deadline)
        generation = db.execute('SELECT * FROM generations WHERE gen_id=?', (gen_id,)).fetchone()
        if generation is None or generation['state'] not in ('prepared', 'published'):
            v.fail('generation_unsealed')
        seal = db.execute('SELECT * FROM generation_seals WHERE gen_id=?', (gen_id,)).fetchone()
        if seal is None:
            v.fail('generation_unsealed')
        # The shared verifier compares physical bytes before virtual content.
        handle, entry, saved = self._sealed_handle(db, generation, live_handle)
        self._verify_generation(db, handle, entry, saved, generation, deadline=deadline)
        return dict(generation), dict(seal), entry['capture']

    def pin_view(self, handle):
        if self._closed:
            v.fail('view_store_closed')
        issued = self._check(handle)
        self._observe(issued)
        if len(self._pins) >= MAX_PINS:
            v.fail('view_pin_limit')
        deadline = time.monotonic() + 30
        with self._reading(deadline) as db:
            head = self._head(db, handle)
            if head['gen_id'] is None:
                v.fail('view_preparing')
            generation, seal, _ = self._read_generation(db, head['gen_id'], handle, deadline)
            if generation['state'] != 'published':
                v.fail('view_head_invalid')
        self._observe(issued)
        pin = ViewPin(handle.view_id, head['gen_id'], head['epoch'], self._incarnation, uuid.uuid4().hex)
        # Retain an internal copy rather than a mutable caller-held handle object.
        copied = self._handle_type(**{k: getattr(handle, k) for k in handle.__dataclass_fields__})
        entry = dict(handle=copied, capture=json.loads(v.canonical(issued['capture'])), binding=issued['binding'])
        self._pins[pin.token] = dict(pin=pin, fields=(pin.view_id, pin.gen_id, pin.epoch, pin.incarnation, pin.token),
            entry=entry, seal=v.digest('memvane-read-pin-seal-v1', seal))
        return pin

    def _pinned(self, pin):
        if self._closed:
            v.fail('view_store_closed')
        record = self._pins.get(pin.token) if type(pin) is ViewPin and isinstance(pin.token, str) else None
        if (record is None or record['pin'] is not pin or pin.incarnation != self._incarnation
                or (pin.view_id, pin.gen_id, pin.epoch, pin.incarnation, pin.token) != record['fields']):
            v.fail('view_pin_invalid')
        return record

    def release(self, pin):
        self._pinned(pin)
        del self._pins[pin.token]

    def _snapshot_state(self, current, original, deadline):
        if current['checkoutDigest'] == original['checkoutDigest']:
            return 'published_snapshot'
        if current['headOid'] == original['headOid']:
            return 'stale_snapshot'
        if current['headOid'] is None or original['headOid'] is None:
            return 'lineage_unverified'
        _remaining(deadline)
        try:
            code, _ = checkout._git(['merge-base', '--is-ancestor', original['headOid'], current['headOid']],
                current['projectRoot'], deadline, ok=(0, 1))
        except checkout.CheckoutUnavailable:
            _remaining(deadline)
            return 'lineage_unverified'
        return 'stale_snapshot' if code == 0 else 'lineage_changed'

    def search(self, pin, fts_query, limit, *, path_prefixes=None, excluded_paths=None, deadline_seconds=2):
        deadline = _query(fts_query, limit, path_prefixes, excluded_paths, deadline_seconds)
        record = self._pinned(pin)
        _remaining(deadline)
        entry = record['entry']
        self._observe(entry)
        _remaining(deadline)
        with self._reading(deadline) as db:
            generation, seal, original = self._read_generation(db, pin.gen_id, entry['handle'], deadline)
            if generation['state'] != 'published' or v.digest('memvane-read-pin-seal-v1', seal) != record['seal']:
                v.fail('view_pin_changed')
            name = fts.table(generation['ordinal'])
            conditions, args = [], [pin.gen_id, fts_query, pin.view_id]
            if path_prefixes is not None:
                if not path_prefixes:
                    conditions.append('0')
                else:
                    conditions.append('('+' OR '.join('(m.path=? OR substr(m.path,1,?)=?)' for _ in path_prefixes)+')')
                    for path in path_prefixes:
                        args.extend((path, len(path)+1, path+'/'))
            if excluded_paths:
                conditions.append('m.path NOT IN ('+','.join('?' for _ in excluded_paths)+')')
                args.extend(excluded_paths)
            extra = ''.join(' AND '+clause for clause in conditions)
            args.append(limit)
            try:
                rows = db.execute(f'''SELECT m.path,m.blob_sha,m.display_title,bm25({name},1.5,4.0,1.0,0.0) AS score
                    FROM {name} JOIN wiki_members m ON m.gen_id=? AND m.path={name}.path_key
                    JOIN generations g ON g.gen_id=m.gen_id
                    WHERE {name} MATCH ? AND g.view_id=? AND m.eligible=1 {extra}
                    ORDER BY score ASC,m.path COLLATE BINARY ASC LIMIT ?''', args).fetchall()
            except sqlite3.Error:
                _remaining(deadline)
                v.fail('fts_query_invalid')
            state = self._snapshot_state(entry['capture'], original, deadline)
            answer = [dict(path=r['path'], blob_sha=r['blob_sha'], body=self._blob(r['blob_sha']),
                display_title=r['display_title'], score=r['score'], view_id=pin.view_id, gen_id=pin.gen_id,
                evidence='commitment-only', owner_verified=False, snapshot_state=state) for r in rows]
            _remaining(deadline)
        self._observe(entry)
        _remaining(deadline)
        return answer

    def verify_views(self):
        deadline = time.monotonic() + 30
        with self._reading(deadline) as db:
            for generation in db.execute('SELECT * FROM generations ORDER BY ordinal').fetchall():
                self._read_generation(db, generation['gen_id'], None, deadline)
            for row in db.execute('SELECT * FROM view_heads').fetchall():
                generation = db.execute('SELECT state,view_id FROM generations WHERE gen_id=?', (row['gen_id'],)).fetchone()
                if generation is None or tuple(generation) != ('published', row['view_id']) or row['epoch'] < 1:
                    v.fail('view_head_invalid')
            # Unsealed slots are retained staging, never a reader head.
            orphans = db.execute('SELECT s.* FROM generation_slots s LEFT JOIN generations g ON g.ordinal=s.ordinal WHERE g.gen_id IS NULL').fetchall()
            for slot in orphans:
                stage = v.parse_canonical(slot['stage_json'])
                v.exact(stage, vals.STAGE_FIELDS)
                fields = stage['view']
                v.exact(fields, vals.VIEW_FIELDS)
                handle = self._handle_type(**fields, incarnation=self._incarnation, token='unsealed')
                vals.check_stage(stage, handle, self._incarnation)
                saved = db.execute('SELECT * FROM worktrees WHERE worktree_id=?', (handle.worktree_id,)).fetchone()
                if (saved is None or slot['view_id'] != handle.view_id or slot['build_id'] != stage['build_id']
                        or slot['manifest_digest'] != stage['manifest_digest']
                        or stage['worktree_binding'] != saved['binding_digest'] or stage['settings_sha'] != saved['settings_sha']):
                    v.fail('generation_slot_invalid')
                _worktree_record(saved, self._repository, self._policy)
                plan = self._derive(handle, stage['manifest'], stage['skip_paths'])
                fts.proof(db, slot['ordinal'], vals.fts_rows(plan['bodies'], {m['path']: m['display_title'] for m in plan['members']}), deadline)
        return True

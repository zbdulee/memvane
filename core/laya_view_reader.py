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


def _sqlite_interrupted(exc):
    if isinstance(exc, BlobStoreError):
        if exc.reason != 'candidate_io_failed':
            return False
        # _session wraps SQLite failures while preserving their exception link.
        exc = exc.__cause__ if exc.__cause__ is not None else exc.__context__
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, 'sqlite_errorcode', None)
    if code is not None:
        return code == getattr(sqlite3, 'SQLITE_INTERRUPT', 9)
    # Python 3.9 does not expose the numeric SQLite error code.
    return exc.args == ('interrupted',)


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


def _card_locations(db, gen_id, paths):
    """``{wiki_path: [locator, ...]}`` from one generation's card_sources rows."""
    import source_spans
    locations = {}
    sql = ('SELECT wiki_path,spans_json FROM card_sources WHERE gen_id=? AND wiki_path IN (' +
           ','.join('?' for _ in paths) + ') ORDER BY wiki_path,ordinal')
    for source in db.execute(sql, (gen_id, *paths)):
        for span in v.parse_canonical(source['spans_json']):
            if span.get('kind') != 'legacy-lines-v1':
                v.fail('legacy_card_trust_invalid')
            locations.setdefault(source['wiki_path'], []).append(span)
    for path, spans in locations.items():
        spans.sort(key=lambda span: (span['path'], span['startLine'], span['endLine']))
        locations[path] = [source_spans.locator(span) for span in spans]
    return locations


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
        except (BlobStoreError, sqlite3.Error) as exc:
            if _sqlite_interrupted(exc) and time.monotonic() >= deadline:
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

    def _read_generation(self, db, gen_id, live_handle, deadline, *, full_vectors=False):
        _remaining(deadline)
        generation = db.execute('SELECT * FROM generations WHERE gen_id=?', (gen_id,)).fetchone()
        if generation is None or generation['state'] not in ('prepared', 'published'):
            v.fail('generation_unsealed')
        seal = db.execute('SELECT * FROM generation_seals WHERE gen_id=?', (gen_id,)).fetchone()
        if seal is None:
            v.fail('generation_unsealed')
        # The shared verifier compares physical bytes before virtual content.
        handle, entry, saved = self._sealed_handle(db, generation, live_handle)
        stage = self._verify_generation(db, handle, entry, saved, generation, deadline=deadline,
                                        full_vectors=full_vectors)
        return dict(generation), dict(seal), entry['capture'], stage

    def pin_view(self, handle, *, deadline=None):
        if self._closed:
            v.fail('view_store_closed')
        now=time.monotonic()
        if deadline is None:
            deadline=now+30
        elif type(deadline) not in (int,float) or not math.isfinite(deadline):
            v.fail('fts_query_invalid')
        else:
            deadline=min(deadline,now+30)
        _remaining(deadline)
        issued = self._check(handle)
        self._observe(issued,deadline=deadline)
        _remaining(deadline)
        if len(self._pins) >= MAX_PINS:
            v.fail('view_pin_limit')
        with self._reading(deadline) as db:
            head = self._head(db, handle)
            if head['gen_id'] is None:
                v.fail('view_preparing')
            generation, seal, _, stage = self._read_generation(db, head['gen_id'], handle, deadline)
            if generation['state'] != 'published':
                v.fail('view_head_invalid')
            sealed = stage['vector_spec']
            # The verified sealed spec, never the mutable shared cache; every
            # later read re-verifies this seal before using it.
            vector = None if sealed is None else dict(
                sealed, dimension=self._profile(sealed['profile_id'])['dimension'])
        self._observe(issued,deadline=deadline)
        _remaining(deadline)
        pin = ViewPin(handle.view_id, head['gen_id'], head['epoch'], self._incarnation, uuid.uuid4().hex)
        # Retain an internal copy rather than a mutable caller-held handle object.
        copied = self._handle_type(**{k: getattr(handle, k) for k in handle.__dataclass_fields__})
        entry = dict(handle=copied, capture=json.loads(v.canonical(issued['capture'])), binding=issued['binding'])
        self._pins[pin.token] = dict(pin=pin, fields=(pin.view_id, pin.gen_id, pin.epoch, pin.incarnation, pin.token),
            entry=entry, seal=v.digest('memvane-read-pin-seal-v1', seal), vector=vector)
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

    def search(self, pin, fts_query, limit, *, path_prefixes=None, excluded_paths=None,
               deadline_seconds=2, _legacy_origin=None):
        if _legacy_origin is not None and _legacy_origin not in ('base', 'sidecar'):
            v.fail('migration_lexical_origin_invalid')
        deadline = _query(fts_query, limit, path_prefixes, excluded_paths, deadline_seconds)
        record = self._pinned(pin)
        _remaining(deadline)
        entry = record['entry']
        self._observe(entry,deadline=deadline)
        _remaining(deadline)
        with self._reading(deadline) as db:
            generation, seal, original, stage = self._read_generation(db, pin.gen_id, entry['handle'], deadline)
            if generation['state'] != 'published' or v.digest('memvane-read-pin-seal-v1', seal) != record['seal']:
                v.fail('view_pin_changed')
            paired = 'sidecar_corpus' in stage.get('legacy', {})
            if paired and _legacy_origin is None:
                v.fail('migration_lexical_sidecar_unproved')
            if not paired and _legacy_origin is not None:
                v.fail('migration_lexical_origin_invalid')
            name = fts.table(generation['ordinal'],sidecar=_legacy_origin == 'sidecar')
            mapping = None
            if 'legacy' in stage:
                origin = _legacy_origin if paired else 'base'
                mapping = {path: full for path, full in stage['legacy']['fts_paths'].items()
                           if not paired or stage['legacy']['fts_sources'][path] == origin}
                args = [v.canonical(stage['legacy']['active_fts'][origin]), fts_query]
                path_expr = f"substr({name}.path_key,instr({name}.path_key,'/')+1)"
            else:
                join = f'JOIN wiki_members m ON m.gen_id=? AND m.path={name}.path_key'
                args = [pin.gen_id, fts_query, pin.view_id]
                order_path = 'm.path'
                path_expr = 'm.path'
            conditions = []
            if path_prefixes is not None:
                if not path_prefixes:
                    conditions.append('0')
                else:
                    conditions.append('('+' OR '.join(f'({path_expr}=? OR substr({path_expr},1,?)=?)'
                                                      for _ in path_prefixes)+')')
                    for path in path_prefixes:
                        args.extend((path, len(path)+1, path+'/'))
            if excluded_paths:
                conditions.append(path_expr+' NOT IN ('+','.join('?' for _ in excluded_paths)+')')
                args.extend(excluded_paths)
            extra = ''.join(' AND '+clause for clause in conditions)
            args.append(limit)
            try:
                if 'legacy' in stage:
                    args.extend((v.canonical(mapping), pin.gen_id, pin.view_id))
                    rows = db.execute(f'''SELECT m.path,m.blob_sha,m.display_title,r.score FROM (
                        SELECT {name}.path_key AS full_path,bm25({name},1.5,4.0,1.0,0.0) AS score
                        FROM {name} JOIN json_each(?) a ON a.key={name}.path_key
                        WHERE {name} MATCH ? {extra}
                        ORDER BY score ASC,{name}.path_key COLLATE BINARY ASC LIMIT ? OFFSET 0
                        ) r JOIN json_each(?) p ON p.value=r.full_path
                        JOIN wiki_members m ON m.gen_id=? AND m.path=p.key
                        JOIN generations g ON g.gen_id=m.gen_id
                        WHERE g.view_id=? AND m.eligible=1
                        ORDER BY r.score ASC,r.full_path COLLATE BINARY ASC''', args).fetchall()
                else:
                    rows = db.execute(f'''SELECT m.path,m.blob_sha,m.display_title,bm25({name},1.5,4.0,1.0,0.0) AS score
                        FROM {name} {join}
                        JOIN generations g ON g.gen_id=m.gen_id
                        WHERE {name} MATCH ? AND g.view_id=? AND m.eligible=1 {extra}
                        ORDER BY score ASC,{order_path} COLLATE BINARY ASC LIMIT ?''', args).fetchall()
            except sqlite3.Error:
                _remaining(deadline)
                v.fail('fts_query_invalid')
            state = self._snapshot_state(entry['capture'], original, deadline)
            locations = {}
            if 'legacy' in stage and rows:
                locations = _card_locations(db, pin.gen_id, [row['path'] for row in rows])
            answer = [dict(path=r['path'], blob_sha=r['blob_sha'], body=self._blob(r['blob_sha']),
                display_title=r['display_title'], score=r['score'], view_id=pin.view_id, gen_id=pin.gen_id,
                evidence='commitment-only', owner_verified=False, snapshot_state=state) for r in rows]
            if 'legacy' in stage:
                for row in answer:
                    row['source_locations'] = locations.get(row['path'], [])
            _remaining(deadline)
        self._observe(entry,deadline=deadline)
        _remaining(deadline)
        return answer

    def card_locations(self, pin, paths, *, deadline_seconds=2):
        """Source line locators of pinned members, from stored card provenance only.

        Any generation (legacy import or ordinary) answers from its own
        ``card_sources`` rows; no source file is opened. Only
        ``legacy-lines-v1`` spans are shown, and any other kind refuses the
        read. A path without spans is absent from the result.
        """
        if (not isinstance(paths, (list, tuple)) or len(paths) > 100
                or any(type(path) is not str or not path for path in paths)):
            v.fail('card_locations_invalid')
        deadline = time.monotonic() + deadline_seconds
        self._pinned(pin)
        if not paths:
            return {}
        with self._reading(deadline) as db:
            generation = db.execute('SELECT state,view_id FROM generations WHERE gen_id=?',
                                    (pin.gen_id,)).fetchone()
            if generation is None or tuple(generation) != ('published', pin.view_id):
                v.fail('view_pin_changed')
            return _card_locations(db, pin.gen_id, list(dict.fromkeys(paths)))

    def verify_views(self):
        deadline = time.monotonic() + 30
        with self._reading(deadline) as db:
            for generation in db.execute('SELECT * FROM generations ORDER BY ordinal').fetchall():
                self._read_generation(db, generation['gen_id'], None, deadline, full_vectors=True)
            for row in db.execute('SELECT * FROM view_heads').fetchall():
                generation = db.execute('SELECT state,view_id FROM generations WHERE gen_id=?', (row['gen_id'],)).fetchone()
                if generation is None or tuple(generation) != ('published', row['view_id']) or row['epoch'] < 1:
                    v.fail('view_head_invalid')
            # Unsealed slots are retained staging, never a reader head.
            orphans = db.execute('SELECT s.* FROM generation_slots s LEFT JOIN generations g ON g.ordinal=s.ordinal WHERE g.gen_id IS NULL').fetchall()
            for slot in orphans:
                stage = v.parse_canonical(slot['stage_json'])
                v.exact(stage, vals.STAGE_FIELDS if 'legacy' not in stage else (*vals.STAGE_FIELDS, 'legacy'))
                fields = stage['view']
                v.exact(fields, vals.VIEW_FIELDS)
                handle = self._handle_type(**fields, incarnation=self._incarnation, token='unsealed')
                vals.check_stage(stage, handle, self._incarnation)
                saved = db.execute('SELECT * FROM worktrees WHERE worktree_id=?', (handle.worktree_id,)).fetchone()
                if (saved is None or slot['view_id'] != handle.view_id or slot['build_id'] != stage['build_id']
                        or slot['manifest_digest'] != stage['manifest_digest']
                        or stage['worktree_binding'] != saved['binding_digest']):
                    v.fail('generation_slot_invalid')
                _worktree_record(saved, self._repository, self._policy)
                if stage['vector_spec'] is not None:
                    self._recipe(stage['vector_spec']['recipe_id']); self._profile(stage['vector_spec']['profile_id'])
                plan = self._derive(handle, stage['manifest'], stage['skip_paths'],
                                    legacy_paths=stage.get('legacy', {}).get('paths', ()), deadline=deadline)
                rows = (self._legacy_fts_rows(stage['legacy'], verified_bodies=plan['verified_bodies'], deadline=deadline) if 'legacy' in stage else
                        vals.fts_rows(plan['bodies'], {m['path']: m['display_title'] for m in plan['members']}, deadline=deadline))
                if 'sidecar_corpus' in stage.get('legacy', {}):
                    side_rows = self._legacy_fts_rows(stage['legacy'], verified_bodies=plan['verified_bodies'],
                                                      deadline=deadline, sidecar=True)
                    fts.proof_pair(db, slot['ordinal'], rows, side_rows, deadline)
                else:
                    fts.proof(db, slot['ordinal'], rows, deadline)
        return True

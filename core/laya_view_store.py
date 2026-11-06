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

import laya_blob_values as v
import laya_checkout_view as checkout
from laya_blob_values import BlobStoreError
from laya_blob_store import (BlobStore, _current_file_node, _insert, _regular,
                             _worktree_record, _worktree_row)
import laya_view_fts as view_fts
import laya_view_values as vals
import laya_view_vectors as vectors
from laya_view_fill import FillMixin
from laya_view_reader import ReaderMixin
from laya_view_vectors import VectorMixin

SETTINGS_LIMIT = 1024 * 1024


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
    import config
    import laya_index_host_route as routes
    import project_dir
    path = Path(saved['settings_path'])
    try:
        raw, _, _ = _regular(path.parent, path.name, SETTINGS_LIMIT)
        if v.hash_bytes(raw) != saved['settings_sha']:
            v.fail('wiki_settings_changed')
        value = json.loads(raw.decode('utf8'), object_pairs_hook=routes._pairs)
        normalized = config.normalize_config(config.expand_minimal_settings(value, project_dir.CURRENT),
                                             project_dir.CURRENT)
    except BlobStoreError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        v.fail('wiki_settings_invalid')
    return vals.skip_patterns(normalized, value)


def _wiki_files(saved, members):
    """Bounded FD reads of the exact admitted wiki bytes; never under the writer lock."""
    files, total = {}, 0
    for member in members:
        try:
            raw, node, rootnode = _regular(saved['wiki_root'], member['path'], v.MAX_BODY)
        except OSError:
            v.fail('wiki_member_unavailable')
        if rootnode != {'dev': saved['wiki_dev'], 'ino': saved['wiki_ino']}:
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
    settings = Path(saved['settings_path'])
    try:
        raw, _, _ = _regular(settings.parent, settings.name, SETTINGS_LIMIT)
        current = {path: _current_file_node(saved['wiki_root'], path) for path in files}
    except OSError:
        v.fail('wiki_member_changed')
    if v.hash_bytes(raw) != saved['settings_sha']:
        v.fail('wiki_settings_changed')
    if current != files:
        v.fail('wiki_member_changed')


def _current_capture(saved, repository, policy, expected, *, deadline=None):
    """Re-observe Git/config identity before commit without a nested DB session."""
    record = _worktree_record(saved, repository, policy)
    current, observation = _worktree_row(saved['project_root'], repository, record, deadline=deadline)
    if (any(saved[k] != value for k, value in current.items())
            or observation['capture'] != expected
            or not checkout.capture_matches(expected, repository, record, fresh=True, deadline=deadline,
                                            _raise_timeout=deadline is not None)):
        v.fail('checkout_changed')


class ViewStore(FillMixin, VectorMixin, ReaderMixin, BlobStore):
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
        current, observation = _worktree_row(saved['project_root'], self._repository, record, deadline=deadline)
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
        row = db.execute('SELECT gen_id,epoch FROM view_heads WHERE view_id=?', (handle.view_id,)).fetchone()
        if row is None:
            return dict(view_id=handle.view_id, gen_id=None, epoch=0, incarnation=self._incarnation)
        sealed = db.execute(
            "SELECT 1 FROM generations g JOIN generation_seals s ON s.gen_id=g.gen_id "
            "AND s.binding_digest=g.binding_digest WHERE g.gen_id=? AND g.view_id=? AND g.state='published'",
            (row['gen_id'], handle.view_id)).fetchone()
        if sealed is None or type(row['epoch']) is not int or row['epoch'] < 1:
            v.fail('view_head_invalid')
        return dict(view_id=handle.view_id, gen_id=row['gen_id'], epoch=row['epoch'],
                    incarnation=self._incarnation)

    def head(self, handle):
        entry = self._check(handle)
        self._observe(entry)
        with self._session() as db:
            return self._head(db, handle)

    def _card(self, handle, member, current, *, legacy=False, cache=None):
        """Original attestation refs stay unchanged; eligibility is derived only."""
        if member['trust_digest'] is None:
            return 0, []
        spec = self._attestation(member['trust_digest'], cache=cache)
        if spec['blob_sha'] != member['blob_sha'] or spec['policy_digest'] != handle.policy_digest:
            v.fail('card_trust_mismatch')
        if legacy and spec['verification_kind'] != 'legacy_verified':
            v.fail('legacy_card_trust_invalid')
        ok, refs = bool(spec['sources']), []
        for ordinal, source in enumerate(spec['sources']):
            old = self._fingerprint(source['fingerprint_id'], cache=cache)
            if (old['worktree_id'], old['branch_key']) != (handle.worktree_id, handle.branch_key):
                v.fail('foreign_card_trust')
            refs.append(dict(ordinal=ordinal, fingerprint_id=source['fingerprint_id'], spans=source['spans']))
            if legacy:
                if (old['binding_state'] != 'legacy_unbound' or old['head_oid'] is not None
                        or old['tracked_state'] != 'unknown'):
                    v.fail('legacy_card_trust_invalid')
                continue
            now = current.get(old['path'])
            if (now is None or now[0]['presence'] != 'present'
                    or 'legacy_unbound' in (old['binding_state'], now[1]['binding_state'])
                    or (now[1]['source_sha'], now[1]['byte_length']) != (old['source_sha'], old['byte_length'])):
                ok = False
        return int(ok), refs

    def _derive(self, handle, manifest, skips, *, retain_bodies=True, legacy_paths=(), deadline=None):
        """Metadata-only derivation inside a short read transaction."""
        if deadline is not None and time.monotonic() >= deadline:
            v.fail('fts_deadline_exceeded')
        legacy_paths = frozenset(legacy_paths)
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
            legacy = member['path'] in legacy_paths
            key = (blob, member['trust_digest'], legacy)
            if key not in verified_cards:
                verified_cards[key] = self._card(handle, member, current, legacy=legacy, cache=cache)
            eligible, refs = verified_cards[key]
            if not legacy and vals.skipped(member['path'], skips):
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

    @staticmethod
    def _roots(db, ordinal, plan, rows, vector_spec, coverage, deadline=None, *,
               expected_storage=None, sidecar_rows=None):
        if sidecar_rows is None:
            lexical, storage = view_fts.proof(db, ordinal, rows, deadline, return_storage=True,
                                              expected_storage=expected_storage)
        else:
            lexical, storage = view_fts.proof_pair(db, ordinal, rows, sidecar_rows, deadline,
                                                   expected_storage=expected_storage)
        return vals.roots(plan['members'], plan['sources'], plan['cards'], lexical, storage,
                          vector_spec, coverage)

    def _legacy_fts_rows(self, legacy, *, verified_bodies=None, deadline=None, sidecar=False):
        """Rebuild the sealed old wiki FTS corpus from admitted wiki blobs."""
        if deadline is not None and time.monotonic() >= deadline:
            v.fail('fts_deadline_exceeded')
        bodies, checked, total, side_rows = {}, dict(verified_bodies or {}), 0, []
        corpus = legacy['sidecar_corpus' if sidecar else 'corpus']
        for row in sorted(corpus, key=lambda item: item['rowid']) if sidecar else corpus:
            if deadline is not None and time.monotonic() >= deadline:
                v.fail('fts_deadline_exceeded')
            if row['hash'] not in checked:
                checked[row['hash']] = self._blob(row['hash'])
            body = checked[row['hash']]
            total += len(body.encode('utf8'))
            if total > vals.MAX_WIKI_BYTES:
                v.fail('generation_too_large')
            if sidecar:
                side_rows.append(dict(filepath=row['path'], title=row['title'], body=body, path_key=row['path']))
            else:
                bodies[row['path']] = body
        if sidecar:
            if deadline is not None and time.monotonic() >= deadline:
                v.fail('fts_deadline_exceeded')
            return side_rows
        return vals.fts_rows(bodies, deadline=deadline)

    def prepare_generation(self, handle, manifest, *, expected_head, vector_spec=None, legacy_snapshot=None,
                           deadline=None):
        if deadline is not None:
            if (type(deadline) not in (int, float) or not math.isfinite(deadline)
                    or time.monotonic() >= deadline):
                v.fail('fts_deadline_exceeded')
        self._require_write()
        entry = self._check(handle)
        base = vals.expected_head(expected_head, handle.view_id, self._incarnation)
        derive_manifest = manifest is None and type(legacy_snapshot) is dict and 'observation' in legacy_snapshot
        if not derive_manifest:
            manifest = vals.manifest(manifest, handle.policy_digest)
        spec = vectors.spec(vector_spec)
        saved = self._observe(entry, deadline=deadline)
        skips = _skip_patterns(saved)
        legacy = None
        if legacy_snapshot is not None:
            import laya_shadow_migration as migration
            bounded = type(legacy_snapshot) is dict and 'observation' in legacy_snapshot
            commitments = legacy_snapshot.get('commitments') if bounded else None
            v.exact(legacy_snapshot, (('observation' if bounded else 'snapshot'),
                                      'endpoint', 'decision', 'provenance_scan',
                                      *(('commitments',) if commitments is not None else ())))
            if commitments is not None:
                v.exact(commitments, ('migration_id', 'verification', 'corpora'))
                v.identifier(commitments['migration_id'], v.UUID)
                v.exact(commitments['corpora'], ('verification_digest', 'base', 'sidecar'))
                if (type(commitments['verification']) is not dict or
                        commitments['corpora']['verification_digest'] != commitments['verification'].get('digest') or
                        commitments['corpora']['base'] is None):
                    v.fail('migration_commitment_mismatch')
            if (spec is not None or (not derive_manifest and
                    (manifest['sources'] or manifest['scan_id'] != legacy_snapshot['provenance_scan'].get('id')))):
                v.fail('migration_manifest_mismatch')
            source_args = {key: value for key, value in legacy_snapshot.items() if key != 'commitments'}
            if commitments is not None:
                source_args['commitment'] = {'migration_id': commitments['migration_id'],
                                             'progress': commitments['verification']}
            source_deadline = min(deadline, time.monotonic() + 30) if deadline is not None else time.monotonic() + 30
            evidence = (migration.collect_generation_members if bounded else migration.verify_generation_members)(
                self, worktree_id=handle.worktree_id, **source_args, deadline=source_deadline)
            if derive_manifest:
                manifest = vals.manifest({'scan_id': legacy_snapshot['provenance_scan']['id'],
                    'policy_digest': handle.policy_digest, 'revocation_epoch': 0,
                    'complete': True, 'sources': [], 'members': evidence['members']}, handle.policy_digest)
            if sorted(evidence['members'], key=lambda row: row['path']) != sorted(
                    manifest['members'], key=lambda row: row['path']):
                v.fail('migration_manifest_mismatch')
            if bounded:
                commitment = migration.inspect_sidecar_bounded(endpoint=legacy_snapshot['endpoint'],
                    decision=legacy_snapshot['decision'], observation=evidence['observation'],
                    deadline=min(deadline, time.monotonic() + 30) if deadline is not None else time.monotonic() + 30)
                needs_sidecar = commitment is not None
                corpus = migration.stage_fts_corpus_bounded(self, worktree_id=handle.worktree_id,
                    endpoint=legacy_snapshot['endpoint'], decision=legacy_snapshot['decision'],
                    observation=evidence['observation'], deadline=min(deadline, time.monotonic() + 30) if deadline is not None else time.monotonic() + 30, sidecar=False,
                    commitment=None if commitments is None else {
                        'migration_id': commitments['migration_id'], 'provenance_scan': legacy_snapshot['provenance_scan'],
                        'verification_digest': commitments['verification']['digest'],
                        'progress': commitments['corpora']['base']})
            else:
                needs_sidecar = migration.sidecar_fts_required(legacy_snapshot['snapshot'],
                    endpoint=legacy_snapshot['endpoint'], decision=legacy_snapshot['decision'],
                    deadline=min(deadline, time.monotonic() + 30) if deadline is not None else time.monotonic() + 30)
                corpus = migration.stage_base_fts_corpus(self, worktree_id=handle.worktree_id,
                    snapshot=legacy_snapshot['snapshot'], endpoint=legacy_snapshot['endpoint'],
                    decision=legacy_snapshot['decision'], deadline=min(deadline, time.monotonic() + 30) if deadline is not None else time.monotonic() + 30, allow_sidecar=True)
            legacy = {'observation': evidence['observation'],
                      'paths': sorted(row['path'] for row in evidence['members']),
                      'fts_paths': evidence['fts_paths'], 'corpus_observation': corpus['observation'],
                      'corpus': corpus['members'], 'scan': dict(legacy_snapshot['provenance_scan']),
                      'active_fts': evidence['active_fts']}
            if needs_sidecar:
                if bounded:
                    if commitments is not None and commitments['corpora']['sidecar'] is None:
                        v.fail('migration_commitment_mismatch')
                    side = migration.stage_fts_corpus_bounded(self, worktree_id=handle.worktree_id,
                        endpoint=legacy_snapshot['endpoint'], decision=legacy_snapshot['decision'],
                        observation=evidence['observation'], deadline=min(deadline, time.monotonic() + 30) if deadline is not None else time.monotonic() + 30, sidecar=True,
                        commitment=None if commitments is None else {
                            'migration_id': commitments['migration_id'], 'provenance_scan': legacy_snapshot['provenance_scan'],
                            'verification_digest': commitments['verification']['digest'],
                            'progress': commitments['corpora']['sidecar']})
                else:
                    side = migration.stage_sidecar_fts_corpus(self, worktree_id=handle.worktree_id,
                        snapshot=legacy_snapshot['snapshot'], endpoint=legacy_snapshot['endpoint'],
                        decision=legacy_snapshot['decision'], deadline=min(deadline, time.monotonic() + 30) if deadline is not None else time.monotonic() + 30)
                    commitment = migration.sidecar_fts_commitment(legacy_snapshot['snapshot'],
                        endpoint=legacy_snapshot['endpoint'], decision=legacy_snapshot['decision'],
                        deadline=min(deadline, time.monotonic() + 30) if deadline is not None else time.monotonic() + 30)
                legacy.update(sidecar_corpus=side['members'], sidecar_observation=side['observation'],
                              sidecar_commitment=commitment, fts_sources=evidence['fts_sources'])
        with self._session() as db:
            if self._head(db, handle) != base:
                v.fail('view_head_conflict')
            plan = self._derive(handle, manifest, skips, deadline=deadline,
                                legacy_paths=() if legacy is None else legacy['paths'])
            # Zero or complete: every eligible member's set is fully validated
            # before any write; a hole, mixed profile or tamper refuses here.
            coverage = self._vector_coverage(db, plan, spec, full=True, deadline=deadline)
            rows = (vals.fts_rows(plan['bodies'], {m['path']: m['display_title'] for m in plan['members']}, deadline=deadline)
                    if legacy is None else self._legacy_fts_rows(legacy, verified_bodies=plan['verified_bodies'], deadline=deadline))
            side_rows = (self._legacy_fts_rows(legacy, verified_bodies=plan['verified_bodies'], sidecar=True, deadline=deadline)
                         if legacy is not None and 'sidecar_corpus' in legacy else None)
        files = _wiki_files(saved, plan['members']) if legacy is None else {}
        # The migration scan survives a process crash in the private intent.
        # Reusing it makes the two committed prepare stages replayable.
        build_id = legacy_snapshot['provenance_scan']['id'] if derive_manifest else uuid.uuid4().hex
        stage = vals.stage(build_id, self._incarnation, handle, saved, base, manifest, skips, files, spec, legacy)
        stage_json = v.canonical(stage)
        # Stage1: metadata-only slot plus generation-owned FTS; the FTS guard
        # owns commit/rollback through FTS sync. No generation row exists yet.
        self._observe(entry, deadline=deadline)
        view_fts._deadline(deadline)
        with self._session(write=True) as db:
            if self._head(db, handle) != base:
                v.fail('view_head_conflict')
            _live(db, saved, files)
            view = db.execute('SELECT worktree_id,branch_key FROM views WHERE view_id=?', (handle.view_id,)).fetchone()
            if view is None:
                _insert(db, 'views', dict(view_id=handle.view_id, worktree_id=handle.worktree_id,
                                          branch_key=handle.branch_key))
            elif tuple(view) != (handle.worktree_id, handle.branch_key):
                v.fail('view_binding_conflict')
            slot = db.execute('SELECT * FROM generation_slots WHERE build_id=?', (build_id,)).fetchone()
            if slot is None:
                _insert(db, 'generation_slots', dict(build_id=build_id, view_id=handle.view_id,
                        manifest_digest=stage['manifest_digest'], stage_json=stage_json, created_ns=time.time_ns()))
                ordinal = db.execute('SELECT ordinal FROM generation_slots WHERE build_id=?', (build_id,)).fetchone()[0]
                with self._fts_guard.building(ordinal):
                    view_fts.create(db, ordinal)
                    view_fts.populate(db, ordinal, rows)
                    if side_rows is not None:
                        view_fts.create(db, ordinal, sidecar=True)
                        view_fts.populate(db, ordinal, side_rows, sidecar=True)
                    _current_capture(saved, self._repository, self._policy, entry['capture'], deadline=deadline)
                    view_fts._deadline(deadline)
                    _live(db, saved, files)
                    view_fts._deadline(deadline)
            else:
                if (not derive_manifest or slot['view_id'] != handle.view_id
                        or slot['manifest_digest'] != stage['manifest_digest'] or slot['stage_json'] != stage_json):
                    v.fail('generation_slot_invalid')
                ordinal = slot['ordinal']
                prior = db.execute('SELECT * FROM generations WHERE ordinal=?', (ordinal,)).fetchone()
                if prior is not None:
                    if (prior['state'] != 'prepared' or
                            self._verify_generation(db, handle, entry, saved, prior,
                                                    deadline=deadline, full_vectors=True) != stage):
                        v.fail('generation_slot_invalid')
                    view_fts._deadline(deadline)
                    return prior['gen_id']
        # Stage2: stable post-commit FTS proof and all roots before identity.
        self._observe(entry, deadline=deadline)
        view_fts._deadline(deadline)
        with self._session(write=True) as db:
            with self._fts_guard.building(ordinal):
                view_fts.verify_index(db, ordinal)
                if side_rows is not None:
                    view_fts.verify_index(db, ordinal, sidecar=True)
                if self._head(db, handle) != base:
                    v.fail('view_head_conflict')
                _live(db, saved, files)
                slot = db.execute('SELECT * FROM generation_slots WHERE ordinal=?', (ordinal,)).fetchone()
                if (slot is None or slot['build_id'] != build_id or slot['view_id'] != handle.view_id
                        or slot['stage_json'] != stage_json
                        or db.execute('SELECT 1 FROM generations WHERE ordinal=?', (ordinal,)).fetchone()):
                    v.fail('generation_slot_invalid')
                roots = self._roots(db, ordinal, plan, rows, spec, coverage, deadline=deadline, sidecar_rows=side_rows)
                binding = vals.binding(stage, ordinal, entry['capture'], roots)
                fields = vals.generation_fields(stage, ordinal, binding, entry['capture'])
                gen_id, now = fields['gen_id'], time.time_ns()
                _insert(db, 'generations', dict(fields, state='building', created_ns=now))
                for member in plan['members']:
                    view_fts._deadline(deadline)
                    _insert(db, 'wiki_members', dict(member, gen_id=gen_id))
                for source in plan['sources']:
                    view_fts._deadline(deadline)
                    _insert(db, 'source_members', dict(source, gen_id=gen_id))
                for card in plan['cards']:
                    view_fts._deadline(deadline)
                    _insert(db, 'card_sources', dict(gen_id=gen_id, wiki_path=card['wiki_path'], ordinal=card['ordinal'],
                            fingerprint_id=card['fingerprint_id'], spans_json=v.canonical(card['spans'])))
                for ref in coverage:
                    view_fts._deadline(deadline)
                    _insert(db, 'member_vectors', dict(ref, gen_id=gen_id))
                # The sealed root must describe the actual stored references.
                if self._verified_vectors(db, stage, plan, gen_id, full=True, deadline=deadline) != coverage:
                    v.fail('vector_rows_mismatch')
                _insert(db, 'generation_seals', dict(roots, gen_id=gen_id, binding_digest=binding, sealed_ns=now))
                if db.execute("UPDATE generations SET state='prepared' WHERE gen_id=? AND state='building'",
                              (gen_id,)).rowcount != 1:
                    v.fail('generation_slot_invalid')
                _current_capture(saved, self._repository, self._policy, entry['capture'], deadline=deadline)
                view_fts._deadline(deadline)
                _live(db, saved, files)
                view_fts._deadline(deadline)
        return gen_id

    def _stored(self, db, gen_id):
        members = [dict(r) for r in db.execute(
            'SELECT path,blob_sha,display_title,eligible,trust_digest FROM wiki_members WHERE gen_id=? ORDER BY path',
            (gen_id,))]
        sources = [dict(r) for r in db.execute(
            'SELECT path,fingerprint_id,presence FROM source_members WHERE gen_id=? ORDER BY path', (gen_id,))]
        cards = [dict(wiki_path=r['wiki_path'], ordinal=r['ordinal'], fingerprint_id=r['fingerprint_id'],
                      spans=v.parse_canonical(r['spans_json'])) for r in db.execute(
            'SELECT * FROM card_sources WHERE gen_id=? ORDER BY wiki_path,ordinal', (gen_id,))]
        return members, sources, cards

    def _verify_generation(self, db, handle, entry, saved, generation, deadline=None, full_vectors=False):
        """Recompute rows, FTS proof, vector references, roots and binding from the original slot."""
        slot = db.execute('SELECT * FROM generation_slots WHERE ordinal=?', (generation['ordinal'],)).fetchone()
        if slot is None or slot['view_id'] != generation['view_id']:
            v.fail('generation_slot_invalid')
        stage = vals.check_stage(v.parse_canonical(slot['stage_json']), handle, self._incarnation)
        if (stage['build_id'] != slot['build_id'] or stage['manifest_digest'] != slot['manifest_digest']
                or stage['worktree_binding'] != saved['binding_digest']
                or stage['settings_sha'] != saved['settings_sha']):
            v.fail('generation_binding_mismatch')
        seal = db.execute('SELECT * FROM generation_seals WHERE gen_id=?', (generation['gen_id'],)).fetchone()
        if seal is None:
            v.fail('fts_storage_mismatch')
        v.identifier(seal['fts_storage_digest'])
        plan = self._derive(handle, stage['manifest'], stage['skip_paths'],
                            legacy_paths=stage.get('legacy', {}).get('paths', ()), deadline=deadline)
        if self._stored(db, generation['gen_id']) != (plan['members'], plan['sources'], plan['cards']):
            v.fail('generation_rows_mismatch')
        # Actual stored references, compared with the sealed spec's complete coverage.
        coverage = self._verified_vectors(db, stage, plan, generation['gen_id'], full=full_vectors, deadline=deadline)
        rows = (self._legacy_fts_rows(stage['legacy'], verified_bodies=plan['verified_bodies'], deadline=deadline) if 'legacy' in stage else
                vals.fts_rows(plan['bodies'], {m['path']: m['display_title'] for m in plan['members']}, deadline=deadline))
        side_rows = (self._legacy_fts_rows(stage['legacy'], verified_bodies=plan['verified_bodies'],
                                          deadline=deadline, sidecar=True)
                     if 'sidecar_corpus' in stage.get('legacy', {}) else None)
        roots = self._roots(db, generation['ordinal'], plan, rows, stage['vector_spec'], coverage, deadline,
                            expected_storage=seal['fts_storage_digest'], sidecar_rows=side_rows)
        if roots['fts_storage_digest'] != seal['fts_storage_digest']:
            v.fail('fts_storage_mismatch')
        binding = vals.binding(stage, generation['ordinal'], entry['capture'], roots)
        fields = vals.generation_fields(stage, generation['ordinal'], binding, entry['capture'])
        seal = db.execute('SELECT * FROM generation_seals WHERE gen_id=?', (generation['gen_id'],)).fetchone()
        if (seal is None or any(generation[k] != val for k, val in fields.items())
                or seal['binding_digest'] != binding or any(seal[k] != val for k, val in roots.items())):
            v.fail('generation_seal_mismatch')
        return stage

    def publish_generation(self, handle, gen_id, *, expected_head, legacy_snapshot=None, deadline=None):
        if deadline is not None and (type(deadline) not in (int, float)
                or not math.isfinite(deadline) or time.monotonic() >= deadline):
            v.fail('fts_deadline_exceeded')
        self._require_write()
        entry = self._check(handle)
        base = vals.expected_head(expected_head, handle.view_id, self._incarnation)
        if not isinstance(gen_id, str) or not vals.GEN_ID.fullmatch(gen_id):
            v.fail('generation_id_invalid')
        saved = self._observe(entry, deadline=deadline)
        with self._session() as db:
            row = db.execute('SELECT g.view_id,s.stage_json FROM generations g '
                             'JOIN generation_slots s ON s.ordinal=g.ordinal WHERE g.gen_id=?',
                             (gen_id,)).fetchone()
            if row is None or row['view_id'] != handle.view_id:
                v.fail('generation_missing')
            staged = vals.check_stage(v.parse_canonical(row['stage_json']), handle, self._incarnation)
        if 'legacy' not in staged:
            if legacy_snapshot is not None:
                v.fail('migration_publish_proof_invalid')
            return self._publish_generation_checked(handle, gen_id, entry, base, saved, deadline=deadline)
        if legacy_snapshot is None:
            v.fail('migration_publish_proof_required')
        import laya_reader_open as readers
        import laya_shadow_migration as migration
        import hashlib
        # Publication obtains its own fresh snapshot under the publication cut.
        # Legacy callers may still supply their earlier snapshot, which does
        # not replace that final proof.
        v.exact(legacy_snapshot, (('snapshot',) if 'snapshot' in legacy_snapshot else ()) +
                ('endpoint', 'decision', 'provenance_scan') +
                (('source_revision',) if 'source_revision' in legacy_snapshot else ()))
        endpoint = legacy_snapshot['endpoint']
        deadline = min(deadline, time.monotonic() + 1.0) if deadline is not None else time.monotonic() + 1.0
        # The old writer also takes the exclusive publication cut. Hold the
        # existing shared cut from fresh snapshot proof through candidate CAS.
        with readers.reader_publish_cut(endpoint, deadline=deadline):
            with readers.inspect_warm_snapshot(endpoint, deadline=deadline) as fresh:
                observed = migration.capture_effective(fresh, endpoint=endpoint,
                    decision=legacy_snapshot['decision'], after=None, deadline=deadline)
                if ('source_revision' in legacy_snapshot and
                        migration._source_revision(fresh) != legacy_snapshot['source_revision']):
                    v.fail('migration_source_revision_unproved')
                corpus_observation = hashlib.sha256(('memvane-base-fts-corpus-v1:' +
                    observed['observation']).encode('ascii')).hexdigest()
                side_observation = hashlib.sha256(('memvane-sidecar-fts-corpus-v1:' +
                    observed['observation']).encode('ascii')).hexdigest()
                if (observed['observation'] != staged['legacy']['observation'] or
                        corpus_observation != staged['legacy']['corpus_observation'] or
                        ('sidecar_corpus' in staged['legacy'] and
                         side_observation != staged['legacy']['sidecar_observation']) or
                        legacy_snapshot['provenance_scan'] != staged['legacy']['scan']):
                    v.fail('migration_snapshot_changed')
                if 'sidecar_corpus' in staged['legacy'] and migration.sidecar_fts_commitment(
                        fresh, endpoint=endpoint, decision=legacy_snapshot['decision'], deadline=deadline
                        ) != staged['legacy']['sidecar_commitment']:
                    v.fail('migration_sidecar_changed')
                remaining = max(0, int((deadline - time.monotonic()) * 1000))
                if not remaining:
                    v.fail('reader_snapshot_deadline')
                previous_timeout = self._db.execute('PRAGMA busy_timeout').fetchone()[0]
                self._db.execute('PRAGMA busy_timeout=' + str(remaining))
                try:
                    return self._publish_generation_checked(handle, gen_id, entry, base, saved,
                                                            expected_stage=staged, deadline=deadline)
                finally:
                    self._db.execute('PRAGMA busy_timeout=' + str(previous_timeout))

    def _publish_generation_checked(self, handle, gen_id, entry, base, saved, *,
                                    expected_stage=None, deadline=None):
        with self._session(write=True) as db:
            generation = db.execute('SELECT * FROM generations WHERE gen_id=?', (gen_id,)).fetchone()
            if generation is None or generation['view_id'] != handle.view_id:
                v.fail('generation_missing')
            stage = self._verify_generation(db, handle, entry, saved, generation, deadline=deadline, full_vectors=True)
            if (expected_stage is not None and stage != expected_stage) or (
                    expected_stage is None and 'legacy' in stage):
                v.fail('migration_snapshot_changed')
            if stage['base'] != base:
                v.fail('view_head_conflict')
            current = self._head(db, handle)
            if generation['state'] == 'published':
                if current['gen_id'] == gen_id and current['epoch'] == base['epoch'] + 1:
                    _current_capture(saved, self._repository, self._policy, entry['capture'], deadline=deadline)
                    if deadline is not None and time.monotonic() >= deadline:
                        v.fail('reader_snapshot_deadline')
                    return current  # exact replay of this publication only
                v.fail('view_head_conflict')
            if generation['state'] != 'prepared' or current != base:
                v.fail('view_head_conflict')
            _live(db, saved, stage['files'])
            if db.execute("UPDATE generations SET state='published' WHERE gen_id=? AND state='prepared'",
                          (gen_id,)).rowcount != 1:
                v.fail('view_head_conflict')
            if base['gen_id'] is None:
                _insert(db, 'view_heads', dict(view_id=handle.view_id, gen_id=gen_id, epoch=1))
            elif db.execute('UPDATE view_heads SET gen_id=?,epoch=epoch+1 WHERE view_id=? AND gen_id=? AND epoch=?',
                            (gen_id, handle.view_id, base['gen_id'], base['epoch'])).rowcount != 1:
                v.fail('view_head_conflict')
            _current_capture(saved, self._repository, self._policy, entry['capture'], deadline=deadline)
            _live(db, saved, stage['files'])
            if deadline is not None and time.monotonic() >= deadline:
                v.fail('reader_snapshot_deadline')
            return self._head(db, handle)

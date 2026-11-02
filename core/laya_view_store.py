"""Capture-bound generation commitments for the inactive candidate format.

Handles are store-issued objects bound to one checkout/settings observation.
Manifests, fingerprints and receipts are caller-owned commitments: a sealed
generation proves structural consistency, never compiler truth, source
observation or operational/deletion authority. No reader, GC or activation.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import time
import uuid

import laya_blob_values as v
from laya_blob_values import BlobStoreError
from laya_blob_store import (BlobStore, _current_file_node, _insert, _regular,
                             _worktree_record, _worktree_row)
import laya_view_fts as view_fts
import laya_view_values as vals
from laya_view_reader import ReaderMixin

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


def _current_capture(saved, repository, policy, expected):
    """Re-observe Git/config identity before commit without a nested DB session."""
    record = _worktree_record(saved, repository, policy)
    current, observation = _worktree_row(saved['project_root'], repository, record)
    if (any(saved[k] != value for k, value in current.items())
            or observation['capture'] != expected):
        v.fail('checkout_changed')


class ViewStore(ReaderMixin, BlobStore):
    _handle_type = ViewHandle

    @classmethod
    def open(cls, path, *, repository, scope_id):
        store = super().open(path, repository=repository, scope_id=scope_id)
        store._handles = {}
        store._pins = {}
        store._closed = False
        return store

    def close(self):
        self._closed = True
        self._pins = {}
        self._handles = {}
        super().close()

    def _observe_worktree(self, worktree_id):
        with self._session() as db:
            row = db.execute('SELECT * FROM worktrees WHERE worktree_id=?', (worktree_id,)).fetchone()
            if row is None:
                v.fail('worktree_missing')
            saved = dict(row)
            record = _worktree_record(row, self._repository, self._policy)
        current, observation = _worktree_row(saved['project_root'], self._repository, record)
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

    def _observe(self, entry):
        saved, capture = self._observe_worktree(entry['handle'].worktree_id)
        if capture != entry['capture'] or saved['binding_digest'] != entry['binding']:
            v.fail('checkout_changed')
        return saved

    def capture_view(self, worktree_id):
        v.identifier(worktree_id, v.UUID)
        saved, capture = self._observe_worktree(worktree_id)
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

    def _card(self, handle, member, current):
        """Original attestation refs stay unchanged; eligibility is derived only."""
        if member['trust_digest'] is None:
            return 0, []
        spec = self._attestation(member['trust_digest'])
        if spec['blob_sha'] != member['blob_sha'] or spec['policy_digest'] != handle.policy_digest:
            v.fail('card_trust_mismatch')
        ok, refs = bool(spec['sources']), []
        for ordinal, source in enumerate(spec['sources']):
            old = self._fingerprint(source['fingerprint_id'])
            if (old['worktree_id'], old['branch_key']) != (handle.worktree_id, handle.branch_key):
                v.fail('foreign_card_trust')
            refs.append(dict(ordinal=ordinal, fingerprint_id=source['fingerprint_id'], spans=source['spans']))
            now = current.get(old['path'])
            if (now is None or now[0]['presence'] != 'present'
                    or 'legacy_unbound' in (old['binding_state'], now[1]['binding_state'])
                    or (now[1]['source_sha'], now[1]['byte_length']) != (old['source_sha'], old['byte_length'])):
                ok = False
        return int(ok), refs

    def _derive(self, handle, manifest, skips):
        """Metadata-only derivation inside a short read transaction."""
        current = {}
        for source in manifest['sources']:
            fp = self._fingerprint(source['fingerprint_id'])
            if (fp['worktree_id'], fp['branch_key'], fp['path']) != (handle.worktree_id, handle.branch_key, source['path']):
                v.fail('source_binding_mismatch')
            if source['presence'] == 'present' and (fp['scan_id'] != manifest['scan_id']
                                                    or fp['head_oid'] != handle.head_oid):
                v.fail('source_observation_stale')
            if not vals.skipped(source['path'], skips):
                current[source['path']] = (source, fp)
        members, cards, bodies, total = [], [], {}, 0
        # These caches live only within this derivation/read snapshot. Shared
        # bytes never share trust unless the exact attestation also matches.
        verified_bodies, verified_cards = {}, {}
        for member in manifest['members']:
            blob = member['blob_sha']
            if blob not in verified_bodies:
                verified_bodies[blob] = self._blob(blob)
            body = verified_bodies[blob]
            key = (blob, member['trust_digest'])
            if key not in verified_cards:
                verified_cards[key] = self._card(handle, member, current)
            eligible, refs = verified_cards[key]
            if vals.skipped(member['path'], skips):
                continue
            total += len(body.encode('utf8'))
            if total > vals.MAX_WIKI_BYTES:
                v.fail('generation_too_large')
            members.append(dict(member, eligible=eligible))
            cards.extend(dict(wiki_path=member['path'], **ref) for ref in refs)
            if eligible:
                bodies[member['path']] = body
        return dict(members=sorted(members, key=lambda m: m['path']),
                    sources=sorted((s for s, _ in current.values()), key=lambda s: s['path']),
                    cards=sorted(cards, key=lambda c: (c['wiki_path'], c['ordinal'])), bodies=bodies)

    @staticmethod
    def _roots(db, ordinal, plan, rows, deadline=None):
        storage = view_fts.storage_digest(db, ordinal, deadline)
        lexical = view_fts.proof(db, ordinal, rows, deadline)
        return vals.roots(plan['members'], plan['sources'], plan['cards'], lexical, storage)

    def prepare_generation(self, handle, manifest, *, expected_head):
        entry = self._check(handle)
        base = vals.expected_head(expected_head, handle.view_id, self._incarnation)
        manifest = vals.manifest(manifest, handle.policy_digest)
        saved = self._observe(entry)
        skips = _skip_patterns(saved)
        with self._session() as db:
            if self._head(db, handle) != base:
                v.fail('view_head_conflict')
            plan = self._derive(handle, manifest, skips)
        files = _wiki_files(saved, plan['members'])
        rows = vals.fts_rows(plan['bodies'], {m['path']: m['display_title'] for m in plan['members']})
        build_id = uuid.uuid4().hex
        stage = vals.stage(build_id, self._incarnation, handle, saved, base, manifest, skips, files)
        stage_json = v.canonical(stage)
        # Stage1: metadata-only slot plus generation-owned FTS; the FTS guard
        # owns commit/rollback through FTS sync. No generation row exists yet.
        self._observe(entry)
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
            _insert(db, 'generation_slots', dict(build_id=build_id, view_id=handle.view_id,
                    manifest_digest=stage['manifest_digest'], stage_json=stage_json, created_ns=time.time_ns()))
            ordinal = db.execute('SELECT ordinal FROM generation_slots WHERE build_id=?', (build_id,)).fetchone()[0]
            with self._fts_guard.building(ordinal):
                view_fts.create(db, ordinal)
                view_fts.populate(db, ordinal, rows)
                _current_capture(saved, self._repository, self._policy, entry['capture'])
                _live(db, saved, files)
        # Stage2: stable post-commit FTS proof and all roots before identity.
        self._observe(entry)
        with self._session(write=True) as db:
            with self._fts_guard.building(ordinal):
                view_fts.verify_index(db, ordinal)
                if self._head(db, handle) != base:
                    v.fail('view_head_conflict')
                _live(db, saved, files)
                slot = db.execute('SELECT * FROM generation_slots WHERE ordinal=?', (ordinal,)).fetchone()
                if (slot is None or slot['build_id'] != build_id or slot['view_id'] != handle.view_id
                        or slot['stage_json'] != stage_json
                        or db.execute('SELECT 1 FROM generations WHERE ordinal=?', (ordinal,)).fetchone()):
                    v.fail('generation_slot_invalid')
                roots = self._roots(db, ordinal, plan, rows)
                binding = vals.binding(stage, ordinal, entry['capture'], roots)
                fields = vals.generation_fields(stage, ordinal, binding, entry['capture'])
                gen_id, now = fields['gen_id'], time.time_ns()
                _insert(db, 'generations', dict(fields, state='building', created_ns=now))
                for member in plan['members']:
                    _insert(db, 'wiki_members', dict(member, gen_id=gen_id))
                for source in plan['sources']:
                    _insert(db, 'source_members', dict(source, gen_id=gen_id))
                for card in plan['cards']:
                    _insert(db, 'card_sources', dict(gen_id=gen_id, wiki_path=card['wiki_path'], ordinal=card['ordinal'],
                            fingerprint_id=card['fingerprint_id'], spans_json=v.canonical(card['spans'])))
                _insert(db, 'generation_seals', dict(roots, gen_id=gen_id, binding_digest=binding, sealed_ns=now))
                if db.execute("UPDATE generations SET state='prepared' WHERE gen_id=? AND state='building'",
                              (gen_id,)).rowcount != 1:
                    v.fail('generation_slot_invalid')
                _current_capture(saved, self._repository, self._policy, entry['capture'])
                _live(db, saved, files)
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

    def _verify_generation(self, db, handle, entry, saved, generation, deadline=None):
        """Recompute rows, FTS proof, roots and binding from the original slot."""
        slot = db.execute('SELECT * FROM generation_slots WHERE ordinal=?', (generation['ordinal'],)).fetchone()
        if slot is None or slot['view_id'] != generation['view_id']:
            v.fail('generation_slot_invalid')
        stage = vals.check_stage(v.parse_canonical(slot['stage_json']), handle, self._incarnation)
        if (stage['build_id'] != slot['build_id'] or stage['manifest_digest'] != slot['manifest_digest']
                or stage['worktree_binding'] != saved['binding_digest']
                or stage['settings_sha'] != saved['settings_sha']):
            v.fail('generation_binding_mismatch')
        seal = db.execute('SELECT * FROM generation_seals WHERE gen_id=?', (generation['gen_id'],)).fetchone()
        if (seal is None or view_fts.storage_digest(db, generation['ordinal'], deadline) != seal['fts_storage_digest']):
            v.fail('fts_storage_mismatch')
        plan = self._derive(handle, stage['manifest'], stage['skip_paths'])
        if self._stored(db, generation['gen_id']) != (plan['members'], plan['sources'], plan['cards']):
            v.fail('generation_rows_mismatch')
        roots = self._roots(db, generation['ordinal'], plan, vals.fts_rows(plan['bodies'], {m['path']: m['display_title'] for m in plan['members']}), deadline)
        binding = vals.binding(stage, generation['ordinal'], entry['capture'], roots)
        fields = vals.generation_fields(stage, generation['ordinal'], binding, entry['capture'])
        seal = db.execute('SELECT * FROM generation_seals WHERE gen_id=?', (generation['gen_id'],)).fetchone()
        if (seal is None or any(generation[k] != val for k, val in fields.items())
                or seal['binding_digest'] != binding or any(seal[k] != val for k, val in roots.items())):
            v.fail('generation_seal_mismatch')
        return stage

    def publish_generation(self, handle, gen_id, *, expected_head):
        entry = self._check(handle)
        base = vals.expected_head(expected_head, handle.view_id, self._incarnation)
        if not isinstance(gen_id, str) or not vals.GEN_ID.fullmatch(gen_id):
            v.fail('generation_id_invalid')
        saved = self._observe(entry)
        with self._session(write=True) as db:
            generation = db.execute('SELECT * FROM generations WHERE gen_id=?', (gen_id,)).fetchone()
            if generation is None or generation['view_id'] != handle.view_id:
                v.fail('generation_missing')
            stage = self._verify_generation(db, handle, entry, saved, generation)
            if stage['base'] != base:
                v.fail('view_head_conflict')
            current = self._head(db, handle)
            if generation['state'] == 'published':
                if current['gen_id'] == gen_id and current['epoch'] == base['epoch'] + 1:
                    _current_capture(saved, self._repository, self._policy, entry['capture'])
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
            _current_capture(saved, self._repository, self._policy, entry['capture'])
            _live(db, saved, stage['files'])
            return self._head(db, handle)

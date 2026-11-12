"""Row-format generations: per-view card intervals, one head FTS, rollback deltas.

A publication writes only the changed card rows, the matching head FTS rows,
one sealed generation row and its deltas, then moves the head by CAS, all in
one SQLite transaction. The seal is a structural commitment over the stored
rows and FTS bytes, never compiler truth, source observation or operational
authority. This module serves lexical normal generations only; legacy
imports, vector coverage and source relations refuse with a fixed reason.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
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


LEGACY_FIELDS = ('origin', 'path_key', 'blob_sha', 'title', 'position', 'active')
_CARD_DOMAIN, _LEGACY_DOMAIN = 'memvane-row-card-v1', 'memvane-row-legacy-row-v1'
_FTS_DOMAINS = {'normal': 'memvane-row-fts-v1', 'base': 'memvane-row-fts-base-v1',
                'sidecar': 'memvane-row-fts-sidecar-v1'}
EMPTY_ACC = dict(bytes=0, count=0, sum='0' * 64)


def _row_bytes(row):
    """Unambiguous bytes of one row: sorted keys, each key and value length-prefixed and typed."""
    parts = []
    for key in sorted(row):
        value = row[key]
        if value is None:
            encoded = b'n'
        elif type(value) is int:
            encoded = b'i' + str(value).encode('ascii')
        elif isinstance(value, str):
            encoded = b's' + value.encode('utf8')
        else:
            v.fail('seal_accumulator_invalid')
        name = key.encode('ascii')
        parts.append(len(name).to_bytes(2, 'big') + name + len(encoded).to_bytes(8, 'big') + encoded)
    return b''.join(parts)


def acc_update(acc, domain, row, sign=1):
    """Add (sign 1) or remove (sign -1) one row's contribution to an order-free multiset accumulator."""
    raw = _row_bytes(row)
    value = int.from_bytes(hashlib.sha256(domain.encode('ascii') + b'\0' + raw).digest(), 'big')
    result = dict(bytes=acc['bytes'] + sign * len(raw), count=acc['count'] + sign,
                  sum=format((int(acc['sum'], 16) + sign * value) % (1 << 256), '064x'))
    if result['bytes'] < 0 or result['count'] < 0:
        v.fail('seal_accumulator_invalid')
    return result


def acc_value(text):
    acc = v.parse_canonical(text)
    v.exact(acc, ('bytes', 'count', 'sum'))
    if (type(acc['bytes']) is not int or type(acc['count']) is not int or acc['bytes'] < 0 or acc['count'] < 0
            or not isinstance(acc['sum'], str) or len(acc['sum']) != 64
            or acc['sum'].strip('0123456789abcdef')):
        v.fail('seal_accumulator_invalid')
    return acc


def acc_digest(kind, acc):
    return v.digest('memvane-row-' + kind + '-acc-v1', acc)


def _card_value(row):
    return {k: row[k] for k in CARD_FIELDS}


def _legacy_value(row):
    return {k: row[k] for k in LEGACY_FIELDS}


_SOURCE_DOMAIN = 'memvane-row-source-v1'


def _source_value(row):
    return dict(path=row['path'], observation_id=row['observation_id'])


_CARD_SOURCE_DOMAIN = 'memvane-row-card-source-v1'
CARD_SOURCE_FIELDS = ('card_path', 'source_path', 'observation_id', 'spans_json')


def _card_source_value(row):
    return {k: row[k] for k in CARD_SOURCE_FIELDS}


def _card_source_key(row):
    return (row['card_path'].encode('utf8'), row['source_path'].encode('utf8'))


def member_acc(cards, legacy_rows=(), sources=(), card_sources=()):
    acc = EMPTY_ACC
    for row in cards:
        acc = acc_update(acc, _CARD_DOMAIN, _card_value(row))
    for row in legacy_rows:
        acc = acc_update(acc, _LEGACY_DOMAIN, _legacy_value(row))
    for row in sources:
        acc = acc_update(acc, _SOURCE_DOMAIN, _source_value(row))
    for row in card_sources:
        acc = acc_update(acc, _CARD_SOURCE_DOMAIN, _card_source_value(row))
    return acc


def member_digest(cards, legacy_rows=(), sources=(), card_sources=()):
    """Card rows, a legacy generation's FTS corpus rows, the source rows and the card source
    rows as one multiset commitment."""
    return acc_digest('member', member_acc(cards, legacy_rows, sources, card_sources))


def fts_acc(rows, origin='normal', positions=None):
    acc = EMPTY_ACC
    for index, row in enumerate(rows):
        value = dict(row) if positions is None else dict(row, position=positions[index])
        acc = acc_update(acc, _FTS_DOMAINS[origin], value)
    return acc


def legacy_physical(base_physical, side_physical):
    return v.digest('memvane-row-legacy-fts-storage-v1', dict(base=base_physical, sidecar=side_physical))


def deferred_physical(prior_physical, gen_id):
    """The physical value a rollback seals when it defers the FTS physical digest."""
    return v.digest('memvane-row-physical-deferred-v1', dict(prior=prior_physical, gen_id=gen_id))


def seal_digest(fields):
    return v.digest(ROW_SEAL_SCHEMA, fields)


def _seal_fields(incarnation, generation):
    keys = ('ordinal', 'gen_id', 'view_id', 'epoch', 'prior_ordinal', 'kind', 'sidecar_present',
            'capture_json', 'policy_digest', 'skip_digest', 'recipe_id', 'profile_id', 'coverage',
            'member_digest', 'source_digest', 'logical_fts_digest', 'physical_fts_digest',
            'member_acc', 'fts_acc', 'legacy_json')
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


def _card_source_item(old, new):
    """One staged card source change: the new row (put) or the closed key (delete)."""
    if new is None:
        return dict(card_path=old['card_path'], source_path=old['source_path'], action='delete',
                    observation_id=None, spans_json=None)
    return dict(new, action='put')


def _item_row(item):
    if item['action'] == 'delete':
        return None
    extra = v.parse_canonical(item['metadata_json'])
    v.exact(extra, ('title', 'eligible', 'metadata'))
    return dict(path=item['path'], blob_sha=item['blob_sha'], title=extra['title'], eligible=extra['eligible'],
                trust_digest=item['trust_digest'], metadata_json=extra['metadata'])


def _sorted_paths(paths):
    return sorted(paths, key=lambda path: path.encode('utf8'))


def _stage_value(incarnation, handle, saved, base, job_id, scan_digest, skips, final, items, spec=None,
                 sources=(), source_items=(), card_sources=(), card_source_items=()):
    """Every committed stage input; the generation id derives from it at prepare."""
    value = dict(schema=ROW_STAGE_SCHEMA, job_id=job_id, incarnation=incarnation,
                 view=vals.view_fields(handle), worktree_binding=saved['binding_digest'], base=base,
                 scan_digest=scan_digest, skip_digest=v.digest('memvane-row-skip-v1', skips),
                 member_digest=member_digest(final, (), sources, card_sources),
                 items_digest=v.digest('memvane-row-stage-items-v1', items))
    if source_items:
        value['source_items_digest'] = v.digest('memvane-row-stage-sources-v1', list(source_items))
    if card_source_items:
        value['card_source_items_digest'] = v.digest('memvane-row-stage-card-sources-v1',
                                                     list(card_source_items))
    if spec is not None:
        value['vector_spec'] = spec
    return value


def _coverage_fields(spec):
    if spec is None:
        return dict(recipe_id=None, profile_id=None, coverage='lexical')
    return dict(recipe_id=spec['recipe_id'], profile_id=spec['profile_id'], coverage='complete')


LEGACY_OBSERVATION_FIELDS = ('observation', 'corpus_observation', 'sidecar_observation', 'sidecar_commitment',
                             'scan')


def _legacy_observation(legacy):
    """The v6 observation values a legacy generation keeps in ``legacy_json``."""
    return {key: legacy.get(key) for key in LEGACY_OBSERVATION_FIELDS}


def _pin_seal_value(generation):
    return dict(seal_digest=generation['seal_digest'])


def _pin_seal(generation):
    """The pin's seal commitment, computed like the generation format's pin seal."""
    return v.digest('memvane-read-pin-seal-v1', _pin_seal_value(generation))


def _path_allowed(path, prefixes, exclusions):
    """The generation format's path filters: exact or directory prefix, then exclusions."""
    if prefixes is not None and not any(path == prefix or path.startswith(prefix + '/') for prefix in prefixes):
        return False
    return not exclusions or path not in exclusions


def _locators(spans):
    """Sorted line locators; any span kind but the two line kinds refuses the read."""
    import source_spans
    for span in spans:
        if not isinstance(span, dict) or span.get('kind') not in (v.LEGACY_LINE_SPAN, v.LINE_SPAN):
            v.fail('legacy_card_trust_invalid')
    ordered = sorted(spans, key=lambda span: (span['path'], span['startLine'], span['endLine']))
    return [source_spans.locator(span) for span in ordered]


def _legacy_corpus_rows(legacy):
    """Corpus rows (base and sidecar, with active flags) of one legacy evidence mapping."""
    sidecar = 'sidecar_corpus' in legacy
    rows = [dict(origin='base', path_key=row['path'], blob_sha=row['hash'], title=None, position=None,
                 active=int(legacy['active_fts']['base'].get(row['path']) == row['hash']))
            for row in legacy['corpus']]
    if sidecar:
        rows.extend(dict(origin='sidecar', path_key=row['path'], blob_sha=row['hash'], title=row['title'],
                         position=row['rowid'],
                         active=int(legacy['active_fts']['sidecar'].get(row['path']) == row['hash']))
                    for row in legacy['sidecar_corpus'])
    rows.sort(key=lambda row: (row['origin'], row['path_key'].encode('utf8')))
    return rows


def _legacy_card_rows(legacy, members, spans):
    """Card rows of a legacy import: FTS path, origin and line spans in each row's metadata."""
    sidecar = 'sidecar_corpus' in legacy
    cards = []
    for member in members:
        origin = legacy['fts_sources'][member['path']] if sidecar else 'base'
        meta = dict(fts_path=legacy['fts_paths'][member['path']], origin=origin,
                    spans=spans.get(member['path'], []))
        cards.append(dict(_card_row(member), metadata_json=v.canonical(meta)))
    return cards


def _legacy_scan(manifest, skips, legacy, legacy_rows):
    return v.digest('memvane-row-stage-scan-v1', dict(
        manifest=vals.manifest_digest(manifest), skip_paths=skips, files=[],
        legacy=dict(observation=_legacy_observation(legacy),
                    corpus=v.digest('memvane-row-legacy-corpus-v1', [_legacy_value(r) for r in legacy_rows]))))


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

    @staticmethod
    def _rows_open_sources(db, view_id, ordinal=None):
        if ordinal is None:
            cursor = db.execute('SELECT path,observation_id FROM s_sources WHERE view_id=? AND gen_to IS NULL '
                                'ORDER BY path', (view_id,))
        else:
            cursor = db.execute('SELECT path,observation_id FROM s_sources WHERE view_id=? AND gen_from<=? '
                                'AND (gen_to IS NULL OR gen_to>?) ORDER BY path', (view_id, ordinal, ordinal))
        try:
            return [dict(path=row[0], observation_id=row[1]) for row in cursor]
        finally:
            cursor.close()

    @staticmethod
    def _observation_objects(spec, fingerprint_id):
        """``(content, observation)`` of one fingerprint (HEAD and scan are not identity)."""
        if spec['binding_state'] == 'observed':
            content = vals.source_content(spec['source_sha'], spec['byte_length'])
            return content, vals.source_observation(
                worktree_id=spec['worktree_id'], branch_key=spec['branch_key'], path=spec['path'],
                tracked_state=spec['tracked_state'], binding_state='observed',
                content_id=content['content_id'], legacy_fingerprint_id=None)
        return None, vals.source_observation(
            worktree_id=spec['worktree_id'], branch_key=spec['branch_key'], path=spec['path'],
            tracked_state=spec['tracked_state'], binding_state=spec['binding_state'],
            content_id=None, legacy_fingerprint_id=fingerprint_id)

    def _rows_source_objects(self, sources, *, cache=None):
        """Content and observation rows of a manifest's present sources (HEAD and scan are not identity)."""
        rows, objects = [], []
        for source in sources:
            if source['presence'] != 'present':
                continue
            spec = self._fingerprint(source['fingerprint_id'], cache=cache)
            content, observation = self._observation_objects(spec, source['fingerprint_id'])
            objects.append((content, observation))
            rows.append(dict(path=source['path'], observation_id=observation['observation_id']))
        return sorted(rows, key=lambda row: row['path'].encode('utf8')), objects

    def _rows_card_source_objects(self, cards, *, cache=None):
        """One card source row per (card, source path) of derived card refs, and their objects.

        ``cards`` are ``_derive``'s refs in (card, ordinal) order. Refs of one
        card to the same source path merge their spans in ordinal order when
        they name one observation; two observations of one path refuse.
        """
        grouped, objects = {}, []
        for card in cards:
            spec = self._fingerprint(card['fingerprint_id'], cache=cache)
            content, observation = self._observation_objects(spec, card['fingerprint_id'])
            key = (card['wiki_path'], spec['path'])
            entry = grouped.get(key)
            if entry is None:
                grouped[key] = dict(card_path=card['wiki_path'], source_path=spec['path'],
                                    observation_id=observation['observation_id'], spans=list(card['spans']))
                objects.append((content, observation))
            elif entry['observation_id'] != observation['observation_id']:
                v.fail('card_source_conflict')
            else:
                entry['spans'].extend(card['spans'])
        rows = [dict(card_path=entry['card_path'], source_path=entry['source_path'],
                     observation_id=entry['observation_id'], spans_json=v.canonical(entry['spans']))
                for entry in grouped.values()]
        return sorted(rows, key=_card_source_key), objects

    def _rows_card_source_changes(self, db, view_id, current, plan):
        """``(final rows, new objects, stage items)`` of an ordinary stage's card sources.

        A card's source rows are a function of its path and trust digest
        (the attestation's refs), so only cards whose row changes are
        re-derived; the open rows of the others are kept.
        """
        wanted = {member['path']: _card_row(member) for member in plan['members']}
        changed = {path for path in set(current) | set(wanted) if current.get(path) != wanted.get(path)}
        derived, objects = self._rows_card_source_objects([card for card in plan['cards']
                                                           if card['wiki_path'] in changed])
        opened = self._rows_open_card_sources(db, view_id)
        final = sorted([row for row in opened if row['card_path'] not in changed] + derived, key=_card_source_key)
        items = [_card_source_item(old, new) for old, new in self._card_source_diff(opened, final)]
        return final, objects, items

    @staticmethod
    def _rows_card_source_spans(row):
        spans = v.parse_canonical(row['spans_json'])
        if not isinstance(spans, list):
            v.fail('card_source_invalid')
        return spans

    @staticmethod
    def _rows_open_card_sources(db, view_id, ordinal=None, card_paths=None):
        """The view's card source rows visible in ``ordinal`` (default: the open interval)."""
        sql = 'SELECT card_path,source_path,observation_id,spans_json FROM s_card_sources WHERE view_id=? '
        args = [view_id]
        if ordinal is None:
            sql += 'AND gen_to IS NULL '
        else:
            sql += 'AND gen_from<=? AND (gen_to IS NULL OR gen_to>?) '
            args += [ordinal, ordinal]
        if card_paths is not None:
            sql += 'AND card_path IN (' + ','.join('?' for _ in card_paths) + ') '
            args += list(card_paths)
        cursor = db.execute(sql + 'ORDER BY card_path,source_path', args)
        try:
            return [dict(zip(CARD_SOURCE_FIELDS, tuple(row))) for row in cursor]
        finally:
            cursor.close()

    @staticmethod
    def _card_source_diff(current, wanted):
        """``[(old, new)]`` card source rows by (card, source path), binary order."""
        current = {_card_source_key(row): row for row in current}
        wanted = {_card_source_key(row): row for row in wanted}
        return [(current.get(key), wanted.get(key)) for key in sorted(set(current) | set(wanted))
                if current.get(key) != wanted.get(key)]

    @staticmethod
    def _apply_card_source_delta(db, view_id, ordinal, old, new):
        """Close and/or open one card source interval and record its rollback delta."""
        row = old if old is not None else new
        for value in (old, new):
            if value is not None:
                v.exact(value, CARD_SOURCE_FIELDS)
        current = db.execute('SELECT card_path,source_path,observation_id,spans_json FROM s_card_sources '
                             'WHERE view_id=? AND card_path=? AND source_path=? AND gen_to IS NULL',
                             (view_id, row['card_path'], row['source_path'])).fetchone()
        if (None if current is None else dict(zip(CARD_SOURCE_FIELDS, tuple(current)))) != old:
            v.fail('card_source_delta_conflict')
        if old is not None and db.execute(
                'UPDATE s_card_sources SET gen_to=? WHERE view_id=? AND card_path=? AND source_path=? '
                'AND gen_to IS NULL', (ordinal, view_id, old['card_path'], old['source_path'])).rowcount != 1:
            v.fail('card_source_delta_conflict')
        if new is not None:
            db.execute('INSERT INTO s_card_sources(view_id,card_path,source_path,gen_from,gen_to,observation_id,'
                       'spans_json) VALUES (?,?,?,?,NULL,?,?)', (view_id, new['card_path'], new['source_path'],
                                                                 ordinal, new['observation_id'], new['spans_json']))
        # A view's first generation has no prior to roll back to: no delta.
        prior = db.execute('SELECT prior_ordinal FROM s_generations WHERE view_id=? AND ordinal=?',
                           (view_id, ordinal)).fetchone()
        if prior is not None and prior[0] is not None:
            key = v.canonical([row['card_path'], row['source_path']])
            db.execute('INSERT INTO s_deltas(view_id,ordinal,rel,key_json,old_json,new_json) VALUES (?,?,?,?,?,?)',
                       (view_id, ordinal, 'card_source', key, None if old is None else v.canonical(old),
                        None if new is None else v.canonical(new)))
            for side, value in (('old', old), ('new', new)):
                if value is not None:
                    db.execute('INSERT INTO s_delta_observations(view_id,ordinal,rel,key_json,side,observation_id) '
                               'VALUES (?,?,?,?,?,?)', (view_id, ordinal, 'card_source', key, side,
                                                         value['observation_id']))

    @staticmethod
    def _rows_put_source_objects(db, objects):
        """Insert content and observation rows that are not stored yet; stored ones must be identical."""
        from laya_blob_store import _insert
        for content, observation in objects:
            if content is not None:
                row = db.execute('SELECT content_id,source_sha,byte_length FROM s_source_content WHERE content_id=?',
                                 (content['content_id'],)).fetchone()
                if row is None:
                    _insert(db, 's_source_content', content)
                else:
                    vals.check_source_content(dict(zip(('content_id', 'source_sha', 'byte_length'), tuple(row))),
                                              content['source_sha'], content['byte_length'])
            fields = ('observation_id', *vals.SOURCE_OBSERVATION_FIELDS)
            row = db.execute('SELECT ' + ','.join(fields) + ' FROM s_source_observations WHERE observation_id=?',
                             (observation['observation_id'],)).fetchone()
            if row is None:
                _insert(db, 's_source_observations', observation)
            elif vals.check_source_observation(dict(zip(fields, tuple(row)))) != observation:
                v.fail('source_observation_mismatch')

    @staticmethod
    def _source_diff(current, wanted):
        """``[(old, new)]`` source rows by path, binary path order."""
        current = {row['path']: row for row in current}
        wanted = {row['path']: row for row in wanted}
        return [(current.get(path), wanted.get(path)) for path in _sorted_paths(set(current) | set(wanted))
                if current.get(path) != wanted.get(path)]

    @staticmethod
    def _apply_source_delta(db, view_id, ordinal, old, new):
        """Close and/or open one source interval and record its rollback delta."""
        row = old if old is not None else new
        current = db.execute('SELECT path,observation_id FROM s_sources WHERE view_id=? AND path=? AND gen_to IS NULL',
                             (view_id, row['path'])).fetchone()
        if (None if current is None else dict(path=current[0], observation_id=current[1])) != old:
            v.fail('source_delta_conflict')
        if old is not None and db.execute('UPDATE s_sources SET gen_to=? WHERE view_id=? AND path=? AND gen_to IS NULL',
                                          (ordinal, view_id, old['path'])).rowcount != 1:
            v.fail('source_delta_conflict')
        if new is not None:
            db.execute('INSERT INTO s_sources(view_id,path,gen_from,gen_to,observation_id) VALUES (?,?,?,NULL,?)',
                       (view_id, new['path'], ordinal, new['observation_id']))
        # A view's first generation has no prior to roll back to: no delta.
        prior = db.execute('SELECT prior_ordinal FROM s_generations WHERE view_id=? AND ordinal=?',
                           (view_id, ordinal)).fetchone()
        if prior is not None and prior[0] is not None:
            key = v.canonical([row['path']])
            db.execute('INSERT INTO s_deltas(view_id,ordinal,rel,key_json,old_json,new_json) VALUES (?,?,?,?,?,?)',
                       (view_id, ordinal, 'source', key, None if old is None else v.canonical(old),
                        None if new is None else v.canonical(new)))
            for side, value in (('old', old), ('new', new)):
                if value is not None:
                    db.execute('INSERT INTO s_delta_observations(view_id,ordinal,rel,key_json,side,observation_id) '
                               'VALUES (?,?,?,?,?,?)', (view_id, ordinal, 'source', key, side, value['observation_id']))

    @staticmethod
    def _rows_open_legacy(db, view_id, ordinal):
        cursor = db.execute('SELECT origin,path_key,blob_sha,title,position,active FROM s_legacy_fts '
                            'WHERE view_id=? AND gen_from<=? AND (gen_to IS NULL OR gen_to>?) '
                            'ORDER BY origin,path_key', (view_id, ordinal, ordinal))
        try:
            return [dict(zip(LEGACY_FIELDS, tuple(row))) for row in cursor]
        finally:
            cursor.close()

    def _rows_legacy_fts(self, legacy_rows, *, cache, deadline=None):
        """Base rows as the generation format built them, sidecar rows in their original order."""
        bodies, side = {}, []
        for row in legacy_rows:
            _expired(deadline)
            blob = row['blob_sha']
            if blob not in cache:
                cache[blob] = self._blob(blob)
            if row['origin'] == 'base':
                bodies[row['path_key']] = cache[blob]
            else:
                side.append(row)
        side.sort(key=lambda row: row['position'])
        sidecar = [dict(filepath=row['path_key'], title=row['title'], body=cache[row['blob_sha']],
                        path_key=row['path_key']) for row in side]
        return vals.fts_rows(bodies, deadline=deadline), (sidecar if side else None), [r['position'] for r in side]

    @staticmethod
    def _rows_legacy_fts_acc(base_rows, side_rows, positions):
        acc = fts_acc(base_rows, 'base')
        for index, row in enumerate(side_rows or ()):
            acc = acc_update(acc, _FTS_DOMAINS['sidecar'], dict(row, position=positions[index]))
        return acc

    @staticmethod
    def _rows_legacy_content(generation, legacy_rows):
        return generation['kind'] == 'legacy' or bool(legacy_rows)

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
        import laya_view_vectors as vectors
        spec = vectors.spec(vector_spec)
        derive_manifest = manifest is None and type(legacy_snapshot) is dict and 'observation' in legacy_snapshot
        if not derive_manifest:
            manifest = vals.manifest(manifest, handle.policy_digest)
        saved = self._observe(entry, deadline=deadline)
        skips = _skip_patterns(saved)
        legacy = None
        if legacy_snapshot is not None:
            # Legacy import: the corpus is re-derived at publish from the re-observed v6 snapshot.
            manifest, legacy = self._legacy_evidence(handle, manifest, legacy_snapshot, spec, derive_manifest,
                                                     deadline)
        with self._session() as db:
            if self._rows_head(db, handle) != base:
                v.fail('view_head_conflict')
            plan = self._derive(handle, manifest, skips, deadline=deadline,
                                legacy_paths=() if legacy is None else legacy['paths'])
            current = {row['path']: row for row in self._rows_open(db, handle.view_id)}
            source_rows, source_objects = self._rows_source_objects(plan['sources'])
            source_items = [dict(path=(old or new)['path'], action='delete' if new is None else 'put',
                                 observation_id=None if new is None else new['observation_id'])
                            for old, new in self._source_diff(self._rows_open_sources(db, handle.view_id),
                                                              source_rows)]
            # A legacy import keeps its line spans in card metadata; its publication closes these rows.
            card_rows, card_objects, card_items = [], [], []
            if legacy is None:
                card_rows, card_objects, card_items = self._rows_card_source_changes(db, handle.view_id, current,
                                                                                    plan)
            # Zero or complete: every eligible member's set is validated before any write.
            self._rows_coverage(db, [_card_row(m) for m in plan['members']], spec, full=True, deadline=deadline)
        if legacy is None:
            wanted = {member['path']: _card_row(member) for member in plan['members']}
        else:
            spans = {}
            for card in plan['cards']:
                spans.setdefault(card['wiki_path'], []).extend(card['spans'])
            wanted = {row['path']: row for row in _legacy_card_rows(legacy, plan['members'], spans)}
        items = [_stage_item(path, wanted.get(path)) for path in _sorted_paths(set(current) | set(wanted))
                 if current.get(path) != wanted.get(path)]
        final = [wanted[path] for path in _sorted_paths(wanted)]
        capture_json = v.canonical(entry['capture'])
        if legacy is None:
            files = _wiki_files(saved, plan['members'])
            job_id = uuid.uuid4().hex
            scan_digest = v.digest('memvane-row-stage-scan-v1', dict(
                manifest=vals.manifest_digest(manifest), skip_paths=skips,
                files=sorted([path, node] for path, node in files.items())))
        else:
            # A migration's provenance scan names its build, so a re-run prepare replays the same stage.
            files = {}
            job_id = (legacy_snapshot.get('build_id', legacy_snapshot['provenance_scan']['id'])
                      if derive_manifest else uuid.uuid4().hex)
            v.identifier(job_id, v.UUID)
            scan_digest = _legacy_scan(manifest, skips, legacy, _legacy_corpus_rows(legacy))
        stage = _stage_value(self._incarnation, handle, saved, base, job_id, scan_digest, skips, final, items,
                             spec, source_rows, source_items, card_rows, card_items)
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
            old = db.execute('SELECT job_id,state,scan_digest FROM s_stage_jobs WHERE view_id=?',
                             (handle.view_id,)).fetchone()
            if old is not None and tuple(old) == (job_id, 'ready', scan_digest):
                return gen_id  # exact replay of this stage
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
            self._rows_put_source_objects(db, source_objects)
            for item in source_items:
                db.execute('INSERT INTO s_stage_source_changes(job_id,path,action,observation_id) VALUES (?,?,?,?)',
                           (job_id, item['path'], item['action'], item['observation_id']))
            self._rows_put_source_objects(db, card_objects)
            for item in card_items:
                db.execute('INSERT INTO s_stage_card_source_changes(job_id,card_path,source_path,action,'
                           'observation_id,spans_json) VALUES (?,?,?,?,?,?)',
                           (job_id, item['card_path'], item['source_path'], item['action'],
                            item['observation_id'], item['spans_json']))
            if spec is not None:
                db.execute('UPDATE s_stage_jobs SET vector_spec_json=? WHERE job_id=?', (v.canonical(spec), job_id))
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
            return self._rows_publish_legacy(handle, gen_id, entry, base, legacy_snapshot, deadline)
        saved = self._observe(entry, deadline=deadline)
        skips = _skip_patterns(saved)

        def capture():
            store_module._current_capture(saved, self._repository, self._policy, entry['capture'],
                                          deadline=deadline)

        def staged_state(db):
            """The view's ready stage, re-derived only from committed rows."""
            job = db.execute('SELECT job_id,incarnation,base_gen_id,base_epoch,capture_json,policy_digest,'
                             'scan_digest,state,vector_spec_json FROM s_stage_jobs WHERE view_id=?',
                             (handle.view_id,)).fetchone()
            if job is None or job[7] != 'ready' or gen_id[2:34] != job[0]:
                v.fail('generation_missing')
            if (tuple(job[1:6]) != (self._incarnation, base['gen_id'], base['epoch'],
                                    v.canonical(entry['capture']), handle.policy_digest)):
                v.fail('stage_identity_changed')
            items = [vals.check_stage_item(dict(zip(vals.STAGE_ITEM_FIELDS, tuple(row)))) for row in db.execute(
                'SELECT path,action,input_sha,blob_sha,trust_digest,metadata_json FROM s_stage_items '
                'WHERE job_id=? ORDER BY path', (job[0],))]
            source_items = [dict(path=r[0], action=r[1], observation_id=r[2]) for r in db.execute(
                'SELECT path,action,observation_id FROM s_stage_source_changes WHERE job_id=? ORDER BY path',
                (job[0],))]
            sources = {row['path']: row for row in self._rows_open_sources(db, handle.view_id)}
            for item in source_items:
                if item['action'] == 'delete':
                    sources.pop(item['path'], None)
                else:
                    sources[item['path']] = dict(path=item['path'], observation_id=item['observation_id'])
            final_sources = [sources[path] for path in _sorted_paths(sources)]
            card_items = [dict(zip(('card_path', 'source_path', 'action', 'observation_id', 'spans_json'),
                                   tuple(r))) for r in db.execute(
                'SELECT card_path,source_path,action,observation_id,spans_json FROM s_stage_card_source_changes '
                'WHERE job_id=? ORDER BY card_path,source_path', (job[0],))]
            card_sources = {_card_source_key(row): row for row in self._rows_open_card_sources(db, handle.view_id)}
            for item in card_items:
                key = _card_source_key(item)
                if item['action'] == 'delete':
                    card_sources.pop(key, None)
                else:
                    card_sources[key] = {k: item[k] for k in CARD_SOURCE_FIELDS}
            final_card_sources = [card_sources[key] for key in sorted(card_sources)]
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
            import laya_view_vectors as vectors
            spec = None if job[8] is None else vectors.spec(v.parse_canonical(job[8]))
            stage = _stage_value(self._incarnation, handle, saved, base, job[0], job[6], skips, final, items,
                                 spec, final_sources, source_items, final_card_sources, card_items)
            if vals.gen_id(job[0], v.digest(ROW_STAGE_SCHEMA, stage)) != gen_id:
                v.fail('stage_identity_changed')
            if any('fts_path' in v.parse_canonical(row['metadata_json']) for row in final):
                v.fail('migration_publish_proof_required')
            return stage, items, current, final, final_sources, final_card_sources

        stage = files = rows = None
        with self._session() as db:
            head = self._rows_head_row(db, handle.view_id)
            published = db.execute('SELECT 1 FROM s_generations WHERE gen_id=?', (gen_id,)).fetchone()
            if published is None:
                if (head.gen_id, head.epoch) != (base['gen_id'], base['epoch']):
                    v.fail('view_head_conflict')
                stage, items, _, final, _, _ = staged_state(db)
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
            items, final_sources, final_card_sources = again[1], again[4], again[5]
            spec = stage.get('vector_spec')
            self._rows_coverage(db, final, spec, full=False, deadline=deadline)
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

            # A publication over a legacy head closes its whole FTS corpus.
            legacy_deltas = ([] if head.ordinal is None else
                             [(row, None) for row in self._rows_open_legacy(db, handle.view_id, head.ordinal)])
            generation = dict(gen_id=gen_id, view_id=handle.view_id, epoch=base['epoch'] + 1,
                              prior_ordinal=head.ordinal, kind='normal', sidecar_present=0,
                              capture_json=v.canonical(entry['capture']), policy_digest=handle.policy_digest,
                              skip_digest=stage['skip_digest'], **_coverage_fields(spec),
                              expected_member_digest=stage['member_digest'], legacy_json=None,
                              source_digest=v.digest('memvane-row-sources-v1', []))
            source_deltas = self._source_diff(self._rows_open_sources(db, handle.view_id), final_sources)
            card_source_deltas = self._card_source_diff(self._rows_open_card_sources(db, handle.view_id),
                                                        final_card_sources)
            written = self._rows_write(db, head, generation, deltas, rows, before_commit, deadline,
                                       legacy_deltas=legacy_deltas, source_deltas=source_deltas,
                                       card_source_deltas=card_source_deltas)
        return dict(view_id=handle.view_id, gen_id=written['gen_id'], epoch=written['epoch'],
                    incarnation=self._incarnation)

    def _rows_write(self, db, head, generation, deltas, rows, before_commit, deadline, *, rollback=None,
                    legacy_deltas=(), legacy_final=None, commit=None, source_deltas=(), card_source_deltas=()):
        """Changed card and legacy corpus rows, their head FTS rows, the sealed generation and the head CAS.

        Runs inside the caller's write transaction and commits it under the
        view's FTS capability; any failure rolls the whole publication back.
        A publication proves the full head FTS, runs the FTS5 integrity check
        and seals the physical digest. A rollback (``rollback`` = the head and
        prior generation rows) updates the head accumulators from the delta
        only, must reach the prior's accumulators and seals the deferral value.
        ``legacy_deltas`` are ``(old, new)`` legacy FTS corpus rows; a legacy
        card's FTS row lives in that corpus, not in the card's own row.
        """
        import laya_view_store as store_module
        view_id = generation['view_id']
        open_sources = {row['path']: row for row in self._rows_open_sources(db, view_id)}
        for old, new in source_deltas:
            if new is None:
                open_sources.pop(old['path'], None)
            else:
                open_sources[new['path']] = new
        final_sources = [open_sources[path] for path in _sorted_paths(open_sources)]
        open_cards = {_card_source_key(row): row for row in self._rows_open_card_sources(db, view_id)}
        for old, new in card_source_deltas:
            if new is None:
                open_cards.pop(_card_source_key(old), None)
            else:
                open_cards[_card_source_key(new)] = new
        final_card_sources = [open_cards[key] for key in sorted(open_cards)]
        sequence = db.execute("SELECT seq FROM sqlite_sequence WHERE name='s_generations'").fetchone()
        ordinal = (0 if sequence is None else sequence[0]) + 1
        changes = {'base': [], 'sidecar': []}
        text_changes = []  # (domain, fts row, sign) for the rollback accumulator
        for old, new in deltas:
            _expired(deadline)
            old_fts, new_fts = self._card_fts_row(old, deadline), self._card_fts_row(new, deadline)
            if old_fts != new_fts:
                changes['base'].append(dict(old=old_fts, new=new_fts))
                text_changes += [('normal', row, sign) for row, sign in ((old_fts, -1), (new_fts, 1)) if row]
        cache = {}
        for old, new in legacy_deltas:
            _expired(deadline)
            pair = {}
            for row, sign in ((old, -1), (new, 1)):
                if row is None:
                    continue
                fts_row = self._legacy_fts_row(row, cache, deadline)
                origin = row['origin']
                value = fts_row if origin == 'base' else dict(fts_row, position=row['position'])
                text_changes.append((origin, value, sign))
                pair['old' if sign < 0 else 'new'] = fts_row
            origin = (old if old is not None else new)['origin']
            if origin == 'base':
                # One change per base path keeps its rowid when only the content moves.
                if pair.get('old') != pair.get('new'):
                    changes['base'].append(dict(old=pair.get('old'), new=pair.get('new')))
            else:
                # Sidecar rows: deletes first, then inserts in their original order.
                if 'old' in pair:
                    changes['sidecar'].append(dict(old=pair['old'], new=None))
                if 'new' in pair:
                    changes['sidecar'].append(dict(old=None, new=pair['new']))
        changes['sidecar'].sort(key=lambda c: (c['new'] is not None,))
        after_sidecar = bool(generation['sidecar_present'])
        before_sidecar = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                    (fts.head_table(view_id, sidecar=True),)).fetchone() is not None
        with self._fts_guard.view_head(view_id):
            if head.ordinal is None:
                fts.create_head(db, view_id)
            fts.apply_mapped_head_changes(db, view_id, changes['base'], deadline=deadline)
            if after_sidecar and not before_sidecar:
                fts.create_head(db, view_id, sidecar=True)
            if after_sidecar:
                inserts = sorted((c for c in changes['sidecar'] if c['new'] is not None),
                                 key=lambda c: self._legacy_position(legacy_deltas, c['new']['path_key']))
                for change in (c for c in changes['sidecar'] if c['new'] is None):
                    fts.apply_mapped_head_changes(db, view_id, [change], sidecar=True, deadline=deadline)
                for change in inserts:
                    fts.apply_mapped_head_changes(db, view_id, [change], sidecar=True, deadline=deadline)
            elif before_sidecar:
                db.execute(f'DROP TABLE {fts.head_table(view_id, sidecar=True)}')
                db.execute("DELETE FROM s_head_fts_map WHERE view_id=? AND origin='sidecar'", (view_id,))
            if rollback is None and legacy_final is not None:
                # A legacy import: the full head proof over both corpora.
                final_cards, final_legacy, base_rows, side_rows, positions = legacy_final
                fts.verify_head_index(db, view_id)
                if side_rows is not None:
                    fts.verify_head_index(db, view_id, sidecar=True)
                self._fts_guard.freeze_head()
                _, base_physical = fts.head_proof(db, view_id, base_rows, deadline, logical=False)
                side_physical = None
                if side_rows is not None:
                    _, side_physical = fts.head_proof(db, view_id, side_rows, deadline, sidecar=True, logical=False)
                physical = legacy_physical(base_physical, side_physical)
                members = member_acc(final_cards, final_legacy, final_sources, final_card_sources)
                text_acc = self._rows_legacy_fts_acc(base_rows, side_rows, positions)
            elif rollback is None:
                if after_sidecar:
                    v.fail('row_format_unsupported')  # publications write ordinary generations only
                fts.verify_head_index(db, view_id)
                self._fts_guard.freeze_head()
                _, physical = fts.head_proof(db, view_id, rows, deadline, logical=False)
                members = member_acc(self._rows_open_after(db, view_id, deltas), (), final_sources,
                                     final_card_sources)
                text_acc = fts_acc(rows)
            else:
                current, prior = rollback
                members, text_acc = acc_value(current['member_acc']), acc_value(current['fts_acc'])
                for old, new in deltas:
                    if old is not None:
                        members = acc_update(members, _CARD_DOMAIN, _card_value(old), -1)
                    if new is not None:
                        members = acc_update(members, _CARD_DOMAIN, _card_value(new))
                for old, new in legacy_deltas:
                    if old is not None:
                        members = acc_update(members, _LEGACY_DOMAIN, _legacy_value(old), -1)
                    if new is not None:
                        members = acc_update(members, _LEGACY_DOMAIN, _legacy_value(new))
                for old, new in source_deltas:
                    if old is not None:
                        members = acc_update(members, _SOURCE_DOMAIN, _source_value(old), -1)
                    if new is not None:
                        members = acc_update(members, _SOURCE_DOMAIN, _source_value(new))
                for old, new in card_source_deltas:
                    if old is not None:
                        members = acc_update(members, _CARD_SOURCE_DOMAIN, _card_source_value(old), -1)
                    if new is not None:
                        members = acc_update(members, _CARD_SOURCE_DOMAIN, _card_source_value(new))
                for origin, row, sign in text_changes:
                    text_acc = acc_update(text_acc, _FTS_DOMAINS[origin], row, sign)
                if (members, text_acc) != (acc_value(prior['member_acc']), acc_value(prior['fts_acc'])):
                    v.fail('rollback_fts_mismatch')
                physical = deferred_physical(current['physical_fts_digest'], generation['gen_id'])
            generation = dict(generation, ordinal=ordinal, member_acc=v.canonical(members),
                              fts_acc=v.canonical(text_acc), member_digest=acc_digest('member', members),
                              logical_fts_digest=acc_digest('fts', text_acc), physical_fts_digest=physical)
            if rollback is None and generation['member_digest'] != generation.pop('expected_member_digest'):
                v.fail('generation_rows_mismatch')
            generation['seal_digest'] = seal_digest(_seal_fields(self._incarnation, generation))
            columns = tuple(generation)
            db.execute(f"INSERT INTO s_generations({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                       tuple(generation[k] for k in columns))
            for old, new in deltas:
                _expired(deadline)
                store_module._apply_card_delta(db, view_id, ordinal, old, new)
            for old, new in legacy_deltas:
                _expired(deadline)
                self._apply_legacy_delta(db, view_id, ordinal, old, new)
            for old, new in source_deltas:
                _expired(deadline)
                self._apply_source_delta(db, view_id, ordinal, old, new)
            for old, new in card_source_deltas:
                _expired(deadline)
                self._apply_card_source_delta(db, view_id, ordinal, old, new)
            if head.ordinal is None:
                db.execute('INSERT INTO s_heads(view_id,ordinal,epoch) VALUES (?,?,1)', (view_id, ordinal))
            elif db.execute('UPDATE s_heads SET ordinal=?,epoch=? WHERE view_id=? AND ordinal=? AND epoch=?',
                            (ordinal, generation['epoch'], view_id, head.ordinal, head.epoch)).rowcount != 1:
                v.fail('view_head_conflict')
            before_commit(db)
            _expired(deadline)
            (commit or (lambda db: db.commit()))(db)
        return generation

    def _rows_open_after(self, db, view_id, deltas):
        """The view's open card rows once ``deltas`` apply (path order)."""
        rows = {row['path']: row for row in self._rows_open(db, view_id)}
        for old, new in deltas:
            if new is None:
                rows.pop(old['path'], None)
            else:
                rows[new['path']] = new
        return [rows[path] for path in _sorted_paths(rows)]

    def _card_fts_row(self, row, deadline):
        """The head FTS row of one ordinary eligible card; legacy cards answer from their corpus."""
        if row is None or not row['eligible'] or 'fts_path' in v.parse_canonical(row['metadata_json']):
            return None
        return self._rows_fts_rows([row], deadline=deadline)[0]

    def _legacy_fts_row(self, row, cache, deadline):
        if row['blob_sha'] not in cache:
            cache[row['blob_sha']] = self._blob(row['blob_sha'])
        body = cache[row['blob_sha']]
        if row['origin'] == 'base':
            return vals.fts_rows({row['path_key']: body}, deadline=deadline)[0]
        return dict(filepath=row['path_key'], title=row['title'], body=body, path_key=row['path_key'])

    @staticmethod
    def _legacy_position(legacy_deltas, path_key):
        for old, new in legacy_deltas:
            if new is not None and new['origin'] == 'sidecar' and new['path_key'] == path_key:
                return new['position']
        v.fail('legacy_fts_delta_invalid')

    @staticmethod
    def _apply_legacy_delta(db, view_id, ordinal, old, new):
        """Close and/or open one legacy corpus interval and record its rollback delta."""
        row = old if old is not None else new
        for value in (old, new):
            if value is not None:
                v.exact(value, LEGACY_FIELDS)
        current = db.execute('SELECT origin,path_key,blob_sha,title,position,active FROM s_legacy_fts '
                             'WHERE view_id=? AND origin=? AND path_key=? AND gen_to IS NULL',
                             (view_id, row['origin'], row['path_key'])).fetchone()
        if (None if current is None else dict(zip(LEGACY_FIELDS, tuple(current)))) != old:
            v.fail('legacy_fts_delta_conflict')
        if old is not None and db.execute(
                'UPDATE s_legacy_fts SET gen_to=? WHERE view_id=? AND origin=? AND path_key=? AND gen_to IS NULL',
                (ordinal, view_id, old['origin'], old['path_key'])).rowcount != 1:
            v.fail('legacy_fts_delta_conflict')
        if new is not None:
            db.execute('INSERT INTO s_legacy_fts(view_id,origin,path_key,gen_from,gen_to,blob_sha,title,position,active) '
                       'VALUES (?,?,?,?,NULL,?,?,?,?)', (view_id, new['origin'], new['path_key'], ordinal,
                                                          new['blob_sha'], new['title'], new['position'], new['active']))
        # A view's first generation has no prior to roll back to: no delta.
        prior = db.execute('SELECT prior_ordinal FROM s_generations WHERE view_id=? AND ordinal=?',
                           (view_id, ordinal)).fetchone()
        if prior is not None and prior[0] is not None:
            key = v.canonical([row['origin'], row['path_key']])
            db.execute('INSERT INTO s_deltas(view_id,ordinal,rel,key_json,old_json,new_json) VALUES (?,?,?,?,?,?)',
                       (view_id, ordinal, 'legacy_fts', key, None if old is None else v.canonical(old),
                        None if new is None else v.canonical(new)))
            for side, value in (('old', old), ('new', new)):
                if value is not None:
                    db.execute('INSERT INTO s_delta_blobs(view_id,ordinal,rel,key_json,side,blob_sha) VALUES (?,?,?,?,?,?)',
                               (view_id, ordinal, 'legacy_fts', key, side, value['blob_sha']))

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
            current = db.execute('SELECT * FROM s_generations WHERE view_id=? AND ordinal=?',
                                 (handle.view_id, head.ordinal)).fetchone()
            current = dict(current)
            # Only the head and prior seal rows are re-checked: a rollback never re-reads the corpus.
            if seal_digest(_seal_fields(self._incarnation, current)) != current['seal_digest']:
                v.fail('generation_seal_mismatch')
            if current['prior_ordinal'] is None:
                v.fail('rollback_prior_missing')
            prior = db.execute('SELECT * FROM s_generations WHERE view_id=? AND ordinal=?',
                               (handle.view_id, current['prior_ordinal'])).fetchone()
            if prior is None:
                v.fail('rollback_prior_missing')
            prior = dict(prior)
            if seal_digest(_seal_fields(self._incarnation, prior)) != prior['seal_digest']:
                v.fail('generation_seal_mismatch')
            deltas, legacy_deltas, source_deltas, card_source_deltas = [], [], [], []
            for rel, old_json, new_json in db.execute(
                    'SELECT rel,old_json,new_json FROM s_deltas WHERE view_id=? AND ordinal=? '
                    'ORDER BY rel,key_json DESC', (handle.view_id, head.ordinal)).fetchall():
                if rel not in ('card', 'legacy_fts', 'source', 'card_source'):
                    v.fail('row_format_unsupported')
                old = None if old_json is None else v.parse_canonical(old_json)
                new = None if new_json is None else v.parse_canonical(new_json)
                fields = dict(card=CARD_FIELDS, legacy_fts=LEGACY_FIELDS, source=('path', 'observation_id'),
                              card_source=CARD_SOURCE_FIELDS)[rel]
                for value in (old, new):
                    if value is not None:
                        v.exact(value, fields)
                dict(card=deltas, legacy_fts=legacy_deltas, source=source_deltas,
                     card_source=card_source_deltas)[rel].append((new, old))
            generation = dict(gen_id=vals.gen_id(uuid.uuid4().hex, v.digest('memvane-row-rollback-v1', dict(
                                  view_id=handle.view_id, head=head.gen_id, prior=prior['gen_id']))),
                              view_id=handle.view_id, epoch=head.epoch + 1, prior_ordinal=head.ordinal,
                              kind='rollback', sidecar_present=prior['sidecar_present'],
                              capture_json=prior['capture_json'], policy_digest=prior['policy_digest'],
                              skip_digest=prior['skip_digest'], recipe_id=prior['recipe_id'],
                              profile_id=prior['profile_id'], coverage=prior['coverage'],
                              source_digest=prior['source_digest'], legacy_json=None)

            def before_commit(db):
                job = db.execute("SELECT job_id FROM s_stage_jobs WHERE view_id=? AND state<>'abandon'",
                                 (handle.view_id,)).fetchone()
                if job is not None:
                    store_module._stage_mark_abandon(db, job[0])

            written = self._rows_write(db, head, generation, deltas, None, before_commit, deadline,
                                       rollback=(current, prior), legacy_deltas=legacy_deltas,
                                       source_deltas=source_deltas, card_source_deltas=card_source_deltas)
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
            for table in ('s_card_sources', 's_sources', 's_legacy_fts', 's_cards'):
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

    # -- conversion from the generation format -------------------------------

    def _rows_convert(self, *, deadline, parity=None):
        """Convert this generation-format store to the row format in one transaction.

        Every view keeps its head and, when present, the published generation
        the head was built on, with their ids and ordinals. Both are verified
        with the generation-format verifier first; the new head seals, the
        schema manifest and every head proof are checked before COMMIT. A
        failure before COMMIT leaves the old format. The caller holds the owner
        and writer proof, the host gate and the operator's approval.
        """
        import re
        import laya_blob_schema as schema
        from laya_blob_store import _insert
        _deadline_value(deadline)
        self._require_write()
        if self._rows:
            v.fail('row_format_already')
        with self._session(write=True) as db:
            if db.execute('SELECT COUNT(*) FROM delete_receipts').fetchone()[0]:
                v.fail('row_conversion_unsupported')
            if db.execute('SELECT COUNT(*) FROM fill_jobs WHERE cursor<target_count').fetchone()[0]:
                v.fail('row_conversion_fill_pending')
            heads = [dict(r) for r in db.execute('SELECT view_id,gen_id,epoch FROM view_heads ORDER BY view_id')]
            plans = []
            for head in heads:
                _expired(deadline)
                pending = db.execute(
                    "SELECT COUNT(*) FROM generations g WHERE g.view_id=? AND g.state<>'published' "
                    'AND g.ordinal>(SELECT ordinal FROM generations WHERE gen_id=?)',
                    (head['view_id'], head['gen_id'])).fetchone()[0]
                if pending:
                    v.fail('row_conversion_stage_pending')
                chain = []
                for gen_id in (head['gen_id'], None):
                    if gen_id is None:
                        gen_id = chain[0]['generation']['parent_gen']
                        if gen_id is None:
                            break
                    generation = db.execute('SELECT * FROM generations WHERE gen_id=?', (gen_id,)).fetchone()
                    if generation is None or generation['state'] != 'published':
                        v.fail('row_conversion_unsupported')
                    handle, entry, saved = self._sealed_handle(db, generation)
                    stage = self._verify_generation(db, handle, entry, saved, generation, deadline=deadline,
                                                    full_vectors=True)
                    seal = dict(db.execute('SELECT * FROM generation_seals WHERE gen_id=?', (gen_id,)).fetchone())
                    stored = self._stored(db, gen_id)
                    members = [_card_row(m) for m in stored[0]]
                    source_rows, source_objects = self._rows_source_objects(stored[1])
                    legacy_rows = []
                    if 'legacy' in stage:
                        # Legacy line spans move into card metadata.
                        members, legacy_rows = self._rows_convert_legacy(db, gen_id, stage['legacy'], members)
                        card_rows, card_objects = [], []
                    else:
                        card_rows, card_objects = self._rows_card_source_objects(stored[2])
                    parent = (None if generation['parent_gen'] is None else db.execute(
                        'SELECT ordinal FROM generations WHERE gen_id=?', (generation['parent_gen'],)).fetchone()[0])
                    chain.append(dict(generation=dict(generation), stage=stage, seal=seal, members=members,
                                      legacy_rows=legacy_rows, parent_ordinal=parent, sources=source_rows,
                                      source_objects=source_objects, card_sources=card_rows,
                                      card_objects=card_objects))
                if len(chain) == 2 and chain[0]['stage']['base'] != dict(
                        view_id=head['view_id'], gen_id=chain[1]['generation']['gen_id'],
                        epoch=head['epoch'] - 1, incarnation=self._incarnation):
                    v.fail('row_conversion_unsupported')
                plans.append(dict(head=head, chain=chain))
            anchor = None
            if parity is not None:
                # Only a published generation of a converted view, proved by the
                # generation-format verifier in this transaction, is anchored.
                v.exact(parity, ('gen_id', 'receipt_digest'))
                v.identifier(parity['receipt_digest'])
                if not isinstance(parity['gen_id'], str) or not vals.GEN_ID.fullmatch(parity['gen_id']):
                    v.fail('row_conversion_parity_invalid')
                anchored = db.execute("SELECT * FROM generations WHERE gen_id=? AND state='published'",
                                      (parity['gen_id'],)).fetchone()
                if anchored is None or anchored['view_id'] not in {plan['head']['view_id'] for plan in plans}:
                    v.fail('row_conversion_parity_invalid')
                handle, entry, saved = self._sealed_handle(db, anchored)
                self._verify_generation(db, handle, entry, saved, anchored, deadline=deadline)
                seal = dict(db.execute('SELECT * FROM generation_seals WHERE gen_id=?', (parity['gen_id'],)).fetchone())
                anchor = dict(view_id=anchored['view_id'], gen_id=parity['gen_id'], ordinal=anchored['ordinal'],
                              incarnation=self._incarnation, receipt_digest=parity['receipt_digest'],
                              proof_digest=v.digest('memvane-row-parity-anchor-v1', dict(
                                  generation=dict(anchored), seal=seal, receipt=parity['receipt_digest'])))
            meta = dict(db.execute('SELECT * FROM store_meta WHERE singleton=1').fetchone())
            sequence = db.execute('SELECT MAX(ordinal) FROM generation_slots').fetchone()[0] or 0
            old_fts = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                                    "AND sql LIKE 'CREATE VIRTUAL TABLE%' AND name GLOB ?",
                                                    (fts.PREFIX + '*',))]
            with self._fts_guard.converting():
                for name in old_fts:
                    fts.table(int(name[len(fts.PREFIX):].replace('side_', '')),
                              sidecar=name.startswith(fts.PREFIX + 'side_'))
                    db.execute(f'DROP TABLE {name}')
                for table in ('member_vectors', 'card_sources', 'wiki_members', 'source_members',
                              'generation_seals', 'delete_receipts', 'view_heads', 'generations',
                              'generation_slots', 'store_meta'):
                    db.execute(f'DROP TABLE {table}')
                existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'")}
                triggers = []
                for ddl in schema.statements(schema.ROW_SCHEMA_VERSION):
                    match = re.match(r'\s*CREATE\s+(?:UNIQUE\s+)?(TABLE|INDEX|TRIGGER)\s+(\w+)', ddl)
                    if match is None:
                        v.fail('row_conversion_unsupported')
                    if match.group(2) in existing:
                        continue
                    if match.group(1) == 'TRIGGER':
                        triggers.append(ddl)
                    else:
                        db.execute(ddl)
                _insert(db, 'store_meta', dict(meta, format=schema.ROW_FORMAT,
                                               schema_version=schema.ROW_SCHEMA_VERSION,
                                               schema_digest=schema.expected_manifest(schema.ROW_SCHEMA_VERSION)))
                for plan in plans:
                    self._rows_convert_view(db, plan, deadline)
                if anchor is not None:
                    _insert(db, 's_parity_anchor', anchor)
                db.execute("INSERT INTO sqlite_sequence(name,seq) SELECT 's_generations',0 "
                           "WHERE NOT EXISTS(SELECT 1 FROM sqlite_sequence WHERE name='s_generations')")
                db.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name='s_generations'", (sequence,))
                self._fts_guard.freeze_head()
                for ddl in triggers:
                    db.execute(ddl)
                db.execute(f'PRAGMA user_version={schema.ROW_SCHEMA_VERSION}')
                if schema.manifest(db, schema.ROW_SCHEMA_VERSION) != schema.expected_manifest(schema.ROW_SCHEMA_VERSION):
                    v.fail('candidate_schema_mismatch')
                result = {}
                for plan in plans:
                    view_id = plan['head']['view_id']
                    head = self._rows_head_row(db, view_id)
                    handle = self._sealed_handle(db, dict(checkout_evidence=plan['chain'][0]['generation']['checkout_evidence'],
                                                          view_id=view_id))[0]
                    # The verifier recomputes the head's physical and logical FTS digests.
                    self._rows_verify(db, head, handle, deadline)
                    result[view_id] = dict(gen_id=head.gen_id, epoch=head.epoch, ordinal=head.ordinal,
                                           prior=len(plan['chain']) == 2)
                _expired(deadline)
                db.commit()
        self.close()
        return result

    def _rows_convert_view(self, db, plan, deadline):
        """One view's head (and its base) as row-format generations, cards, deltas and head FTS."""
        view_id, epoch = plan['head']['view_id'], plan['head']['epoch']
        chain = list(reversed(plan['chain']))  # base first, head last
        prior_ordinal = None
        for index, item in enumerate(chain):
            generation, stage = item['generation'], item['stage']
            rows = sorted(item['members'], key=lambda row: row['path'].encode('utf8'))
            legacy = 'legacy' in stage
            last = index == len(chain) - 1
            if legacy:
                base_rows, side_rows, positions = self._rows_legacy_fts(item['legacy_rows'], cache={},
                                                                        deadline=deadline)
                text_acc = self._rows_legacy_fts_acc(base_rows, side_rows, positions)
                members = member_acc(rows, item['legacy_rows'], item['sources'])
                if last:
                    fts.create_head(db, view_id)
                    fts.populate_head(db, view_id, base_rows)
                    fts.map_head(db, view_id)
                    if side_rows is not None:
                        fts.create_head(db, view_id, sidecar=True)
                        fts.populate_head(db, view_id, side_rows, sidecar=True)
                        fts.map_head(db, view_id, sidecar=True)
            else:
                fts_rows = self._rows_fts_rows(rows, cache={}, deadline=deadline)
                text_acc, side_rows = fts_acc(fts_rows), None
                members = member_acc(rows, (), item['sources'], item['card_sources'])
                if last:
                    fts.create_head(db, view_id)
                    fts.populate_head(db, view_id, fts_rows)
                    fts.map_head(db, view_id)
            row = dict(ordinal=generation['ordinal'], gen_id=generation['gen_id'], view_id=view_id,
                       epoch=epoch - (len(chain) - 1 - index),
                       prior_ordinal=prior_ordinal if index else item['parent_ordinal'],
                       kind='legacy' if legacy else 'normal', sidecar_present=int(side_rows is not None),
                       capture_json=generation['checkout_evidence'], policy_digest=generation['policy_digest'],
                       skip_digest=v.digest('memvane-row-skip-v1', stage['skip_paths']),
                       **_coverage_fields(stage['vector_spec']),
                       member_digest=acc_digest('member', members),
                       source_digest=v.digest('memvane-row-sources-v1', []),
                       logical_fts_digest=acc_digest('fts', text_acc),
                       physical_fts_digest=item['seal']['fts_storage_digest'],
                       member_acc=v.canonical(members), fts_acc=v.canonical(text_acc),
                       legacy_json=v.canonical(_legacy_observation(stage['legacy'])) if legacy else None)
            item['row'] = row
            prior_ordinal = generation['ordinal']
        # The head's physical digest is sealed after every FTS write is flushed.
        db.execute('SAVEPOINT s_convert_flush')
        db.execute('RELEASE s_convert_flush')
        if chain[-1]['row']['kind'] == 'legacy':
            chain[-1]['row']['physical_fts_digest'] = legacy_physical(
                fts.head_storage_digest(db, view_id, deadline),
                fts.head_storage_digest(db, view_id, deadline, sidecar=True)
                if chain[-1]['row']['sidecar_present'] else None)
        else:
            chain[-1]['row']['physical_fts_digest'] = fts.head_storage_digest(db, view_id, deadline)
        for item in chain:
            row = item['row']
            row['seal_digest'] = seal_digest(_seal_fields(self._incarnation, row))
            columns = tuple(row)
            db.execute(f"INSERT INTO s_generations({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                       tuple(row[k] for k in columns))
        head_ordinal = chain[-1]['row']['ordinal']
        # Source intervals: kept across both generations when unchanged, else closed and reopened.
        for item in chain:
            self._rows_put_source_objects(db, item['source_objects'])
        base_sources = {row['path']: row for row in chain[0]['sources']} if len(chain) == 2 else {}
        top_sources = {row['path']: row for row in chain[-1]['sources']}
        for path in _sorted_paths(set(base_sources) | set(top_sources)):
            old, new = base_sources.get(path), top_sources.get(path)
            spans = []
            if old is not None and old == new:
                spans.append((old, chain[0]['row']['ordinal'], None))
            else:
                if old is not None:
                    spans.append((old, chain[0]['row']['ordinal'], head_ordinal))
                if new is not None:
                    spans.append((new, head_ordinal, None))
                if len(chain) == 2:
                    key = v.canonical([path])
                    db.execute('INSERT INTO s_deltas(view_id,ordinal,rel,key_json,old_json,new_json) '
                               'VALUES (?,?,?,?,?,?)', (view_id, head_ordinal, 'source', key,
                                                         None if old is None else v.canonical(old),
                                                         None if new is None else v.canonical(new)))
                    for side, value in (('old', old), ('new', new)):
                        if value is not None:
                            db.execute('INSERT INTO s_delta_observations(view_id,ordinal,rel,key_json,side,'
                                       'observation_id) VALUES (?,?,?,?,?,?)',
                                       (view_id, head_ordinal, 'source', key, side, value['observation_id']))
            for value, start, end in spans:
                db.execute('INSERT INTO s_sources(view_id,path,gen_from,gen_to,observation_id) VALUES (?,?,?,?,?)',
                           (view_id, path, start, end, value['observation_id']))
        # Card source intervals: the same rule, keyed by card and source path.
        for item in chain:
            self._rows_put_source_objects(db, item['card_objects'])
        base_cards = ({_card_source_key(row): row for row in chain[0]['card_sources']}
                      if len(chain) == 2 else {})
        top_cards = {_card_source_key(row): row for row in chain[-1]['card_sources']}
        for key in sorted(set(base_cards) | set(top_cards)):
            _expired(deadline)
            old, new = base_cards.get(key), top_cards.get(key)
            spans = []
            if old is not None and old == new:
                spans.append((old, chain[0]['row']['ordinal'], None))
            else:
                if old is not None:
                    spans.append((old, chain[0]['row']['ordinal'], head_ordinal))
                if new is not None:
                    spans.append((new, head_ordinal, None))
                if len(chain) == 2:
                    row = old if old is not None else new
                    delta_key = v.canonical([row['card_path'], row['source_path']])
                    db.execute('INSERT INTO s_deltas(view_id,ordinal,rel,key_json,old_json,new_json) '
                               'VALUES (?,?,?,?,?,?)', (view_id, head_ordinal, 'card_source', delta_key,
                                                         None if old is None else v.canonical(old),
                                                         None if new is None else v.canonical(new)))
                    for side, value in (('old', old), ('new', new)):
                        if value is not None:
                            db.execute('INSERT INTO s_delta_observations(view_id,ordinal,rel,key_json,side,'
                                       'observation_id) VALUES (?,?,?,?,?,?)',
                                       (view_id, head_ordinal, 'card_source', delta_key, side,
                                        value['observation_id']))
            for value, start, end in spans:
                db.execute('INSERT INTO s_card_sources(view_id,card_path,source_path,gen_from,gen_to,observation_id,'
                           'spans_json) VALUES (?,?,?,?,?,?,?)', (view_id, value['card_path'], value['source_path'],
                                                                  start, end, value['observation_id'],
                                                                  value['spans_json']))
        base = {row['path']: row for row in chain[0]['members']} if len(chain) == 2 else {}
        top = {row['path']: row for row in chain[-1]['members']}
        for path in _sorted_paths(set(base) | set(top)):
            _expired(deadline)
            old, new = base.get(path), top.get(path)
            spans = []
            if old is not None and old == new:
                spans.append((old, chain[0]['row']['ordinal'], None))
            else:
                if old is not None:
                    spans.append((old, chain[0]['row']['ordinal'], head_ordinal))
                if new is not None:
                    spans.append((new, head_ordinal, None))
                if len(chain) == 2:
                    key = v.canonical([path])
                    db.execute('INSERT INTO s_deltas(view_id,ordinal,rel,key_json,old_json,new_json) '
                               'VALUES (?,?,?,?,?,?)', (view_id, head_ordinal, 'card', key,
                                                         None if old is None else v.canonical(old),
                                                         None if new is None else v.canonical(new)))
                    for side, value in (('old', old), ('new', new)):
                        if value is not None:
                            db.execute('INSERT INTO s_delta_blobs(view_id,ordinal,rel,key_json,side,blob_sha) '
                                       'VALUES (?,?,?,?,?,?)', (view_id, head_ordinal, 'card', key, side,
                                                                 value['blob_sha']))
            for value, start, end in spans:
                db.execute('INSERT INTO s_cards(view_id,path,gen_from,gen_to,blob_sha,title,eligible,'
                           'trust_digest,metadata_json) VALUES (?,?,?,?,?,?,?,?,?)',
                           (view_id, path, start, end, value['blob_sha'], value['title'], value['eligible'],
                            value['trust_digest'], value['metadata_json']))
        if any(item['legacy_rows'] for item in chain):
            closed = len(chain) == 2 and not chain[-1]['legacy_rows']
            if len(chain) == 2 and not closed and chain[0]['legacy_rows'] != chain[-1]['legacy_rows']:
                v.fail('row_conversion_unsupported')
            for value in chain[0]['legacy_rows']:
                db.execute('INSERT INTO s_legacy_fts(view_id,origin,path_key,gen_from,gen_to,blob_sha,title,'
                           'position,active) VALUES (?,?,?,?,?,?,?,?,?)',
                           (view_id, value['origin'], value['path_key'], chain[0]['row']['ordinal'],
                            head_ordinal if closed else None, value['blob_sha'], value['title'],
                            value['position'], value['active']))
                if closed:
                    # The ordinary head closed the legacy base corpus: its rollback delta.
                    key = v.canonical([value['origin'], value['path_key']])
                    db.execute('INSERT INTO s_deltas(view_id,ordinal,rel,key_json,old_json,new_json) '
                               'VALUES (?,?,?,?,?,NULL)', (view_id, head_ordinal, 'legacy_fts', key,
                                                           v.canonical(value)))
                    db.execute('INSERT INTO s_delta_blobs(view_id,ordinal,rel,key_json,side,blob_sha) '
                               'VALUES (?,?,?,?,?,?)', (view_id, head_ordinal, 'legacy_fts', key, 'old',
                                                         value['blob_sha']))
        db.execute('INSERT INTO s_heads(view_id,ordinal,epoch) VALUES (?,?,?)', (view_id, head_ordinal, epoch))

    def _rows_convert_legacy(self, db, gen_id, legacy, members):
        """Card metadata (FTS path, origin, line spans) and corpus rows of one legacy generation."""
        sidecar = 'sidecar_corpus' in legacy
        spans = {}
        for row in db.execute('SELECT wiki_path,spans_json FROM card_sources WHERE gen_id=? ORDER BY wiki_path,ordinal',
                              (gen_id,)):
            spans.setdefault(row['wiki_path'], []).extend(v.parse_canonical(row['spans_json']))
        cards = []
        for member in members:
            origin = legacy['fts_sources'][member['path']] if sidecar else 'base'
            meta = dict(fts_path=legacy['fts_paths'][member['path']], origin=origin,
                        spans=spans.get(member['path'], []))
            cards.append(dict(member, metadata_json=v.canonical(meta)))
        rows = [dict(origin='base', path_key=row['path'], blob_sha=row['hash'], title=None, position=None,
                     active=int(legacy['active_fts']['base'].get(row['path']) == row['hash']))
                for row in legacy['corpus']]
        if sidecar:
            rows.extend(dict(origin='sidecar', path_key=row['path'], blob_sha=row['hash'], title=row['title'],
                             position=row['rowid'],
                             active=int(legacy['active_fts']['sidecar'].get(row['path']) == row['hash']))
                        for row in legacy['sidecar_corpus'])
        rows.sort(key=lambda row: (row['origin'], row['path_key'].encode('utf8')))
        return cards, rows

    # -- reads ---------------------------------------------------------------

    def _rows_verify(self, db, head, live_handle, deadline):
        """Recompute the head seal from its stored rows and FTS bytes."""
        _expired(deadline)
        generation = db.execute('SELECT * FROM s_generations WHERE view_id=? AND ordinal=?',
                                (live_handle.view_id, head.ordinal)).fetchone()
        if generation is None or generation['gen_id'] != head.gen_id or generation['epoch'] != head.epoch:
            v.fail('view_head_invalid')
        generation = dict(generation)
        if generation['coverage'] not in ('lexical', 'complete'):
            v.fail('row_format_unsupported')
        handle, entry, saved = self._sealed_handle(
            db, dict(checkout_evidence=generation['capture_json'], view_id=generation['view_id']), live_handle)
        if generation['policy_digest'] != self._policy:
            v.fail('generation_binding_mismatch')
        rows = self._rows_open(db, generation['view_id'], generation['ordinal'])
        legacy_rows = self._rows_open_legacy(db, generation['view_id'], generation['ordinal'])
        legacy = self._rows_legacy_content(generation, legacy_rows)
        members = member_acc(rows, legacy_rows if legacy else (),
                             self._rows_open_sources(db, generation['view_id'], generation['ordinal']),
                             self._rows_open_card_sources(db, generation['view_id'], generation['ordinal']))
        if (v.canonical(members) != generation['member_acc']
                or acc_digest('member', members) != generation['member_digest']):
            v.fail('generation_rows_mismatch')
        if generation['sidecar_present'] != int(any(row['origin'] == 'sidecar' for row in legacy_rows)):
            v.fail('generation_rows_mismatch')
        if generation['coverage'] == 'complete':
            self._rows_coverage(db, rows, generation, full=False, deadline=deadline)
        cache, checked = {}, {'blobs': set(), 'worktrees': {}, 'captures': set(), 'fingerprints': {}}
        for row in rows:
            _expired(deadline)
            if row['eligible']:
                if row['trust_digest'] is None:
                    v.fail('card_trust_mismatch')
                if row['blob_sha'] not in cache:
                    # One admitted read per body: the attestation check reuses it.
                    cache[row['blob_sha']] = self._blob(row['blob_sha'], cache=checked)
                    checked['blobs'].add(row['blob_sha'])
                spec = self._attestation(row['trust_digest'], cache=checked)
                if spec['blob_sha'] != row['blob_sha'] or spec['policy_digest'] != handle.policy_digest:
                    v.fail('card_trust_mismatch')
        # A rollback generation defers the physical digest to the next publication.
        deferred = None
        if generation['kind'] == 'rollback':
            prior = db.execute('SELECT physical_fts_digest FROM s_generations WHERE view_id=? AND ordinal=?',
                               (generation['view_id'], generation['prior_ordinal'])).fetchone()
            if prior is None or generation['physical_fts_digest'] != deferred_physical(prior[0], generation['gen_id']):
                v.fail('fts_storage_mismatch')
            deferred = True
        if legacy:
            base_rows, side_rows, positions = self._rows_legacy_fts(legacy_rows, cache=cache, deadline=deadline)
            _, base_physical = fts.head_proof(db, generation['view_id'], base_rows, deadline, logical=False)
            side_physical = None
            if side_rows is not None:
                _, side_physical = fts.head_proof(db, generation['view_id'], side_rows, deadline, sidecar=True,
                                                  logical=False)
            text_acc = self._rows_legacy_fts_acc(base_rows, side_rows, positions)
            physical = legacy_physical(base_physical, side_physical)
            maps = [False, True] if side_rows is not None else [False]
        else:
            fts_rows = self._rows_fts_rows(rows, cache=cache, deadline=deadline)
            _, physical = fts.head_proof(db, generation['view_id'], fts_rows, deadline, logical=False)
            text_acc = fts_acc(fts_rows)
            maps = [False]
        if not deferred and physical != generation['physical_fts_digest']:
            v.fail('fts_storage_mismatch')
        counts = ({False: len(base_rows), True: len(side_rows or ())} if legacy
                  else {False: len(fts_rows), True: 0})
        for sidecar in maps:
            if not fts.head_map_matches(db, generation['view_id'], counts[sidecar], sidecar=sidecar):
                v.fail('fts_head_map_mismatch')
        if not maps[-1] and db.execute("SELECT COUNT(*) FROM s_head_fts_map WHERE view_id=? AND origin='sidecar'",
                                       (generation['view_id'],)).fetchone()[0]:
            v.fail('fts_head_map_mismatch')
        logical = acc_digest('fts', text_acc)
        if v.canonical(text_acc) != generation['fts_acc']:
            v.fail('generation_seal_mismatch')
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
                                     entry=entry, seal=_pin_seal(generation),
                                     vector=None if generation['coverage'] != 'complete' else dict(
                                         recipe_id=generation['recipe_id'], profile_id=generation['profile_id'],
                                         dimension=self._profile(generation['profile_id'])['dimension']),
                                     verified=record['verified'], ordinal=head.ordinal)
        return pin

    def _rows_search(self, pin, fts_query, limit, *, path_prefixes, excluded_paths, deadline_seconds,
                     legacy_origin, observed):
        import sqlite3
        from laya_view_reader import _query, _remaining
        if legacy_origin is not None and legacy_origin not in ('base', 'sidecar'):
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
            if _pin_seal(generation) != record['seal']:
                v.fail('view_pin_changed')
            legacy_rows = self._rows_open_legacy(db, pin.view_id, head.ordinal)
            legacy_answer = None
            if self._rows_legacy_content(generation, legacy_rows):
                answer = self._rows_legacy_search(db, pin, generation, legacy_rows, fts_query, limit,
                                                  path_prefixes, excluded_paths, legacy_origin, deadline)
                state = self._snapshot_state(entry['capture'], v.parse_canonical(generation['capture_json']),
                                             deadline)
                legacy_answer = [dict(row, snapshot_state=state) for row in answer]
            if legacy_answer is None and legacy_origin is not None:
                v.fail('migration_lexical_origin_invalid')
            if legacy_answer is not None:
                answer = legacy_answer
            else:
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

    def _rows_legacy_search(self, db, pin, generation, legacy_rows, fts_query, limit, path_prefixes,
                            excluded_paths, legacy_origin, deadline):
        """The generation format's legacy lexical search over the view's head FTS corpora."""
        import sqlite3
        from laya_view_reader import _remaining
        paired = bool(generation['sidecar_present'])
        if paired and legacy_origin is None:
            v.fail('migration_lexical_sidecar_unproved')
        if not paired and legacy_origin is not None:
            v.fail('migration_lexical_origin_invalid')
        origin = legacy_origin if paired else 'base'
        name = fts.head_table(pin.view_id, sidecar=origin == 'sidecar')
        cards = {row['path']: row for row in self._rows_open(db, pin.view_id, generation['ordinal'])}
        mapping = {}
        for path, row in cards.items():
            meta = v.parse_canonical(row['metadata_json'])
            if not paired or meta.get('origin') == origin:
                mapping[path] = meta['fts_path']
        # The same JSON object the generation format bound: active FTS path -> blob.
        active = {row['path_key']: row['blob_sha'] for row in legacy_rows if row['origin'] == origin and row['active']}
        args = [fts_query, v.canonical(active)]
        path_expr = f"substr({name}.path_key,instr({name}.path_key,'/')+1)"
        conditions = []
        if path_prefixes is not None:
            if not path_prefixes:
                conditions.append('0')
            else:
                conditions.append('(' + ' OR '.join(f'({path_expr}=? OR substr({path_expr},1,?)=?)'
                                                    for _ in path_prefixes) + ')')
                for path in path_prefixes:
                    args.extend((path, len(path) + 1, path + '/'))
        if excluded_paths:
            conditions.append(path_expr + ' NOT IN (' + ','.join('?' for _ in excluded_paths) + ')')
            args.extend(excluded_paths)
        extra = ''.join(' AND ' + clause for clause in conditions)
        args.append(limit)
        try:
            ranked = db.execute(f'''SELECT {name}.path_key AS full_path,
                bm25({name},1.5,4.0,1.0,0.0) AS score FROM {name}
                WHERE {name} MATCH ? AND {name}.path_key IN (SELECT key FROM json_each(?)) {extra}
                ORDER BY score ASC,{name}.path_key COLLATE BINARY ASC LIMIT ?''', args).fetchall()
        except sqlite3.Error:
            _remaining(deadline)
            v.fail('fts_query_invalid')
        relative = {}
        for path, full in mapping.items():
            relative.setdefault(full, []).append(path)
        window = [(path, row['score']) for row in ranked for path in sorted(
            relative.get(row['full_path'], ()), key=lambda p: p.encode('utf8'))]
        answer = []
        for path, score in window:
            card = cards.get(path)
            if card is None or not card['eligible']:
                continue
            answer.append(dict(path=path, blob_sha=card['blob_sha'], body=self._blob(card['blob_sha']),
                               display_title=card['title'], score=score, view_id=pin.view_id, gen_id=pin.gen_id,
                               evidence='commitment-only', owner_verified=False,
                               source_locations=_locators(v.parse_canonical(card['metadata_json']).get('spans', []))))
        return answer

    def _rows_card_locations(self, pin, paths, deadline_seconds):
        """Line locators of pinned cards from their row metadata; no source file is opened."""
        if (not isinstance(paths, (list, tuple)) or len(paths) > 100
                or any(type(path) is not str or not path for path in paths)):
            v.fail('card_locations_invalid')
        deadline = time.monotonic() + deadline_seconds
        record = self._pinned(pin)
        if not paths:
            return {}
        with self._reading(deadline) as db:
            head = self._rows_head_row(db, pin.view_id)
            if (head.ordinal, head.gen_id, head.epoch) != (record['ordinal'], pin.gen_id, pin.epoch):
                v.fail('view_head_changed')
            wanted = list(dict.fromkeys(paths))
            chosen = set(wanted)
            # Legacy imports keep line spans in card metadata; ordinary cards in card source rows.
            spans = {}
            for row in self._rows_open_card_sources(db, pin.view_id, head.ordinal, card_paths=wanted):
                spans.setdefault(row['card_path'], []).extend(self._rows_card_source_spans(row))
            result = {}
            for row in self._rows_open(db, pin.view_id, head.ordinal):
                if row['path'] not in chosen:
                    continue
                found = v.parse_canonical(row['metadata_json']).get('spans', []) + spans.get(row['path'], [])
                if found:
                    result[row['path']] = _locators(found)
            return result

    # -- vector coverage, fill cursor and exact search ------------------------

    def _rows_coverage(self, db, cards, spec, *, full, deadline=None):
        """Every eligible card's blob has the spec's complete set; ``full`` re-validates each set."""
        if spec is None:
            return []
        self._recipe(spec['recipe_id']); self._profile(spec['profile_id'])
        heads = {}
        for row in cards:
            _expired(deadline)
            if not row['eligible'] or row['blob_sha'] in heads:
                continue
            head = self._vector_heads(db, row['blob_sha'], spec['recipe_id'], spec['profile_id'])
            if full and len(self._vectors(row['blob_sha'], spec['recipe_id'], spec['profile_id'])) != head['chunk_count']:
                v.fail('embedding_set_invalid')
            heads[row['blob_sha']] = head
        return heads

    def _rows_fill_plan(self, handle, recipe_id, profile_id):
        """Eligible head cards in path order and the head/spec-bound cursor of this view."""
        from laya_view_fill import JOB_SCHEMA
        import laya_fill_schema as fill_schema
        entry = self._check(handle)
        v.identifier(recipe_id); v.identifier(profile_id)
        saved = self._observe(entry)
        with self._session() as db:
            self._recipe(recipe_id); self._profile(profile_id)
            head = self._rows_head_row(db, handle.view_id)
            if head.ordinal is None:
                v.fail('view_preparing')
            targets = []
            for row in self._rows_open(db, handle.view_id, head.ordinal):
                if row['eligible']:
                    size = db.execute('SELECT byte_length FROM wiki_blobs WHERE blob_sha=?',
                                      (row['blob_sha'],)).fetchone()[0]
                    targets.append(dict(path=row['path'], blob_sha=row['blob_sha'], byte_length=size))
            if len(targets) > fill_schema.MAX_TARGETS:
                v.fail('fill_targets_too_large')
            identity = dict(schema=JOB_SCHEMA + ':rows', incarnation=self._incarnation, view_id=handle.view_id,
                            head_ordinal=head.ordinal, gen_id=head.gen_id, recipe_id=recipe_id,
                            profile_id=profile_id, targets=v.digest('memvane-fill-targets-v1', targets))
            job = dict(job_id=v.digest(JOB_SCHEMA, identity), head_ordinal=head.ordinal,
                       target_count=len(targets))
            row = db.execute('SELECT job_id,head_ordinal,target_count,cursor FROM s_fill_cursors '
                             'WHERE view_id=? AND recipe_id=? AND profile_id=?',
                             (handle.view_id, recipe_id, profile_id)).fetchone()
            stored = row[3] if row is not None and tuple(row[:3]) == (job['job_id'], head.ordinal, len(targets)) else None
            # A finished pass is complete only while every target's set is present;
            # otherwise the next pass restarts from the first target.
            job['restart'] = stored == len(targets) and any(
                db.execute('SELECT 1 FROM embedding_sets WHERE blob_sha=? AND recipe_id=? AND profile_id=?',
                           (target['blob_sha'], recipe_id, profile_id)).fetchone() is None for target in targets)
        return entry, saved, job, targets, stored

    def _rows_fill_status(self, handle, recipe_id, profile_id):
        _, _, job, _, stored = self._rows_fill_plan(handle, recipe_id, profile_id)
        cursor = 0 if stored is None or job['restart'] else stored
        return dict(job_id=job['job_id'], cursor=cursor,
                    complete=stored is not None and not job['restart'] and cursor == job['target_count'])

    def _rows_fill_view(self, handle, recipe_id, profile_id, *, chunker, embedder, write_fence, expected_cursor,
                        max_files, max_bytes, deadline_seconds):
        """The generation format's bounded fill loop over the head's eligible cards.

        The cursor moves forward in its own transaction only after every
        covered target's set committed; a new head or spec starts over.
        """
        import laya_fill_schema as fill_schema
        import laya_view_fill as fills
        import laya_view_store as views
        from laya_blob_fill import ContentFiller
        self._require_write()
        deadline = fills._deadline(deadline_seconds)
        fills._bounded(expected_cursor, 0, fill_schema.MAX_TARGETS, 'fill_cursor_invalid')
        fills._bounded(max_files, 1, fills.MAX_FILES, 'fill_limit_invalid')
        fills._bounded(max_bytes, 1, fills.MAX_BYTES, 'fill_limit_invalid')
        if not all(callable(fn) for fn in (chunker, embedder, write_fence)):
            v.fail('fill_callback_invalid')
        self._fill_idle()
        entry, saved, job, targets, stored = self._rows_fill_plan(handle, recipe_id, profile_id)
        cursor = 0 if stored is None or job['restart'] else stored
        if expected_cursor != cursor:
            v.fail('fill_cursor_conflict')
        fills._remaining(deadline)
        status = lambda new: dict(job_id=job['job_id'], cursor=new, complete=new == job['target_count'])
        if stored is not None and not job['restart'] and cursor == job['target_count']:
            return dict(status(cursor), status='complete', filled=0, cache_hits=0, deferred=False)
        fenced = []

        def fence():
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
                deferred = steps == 0
                break
            try:
                result = filler.ensure_content(target['blob_sha'], recipe_id, profile_id, chunker=chunker,
                                               embedder=embedder, write_fence=fence,
                                               deadline_seconds=fills._remaining(deadline))
            except v.BlobStoreError as exc:
                if exc.reason != fills.BUSY:
                    raise
                deferred = True
                break
            if result['status'] == 'filled':
                filled += 1
                self._observe(entry)
            else:
                hits += 1
            fills._remaining(deadline)
            steps += 1
            used += target['byte_length']
        if steps == 0 and not (stored is None and not targets):
            return dict(status(cursor), status='deferred', filled=filled, cache_hits=hits, deferred=deferred)
        new = cursor + steps
        fills._remaining(deadline)
        self._check(entry['handle'])
        fence()
        with self._session(write=True) as db:
            again = db.execute('SELECT job_id,head_ordinal,target_count,cursor FROM s_fill_cursors '
                               'WHERE view_id=? AND recipe_id=? AND profile_id=?',
                               (handle.view_id, recipe_id, profile_id)).fetchone()
            current = (again[3] if again is not None and tuple(again[:3]) == (
                job['job_id'], job['head_ordinal'], job['target_count']) else None)
            if current != stored or self._rows_head_row(db, handle.view_id).ordinal != job['head_ordinal']:
                v.fail('fill_cursor_conflict')
            for target in targets[cursor:cursor + steps]:
                if db.execute('SELECT 1 FROM embedding_sets WHERE blob_sha=? AND recipe_id=? AND profile_id=?',
                              (target['blob_sha'], recipe_id, profile_id)).fetchone() is None:
                    v.fail('fill_content_missing')
            if new == job['target_count'] and any(
                    db.execute('SELECT 1 FROM embedding_sets WHERE blob_sha=? AND recipe_id=? AND profile_id=?',
                               (t['blob_sha'], recipe_id, profile_id)).fetchone() is None for t in targets):
                new = 0  # a set of an earlier target vanished: the next pass starts over
            db.execute('INSERT INTO s_fill_cursors(view_id,recipe_id,profile_id,job_id,head_ordinal,target_count,cursor) '
                       'VALUES (?,?,?,?,?,?,?) ON CONFLICT(view_id,recipe_id,profile_id) DO UPDATE SET '
                       'job_id=excluded.job_id,head_ordinal=excluded.head_ordinal,'
                       'target_count=excluded.target_count,cursor=excluded.cursor',
                       (handle.view_id, recipe_id, profile_id, job['job_id'], job['head_ordinal'],
                        job['target_count'], new))
            views._live(db, saved, {})
            views._current_capture(saved, self._repository, self._policy, entry['capture'])
            fills._remaining(deadline)
        done = new == job['target_count']
        # A pass that ends with a vanished earlier set reports progress from the first target again.
        return dict(status(new), status='complete' if done else 'progress', filled=filled, cache_hits=hits,
                    deferred=deferred)

    def _rows_search_exact(self, pin, query_vector, limit, *, profile_id, max_float_work, deadline_seconds,
                           path_prefixes, excluded_paths):
        """Exact max-cosine over the pinned head's eligible cards and the sealed spec's sets."""
        import laya_view_vectors as vectors
        deadline = time.monotonic() + vectors.options(limit, max_float_work, deadline_seconds,
                                                      path_prefixes, excluded_paths)
        v.identifier(profile_id)
        query = vectors.query_values(query_vector)
        record = self._pinned(pin)
        entry = record['entry']
        vectors._left(deadline)
        self._observe(entry, deadline=deadline, timeout_reason=vectors.DEADLINE_EXCEEDED)
        vectors._left(deadline)
        with self._reading(deadline) as db:
            head = self._rows_head_row(db, pin.view_id)
            if (head.ordinal, head.gen_id, head.epoch) != (record['ordinal'], pin.gen_id, pin.epoch):
                v.fail('view_head_changed')
            generation, original = self._rows_verified(db, head, entry['handle'], deadline, record)
            if _pin_seal(generation) != record['seal']:
                v.fail('view_pin_changed')
            if generation['coverage'] != 'complete':
                v.fail('vector_coverage_absent')
            if profile_id != generation['profile_id']:
                v.fail('vector_profile_mismatch')
            recipe_id = generation['recipe_id']
            dimension = self._profile(profile_id)['dimension']
            if len(query) != dimension:
                v.fail('vector_query_invalid')
            cards = [row for row in self._rows_open(db, pin.view_id, head.ordinal) if row['eligible']
                     and _path_allowed(row['path'], path_prefixes, excluded_paths)]
            heads = self._rows_coverage(db, cards, generation, full=False, deadline=deadline)
            if sum(heads[row['blob_sha']]['chunk_count'] for row in cards) * dimension > max_float_work:
                v.fail(vectors.WORK_EXCEEDED)
            groups = {}
            for row in cards:
                groups.setdefault(row['blob_sha'], []).append(row)
            top = []
            for blob, members in groups.items():
                vectors._left(deadline)
                rows = self._vectors(blob, recipe_id, profile_id)
                if len(rows) != heads[blob]['chunk_count']:
                    v.fail('vector_rows_mismatch')
                score = vectors._set_score(query, rows, dimension, deadline)
                rows = None
                for row in members:
                    vectors._keep(top, score, dict(path=row['path'], blob_sha=blob, display_title=row['title']), limit)
            state = self._snapshot_state(entry['capture'], v.parse_canonical(generation['capture_json']), deadline)
            answer = [dict(path=ref['path'], blob_sha=ref['blob_sha'], body=self._blob(ref['blob_sha']),
                           display_title=ref['display_title'], score=-negative, view_id=pin.view_id,
                           gen_id=pin.gen_id, evidence='commitment-only', owner_verified=False,
                           snapshot_state=state) for negative, _, _, ref in top]
            vectors._left(deadline)
        self._observe(entry, deadline=deadline, timeout_reason=vectors.DEADLINE_EXCEEDED)
        vectors._left(deadline)
        return answer

    # -- generation-format shaped reads for the shadow migration -------------

    def _rows_read_pinned(self, db, pin, deadline):
        """``(generation, seal, capture, stage)`` of a pinned row head in the generation format's shape.

        ``stage`` carries ``vector_spec`` and, for a legacy head, the ``legacy``
        mapping rebuilt from card metadata, legacy corpus rows and ``legacy_json``.
        """
        record = self._pinned(pin)
        head = self._rows_head_row(db, pin.view_id)
        if (head.ordinal, head.gen_id, head.epoch) != (record['ordinal'], pin.gen_id, pin.epoch):
            v.fail('view_head_changed')
        generation, capture = self._rows_verified(db, head, record['entry']['handle'], deadline, record)
        stage = dict(vector_spec=None if generation['coverage'] != 'complete' else dict(
            recipe_id=generation['recipe_id'], profile_id=generation['profile_id']))
        legacy = self._rows_legacy_mapping(db, generation)
        if legacy is not None:
            stage['legacy'] = legacy
        shaped = dict(state='published', ordinal=generation['ordinal'], gen_id=generation['gen_id'],
                      view_id=generation['view_id'])
        return shaped, _pin_seal_value(generation), capture, stage

    def _rows_legacy_mapping(self, db, generation):
        legacy_rows = self._rows_open_legacy(db, generation['view_id'], generation['ordinal'])
        if not self._rows_legacy_content(generation, legacy_rows):
            return None
        cards = self._rows_open(db, generation['view_id'], generation['ordinal'])
        meta = {row['path']: v.parse_canonical(row['metadata_json']) for row in cards}
        observed = (dict.fromkeys(LEGACY_OBSERVATION_FIELDS) if generation['legacy_json'] is None
                    else v.parse_canonical(generation['legacy_json']))
        legacy = dict(observed, paths=[row['path'] for row in cards],
                      fts_paths={path: value['fts_path'] for path, value in meta.items()},
                      corpus=[dict(path=row['path_key'], hash=row['blob_sha'])
                              for row in legacy_rows if row['origin'] == 'base'],
                      active_fts={origin: {row['path_key']: row['blob_sha'] for row in legacy_rows
                                           if row['origin'] == origin and row['active']}
                                  for origin in ('base', 'sidecar')})
        if generation['sidecar_present']:
            legacy['fts_sources'] = {path: value['origin'] for path, value in meta.items()}
            legacy['sidecar_corpus'] = [dict(path=row['path_key'], hash=row['blob_sha'], title=row['title'],
                                             rowid=row['position'])
                                        for row in legacy_rows if row['origin'] == 'sidecar']
        return legacy

    def _pin_fts_table(self, pin, *, sidecar=False):
        """The FTS table a pinned head searches: per-generation (format 3) or the view head (format 4)."""
        record = self._pinned(pin)
        if self._rows:
            return fts.head_table(pin.view_id, sidecar=sidecar)
        return fts.table(record['verified']['generation']['ordinal'], sidecar=sidecar)

    def _generation_rows_at(self, db, gen_id):
        row = db.execute('SELECT view_id,ordinal FROM s_generations WHERE gen_id=?', (gen_id,)).fetchone()
        if row is None:
            v.fail('generation_missing')
        return row[0], row[1]

    @contextmanager
    def _joined_session(self):
        """The caller's open read transaction, or a new short session when there is none."""
        if self._db.in_transaction:
            yield self._db
        else:
            with self._session() as db:
                yield db

    def _eligible_members(self, gen_id, *, after=None, limit=None):
        """``[(path, blob_sha)]`` of a generation's eligible cards in path order, either format."""
        with self._joined_session() as db:
            if not self._rows:
                sql = 'SELECT path,blob_sha FROM wiki_members WHERE gen_id=? AND eligible=1'
                args = [gen_id]
            else:
                view_id, ordinal = self._generation_rows_at(db, gen_id)
                sql = ('SELECT path,blob_sha FROM s_cards WHERE view_id=? AND eligible=1 AND gen_from<=? '
                       'AND (gen_to IS NULL OR gen_to>?)')
                args = [view_id, ordinal, ordinal]
            if after is not None:
                sql += ' AND path>?'
                args.append(after)
            sql += ' ORDER BY path'
            if limit is not None:
                sql += ' LIMIT ?'
                args.append(limit)
            return [(row[0], row[1]) for row in db.execute(sql, args).fetchall()]

    def _eligible_count(self, gen_id):
        with self._joined_session() as db:
            if not self._rows:
                return db.execute('SELECT COUNT(*) FROM wiki_members WHERE gen_id=? AND eligible=1',
                                  (gen_id,)).fetchone()[0]
            view_id, ordinal = self._generation_rows_at(db, gen_id)
            return db.execute('SELECT COUNT(*) FROM s_cards WHERE view_id=? AND eligible=1 AND gen_from<=? '
                              'AND (gen_to IS NULL OR gen_to>?)', (view_id, ordinal, ordinal)).fetchone()[0]

    def _eligible_member_after(self, gen_id, after):
        """``(path, blob_sha, title)`` of the first eligible card after ``after`` in binary order, or None."""
        with self._joined_session() as db:
            if not self._rows:
                return db.execute('SELECT path,blob_sha,display_title FROM wiki_members '
                                  'WHERE gen_id=? AND eligible=1 AND path>? ORDER BY path COLLATE BINARY LIMIT 1',
                                  (gen_id, after)).fetchone()
            view_id, ordinal = self._generation_rows_at(db, gen_id)
            row = db.execute('SELECT path,blob_sha,title FROM s_cards WHERE view_id=? AND eligible=1 AND gen_from<=? '
                             'AND (gen_to IS NULL OR gen_to>?) AND path>? ORDER BY path COLLATE BINARY LIMIT 1',
                             (view_id, ordinal, ordinal, after)).fetchone()
            return None if row is None else tuple(row)

    def _prepared_stage(self, gen_id, handle):
        """The base head (and legacy marker) of a prepared or published generation, either format."""
        with self._joined_session() as db:
            if not self._rows:
                row = db.execute('SELECT s.stage_json FROM generations g JOIN generation_slots s '
                                 'ON s.ordinal=g.ordinal WHERE g.gen_id=?', (gen_id,)).fetchone()
                if row is None:
                    v.fail('generation_missing')
                return vals.check_stage(v.parse_canonical(row['stage_json']), handle, self._incarnation)
            published = db.execute('SELECT g.view_id,g.epoch,p.gen_id FROM s_generations g LEFT JOIN s_generations p '
                                   'ON p.view_id=g.view_id AND p.ordinal=g.prior_ordinal WHERE g.gen_id=?',
                                   (gen_id,)).fetchone()
            if published is not None:
                base = dict(view_id=published[0], gen_id=published[2], epoch=published[1] - 1,
                            incarnation=self._incarnation)
            else:
                job = db.execute("SELECT base_gen_id,base_epoch FROM s_stage_jobs WHERE view_id=? AND job_id=? "
                                 "AND state='ready'", (handle.view_id, gen_id[2:34])).fetchone()
                if job is None:
                    v.fail('generation_missing')
                base = dict(view_id=handle.view_id, gen_id=job[0], epoch=job[1], incarnation=self._incarnation)
            # Row stages keep only their scan digest; the legacy publication re-derives and compares it.
            return dict(base=base, legacy=dict(observation=None))

    def _rows_preverify(self, handle, deadline):
        self._check(handle)
        with self._reading(deadline) as db:
            head = self._rows_head_row(db, handle.view_id)
            if head.gen_id is None:
                return None
            self._rows_verify(db, head, handle, deadline)
        return head.gen_id

    # -- legacy import publication -------------------------------------------

    def _rows_publish_legacy(self, handle, gen_id, entry, base, legacy_snapshot, deadline):
        """Publish a staged legacy import after re-deriving its corpus from the re-observed v6 snapshot.

        The fresh observation, sidecar commitment and source revision are
        proved on a pinned snapshot first; the corpus and members are then
        re-derived from that observation and must reproduce the stage's scan
        digest. Only the cut identity and the commit run under the shared
        publication cut, as in the generation format.
        """
        from contextlib import contextmanager
        import laya_reader_open as readers
        import laya_shadow_migration as migration
        import laya_view_store as store_module
        from laya_view_store import _CUT_SECONDS, _live, _skip_patterns
        v.exact(legacy_snapshot, (('snapshot',) if 'snapshot' in legacy_snapshot else ()) +
                ('endpoint', 'decision', 'provenance_scan') +
                (('source_revision',) if 'source_revision' in legacy_snapshot else ()))
        endpoint, decision = legacy_snapshot['endpoint'], legacy_snapshot['decision']
        if deadline is None:
            deadline = time.monotonic() + migration._STEP_SECONDS
        saved = self._observe(entry, deadline=deadline)
        skips = _skip_patterns(saved)
        with self._session() as db:
            head = self._rows_head_row(db, handle.view_id)
            published = db.execute('SELECT view_id,epoch FROM s_generations WHERE gen_id=?', (gen_id,)).fetchone()
            if published is not None:
                if (tuple(published) == (handle.view_id, base['epoch'] + 1) and head.gen_id == gen_id
                        and head.epoch == base['epoch'] + 1):
                    return self._rows_head(db, handle)  # exact replay of this publication only
                v.fail('view_head_conflict')
            if (head.gen_id, head.epoch) != (base['gen_id'], base['epoch']):
                v.fail('view_head_conflict')
        with readers.inspect_warm_snapshot(endpoint, deadline=deadline) as fresh:
            observed = migration.capture_effective(fresh, endpoint=endpoint, decision=decision, after=None,
                                                   deadline=deadline)
            if ('source_revision' in legacy_snapshot and
                    migration._source_revision(fresh) != legacy_snapshot['source_revision']):
                v.fail('migration_source_revision_unproved')
            pinned = migration.publication_cut_identity(fresh, endpoint=endpoint, decision=decision,
                                                        deadline=deadline)
        manifest, legacy = self._legacy_evidence(
            handle, None, dict(observation=observed['observation'], endpoint=endpoint, decision=decision,
                               provenance_scan=legacy_snapshot['provenance_scan']), None, True, deadline)
        legacy_rows = _legacy_corpus_rows(legacy)
        with self._session() as db:
            plan = self._derive(handle, manifest, skips, deadline=deadline, legacy_paths=legacy['paths'])
            spans = {}
            for card in plan['cards']:
                spans.setdefault(card['wiki_path'], []).extend(card['spans'])
            wanted = {row['path']: row for row in _legacy_card_rows(legacy, plan['members'], spans)}
            current = {row['path']: row for row in self._rows_open(db, handle.view_id)}
            job = db.execute('SELECT job_id,incarnation,base_gen_id,base_epoch,capture_json,policy_digest,'
                             'scan_digest,state FROM s_stage_jobs WHERE view_id=?', (handle.view_id,)).fetchone()
        if job is None or job[7] != 'ready' or gen_id[2:34] != job[0]:
            v.fail('generation_missing')
        items = [_stage_item(path, wanted.get(path)) for path in _sorted_paths(set(current) | set(wanted))
                 if current.get(path) != wanted.get(path)]
        final = [wanted[path] for path in _sorted_paths(wanted)]
        scan_digest = _legacy_scan(manifest, skips, legacy, legacy_rows)
        stage = _stage_value(self._incarnation, handle, saved, base, job[0], scan_digest, skips, final, items)
        if (tuple(job[1:7]) != (self._incarnation, base['gen_id'], base['epoch'], v.canonical(entry['capture']),
                                handle.policy_digest, scan_digest)
                or vals.gen_id(job[0], v.digest(ROW_STAGE_SCHEMA, stage)) != gen_id):
            v.fail('migration_snapshot_changed')
        base_rows, side_rows, positions = self._rows_legacy_fts(legacy_rows, cache={}, deadline=deadline)

        @contextmanager
        def publication_cut():
            cut_deadline = min(deadline, time.monotonic() + _CUT_SECONDS)
            with readers.reader_publish_cut(endpoint, deadline=cut_deadline):
                if migration.current_cut_identity(endpoint, decision=decision, deadline=cut_deadline) != pinned:
                    v.fail('migration_snapshot_changed')
                yield cut_deadline

        def commit(db):
            if time.monotonic() >= deadline:
                v.fail('reader_snapshot_deadline')
            with publication_cut() as cut_deadline:
                if time.monotonic() >= cut_deadline:
                    v.fail('reader_snapshot_deadline')
                db.commit()

        remaining = max(0, int(min(deadline - time.monotonic(), _CUT_SECONDS) * 1000))
        if not remaining:
            v.fail('reader_snapshot_deadline')
        previous_timeout = self._db.execute('PRAGMA busy_timeout').fetchone()[0]
        self._db.execute('PRAGMA busy_timeout=' + str(remaining))
        try:
            with self._session(write=True) as db:
                head = self._rows_head_row(db, handle.view_id)
                if (head.gen_id, head.epoch) != (base['gen_id'], base['epoch']):
                    v.fail('view_head_conflict')
                again = db.execute('SELECT job_id,scan_digest,state FROM s_stage_jobs WHERE view_id=?',
                                   (handle.view_id,)).fetchone()
                stored = [dict(zip(vals.STAGE_ITEM_FIELDS, tuple(row))) for row in db.execute(
                    'SELECT path,action,input_sha,blob_sha,trust_digest,metadata_json FROM s_stage_items '
                    'WHERE job_id=? ORDER BY path', (job[0],))]
                if again is None or tuple(again) != (job[0], scan_digest, 'ready') or stored != items:
                    v.fail('migration_snapshot_changed')
                _live(db, saved, {})
                before = {row['path']: row for row in self._rows_open(db, handle.view_id)}
                deltas = [(before.get(item['path']), _item_row(item)) for item in items]
                old_legacy = ({} if head.ordinal is None else
                              {(r['origin'], r['path_key']): r
                               for r in self._rows_open_legacy(db, handle.view_id, head.ordinal)})
                new_legacy = {(r['origin'], r['path_key']): r for r in legacy_rows}
                legacy_deltas = [(old_legacy.get(key), new_legacy.get(key))
                                 for key in sorted(set(old_legacy) | set(new_legacy),
                                                   key=lambda k: (k[0], k[1].encode('utf8')))
                                 if old_legacy.get(key) != new_legacy.get(key)]

                def before_commit(db):
                    store_module._stage_mark_abandon(db, job[0])
                    store_module._current_capture(saved, self._repository, self._policy, entry['capture'],
                                                  deadline=deadline)
                    _live(db, saved, {})

                generation = dict(gen_id=gen_id, view_id=handle.view_id, epoch=base['epoch'] + 1,
                                  prior_ordinal=head.ordinal, kind='legacy',
                                  sidecar_present=int(side_rows is not None),
                                  capture_json=v.canonical(entry['capture']), policy_digest=handle.policy_digest,
                                  skip_digest=stage['skip_digest'], **_coverage_fields(None),
                                  expected_member_digest=member_digest(final, legacy_rows),
                                  legacy_json=v.canonical(_legacy_observation(legacy)),
                                  source_digest=v.digest('memvane-row-sources-v1', []))
                source_deltas = [(row, None) for row in self._rows_open_sources(db, handle.view_id)]
                card_source_deltas = [(row, None) for row in self._rows_open_card_sources(db, handle.view_id)]
                written = self._rows_write(db, head, generation, deltas, None, before_commit, deadline,
                                           legacy_deltas=legacy_deltas, source_deltas=source_deltas,
                                           card_source_deltas=card_source_deltas,
                                           legacy_final=(final, legacy_rows, base_rows, side_rows, positions),
                                           commit=commit)
        finally:
            self._db.execute('PRAGMA busy_timeout=' + str(previous_timeout))
        return dict(view_id=handle.view_id, gen_id=written['gen_id'], epoch=written['epoch'],
                    incarnation=self._incarnation)


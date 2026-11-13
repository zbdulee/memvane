"""Row-format generations: per-view card intervals, one head FTS, rollback deltas.

A publication writes only the changed card rows, the matching head FTS rows,
one sealed generation row and its deltas, then moves the head by CAS, all in
one SQLite transaction. The seal is a structural commitment over the stored
rows and FTS bytes, never compiler truth, source observation or operational
authority. Generations include lexical and vector coverage and source relations.
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


_CARD_DOMAIN = 'memvane-row-card-v1'
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
    return _acc_checked(v.parse_canonical(text))


def _acc_checked(acc):
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


_SOURCE_DOMAIN = 'memvane-row-source-v1'


def _source_value(row):
    return dict(path=row['path'], observation_id=row['observation_id'])


_CARD_SOURCE_DOMAIN = 'memvane-row-card-source-v1'
CARD_SOURCE_FIELDS = ('card_path', 'source_path', 'observation_id', 'spans_json')


def _card_source_value(row):
    return {k: row[k] for k in CARD_SOURCE_FIELDS}


def _card_source_key(row):
    return (row['card_path'].encode('utf8'), row['source_path'].encode('utf8'))


_SOURCE_DOMAINS = (_SOURCE_DOMAIN, _CARD_SOURCE_DOMAIN)
_ACC_KEYS = ('bytes', 'count', 'sum')


def member_acc(cards, sources=(), card_sources=()):
    """Accumulate card rows and their source membership."""
    acc = EMPTY_ACC
    for row in cards:
        acc = acc_update(acc, _CARD_DOMAIN, _card_value(row))
    source = EMPTY_ACC
    for row in sources:
        source = acc_update(source, _SOURCE_DOMAIN, _source_value(row))
    for row in card_sources:
        source = acc_update(source, _CARD_SOURCE_DOMAIN, _card_source_value(row))
    return dict(acc, sources=source)


def members_update(members, domain, row, sign=1):
    """Add or remove one row in the accumulator its domain belongs to."""
    if domain in _SOURCE_DOMAINS:
        return dict(members, sources=acc_update(members['sources'], domain, row, sign))
    return dict(acc_update({k: members[k] for k in _ACC_KEYS}, domain, row, sign), sources=members['sources'])


def members_value(text):
    members = v.parse_canonical(text)
    v.exact(members, _ACC_KEYS + ('sources',))
    _acc_checked({k: members[k] for k in _ACC_KEYS})
    _acc_checked(members['sources'])
    return members


def members_digest(members):
    """The card side's ``member_digest``."""
    return acc_digest('member', {k: members[k] for k in _ACC_KEYS})


def members_source_digest(members):
    """``source_digest``: the source membership accumulator under its own domain."""
    return acc_digest('source', members['sources'])


def member_digest(cards):
    return members_digest(member_acc(cards))


def source_digest(sources=(), card_sources=()):
    """Source rows and card source rows as one multiset commitment."""
    return members_source_digest(member_acc((), sources, card_sources))


def fts_acc(rows, origin='normal', positions=None):
    acc = EMPTY_ACC
    for index, row in enumerate(rows):
        value = dict(row) if positions is None else dict(row, position=positions[index])
        acc = acc_update(acc, _FTS_DOMAINS[origin], value)
    return acc


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
                 member_digest=member_digest(final), source_digest=source_digest(sources, card_sources),
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

        ``cards`` are attestation refs with their ordinals. Refs of one card to
        the same source path fold into one row: the observation of the lowest
        ordinal and every ref's spans in ordinal order. One path can carry two
        observations when a writer card's ``sourceRevisions`` cite it at two
        shas; the generation format keeps both as ordinals, and its locators
        come from the spans alone, so the folded row reads the same.

        The row seals only what it stores. The other observations stay bound
        through the card row's trust digest (the attestation names every
        source fingerprint), and a pin re-derives the accumulator from the
        stored rows, so they are not separate accumulator contributions.
        """
        grouped, objects = {}, []
        for card in sorted(cards, key=lambda c: (c['wiki_path'].encode('utf8'), c['ordinal'])):
            spec = self._fingerprint(card['fingerprint_id'], cache=cache)
            key = (card['wiki_path'], spec['path'])
            entry = grouped.get(key)
            if entry is None:
                content, observation = self._observation_objects(spec, card['fingerprint_id'])
                grouped[key] = dict(card_path=card['wiki_path'], source_path=spec['path'],
                                    observation_id=observation['observation_id'], spans=list(card['spans']))
                objects.append((content, observation))
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

    def _rows_prepare(self, handle, manifest, *, expected_head, vector_spec, deadline):
        from laya_view_store import _skip_patterns, _wiki_files, _live
        from laya_blob_store import _insert
        import laya_view_store as store_module
        _deadline_value(deadline)
        self._require_write()
        entry = self._check(handle)
        base = vals.expected_head(expected_head, handle.view_id, self._incarnation)
        import laya_view_vectors as vectors
        spec = vectors.spec(vector_spec)
        manifest = vals.manifest(manifest, handle.policy_digest)
        saved = self._observe(entry, deadline=deadline)
        skips = _skip_patterns(saved)
        with self._session() as db:
            if self._rows_head(db, handle) != base:
                v.fail('view_head_conflict')
            plan = self._derive(handle, manifest, skips, deadline=deadline)
            current = {row['path']: row for row in self._rows_open(db, handle.view_id)}
            source_rows, source_objects = self._rows_source_objects(plan['sources'])
            source_items = [dict(path=(old or new)['path'], action='delete' if new is None else 'put',
                                 observation_id=None if new is None else new['observation_id'])
                            for old, new in self._source_diff(self._rows_open_sources(db, handle.view_id),
                                                              source_rows)]
            card_rows, card_objects, card_items = self._rows_card_source_changes(
                db, handle.view_id, current, plan)
            # Zero or complete: every eligible member's set is validated before any write.
            self._rows_coverage(db, [_card_row(m) for m in plan['members']], spec, full=True, deadline=deadline)
        wanted = {member['path']: _card_row(member) for member in plan['members']}
        items = [_stage_item(path, wanted.get(path)) for path in _sorted_paths(set(current) | set(wanted))
                 if current.get(path) != wanted.get(path)]
        final = [wanted[path] for path in _sorted_paths(wanted)]
        capture_json = v.canonical(entry['capture'])
        files = _wiki_files(saved, plan['members'])
        job_id = uuid.uuid4().hex
        scan_digest = v.digest('memvane-row-stage-scan-v1', dict(
            manifest=vals.manifest_digest(manifest), skip_paths=skips,
            files=sorted([path, node] for path, node in files.items())))
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

    def _rows_publish(self, handle, gen_id, *, expected_head, deadline):
        from laya_view_store import _live, _skip_patterns, _wiki_files
        import laya_view_store as store_module
        _deadline_value(deadline)
        self._require_write()
        entry = self._check(handle)
        base = vals.expected_head(expected_head, handle.view_id, self._incarnation)
        if not isinstance(gen_id, str) or not vals.GEN_ID.fullmatch(gen_id):
            v.fail('generation_id_invalid')
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

            generation = dict(gen_id=gen_id, view_id=handle.view_id, epoch=base['epoch'] + 1,
                              prior_ordinal=head.ordinal, kind='normal', sidecar_present=0,
                              capture_json=v.canonical(entry['capture']), policy_digest=handle.policy_digest,
                              skip_digest=stage['skip_digest'], **_coverage_fields(spec),
                              expected_member_digest=stage['member_digest'], legacy_json=None,
                              expected_source_digest=stage['source_digest'])
            source_deltas = self._source_diff(self._rows_open_sources(db, handle.view_id), final_sources)
            card_source_deltas = self._card_source_diff(self._rows_open_card_sources(db, handle.view_id),
                                                        final_card_sources)
            written = self._rows_write(db, head, generation, deltas, rows, before_commit, deadline,
                                       source_deltas=source_deltas,
                                       card_source_deltas=card_source_deltas)
        return dict(view_id=handle.view_id, gen_id=written['gen_id'], epoch=written['epoch'],
                    incarnation=self._incarnation)

    def _rows_write(self, db, head, generation, deltas, rows, before_commit, deadline, *, rollback=None,
                    commit=None, source_deltas=(), card_source_deltas=()):
        """Changed card rows, their head FTS rows, the sealed generation and the head CAS.

        Runs inside the caller's write transaction and commits it under the
        view's FTS capability; any failure rolls the whole publication back.
        A publication proves the full head FTS, runs the FTS5 integrity check
        and seals the physical digest. A rollback (``rollback`` = the head and
        prior generation rows) updates the head accumulators from the delta
        only, must reach the prior's accumulators and seals the deferral value.
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
        changes = []
        text_changes = []  # (domain, fts row, sign) for the rollback accumulator
        for old, new in deltas:
            _expired(deadline)
            old_fts, new_fts = self._card_fts_row(old, deadline), self._card_fts_row(new, deadline)
            if old_fts != new_fts:
                changes.append(dict(old=old_fts, new=new_fts))
                text_changes += [('normal', row, sign) for row, sign in ((old_fts, -1), (new_fts, 1)) if row]
        with self._fts_guard.view_head(view_id):
            if head.ordinal is None:
                fts.create_head(db, view_id)
            fts.apply_mapped_head_changes(db, view_id, changes, deadline=deadline)
            if rollback is None:
                fts.verify_head_index(db, view_id)
                self._fts_guard.freeze_head()
                _, physical = fts.head_proof(db, view_id, rows, deadline, logical=False)
                members = member_acc(self._rows_open_after(db, view_id, deltas), final_sources,
                                     final_card_sources)
                text_acc = fts_acc(rows)
            else:
                current, prior = rollback
                members, text_acc = members_value(current['member_acc']), acc_value(current['fts_acc'])
                for domain, value, pairs in ((_CARD_DOMAIN, _card_value, deltas),
                                             (_SOURCE_DOMAIN, _source_value, source_deltas),
                                             (_CARD_SOURCE_DOMAIN, _card_source_value, card_source_deltas)):
                    for old, new in pairs:
                        if old is not None:
                            members = members_update(members, domain, value(old), -1)
                        if new is not None:
                            members = members_update(members, domain, value(new))
                for origin, row, sign in text_changes:
                    text_acc = acc_update(text_acc, _FTS_DOMAINS[origin], row, sign)
                if (members, text_acc) != (members_value(prior['member_acc']), acc_value(prior['fts_acc'])):
                    v.fail('rollback_fts_mismatch')
                physical = deferred_physical(current['physical_fts_digest'], generation['gen_id'])
            generation = dict(generation, ordinal=ordinal, member_acc=v.canonical(members),
                              fts_acc=v.canonical(text_acc), member_digest=members_digest(members),
                              source_digest=members_source_digest(members),
                              logical_fts_digest=acc_digest('fts', text_acc), physical_fts_digest=physical)
            if rollback is None and (generation['member_digest'] != generation.pop('expected_member_digest')
                                     or generation['source_digest'] != generation.pop('expected_source_digest')):
                v.fail('generation_rows_mismatch')
            generation['seal_digest'] = seal_digest(_seal_fields(self._incarnation, generation))
            columns = tuple(generation)
            db.execute(f"INSERT INTO s_generations({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                       tuple(generation[k] for k in columns))
            for old, new in deltas:
                _expired(deadline)
                store_module._apply_card_delta(db, view_id, ordinal, old, new)
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
        """The head FTS row of one eligible card."""
        if row is None or not row['eligible'] or 'fts_path' in v.parse_canonical(row['metadata_json']):
            return None
        return self._rows_fts_rows([row], deadline=deadline)[0]

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
            deltas, source_deltas, card_source_deltas = [], [], []
            for rel, old_json, new_json in db.execute(
                    'SELECT rel,old_json,new_json FROM s_deltas WHERE view_id=? AND ordinal=? '
                    'ORDER BY rel,key_json DESC', (handle.view_id, head.ordinal)).fetchall():
                if rel not in ('card', 'source', 'card_source'):
                    v.fail('row_format_unsupported')
                old = None if old_json is None else v.parse_canonical(old_json)
                new = None if new_json is None else v.parse_canonical(new_json)
                fields = dict(card=CARD_FIELDS, source=('path', 'observation_id'),
                              card_source=CARD_SOURCE_FIELDS)[rel]
                for value in (old, new):
                    if value is not None:
                        v.exact(value, fields)
                dict(card=deltas, source=source_deltas,
                     card_source=card_source_deltas)[rel].append((new, old))
            generation = dict(gen_id=vals.gen_id(uuid.uuid4().hex, v.digest('memvane-row-rollback-v1', dict(
                                  view_id=handle.view_id, head=head.gen_id, prior=prior['gen_id']))),
                              view_id=handle.view_id, epoch=head.epoch + 1, prior_ordinal=head.ordinal,
                              kind='rollback', sidecar_present=prior['sidecar_present'],
                              capture_json=prior['capture_json'], policy_digest=prior['policy_digest'],
                              skip_digest=prior['skip_digest'], recipe_id=prior['recipe_id'],
                              profile_id=prior['profile_id'], coverage=prior['coverage'], legacy_json=None)

            def before_commit(db):
                job = db.execute("SELECT job_id FROM s_stage_jobs WHERE view_id=? AND state<>'abandon'",
                                 (handle.view_id,)).fetchone()
                if job is not None:
                    store_module._stage_mark_abandon(db, job[0])

            written = self._rows_write(db, head, generation, deltas, None, before_commit, deadline,
                                       rollback=(current, prior),
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
        if generation['coverage'] not in ('lexical', 'complete'):
            v.fail('row_format_unsupported')
        handle, entry, saved = self._sealed_handle(
            db, dict(checkout_evidence=generation['capture_json'], view_id=generation['view_id']), live_handle)
        if generation['policy_digest'] != self._policy:
            v.fail('generation_binding_mismatch')
        rows = self._rows_open(db, generation['view_id'], generation['ordinal'])
        members = member_acc(rows,
                             self._rows_open_sources(db, generation['view_id'], generation['ordinal']),
                             self._rows_open_card_sources(db, generation['view_id'], generation['ordinal']))
        if (v.canonical(members) != generation['member_acc']
                or members_digest(members) != generation['member_digest']
                or members_source_digest(members) != generation['source_digest']):
            v.fail('generation_rows_mismatch')
        if generation['sidecar_present'] or generation['kind'] not in ('normal', 'rollback'):
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
        fts_rows = self._rows_fts_rows(rows, cache=cache, deadline=deadline)
        _, physical = fts.head_proof(db, generation['view_id'], fts_rows, deadline, logical=False)
        text_acc = fts_acc(fts_rows)
        if not deferred and physical != generation['physical_fts_digest']:
            v.fail('fts_storage_mismatch')
        if not fts.head_map_matches(db, generation['view_id'], len(fts_rows)):
            v.fail('fts_head_map_mismatch')
        if db.execute("SELECT COUNT(*) FROM s_head_fts_map WHERE view_id=? AND origin='sidecar'",
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
            if _pin_seal(generation) != record['seal']:
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

    # -- pinned generation reads ---------------------------------------------

    def _rows_read_pinned(self, db, pin, deadline):
        """Return generation, seal, capture and vector specification for a pinned head."""
        record = self._pinned(pin)
        head = self._rows_head_row(db, pin.view_id)
        if (head.ordinal, head.gen_id, head.epoch) != (record['ordinal'], pin.gen_id, pin.epoch):
            v.fail('view_head_changed')
        generation, capture = self._rows_verified(db, head, record['entry']['handle'], deadline, record)
        stage = dict(vector_spec=None if generation['coverage'] != 'complete' else dict(
            recipe_id=generation['recipe_id'], profile_id=generation['profile_id']))
        shaped = dict(state='published', ordinal=generation['ordinal'], gen_id=generation['gen_id'],
                      view_id=generation['view_id'])
        return shaped, _pin_seal_value(generation), capture, stage

    def _pin_fts_table(self, pin, *, sidecar=False):
        """The FTS table a pinned view head searches."""
        record = self._pinned(pin)
        return fts.head_table(pin.view_id, sidecar=sidecar)

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
            view_id, ordinal = self._generation_rows_at(db, gen_id)
            return db.execute('SELECT COUNT(*) FROM s_cards WHERE view_id=? AND eligible=1 AND gen_from<=? '
                              'AND (gen_to IS NULL OR gen_to>?)', (view_id, ordinal, ordinal)).fetchone()[0]

    def _eligible_member_after(self, gen_id, after):
        """``(path, blob_sha, title)`` of the first eligible card after ``after`` in binary order, or None."""
        with self._joined_session() as db:
            view_id, ordinal = self._generation_rows_at(db, gen_id)
            row = db.execute('SELECT path,blob_sha,title FROM s_cards WHERE view_id=? AND eligible=1 AND gen_from<=? '
                             'AND (gen_to IS NULL OR gen_to>?) AND path>? ORDER BY path COLLATE BINARY LIMIT 1',
                             (view_id, ordinal, ordinal, after)).fetchone()
            return None if row is None else tuple(row)


    def _rows_preverify(self, handle, deadline):
        self._check(handle)
        with self._reading(deadline) as db:
            head = self._rows_head_row(db, handle.view_id)
            if head.gen_id is None:
                return None
            self._rows_verify(db, head, handle, deadline)
        return head.gen_id


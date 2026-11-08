"""Inactive migration capture from an issued, pinned v6 reader snapshot."""
import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
import errno
import json
import math
import re
import sqlite3
import time

import laya_journal_reader as journal
import laya_reader_lease as leases
import laya_reader_open as readers
from laya_index_a import IndexContractError, _valid_relative_path
from laya_index_host_route import wiki_index_roots
from laya_blob_store import legacy_card_evidence
from laya_incremental_docs_query import MAX_DOCS, _exclusions
from source_text import is_wiki_meta_page

MAX_BATCH = 100
MAX_BATCH_BYTES = 8 * 1024 * 1024
# Provisional cooperative limit for the inactive shadow path; release still
# requires measured writer/reader impact and recovery proof.
#
# Step budget map (the structure is fixed here; the numbers wait for the
# measured writer/reader impact):
# - _STEP_SECONDS bounds every interval that holds something a NormalWriter,
#   reader or installer operation can wait on: each advance_shadow stage that
#   runs under the installer and operation locks (copy, verification, corpus,
#   publish around the private head CAS, the caught-up recheck, intent
#   writes), and each pinned v6 page snapshot with its physical reader lock
#   (the candidate build's per-page rechecks clamp their own snapshot too).
# - The publication cut is nested inside that step: ViewStore.publish_generation
#   clamps its shared cut and the fresh snapshot under it to 1.0 s on its own.
# - The candidate build (prepare_shadow_generation) holds neither lock nor one
#   snapshot across the build, so it runs on the caller's wall deadline,
#   capped per source pass by ViewStore.prepare_generation. Its final source
#   recheck reacquires both locks under a fresh _STEP_SECONDS deadline.
# - Recall parity pages (compare_recall_corpus) hold no writer lock; each page
#   is one pinned v6 snapshot under _STEP_SECONDS. Only the parity seal
#   quiesces writers: it holds the installer, operation and sync owner locks
#   (convert_reader_v5 order, without the provider lane) for at most
#   _STEP_SECONDS, never waits for them, and reports the held time.
# - The O(generation) published-head proof (laya_journal_reader
#   _published_snapshot) runs before each step outside both locks on the
#   wall deadline, interrupted at it (_prove_head_unlocked); its read
#   transaction ends with the proof and its idle connection closes after the
#   step. The step's snapshots reuse it while the base store's file identity
#   and head row hold (published_proof_reuse, shared across one driver pass).
# - A recall parity page that misses its step deadline is retried from the
#   same cursor at half the members (_shrink_parity_page), down to one.
_STEP_SECONDS = 3.0
# Writer-receipt catch-up (design 8.2-6) replays at most this many changed
# paths per step; more changes take the full path/SHA recapture instead.
MAX_CATCHUP_PATHS = 100


class _MigrationObservationChanged(IndexContractError):
    def __init__(self, observation):
        super().__init__('migration_snapshot_changed')
        self.observation = observation


def _wiki_card_path(path, prefixes):
    return any(path.startswith(prefix) and not is_wiki_meta_page(path[len(prefix):])
               for prefix in prefixes)


def _prefix_ranges(prefixes):
    """Ordered disjoint path ranges [prefix, next) that hold exactly the ``prefix`` paths."""
    ranges = []
    for prefix in sorted(set(prefixes)):
        if type(prefix) is not str or not prefix.endswith('/'):
            raise IndexContractError('migration_inventory_invalid')
        if not any(prefix.startswith(lower) for lower, _ in ranges):
            # '0' is the code point after '/', so the range is exactly the
            # paths starting with ``prefix`` under SQLite BINARY ordering.
            ranges.append((prefix, prefix[:-1] + '0'))
    return ranges


def _membership_rows(base, *, gen, scope, ranges, include_ineligible, after, limit):
    """At most ``limit`` wiki-root membership rows after ``after``, path-ordered."""
    columns = 'm.path,m.hash,m.eligible' if include_ineligible else 'm.path,m.hash'
    eligibility = '' if include_ineligible else 'AND m.eligible=1 '
    rows = []
    for lower, upper in ranges:
        if after is not None and after >= upper:
            continue
        rows.extend(base.execute('SELECT ' + columns + ' FROM membership m WHERE m.gen=? AND m.scope=? ' +
                                 eligibility + 'AND m.path>=? AND m.path<? AND m.path>? '
                                 'ORDER BY m.path LIMIT ?',
                                 (gen, scope, lower, upper, '' if after is None else after,
                                  limit - len(rows))).fetchall())
        if len(rows) >= limit:
            break
    return rows


def _sidecar_rows(side, *, after, limit):
    """At most ``limit`` sidecar docs after ``after``, path-ordered."""
    if after is None:
        return side.execute('SELECT path,hash FROM docs ORDER BY path LIMIT ?', (limit,)).fetchall()
    return side.execute('SELECT path,hash FROM docs WHERE path>? ORDER BY path LIMIT ?',
                        (after, limit)).fetchall()


def _path_stream(fetch, *, after, chunk, deadline, check, reason):
    """Yield strictly path-ordered rows after ``after``, fetching ``chunk`` at a time."""
    while True:
        rows = fetch(after, chunk)
        if time.monotonic() >= deadline:
            raise IndexContractError('reader_snapshot_deadline')
        for row in rows:
            if type(row[0]) is not str or (after is not None and not row[0] > after):
                raise IndexContractError(reason)
            check(row)
            after = row[0]
            yield row
        if len(rows) < chunk:
            return


def _membership_rows_at(base, *, gen, scope, paths):
    """Eligible membership rows of exactly ``paths``, path-ordered."""
    return base.execute('SELECT m.path,m.hash FROM membership m WHERE m.gen=? AND m.scope=? AND m.eligible=1 '
                        'AND m.path IN (SELECT value FROM json_each(?)) ORDER BY m.path',
                        (gen, scope, json.dumps(paths))).fetchall()


def _sidecar_rows_at(side, *, paths):
    """Sidecar docs of exactly ``paths``, path-ordered."""
    return side.execute('SELECT path,hash FROM docs WHERE path IN (SELECT value FROM json_each(?)) '
                        'ORDER BY path', (json.dumps(paths),)).fetchall()


def _target_stream(fetch, targets, *, chunk, deadline, check, reason):
    """Yield rows for sorted ``targets`` only, ``chunk`` target paths per read."""
    after = None
    for start in range(0, len(targets), chunk):
        part = targets[start:start + chunk]
        rows = fetch(part)
        if time.monotonic() >= deadline:
            raise IndexContractError('reader_snapshot_deadline')
        wanted = set(part)
        for row in rows:
            if (type(row[0]) is not str or row[0] not in wanted
                    or (after is not None and not row[0] > after)):
                raise IndexContractError(reason)
            check(row)
            after = row[0]
            yield row


def _merge_effective(base_rows, side_rows, *, prefixes, excluded_paths, excluded_hashes,
                     include_ineligible, limit):
    """Merge path-ordered base membership and sidecar docs into effective members.

    A sidecar doc replaces the base row of its path unless both carry the same
    digest and the base row is eligible. Rows are pulled only as needed, so a
    page of ``limit`` members reads at most ``limit + 1`` rows per source plus
    the meta-page and hidden rows it skips. Returns (members, more).
    """
    want = None if limit is None else limit + 1
    out, pending = [], object()
    b = s = pending
    while want is None or len(out) < want:
        if b is pending:
            b = next(base_rows, None)
        if s is pending:
            s = next(side_rows, None)
        if b is None and s is None:
            break
        if s is None or (b is not None and b[0] < s[0]):
            path, row, doc, b = b[0], b, None, pending
        elif b is None or s[0] < b[0]:
            path, row, doc, s = s[0], None, s, pending
        else:
            path, row, doc, b, s = b[0], b, s, pending, pending
        if not _wiki_card_path(path, prefixes):
            continue
        eligible = 1 if row is None or len(row) < 3 else row[2]
        if doc is None or (row is not None and row[1] == doc[1] and eligible == 1):
            digest, source = row[1], 'base'
        else:
            digest, source, eligible = doc[1], 'sidecar', 1
        hidden = path in excluded_paths or digest in excluded_hashes
        if hidden and not include_ineligible:
            continue
        member = {'path': path, 'hash': digest, 'source': source}
        if include_ineligible:
            member.update(eligible=eligible, excluded=hidden)
        out.append(member)
    if want is None:
        return out, False
    return out[:limit], len(out) > limit


def capture_effective(snapshot, *, endpoint, decision, after, deadline, include_ineligible=False,
                      _all_inventory=False, _only=None, _page_limit=None):
    """Capture one metadata page; this observation does not authorize a write."""
    if not any(item is snapshot and bound is endpoint and saved == decision
               for item, bound, saved in readers._observations.get()):
        raise IndexContractError('reader_snapshot_unpinned')
    readers._bound(endpoint)
    selection = leases._reader_selections.get(endpoint)
    if selection is None or selection.preferred_endpoint != endpoint.endpoint_id:
        raise IndexContractError('reader_selection_unproved')
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.monotonic() >= deadline):
        raise IndexContractError('reader_snapshot_deadline')
    if (type(include_ineligible) is not bool or type(_all_inventory) is not bool
            or (_all_inventory and (not include_ineligible or after is not None))
            or (_only is not None and (type(_only) is not frozenset or _all_inventory or include_ineligible
                                       or not all(type(path) is str for path in _only)))
            or (_page_limit is not None and (type(_page_limit) is not int or _page_limit < 1))):
        raise IndexContractError('migration_inventory_invalid')
    deadline = min(deadline, time.monotonic() + _STEP_SECONDS)
    with readers._validation_deadline(snapshot.connections, deadline):
        observation = hashlib.sha256(json.dumps({
            'endpoint': endpoint.endpoint_id, 'epoch': selection.epoch,
            'published': snapshot.published, 'exclusionEpoch': snapshot.generation_exclusion_epoch,
            'baseRevision': snapshot.base_revision, 'sidecar': snapshot.sidecar,
            'settings': endpoint.record['settings'], 'decision': decision,
            'inventory': include_ineligible,
        }, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode('ascii')).hexdigest()
        if after is not None:
            if (type(after) is not dict or set(after) != {'after', 'observation'}
                    or type(after['after']) is not str or not _valid_relative_path(after['after'])
                    or type(after['observation']) is not str):
                raise IndexContractError('migration_cursor_invalid')
            if after['observation'] != observation:
                raise IndexContractError('migration_snapshot_changed')
            after = after['after']
        base, _, _, side = snapshot.connections
        if side is None:
            if (endpoint.record.get('legacy_sidecar_baseline') is not None or
                    base.execute("SELECT 1 FROM schema_meta WHERE key='reader-sidecar-required:v1'").fetchone()):
                raise IndexContractError('reader_sidecar_revision_unproved')
        elif snapshot.sidecar.get('baseline') is not True:
            baseline = endpoint.record.get('legacy_sidecar_baseline')
            if baseline is None or dict(baseline) != snapshot.sidecar.get('legacyCommitment'):
                raise IndexContractError('reader_sidecar_revision_unproved')
        prefixes = tuple(name + '/' for name in wiki_index_roots(decision['collections']))
        ranges = _prefix_ranges(prefixes)
        scope, gen = decision['scopeId'], snapshot.published[0]
        excluded_paths, excluded_hashes = _exclusions(base, scope)
        def valid(path, digest):
            return (type(path) is str and _valid_relative_path(path)
                    and path.startswith(prefixes) and type(digest) is str
                    and re.fullmatch(r'[0-9a-f]{64}', digest) is not None)
        if after is None:
            # Every page walk starts here, and its observation binds the
            # immutable published membership and the sidecar revision, so the
            # inventory bound is proved once per walk. The count reads index
            # entries only; no member row is materialized.
            eligibility = '' if include_ineligible else 'AND m.eligible=1 '
            total = sum(base.execute('SELECT count(*) FROM (SELECT 1 FROM membership m WHERE m.gen=? '
                                     'AND m.scope=? ' + eligibility + 'AND m.path>=? AND m.path<? LIMIT ?)',
                                     (gen, scope, lower, upper, MAX_DOCS + 1)).fetchone()[0]
                        for lower, upper in ranges)
            if total > MAX_DOCS:
                raise IndexContractError('migration_membership_limit')
            if side is not None and side.execute('SELECT count(*) FROM (SELECT 1 FROM docs LIMIT ?)',
                                                 (MAX_DOCS + 1,)).fetchone()[0] > MAX_DOCS:
                raise IndexContractError('migration_sidecar_invalid')
        def check_base(row):
            if ((include_ineligible and (type(row[2]) is not int or row[2] not in (0, 1)))
                    or not valid(row[0], row[1])):
                raise IndexContractError('migration_membership_invalid')
        def check_side(row):
            if not valid(row[0], row[1]):
                raise IndexContractError('migration_sidecar_invalid')
        # One page reads at most size + 1 rows per source (the extra row
        # decides ``next``), plus the meta-page and hidden rows it skips.
        size = MAX_BATCH if _page_limit is None else min(_page_limit, MAX_BATCH)
        if _only is None:
            base_rows = _path_stream(
                lambda start, limit: _membership_rows(base, gen=gen, scope=scope, ranges=ranges,
                    include_ineligible=include_ineligible, after=start, limit=limit),
                after=after, chunk=size + 1, deadline=deadline, check=check_base,
                reason='migration_membership_invalid')
            side_rows = iter(()) if side is None else _path_stream(
                lambda start, limit: _sidecar_rows(side, after=start, limit=limit),
                after=after, chunk=size + 1, deadline=deadline, check=check_side,
                reason='migration_sidecar_invalid')
        else:
            # Catch-up pages read only the requested paths, in path order,
            # with the same merge, exclusion and page boundary rules.
            targets = sorted(path for path in _only if after is None or path > after)
            base_rows = _target_stream(
                lambda part: _membership_rows_at(base, gen=gen, scope=scope, paths=part),
                [path for path in targets if path.startswith(prefixes)], chunk=size + 1,
                deadline=deadline, check=check_base, reason='migration_membership_invalid')
            side_rows = iter(()) if side is None else _target_stream(
                lambda part: _sidecar_rows_at(side, paths=part), targets, chunk=size + 1,
                deadline=deadline, check=check_side, reason='migration_sidecar_invalid')
        members, more = _merge_effective(base_rows, side_rows, prefixes=prefixes,
            excluded_paths=excluded_paths, excluded_hashes=excluded_hashes,
            include_ineligible=include_ineligible, limit=None if _all_inventory else size)
        if time.monotonic() >= deadline:
            raise IndexContractError('reader_snapshot_deadline')
        return {'members': members, 'observation': observation,
                'next': {'after': members[-1]['path'], 'observation': observation} if more else None}


def capture_wiki_bytes(snapshot, *, endpoint, decision, after, deadline, _expected_observation=None,
                       _only=None, _page_limit=None):
    """Read proved wiki bytes only; returned rows alone do not authorize import."""
    from laya_blob_values import MAX_BODY
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.monotonic() >= deadline):
        raise IndexContractError('reader_snapshot_deadline')
    deadline = min(deadline, time.monotonic() + _STEP_SECONDS)
    if (_expected_observation is not None and
            (type(_expected_observation) is not str or re.fullmatch(r'[0-9a-f]{64}', _expected_observation) is None
             or (after is not None and (not isinstance(after, dict)
                                         or after.get('observation') != _expected_observation)))):
        raise IndexContractError('migration_cursor_invalid')
    try:
        page = capture_effective(snapshot, endpoint=endpoint, decision=decision,
                                 after=after, deadline=deadline, _only=_only,
                                 _page_limit=_page_limit)
    except IndexContractError as exc:
        if _expected_observation is None or str(exc) != 'migration_snapshot_changed':
            raise
        current = capture_effective(snapshot, endpoint=endpoint, decision=decision,
                                    after=None, deadline=deadline)
        if current['observation'] == _expected_observation:
            raise
        raise _MigrationObservationChanged(current['observation']) from exc
    if _expected_observation is not None and page['observation'] != _expected_observation:
        raise _MigrationObservationChanged(page['observation'])
    base, _, _, side = snapshot.connections
    result, used = [], 0
    pinned = None  # checks this pinned snapshot cannot change: once per page
    with readers._validation_deadline(snapshot.connections, deadline):
        for member in page['members']:
            if time.monotonic() >= deadline:
                raise IndexContractError('reader_snapshot_deadline')
            if member['source'] == 'base':
                row = base.execute('SELECT length(CAST(body AS BLOB)) FROM content WHERE hash=?',
                                   (member['hash'],)).fetchone()
                if row is None:
                    raise IndexContractError('migration_member_changed')
                size, body = row[0], None
            else:
                size_row = side.execute('SELECT length(CAST(body AS BLOB)) FROM docs WHERE path=?',
                                        (member['path'],)).fetchone()
                if size_row is None:
                    raise IndexContractError('migration_member_changed')
                size = size_row[0]
                body = None
            if type(size) is not int or not 0 <= size <= MAX_BODY:
                raise IndexContractError('migration_body_limit')
            if used + size > MAX_BATCH_BYTES:
                if not result:
                    raise IndexContractError('migration_body_limit')
                return {'members': result, 'observation': page['observation'],
                        'next': {'after': result[-1]['path'], 'observation': page['observation']}}
            if member['source'] == 'base':
                body_row = base.execute('SELECT body FROM content WHERE hash=?', (member['hash'],)).fetchone()
                if body_row is None:
                    raise IndexContractError('migration_member_changed')
                body = body_row[0]
                from laya_index_a import _qmd_title
                title = _qmd_title(body, member['path'])
            else:
                if pinned is None:
                    pinned = journal.sidecar_pinned_checks(side, base, decision)
                proved = journal._sidecar_document_snapshot(side, base, decision, member['path'],
                                                             deadline=deadline, require_vectors=False,
                                                             pinned=pinned)
                if proved['hash'] != member['hash']:
                    raise IndexContractError('migration_member_changed')
                body = proved['body']
                title = proved['title']
            if (type(body) is not str or len(body.encode('utf8')) != size
                    or hashlib.sha256(body.encode('utf8')).hexdigest() != member['hash']):
                raise IndexContractError('migration_member_changed')
            used += size
            result.append({**member, 'body': body, 'title': title})
    return {'members': result, 'observation': page['observation'], 'next': page['next']}


def sidecar_fts_commitment(snapshot, *, endpoint, decision, deadline):
    """Bind actual sidecar FTS storage to one already pinned snapshot."""
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.monotonic() >= deadline):
        raise IndexContractError('reader_snapshot_deadline')
    if not any(item is snapshot and bound is endpoint and saved == decision
               for item, bound, saved in readers._observations.get()):
        raise IndexContractError('reader_snapshot_unpinned')
    readers._bound(endpoint)
    selection = leases._reader_selections.get(endpoint)
    if selection is None or selection.preferred_endpoint != endpoint.endpoint_id:
        raise IndexContractError('reader_selection_unproved')
    side = snapshot.connections[3]
    if side is None or not isinstance(snapshot.sidecar, dict):
        raise IndexContractError('migration_sidecar_invalid')
    if snapshot.sidecar.get('baseline') is not True:
        saved = snapshot.sidecar.get('legacyCommitment')
        baseline = endpoint.record.get('legacy_sidecar_baseline')
        if baseline is None or dict(baseline) != saved:
            raise IndexContractError('reader_sidecar_revision_unproved')
        return dict(saved)
    with readers._validation_deadline(snapshot.connections, deadline):
        identity = journal._sidecar_identity(side, decision)
        return journal._legacy_sidecar_commitment(side, identity, deadline=deadline)


def capture_sidecar_fts_corpus(snapshot, *, endpoint, decision, after, deadline):
    """Capture all sidecar FTS rows, including rows hidden from results."""
    from laya_blob_values import MAX_BODY
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.monotonic() >= deadline):
        raise IndexContractError('reader_snapshot_deadline')
    deadline = min(deadline, time.monotonic() + _STEP_SECONDS)
    current = capture_effective(snapshot, endpoint=endpoint, decision=decision,
                                after=None, deadline=deadline)
    observation = hashlib.sha256(('memvane-sidecar-fts-corpus-v1:' +
                                  current['observation']).encode('ascii')).hexdigest()
    path_after = ''
    if after is not None:
        if (type(after) is not dict or set(after) != {'after', 'observation'}
                or type(after['after']) is not str or not _valid_relative_path(after['after'])
                or after['observation'] != observation):
            raise IndexContractError('migration_snapshot_changed')
        path_after = after['after']
    base, _, _, side = snapshot.connections
    if side is None:
        return {'members': [], 'observation': observation, 'next': None}
    prefixes = tuple(name + '/' for name in wiki_index_roots(decision['collections']))
    with readers._validation_deadline(snapshot.connections, deadline):
        rows = side.execute('SELECT path,length(CAST(body AS BLOB)),rowid FROM docs WHERE path>? '
                            'ORDER BY path COLLATE BINARY LIMIT ?',
                            (path_after, MAX_BATCH + 1)).fetchall()
        result, used = [], 0
        pinned = None  # checks this pinned snapshot cannot change: once per page
        for path, size, rowid in rows:
            if len(result) == MAX_BATCH:
                break
            if (type(path) is not str or not _valid_relative_path(path)
                    or not path.startswith(prefixes)):
                raise IndexContractError('migration_sidecar_invalid')
            if not _wiki_card_path(path, prefixes):
                raise IndexContractError('migration_meta_fts_corpus_unproved')
            if type(size) is not int or not 0 <= size <= MAX_BODY:
                raise IndexContractError('migration_body_limit')
            if used + size > MAX_BATCH_BYTES:
                break
            if pinned is None:
                pinned = journal.sidecar_pinned_checks(side, base, decision)
            document = journal._sidecar_document_snapshot(side, base, decision, path,
                deadline=deadline, require_vectors=False, _for_fts_corpus=True, pinned=pinned)
            if len(document['body'].encode('utf8')) != size:
                raise IndexContractError('migration_member_changed')
            if type(rowid) is not int:
                raise IndexContractError('migration_sidecar_invalid')
            result.append({**{key: document[key] for key in ('path', 'hash', 'title', 'body')},
                           'rowid': rowid})
            used += size
        if time.monotonic() >= deadline:
            raise IndexContractError('reader_snapshot_deadline')
        if rows and not result:
            raise IndexContractError('migration_body_limit')
        return {'members': result, 'observation': observation,
                'next': {'after': result[-1]['path'], 'observation': observation}
                        if len(rows) > len(result) else None}


def capture_base_fts_corpus(snapshot, *, endpoint, decision, after, deadline):
    """Capture old base FTS wiki rows, including hidden-by-exclusion rows."""
    from laya_blob_values import MAX_BODY
    from laya_index_a import _qmd_title
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.monotonic() >= deadline):
        raise IndexContractError('reader_snapshot_deadline')
    deadline = min(deadline, time.monotonic() + _STEP_SECONDS)
    current = capture_effective(snapshot, endpoint=endpoint, decision=decision,
                                after=None, deadline=deadline)
    observation = hashlib.sha256(('memvane-base-fts-corpus-v1:' +
                                  current['observation']).encode('ascii')).hexdigest()
    path_after = ''
    if after is not None:
        if (type(after) is not dict or set(after) != {'after', 'observation'}
                or type(after['after']) is not str or not _valid_relative_path(after['after'])
                or after['observation'] != observation):
            raise IndexContractError('migration_snapshot_changed')
        path_after = after['after']
    prefixes = tuple(name + '/' for name in wiki_index_roots(decision['collections']))
    base = snapshot.connections[0]
    args = (snapshot.published[0], decision['scopeId'])
    with readers._validation_deadline(snapshot.connections, deadline):
        # Probe only the gaps between wiki-root ranges: each is one index
        # seek, so this check does not grow with the wiki corpus.
        edges = [None] + [edge for item in _prefix_ranges(prefixes) for edge in item] + [None]
        for lower, upper in zip(edges[0::2], edges[1::2]):
            nonwiki = base.execute('SELECT 1 FROM membership m WHERE m.gen=? AND m.scope=? AND m.eligible=1' +
                                   ('' if lower is None else ' AND m.path>=?') +
                                   ('' if upper is None else ' AND m.path<?') + ' LIMIT 1',
                                   args + tuple(edge for edge in (lower, upper) if edge is not None)
                                   ).fetchone()
            if nonwiki is not None:
                raise IndexContractError('migration_raw_fts_corpus_unproved')
        rows = base.execute('SELECT m.path,m.hash FROM membership m '
            'WHERE m.gen=? AND m.scope=? AND m.eligible=1 AND m.path>? '
            'ORDER BY m.path COLLATE BINARY LIMIT ?',
            (snapshot.published[0], decision['scopeId'], path_after, MAX_BATCH + 1)).fetchall()
        result, used = [], 0
        for path, digest in rows:
            if len(result) == MAX_BATCH:
                break
            if (type(path) is not str or not _valid_relative_path(path) or not path.startswith(prefixes)
                    or type(digest) is not str or re.fullmatch(r'[0-9a-f]{64}', digest) is None):
                raise IndexContractError('migration_membership_invalid')
            if not _wiki_card_path(path, prefixes):
                raise IndexContractError('migration_meta_fts_corpus_unproved')
            size_row = base.execute('SELECT length(CAST(body AS BLOB)) FROM content WHERE hash=?',
                                    (digest,)).fetchone()
            size = size_row[0] if size_row else None
            if type(size) is not int or not 0 <= size <= MAX_BODY:
                raise IndexContractError('migration_body_limit')
            if used + size > MAX_BATCH_BYTES:
                break
            body_row = base.execute('SELECT body FROM content WHERE hash=?', (digest,)).fetchone()
            body = body_row[0] if body_row else None
            if (type(body) is not str or len(body.encode('utf8')) != size
                    or hashlib.sha256(body.encode('utf8')).hexdigest() != digest):
                raise IndexContractError('migration_member_changed')
            result.append({'path': path, 'hash': digest, 'body': body, 'title': _qmd_title(body, path)})
            used += size
        if time.monotonic() >= deadline:
            raise IndexContractError('reader_snapshot_deadline')
        if rows and not result:
            raise IndexContractError('migration_body_limit')
        return {'members': result, 'observation': observation,
                'next': {'after': result[-1]['path'], 'observation': observation}
                        if len(rows) > len(result) else None}


def import_wiki_batch(store, *, worktree_id, snapshot, endpoint, decision, after, deadline,
                      provenance_scan=None, verify_only=False, base_fts_corpus=False,
                      sidecar_fts_corpus=False, _expected_observation=None, _only=None):
    """Stage snapshot wiki bodies and optional unbound evidence; publish nothing."""
    import laya_blob_store as blobs
    import laya_blob_values as values
    import laya_checkout_view as checkout
    from pathlib import Path

    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.monotonic() >= deadline):
        raise IndexContractError('reader_snapshot_deadline')
    deadline = min(deadline, time.monotonic() + _STEP_SECONDS)
    if (type(verify_only) is not bool or type(base_fts_corpus) is not bool
            or type(sidecar_fts_corpus) is not bool or (base_fts_corpus and sidecar_fts_corpus)
            or ((base_fts_corpus or sidecar_fts_corpus) and provenance_scan is not None)
            or (verify_only and provenance_scan is None and not (base_fts_corpus or sidecar_fts_corpus))
            or (_only is not None and (base_fts_corpus or sidecar_fts_corpus))):
        values.fail('migration_verify_invalid')
    if not verify_only:
        store._require_write()
    values.identifier(worktree_id, values.UUID)
    if provenance_scan is not None:
        values.exact(provenance_scan, ('id', 'observed_ns'))
        values.identifier(provenance_scan['id'], values.UUID)
        values.integer(provenance_scan['observed_ns'])
    trusted = []
    prior_busy = store._db.execute('PRAGMA busy_timeout').fetchone()[0]
    remaining = max(0, int((deadline - time.monotonic()) * 1000))
    if remaining == 0:
        raise IndexContractError('reader_snapshot_deadline')
    store._db.execute('PRAGMA busy_timeout=' + str(remaining))
    try:
        with store._session() as db:
            saved = db.execute('SELECT * FROM worktrees WHERE worktree_id=?', (worktree_id,)).fetchone()
            if saved is None:
                values.fail('worktree_missing')
            saved = dict(saved)
        worktree = blobs._worktree_record(saved, store._repository, store._policy)
        if worktree['gitDir'] != store._repository['commonGitDir']:
            values.fail('migration_main_checkout_required')
        if (saved['project_root'] != endpoint.record['project']
                or store._scope != endpoint.record['scope']):
            values.fail('migration_endpoint_changed')
        row, observed = blobs._worktree_row(saved['project_root'], store._repository,
                                             worktree, deadline=deadline)
        if any(saved[key] != value for key, value in row.items()):
            values.fail('wiki_binding_changed')
        capture = (capture_base_fts_corpus if base_fts_corpus else
                   capture_sidecar_fts_corpus if sidecar_fts_corpus else capture_wiki_bytes)
        is_corpus = base_fts_corpus or sidecar_fts_corpus
        extra = ({} if _expected_observation is None or is_corpus else
                 {'_expected_observation': _expected_observation})
        if _only is not None:
            extra['_only'] = _only
        page = capture(snapshot, endpoint=endpoint, decision=decision, after=after, deadline=deadline, **extra)
        if _expected_observation is not None and is_corpus:
            values.identifier(_expected_observation)
            prefix = 'memvane-base-fts-corpus-v1:' if base_fts_corpus else 'memvane-sidecar-fts-corpus-v1:'
            expected_corpus = hashlib.sha256((prefix + _expected_observation).encode('ascii')).hexdigest()
            if page['observation'] != expected_corpus:
                raise IndexContractError('migration_snapshot_changed')
        roots = {item['name']: item['sourceRoot'] for item in decision['collections']
                 if item.get('role') == 'wiki'}
        remaining = max(0, int((deadline - time.monotonic()) * 1000))
        if remaining == 0:
            raise IndexContractError('reader_snapshot_deadline')
        store._db.execute('PRAGMA busy_timeout=' + str(remaining))
        with store._session(write=not verify_only) as db:
            for member in page['members']:
                if time.monotonic() >= deadline:
                    raise IndexContractError('reader_snapshot_deadline')
                name, relative = member['path'].split('/', 1)
                values.relative(relative)
                if name not in roots or str(Path(roots[name])) != saved['wiki_root']:
                    values.fail('migration_wiki_root_changed')
                digest, body = member['hash'], member['body']
                raw = body.encode('utf8')
                proof = {'schema': 'memvane-v6-wiki-admission-v1', 'blob_sha': digest,
                         'worktree_id': worktree_id, 'path': relative,
                         'binding_digest': row['binding_digest'],
                         'wiki_root': saved['wiki_root'],
                         'wiki_identity': {'dev': saved['wiki_dev'], 'ino': saved['wiki_ino']},
                         'repository_digest': worktree['repositoryDigest'],
                         'capture': observed['capture'], 'endpoint_id': endpoint.endpoint_id,
                         'observation': page['observation']}
                existing = db.execute('SELECT blob_sha FROM wiki_blobs WHERE blob_sha=?', (digest,)).fetchone()
                if existing:
                    if store._blob(digest) != body:
                        values.fail('blob_conflict')
                elif verify_only:
                    values.fail('migration_staged_missing')
                else:
                    now = time.time_ns()
                    blobs._insert(db, 'wiki_blobs', dict(blob_sha=digest, body=body,
                                                         byte_length=len(raw), created_ns=now))
                    blobs._insert(db, 'blob_admissions', dict(blob_sha=digest, worktree_id=worktree_id,
                        proof_digest=values.digest('memvane-v6-wiki-admission-v1', proof),
                        proof_json=values.canonical(proof), created_ns=now))
                if provenance_scan is not None:
                    evidence = legacy_card_evidence(member, decision)
                    if evidence is not None:
                        trust = store._put_legacy_attestation(db, blob_sha=digest, worktree_id=worktree_id,
                            branch_key=observed['capture']['branchKey'], evidence=evidence,
                            provenance_scan=provenance_scan, verify_only=verify_only)
                        trusted.append({'path': member['path'], 'hash': digest, 'trust_digest': trust,
                                        'title': member['title'], 'source': member['source']})
            import os
            try:
                blobs._bound_settings(saved)
            except values.BlobStoreError:
                values.fail('wiki_admission_changed')
            wiki_fd = blobs._dirfd(Path(saved['wiki_root']))
            try:
                wiki_node = blobs._node(os.fstat(wiki_fd))
            finally:
                os.close(wiki_fd)
            if (wiki_node != {'dev': saved['wiki_dev'], 'ino': saved['wiki_ino']}
                    or not checkout.capture_matches(observed['capture'], store._repository,
                                                    worktree, deadline=deadline)
                    or time.monotonic() >= deadline):
                values.fail('wiki_admission_changed')
            readers._bound(endpoint)
            with journal.journal.read_only_journals():
                current = (journal._recheck(endpoint)[2]['readerDecision']
                           if endpoint in journal._upgrade_readers else
                           journal.journal.select_route(endpoint.record['project'], action='recall'))
            if current != decision or time.monotonic() >= deadline:
                values.fail('reader_selection_changed')
            if not any(item is snapshot and bound is endpoint and selected == decision
                       for item, bound, selected in readers._observations.get()):
                values.fail('migration_snapshot_changed')
        return {'imported': len(page['members']), 'observation': page['observation'],
                'next': page['next'], 'trusted': trusted,
                'inventory': [{key: item[key] for key in ('path', 'hash', 'source')}
                              for item in page['members']] if provenance_scan is not None else [],
                'corpus': [{**{'path': item['path'], 'hash': item['hash']},
                            **({'title': item['title'], 'rowid': item['rowid']}
                               if sidecar_fts_corpus else {})}
                           for item in page['members']] if base_fts_corpus or sidecar_fts_corpus else []}
    finally:
        store._db.execute('PRAGMA busy_timeout=' + str(prior_busy))


def _merge_verified_page(state, page):
    import laya_view_values as views
    for row in page['inventory']:
        if row['source'] not in state['active_fts'] or row['path'] in state['active_fts'][row['source']]:
            raise IndexContractError('migration_duplicate_wiki_path')
        state['active_fts'][row['source']][row['path']] = row['hash']
    for row in page['trusted']:
        _, relative = row['path'].split('/', 1)
        if relative in state['fts_paths']:
            raise IndexContractError('migration_duplicate_wiki_path')
        state['fts_paths'][relative] = row['path']
        state['fts_sources'][relative] = row['source']
        state['members'].append({'path': relative, 'blob_sha': row['hash'],
            'trust_digest': row['trust_digest'], 'display_title': row['title']})
        if len(state['members']) > views.MAX_MEMBERS:
            raise IndexContractError('migration_membership_limit')


def _verified_state(observation=None):
    return {'observation': observation, 'members': [], 'fts_paths': {}, 'fts_sources': {},
            'active_fts': {'base': {}, 'sidecar': {}}}


def verify_generation_members(store, *, worktree_id, snapshot, endpoint, decision, provenance_scan, deadline):
    """Reconstruct a complete candidate mapping from one live pinned old snapshot."""
    after, state = None, _verified_state()
    while True:
        page = import_wiki_batch(store, worktree_id=worktree_id, snapshot=snapshot,
            endpoint=endpoint, decision=decision, after=after, deadline=deadline,
            provenance_scan=provenance_scan, verify_only=True)
        if state['observation'] is not None and state['observation'] != page['observation']:
            raise IndexContractError('migration_snapshot_changed')
        state['observation'] = page['observation']
        _merge_verified_page(state, page)
        after = page['next']
        if after is None:
            break
    return state


def verify_migration_page(store, *, worktree_id, endpoint, decision, provenance_scan,
                          observation, cursor, deadline):
    """Reverify one copied page in a single bounded snapshot, without writes."""
    import laya_blob_values as values
    values.identifier(observation)
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.monotonic() >= deadline):
        raise IndexContractError('reader_snapshot_deadline')
    step_deadline = min(deadline, time.monotonic() + _STEP_SECONDS)
    with readers.inspect_warm_snapshot(endpoint, deadline=step_deadline) as snapshot:
        page = import_wiki_batch(store, worktree_id=worktree_id, snapshot=snapshot,
            endpoint=endpoint, decision=decision, after=cursor, deadline=step_deadline,
            provenance_scan=provenance_scan, verify_only=True, _expected_observation=observation)
    if time.monotonic() >= step_deadline:
        raise IndexContractError('reader_snapshot_deadline')
    if page['observation'] != observation:
        raise IndexContractError('migration_snapshot_changed')
    return page


def collect_generation_members(store, *, worktree_id, endpoint, decision, provenance_scan,
                               observation, deadline, commitment=None):
    """Reverify copied mapping over bounded snapshots; this result grants no authority."""
    if commitment is not None:
        import laya_blob_values as values
        values.exact(commitment, ('migration_id', 'progress'))
        values.identifier(commitment['migration_id'], values.UUID)
        identity = {'migration_id': commitment['migration_id'], 'observation': observation,
                    'provenance_scan': provenance_scan}
        expected = _verification_progress(commitment['progress'], identity)
        if not expected['complete']:
            raise IndexContractError('migration_commitment_mismatch')
        progress = _initial_verification(identity)
    after, state = None, _verified_state(observation)
    while True:
        page = verify_migration_page(store, worktree_id=worktree_id, endpoint=endpoint,
            decision=decision, provenance_scan=provenance_scan, observation=observation,
            cursor=after, deadline=deadline)
        _merge_verified_page(state, page)
        if commitment is not None:
            progress = _fold_verification(progress, page)
        after = page['next']
        if after is None:
            if commitment is not None and progress != expected:
                raise IndexContractError('migration_commitment_mismatch')
            return state


def _stage_fts_corpus(store, *, worktree_id, snapshot, endpoint, decision, deadline,
                      verify_only, sidecar):
    import laya_view_values as views
    cursor, observation, corpus = None, None, []
    while True:
        page = import_wiki_batch(store, worktree_id=worktree_id, snapshot=snapshot,
            endpoint=endpoint, decision=decision, after=cursor, deadline=deadline,
            base_fts_corpus=not sidecar, sidecar_fts_corpus=sidecar, verify_only=verify_only)
        if observation is not None and observation != page['observation']:
            raise IndexContractError('migration_snapshot_changed')
        observation = page['observation']
        corpus.extend(page['corpus'])
        if len(corpus) > views.MAX_MEMBERS:
            raise IndexContractError('migration_membership_limit')
        cursor = page['next']
        if cursor is None:
            break
    return {'observation': observation, 'members': corpus}


def stage_corpus_page(store, *, worktree_id, endpoint, decision, observation,
                      cursor, deadline, sidecar, verify_only=False):
    """Stage or check one corpus page under one bounded snapshot."""
    import laya_blob_values as values
    values.identifier(observation)
    if type(sidecar) is not bool or type(verify_only) is not bool:
        raise IndexContractError('migration_verify_invalid')
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.monotonic() >= deadline):
        raise IndexContractError('reader_snapshot_deadline')
    prefix = 'memvane-sidecar-fts-corpus-v1:' if sidecar else 'memvane-base-fts-corpus-v1:'
    expected = hashlib.sha256((prefix + observation).encode('ascii')).hexdigest()
    step_deadline = min(deadline, time.monotonic() + _STEP_SECONDS)
    with readers.inspect_warm_snapshot(endpoint, deadline=step_deadline) as snapshot:
        page = import_wiki_batch(store, worktree_id=worktree_id, snapshot=snapshot,
            endpoint=endpoint, decision=decision, after=cursor, deadline=step_deadline,
            base_fts_corpus=not sidecar, sidecar_fts_corpus=sidecar,
            verify_only=verify_only, _expected_observation=observation)
    if time.monotonic() >= step_deadline:
        raise IndexContractError('reader_snapshot_deadline')
    if page['observation'] != expected:
        raise IndexContractError('migration_snapshot_changed')
    return page


def stage_fts_corpus_bounded(store, *, worktree_id, endpoint, decision, observation,
                             deadline, sidecar, verify_only=False, commitment=None):
    """Stage or check one FTS corpus across separately validated bounded snapshots."""
    import laya_view_values as views
    if commitment is not None:
        import laya_blob_values as values
        values.exact(commitment, ('migration_id', 'provenance_scan', 'verification_digest', 'progress'))
        values.identifier(commitment['migration_id'], values.UUID)
        values.identifier(commitment['verification_digest'])
        identity = {'migration_id': commitment['migration_id'], 'observation': observation,
                    'provenance_scan': commitment['provenance_scan']}
        prefix = 'memvane-sidecar-fts-corpus-v1:' if sidecar else 'memvane-base-fts-corpus-v1:'
        corpus_observation = hashlib.sha256((prefix + observation).encode('ascii')).hexdigest()
        expected = _verification_progress(commitment['progress'], identity,
                                          cursor_observation=corpus_observation)
        if not expected['complete']:
            raise IndexContractError('migration_commitment_mismatch')
        progress = _initial_corpus(identity, 'sidecar' if sidecar else 'base', corpus_observation)
    cursor, corpus = None, []
    while True:
        page = stage_corpus_page(store, worktree_id=worktree_id, endpoint=endpoint,
            decision=decision, observation=observation, cursor=cursor, deadline=deadline,
            sidecar=sidecar, verify_only=verify_only)
        corpus.extend(page['corpus'])
        if commitment is not None:
            progress = _fold_corpus(progress, page)
        if len(corpus) > views.MAX_MEMBERS:
            raise IndexContractError('migration_membership_limit')
        cursor = page['next']
        if cursor is None:
            if commitment is not None and progress != expected:
                raise IndexContractError('migration_commitment_mismatch')
            return {'observation': page['observation'], 'members': corpus}


def _committed_pages(items, size):
    """Split rows exactly as the bounded captures do: MAX_BATCH rows, then MAX_BATCH_BYTES."""
    pages, page, used = [], [], 0
    for item in items:
        length = size(item)
        if page and (len(page) == MAX_BATCH or used + length > MAX_BATCH_BYTES):
            pages.append(page)
            page, used = [], 0
        if length > MAX_BATCH_BYTES:
            raise IndexContractError('migration_body_limit')
        page.append(item)
        used += length
    pages.append(page)
    return pages


def _fold_committed(progress, fold, pages, observation, content):
    for index, rows in enumerate(pages):
        last = index == len(pages) - 1
        page = {**content(rows), 'next': None if last else {'after': rows[-1]['path'],
                                                             'observation': observation}}
        progress = fold(progress, page)
        yield progress, page


def reuse_committed_members(store, *, worktree_id, endpoint, decision, provenance_scan,
                            observation, commitments, deadline):
    """Rebuild the committed mapping and corpora without re-reading old bodies.

    The verification and corpus steps already proved every page against the
    source at ``observation`` and folded it into the intent's chains. Here one
    pinned snapshot supplies only metadata (paths, hashes, sources, sidecar
    titles and rowids) and must still show ``observation``; bodies come from
    the candidate's content-addressed blobs. The rebuilt pages are accepted
    only if they refold to exactly the committed chains, so any difference
    between the reused chain and the pinned source refuses. The caller's
    final recheck still proves the source revision; this grants no authority.
    """
    import laya_blob_store as blobs
    import laya_blob_values as values
    from pathlib import Path
    from laya_index_a import _qmd_title

    values.identifier(observation)
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.monotonic() >= deadline):
        raise IndexContractError('reader_snapshot_deadline')
    values.exact(commitments, ('migration_id', 'verification', 'corpora'))
    values.identifier(commitments['migration_id'], values.UUID)
    values.identifier(worktree_id, values.UUID)
    identity = {'migration_id': commitments['migration_id'], 'observation': observation,
                'provenance_scan': provenance_scan}
    expected = _verification_progress(commitments['verification'], identity)
    corpora = commitments['corpora']
    values.exact(corpora, ('verification_digest', 'base', 'sidecar'))
    if (not expected['complete'] or corpora['verification_digest'] != expected['digest']
            or corpora['base'] is None):
        raise IndexContractError('migration_commitment_mismatch')
    expected_corpora = {}
    for key in ('base', 'sidecar'):
        if corpora[key] is not None:
            expected_corpora[key] = _verification_progress(
                corpora[key], identity, cursor_observation=_corpus_observation(key, observation))
            if not expected_corpora[key]['complete']:
                raise IndexContractError('migration_commitment_mismatch')
    with store._session() as db:
        saved = db.execute('SELECT * FROM worktrees WHERE worktree_id=?', (worktree_id,)).fetchone()
    if saved is None:
        values.fail('worktree_missing')
    saved = dict(saved)
    worktree = blobs._worktree_record(saved, store._repository, store._policy)
    if worktree['gitDir'] != store._repository['commonGitDir']:
        values.fail('migration_main_checkout_required')
    if saved['project_root'] != endpoint.record['project'] or store._scope != endpoint.record['scope']:
        values.fail('migration_endpoint_changed')
    row, observed = blobs._worktree_row(saved['project_root'], store._repository,
                                         worktree, deadline=deadline)
    if any(saved[key] != value for key, value in row.items()):
        values.fail('wiki_binding_changed')
    roots = {item['name']: item['sourceRoot'] for item in decision['collections']
             if item.get('role') == 'wiki'}
    prefixes = tuple(name + '/' for name in wiki_index_roots(decision['collections']))

    step_deadline = min(deadline, time.monotonic() + _STEP_SECONDS)
    with readers.inspect_warm_snapshot(endpoint, deadline=step_deadline) as snapshot:
        current = capture_effective(snapshot, endpoint=endpoint, decision=decision,
                                    after=None, deadline=step_deadline)
        if current['observation'] != observation:
            raise IndexContractError('migration_snapshot_changed')
        inventory = capture_effective(snapshot, endpoint=endpoint, decision=decision, after=None,
                                      deadline=step_deadline, include_ineligible=True, _all_inventory=True)
        base, _, _, side = snapshot.connections
        needs_sidecar = side is not None and any(item['source'] == 'sidecar'
                                                 for item in inventory['members'])
        sidecar_commitment = (sidecar_fts_commitment(snapshot, endpoint=endpoint, decision=decision,
                                                     deadline=step_deadline) if needs_sidecar else None)
        with readers._validation_deadline(snapshot.connections, step_deadline):
            args = (snapshot.published[0], decision['scopeId'])
            edges = [None] + [edge for item in _prefix_ranges(prefixes) for edge in item] + [None]
            for lower, upper in zip(edges[0::2], edges[1::2]):
                if base.execute('SELECT 1 FROM membership m WHERE m.gen=? AND m.scope=? AND m.eligible=1' +
                                ('' if lower is None else ' AND m.path>=?') +
                                ('' if upper is None else ' AND m.path<?') + ' LIMIT 1',
                                args + tuple(edge for edge in (lower, upper) if edge is not None)).fetchone():
                    raise IndexContractError('migration_raw_fts_corpus_unproved')
            base_rows = base.execute('SELECT m.path,m.hash FROM membership m WHERE m.gen=? AND m.scope=? '
                                     'AND m.eligible=1 ORDER BY m.path COLLATE BINARY LIMIT ?',
                                     (snapshot.published[0], decision['scopeId'], MAX_DOCS + 1)).fetchall()
            side_rows = (side.execute('SELECT path,hash,title,rowid FROM docs ORDER BY path COLLATE BINARY '
                                      'LIMIT ?', (MAX_DOCS + 1,)).fetchall() if needs_sidecar else [])
        if len(base_rows) > MAX_DOCS or len(side_rows) > MAX_DOCS:
            raise IndexContractError('migration_membership_limit')
    if time.monotonic() >= step_deadline:
        raise IndexContractError('reader_snapshot_deadline')

    titles = {path: title for path, _, title, _ in side_rows}
    effective = [item for item in inventory['members'] if item['eligible'] == 1 and not item['excluded']]
    base_corpus = [{'path': path, 'hash': digest} for path, digest in base_rows]
    side_corpus = [{'path': path, 'hash': digest, 'title': title, 'rowid': rowid}
                   for path, digest, title, rowid in side_rows]
    bodies = {}
    with store._session() as db:
        def body(digest):
            if time.monotonic() >= deadline:
                raise IndexContractError('reader_snapshot_deadline')
            if digest not in bodies:
                bodies[digest] = store._blob(digest)
            return bodies[digest]

        def size(item):
            return len(body(item['hash']).encode('utf8'))

        def mapping(members):
            inventory_rows, trusted = [], []
            for member in members:
                name, _ = member['path'].split('/', 1)
                if name not in roots or str(Path(roots[name])) != saved['wiki_root']:
                    values.fail('migration_wiki_root_changed')
                text = body(member['hash'])
                title = (_qmd_title(text, member['path']) if member['source'] == 'base'
                         else titles.get(member['path']))
                inventory_rows.append({key: member[key] for key in ('path', 'hash', 'source')})
                evidence = legacy_card_evidence({**inventory_rows[-1], 'body': text, 'title': title}, decision)
                if evidence is not None:
                    trust = store._put_legacy_attestation(db, blob_sha=member['hash'], worktree_id=worktree_id,
                        branch_key=observed['capture']['branchKey'], evidence=evidence,
                        provenance_scan=provenance_scan, verify_only=True)
                    trusted.append({'path': member['path'], 'hash': member['hash'], 'trust_digest': trust,
                                    'title': title, 'source': member['source']})
            return {'inventory': inventory_rows, 'trusted': trusted}

        state, progress = _verified_state(observation), _initial_verification(identity)
        for progress, page in _fold_committed(progress, _fold_verification,
                                              _committed_pages(effective, size), observation, mapping):
            _merge_verified_page(state, page)
        if progress != expected:
            raise IndexContractError('migration_commitment_mismatch')
        corpora_out = {}
        for key, rows in (('base', base_corpus), ('sidecar', side_corpus)):
            if key == 'sidecar' and not needs_sidecar:
                continue
            if key not in expected_corpora:
                raise IndexContractError('migration_commitment_mismatch')
            corpus_observation = _corpus_observation(key, observation)
            progress = _initial_corpus(identity, key, corpus_observation)
            for progress, _ in _fold_committed(progress, _fold_corpus, _committed_pages(rows, size),
                                               corpus_observation, lambda part: {'corpus': part}):
                pass
            if progress != expected_corpora[key]:
                raise IndexContractError('migration_commitment_mismatch')
            corpora_out[key] = {'observation': corpus_observation, 'members': rows}
    return {'evidence': state, 'sidecar_commitment': sidecar_commitment,
            'corpus': corpora_out['base'], 'sidecar_corpus': corpora_out.get('sidecar')}


def sidecar_fts_required(snapshot, *, endpoint, decision, deadline):
    """Detect any effective changed sidecar, including hidden members."""
    cursor = None
    while True:
        page = capture_effective(snapshot, endpoint=endpoint, decision=decision,
            after=cursor, deadline=deadline, include_ineligible=True)
        if any(row['source'] == 'sidecar' for row in page['members']):
            return True
        cursor = page['next']
        if cursor is None:
            return False


def inspect_sidecar_bounded(*, endpoint, decision, observation, deadline):
    """Check hidden effective sidecar rows using separately validated snapshots."""
    import laya_blob_values as values
    values.identifier(observation)
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.monotonic() >= deadline):
        raise IndexContractError('reader_snapshot_deadline')
    cursor = None
    while True:
        step_deadline = min(deadline, time.monotonic() + _STEP_SECONDS)
        with readers.inspect_warm_snapshot(endpoint, deadline=step_deadline) as snapshot:
            current = capture_effective(snapshot, endpoint=endpoint, decision=decision,
                                        after=None, deadline=step_deadline)
            if current['observation'] != observation:
                raise IndexContractError('migration_snapshot_changed')
            absent = snapshot.connections[3] is None
            if not absent:
                page = capture_effective(snapshot, endpoint=endpoint, decision=decision,
                                         after=cursor, deadline=step_deadline, include_ineligible=True)
                found = any(row['source'] == 'sidecar' for row in page['members'])
                commitment = (sidecar_fts_commitment(snapshot, endpoint=endpoint, decision=decision,
                                                      deadline=step_deadline) if found else None)
        if time.monotonic() >= step_deadline:
            raise IndexContractError('reader_snapshot_deadline')
        if absent:
            return None
        if found:
            return commitment
        cursor = page['next']
        if cursor is None:
            return None


def stage_base_fts_corpus(store, *, worktree_id, snapshot, endpoint, decision, deadline,
                          verify_only=False, allow_sidecar=False):
    """Stage old base FTS wiki corpus; callers must handle any changed sidecar."""
    if type(allow_sidecar) is not bool:
        raise IndexContractError('migration_verify_invalid')
    if not allow_sidecar and sidecar_fts_required(snapshot, endpoint=endpoint,
                                                 decision=decision, deadline=deadline):
        raise IndexContractError('migration_lexical_sidecar_unproved')
    return _stage_fts_corpus(store, worktree_id=worktree_id, snapshot=snapshot, endpoint=endpoint,
                             decision=decision, deadline=deadline, verify_only=verify_only, sidecar=False)


def stage_sidecar_fts_corpus(store, *, worktree_id, snapshot, endpoint, decision, deadline, verify_only=False):
    """Stage or recheck the complete old sidecar FTS wiki corpus without publishing."""
    return _stage_fts_corpus(store, worktree_id=worktree_id, snapshot=snapshot, endpoint=endpoint,
                             decision=decision, deadline=deadline, verify_only=verify_only, sidecar=True)


def compare_lexical_queries(snapshot, *, endpoint, decision, store, pin, queries, deadline):
    """Compare supplied raw FTS queries per old corpus; no parity receipt is issued."""
    from laya_index_a import _search_pinned_fts
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.monotonic() >= deadline):
        raise IndexContractError('reader_snapshot_deadline')
    if (type(queries) is not tuple or not 1 <= len(queries) <= 32
            or any(type(query) is not str or not query or len(query.encode('utf8')) > 4096
                   for query in queries)):
        raise IndexContractError('migration_lexical_queries_invalid')
    selected = capture_effective(snapshot, endpoint=endpoint, decision=decision,
                                 after=None, deadline=deadline)
    active = {'base': {}, 'sidecar': {}}
    shadowed = []
    # capture_effective already validates and materializes the complete
    # inventory before paging. Reusing that single pinned result avoids
    # repeating the same full scan once for each page.
    inventory = capture_effective(snapshot, endpoint=endpoint, decision=decision,
        after=None, deadline=deadline, include_ineligible=True, _all_inventory=True)
    for row in inventory['members']:
        if row['source'] == 'sidecar':
            shadowed.append(row['path'])
        if row['eligible'] and not row['excluded']:
            active[row['source']][row['path']] = row['hash']
    if shadowed and pin is None:
        raise IndexContractError('migration_lexical_sidecar_unproved')
    record = store._pinned(pin)
    if store._scope != endpoint.record['scope']:
        raise IndexContractError('migration_endpoint_changed')
    with store._reading(deadline) as db:
        generation, _, _, stage = store._read_pinned(db, pin, deadline)
        root = db.execute('SELECT project_root FROM worktrees WHERE worktree_id=?',
                          (record['entry']['handle'].worktree_id,)).fetchone()
        if (generation['state'] != 'published' or root is None
                or root[0] != endpoint.record['project'] or 'legacy' not in stage
                or stage['legacy']['observation'] != selected['observation']):
            raise IndexContractError('migration_snapshot_changed')
        fts_paths = stage['legacy']['fts_paths']
        paired = 'sidecar_corpus' in stage['legacy']
        if bool(shadowed) != paired:
            raise IndexContractError('migration_lexical_sidecar_unproved')
        if stage['legacy']['active_fts'] != active:
            raise IndexContractError('migration_snapshot_changed')
    if paired and sidecar_fts_commitment(snapshot, endpoint=endpoint, decision=decision,
                                         deadline=deadline) != stage['legacy']['sidecar_commitment']:
        raise IndexContractError('migration_sidecar_changed')
    if paired:
        # The legacy sidecar presence probe uses its first FTS row without a
        # rank ORDER BY; trust of that particular row controls body exposure.
        # Rebuilt FTS rowids can differ even when ranked results are equal.
        from laya_host_response import _verified_wiki_row
        from laya_index_source_port import auxiliary_wiki_row
        from laya_view_fts import table as fts_table
        side = snapshot.connections[3]
        paths_json = json.dumps(list(active['sidecar']), ensure_ascii=False, separators=(',', ':'))
        verified_paths = {full for relative, full in fts_paths.items()
                          if stage['legacy']['fts_sources'][relative] == 'sidecar'}
        roots = wiki_index_roots(decision['collections'])
        with readers._validation_deadline(snapshot.connections, deadline), store._reading(deadline) as db:
            generation, _, _, _ = store._read_pinned(db, pin, deadline)
            name = fts_table(generation['ordinal'], sidecar=True)
            for query in queries:
                try:
                    old_probe = side.execute('SELECT d.path,d.hash,d.body FROM docs_fts f '
                        'JOIN docs d ON d.rowid=f.rowid WHERE docs_fts MATCH ? '
                        'AND d.path IN (SELECT value FROM json_each(?)) LIMIT 1',
                        (query, paths_json)).fetchone()
                    new_probe = db.execute(f'SELECT path_key FROM {name} WHERE {name} MATCH ? '
                        'AND path_key IN (SELECT value FROM json_each(?)) LIMIT 1',
                        (query, paths_json)).fetchone()
                except sqlite3.Error as exc:
                    raise IndexContractError('migration_lexical_query_unproved') from exc
                if (old_probe is None) != (new_probe is None):
                    raise IndexContractError('migration_lexical_probe_mismatch')
                if old_probe is not None:
                    path, digest, body = old_probe
                    row = {'path': path, 'hash': digest, 'body': body}
                    trusted = (not auxiliary_wiki_row(path, body, roots) and
                        _verified_wiki_row(row, decision,
                            source_skip_paths=tuple(decision['recallSettings']['skipPaths']),
                            current_source=False))
                    if trusted != (new_probe[0] in verified_paths):
                        raise IndexContractError('migration_lexical_probe_mismatch')
    base = snapshot.connections[0]
    with readers._validation_deadline(snapshot.connections, deadline):
        ordinal = base.execute('SELECT ord FROM gen_index WHERE gen=?', (snapshot.published[0],)).fetchone()
        if ordinal is None:
            raise IndexContractError('reader_generation_unpublished')
        prefixes = tuple(name + '/' for name in sorted(wiki_index_roots(decision['collections'])))
        old_batches = _search_pinned_fts(base, decision['scopeId'], snapshot.published[0],
            ordinal[0], queries[:8], 100, path_prefixes=prefixes,
            excluded_paths=tuple(sorted(shadowed)) if paired else None, deadline=deadline)
        for offset in range(8, len(queries), 8):
            old_batches.extend(_search_pinned_fts(base, decision['scopeId'], snapshot.published[0],
                ordinal[0], queries[offset:offset + 8], 100, path_prefixes=prefixes,
                excluded_paths=tuple(sorted(shadowed)) if paired else None, deadline=deadline))
        for query, old in zip(queries, old_batches):
            origins = [('base', old)]
            if paired:
                side = snapshot.connections[3]
                if side is None:
                    raise IndexContractError('migration_sidecar_changed')
                try:
                    rows = side.execute('SELECT d.path,d.hash,d.body,bm25(docs_fts,1.5,4.0,1.0) '
                        'FROM docs_fts JOIN docs d ON d.rowid=docs_fts.rowid '
                        'JOIN json_each(?) a ON a.key=d.path WHERE docs_fts MATCH ? '
                        'ORDER BY bm25(docs_fts,1.5,4.0,1.0),d.path COLLATE BINARY LIMIT 100',
                        (json.dumps(active['sidecar'], sort_keys=True, separators=(',', ':')), query)).fetchall()
                except sqlite3.Error as exc:
                    raise IndexContractError('migration_lexical_query_unproved') from exc
                origins.append(('sidecar', [{'path': path, 'hash': digest, 'body': body, 'score': score,
                    'scope': decision['scopeId'], 'generation': snapshot.published[0]}
                    for path, digest, body, score in rows]))
            for origin, original in origins:
                if len(original) == 100:
                    raise IndexContractError('migration_lexical_limit_unproved')
                expected = []
                for row in original:
                    evidence = legacy_card_evidence(row, decision)
                    if evidence is not None:
                        expected.append((row['path'], row['hash'], evidence['locators'], row['score']))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise IndexContractError('reader_snapshot_deadline')
                actual = store.search(pin, query, 100, deadline_seconds=min(remaining, 30),
                                      **({'_legacy_origin': origin} if paired else {}))
                if len(actual) == 100 or len(expected) != len(actual):
                    raise IndexContractError('migration_lexical_parity_mismatch')
                for old_row, new_row in zip(expected, actual):
                    if (old_row[:3] != (fts_paths[new_row['path']], new_row['blob_sha'],
                                        new_row.get('source_locations'))
                            or not math.isclose(old_row[3], new_row['score'], rel_tol=1e-12, abs_tol=1e-12)):
                        raise IndexContractError('migration_lexical_parity_mismatch')
    return {'observation': selected['observation'], 'query_count': len(queries)}


def compare_incremental_lexical_prompts(snapshot, *, endpoint, decision, store, pin, prompts, deadline):
    """Compare selected sidecar lexical recall for bounded caller-supplied prompts.

    This is observational evidence only: it creates no receipt or authority.
    """
    from laya_host_response import _fuse_verified_lists, _promote_exact_ep, _v2_body_evidence
    from laya_incremental_docs_query import query_incremental_snapshot, _lexical_plan, MAX_FTS
    from laya_view_fts import table as fts_table
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.monotonic() >= deadline):
        raise IndexContractError('reader_snapshot_deadline')
    if (type(prompts) is not tuple or not 1 <= len(prompts) <= 8
            or any(type(prompt) is not str or not prompt or len(prompt.encode('utf8')) > 4096
                   for prompt in prompts)):
        raise IndexContractError('migration_lexical_queries_invalid')
    selected = capture_effective(snapshot, endpoint=endpoint, decision=decision,
                                 after=None, deadline=deadline)
    record = store._pinned(pin)
    if store._scope != endpoint.record['scope']:
        raise IndexContractError('migration_endpoint_changed')
    with store._reading(deadline) as db:
        generation, seal, _, stage = store._read_pinned(db, pin, deadline)
        import laya_blob_values as values
        root = db.execute('SELECT project_root FROM worktrees WHERE worktree_id=?',
                          (record['entry']['handle'].worktree_id,)).fetchone()
        legacy = stage.get('legacy', {})
        if (generation['state'] != 'published'
                or values.digest('memvane-read-pin-seal-v1', seal) != record['seal']
                or root is None or root[0] != endpoint.record['project']
                or legacy['observation'] != selected['observation']):
            raise IndexContractError('migration_snapshot_changed')
        if 'sidecar_corpus' not in legacy:
            raise IndexContractError('migration_lexical_sidecar_unproved')
        name = fts_table(generation['ordinal'], sidecar=True)
        active = legacy['active_fts']['sidecar']
        paths_json = json.dumps(list(active), ensure_ascii=False, separators=(',', ':'))
        full_paths = legacy['fts_paths']
        trusted = {full for relative, full in full_paths.items()
                   if legacy['fts_sources'][relative] == 'sidecar'}
    if sidecar_fts_commitment(snapshot, endpoint=endpoint, decision=decision, deadline=deadline
                              ) != legacy['sidecar_commitment']:
        raise IndexContractError('migration_sidecar_changed')
    for prompt in prompts:
        old_started = time.monotonic()
        old, old_titles, old_locations, _, _ = query_incremental_snapshot(
            prompt, decision, {'vectorProfile': decision['vectorProfile']}, None,
            base=snapshot.connections[0], side=snapshot.connections[3], deadline=deadline)
        if time.monotonic() - old_started >= 1.0:
            raise IndexContractError('migration_recall_probe_unproved')
        def probe(atom):
            with store._reading(deadline) as db:
                _, seal, _, _ = store._read_pinned(db, pin, deadline)
                if values.digest('memvane-read-pin-seal-v1', seal) != record['seal']:
                    raise IndexContractError('migration_snapshot_changed')
                match = db.execute(f'SELECT path_key FROM {name} WHERE {name} MATCH ? '
                    'AND path_key IN (SELECT value FROM json_each(?)) LIMIT 1',
                    (atom, paths_json)).fetchone()
                return None if match is None else match[0]
        def search(fts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise IndexContractError('reader_snapshot_deadline')
            return store.search(pin, fts, MAX_FTS, _legacy_origin='sidecar',
                                deadline_seconds=min(remaining, 30))
        (built, present, chosen, mode, probe_matches, probe_complete, lexical) = _lexical_plan(
            prompt, decision['recallSettings']['lexicalPatterns'], probe=probe, search=search,
            deadline=deadline)
        if not probe_complete:
            raise IndexContractError('migration_recall_probe_unproved')
        probe_verified = all(path in trusted for path in probe_matches)
        lists, locations = [], {}
        lex_hits = ep_hits = False
        for position, matches in lexical:
            rows = []
            for match in matches:
                full = full_paths[match['path']]
                row = {'scope': decision['scopeId'], 'generation': 'preview-' + decision['installId'],
                       'path': full, 'hash': match['blob_sha'], 'body': match['body'], 'score': match['score']}
                rows.append(row)
                if match.get('source_locations'):
                    locations[full] = match['source_locations']
            lists.append(rows[:20])
            lex_hits |= bool(rows)
            ep_hits |= position > 0 and bool(rows)
        lists.append([])  # No provider: match the existing lexical-only sidecar path.
        rows = _promote_exact_ep(_fuse_verified_lists(lists, max_rows=20), prompt)
        skip = tuple(decision['recallSettings']['skipPaths'])
        rows = [row for row in rows if row['score'] >= decision['recallSettings']['minScore']
                and not any(pattern in row['path'] for pattern in skip)]
        locations = {row['path']: locations[row['path']]
                     for row in rows[:decision['recallSettings']['topN']] if row['path'] in locations}
        body_ok = probe_verified and _v2_body_evidence(built, present, mode, chosen, lex_hits, ep_hits)
        titles = frozenset() if body_ok else frozenset(row['path'] for row in rows)
        old_locations = {path: list(spans) for path, spans in old_locations.items()}
        if rows != old or titles != old_titles or locations != old_locations:
            raise IndexContractError('migration_recall_parity_mismatch')
    return {'observation': selected['observation'], 'prompt_count': len(prompts)}


# Recall parity corpus: every member contributes at most this many prompts,
# one snapshot compares at most _PARITY_MEMBERS members, so a page holds at
# most _PARITY_PROMPTS old and candidate recalls.
_PARITY_MEMBERS = 4
_PARITY_PROMPTS_PER_MEMBER = 4
_PARITY_PROMPTS = _PARITY_MEMBERS * _PARITY_PROMPTS_PER_MEMBER
_PARITY_PROMPT_BYTES = 512
_HEADING = re.compile(r'^#{1,6}[ \t]+(.+?)[ \t#]*$', re.MULTILINE)


def recall_corpus_prompts(path, body, title):
    """Derive one member's deterministic recall prompts.

    Order: indexed title, frontmatter title, file name stem, first heading,
    first CJK run, then the remaining headings and CJK runs. Prompts are
    whitespace-normalized, case-insensitively unique, and cut at a word
    boundary to ``_PARITY_PROMPT_BYTES``.
    """
    from laya_index_a import _QMD_CJK_RUN
    import yaml_scalars
    text = body.replace('\r\n', '\n').replace('\r', '\n')
    front, content = None, text
    if text.startswith('---\n'):
        end = text.find('\n---\n', 4)
        if end >= 0:
            front, content = text[4:end], text[end + 5:]
    named = None
    if front is not None:
        match = re.search(r'^title:[ \t]*(.+)$', front, re.MULTILINE)
        if match:
            named = yaml_scalars.load(match.group(1).strip())
    stem = path.rsplit('/', 1)[-1]
    stem = re.sub(r'[-_.]+', ' ', stem[:-3] if stem.endswith('.md') else stem)
    headings = [match.group(1) for match in _HEADING.finditer(content)]
    runs = [match.group(0) for match in _QMD_CJK_RUN.finditer(content) if len(match.group(0)) >= 2]
    ordered = [title, named, stem, *headings[:1], *runs[:1], *headings[1:], *runs[1:]]
    prompts, seen = [], set()
    for value in ordered:
        if type(value) is not str:
            continue
        words, size = [], 0
        for word in value.split():
            size += len(word.encode('utf8')) + (1 if words else 0)
            if size > _PARITY_PROMPT_BYTES:
                break
            words.append(word)
        prompt = ' '.join(words)
        if not prompt or prompt.casefold() in seen:
            continue
        seen.add(prompt.casefold())
        prompts.append(prompt)
        if len(prompts) == _PARITY_PROMPTS_PER_MEMBER:
            break
    return tuple(prompts)


def compare_combined_lexical_prompts(snapshot, *, endpoint, decision, store, pin, prompts, deadline):
    """Compare the old combined lexical recall with the candidate's on one pin.

    The old answer is ``query_v6_snapshot``: ``query_incremental_snapshot``
    plus ``query_selected_v2`` and the hook's merge rule on the pinned v6
    snapshot. The candidate answer is the same reader with its base and
    sidecar FTS lookups served by the candidate's legacy-origin tables. Each
    candidate row must carry the old trust and source locators. Observational
    evidence only: no receipt or authority.
    """
    from laya_index_source_port import auxiliary_wiki_row
    from laya_v6_reader import query_v6_snapshot
    from laya_view_fts import table as fts_table
    import laya_blob_values as values
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.monotonic() >= deadline):
        raise IndexContractError('reader_snapshot_deadline')
    if (type(prompts) is not tuple or not 1 <= len(prompts) <= _PARITY_PROMPTS
            or any(type(prompt) is not str or not prompt.strip() or len(prompt.encode('utf8')) > 4096
                   for prompt in prompts)):
        raise IndexContractError('migration_lexical_queries_invalid')
    selected = capture_effective(snapshot, endpoint=endpoint, decision=decision,
                                 after=None, deadline=deadline)
    record = store._pinned(pin)
    handle = record['entry']['handle']
    if store._scope != endpoint.record['scope']:
        raise IndexContractError('migration_endpoint_changed')
    with store._reading(deadline) as db:
        generation, seal, _, stage = store._read_pinned(db, pin, deadline)
        root = db.execute('SELECT project_root FROM worktrees WHERE worktree_id=?',
                          (handle.worktree_id,)).fetchone()
        legacy = stage.get('legacy')
        if (generation['state'] != 'published'
                or values.digest('memvane-read-pin-seal-v1', seal) != record['seal']
                or root is None or root[0] != endpoint.record['project'] or legacy is None
                or legacy['observation'] != selected['observation']):
            raise IndexContractError('migration_snapshot_changed')
        paired = 'sidecar_corpus' in legacy
        side_table = fts_table(generation['ordinal'], sidecar=True)
        # An unpaired (no sidecar) generation records no per-path FTS origin.
        full_paths, sources = legacy['fts_paths'], legacy.get('fts_sources')
        side_json = json.dumps(list(legacy['active_fts']['sidecar']), ensure_ascii=False,
                               separators=(',', ':'))
    if paired and sidecar_fts_commitment(snapshot, endpoint=endpoint, decision=decision,
                                         deadline=deadline) != legacy['sidecar_commitment']:
        raise IndexContractError('migration_sidecar_changed')
    roots = wiki_index_roots(decision['collections'])
    labels = {'base': snapshot.published[0], 'sidecar': 'preview-' + decision['installId']}

    def seconds(limit=None):
        left = deadline - time.monotonic()
        if left <= 0:
            raise IndexContractError('reader_snapshot_deadline')
        return min(left, 30 if limit is None else limit)

    def search(query, limit, deadline_seconds, **origin):
        # A candidate lookup past the old reader's probe/lexical deadline
        # would make the old reader degrade differently: unproved, not equal.
        try:
            # The page observes the checkout once before and once after its
            # comparisons (compare_recall_corpus), not per candidate lookup.
            return store.search(pin, query, limit, deadline_seconds=seconds(deadline_seconds),
                                _observed=True, **origin)
        except values.BlobStoreError as exc:
            if exc.reason == 'fts_deadline_exceeded':
                raise IndexContractError('migration_recall_probe_unproved') from exc
            raise

    def served(rows, origin, excluded=frozenset()):
        # A candidate row the old reader would not have served at this rank
        # (other origin, shadowed, untrusted, other locators) changes the
        # ranked window, so it is a parity mismatch, never a filtered row.
        answer = []
        for row in rows:
            full = full_paths.get(row['path'])
            if full is None or (paired and sources[row['path']] != origin) or full in excluded:
                raise IndexContractError('migration_recall_parity_mismatch')
            shaped = {'scope': decision['scopeId'], 'generation': labels[origin], 'path': full,
                      'hash': row['blob_sha'], 'body': row['body'], 'score': row['score']}
            evidence = legacy_card_evidence(shaped, decision)
            if (evidence is None or auxiliary_wiki_row(full, row['body'], roots)
                    or evidence['locators'] != row.get('source_locations')):
                raise IndexContractError('migration_recall_parity_mismatch')
            answer.append(shaped)
        return answer

    # End of the DF probes of the current candidate recall: every sidecar
    # probe and the first base batch (the selected reader's probe batch).
    probes = {'base': False, 'end': None}

    def base_search(queries, limit, *, path_prefixes=None, excluded_paths=None, deadline_seconds=None):
        excluded = frozenset(excluded_paths or ())
        origin = {'_legacy_origin': 'base'} if paired else {}
        batches = [served(search(query, limit, deadline_seconds, **origin), 'base', excluded)
                   for query in queries]
        if not probes['base']:
            probes['base'], probes['end'] = True, time.monotonic()
        return batches

    def side_probe(atom):
        if not paired:
            raise IndexContractError('migration_lexical_sidecar_unproved')
        with store._reading(deadline) as db:
            _, current, _, _ = store._read_pinned(db, pin, deadline)
            if values.digest('memvane-read-pin-seal-v1', current) != record['seal']:
                raise IndexContractError('migration_snapshot_changed')
            match = db.execute(f'SELECT path_key FROM {side_table} WHERE {side_table} MATCH ? '
                               'AND path_key IN (SELECT value FROM json_each(?)) LIMIT 1',
                               (atom, side_json)).fetchone()
        probes['end'] = time.monotonic()
        return None if match is None else match[0]

    def side_search(fts):
        if not paired:
            raise IndexContractError('migration_lexical_sidecar_unproved')
        from laya_incremental_docs_query import MAX_FTS
        return [row['path'] for row in served(search(fts, MAX_FTS, None, _legacy_origin='sidecar'),
                                              'sidecar')]

    lexical = {'base': base_search, 'sidecar': {'probe': side_probe, 'search': side_search}}
    for prompt in prompts:
        # Both DF probe phases stop 1 s after they start, then silently use
        # the positional fallback. Old: the whole recall finished inside 1 s.
        # Candidate: its last probe answered inside 0.9 s of the recall start.
        started = time.monotonic()
        old = query_v6_snapshot(snapshot, endpoint=endpoint, prompt=prompt, decision=decision,
                                provider=None, deadline=deadline)
        if time.monotonic() - started >= 1.0:
            raise IndexContractError('migration_recall_probe_unproved')
        probes['base'], probes['end'] = False, None
        started = time.monotonic()
        new = query_v6_snapshot(snapshot, endpoint=endpoint, prompt=prompt, decision=decision,
                                provider=None, deadline=deadline, _lexical=lexical)
        if probes['end'] is not None and probes['end'] - started >= 0.9:
            raise IndexContractError('migration_recall_probe_unproved')
        if old.semantic_status != 'lexical_only' or new.semantic_status != 'lexical_only':
            raise IndexContractError('migration_recall_probe_unproved')
        if ((old.data_generation, old.rows, old.title_only, old.source_locations)
                != (new.data_generation, new.rows, new.title_only, new.source_locations)):
            raise IndexContractError('migration_recall_parity_mismatch')
    return {'observation': selected['observation'], 'prompt_count': len(prompts)}


_INTENT = 'shadow-migration-intent.json'
_INTENT_KEYS = frozenset(('schema', 'migration_id', 'project', 'state', 'state_node',
                          'install_id', 'endpoint_id', 'observation', 'candidate',
                          'candidate_node', 'candidate_incarnation', 'worktree_id',
                          'phase', 'cursor', 'provenance_scan', 'source_revision',
                          'migration_state'))
# Design 8.2 names stored as ``migration_state``; derived only by _shadow_state.
#   copying   - phase copying, or copied with verification, corpora, candidate
#               build or private publish still pending (restart included)
#   caught_up - complete commitments and a recorded private candidate head
#   shadow_verified - caught_up plus a sealed recall parity record bound to
#               that head, its source observation and revision (U5 receipt)
#   reader_ready - shadow_verified plus a ``reader_ready`` record (U15): the
#               schema3 target, the v6 CAS base and the release commit both
#               hosts observed, bound to that sealed parity
#   switched  - reader_ready plus the journal CAS result naming its target
# The journal reader selection is the fact; the intent records it. deferred
# and retry are restart_required plus a reason code, not states.
_SHADOW_STATES = ('copying', 'caught_up', 'shadow_verified', 'reader_ready', 'switched')
# Source changes proved by a later step; the copy restarts from a fresh scan.
_SHADOW_RESTART_REASONS = frozenset(('migration_snapshot_changed', 'migration_source_revision_unproved'))
# Writer races on the reader store itself; the next step re-proves the source.
_SHADOW_RETRY_REASONS = frozenset(('reader_store_changed', 'migration_space_deferred',
                                   'migration_checkout_changed'))


def _shadow_state(intent):
    if intent.get('published') is None:
        return 'copying'
    parity = _bound_parity(intent)
    if parity is None or parity['seal'] is None:
        return 'caught_up'
    if _bound_switched(intent) is not None:
        return 'switched'
    return 'reader_ready' if _bound_reader_ready(intent) is not None else 'shadow_verified'


def _stamp(intent):
    return {**intent, 'migration_state': _shadow_state(intent)}


_MIGRATION_REASONS = frozenset((
    'candidate_busy', 'candidate_file_changed', 'candidate_io_failed', 'candidate_storage_full', 'migration_body_limit',
    'migration_candidate_invalid', 'migration_copy_incomplete', 'migration_verification_incomplete',
    'migration_restart_pending', 'migration_commitment_mismatch',
    'migration_publication_unrecorded',
    'migration_source_revision_unproved',
    'migration_endpoint_changed', 'migration_intent_changed',
    'migration_intent_invalid', 'migration_member_changed', 'migration_route_changed',
    'migration_snapshot_changed', 'migration_worktree_changed', 'reader_snapshot_deadline',
    'migration_cursor_invalid', 'migration_duplicate_wiki_path', 'migration_membership_invalid',
    'migration_membership_limit', 'migration_sidecar_invalid', 'migration_staged_missing',
    'migration_lexical_queries_invalid', 'migration_lexical_query_unproved',
    'migration_lexical_sidecar_unproved', 'migration_lexical_probe_mismatch',
    'migration_lexical_limit_unproved', 'migration_lexical_parity_mismatch',
    'migration_recall_probe_unproved', 'migration_recall_parity_mismatch',
    'reader_component_changed', 'reader_lifetime_busy', 'reader_selection_changed',
    'reader_selection_unproved', 'reader_snapshot_changed', 'reader_store_changed',
    'reader_sidecar_revision_unproved', 'fts_deadline_exceeded'))
_MIGRATION_REASONS |= frozenset(('sync_owner_busy', 'migration_parity_incomplete',
                                 'migration_space_deferred', 'migration_checkout_changed'))
_MIGRATION_REASONS |= frozenset(('switch_parity_missing', 'switch_catchup_stale', 'switch_bridge_missing',
                                 'switch_hosts_unsupported', 'switch_bundle_mismatch', 'switch_lifecycle_busy',
                                 'switch_layout_not_current', 'switch_selection_changed',
                                 'switch_reader_ready_stale', 'initial_build_busy', 'learning_compute_busy',
                                 'reader_hosts_pending', 'reader_bridge_unproved', 'stale_reader_cas',
                                 'reader_transition_busy', 'reader_head_regressed', 'reader_parity_unproved'))
_MIGRATION_REASONS |= frozenset(('migration_vector_invalid', 'migration_vector_spec_invalid',
                                 'migration_vector_queries_invalid', 'migration_vectors_incomplete',
                                 'migration_semantic_parity_mismatch'))
# Step-deadline codes of the vector seal proof and the checkout observation:
# the same grade as ``reader_snapshot_deadline`` (a driver error that rewakes
# within the error streak; a retry at the parity seal; a smaller parity page).
_STEP_DEADLINE_REASONS = frozenset(('reader_snapshot_deadline', 'vector_deadline_exceeded',
                                    'checkout_budget_exhausted'))
_MIGRATION_REASONS |= _STEP_DEADLINE_REASONS


# Operations in shadow order (``op`` of every ``migration.shadow`` event);
# the logs skill renders progress as a position in this tuple. A restart
# replays ``copy``; comparisons belong to ``parity`` and ``vectors``.
MIGRATION_OPS = ('begin', 'copy', 'verification', 'base_corpus', 'sidecar_corpus',
                 'prepare', 'publish', 'vectors', 'parity', 'parity_seal', 'reader_ready', 'switch')
# Reasons that leave intent and candidate as they were and only ask a later
# step to try again; their terminal event outcome is ``deferred``.
_DEFERRED_REASONS = frozenset(('migration_space_deferred',))
# Switch gates that do not hold yet (receipts, catch-up, hosts, layout,
# lifecycle): nothing is written and a later pass proves them again. Their
# terminal event outcome is ``waiting``, never ``error``.
_WAITING_REASONS = frozenset(reason for reason in _MIGRATION_REASONS if reason.startswith('switch_'))
# Integer diagnostics a terminal event may carry (bounded; never text).
_EVENT_FIELDS = ('needed_bytes', 'free_bytes', 'quiesce_ms', 'page_members',
                 'capture_ms', 'pin_verify_ms', 'inspect_ms', 'members')
# One step's published-proof diagnostics (``advance_shadow``): the unlocked
# preproof's code and ms, and whether the locked step reused a proof.
_STEP_PROOF = ContextVar('laya_shadow_step_proof', default=None)

# Space pre-check (design 10: old DB + new DB + WAL + reserve, from actual
# file sizes and the expected copy size; no fixed 2x guarantee is claimed).
# The candidate copies the wiki members of the selected v6 store (bodies,
# FTS and compatible vectors), so the v6 DB and its WAL bound the final
# candidate size; raw collections in the v6 DB make that an over-estimate,
# never an under-estimate of one generation. Bytes still to come are that
# bound minus what the candidate DB and WAL already hold. One step writes at
# most MAX_BATCH_BYTES of bodies plus their FTS and index pages into the
# WAL before a checkpoint; twice the batch covers both. The reserve keeps
# the copy from taking the filesystem's last blocks. Measured sizes (U8, n)
# may replace these provisional constants.
_SPACE_WAL_HEADROOM = 2 * MAX_BATCH_BYTES
_SPACE_RESERVE = 16 * 1024 * 1024


def _file_bytes(path):
    import os
    try:
        return os.lstat(path).st_size
    except FileNotFoundError:
        return 0


def _space_needed(source, candidate):
    """Projected bytes a copy may still write, from actual file sizes."""
    projected = sum(_file_bytes(str(source) + suffix) for suffix in ('', '-wal'))
    written = sum(_file_bytes(str(candidate) + suffix) for suffix in ('', '-wal'))
    return max(0, projected - written) + _SPACE_WAL_HEADROOM + _SPACE_RESERVE


def _space_check(trace, state, decision, store):
    """Defer the start or the next copy while the projected bytes do not fit.

    Read-only: a deferral writes nothing to the intent or the candidate.
    """
    import os
    stat = os.statvfs(state)
    free = stat.f_bavail * stat.f_frsize
    needed = _space_needed(decision['dbPath'], store._path)
    if free < needed:
        trace['stage'] = 'space'
        trace['fields'] = {'needed_bytes': needed, 'free_bytes': free}
        raise IndexContractError('migration_space_deferred')


def _migration_event(state, trace, outcome, *, reason=None, ms=None):
    try:
        import event_log
        fields = {key: value for key, value in (trace.get('fields') or {}).items()
                  if key in _EVENT_FIELDS and type(value) in (int, float)}
        proof = _STEP_PROOF.get()
        if proof is not None:
            fields['preproof'] = event_log.code(proof['preproof'])
            if type(proof['preproof_ms']) is int:
                fields['preproof_ms'] = proof['preproof_ms']
            if proof.get('prewarm') is not None:
                fields['prewarm'] = event_log.code(proof['prewarm'])
                if type(proof.get('prewarm_ms')) is int:
                    fields['prewarm_ms'] = proof['prewarm_ms']
            lookups = proof['lookups']
            if lookups['hits'] or lookups['misses']:
                fields['proof_reused'] = int(lookups['hits'] > 0 and not lookups['misses'])
                fields['proof_misses'] = lookups['misses']
        event_log.emit('migration.shadow', state_root=state, host='worker',
            outcome=outcome, reason=reason, ms=ms, stage=trace['stage'], op=trace['op'],
            migration=trace['migration'], attempt=trace['attempt'], **fields)
    except Exception:
        pass


class _VectorsWarmed(IndexContractError):
    """A vectors step whose fixed costs spent the member budget: no member, nothing written."""

    def __init__(self):
        super().__init__('reader_snapshot_deadline')


@contextmanager
def _migration_trace(state, stage, op):
    """Emit one bounded terminal event without source text or exception messages."""
    import uuid
    if op not in MIGRATION_OPS:
        raise IndexContractError('migration_trace_invalid')
    trace = {'stage': stage, 'op': op, 'migration': None, 'outcome': None, 'attempt': uuid.uuid4().hex}
    started = time.monotonic()
    reason = None
    try:
        yield trace
    except _VectorsWarmed:
        # Not a failure: the step's fixed costs are now warm and the next
        # step of the pass runs the page (the driver bounds repeats).
        trace['outcome'], reason = 'warmed', 'reader_snapshot_deadline'
        raise
    except Exception as exc:
        trace['outcome'] = 'error'
        value = str(exc)
        if value in _MIGRATION_REASONS:
            reason = value
            if value in _DEFERRED_REASONS:
                trace['outcome'] = 'deferred'
            elif value in _WAITING_REASONS or (trace['op'] in ('reader_ready', 'switch')
                                               and value in _SWITCH_STALE):
                # A source change seen by the switch gate's catch-up recheck
                # is ``switch_catchup_stale`` to the driver: also a wait.
                trace['outcome'] = 'waiting'
        elif isinstance(exc, OSError):
            reason = 'storage_full' if exc.errno == errno.ENOSPC else 'storage_io'
        elif isinstance(exc, sqlite3.Error):
            reason = 'sqlite_error'
        else:
            reason = 'unexpected'
        raise
    finally:
        if trace['outcome'] is not None:
            _migration_event(state, trace, trace['outcome'], reason=reason or trace.get('reason'),
                             ms=(time.monotonic()-started)*1000)


def _migration_state(project, store):
    """Resolve private state read-only; the caller must recheck under locks."""
    from pathlib import Path
    from laya_blob_store import _db_file
    project = Path(project)
    if not project.is_absolute() or project.resolve(strict=True) != project:
        raise IndexContractError('migration_project_invalid')
    decision = journal.journal.select_route(str(project), action='recall')
    if decision.get('state') != 'candidate' or decision.get('projectRoot') != str(project):
        raise IndexContractError('migration_route_invalid')
    state = journal._state(Path(decision['journalPath']).parent)
    if store._path.parent != state or store._path == Path(decision['dbPath']):
        raise IndexContractError('migration_candidate_invalid')
    node = _db_file(store._path)
    if node != store._identity:
        raise IndexContractError('candidate_file_changed')
    return project, state, decision, node


def _source_revision(snapshot):
    sidecar = snapshot.sidecar
    kind = 'absent' if sidecar is None else ('sealed' if sidecar['baseline'] else 'legacy')
    return {'generation': snapshot.published[0], 'base_revision': snapshot.base_revision,
            'sidecar_kind': kind, 'sidecar_revision': None if sidecar is None else sidecar['revision']}


def _cut_identity(base, side, decision):
    """O(1) commitment to every old v6 change the publication cut orders.

    Base: published membership, content, candidate seals, vector rows/seals
    and generation state are sealed by triggers once published, and the
    exclusions/schema_meta rowid tables are append-only, so equal head and
    max(rowid) values mean the same published image and receipt chain.
    Sidecar: every coordinated docs/docs_fts mutation appends a
    publication_revisions row in the same visible (EX cut) transaction, and
    a legacy sidecar is sealed (revision 0) before its first coordinated row.
    Writes outside the cut (staged generations, content, pins, cached cursor)
    cannot touch the published image; baseline inserts only move this value.
    """
    scope = decision['scopeId']
    head = base.execute('SELECT gen FROM head WHERE scope=?', (scope,)).fetchone()
    incarnation = base.execute("SELECT value FROM schema_meta WHERE key='incarnation'").fetchone()
    identity = {
        'format': [base.execute('PRAGMA application_id').fetchone()[0],
                   base.execute('PRAGMA user_version').fetchone()[0]],
        'incarnation': None if incarnation is None else incarnation[0],
        'head': None if head is None else head[0],
        'exclusions': base.execute('SELECT max(rowid) FROM exclusions').fetchone()[0],
        'schema_meta': base.execute('SELECT max(rowid) FROM schema_meta').fetchone()[0],
        'sidecar': None,
    }
    if side is not None:
        sealed = side.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                              "AND name='publication_revisions'").fetchone()
        identity['sidecar'] = {
            'format': side.execute('PRAGMA user_version').fetchone()[0],
            'meta': [list(row) for row in side.execute('SELECT * FROM meta')],
            'revision': (side.execute('SELECT max(revision) FROM publication_revisions').fetchone()[0]
                         if sealed else 'unsealed'),
        }
    return identity


def publication_cut_identity(snapshot, *, endpoint, decision, deadline):
    """Cut identity of an already proved pinned snapshot; grants nothing."""
    if not any(item is snapshot and bound is endpoint and saved == decision
               for item, bound, saved in readers._observations.get()):
        raise IndexContractError('reader_snapshot_unpinned')
    readers._bound(endpoint)
    base, _, _, side = snapshot.connections
    with readers._validation_deadline(snapshot.connections, deadline):
        return _cut_identity(base, side, decision)


def current_cut_identity(endpoint, *, decision, deadline):
    """Read the cut identity from fresh read-only handles.

    The caller holds the publication cut, so sidecar creation and every
    compared change are excluded while this reads; it opens no full snapshot.
    """
    import os
    from contextlib import ExitStack
    from pathlib import Path
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.monotonic() >= deadline):
        raise IndexContractError('reader_snapshot_deadline')
    readers._bound(endpoint)
    record = endpoint.record
    with journal.journal.read_only_journals():
        current = (journal._recheck(endpoint)[2]['readerDecision'] if endpoint in journal._upgrade_readers
                   else journal.journal.select_route(record['project'], action='recall'))
    if current != decision:
        raise IndexContractError('reader_selection_changed')
    with ExitStack() as stack:
        owners = []
        for role in ('base', 'sidecar'):
            if role == 'sidecar' and not os.path.lexists(Path(record['sidecar_path'])):
                owners.append(None)
                continue
            owner = readers.open_warm_component(endpoint, role, deadline=deadline)
            stack.callback(owner.close)
            owners.append(owner)
        connections = tuple(owner.db for owner in owners if owner is not None)
        for db in connections:
            db.execute('BEGIN')
            stack.callback(lambda db=db: db.rollback() if db.in_transaction else None)
        with readers._validation_deadline(connections, deadline):
            return _cut_identity(owners[0].db, None if owners[1] is None else owners[1].db, decision)


def _revision_evidence(snapshot, *, endpoint, decision, previous, deadline):
    """Classify receipt continuity and return the validated base receipts after it."""
    import laya_publication_revision as revisions
    if not any(item is snapshot and bound is endpoint and saved == decision
               for item, bound, saved in readers._observations.get()):
        raise IndexContractError('reader_snapshot_unpinned')
    readers._bound(endpoint)
    current = _source_revision(snapshot)
    old_rev, new_rev = previous['base_revision'], current['base_revision']
    base, receipts = 'unavailable', None
    if old_rev is not None and new_rev is not None:
        if new_rev < old_rev:
            base = 'gap'
        elif new_rev - old_rev <= 1024:
            prefix = revisions._PREFIX
            first, last = prefix + f'{old_rev:020d}', prefix + f'{new_rev:020d}'
            identity = revisions._identity('base', (decision['installId'], decision['scopeId'],
                                                     decision['dbIncarnation']))
            with readers._validation_deadline(snapshot.connections, deadline):
                rows = snapshot.connections[0].execute(
                    'SELECT key,value FROM schema_meta WHERE key>=? AND key<=? ORDER BY key LIMIT 1026',
                    (first, last)).fetchall()
            base = 'gap'
            if len(rows) == new_rev - old_rev + 1:
                prior_head = previous['generation']
                valid, items = True, []
                for offset, (key, raw) in enumerate(rows):
                    if time.monotonic() >= deadline:
                        raise IndexContractError('reader_snapshot_deadline')
                    try:
                        item = json.loads(raw)
                        valid = (isinstance(item, dict) and key == prefix + f'{old_rev + offset:020d}'
                                 and item.get('component') == 'base' and item.get('identity') == identity
                                 and json.dumps(item, sort_keys=True, separators=(',', ':'),
                                                ensure_ascii=True) == raw)
                        if offset == 0:
                            valid = valid and ((item.get('head') if old_rev == 0 else item.get('after')) == prior_head)
                        else:
                            digest = item.get('changeDigest')
                            valid = (valid and set(item) == {'component', 'identity', 'before', 'after',
                                                             'changeDigest', 'exclusionCount'}
                                     and item['before'] == prior_head and isinstance(item['after'], str)
                                     and bool(item['after']) and type(item['exclusionCount']) is int
                                     and item['exclusionCount'] >= 0 and isinstance(digest, str)
                                     and re.fullmatch(r'[0-9a-f]{64}', digest) is not None)
                            prior_head = item['after']
                            items.append(item)
                    except (KeyError, TypeError, ValueError):
                        valid = False
                    if not valid:
                        break
                if valid and prior_head == current['generation']:
                    base = 'unchanged' if new_rev == old_rev else 'continuous'
                    receipts = items
    old_side, new_side = previous['sidecar_revision'], current['sidecar_revision']
    sidecar = 'unavailable'
    if previous['sidecar_kind'] == current['sidecar_kind'] == 'sealed':
        sidecar = ('regressed' if new_side < old_side else
                   'unchanged' if new_side == old_side else 'advanced')
    elif previous['sidecar_kind'] == current['sidecar_kind'] == 'absent':
        sidecar = 'unchanged'
    return {'base': base, 'sidecar': sidecar}, receipts


def _revision_check(snapshot, *, endpoint, decision, previous, deadline):
    """Classify receipt continuity only; this check alone authorizes no reuse."""
    return _revision_evidence(snapshot, endpoint=endpoint, decision=decision,
                              previous=previous, deadline=deadline)[0]


class _ReceiptUnresolved(Exception):
    """The writer receipts do not name every changed path; recapture fully."""


def _revision_changes(snapshot, *, decision, previous, check, receipts, deadline):
    """Name the paths behind continuous writer receipts, or ``None``.

    Every path is proved against the writer's own ``changeDigest``:
    exclusions against the append-only exclusion rows, sidecar transitions
    against the row digests, and a base head transition by the membership
    difference of its two immutable generations. Extra paths only cost
    replay; an unexplained receipt falls back to the full recapture.
    """
    import laya_publication_revision as revisions
    if (check['base'] not in ('unchanged', 'continuous') or receipts is None
            or check['sidecar'] not in ('unchanged', 'advanced')):
        return None
    base, _, _, side = snapshot.connections
    scope = decision['scopeId']
    prefixes = tuple(name + '/' for name in wiki_index_roots(decision['collections']))
    effective, base_paths, side_paths = set(), set(), set()

    def tick():
        if time.monotonic() >= deadline:
            raise IndexContractError('reader_snapshot_deadline')
        if len(effective | base_paths | side_paths) > MAX_CATCHUP_PATHS:
            raise _ReceiptUnresolved()

    def digest_of(value):
        raw = json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
        return hashlib.sha256(raw.encode('ascii')).hexdigest()

    def hex_or_none(value):
        return value is None or (isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None)

    try:
        with readers._validation_deadline(snapshot.connections, deadline):
            if check['sidecar'] == 'advanced':
                old_rev = previous['sidecar_revision']
                new_rev = snapshot.sidecar['revision']
                if side is None or new_rev - old_rev > MAX_CATCHUP_PATHS:
                    return None
                rows = side.execute('SELECT revision,value FROM publication_revisions '
                                    'WHERE revision>? AND revision<=? ORDER BY revision',
                                    (old_rev, new_rev)).fetchall()
                if [row[0] for row in rows] != list(range(old_rev + 1, new_rev + 1)):
                    return None
                items = []
                for _, raw in rows:
                    item = json.loads(raw)
                    if (not isinstance(item, dict) or item.get('component') != 'sidecar'
                            or not hex_or_none(item.get('before')) or not hex_or_none(item.get('after'))
                            or item.get('before') == item.get('after')
                            or not isinstance(item.get('changeDigest'), str)):
                        return None
                    items.append(item)
                known = {}
                for row in side.execute('SELECT path,hash,title,body,profile_id,chunk_count,scan_id FROM docs'):
                    if time.monotonic() >= deadline:
                        raise IndexContractError('reader_snapshot_deadline')
                    known[revisions._sidecar_row_digest(tuple(row))] = row[0]
                for item in reversed(items):
                    path = known.get(item['after']) if item['after'] is not None else None
                    if path is None:
                        # A deleted row names its path only through the digest.
                        candidates = sorted(side_paths) + [row[0] for row in base.execute(
                            'SELECT path FROM membership WHERE gen=? AND scope=? ORDER BY path',
                            (snapshot.published[0], scope))]
                        path = next((candidate for candidate in candidates if digest_of(
                            ['sidecar', candidate, item['before'], item['after']]) == item['changeDigest']), None)
                    if path is None or digest_of(['sidecar', path, item['before'],
                                                  item['after']]) != item['changeDigest']:
                        return None
                    side_paths.add(path)
                    if item['before'] is not None:
                        known[item['before']] = path
                    tick()
            exclusion_digests = {}
            for item in receipts:
                tick()
                if item['before'] != item['after']:
                    if item['before'] is None:
                        return None
                    gens = base.execute('SELECT COUNT(*) FROM generations WHERE scope=? AND gen IN (?,?)',
                                        (scope, item['before'], item['after'])).fetchone()[0]
                    if gens != 2:
                        return None
                    diff = base.execute(
                        'SELECT path FROM (SELECT path,hash,eligible FROM membership WHERE gen=? AND scope=? '
                        'EXCEPT SELECT path,hash,eligible FROM membership WHERE gen=? AND scope=?) '
                        'UNION SELECT path FROM (SELECT path,hash,eligible FROM membership WHERE gen=? AND scope=? '
                        'EXCEPT SELECT path,hash,eligible FROM membership WHERE gen=? AND scope=?) LIMIT ?',
                        (item['before'], scope, item['after'], scope, item['after'], scope,
                         item['before'], scope, MAX_CATCHUP_PATHS + 1)).fetchall()
                    for (path,) in diff:
                        # Raw members are outside the wiki-only corpus proof.
                        if type(path) is not str or not path.startswith(prefixes):
                            return None
                        base_paths.add(path)
                    continue
                head = item['after']
                if head not in exclusion_digests:
                    table = {}
                    for kind, value in base.execute('SELECT kind,value FROM exclusions WHERE scope=?', (scope,)):
                        if time.monotonic() >= deadline:
                            raise IndexContractError('reader_snapshot_deadline')
                        table[digest_of(['exclusion', scope, head, kind, value])] = (kind, value)
                    exclusion_digests[head] = table
                found = exclusion_digests[head].get(item['changeDigest'])
                if found is None:
                    return None
                kind, value = found
                if kind == 'path':
                    hidden = ({value} if value in side_paths or base.execute(
                        'SELECT 1 FROM membership WHERE gen=? AND scope=? AND path=?',
                        (head, scope, value)).fetchone() or (side is not None and side.execute(
                        'SELECT 1 FROM docs WHERE path=?', (value,)).fetchone()) else set())
                else:
                    hidden = {row[0] for row in base.execute(
                        'SELECT path FROM membership WHERE hash=? AND gen=? AND scope=? LIMIT ?',
                        (value, head, scope, MAX_CATCHUP_PATHS + 1))}
                    if side is not None:
                        hidden |= {row[0] for row in side.execute(
                            'SELECT path FROM docs WHERE hash=? LIMIT ?', (value, MAX_CATCHUP_PATHS + 1))}
                effective |= hidden
            effective |= base_paths | side_paths
            tick()
    except _ReceiptUnresolved:
        return None
    except (json.JSONDecodeError, TypeError, KeyError):
        # Deadline and reader errors are ValueErrors too; only malformed
        # receipt values fall back here.
        return None
    effective = {path for path in effective if _wiki_card_path(path, prefixes)}
    return {'effective': sorted(effective), 'base': sorted(base_paths), 'sidecar': sorted(side_paths)}


def _static_digest(endpoint, decision):
    import laya_blob_values as values
    return values.digest('memvane-shadow-static-v1', {'endpoint': endpoint.endpoint_id,
                                                      'settings': endpoint.record['settings'],
                                                      'decision': decision})


def _build_id(intent):
    """The candidate slot id of this intent's source state.

    A slot is bound to one stage, so a source change needs a new id. A fresh
    scan uses its scan id; every receipt catch-up extends the catch-up digest
    chain, so the kept scan gets a new, replayable id per caught-up source.
    """
    import laya_blob_values as values
    catch_up = intent.get('catch_up')
    if catch_up is None:
        return intent['provenance_scan']['id']
    return values.digest('memvane-shadow-build-v1', {'scan': intent['provenance_scan']['id'],
                                                    'catch_up': catch_up['digest']})[:32]


def _check_source_revision(source):
    import laya_blob_values as values
    values.exact(source, ('generation', 'base_revision', 'sidecar_kind', 'sidecar_revision'))
    if (not isinstance(source['generation'], str) or not source['generation']
            or source['sidecar_kind'] not in ('absent', 'legacy', 'sealed')
            or (source['sidecar_kind'] == 'sealed') != (source['sidecar_revision'] is not None)):
        raise IndexContractError('migration_intent_invalid')
    if source['base_revision'] is not None:
        values.integer(source['base_revision'])
    if source['sidecar_revision'] is not None:
        values.integer(source['sidecar_revision'])


def _migration_intent(path, *, project, state, node, store):
    import laya_blob_values as values
    import laya_ledger_common as ledger
    intent = ledger._read_json(path)
    if (not isinstance(intent, dict) or not _INTENT_KEYS <= set(intent)
            or set(intent) - _INTENT_KEYS - {'verification', 'corpora', 'restart_required', 'published',
                                            'prepared', 'revision_check', 'catch_up', 'static',
                                            'vectors', 'parity', 'reader_ready', 'switched'}
            or intent['schema'] != 'memvane-shadow-migration-v1'
            or intent['project'] != str(project) or intent['state'] != str(state)
            or intent['state_node'] != journal._node(state)
            or intent['candidate'] != str(store._path) or intent['candidate_node'] != node
            or intent['candidate_incarnation'] != store._incarnation
            or intent['phase'] not in ('copying', 'copied')):
        raise IndexContractError('migration_intent_invalid')
    values.identifier(intent['migration_id'], values.UUID)
    values.identifier(intent['worktree_id'], values.UUID)
    values.identifier(intent['endpoint_id'])
    values.identifier(intent['observation'])
    source = intent['source_revision']
    _check_source_revision(source)
    if intent.get('static') is not None:
        values.identifier(intent['static'])
    catch_up = intent.get('catch_up')
    if catch_up is not None:
        values.exact(catch_up, ('migration_id', 'from', 'to', 'catch_ups', 'paths', 'digest'))
        _check_source_revision(catch_up['from'])
        values.integer(catch_up['catch_ups'], 1)
        values.integer(catch_up['paths'], 0)
        values.identifier(catch_up['digest'])
        if catch_up['migration_id'] != intent['migration_id'] or catch_up['to'] != source:
            raise IndexContractError('migration_intent_invalid')
    revision_check = intent.get('revision_check')
    if revision_check is not None:
        values.exact(revision_check, ('base', 'sidecar'))
        if (revision_check['base'] not in ('unavailable', 'gap', 'unchanged', 'continuous')
                or revision_check['sidecar'] not in ('unavailable', 'regressed', 'unchanged', 'advanced')):
            raise IndexContractError('migration_intent_invalid')
    values.exact(intent['provenance_scan'], ('id', 'observed_ns'))
    values.identifier(intent['provenance_scan']['id'], values.UUID)
    values.integer(intent['provenance_scan']['observed_ns'])
    if intent['cursor'] is not None and (not isinstance(intent['cursor'], dict)
            or intent['cursor'].get('observation') != intent['observation']):
        raise IndexContractError('migration_intent_invalid')
    if intent['phase'] == 'copying' and (intent.get('verification') is not None
                                         or intent.get('corpora') is not None):
        raise IndexContractError('migration_intent_invalid')
    if (type(intent.get('restart_required', False)) is not bool or
            (intent['phase'] == 'copying' and intent.get('restart_required', False))):
        raise IndexContractError('migration_intent_invalid')
    published = intent.get('published')
    if published is not None:
        import laya_view_values as view_values
        values.exact(published, ('migration_id', 'generation', 'view_id', 'epoch', 'observation',
                                 'scan_id', 'verification_digest', 'base_digest', 'sidecar_digest',
                                 'policy_digest', 'candidate_incarnation', 'source_revision'))
        verification, corpora = _completed_shadow_commitments(intent)
        if (not isinstance(published['generation'], str)
                or not view_values.GEN_ID.fullmatch(published['generation'])
                or published['migration_id'] != intent['migration_id']
                or published['observation'] != intent['observation']
                or published['scan_id'] != intent['provenance_scan']['id']
                or published['verification_digest'] != verification['digest']
                or published['base_digest'] != corpora['base']['digest']
                or published['sidecar_digest'] != corpora['sidecar']['digest']
                or published['policy_digest'] != store._policy
                or published['source_revision'] != source
                or published['candidate_incarnation'] != store._incarnation):
            raise IndexContractError('migration_intent_invalid')
        values.identifier(published['view_id'])
        values.integer(published['epoch'], 1)
    prepared = intent.get('prepared')
    if prepared is not None:
        import laya_view_values as view_values
        values.exact(prepared, ('migration_id', 'generation', 'observation', 'scan_id',
                                'candidate_incarnation'))
        if (intent['phase'] != 'copied' or not isinstance(prepared['generation'], str)
                or not view_values.GEN_ID.fullmatch(prepared['generation'])
                or prepared['migration_id'] != intent['migration_id']
                or prepared['observation'] != intent['observation']
                or prepared['scan_id'] != intent['provenance_scan']['id']
                or prepared['candidate_incarnation'] != store._incarnation
                or (published is not None and published['generation'] != prepared['generation'])):
            raise IndexContractError('migration_intent_invalid')
    if intent.get('vectors') is not None:
        _check_vector_record(intent['vectors'])
    if intent.get('parity') is not None:
        _check_parity_record(intent['parity'])
    if intent.get('reader_ready') is not None:
        _check_ready_record(intent['reader_ready'])
    if intent.get('switched') is not None:
        _check_switched_record(intent['switched'])
    if intent['migration_state'] not in _SHADOW_STATES or intent['migration_state'] != _shadow_state(intent):
        raise IndexContractError('migration_intent_invalid')
    return intent


# Page commitments are hash chains over page contents and the next path only;
# the source observation is bound beside them (progress observation and
# cursor), never inside the chain. ``marks`` keeps [next path, digest, rows]
# after every non-final page, so a receipt catch-up can keep the unchanged
# prefix and a fresh pass over the same source reproduces the same chain.
_PROGRESS_KEYS = ('observation', 'scan_id', 'batch_limit', 'byte_limit',
                  'cursor', 'digest', 'pages', 'rows', 'complete', 'marks')


def _progress_current(progress):
    """True for a progress this bundle can resume; others are recomputed."""
    return (isinstance(progress, dict) and set(progress) == set(_PROGRESS_KEYS)
            and progress['batch_limit'] == MAX_BATCH and progress['byte_limit'] == MAX_BATCH_BYTES)


def _verification_progress(progress, intent, *, check_limits=True, cursor_observation=None):
    import laya_blob_values as values
    if not check_limits and isinstance(progress, dict) and 'marks' not in progress:
        # Written before page marks existed; the caller restarts it.
        values.exact(progress, _PROGRESS_KEYS[:-1])
        return progress
    values.exact(progress, _PROGRESS_KEYS)
    values.identifier(progress['digest'])
    values.integer(progress['pages'], 1, MAX_DOCS + 1)
    values.integer(progress['rows'], 0, MAX_DOCS)
    cursor = progress['cursor']
    if (progress['observation'] != intent['observation']
            or progress['scan_id'] != intent['provenance_scan']['id']
            or type(progress['batch_limit']) is not int or progress['batch_limit'] < 1
            or type(progress['byte_limit']) is not int or progress['byte_limit'] < 1
            or (check_limits and (progress['batch_limit'] != MAX_BATCH
                                  or progress['byte_limit'] != MAX_BATCH_BYTES))
            or type(progress['complete']) is not bool
            or progress['complete'] != (cursor is None)
            or (cursor is not None and (type(cursor) is not dict
                or set(cursor) != {'after', 'observation'}
                or not _valid_relative_path(cursor['after'])
                or cursor['observation'] != (intent['observation'] if cursor_observation is None
                                               else cursor_observation)))):
        raise IndexContractError('migration_intent_invalid')
    marks = progress['marks']
    if type(marks) is not list or len(marks) != progress['pages'] - int(progress['complete']):
        raise IndexContractError('migration_intent_invalid')
    previous_path, previous_rows = None, 0
    for mark in marks:
        if (type(mark) is not list or len(mark) != 3 or type(mark[0]) is not str
                or not _valid_relative_path(mark[0])
                or (previous_path is not None and mark[0] <= previous_path)
                or type(mark[2]) is not int or not previous_rows <= mark[2] <= progress['rows']):
            raise IndexContractError('migration_intent_invalid')
        values.identifier(mark[1])
        previous_path, previous_rows = mark[0], mark[2]
    if cursor is not None and marks[-1] != [cursor['after'], progress['digest'], progress['rows']]:
        raise IndexContractError('migration_intent_invalid')
    return progress


def _initial_verification(intent):
    import laya_blob_values as values
    return {'observation': intent['observation'], 'scan_id': intent['provenance_scan']['id'],
        'batch_limit': MAX_BATCH, 'byte_limit': MAX_BATCH_BYTES,
        'cursor': None, 'digest': values.digest('memvane-shadow-verification-v2', {
            'migration': intent['migration_id'], 'scan': intent['provenance_scan'],
            'batch_limit': MAX_BATCH, 'byte_limit': MAX_BATCH_BYTES}),
        'pages': 0, 'rows': 0, 'complete': False, 'marks': []}


def _fold_page(progress, domain, content, next_cursor, rows):
    import laya_blob_values as values
    if rows > MAX_DOCS or progress['pages'] >= MAX_DOCS + 1:
        raise IndexContractError('migration_membership_limit')
    after = None if next_cursor is None else next_cursor['after']
    digest = values.digest(domain, {'previous': progress['digest'], **content, 'next': after})
    return {**progress, 'cursor': next_cursor, 'pages': progress['pages'] + 1,
        'rows': rows, 'complete': next_cursor is None, 'digest': digest,
        'marks': progress['marks'] + ([] if after is None else [[after, digest, rows]])}


def _fold_verification(progress, page):
    return _fold_page(progress, 'memvane-shadow-verification-page-v2',
                      {'inventory': page['inventory'], 'trusted': page['trusted']},
                      page['next'], progress['rows'] + len(page['inventory']))


def _initial_corpus(intent, key, corpus_observation):
    import laya_blob_values as values
    return {'observation': intent['observation'], 'scan_id': intent['provenance_scan']['id'],
        'batch_limit': MAX_BATCH, 'byte_limit': MAX_BATCH_BYTES,
        'cursor': None, 'pages': 0, 'rows': 0, 'complete': False, 'marks': [],
        'digest': values.digest('memvane-shadow-corpus-v2', {
            'migration': intent['migration_id'], 'corpus': key,
            'batch_limit': MAX_BATCH, 'byte_limit': MAX_BATCH_BYTES})}


def _fold_corpus(progress, page):
    return _fold_page(progress, 'memvane-shadow-corpus-page-v2', {'corpus': page['corpus']},
                      page['next'], progress['rows'] + len(page['corpus']))


def _corpus_observation(key, observation):
    prefix = 'memvane-sidecar-fts-corpus-v1:' if key == 'sidecar' else 'memvane-base-fts-corpus-v1:'
    return hashlib.sha256((prefix + observation).encode('ascii')).hexdigest()


def _replay_progress(progress, first, observation, cursor_observation):
    """Rebind a page chain to a new observation, keeping pages before ``first``.

    A kept page must end before ``first`` and so must the page after it: a
    page boundary also depends on the next path (count and byte limits), so the
    last page ending before ``first`` is replayed too. ``None`` restarts it.
    """
    if progress is None:
        return None
    if first is None:
        cursor = progress['cursor']
        return {**progress, 'observation': observation,
                'cursor': None if cursor is None else {'after': cursor['after'],
                                                       'observation': cursor_observation}}
    kept = [mark for mark in progress['marks'] if mark[0] < first][:-1]
    if not kept:
        return None
    after, digest, rows = kept[-1]
    return {**progress, 'observation': observation, 'digest': digest, 'rows': rows,
            'pages': len(kept), 'complete': False, 'marks': kept,
            'cursor': {'after': after, 'observation': cursor_observation}}


def begin_migration(store, *, worktree_id, project, executing_root, deadline):
    """Create a private copy intent; never switch the selected reader."""
    import uuid
    from pathlib import Path
    import project_dir
    import laya_fresh_wiki_origin as fresh
    import laya_ledger_common as ledger
    from laya_product_install import _install_lock

    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.monotonic() >= deadline):
        raise IndexContractError('reader_snapshot_deadline')
    project, state, decision, node = _migration_state(project, store)
    marker = project_dir.runtime_file(project, fresh.MARKER)
    with _migration_trace(state, 'lock', 'begin') as trace, _install_lock(state), ledger._operation_lock(marker):
        if _migration_state(project, store) != (project, state, decision, node):
            raise IndexContractError('migration_route_changed')
        _space_check(trace, state, decision, store)
        trace['stage'] = 'endpoint'
        with leases.resolve_endpoint(project, action='recall', host_root=Path(executing_root)) as endpoint:
            if endpoint.record['state'] != str(state) or endpoint.record['scope'] != store._scope:
                raise IndexContractError('migration_endpoint_changed')
            trace['stage'] = 'snapshot'
            with readers.inspect_warm_snapshot(endpoint, deadline=deadline) as snapshot:
                page = capture_effective(snapshot, endpoint=endpoint, decision=decision,
                                         after=None, deadline=deadline)
                import laya_blob_values as values
                values.identifier(worktree_id, values.UUID)
                with store._session() as db:
                    saved = db.execute('SELECT project_root FROM worktrees WHERE worktree_id=?',
                                       (worktree_id,)).fetchone()
                if saved is None or saved[0] != str(project):
                    raise IndexContractError('migration_worktree_changed')
                intent = {'schema': 'memvane-shadow-migration-v1', 'migration_id': uuid.uuid4().hex,
                    'project': str(project), 'state': str(state), 'state_node': journal._node(state),
                    'install_id': endpoint.record['install_id'], 'endpoint_id': endpoint.endpoint_id,
                    'observation': page['observation'], 'source_revision': _source_revision(snapshot),
                    'candidate': str(store._path),
                    'candidate_node': node, 'candidate_incarnation': store._incarnation,
                    'worktree_id': worktree_id, 'phase': 'copying', 'cursor': None,
                    'verification': None, 'corpora': None, 'restart_required': False,
                    'static': _static_digest(endpoint, decision),
                    'provenance_scan': {'id': uuid.uuid4().hex, 'observed_ns': time.time_ns()}}
                intent = _stamp(intent)
                trace['migration'] = intent['migration_id']
                _migration_event(state, trace, 'attempt')
                trace['stage'] = 'snapshot_postcheck'
        # A failed snapshot post-check must not leave a successful begin intent.
        trace['stage'] = 'intent'
        ledger._write(state / _INTENT, intent, replace=False)
        trace.update(migration=intent['migration_id'], outcome='started')
        return intent


def advance_migration(store, *, project, executing_root, deadline):
    """Replay a bounded copy and advance its intent only after blob commit."""
    from pathlib import Path
    import project_dir
    import laya_fresh_wiki_origin as fresh
    import laya_ledger_common as ledger
    from laya_product_install import _install_lock

    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.monotonic() >= deadline):
        raise IndexContractError('reader_snapshot_deadline')
    deadline = min(deadline, time.monotonic() + _STEP_SECONDS)
    project, state, decision, node = _migration_state(project, store)
    marker = project_dir.runtime_file(project, fresh.MARKER)
    with _migration_trace(state, 'lock', 'copy') as trace, _install_lock(state), ledger._operation_lock(marker):
        if _migration_state(project, store) != (project, state, decision, node):
            raise IndexContractError('migration_route_changed')
        trace['stage'] = 'intent'
        path = state / _INTENT
        intent = _migration_intent(path, project=project, state=state, node=node, store=store)
        trace['migration'] = intent['migration_id']
        _migration_event(state, trace, 'attempt')
        # Every path below may import pages (copy, catch-up, full restart).
        _space_check(trace, state, decision, store)
        trace['stage'] = 'endpoint'
        with leases.resolve_endpoint(project, action='recall', host_root=Path(executing_root)) as endpoint:
            if (endpoint.endpoint_id != intent['endpoint_id']
                    or endpoint.record['install_id'] != intent['install_id']):
                raise IndexContractError('migration_endpoint_changed')
            trace['stage'] = 'snapshot'
            with readers.inspect_warm_snapshot(endpoint, deadline=deadline) as snapshot:
                def restart(observation):
                    """Catch up from continuous writer receipts, else recapture fully.

                    The catch-up keeps the scan and every page commitment before
                    the first changed path, stages the changed paths the copy
                    already passed, and binds the replayed revision range in
                    ``catch_up``. That receipt is not parity: the candidate
                    build still re-proves every page against the live source.
                    """
                    import uuid
                    import laya_blob_values as values
                    check, receipts = _revision_evidence(snapshot, endpoint=endpoint, decision=decision,
                                                         previous=intent['source_revision'], deadline=deadline)
                    current = _source_revision(snapshot)
                    static = _static_digest(endpoint, decision)
                    corpora = intent.get('corpora')
                    progresses = [intent.get('verification')] + ([] if corpora is None else
                                                                  [corpora['base'], corpora['sidecar']])
                    changes = None
                    # A prepared or published slot of the kept scan does not
                    # block a catch-up: the caught-up source builds under a new
                    # _build_id, and the old slot stays private.
                    if (intent.get('static') == static
                            and all(item is None or _progress_current(item) for item in progresses)):
                        changes = _revision_changes(snapshot, decision=decision,
                            previous=intent['source_revision'], check=check, receipts=receipts,
                            deadline=deadline)
                    if changes is None:
                        if check['base'] == 'gap':
                            trace['reason'] = 'revision_gap'
                        return _stamp({**intent, 'observation': observation, 'cursor': None, 'phase': 'copying',
                                'source_revision': current, 'revision_check': check, 'static': static,
                                'verification': None, 'corpora': None, 'restart_required': False,
                                'published': None, 'prepared': None, 'catch_up': None,
                                'provenance_scan': {'id': uuid.uuid4().hex, 'observed_ns': time.time_ns()}}), 'restarted'
                    trace['stage'] = 'catch_up'
                    cursor = intent['cursor']
                    # Paths after the copy cursor are copied by the remaining pages.
                    targets = [path for path in changes['effective'] if intent['phase'] == 'copied'
                               or (cursor is not None and path <= cursor['after'])]
                    after, only = None, frozenset(targets)
                    while targets:
                        batch = import_wiki_batch(store, worktree_id=intent['worktree_id'], snapshot=snapshot,
                            endpoint=endpoint, decision=decision, after=after, deadline=deadline,
                            provenance_scan=intent['provenance_scan'], _expected_observation=observation,
                            _only=only)
                        after = batch['next']
                        if after is None:
                            break
                    if corpora is not None:
                        corpora = {**corpora, **{key: _replay_progress(corpora[key], (changes[key] or [None])[0],
                                                                       observation, _corpus_observation(key, observation))
                                                 for key in ('base', 'sidecar')}}
                    verification = _replay_progress(intent.get('verification'), (changes['effective'] or [None])[0],
                                                    observation, observation)
                    previous = intent.get('catch_up')
                    touched = set(changes['effective']) | set(changes['base']) | set(changes['sidecar'])
                    receipt = {'migration_id': intent['migration_id'],
                               'from': intent['source_revision'] if previous is None else previous['from'],
                               'to': current, 'catch_ups': 1 if previous is None else previous['catch_ups'] + 1,
                               'paths': len(touched) + (0 if previous is None else previous['paths']),
                               'digest': values.digest('memvane-shadow-catch-up-v1', {
                                   'previous': None if previous is None else previous['digest'],
                                   'from': intent['source_revision'], 'to': current, 'changes': changes})}
                    return _stamp({**intent, 'observation': observation, 'source_revision': current,
                            'revision_check': check, 'restart_required': False,
                            'cursor': None if cursor is None else {'after': cursor['after'], 'observation': observation},
                            'verification': verification, 'corpora': corpora, 'published': None, 'prepared': None,
                            'catch_up': receipt}), 'catch_up'
                if intent['phase'] == 'copied':
                    observed = capture_effective(snapshot, endpoint=endpoint, decision=decision,
                                                 after=None, deadline=deadline)
                    if observed['observation'] != intent['observation']:
                        updated, outcome = restart(observed['observation'])
                    elif _source_revision(snapshot) != intent['source_revision']:
                        raise IndexContractError('migration_source_revision_unproved')
                    else:
                        progress = intent.get('verification')
                        if progress is not None:
                            _verification_progress(progress, intent, check_limits=False)
                        if progress is not None and not _progress_current(progress):
                            updated, outcome = _stamp({**intent, 'verification': None, 'corpora': None,
                                                       'restart_required': False, 'published': None,
                                                       'prepared': None}), 'restarted'
                        elif intent.get('restart_required', False):
                            updated, outcome = {**intent, 'restart_required': False}, 'copied'
                        else:
                            updated, outcome = intent, 'copied'
                else:
                    trace['stage'] = 'copy'
                    try:
                        batch = import_wiki_batch(store, worktree_id=intent['worktree_id'], snapshot=snapshot,
                            endpoint=endpoint, decision=decision, after=intent['cursor'], deadline=deadline,
                            provenance_scan=intent['provenance_scan'],
                            _expected_observation=intent['observation'])
                    except _MigrationObservationChanged as changed:
                        # The bounded, pinned capture proved a different source;
                        # catch up or replay all paths, never publish the stale cursor.
                        updated, outcome = restart(changed.observation)
                    else:
                        updated = {**intent, 'cursor': batch['next'],
                                   'phase': 'copied' if batch['next'] is None else 'copying'}
                        outcome = 'copied' if updated['phase'] == 'copied' else 'progress'
                trace['stage'] = 'snapshot_postcheck'
        # Snapshot and endpoint post-checks must succeed before advancing the cursor.
        if updated != intent:
            if time.monotonic() >= deadline:
                raise IndexContractError('reader_snapshot_deadline')
            if ledger._read_json(path) != intent:
                raise IndexContractError('migration_intent_changed')
            if time.monotonic() >= deadline:
                raise IndexContractError('reader_snapshot_deadline')
            trace['stage'] = 'intent'
            ledger._write(path, updated, replace=True)
        trace['outcome'] = outcome
        return updated


def advance_verification(store, *, project, executing_root, deadline):
    """Commit one source-bound mapping verification page to the copy intent."""
    from pathlib import Path
    import laya_blob_values as values
    import laya_fresh_wiki_origin as fresh
    import laya_ledger_common as ledger
    import project_dir
    from laya_product_install import _install_lock

    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.monotonic() >= deadline):
        raise IndexContractError('reader_snapshot_deadline')
    deadline = min(deadline, time.monotonic() + _STEP_SECONDS)
    project, state, decision, node = _migration_state(project, store)
    marker = project_dir.runtime_file(project, fresh.MARKER)
    with _migration_trace(state, 'lock', 'verification') as trace, _install_lock(state), ledger._operation_lock(marker):
        if _migration_state(project, store) != (project, state, decision, node):
            raise IndexContractError('migration_route_changed')
        path = state / _INTENT
        intent = _migration_intent(path, project=project, state=state, node=node, store=store)
        trace['migration'] = intent['migration_id']
        _migration_event(state, trace, 'attempt')
        if intent.get('restart_required', False):
            raise IndexContractError('migration_restart_pending')
        if intent['phase'] != 'copied' or intent['cursor'] is not None:
            raise IndexContractError('migration_copy_incomplete')
        progress = intent.get('verification')
        if progress is None:
            progress = _initial_verification(intent)
        else:
            _verification_progress(progress, intent)
        trace['stage'] = 'endpoint'
        with leases.resolve_endpoint(project, action='recall', host_root=Path(executing_root)) as endpoint:
            if (endpoint.endpoint_id != intent['endpoint_id']
                    or endpoint.record['install_id'] != intent['install_id']):
                raise IndexContractError('migration_endpoint_changed')
            trace['stage'] = 'snapshot'
            if progress['complete']:
                with readers.inspect_warm_snapshot(endpoint, deadline=deadline) as snapshot:
                    current = capture_effective(snapshot, endpoint=endpoint, decision=decision,
                                                after=None, deadline=deadline)
                    if current['observation'] != intent['observation']:
                        raise IndexContractError('migration_snapshot_changed')
                updated = intent
            else:
                page = verify_migration_page(store, worktree_id=intent['worktree_id'], endpoint=endpoint,
                    decision=decision, provenance_scan=intent['provenance_scan'],
                    observation=intent['observation'], cursor=progress['cursor'], deadline=deadline)
                folded = _fold_verification(progress, page)
                updated = {**intent, 'verification': folded}
                if folded['complete'] and intent.get('corpora') is not None:
                    # Corpus chains do not depend on the verification chain; a
                    # catch-up kept them, so the completed mapping rebinds them.
                    updated['corpora'] = {**intent['corpora'], 'verification_digest': folded['digest']}
        if time.monotonic() >= deadline:
            raise IndexContractError('reader_snapshot_deadline')
        if updated != intent:
            if ledger._read_json(path) != intent:
                raise IndexContractError('migration_intent_changed')
            if time.monotonic() >= deadline:
                raise IndexContractError('reader_snapshot_deadline')
            trace['stage'] = 'intent'
            ledger._write(path, updated, replace=True)
        trace['outcome'] = 'verified' if updated['verification']['complete'] else 'progress'
        return updated


def advance_corpus(store, *, project, executing_root, sidecar, deadline):
    """Stage one old FTS corpus page and persist only its progress commitment."""
    from pathlib import Path
    import laya_blob_values as values
    import laya_fresh_wiki_origin as fresh
    import laya_ledger_common as ledger
    import project_dir
    from laya_product_install import _install_lock

    if type(sidecar) is not bool:
        raise IndexContractError('migration_verify_invalid')
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.monotonic() >= deadline):
        raise IndexContractError('reader_snapshot_deadline')
    deadline = min(deadline, time.monotonic() + _STEP_SECONDS)
    project, state, decision, node = _migration_state(project, store)
    marker = project_dir.runtime_file(project, fresh.MARKER)
    op = 'sidecar_corpus' if sidecar else 'base_corpus'
    with _migration_trace(state, 'lock', op) as trace, _install_lock(state), ledger._operation_lock(marker):
        if _migration_state(project, store) != (project, state, decision, node):
            raise IndexContractError('migration_route_changed')
        path = state / _INTENT
        intent = _migration_intent(path, project=project, state=state, node=node, store=store)
        trace['migration'] = intent['migration_id']
        _migration_event(state, trace, 'attempt')
        if intent.get('restart_required', False):
            raise IndexContractError('migration_restart_pending')
        verification = intent.get('verification')
        if intent['phase'] != 'copied' or verification is None:
            raise IndexContractError('migration_verification_incomplete')
        _verification_progress(verification, intent)
        if not verification['complete']:
            raise IndexContractError('migration_verification_incomplete')
        corpora = intent.get('corpora')
        if corpora is None:
            corpora = {'verification_digest': verification['digest'], 'base': None, 'sidecar': None}
        else:
            values.exact(corpora, ('verification_digest', 'base', 'sidecar'))
            if corpora['verification_digest'] != verification['digest']:
                raise IndexContractError('migration_intent_invalid')
        key = 'sidecar' if sidecar else 'base'
        prefix = 'memvane-sidecar-fts-corpus-v1:' if sidecar else 'memvane-base-fts-corpus-v1:'
        corpus_observation = hashlib.sha256((prefix + intent['observation']).encode('ascii')).hexdigest()
        progress = corpora[key]
        if progress is None:
            progress = _initial_corpus(intent, key, corpus_observation)
        else:
            _verification_progress(progress, intent, cursor_observation=corpus_observation)
        trace['stage'] = 'endpoint'
        with leases.resolve_endpoint(project, action='recall', host_root=Path(executing_root)) as endpoint:
            if (endpoint.endpoint_id != intent['endpoint_id']
                    or endpoint.record['install_id'] != intent['install_id']):
                raise IndexContractError('migration_endpoint_changed')
            trace['stage'] = 'snapshot'
            if progress['complete']:
                with readers.inspect_warm_snapshot(endpoint, deadline=deadline) as snapshot:
                    current = capture_effective(snapshot, endpoint=endpoint, decision=decision,
                                                after=None, deadline=deadline)
                    if current['observation'] != intent['observation']:
                        raise IndexContractError('migration_snapshot_changed')
                updated = intent
            else:
                page = stage_corpus_page(store, worktree_id=intent['worktree_id'], endpoint=endpoint,
                    decision=decision, observation=intent['observation'], cursor=progress['cursor'],
                    deadline=deadline, sidecar=sidecar)
                updated = {**intent, 'corpora': {**corpora, key: _fold_corpus(progress, page)}}
        if time.monotonic() >= deadline:
            raise IndexContractError('reader_snapshot_deadline')
        if updated != intent:
            if ledger._read_json(path) != intent:
                raise IndexContractError('migration_intent_changed')
            if time.monotonic() >= deadline:
                raise IndexContractError('reader_snapshot_deadline')
            trace['stage'] = 'intent'
            ledger._write(path, updated, replace=True)
        trace['outcome'] = 'staged' if updated['corpora'][key]['complete'] else 'progress'
        return updated


def _mark_shadow_restart(store, project, expected, deadline, reason=None):
    """Persist a non-authoritative retry request after a source mismatch."""
    import laya_fresh_wiki_origin as fresh
    import laya_ledger_common as ledger
    import project_dir
    from laya_product_install import _install_lock
    project, state, decision, node = _migration_state(project, store)
    marker = project_dir.runtime_file(project, fresh.MARKER)
    with _migration_trace(state, 'restart_pending', 'copy') as trace, _install_lock(state), ledger._operation_lock(marker):
        if _migration_state(project, store) != (project, state, decision, node):
            raise IndexContractError('migration_route_changed')
        path = state / _INTENT
        current = _migration_intent(path, project=project, state=state, node=node, store=store)
        trace['migration'] = current['migration_id']
        _migration_event(state, trace, 'attempt')
        if current != expected or current['phase'] != 'copied':
            raise IndexContractError('migration_intent_changed')
        if time.monotonic() >= deadline:
            raise IndexContractError('reader_snapshot_deadline')
        updated = _stamp({**current, 'restart_required': True, 'published': None, 'prepared': None})
        ledger._write(path, updated, replace=True)
        trace.update(outcome='restart_pending', reason=reason)
        return updated


def _record_prepared(store, project, expected, generation, deadline):
    """Point the intent at a proved private candidate; this grants no parity."""
    import laya_fresh_wiki_origin as fresh
    import laya_ledger_common as ledger
    import project_dir
    from laya_product_install import _install_lock
    project, state, decision, node = _migration_state(project, store)
    marker = project_dir.runtime_file(project, fresh.MARKER)
    with _migration_trace(state, 'prepared', 'prepare') as trace, _install_lock(state), ledger._operation_lock(marker):
        if _migration_state(project, store) != (project, state, decision, node):
            raise IndexContractError('migration_route_changed')
        path = state / _INTENT
        current = _migration_intent(path, project=project, state=state, node=node, store=store)
        trace['migration'] = current['migration_id']
        _migration_event(state, trace, 'attempt')
        if current != expected or current.get('prepared') is not None or current.get('published') is not None:
            raise IndexContractError('migration_intent_changed')
        _completed_shadow_commitments(current)
        updated = _stamp({**current, 'prepared': {
            'migration_id': current['migration_id'], 'generation': generation,
            'observation': current['observation'], 'scan_id': current['provenance_scan']['id'],
            'candidate_incarnation': store._incarnation}})
        if time.monotonic() >= deadline:
            raise IndexContractError('reader_snapshot_deadline')
        ledger._write(path, updated, replace=True)
        trace['outcome'] = 'prepared'
        return updated


def _completed_shadow_commitments(intent):
    import laya_blob_values as values
    if intent['phase'] != 'copied' or intent.get('restart_required', False):
        raise IndexContractError('migration_verification_incomplete')
    verification, corpora = intent.get('verification'), intent.get('corpora')
    if verification is None or corpora is None:
        raise IndexContractError('migration_verification_incomplete')
    _verification_progress(verification, intent)
    values.exact(corpora, ('verification_digest', 'base', 'sidecar'))
    if not verification['complete'] or corpora['verification_digest'] != verification['digest']:
        raise IndexContractError('migration_verification_incomplete')
    for key in ('base', 'sidecar'):
        progress = corpora[key]
        if progress is None:
            raise IndexContractError('migration_verification_incomplete')
        prefix = ('memvane-sidecar-fts-corpus-v1:' if key == 'sidecar'
                  else 'memvane-base-fts-corpus-v1:')
        observed = hashlib.sha256((prefix + intent['observation']).encode('ascii')).hexdigest()
        _verification_progress(progress, intent, cursor_observation=observed)
        if not progress['complete']:
            raise IndexContractError('migration_verification_incomplete')
    return verification, corpora


def prepare_shadow_generation(store, *, project, executing_root, deadline):
    """Prepare a non-authoritative candidate without holding installer locks during the build."""
    from contextlib import ExitStack
    from pathlib import Path
    import laya_fresh_wiki_origin as fresh
    import laya_ledger_common as ledger
    import project_dir
    from laya_product_install import _install_lock

    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.monotonic() >= deadline):
        raise IndexContractError('reader_snapshot_deadline')
    project, state, decision, node = _migration_state(project, store)
    marker = project_dir.runtime_file(project, fresh.MARKER)
    path = state / _INTENT
    with _migration_trace(state, 'prepare', 'prepare') as trace, ExitStack() as lifetime:
        with _install_lock(state), ledger._operation_lock(marker):
            if _migration_state(project, store) != (project, state, decision, node):
                raise IndexContractError('migration_route_changed')
            intent = _migration_intent(path, project=project, state=state, node=node, store=store)
            trace['migration'] = intent['migration_id']
            _migration_event(state, trace, 'attempt')
            verification, corpora = _completed_shadow_commitments(intent)
            endpoint = lifetime.enter_context(leases.resolve_endpoint(
                project, action='recall', host_root=Path(executing_root)))
            if (endpoint.endpoint_id != intent['endpoint_id']
                    or endpoint.record['install_id'] != intent['install_id']):
                raise IndexContractError('migration_endpoint_changed')
        trace['stage'] = 'prepare'
        handle = store.capture_view(intent['worktree_id'], deadline=deadline)
        head = store.head(handle)
        receipt = intent.get('published')
        if receipt is not None:
            if (head['gen_id'] != receipt['generation'] or head['view_id'] != receipt['view_id']
                    or head['epoch'] != receipt['epoch'] or head['incarnation'] != receipt['candidate_incarnation']):
                raise IndexContractError('migration_snapshot_changed')
            pin = store.pin_view(handle, deadline=deadline)
            try:
                if (pin.gen_id != receipt['generation'] or pin.view_id != receipt['view_id']
                        or pin.epoch != receipt['epoch'] or pin.incarnation != receipt['candidate_incarnation']):
                    raise IndexContractError('migration_snapshot_changed')
            finally:
                store.release(pin)
            generation = receipt['generation']
        else:
            proof = {'observation': intent['observation'], 'endpoint': endpoint, 'decision': decision,
                     'provenance_scan': intent['provenance_scan'], 'build_id': _build_id(intent),
                     'commitments': {'migration_id': intent['migration_id'],
                                     'verification': verification, 'corpora': corpora}}
            generation = store.prepare_generation(handle, None, expected_head=head,
                                                  legacy_snapshot=proof, deadline=deadline)
        lifetime.close()
        # The candidate is private. Recheck the source and intent under the
        # established lock order before returning it, but grant no parity.
        with _install_lock(state), ledger._operation_lock(marker):
            if _migration_state(project, store) != (project, state, decision, node):
                raise IndexContractError('migration_route_changed')
            if _migration_intent(path, project=project, state=state, node=node, store=store) != intent:
                raise IndexContractError('migration_intent_changed')
            if store.head(handle) != head:
                raise IndexContractError('migration_snapshot_changed')
            with leases.resolve_endpoint(project, action='recall', host_root=Path(executing_root)) as current:
                if (current.endpoint_id != intent['endpoint_id']
                        or current.record['install_id'] != intent['install_id']):
                    raise IndexContractError('migration_endpoint_changed')
                step_deadline = min(deadline, time.monotonic() + _STEP_SECONDS)
                with readers.inspect_warm_snapshot(current, deadline=step_deadline) as snapshot:
                    page = capture_effective(snapshot, endpoint=current, decision=decision,
                                             after=None, deadline=step_deadline)
                    if page['observation'] != intent['observation']:
                        raise IndexContractError('migration_snapshot_changed')
                    if _source_revision(snapshot) != intent['source_revision']:
                        raise IndexContractError('migration_source_revision_unproved')
        trace['outcome'] = 'prepared'
        return generation


def publish_shadow_generation(store, generation, *, project, executing_root, deadline):
    """Publish a proved private shadow head without changing the selected reader."""
    from pathlib import Path
    import laya_blob_values as values
    import laya_fresh_wiki_origin as fresh
    import laya_ledger_common as ledger
    import laya_view_values as view_values
    import project_dir
    from laya_product_install import _install_lock

    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.monotonic() >= deadline):
        raise IndexContractError('reader_snapshot_deadline')
    if not isinstance(generation, str) or not view_values.GEN_ID.fullmatch(generation):
        values.fail('generation_id_invalid')
    project, state, decision, node = _migration_state(project, store)
    marker = project_dir.runtime_file(project, fresh.MARKER)
    with _migration_trace(state, 'publish', 'publish') as trace, _install_lock(state), ledger._operation_lock(marker):
        if _migration_state(project, store) != (project, state, decision, node):
            raise IndexContractError('migration_route_changed')
        intent = _migration_intent(state / _INTENT, project=project, state=state, node=node, store=store)
        trace['migration'] = intent['migration_id']
        _migration_event(state, trace, 'attempt')
        verification, corpora = _completed_shadow_commitments(intent)
        if intent.get('published') is not None and intent['published']['generation'] != generation:
            raise IndexContractError('migration_intent_changed')
        if intent.get('prepared') is not None and intent['prepared']['generation'] != generation:
            raise IndexContractError('migration_intent_changed')
        with leases.resolve_endpoint(project, action='recall', host_root=Path(executing_root)) as endpoint:
            if (endpoint.endpoint_id != intent['endpoint_id']
                    or endpoint.record['install_id'] != intent['install_id']):
                raise IndexContractError('migration_endpoint_changed')
            handle = store.capture_view(intent['worktree_id'], deadline=deadline)
            receipt = intent.get('published')
            if receipt is not None:
                pin = store.pin_view(handle, deadline=deadline)
                try:
                    if (pin.gen_id != generation or pin.view_id != receipt['view_id']
                            or pin.epoch != receipt['epoch']
                            or pin.incarnation != receipt['candidate_incarnation']):
                        raise IndexContractError('migration_snapshot_changed')
                    step_deadline = min(deadline, time.monotonic() + _STEP_SECONDS)
                    with readers.inspect_warm_snapshot(endpoint, deadline=step_deadline) as snapshot:
                        page = capture_effective(snapshot, endpoint=endpoint, decision=decision,
                                                 after=None, deadline=step_deadline)
                        if page['observation'] != intent['observation']:
                            raise IndexContractError('migration_snapshot_changed')
                        if _source_revision(snapshot) != intent['source_revision']:
                            raise IndexContractError('migration_source_revision_unproved')
                    published = store.head(handle)
                    if (published['gen_id'] != pin.gen_id or published['view_id'] != pin.view_id
                            or published['epoch'] != pin.epoch or published['incarnation'] != pin.incarnation):
                        raise IndexContractError('migration_snapshot_changed')
                finally:
                    store.release(pin)
                trace['outcome'] = 'published'
                return published
            with store._session() as db:
                row = db.execute('SELECT s.stage_json FROM generations g JOIN generation_slots s '
                                 'ON s.ordinal=g.ordinal WHERE g.gen_id=?', (generation,)).fetchone()
                if row is None:
                    values.fail('generation_missing')
                stage = view_values.check_stage(values.parse_canonical(row['stage_json']),
                                                handle, store._incarnation)
            legacy = stage.get('legacy')
            if (legacy is None or legacy['observation'] != intent['observation']
                    or legacy['scan'] != intent['provenance_scan']):
                raise IndexContractError('migration_snapshot_changed')
            published = store.publish_generation(handle, generation, expected_head=stage['base'],
                legacy_snapshot={'endpoint': endpoint, 'decision': decision,
                                 'provenance_scan': intent['provenance_scan'],
                                 'source_revision': intent['source_revision']}, deadline=deadline)
            receipt = {'migration_id': intent['migration_id'], 'generation': generation,
                       'view_id': published['view_id'], 'epoch': published['epoch'],
                       'observation': intent['observation'], 'scan_id': intent['provenance_scan']['id'],
                       'verification_digest': verification['digest'], 'base_digest': corpora['base']['digest'],
                       'sidecar_digest': corpora['sidecar']['digest'], 'policy_digest': handle.policy_digest,
                       'candidate_incarnation': store._incarnation, 'source_revision': intent['source_revision']}
            if intent.get('published') not in (None, receipt):
                raise IndexContractError('migration_intent_changed')
            if intent.get('published') is None:
                if (time.monotonic() >= deadline or ledger._read_json(state / _INTENT) != intent
                        or store.head(handle) != published):
                    raise IndexContractError('migration_intent_changed')
                ledger._write(state / _INTENT, _stamp({**intent, 'published': receipt}), replace=True)
        trace['outcome'] = 'published'
        return published


def compare_shadow_queries(store, generation, *, project, executing_root, queries, deadline, prompts=()):
    """Compare bounded lexical and optional sidecar recall samples, without issuing a receipt."""
    from contextlib import ExitStack
    from pathlib import Path
    import laya_blob_values as values
    import laya_fresh_wiki_origin as fresh
    import laya_ledger_common as ledger
    import laya_view_values as view_values
    import project_dir
    from laya_product_install import _install_lock

    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.monotonic() >= deadline):
        raise IndexContractError('reader_snapshot_deadline')
    if not isinstance(generation, str) or not view_values.GEN_ID.fullmatch(generation):
        values.fail('generation_id_invalid')
    if (type(queries) is not tuple or not 1 <= len(queries) <= 32
            or any(type(query) is not str or not query or len(query.encode('utf8')) > 4096
                   for query in queries)):
        raise IndexContractError('migration_lexical_queries_invalid')
    if (type(prompts) is not tuple or len(prompts) > 8
            or any(type(prompt) is not str or not prompt or len(prompt.encode('utf8')) > 4096
                   for prompt in prompts)):
        raise IndexContractError('migration_lexical_queries_invalid')
    project, state, decision, node = _migration_state(project, store)
    marker = project_dir.runtime_file(project, fresh.MARKER)
    path = state / _INTENT
    with _migration_trace(state, 'compare', 'parity') as trace, ExitStack() as lifetime:
        with _install_lock(state), ledger._operation_lock(marker):
            if _migration_state(project, store) != (project, state, decision, node):
                raise IndexContractError('migration_route_changed')
            intent = _migration_intent(path, project=project, state=state, node=node, store=store)
            trace['migration'] = intent['migration_id']
            _migration_event(state, trace, 'attempt')
            _completed_shadow_commitments(intent)
            receipt = intent.get('published')
            if receipt is None or receipt['generation'] != generation:
                raise IndexContractError('migration_publication_unrecorded')
            endpoint = lifetime.enter_context(leases.resolve_endpoint(
                project, action='recall', host_root=Path(executing_root)))
            if (endpoint.endpoint_id != intent['endpoint_id']
                    or endpoint.record['install_id'] != intent['install_id']):
                raise IndexContractError('migration_endpoint_changed')
        handle = store.capture_view(intent['worktree_id'], deadline=deadline)
        pin = store.pin_view(handle, deadline=deadline)
        try:
            if (pin.gen_id != generation or pin.view_id != receipt['view_id']
                    or pin.epoch != receipt['epoch'] or pin.incarnation != receipt['candidate_incarnation']):
                raise IndexContractError('migration_snapshot_changed')
            step_deadline = min(deadline, time.monotonic() + _STEP_SECONDS)
            with readers.inspect_warm_snapshot(endpoint, deadline=step_deadline) as snapshot:
                result = compare_lexical_queries(snapshot, endpoint=endpoint, decision=decision,
                    store=store, pin=pin, queries=queries, deadline=step_deadline)
                if result['observation'] != intent['observation']:
                    raise IndexContractError('migration_snapshot_changed')
                if prompts:
                    recall = compare_incremental_lexical_prompts(snapshot, endpoint=endpoint,
                        decision=decision, store=store, pin=pin, prompts=prompts, deadline=step_deadline)
                    if recall['observation'] != intent['observation']:
                        raise IndexContractError('migration_snapshot_changed')
                    result = {**result, 'prompt_count': recall['prompt_count']}
        finally:
            store.release(pin)
        lifetime.close()
        with _install_lock(state), ledger._operation_lock(marker):
            if _migration_state(project, store) != (project, state, decision, node):
                raise IndexContractError('migration_route_changed')
            if _migration_intent(path, project=project, state=state, node=node, store=store) != intent:
                raise IndexContractError('migration_intent_changed')
            if store.head(handle)['gen_id'] != generation:
                raise IndexContractError('migration_snapshot_changed')
            with leases.resolve_endpoint(project, action='recall', host_root=Path(executing_root)) as current:
                if (current.endpoint_id != intent['endpoint_id']
                        or current.record['install_id'] != intent['install_id']):
                    raise IndexContractError('migration_endpoint_changed')
                step_deadline = min(deadline, time.monotonic() + _STEP_SECONDS)
                with readers.inspect_warm_snapshot(current, deadline=step_deadline) as snapshot:
                    page = capture_effective(snapshot, endpoint=current, decision=decision,
                                             after=None, deadline=step_deadline)
                    if page['observation'] != intent['observation']:
                        raise IndexContractError('migration_snapshot_changed')
                    if _source_revision(snapshot) != intent['source_revision']:
                        raise IndexContractError('migration_source_revision_unproved')
        trace['outcome'] = 'compared'
        return result


class _ParityPageDeadline(IndexContractError):
    """A recall parity page that ran out of its step deadline (``reader_snapshot_deadline``)."""
    def __init__(self):
        super().__init__('reader_snapshot_deadline')


# Timeout codes a parity page can surface: the step deadline codes, the
# candidate pin/capture/search deadline, and a candidate lookup that the
# lexical compare reports as ``migration_recall_probe_unproved`` from one.
_PAGE_TIMEOUT_REASONS = frozenset(('fts_deadline_exceeded',)) | _STEP_DEADLINE_REASONS


def _page_deadline_spent(exc, deadline):
    """True when ``exc`` is a timeout code (itself or an explicit cause) and ``deadline`` has passed.

    A timeout code before the page deadline (a shorter inner budget, such as
    the old reader's probe deadline) and every proof failure are not spent
    page budgets. Nor is a checkout observation that failed for another
    reason: the view store reports every ``CheckoutUnavailable`` as its
    timeout code, so one whose reason is not ``checkout_budget_exhausted``,
    found as the cause or the handled exception behind the code, vetoes it.
    """
    from laya_checkout_view import CheckoutUnavailable
    if time.monotonic() < deadline:
        return False
    timeout, link, seen = False, exc, 0
    while link is not None and seen < 8:
        if str(link) in _PAGE_TIMEOUT_REASONS:
            timeout = True
            break
        link, seen = link.__cause__, seen + 1
    link, seen = exc, 0
    while timeout and link is not None and seen < 8:
        if isinstance(link, CheckoutUnavailable) and link.reason != 'checkout_budget_exhausted':
            return False
        link, seen = link.__cause__ or link.__context__, seen + 1
    return timeout


def compare_recall_corpus(store, generation, *, project, executing_root, after, deadline,
                          page_members=None):
    """Compare one page of the member-derived recall corpus, without a receipt.

    A page is at most ``page_members`` (default ``_PARITY_MEMBERS``) members of the recorded effective
    inventory and at most ``_PARITY_PROMPTS`` prompts, compared inside one
    pinned v6 snapshot held for at most ``_STEP_SECONDS``. Pass the returned
    ``next`` cursor until it is None. The digest commits to the compared
    members and prompt hashes; prompts themselves are never recorded.
    """
    from contextlib import ExitStack
    from pathlib import Path
    import laya_blob_values as values
    import laya_fresh_wiki_origin as fresh
    import laya_ledger_common as ledger
    import laya_view_values as view_values
    import project_dir
    from laya_product_install import _install_lock

    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.monotonic() >= deadline):
        raise IndexContractError('reader_snapshot_deadline')
    if not isinstance(generation, str) or not view_values.GEN_ID.fullmatch(generation):
        values.fail('generation_id_invalid')
    if page_members is None:
        page_members = _PARITY_MEMBERS
    if type(page_members) is not int or not 1 <= page_members <= _PARITY_MEMBERS:
        raise IndexContractError('migration_cursor_invalid')
    project, state, decision, node = _migration_state(project, store)
    marker = project_dir.runtime_file(project, fresh.MARKER)
    path = state / _INTENT
    with _migration_trace(state, 'compare_corpus', 'parity') as trace, ExitStack() as lifetime:
        with _install_lock(state), ledger._operation_lock(marker):
            if _migration_state(project, store) != (project, state, decision, node):
                raise IndexContractError('migration_route_changed')
            intent = _migration_intent(path, project=project, state=state, node=node, store=store)
            trace['migration'] = intent['migration_id']
            _migration_event(state, trace, 'attempt')
            _completed_shadow_commitments(intent)
            receipt = intent.get('published')
            if receipt is None or receipt['generation'] != generation:
                raise IndexContractError('migration_publication_unrecorded')
            if after is not None and (type(after) is not dict
                                      or after.get('observation') != intent['observation']):
                raise IndexContractError('migration_cursor_invalid')
            endpoint = lifetime.enter_context(leases.resolve_endpoint(
                project, action='recall', host_root=Path(executing_root)))
            if (endpoint.endpoint_id != intent['endpoint_id']
                    or endpoint.record['install_id'] != intent['install_id']):
                raise IndexContractError('migration_endpoint_changed')
        page_deadline = [deadline]
        try:
            # One checkout observation opens the page (capture_view) and one
            # closes it below; the lookups in between do not re-observe it.
            handle = store.capture_view(intent['worktree_id'], deadline=deadline)
            pin = store.pin_view(handle, deadline=deadline, _observed=True)
            try:
                if (pin.gen_id != generation or pin.view_id != receipt['view_id']
                        or pin.epoch != receipt['epoch'] or pin.incarnation != receipt['candidate_incarnation']):
                    raise IndexContractError('migration_snapshot_changed')
                step_deadline = page_deadline[0] = min(deadline, time.monotonic() + _STEP_SECONDS)
                with readers.inspect_warm_snapshot(endpoint, deadline=step_deadline) as snapshot:
                    page = capture_wiki_bytes(snapshot, endpoint=endpoint, decision=decision, after=after,
                        deadline=step_deadline, _expected_observation=intent['observation'],
                        _page_limit=page_members)
                    if _source_revision(snapshot) != intent['source_revision']:
                        raise IndexContractError('migration_source_revision_unproved')
                    prompts, seen, committed = [], set(), []
                    for member in page['members']:
                        own = recall_corpus_prompts(member['path'], member['body'], member['title'])
                        committed.append([member['path'], member['hash'],
                            [hashlib.sha256(prompt.encode('utf8')).hexdigest() for prompt in own]])
                        for prompt in own:
                            if prompt not in seen:
                                seen.add(prompt)
                                prompts.append(prompt)
                    if prompts:
                        compare_combined_lexical_prompts(snapshot, endpoint=endpoint, decision=decision,
                            store=store, pin=pin, prompts=tuple(prompts), deadline=step_deadline)
                # A checkout that moved during the page discards its result; the
                # cursor stays and a later step compares the same page again.
                try:
                    store._observe(store._pinned(pin)['entry'], deadline=deadline,
                                   timeout_reason='reader_snapshot_deadline')
                except values.BlobStoreError as exc:
                    if exc.reason == 'checkout_changed':
                        raise IndexContractError('migration_checkout_changed') from exc
                    raise
            finally:
                store.release(pin)
        except Exception as exc:
            # A page whose step deadline ran out is retried smaller
            # (_shrink_parity_page), wherever the timeout surfaced; any other
            # failure, including a proof failure, keeps its own code.
            if _page_deadline_spent(exc, page_deadline[0]):
                raise _ParityPageDeadline() from exc
            raise
        trace['outcome'] = 'compared'
        digest = hashlib.sha256(('memvane-recall-corpus-page-v1:' + json.dumps(
            [intent['observation'], generation, committed], ensure_ascii=True,
            separators=(',', ':'))).encode('ascii')).hexdigest()
        return {'observation': intent['observation'], 'members': len(committed),
                'prompts': len(prompts), 'digest': digest, 'next': page['next']}


def _prove_head_unlocked(project, stack, deadline):
    """Prove the selected v6 head before a step, outside both locks, for reuse.

    One query_only snapshot of the base store on the caller's wall deadline
    (no reader window, installer or operation lock; a WAL reader blocks no
    writer), interrupted at that deadline; its read transaction ends with the
    proof (``laya_journal_reader.proved_published``). The idle connection is
    kept in ``stack`` until the step ends, so its close cannot checkpoint the
    WAL between this proof and the step. Under ``published_proof_reuse`` the
    locked step's snapshot reuses this proof while the store's file identity
    and head row hold, so the O(generation) proof no longer spends the step
    budget. Best effort: any failure leaves the step to prove and report on
    its own.
    """
    from pathlib import Path
    started = time.monotonic()
    try:
        # Not _migration_state: the step's own dispatch stays its first call.
        decision = journal.journal.select_route(str(project), action='recall')
        if decision.get('state') == 'candidate' and decision.get('projectRoot') == str(project):
            stack.enter_context(journal.proved_published(
                journal._state(Path(decision['journalPath']).parent),
                Path(decision['dbPath']), decision, deadline=deadline))
            result = 'ok'
        else:
            result = 'route_not_candidate'
    except Exception as exc:
        # The step proves again and reports its own failure; this code only
        # says why the reuse it hoped for was not prepared.
        value = str(exc)
        result = value if re.fullmatch(r'[a-z0-9_]{1,80}', value) else type(exc).__name__
    return result, int((time.monotonic() - started) * 1000)


def _warm_candidate_unlocked(store, project, deadline):
    """Warm the candidate's checkout capture and pre-verify its head, outside both locks.

    Like ``_prove_head_unlocked``: on the caller's wall deadline, before the
    step's own budget starts, with no installer or operation lock. Once the
    intent records a published private head, one checkout capture fills the
    process capture cache (the step's capture then reuses it while the
    checkout fingerprint holds) and ``ViewStore._preverify_view`` verifies
    that head generation, which the step's pin reuses while no commit reached
    the store since. Best effort: any failure leaves the step to do both on
    its own. Returns ``(code, ms)``.
    """
    from pathlib import Path
    import laya_ledger_common as ledger
    started = time.monotonic()
    try:
        # Not _migration_state: the step's own dispatch stays its first call.
        # The intent only names the worktree and the expected head here; the
        # step reads and validates it again under both locks.
        decision = journal.journal.select_route(str(project), action='recall')
        if decision.get('state') != 'candidate' or decision.get('projectRoot') != str(project):
            return 'route_not_candidate', int((time.monotonic() - started) * 1000)
        intent = ledger._read_json(Path(decision['journalPath']).parent / _INTENT)
        published = intent.get('published') if isinstance(intent, dict) else None
        if not isinstance(published, dict) or not isinstance(intent.get('worktree_id'), str):
            result = 'no_published_head'
        else:
            handle = store.capture_view(intent['worktree_id'], deadline=deadline)
            gen_id = store._preverify_view(handle, deadline=deadline)
            result = 'ok' if gen_id == published.get('generation') else 'head_moved'
    except Exception as exc:
        value = str(exc)
        result = value if re.fullmatch(r'[a-z0-9_]{1,80}', value) else type(exc).__name__
    return result, int((time.monotonic() - started) * 1000)


def advance_shadow(store, *, project, executing_root, deadline, vector_spec=None, prepare=True):
    """Run at most one bounded step of the private shadow state machine per call.

    The published v6 head is first proved outside both locks on the wall
    ``deadline`` (``_prove_head_unlocked``); the step's own snapshots reuse
    that proof within one ``published_proof_reuse`` block (the driver shares
    one across a pass). See ``_advance_shadow_step``.
    """
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.monotonic() >= deadline):
        raise IndexContractError('reader_snapshot_deadline')
    from contextlib import ExitStack
    with journal.published_proof_reuse(), ExitStack() as stack:
        preproof, preproof_ms = _prove_head_unlocked(project, stack, deadline)
        prewarm, prewarm_ms = _warm_candidate_unlocked(store, project, deadline)
        with journal.published_proof_lookups() as lookups:
            token = _STEP_PROOF.set({'preproof': preproof, 'preproof_ms': preproof_ms,
                                     'prewarm': prewarm, 'prewarm_ms': prewarm_ms,
                                     'lookups': lookups})
            try:
                return _advance_shadow_step(store, project=project, executing_root=executing_root,
                                            deadline=deadline, vector_spec=vector_spec, prepare=prepare)
            finally:
                _STEP_PROOF.reset(token)


def _advance_shadow_step(store, *, project, executing_root, deadline, vector_spec=None, prepare=True):
    """Run at most one bounded step of the private shadow state machine per call.

    copy -> verification -> base/sidecar corpus -> prepare -> publish ->
    caught_up -> recall parity pages -> parity seal -> shadow_verified.
    Each call performs one of them; the selected reader never changes. With a
    ``vector_spec`` the caught-up head first pages compatible legacy vectors
    (advance_vectors); without one, or with nothing compatible, the lexical
    shadow is complete on its own. The seal step reports ``quiesce_ms``; a busy
    writer or an exceeded quiesce bound returns stage ``retry`` with ``reason``
    and leaves the intent unchanged. A copy whose projected bytes do not fit
    (``migration_space_deferred``) also returns ``retry`` with nothing written.
    With ``prepare=False`` a due lock-free prepare is not started: the call
    returns stage ``prepare_deferred`` and writes nothing.
    """
    import laya_blob_values as values
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.monotonic() >= deadline):
        raise IndexContractError('reader_snapshot_deadline')
    wall_deadline = deadline
    deadline = min(deadline, time.monotonic() + _STEP_SECONDS)
    project, state, _, node = _migration_state(project, store)
    # This read only chooses the next operation. That operation reacquires
    # both existing locks and validates the current intent and endpoint.
    path = state / _INTENT
    intent = _migration_intent(path, project=project, state=state, node=node, store=store)
    args = dict(project=project, executing_root=executing_root, deadline=deadline)
    extra = {}

    def step(stage, current):
        return {'stage': stage, 'migration_state': current['migration_state'], 'intent': current}

    def reread():
        return _migration_intent(path, project=project, state=state, node=node, store=store)

    def retryable(stage, run):
        # A NormalWriter admission that changes the reader store while this
        # step resolves its endpoint proves no source change, and the intent
        # was not written; report a bounded retry rather than an error.
        try:
            return step(stage, run())
        except ValueError as exc:
            if str(exc) in _SHADOW_RETRY_REASONS:
                return step('retry', reread())
            raise

    if intent['phase'] == 'copying':
        return retryable('copy', lambda: advance_migration(store, **args))
    if intent.get('restart_required', False):
        return retryable('restart', lambda: advance_migration(store, **args))
    verification = intent.get('verification')
    if verification is not None:
        _verification_progress(verification, intent, check_limits=False)
        if not _progress_current(verification):
            return retryable('restart', lambda: advance_migration(store, **args))
    corpora = intent.get('corpora')
    if corpora is not None:
        # A receipt catch-up may keep corpus chains while the mapping chain
        # replays; the completed mapping rebinds them (advance_verification).
        values.exact(corpora, ('verification_digest', 'base', 'sidecar'))
        for key in ('base', 'sidecar'):
            if corpora[key] is not None:
                _verification_progress(corpora[key], intent,
                                       cursor_observation=_corpus_observation(key, intent['observation']))
    if verification is None or not verification['complete']:
        stage, run = 'verification', lambda: advance_verification(store, **args)
    else:
        if corpora is not None and corpora['verification_digest'] != verification['digest']:
            raise IndexContractError('migration_intent_invalid')
        prepared, published = intent.get('prepared'), intent.get('published')
        if corpora is None or corpora['base'] is None or not corpora['base']['complete']:
            stage, run = 'base_corpus', lambda: advance_corpus(store, sidecar=False, **args)
        elif corpora['sidecar'] is None or not corpora['sidecar']['complete']:
            stage, run = 'sidecar_corpus', lambda: advance_corpus(store, sidecar=True, **args)
        elif published is not None and vector_spec is not None and _vectors_pending(intent, vector_spec):
            def run():
                return advance_vectors(store, vector_spec=vector_spec, **args)
            stage = 'vectors'
        elif published is not None and intent['migration_state'] == 'caught_up':
            parity = _bound_parity(intent)
            if parity is None or not parity['compared']:
                stage, run = 'parity', lambda: _advance_parity(store, **args)
            else:
                def run():
                    updated, extra['quiesce_ms'] = _seal_parity(store, **args)
                    return updated
                stage = 'parity_seal'
        elif published is not None:
            # Read-only replay: re-proves the recorded private head against the
            # unchanged source under the step deadline, without the cut.
            def run():
                publish_shadow_generation(store, published['generation'], **args)
                return reread()
            stage = 'shadow_verified'
        elif prepared is None:
            if not prepare:
                return step('prepare_deferred', intent)
            # The lock-free build runs on the caller's wall deadline; only the
            # intent pointer is written under both locks afterwards.
            def run():
                generation = prepare_shadow_generation(store, project=project,
                    executing_root=executing_root, deadline=wall_deadline)
                return _record_prepared(store, project, intent, generation,
                                        min(wall_deadline, time.monotonic() + _STEP_SECONDS))
            stage = 'prepare'
        else:
            def run():
                publish_shadow_generation(store, prepared['generation'], **args)
                return reread()
            stage = 'publish'
    try:
        return {**retryable(stage, run), **extra}
    except _VectorsWarmed:
        return step('warmed', reread())
    except ValueError as exc:
        reason = str(exc)
        if stage == 'parity_seal' and reason in _PARITY_SEAL_RETRY_REASONS:
            # The seal released the writer again; nothing was written.
            return {**step('retry', reread()), 'reason': reason}
        if stage == 'parity' and isinstance(exc, _ParityPageDeadline):
            shrunk = _shrink_parity_page(store, project, intent,
                                         min(wall_deadline, time.monotonic() + _STEP_SECONDS), reason)
            if shrunk is not None:
                return step('parity_shrunk', shrunk)
            raise
        if (not isinstance(exc, (IndexContractError, values.BlobStoreError))
                or reason not in _SHADOW_RESTART_REASONS):
            raise
        # A second full proof may not fit this step. Persist only a request;
        # the next step re-proves the source before resetting any cursor.
        return step('restart_pending', _mark_shadow_restart(
            store, project, intent, min(wall_deadline, time.monotonic() + _STEP_SECONDS), reason))


# Recall parity (U5). Pages run lock-free: each compare_recall_corpus page is
# one pinned v6 snapshot bound to the intent observation, so a writer commit
# inside a page cannot change its result and a commit between pages fails the
# next page (or the seal) as migration_snapshot_changed. A change restarts the
# whole parity instead of only affected pages: it replaces the private head
# (catch-up builds a new generation), and combined recall ranks every prompt
# against the whole corpus, so one changed member can move any prompt. The
# discarded work is counted (``attempt``, ``discarded_pages``). The record is
# bound like ``vectors``: a restart replaces ``published`` and the record stops
# applying without any restart path clearing it.
_PARITY_RECORD = ('migration_id', 'generation', 'candidate_incarnation', 'observation',
                  'source_revision', 'vectors', 'cursor', 'pages', 'members', 'prompts',
                  'digest', 'compared', 'seal', 'attempt', 'discarded_pages')
# A parity page that missed its step deadline is retried at half the members
# (4 -> 2 -> 1) from the same cursor; the record keeps the smaller size as the
# optional ``page_members`` (absent means _PARITY_MEMBERS). A page of one
# member that still misses its deadline is an error, as before.
_PARITY_PAGE_KEY = 'page_members'
# Seal outcomes that leave the intent unchanged and only ask for a later step.
_PARITY_SEAL_RETRY_REASONS = frozenset(('sync_owner_busy',)) | _STEP_DEADLINE_REASONS


def _check_parity_record(record):
    """Shape only; _bound_parity decides whether a record still applies."""
    import laya_blob_values as values
    import laya_view_values as view_values
    if _PARITY_PAGE_KEY in record:
        values.exact(record, _PARITY_RECORD + (_PARITY_PAGE_KEY,))
        values.integer(record[_PARITY_PAGE_KEY], 1, _PARITY_MEMBERS - 1)
    else:
        values.exact(record, _PARITY_RECORD)
    values.identifier(record['migration_id'], values.UUID)
    values.identifier(record['candidate_incarnation'], values.UUID)
    if not isinstance(record['generation'], str) or not view_values.GEN_ID.fullmatch(record['generation']):
        raise IndexContractError('migration_intent_invalid')
    values.identifier(record['observation'])
    _check_source_revision(record['source_revision'])
    values.identifier(record['digest'])
    for key in ('vectors', 'seal'):
        if record[key] is not None:
            values.identifier(record[key])
    pages = values.integer(record['pages'], 0, MAX_DOCS + 1)
    values.integer(record['members'], 0, MAX_DOCS)
    values.integer(record['prompts'], 0, pages * _PARITY_PROMPTS)
    values.integer(record['attempt'], 1)
    values.integer(record['discarded_pages'], 0)
    cursor = record['cursor']
    if (type(record['compared']) is not bool
            or record['compared'] != (pages > 0 and cursor is None)
            or (record['seal'] is not None and not record['compared'])
            or (cursor is not None and (type(cursor) is not dict or set(cursor) != {'after', 'observation'}
                                        or not _valid_relative_path(cursor['after'])
                                        or cursor['observation'] != record['observation']))):
        raise IndexContractError('migration_intent_invalid')
    return record


def _complete_vectors(intent):
    """Digest of a complete vector record bound to the private head, else None."""
    record, receipt = intent.get('vectors'), intent.get('published')
    if (record is None or receipt is None or not record['complete']
            or record['migration_id'] != intent['migration_id']
            or record['generation'] != receipt['generation']
            or record['candidate_incarnation'] != receipt['candidate_incarnation']):
        return None
    return record['digest']


def _bound_parity(intent):
    """The parity record bound to the current head, source and vectors, else None."""
    record, receipt = intent.get('parity'), intent.get('published')
    if (record is None or receipt is None or record['migration_id'] != intent['migration_id']
            or record['generation'] != receipt['generation']
            or record['candidate_incarnation'] != receipt['candidate_incarnation']
            or record['observation'] != intent['observation']
            or record['source_revision'] != intent['source_revision']
            or record['vectors'] != _complete_vectors(intent)):
        return None
    return record


def _initial_parity(intent):
    import laya_blob_values as values
    previous, receipt = intent.get('parity'), intent['published']
    again = previous is not None and previous['migration_id'] == intent['migration_id']
    vectors = _complete_vectors(intent)
    # A restart of the same migration keeps a page size it already learned.
    page = {_PARITY_PAGE_KEY: previous[_PARITY_PAGE_KEY]} if again and _PARITY_PAGE_KEY in previous else {}
    return {**page, 'migration_id': intent['migration_id'], 'generation': receipt['generation'],
            'candidate_incarnation': receipt['candidate_incarnation'],
            'observation': intent['observation'], 'source_revision': intent['source_revision'],
            'vectors': vectors, 'cursor': None, 'pages': 0, 'members': 0, 'prompts': 0,
            'compared': False, 'seal': None,
            'digest': values.digest('memvane-shadow-recall-parity-v1', {
                'migration': intent['migration_id'], 'published': receipt, 'vectors': vectors}),
            'attempt': previous['attempt'] + 1 if again else 1,
            'discarded_pages': previous['discarded_pages'] + previous['pages'] if again else 0}


def _fold_parity(record, page):
    import laya_blob_values as values
    if page['observation'] != record['observation']:
        raise IndexContractError('migration_snapshot_changed')
    if record['pages'] >= MAX_DOCS + 1:
        raise IndexContractError('migration_membership_limit')
    nxt = page['next']
    digest = values.digest('memvane-shadow-recall-parity-page-v1', {
        'previous': record['digest'], 'page': page['digest'],
        'next': None if nxt is None else nxt['after']})
    return {**record, 'cursor': nxt, 'pages': record['pages'] + 1,
            'members': record['members'] + page['members'], 'prompts': record['prompts'] + page['prompts'],
            'digest': digest, 'compared': nxt is None}


def _advance_parity(store, *, project, executing_root, deadline):
    """Compare one recall corpus page and fold its digest into the intent."""
    import laya_fresh_wiki_origin as fresh
    import laya_ledger_common as ledger
    import project_dir
    from laya_product_install import _install_lock
    project, state, decision, node = _migration_state(project, store)
    path = state / _INTENT
    # This read only picks the cursor; compare_recall_corpus re-proves the
    # head under both locks and the write below rechecks the intent.
    intent = _migration_intent(path, project=project, state=state, node=node, store=store)
    if intent['migration_state'] != 'caught_up':
        raise IndexContractError('migration_publication_unrecorded')
    record = _bound_parity(intent) or _initial_parity(intent)
    if record['compared']:
        raise IndexContractError('migration_intent_changed')
    page = compare_recall_corpus(store, record['generation'], project=project,
        executing_root=executing_root, after=record['cursor'], deadline=deadline,
        page_members=record.get(_PARITY_PAGE_KEY, _PARITY_MEMBERS))
    updated = _stamp({**intent, 'parity': _fold_parity(record, page)})
    marker = project_dir.runtime_file(project, fresh.MARKER)
    with _migration_trace(state, 'parity', 'parity') as trace, _install_lock(state), ledger._operation_lock(marker):
        if _migration_state(project, store) != (project, state, decision, node):
            raise IndexContractError('migration_route_changed')
        current = _migration_intent(path, project=project, state=state, node=node, store=store)
        trace['migration'] = current['migration_id']
        _migration_event(state, trace, 'attempt')
        if current != intent:
            raise IndexContractError('migration_intent_changed')
        if time.monotonic() >= deadline:
            # The page compared, but its step deadline ran out before the
            # cursor write: the same spent page budget as inside the compare.
            raise _ParityPageDeadline()
        ledger._write(path, updated, replace=True)
        trace['outcome'] = 'parity_page'
    return updated


def _shrink_parity_page(store, project, expected, deadline, reason):
    """Halve the parity page after a page missed its step deadline; None at one member.

    The discarded page wrote nothing and the cursor stays, so the next step
    compares the same members in smaller pages. Under both locks, on a fresh
    ``deadline`` (the page's own one is spent).
    """
    import laya_fresh_wiki_origin as fresh
    import laya_ledger_common as ledger
    import project_dir
    from laya_product_install import _install_lock
    project, state, decision, node = _migration_state(project, store)
    marker = project_dir.runtime_file(project, fresh.MARKER)
    with _migration_trace(state, 'parity_shrink', 'parity') as trace, _install_lock(state), \
            ledger._operation_lock(marker):
        if _migration_state(project, store) != (project, state, decision, node):
            raise IndexContractError('migration_route_changed')
        path = state / _INTENT
        current = _migration_intent(path, project=project, state=state, node=node, store=store)
        trace['migration'] = current['migration_id']
        if current != expected or current['migration_state'] != 'caught_up':
            raise IndexContractError('migration_intent_changed')
        record = _bound_parity(current) or _initial_parity(current)
        size = record.get(_PARITY_PAGE_KEY, _PARITY_MEMBERS)
        if record['compared'] or size <= 1:
            return None
        _migration_event(state, trace, 'attempt')
        if time.monotonic() >= deadline:
            raise IndexContractError('reader_snapshot_deadline')
        updated = _stamp({**current, 'parity': {**record, _PARITY_PAGE_KEY: size // 2}})
        ledger._write(path, updated, replace=True)
        trace.update(outcome='parity_page_shrunk', reason=reason, fields={'page_members': size // 2})
        return updated


def _parity_seal_digest(intent, record):
    import laya_blob_values as values
    catch_up = intent.get('catch_up')
    return values.digest('memvane-shadow-parity-receipt-v1', {
        'migration': intent['migration_id'], 'published': intent['published'],
        'catch_up': None if catch_up is None else catch_up['digest'], 'vectors': record['vectors'],
        'recall': {key: record[key] for key in ('digest', 'pages', 'members', 'prompts')}})


def _seal_parity(store, *, project, executing_root, deadline):
    """Final catch-up proof under a bounded writer quiesce, then seal the receipt.

    Holds the installer, operation and sync owner locks (none of them waits)
    while it re-proves the source observation and revision the parity pages
    compared and the private head, so no writer commit can fall between that
    proof and the sealed intent. Returns (intent, held milliseconds).
    """
    from contextlib import ExitStack
    from pathlib import Path
    import laya_fresh_wiki_origin as fresh
    import laya_ledger_common as ledger
    import project_dir
    from laya_index_sync import _owner_lock
    from laya_product_install import _install_lock
    project, state, decision, node = _migration_state(project, store)
    marker = project_dir.runtime_file(project, fresh.MARKER)
    path = state / _INTENT
    with _migration_trace(state, 'parity_seal', 'parity_seal') as trace, ExitStack() as locks:
        locks.enter_context(_install_lock(state))
        locks.enter_context(ledger._operation_lock(marker))
        if not locks.enter_context(_owner_lock(Path(decision['queuePath']), wait=True, deadline=deadline)):
            raise IndexContractError('sync_owner_busy')
        held = time.monotonic()
        quiesce = min(deadline, held + _STEP_SECONDS)
        if _migration_state(project, store) != (project, state, decision, node):
            raise IndexContractError('migration_route_changed')
        intent = _migration_intent(path, project=project, state=state, node=node, store=store)
        trace['migration'] = intent['migration_id']
        _migration_event(state, trace, 'attempt')
        _completed_shadow_commitments(intent)
        record, receipt = _bound_parity(intent), intent.get('published')
        if record is None or not record['compared'] or record['seal'] is not None:
            raise IndexContractError('migration_parity_incomplete')
        with leases.resolve_endpoint(project, action='recall', host_root=Path(executing_root)) as endpoint:
            if (endpoint.endpoint_id != intent['endpoint_id']
                    or endpoint.record['install_id'] != intent['install_id']):
                raise IndexContractError('migration_endpoint_changed')
            head = store.head(store.capture_view(intent['worktree_id'], deadline=quiesce))
            if (head['gen_id'] != receipt['generation'] or head['view_id'] != receipt['view_id']
                    or head['epoch'] != receipt['epoch']
                    or head['incarnation'] != receipt['candidate_incarnation']):
                raise IndexContractError('migration_snapshot_changed')
            with readers.inspect_warm_snapshot(endpoint, deadline=quiesce) as snapshot:
                page = capture_effective(snapshot, endpoint=endpoint, decision=decision,
                                         after=None, deadline=quiesce)
                if page['observation'] != intent['observation']:
                    raise IndexContractError('migration_snapshot_changed')
                if _source_revision(snapshot) != intent['source_revision']:
                    raise IndexContractError('migration_source_revision_unproved')
        updated = _stamp({**intent, 'parity': {**record, 'seal': _parity_seal_digest(intent, record)}})
        if time.monotonic() >= quiesce or ledger._read_json(path) != intent:
            raise IndexContractError('reader_snapshot_deadline')
        ledger._write(path, updated, replace=True)
        quiesce_ms = round((time.monotonic() - held) * 1000, 3)
        trace.update(outcome='shadow_verified', fields={'quiesce_ms': quiesce_ms})
        return updated, quiesce_ms


def shadow_parity_receipt(state, *, candidate, candidate_incarnation):
    """The sealed U5 parity receipt for exactly this candidate, else None.

    Returns only values sealed in the shadow intent; it re-proves nothing
    against the live source (the reader CAS and switch own those checks).
    Absent, unsealed, stale, foreign or unreadable intents return None, which
    the journal CAS refuses as ``reader_parity_unproved``.
    """
    from pathlib import Path
    from types import SimpleNamespace
    import laya_ledger_common as ledger
    from laya_blob_store import _db_file
    try:
        state, candidate = Path(state), Path(candidate)
        intent = ledger._read_json(state / _INTENT)
        if not isinstance(intent, dict) or not isinstance(intent.get('published'), dict):
            return None
        store = SimpleNamespace(_path=candidate, _incarnation=candidate_incarnation,
                                _policy=intent['published'].get('policy_digest'))
        intent = _migration_intent(state / _INTENT, project=intent.get('project'), state=state,
                                   node=_db_file(candidate), store=store)
        record = _bound_parity(intent)
        # reader_ready and switched keep the sealed parity they were proved on.
        if (intent['migration_state'] not in ('shadow_verified', 'reader_ready', 'switched')
                or record is None or record['seal'] is None):
            return None
        return {'migration_id': intent['migration_id'], 'generation': record['generation'],
                'digest': record['seal'], 'candidate': str(candidate),
                'candidate_incarnation': record['candidate_incarnation']}
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return None


# Reader switch (U15, design 4.1-4.3, decision K1): shadow_verified ->
# reader_ready -> switched, one sync-driver step each. The reader_ready step
# proves every precondition under the lock bundle and records the schema3
# target, the v6 CAS base and a fixed transition id; the next pass re-proves
# them and runs the one journal CAS (laya_journal_reader.cas_reader). A gate
# that does not hold returns stage ``retry`` with its code and writes nothing,
# so the driver tries again on a later pass. Preconditions:
#   1. the sealed U5 parity receipt of this candidate (switch_parity_missing);
#   2. latest catch-up: under the sync owner lock the private head is the
#      published one and the v6 source observation and revision are the ones
#      the parity compared (switch_catchup_stale);
#   3-4. a live loaded-reader receipt set from Claude and Codex at the current
#      reader epoch and preferred v6 endpoint, inside its TTL
#      (switch_bridge_missing; cas_reader rechecks it before the replace);
#   5. no older-id Hermes copy (switch_hosts_unsupported);
#   6. executing bundle = journal-selected bundle = receipt release commit
#      (switch_bundle_mismatch); the receipts carry the release commit whose
#      bundle ships the schema3 reader, so they are its capability proof;
#   7. no bundle upgrade, state move or project-folder move in progress and
#      the project folder is ``.memvane`` only (switch_lifecycle_busy,
#      switch_layout_not_current).
# Lock bundle (design 4.3-2): installer -> build owner -> operation ->
# provider lane -> sync owner, never waiting. cas_reader takes the journal
# transition lock itself. Each step holds it for at most _SWITCH_SECONDS.
_SWITCH_SECONDS = 10.0
_SWITCH_HOSTS = frozenset(('claude', 'codex'))
_READY_RECORD = ('migration_id', 'generation', 'candidate_incarnation', 'parity_seal', 'observation',
                 'source_revision', 'release_commit', 'capability', 'endpoint_id', 'store_incarnation',
                 'worktree_binding', 'floor', 'expected_epoch', 'expected_endpoint', 'transition_id')
_SWITCHED_RECORD = ('migration_id', 'endpoint_id', 'transition_id', 'reader_epoch', 'previous_endpoint')
# Gate refusals and busy locks: nothing was written, a later pass retries.
_SWITCH_RETRY = frozenset((
    'switch_parity_missing', 'switch_catchup_stale', 'switch_bridge_missing', 'switch_hosts_unsupported',
    'switch_bundle_mismatch', 'switch_lifecycle_busy', 'switch_layout_not_current',
    'switch_selection_changed', 'switch_reader_ready_stale', 'sync_owner_busy', 'initial_build_busy',
    'learning_compute_busy', 'reader_snapshot_deadline', 'reader_hosts_pending', 'reader_bridge_unproved',
    'stale_reader_cas', 'reader_transition_busy', 'reader_head_regressed', 'reader_parity_unproved'))
# Source changes seen by the catch-up recheck; advance_shadow restarts on them.
_SWITCH_STALE = frozenset(('migration_snapshot_changed', 'migration_source_revision_unproved',
                           'migration_endpoint_changed'))


def _check_ready_record(record):
    import laya_blob_values as values
    import laya_view_values as view_values
    from laya_product_bundle import COMMIT
    values.exact(record, _READY_RECORD)
    for key in ('migration_id', 'candidate_incarnation', 'store_incarnation'):
        values.identifier(record[key], values.UUID)
    for key in ('parity_seal', 'observation', 'endpoint_id', 'worktree_binding', 'expected_endpoint'):
        values.identifier(record[key])
    _check_source_revision(record['source_revision'])
    values.integer(record['expected_epoch'], 1)
    floor = record['floor']
    values.exact(floor, ('view_id', 'epoch', 'ordinal'))
    values.identifier(floor['view_id'])
    values.integer(floor['epoch'], 1)
    values.integer(floor['ordinal'], 1)
    if (not isinstance(record['generation'], str) or not view_values.GEN_ID.fullmatch(record['generation'])
            or not isinstance(record['release_commit'], str) or not COMMIT.fullmatch(record['release_commit'])
            or record['capability'] != journal.SCHEMA3_CAPABILITY
            or not isinstance(record['transition_id'], str)
            or not re.fullmatch(r'[0-9a-f]{32}', record['transition_id'])):
        raise IndexContractError('migration_intent_invalid')
    return record


def _check_switched_record(record):
    import laya_blob_values as values
    values.exact(record, _SWITCHED_RECORD)
    values.identifier(record['migration_id'], values.UUID)
    values.identifier(record['endpoint_id'])
    values.identifier(record['previous_endpoint'])
    values.integer(record['reader_epoch'], 2)
    if not isinstance(record['transition_id'], str) or not re.fullmatch(r'[0-9a-f]{32}', record['transition_id']):
        raise IndexContractError('migration_intent_invalid')
    return record


def _bound_reader_ready(intent):
    """The reader_ready record bound to the current sealed parity, else None."""
    record, parity = intent.get('reader_ready'), _bound_parity(intent)
    if (record is None or parity is None or parity['seal'] is None
            or record['migration_id'] != intent['migration_id'] or record['parity_seal'] != parity['seal']
            or record['generation'] != parity['generation']
            or record['candidate_incarnation'] != parity['candidate_incarnation']
            or record['observation'] != intent['observation']
            or record['source_revision'] != intent['source_revision']):
        return None
    return record


def _bound_switched(intent):
    """The switched record naming the bound reader_ready target, else None."""
    record, ready = intent.get('switched'), _bound_reader_ready(intent)
    if (record is None or ready is None or record['migration_id'] != intent['migration_id']
            or record['endpoint_id'] != ready['endpoint_id']
            or record['transition_id'] != ready['transition_id']
            or record['previous_endpoint'] != ready['expected_endpoint']
            or record['reader_epoch'] != ready['expected_epoch'] + 1):
        return None
    return record


def _switched_intent(intent, selection):
    return _stamp({**intent, 'switched': {
        'migration_id': intent['migration_id'], 'endpoint_id': selection.preferred_endpoint,
        'transition_id': selection.transition_id, 'reader_epoch': selection.epoch,
        'previous_endpoint': selection.previous_endpoint}})


def _cas_landed(ready, selection):
    """The journal already selects exactly the recorded transition."""
    return (selection.preferred_endpoint == ready['endpoint_id']
            and selection.transition_id == ready['transition_id']
            and selection.previous_endpoint == ready['expected_endpoint']
            and selection.epoch == ready['expected_epoch'] + 1)


def _lifecycle_idle(project, state, code):
    """Read-only: no lifecycle intent in progress or stopped; else ``code``.

    Each intent captured the reader it started under (an active upgrade
    re-proves the selected endpoint against its fenced reader; a state or
    project folder move re-seals the then-selected record), so a reader CAS
    underneath would strand it or make it re-seal another record. The
    SessionStart bundle worker finishes a stopped intent; the caller asks
    again afterwards.
    """
    import os
    import laya_fresh_wiki_origin as fresh
    import laya_active_upgrade as active
    import laya_bound_upgrade as bound
    import laya_inactive_upgrade as inactive
    import laya_project_dir_move as folder_move
    import laya_state_move as state_move
    import project_dir
    # The other intents are renamed to receipts when they finish; a finished
    # active upgrade keeps its intent file as the receipt until a later bundle
    # archives it, so only an unsettled one is busy.
    intents = (bound.INTENT, inactive.INTENT, state_move.INTENT, folder_move.INTENT)
    pointers = (state_move.POINTER, folder_move.POINTER)
    if (fresh.upgrade_fenced(str(project)) or any(os.path.lexists(state / name) for name in intents)
            or any(os.path.lexists(project_dir.runtime_file(project, name)) for name in pointers)):
        raise IndexContractError(code)
    if os.path.lexists(state / active.INTENT):
        try:
            intent = active._load_intent(state, project)
            # Read-only; refuses an intent whose transition has not settled.
            active.finished_intent_receipt(state, project, intent)
        except Exception as exc:
            raise IndexContractError(code) from exc


def _switch_preconditions(project, state):
    """Read-only lifecycle, layout and host gates (7, 5)."""
    import laya_bridge_readiness as bridge
    import project_dir
    if project_dir.layout(project) != 'current':
        raise IndexContractError('switch_layout_not_current')
    _lifecycle_idle(project, state, 'switch_lifecycle_busy')
    try:
        bridge._require_supported_hosts(_SWITCH_HOSTS)
    except ValueError as exc:
        raise IndexContractError('switch_hosts_unsupported') from exc


# reader_ready fields that follow the sealed target's endpoint id alone. A
# re-proof that differs only here switches on the new target (``_switch_step``).
_RESEAL_FIELDS = frozenset(('endpoint_id', 'transition_id'))


def _transition_id(expected_endpoint, endpoint_id):
    # Fixed per (base, target): a retry after a committed CAS is its replay.
    return hashlib.sha256(('memvane-shadow-switch-v1\0%s\0%s' % (expected_endpoint, endpoint_id))
                          .encode('ascii')).hexdigest()[:32]


def _offer_sync(project, start=False, delay=0):
    """One existing laya-sync wake so the next pass follows; best effort.

    ``delay`` makes it due that many seconds later (the driver's rewake,
    ``laya_product_sync_worker._shadow_rewake``). Returns the request result,
    or None when nothing was offered.
    """
    try:
        import runtime_work
        if delay:
            return runtime_work.request('laya-sync', str(project), start=start, delay=delay)
        return runtime_work.request('laya-sync', str(project), start=start)
    except Exception:
        return None  # The next edit or session start wakes the sync anyway.


def _switch_step(store, *, project, executing_root, deadline, expected):
    """Prove the switch preconditions under the lock bundle; record or CAS.

    ``expected`` is the shadow_verified (record reader_ready) or reader_ready
    (journal CAS, then record switched) intent read by the caller. Returns
    the written intent, or None after dropping a reader_ready record whose
    re-proof differs (the next pass proves reader_ready again).
    """
    from contextlib import ExitStack
    from pathlib import Path
    import laya_bridge_readiness as bridge
    import laya_fresh_wiki_origin as fresh
    import laya_initial_build as build
    import laya_ledger_common as ledger
    import project_dir
    from context_learning.resource_coordination import Coordinator
    from laya_index_sync import _owner_lock
    from laya_product_install import _install_lock
    project, state, decision, node = _migration_state(project, store)
    path, journal_path = state / _INTENT, state / 'install-journal.sqlite'
    executing_root = Path(executing_root)
    switching = expected['migration_state'] == 'reader_ready'
    _switch_preconditions(project, state)
    if shadow_parity_receipt(state, candidate=store._path, candidate_incarnation=store._incarnation) is None:
        raise IndexContractError('switch_parity_missing')
    # Initial-conversion recovery takes this lock bundle itself; seal first.
    installer = journal.seal_current_reader(state, project, executing_root=executing_root)
    marker = project_dir.runtime_file(project, fresh.MARKER)
    op = 'switch' if switching else 'reader_ready'
    with _migration_trace(state, op, op) as trace, ExitStack() as locks:
        locks.enter_context(_install_lock(state))
        if not locks.enter_context(build.owner(state)):
            raise IndexContractError('initial_build_busy')
        locks.enter_context(ledger._operation_lock(marker))
        locks.enter_context(Coordinator().quiesce_provider())
        if not locks.enter_context(_owner_lock(Path(decision['queuePath']), wait=True, deadline=deadline)):
            raise IndexContractError('sync_owner_busy')
        held = time.monotonic()
        quiesce = min(deadline, held + _SWITCH_SECONDS)
        if _migration_state(project, store) != (project, state, decision, node):
            raise IndexContractError('migration_route_changed')
        intent = _migration_intent(path, project=project, state=state, node=node, store=store)
        trace['migration'] = intent['migration_id']
        _migration_event(state, trace, 'attempt')
        if intent != expected:
            raise IndexContractError('migration_intent_changed')
        _switch_preconditions(project, state)
        parity, published = _bound_parity(intent), intent['published']
        receipt = shadow_parity_receipt(state, candidate=store._path, candidate_incarnation=store._incarnation)
        if receipt is None or parity is None or receipt['digest'] != parity['seal']:
            raise IndexContractError('switch_parity_missing')
        # 6: the executing root is proved to be the journal-selected bundle.
        _record, _raw, proof = journal._recheck(installer.endpoint)
        selected = journal.journal.inspect_journal(journal_path).get('selected') or {}
        commit = proof.get('sourceCommit')
        if (proof.get('productRouteAllowed') is not True
                or proof.get('executingPluginRoot') != str(executing_root)
                or selected.get('sourceCommit') != commit
                or selected.get('bundleManifestSha256') != proof.get('bundleManifestSha256')):
            raise IndexContractError('switch_bundle_mismatch')
        # 3-4: the receipts name this commit and the current CAS base.
        try:
            receipts = bridge.find_loaded_bridge(state, release_commit=commit)
        except ValueError as exc:
            raise IndexContractError('switch_bridge_missing') from exc
        base = journal.read_selection(journal_path)
        # 2: latest catch-up, with every writer held off by the owner lock.
        with leases.resolve_endpoint(project, action='recall', host_root=executing_root) as endpoint:
            if (endpoint.endpoint_id != intent['endpoint_id'] or endpoint.endpoint_id != base.preferred_endpoint
                    or endpoint.record['install_id'] != intent['install_id']):
                raise IndexContractError('switch_selection_changed')
            head = store.head(store.capture_view(intent['worktree_id'], deadline=quiesce))
            if (head['gen_id'] != published['generation'] or head['view_id'] != published['view_id']
                    or head['epoch'] != published['epoch']
                    or head['incarnation'] != published['candidate_incarnation']):
                raise IndexContractError('switch_catchup_stale')
            with readers.inspect_warm_snapshot(endpoint, deadline=quiesce) as snapshot:
                page = capture_effective(snapshot, endpoint=endpoint, decision=decision,
                                         after=None, deadline=quiesce)
                if (page['observation'] != intent['observation']
                        or _source_revision(snapshot) != intent['source_revision']):
                    raise IndexContractError('switch_catchup_stale')
        target = journal.seal_schema3_endpoint(installer, store_path=store._path,
            parity={key: receipt[key] for key in journal.PARITY_KEYS}, deadline=quiesce)
        record = target.record
        ready = {'migration_id': intent['migration_id'], 'generation': parity['generation'],
                 'candidate_incarnation': parity['candidate_incarnation'], 'parity_seal': parity['seal'],
                 'observation': intent['observation'], 'source_revision': intent['source_revision'],
                 'release_commit': commit, 'capability': record['capability'],
                 'endpoint_id': target.endpoint_id, 'store_incarnation': record['incarnation'],
                 'worktree_binding': record['worktree_binding'], 'floor': dict(record['head_floor']),
                 'expected_epoch': base.epoch, 'expected_endpoint': base.preferred_endpoint,
                 'transition_id': _transition_id(base.preferred_endpoint, target.endpoint_id)}
        if time.monotonic() >= quiesce:
            raise IndexContractError('reader_snapshot_deadline')
        if not switching:
            updated = _stamp({**intent, 'reader_ready': ready, 'switched': None})
            if ledger._read_json(path) != intent:
                raise IndexContractError('migration_intent_changed')
            ledger._write(path, updated, replace=True)
            trace['outcome'] = 'reader_ready'
            return updated
        moved = {key for key in ready if ready[key] != intent['reader_ready'].get(key)}
        if moved - _RESEAL_FIELDS:
            # The base, target or proof moved since reader_ready: prove it again.
            ledger._write(path, _stamp({**intent, 'reader_ready': None}), replace=True)
            trace.update(outcome='reader_ready_stale', reason='switch_reader_ready_stale')
            return None
        if moved:
            # Only the re-sealed target id moved (its record carries the
            # install journal epoch, which every sync wake's first-write mark
            # bumps). Record the new target before the CAS so crash recovery
            # (``_cas_landed``) names the committed transition.
            if ledger._read_json(path) != intent:
                raise IndexContractError('migration_intent_changed')
            intent = _stamp({**intent, 'reader_ready': ready})
            ledger._write(path, intent, replace=True)
        selection = journal.cas_reader(journal_path, expected_epoch=ready['expected_epoch'],
            expected_endpoint=ready['expected_endpoint'], target=target,
            transition_id=ready['transition_id'], loaded_bridge=(receipts, commit), deadline=quiesce)
        if not _cas_landed(ready, selection):
            raise IndexContractError('reader_selection_changed')
        # The journal selection is the fact from here on; record it.
        updated = _switched_intent(intent, selection)
        ledger._write(path, updated, replace=True)
        trace['outcome'] = 'switched'
        return updated


def _record_switched(store, project, state, node, expected, selection):
    """Record a CAS that committed before its intent write (crash recovery)."""
    import laya_fresh_wiki_origin as fresh
    import laya_ledger_common as ledger
    import project_dir
    from laya_product_install import _install_lock
    marker = project_dir.runtime_file(project, fresh.MARKER)
    path = state / _INTENT
    with _migration_trace(state, 'switch', 'switch') as trace, _install_lock(state), ledger._operation_lock(marker):
        intent = _migration_intent(path, project=project, state=state, node=node, store=store)
        trace['migration'] = intent['migration_id']
        _migration_event(state, trace, 'attempt')
        current = journal.read_selection(state / 'install-journal.sqlite')
        if intent != expected or not _cas_landed(intent['reader_ready'], current):
            raise IndexContractError('migration_intent_changed')
        updated = _switched_intent(intent, current)
        ledger._write(path, updated, replace=True)
        trace['outcome'] = 'switched'
        return updated


def advance_reader_ready(store, *, project, executing_root, deadline):
    """One U15 driver step after a re-proved shadow_verified head.

    shadow_verified -> prove the switch gates and record ``reader_ready``;
    reader_ready -> re-prove them and run the one journal CAS, then record
    ``switched``. A CAS that committed before its intent write is recorded
    without a second CAS. Returns ``{stage, migration_state[, reason]}``;
    stage ``retry`` means a gate did not hold and nothing was written (or a
    stale reader_ready record was dropped for a fresh proof).
    """
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.monotonic() >= deadline):
        raise IndexContractError('reader_snapshot_deadline')
    project, state, _, node = _migration_state(project, store)
    path = state / _INTENT
    intent = _migration_intent(path, project=project, state=state, node=node, store=store)
    current = intent['migration_state']
    if current == 'switched':
        return {'stage': 'switched', 'migration_state': current}

    def retry(reason):
        try:
            now = _migration_intent(path, project=project, state=state, node=node, store=store)
        except (OSError, ValueError):
            now = intent
        return {'stage': 'retry', 'reason': reason, 'migration_state': now['migration_state']}

    if current not in ('shadow_verified', 'reader_ready'):
        return retry('switch_parity_missing')
    try:
        if current == 'reader_ready':
            selection = journal.read_selection(state / 'install-journal.sqlite')
            if _cas_landed(intent['reader_ready'], selection):
                updated = _record_switched(store, project, state, node, intent, selection)
                _offer_sync(project)
                return {'stage': 'switched', 'migration_state': updated['migration_state']}
        updated = _switch_step(store, project=project, executing_root=executing_root,
                               deadline=deadline, expected=intent)
        if updated is None:
            return retry('switch_reader_ready_stale')
    except ValueError as exc:
        reason = str(exc)
        reason = 'switch_catchup_stale' if reason in _SWITCH_STALE else reason
        if reason not in _SWITCH_RETRY:
            raise
        return retry(reason)
    _offer_sync(project)
    return {'stage': updated['migration_state'], 'migration_state': updated['migration_state']}


def settle_switched(project):
    """Record ``switched`` when the journal already selects the recorded target.

    For a sync pass that finds a schema3 reader selected: a CAS that committed
    right before its intent write leaves the intent at reader_ready. Any other
    intent (absent, unreadable, another state, another transition) is left as
    it is and nothing is emitted. Returns True when it recorded the switch.
    """
    from pathlib import Path
    from types import SimpleNamespace
    import laya_ledger_common as ledger
    from laya_blob_store import _db_file
    try:
        project = Path(project)
        decision = journal.journal.select_route(str(project), action='recall')
        if decision.get('state') != 'candidate' or decision.get('projectRoot') != str(project):
            return False
        state = journal._state(Path(decision['journalPath']).parent)
        raw = ledger._read_json(state / _INTENT)
        if (not isinstance(raw, dict) or raw.get('migration_state') != 'reader_ready'
                or not isinstance(raw.get('published'), dict) or not isinstance(raw.get('candidate'), str)):
            return False
        candidate = Path(raw['candidate'])
        store = SimpleNamespace(_path=candidate, _incarnation=raw.get('candidate_incarnation'),
                                _policy=raw['published'].get('policy_digest'))
        node = _db_file(candidate)
        intent = _migration_intent(state / _INTENT, project=project, state=state, node=node, store=store)
        ready = _bound_reader_ready(intent)
        selection = journal.read_selection(state / 'install-journal.sqlite')
        if ready is None or not _cas_landed(ready, selection):
            return False
    except (OSError, ValueError, KeyError, TypeError):
        return False
    _record_switched(store, project, state, node, intent, selection)
    _offer_sync(project)
    return True


# Reader rollback (U16, design 4.6, decision K7): one reader CAS from the
# selected schema3 endpoint back to a v6 endpoint re-issued from the current
# install, then one laya-sync wake. While schema3 was selected the v6 writer
# plan refused (writer_selection_unavailable), so the v6 store is the one the
# switch left; the woken v6 sync re-indexes what changed since (no replay,
# K7). The journal is the record: the latest schema3 -> v6 transition is the
# rollback, and it holds the shadow driver (``rollback_hold``) until an
# explicit resume names it. Nothing is deleted (D4): v6 DB, sidecar, the
# schema3 candidate store, intent and activation all stay for PR10.
_ROLLBACK_SECONDS = 10.0
# Store identity the re-issued v6 endpoint must share with the v6 endpoint
# selected before the switch. Nodes, not paths: a state move renames them.
_ROLLBACK_STORE = ('store_format', 'capability', 'incarnation', 'profile', 'checkout_policy',
                   'db_node', 'catalog_node', 'binding_node', 'legacy_sidecar_baseline')
_ROLLBACK_PATHS = ('db_path', 'catalog_path', 'binding_path', 'sidecar_path')
# Busy locks, a concurrent CAS and a lifecycle intent the SessionStart worker
# still has to finish: nothing was written; ask again later.
ROLLBACK_BUSY = frozenset(('install_busy', 'cutover_operation_busy', 'sync_owner_busy', 'initial_build_busy',
                           'learning_compute_busy', 'reader_transition_busy', 'stale_reader_cas',
                           'reader_selection_changed', 'rollback_lifecycle_busy'))


def _reader_history(db):
    """``[(epoch, transition_id, preferred record, previous record or None)]`` by epoch."""
    rows = db.execute('SELECT epoch,transition_id,preferred_endpoint,previous_endpoint '
                      'FROM reader_transitions ORDER BY epoch').fetchall()
    records = {}

    def record(endpoint_id):
        if endpoint_id is None:
            return None
        if endpoint_id not in records:
            records[endpoint_id] = journal._stored_endpoint(db, endpoint_id)
        return records[endpoint_id]
    return [(epoch, transition, preferred, previous, record(preferred), record(previous))
            for epoch, transition, preferred, previous in rows]


def _last_rollback(history):
    for epoch, transition, preferred, previous, record, before in reversed(history):
        if (before is not None and before.get('store_format') == 'schema3'
                and record.get('store_format') == 'v6'):
            return {'transition_id': transition, 'reader_epoch': epoch, 'endpoint_id': preferred,
                    'previous_endpoint': previous}
    return None


def _read_history(journal_path):
    from pathlib import Path
    path = journal._journal(journal_path)
    with journal._readonly(path, Path(path).parent) as db:
        selection, record = journal._selection(db)
        return selection, record, _reader_history(db)


def rollback_hold(state):
    """The latest rollback transition when it still holds the shadow driver, else None.

    Read-only. A rollback holds the driver until an activation record names
    it in ``resumed_after`` (``laya_product_sync_worker.resume_shadow_migration``).
    """
    import os
    from pathlib import Path
    import laya_ledger_common as ledger
    state = Path(state)
    _selection, _record, history = _read_history(state / 'install-journal.sqlite')
    last = _last_rollback(history)
    if last is None:
        return None
    activation = state / 'shadow-migration-activation.json'
    record = ledger._read_json(activation) if os.path.lexists(activation) else None
    if isinstance(record, dict) and record.get('resumed_after') == last['transition_id']:
        return None
    return last


def reader_status(project):
    """Read-only reader selection, migration and rollback state of a selected main checkout."""
    import os
    from pathlib import Path
    import laya_ledger_common as ledger
    project = Path(project)
    decision = journal.journal.select_route(str(project), action='recall')
    if decision.get('state') != 'candidate' or decision.get('projectRoot') != str(project):
        raise IndexContractError('migration_route_invalid')
    state = journal._state(Path(decision['journalPath']).parent)
    selection, record, history = _read_history(state / 'install-journal.sqlite')

    def raw(name, key):
        try:
            value = ledger._read_json(state / name) if os.path.lexists(state / name) else None
        except (OSError, ValueError):
            return 'unreadable'
        return value.get(key) if isinstance(value, dict) else None
    return {'status': 'inspected', 'reader': {'epoch': selection.epoch, 'store_format': record['store_format'],
                                              'endpoint_id': selection.preferred_endpoint,
                                              'previous_endpoint': selection.previous_endpoint},
            'migration_state': raw(_INTENT, 'migration_state'),
            'candidate': raw('shadow-migration-activation.json', 'candidate'),
            'rollback': _last_rollback(history), 'held': rollback_hold(state) is not None}


def _rollback_transition_id(expected_endpoint, endpoint_id, epoch):
    # Fixed per (base, target, epoch): a retry after a committed CAS is its replay.
    return hashlib.sha256(('memvane-reader-rollback-v1\0%s\0%s\0%d' % (expected_endpoint, endpoint_id, epoch))
                          .encode('ascii')).hexdigest()[:32]


def _rollback_event(state, outcome, reason=None, ms=None):
    try:
        import event_log
        event_log.emit('migration.rollback', state_root=state, outcome=outcome, reason=reason, ms=ms)
    except Exception:
        pass


def rollback_reader(project, *, executing_root):
    """Select a v6 reader again on a schema3-selected install (explicit request only).

    Returns ``{status, transition_id, reader_epoch, endpoint_id,
    previous_endpoint}``: ``rolled_back`` after this call's CAS, or
    ``rollback_retained`` when the journal already records the rollback (a
    repeated request, or one whose process died after the CAS); both offer
    one laya-sync wake. Refuses ``rollback_not_switched`` when v6 is selected
    and never rolled back, ``rollback_v6_changed`` when the v6 store is not
    the one the switch left, ``reader_successor_unproved`` when a newer
    bundle attestation is not on the schema3 record's sealed successor chain,
    and every busy lock with its code (``ROLLBACK_BUSY``) without writing
    anything.
    """
    from contextlib import ExitStack
    from pathlib import Path
    import laya_fresh_wiki_origin as fresh
    import laya_initial_build as build
    import laya_ledger_common as ledger
    import project_dir
    from context_learning.resource_coordination import Coordinator
    from laya_index_sync import _owner_lock
    from laya_product_install import _install_lock
    started = time.monotonic()
    project, executing_root = Path(project), Path(executing_root)
    if not project.is_absolute() or project.resolve(strict=True) != project:
        raise IndexContractError('migration_project_invalid')
    decision = journal.journal.select_route(str(project), action='recall')
    if decision.get('state') != 'candidate' or decision.get('projectRoot') != str(project):
        # A fenced route reads as blocked until the SessionStart worker
        # settles the upgrade; that is busy (ask again), not a refusal.
        if fresh.upgrade_fenced(str(project)):
            raise IndexContractError('rollback_lifecycle_busy')
        raise IndexContractError('migration_route_invalid')
    state = journal._state(Path(decision['journalPath']).parent)
    path = state / 'install-journal.sqlite'

    def retained(history):
        last = _last_rollback(history)
        if last is None:
            raise IndexContractError('rollback_not_switched')
        _offer_sync(project, start=True)
        _rollback_event(state, 'retained')
        return {'status': 'rollback_retained', **last}

    selection, record, history = _read_history(path)
    if record['store_format'] != 'schema3':
        return retained(history)
    # Initial-conversion recovery takes the lock bundle itself; seal first.
    installer = journal.seal_current_reader(state, project, executing_root=executing_root)
    try:
        with ExitStack() as locks:
            locks.enter_context(_install_lock(state))
            if not locks.enter_context(build.owner(state)):
                raise IndexContractError('initial_build_busy')
            locks.enter_context(ledger._operation_lock(project_dir.runtime_file(project, fresh.MARKER)))
            locks.enter_context(Coordinator().quiesce_provider())
            if not locks.enter_context(_owner_lock(Path(decision['queuePath']))):
                raise IndexContractError('sync_owner_busy')
            deadline = time.monotonic() + _ROLLBACK_SECONDS
            if journal.journal.select_route(str(project), action='recall') != decision:
                raise IndexContractError('migration_route_changed')
            _lifecycle_idle(project, state, 'rollback_lifecycle_busy')
            # The owner re-seals a schema3 record whose recall settings changed,
            # so the v6 target (current settings) shares the install identity.
            # When the schema3 store cannot be read, the CAS admits the
            # settings difference (``rollback_pair``) on the v6 proof below; a
            # newer attestation (a bundle upgrade since the switch) also needs
            # the sealed successor chain, which ``cas_reader`` checks.
            try:
                journal.resettle_selection(state, project, executing_root)
            except (OSError, ValueError):
                pass  # An unreadable schema3 store must not block its rollback.
            selection, record, history = _read_history(path)
            if record['store_format'] != 'schema3':
                return retained(history)
            before = next((stored for *_row, stored, _prev in reversed(history)
                           if stored.get('store_format') == 'v6'), None)
            if before is None:
                raise IndexContractError('rollback_not_switched')
            target = installer.endpoint
            current = journal._recheck(target)[0]
            if (any(current.get(key) != before.get(key) for key in _ROLLBACK_STORE)
                    or any(Path(current[key]).name != Path(before[key]).name for key in _ROLLBACK_PATHS)):
                raise IndexContractError('rollback_v6_changed')
            transition = _rollback_transition_id(selection.preferred_endpoint, target.endpoint_id,
                                                 selection.epoch)
            landed = journal.cas_reader(path, expected_epoch=selection.epoch,
                                        expected_endpoint=selection.preferred_endpoint, target=target,
                                        transition_id=transition, deadline=deadline)
    except (ValueError, RuntimeError) as exc:
        _rollback_event(state, 'refused', reason=str(exc), ms=(time.monotonic() - started) * 1000)
        raise
    _offer_sync(project, start=True)
    _rollback_event(state, 'rolled_back', ms=(time.monotonic() - started) * 1000)
    return {'status': 'rolled_back', 'transition_id': landed.transition_id, 'reader_epoch': landed.epoch,
            'endpoint_id': landed.preferred_endpoint, 'previous_endpoint': landed.previous_endpoint}


# Compatible legacy vectors (design 8.2-5, decision K5). The migrated
# generation stays lexical-only: a legacy proof refuses a vector_spec in
# ViewStore.prepare_generation, and a vector generation is zero-or-complete,
# so a partially compatible corpus cannot be sealed as one. Compatible v6
# vectors are copied into the shared content tables (chunk_sets and
# embedding_sets) only; every other eligible member is marked
# ``budget-deferred`` for the independent new-recipe backfill. Nothing here
# loads a provider, embeds text or changes the selected reader.
_VECTOR_DEFERRED = ('profile_incompatible', 'vector_rows_absent', 'chunk_positions_differ',
                    'multi_chunk_unreconstructable', 'input_differs')
_VECTOR_RECORD = ('migration_id', 'generation', 'candidate_incarnation', 'recipe_id', 'profile_id',
                  'cursor', 'complete', 'total', 'copied', 'deferred', 'digest')
_VECTOR_PARITY_ABS = 1e-6


def _check_vector_record(record):
    """Shape only; _bound_vectors decides whether a record still applies."""
    import laya_blob_values as values
    import laya_view_values as view_values
    values.exact(record, _VECTOR_RECORD)
    values.identifier(record['migration_id'], values.UUID)
    if not isinstance(record['generation'], str) or not view_values.GEN_ID.fullmatch(record['generation']):
        raise IndexContractError('migration_intent_invalid')
    for key in ('candidate_incarnation', 'recipe_id', 'profile_id', 'digest'):
        values.identifier(record[key], values.UUID if key == 'candidate_incarnation' else values.SHA)
    if type(record['complete']) is not bool or (record['cursor'] is not None and (
            type(record['cursor']) is not str or not _valid_relative_path(record['cursor']))):
        raise IndexContractError('migration_intent_invalid')
    if record['complete'] != (record['cursor'] is None):
        raise IndexContractError('migration_intent_invalid')
    values.exact(record['deferred'], _VECTOR_DEFERRED)
    total = values.integer(record['total'], 0, MAX_DOCS)
    counted = values.integer(record['copied'], 0, MAX_DOCS) + sum(
        values.integer(record['deferred'][key], 0, MAX_DOCS) for key in _VECTOR_DEFERRED)
    if counted > total or (record['complete'] and counted != total):
        raise IndexContractError('migration_intent_invalid')
    return record


def _vector_spec(vector_spec):
    import laya_view_vectors as view_vectors
    try:
        spec = view_vectors.spec(vector_spec)
    except Exception as exc:
        raise IndexContractError('migration_vector_spec_invalid') from exc
    if spec is None:
        raise IndexContractError('migration_vector_spec_invalid')
    return spec


def _bound_vectors(intent, spec):
    """The vector record bound to the current private head and spec, else None.

    A restart replaces ``published``, so an older record stops applying without
    any restart path having to clear it.
    """
    record, receipt = intent.get('vectors'), intent.get('published')
    if (record is None or receipt is None or record['migration_id'] != intent['migration_id']
            or record['generation'] != receipt['generation']
            or record['candidate_incarnation'] != receipt['candidate_incarnation']
            or record['recipe_id'] != spec['recipe_id'] or record['profile_id'] != spec['profile_id']):
        return None
    return record


def _vectors_pending(intent, vector_spec):
    record = _bound_vectors(intent, _vector_spec(vector_spec))
    return record is None or not record['complete']


def _legacy_vector_set(base, *, scope, generation, paths, blob, seal, deadline):
    """The sealed v6 chunk set bound to one effective member, re-proved row by row."""
    import struct
    import laya_index_vector as legacy
    found = []
    for path in paths:
        found.extend(base.execute(
            'SELECT r.title,r.profile_id,COUNT(*) FROM gen_vector_rows r JOIN membership m '
            'ON m.gen=r.gen AND m.scope=r.scope AND m.path=r.path AND m.hash=r.hash '
            'WHERE r.gen=? AND r.scope=? AND r.path=? AND r.hash=? AND m.eligible=1 '
            'GROUP BY r.title,r.profile_id', (generation, scope, path, blob)).fetchall())
    if not found:
        return None
    if len(found) != 1 or found[0][1] != seal['profile_id']:
        raise IndexContractError('migration_vector_invalid')
    title, profile_id, count = found[0]
    dimension = seal['dimension']
    rows = legacy._read_chunk_set(base, blob, title, profile_id, dimension, deadline=deadline)
    if rows is None or len(rows) != count:
        raise IndexContractError('migration_vector_invalid')
    evidence, result = legacy.source_evidence(blob, title), []
    for row in rows:
        vector = row.get('vector')
        if not isinstance(vector, (list, tuple)) or len(vector) != dimension:
            raise IndexContractError('migration_vector_invalid')
        try:
            raw = struct.pack('<' + 'f' * dimension, *vector)
        except (struct.error, TypeError, OverflowError) as exc:
            raise IndexContractError('migration_vector_invalid') from exc
        # Copied bytes must be the sealed bytes: re-derive the v6 row digest.
        if legacy._provenance(blob, title, profile_id, row['seq'], row['pos'], evidence, raw) != row['provenance']:
            raise IndexContractError('migration_vector_invalid')
        result.append({'seq': row['seq'], 'pos': row['pos'], 'embedding': raw})
    return title, result


# Step time a vectors page keeps for finishing: the closing checkout check,
# the intent premise, the page commit and the intent write.
_VECTOR_FINISH_RESERVE = 0.5


def _out_of_budget(stop_at):
    """Whether a vectors page should stop before its next member."""
    return stop_at is not None and time.monotonic() >= stop_at


# Deadline codes of the reads a vectors member makes (v6 snapshot, v6 chunk
# set, candidate store).
_MEMBER_DEADLINE_CODES = frozenset(('reader_snapshot_deadline', 'vector_deadline_exceeded',
                                    'fts_deadline_exceeded'))


def _deadline_reached(exc, deadline):
    """A member that ran out of the step budget, not a member that failed."""
    if str(exc) in _MEMBER_DEADLINE_CODES:
        return True
    return (isinstance(exc, sqlite3.OperationalError) and str(exc) == 'interrupted'
            and time.monotonic() >= deadline)


def _classify_vectors(store, base, *, scope, generation, members, names, seal, profile, spec,
                      deadline, write, stop_at=None, session=None):
    """Classify a page of candidate members; copy exact-input sets when ``write``.

    The page runs in one candidate-store session (``BlobStore._vector_page``):
    the store header, blob admissions, recipe, profile and each member's
    inputs are proved once. Each member is its own savepoint. With
    ``stop_at`` the page stops before a member once that time has passed,
    and a member that runs out of the step deadline is rolled back alone;
    the returned rows are then a prefix of ``members`` (at least one row,
    else ``reader_snapshot_deadline``) and the caller records progress up to
    the last of them.

    Input equality is proved per row against ``BlobStore.vector_input_sha``.
    Without a candidate chunk set the reconstructable v6 inputs are the
    provider's first chunking pass (``qmd_first_pass``; one chunk at
    position 0 for a short document): v6 positions equal to it name those
    texts, the chunks the view recipe gives a document the provider did not
    re-split, and the equal embedding fingerprint commits that chunk recipe.
    A re-split document (``multi_chunk_unreconstructable``) and anything else
    waits for the new-recipe backfill.
    """
    if session is None:
        with store._vector_page(write=write) as own:
            return _classify_vectors(store, base, scope=scope, generation=generation, members=members,
                                     names=names, seal=seal, profile=profile, spec=spec,
                                     deadline=deadline, write=write, stop_at=stop_at, session=own)
    recipe_id, profile_id = spec['recipe_id'], spec['profile_id']
    compatible = all(profile[key] == seal[key] for key in ('model_id', 'embedding_fingerprint', 'dimension'))
    page = []
    if stop_at is not None:
        # The candidate store's own SQL also stops at the member deadline.
        store._db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    try:
        for path, blob in members:
            if time.monotonic() >= deadline or (page and _out_of_budget(stop_at)):
                if page and stop_at is not None:
                    break
                raise IndexContractError('reader_snapshot_deadline')
            try:
                with session.member():
                    row = _classify_member(store, base, scope=scope, generation=generation,
                                           path=path, blob=blob, names=names, seal=seal,
                                           compatible=compatible, recipe_id=recipe_id,
                                           profile_id=profile_id, deadline=deadline, write=write)
            except Exception as exc:
                if stop_at is not None and _deadline_reached(exc, deadline):
                    if page:
                        break
                    raise IndexContractError('reader_snapshot_deadline') from exc
                raise
            page.append(row)
    finally:
        if stop_at is not None:
            store._db.set_progress_handler(None, 0)
    return page


def _classify_member(store, base, *, scope, generation, path, blob, names, seal, compatible,
                     recipe_id, profile_id, deadline, write):
    """One member's classification row; copies its exact-input set when ``write``."""
    import laya_blob_values as values
    from laya_blob_values import BlobStoreError
    from laya_view_vectors import qmd_first_pass

    def defer(reason):
        return {'path': path, 'blob_sha': blob, 'status': 'budget-deferred', 'reason': reason,
                'embedding_digest': None}
    if not compatible:
        return defer('profile_incompatible')
    legacy = _legacy_vector_set(base, scope=scope, generation=generation,
                                paths=[name + '/' + path for name in names], blob=blob,
                                seal=seal, deadline=deadline)
    if legacy is None:
        return defer('vector_rows_absent')
    title, old = legacy
    try:
        chunks = store.chunks(blob, recipe_id)
    except BlobStoreError as exc:
        if exc.reason != 'chunk_set_missing':
            raise
        chunks = None
    if chunks is not None:
        if [row['position'] for row in chunks] != [row['pos'] for row in old]:
            return defer('chunk_positions_differ')
        texts = [row['text'] for row in chunks]
    else:
        body = store.document(blob)['body']
        parts = qmd_first_pass(body)
        if [row['pos'] for row in old] != [pos for pos, _text in parts]:
            return defer('multi_chunk_unreconstructable')
        if values.markdown_title(body, blob) != title:
            # Checked before any write: a derived chunk set is stored only
            # together with the copied set it explains.
            return defer('input_differs')
        texts = [text for _pos, text in parts]
    expected = [values.hash_bytes(('title: ' + title + ' | text: ' + text).encode('utf8')) for text in texts]
    if chunks is None:
        if not write:
            return defer('input_differs')
        store.put_chunk_set(blob, recipe_id, [{'seq': seq, 'position': pos, 'text': text,
                                               'text_sha': values.hash_bytes(text.encode('utf8'))}
                                              for seq, (pos, text) in enumerate(parts)])
    actual = [store.vector_input_sha(blob, recipe_id, profile_id, seq) for seq in range(len(expected))]
    if actual != expected:
        if chunks is None:
            raise IndexContractError('migration_vector_invalid')
        return defer('input_differs')
    rows = [{'seq': row['seq'], 'input_sha': sha, 'embedding': row['embedding']}
            for row, sha in zip(old, expected)]
    if write:
        digest = store.put_embedding_set(blob, recipe_id, profile_id, rows)
    else:
        try:
            saved = store.vectors(blob, recipe_id, profile_id)
        except BlobStoreError as exc:
            if exc.reason != 'embedding_set_missing':
                raise
            return defer('input_differs')
        if saved != rows:
            raise IndexContractError('migration_vector_invalid')
        digest = values.embedding_digest(blob, recipe_id, profile_id, rows)
    return {'path': path, 'blob_sha': blob, 'status': 'copied', 'reason': None,
            'embedding_digest': digest}


@contextmanager
def _vector_session(store, project, executing_root, *, deadline, observed=False, timings=None):
    """Locked intent/endpoint proof, pinned private head and pinned v6 image.

    The installer and operation locks are held for the whole bounded step,
    like advance_migration, so the recorded intent cannot move underneath.
    ``observed`` (``advance_vectors`` only): the pin skips its own checkout
    observations because the caller observes the checkout once more before
    it records anything; every other caller keeps the pin's own.
    """
    from pathlib import Path
    import laya_fresh_wiki_origin as fresh
    import laya_ledger_common as ledger
    import project_dir
    from laya_product_install import _install_lock
    project, state, decision, node = _migration_state(project, store)
    marker = project_dir.runtime_file(project, fresh.MARKER)
    with _install_lock(state), ledger._operation_lock(marker):
        if _migration_state(project, store) != (project, state, decision, node):
            raise IndexContractError('migration_route_changed')
        path = state / _INTENT
        intent = _migration_intent(path, project=project, state=state, node=node, store=store)
        _completed_shadow_commitments(intent)
        receipt = intent.get('published')
        if receipt is None:
            raise IndexContractError('migration_publication_unrecorded')
        with leases.resolve_endpoint(project, action='recall', host_root=Path(executing_root)) as endpoint:
            if (endpoint.endpoint_id != intent['endpoint_id']
                    or endpoint.record['install_id'] != intent['install_id']):
                raise IndexContractError('migration_endpoint_changed')
            timings = {} if timings is None else timings
            clock = time.monotonic()
            handle = store.capture_view(intent['worktree_id'], deadline=deadline)
            timings['capture_ms'] = int((time.monotonic() - clock) * 1000)
            clock = time.monotonic()
            pin = store.pin_view(handle, deadline=deadline, _observed=observed)
            timings['pin_verify_ms'] = int((time.monotonic() - clock) * 1000)
            try:
                if (pin.gen_id != receipt['generation'] or pin.view_id != receipt['view_id']
                        or pin.epoch != receipt['epoch'] or pin.incarnation != receipt['candidate_incarnation']):
                    raise IndexContractError('migration_snapshot_changed')
                clock = time.monotonic()
                with readers.inspect_warm_snapshot(endpoint, deadline=deadline) as snapshot:
                    timings['inspect_ms'] = int((time.monotonic() - clock) * 1000)
                    page = capture_effective(snapshot, endpoint=endpoint, decision=decision,
                                             after=None, deadline=deadline)
                    if page['observation'] != intent['observation']:
                        raise IndexContractError('migration_snapshot_changed')
                    if _source_revision(snapshot) != intent['source_revision']:
                        raise IndexContractError('migration_source_revision_unproved')
                    seal = snapshot.vector_seal
                    if (seal is None or seal.get('generation') != snapshot.published[0]
                            or seal.get('scope') != decision['scopeId']):
                        raise IndexContractError('migration_vector_invalid')
                    yield dict(state=state, path=path, intent=intent, decision=decision, endpoint=endpoint,
                               snapshot=snapshot, pin=pin, seal=dict(seal),
                               names=tuple(wiki_index_roots(decision['collections'])))
            finally:
                store.release(pin)


def _candidate_members(store, generation, after, limit):
    with store._session() as db:
        if after is None:
            rows = db.execute('SELECT path,blob_sha FROM wiki_members WHERE gen_id=? AND eligible=1 '
                              'ORDER BY path LIMIT ?', (generation, limit)).fetchall()
        else:
            rows = db.execute('SELECT path,blob_sha FROM wiki_members WHERE gen_id=? AND eligible=1 '
                              'AND path>? ORDER BY path LIMIT ?', (generation, after, limit)).fetchall()
        return [(row[0], row[1]) for row in rows]


def advance_vectors(store, *, project, executing_root, vector_spec, deadline):
    """Copy one page of exact-input legacy vectors and mark the rest budget-deferred.

    Grants no parity and never changes the selected reader or the private head.
    """
    import laya_blob_values as values
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.monotonic() >= deadline):
        raise IndexContractError('reader_snapshot_deadline')
    deadline = min(deadline, time.monotonic() + _STEP_SECONDS)
    spec = _vector_spec(vector_spec)
    profile = store._profile(spec['profile_id'])
    store._recipe(spec['recipe_id'])
    _, state, _, _ = _migration_state(project, store)
    # One checkout observation opens the step (capture_view) and one closes
    # it before anything is committed.
    timings = {}
    with _migration_trace(state, 'vectors', 'vectors') as trace, \
            _vector_session(store, project, executing_root, deadline=deadline, observed=True,
                            timings=timings) as proof:
        trace['fields'] = timings
        intent, receipt = proof['intent'], proof['intent']['published']
        trace['migration'] = intent['migration_id']
        _migration_event(state, trace, 'attempt')
        record = _bound_vectors(intent, spec)
        if record is not None and record['complete']:
            trace['outcome'] = 'vectors_complete'
            return intent
        if record is None:
            with store._session() as db:
                total = db.execute('SELECT COUNT(*) FROM wiki_members WHERE gen_id=? AND eligible=1',
                                   (receipt['generation'],)).fetchone()[0]
            record = {'migration_id': intent['migration_id'], 'generation': receipt['generation'],
                      'candidate_incarnation': receipt['candidate_incarnation'],
                      'recipe_id': spec['recipe_id'], 'profile_id': spec['profile_id'],
                      'cursor': None, 'complete': False, 'total': values.integer(total, 0, MAX_DOCS),
                      'copied': 0, 'deferred': {key: 0 for key in _VECTOR_DEFERRED},
                      'digest': values.digest('memvane-shadow-vectors-v1', {
                          'migration_id': intent['migration_id'], 'generation': receipt['generation'],
                          'recipe_id': spec['recipe_id'], 'profile_id': spec['profile_id']})}
        members = _candidate_members(store, receipt['generation'], record['cursor'], MAX_BATCH)
        snapshot = proof['snapshot']
        import laya_ledger_common as ledger
        # Members get the step budget minus a reserve; the reserve finishes
        # the page (closing checkout check, intent premise) before the page
        # commits. Anything that does not fit rolls the whole page back, so
        # the candidate store never holds sets the intent does not record.
        member_deadline = deadline - _VECTOR_FINISH_RESERVE
        timings['members'] = 0
        if time.monotonic() >= member_deadline:
            raise _VectorsWarmed()
        prior_busy = store._db.execute('PRAGMA busy_timeout').fetchone()[0]
        store._db.execute('PRAGMA busy_timeout=' + str(max(1, int((member_deadline - time.monotonic()) * 1000))))
        try:
            with store._vector_page(write=True) as session:
                try:
                    with readers._validation_deadline(snapshot.connections, member_deadline):
                        page = _classify_vectors(store, snapshot.connections[0],
                            scope=proof['decision']['scopeId'], generation=snapshot.published[0],
                            members=members, names=proof['names'], seal=proof['seal'], profile=profile,
                            spec=spec, deadline=member_deadline, write=True, stop_at=member_deadline,
                            session=session)
                except ValueError as exc:
                    if members and _deadline_reached(exc, member_deadline):
                        # Not one member fit after the step's fixed costs: the
                        # page rolls back; the pass's next step runs it warm.
                        raise _VectorsWarmed() from exc
                    raise
                timings['members'] = len(page)
                deferred = dict(record['deferred'])
                for row in page:
                    if row['status'] != 'copied':
                        deferred[row['reason']] += 1
                if len(page) < len(members):
                    # The member budget ran out inside the page: record
                    # progress up to the last member it finished.
                    cursor = page[-1]['path']
                else:
                    cursor = members[-1][0] if len(members) == MAX_BATCH else None
                updated_record = _check_vector_record({**record, 'cursor': cursor, 'complete': cursor is None,
                    'copied': record['copied'] + sum(row['status'] == 'copied' for row in page),
                    'deferred': deferred,
                    'digest': values.digest('memvane-shadow-vectors-page-v1', {'previous': record['digest'],
                                                                              'page': page, 'next': cursor})})
                updated = {**intent, 'vectors': updated_record}
                try:
                    store._observe(store._pinned(proof['pin'])['entry'], deadline=deadline,
                                   timeout_reason='reader_snapshot_deadline')
                except values.BlobStoreError as exc:
                    if exc.reason == 'checkout_changed':
                        raise IndexContractError('migration_checkout_changed') from exc
                    raise
                if time.monotonic() >= deadline:
                    raise IndexContractError('reader_snapshot_deadline')
                if ledger._read_json(proof['path']) != intent:
                    raise IndexContractError('migration_intent_changed')
                trace['stage'] = 'intent'
            # Committed. Only the intent write follows: an I/O failure there
            # leaves sets the next page re-verifies, never a cursor ahead of
            # the store.
        finally:
            store._db.execute('PRAGMA busy_timeout=' + str(prior_busy))
        ledger._write(proof['path'], updated, replace=True)
        trace['outcome'] = 'vectors_complete' if updated_record['complete'] else 'progress'
        return updated


def compare_vector_queries(store, generation, *, project, executing_root, vector_spec, query_vectors,
                           limit, deadline):
    """Compare old/new exact semantic top-k on the copied subset; issues no receipt.

    The old lane is the actual pinned v6 exact search with every member
    outside the subset excluded; the new lane scores the candidate's validated
    embedding sets with the view store's own exact scoring.
    """
    import laya_blob_values as values
    import laya_index_vector as legacy
    import laya_view_values as view_values
    import laya_view_vectors as view_vectors
    from laya_index_a import SnapshotIndex
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.monotonic() >= deadline):
        raise IndexContractError('reader_snapshot_deadline')
    deadline = min(deadline, time.monotonic() + _STEP_SECONDS)
    if not isinstance(generation, str) or not view_values.GEN_ID.fullmatch(generation):
        values.fail('generation_id_invalid')
    if (type(query_vectors) is not tuple or not 1 <= len(query_vectors) <= 32
            or type(limit) is not int or not 1 <= limit <= view_vectors.MAX_LIMIT):
        raise IndexContractError('migration_vector_queries_invalid')
    spec = _vector_spec(vector_spec)
    profile = store._profile(spec['profile_id'])
    store._recipe(spec['recipe_id'])
    dimension = profile['dimension']
    queries = []
    for value in query_vectors:
        query = view_vectors.query_values(value)
        if len(query) != dimension:
            values.fail('vector_query_invalid')
        queries.append((list(value), query))
    _, state, _, _ = _migration_state(project, store)
    with _migration_trace(state, 'vector_compare', 'vectors') as trace, \
            _vector_session(store, project, executing_root, deadline=deadline) as proof:
        intent, snapshot = proof['intent'], proof['snapshot']
        trace['migration'] = intent['migration_id']
        _migration_event(state, trace, 'attempt')
        record = _bound_vectors(intent, spec)
        if record is None or not record['complete'] or record['generation'] != generation:
            raise IndexContractError('migration_vectors_incomplete')
        decision, seal, base = proof['decision'], proof['seal'], snapshot.connections[0]
        if any(profile[key] != seal[key] for key in ('model_id', 'embedding_fingerprint', 'dimension')):
            trace['outcome'] = 'compared'
            return {'subset': 0, 'total': record['total'], 'compared': 0, 'semantic_status': 'no_compatible_vectors'}
        with readers._validation_deadline(snapshot.connections, deadline):
            members, after = [], None
            while True:
                chunk = _candidate_members(store, generation, after, MAX_BATCH)
                members.extend(chunk)
                if len(chunk) < MAX_BATCH:
                    break
                after = chunk[-1][0]
            page = _classify_vectors(store, base, scope=decision['scopeId'], generation=snapshot.published[0],
                members=members, names=proof['names'], seal=seal, profile=profile, spec=spec,
                deadline=deadline, write=False)
            subset = {row['path']: row['blob_sha'] for row in page if row['status'] == 'copied'}
            if len(subset) != record['copied']:
                raise IndexContractError('migration_vector_invalid')
            if not subset:
                trace['outcome'] = 'compared'
                return {'subset': 0, 'total': record['total'], 'compared': 0,
                        'semantic_status': 'no_compatible_vectors'}
            prefixes = tuple(name + '/' for name in proof['names'])
            kept = {name + '/' + path for path in subset for name in proof['names']}
            excluded = tuple(sorted(row[0] for row in base.execute(
                'SELECT m.path FROM membership m WHERE m.gen=? AND m.scope=? AND m.eligible=1 '
                'AND EXISTS (SELECT 1 FROM json_each(?) p WHERE substr(m.path,1,length(p.value))=p.value)',
                (snapshot.published[0], decision['scopeId'], json.dumps(prefixes))) if row[0] not in kept))
            with store._session() as db:
                work = sum(db.execute('SELECT chunk_count FROM embedding_sets WHERE blob_sha=? '
                                      'AND recipe_id=? AND profile_id=?',
                                      (blob, spec['recipe_id'], spec['profile_id'])).fetchone()[0]
                           for blob in set(subset.values())) * dimension
            if work > view_vectors.DEFAULT_WORK:
                trace['outcome'] = 'compared'
                return {'subset': len(subset), 'total': record['total'], 'compared': 0,
                        'semantic_status': view_vectors.DEFERRED[view_vectors.WORK_EXCEEDED]}
        # The v6 exact search owns the snapshot progress handler itself.
        index = SnapshotIndex(base, scope=decision['scopeId'], generation=snapshot.published[0],
                              incarnation=decision['dbIncarnation'], deadline=deadline, _observation=snapshot)
        token = index.pin()
        try:
            for raw, query in queries:
                if time.monotonic() >= deadline:
                    raise IndexContractError('reader_snapshot_deadline')
                old = legacy.LayaVectorIndex(index).search_exact(token, raw,
                    model_id=seal['model_id'], embedding_fingerprint=seal['embedding_fingerprint'],
                    dimension=dimension, limit=limit, max_float_work=view_vectors.DEFAULT_WORK,
                    path_prefixes=prefixes, excluded_paths=excluded or None)
                top, new_sets = [], {}
                for path, blob in sorted(subset.items()):
                    if blob not in new_sets:
                        new_sets[blob] = view_vectors._set_score(
                            query, store.vectors(blob, spec['recipe_id'], spec['profile_id']),
                            dimension, deadline)
                    view_vectors._keep(top, new_sets[blob], {'path': path}, limit)
                new = [(ref['path'], -negative) for negative, _, _, ref in top]
                mapped = [(row['path'].split('/', 1)[1], 1.0 - row['score']) for row in old]
                if ([path for path, _ in mapped] != [path for path, _ in new]
                        or any(abs(left - right) > _VECTOR_PARITY_ABS
                               for (_, left), (_, right) in zip(mapped, new))):
                    raise IndexContractError('migration_semantic_parity_mismatch')
        finally:
            index.release(token)
        trace['outcome'] = 'compared'
        return {'subset': len(subset), 'total': record['total'], 'compared': len(queries),
                'semantic_status': 'complete'}

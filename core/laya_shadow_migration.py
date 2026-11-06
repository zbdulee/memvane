"""Inactive migration capture from an issued, pinned v6 reader snapshot."""
import hashlib
from contextlib import contextmanager
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
_STEP_SECONDS = 3.0


class _MigrationObservationChanged(IndexContractError):
    def __init__(self, observation):
        super().__init__('migration_snapshot_changed')
        self.observation = observation


def _wiki_card_path(path, prefixes):
    return any(path.startswith(prefix) and not is_wiki_meta_page(path[len(prefix):])
               for prefix in prefixes)


def capture_effective(snapshot, *, endpoint, decision, after, deadline, include_ineligible=False,
                      _all_inventory=False):
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
            or (_all_inventory and (not include_ineligible or after is not None))):
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
        scope = decision['scopeId']
        excluded_paths, excluded_hashes = _exclusions(base, scope)
        def valid(path, digest):
            return (type(path) is str and _valid_relative_path(path)
                    and path.startswith(prefixes) and type(digest) is str
                    and re.fullmatch(r'[0-9a-f]{64}', digest) is not None)
        columns = 'm.path,m.hash,m.eligible' if include_ineligible else 'm.path,m.hash'
        eligibility = '' if include_ineligible else 'AND m.eligible=1 '
        rows = base.execute('SELECT ' + columns + ' FROM membership m WHERE m.gen=? AND m.scope=? ' + eligibility +
                            'AND EXISTS (SELECT 1 FROM json_each(?) p '
                            'WHERE substr(m.path,1,length(p.value))=p.value) ORDER BY m.path LIMIT ?',
                            (snapshot.published[0], scope, json.dumps(prefixes), MAX_DOCS + 1)).fetchall()
        if time.monotonic() >= deadline:
            raise IndexContractError('reader_snapshot_deadline')
        if len(rows) > MAX_DOCS:
            raise IndexContractError('migration_membership_limit')
        if include_ineligible:
            if any(type(row[2]) is not int or row[2] not in (0, 1) for row in rows):
                raise IndexContractError('migration_membership_invalid')
            members = {path: (digest, 'base', eligible) for path, digest, eligible in rows}
        else:
            members = {path: (digest, 'base', 1) for path, digest in rows}
        if len(members) != len(rows) or any(not valid(path, value[0]) for path, value in members.items()):
            raise IndexContractError('migration_membership_invalid')
        members = {path: value for path, value in members.items()
                   if _wiki_card_path(path, prefixes)}
        if side is not None:
            docs = side.execute('SELECT path,hash FROM docs ORDER BY path').fetchall()
            if len(docs) > MAX_DOCS or len({row[0] for row in docs}) != len(docs):
                raise IndexContractError('migration_sidecar_invalid')
            for path, digest in docs:
                if time.monotonic() >= deadline:
                    raise IndexContractError('reader_snapshot_deadline')
                if not valid(path, digest):
                    raise IndexContractError('migration_sidecar_invalid')
                if not _wiki_card_path(path, prefixes):
                    continue
                previous = members.get(path)
                if previous is not None and previous[0] == digest and previous[2] == 1:
                    continue
                members.pop(path, None)
                if include_ineligible or (path not in excluded_paths and digest not in excluded_hashes):
                    members[path] = (digest, 'sidecar', 1)
        paths = [path for path in sorted(members)
                 if (after is None or path > after) and (include_ineligible or
                 (path not in excluded_paths and members[path][0] not in excluded_hashes))]
        page = paths if _all_inventory else paths[:MAX_BATCH]
        def output(path):
            digest, source, eligible = members[path]
            result = {'path': path, 'hash': digest, 'source': source}
            if include_ineligible:
                result.update(eligible=eligible, excluded=path in excluded_paths or digest in excluded_hashes)
            return result
        return {'members': [output(path) for path in page], 'observation': observation,
                'next': {'after': page[-1], 'observation': observation}
                        if len(paths) > len(page) else None}


def capture_wiki_bytes(snapshot, *, endpoint, decision, after, deadline, _expected_observation=None):
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
                                 after=after, deadline=deadline)
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
                proved = journal._sidecar_document_snapshot(side, base, decision, member['path'],
                                                             deadline=deadline, require_vectors=False)
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
            document = journal._sidecar_document_snapshot(side, base, decision, path,
                deadline=deadline, require_vectors=False, _for_fts_corpus=True)
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
    args = (snapshot.published[0], decision['scopeId'], json.dumps(prefixes))
    with readers._validation_deadline(snapshot.connections, deadline):
        nonwiki = base.execute('SELECT 1 FROM membership m WHERE m.gen=? AND m.scope=? AND m.eligible=1 '
            'AND NOT EXISTS (SELECT 1 FROM json_each(?) p '
            'WHERE substr(m.path,1,length(p.value))=p.value) LIMIT 1', args).fetchone()
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
                      sidecar_fts_corpus=False, _expected_observation=None):
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
            or (verify_only and provenance_scan is None and not (base_fts_corpus or sidecar_fts_corpus))):
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
                         'binding_digest': row['binding_digest'], 'settings_sha': row['settings_sha'],
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
            import project_dir
            settings = Path(saved['settings_path'])
            current_settings = project_dir.settings_path(Path(saved['project_root']))
            if current_settings != settings:
                values.fail('wiki_admission_changed')
            raw_settings, _, _ = blobs._regular(settings.parent, settings.name, 1024 * 1024)
            wiki_fd = blobs._dirfd(Path(saved['wiki_root']))
            try:
                wiki_node = blobs._node(os.fstat(wiki_fd))
            finally:
                os.close(wiki_fd)
            if (values.hash_bytes(raw_settings) != saved['settings_sha']
                    or wiki_node != {'dev': saved['wiki_dev'], 'ino': saved['wiki_ino']}
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
        progress = _initial_corpus(identity, commitment['verification_digest'],
                                   'sidecar' if sidecar else 'base', corpus_observation)
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
        generation, _, _, stage = store._read_generation(db, pin.gen_id, record['entry']['handle'], deadline)
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
            generation, _, _, _ = store._read_generation(db, pin.gen_id, record['entry']['handle'], deadline)
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
        generation, seal, _, stage = store._read_generation(db, pin.gen_id, record['entry']['handle'], deadline)
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
                _, seal, _, _ = store._read_generation(db, pin.gen_id, record['entry']['handle'], deadline)
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
# Later states (shadow_verified, reader_ready, switched) belong to later
# steps. deferred/retry are restart_required plus a reason code, not states.
_SHADOW_STATES = ('copying', 'caught_up')
# Source changes proved by a later step; the copy restarts from a fresh scan.
_SHADOW_RESTART_REASONS = frozenset(('migration_snapshot_changed', 'migration_source_revision_unproved'))


def _shadow_state(intent):
    return 'caught_up' if intent.get('published') is not None else 'copying'


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


def _migration_event(state, trace, outcome, *, reason=None, ms=None):
    try:
        import event_log
        event_log.emit('migration.shadow', state_root=state, host='worker',
            outcome=outcome, reason=reason, ms=ms, stage=trace['stage'],
            migration=trace['migration'], attempt=trace['attempt'])
    except Exception:
        pass


@contextmanager
def _migration_trace(state, stage):
    """Emit one bounded terminal event without source text or exception messages."""
    import uuid
    trace = {'stage': stage, 'migration': None, 'outcome': None, 'attempt': uuid.uuid4().hex}
    started = time.monotonic()
    reason = None
    try:
        yield trace
    except Exception as exc:
        trace['outcome'] = 'error'
        value = str(exc)
        if value in _MIGRATION_REASONS:
            reason = value
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


def _revision_check(snapshot, *, endpoint, decision, previous, deadline):
    """Classify receipt continuity only; every change still triggers full recopy."""
    import laya_publication_revision as revisions
    if not any(item is snapshot and bound is endpoint and saved == decision
               for item, bound, saved in readers._observations.get()):
        raise IndexContractError('reader_snapshot_unpinned')
    readers._bound(endpoint)
    current = _source_revision(snapshot)
    old_rev, new_rev = previous['base_revision'], current['base_revision']
    base = 'unavailable'
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
                valid = True
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
                    except (KeyError, TypeError, ValueError):
                        valid = False
                    if not valid:
                        break
                if valid and prior_head == current['generation']:
                    base = 'unchanged' if new_rev == old_rev else 'continuous'
    old_side, new_side = previous['sidecar_revision'], current['sidecar_revision']
    sidecar = 'unavailable'
    if previous['sidecar_kind'] == current['sidecar_kind'] == 'sealed':
        sidecar = ('regressed' if new_side < old_side else
                   'unchanged' if new_side == old_side else 'advanced')
    elif previous['sidecar_kind'] == current['sidecar_kind'] == 'absent':
        sidecar = 'unchanged'
    return {'base': base, 'sidecar': sidecar}


def _migration_intent(path, *, project, state, node, store):
    import laya_blob_values as values
    import laya_ledger_common as ledger
    intent = ledger._read_json(path)
    if (not isinstance(intent, dict) or not _INTENT_KEYS <= set(intent)
            or set(intent) - _INTENT_KEYS - {'verification', 'corpora', 'restart_required', 'published',
                                            'prepared', 'revision_check'}
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
    values.exact(source, ('generation', 'base_revision', 'sidecar_kind', 'sidecar_revision'))
    if (not isinstance(source['generation'], str) or not source['generation']
            or source['sidecar_kind'] not in ('absent', 'legacy', 'sealed')
            or (source['sidecar_kind'] == 'sealed') != (source['sidecar_revision'] is not None)):
        raise IndexContractError('migration_intent_invalid')
    if source['base_revision'] is not None:
        values.integer(source['base_revision'])
    if source['sidecar_revision'] is not None:
        values.integer(source['sidecar_revision'])
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
    if intent['migration_state'] not in _SHADOW_STATES or intent['migration_state'] != _shadow_state(intent):
        raise IndexContractError('migration_intent_invalid')
    return intent


def _verification_progress(progress, intent, *, check_limits=True, cursor_observation=None):
    import laya_blob_values as values
    values.exact(progress, ('observation', 'scan_id', 'batch_limit', 'byte_limit',
                            'cursor', 'digest', 'pages', 'rows', 'complete'))
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
    return progress


def _initial_verification(intent):
    import laya_blob_values as values
    return {'observation': intent['observation'], 'scan_id': intent['provenance_scan']['id'],
        'batch_limit': MAX_BATCH, 'byte_limit': MAX_BATCH_BYTES,
        'cursor': None, 'digest': values.digest('memvane-shadow-verification-v1', {
            'migration': intent['migration_id'], 'observation': intent['observation'],
            'scan': intent['provenance_scan'], 'batch_limit': MAX_BATCH,
            'byte_limit': MAX_BATCH_BYTES}), 'pages': 0, 'rows': 0, 'complete': False}


def _fold_verification(progress, page):
    import laya_blob_values as values
    rows = progress['rows'] + len(page['inventory'])
    if rows > MAX_DOCS or progress['pages'] >= MAX_DOCS + 1:
        raise IndexContractError('migration_membership_limit')
    return {**progress, 'cursor': page['next'], 'pages': progress['pages'] + 1,
        'rows': rows, 'complete': page['next'] is None,
        'digest': values.digest('memvane-shadow-verification-page-v1', {
            'previous': progress['digest'], 'inventory': page['inventory'],
            'trusted': page['trusted'], 'next': page['next']})}


def _initial_corpus(intent, verification_digest, key, corpus_observation):
    import laya_blob_values as values
    return {'observation': intent['observation'], 'scan_id': intent['provenance_scan']['id'],
        'batch_limit': MAX_BATCH, 'byte_limit': MAX_BATCH_BYTES,
        'cursor': None, 'pages': 0, 'rows': 0, 'complete': False,
        'digest': values.digest('memvane-shadow-corpus-v1', {
            'migration': intent['migration_id'], 'verification': verification_digest,
            'corpus': key, 'observation': corpus_observation,
            'batch_limit': MAX_BATCH, 'byte_limit': MAX_BATCH_BYTES})}


def _fold_corpus(progress, page):
    import laya_blob_values as values
    rows = progress['rows'] + len(page['corpus'])
    if rows > MAX_DOCS or progress['pages'] >= MAX_DOCS + 1:
        raise IndexContractError('migration_membership_limit')
    return {**progress, 'cursor': page['next'], 'pages': progress['pages'] + 1,
        'rows': rows, 'complete': page['next'] is None,
        'digest': values.digest('memvane-shadow-corpus-page-v1', {
            'previous': progress['digest'], 'corpus': page['corpus'], 'next': page['next']})}


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
    with _migration_trace(state, 'lock') as trace, _install_lock(state), ledger._operation_lock(marker):
        if _migration_state(project, store) != (project, state, decision, node):
            raise IndexContractError('migration_route_changed')
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
    with _migration_trace(state, 'lock') as trace, _install_lock(state), ledger._operation_lock(marker):
        if _migration_state(project, store) != (project, state, decision, node):
            raise IndexContractError('migration_route_changed')
        trace['stage'] = 'intent'
        path = state / _INTENT
        intent = _migration_intent(path, project=project, state=state, node=node, store=store)
        trace['migration'] = intent['migration_id']
        _migration_event(state, trace, 'attempt')
        trace['stage'] = 'endpoint'
        with leases.resolve_endpoint(project, action='recall', host_root=Path(executing_root)) as endpoint:
            if (endpoint.endpoint_id != intent['endpoint_id']
                    or endpoint.record['install_id'] != intent['install_id']):
                raise IndexContractError('migration_endpoint_changed')
            trace['stage'] = 'snapshot'
            with readers.inspect_warm_snapshot(endpoint, deadline=deadline) as snapshot:
                def restart(observation):
                    import uuid
                    check = _revision_check(snapshot, endpoint=endpoint, decision=decision,
                                            previous=intent['source_revision'], deadline=deadline)
                    if check['base'] == 'gap':
                        trace['reason'] = 'revision_gap'
                    return _stamp({**intent, 'observation': observation, 'cursor': None, 'phase': 'copying',
                            'source_revision': _source_revision(snapshot),
                            'revision_check': check,
                            'verification': None, 'corpora': None, 'restart_required': False, 'published': None,
                            'prepared': None,
                            'provenance_scan': {'id': uuid.uuid4().hex, 'observed_ns': time.time_ns()}})
                if intent['phase'] == 'copied':
                    observed = capture_effective(snapshot, endpoint=endpoint, decision=decision,
                                                 after=None, deadline=deadline)
                    if observed['observation'] != intent['observation']:
                        updated, outcome = restart(observed['observation']), 'restarted'
                    elif _source_revision(snapshot) != intent['source_revision']:
                        raise IndexContractError('migration_source_revision_unproved')
                    else:
                        progress = intent.get('verification')
                        if progress is not None:
                            _verification_progress(progress, intent, check_limits=False)
                        if progress is not None and (progress['batch_limit'] != MAX_BATCH
                                                     or progress['byte_limit'] != MAX_BATCH_BYTES):
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
                        # replay all paths and never publish the stale cursor.
                        updated, outcome = restart(changed.observation), 'restarted'
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
    with _migration_trace(state, 'lock') as trace, _install_lock(state), ledger._operation_lock(marker):
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
                updated = {**intent, 'verification': _fold_verification(progress, page)}
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
    with _migration_trace(state, 'lock') as trace, _install_lock(state), ledger._operation_lock(marker):
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
            progress = _initial_corpus(intent, verification['digest'], key, corpus_observation)
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
    with _migration_trace(state, 'restart_pending') as trace, _install_lock(state), ledger._operation_lock(marker):
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
    with _migration_trace(state, 'prepared') as trace, _install_lock(state), ledger._operation_lock(marker):
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
    with _migration_trace(state, 'prepare') as trace, ExitStack() as lifetime:
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
                     'provenance_scan': intent['provenance_scan'],
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
    with _migration_trace(state, 'publish') as trace, _install_lock(state), ledger._operation_lock(marker):
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
    with _migration_trace(state, 'compare') as trace, ExitStack() as lifetime:
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


def advance_shadow(store, *, project, executing_root, deadline):
    """Run at most one bounded step of the private shadow state machine per call.

    copy -> verification -> base/sidecar corpus -> prepare -> publish -> caught_up.
    Each call performs one of them; the selected reader never changes.
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

    def step(stage, current):
        return {'stage': stage, 'migration_state': current['migration_state'], 'intent': current}

    def reread():
        return _migration_intent(path, project=project, state=state, node=node, store=store)

    if intent['phase'] == 'copying':
        return step('copy', advance_migration(store, **args))
    if intent.get('restart_required', False):
        return step('restart', advance_migration(store, **args))
    verification = intent.get('verification')
    if verification is not None:
        _verification_progress(verification, intent, check_limits=False)
        if (verification['batch_limit'] != MAX_BATCH or
                verification['byte_limit'] != MAX_BATCH_BYTES):
            return step('restart', advance_migration(store, **args))
    corpora = intent.get('corpora')
    if verification is None or not verification['complete']:
        if corpora is not None:
            raise IndexContractError('migration_intent_invalid')
        stage, run = 'verification', lambda: advance_verification(store, **args)
    else:
        if corpora is not None:
            values.exact(corpora, ('verification_digest', 'base', 'sidecar'))
            if corpora['verification_digest'] != verification['digest']:
                raise IndexContractError('migration_intent_invalid')
            for key in ('base', 'sidecar'):
                progress = corpora[key]
                if progress is not None:
                    prefix = ('memvane-sidecar-fts-corpus-v1:' if key == 'sidecar'
                              else 'memvane-base-fts-corpus-v1:')
                    observed = hashlib.sha256((prefix + intent['observation']).encode('ascii')).hexdigest()
                    _verification_progress(progress, intent, cursor_observation=observed)
            if corpora['sidecar'] is not None and (corpora['base'] is None or
                                                  not corpora['base']['complete']):
                raise IndexContractError('migration_intent_invalid')
        prepared, published = intent.get('prepared'), intent.get('published')
        if corpora is None or corpora['base'] is None or not corpora['base']['complete']:
            stage, run = 'base_corpus', lambda: advance_corpus(store, sidecar=False, **args)
        elif corpora['sidecar'] is None or not corpora['sidecar']['complete']:
            stage, run = 'sidecar_corpus', lambda: advance_corpus(store, sidecar=True, **args)
        elif published is not None:
            # Read-only replay: re-proves the recorded private head against the
            # unchanged source under the step deadline, without the cut.
            def run():
                publish_shadow_generation(store, published['generation'], **args)
                return reread()
            stage = 'caught_up'
        elif prepared is None:
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
        return step(stage, run())
    except (IndexContractError, values.BlobStoreError) as exc:
        reason = str(exc)
        if reason not in _SHADOW_RESTART_REASONS:
            raise
        # A second full proof may not fit this step. Persist only a request;
        # the next step re-proves the source before resetting any cursor.
        return step('restart_pending', _mark_shadow_restart(
            store, project, intent, min(wall_deadline, time.monotonic() + _STEP_SECONDS), reason))

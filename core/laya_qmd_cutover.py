"""Installer-owned QMD-origin cutover with a persistent route recovery marker.

Only explicit installer calls mutate the candidate, journal, or route. QMD is
opened read-only; its original database and backup are never removed. The
project marker keeps an interrupted selected route from silently becoming a
legacy QMD hook invocation.
"""
from __future__ import annotations

from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile

from laya_index_a import LayaIndexA
from laya_index_claim_queue import ClaimQueue
from laya_index_source_port import publish_from_source, scan
from laya_index_vector import verify_generation_seal
from laya_qmd_migration import inspect_qmd, verify_restore
from laya_install_journal import (cas_activate, cas_restore_qmd, inspect_journal,
                                  preflight_product_candidate)

SCHEMA = 'laya-qmd-product-cutover-v1'
MARKER_SCHEMA = 'laya-qmd-selection-marker-v1'
LEDGER = 'qmd-cutover.json'
MARKER = 'laya-index-selection.json'
ROUTE = 'laya-index-route.json'
SHA = re.compile(r'[0-9a-f]{64}\Z')


class CutoverBlocked(RuntimeError):
    pass


def _fail(reason):
    raise CutoverBlocked(reason)


def _canon(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _private_file(path, limit=65536):
    path = Path(path)
    if path.is_symlink():
        _fail('cutover_symlink')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) & 0o077
                or before.st_size > limit):
            _fail('cutover_file_unsafe')
        raw = os.read(fd, limit + 1)
        after = os.fstat(fd)
        if (len(raw) != before.st_size or
                (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size)
                != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size)):
            _fail('cutover_file_changed')
        return raw
    except OSError as exc:
        raise CutoverBlocked('cutover_file_unavailable') from exc
    finally:
        os.close(fd)


def _read_json(path):
    try:
        raw = _private_file(path)
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    _fail('cutover_record_duplicate_key')
                value[key] = item
            return value
        def invalid_constant(_value):
            _fail('cutover_record_nonfinite')
        parsed = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                            parse_constant=invalid_constant)
        if raw != _canon(parsed):
            _fail('cutover_record_noncanonical')
        return parsed
    except (UnicodeError, ValueError, TypeError) as exc:
        raise CutoverBlocked('cutover_record_invalid') from exc


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(path, value, *, replace):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix='.laya-cutover-', dir=path.parent)
    temp = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(_canon(value) if not isinstance(value, bytes) else value)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            if path.is_symlink():
                _fail('cutover_symlink')
            os.replace(temp, path)
        else:
            os.link(temp, path)
        _fsync_dir(path.parent)
    except FileExistsError as exc:
        raise CutoverBlocked('cutover_record_exists') from exc
    finally:
        temp.unlink(missing_ok=True)


def _paths(project_root, journal_path):
    project = Path(project_root)
    journal = Path(journal_path)
    if (not project.is_absolute() or project.resolve(strict=True) != project
            or not journal.is_absolute() or journal.name != 'install-journal.sqlite'
            or journal.is_symlink() or journal.parent.resolve(strict=True) != journal.parent):
        _fail('cutover_paths_invalid')
    auto = project / '.auto-context'
    if auto.is_symlink() or not auto.is_dir():
        _fail('cutover_project_unavailable')
    parent = journal.parent
    info = parent.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        _fail('cutover_owner_unavailable')
    return parent / LEDGER, auto / MARKER, auto / ROUTE


def _ledger(ledger_path):
    row = _read_json(ledger_path)
    required = {'schema','epoch','phase','projectRoot','journalPath','routeSha256',
                'sourceDb','backupDb','sourceFormat','collection','sourceDigest',
                'sourceRoot','candidateDb','queueDb','scope','indexGeneration',
                'sourceManifest','vectorProfileId','claim','candidateIncarnation',
                'attestationSha256','providerFileIdentitySha256',
                'sourceIdentity','backupIdentity','candidateIdentity','queueIdentity'}
    if (not isinstance(row, dict) or set(row) != required or row['schema'] != SCHEMA
            or type(row['epoch']) is not int or row['epoch'] < 1
            or row['phase'] not in ('prepared','switching','active','restoring','restored')
            or any(not isinstance(row[k], str) or not row[k] for k in
                   required - {'epoch','phase','sourceIdentity','backupIdentity',
                               'candidateIdentity','queueIdentity'})
            or any(not isinstance(row[k], list) or len(row[k]) != 2 or
                   any(type(v) is not int or v < 0 for v in row[k]) for k in
                   ('sourceIdentity','backupIdentity','candidateIdentity','queueIdentity'))
            or not SHA.fullmatch(row['routeSha256'])
            or not SHA.fullmatch(row['sourceDigest'])
            or not SHA.fullmatch(row['sourceManifest'])
            or not SHA.fullmatch(row['attestationSha256'])
            or not SHA.fullmatch(row['providerFileIdentitySha256'])):
        _fail('cutover_ledger_invalid')
    if Path(row['journalPath']).parent / LEDGER != Path(ledger_path):
        _fail('cutover_ledger_path_mismatch')
    return row


def _identity(path):
    path = Path(path)
    if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or path.resolve(strict=True) != path):
        _fail('cutover_bound_file_unavailable')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        _fail('cutover_bound_file_unsafe')
    return [info.st_dev, info.st_ino]


def _check_evidence(row):
    for name, field in (('sourceDb','sourceIdentity'), ('backupDb','backupIdentity'),
                        ('candidateDb','candidateIdentity'), ('queueDb','queueIdentity')):
        if _identity(row[name]) != row[field]:
            _fail('cutover_bound_file_replaced')
    if inspect_qmd(row['sourceDb'], row['sourceFormat'], row['collection'])['digest'] != row['sourceDigest']:
        _fail('cutover_source_changed')
    verify_restore(row['backupDb'], row['sourceFormat'], row['collection'], row['sourceDigest'])
    with closing(LayaIndexA(row['candidateDb'])) as index:
        if index.incarnation() != row['candidateIncarnation']:
            _fail('cutover_candidate_changed')
        index.db.execute('BEGIN')
        try:
            head = index.db.execute('SELECT gen FROM head WHERE scope=?', (row['scope'],)).fetchone()
            seal = index.db.execute('SELECT source_manifest,claim_digest,claim_epoch FROM candidate_seals WHERE gen=?',
                                    (row['indexGeneration'],)).fetchone()
            receipt = index.db.execute('SELECT scope,gen,source_manifest,claim_digest,claim_epoch FROM receipts WHERE claim=?',
                                       (row['claim'],)).fetchone()
            vector = verify_generation_seal(index, row['indexGeneration'])
            index.db.rollback()
        except BaseException:
            index.db.rollback()
            raise
        if (head != (row['indexGeneration'],) or
                seal != (row['sourceManifest'], row['sourceDigest'], 0) or
                receipt != (row['scope'],row['indexGeneration'],row['sourceManifest'],row['sourceDigest'],0) or
                vector['profile_id'] != row['vectorProfileId']):
            _fail('cutover_candidate_evidence_mismatch')
        if scan(row['sourceRoot'], row['scope'], index).manifest != row['sourceManifest']:
            _fail('cutover_source_manifest_changed')
    with closing(ClaimQueue.open(row['queueDb'])) as queue:
        if queue.binding() != row['candidateIncarnation']:
            _fail('cutover_queue_binding_mismatch')


def _marker(row):
    return {'schema':MARKER_SCHEMA, 'projectRoot':row['projectRoot'],
            'journalPath':row['journalPath'], 'ledgerPath':str(Path(row['journalPath']).parent / LEDGER),
            'routeSha256':row['routeSha256']}


def _check_marker(row, marker_path):
    if _read_json(marker_path) != _marker(row):
        _fail('cutover_marker_changed')


def _change(ledger_path, expected_epoch, expected_phase, next_phase):
    lock_path = Path(ledger_path).with_suffix('.lock')
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            _fail('cutover_lock_unsafe')
        fcntl.flock(fd, fcntl.LOCK_EX)
        row = _ledger(ledger_path)
        if row['epoch'] != expected_epoch or row['phase'] != expected_phase:
            _fail('cutover_stale_cas')
        row['epoch'] += 1
        row['phase'] = next_phase
        _write(ledger_path, row, replace=True)
        return row
    finally:
        os.close(fd)


def bind_qmd_origin(*, project_root, journal_path, route_bytes, source_db,
                    backup_db, source_format, collection, source_digest,
                    source_root, candidate_db, queue_db, scope,
                    index_generation, source_manifest, vector_profile_id,
                    claim):
    """Publish a sealed candidate and durably bind the still-selected QMD origin.

    The installer has already prepared the full vector candidate and journal.
    This operation does not publish the project route or modify the QMD DB.
    """
    ledger_path, marker_path, route_path = _paths(project_root, journal_path)
    if route_path.exists() or route_path.is_symlink() or ledger_path.exists() or marker_path.exists():
        _fail('cutover_selection_already_exists')
    if not isinstance(route_bytes, bytes) or not route_bytes or len(route_bytes) > 8192:
        _fail('cutover_route_invalid')
    from laya_install_journal import _route
    route, route_sha = _route(route_bytes, Path(journal_path))
    if (route['projectRoot'] != str(project_root) or route['dbPath'] != str(candidate_db)
            or route['queuePath'] != str(queue_db) or route['scopeId'] != scope):
        _fail('cutover_route_mismatch')
    journal = inspect_journal(journal_path)
    if (journal['state'] != 'candidate_ready' or journal['pending'] is None or
            journal['pendingRouteSha256'] != route_sha):
        _fail('cutover_journal_not_ready')
    if route_sha != hashlib.sha256(route_bytes).hexdigest():
        _fail('cutover_route_invalid')
    approval = preflight_product_candidate(journal_path)
    if approval['pendingRouteSha256'] != route_sha:
        _fail('cutover_attested_route_mismatch')
    if inspect_qmd(source_db, source_format, collection)['digest'] != source_digest:
        _fail('cutover_source_changed')
    verify_restore(backup_db, source_format, collection, source_digest)
    if (route['sourceRoot'] != str(source_root) or
            route['collectionName'] != collection or
            route['vectorProfile']['profileId'] != vector_profile_id or
            len({str(Path(p)) for p in (source_db, backup_db, candidate_db, queue_db)}) != 4):
        _fail('cutover_source_root_mismatch')
    with closing(LayaIndexA(candidate_db)) as index:
        if scan(source_root, scope, index).manifest != source_manifest:
            _fail('cutover_source_manifest_changed')
        index.db.execute('BEGIN')
        try:
            vector = verify_generation_seal(index, index_generation)
            index.db.rollback()
        except BaseException:
            index.db.rollback()
            raise
        if vector['profile_id'] != vector_profile_id:
            _fail('cutover_vector_profile_mismatch')
        result = publish_from_source(index, source_root, scope, None, index_generation,
                                     claim, claim_digest=source_digest, claim_epoch=0,
                                     require_vector=True)
        if result not in ('published','replayed'):
            _fail('cutover_publish_unavailable')
        incarnation = index.incarnation()
        if route['dbIncarnation'] != incarnation:
            _fail('cutover_candidate_incarnation_mismatch')
    if inspect_qmd(source_db, source_format, collection)['digest'] != source_digest:
        _fail('cutover_source_changed')
    with closing(ClaimQueue.open(queue_db)) as queue:
        if queue.binding() != incarnation:
            _fail('cutover_queue_binding_mismatch')
    row = {'schema':SCHEMA,'epoch':1,'phase':'prepared','projectRoot':str(project_root),
           'journalPath':str(journal_path),'routeSha256':route_sha,
           'sourceDb':str(source_db),'backupDb':str(backup_db),
           'sourceFormat':source_format,'collection':collection,'sourceDigest':source_digest,
           'sourceRoot':str(source_root),'candidateDb':str(candidate_db),
           'queueDb':str(queue_db),'scope':scope,'indexGeneration':index_generation,
           'sourceManifest':source_manifest,'vectorProfileId':vector_profile_id,
           'claim':claim,'candidateIncarnation':incarnation,
           'attestationSha256':approval['attestationSha256'],
           'providerFileIdentitySha256':approval['providerFileIdentitySha256'],
           'sourceIdentity':_identity(source_db),'backupIdentity':_identity(backup_db),
           'candidateIdentity':_identity(candidate_db),'queueIdentity':_identity(queue_db)}
    _check_evidence(row)
    _write(ledger_path, row, replace=False)
    _write(marker_path, _marker(row), replace=False)
    return row


def classify_marker(project_root, marker_path, route_path):
    """Read-only selector result: candidate, legacy, or blocked."""
    try:
        marker = _read_json(marker_path)
        if (not isinstance(marker, dict) or set(marker) !=
                {'schema','projectRoot','journalPath','ledgerPath','routeSha256'}
                or marker['schema'] != MARKER_SCHEMA or
                marker['projectRoot'] != str(project_root)):
            return 'blocked'
        ledger_path, expected_marker, expected_route = _paths(project_root, marker['journalPath'])
        if expected_marker != Path(marker_path) or expected_route != Path(route_path) or str(ledger_path) != marker['ledgerPath']:
            return 'blocked'
        row = _ledger(ledger_path)
        _check_marker(row, marker_path)
        journal = inspect_journal(row['journalPath'])
        present = route_path.exists() or route_path.is_symlink()
        if present:
            if route_path.is_symlink() or _sha(_private_file(route_path, 8192)) != row['routeSha256']:
                return 'blocked'
            if (row['phase'] == 'active' and journal['state'] == 'active' and
                    journal['selected'] is not None and
                    journal['selectedRouteSha256'] == row['routeSha256'] and
                    journal['providerAttestationSha256'] == row['attestationSha256'] and
                    journal['providerFileIdentitySha256'] == row['providerFileIdentitySha256']):
                return 'candidate'
            return 'blocked'
        if row['phase'] == 'prepared' and journal['state'] == 'candidate_ready':
            return 'legacy'
        if row['phase'] == 'restored' and journal['state'] == 'restored_qmd':
            return 'legacy'
        return 'blocked'
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error):
        return 'blocked'


def activate_qmd_origin(project_root, journal_path, route_bytes):
    ledger_path, marker_path, route_path = _paths(project_root, journal_path)
    row = _ledger(ledger_path)
    _check_marker(row, marker_path)
    if row['phase'] != 'prepared' or _sha(route_bytes) != row['routeSha256']:
        _fail('cutover_not_prepared')
    _check_evidence(row)
    journal = inspect_journal(journal_path)
    if journal['state'] != 'candidate_ready':
        _fail('cutover_journal_not_ready')
    approval = preflight_product_candidate(journal_path)
    if (approval['attestationSha256'] != row['attestationSha256'] or
            approval['providerFileIdentitySha256'] != row['providerFileIdentitySha256'] or
            approval['pendingRouteSha256'] != row['routeSha256']):
        _fail('cutover_attestation_changed')
    row = _change(ledger_path, row['epoch'], 'prepared', 'switching')
    _write(route_path, route_bytes, replace=False)
    active = cas_activate(journal_path, journal['epoch'], route_bytes)
    if (active['state'] != 'active' or
            active['providerAttestationSha256'] != row['attestationSha256'] or
            active['providerFileIdentitySha256'] != row['providerFileIdentitySha256']):
        _fail('cutover_journal_activation_unconfirmed')
    return _change(ledger_path, row['epoch'], 'switching', 'active')


def restore_qmd_origin(project_root, journal_path):
    ledger_path, marker_path, route_path = _paths(project_root, journal_path)
    row = _ledger(ledger_path)
    _check_marker(row, marker_path)
    journal = inspect_journal(journal_path)
    if (row['phase'] != 'active' or journal['state'] != 'active' or
            journal['newWriteSeen'] or not route_path.is_file() or
            _sha(_private_file(route_path, 8192)) != row['routeSha256']):
        _fail('cutover_rollback_not_authorized')
    _check_evidence(row)
    row = _change(ledger_path, row['epoch'], 'active', 'restoring')
    # A concurrent selected writer may mark forward-only after our first read.
    # Recheck before unlink; if it raced after unlink, the marker remains
    # restoring and recovery reinstates the exact route.
    if inspect_journal(journal_path)['newWriteSeen']:
        _change(ledger_path, row['epoch'], 'restoring', 'active')
        _fail('cutover_rollback_not_authorized')
    route_path.unlink()
    _fsync_dir(route_path.parent)
    restored = cas_restore_qmd(journal_path, journal['epoch'], row['routeSha256'])
    if restored['state'] != 'restored_qmd':
        _fail('cutover_rollback_unconfirmed')
    return _change(ledger_path, row['epoch'], 'restoring', 'restored')


def recover_qmd_origin(project_root, journal_path, route_bytes):
    """Settle interrupted selection without deleting either source database."""
    ledger_path, marker_path, route_path = _paths(project_root, journal_path)
    row = _ledger(ledger_path)
    _check_marker(row, marker_path)
    if not isinstance(route_bytes, bytes) or _sha(route_bytes) != row['routeSha256']:
        _fail('cutover_route_mismatch')
    journal = inspect_journal(journal_path)
    present = route_path.exists() or route_path.is_symlink()
    if row['phase'] == 'switching':
        _check_evidence(row)
        if journal['state'] == 'candidate_ready':
            approval = preflight_product_candidate(journal_path)
            if (approval['attestationSha256'] != row['attestationSha256'] or
                    approval['providerFileIdentitySha256'] != row['providerFileIdentitySha256']):
                _fail('cutover_attestation_changed')
        if not present and journal['state'] == 'candidate_ready':
            return _change(ledger_path, row['epoch'], 'switching', 'prepared')
        if not present or route_path.is_symlink() or _private_file(route_path, 8192) != route_bytes:
            _fail('cutover_route_uncertain')
        if journal['state'] == 'candidate_ready':
            journal = cas_activate(journal_path, journal['epoch'], route_bytes)
        if (journal['state'] != 'active' or
                journal['providerAttestationSha256'] != row['attestationSha256'] or
                journal['providerFileIdentitySha256'] != row['providerFileIdentitySha256']):
            _fail('cutover_journal_uncertain')
        return _change(ledger_path, row['epoch'], 'switching', 'active')
    if row['phase'] == 'restoring':
        if journal['newWriteSeen']:
            if not present:
                _write(route_path, route_bytes, replace=False)
            elif route_path.is_symlink() or _private_file(route_path, 8192) != route_bytes:
                _fail('cutover_route_uncertain')
            return _change(ledger_path, row['epoch'], 'restoring', 'active')
        _check_evidence(row)
        if present:
            if route_path.is_symlink() or _private_file(route_path, 8192) != route_bytes:
                _fail('cutover_route_uncertain')
            route_path.unlink()
            _fsync_dir(route_path.parent)
        if journal['state'] == 'active':
            journal = cas_restore_qmd(journal_path, journal['epoch'], row['routeSha256'])
        if journal['state'] != 'restored_qmd':
            _fail('cutover_journal_uncertain')
        return _change(ledger_path, row['epoch'], 'restoring', 'restored')
    return row

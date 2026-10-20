"""Installer-owned synthetic activation journal and read-only route resolver.

This module writes only an explicit, private journal path. It never publishes a
project route, touches QMD, deletes an engine, or authorizes destructive GC.
Route-file publication is a separate installer step. CAS plus recovery records
detect interruption, but are not a cross-file power-loss transaction.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys

from laya_index_a import IndexContractError, LayaIndexA
from laya_index_claim_queue import ClaimQueue, UnknownDatabase
from laya_index_host_route import (FIELDS as ROUTE_FIELDS, FIELDS_V2 as ROUTE_FIELDS_V2,
                                   RECORD as ROUTE_FILE, SCHEMA as ROUTE_SCHEMA,
                                   SCHEMA_V2 as ROUTE_SCHEMA_V2, select_route)
from laya_package import _user_qmd_inventory
from laya_product_bundle import (BundleEvidenceError, COMMIT, inspect_product_candidate,
                                 inspect_full_plugin_candidate,
                                 PRODUCTION_ATTESTATION_SCHEMA,
                                 production_provider_approved,
                                 validate_production_provider_attestation,
                                 validate_vector_profile)


SCHEMA = 'laya-installer-journal-v3'
DEVELOPMENT_APPROVAL_SCHEMA = 'laya-development-vector-approval-v1'
APP_ID = 0x4c594a31
VERSION = 3
ID = re.compile(r'[0-9a-f]{32}\Z')
GEN = re.compile(r'g-[0-9a-f]{32}\Z')
SHA = re.compile(r'[0-9a-f]{64}\Z')
PRODUCTION_ATTESTATION = 'production-provider-attestation.json'
_CREATE = '''CREATE TABLE selection(
  id INTEGER PRIMARY KEY CHECK(id=1), schema TEXT NOT NULL,
  epoch INTEGER NOT NULL, state TEXT NOT NULL,
  selected TEXT, selected_route_sha TEXT, previous TEXT, previous_route_sha TEXT,
  pending TEXT, pending_route_sha TEXT,
  scope_id TEXT NOT NULL, project_root TEXT NOT NULL,
  project_dev INTEGER NOT NULL, project_ino INTEGER NOT NULL,
  user_qmd_inventory TEXT NOT NULL, new_write_seen INTEGER NOT NULL,
  provider_attestation_sha TEXT, previous_provider_attestation_sha TEXT,
  provider_file_identity TEXT, previous_provider_file_identity TEXT)'''


class JournalEvidenceError(ValueError):
    pass


def _fail(reason):
    raise JournalEvidenceError(reason)


def _canon(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'))


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _fail('duplicate_journal_route_key')
        result[key] = value
    return result


def _nonfinite(_value):
    _fail('nonfinite_journal_route_number')


def _attestation(path):
    """Read installer-owned bytes; a declaration inside the plugin is not proof."""
    target = Path(path).parent / PRODUCTION_ATTESTATION
    if not target.exists() and not target.is_symlink():
        return None, None
    try:
        if target.is_symlink():
            _fail('unsafe_production_attestation')
        fd = os.open(target, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) & 0o077
                    or before.st_size > 1024 * 1024):
                _fail('unsafe_production_attestation')
            raw = os.read(fd, 1024 * 1024 + 1)
            after = os.fstat(fd)
            if (len(raw) != before.st_size or
                    (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size)
                    != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size)):
                _fail('production_attestation_changed')
        finally:
            os.close(fd)
        parsed = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                            parse_constant=_nonfinite)
        if not isinstance(parsed, dict) or raw != _canon(parsed).encode('utf-8'):
            _fail('production_attestation_noncanonical')
        if parsed.get('journalPath') != str(Path(path)):
            _fail('production_attestation_journal_mismatch')
        return parsed, hashlib.sha256(raw).hexdigest()
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise JournalEvidenceError('production_attestation_unavailable') from exc


def stage_production_attestation(journal_path, bundle_proof, provider_manifest,
                                 dependency_closure):
    """No-clobber installer record, validated before journal CAS can bind it.

    Repeating an identical, complete staging operation is safe. A partial or
    different file is retained for explicit recovery instead of overwritten.
    """
    journal = _private_parent(journal_path)
    proof = inspect_full_plugin_candidate(bundle_proof.get('pluginRoot'))
    if proof != bundle_proof:
        _fail('production_bundle_changed')
    record = {'schema': PRODUCTION_ATTESTATION_SCHEMA,
              'sourceCommit': proof['sourceCommit'],
              'bundleManifestSha256': proof['bundleManifestSha256'],
              'ownershipManifestSha256': proof['ownershipManifestSha256'],
              'journalPath': str(journal),
              'vectorProfile': proof['vectorProfile'],
              'providerManifest': provider_manifest,
              'dependencyClosure': dependency_closure}
    try:
        validate_production_provider_attestation(proof['vectorProfile'], proof, record)
    except BundleEvidenceError as exc:
        raise JournalEvidenceError('production_attestation_invalid') from exc
    raw = _canon(record).encode('utf-8')
    if len(raw) > 1024 * 1024:
        _fail('production_attestation_too_large')
    target = journal.parent / PRODUCTION_ATTESTATION
    if target.exists() or target.is_symlink():
        existing, digest = _attestation(journal)
        if existing != record:
            _fail('production_attestation_already_owned')
        return {'attestationPath': str(target), 'attestationSha256': digest}
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        parent_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except OSError as exc:
        raise JournalEvidenceError('production_attestation_stage_incomplete') from exc
    existing, digest = _attestation(journal)
    if existing != record:
        _fail('production_attestation_changed')
    return {'attestationPath': str(target), 'attestationSha256': digest}


def _provider_file_identity(attestation):
    """Cheap immutable-file identity after one full validated hash pass."""
    manifest = attestation['providerManifest']
    paths = {manifest[key] for key in
             ('providerPath', 'nodePath', 'moduleEntryPath', 'workerPath', 'modelPath')}
    paths.update(item['path'] for item in attestation['dependencyClosure'])
    rows = []
    try:
        for raw in sorted(paths):
            path = Path(raw)
            if not path.is_absolute() or path.resolve(strict=True) != path:
                _fail('production_provider_file_changed')
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                _fail('production_provider_file_changed')
            rows.append([raw, info.st_dev, info.st_ino, info.st_size,
                         info.st_mtime_ns, info.st_ctime_ns])
    except (OSError, RuntimeError, TypeError, KeyError) as exc:
        raise JournalEvidenceError('production_provider_file_unavailable') from exc
    return rows


def _strict_vector_profile(value):
    if not isinstance(value, dict):
        _fail('invalid_vector_profile')
    relative = value.get('providerRelativePath')
    digest = value.get('providerArtifactSha256')
    if type(relative) is not str or type(digest) is not str:
        _fail('invalid_vector_profile')
    try:
        return validate_vector_profile(value, {relative: {'sha256': digest}})
    except BundleEvidenceError as exc:
        raise JournalEvidenceError('invalid_vector_profile') from exc


def _route(raw, journal_path):
    if type(raw) is not bytes or len(raw) > 8192:
        _fail('invalid_route_bytes')
    try:
        route = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                           parse_constant=_nonfinite)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise JournalEvidenceError('invalid_route_bytes') from exc
    schema = route.get('schema') if isinstance(route, dict) else None
    expected_fields = (ROUTE_FIELDS if schema == ROUTE_SCHEMA else
                       ROUTE_FIELDS_V2 if schema == ROUTE_SCHEMA_V2 else None)
    if (not isinstance(route, dict) or expected_fields is None or
            set(route) != expected_fields or route.get('status') != 'active' or
            route.get('journalPath') != str(journal_path)):
        _fail('route_contract_mismatch')
    _strict_vector_profile(route.get('vectorProfile'))
    return route, hashlib.sha256(raw).hexdigest()


def _private_parent(path):
    try:
        path = Path(path)
        if not path.is_absolute() or path.is_symlink() or path.name != 'install-journal.sqlite':
            _fail('invalid_journal_path')
        parent = path.parent
        if parent.is_symlink() or not parent.is_dir():
            _fail('unsafe_journal_parent')
        parent = parent.resolve(strict=True)
        info = parent.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            _fail('unsafe_journal_parent')
        return parent / path.name
    except (OSError, TypeError, RuntimeError) as exc:
        raise JournalEvidenceError('journal_path_unavailable') from exc


def _identity(proof, route, journal_path):
    if not isinstance(proof, dict) or proof.get('status') not in (
            'exact_commit_candidate_consistent',
            'exact_commit_full_plugin_candidate_consistent'):
        _fail('unverified_bundle')
    full = proof['status'] == 'exact_commit_full_plugin_candidate_consistent'
    live = (inspect_full_plugin_candidate(proof.get('pluginRoot')) if full else
            inspect_product_candidate(proof.get('engineRoot')))
    if live != proof:
        _fail('bundle_proof_changed')
    root = live['pluginRoot'] if full else live['engineRoot']
    if ((route['schema'] == ROUTE_SCHEMA and
         Path(root).parent != journal_path.parent) or
            route.get('pluginRoot') != root or
            type(route.get('installId')) is not str or not ID.fullmatch(route['installId']) or
            type(route.get('generation')) is not str or not GEN.fullmatch(route['generation']) or
            route.get('bundleManifestSha256') != live['bundleManifestSha256'] or
            route.get('ownershipManifestSha256') != live['ownershipManifestSha256'] or
            route.get('vectorProfile') != live.get('vectorProfile')):
        _fail('route_bundle_mismatch')
    return {'pluginRoot': root, 'installId': route['installId'],
            'generation': route['generation'],
            'bundleManifestSha256': live['bundleManifestSha256'],
            'ownershipManifestSha256': live['ownershipManifestSha256'],
            'sourceCommit': live['sourceCommit'],
            'vectorProfile': live.get('vectorProfile'),
            'bundleKind': 'full_plugin' if full else 'engine_subbundle'}


def _inspect_identity(identity):
    if not isinstance(identity, dict):
        _fail('invalid_bundle_identity')
    if identity.get('bundleKind') == 'full_plugin':
        proof = inspect_full_plugin_candidate(identity.get('pluginRoot'))
    elif identity.get('bundleKind') == 'engine_subbundle':
        proof = inspect_product_candidate(identity.get('pluginRoot'))
    else:
        _fail('invalid_bundle_identity')
    if (proof['bundleManifestSha256'] != identity.get('bundleManifestSha256') or
            proof['ownershipManifestSha256'] != identity.get('ownershipManifestSha256') or
            proof['sourceCommit'] != identity.get('sourceCommit') or
            proof.get('vectorProfile') != identity.get('vectorProfile')):
        _fail('installed_bundle_changed')
    return proof


def _bind_attestation(journal_path, identity, proof):
    if identity.get('bundleKind') != 'full_plugin':
        return None, None
    record, digest = _attestation(journal_path)
    if record is None:
        return None, None
    try:
        validate_production_provider_attestation(proof.get('vectorProfile'),
                                                 proof, record)
    except BundleEvidenceError as exc:
        raise JournalEvidenceError('production_attestation_invalid') from exc
    return digest, _provider_file_identity(record)


def preflight_product_candidate(journal_path, *, require_executing=False):
    """Review the pending installed provider before publishing a QMD cutover.

    The returned digest is an expectation to persist before the route becomes
    visible. Recovery must compare current attestation bytes to this value.
    """
    pending = inspect_journal(journal_path)
    if pending['state'] != 'candidate_ready' or pending['pending'] is None:
        _fail('product_candidate_not_pending')
    identity = pending['pending']
    if identity.get('bundleKind') != 'full_plugin':
        _fail('product_full_plugin_required')
    proof = _inspect_identity(identity)
    if require_executing:
        _verified_executing_root(identity, proof)
    attestation, digest = _attestation(journal_path)
    if not production_provider_approved(
            proof.get('vectorProfile'), proof, attestation,
            journal_attestation_sha256=digest):
        _fail('production_provider_unapproved')
    identity_rows = _provider_file_identity(attestation)
    return {'attestationSha256': digest,
            'providerFileIdentitySha256': hashlib.sha256(_canon(identity_rows).encode('utf-8')).hexdigest(),
            'providerManifest': attestation['providerManifest'],
            'pendingRouteSha256': pending['pendingRouteSha256']}


def _executing_from_candidate(plugin_root):
    """The verifier and its loaded Laya dependencies must be the sealed files."""
    core = Path(plugin_root) / 'core'
    required = ('laya_install_journal', 'laya_product_bundle', 'laya_index_a',
                'laya_index_claim_queue', 'laya_index_host_route', 'laya_package',
                'config')
    for name in required:
        module = sys.modules.get(name)
        source = getattr(module, '__file__', None)
        if source is None or Path(source).resolve(strict=False) != core / (name + '.py'):
            _fail('executing_code_outside_bundle')
    # A candidate/core first import path is necessary so subsequent runtime,
    # flow and source-port imports cannot silently come from another checkout.
    if not sys.path or Path(sys.path[0]).resolve(strict=False) != core:
        _fail('future_import_path_unbound')


def _verified_executing_root(selected, owner_proof):
    """Prove this host's sealed copy against the journal-bound owner copy."""
    root = Path(__file__).resolve().parent.parent
    if str(root) != selected['pluginRoot']:
        try:
            local = inspect_full_plugin_candidate(root)
        except BundleEvidenceError as exc:
            raise JournalEvidenceError('executing_code_outside_bundle') from exc
        if (local['pluginRoot'] != str(root) or
                any(local[key] != owner_proof[key] for key in
                    ('sourceCommit', 'bundleManifestSha256',
                     'vectorProfile', 'includedFiles'))):
            _fail('executing_bundle_not_selected')
    _executing_from_candidate(root)
    return str(root)


def _project(route):
    try:
        path = Path(route.get('projectRoot'))
        if not path.is_absolute() or path.is_symlink() or not path.is_dir():
            _fail('invalid_project_root')
        path = path.resolve(strict=True)
        info = path.stat()
        if (str(path) != route['projectRoot'] or type(route.get('scopeId')) is not str or
                not ID.fullmatch(route['scopeId'])):
            _fail('invalid_project_root')
        return str(path), info.st_dev, info.st_ino
    except (OSError, TypeError, RuntimeError) as exc:
        raise JournalEvidenceError('project_root_unavailable') from exc


def _observed_route(row, supplied_raw):
    route_path = Path(row['project_root']) / '.auto-context' / ROUTE_FILE
    try:
        if route_path.is_symlink() or not route_path.is_file():
            _fail('route_unavailable')
        before = route_path.stat()
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or
                before.st_mode & stat.S_IWOTH or before.st_size > 8192):
            _fail('unsafe_route_file')
        fd = os.open(route_path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            raw = os.read(fd, 8193)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        now = route_path.stat()
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if len(raw) > 8192 or identity(before) != identity(after) or identity(before) != identity(now):
            _fail('route_file_changed')
        if supplied_raw is not None and raw != supplied_raw:
            _fail('observed_route_mismatch')
        return raw
    except OSError as exc:
        raise JournalEvidenceError('route_unavailable') from exc


def _open(path, *, write):
    path = _private_parent(path)
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
            _fail('unsafe_journal_file')
        uri = path.as_uri() + ('?mode=rw' if write else '?mode=ro')
        db = sqlite3.connect(uri, uri=True, timeout=2, isolation_level=None)
        if (db.execute('PRAGMA application_id').fetchone()[0] != APP_ID or
                db.execute('PRAGMA user_version').fetchone()[0] != VERSION or
                db.execute("SELECT sql FROM sqlite_master WHERE name='selection'").fetchone() != (_CREATE,)):
            db.close()
            _fail('journal_schema_unknown')
        return path, db
    except (OSError, sqlite3.Error) as exc:
        raise JournalEvidenceError('journal_unavailable') from exc


def _row(db):
    record = db.execute('SELECT * FROM selection WHERE id=1').fetchone()
    if record is None or db.execute('SELECT count(*) FROM selection').fetchone()[0] != 1:
        _fail('journal_selection_missing')
    fields = ('id', 'schema', 'epoch', 'state', 'selected', 'selected_route_sha',
              'previous', 'previous_route_sha', 'pending', 'pending_route_sha',
              'scope_id', 'project_root', 'project_dev', 'project_ino',
              'user_qmd_inventory', 'new_write_seen', 'provider_attestation_sha',
              'previous_provider_attestation_sha', 'provider_file_identity',
              'previous_provider_file_identity')
    row = dict(zip(fields, record))
    if (row['schema'] != SCHEMA or type(row['epoch']) is not int or row['epoch'] < 1 or
            row['state'] not in ('candidate_ready', 'pending', 'active', 'restored_old',
                                 'restored_qmd') or
            type(row['new_write_seen']) is not int or row['new_write_seen'] not in (0, 1)):
        _fail('journal_state_invalid')
    try:
        for key in ('selected', 'previous', 'pending', 'user_qmd_inventory',
                    'provider_file_identity', 'previous_provider_file_identity'):
            row[key] = json.loads(row[key], object_pairs_hook=_pairs,
                                  parse_constant=_nonfinite) if row[key] is not None else None
    except (TypeError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise JournalEvidenceError('journal_value_invalid') from exc
    if not isinstance(row['user_qmd_inventory'], list) or not row['user_qmd_inventory']:
        _fail('journal_inventory_invalid')
    for key in ('provider_file_identity', 'previous_provider_file_identity'):
        value = row[key]
        if value is not None and (not isinstance(value, list) or
                not value or any(not isinstance(item, list) or len(item) != 6 or
                    type(item[0]) is not str or
                    any(type(n) is not int or n < 0 for n in item[1:])
                    for item in value) or
                [item[0] for item in value] != sorted(set(item[0] for item in value))):
            _fail('journal_provider_identity_invalid')
    if (type(row['scope_id']) is not str or not ID.fullmatch(row['scope_id']) or
            type(row['project_root']) is not str or not Path(row['project_root']).is_absolute() or
            type(row['project_dev']) is not int or type(row['project_ino']) is not int or
            any(value is not None and (type(value) is not str or not SHA.fullmatch(value))
                for value in (row['selected_route_sha'], row['previous_route_sha'],
                              row['pending_route_sha'],
                              row['provider_attestation_sha'],
                              row['previous_provider_attestation_sha']))):
        _fail('journal_value_invalid')
    for key in ('selected', 'previous', 'pending'):
        value = row[key]
        if value is None:
            continue
        if (not isinstance(value, dict) or
                set(value) != {'pluginRoot', 'installId', 'generation',
                               'bundleManifestSha256', 'ownershipManifestSha256',
                               'sourceCommit', 'bundleKind', 'vectorProfile'} or
                type(value['pluginRoot']) is not str or
                not Path(value['pluginRoot']).is_absolute() or
                type(value['installId']) is not str or not ID.fullmatch(value['installId']) or
                type(value['generation']) is not str or not GEN.fullmatch(value['generation']) or
                any(type(value[k]) is not str or not SHA.fullmatch(value[k]) for k in
                    ('bundleManifestSha256', 'ownershipManifestSha256')) or
                type(value['sourceCommit']) is not str or
                not COMMIT.fullmatch(value['sourceCommit']) or
                value['bundleKind'] not in ('full_plugin', 'engine_subbundle')):
            _fail('journal_value_invalid')
        if value['vectorProfile'] is not None:
            _strict_vector_profile(value['vectorProfile'])
    if ((row['state'] in ('active', 'restored_old') and
         (row['selected'] is None or row['selected_route_sha'] is None)) or
            (row['state'] in ('candidate_ready', 'pending') and
             (row['pending'] is None or row['pending_route_sha'] is None)) or
            (row['state'] == 'restored_qmd' and
             (row['selected'] is not None or row['selected_route_sha'] is not None
              or row['pending'] is not None or row['pending_route_sha'] is not None
              or row['new_write_seen'] or row['provider_attestation_sha'] or
              row['provider_file_identity']))):
        _fail('journal_state_invalid')
    return row


def create_journal(journal_path, bundle_proof, proposed_route_raw, *, user_qmd_roots):
    """No-clobber synthetic journal; route publication remains external."""
    path = _private_parent(journal_path)
    if path.exists() or path.is_symlink():
        _fail('journal_already_exists')
    route, digest = _route(proposed_route_raw, path)
    identity = _identity(bundle_proof, route, path)
    project, dev, ino = _project(route)
    inventory = _user_qmd_inventory(Path(identity['pluginRoot']), user_qmd_roots)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    os.close(fd)
    try:
        with closing(sqlite3.connect(path, timeout=2, isolation_level=None)) as db:
            db.execute('PRAGMA journal_mode=DELETE')
            db.execute('PRAGMA synchronous=FULL')
            db.execute(f'PRAGMA application_id={APP_ID}')
            db.execute(f'PRAGMA user_version={VERSION}')
            db.execute('BEGIN IMMEDIATE')
            db.execute(_CREATE)
            db.execute('INSERT INTO selection VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (SCHEMA, 1, 'candidate_ready', None, None, None, None,
                        _canon(identity), digest, route['scopeId'], project, dev, ino,
                        _canon(inventory), 0, None, None, None, None))
            db.commit()
        file_fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            os.fsync(file_fd)
        finally:
            os.close(file_fd)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except (OSError, sqlite3.Error) as exc:
        raise JournalEvidenceError('journal_creation_incomplete') from exc
    return inspect_journal(path)


def inspect_journal(journal_path):
    path, db = _open(journal_path, write=False)
    with closing(db):
        db.execute('BEGIN')
        try:
            row = _row(db)
            db.commit()
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            db.rollback()
            raise JournalEvidenceError('journal_unavailable') from exc
        except BaseException:
            db.rollback()
            raise
    return {'journalPath': str(path), 'epoch': row['epoch'], 'state': row['state'],
            'selected': row['selected'], 'previous': row['previous'],
            'pending': row['pending'], 'scopeId': row['scope_id'],
            'projectRoot': row['project_root'], 'newWriteSeen': bool(row['new_write_seen']),
            'selectedRouteSha256': row['selected_route_sha'],
            'pendingRouteSha256': row['pending_route_sha'],
            'providerAttestationSha256': row['provider_attestation_sha'],
            'previousProviderAttestationSha256': row['previous_provider_attestation_sha'],
            'providerFileIdentitySha256': (hashlib.sha256(_canon(row['provider_file_identity']).encode('utf-8')).hexdigest()
                                           if row['provider_file_identity'] is not None else None)}


def _cas(path, expected_epoch, mutate):
    path, db = _open(path, write=True)
    try:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('BEGIN IMMEDIATE')
        row = _row(db)
        if type(expected_epoch) is not int or row['epoch'] != expected_epoch:
            _fail('stale_journal_cas')
        values = mutate(path, row)
        if values is not None:
            fields = ','.join(f'{key}=?' for key in values)
            args = (*values.values(), expected_epoch + 1, expected_epoch)
            changed = db.execute(f'UPDATE selection SET {fields},epoch=? WHERE id=1 AND epoch=?',
                                 args)
            if changed.rowcount != 1:
                _fail('stale_journal_cas')
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
    return inspect_journal(path)


def cas_stage_update(journal_path, expected_epoch, next_bundle_proof, proposed_route_raw):
    def stage(path, row):
        if row['state'] not in ('active', 'restored_old'):
            _fail('journal_not_active')
        route, digest = _route(proposed_route_raw, path)
        identity = _identity(next_bundle_proof, route, path)
        if (_project(route) != (row['project_root'], row['project_dev'], row['project_ino']) or
                route['scopeId'] != row['scope_id'] or identity == row['selected']):
            _fail('update_identity_mismatch')
        return {'state': 'pending', 'previous': _canon(row['selected']),
                'previous_route_sha': row['selected_route_sha'],
                'previous_provider_attestation_sha': row['provider_attestation_sha'],
                'previous_provider_file_identity': (_canon(row['provider_file_identity'])
                                                    if row['provider_file_identity'] is not None else None),
                'pending': _canon(identity), 'pending_route_sha': digest,
                'new_write_seen': 0}
    return _cas(journal_path, expected_epoch, stage)


def cas_recover(journal_path, expected_epoch, observed_route_raw):
    """Finish pending switch if new route is visible, else retain exact old route."""
    def recover(path, row):
        if row['state'] not in ('candidate_ready', 'pending') or row['pending'] is None:
            _fail('journal_not_pending')
        route, digest = _route(_observed_route(row, observed_route_raw), path)
        if (_project(route) != (row['project_root'], row['project_dev'], row['project_ino']) or
                route['scopeId'] != row['scope_id']):
            _fail('recovery_project_mismatch')
        if digest == row['pending_route_sha']:
            if any(route.get(k) != row['pending'].get(k) for k in
                   ('pluginRoot', 'installId', 'generation', 'bundleManifestSha256',
                    'ownershipManifestSha256', 'vectorProfile')):
                _fail('recovery_identity_mismatch')
            proof = _inspect_identity(row['pending'])
            att_sha, file_identity = _bind_attestation(path, row['pending'], proof)
            return {'state': 'active', 'selected': _canon(row['pending']),
                    'selected_route_sha': digest, 'pending': None,
                    'pending_route_sha': None,
                    'provider_attestation_sha': att_sha,
                    'provider_file_identity': (_canon(file_identity) if file_identity is not None else None)}
        if row['previous'] is not None and digest == row['previous_route_sha']:
            _inspect_identity(row['previous'])
            return {'state': 'restored_old', 'selected': _canon(row['previous']),
                    'selected_route_sha': digest, 'pending': None,
                    'pending_route_sha': None,
                    'provider_attestation_sha': row['previous_provider_attestation_sha'],
                    'provider_file_identity': (_canon(row['previous_provider_file_identity'])
                                               if row['previous_provider_file_identity'] is not None else None)}
        _fail('uncertain_route_recovery')
    return _cas(journal_path, expected_epoch, recover)


def cas_activate(journal_path, expected_epoch, observed_route_raw):
    """Commit only the proposed route branch; another observation blocks."""
    def activate(path, row):
        if row['state'] not in ('candidate_ready', 'pending') or row['pending'] is None:
            _fail('journal_not_pending')
        route, digest = _route(_observed_route(row, observed_route_raw), path)
        if digest != row['pending_route_sha']:
            _fail('activation_not_selected')
        if (_project(route) != (row['project_root'], row['project_dev'], row['project_ino']) or
                route['scopeId'] != row['scope_id'] or
                any(route.get(k) != row['pending'].get(k) for k in
                    ('pluginRoot', 'installId', 'generation', 'bundleManifestSha256',
                     'ownershipManifestSha256', 'vectorProfile'))):
            _fail('activation_identity_mismatch')
        proof = _inspect_identity(row['pending'])
        att_sha, file_identity = _bind_attestation(path, row['pending'], proof)
        return {'state': 'active', 'selected': _canon(row['pending']),
                'selected_route_sha': digest, 'pending': None,
                'pending_route_sha': None,
                'provider_attestation_sha': att_sha,
                'provider_file_identity': (_canon(file_identity) if file_identity is not None else None)}
    return _cas(journal_path, expected_epoch, activate)


def cas_mark_new_write(journal_path, expected_epoch):
    def mark(_path, row):
        if row['state'] != 'active' or row['selected'] is None:
            _fail('journal_not_active')
        return {'new_write_seen': 1}
    return _cas(journal_path, expected_epoch, mark)


def cas_restore(journal_path, expected_epoch, observed_old_route_raw):
    def restore(path, row):
        if row['state'] != 'active' or row['previous'] is None or row['new_write_seen']:
            _fail('rollback_not_authorized')
        route, digest = _route(_observed_route(row, observed_old_route_raw), path)
        if digest != row['previous_route_sha'] or any(route.get(k) != row['previous'].get(k)
                for k in ('pluginRoot', 'installId', 'generation',
                          'bundleManifestSha256', 'ownershipManifestSha256',
                          'vectorProfile')):
            _fail('rollback_route_mismatch')
        _inspect_identity(row['previous'])
        return {'state': 'restored_old', 'selected': _canon(row['previous']),
                'selected_route_sha': digest, 'new_write_seen': 0,
                'provider_attestation_sha': row['previous_provider_attestation_sha'],
                'provider_file_identity': (_canon(row['previous_provider_file_identity'])
                                           if row['previous_provider_file_identity'] is not None else None)}
    return _cas(journal_path, expected_epoch, restore)


def cas_restore_qmd(journal_path, expected_epoch, expected_new_route_sha):
    """Record an initial QMD selection after the reviewed route was removed.

    Only the QMD-origin cutover ledger may call this transition. Its durable
    restoring phase must be checked by the caller before route removal. A
    missing route on its own never authorizes legacy fallback.
    """
    def restore(_path, row):
        if (row['state'] != 'active' or row['previous'] is not None or
                row['new_write_seen'] or row['selected'] is None or
                row['selected_route_sha'] != expected_new_route_sha):
            _fail('qmd_rollback_not_authorized')
        from laya_qmd_cutover import LEDGER, MARKER, _check_marker, _ledger
        cutover = _ledger(Path(_path).parent / LEDGER)
        if (cutover['phase'] != 'restoring' or
                cutover['journalPath'] != str(_path) or
                cutover['projectRoot'] != row['project_root'] or
                cutover['routeSha256'] != expected_new_route_sha):
            _fail('qmd_restore_ledger_mismatch')
        _check_marker(cutover, Path(row['project_root']) / '.auto-context' / MARKER)
        route_path = Path(row['project_root']) / '.auto-context' / ROUTE_FILE
        if route_path.exists() or route_path.is_symlink():
            _fail('qmd_route_still_selected')
        return {'state': 'restored_qmd', 'selected': None,
                'selected_route_sha': None, 'pending': None,
                'pending_route_sha': None, 'new_write_seen': 0,
                'provider_attestation_sha': None, 'provider_file_identity': None}
    return _cas(journal_path, expected_epoch, restore)


def verify_selected_route(decision):
    """Read-only product proof for a host-validated candidate decision.

    The caller must still construct LayaIndexRuntime, which rechecks DB and
    queue identity before serving. No legacy fallback is authorized on error.
    """
    if (not isinstance(decision, dict) or decision.get('state') != 'candidate' or
            any(type(decision.get(key)) is not str for key in
                ('journalPath', 'pluginRoot', 'projectRoot', 'dbPath',
                 'queuePath', 'dbIncarnation', 'scopeId'))):
        _fail('unselected_host_route')
    journal = _private_parent(decision.get('journalPath'))
    engine = Path(decision.get('pluginRoot'))
    raw = _observed_route({'project_root': decision.get('projectRoot')}, None)
    route, digest = _route(raw, journal)
    if (journal != journal.parent / 'install-journal.sqlite' or
            (route['schema'] == ROUTE_SCHEMA and engine.parent != journal.parent)):
        _fail('journal_engine_root_mismatch')
    fresh = select_route(route['projectRoot'], action='recall')
    if fresh.get('state') != 'candidate' or fresh != decision:
        _fail('host_route_unverified')
    for key in ROUTE_FIELDS:
        if key in fresh and fresh[key] != route[key]:
            _fail('host_decision_changed')
    path, db = _open(journal, write=False)
    with closing(db):
        db.execute('BEGIN')
        try:
            row = _row(db)
            if (row['state'] not in ('active', 'restored_old') or
                    row['selected'] is None or row['selected_route_sha'] != digest or
                    _project(route) != (row['project_root'], row['project_dev'], row['project_ino']) or
                    route['scopeId'] != row['scope_id'] or
                    any(route.get(k) != row['selected'].get(k) for k in
                        ('pluginRoot', 'installId', 'generation',
                         'bundleManifestSha256', 'ownershipManifestSha256',
                         'vectorProfile'))):
                _fail('inactive_or_mismatched_journal')
            proof = _inspect_identity(row['selected'])
            inventory = row['user_qmd_inventory']
            observed = _user_qmd_inventory(Path(row['selected']['pluginRoot']),
                                           [r['root'] for r in inventory])
            if observed != inventory:
                _fail('user_qmd_inventory_changed')
            # Read-only DB/queue identity classification. The runtime repeats
            # both checks when opening writable connections.
            db_path, queue_path = Path(route['dbPath']), Path(route['queuePath'])
            if (not db_path.is_absolute() or not queue_path.is_absolute() or
                    db_path.is_symlink() or queue_path.is_symlink() or
                    db_path == queue_path):
                _fail('invalid_index_paths')
            for candidate_path in (db_path, queue_path):
                canonical_path = candidate_path.resolve(strict=True)
                for foreign in inventory:
                    user_root = Path(foreign['root'])
                    if canonical_path == user_root or user_root in canonical_path.parents:
                        _fail('index_path_overlaps_user_qmd')
                plugin_root = Path(row['selected']['pluginRoot'])
                if canonical_path == plugin_root or plugin_root in canonical_path.parents:
                    _fail('index_path_overlaps_plugin')
            with closing(sqlite3.connect(db_path.resolve(strict=True).as_uri() + '?mode=ro',
                                         uri=True, timeout=2)) as index_db:
                index_db.execute('BEGIN')
                LayaIndexA._check_header(index_db)
                incarnation = index_db.execute(
                    "SELECT value FROM schema_meta WHERE key='incarnation'").fetchone()[0]
                if incarnation != route['dbIncarnation']:
                    _fail('index_incarnation_mismatch')
                index_db.commit()
            with closing(sqlite3.connect(queue_path.resolve(strict=True).as_uri() + '?mode=ro',
                                         uri=True, timeout=2)) as queue_db:
                queue_db.execute('BEGIN')
                if ClaimQueue._check_database(queue_db) != incarnation:
                    _fail('queue_incarnation_mismatch')
                queue_db.commit()
            db.commit()
        except (OSError, RuntimeError, sqlite3.Error, IndexContractError,
                UnknownDatabase) as exc:
            db.rollback()
            raise JournalEvidenceError('runtime_identity_unavailable') from exc
        except BaseException:
            db.rollback()
            raise
    full = row['selected']['bundleKind'] == 'full_plugin'
    executing_root = _verified_executing_root(row['selected'], proof) if full else None
    attestation, observed_sha = _attestation(path) if full else (None, None)
    sealed_sha = row['provider_attestation_sha']
    if sealed_sha is not None and observed_sha != sealed_sha:
        _fail('production_attestation_changed')
    provider_manifest = None
    if sealed_sha is not None:
        if (row['provider_file_identity'] is None or
                _provider_file_identity(attestation) != row['provider_file_identity'] or
                attestation.get('vectorProfile') != proof.get('vectorProfile') or
                any(attestation.get(key) != proof.get(key) for key in
                    ('sourceCommit', 'bundleManifestSha256',
                     'ownershipManifestSha256'))):
            _fail('production_provider_identity_changed')
        # Full hashes and recipe closure were checked at installer activation.
        # The journal seals that result; per-hook stat checks keep the model
        # and native closure bounded without reopening hundreds of MB.
        provider_manifest = attestation['providerManifest']
    approved = full and sealed_sha is not None and provider_manifest is not None
    return {'status': ('installer_active_full_plugin_verified' if approved else
                       'production_attestation_unapproved' if sealed_sha is not None else
                       'production_attestation_unbound' if full else
                       'engine_subbundle_verified_host_closure_unproved'),
            'productRouteAllowed': approved,
            'journalPath': str(path), 'selectionEpoch': row['epoch'],
            'pluginRoot': row['selected']['pluginRoot'], 'sourceCommit': proof['sourceCommit'],
            **({'executingPluginRoot': executing_root} if full else {}),
            'bundleManifestSha256': proof['bundleManifestSha256'],
            'ownershipManifestSha256': proof['ownershipManifestSha256'],
            'installId': route['installId'], 'generation': route['generation'],
            'projectRoot': route['projectRoot'], 'scopeId': route['scopeId'],
            'dbIncarnation': route['dbIncarnation'],
            'vectorProfile': proof.get('vectorProfile'),
            **({'providerManifest': provider_manifest} if approved else {})}


def verify_worker_attestation(decision):
    """Rehash model, worker and native closure once before an owned worker starts."""
    selected = verify_selected_route(decision)
    if selected.get('productRouteAllowed') is not True:
        _fail('worker_provider_unapproved')
    path, db = _open(selected['journalPath'], write=False)
    with closing(db):
        db.execute('BEGIN')
        try:
            row = _row(db)
            if (row['epoch'] != selected['selectionEpoch'] or
                    row['provider_attestation_sha'] is None or
                    row['selected'] is None):
                _fail('worker_selection_changed')
            identity = row['selected']
            expected_sha = row['provider_attestation_sha']
            db.commit()
        except BaseException:
            db.rollback()
            raise
    proof = _inspect_identity(identity)
    record, digest = _attestation(path)
    if digest != expected_sha:
        _fail('worker_attestation_changed')
    try:
        manifest = validate_production_provider_attestation(
            proof.get('vectorProfile'), proof, record)
    except BundleEvidenceError as exc:
        raise JournalEvidenceError('worker_attestation_invalid') from exc
    if verify_selected_route(decision) != selected:
        _fail('worker_selection_changed')
    return manifest


def verify_development_vector_approval(decision, approval_manifest_path):
    """Explicit, read-only L0 proof; never authorizes the product hook."""
    proof = verify_selected_route(decision)
    if proof['productRouteAllowed'] or proof['vectorProfile'] is None:
        _fail('development_approval_not_applicable')
    try:
        path = Path(approval_manifest_path)
    except (TypeError, ValueError) as exc:
        raise JournalEvidenceError('invalid_development_approval_path') from exc
    if (not path.is_absolute() or path.is_symlink() or
            path != Path(proof['journalPath']).parent / 'development-vector-approval.json'):
        _fail('invalid_development_approval_path')
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077 or
                info.st_size > 8192):
            _fail('unsafe_development_approval')
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            opened = os.fstat(fd)
            raw = os.read(fd, 8193)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        now = path.lstat()
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns,
                              s.st_ctime_ns)
        if (len(raw) > 8192 or identity(info) != identity(opened) or
                identity(info) != identity(after) or identity(info) != identity(now)):
            _fail('development_approval_changed')
        approval = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                              parse_constant=_nonfinite)
    except (OSError, TypeError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise JournalEvidenceError('development_approval_unavailable') from exc
    expected = {'schema': DEVELOPMENT_APPROVAL_SCHEMA, 'testOwned': True,
                'pluginRoot': proof['pluginRoot'], 'sourceCommit': proof['sourceCommit'],
                'bundleManifestSha256': proof['bundleManifestSha256'],
                'ownershipManifestSha256': proof['ownershipManifestSha256'],
                'vectorProfile': proof['vectorProfile']}
    if type(approval) is not dict or approval != expected:
        _fail('development_approval_mismatch')
    return {**proof, 'status': 'development_only_vector_provider_verified',
            'productRouteAllowed': False, 'developmentOnlyRouteAllowed': True}

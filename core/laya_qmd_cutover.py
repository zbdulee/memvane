"""Installer-owned QMD-origin cutover with a persistent route recovery marker.

Only explicit installer calls mutate the candidate, journal, or route. QMD is
opened read-only; its original database and backup are never removed. The
project marker keeps an interrupted selected route from silently becoming a
legacy QMD hook invocation.

Ledger schemas: v1 (one collection) and v2 (raw+wiki) bind a candidate that
the full-scan producer published here (``bind_qmd_origin``). v3 binds a
candidate an inactive initial build already sealed on the capacity path
(``bind_qmd_origin_bound``): its evidence is the stored generation binding,
the catalog snapshot it names and the vector seal, re-proved from those
durable stores. A v3 proof never scans the source tree and never publishes.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import fcntl
from functools import wraps
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
from laya_index_source_port import (publish_collections_from_source,
                                    publish_from_source, scan,
                                    scan_collections)
from laya_index_vector import verify_generation_seal
from laya_qmd_migration import (QmdMigrationBlocked, inspect_qmd,
                                inspect_qmd_collections,
                                verify_restore, verify_restore_collections)
from laya_install_journal import (cas_activate, cas_restore_qmd, inspect_journal,
                                  preflight_product_candidate)

SCHEMA = 'laya-qmd-product-cutover-v1'
SCHEMA_V2 = 'laya-qmd-product-cutover-v2'
SCHEMA_V3 = 'laya-qmd-product-cutover-v3'
MARKER_SCHEMA = 'laya-qmd-selection-marker-v1'
LEDGER = 'qmd-cutover.json'
MARKER = 'laya-index-selection.json'
ROUTE = 'laya-index-route.json'
SHA = re.compile(r'[0-9a-f]{64}\Z')
HEX32 = re.compile(r'[0-9a-f]{32}\Z')
PHASES = ('prepared', 'switching', 'active', 'restoring', 'restored')
# v3 only, terminal: a ``prepared`` binding whose QMD digest moved after the
# bind, withdrawn so a fresh install can bind a fresh generation. Nothing in
# its state root is deleted; it never selects, activates or rebinds again.
RETIRED = 'retired'
V3_PHASES = PHASES + (RETIRED,)
RETIREMENT = 'cutover-retirement.json'
RETIREMENT_SCHEMA = 'laya-qmd-cutover-retirement-v1'
RETIREMENT_SCHEMA_V2 = 'laya-qmd-cutover-retirement-v2'
REPLACEMENT_RETIREMENT = 'replacement_evidence_invalid'
SOURCE_IDENTITY_RETIREMENT = 'source_identity_changed'
# v3 files whose (dev, ino) the ledger pins.
BOUND_FILES = (('sourceDb', 'sourceIdentity'), ('backupDb', 'backupIdentity'),
               ('candidateDb', 'candidateIdentity'), ('queueDb', 'queueIdentity'),
               ('catalogDb', 'catalogIdentity'), ('bindingStore', 'bindingStoreIdentity'))
# What a replayed v3 bind must repeat exactly. Only the backup of the same QMD
# digest may be refreshed while the ledger is still ``prepared``. A changed
# QMD digest is never rebound: the sealed generation predates that change, so
# a fresh generation is required (``cutover_source_drift_requires_fresh_generation``).
BOUND_IDENTITY =('projectRoot', 'journalPath', 'routeSha256', 'sourceDb',
                  'sourceFormat', 'collections', 'candidateDb', 'queueDb', 'catalogDb',
                  'bindingStore', 'scope', 'indexGeneration', 'sourceManifest',
                  'bindingDigest', 'catalogScanId', 'catalogRawDigest',
                  'vectorProfileId', 'vectorRows', 'candidateIncarnation',
                  'attestationSha256', 'providerFileIdentitySha256', 'sourceIdentity',
                  'candidateIdentity', 'queueIdentity', 'catalogIdentity',
                  'bindingStoreIdentity')
BOUND_REFRESH = ('backupDb', 'backupIdentity')
V3_FIELDS = frozenset(('schema', 'epoch', 'phase', 'sourceDigest',
                       *BOUND_IDENTITY, *BOUND_REFRESH))
V3_REPLACEMENT_FIELDS = V3_FIELDS | {'replacementEvidenceSha256'}
V3_RETIREMENT_OBSERVED = 'retirementObservedSourceDigest'
V3_RETIREMENT_IDENTITY = 'retirementObservedSourceIdentity'
SOURCE_DRIFT = 'cutover_source_drift_requires_fresh_generation'
# Ledger field -> ``laya_initial_build.evidence`` field.
BOUND_EVIDENCE = (('indexGeneration', 'generation'), ('bindingDigest', 'bindingDigest'),
                  ('sourceManifest', 'sourceManifest'), ('catalogScanId', 'catalogScanId'),
                  ('catalogRawDigest', 'catalogRawDigest'),
                  ('vectorProfileId', 'vectorProfileId'), ('vectorRows', 'vectorRows'),
                  ('candidateIncarnation', 'dbIncarnation'))


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
    return _validated(_read_json(ledger_path), ledger_path)


def _identity_pair(value):
    return (isinstance(value, list) and len(value) == 2 and
            all(type(v) is int and v >= 0 for v in value))


def _two_collections(collections):
    try:
        from laya_qmd_migration import _collection_names
        names = _collection_names([item['name'] for item in collections])
        return (isinstance(collections, list) and len(collections) == 2
                and [item['name'] for item in collections] == list(names)
                and all(isinstance(item, dict) and set(item) ==
                        {'name','role','sourceRoot'} and item['role'] in
                        ('raw','wiki') and isinstance(item['sourceRoot'], str)
                        for item in collections)
                and sorted(item['role'] for item in collections) == ['raw','wiki'])
    except (TypeError, KeyError, ValueError, QmdMigrationBlocked):
        return False


def _validated_v3(row):
    from laya_index_capacity import generation_digest
    identities = {field for _name, field in BOUND_FILES}
    scalar = V3_FIELDS - identities - {'epoch', 'phase', 'collections', 'vectorRows'}
    fields = set(row) - {'retirementReason', V3_RETIREMENT_OBSERVED,
                         V3_RETIREMENT_IDENTITY}
    if (fields not in (V3_FIELDS, V3_REPLACEMENT_FIELDS)
            or type(row['epoch']) is not int or row['epoch'] < 1
            or row['phase'] not in V3_PHASES
            or any(not isinstance(row[k], str) or not row[k] for k in scalar)
            or any(not _identity_pair(row[k]) for k in identities)
            or type(row['vectorRows']) is not int or row['vectorRows'] < 1
            or any(not SHA.fullmatch(row[k]) for k in
                   ('routeSha256', 'sourceDigest', 'sourceManifest', 'bindingDigest',
                    'catalogRawDigest', 'vectorProfileId', 'attestationSha256',
                    'providerFileIdentitySha256'))
            or ('replacementEvidenceSha256' in row and
                (not isinstance(row['replacementEvidenceSha256'], str) or
                 not SHA.fullmatch(row['replacementEvidenceSha256'])))
            or ('retirementReason' in row and
                (row['phase'] != RETIRED or
                 row['retirementReason'] not in
                 (REPLACEMENT_RETIREMENT, SOURCE_IDENTITY_RETIREMENT) or
                 (row['retirementReason'] == REPLACEMENT_RETIREMENT and
                  'replacementEvidenceSha256' not in row) or
                 (row['retirementReason'] == SOURCE_IDENTITY_RETIREMENT and
                  (V3_RETIREMENT_IDENTITY not in row or
                   V3_RETIREMENT_OBSERVED not in row))))
            or (V3_RETIREMENT_OBSERVED in row and
                (row['phase'] != RETIRED or
                 not isinstance(row[V3_RETIREMENT_OBSERVED], str) or
                 not SHA.fullmatch(row[V3_RETIREMENT_OBSERVED])))
            or (V3_RETIREMENT_IDENTITY in row and
                (row.get('retirementReason') != SOURCE_IDENTITY_RETIREMENT or
                 not _identity_pair(row[V3_RETIREMENT_IDENTITY]) or
                 row[V3_RETIREMENT_IDENTITY] == row['sourceIdentity'] or
                 row[V3_RETIREMENT_OBSERVED] != row['sourceDigest']))
            or any(not HEX32.fullmatch(row[k]) for k in
                   ('scope', 'catalogScanId', 'candidateIncarnation'))
            or generation_digest(row['indexGeneration']) != row['bindingDigest']
            or not _two_collections(row['collections'])
            or len({row[name] for name, _field in BOUND_FILES}) != len(BOUND_FILES)):
        _fail('cutover_ledger_invalid')
    return row


def _validated(row, ledger_path):
    common = {'schema','epoch','phase','projectRoot','journalPath','routeSha256',
                'sourceDb','backupDb','sourceFormat','collection','sourceDigest',
                'sourceRoot','candidateDb','queueDb','scope','indexGeneration',
                'sourceManifest','vectorProfileId','claim','candidateIncarnation',
                'attestationSha256','providerFileIdentitySha256',
                'sourceIdentity','backupIdentity','candidateIdentity','queueIdentity'}
    if not isinstance(row, dict):
        _fail('cutover_ledger_invalid')
    if row.get('schema') == SCHEMA_V3:
        _validated_v3(row)
        if Path(row['journalPath']).parent / LEDGER != Path(ledger_path):
            _fail('cutover_ledger_path_mismatch')
        return row
    if row.get('schema') == SCHEMA:
        required = common
    elif row.get('schema') == SCHEMA_V2:
        required = (common - {'collection','sourceRoot'}) | {'collections'}
    else:
        _fail('cutover_ledger_invalid')
    scalar = required - {'epoch','phase','sourceIdentity','backupIdentity',
                         'candidateIdentity','queueIdentity','collections'}
    if (set(row) != required
            or type(row['epoch']) is not int or row['epoch'] < 1
            or row['phase'] not in PHASES
            or any(not isinstance(row[k], str) or not row[k] for k in
                   scalar)
            or any(not _identity_pair(row[k]) for k in
                   ('sourceIdentity','backupIdentity','candidateIdentity','queueIdentity'))
            or not SHA.fullmatch(row['routeSha256'])
            or not SHA.fullmatch(row['sourceDigest'])
            or not SHA.fullmatch(row['sourceManifest'])
            or not SHA.fullmatch(row['attestationSha256'])
            or not SHA.fullmatch(row['providerFileIdentitySha256'])):
        _fail('cutover_ledger_invalid')
    if row['schema'] == SCHEMA_V2 and not _two_collections(row['collections']):
        _fail('cutover_ledger_invalid')
    if Path(row['journalPath']).parent / LEDGER != Path(ledger_path):
        _fail('cutover_ledger_path_mismatch')
    return row


def _source_binding(row):
    if row['schema'] == SCHEMA:
        return (row['collection'],), row['sourceRoot']
    collections = row['collections']
    return tuple(item['name'] for item in collections), {
        item['name']: item['sourceRoot'] for item in collections}


def _qmd_digest(source_db, source_format, names, multi):
    return (inspect_qmd_collections(source_db, source_format, names)['digest']
            if multi else inspect_qmd(source_db, source_format, names[0])['digest'])


def _restore_verified(backup_db, source_format, names, digest, multi):
    if multi:
        return verify_restore_collections(backup_db, source_format, names, digest)
    return verify_restore(backup_db, source_format, names[0], digest)


def _source_manifest(index, roots, scope, multi):
    return (scan_collections(roots, scope, index).manifest if multi
            else scan(roots, scope, index).manifest)


def _preflight_multi_settings(route, project_root):
    """Refuse a route the selected host cannot serve before publication."""
    import config
    from laya_index_host_route import (_multi_compile_supported,
                                       _multi_settings_supported, _pairs,
                                       _read_regular, _source_for_collection)
    settings = Path(project_root) / '.auto-context' / 'settings.json'
    if settings.is_symlink():
        _fail('cutover_settings_unsafe')
    raw = _read_regular(settings, 2 * 1024 * 1024)
    if _sha(raw) != route['settingsSha256']:
        _fail('cutover_settings_changed')
    parsed = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs)
    if not isinstance(parsed, dict):
        _fail('cutover_settings_unsupported')
    cfg = config.normalize_config(parsed)
    declared = route['collections']
    names = tuple(item['name'] for item in declared)
    roles = config.role_map(cfg)
    if (cfg.get('indexing') is not True or
            sorted(cfg.get('collections', [])) != list(names) or
            not _multi_settings_supported(parsed, cfg, names) or
            set(cfg.get('collectionPaths', {})) != set(names)):
        _fail('cutover_settings_unsupported')
    wiki_root = next((item['sourceRoot'] for item in declared
                      if item['role'] == 'wiki'), None)
    if (wiki_root is None or not _multi_compile_supported(
            parsed, cfg, Path(project_root), wiki_root)):
        _fail('cutover_settings_unsupported')
    for item in declared:
        name = item['name']
        if (item['role'] != roles.get(name) or
                item['sourceRoot'] != _source_for_collection(
                    Path(project_root), cfg['collectionPaths'][name])):
            _fail('cutover_settings_mismatch')


def _preflight_single_settings(route, project_root):
    import config
    from laya_index_host_route import (_pairs, _read_regular,
                                       _selected_settings_supported,
                                       _source_for_collection)
    settings = Path(project_root) / '.auto-context' / 'settings.json'
    if settings.is_symlink():
        _fail('cutover_settings_unsafe')
    raw = _read_regular(settings, 2 * 1024 * 1024)
    if _sha(raw) != route['settingsSha256']:
        _fail('cutover_settings_changed')
    parsed = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs)
    if not isinstance(parsed, dict):
        _fail('cutover_settings_unsupported')
    cfg = config.normalize_config(parsed)
    name = route['collectionName']
    raw_compile = parsed.get('compile', {})
    if (cfg.get('indexing') is not True or
            cfg.get('collections') != [name] or
            set(cfg.get('collectionPaths', {})) != {name} or
            not _selected_settings_supported(parsed, cfg, name) or
            not isinstance(raw_compile, dict) or
            raw_compile.get('mode', 'off') != 'off' or
            raw_compile.get('enabled') is True or
            raw_compile.get('autoWrite') is True or
            config.compile_active(cfg.get('compile')) or
            route['sourceRoot'] != _source_for_collection(
                Path(project_root), cfg['collectionPaths'][name])):
        _fail('cutover_settings_unsupported')


def _preflight_bound_settings(row, route_bytes):
    from laya_install_journal import _route
    from laya_index_host_route import SCHEMA as ROUTE_V1, SCHEMA_V2 as ROUTE_V2
    route, digest = _route(route_bytes, Path(row['journalPath']))
    if digest != row['routeSha256']:
        _fail('cutover_route_mismatch')
    if row['schema'] in (SCHEMA_V2, SCHEMA_V3):
        if (route['schema'] != ROUTE_V2 or
                route['collections'] != row['collections']):
            _fail('cutover_route_mismatch')
        _preflight_multi_settings(route, row['projectRoot'])
    else:
        if (route['schema'] != ROUTE_V1 or
                route['collectionName'] != row['collection'] or
                route['sourceRoot'] != row['sourceRoot']):
            _fail('cutover_route_mismatch')
        _preflight_single_settings(route, row['projectRoot'])


def _identity(path):
    path = Path(path)
    if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or path.resolve(strict=True) != path):
        _fail('cutover_bound_file_unavailable')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        _fail('cutover_bound_file_unsafe')
    return [info.st_dev, info.st_ino]


def _check_bound_evidence(row, restore):
    """Re-prove a v3 binding from its durable stores; never scans the source.

    Every ledger file keeps its (dev, ino); QMD still has the bound digest and
    the backup restores to it. Before selection (``restore=False``) the
    candidate must still serve exactly the sealed build: the same published
    head under its stored binding, the catalog snapshot that binding names
    as the current servable one with the same live root nodes, and the
    vector seal for the reviewed profile, all read through the incarnation-
    and queue-checked runtime. For a rollback (``restore=True``) the head
    must still be the bound generation (nothing was published after
    selection), but the catalog may have rescanned and the roots may be
    gone: the user is leaving the candidate, not serving it.
    """
    _check_replacement_source(row)
    for name, field in BOUND_FILES:
        if _identity(row[name]) != row[field]:
            _fail('cutover_bound_file_replaced')
    names, roots = _source_binding(row)
    if _qmd_digest(row['sourceDb'], row['sourceFormat'], names, True) != row['sourceDigest']:
        # Before selection the sealed generation is stale; it is never rebound.
        _fail('cutover_source_changed' if restore else SOURCE_DRIFT)
    _restore_verified(row['backupDb'], row['sourceFormat'], names,
                      row['sourceDigest'], True)
    if restore:
        with closing(LayaIndexA(row['candidateDb'])) as index:
            if index.incarnation() != row['candidateIncarnation']:
                _fail('cutover_candidate_changed')
            index.db.execute('BEGIN')
            try:
                head = index.db.execute('SELECT gen FROM head WHERE scope=?',
                                        (row['scope'],)).fetchone()
                index.db.rollback()
            except BaseException:
                index.db.rollback()
                raise
        if head != (row['indexGeneration'],):
            _fail('cutover_candidate_evidence_mismatch')
        with closing(ClaimQueue.open(row['queueDb'])) as queue:
            if queue.binding() != row['candidateIncarnation']:
                _fail('cutover_queue_binding_mismatch')
        return
    import laya_initial_build
    from laya_index_host_route import wiki_index_roots
    from laya_index_runtime import LayaIndexRuntime, capacity_store_paths
    if capacity_store_paths(row['candidateDb']) != (row['catalogDb'], row['bindingStore']):
        _fail('cutover_capacity_store_mismatch')
    with LayaIndexRuntime(source_root=wiki_index_roots(row['collections']), scope=row['scope'],
                          db_path=row['candidateDb'], queue_path=row['queueDb'],
                          db_incarnation=row['candidateIncarnation'],
                          catalog_path=row['catalogDb'],
                          binding_store_path=row['bindingStore']) as runtime:
        live = laya_initial_build.evidence(runtime, row['indexGeneration'],
                                           row['vectorProfileId'])
    if live is None or any(live[theirs] != row[ours] for ours, theirs in BOUND_EVIDENCE):
        _fail('cutover_candidate_evidence_mismatch')


def _check_replacement_source(row):
    """An inactive model proof may bind a Laya candidate, never the QMD route.

    The old shared generation must still be selected while QMD is the source.
    This proof is intentionally not consulted by forward recovery after the
    Laya journal committed: the old QMD pointer need not remain selected then.
    """
    expected = row.get('replacementEvidenceSha256')
    if expected is None:
        return
    try:
        import shared_qmd_setup
        from laya_install_journal import _attestation
        evidence = shared_qmd_setup.replacement_model_evidence()
        attestation, attestation_sha = _attestation(row['journalPath'])
    except Exception as exc:
        raise CutoverBlocked('cutover_replacement_evidence_changed') from exc
    provider = attestation.get('providerManifest') if isinstance(attestation, dict) else None
    if (not isinstance(evidence, dict) or
            not isinstance(evidence.get('modelSha256'), str) or
            not SHA.fullmatch(evidence['modelSha256']) or
            type(evidence.get('dimension')) is not int or
            evidence['dimension'] < 1 or
            not isinstance(provider, dict) or
            not isinstance(provider.get('modelSha256'), str) or
            not SHA.fullmatch(provider['modelSha256']) or
            type(provider.get('dimension')) is not int or
            provider['dimension'] < 1 or
            evidence.get('evidenceSha256') != expected or
            evidence.get('sourceDb') != row['sourceDb'] or
            attestation_sha != row['attestationSha256'] or
            provider.get('modelSha256') != evidence.get('modelSha256') or
            provider.get('dimension') != evidence.get('dimension')):
        _fail('cutover_replacement_evidence_changed')


def _transition_source_error(row, names, multi):
    """Return a reason while switching, so an uncommitted route can withdraw."""
    try:
        current = _qmd_digest(row['sourceDb'], row['sourceFormat'], names, multi)
    except Exception:
        return 'cutover_source_unavailable'
    if current != row['sourceDigest']:
        return SOURCE_DRIFT if row['schema'] == SCHEMA_V3 else 'cutover_source_changed'
    try:
        _check_replacement_source(row)
    except Exception:
        return 'cutover_replacement_evidence_changed'
    return None


def _check_evidence(row, *, restore=False):
    if row['schema'] == SCHEMA_V3:
        return _check_bound_evidence(row, restore)
    for name, field in (('sourceDb','sourceIdentity'), ('backupDb','backupIdentity'),
                        ('candidateDb','candidateIdentity'), ('queueDb','queueIdentity')):
        if _identity(row[name]) != row[field]:
            _fail('cutover_bound_file_replaced')
    names, roots = _source_binding(row)
    multi = row['schema'] == SCHEMA_V2
    if _qmd_digest(row['sourceDb'], row['sourceFormat'], names, multi) != row['sourceDigest']:
        _fail('cutover_source_changed')
    _restore_verified(row['backupDb'], row['sourceFormat'], names,
                      row['sourceDigest'], multi)
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
        from laya_index_host_route import wiki_index_roots
        index_roots = wiki_index_roots(row['collections']) if multi else roots
        if _source_manifest(index, index_roots, row['scope'], multi) != row['sourceManifest']:
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


def _recover_missing_marker(row, marker_path, route_path, route_bytes):
    """Finish the prepared ledger's interrupted marker publication only."""
    if (row['phase'] != 'prepared' or marker_path.exists() or marker_path.is_symlink()
            or route_path.exists() or route_path.is_symlink()):
        _fail('cutover_marker_changed')
    if not isinstance(route_bytes, bytes) or _sha(route_bytes) != row['routeSha256']:
        _fail('cutover_route_mismatch')
    journal = inspect_journal(row['journalPath'])
    if (journal['state'] != 'candidate_ready' or journal['pending'] is None or
            journal['pendingRouteSha256'] != row['routeSha256']):
        _fail('cutover_journal_not_ready')
    _preflight_bound_settings(row, route_bytes)
    _check_evidence(row)
    approval = preflight_product_candidate(row['journalPath'])
    if (approval['pendingRouteSha256'] != row['routeSha256'] or
            approval['attestationSha256'] != row['attestationSha256'] or
            approval['providerFileIdentitySha256'] != row['providerFileIdentitySha256']):
        _fail('cutover_attestation_changed')
    _write(marker_path, _marker(row), replace=False)
    _check_marker(row, marker_path)


@contextmanager
def _ledger_lock(ledger_path):
    lock_path = Path(ledger_path).with_suffix('.lock')
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            _fail('cutover_lock_unsafe')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@contextmanager
def _operation_lock(marker_path):
    """Serialize route mutations for one project across different state roots.

    A separate lock avoids nesting ``_ledger_lock`` inside ``_change``. The
    installed product lock, when present, is acquired first by its caller.
    Readers may observe intermediate phases and must classify them blocked.
    """
    parent = Path(marker_path).parent
    info = parent.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
            stat.S_IMODE(info.st_mode) & 0o022):
        _fail('cutover_operation_parent_unsafe')
    path = parent / 'laya-qmd-cutover-operation.lock'
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR |
                     getattr(os, 'O_NOFOLLOW', 0), 0o600)
    except OSError as exc:
        raise CutoverBlocked('cutover_operation_lock_unavailable') from exc
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or
                before.st_nlink != 1 or stat.S_IMODE(before.st_mode) & 0o077):
            _fail('cutover_operation_lock_unsafe')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CutoverBlocked('cutover_operation_busy') from exc
        after = path.lstat()
        if (not stat.S_ISREG(after.st_mode) or
                (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)):
            _fail('cutover_operation_lock_changed')
        yield
    finally:
        os.close(fd)


def _serialized_operation(fn):
    @wraps(fn)
    def guarded(*args, **kwargs):
        project = kwargs.get('project_root', args[0] if args else None)
        journal = kwargs.get('journal_path', args[1] if len(args) > 1 else None)
        _ledger_path, marker_path, _route_path = _paths(project, journal)
        with _operation_lock(marker_path):
            return fn(*args, **kwargs)
    return guarded


def _change(ledger_path, expected_epoch, expected_phase, next_phase, updates=None):
    with _ledger_lock(ledger_path):
        row = _ledger(ledger_path)
        if row['epoch'] != expected_epoch or row['phase'] != expected_phase:
            _fail('cutover_stale_cas')
        row['epoch'] += 1
        row['phase'] = next_phase
        row.update(updates or {})
        _validated(row, ledger_path)
        _write(ledger_path, row, replace=True)
        return row


@_serialized_operation
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
    from laya_index_host_route import SCHEMA_V2 as ROUTE_V2
    multi = route['schema'] == ROUTE_V2
    if multi:
        from laya_qmd_migration import _collection_names
        names = _collection_names(collection)
        if (not isinstance(source_root, dict) or
                route['collections'] != [
                    {'name': name, 'role': item['role'],
                     'sourceRoot': source_root.get(name)}
                    for name, item in zip(names, route['collections'])] or
                [item['name'] for item in route['collections']] != list(names)):
            _fail('cutover_source_root_mismatch')
        _preflight_multi_settings(route, project_root)
    else:
        names = (collection,)
        _preflight_single_settings(route, project_root)
    if _qmd_digest(source_db, source_format, names, multi) != source_digest:
        _fail('cutover_source_changed')
    _restore_verified(backup_db, source_format, names, source_digest, multi)
    if ((not multi and (route['sourceRoot'] != str(source_root) or
                       route['collectionName'] != collection)) or
            route['vectorProfile']['profileId'] != vector_profile_id or
            len({str(Path(p)) for p in (source_db, backup_db, candidate_db, queue_db)}) != 4):
        _fail('cutover_source_root_mismatch')
    with closing(LayaIndexA(candidate_db)) as index:
        from laya_index_host_route import wiki_index_roots
        index_roots = wiki_index_roots(route['collections']) if multi else source_root
        if _source_manifest(index, index_roots, scope, multi) != source_manifest:
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
        if multi:
            result = publish_collections_from_source(
                index, wiki_index_roots(route['collections']), scope, None,
                index_generation, claim,
                claim_digest=source_digest, claim_epoch=0, require_vector=True)
        else:
            result = publish_from_source(
                index, source_root, scope, None, index_generation, claim,
                claim_digest=source_digest, claim_epoch=0, require_vector=True)
        if result not in ('published','replayed'):
            _fail('cutover_publish_unavailable')
        incarnation = index.incarnation()
        if route['dbIncarnation'] != incarnation:
            _fail('cutover_candidate_incarnation_mismatch')
    if _qmd_digest(source_db, source_format, names, multi) != source_digest:
        _fail('cutover_source_changed')
    with closing(ClaimQueue.open(queue_db)) as queue:
        if queue.binding() != incarnation:
            _fail('cutover_queue_binding_mismatch')
    row = {'schema':SCHEMA_V2 if multi else SCHEMA,
           'epoch':1,'phase':'prepared','projectRoot':str(project_root),
           'journalPath':str(journal_path),'routeSha256':route_sha,
           'sourceDb':str(source_db),'backupDb':str(backup_db),
           'sourceFormat':source_format,'sourceDigest':source_digest,
           'candidateDb':str(candidate_db),
           'queueDb':str(queue_db),'scope':scope,'indexGeneration':index_generation,
           'sourceManifest':source_manifest,'vectorProfileId':vector_profile_id,
           'claim':claim,'candidateIncarnation':incarnation,
           'attestationSha256':approval['attestationSha256'],
           'providerFileIdentitySha256':approval['providerFileIdentitySha256'],
           'sourceIdentity':_identity(source_db),'backupIdentity':_identity(backup_db),
           'candidateIdentity':_identity(candidate_db),'queueIdentity':_identity(queue_db)}
    if multi:
        row['collections'] = route['collections']
    else:
        row['collection'] = collection
        row['sourceRoot'] = str(source_root)
    _check_evidence(row)
    _write(ledger_path, row, replace=False)
    _write(marker_path, _marker(row), replace=False)
    return row


def _inactive_approval(journal_path, route_sha):
    journal = inspect_journal(journal_path)
    if (journal['state'] != 'candidate_ready' or journal['pending'] is None or
            journal['pendingRouteSha256'] != route_sha or journal['newWriteSeen']):
        _fail('cutover_journal_not_ready')
    approval = preflight_product_candidate(journal_path)
    if approval['pendingRouteSha256'] != route_sha:
        _fail('cutover_attested_route_mismatch')
    return journal, approval


@_serialized_operation
def bind_qmd_origin_bound(*, project_root, journal_path, route_bytes, source_db,
                          backup_db, source_format, collection, source_digest,
                          source_root, candidate_db, queue_db, catalog_db,
                          binding_store, scope, index_generation, source_manifest,
                          binding_digest, catalog_scan_id, catalog_raw_digest,
                          vector_profile_id, vector_rows,
                          replacement_evidence_sha256=None):
    """Durably bind a sealed initial-build candidate to the still-selected QMD.

    The candidate was built, sealed and published on the capacity path by the
    inactive initial build; this neither scans the source tree nor publishes.
    It re-proves the exact inactive journal, attested route, project settings
    and provider, the QMD digest and its restorable backup, and the sealed
    candidate (``_check_bound_evidence``), then writes the v3 ledger and the
    project marker, each fsynced and no-clobber. QMD stays selected.

    Idempotent: a repeat with the same binding re-proves it, completes a
    marker an interruption left unwritten and returns the ledger. While the
    ledger is still ``prepared`` a repeat may refresh only the backup of the
    same QMD digest. QMD keeps indexing until activation; a changed digest
    means the sealed generation is stale and is refused as ``SOURCE_DRIFT``
    without touching the ledger, marker or backups. Any other difference is
    refused; a ledger that moved past ``prepared`` is returned unchanged.
    """
    ledger_path, marker_path, route_path = _paths(project_root, journal_path)
    if not isinstance(route_bytes, bytes) or not route_bytes or len(route_bytes) > 8192:
        _fail('cutover_route_invalid')
    from laya_install_journal import _route
    from laya_index_host_route import SCHEMA_V2 as ROUTE_V2
    from laya_index_runtime import capacity_store_paths
    from laya_qmd_migration import _collection_names
    route, route_sha = _route(route_bytes, Path(journal_path))
    if route_sha != _sha(route_bytes):
        _fail('cutover_route_invalid')
    names = _collection_names(collection)
    if (route['schema'] != ROUTE_V2 or route['projectRoot'] != str(project_root)
            or route['dbPath'] != str(candidate_db) or route['queuePath'] != str(queue_db)
            or route['scopeId'] != scope
            or route['vectorProfile']['profileId'] != vector_profile_id
            or not isinstance(source_root, dict) or set(source_root) != set(names)
            or [item['name'] for item in route['collections']] != list(names)
            or any(item['sourceRoot'] != source_root[item['name']]
                   for item in route['collections'])):
        _fail('cutover_route_mismatch')
    if capacity_store_paths(candidate_db) != (str(catalog_db), str(binding_store)):
        _fail('cutover_capacity_store_mismatch')
    if (replacement_evidence_sha256 is not None and
            (not isinstance(replacement_evidence_sha256, str) or
             not SHA.fullmatch(replacement_evidence_sha256))):
        _fail('cutover_replacement_evidence_invalid')
    if ledger_path.exists() or ledger_path.is_symlink():
        current = _ledger(ledger_path)
        if current['phase'] == RETIRED:
            # A retired binding is never revived; a fresh install binds anew.
            _fail('cutover_binding_retired')
        if current['phase'] != 'prepared':
            # The journal is no longer inactive; report the settled binding.
            if (current['schema'] != SCHEMA_V3 or current['routeSha256'] != route_sha or
                    current['candidateDb'] != str(candidate_db) or
                    current['indexGeneration'] != index_generation or
                    current.get('replacementEvidenceSha256') != replacement_evidence_sha256):
                _fail('cutover_selection_already_exists')
            _check_marker(current, marker_path)
            return current
    _journal, approval = _inactive_approval(journal_path, route_sha)
    _preflight_multi_settings(route, project_root)
    row = {'schema': SCHEMA_V3, 'epoch': 1, 'phase': 'prepared',
           'projectRoot': str(project_root), 'journalPath': str(journal_path),
           'routeSha256': route_sha, 'sourceDb': str(source_db),
           'backupDb': str(backup_db), 'sourceFormat': source_format,
           'sourceDigest': source_digest, 'collections': route['collections'],
           'candidateDb': str(candidate_db), 'queueDb': str(queue_db),
           'catalogDb': str(catalog_db), 'bindingStore': str(binding_store),
           'scope': scope, 'indexGeneration': index_generation,
           'sourceManifest': source_manifest, 'bindingDigest': binding_digest,
           'catalogScanId': catalog_scan_id, 'catalogRawDigest': catalog_raw_digest,
           'vectorProfileId': vector_profile_id, 'vectorRows': vector_rows,
           'candidateIncarnation': route['dbIncarnation'],
           'attestationSha256': approval['attestationSha256'],
           'providerFileIdentitySha256': approval['providerFileIdentitySha256']}
    if replacement_evidence_sha256 is not None:
        row['replacementEvidenceSha256'] = replacement_evidence_sha256
    for name, field in BOUND_FILES:
        row[field] = _identity(row[name])
    _validated(row, ledger_path)
    if ledger_path.exists() or ledger_path.is_symlink():
        return _replay_bound(row, ledger_path, marker_path, route_path)
    if (route_path.exists() or route_path.is_symlink() or
            marker_path.exists() or marker_path.is_symlink()):
        _fail('cutover_selection_already_exists')
    _check_evidence(row)
    # The proof read stores, not the journal: confirm it did not move meanwhile.
    _inactive_approval(journal_path, route_sha)
    _write(ledger_path, row, replace=False)
    _write(marker_path, _marker(row), replace=False)
    _check_marker(row, marker_path)
    return row


def _replay_bound(fresh, ledger_path, marker_path, route_path):
    with _ledger_lock(ledger_path):
        current = _ledger(ledger_path)
        if (current['schema'] != SCHEMA_V3 or
                any(current[key] != fresh[key] for key in BOUND_IDENTITY) or
                current.get('replacementEvidenceSha256') !=
                fresh.get('replacementEvidenceSha256')):
            _fail('cutover_selection_already_exists')
        if current['phase'] == RETIRED:
            _fail('cutover_binding_retired')
        if current['phase'] != 'prepared':
            # Selection moved on: activate/restore/recover settle it, not bind.
            _check_marker(current, marker_path)
            return current
        if route_path.exists() or route_path.is_symlink():
            _fail('cutover_route_uncertain')
        if marker_path.exists() or marker_path.is_symlink():
            _check_marker(current, marker_path)
        if current['sourceDigest'] != fresh['sourceDigest']:
            # The bound generation was sealed before QMD indexed this change.
            _fail(SOURCE_DRIFT)
        _check_evidence(fresh)
        _inactive_approval(fresh['journalPath'], fresh['routeSha256'])
        if any(current[key] != fresh[key] for key in BOUND_REFRESH):
            current = {**current, **{key: fresh[key] for key in BOUND_REFRESH},
                       'epoch': current['epoch'] + 1}
            _validated(current, ledger_path)
            _write(ledger_path, current, replace=True)
    if not marker_path.exists() and not marker_path.is_symlink():
        _write(marker_path, _marker(current), replace=False)
    _check_marker(current, marker_path)
    return current


def inspect_bound_cutover(journal_path):
    """The validated ledger beside ``journal_path``, or None. Read-only."""
    ledger_path = Path(journal_path).parent / LEDGER
    if not ledger_path.exists() and not ledger_path.is_symlink():
        return None
    return _ledger(ledger_path)


def _retirement(row, observed):
    result = {'schema': (RETIREMENT_SCHEMA_V2 if 'retirementReason' in row
                         else RETIREMENT_SCHEMA), 'projectRoot': row['projectRoot'],
            'journalPath': row['journalPath'], 'routeSha256': row['routeSha256'],
            'candidateDb': row['candidateDb'],
            'candidateIncarnation': row['candidateIncarnation'],
            'indexGeneration': row['indexGeneration'],
            'bindingDigest': row['bindingDigest'], 'backupDb': row['backupDb'],
            'boundSourceDigest': row['sourceDigest'],
            'observedSourceDigest': observed, 'marker': _marker(row)}
    if 'retirementReason' in row:
        result['reason'] = row['retirementReason']
    if V3_RETIREMENT_IDENTITY in row:
        result['observedSourceIdentity'] = row[V3_RETIREMENT_IDENTITY]
    return result


def _settle_retired_binding(row, record_path, marker_path, route_path):
    """Complete only this terminal ledger's receipt and marker withdrawal.

    Ownership, absence of a selected route, and journal inactivity were
    proved before the retired CAS. A later journal or route may belong to a
    new install; neither is read or modified here. An owned marker with any
    route remains uncertain and is never withdrawn automatically.
    """
    own_marker = False
    if marker_path.exists() or marker_path.is_symlink():
        if marker_path.is_symlink():
            _fail('cutover_marker_changed')
        current = _read_json(marker_path)
        if current == _marker(row):
            own_marker = True
            if route_path.exists() or route_path.is_symlink():
                _fail('cutover_route_uncertain')
        elif not isinstance(current, dict) or current.get('journalPath') == row['journalPath']:
            _fail('cutover_marker_changed')
    if record_path.exists() or record_path.is_symlink():
        stored = _read_json(record_path)
        if (not isinstance(stored, dict) or
                {key: value for key, value in stored.items()
                 if key != 'observedSourceDigest'} !=
                {key: value for key, value in _retirement(row, None).items()
                 if key != 'observedSourceDigest'} or
                not isinstance(stored.get('observedSourceDigest'), str) or
                not SHA.fullmatch(stored['observedSourceDigest']) or
                (V3_RETIREMENT_OBSERVED in row and
                 stored['observedSourceDigest'] != row[V3_RETIREMENT_OBSERVED]) or
                (row.get('retirementReason') == REPLACEMENT_RETIREMENT and
                 stored['observedSourceDigest'] != row['sourceDigest'])):
            _fail('cutover_retirement_mismatch')
        retirement = stored
    else:
        observed = row.get(V3_RETIREMENT_OBSERVED)
        if observed is None:
            # Historical retired ledgers did not pin the observation. Keep
            # their original recovery path without weakening new records.
            if row.get('retirementReason') == REPLACEMENT_RETIREMENT:
                observed = row['sourceDigest']
            else:
                names, _roots = _source_binding(row)
                observed = _qmd_digest(row['sourceDb'], row['sourceFormat'], names, True)
        retirement = _retirement(row, observed)
        _write(record_path, retirement, replace=False)
    if own_marker:
        marker_path.unlink()
        _fsync_dir(marker_path.parent)
    return {**row, 'retirement': retirement}


@_serialized_operation
def _retire_prepared_binding(project_root, journal_path, route_bytes, reason):
    """Withdraw an unselected v3 binding after source or model-proof drift.

    The sealed generation is never rebound. Explicit, idempotent retirement
    makes room for a fresh install identity without deleting source or backup.

    Steps, each durable and each resumed by repeating the call:

    1. Under the ledger lock, CAS ``prepared`` -> ``retired`` (terminal).
       Only after re-proving: the exact reviewed route bytes, no route file
       of this binding, the pending unselected journal with no selected
       write, the project marker (when present), the pinned candidate,
       queue, catalog, binding-store and backup files, the candidate and
       queue incarnation, and the selected reason's exact source evidence.
    2. Write the no-clobber retirement record beside the ledger. It keeps
       the bound and the observed digests and the exact marker bytes.
    3. Withdraw only this binding's project marker. A different marker (a
       later install's) is never touched.

    Nothing is deleted from the state root: the QMD source and its backup,
    the candidate stores, the journal, the ledger and the reviewed route
    stay. QMD stays selected throughout: ``retired`` without a route
    classifies as ``legacy``, and activation, bind and recovery refuse it.
    """
    ledger_path, marker_path, route_path = _paths(project_root, journal_path)
    record_path = ledger_path.parent / RETIREMENT
    row = _ledger(ledger_path)
    if (row['schema'] != SCHEMA_V3 or
            row['projectRoot'] != str(project_root) or
            row['journalPath'] != str(journal_path)):
        _fail('cutover_retire_unsupported')
    if reason == REPLACEMENT_RETIREMENT:
        if 'replacementEvidenceSha256' not in row:
            _fail('cutover_replacement_retire_unsupported')
    elif reason != 'source_drift':
        _fail('cutover_retirement_reason_invalid')
    if row['phase'] == RETIRED:
        observed_reason = row.get('retirementReason')
        if (reason == REPLACEMENT_RETIREMENT and
                observed_reason != REPLACEMENT_RETIREMENT or
                reason == 'source_drift' and observed_reason not in
                (None, SOURCE_IDENTITY_RETIREMENT)):
            _fail('cutover_retirement_reason_mismatch')
    if not isinstance(route_bytes, bytes) or _sha(route_bytes) != row['routeSha256']:
        _fail('cutover_route_mismatch')
    if row['phase'] == RETIRED:
        return _settle_retired_binding(row, record_path, marker_path, route_path)
    if route_path.exists() or route_path.is_symlink():
        _fail('cutover_route_uncertain')
    journal = inspect_journal(row['journalPath'])
    if (journal['state'] != 'candidate_ready' or journal['pending'] is None or
            journal['pendingRouteSha256'] != row['routeSha256'] or journal['newWriteSeen']):
        _fail('cutover_journal_not_ready')
    if (reason == REPLACEMENT_RETIREMENT and
            (journal['projectRoot'] != row['projectRoot'] or
             journal['scopeId'] != row['scope'] or
             journal['providerAttestationSha256'] is not None or
             journal['providerFileIdentitySha256'] is not None)):
        _fail('cutover_journal_not_ready')
    names, _roots = _source_binding(row)
    if row['phase'] == 'prepared':
        if marker_path.exists() or marker_path.is_symlink():
            _check_marker(row, marker_path)
        for name, field in BOUND_FILES:
            # A drift retirement permits QMD to have replaced its own file;
            # a proof retirement requires the same, still-restorable source.
            if ((reason == REPLACEMENT_RETIREMENT or name != 'sourceDb') and
                    _identity(row[name]) != row[field]):
                _fail('cutover_bound_file_replaced')
        with closing(LayaIndexA(row['candidateDb'])) as index:
            if index.incarnation() != row['candidateIncarnation']:
                _fail('cutover_candidate_changed')
        with closing(ClaimQueue.open(row['queueDb'])) as queue:
            if queue.binding() != row['candidateIncarnation']:
                _fail('cutover_queue_binding_mismatch')
        observed_identity = _identity(row['sourceDb'])
        observed = _qmd_digest(row['sourceDb'], row['sourceFormat'], names, True)
        if reason == REPLACEMENT_RETIREMENT:
            if observed != row['sourceDigest']:
                _fail(SOURCE_DRIFT)
            _restore_verified(row['backupDb'], row['sourceFormat'], names,
                              row['sourceDigest'], True)
            from laya_install_journal import _attestation
            attestation, digest = _attestation(row['journalPath'])
            if (digest != row['attestationSha256'] or
                    not isinstance(attestation, dict)):
                _fail('cutover_attestation_changed')
            with closing(LayaIndexA(row['candidateDb'])) as index:
                head = index.db.execute('SELECT gen FROM head WHERE scope=?',
                                        (row['scope'],)).fetchone()
            if head != (row['indexGeneration'],):
                _fail('cutover_candidate_evidence_mismatch')
            try:
                _check_replacement_source(row)
            except CutoverBlocked as exc:
                if str(exc) != 'cutover_replacement_evidence_changed':
                    raise
            else:
                _fail('cutover_replacement_evidence_still_valid')
            row = _change(ledger_path, row['epoch'], 'prepared', RETIRED,
                          {'retirementReason': REPLACEMENT_RETIREMENT,
                           V3_RETIREMENT_OBSERVED: observed})
        else:
            if (observed == row['sourceDigest'] and
                    observed_identity == row['sourceIdentity']):
                # Still fresh: activate it instead; nothing to retire.
                _fail('cutover_source_not_drifted')
            updates = {V3_RETIREMENT_OBSERVED: observed}
            if observed == row['sourceDigest']:
                updates.update(retirementReason=SOURCE_IDENTITY_RETIREMENT,
                               **{V3_RETIREMENT_IDENTITY: observed_identity})
            row = _change(ledger_path, row['epoch'], 'prepared', RETIRED, updates)
    elif row['phase'] != RETIRED:
        _fail('cutover_not_prepared')
    return _settle_retired_binding(row, record_path, marker_path, route_path)


def retire_drifted_binding(project_root, journal_path, route_bytes):
    """Retire an unselected v3 candidate only after its QMD source drifted."""
    return _retire_prepared_binding(project_root, journal_path, route_bytes,
                                    'source_drift')


def retire_unusable_replacement_binding(project_root, journal_path, route_bytes):
    """Retire an unselected v3 candidate whose pinned model proof was lost.

    The selected QMD source must retain its bound digest and the installed
    candidate, backup, attestation, journal and route owner must remain exact.
    No route or selected write may exist. This keeps every byte for a fresh
    reviewed preparation with a new install identity.
    """
    return _retire_prepared_binding(project_root, journal_path, route_bytes,
                                    REPLACEMENT_RETIREMENT)


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
        # ``retired`` keeps its marker only until retirement archives it; like
        # ``prepared`` it never published a route, so QMD is still selected.
        if (row['phase'] in ('prepared', RETIRED) and journal['state'] == 'candidate_ready'
                and journal['pending'] is not None and
                journal['pendingRouteSha256'] == row['routeSha256'] and
                not journal['newWriteSeen']):
            return 'legacy'
        if row['phase'] == 'restored' and journal['state'] == 'restored_qmd':
            return 'legacy'
        return 'blocked'
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error):
        return 'blocked'


@_serialized_operation
def activate_qmd_origin(project_root, journal_path, route_bytes):
    ledger_path, marker_path, route_path = _paths(project_root, journal_path)
    row = _ledger(ledger_path)
    if row['phase'] == RETIRED:
        _fail('cutover_binding_retired')
    _check_marker(row, marker_path)
    if row['phase'] != 'prepared' or _sha(route_bytes) != row['routeSha256']:
        _fail('cutover_not_prepared')
    _preflight_bound_settings(row, route_bytes)
    _check_evidence(row)
    journal = inspect_journal(journal_path)
    if journal['state'] != 'candidate_ready':
        _fail('cutover_journal_not_ready')
    approval = preflight_product_candidate(journal_path)
    if (approval['attestationSha256'] != row['attestationSha256'] or
            approval['providerFileIdentitySha256'] != row['providerFileIdentitySha256'] or
            approval['pendingRouteSha256'] != row['routeSha256']):
        _fail('cutover_attestation_changed')
    # The selected v2 index contains wiki cards only. Seed the separate raw
    # path/hash discovery cursor while QMD is still selected, before any route
    # publication. This sidecar has no source bodies and cannot serve search.
    route = json.loads(route_bytes)
    from laya_index_host_route import SCHEMA_V2 as HOST_ROUTE_V2
    if route.get('schema') == HOST_ROUTE_V2:
        import laya_wiki_discovery
        names, _roots = _source_binding(row)
        inventory = inspect_qmd_collections(row['sourceDb'], row['sourceFormat'], names)
        if inventory['digest'] != row['sourceDigest']:
            _fail('cutover_source_changed')
        raw_names = {item['name'] for item in route['collections']
                     if item['role'] == 'raw'}
        baseline = {path: sha for path, _title, sha, active_doc, _body
                    in inventory['documents']
                    if active_doc and path.split('/', 1)[0] in raw_names}
        laya_wiki_discovery.seed_source_snapshot({
            **route,
            'collectionRoots': {item['name']: item['sourceRoot']
                                for item in route['collections']},
        }, baseline)
    row = _change(ledger_path, row['epoch'], 'prepared', 'switching')
    # The marker no longer classifies as legacy once ``switching``. A QMD
    # write that landed after the proof above makes the bound generation
    # stale: withdraw to ``prepared`` before any route or journal change.
    names, _roots = _source_binding(row)
    multi = row['schema'] != SCHEMA
    changed = _transition_source_error(row, names, multi)
    if changed:
        _change(ledger_path, row['epoch'], 'switching', 'prepared')
        _fail(changed)
    _write(route_path, route_bytes, replace=False)
    # A route under ``switching`` classifies ``blocked``, so no hook served
    # it yet. A QMD write that landed while it was written still withdraws:
    # only the exact route this activation wrote is removed, before any
    # journal commit. After the commit a later QMD write is a source edit
    # the queued selected sync catches up (the product entry reports it).
    changed = _transition_source_error(row, names, multi)
    if changed:
        if route_path.is_symlink() or _private_file(route_path, 8192) != route_bytes:
            _fail('cutover_route_uncertain')
        route_path.unlink()
        _fsync_dir(route_path.parent)
        _change(ledger_path, row['epoch'], 'switching', 'prepared')
        _fail(changed)
    active = cas_activate(journal_path, journal['epoch'], route_bytes)
    if (active['state'] != 'active' or
            active['providerAttestationSha256'] != row['attestationSha256'] or
            active['providerFileIdentitySha256'] != row['providerFileIdentitySha256']):
        _fail('cutover_journal_activation_unconfirmed')
    return _change(ledger_path, row['epoch'], 'switching', 'active')


@_serialized_operation
def restore_qmd_origin(project_root, journal_path):
    ledger_path, marker_path, route_path = _paths(project_root, journal_path)
    row = _ledger(ledger_path)
    _check_marker(row, marker_path)
    journal = inspect_journal(journal_path)
    if (row['phase'] != 'active' or journal['state'] != 'active' or
            journal['newWriteSeen'] or not route_path.is_file() or
            _sha(_private_file(route_path, 8192)) != row['routeSha256']):
        _fail('cutover_rollback_not_authorized')
    _check_evidence(row, restore=True)
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


@_serialized_operation
def recover_qmd_origin(project_root, journal_path, route_bytes):
    """Settle interrupted selection without deleting either source database."""
    ledger_path, marker_path, route_path = _paths(project_root, journal_path)
    row = _ledger(ledger_path)
    if not isinstance(route_bytes, bytes) or _sha(route_bytes) != row['routeSha256']:
        _fail('cutover_route_mismatch')
    if row['phase'] == RETIRED:
        # Never recreate a retired binding's marker; retirement settles it.
        return row
    if not marker_path.exists() and not marker_path.is_symlink():
        _recover_missing_marker(row, marker_path, route_path, route_bytes)
    _check_marker(row, marker_path)
    journal = inspect_journal(journal_path)
    present = route_path.exists() or route_path.is_symlink()
    if row['phase'] == 'switching':
        if journal['state'] == 'candidate_ready':
            if (journal['pending'] is None or
                    journal['pendingRouteSha256'] != row['routeSha256'] or
                    journal['newWriteSeen']):
                _fail('cutover_journal_uncertain')
            if present and (route_path.is_symlink() or
                            _private_file(route_path, 8192) != route_bytes):
                _fail('cutover_route_uncertain')
            # A v3 recovery never completes an activation the journal did not
            # commit: it withdraws to ``prepared`` and the installer repeats
            # the explicit, re-proving activation.
            withdraw = row['schema'] == SCHEMA_V3
            if not withdraw:
                try:
                    _preflight_bound_settings(row, route_bytes)
                    _check_evidence(row)
                    approval = preflight_product_candidate(journal_path)
                    if (approval['pendingRouteSha256'] != row['routeSha256'] or
                            approval['attestationSha256'] != row['attestationSha256'] or
                            approval['providerFileIdentitySha256'] != row['providerFileIdentitySha256']):
                        _fail('cutover_attestation_changed')
                except Exception:
                    withdraw = True
            if withdraw:
                # No journal commit or selected write occurred. Withdraw only
                # the exact route owned by this prepared ledger; preserve both
                # databases and let the installer replan changed evidence.
                latest = inspect_journal(journal_path)
                if (latest['state'] != 'candidate_ready' or
                        latest['epoch'] != journal['epoch'] or
                        latest['pendingRouteSha256'] != row['routeSha256'] or
                        latest['newWriteSeen']):
                    _fail('cutover_journal_uncertain')
                if present:
                    route_path.unlink()
                    _fsync_dir(route_path.parent)
                return _change(ledger_path, row['epoch'], 'switching', 'prepared')
            if not present:
                return _change(ledger_path, row['epoch'], 'switching', 'prepared')
            journal = cas_activate(journal_path, journal['epoch'], route_bytes)
        elif journal['state'] == 'active' and not present:
            if (journal['selectedRouteSha256'] != row['routeSha256'] or
                    journal['providerAttestationSha256'] != row['attestationSha256'] or
                    journal['providerFileIdentitySha256'] != row['providerFileIdentitySha256']):
                _fail('cutover_journal_uncertain')
            _write(route_path, route_bytes, replace=False)
        if (not route_path.is_file() or route_path.is_symlink() or
                _private_file(route_path, 8192) != route_bytes):
            _fail('cutover_route_uncertain')
        if (journal['state'] != 'active' or
                journal['selectedRouteSha256'] != row['routeSha256'] or
                journal['providerAttestationSha256'] != row['attestationSha256'] or
                journal['providerFileIdentitySha256'] != row['providerFileIdentitySha256']):
            _fail('cutover_journal_uncertain')
        return _change(ledger_path, row['epoch'], 'switching', 'active')
    if row['phase'] == 'prepared' and journal['state'] == 'active':
        # A racing journal commit can land after an interrupted switching
        # rollback. The committed route is forward-only, even when its file
        # was withdrawn before the journal transaction became visible.
        if (journal['selectedRouteSha256'] != row['routeSha256'] or
                journal['providerAttestationSha256'] != row['attestationSha256'] or
                journal['providerFileIdentitySha256'] != row['providerFileIdentitySha256']):
            _fail('cutover_journal_uncertain')
        if not present:
            _write(route_path, route_bytes, replace=False)
        elif route_path.is_symlink() or _private_file(route_path, 8192) != route_bytes:
            _fail('cutover_route_uncertain')
        return _change(ledger_path, row['epoch'], 'prepared', 'active')
    if row['phase'] == 'restoring':
        if journal['newWriteSeen']:
            if (journal['state'] != 'active' or
                    journal['selectedRouteSha256'] != row['routeSha256'] or
                    journal['providerAttestationSha256'] != row['attestationSha256'] or
                    journal['providerFileIdentitySha256'] != row['providerFileIdentitySha256']):
                _fail('cutover_journal_uncertain')
            if not present:
                _write(route_path, route_bytes, replace=False)
            elif route_path.is_symlink() or _private_file(route_path, 8192) != route_bytes:
                _fail('cutover_route_uncertain')
            return _change(ledger_path, row['epoch'], 'restoring', 'active')
        _check_evidence(row, restore=True)
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

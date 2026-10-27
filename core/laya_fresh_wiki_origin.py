"""Independent, non-destructive cutover for a newly embedded wiki generation.

This ledger never names a QMD source database or claims provenance for old
vectors. It owns only the private Laya candidate and the exact project route.
The older QMD installation and project settings are left in place.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat

from laya_index_a import LayaIndexA
from laya_index_claim_queue import ClaimQueue
from laya_install_journal import (cas_activate, cas_restore_fresh,
                                  inspect_journal, preflight_product_candidate)
from laya_qmd_cutover import (_fsync_dir, _identity, _ledger_lock,
                              _operation_lock, _preflight_multi_settings,
                              _private_file, _read_json, _write)

SCHEMA = 'laya-fresh-wiki-cutover-v1'
MARKER_SCHEMA = 'laya-fresh-wiki-selection-marker-v1'
LEDGER = 'fresh-wiki-cutover.json'
MARKER = 'laya-index-selection.json'
ROUTE = 'laya-index-route.json'
PHASES = ('prepared', 'switching', 'active', 'upgrading', 'restoring', 'restored', 'retired')
HEX64 = re.compile(r'[0-9a-f]{64}\Z')
HEX32 = re.compile(r'[0-9a-f]{32}\Z')
FILES = (('candidateDb', 'candidateIdentity'), ('queueDb', 'queueIdentity'),
         ('catalogDb', 'catalogIdentity'), ('bindingStore', 'bindingStoreIdentity'))
FIELDS = frozenset(('schema', 'epoch', 'phase', 'projectRoot', 'journalPath',
                    'routeSha256', 'collections', 'candidateDb', 'queueDb',
                    'catalogDb', 'bindingStore', 'scope', 'indexGeneration',
                    'sourceManifest', 'bindingDigest', 'catalogScanId',
                    'catalogRawDigest', 'vectorProfileId', 'vectorRows',
                    'candidateIncarnation', 'attestationSha256',
                    'providerFileIdentitySha256', *(field for _, field in FILES)))


class FreshWikiBlocked(RuntimeError):
    pass


def _fail(reason):
    raise FreshWikiBlocked(reason)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _paths(project_root, journal_path):
    project, journal = Path(project_root), Path(journal_path)
    if (not project.is_absolute() or project.resolve(strict=True) != project or
            not journal.is_absolute() or journal.name != 'install-journal.sqlite' or
            journal.is_symlink() or journal.parent.resolve(strict=True) != journal.parent):
        _fail('fresh_wiki_paths_invalid')
    auto = project / '.auto-context'
    if auto.is_symlink() or not auto.is_dir():
        _fail('fresh_wiki_project_unavailable')
    info = journal.parent.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        _fail('fresh_wiki_owner_unavailable')
    return journal.parent / LEDGER, auto / MARKER, auto / ROUTE


def _load(path):
    row = _read_json(path)
    from laya_index_capacity import generation_digest
    from laya_index_host_route import wiki_index_roots, _canonical_absolute
    if (not isinstance(row, dict) or set(row) != FIELDS or
            row['schema'] != SCHEMA or type(row['epoch']) is not int or
            row['epoch'] < 1 or row['phase'] not in PHASES or
            type(row['vectorRows']) is not int or row['vectorRows'] < 1 or
            any(not isinstance(row[key], str) or not row[key] for key in
                FIELDS - {'epoch', 'phase', 'collections', 'vectorRows',
                          *(field for _, field in FILES)}) or
            any(not HEX64.fullmatch(row[key]) for key in
                ('routeSha256', 'sourceManifest', 'bindingDigest',
                 'catalogRawDigest', 'vectorProfileId', 'attestationSha256',
                 'providerFileIdentitySha256')) or
            any(not HEX32.fullmatch(row[key]) for key in
                ('scope', 'catalogScanId', 'candidateIncarnation')) or
            generation_digest(row['indexGeneration']) != row['bindingDigest'] or
            Path(row['journalPath']).parent / LEDGER != path or
            any(not isinstance(row[field], list) or len(row[field]) != 2 or
                any(type(value) is not int or value < 0 for value in row[field])
                for _, field in FILES)):
        _fail('fresh_wiki_ledger_invalid')
    try:
        wiki = wiki_index_roots(row['collections'])
        raw = next(item['sourceRoot'] for item in row['collections']
                   if item['role'] == 'raw')
        wiki_root = next(iter(wiki.values()))
        if (len({item['name'] for item in row['collections']}) != 2 or
                not _canonical_absolute(raw) or
                raw != row['projectRoot'] or
                wiki_root != str(Path(raw) / '.auto-context' / 'wiki')):
            _fail('fresh_wiki_ledger_invalid')
    except (ValueError, KeyError, TypeError, StopIteration) as exc:
        raise FreshWikiBlocked('fresh_wiki_ledger_invalid') from exc
    if len({row[key] for key, _ in FILES}) != len(FILES):
        _fail('fresh_wiki_ledger_invalid')
    return row


def _marker(row):
    return {'schema': MARKER_SCHEMA, 'projectRoot': row['projectRoot'],
            'journalPath': row['journalPath'],
            'ledgerPath': str(Path(row['journalPath']).parent / LEDGER),
            'routeSha256': row['routeSha256']}


def _check_marker(row, marker_path):
    if _read_json(marker_path) != _marker(row):
        _fail('fresh_wiki_marker_changed')


def _change(path, epoch, phase, next_phase):
    with _ledger_lock(path):
        row = _load(path)
        if row['epoch'] != epoch or row['phase'] != phase:
            _fail('fresh_wiki_stale_cas')
        row = {**row, 'epoch': epoch + 1, 'phase': next_phase}
        _write(path, row, replace=True)
        return _load(path)


def _proof(row, route_bytes, *, restore=False):
    from laya_index_host_route import wiki_index_roots
    from laya_index_runtime import LayaIndexRuntime, capacity_store_paths
    from laya_initial_build import evidence
    from laya_install_journal import _route
    route, digest = _route(route_bytes, Path(row['journalPath']))
    if (digest != row['routeSha256'] or
            route['projectRoot'] != row['projectRoot'] or
            route['collections'] != row['collections'] or
            route['dbPath'] != row['candidateDb'] or
            route['queuePath'] != row['queueDb'] or
            route['scopeId'] != row['scope'] or
            route['dbIncarnation'] != row['candidateIncarnation']):
        _fail('fresh_wiki_route_mismatch')
    if not restore:
        _preflight_multi_settings(route, row['projectRoot'])
    for name, field in FILES:
        if _identity(row[name]) != row[field]:
            _fail('fresh_wiki_store_replaced')
    if capacity_store_paths(row['candidateDb']) != (row['catalogDb'], row['bindingStore']):
        _fail('fresh_wiki_capacity_mismatch')
    if restore:
        with closing(LayaIndexA(row['candidateDb'])) as index:
            if index.incarnation() != row['candidateIncarnation']:
                _fail('fresh_wiki_candidate_changed')
        with closing(ClaimQueue.open(row['queueDb'])) as queue:
            if queue.binding() != row['candidateIncarnation']:
                _fail('fresh_wiki_queue_changed')
        return
    with LayaIndexRuntime(source_root=wiki_index_roots(row['collections']),
                          scope=row['scope'], db_path=row['candidateDb'],
                          queue_path=row['queueDb'],
                          db_incarnation=row['candidateIncarnation'],
                          catalog_path=row['catalogDb'],
                          binding_store_path=row['bindingStore']) as runtime:
        live = evidence(runtime, row['indexGeneration'], row['vectorProfileId'])
    matched = (('generation', 'indexGeneration'),
               ('bindingDigest', 'bindingDigest'),
               ('sourceManifest', 'sourceManifest'),
               ('catalogScanId', 'catalogScanId'),
               ('catalogRawDigest', 'catalogRawDigest'),
               ('vectorProfileId', 'vectorProfileId'),
               ('vectorRows', 'vectorRows'),
               ('dbIncarnation', 'candidateIncarnation'))
    if live is None or any(live[left] != row[right] for left, right in matched):
        _fail('fresh_wiki_candidate_changed')
    approved = preflight_product_candidate(row['journalPath'])
    if (approved['pendingRouteSha256'] != row['routeSha256'] or
            approved['attestationSha256'] != row['attestationSha256'] or
            approved['providerFileIdentitySha256'] != row['providerFileIdentitySha256']):
        _fail('fresh_wiki_attestation_changed')


def bind(*, project_root, journal_path, route_bytes, sealed, identity):
    """Bind an already sealed, fresh and inactive private candidate."""
    ledger, marker, route = _paths(project_root, journal_path)
    with _operation_lock(marker):
        if (route.exists() or route.is_symlink() or
                (Path(journal_path).parent / 'qmd-cutover.json').exists()):
            _fail('fresh_wiki_selection_already_exists')
        journal = inspect_journal(journal_path)
        if (journal['state'] != 'candidate_ready' or journal['newWriteSeen'] or
                journal['pendingRouteSha256'] != _sha(route_bytes)):
            _fail('fresh_wiki_journal_not_ready')
        approved = preflight_product_candidate(journal_path)
        row = {'schema': SCHEMA, 'epoch': 1, 'phase': 'prepared',
               'projectRoot': str(project_root), 'journalPath': str(journal_path),
               'routeSha256': _sha(route_bytes),
               'collections': identity['collections'],
               'candidateDb': identity['candidateDb'],
               'queueDb': identity['queueDb'],
               'catalogDb': identity['catalogDb'],
               'bindingStore': identity['bindingStore'],
               'scope': identity['scopeId'],
               'indexGeneration': sealed['generation'],
               'sourceManifest': sealed['sourceManifest'],
               'bindingDigest': sealed['bindingDigest'],
               'catalogScanId': sealed['catalogScanId'],
               'catalogRawDigest': sealed['catalogRawDigest'],
               'vectorProfileId': sealed['vectorProfileId'],
               'vectorRows': sealed['vectorRows'],
               'candidateIncarnation': sealed['dbIncarnation'],
               'attestationSha256': approved['attestationSha256'],
               'providerFileIdentitySha256': approved['providerFileIdentitySha256'],
               **{field: _identity(identity[name]) for name, field in FILES}}
        if (identity['routeSha256'] != row['routeSha256'] or
                identity['attestationSha256'] != row['attestationSha256'] or
                identity['vectorProfileId'] != row['vectorProfileId'] or
                identity['dbIncarnation'] != row['candidateIncarnation']):
            _fail('fresh_wiki_build_identity_changed')
        _proof(row, route_bytes)
        if ledger.exists() or ledger.is_symlink():
            existing = _load(ledger)
            if existing != row:
                _fail('fresh_wiki_binding_changed')
        else:
            _write(ledger, row, replace=False)
        if marker.exists() or marker.is_symlink():
            _check_marker(row, marker)
        else:
            _write(marker, _marker(row), replace=False)
        return _load(ledger)


def classify_marker(project_root, marker_path, route_path):
    """Read-only fail-closed selection classification for this ledger."""
    try:
        marker = _read_json(marker_path)
        if (not isinstance(marker, dict) or marker.get('schema') != MARKER_SCHEMA or
                set(marker) != {'schema', 'projectRoot', 'journalPath',
                                'ledgerPath', 'routeSha256'} or
                marker['projectRoot'] != str(project_root)):
            return 'blocked'
        ledger, expected_marker, expected_route = _paths(project_root,
                                                           marker['journalPath'])
        if (str(ledger) != marker['ledgerPath'] or
                expected_marker != Path(marker_path) or
                expected_route != Path(route_path)):
            return 'blocked'
        row = _load(ledger)
        _check_marker(row, marker_path)
        journal = inspect_journal(row['journalPath'])
        present = route_path.exists() or route_path.is_symlink()
        if present:
            if route_path.is_symlink() or _sha(_private_file(route_path, 8192)) != row['routeSha256']:
                return 'blocked'
            if (row['phase'] == 'active' and journal['state'] in ('active', 'restored_old') and
                    journal['selectedRouteSha256'] == row['routeSha256'] and
                    journal['providerAttestationSha256'] == row['attestationSha256'] and
                    journal['providerFileIdentitySha256'] == row['providerFileIdentitySha256']):
                return 'candidate'
            return 'blocked'
        if (row['phase'] in ('prepared', 'retired') and
                journal['state'] == 'candidate_ready' and
                journal['pendingRouteSha256'] == row['routeSha256'] and
                not journal['newWriteSeen']):
            return 'legacy'
        if row['phase'] == 'restored' and journal['state'] == 'restored_qmd':
            return 'legacy'
        return 'blocked'
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error):
        return 'blocked'


def activate(project_root, journal_path, route_bytes):
    ledger, marker, route = _paths(project_root, journal_path)
    with _operation_lock(marker):
        row = _load(ledger)
        if row['phase'] == 'retired':
            _fail('fresh_wiki_binding_retired')
        _check_marker(row, marker)
        if row['phase'] != 'prepared' or _sha(route_bytes) != row['routeSha256']:
            _fail('fresh_wiki_not_prepared')
        _proof(row, route_bytes)
        if inspect_journal(journal_path)['state'] != 'candidate_ready':
            _fail('fresh_wiki_journal_not_ready')
        import laya_wiki_discovery
        decision = {**route_decision(route_bytes),
                    'collectionRoots': {item['name']: item['sourceRoot']
                                        for item in row['collections']}}
        raw = {item['name'] for item in row['collections'] if item['role'] == 'raw'}
        baseline = laya_wiki_discovery._raw_snapshot(decision, raw)
        laya_wiki_discovery.seed_source_snapshot(decision, baseline)
        row = _change(ledger, row['epoch'], 'prepared', 'switching')
        _write(route, route_bytes, replace=False)
        journal = inspect_journal(journal_path)
        committed = cas_activate(journal_path, journal['epoch'], route_bytes)
        if (committed['state'] != 'active' or
                committed['selectedRouteSha256'] != row['routeSha256'] or
                committed['providerAttestationSha256'] != row['attestationSha256'] or
                committed['providerFileIdentitySha256'] != row['providerFileIdentitySha256']):
            _fail('fresh_wiki_activation_unconfirmed')
        return _change(ledger, row['epoch'], 'switching', 'active')


def route_decision(route_bytes):
    import json
    return json.loads(route_bytes.decode('utf-8'))


def recover(project_root, journal_path, route_bytes):
    ledger, marker, route = _paths(project_root, journal_path)
    with _operation_lock(marker):
        row = _load(ledger)
        if row['phase'] == 'retired':
            return row
        _check_marker(row, marker)
        if _sha(route_bytes) != row['routeSha256']:
            _fail('fresh_wiki_route_mismatch')
        journal = inspect_journal(journal_path)
        present = route.exists() or route.is_symlink()
        if present and (route.is_symlink() or _private_file(route, 8192) != route_bytes):
            _fail('fresh_wiki_route_uncertain')
        if row['phase'] == 'switching':
            if journal['state'] == 'candidate_ready' and not journal['newWriteSeen']:
                if present:
                    route.unlink()
                    _fsync_dir(route.parent)
                return _change(ledger, row['epoch'], 'switching', 'prepared')
            if (journal['state'] == 'active' and
                    journal['selectedRouteSha256'] == row['routeSha256'] and
                    journal['providerAttestationSha256'] == row['attestationSha256'] and
                    journal['providerFileIdentitySha256'] == row['providerFileIdentitySha256']):
                if not present:
                    _write(route, route_bytes, replace=False)
                return _change(ledger, row['epoch'], 'switching', 'active')
            _fail('fresh_wiki_activation_uncertain')
        if row['phase'] == 'restoring':
            if journal['state'] == 'active' and journal['newWriteSeen']:
                if not present:
                    _write(route, route_bytes, replace=False)
                return _change(ledger, row['epoch'], 'restoring', 'active')
            if journal['state'] == 'active' and not journal['newWriteSeen']:
                if present:
                    route.unlink()
                    _fsync_dir(route.parent)
                journal = cas_restore_fresh(journal_path, journal['epoch'],
                                            row['routeSha256'])
            if journal['state'] == 'restored_qmd' and not (route.exists() or route.is_symlink()):
                return _change(ledger, row['epoch'], 'restoring', 'restored')
            _fail('fresh_wiki_restore_uncertain')
        if row['phase'] == 'active' and journal['state'] in ('active', 'restored_old'):
            if (journal['selectedRouteSha256'] != row['routeSha256'] or
                    journal['providerAttestationSha256'] != row['attestationSha256'] or
                    journal['providerFileIdentitySha256'] != row['providerFileIdentitySha256']):
                _fail('fresh_wiki_activation_uncertain')
            if not present:
                _write(route, route_bytes, replace=False)
            return row
        return row


def restore(project_root, journal_path):
    ledger, marker, route = _paths(project_root, journal_path)
    with _operation_lock(marker):
        row = _load(ledger)
        _check_marker(row, marker)
        if row['phase'] != 'active' or route.is_symlink() or not route.is_file():
            _fail('fresh_wiki_rollback_not_authorized')
        route_bytes = _private_file(route, 8192)
        if _sha(route_bytes) != row['routeSha256']:
            _fail('fresh_wiki_route_changed')
        journal = inspect_journal(journal_path)
        if journal['state'] != 'active' or journal['previous'] is not None or journal['newWriteSeen']:
            _fail('fresh_wiki_rollback_forward_only')
        _proof(row, route_bytes, restore=True)
        row = _change(ledger, row['epoch'], 'active', 'restoring')
        journal = inspect_journal(journal_path)
        if journal['newWriteSeen']:
            _change(ledger, row['epoch'], 'restoring', 'active')
            _fail('fresh_wiki_rollback_forward_only')
        route.unlink()
        _fsync_dir(route.parent)
        cas_restore_fresh(journal_path, journal['epoch'], row['routeSha256'])
        return _change(ledger, row['epoch'], 'restoring', 'restored')


def retire(project_root, journal_path):
    """Release an unselected stale binding; keep its stores and journal."""
    ledger, marker, route = _paths(project_root, journal_path)
    with _operation_lock(marker):
        row = _load(ledger)
        if (row['phase'] not in ('prepared', 'retired') or
                route.exists() or route.is_symlink()):
            _fail('fresh_wiki_retirement_not_authorized')
        journal = inspect_journal(journal_path)
        if (journal['state'] != 'candidate_ready' or journal['newWriteSeen'] or
                journal['pendingRouteSha256'] != row['routeSha256']):
            _fail('fresh_wiki_retirement_not_authorized')
        if marker.exists() or marker.is_symlink():
            _check_marker(row, marker)
        if row['phase'] == 'prepared':
            row = _change(ledger, row['epoch'], 'prepared', 'retired')
        if marker.exists() or marker.is_symlink():
            _check_marker(row, marker)
            marker.unlink()
            _fsync_dir(marker.parent)
        return row

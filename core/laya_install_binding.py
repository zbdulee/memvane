"""Exact installer-active/project-route CAS receipt *contract*, no activation.

A future installer owns the active journal and must CAS its selection/route
digest/receipt under its own writer lock. The project route JSON is a separate
file, so the installer must publish and recover it with fail-closed hash
comparison; there is no cross-file atomic transaction. This read-only value
checker is not a CAS implementation and cannot authenticate a journal.
The current source-only bundle always returns productRouteAllowed=False.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from laya_index_host_route import FIELDS as ROUTE_FIELDS, SCHEMA as ROUTE_SCHEMA
from laya_package import _user_qmd_inventory


ACTIVE_SCHEMA = 'laya-installer-active-v1'
CAS_SCHEMA = 'laya-project-route-cas-v1'
IDENTITY_FIELDS = ('installId', 'generation', 'bundleManifestSha256',
                   'ownershipManifestSha256', 'pluginRoot')
SHA256 = re.compile(r'[0-9a-f]{64}\Z')
ID = re.compile(r'[0-9a-f]{32}\Z')
GENERATION = re.compile(r'g-[0-9a-f]{32}\Z')


class BindingEvidenceError(ValueError):
    pass


def _fail(reason):
    raise BindingEvidenceError(reason)


def route_digest(route_raw):
    """Digest the exact bytes seen by the host selector, including whitespace."""
    if not isinstance(route_raw, bytes) or len(route_raw) > 8192:
        _fail('invalid_route_bytes')
    return hashlib.sha256(route_raw).hexdigest()


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail('duplicate_route_key')
        result[key] = value
    return result


def _nonfinite(_value):
    _fail('nonfinite_route_number')


def validate_binding(installer_active, project_route, route_raw, cas_receipt, artifact,
                     *, host_project_root, user_qmd_roots):
    """Check equality of four identifiers and one route CAS transition.

    Inputs must come from independently authenticated installer and project
    sources. The producer must atomically persist the selection, route digest
    and CAS receipt in one installer-owned journal transaction; the route JSON
    file is verified separately and cannot share that transaction.
    No caller should route traffic based solely on this function's result.
    """
    if not all(isinstance(v, dict) for v in
               (installer_active, project_route, cas_receipt, artifact)):
        _fail('invalid_binding_record')
    if not isinstance(route_raw, bytes) or len(route_raw) > 8192:
        _fail('invalid_route_bytes')
    try:
        parsed_route = json.loads(route_raw.decode('utf-8'), object_pairs_hook=_unique_pairs,
                                  parse_constant=_nonfinite)
    except (AttributeError, UnicodeError, json.JSONDecodeError) as exc:
        raise BindingEvidenceError('invalid_route_bytes') from exc
    if parsed_route != project_route or not isinstance(parsed_route, dict) or set(parsed_route) != ROUTE_FIELDS:
        _fail('route_bytes_changed')
    route_sha = route_digest(route_raw)
    active = installer_active.get('selected')
    receipted = cas_receipt.get('selected')
    if not all(isinstance(v, dict) for v in (active, receipted)):
        _fail('missing_selected_identity')
    try:
        root = Path(host_project_root)
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            _fail('host_project_root_mismatch')
        canonical_root = root.resolve(strict=True)
        root_info = canonical_root.stat()
        inventory = _user_qmd_inventory(Path(artifact.get('pluginRoot')), user_qmd_roots)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        if isinstance(exc, BindingEvidenceError):
            raise
        raise BindingEvidenceError('host_or_inventory_unavailable') from exc
    if inventory != artifact.get('userQmdInventory'):
        _fail('user_qmd_inventory_mismatch')
    if (installer_active.get('schema') != ACTIVE_SCHEMA or
            project_route.get('schema') != ROUTE_SCHEMA or
            cas_receipt.get('schema') != CAS_SCHEMA or
            installer_active.get('state') != 'active' or
            project_route.get('status') != 'active' or
            artifact.get('status') != 'active_receipt_consistent' or
            type(project_route.get('projectRoot')) is not str or
            project_route['projectRoot'] != str(canonical_root) or
            not isinstance(project_route.get('scopeId'), str) or
            not re.fullmatch(r'[0-9a-f]{32}', project_route['scopeId']) or
            project_route['scopeId'] != cas_receipt.get('scopeId')):
        _fail('binding_identity_mismatch')
    for field in IDENTITY_FIELDS:
        value = artifact.get(field)
        if value is None or active.get(field) != value or project_route.get(field) != value or receipted.get(field) != value:
            _fail('binding_identity_mismatch')
    if any(type(active[field]) is not str or not active[field] for field in IDENTITY_FIELDS):
        _fail('binding_identity_mismatch')
    try:
        canonical_plugin = Path(active['pluginRoot'])
        canonical_plugin_ok = (canonical_plugin.is_absolute() and
                               active['pluginRoot'] == str(canonical_plugin.resolve(strict=False)))
    except (OSError, RuntimeError, ValueError) as exc:
        raise BindingEvidenceError('binding_identity_mismatch') from exc
    if (not ID.fullmatch(active['installId']) or
            not GENERATION.fullmatch(active['generation']) or
            not canonical_plugin_ok):
        _fail('binding_identity_mismatch')
    if any(not SHA256.fullmatch(active[field]) for field in
           ('bundleManifestSha256', 'ownershipManifestSha256')):
        _fail('binding_identity_mismatch')
    selection_epoch = installer_active.get('selectionEpoch')
    bindings = installer_active.get('routeBindings')
    scope = project_route['scopeId']
    if not isinstance(bindings, dict) or not isinstance(bindings.get(scope), dict):
        _fail('binding_cas_receipt_mismatch')
    route_epoch = bindings[scope].get('routeEpoch')
    if (type(selection_epoch) is not int or selection_epoch < 1 or
            type(route_epoch) is not int or route_epoch < 1 or
            bindings[scope].get('routeSha256') != route_sha or
            bindings[scope].get('projectRoot') != str(canonical_root) or
            type(bindings[scope].get('projectDev')) is not int or
            type(bindings[scope].get('projectIno')) is not int or
            bindings[scope]['projectDev'] != root_info.st_dev or
            bindings[scope]['projectIno'] != root_info.st_ino or
            cas_receipt.get('projectRoot') != str(canonical_root) or
            type(cas_receipt.get('projectDev')) is not int or
            type(cas_receipt.get('projectIno')) is not int or
            cas_receipt['projectDev'] != root_info.st_dev or
            cas_receipt['projectIno'] != root_info.st_ino or
            type(cas_receipt.get('selectionEpoch')) is not int or
            cas_receipt['selectionEpoch'] != selection_epoch or
            type(cas_receipt.get('expectedRouteEpoch')) is not int or
            cas_receipt['expectedRouteEpoch'] != route_epoch - 1 or
            type(cas_receipt.get('newRouteEpoch')) is not int or
            cas_receipt['newRouteEpoch'] != route_epoch or
            cas_receipt.get('newRouteSha256') != route_sha):
        _fail('binding_cas_receipt_mismatch')
    return {'status': 'binding_values_consistent_no_atomicity_proof',
            'scopeId': scope, 'installId': active['installId'],
            'projectRoot': str(canonical_root),
            'selectionEpoch': selection_epoch, 'routeEpoch': route_epoch,
            'productRouteAllowed': False,
            'reason': 'source_bundle_incomplete_and_journal_cas_unproved'}

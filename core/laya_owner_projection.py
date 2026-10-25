"""Read-only Laya owner projection for an edit outside the selected project.

Discovery is only the existing shared-store owner registry (change_owner,
B1/B2a): approvedProjects plus collections.json, re-proved against the
owner's current settings, OFF/optout, role, symlinks and path ambiguity.
There is no second list, HOME scan, ancestor route search or enrollment, and
an unregistered path is never registered here.

A registry owner is a Laya owner only if its own installed state agrees:

- its route record (no-follow read) binds the same settings digest that the
  shared store approved and that the current settings file hashes to;
- the route's collection of that name has the registered source root;
- select_route still classifies it as a candidate for that exact root, which
  re-checks settings, optout and route shape;
- its install journal proves the selected bundle, project identity (the
  journal binds the root's device/inode, so another worktree of the same
  repository is another owner), DB/queue incarnation and product approval,
  and this process executes that same sealed bundle.

A missing path (deletion, the old side of a move) is routed only by a prior
accepted observation (B2a), never by the directory that exists now. Accepted
present targets are recorded as that evidence after the wake.

Every refusal is a fixed reason string; no target path, project path or file
body is part of it. Accepted and refused events take different code paths:
nothing here claims they are indistinguishable by latency.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat

# The registry may also name a QMD (non-Laya) owner; this module never
# routes to its queue. Reasons from change_owner pass through unchanged.
_PROJECTION_KEYS = ('projectRoot', 'scopeId', 'dbIncarnation', 'installId',
                    'generation')
_DURABLE_WAKE_STATES = frozenset(('queued', 'busy',
                                  'deferred_queue_capacity_retained'))


def _route_settings_digest(root: str) -> str | None:
    from laya_index_host_route import MAX_RECORD_BYTES, RECORD, _pairs, _read_regular
    raw = _read_regular(Path(root) / '.auto-context' / RECORD, MAX_RECORD_BYTES)
    record = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs)
    value = record.get('settingsSha256') if isinstance(record, dict) else None
    return value if isinstance(value, str) else None


def _root_identity(root: str) -> list[int]:
    info = os.lstat(root)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError('owner_root_changed')
    return [info.st_dev, info.st_ino]


def _collection_root(decision: dict, name: str):
    roots = decision.get('collectionRoots')
    if isinstance(roots, dict):
        return roots.get(name)
    return decision.get('sourceRoot') if decision.get('collectionName') == name else None


def project_owner(raw, base, *, snapshot=None, home=None):
    """({projection, decision, proof, registryOwner}, None) or (None, reason)."""
    import change_owner
    import laya_install_journal
    from laya_index_host_route import select_route
    owner, reason = change_owner.resolve_change(raw, base, snapshot=snapshot, home=home)
    if reason:
        return None, reason
    root = owner['projectRoot']
    decision = select_route(root, action='index')
    if decision.get('state') == 'legacy':
        return None, 'not_laya_owner'
    if decision.get('state') != 'candidate' or decision.get('projectRoot') != root:
        return None, 'owner_route_blocked'
    try:
        digest = _route_settings_digest(root)
        identity = _root_identity(root)
    except (OSError, ValueError, TypeError, UnicodeError):
        return None, 'owner_route_unreadable'
    if digest != owner['settingsSha256']:
        return None, 'stale_approval'
    if _collection_root(decision, owner['collection']) != owner['path']:
        return None, 'collection_root_mismatch'
    try:
        proof = laya_install_journal.verify_selected_route(decision)
    except Exception:
        return None, 'owner_journal_unverified'
    if not isinstance(proof, dict) or proof.get('productRouteAllowed') is not True:
        return None, 'owner_product_unavailable'
    if any(proof.get(key) != decision.get(key) for key in _PROJECTION_KEYS):
        return None, 'owner_journal_mismatch'
    loaded_root = str(Path(__file__).resolve().parent.parent)
    if proof.get('executingPluginRoot', decision.get('pluginRoot')) != loaded_root:
        return None, 'owner_bundle_not_executing'
    projection = {key: decision[key] for key in _PROJECTION_KEYS}
    projection.update(projectIdentity=identity, journalPath=decision['journalPath'],
                      collection=owner['collection'], sourceRoot=owner['path'],
                      settingsSha256=digest, registryRevision=owner['revision'],
                      change=owner['change'], targetIdentity=list(owner['identity']))
    return {'projection': projection, 'decision': decision, 'proof': proof,
            'registryOwner': owner}, None


def _stable(proof: dict) -> dict:
    # The owner's worker may advance the journal epoch while we run.
    return {key: value for key, value in proof.items() if key != 'selectionEpoch'}


def _same_owner(first: dict, again: dict) -> bool:
    return (first['projection'] == again['projection']
            and first['decision'] == again['decision']
            and _stable(first['proof']) == _stable(again['proof'])
            and again['proof'].get('selectionEpoch', -1)
            >= first['proof'].get('selectionEpoch', 0))


def _wake(root: str, accepted: list, base, home) -> str | None:
    """Re-prove, record one durable owner-specific wake, re-prove; reason or None."""
    import change_owner
    import laya_install_journal
    import runtime_work
    from laya_index_host_route import select_route
    for raw, first in accepted:
        again, reason = project_owner(raw, base, home=home)
        if reason or not _same_owner(first, again):
            return 'owner_changed'
    first = accepted[0][1]
    try:
        result = runtime_work.request('laya-sync', root)
    except Exception:
        result = None
    try:
        decision = select_route(root, action='index')
        proof = laya_install_journal.verify_selected_route(decision)
    except Exception:
        return 'owner_proof_changed'
    if (decision != first['decision'] or not isinstance(proof, dict)
            or _stable(proof) != _stable(first['proof'])
            or proof.get('selectionEpoch', -1) < first['proof'].get('selectionEpoch', 0)):
        # The wake is durable and its worker re-proves this owner itself.
        return 'owner_proof_changed'
    if not isinstance(result, dict) or result.get('status') not in _DURABLE_WAKE_STATES:
        return 'owner_sync_unavailable'
    present = [owner['registryOwner'] for _raw, owner in accepted
               if owner['registryOwner']['change'] == 'present']
    if present:
        try:
            change_owner.record_observations(present, home=home)
        except Exception:
            pass  # Missing evidence only makes a later delete fail closed.
    return None


def route_external(paths, base, *, selected_root, home=None):
    """Wake each proved Laya owner once; ([owner roots], [(raw, reason)]).

    ``selected_root`` is the hook's own project. A path that projects to it
    is refused here: the caller's existing selected path owns that edit.
    """
    import change_owner
    snapshot, reason = change_owner.registry_snapshot(home)
    if reason:
        return [], [(raw, reason) for raw in paths]
    groups, refused = {}, []
    for raw in paths:
        owner, reason = project_owner(raw, base, snapshot=snapshot, home=home)
        if not reason and owner['projection']['projectRoot'] == selected_root:
            reason = 'owner_is_selected_project'
        if reason:
            refused.append((raw, reason))
        else:
            groups.setdefault(owner['projection']['projectRoot'], []).append((raw, owner))
    woken = []
    for root, accepted in sorted(groups.items()):
        reason = _wake(root, accepted, base, home)
        if reason:
            refused.extend((raw, reason) for raw, _owner in accepted)
        else:
            woken.append(root)
    return woken, refused

"""Read-only import of the pre-release ownership ledger. The existing card/queue locks serialize imports."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re

from memvane.kernel.atomic_rename import canonical_json_bytes
from memvane.store.api import regular
from memvane.store.types import MAX_BODY
from memvane.wiki.verify import handoff_work as work
from memvane.wiki.verify.pending_replacement import job_revisions, pending_matches_job
from memvane.wiki.workspace import types as workspace_types

NAME = '.pending-ownership.json'
OLD_SWAP = re.compile(r'\.verify-swap-[a-z0-9_]{8}\Z')
NEW_SWAP = re.compile(r'\.verify-swap-[a-f0-9]{32}\Z')
PENDING = re.compile(r'\..+\.pending-[0-9a-f]{16}\Z')
LIMIT = 8 * 1024 * 1024


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _relative(value):
    if not isinstance(value, str) or not value or '\x00' in value:
        return False
    path = Path(value)
    return not path.is_absolute() and '..' not in path.parts and path.as_posix() == value


def _pending(rel):
    return _relative(rel) and PENDING.fullmatch(Path(rel).name)


def _key(job):
    from memvane.wiki.verify.wiki_verify_worker import _cleanup_job_key
    return _cleanup_job_key(job)


def operation(plan):
    return 'retirement_' + _sha((_key(plan['job']) + plan['sha256'] + plan['swap']).encode())


def _valid_plan(rel, plan, owners, swaps):
    if not isinstance(plan, dict) or set(plan) != {'job', 'sha256', 'reason', 'swap'}:
        return False
    job = plan['job']
    if (not isinstance(job, dict) or not _relative(job.get('targetPath')) or
            job.get('pendingPath') != rel or owners.get(rel) != plan['sha256'] or
            plan['reason'] not in ('pending_source_changed', 'pending_replacement_superseded') or
            not isinstance(plan['swap'], str) or not any(pattern.fullmatch(plan['swap']) for pattern in swaps)):
        return False
    target, marker = Path(job['targetPath']), job.get('sourceHash')
    sha = job.get('expectedTargetHash')
    return (bool(target.name) and isinstance(marker, str) and re.fullmatch('[0-9a-f]{16}', marker)
            and not target.name.startswith('.') and isinstance(sha, str) and work.SHA.fullmatch(sha)
            and target.with_name('.' + target.name + '.pending-' + marker).as_posix() == rel
            and job_revisions(job))


def _valid_state(state):
    if not isinstance(state, dict) or set(state) != {'owners', 'retirements'}:
        return False
    owners, retired = state['owners'], state['retirements']
    return (isinstance(owners, dict) and isinstance(retired, dict)
            and all(_pending(k) and isinstance(v, str) and work.SHA.fullmatch(v) for k, v in owners.items())
            and all(_valid_plan(k, v, owners, (OLD_SWAP, NEW_SWAP)) for k, v in retired.items()))


def _name(queue):
    return 'ownership-' + _sha(os.fsencode(queue.name))[:24]


def _empty():
    return {'version': 1, 'state': {'owners': {}, 'retirements': {}}, 'imports': {}, 'reserved': []}


def _index(queue):
    try:
        with work.private_root(queue) as fd:
            data = work._file(fd, _name(queue), work.CONTROL_LIMIT)
    except FileNotFoundError:
        return _empty()
    index = json.loads(data, object_pairs_hook=work._unique)
    if not isinstance(index, dict):
        raise ValueError('invalid_ownership_index')
    checksum = index.pop('checksum', None)
    if (checksum != _sha(canonical_json_bytes(index)) or set(index) != set(_empty())
            or type(index['version']) is not int or index['version'] != 1 or not _valid_state(index['state'])
            or not isinstance(index['reserved'], list) or not all(_pending(r) for r in index['reserved'])
            or not isinstance(index['imports'], dict) or not all(isinstance(k, str) and work.SHA.fullmatch(k)
            and isinstance(v, str) and work.HEX_ID.fullmatch(v) for k, v in index['imports'].items())):
        raise ValueError('invalid_ownership_index')
    return index


def _save(queue, index):
    raw = canonical_json_bytes(dict(index, checksum=_sha(canonical_json_bytes(index))))
    if len(raw) > work.CONTROL_LIMIT:
        raise ValueError('ownership_index_limit')
    with work.private_root(queue, create=True) as fd:
        work._publish(fd, _name(queue), raw, replace=True)


def _public_bytes(root, rel, limit):
    raw, _, _ = regular(root, rel, limit)
    return raw


def _legacy(queue):
    try:
        return _public_bytes(queue.parent, NAME, LIMIT)
    except FileNotFoundError:
        return None


def _names(raw):
    # Even a partial/unknown document can name an old pending file unambiguously.
    text = raw.decode('utf-8', 'surrogateescape')
    offset = 0
    names = set()
    while (start := text.find('"', offset)) >= 0:
        try:
            value, offset = json.decoder.scanstring(text, start + 1)
        except ValueError:
            offset = start + 1
            continue
        if _pending(value) and not any(0xd800 <= ord(ch) <= 0xdfff for ch in value):
            names.add(value)
    return names


def _rows(data):
    if data and not data.endswith(b'\n'):
        raise ValueError('incomplete_legacy_evidence')
    rows = []
    for line in data.split(b'\n')[:-1]:
        if len(rows) >= 4096 or len(line) >= work.SLAB:
            raise ValueError('legacy_evidence_limit')
        row = json.loads(line.decode('utf8'), object_pairs_hook=work._unique)
        if not isinstance(row, dict):
            raise ValueError('invalid_legacy_evidence')
        rows.append(row)
    return rows


def _artifact(parent, name, limit, budget=None):
    try:
        raw = _public_bytes(parent, name, min(limit, budget[0]) if budget is not None else limit)
        if budget is not None:
            budget[0] -= len(raw)
        return {'status': 'present', 'sha256': _sha(raw), 'length': len(raw)}, raw
    except FileNotFoundError:
        return {'status': 'absent'}, None
    except (OSError, ValueError):
        return {'status': 'unreadable'}, None


def _audit_index(queue, budget=None):
    info, raw = _artifact(queue.parent, workspace_types.VERIFY_DELETED, LIMIT, budget)
    if raw is None:
        return info, {}
    by_id = {}
    try:
        for row in _rows(raw):
            reasons = row.get('reasons')
            if (not isinstance(reasons, list) or not all(isinstance(reason, str) for reason in reasons)
                    or not isinstance(row.get('targetPath'), str) or not isinstance(row.get('verdict'), str)):
                raise ValueError('unknown_legacy_audit')
            for reason in reasons:
                if reason.startswith('retirement_'):
                    by_id.setdefault(reason, []).append(row)
    except (ValueError, RecursionError):
        info['status'] = 'unreadable'
    return info, by_id


def _exact_audit(rows, rel, plan, op):
    return (len(rows) == 1 and rows[0].get('targetPath') == rel and rows[0].get('verdict') == 'stale_replacement'
            and rows[0].get('producedBy') == plan['job'].get('engine', '')
            and rows[0].get('sourcePaths') == [row['path'] for row in job_revisions(plan['job'])]
            and rows[0].get('reasons') == [plan['reason'], op])


def _inspect(queue, raw):
    proof = {'reserved': sorted(_names(raw)), 'records': {}, 'status': 'unknown'}
    imported = {'owners': {}, 'retirements': {}}
    try:
        source = json.loads(raw.decode(), object_pairs_hook=work._unique)
    except (ValueError, RecursionError):
        return proof, imported
    if (not isinstance(source, dict) or set(source) != set(imported)
            or not all(isinstance(v, dict) for v in source.values())):
        return proof, imported
    proof['status'] = 'recognized'
    cards, artifacts = [24 * 1024 ** 2], [LIMIT]
    audit, rows = _audit_index(queue, artifacts) if source['retirements'] else ({'status': 'unused'}, {})
    proof['audit'] = audit
    root = queue.parent.parent.parent
    for rel in proof['reserved']:
        digest = source['owners'].get(rel)
        if not isinstance(digest, str) or not work.SHA.fullmatch(digest):
            continue
        candidate, data = _artifact(root, rel, MAX_BODY, cards)
        detail = proof['records'][rel] = {'candidate': candidate, 'result': 'unproved'}
        matching = data is not None and candidate['sha256'] == digest
        if rel not in source['retirements']:
            if matching:
                imported['owners'][rel] = digest
                detail['result'] = 'owner'
            continue
        plan = source['retirements'][rel]
        if not _valid_plan(rel, plan, source['owners'], (OLD_SWAP,)):
            continue
        try:
            op = operation(plan)
        except (ValueError, RecursionError):
            continue
        detail['operation'] = op
        swap, _ = _artifact(queue.parent, plan['swap'], LIMIT, artifacts)
        detail['swap'] = swap
        audits = rows.get(op, [])
        if audits:
            if _exact_audit(audits, rel, plan, op) and audit['status'] != 'unreadable':
                detail['result'] = 'past-audit' if candidate['status'] == 'absent' else 'prior-attempt'
            continue
        if matching and swap['status'] == 'absent' and audit['status'] != 'unreadable':
            try:
                matches = pending_matches_job(data.decode(), plan['job'])
            except (ValueError, RecursionError):
                matches = False
            if matches:
                detail['result'] = 'requires-freshness-check'
                imported['owners'][rel] = digest
                imported['retirements'][rel] = plan
    return proof, imported


def current_retirement(queue, rel, plan):
    try:
        raw = _legacy(queue)
        if raw is None or _sha(raw) not in _index(queue)['imports']:
            return False
        source = json.loads(raw.decode(), object_pairs_hook=work._unique)
        if source['retirements'].get(rel) != plan or source['owners'].get(rel) != plan['sha256']:
            return False
        audit, rows = _audit_index(queue)
        op = operation(plan)
        return (audit['status'] != 'unreadable' and (not rows.get(op) or _exact_audit(rows[op], rel, plan, op))
                and not os.path.lexists(queue.with_name(plan['swap'])))
    except (OSError, TypeError, ValueError, KeyError, RecursionError):
        return False


def has_state(queue):
    if os.path.lexists(queue.with_name(NAME)):
        return True
    try:
        with work.private_root(queue) as fd:
            os.stat(_name(queue), dir_fd=fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def reserved(queue, rel, *, fresh=False):
    index = _index(queue)
    if rel in index['reserved'] or rel in index['state']['retirements']:
        return True
    try:
        raw = _legacy(queue)
    except (OSError, ValueError):
        return not fresh
    if raw is None:
        return False
    if rel in _names(raw):
        return True
    if fresh:
        return False
    try:
        source = json.loads(raw.decode(), object_pairs_hook=work._unique)
    except (ValueError, RecursionError):
        return True
    return (not isinstance(source, dict) or set(source) != {'owners', 'retirements'}
            or not all(isinstance(value, dict) for value in source.values()))


def _forget_old_authority(index, proved=None):
    state = index['state']
    for rel in index['reserved']:
        plan = state['retirements'].get(rel)
        if plan and NEW_SWAP.fullmatch(plan['swap']):
            continue
        if (proved is not None and proved['owners'].get(rel) == state['owners'].get(rel)
                and proved['retirements'].get(rel) == plan):
            continue
        state['owners'].pop(rel, None)
        state['retirements'].pop(rel, None)


def _receipt(queue, digest, identity=None):
    # The immutable pointer also distinguishes an absent mutable index from a first import.
    name = _name(queue) + '-' + digest
    try:
        with work.private_root(queue, create=identity is not None) as fd:
            try:
                previous = work._file(fd, name, 32).decode('ascii')
            except FileNotFoundError:
                if identity is None:
                    return None
                work._publish(fd, name, identity.encode('ascii'))
                return identity
            if not work.HEX_ID.fullmatch(previous) or identity is not None and identity != previous:
                raise ValueError('conflicting_legacy_receipt')
            return previous
    except FileNotFoundError:
        if identity is not None:
            raise
        return None


def load(queue):
    index = _index(queue)
    try:
        raw = _legacy(queue)
    except (OSError, ValueError):
        _forget_old_authority(index)
        return index['state']
    if raw is None:
        _forget_old_authority(index)
        return index['state']
    digest = _sha(raw)
    binding = {'queue': queue.name, 'digest': digest}
    previous = index['imports'].get(digest)
    identity = _receipt(queue, digest)
    if previous and previous != identity:
        raise ValueError('missing_legacy_import_receipt')
    if identity:
        item = work.Work.lookup(queue, 'legacy', identity)
        if (item is None or item.state['binding'] != binding or item.state.get('sourceDigest') != digest
                or item.read('raw') != raw):
            raise ValueError('missing_legacy_import_evidence')
        if previous:
            if item.state['phase'] == 'imported':
                with work.private_root(queue) as fd:
                    os.fsync(fd)
                item.commit('finished')
            if not item._closed:
                item.archive()
            return index['state']
        if item.state['phase'] == 'finished':
            # A lost index cannot authorize a second activation of superseded owners.
            return index['state']
    else:
        entries = work.Work.entries(queue, 'legacy')
        matching = [item for item in entries if item.state['binding'] == binding]
        if len(matching) > 1 or entries.issues:
            _forget_old_authority(index)
            return index['state']
        item = matching[0] if matching else work.Work.create(queue, 'legacy', binding, raw)
    if item.read('raw') != raw:
        raise ValueError('legacy_import_bytes_changed')
    if item.state['phase'] == 'reserved':
        proof, imported = _inspect(queue, raw)
        # An oversized control record stays reserved, with both original copies intact.
        if len(canonical_json_bytes([proof, imported])) + 1024 > work.CONTROL_LIMIT:
            _forget_old_authority(index)
            return index['state']
        item.commit('imported', sourceDigest=digest, proof=proof, imported=imported)
    item = work.Work.lookup(queue, 'legacy', item.state['id'])
    if item is None or item.read('raw') != raw or _legacy(queue) != raw:
        raise ValueError('legacy_import_bytes_changed')
    saved = item.state
    if (saved['phase'] != 'imported' or saved.get('sourceDigest') != digest or saved['binding'] != binding
            or not _valid_state(saved['imported'])):
        raise ValueError('legacy_import_reference_conflict')
    _receipt(queue, digest, item.state['id'])
    proof = saved['proof']
    _forget_old_authority(index, saved['imported'])
    for rel, sha in saved['imported']['owners'].items():
        if rel in index['reserved'] or rel in index['state']['owners'] or rel in index['state']['retirements']:
            continue
        index['state']['owners'][rel] = sha
        if rel in saved['imported']['retirements']:
            index['state']['retirements'][rel] = saved['imported']['retirements'][rel]
    index['reserved'] = sorted(set(index['reserved']) | set(proof['reserved']))
    index['imports'][digest] = item.state['id']
    _save(queue, index)
    item.commit('finished')
    item.archive()
    return index['state']


def store(queue, state):
    if not _valid_state(state):
        raise ValueError('invalid_pending_ownership')
    index = _index(queue)
    for rel in index['reserved']:
        if rel in state['owners'] and state['owners'][rel] != index['state']['owners'].get(rel):
            raise ValueError('legacy_pending_name_reserved')
    index['state'] = state
    _save(queue, index)

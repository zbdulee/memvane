"""Account for additional recovery copies under the existing project card/verify queue locks."""
from __future__ import annotations

import errno
import hashlib
import json
import os

from memvane.kernel.atomic_rename import canonical_json_bytes
from memvane.wiki.verify import handoff_work as store

LIMIT = 64 * 1024 ** 2
INDEX = 'copy-quota'


def charge(kind, binding, blobs):
    # A newly created card's initial control, or the source inode, is its primary authority.
    if (kind == 'legacy' or binding.get('action') in ('create', 'borrow') or
            blobs['raw'].get('storage') == 'inode'):
        return 0
    return sum(blob['length'] for blob in blobs.values())


def _write(root, used, pending, *, charge_budget=True):
    content = dict(version=1, used=used, pending=pending)
    data = dict(content, checksum=hashlib.sha256(canonical_json_bytes(content)).hexdigest())
    store._publish(root, INDEX, canonical_json_bytes(data),
                   replace=store._exists(root, INDEX), charge_budget=charge_budget)


def _reconcile(root, used, change, *, charge_budget=True):
    if change is None:
        return
    identity, delta = change
    if not store.HEX_ID.fullmatch(identity) or type(delta) is not int or not 0 < abs(delta) <= LIMIT:
        raise OSError('invalid_copy_reservation')
    name = 'allocated-' + identity
    value = str(abs(delta)).encode('ascii')
    if store._exists(root, name):
        if store._file(root, name, 12, charge_budget=charge_budget) != value:
            raise OSError('changed_copy_reservation')
        if delta < 0:
            os.unlink(name, dir_fd=root)
            os.fsync(root)
    elif delta > 0:
        store._publish(root, name, value, charge_budget=charge_budget)
    _write(root, used, None, charge_budget=charge_budget)


def _used(root, *, charge_budget=True):
    if not store._exists(root, INDEX):
        return 0
    state = json.loads(store._file(root, INDEX, 512, charge_budget=charge_budget))
    checksum = state.pop('checksum')
    if (hashlib.sha256(canonical_json_bytes(state)).hexdigest() != checksum or
            set(state) != {'version', 'used', 'pending'} or state['version'] != 1 or
            type(state['used']) is not int or not 0 <= state['used'] <= LIMIT):
        raise OSError('invalid_copy_quota')
    _reconcile(root, state['used'], state['pending'], charge_budget=charge_budget)
    return state['used']


def allocate(root, identity, amount):
    if not amount:
        return
    used = _used(root)
    if used + amount > LIMIT:
        raise OSError(errno.ENOSPC, 'handoff_copy_quota')
    if store._exists(root, 'allocated-' + identity):
        raise FileExistsError('handoff_id_collision')
    change = [identity, amount]
    _write(root, used + amount, change)
    _reconcile(root, used + amount, change)


def release(root, identity, *, charge_budget=True):
    used = _used(root, charge_budget=charge_budget)
    name = 'allocated-' + identity
    if not store._exists(root, name):
        return
    amount = int(store._file(root, name, 12, charge_budget=charge_budget))
    if not 0 < amount <= used:
        raise OSError('invalid_copy_allocation')
    change = [identity, -amount]
    _write(root, used - amount, change, charge_budget=charge_budget)
    _reconcile(root, used - amount, change, charge_budget=charge_budget)

"""Per-installation embedding owner contract, served by the per-user service.

Each selected or pending installation keeps its sealed provider socket, its
owner lease (``.resident.lock``) and its owner state record. The per-user
``laya_resident_service`` process adopts the installation: it holds that
lease, binds that socket and answers it from one shared embedding worker of
the same provider identity. A query hook never loads a model; a sync or
query only asks the service to adopt and reports ``preparing``.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
from pathlib import Path
import socket
import stat
import tempfile
import time

from laya_index_a import IndexContractError
from laya_local_embed_service import _private_parent
from resource_deferred import ResourceDeferred


_IDENTITY = ('approvedProviderId', 'providerArtifactSha256',
             'embeddingFingerprint')
_REASONS = frozenset(('starting', 'ready', 'resource_deferred',
                      'startup_failed', 'worker_exited', 'identity_invalid',
                      'attestation_invalid', 'unexpected'))
_MAX_FAILURES = 3
_FAILURE_COOLDOWN_SECONDS = 300
_TRANSIENT_FAILURES = frozenset(('startup_failed', 'worker_exited'))


def _state_path(socket_path):
    return socket_path + '.resident.state'


def read_owner_state(manifest):
    """Return only fixed, same-fingerprint diagnostics from private state."""
    path = _state_path(manifest['serviceSocketPath'])
    _private_parent(manifest['serviceSocketPath'])
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > 512):
            raise IndexContractError('provider_owner_state_invalid')
        raw = os.read(fd, 513)
    finally:
        os.close(fd)
    try:
        row = json.loads(raw.decode('utf-8'))
    except (UnicodeError, ValueError) as exc:
        raise IndexContractError('provider_owner_state_invalid') from exc
    if (not isinstance(row, dict) or set(row) !=
            {'fingerprint', 'reason', 'terminal', 'at', 'attempts'}
            or not isinstance(row['fingerprint'], str)
            or not isinstance(row['reason'], str) or row['reason'] not in _REASONS
            or type(row['terminal']) is not bool
            or type(row['at']) not in (int, float) or not math.isfinite(row['at'])
            or type(row['attempts']) is not int or not 0 <= row['attempts'] <= _MAX_FAILURES):
        raise IndexContractError('provider_owner_state_invalid')
    return row if row['fingerprint'] == manifest['embeddingFingerprint'] else None


def _write_state(manifest, reason, *, terminal=False, attempts=0):
    path = _state_path(manifest['serviceSocketPath'])
    _private_parent(manifest['serviceSocketPath'])
    try:
        old = os.lstat(path)
    except FileNotFoundError:
        old = None
    if old is not None and (not stat.S_ISREG(old.st_mode) or old.st_uid != os.getuid()
                            or old.st_nlink != 1 or old.st_mode & 0o077):
        raise IndexContractError('provider_owner_state_invalid')
    row = {'fingerprint': manifest['embeddingFingerprint'], 'reason': reason,
           'terminal': terminal, 'at': time.time(), 'attempts': attempts}
    fd, temporary = tempfile.mkstemp(prefix='.laya-resident-state-',
                                     dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as target:
            json.dump(row, target, separators=(',', ':'))
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        directory = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def owner_lock_held(manifest):
    """Check the exact private owner lease without creating or stealing it."""
    path = manifest['serviceSocketPath'] + '.resident.lock'
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        info = os.fstat(fd)
        named = os.lstat(path)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077
                or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)):
            raise IndexContractError('provider_owner_lock_invalid')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


def _failure_decision(manifest, *, own_lock=False):
    row = read_owner_state(manifest)
    if row is None:
        return
    if (row['reason'] in _TRANSIENT_FAILURES
            and (row['terminal'] or row['attempts'] >= _MAX_FAILURES)):
        # Older services persisted transient exhaustion as terminal. Keep one
        # fixed cooldown, including across restarts, without renewing it on
        # each prompt. A wall-clock rollback must not make it indefinite.
        elapsed = time.time() - row['at']
        if 0 <= elapsed < _FAILURE_COOLDOWN_SECONDS:
            raise ResourceDeferred('laya_service_backoff')
        return
    if row['terminal']:
        raise IndexContractError('provider_start_failed')
    delay = (5 if row['reason'] in ('resource_deferred', 'starting') else
             min(300, 5 * 2 ** max(0, row['attempts'] - 1)))
    if time.time() - row['at'] < delay:
        raise ResourceDeferred('laya_service_resource_deferred' if
                               row['reason'] == 'resource_deferred' else
                               'laya_service_backoff')
    if row['attempts'] >= _MAX_FAILURES:
        # Also bounds abrupt owner death: SIGKILL cannot write an exit record,
        # but the prior start/ready record survives without its socket.
        if not own_lock and owner_lock_held(manifest):
            raise ResourceDeferred('laya_service_backoff')
        _write_state(manifest, 'worker_exited', terminal=True,
                     attempts=_MAX_FAILURES)
        raise ResourceDeferred('laya_service_backoff')


def _record_failure(manifest, reason, *, terminal=False, count=True):
    prior = read_owner_state(manifest)
    if reason == 'resource_deferred':
        attempts = 0
    elif prior is not None and prior['reason'] in ('starting', 'ready'):
        attempts = prior['attempts']  # launch was counted before the child ran
    else:
        attempts = min(_MAX_FAILURES, (prior['attempts'] if prior else 0) + int(count))
    _write_state(manifest, reason, terminal=terminal or attempts >= _MAX_FAILURES,
                 attempts=attempts)


def probe(manifest):
    """Read the existing socket's identity without running an embedding."""
    path = manifest['serviceSocketPath']
    _private_parent(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return 'absent'
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise IndexContractError('provider_socket_invalid')
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(.25)
            conn.connect(path)
            if os.lstat(path).st_ino != info.st_ino:
                raise IndexContractError('provider_socket_changed')
            conn.sendall(b'{"kind":"status"}\n')
            conn.shutdown(socket.SHUT_WR)
            answer = bytearray()
            while True:
                part = conn.recv(512)
                if not part:
                    break
                answer.extend(part)
                if len(answer) > 512:
                    raise IndexContractError('provider_status_invalid')
    except (ConnectionRefusedError, FileNotFoundError):
        return 'absent'
    except (OSError, TimeoutError) as exc:
        raise ResourceDeferred('laya_service_busy') from exc
    try:
        row = json.loads(answer.decode('utf-8'))
    except (UnicodeError, ValueError) as exc:
        raise IndexContractError('provider_status_invalid') from exc
    if not isinstance(row, dict) or row.get('status') != 'ready':
        raise IndexContractError('provider_status_invalid')
    return ('ready' if all(row.get(key) == manifest[key] for key in _IDENTITY)
            else 'mismatch')


class AttachedService:
    """A borrowed service is never stopped by a sync/build client."""

    def stop(self):
        pass


def ensure_running(project, manifest, *, service_root=None, spawn=None):
    """Attach to the exact warm socket or ask the per-user service to adopt.

    Call only after the selected or pending preview route and its installer
    proof have been checked. Never waits for model loading inside a hook or a
    provider activity lease; the service adopts off-hook and the durable sync
    wake or the next prompt retries this operation.
    """
    status = probe(manifest)
    if status == 'ready':
        # An older finite build worker may own this same exact socket during
        # a bundle upgrade. It will close on return, so never mistake it for
        # the independent owner promised to preview queries between wakes.
        if not owner_lock_held(manifest):
            raise ResourceDeferred('laya_service_preparing')
        return AttachedService()
    if status == 'mismatch':
        raise ResourceDeferred('laya_service_identity_transition')
    _failure_decision(manifest)
    from laya_resident_service import request_adopt
    request_adopt(str(project), root=service_root, spawn=spawn)
    raise ResourceDeferred('laya_service_preparing')


def admit_query(*, coordinator=None, report=None):
    """Admit one new query using the existing runtime pressure policy.

    The loaded model stays alive on refusal. A hook cannot wait out pressure
    beyond its two-second provider budget, so the caller reports deferral and
    the next prompt can retry; no prompt is persisted in a background queue.
    """
    from context_learning.resource_coordination import Coordinator
    from context_learning.shared import resource_report
    from laya_product_sync_worker import EMBED_POLICY

    try:
        # The learning lane root may be mid-move (a sentinel where the old
        # tree was): any failure to reach it is a deferral, never a refusal.
        controller = coordinator or Coordinator()
        observed = (report if report is not None else
                    resource_report(EMBED_POLICY, detailed=True,
                                    deadline=time.monotonic() + 1))
        verdict = controller.check(EMBED_POLICY, observed)
    except Exception as exc:
        raise ResourceDeferred('laya_query_resource_deferred') from exc
    if verdict.get('ready') is not True:
        raise ResourceDeferred('laya_query_resource_deferred')


def wait_for_release(socket_path, timeout=3):
    """Observe the exact installation socket after a settled QMD restore.

    Only the owner may stop or unlink it. An unverified or still-live socket
    remains visible to the caller as pending cleanup, never force-removed.
    """
    deadline = time.monotonic() + timeout
    lock_path = str(socket_path) + '.resident.lock'
    while True:
        try:
            info = os.lstat(socket_path)
        except FileNotFoundError:
            info = None
        if info is not None and (not stat.S_ISSOCK(info.st_mode)
                                 or info.st_uid != os.getuid()
                                 or info.st_mode & 0o077):
            return False
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
        except FileNotFoundError:
            locked = False
        except OSError:
            return False
        else:
            try:
                lock = os.fstat(fd)
                named = os.lstat(lock_path)
                if (not stat.S_ISREG(lock.st_mode) or lock.st_uid != os.getuid()
                        or lock.st_nlink != 1 or lock.st_mode & 0o077
                        or (lock.st_dev, lock.st_ino) != (named.st_dev, named.st_ino)):
                    return False
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    locked = True
                else:
                    locked = False
            finally:
                os.close(fd)
        if info is None and not locked:
            # Both signals must agree. An owner loading its model owns the
            # lock before its socket exists, so socket absence alone is not
            # completion evidence.
            if not os.path.lexists(socket_path):
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(.05)


def _companion_selected(project):
    """(decision, proof) of a v5 install bound to another host copy, or None.

    Two host caches of one release can each hold installs: the upgrade worker
    binds an install to whichever host ran it first, and the other host's copy
    is then sealed as its companion. The per-user service runs from one of
    them. The v5 writer plan pins the journal's own plugin root, so a service
    on the other copy could never prove those installs and never adopted
    them. The service only embeds and opens no index store for writing. It
    takes the same queue owner and v5 lifetime and physical windows as the
    owner-copy proof (the plan captured for the journal-bound root) and,
    inside them, the full selected proof the hooks take: the journal-bound
    owner copy and this executing copy proven as the same sealed bundle
    (``_verified_executing_root``). The worker still starts from the attested
    provider paths. None when the install is bound to this copy, is not a
    selected v5 install, or the journal says otherwise; writers keep the
    exact-root plan.
    """
    import laya_install_journal as journal
    from laya_index_host_route import select_route
    from laya_product_sync_worker import _selected
    executing = str(journal._executing_root())
    with journal.read_only_journals():
        decision = select_route(str(project), action='update')
        if (decision.get('state') != 'candidate' or decision.get('projectRoot') != str(project)
                or type(decision.get('journalPath')) is not str
                or type(decision.get('pluginRoot')) is not str
                or decision.get('pluginRoot') == executing):
            return None
        try:
            version = journal.journal_version(decision['journalPath'])
        except journal.JournalEvidenceError:
            return None  # the owned-route path reports its own journal state
        if version != 5:
            return None
    # The same queue owner and v5 lifetime and physical windows as a writer's
    # proof, captured for the journal-bound root; the proof inside them is
    # the companion-accepting full selected proof.
    decision, proof = _selected(project, companion_of=decision['pluginRoot'])
    if (proof.get('productRouteAllowed') is not True
            or proof.get('executingPluginRoot') != executing
            or proof.get('pluginRoot') != decision.get('pluginRoot')
            or not isinstance(proof.get('providerManifest'), dict)):
        raise IndexContractError('selected_provider_unavailable')
    return decision, proof


def _owned_route(project):
    """Prove one selected or pending installation using its own route contract."""
    from laya_product_sync_worker import _selected
    companion = _companion_selected(project)
    if companion is not None:
        return companion[0], companion[1], 'selected'
    try:
        decision, proof = _selected(project)
        return decision, proof, 'selected'
    except IndexContractError as exc:
        if str(exc) != 'selected_route_unavailable':
            raise
    from laya_index_host_route import select_route, verify_preview_route
    decision = select_route(str(project), action='recall')
    if (decision.get('state') != 'preview'
            or decision.get('projectRoot') != str(project)):
        raise IndexContractError('provider_owner_route_unavailable')
    proof = verify_preview_route(decision)
    if not isinstance(proof.get('providerManifest'), dict):
        raise IndexContractError('provider_owner_identity_invalid')
    return decision, proof, 'preview'


def _owner_identity(decision, proof):
    """Fields unchanged when this same pending installation is selected."""
    keys = ('projectRoot', 'pluginRoot', 'installId', 'journalPath', 'scopeId',
            'dbPath', 'dbIncarnation', 'bundleManifestSha256',
            'ownershipManifestSha256', 'vectorProfile')
    if any(decision.get(key) is None for key in keys):
        raise IndexContractError('provider_owner_identity_invalid')
    return tuple(decision[key] for key in keys) + (proof['providerManifest'],)

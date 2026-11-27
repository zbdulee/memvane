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
import socket
import stat
import tempfile
import time

from memvane.kernel.errors import IndexContractError
from memvane.provider.api import private_parent as _private_parent
from memvane.provider.types import attestation_reads
from memvane.kernel.resource_deferred import ResourceDeferred


_IDENTITY = ('approvedProviderId', 'providerArtifactSha256',
             'embeddingFingerprint')
_REASONS = frozenset(('starting', 'ready', 'resource_deferred',
                      'startup_failed', 'worker_exited', 'identity_invalid',
                      'attestation_invalid', 'unexpected'))
_MAX_FAILURES = 3
_FAILURE_COOLDOWN_SECONDS = 300
_ATTESTATION_RETRY_SECONDS = 3600
from memvane.resident.types import TRANSIENT_FAILURES as _TRANSIENT_FAILURES


def _state_path(socket_path):
    return socket_path + '.resident.state'


def read_owner_state(manifest):
    """Return only fixed, same-fingerprint diagnostics from private state."""
    row, _ = _read_record(manifest)
    return row


def _read_record(manifest, *, path=None):
    if path is None:
        path = _state_path(manifest['serviceSocketPath'])
    _private_parent(manifest['serviceSocketPath'])
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, None
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
    return (row, info) if row['fingerprint'] == manifest['embeddingFingerprint'] else (None, None)


def _proof_record_path(path, info):
    # Bound to the published file, including when a 1.x writer replaces it at the same wall time.
    return f'{path}.proof-{info.st_ino:x}-{info.st_mtime_ns:x}'


def _write_state(manifest, reason, *, terminal=False, attempts=0, proof_attempts=None):
    from memvane.kernel.atomic_rename import canonical_json_bytes, fsync_dir, write_new_file
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
    fd, temporary = tempfile.mkstemp(prefix='.laya-resident-state-', dir=os.path.dirname(path))
    checkpoint = None
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as target:
            json.dump(row, target, separators=(',', ':'))
            target.flush()
            os.fsync(target.fileno())
            if proof_attempts is not None:
                new_proof = _proof_record_path(path, os.fstat(target.fileno()))
                # The proof must be durable before the rename publishes both to adoption.
                write_new_file(new_proof, canonical_json_bytes(dict(row, attempts=proof_attempts)))
                checkpoint = new_proof
        os.replace(temporary, path)
        checkpoint = None  # Its lifetime now follows the published diagnostic.
        fsync_dir(os.path.dirname(path))
        if old is not None:
            _discard_proof_record(_proof_record_path(path, old))
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)
        if checkpoint is not None:
            _discard_proof_record(checkpoint)


def _discard_proof_record(path):
    try:
        os.unlink(path)
    except OSError:
        pass  # An unreferenced checkpoint cannot charge any other diagnostic.


def _install_state(manifest):
    row, info = _read_record(manifest)
    if not row or row['reason'] not in ('startup_failed', 'resource_deferred') or not row['attempts']:
        return row
    path = _state_path(manifest['serviceSocketPath'])
    proof, _ = _read_record(manifest, path=_proof_record_path(path, info))
    current = os.lstat(path)
    if not os.path.samestat(current, info) or current.st_mtime_ns != info.st_mtime_ns:
        raise ResourceDeferred('laya_service_backoff')  # The pair changed during this read.
    if proof:
        if row['reason'] == 'startup_failed':
            if (not 0 <= proof['attempts'] < _MAX_FAILURES
                    or proof != dict(row, attempts=proof['attempts'])):
                raise IndexContractError('provider_owner_state_invalid')
            # Only the shared worker has a retry deadline for a projected launch failure;
            # keep this installation's prior proof-attempt count without charging launches.
            return proof if proof['attempts'] else None
        if proof != dict(row, attempts=row['attempts']):
            raise IndexContractError('provider_owner_state_invalid')
        return proof
    if row['reason'] == 'resource_deferred':
        # Old admission diagnostics could include another installation's process failures.
        return dict(row, attempts=0)
    return row


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


def _failure_window_remaining(row):
    """Seconds until this terminal or exhausted record permits another attempt."""
    if row['reason'] in _TRANSIENT_FAILURES:
        cooldown = _FAILURE_COOLDOWN_SECONDS
    elif row['reason'] == 'attestation_invalid':
        cooldown = _ATTESTATION_RETRY_SECONDS
    else:
        return math.inf
    elapsed = time.time() - row['at']
    return cooldown - elapsed if 0 <= elapsed < cooldown else 0


def _exhausted(row):
    return row['terminal'] or (row['reason'] in _TRANSIENT_FAILURES and row['attempts'] >= _MAX_FAILURES)


def _record_shared_wait(manifest, reason, terminal, failures):
    prior = _install_state(manifest)
    if prior and not terminal and prior['reason'] in ('starting', 'ready') and prior['attempts'] >= _MAX_FAILURES:
        return  # Only the installing owner can distinguish an interrupted last start from a live one.
    if prior and _exhausted(prior):
        if _failure_window_remaining(prior) and (prior['terminal'] or not terminal):
            return  # A neighbour cannot renew or shorten this proof deadline.
        prior = None
    attempts = prior['attempts'] if prior else 0
    if reason == 'startup_failed':
        # Project the shared launch failure onto this installation while keeping
        # its prior local proof-attempt count in the linked proof record.
        _write_state(manifest, reason, attempts=max(attempts, failures),
                     proof_attempts=min(attempts, _MAX_FAILURES - 1))
    elif reason == 'resource_deferred' and attempts:
        _write_state(manifest, reason, attempts=attempts, proof_attempts=attempts)
    else:
        _write_state(manifest, reason, terminal=terminal, attempts=attempts)


def _process_delay(reason, failures):
    if failures >= _MAX_FAILURES:
        return _FAILURE_COOLDOWN_SECONDS
    return 5 if reason == 'worker_exited' else 5 * 2 ** failures


def _expire_process_retry(worker, manifest):
    # Commit expiry before admitting a load. A later clock correction cannot revive this budget.
    _write_state(dict(manifest, serviceSocketPath=worker.socket_path), 'starting')
    worker.failures, worker.state, worker.retry_at = 0, 'starting', 0


def _restore_process_retry(worker, manifest):
    # Only this socket is shared by the full worker key. Installation rows have ambiguous provenance.
    state = dict(manifest, serviceSocketPath=worker.socket_path)
    try:
        row = read_owner_state(state)
    except IndexContractError as exc:
        if str(exc) != 'provider_owner_state_invalid':
            raise
        return  # No usable process history. The separate installation backoffs still apply.
    if not row or row['reason'] not in _TRANSIENT_FAILURES or not row['attempts']:
        return
    failures = row['attempts']
    elapsed = time.time() - row['at']
    delay = _process_delay(row['reason'], failures)
    if elapsed < 0 or (failures >= _MAX_FAILURES and elapsed >= delay):
        _expire_process_retry(worker, manifest)
        return
    remaining = max(0, delay - elapsed)
    worker.failures = failures
    worker.state = 'cooldown' if failures >= _MAX_FAILURES else 'failed'
    worker.retry_at = time.monotonic() + remaining


def _record_process_outcome(worker, manifest, reason):
    if reason == 'ready':
        worker.failures, worker.state, worker.retry_at = 0, 'ready', 0
    else:
        worker.failures = min(_MAX_FAILURES, worker.failures + 1)
        worker.state = 'cooldown' if worker.failures >= _MAX_FAILURES else 'failed'
        worker.retry_at = time.monotonic() + _process_delay(reason, worker.failures)
    try:
        _write_state(dict(manifest, serviceSocketPath=worker.socket_path), reason, attempts=worker.failures)
    except (OSError, ValueError):
        # A failed checkpoint leaves the last atomic record. Keep enforcing the live process budget.
        pass


def _adoption_delay(manifest):
    """Recheck this installation at expiry or after five minutes if its proof changes."""
    try:
        row = _install_state(manifest)
    except (OSError, ValueError):
        return 5
    if row and _exhausted(row):
        return min(_FAILURE_COOLDOWN_SECONDS, _failure_window_remaining(row))
    return 5


def _failure_decision(manifest, *, own_lock=False, installation=False):
    row = _install_state(manifest) if installation else read_owner_state(manifest)
    if row is None:
        return row
    if (row['reason'] in _TRANSIENT_FAILURES
            and (row['terminal'] or row['attempts'] >= _MAX_FAILURES)):
        # Older services persisted transient exhaustion as terminal. Keep one
        # fixed cooldown, including across restarts, without renewing it on
        # each prompt. A wall-clock rollback must not make it indefinite.
        if _failure_window_remaining(row):
            raise ResourceDeferred('laya_service_backoff')
        return
    if row['terminal']:
        if not _failure_window_remaining(row):
            return  # A fresh full proof is due, also after a clock rollback.
        raise IndexContractError('provider_start_failed')
    delay = (5 if row['reason'] in ('resource_deferred', 'starting') else
             min(300, 5 * 2 ** max(0, row['attempts'] - 1)))
    if 0 <= time.time() - row['at'] < delay:
        raise ResourceDeferred('laya_service_resource_deferred' if
                               row['reason'] == 'resource_deferred' else
                               'laya_service_backoff')
    if row['reason'] == 'resource_deferred' and row['attempts'] >= _MAX_FAILURES:
        return  # Old propagated admission waits do not prove a process started or exited.
    if row['attempts'] >= _MAX_FAILURES:
        # Also bounds abrupt owner death: SIGKILL cannot write an exit record,
        # but the prior start/ready record survives without its socket.
        if not own_lock and owner_lock_held(manifest):
            raise ResourceDeferred('laya_service_backoff')
        _write_state(manifest, 'worker_exited', terminal=True,
                     attempts=_MAX_FAILURES)
        raise ResourceDeferred('laya_service_backoff')
    return row




def _error_code(exc):
    code = str(exc)
    return code if code.replace('_', '').isalnum() and code.islower() and len(code) <= 80 else type(exc).__name__


def _retry_view(remaining):
    """``retryInSeconds`` (None when no wait applies) and ``retryNever`` for an unbounded wait."""
    if math.isinf(remaining):
        return {'retryInSeconds': None, 'retryNever': True}
    return {'retryInSeconds': round(remaining, 3) if remaining > 0 else None, 'retryNever': False}


def _record_view(row):
    return {key: row[key] for key in ('reason', 'terminal', 'at', 'attempts')}


def _inspect_installation(manifest):
    try:
        row, info = _read_record(manifest)
        if row is None:
            return {'status': 'absent'}
        view = {'status': 'present', **_record_view(row), 'proofAttempts': None}
        path = _state_path(manifest['serviceSocketPath'])
        proof, _ = _read_record(manifest, path=_proof_record_path(path, info))
        if proof is not None:
            view['proofAttempts'] = proof['attempts']
        effective = _install_state(manifest)
    except FileNotFoundError:
        return {'status': 'absent'}
    except (OSError, ValueError, IndexContractError, ResourceDeferred) as exc:
        return {'status': 'unreadable', 'error': _error_code(exc)}
    if effective is None:
        return dict(view, effectiveAttempts=0, exhausted=False, **_retry_view(0))
    exhausted = _exhausted(effective)
    remaining = _failure_window_remaining(effective) if exhausted else 0
    return dict(view, effectiveAttempts=effective['attempts'], exhausted=exhausted, **_retry_view(remaining))


def _inspect_shared_worker(manifest):
    try:
        from memvane.runtime.api import service_root
        from memvane.resident.resident_service import _worker_key, _worker_socket
        socket_path = _worker_socket(service_root(create=False), _worker_key(manifest))
    except (OSError, ValueError, KeyError) as exc:
        # A service root not created yet reads as private_state's ValueError; ServiceFailure has a code.
        missing = isinstance(exc, FileNotFoundError) or (isinstance(exc, ValueError)
                                                         and getattr(exc, 'code', None) is None)
        return {'status': 'absent'} if missing else {'status': 'unreadable', 'error': _error_code(exc)}
    try:
        row = read_owner_state(dict(manifest, serviceSocketPath=socket_path))
    except FileNotFoundError:
        return {'status': 'absent', 'socket': os.path.basename(socket_path)}
    except (OSError, ValueError, IndexContractError) as exc:
        return {'status': 'unreadable', 'socket': os.path.basename(socket_path), 'error': _error_code(exc)}
    if row is None:
        return {'status': 'absent', 'socket': os.path.basename(socket_path)}
    view = {'status': 'present', 'socket': os.path.basename(socket_path), **_record_view(row)}
    remaining = 0
    if row['reason'] in _TRANSIENT_FAILURES and row['attempts']:
        elapsed = time.time() - row['at']
        delay = _process_delay(row['reason'], row['attempts'])
        # The resident expires a future-dated or elapsed cooldown on restart.
        remaining = 0 if elapsed < 0 else max(0, delay - elapsed)
    return dict(view, **_retry_view(remaining))


def inspect_owner_state(manifest):
    """A read-only view of one installation's embedding owner; never writes, locks or raises.

    ``socket`` is ``probe``'s answer (``ready``, ``mismatch``, ``absent``) or ``error``
    with its code. ``installation`` is the installation's own record (``.resident.state``
    and its linked ``.proof-*`` record) with the seconds left before another proof
    (``_ATTESTATION_RETRY_SECONDS`` after an invalid attestation). ``sharedWorker`` is the
    shared worker's process record beside ``w-<24 hex>.sock`` with its retry delay.
    Unlike ``_failure_decision`` it never records an exhausted start nor tests the owner lock.
    """
    try:
        socket_view = {'status': probe(manifest)}
    except FileNotFoundError:
        socket_view = {'status': 'absent'}
    except (OSError, ValueError, KeyError, IndexContractError, ResourceDeferred) as exc:
        socket_view = {'status': 'error', 'error': _error_code(exc)}
    return {'socket': socket_view, 'installation': _inspect_installation(manifest),
            'sharedWorker': _inspect_shared_worker(manifest)}


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
    from memvane.resident.resident_service import request_adopt
    request_adopt(str(project), root=service_root, spawn=spawn)
    raise ResourceDeferred('laya_service_preparing')


def admit_query(*, coordinator=None, report=None):
    """Admit one new query using the existing runtime pressure policy.

    The loaded model stays alive on refusal. A hook cannot wait out pressure
    beyond its two-second provider budget, so the caller reports deferral and
    the next prompt can retry; no prompt is persisted in a background queue.
    A test runtime root's fixture provider port admits instead.
    """
    from memvane.provider.api import fixture_port
    port = fixture_port()
    if port is not None:
        return port.admit_query(coordinator=coordinator, report=report)
    from memvane.runtime.api import coordinator as Coordinator
    from memvane.runtime.api import resource_report
    from memvane.resident.types import EMBED_POLICY

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


def _lock_held(lock_path):
    """True/False for a private lock file's holder, None when it is not ours."""
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    except OSError:
        return None
    try:
        lock = os.fstat(fd)
        named = os.lstat(lock_path)
        if (not stat.S_ISREG(lock.st_mode) or lock.st_uid != os.getuid()
                or lock.st_nlink != 1 or lock.st_mode & 0o077
                or (lock.st_dev, lock.st_ino) != (named.st_dev, named.st_ino)):
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


def _refuses_connections(socket_path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        try:
            probe.connect(str(socket_path))
        except (ConnectionRefusedError, FileNotFoundError):
            return True
        except OSError:
            return False
    return False


def wait_for_release(socket_path, timeout=3):
    """Observe the exact installation socket after a settled QMD restore.

    Only the owner may stop or unlink it. An unverified or still-live socket
    remains visible to the caller as pending cleanup, never force-removed.
    A socket left by an owner that died with the host (a reboot or crash)
    holds neither owner lock and refuses connections; it is released, and
    the next owner removes it under its own lock.
    """
    deadline = time.monotonic() + timeout
    lock_path = str(socket_path) + '.resident.lock'
    service_lock_path = str(socket_path) + '.lock'
    while True:
        try:
            info = os.lstat(socket_path)
        except FileNotFoundError:
            info = None
        if info is not None and (not stat.S_ISSOCK(info.st_mode)
                                 or info.st_uid != os.getuid()
                                 or info.st_mode & 0o077):
            return False
        locked = _lock_held(lock_path)
        if locked is None:
            return False
        if info is None and not locked:
            # Both signals must agree. An owner loading its model owns the
            # lock before its socket exists, so socket absence alone is not
            # completion evidence.
            if not os.path.lexists(socket_path):
                return True
        if info is not None and not locked:
            # Every live owner holds the service lock while its socket exists.
            service_locked = _lock_held(service_lock_path)
            if service_locked is None:
                return False
            if (not service_locked and _refuses_connections(socket_path) and
                    _lock_held(lock_path) is False):
                try:
                    unchanged = os.lstat(socket_path).st_ino == info.st_ino
                except FileNotFoundError:
                    unchanged = True
                if unchanged:
                    return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(.05)




def _owned_route(project):
    """Prove one selected or pending installation using its own route contract."""
    from memvane.journal.api import selected_project_proof as _selected
    try:
        decision, proof = _selected(project)
        return decision, proof, 'selected'
    except IndexContractError as exc:
        if str(exc) != 'selected_route_unavailable':
            raise
    from memvane.journal.api import select_route
    from memvane.journal.api import verify_preview_route
    decision = select_route(str(project), action='recall')
    if (decision.get('state') != 'preview'
            or decision.get('projectRoot') != str(project)):
        raise IndexContractError('provider_owner_route_unavailable')
    proof = verify_preview_route(decision)
    if not isinstance(proof.get('providerManifest'), dict):
        raise IndexContractError('provider_owner_identity_invalid')
    return decision, proof, 'preview'


def _owner_identity(decision, proof):
    """Fields unchanged when this same pending 1.0 installation is selected."""
    keys = ('projectRoot', 'installId', 'journalPath', 'scopeId',
            'storePath', 'storeIncarnation', 'vectorProfile')
    if any(decision.get(key) is None for key in keys):
        raise IndexContractError('provider_owner_identity_invalid')
    return tuple(decision[key] for key in keys) + (proof['providerManifest'],)


def start_attested_worker(manifest, admit, *, execution_state, provider, worker):
    """Start only the exact attested or independently reattested artifacts."""
    from memvane.kernel.errors import IndexContractError
    from memvane.provider import api as provider_api
    local = {**manifest, 'providerPath': str(provider), 'workerPath': str(worker)}
    try:
        with attestation_reads():
            approved = provider_api.reattestation_check(execution_state, local)
    except ValueError as exc:
        if str(exc) == 'provider_artifact_changed':
            raise  # The file moved while being hashed; a new observation may succeed.
        raise IndexContractError('provider_attestation_invalid') from exc
    if not approved:
        raise IndexContractError('provider_attestation_invalid')
    if execution_state is None:
        return provider_api.start_owned_embed_service(local, admit=admit)
    return provider_api.start_owned_embed_service(local, admit=admit, execution_state=execution_state)


def forward_with_fallback(socket_path, payload, deadline, handle, left, read_all, response_limit):
    """Retry the same request once if an owned GPU worker requests CPU fallback."""
    previous = handle.process
    def send():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(min(left(deadline), 5))
            conn.connect(socket_path)
            conn.settimeout(left(deadline))
            conn.sendall(payload)
            conn.shutdown(socket.SHUT_WR)
            answer = read_all(conn, response_limit, deadline)
        if not answer:
            raise ConnectionError('laya_service_worker_closed')
        return answer
    try:
        return send()
    except (OSError, TimeoutError, ConnectionError):
        restart = getattr(handle, 'restart_cpu', None)
        if not callable(restart) or not restart(previous):
            raise
        return send()

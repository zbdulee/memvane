"""Selected installation's off-hook, private embedding service owner.

The bounded runtime dispatcher only starts this owner. The owner keeps the
existing parent pipe open while the selected installation remains live, so a
query hook never loads a model and a completed sync never unloads it.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
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
        raise IndexContractError('provider_start_failed')


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


def _spawn_owner(project, manifest):
    if os.environ.get('QMD_SETUP_GUARD_FIXTURE') == '1' or os.environ.get('QMD_SANDBOX'):
        raise ResourceDeferred('laya_service_fixture')
    path = manifest['serviceSocketPath']
    _private_parent(path)
    lock_path = path + '.resident.lock'
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or info.st_mode & 0o077):
            raise IndexContractError('provider_owner_lock_invalid')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        _failure_decision(manifest, own_lock=True)
        env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'TMPDIR', 'USER')
               if key in os.environ}
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        prior = read_owner_state(manifest)
        _write_state(manifest, 'starting',
                     attempts=min(_MAX_FAILURES, (prior['attempts'] if prior else 0) + 1))
        try:
            subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), 'serve',
                              str(project), path, manifest['embeddingFingerprint'], str(fd)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True, pass_fds=(fd,),
                             start_new_session=True, env=env)
        except OSError as exc:
            _record_failure(manifest, 'startup_failed')
            raise ResourceDeferred('laya_service_start_failed') from exc
    finally:
        os.close(fd)


def ensure_running(project, manifest):
    """Attach to an exact warm owner or request a detached cold start.

    Call only after the selected route and installer proof have been checked.
    In particular, never wait for model loading inside a hook or a provider
    activity lease; the durable sync wake retries this operation later.
    """
    status = probe(manifest)
    if status == 'ready':
        return AttachedService()
    if status == 'mismatch':
        raise ResourceDeferred('laya_service_identity_transition')
    _failure_decision(manifest)
    _spawn_owner(project, manifest)
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

    controller = coordinator or Coordinator()
    try:
        observed = (report if report is not None else
                    resource_report(EMBED_POLICY, detailed=True,
                                    deadline=time.monotonic() + 1))
        verdict = controller.check(EMBED_POLICY, observed)
    except (OSError, ValueError, TypeError, KeyError, ResourceDeferred) as exc:
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


def _serve(project, socket_path, fingerprint, owner_fd):
    """Run only in the detached child with its inherited installation lease."""
    from context_learning.resource_coordination import Coordinator
    from context_learning.shared import resource_report
    from laya_local_embed_service import start_owned_embed_service
    from laya_product_sync_worker import EMBED_POLICY, _selected

    project = Path(project)
    path = Path(socket_path)
    lock_path = Path(str(path) + '.resident.lock')
    held = os.fstat(owner_fd)
    expected = lock_path.lstat()
    if (not stat.S_ISREG(held.st_mode) or held.st_uid != os.getuid()
            or held.st_nlink != 1 or held.st_mode & 0o077
            or (held.st_dev, held.st_ino) != (expected.st_dev, expected.st_ino)
            or not project.is_absolute() or project.is_symlink()
            or not project.is_dir() or not path.is_absolute()):
        return 2
    try:
        fcntl.flock(owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 2
    decision, proof = _selected(project)
    manifest = proof['providerManifest']
    if (decision['projectRoot'] != str(project)
            or decision['pluginRoot'] != str(Path(__file__).resolve().parents[1])
            or manifest['serviceSocketPath'] != socket_path
            or manifest['embeddingFingerprint'] != fingerprint):
        raise IndexContractError('provider_owner_identity_invalid')
    from laya_install_journal import verify_worker_attestation
    if verify_worker_attestation(decision) != manifest:
        raise IndexContractError('provider_attestation_invalid')
    controller = Coordinator()
    with controller.activity('provider', EMBED_POLICY,
                             resource_report(EMBED_POLICY, detailed=True)):
        def admit():
            return controller.check(EMBED_POLICY,
                                    resource_report(EMBED_POLICY, detailed=True))['ready']
        service = start_owned_embed_service(manifest, admit=admit)
    try:
        def still_selected():
            current, current_proof = _selected(project)
            return (current['installId'] == decision['installId']
                    and current['pluginRoot'] == decision['pluginRoot']
                    and current['journalPath'] == decision['journalPath']
                    and current_proof['providerManifest'] == manifest)

        # A restore can commit during the cold load. Recheck immediately
        # after READY, then keep observing the selection while idle.
        try:
            if not still_selected():
                return 0
        except Exception:
            return 0
        prior = read_owner_state(manifest)
        _write_state(manifest, 'ready', attempts=prior['attempts'] if prior else 0)
        while service.process.poll() is None:
            time.sleep(1)
            try:
                if not still_selected():
                    break
            except Exception:
                # A removed/blocked route may no longer own this model.
                break
        else:
            raise IndexContractError('provider_worker_exited')
    finally:
        service.stop()
    return 0


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 5 or args[0] != 'serve':
        return 2
    manifest = {'serviceSocketPath': args[2], 'embeddingFingerprint': args[3]}
    try:
        return _serve(args[1], args[2], args[3], int(args[4]))
    except ResourceDeferred:
        _record_failure(manifest, 'resource_deferred', count=False)
        return 75
    except IndexContractError as exc:
        code = str(exc)
        if code == 'provider_admission_deferred':
            _record_failure(manifest, 'resource_deferred', count=False)
            return 75
        if code in ('provider_owner_identity_invalid', 'provider_artifact_hash_mismatch',
                    'provider_model_not_gguf', 'provider_attestation_invalid'):
            _record_failure(manifest, 'identity_invalid' if code ==
                            'provider_owner_identity_invalid' else 'attestation_invalid',
                            terminal=True)
        else:
            _record_failure(manifest, 'worker_exited' if code ==
                            'provider_worker_exited' else 'startup_failed')
        return 1
    except Exception:
        _record_failure(manifest, 'unexpected')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

"""Route one UID-shared Laya wake into its sealed installed bundle.

This is only a dispatcher. The target process repeats the full owner proof
before any model or store work; host/query authorization remains unchanged.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import time

from runtime_wake_policy import DispatchFailure


KINDS = frozenset(('laya-sync', 'laya-compile'))
HEX40 = re.compile(r'[0-9a-f]{40}\Z')
MAX_RESULT = 65536
# Selected sync and compile had no handler wall-clock deadline. Their existing
# step/queue policies still decide normal progress; this loose backstop detects
# a stuck delegated child without imposing a short QA-only product timeout.
SYNC_SECONDS = 86400
QA_BARRIER_SECONDS = 600
COMPILE_SECONDS = 86400
STOP_GRACE_SECONDS = 2
STABLE_TARGET_FIELDS = ('project', 'root', 'sourceCommit',
                        'bundleManifestSha256', 'ownershipManifestSha256',
                        'installId', 'scopeId', 'dbIncarnation',
                        'dbPath', 'queuePath', 'journalPath')


def _project(value):
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or str(path.resolve(strict=False)) != str(path):
        raise DispatchFailure('target_invalid')
    try:
        info = path.stat()
    except OSError as exc:
        raise DispatchFailure('target_unavailable') from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise DispatchFailure('target_invalid')
    return path


def resolve(kind, value):
    """Verify an active route or registered inactive build without executing it."""
    if kind not in KINDS:
        raise DispatchFailure('kind_invalid')
    project = _project(value)
    from laya_index_host_route import select_route
    route = select_route(str(project), action='update')
    if route.get('state') == 'candidate':
        from laya_install_journal import verify_dispatch_target
        try:
            proof = verify_dispatch_target(route)
        except (OSError, RuntimeError, ValueError) as exc:
            raise DispatchFailure('target_journal_changed') from exc
        root, commit, bundle_sha = (proof['pluginRoot'], proof['sourceCommit'],
                                    proof['bundleManifestSha256'])
        owner_sha = proof['ownershipManifestSha256']
        stable = {key: proof[key] for key in ('installId', 'scopeId',
                                             'dbIncarnation', 'journalPath')}
        stable.update(dbPath=route['dbPath'], queuePath=route['queuePath'])
        progress = {'selectionEpoch': proof['selectionEpoch'],
                    'generation': proof['generation']}
    elif kind == 'laya-sync' and route.get('state') == 'legacy':
        from laya_initial_build import lookup
        try:
            record = lookup(project)
        except (OSError, RuntimeError, ValueError) as exc:
            raise DispatchFailure('target_journal_changed') from exc
        if record is None:
            raise DispatchFailure('target_unavailable')
        identity = record['identity']
        if record['phase'] in ('bound', 'retired'):
            return {'terminal': True, 'project': str(project)}
        from laya_install_journal import inspect_journal, preflight_product_candidate
        try:
            journal = inspect_journal(identity['journalPath'])
        except (OSError, RuntimeError, ValueError) as exc:
            raise DispatchFailure('target_journal_changed') from exc
        pending = journal.get('pending')
        if (journal.get('state') != 'candidate_ready' or not isinstance(pending, dict)
                or journal.get('projectRoot') != str(project)):
            raise DispatchFailure('target_journal_changed')
        try:
            proof = preflight_product_candidate(identity['journalPath'])
        except (OSError, RuntimeError, ValueError) as exc:
            raise DispatchFailure('target_journal_changed') from exc
        if (proof['pendingRouteSha256'] != identity['routeSha256'] or
                proof['attestationSha256'] != identity['attestationSha256']):
            raise DispatchFailure('target_journal_changed')
        root, commit, bundle_sha = (pending['pluginRoot'], pending['sourceCommit'],
                                    pending['bundleManifestSha256'])
        owner_sha = pending['ownershipManifestSha256']
        stable = {'installId': identity['installId'], 'scopeId': identity['scopeId'],
                  'dbIncarnation': identity['dbIncarnation'],
                  'dbPath': identity['candidateDb'], 'queuePath': identity['queueDb'],
                  'journalPath': identity['journalPath']}
        progress = {'selectionEpoch': journal['epoch'],
                    'generation': record.get('sealed', {}).get('generation')
                    if isinstance(record.get('sealed'), dict) else None}
    else:
        raise DispatchFailure('target_unavailable')
    if (any(not isinstance(value, str) or not value for value in stable.values())
            or not isinstance(commit, str) or not HEX40.fullmatch(commit)
            or not isinstance(bundle_sha, str) or not re.fullmatch(r'[0-9a-f]{64}', bundle_sha)
            or not isinstance(owner_sha, str) or not re.fullmatch(r'[0-9a-f]{64}', owner_sha)):
        raise DispatchFailure('target_identity_invalid')
    root = Path(root)
    worker = root / 'core' / ('laya_product_sync_worker.py' if kind == 'laya-sync'
                            else 'wiki_selected_laya.py')
    try:
        info = worker.lstat()
    except OSError as exc:
        raise DispatchFailure('target_entry_unavailable', commit, bundle_sha) from exc
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o022 or worker.is_symlink()):
        raise DispatchFailure('target_entry_invalid', commit, bundle_sha)
    return {'terminal': False, 'project': str(project), 'root': str(root),
            'sourceCommit': commit, 'bundleManifestSha256': bundle_sha,
            'ownershipManifestSha256': owner_sha, **stable, **progress}


def _child_env():
    # Do not pass a foreign host plugin root to the target bundle. Keep only
    # the already authorized local runtime, QA ceiling and guard envelope.
    keys = ('PATH', 'HOME', 'LANG', 'TMPDIR', 'USER', 'QMD_QA_CALL_LEDGER',
            'QMD_QA_CALL_LIMIT', 'QMD_DEVELOPMENT_GUARD_RUN',
            'QMD_DEVELOPMENT_GUARD_NONCE', 'LAYA_QA_SYNC_BARRIER')
    env = {key: os.environ[key] for key in keys if key in os.environ}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['QMD_RUNTIME_DISPATCH'] = '1'
    return env


def _stop_owned_group(child):
    """Stop only the private process group created for this one wake."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            break
        try:
            child.wait(timeout=STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        if sig == signal.SIGTERM:
            time.sleep(.05)


def _run_child(argv, project, seconds, *, owner_fd=None):
    """Bounded output and owned descendant cleanup for a sealed one-shot job."""
    env = _child_env()
    env['QMD_LAYA_WAKE_OWNER_PIPE'] = '1'
    child = subprocess.Popen(argv, cwd=project, env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, start_new_session=True,
                             close_fds=True,
                             pass_fds=((owner_fd,) if owner_fd is not None else ()))
    deadline = time.monotonic() + seconds
    chunks = []
    used = 0
    selector = selectors.DefaultSelector()
    try:
        selector.register(child.stdout, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, seconds)
            for key, _ in selector.select(remaining):
                raw = os.read(key.fd, min(8192, MAX_RESULT + 1 - used))
                if not raw:
                    selector.unregister(key.fileobj)
                    break
                used += len(raw)
                if used > MAX_RESULT:
                    raise ValueError('target_output_limit')
                chunks.append(raw)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(argv, seconds)
        child.wait(timeout=remaining)
        if child.returncode:
            _stop_owned_group(child)
        return subprocess.CompletedProcess(argv, child.returncode, b''.join(chunks))
    except BaseException:
        _stop_owned_group(child)
        raise
    finally:
        selector.close()
        child.stdin.close()
        child.stdout.close()


def run(kind, value, *, owner_fd=None):
    target = resolve(kind, value)
    if target['terminal']:
        return {'state': 'unchanged'}
    commit = target['sourceCommit']
    bundle_sha = target['bundleManifestSha256']
    argv = [sys.executable, '-B', str(Path(__file__).with_name('laya_wake_entry.py')),
            kind, target['project'], target['root'], commit,
            target['bundleManifestSha256'], target['ownershipManifestSha256']]
    try:
        options = {'owner_fd': owner_fd} if owner_fd is not None else {}
        child = _run_child(argv, target['project'],
                           (SYNC_SECONDS + (QA_BARRIER_SECONDS
                                            if os.environ.get('LAYA_QA_SYNC_BARRIER') else 0)
                            if kind == 'laya-sync' else COMPILE_SECONDS), **options)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DispatchFailure('target_launch_failed', commit, bundle_sha) from exc
    except ValueError as exc:
        raise DispatchFailure('target_child_failed', commit, bundle_sha) from exc
    if child.returncode != 0:
        raise DispatchFailure('target_child_failed', commit, bundle_sha)
    try:
        result = json.loads(child.stdout.decode('utf-8'))
    except (TypeError, ValueError) as exc:
        raise DispatchFailure('target_result_invalid', commit, bundle_sha) from exc
    if not isinstance(result, dict) or result.get('state') not in (
            'indexed', 'unchanged', 'drained', 'pending', 'deferred', 'blocked'):
        raise DispatchFailure('target_result_invalid', commit, bundle_sha)
    if result['state'] not in (('drained',) if kind == 'laya-compile' else
                              ('indexed', 'unchanged')):
        raise DispatchFailure('target_deferred', commit, bundle_sha)
    current = resolve(kind, value)
    if (current['terminal'] or any(current[field] != target[field]
                                   for field in STABLE_TARGET_FIELDS)):
        # A successful sync may advance its journal epoch and indexed
        # generation. It cannot ACK a different install, scope or DB.
        raise DispatchFailure('target_journal_changed', commit, bundle_sha)
    return result

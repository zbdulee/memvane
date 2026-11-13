"""Route one UID-shared Laya wake into the executing plugin.

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

from memvane.runtime.runtime_wake_policy import DispatchFailure


KINDS = frozenset(('laya-sync', 'laya-compile'))
MAX_RESULT = 65536
# Last bytes of a failed child's stderr kept for the wake's failure record.
STDERR_TAIL = 4096
# Selected sync and compile had no handler wall-clock deadline. Their existing
# step/queue policies still decide normal progress; this loose backstop detects
# a stuck delegated child without imposing a short QA-only product timeout.
SYNC_SECONDS = 86400
QA_BARRIER_SECONDS = 600
COMPILE_SECONDS = 86400
STOP_GRACE_SECONDS = 2
STABLE_TARGET_FIELDS = ('project', 'installId', 'scopeId', 'storeIncarnation',
                        'storePath', 'journalPath')


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


def _bootstrap():
    """The executing plugin's one-wake bootstrap."""
    return Path(__file__).resolve().parents[2] / 'entry/run.py'


def resolve(kind, value):
    """Verify an active route or registered inactive build without executing it."""
    if kind not in KINDS:
        raise DispatchFailure('kind_invalid')
    project = _project(value)
    from memvane.route.index_host_route import select_route
    route = select_route(str(project), action='update')
    if route.get('state') == 'candidate':
        from memvane.journal.install_journal import verify_dispatch_target
        try:
            proof = verify_dispatch_target(route)
        except (OSError, RuntimeError, ValueError) as exc:
            raise DispatchFailure('target_journal_changed') from exc
        stable = {key: proof[key] for key in ('installId', 'scopeId',
                                             'storeIncarnation', 'journalPath')}
        stable.update(storePath=route['storePath'])
        progress = {'selectionEpoch': proof['selectionEpoch'],
                    'generation': proof.get('headGeneration')}
    elif kind == 'laya-sync' and route.get('state') == 'legacy':
        from memvane.install.initial_build import lookup
        try:
            record = lookup(project)
        except (OSError, RuntimeError, ValueError) as exc:
            raise DispatchFailure('target_journal_changed') from exc
        if record is None:
            raise DispatchFailure('target_unavailable')
        identity = record['identity']
        if record['phase'] in ('bound', 'retired'):
            return {'terminal': True, 'project': str(project)}
        from memvane.journal.install_journal import inspect_journal, preflight_product_candidate
        try:
            journal = inspect_journal(identity['journalPath'])
        except (OSError, RuntimeError, ValueError) as exc:
            raise DispatchFailure('target_journal_changed') from exc
        if record.get('schema') == 'memvane-initial-build-v1':
            from memvane.journal.install_journal import _attestation, _route, _v1_provider_manifest
            from memvane.route.index_host_route import _classify_v4, _read_regular, MAX_RECORD_BYTES
            import hashlib
            from memvane.settings import project_dir
            try:
                state = Path(identity['stateRoot'])
                raw = _read_regular(state / 'route.json', MAX_RECORD_BYTES)
                route_record, _ = _route(raw, Path(identity['journalPath']))
                candidate = _classify_v4(project, project_dir.settings_path(project),
                                         route_record)
                attestation, digest = _attestation(Path(identity['journalPath']))
                _v1_provider_manifest(attestation, journal['vectorProfile'])
                if (candidate.get('state') != 'candidate'
                        or hashlib.sha256(raw).hexdigest() != identity['routeSha256']
                        or journal['state'] != 'prepared'
                        or journal['routeSha256'] != identity['routeSha256']
                        or digest != journal['attestationSha256']
                        or digest != identity['attestationSha256']
                        or candidate['vectorProfile'] != journal['vectorProfile']
                        or any(journal[key] != identity[key] for key in
                               ('projectRoot', 'installId', 'scopeId', 'storePath', 'storeIncarnation'))
                        or any(candidate[key] != identity[key] for key in
                               ('projectRoot', 'installId', 'scopeId', 'storePath', 'storeIncarnation'))):
                    raise ValueError('initial_build_identity_changed')
                proof = {'stateFormat': journal['stateFormat'], 'writerFloor': journal['writerFloor']}
            except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
                raise DispatchFailure('target_journal_changed') from exc
        else:
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
        stable = {'installId': identity['installId'], 'scopeId': identity['scopeId'],
                  'storeIncarnation': identity['storeIncarnation'],
                  'storePath': identity['storePath'], 'journalPath': identity['journalPath']}
        progress = {'selectionEpoch': journal['epoch'],
                    'generation': record.get('sealed', {}).get('generation')
                    if isinstance(record.get('sealed'), dict) else None}
    else:
        raise DispatchFailure('target_unavailable')
    from memvane.kernel.memvane_version import same_major, writer_floor_allows
    try:
        matching_major = same_major(proof.get('stateFormat'))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DispatchFailure('target_journal_changed') from exc
    if not matching_major:
        raise DispatchFailure('state_format_mismatch')
    try:
        writable = writer_floor_allows(proof.get('writerFloor'))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DispatchFailure('target_journal_changed') from exc
    if not writable:
        raise DispatchFailure('writer_floor')
    if any(not isinstance(value, str) or not value for value in stable.values()):
        raise DispatchFailure('target_identity_invalid')
    root = Path(__file__).resolve().parents[2]
    worker = root / ('memvane/sync/product_sync_worker.py' if kind == 'laya-sync'
                     else 'memvane/wiki/workspace/wiki_selected_laya.py')
    for entry in (_bootstrap(), worker):
        try:
            info = entry.lstat()
        except OSError as exc:
            raise DispatchFailure('target_entry_unavailable') from exc
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022):
            raise DispatchFailure('target_entry_invalid')
    return {'terminal': False, 'project': str(project), **stable, **progress}


def _child_env():
    # Keep only the authorized local runtime, QA ceiling and guard envelope.
    keys = ('PATH', 'HOME', 'LANG', 'TMPDIR', 'USER', 'QMD_QA_CALL_LEDGER',
            'QMD_QA_CALL_LIMIT', 'QMD_DEVELOPMENT_GUARD_RUN',
            'QMD_DEVELOPMENT_GUARD_NONCE', 'LAYA_QA_SYNC_BARRIER',
            'MEMVANE_TEST_RUNTIME_ROOT', 'MEMVANE_TEST_ALLOW_UNINSTALLED_ROOT')
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


def _drain_ready(pipe, tail, *, limit=1 << 20):
    """Read what an exited child left in its stderr pipe without waiting."""
    os.set_blocking(pipe.fileno(), False)
    read = 0
    while read < limit:
        try:
            raw = os.read(pipe.fileno(), 8192)
        except BlockingIOError:
            return
        if not raw:
            return
        read += len(raw)
        tail.extend(raw)
        del tail[:-STDERR_TAIL]


def _run_child(argv, project, seconds, *, owner_fd=None):
    """Bounded output and owned descendant cleanup for a sealed one-shot job."""
    env = _child_env()
    env['QMD_LAYA_WAKE_OWNER_PIPE'] = '1'
    child = subprocess.Popen(argv, cwd=project, env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, start_new_session=True,
                             close_fds=True,
                             pass_fds=((owner_fd,) if owner_fd is not None else ()))
    deadline = time.monotonic() + seconds
    chunks = []
    used = 0
    # Only a bounded tail is retained. stderr is drained while stdout is open
    # so a chatty child never blocks on a full pipe; completion still follows
    # stdout EOF alone, as a descendant may inherit stderr past the child.
    tail = bytearray()
    selector = selectors.DefaultSelector()
    try:
        selector.register(child.stdout, selectors.EVENT_READ)
        selector.register(child.stderr, selectors.EVENT_READ)
        while child.stdout in (key.fileobj for key in selector.get_map().values()):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, seconds)
            for key, _ in selector.select(remaining):
                if key.fileobj is child.stderr:
                    raw = os.read(key.fd, 8192)
                    if not raw:
                        selector.unregister(key.fileobj)
                        continue
                    tail.extend(raw)
                    del tail[:-STDERR_TAIL]
                    continue
                raw = os.read(key.fd, min(8192, MAX_RESULT + 1 - used))
                if not raw:
                    selector.unregister(key.fileobj)
                    continue
                used += len(raw)
                if used > MAX_RESULT:
                    raise ValueError('target_output_limit')
                chunks.append(raw)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(argv, seconds)
        child.wait(timeout=remaining)
        if child.stderr in (key.fileobj for key in selector.get_map().values()):
            _drain_ready(child.stderr, tail)
        if child.returncode:
            _stop_owned_group(child)
        return subprocess.CompletedProcess(argv, child.returncode, b''.join(chunks),
                                           bytes(tail))
    except BaseException:
        _stop_owned_group(child)
        raise
    finally:
        selector.close()
        child.stdin.close()
        child.stdout.close()
        child.stderr.close()


def _failure_detail(child):
    tail = getattr(child, 'stderr', None)
    text = (tail[-STDERR_TAIL:].decode('utf-8', 'replace')
            if isinstance(tail, bytes) else '')
    return ('exit ' + str(child.returncode) + ('\n' + text if text else ''))


def _code(value):
    return (re.sub(r'[^a-z0-9_.:-]', '_', value)[:80]
            if isinstance(value, str) and value else None)


def _member_path(value):
    """A collection-relative Markdown member path from a child result, else None."""
    if (not isinstance(value, str) or not value.endswith('.md') or len(value) > 512
            or value.startswith('/') or '\\' in value
            or any(part in ('', '.', '..') for part in value.split('/'))
            or any(ord(ch) < 32 for ch in value)):
        return None
    return value


def _deferred_detail(result):
    """Fixed-code why of a not-done target result, for the failure record."""
    if result.get('state') == 'pending' and result.get('progressKind') in ('ingest', 'vector'):
        return 'deferred:pending:schema3_progress_' + result['progressKind']
    parts = ['deferred', _code(result.get('state')) or 'unlabeled',
             _code(result.get('reason')) or 'unlabeled']
    extra = _code(result.get('detail'))
    if extra:
        parts.append(extra)
    return ':'.join(parts)


def _diagnostic_route(target):
    """A freshly selected route proved to be this wake's own install, else None.

    The route must name the same install ID and journal as ``target``; any
    lookup failure or mismatch fails closed. Never raises.
    """
    try:
        from memvane.diagnostics import event_log
        from memvane.route import index_host_route as laya_index_host_route
        route = laya_index_host_route.select_route(target['project'], action='update')
        main = (route.get('mainRoute') if isinstance(route, dict)
                and route.get('state') == 'worktree' else route)
        journal = target.get('journalPath')
        install = target.get('installId')
        if (not isinstance(main, dict) or not isinstance(journal, str) or not journal
                or main.get('journalPath') != journal
                or event_log.state_root_for_decision(route) != event_log.state_root_of(journal)
                or not isinstance(install, str) or event_log.install_of(route) != install):
            return None
        return route
    except Exception:  # noqa: BLE001 - diagnostics never alter the wake
        return None


def _diagnostic_fields(target, kind, exc):
    """Extra ``wake.*`` fields linking an opt-in diagnostic; {} when off.

    A child that exited non-zero lost its own stack in another process: the
    dispatcher's wrapper is not that stack, so ``trace_unavailable`` is
    recorded and, only with consent, a private bounded ``child_stderr_tail``
    diagnostic of the failure detail (never in the ordinary event). Fixed
    deferral states carry no trace. Anything else captures the original
    exception chain.
    """
    fields = {}
    try:
        code = getattr(exc, 'code', None) if isinstance(exc, DispatchFailure) else None
        if code == 'target_deferred':
            return {}
        fields = {'trace_unavailable': 'child_process'} if code == 'target_child_failed' else {}
        route = _diagnostic_route(target)
        if route is None:
            return fields
        from memvane.diagnostics import diagnostic_trace
        if code == 'target_child_failed':
            if not diagnostic_trace.enabled(route):
                return fields
            detail = getattr(exc, 'detail', None)
            diagnostic_id = (diagnostic_trace.capture(
                route, event='wake.' + kind.removeprefix('laya-'), reason=code,
                child_stderr=detail) if getattr(exc, 'child_stderr_available', False)
                and isinstance(detail, str) and detail else None)
            if diagnostic_id is None and exc.__cause__ is not None:
                diagnostic_id = diagnostic_trace.capture(
                    route, event='wake.' + kind.removeprefix('laya-'),
                    reason=code, exc=exc.__cause__)
            if diagnostic_id:
                fields['diagnostic_id'] = diagnostic_id
            return fields
        diagnostic_id = diagnostic_trace.capture(
            route, event='wake.' + kind.removeprefix('laya-'),
            reason=code or type(exc).__name__,
            exc=None if code == 'target_preparing' else exc,
            warning=code == 'target_preparing')
        return {'diagnostic_id': diagnostic_id} if diagnostic_id else {}
    except Exception:  # noqa: BLE001 - diagnostics never alter the wake
        return fields


def _event(target, kind, started, *, outcome, reason=None, result=None, extra=None):
    """One ``wake.*`` event (and ``index.publish``) in the target install's log."""
    try:
        from memvane.diagnostics import event_log
        state_root = event_log.state_root_of(target.get('journalPath'))
        if state_root is None:
            return
        result = result if isinstance(result, dict) else {}
        common = {'state_root': state_root, 'install': target.get('installId'),
                  'host': 'worker', 'session': None}
        event_log.emit('wake.' + kind.removeprefix('laya-'), outcome=outcome, reason=reason,
                       ms=event_log.elapsed_ms(started),
                       generation=result.get('generation'),
                       previousGeneration=target.get('generation'),
                       detail=result.get('detail'), path=result.get('path'),
                       cause=result.get('cause'),
                       **(extra if isinstance(extra, dict) else {}), **common)
        if result.get('state') == 'indexed':
            event_log.emit('index.publish', outcome='published',
                           generation=result.get('generation'),
                           previousGeneration=target.get('generation'),
                           epoch=target.get('selectionEpoch'), **common)
    except Exception:
        pass


def drop_event(kind, value, *, reason, attempts=None):
    """Best-effort ``wake.drop`` in the install log of a still-routed project.

    A wake whose project directory is gone has no install log left to reach;
    the queue-side drop ledger (``runtime-wake-drops.json``) records it.
    """
    try:
        if not Path(value).is_dir():
            return False
        from memvane.diagnostics import event_log
        from memvane.route.index_host_route import select_route
        decision = select_route(str(value), action='update')
        return event_log.emit('wake.drop', outcome='dropped', reason=reason,
                              decision=decision, host='worker', session=None,
                              kind=kind, attempts=attempts)
    except Exception:
        return False


def stall_event(kind, value, *, since, observations, retry_in):
    """Best-effort report of a ``target_preparing`` streak that outlived the stall bound.

    Logged once per streak as a failure (``target_preparing_stalled``), so the
    log summary and errors view show an owner that never became ready.
    """
    try:
        if not Path(value).is_dir():
            return False
        from memvane.diagnostics import event_log
        from memvane.route.index_host_route import select_route
        decision = select_route(str(value), action='update')
        return event_log.emit('wake.' + kind.removeprefix('laya-'), outcome='failed',
                              reason='target_preparing_stalled', decision=decision,
                              host='worker', session=None,
                              since=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(since)),
                              observations=observations, retryIn=retry_in)
    except Exception:
        return False


def run(kind, value, *, owner_fd=None):
    target = resolve(kind, value)
    if target['terminal']:
        return {'state': 'unchanged'}
    started = time.monotonic()
    try:
        result = _run_target(kind, value, target, owner_fd)
    except DispatchFailure as exc:
        # Only a deferral's detail is fixed codes; a child's stderr tail never is.
        diagnostic_fields = _diagnostic_fields(target, kind, exc)
        if exc.code == 'target_preparing':
            diagnostic_fields['severity'] = 'warning'
        _event(target, kind, started, outcome='failed', reason=exc.code,
               result={'detail': getattr(exc, 'detail', None),
                       'path': getattr(exc, 'member_path', None),
                       'cause': getattr(exc, 'member_cause', None)}
               if exc.code in ('target_deferred', 'target_preparing') else None,
               extra=diagnostic_fields)
        raise
    except Exception as exc:
        _event(target, kind, started, outcome='failed', reason=type(exc).__name__,
               extra=_diagnostic_fields(target, kind, exc))
        raise
    _event(target, kind, started, outcome=result.get('state'), reason=result.get('reason'),
           result=result)
    return result


def _run_target(kind, value, target, owner_fd):
    argv = [sys.executable, '-B', str(_bootstrap()), 'wake', kind, target['project']]
    try:
        options = {'owner_fd': owner_fd} if owner_fd is not None else {}
        child = _run_child(argv, target['project'],
                           (SYNC_SECONDS + (QA_BARRIER_SECONDS
                                            if os.environ.get('LAYA_QA_SYNC_BARRIER') else 0)
                            if kind == 'laya-sync' else COMPILE_SECONDS), **options)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DispatchFailure('target_launch_failed') from exc
    except ValueError as exc:
        raise DispatchFailure('target_child_failed',
                              detail=str(exc) or type(exc).__name__) from exc
    if child.returncode != 0:
        failure = DispatchFailure('target_child_failed',
                                  detail=_failure_detail(child))
        failure.child_stderr_available = True
        raise failure
    try:
        result = json.loads(child.stdout.decode('utf-8'))
    except (TypeError, ValueError) as exc:
        raise DispatchFailure('target_result_invalid') from exc
    if not isinstance(result, dict) or result.get('state') not in (
            'indexed', 'unchanged', 'drained', 'pending', 'deferred', 'blocked'):
        raise DispatchFailure('target_result_invalid')
    if result['state'] not in (('drained',) if kind == 'laya-compile' else
                              ('indexed', 'unchanged')):
        if (kind == 'laya-sync' and result['state'] == 'deferred'
                and result.get('reason') == 'laya_service_preparing'):
            # The target just started (or is still loading) its detached
            # embedding owner. Waiting on that owner is not a failed attempt.
            raise DispatchFailure('target_preparing')
        failure = DispatchFailure('target_deferred',
                                  detail=_deferred_detail(result))
        # The fixed-code detail cannot carry a (non-ASCII) card path; the
        # install log names the member the fill stopped at separately.
        failure.member_path = _member_path(result.get('path'))
        failure.member_cause = (_code(result.get('cause'))
                                if failure.member_path is not None else None)
        raise failure
    current = resolve(kind, value)
    if (current['terminal'] or any(current[field] != target[field]
                                   for field in STABLE_TARGET_FIELDS)):
        # A successful sync may advance its journal epoch and indexed
        # generation. It cannot ACK a different install, scope or DB.
        raise DispatchFailure('target_journal_changed')
    return result

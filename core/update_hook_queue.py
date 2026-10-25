"""Durable SessionStart update handoff; only the project path is persisted."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import tempfile
import time


def _private_queue():
    root = Path(os.environ.get('QMD_UPDATE_HOOK_QUEUE_DIR',
        str(Path.home()/'.cache/qmd/update-hook-jobs')))
    if not root.is_absolute() or root.is_symlink(): raise ValueError('unsafe_update_queue')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('unsafe_update_queue')
    return root


SETUP_REQUIRED = 'setup_required_store_not_selected'


def _no_project_config_candidate(cwd):
    """find_project_config reads unreadable settings as 'none'; recheck files."""
    import config as qmd_config
    for directory in qmd_config._project_search_dirs(cwd):
        for candidate, _, _ in qmd_config._candidate_configs(directory):
            if candidate.exists() or candidate.is_symlink():
                return False
    return True


def _write(path, value):
    fd, tmp = tempfile.mkstemp(prefix='.update-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf8') as out:
            json.dump(value, out, sort_keys=True)
            out.write('\n'); out.flush(); os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def readiness(key, status, run_id):
    """Report the actual QMD phase from the detached update/embed children."""
    if len(key) != 32 or any(c not in '0123456789abcdef' for c in key):
        raise ValueError('invalid_update_job_key')
    if status not in ('indexing', 'lexical_ready', 'completed', 'failed', 'embed_failed'):
        raise ValueError('invalid_update_phase')
    if len(run_id) != 32 or any(c not in '0123456789abcdef' for c in run_id):
        raise ValueError('invalid_update_run')
    queue = _private_queue()
    path = queue / (key + '.status.json')
    lock = queue / (key + '.status.lock')
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('unsafe_update_status_lock')
        fcntl.flock(fd, fcntl.LOCK_EX)
        if path.is_symlink():
            raise ValueError('unsafe_update_status')
        if status != 'indexing':
            try:
                previous = json.loads(path.read_text())
            except (OSError, ValueError):
                return
            if not isinstance(previous, dict) or previous.get('runId') != run_id:
                return
        service_ready = False
        if status == 'completed':
            try:
                manager = Path(__file__).with_name('backend_manager.sh')
                check = subprocess.run(['bash', str(manager), 'health-strict'],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=3, check=False)
                service_ready = check.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                pass
        _write(path, {'schema': 'qmd-update-hook-status-v1', 'cwdHash': key,
                      'runId': run_id, 'status': status,
                      'searchReady': status == 'completed' and service_ready,
                      'lexicalReady': status in ('lexical_ready', 'completed', 'embed_failed'),
                      'vectorReady': status == 'completed', 'at': time.time()})
    finally:
        os.close(fd)


def _setup_notice(cwd, queue, guard=None):
    """One local, bounded installation-status hint; never starts migration."""
    if os.environ.get('QMD_SUPPRESS_NOTICE'): return
    import config as qmd_config
    if guard is None:
        import setup_guard
        guard = setup_guard.status(cwd)
    if guard['status'] == 'optout': return
    found = qmd_config.find_project_config(cwd)
    if found['configFormat'] == 'local-optout': return
    project = Path(found['projectRoot'] if found['configFormat'] != 'none'
                   else qmd_config.project_identity_root(cwd)).resolve()
    auto = project / '.auto-context'
    settings = auto / 'settings.json'
    pointer = auto / 'qmd-index-active.json'
    journal = auto / 'install-update-journal.json'
    # Use the same decision that stopped hooks/workers. A present but invalid
    # pointer or a prepared journal must still produce repair guidance.
    reason = guard.get('reason') if guard['status'] == 'setup_required' else None
    if reason is None:
        if settings.is_symlink() or pointer.is_symlink() or journal.is_symlink(): return
        if not settings.is_file(): reason = 'settings_not_ready'
        elif not pointer.is_file():
            try:
                import qmd_route
                route = qmd_route.project_paths(project, isolated=True)
                if route.get('shared') is not True:
                    reason = 'v2_index_not_selected'
            except (OSError, ValueError, KeyError, TypeError):
                reason = 'plugin_qmd_store_not_selected'
        if journal.is_file() and journal.stat().st_size <= 65536:
            try:
                state = json.loads(journal.read_text())
                if state.get('schema') != 'qmd-install-update-v1': reason = 'setup_schema_review_required'
            except (OSError, ValueError, AttributeError):
                reason = 'setup_schema_review_required'
    package = json.loads((Path(__file__).parent.parent / 'package.json').read_text())
    version = package['version']
    project_key = hashlib.sha256(str(project).encode()).hexdigest()[:32]
    version_path = queue / ('setup-version-' + project_key + '.json')
    if version_path.is_symlink(): return
    previous = None
    if version_path.is_file() and version_path.stat().st_size <= 4096:
        state = json.loads(version_path.read_text())
        if isinstance(state, dict) and state.get('schema') == 'qmd-setup-version-v1':
            previous = state.get('version')
    version_changed = previous is not None and previous != version
    if reason is None and settings.is_file():
        import setup_readiness
        reason = setup_readiness.laya_preflight()['reason']
    if reason is None and version_changed:
        reason = 'plugin_version_changed'
    version_state = {'schema': 'qmd-setup-version-v1', 'version': version,
                     'project': str(project)}
    if reason is None:
        _write(version_path, version_state)
        return
    token = hashlib.sha256((str(project) + '\0' + version + '\0' + reason).encode()).hexdigest()[:32]
    marker = queue / ('setup-notice-' + token + '.json')
    fd, tmp = tempfile.mkstemp(prefix='.setup-notice-', dir=queue)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf8') as out:
            json.dump({'schema': 'qmd-setup-notice-v1', 'project': str(project),
                       'pluginVersion': version, 'reason': reason}, out, sort_keys=True)
            out.write('\n'); out.flush(); os.fsync(out.fileno())
        try: os.link(tmp, marker, follow_symlinks=False)
        except FileExistsError: return
        _write(version_path, version_state)
        version_hint = ' plugin_version_changed;' if version_changed and reason != 'plugin_version_changed' else ''
        print(f'[qmd] 플러그인 {version}:{version_hint} 이 프로젝트 설치 상태({reason})를 검토하세요. '
              'setup skill에 “설치 계획 보여줘”를 요청하면 읽기 전용 점검을 시작합니다. '
              '긴 준비와 적용은 각각 명시적 승인 후에만 실행됩니다.')
    finally:
        Path(tmp).unlink(missing_ok=True)


def enqueue(raw):
    if len(raw) > 65536: raise ValueError('oversized_session_start')
    payload = json.loads(raw) if raw.strip() else {}
    if not isinstance(payload, dict): raise ValueError('invalid_session_start')
    cwd = payload.get('cwd') or os.getcwd()
    path = Path(cwd)
    if not isinstance(cwd, str) or not path.is_absolute() or not path.is_dir():
        raise ValueError('invalid_session_start_cwd')
    cwd = str(path.resolve())
    import config as qmd_config
    from context_learning.synthetic_scope import invocation_in_generated_story
    if not invocation_in_generated_story(qmd_config.find_project_config(cwd), payload.get('cwd')):
        return
    queue = _private_queue()
    key = hashlib.sha256(cwd.encode()).hexdigest()[:32]
    import setup_guard
    guard = setup_guard.status(cwd)
    if guard['status'] != 'ready':
        if guard['status'] == 'setup_required':
            try: _setup_notice(cwd, queue, guard)
            except (OSError, ValueError, KeyError, TypeError): pass
        return
    if os.environ.get('QMD_ENGINE') == 'claude':
        import file_change_queue
        try: file_change_queue.handle('reconcile',payload)
        except (OSError,ValueError,TypeError,KeyError): pass
    job = queue/(key+'.job.json')
    # One pending job per project. Never replace a running worker's input.
    fd = None
    try:
        fd = os.open(job, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w', encoding='utf8') as out:
            fd = None
            json.dump({'schema': 'qmd-update-hook-job-v1', 'cwd': cwd}, out)
            out.write('\n'); out.flush(); os.fsync(out.fileno())
    except FileExistsError:
        if job.is_symlink() or job.stat().st_uid != os.getuid() or job.stat().st_mode & 0o077:
            raise ValueError('unsafe_update_job')
    finally:
        if fd is not None: os.close(fd)
    # Persist a restart hint before the detached process can be interrupted.
    import runtime_work
    receipt=runtime_work.request('update',key,start=False)
    if isinstance(receipt,dict) and receipt['status']=='deferred_queue_capacity':
        runtime_work.hook_wake();return
    # Local version/schema/pointer checks only; no registry or model call.
    try: _setup_notice(cwd, queue, guard)
    except (OSError, ValueError, KeyError, TypeError): pass
    # The first opt-in decision must reach the current host turn. Resolve is a
    # bounded, read-only path; all scans/index/embed remain in the worker.
    if not os.environ.get('QMD_SUPPRESS_NOTICE'):
        try:
            update_script = os.environ.get('QMD_CORE_UPDATE_SCRIPT') or str(Path(__file__).with_name('update.sh'))
            found = qmd_config.find_project_config(cwd)
            result = subprocess.run(['bash', update_script, '--resolve-only', '--cwd', found['projectRoot']],
                cwd=cwd, input=json.dumps(found['config']), capture_output=True, text=True,
                timeout=1.0, check=False)
            resolved = json.loads(result.stdout)
            if result.returncode == 0 and isinstance(resolved, dict) and resolved.get('reason') == 'pending':
                print('[qmd] 이 폴더의 검색 등록 여부가 아직 결정되지 않았습니다. 등록 범위를 확인한 뒤 opt-in 또는 opt-out을 선택해 주세요.')
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
            pass
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'worker', key],
        cwd=queue, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
    runtime_work.hook_wake()


def worker(key):
    if len(key) != 32 or any(c not in '0123456789abcdef' for c in key):
        raise ValueError('invalid_update_job_key')
    queue = _private_queue()
    lock = queue/(key+'.lock')
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_uid != os.getuid() or os.fstat(fd).st_mode & 0o077:
            raise ValueError('unsafe_update_lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        job = queue/(key+'.job.json')
        if not job.is_file() or job.is_symlink(): return
        row = json.loads(job.read_text())
        cwd = row.get('cwd') if isinstance(row, dict) else None
        if (not isinstance(row, dict) or row.get('schema') != 'qmd-update-hook-job-v1' or not isinstance(cwd, str)
                or hashlib.sha256(cwd.encode()).hexdigest()[:32] != key):
            raise ValueError('invalid_update_job')
        import config as qmd_config
        from context_learning.synthetic_scope import invocation_in_generated_story
        if not invocation_in_generated_story(qmd_config.find_project_config(cwd), cwd):
            return
        log = queue/(key+'.log')
        if log.is_symlink(): raise ValueError('unsafe_update_log')
        if log.is_file() and log.stat().st_size > 1024*1024:
            os.replace(log, queue/(key+'.log.1'))
        with log.open('a', encoding='utf8') as output:
            os.chmod(log, 0o600)
            output.write(f'{time.time():.3f} update started\n'); output.flush()
            try:
                import setup_guard
                if setup_guard.status(cwd)['status'] != 'ready':
                    _write(queue/(key+'.status.json'), {'schema': 'qmd-update-hook-status-v1',
                        'cwdHash': key, 'status': 'setup_required', 'at': time.time()})
                    output.write(f'{time.time():.3f} setup required; job retained\n'); output.flush()
                    return
                import qmd_route
                try:
                    selected = qmd_route.project_paths(cwd)
                except ValueError as exc:
                    # A genuine first install has no project config and no
                    # selected plugin store: that is setup-required, not a
                    # worker failure. Any configured project, or any other
                    # store error (unsafe/corrupt/unapproved), still fails closed.
                    found = qmd_config.find_project_config(cwd)
                    if (str(exc) != 'plugin_qmd_store_not_ready' or found.get('configFormat') != 'none'
                            or (found.get('config') or {}).get('collections')
                            or not _no_project_config_candidate(cwd)
                            or setup_guard.status(cwd)['status'] != 'ready'):
                        raise
                    _write(queue/(key+'.status.json'), {'schema': 'qmd-update-hook-status-v1',
                        'cwdHash': key, 'status': 'setup_required',
                        'reason': 'plugin_qmd_store_not_selected', 'at': time.time()})
                    output.write(f'{time.time():.3f} setup required: plugin store not selected; job retained\n'); output.flush()
                    # The job stays for a later SessionStart after setup. The
                    # result lets the wake handler ACK only this revision.
                    return SETUP_REQUIRED
                if not selected['selected']:
                    manager = os.environ.get('QMD_BACKEND_MANAGER') or str(Path(__file__).with_name('backend_manager.sh'))
                    for action in ('ensure', 'warm', 'rotate'):
                        subprocess.run(['bash', manager, action], cwd=cwd, stdin=subprocess.DEVNULL,
                            stdout=output, stderr=output, timeout=30, check=False)
                update_script = os.environ.get('QMD_CORE_UPDATE_SCRIPT') or str(Path(__file__).with_name('update.sh'))
                product_update = 'QMD_CORE_UPDATE_SCRIPT' not in os.environ
                run_id = secrets.token_hex(16) if product_update else None
                if product_update:
                    readiness(key, 'indexing', run_id)
                result = subprocess.run(['bash', update_script],
                    cwd=cwd, input=json.dumps({'cwd': cwd}), text=True,
                    capture_output=True, timeout=None if product_update else 120,
                    check=False,
                    env={**os.environ, 'QMD_UPDATE_HOOK_KEY': key,
                         'QMD_UPDATE_HOOK_RUN_ID': run_id,
                         'QMD_UPDATE_HOOK_WAIT': '1'} if product_update else None)
                status = 'completed' if result.returncode == 0 else 'failed'
                if product_update and status == 'completed':
                    try:
                        observed = json.loads((queue/(key+'.status.json')).read_text())
                        phase = observed.get('status') if observed.get('runId') == run_id else None
                    except (OSError, ValueError, AttributeError):
                        phase = None
                    # update.sh deliberately exits zero for hook safety even
                    # after a fail-closed setup/ownership/lock abort. Its
                    # lexical_ready marker is the minimum proof that QMD
                    # update actually reached the embed handoff.
                    if phase != 'completed':
                        status = 'failed'
                # The SessionStart output is a notice, not source content.
                output.write((result.stdout or '')[:2048] + '\n')
                output.write((result.stderr or '')[:2048] + '\n')
                output.write(f'{time.time():.3f} update {status}\n'); output.flush()
                if product_update and status == 'failed':
                    readiness(key, 'failed', run_id)
                elif not product_update:
                    _write(queue/(key+'.status.json'), {'schema': 'qmd-update-hook-status-v1',
                        'cwdHash': key, 'status': status,
                        'searchReady': None,
                        'at': time.time()})
                if status == 'completed': job.unlink()
                else:
                    import runtime_work
                    runtime_work.request('update',key)
            except (OSError, subprocess.TimeoutExpired) as exc:
                output.write(f'{time.time():.3f} update failed:{type(exc).__name__}\n')
                _write(queue/(key+'.status.json'), {'schema': 'qmd-update-hook-status-v1',
                    'cwdHash': key, 'status': 'failed', 'at': time.time()})
    finally:
        os.close(fd)


if __name__ == '__main__':
    try:
        if len(sys.argv) == 2 and sys.argv[1] == 'enqueue':
            enqueue(sys.stdin.read(65537))
        elif len(sys.argv) == 3 and sys.argv[1] == 'worker':
            worker(sys.argv[2])
        elif len(sys.argv) == 5 and sys.argv[1] == 'readiness':
            readiness(sys.argv[2], sys.argv[3], sys.argv[4])
        else:
            raise SystemExit(2)
    except BaseException as exc:
        if (len(sys.argv) == 2 and sys.argv[1] == 'enqueue') or (len(sys.argv) == 3 and sys.argv[1] == 'worker'):
            try:
                queue = _private_queue()
                phase = sys.argv[1]
                key = sys.argv[2] if phase == 'worker' else None
                if key is None or (len(key) == 32 and all(c in '0123456789abcdef' for c in key)):
                    log = queue/((key+'.log') if key else 'enqueue.log')
                    if log.is_symlink(): raise ValueError('unsafe_update_log')
                    if log.is_file() and log.stat().st_size > 1024*1024:
                        os.replace(log, queue/(log.name+'.1'))
                    with log.open('a', encoding='utf8') as output:
                        os.chmod(log, 0o600)
                        output.write(f'{time.time():.3f} {phase} failed:{type(exc).__name__}\n')
                    _write(queue/((key+'.status.json') if key else 'enqueue.status.json'),
                        {'schema': 'qmd-update-hook-status-v1', 'cwdHash': key,
                         'status': phase + '_failed', 'at': time.time()})
            except BaseException:
                pass
        raise SystemExit(1)

"""Deferred Claude shell CUD detection from file state, never shell text.

PostToolUse records a coalesced job. Stop/SessionStart wake the snapshot worker;
only the ordinary durable indexing/compile queues can perform downstream work.
"""
from __future__ import annotations
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import uuid

import config
import dirty_queue
import qmd_route
import setup_guard
import sqlite_read
import sync

FOLDER = '.auto-context/file-changes'
MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_PASSES = 4


def _context(cwd):
    if not isinstance(cwd, str) or not Path(cwd).is_absolute(): return None
    found = config.find_project_config(cwd)
    if (found['config'].get('indexing') is not True or
            not config.event_enabled(found['config'], 'postToolUse') or
            setup_guard.status(cwd)['status'] != 'ready'):
        return None
    from context_learning.synthetic_scope import invocation_in_generated_story
    if not invocation_in_generated_story(found, cwd): return None
    root = Path(found['projectRoot']).resolve(strict=True)
    roots, refused = sync.resolve_collection_roots(str(root), found['config'])
    # An allowRoots grant does not widen shell observation to another project.
    roots = [(name, path) for name, path in roots if path == root or root in path.parents]
    if refused or not roots: return None
    scope = hashlib.sha256(json.dumps({'project':str(root), 'roots':[(n,str(p)) for n,p in roots],
        'roles':config.role_map(found['config'])},sort_keys=True).encode()).hexdigest()
    return root, found['config'], roots, scope


def _folder(root):
    folder = root / FOLDER
    if folder.is_symlink() or (root / '.auto-context').is_symlink():
        raise ValueError('unsafe_file_change_directory')
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = folder.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('unsafe_file_change_directory')
    return folder


def _read(path):
    if not path.exists() and not path.is_symlink(): return None
    # A FIFO can block in open before fstat gets a chance to reject it.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > MAX_STATE_BYTES):
            raise ValueError('unsafe_file_change_state')
        return json.loads(os.read(fd, info.st_size+1))
    finally:
        os.close(fd)


def _write(path, value):
    if path.is_symlink(): raise ValueError('unsafe_file_change_state')
    data = json.dumps(value,sort_keys=True).encode()+b'\n'
    if len(data) > MAX_STATE_BYTES: raise ValueError('file_change_state_too_large')
    fd, name = tempfile.mkstemp(prefix='.change-',dir=path.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
        directory = os.open(path.parent,os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def _lock(folder, name, *, nonblocking=False):
    fd = os.open(folder/name,os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('unsafe_file_change_lock')
        fcntl.flock(fd,fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        yield
    finally:
        os.close(fd)


def _spawn_worker(root):
    subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'worker',str(root)],
        cwd=root,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        close_fds=True,start_new_session=True)


def _successful_bash(payload):
    response = payload.get('tool_response')
    if payload.get('hook_event_name') != 'PostToolUse' or payload.get('tool_name') != 'Bash' or not isinstance(response,dict):
        return False
    tool_input = payload.get('tool_input')
    if isinstance(tool_input,dict) and tool_input.get('run_in_background'): return False
    if any(response.get(k) for k in ('is_error','error','interrupted','backgroundTaskId','background_task_id')):
        return False
    codes = [response[k] for k in ('exit_code','exitCode','returncode') if k in response]
    if codes: return all(type(code) is int and code == 0 for code in codes)
    # Official Bash Output shape; PostToolUse itself is the success event.
    return (response.get('interrupted') is False and isinstance(response.get('stdout'),str)
            and isinstance(response.get('stderr'),str))


def handle(action, payload):
    if os.environ.get('QMD_SANDBOX') or not isinstance(payload,dict): return
    if action == 'event' and not _successful_bash(payload): return
    if action == 'boundary' and payload.get('hook_event_name') != 'Stop': return
    if action == 'reconcile' and payload.get('hook_event_name') != 'SessionStart': return
    if action not in ('event','boundary','reconcile'): raise ValueError('invalid_file_change_action')
    context = _context(payload.get('cwd'))
    if context is None: return
    root, _cfg, _roots, scope = context
    # A no-event Stop should create no new directory, file, scan or worker.
    if action == 'boundary' and not (root/FOLDER/'pending.json').exists(): return
    folder = _folder(root)
    if action != 'boundary':
        with _lock(folder,'.state.lock'):
            existing = _read(folder/'pending.json')
            # Recovery must preserve the original event's compile consent.
            if action == 'event' or existing is None:
                _write(folder/'pending.json',{'schema':'qmd-file-change-job-v1',
                    'scope':scope,'origin':action,'nonce':uuid.uuid4().hex})
    if action != 'event': _spawn_worker(root)


def _snapshot(root, roots):
    result = {}
    for name, path in roots:
        files = sync.scan_files(path,hash_markdown=True) or {}
        # Never observe our own queue/snapshot/status when collection root is '.'.
        files = {rel:meta for rel,meta in files.items()
                 if root/FOLDER not in (path/rel).parents}
        result[name] = {'root':str(path),'files':files}
    return result


def _first_baseline(root, current, cfg, *, event):
    """Compare active QMD paths/hashes, including a first-event create/update."""
    baseline = {name:{'root':row['root'],'files':{}} for name,row in current.items()}
    route = qmd_route.project_paths(root,isolated=True,reject_conflicts=True,
        fixture_env=os.environ.get('QMD_SETUP_GUARD_FIXTURE') == '1')
    roles = config.role_map(cfg)
    with sqlite_read.connect(route['INDEX_PATH']) as db:
        for name, collection in baseline.items():
            if not config.is_indexed_collection(roles,name):
                if not event: collection['files'] = current[name]['files'].copy()
                continue
            for rel, digest in db.execute('SELECT path,hash FROM documents WHERE collection=? AND active=1',(name,)):
                if (not isinstance(rel,str) or Path(rel).is_absolute() or '..' in Path(rel).parts
                        or str(rel).startswith('.auto-context/file-changes/')): continue
                meta = current[name]['files'].get(rel)
                if meta is None: collection['files'][rel] = {'indexedMissing':True}
                elif meta.get('sha256') == digest: collection['files'][rel] = meta.copy()
                else: collection['files'][rel] = {'sha256':digest}
    return baseline


def _kick(root, compile_queued):
    manager = os.environ.get('QMD_BACKEND_MANAGER') or str(Path(__file__).with_name('backend_manager.sh'))
    subprocess.run(['bash',manager,'kick-index'],stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10,check=False)
    if compile_queued:
        subprocess.run(['bash',manager,'kick-wiki-compile',str(root)],stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10,check=False)


def worker(root):
    context = _context(str(root))
    if context is None: return
    root, cfg, roots, scope = context
    folder = _folder(root);finished = False;processed = False
    try:
        with _lock(folder,'.worker.lock',nonblocking=True):
            for _pass in range(MAX_PASSES):
                job = _read(folder/'pending.json')
                if job is None: finished = True;break
                if (not isinstance(job,dict) or set(job) != {'schema','scope','origin','nonce'}
                        or job['schema'] != 'qmd-file-change-job-v1' or job['scope'] != scope
                        or job['origin'] not in ('event','reconcile')
                        or not isinstance(job['nonce'],str) or len(job['nonce']) != 32):
                    raise ValueError('file_change_job_scope_invalid')
                # Setup/policy can change while a detached worker waits.
                if _context(str(root)) != context: raise ValueError('file_change_scope_changed')
                previous = _read(folder/'snapshot.json')
                current = _snapshot(root,roots)
                if previous is None or previous.get('scope') != scope:
                    old = _first_baseline(root,current,cfg,event=job['origin']=='event')
                else:
                    if previous.get('schema') != 'qmd-file-change-snapshot-v1' or not isinstance(previous.get('collections'),dict):
                        raise ValueError('invalid_file_change_snapshot')
                    old = previous['collections']
                changed = {};files = {};totals = {'created':0,'updated':0,'deleted':0}
                roles = config.role_map(cfg)
                for name, row in current.items():
                    before = old.get(name,{})
                    if before.get('root') != row['root']: before = {}
                    created, updated, deleted = sync.compare_collection(before.get('files',{}),row['files'])
                    for key, values in (('created',created),('updated',updated),('deleted',deleted)): totals[key] += len(values)
                    if created or updated or deleted:
                        files[name] = {'root':row['root'],'created':created,'updated':updated,'deleted':deleted}
                        if config.is_indexed_collection(roles,name): changed[name] = row['root']
                if _context(str(root)) != context: raise ValueError('file_change_scope_changed')
                if changed: dirty_queue.enqueue_project_collections(root,changed)
                snapshot = {'collections':current}
                compiled = sync.enqueue_compile_sources(str(root),cfg,files,snapshot,{'collections':old},
                    session_start=job['origin']=='reconcile')
                _write(folder/'snapshot.json',{'schema':'qmd-file-change-snapshot-v1','scope':scope,
                    'collections':snapshot['collections']})
                # Persist enqueue/snapshot before ACK; retain a concurrent newer event.
                with _lock(folder,'.state.lock'):
                    if not compiled['deferred'] and _read(folder/'pending.json') == job:
                        (folder/'pending.json').unlink();finished = True
                _write(folder/'status.json',{'schema':'qmd-file-change-status-v1',**totals,
                    'collectionsQueued':sorted(changed),'compileQueued':compiled['queued'],
                    'compileDeferred':compiled['deferred']})
                if changed or compiled['queued']: _kick(root,compiled['queued'])
                # Preserve the event grant across the next wake. A per-wake cap,
                # including zero, must not become an unlimited automatic loop.
                if compiled['deferred']: processed = False;break
                processed = True
                if finished: break
    except BlockingIOError:
        return
    except Exception as exc:
        processed = False
        _write(folder/'status.json',{'schema':'qmd-file-change-status-v1','status':'deferred',
            'reason':type(exc).__name__})
    # Close the single-flight lock before waking a job that arrived at exit.
    if (finished or processed) and (folder/'pending.json').exists(): _spawn_worker(root)


if __name__ == '__main__':
    try:
        action = sys.argv[1]
        if action == 'worker': worker(Path(sys.argv[2]))
        else:
            raw = sys.stdin.read(65537)
            if len(raw) <= 65536: handle(action,json.loads(raw))
    except (OSError,ValueError,TypeError,KeyError):
        pass  # Hook stdout remains silent; an existing durable job is preserved.

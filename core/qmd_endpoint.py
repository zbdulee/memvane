"""Private, proved localhost endpoint; publish only after QMD actually binds."""
from contextlib import contextmanager
import fcntl
import install_deadline
import errno
import json
import os
from pathlib import Path
import re
import stat
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

SCHEMA='qmd-endpoint-v1'
DEFAULT_PORT=8584
ROOT=Path(__file__).resolve().parent.parent
INSPECTION_MESSAGE='sandbox/OS 제한으로 QMD 프로세스 상태를 확인할 수 없어 자동 시작·재시작을 중단했습니다. 승인된 호스트 hook 또는 사용자 터미널에서 확인하세요.'

def root():
    import qmd_runtime
    return Path(os.environ.get('QMD_BACKEND_STATE_DIR') or
                qmd_runtime.managed_root().parent.parent/'backend')

def private_dir(path, *, create=False):
    if create and not path.exists():path.mkdir(mode=0o700,parents=True)
    info=path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
        raise ValueError('unsafe_qmd_endpoint_directory')
    if path.is_symlink():raise ValueError('unsafe_qmd_endpoint_directory')
    return path

def expected():
    import qmd_route,qmd_runtime,qmd_shared_store
    info=qmd_route.binary_info()
    return {'node':info['QMD_NODE_BIN'],'entry':info['QMD_ENTRY'],
        'runtime':qmd_runtime.select()['generation'],'store':qmd_shared_store.select()['generation']}

def process_command(pid):
    try:p=subprocess.run(['ps','-p',str(pid),'-o','command='],capture_output=True,text=True,timeout=2)
    except (OSError,subprocess.TimeoutExpired):raise ValueError('qmd_endpoint_identity_unavailable') from None
    if p.returncode:raise ValueError('qmd_endpoint_identity_unavailable')
    return p.stdout.strip()

def alive(pid):
    try:os.kill(pid,0);return True
    except ProcessLookupError:return False
    except PermissionError:raise ValueError('qmd_endpoint_identity_unavailable') from None

def bound(pid,port):
    try:
        p=subprocess.run(['lsof','-nP','-a','-p',str(pid),'-iTCP:'+str(port),'-sTCP:LISTEN','-Fn'],
            capture_output=True,text=True,timeout=2)
    except (OSError,subprocess.TimeoutExpired):raise ValueError('qmd_endpoint_identity_unavailable') from None
    if p.returncode not in (0,1):raise ValueError('qmd_endpoint_identity_unavailable')
    names=[line[1:] for line in p.stdout.splitlines() if line.startswith('n')]
    allowed={f'127.0.0.1:{port}':'127.0.0.1',f'[::1]:{port}':'::1'}
    if not names:return set()
    if any(name not in allowed for name in names):raise ValueError('qmd_endpoint_nonlocal_bind')
    return {allowed[name] for name in names}

def loopback_url(host,port):
    return f"http://{'[::1]' if host=='::1' else '127.0.0.1'}:{port}"

def healthy(port,url=None):
    try:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open((url or loopback_url('127.0.0.1',port))+'/health',timeout=.4) as response:
            body=response.read(65537)
            parsed=json.loads(body)
            return response.status==200 and len(body)<=65536 and isinstance(parsed,dict) and parsed.get('status')=='ok'
    except (OSError,ValueError):return False

def owned(row):
    if not alive(row['pid']):return False
    command=process_command(row['pid'])
    prefix=f"{row['node']} {row['entry']} mcp --http"
    if prefix not in command or not re.search(r' --port(?:=| )'+str(row['port'])+r'(?: |$)',command):
        raise ValueError('qmd_endpoint_identity_invalid')
    hosts=bound(row['pid'],row['port'])
    if row['url'] not in {loopback_url(host,row['port']) for host in hosts}:
        raise ValueError('qmd_endpoint_identity_invalid')
    return True

def raw():
    base=root()
    if not base.exists():return None
    private_dir(base)
    path=base/'endpoint.json'
    if not path.exists() and not path.is_symlink():return None
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077 or info.st_nlink!=1 or info.st_size>8192:
            raise ValueError('unsafe_qmd_endpoint_file')
        value=json.loads(os.read(fd,8193))
    finally:os.close(fd)
    keys={'schema','pid','port','url','node','entry','runtime','store'}
    if (not isinstance(value,dict) or set(value)!=keys or value['schema']!=SCHEMA
        or type(value['pid']) is not int or value['pid']<=1 or type(value['port']) is not int
        or not 1024<=value['port']<=65535 or not isinstance(value['url'],str)
        or value['url'] not in {loopback_url(host,value['port']) for host in ('127.0.0.1','::1')}
        or any(not isinstance(value[key],str) or not value[key] for key in ('node','entry','runtime','store'))):
        raise ValueError('qmd_endpoint_invalid')
    return value

def read():
    row=raw()
    if row is None:raise ValueError('qmd_endpoint_not_selected')
    if not alive(row['pid']):raise ValueError('qmd_endpoint_stale')
    if any(row[key]!=value for key,value in expected().items()):
        raise ValueError('qmd_endpoint_generation_conflict')
    if not owned(row):raise ValueError('qmd_endpoint_stale')
    return row

def daemon_url():
    if os.environ.get('QMD_SETUP_GUARD_FIXTURE')=='1':
        return os.environ.get('QMD_DAEMON_URL') or 'http://localhost:'+os.environ.get('QMD_DAEMON_PORT',str(DEFAULT_PORT))
    row=read()
    override=os.environ.get('QMD_DAEMON_URL')
    if override and override!=row['url']:raise ValueError('qmd_endpoint_url_conflict')
    return row['url']

def optional_url():
    try:return daemon_url()
    except (OSError,ValueError):return ''

@contextmanager
def locked():
    base=private_dir(root(),create=True)
    fd=os.open(base/'endpoint-start.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077 or info.st_nlink!=1:
            raise ValueError('unsafe_qmd_endpoint_lock')
        flags=fcntl.LOCK_EX if install_deadline.active() else fcntl.LOCK_EX|fcntl.LOCK_NB
        try:install_deadline.flock(fd,flags)
        except BlockingIOError:
            from context_learning.local_cycle import ResourceDeferred
            raise ResourceDeferred('qmd_startup_busy') from None
        yield base
    finally:os.close(fd)

def publish(base,row):
    fd,name=tempfile.mkstemp(prefix='.endpoint-',dir=base)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w') as output:
            json.dump(row,output,sort_keys=True);output.write('\n');output.flush();os.fsync(output.fileno())
        os.replace(name,base/'endpoint.json')
        directory=os.open(base,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:Path(name).unlink(missing_ok=True)

def child_environment(port):
    from runtime_cpu import environment
    env=environment({**os.environ,'QMD_DAEMON_PORT':str(port)})
    # A selected explicit CPU runtime must agree with the daemon's direct Node
    # entry. An auto runtime never installs a global/implicit CPU override.
    import qmd_runtime
    if qmd_runtime.select().get('forceCpu') is True:env['QMD_FORCE_CPU']='1'
    return env

def spawn(port,log):
    from runtime_cpu import lease,environment
    from context_learning.local_cycle import ResourceDeferred
    from runtime_admission import PreSendDeferred
    scope=lease()
    try:budget=scope.__enter__()
    except ResourceDeferred as exc:raise PreSendDeferred(str(exc),exc.report) from None
    try:
        return subprocess.Popen(['bash',str(ROOT/'backend/daemon.sh')],env=environment(child_environment(port)),
            cwd=ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,
            start_new_session=True,pass_fds=budget.fds)
    finally:scope.__exit__(None,None,None)

def limits():
    try:
        base=int(os.environ.get('QMD_DAEMON_PORT',str(DEFAULT_PORT)))
        attempts=int(os.environ.get('QMD_DAEMON_PORT_ATTEMPTS','16'))
        seconds=float(os.environ.get('QMD_DAEMON_READY_SECONDS','10'))
    except ValueError:raise ValueError('invalid_qmd_port_scan') from None
    if not 1024<=base<=65535 or not 1<=attempts<=128 or not .1<=seconds<=60 or base+attempts-1>65535:
        raise ValueError('invalid_qmd_port_scan')
    return base,attempts,seconds

def port_busy(port,*,owned_hosts=()):
    # Probe both loopback families without contacting or adopting listeners.
    # This is advisory: the child's actual bind still decides a start race.
    for family,host in ((socket.AF_INET,'127.0.0.1'),(socket.AF_INET6,'::1')):
        if host in owned_hosts:continue
        try:
            with socket.socket(family,socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                if family==socket.AF_INET6:probe.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
                probe.bind((host,port))
        except OSError as error:
            if error.errno==errno.EADDRINUSE:return True
            if family==socket.AF_INET6 and error.errno in (errno.EAFNOSUPPORT,errno.EPROTONOSUPPORT,errno.EADDRNOTAVAIL):continue
            raise ValueError('qmd_port_probe_unavailable') from None
    return False

def inspection_available():
    # Establish process-inspection capability before a child or endpoint can
    # be created. Denial is not evidence that a daemon is dead.
    alive(os.getpid())
    if not process_command(os.getpid()):raise ValueError('qmd_endpoint_identity_unavailable')

def ensure():
    inspection_available()
    with locked() as base:
        return ensure_locked(base)

def log_path():
    return root()/'daemon.log'

def ensure_locked(base):
    base_port,attempts,seconds=limits();spec=expected()
    row=raw()
    if row is not None and alive(row['pid']):
        if any(row[key]!=value for key,value in spec.items()):raise ValueError('qmd_endpoint_generation_conflict')
        if not owned(row):raise ValueError('qmd_endpoint_identity_invalid')
        if not healthy(row['port'],row['url']):raise ValueError('qmd_endpoint_unhealthy')
        return row
    from runtime_admission import startup
    with startup() as reservation:
        return _start_locked(base,spec,base_port,attempts,seconds,reservation.deadline)


def _start_locked(base,spec,base_port,attempts,seconds,startup_deadline):
    # One managed current log also covers bounded failed attempts. logrotate
    # reads this exact path, rather than leaving random per-start files.
    fd=os.open(log_path(),os.O_RDWR|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW,0o600)
    info=os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077 or info.st_nlink!=1:
        os.close(fd);raise ValueError('unsafe_qmd_endpoint_log')
    with os.fdopen(fd,'a+b') as log:
        for port in range(base_port,base_port+attempts):
            if time.monotonic()>=startup_deadline:raise ValueError('qmd_daemon_bind_health_timeout')
            if port_busy(port):continue
            log.seek(0,2);offset=log.tell()
            from runtime_admission import startup_pending,startup_settled,PreSendDeferred
            startup_pending()
            try:child=spawn(port,log)
            except (OSError,PreSendDeferred):
                startup_settled();raise
            published=False;collision=False
            candidate={'schema':SCHEMA,**spec,'pid':child.pid,'port':port,'url':loopback_url('127.0.0.1',port)}
            deadline=min(startup_deadline,time.monotonic()+seconds)
            try:
                while child.poll() is None and time.monotonic()<deadline:
                    hosts=bound(child.pid,port)
                    # A foreign listener in the other family can win after
                    # preflight. Retire only our new child and advance.
                    if hosts and port_busy(port,owned_hosts=hosts):collision=True;break
                    if hosts:candidate['url']=loopback_url(sorted(hosts)[0],port)
                    try:ready=bool(hosts) and owned(candidate) and healthy(port,candidate['url'])
                    except ValueError as error:
                        if str(error)=='qmd_endpoint_identity_invalid':ready=False
                        else:raise
                    if ready:
                        if expected()!=spec:raise ValueError('qmd_endpoint_generation_conflict')
                        publish(base,candidate)
                        pidfd,pidname=tempfile.mkstemp(prefix='.pid-',dir=base)
                        with os.fdopen(pidfd,'w') as pidout:
                            pidout.write(str(child.pid)+'\n');pidout.flush();os.fsync(pidout.fileno())
                        os.replace(pidname,base/f'daemon-{port}.pid')
                        published=True;startup_settled();return candidate
                    time.sleep(.05)
                if collision:continue
                if child.poll() is None:raise ValueError('qmd_daemon_bind_health_timeout')
                log.flush();log.seek(max(offset,os.fstat(log.fileno()).st_size-65536))
                error=log.read(65536).decode('utf8','replace')
                if 'EADDRINUSE' not in error and 'Address already in use' not in error:
                    raise ValueError('qmd_daemon_start_failed')
            finally:
                if child.poll() is None and not published:
                    child.terminate()
                    try:child.wait(timeout=2)
                    except subprocess.TimeoutExpired:child.kill();child.wait(timeout=2)
                    startup_settled()
                elif not published:startup_settled()
    raise ValueError('qmd_port_range_exhausted')

def attest_record(row):
    """Revalidate a prior generation only for explicit managed reload."""
    import qmd_runtime,qmd_shared_store,runtime_update
    runtime=Path(row['runtime']);generation=Path(row['store'])
    if runtime.parent!=qmd_runtime.managed_root()/'generations' or generation.parent!=qmd_shared_store.root()/'generations':
        raise ValueError('qmd_endpoint_previous_proof_invalid')
    prepared=qmd_runtime._prepared(runtime);qmd=Path(prepared['proof']['qmd'])
    entry=Path(prepared['entry']) if prepared.get('entry') else qmd.parent.parent/'dist/cli/qmd.js'
    if not entry.is_file() or entry.is_symlink():entry=qmd
    if row['node']!=prepared['proof']['node'] or row['entry']!=str(entry):
        raise ValueError('qmd_endpoint_previous_proof_invalid')
    qmd_shared_store._private_dir(generation)
    proof=json.loads(qmd_shared_store._private_file(generation/'prepared.json').read_text())
    index=qmd_shared_store._private_file(generation/'index.sqlite')
    if proof.get('schema')!=qmd_shared_store.SCHEMA or proof.get('generation')!=generation.name or runtime_update.inspect_index(index,include_file_hash=False).get('schemaSha256')!=proof.get('indexSchemaSha256'):
        raise ValueError('qmd_endpoint_previous_proof_invalid')

def reload():
    from runtime_admission import startup
    inspection_available()
    with locked() as base, startup() as reservation:
        expected()  # A missing/invalid new selection never authorizes a stop.
        row=raw()
        if row and alive(row['pid']):
            attest_record(row);owned(row)
            from runtime_admission import startup_pending,startup_settled
            startup_pending()
            try:os.kill(row['pid'],15)
            except PermissionError:raise ValueError('qmd_endpoint_identity_unavailable') from None
            except ProcessLookupError:pass
            deadline=min(reservation.deadline,time.monotonic()+limits()[2])
            while alive(row['pid']) and time.monotonic()<deadline:
                try:os.waitpid(row['pid'],os.WNOHANG)
                except ChildProcessError:pass
                time.sleep(.05)
            if alive(row['pid']):raise ValueError('qmd_endpoint_shutdown_timeout')
            startup_settled()
        return ensure_locked(base)

def assert_quiescent():
    row=raw()
    if row and alive(row['pid']):raise ValueError('qmd_daemon_must_be_quiescent')

def main(args):
    action=args[0]
    if action=='diagnose':
        inspection_available();print(json.dumps({'status':'process_inspection_available'}));return 0
    if action=='ensure':ensure();return 0
    if action=='reload':reload();return 0
    if action=='quiescent':assert_quiescent();return 0
    if action=='log':print(log_path());return 0
    if action=='identity':
        row=raw()
        if row is None or not alive(row['pid']):return 1
        row=read();print(str(row['pid'])+'\t'+process_command(row['pid']))
        return 0 if healthy(row['port'],row['url']) else 3
    row=read()
    if action=='port':print(row['port'])
    elif action=='url':print(daemon_url())
    elif action=='health':return 0 if healthy(row['port'],row['url']) else 1
    else:return 2
    return 0

if __name__=='__main__':
    try:raise SystemExit(main(sys.argv[1:]))
    except (OSError,ValueError,KeyError,IndexError) as error:
        message=str(error)
        if message=='qmd_endpoint_identity_unavailable':message+=': '+INSPECTION_MESSAGE
        from context_learning.local_cycle import ResourceDeferred
        print(message,file=sys.stderr);raise SystemExit(75 if isinstance(error,ResourceDeferred) else 2)

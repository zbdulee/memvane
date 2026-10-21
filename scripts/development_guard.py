#!/usr/bin/env python3
"""One user-wide development job, sampled tree budgets, private checkpoints.

This is a cooperative sampled guard, not an OS containment boundary. A process
that forks, detaches and exits between samples can escape ancestry observation.
Never use it as permission to start models or to bypass a sandbox denial.
"""
import argparse
from contextlib import AbstractContextManager
import fcntl
import json
import math
import os
from pathlib import Path
import pwd
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'core'))
from runtime_cpu import MAX_CPU_FRACTION,environment as cpu_environment

PROBE_DEADLINE=None

class GuardError(Exception):pass


class Policy:
    # Initial conservative proposals, not measured safe limits for this Mac.
    resource_limits=True
    min_available_percent=35
    max_tree_rss_fraction=.15
    max_tree_cpu_fraction=MAX_CPU_FRACTION
    max_swap_growth_fraction=.01
    max_load_per_cpu=.75
    max_processes=32


def private_dir(root):
    root=Path(root)
    if any(p.is_symlink() and p not in (Path('/tmp'),Path('/var')) for p in (root,*root.parents)):
        raise GuardError('unsafe_guard_directory')
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    info=root.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
        raise GuardError('unsafe_guard_directory')
    return root


class Lease(AbstractContextManager):
    """Same UID/path for tests, backup and hashing across all worktrees."""
    def __init__(self,root):self.root=Path(root);self.fd=None
    def __enter__(self):
        private_dir(self.root)
        try:
            self.fd=os.open(self.root/'heavy.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
            info=os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1 or info.st_mode&0o077:
                raise GuardError('unsafe_guard_lock')
            fcntl.flock(self.fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.__exit__(None,None,None);raise GuardError('development_job_busy')
        except OSError as exc:
            self.__exit__(None,None,None);raise GuardError('unsafe_guard_lock') from exc
        except BaseException:
            self.__exit__(None,None,None);raise
        return self
    def __exit__(self,*_):
        if self.fd is not None:os.close(self.fd);self.fd=None


def put(path,value):
    path=Path(path);private_dir(path.parent)
    if path.is_symlink():raise GuardError('unsafe_checkpoint')
    if path.exists():
        info=path.stat()
        if info.st_uid!=os.getuid() or info.st_nlink!=1 or not stat.S_ISREG(info.st_mode):
            raise GuardError('unsafe_checkpoint')
    fd,name=tempfile.mkstemp(prefix='.guard-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump(value,stream,sort_keys=True);stream.write('\n');stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
    finally:Path(name).unlink(missing_ok=True)


def checkpoint(root,value):put(Path(root)/'checkpoint.json',value)


def parse_processes(raw):
    rows=[]
    for line in raw.splitlines():
        if not line.strip():continue
        parts=line.split(None,10)
        if len(parts)<10:raise GuardError('process_monitor_unavailable')
        try:
            pid,ppid,pgid,rss=map(int,parts[:4]);cpu=float(parts[4])
            if pid<1 or rss<0 or cpu<0 or not math.isfinite(cpu):raise ValueError()
        except ValueError as exc:raise GuardError('process_monitor_unavailable') from exc
        rows.append({'pid':pid,'ppid':ppid,'pgid':pgid,'rss_bytes':rss*1024,
                     'cpu_percent':cpu,'start':' '.join(parts[5:10])})
    if not rows:raise GuardError('process_monitor_unavailable')
    return rows


def read_command(argv):
    try:
        remaining=PROBE_DEADLINE-time.monotonic() if PROBE_DEADLINE is not None else 3
        if remaining<=0:raise GuardError('wall_time_budget')
        result=subprocess.run(argv,stdin=subprocess.DEVNULL,capture_output=True,text=True,
                              timeout=min(3,remaining),env={**os.environ,'LC_ALL':'C'})
        if result.returncode or len(result.stdout)>2*1024*1024:raise GuardError('monitor_unavailable')
        return result.stdout
    except (OSError,subprocess.TimeoutExpired) as exc:raise GuardError('monitor_unavailable') from exc


def processes():
    return parse_processes(read_command(['/bin/ps','-axo','pid=,ppid=,pgid=,rss=,%cpu=,lstart=,comm=']))


def system_sample():
    if sys.platform=='darwin':
        total=int(read_command(['/usr/sbin/sysctl','-n','hw.memsize']).strip())
        pressure=int(read_command(['/usr/sbin/sysctl','-n','kern.memorystatus_vm_pressure_level']).strip())
        free=re.search(r'System-wide memory free percentage:\s*(\d+)%',read_command(['/usr/bin/memory_pressure','-Q']))
        swap=re.search(r'used\s*=\s*([\d.]+)([MG])',read_command(['/usr/sbin/sysctl','-n','vm.swapusage']))
        if free is None or swap is None or pressure not in (1,2,4):raise GuardError('monitor_unavailable')
        available=float(free[1]);used=float(swap[1])*(1024**(2 if swap[2]=='M' else 3))
        state={1:'normal',2:'warning',4:'critical'}[pressure]
    elif sys.platform.startswith('linux'):
        fields={line.split(':')[0]:int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.split(':')[0] in ('MemTotal','MemAvailable','SwapTotal','SwapFree')}
        total=fields['MemTotal'];available=fields['MemAvailable']/total*100
        used=fields['SwapTotal']-fields['SwapFree']
        psi=Path('/proc/pressure/memory').read_text()
        full=re.search(r'full avg10=([\d.]+)',psi)
        if full is None:raise GuardError('monitor_unavailable')
        state='warning' if float(full[1])>.1 else 'normal'
    else:raise GuardError('unsupported_resource_monitor')
    cpus=os.cpu_count()
    if not cpus:raise GuardError('monitor_unavailable')
    return {'total_bytes':total,'available_percent':available,'pressure':state,
            'swap_bytes':used,'load_per_cpu':os.getloadavg()[0]/cpus,'cpu_count':cpus}


class OwnedTree:
    def __init__(self,root_pid):
        self.root_pid=root_pid;self.identities={};self.group_open=True;self.established=False
    def observe(self,rows):
        by_pid={row['pid']:row for row in rows}
        owned={pid for pid,start in self.identities.items() if pid in by_pid and by_pid[pid]['start']==start}
        # Seed only the live root launched by Popen. Once all verified original
        # group anchors disappear, its numeric PGID is never trusted again.
        if not self.established and self.root_pid in by_pid and self.group_open:
            root=by_pid[self.root_pid]
            if root['pgid']==self.root_pid:
                owned.add(self.root_pid);self.established=True
        anchored=any(by_pid[pid]['pgid']==self.root_pid for pid in owned)
        if not anchored:self.group_open=False
        if self.group_open and anchored:
            for row in rows:
                if row['pgid']==self.root_pid and row['pid'] not in self.identities:
                    owned.add(row['pid'])
        changed=True
        while changed:
            changed=False
            for row in rows:
                if row['ppid'] in owned and row['pid'] not in owned:
                    if row['pid'] not in self.identities or self.identities[row['pid']]==row['start']:
                        owned.add(row['pid']);changed=True
        for pid in owned:self.identities.setdefault(pid,by_pid[pid]['start'])
        return {pid:by_pid[pid] for pid in owned}


def violation(policy,sample,rows,baseline_swap,*,calibration_policy=None,report=None):
    if len(rows)>policy.max_processes:return 'tree_process_count'
    # Mock/regression tests keep ownership, serialization and wall-time cleanup,
    # but do not inherit admission budgets intended for actual model work.
    if not policy.resource_limits:return None
    keys=('total_bytes','available_percent','swap_bytes','load_per_cpu','cpu_count')
    if any(type(sample.get(k)) not in (int,float) or not math.isfinite(sample[k]) for k in keys):return 'monitor_unavailable'
    if sample['total_bytes']<=0 or sample['cpu_count']<=0 or not 0<=sample['available_percent']<=100:return 'monitor_unavailable'
    if sample.get('pressure') not in ('normal','warning','critical'):return 'monitor_unavailable'
    if calibration_policy is None:
        if sample['pressure']!='normal':return 'os_memory_pressure'
        if sample['available_percent']<policy.min_available_percent:return 'relative_available'
    else:
        if sample['pressure']=='critical':return 'os_memory_pressure'
        from context_learning.bounded_calibration import verdict
        scoped=verdict(report,calibration_policy)
        if not scoped['ready']:return scoped['reason']
    if sample['swap_bytes']-baseline_swap>sample['total_bytes']*policy.max_swap_growth_fraction:return 'swap_growth'
    if sample['load_per_cpu']>policy.max_load_per_cpu:return 'system_load'
    if sum(p['rss_bytes'] for p in rows)>sample['total_bytes']*policy.max_tree_rss_fraction:return 'tree_rss'
    if sum(p['cpu_percent'] for p in rows)>sample['cpu_count']*100*policy.max_tree_cpu_fraction:return 'tree_cpu'
    return None


def signal_owned(tree,rows,sig,send=os.kill):
    current={p['pid']:p for p in rows}
    # Never signal a group, a name, our own process, or a reused PID.
    for pid,start in sorted(tree.identities.items(),reverse=True):
        if pid<=1 or pid in (os.getpid(),os.getppid()):continue
        if pid in current and current[pid]['start']==start:
            try:send(pid,sig)
            except ProcessLookupError:pass


def command_for(kind,argv):
    if not argv:raise GuardError('missing_command')
    if kind not in ('test','model-test'):return argv
    if Path(argv[0]).name!='node' or '--test' not in argv:raise GuardError('test_requires_direct_node_test')
    result=list(argv)
    for i,arg in enumerate(result):
        if arg.startswith('--test-concurrency=') and arg!='--test-concurrency=1':raise GuardError('test_concurrency_must_be_one')
        if arg=='--test-concurrency':
            if i+1>=len(result) or result[i+1]!='1':raise GuardError('test_concurrency_must_be_one')
    if not any(arg.startswith('--test-concurrency') for arg in result):result.insert(result.index('--test')+1,'--test-concurrency=1')
    return result


def observe_log(run,system,owned,elapsed):
    row={'elapsed_seconds':round(elapsed,2),'system':system,'owned':list(owned.values()),
         'tree_rss_bytes':sum(p['rss_bytes'] for p in owned.values()),
         'tree_lifetime_cpu_percent':sum(p['cpu_percent'] for p in owned.values())}
    with (run/'samples.jsonl').open('a') as stream:stream.write(json.dumps(row,sort_keys=True)+'\n')
    (run/'samples.jsonl').chmod(0o600)


def cleanup(tree,child):
    global PROBE_DEADLINE
    previous_deadline=PROBE_DEADLINE
    PROBE_DEADLINE=time.monotonic()+10
    def send(pid,sig):
        # Refresh immediately before each signal, never act on an old sample.
        current={r['pid']:r for r in processes()}
        if pid in current and current[pid]['start']==tree.identities[pid]:os.kill(pid,sig)
    try:
        for sig in (signal.SIGTERM,signal.SIGKILL):
            rows=processes();tree.observe(rows);signal_owned(tree,rows,sig,send)
            time.sleep(sleep_budget(.3,PROBE_DEADLINE-time.monotonic()))
            child.poll()
            if not tree.observe(processes()):return tree.established
        return False
    finally:PROBE_DEADLINE=previous_deadline


def execution_hold():
    hold=Path(__file__).resolve().parents[1]/'.development-hold.json'
    return hold.exists() and json.loads(hold.read_text()).get('hold') is True


def sleep_budget(interval,remaining):return max(0,min(interval,remaining))


def validate_budgets(interval,duration):
    if not math.isfinite(interval) or not 2<=interval<=10 or not math.isfinite(duration) or not 0<duration<=1800:
        raise GuardError('finite_duration_and_sampling_interval_2_to_10_required')


def state_root():
    home=Path(pwd.getpwuid(os.getuid()).pw_dir)
    if sys.platform=='darwin':return home/'Library/Logs/qmd-development-guard'
    return home/'.local/state/qmd-development-guard'


def run_job(args,root):
    global PROBE_DEADLINE
    command=command_for(args.kind,args.command)
    if execution_hold():raise GuardError('work_remains_on_hold')
    with Lease(root) as lease:
        calibration=None;calibration_policy=None
        if getattr(args,'calibration_envelope',None):
            from context_learning.bounded_calibration import load
            if args.kind not in ('test','model-test') or args.max_seconds>30:raise GuardError('calibration_requires_short_test')
            calibration=load(args.calibration_envelope,command)
            calibration_policy={**calibration['policy'],'calibration':calibration}
        policy=Policy()
        policy.resource_limits=args.kind!='test' or calibration is not None
        active=root/'active.json'
        if active.exists() and not json.loads(active.read_text()).get('settled',False):
            raise GuardError('previous_job_unsettled_review_checkpoint')
        run=private_dir(root/'runs'/uuid.uuid4().hex)
        state={'status':'preflight','completed':False,'settled':False,'kind':args.kind,
               'resourceLimits':policy.resource_limits,'run':str(run)}
        if args.kind=='model-test':state['proofNonce']=secrets.token_hex(24)
        if calibration is not None:state['calibrationRunId']=calibration['runId']
        def save():checkpoint(run,state);put(active,state)
        save();child=None;tree=None;interrupted=[];calibration_fd=None
        original_calibration=os.environ.get('QMD_CALIBRATION_ENVELOPE')
        began=time.monotonic();PROBE_DEADLINE=began+args.max_seconds
        previous={sig:signal.getsignal(sig) for sig in (signal.SIGINT,signal.SIGTERM)}
        for sig in previous:signal.signal(sig,lambda signum,_frame:interrupted.append(signum))
        try:
            if calibration is not None:
                from context_learning.bounded_calibration import _sha
                claims=private_dir(root/'calibrations');nonce=uuid.uuid4().hex
                try:claim=os.open(claims/(calibration['runId']+'.json'),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                except FileExistsError:raise GuardError('calibration_already_consumed') from None
                with os.fdopen(claim,'w') as stream:
                    json.dump({'run':str(run),'expiresMono':calibration['expiresMono'],'nonce':nonce,
                        'envelopePath':str(Path(args.calibration_envelope).resolve()),'envelopeSha256':_sha(calibration)},stream)
                    stream.flush();os.fsync(stream.fileno())
                calibration_fd=os.open(claims/(calibration['runId']+'.lock'),os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                fcntl.flock(calibration_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                directory=os.open(claims,os.O_RDONLY)
                try:os.fsync(directory)
                finally:os.close(directory)
                state['calibrationNonce']=nonce;save()
                os.environ['QMD_CALIBRATION_ENVELOPE']=str(Path(args.calibration_envelope).resolve())
            baseline=system_sample();processes()
            def scoped_report(admission=False):
                if calibration_policy is None:return None
                from context_learning.shared import resource_report
                return resource_report(calibration_policy,detailed=True,admission=admission)
            report=scoped_report(admission=True)
            reason=violation(policy,baseline,[],baseline['swap_bytes'],calibration_policy=calibration_policy,report=report)
            if report is not None:
                baseline['calibration_available_mib']=report['available_mib']
                if report['ready'] is not True:reason=reason or 'calibration_admission_budget'
            observe_log(run,baseline,{},0)
            if reason:raise GuardError(reason)
            environment=cpu_environment()
            if args.kind=='model-test':
                environment['QMD_DEVELOPMENT_GUARD_RUN']=str(run)
                environment['QMD_DEVELOPMENT_GUARD_NONCE']=state['proofNonce']
            if interrupted:raise GuardError('interrupted')
            if time.monotonic()>=PROBE_DEADLINE:raise GuardError('wall_time_budget')
            child=subprocess.Popen(command,start_new_session=True,env=environment,pass_fds=(lease.fd,))
            tree=OwnedTree(child.pid);state.update(status='running',pid=child.pid);save()
            while True:
                owned=tree.observe(processes());sample=system_sample();elapsed=time.monotonic()-began
                report=scoped_report()
                if report is not None:sample['calibration_available_mib']=report['available_mib']
                observe_log(run,sample,owned,elapsed)
                reason=violation(policy,sample,list(owned.values()),baseline['swap_bytes'],calibration_policy=calibration_policy,report=report)
                if interrupted:reason='interrupted'
                elif elapsed>=args.max_seconds:reason='wall_time_budget'
                if reason:
                    settled=cleanup(tree,child)
                    state.update(status='stopped',reason=reason,settled=settled);save();return 75
                code=child.poll()
                if code is not None and not owned:
                    if not tree.established:
                        state.update(status='stopped',reason='root_identity_unconfirmed',settled=False);save();return 75
                    state.update(status='passed' if code==0 else 'failed',exitCode=code,
                                 completed=code==0,settled=True);save();return code
                time.sleep(sleep_budget(args.sample_seconds,PROBE_DEADLINE-time.monotonic()))
        except BaseException as exc:
            settled=child is None
            if tree is not None:
                try:settled=cleanup(tree,child)
                except Exception:settled=False
            state.update(status='stopped',reason=str(exc) if isinstance(exc,GuardError) else type(exc).__name__,settled=settled)
            save();return 75
        finally:
            PROBE_DEADLINE=None
            if calibration_fd is not None:os.close(calibration_fd)
            if original_calibration is None:os.environ.pop('QMD_CALIBRATION_ENVELOPE',None)
            else:os.environ['QMD_CALIBRATION_ENVELOPE']=original_calibration
            for sig,handler in previous.items():signal.signal(sig,handler)
            print(json.dumps({key:value for key,value in state.items() if key!='proofNonce'},
                             sort_keys=True),flush=True)


def main():
    global PROBE_DEADLINE
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind',choices=('test','model-test','backup','hash'),default='test')
    parser.add_argument('--sample-only',action='store_true')
    parser.add_argument('--sample-seconds',type=float,default=5)
    parser.add_argument('--max-seconds',type=float,default=900)
    parser.add_argument('--calibration-envelope',help='explicit private one-shot synthetic trial; never ordinary tests')
    parser.add_argument('command',nargs=argparse.REMAINDER)
    args=parser.parse_args()
    if args.command[:1]==['--']:args.command=args.command[1:]
    try:
        validate_budgets(args.sample_seconds,args.max_seconds)
        root=state_root()
        if args.sample_only:
            if args.max_seconds>30:raise GuardError('sample_only_requires_at_most_30_seconds')
            with Lease(root):
                started=time.monotonic();PROBE_DEADLINE=started+args.max_seconds
                run=private_dir(root/'runs'/('sampler-'+uuid.uuid4().hex));count=0
                checkpoint(run,{'kind':'sampler','completed':False})
                while time.monotonic()<PROBE_DEADLINE:
                    sample=system_sample();rows=processes();count+=1
                    owned={r['pid']:r for r in rows if r['pid']==os.getpid()}
                    observe_log(run,sample,owned,time.monotonic()-started)
                    print(json.dumps({'elapsed':round(time.monotonic()-started,2),'system':sample,
                         'sampler_rss_bytes':sum(p['rss_bytes'] for p in owned.values()),
                         'sampler_cpu_percent':sum(p['cpu_percent'] for p in owned.values())}),flush=True)
                    time.sleep(sleep_budget(args.sample_seconds,PROBE_DEADLINE-time.monotonic()))
                checkpoint(run,{'kind':'sampler','completed':True,'samples':count})
                print(json.dumps({'kind':'sampler','samples':count,'record':str(run)}),flush=True)
                PROBE_DEADLINE=None
            return 0
        return run_job(args,root)
    except (GuardError,OSError,ValueError,KeyError) as exc:
        print(json.dumps({'status':'refused','completed':False,'reason':str(exc) if isinstance(exc,GuardError) else type(exc).__name__}),flush=True)
        return 75


if __name__=='__main__':raise SystemExit(main())

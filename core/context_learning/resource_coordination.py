"""Owner-only kernel leases: inference demand first, one bounded trainer.

No PID signaling, TTL lease stealing, model selection or service registration.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
import math
import os
from pathlib import Path
import time
from . import resident
from .local_cycle import ResourceDeferred, _atomic_json
from .dataset import read_private_json

SLOTS=64
MAX_OBSERVATION_AGE=2
RECOVERY_SECONDS=5
RECOVERY_MAX_GAP=7
_COMPUTE_OWNER=ContextVar('qmd_compute_owner',default=None)
_INFERENCE_OWNER=ContextVar('qmd_inference_owner',default=None)
_STARTUP_OWNER=ContextVar('qmd_startup_owner',default=None)


def _lock(path):
    fd=resident._private_lock(path)
    if os.fstat(fd).st_nlink!=1:
        os.close(fd);raise ValueError('unsafe_coordination_lock')
    return fd


def _present(path):
    # One non-following probe includes dangling links. Only absence is ignored;
    # permission and other OS errors must still refuse admission.
    try:os.lstat(path)
    except FileNotFoundError:return False
    return True


def default_root():
    from .inference_service import service_root
    return service_root(create=True)


def observation_fresh(report,*,now=None):
    if not isinstance(report,dict):return False
    observed=report.get('observed_at_mono')
    now=time.monotonic() if now is None else now
    return (type(observed) in (int,float) and math.isfinite(observed)
            and 0<=now-observed<=MAX_OBSERVATION_AGE)


def decision(policy,report,*,resident_credit=0,now=None):
    from resource_platform import normal_pressure,number
    from .bounded_calibration import pressure_ready
    result={'ready':False,'reason':'resource_unknown','microbatch_size':None}
    if not observation_fresh(report,now=now):return {**result,'reason':'resource_observation_stale'}
    total,available,load=report.get('total_mib'),report.get('available_mib'),report.get('load_per_cpu')
    if (report.get('ready') is not True or any(type(x)not in(int,float) or not math.isfinite(x) for x in (total,available,load))
            or total<=0 or available<0 or load<0):return result
    load_limit=policy.get('maxLoadPerCpu',1)
    if not number(load_limit) or not number(resident_credit):return result
    if load>load_limit:return {**result,'reason':'cpu_pressure'}
    # Credit only measured resident allocation, never inferred from a timer.
    if not pressure_ready(report,policy,now=now):
        return {**result,'reason':'os_memory_pressure'}
    if 'adaptiveMemory' in policy:
        from .memory_policy import assess
        admission=assess(report,policy,resident_credit=resident_credit,
            phase='loaded_self' if resident_credit>0 else 'cold',now=now)
        if not admission['ready']:return {**result,'reason':admission['reason'],'memory_admission':admission}
        size=admission['training_limits']['microbatchSize']
        limits=admission['training_limits']
    else:
        reserve=policy.get('minAvailableMib',768);peak=policy.get('maxRssMib')
        target_fraction=policy.get('targetHeadroomFraction',.35)
        if not number(reserve) or not number(peak) or peak<=0 or not number(target_fraction) or target_fraction>1:
            return {**result,'reason':'memory_budget_unknown'}
        extra=peak-min(resident_credit,peak)
        admission={'reserve_mib':reserve,'reserve_source':'configured_absolute_floor',
            'estimated_peak_mib':peak,'required_additional_mib':extra,
            'estimate_source':'configured_max_rss_proxy','uncertainty':'not_measured; upper-bound proxy, not a minimum',
            'target_headroom_mib':math.ceil(total*target_fraction),
            'target_met':available-extra>=total*target_fraction,'target_role':'soft_expansion_goal'}
        if available<reserve+extra:return {**result,'reason':'memory_reserve','memory_admission':admission}
        # No measured shape/profile means no automatic expansion to batch2.
        size,limits=1,None
    if load>load_limit*.75:size=1
    if limits is not None:limits={**limits,'microbatchSize':size}
    return {'ready':True,'reason':'admitted','microbatch_size':size,'training_limits':limits,'memory_admission':admission}


class Coordinator:
    def __init__(self,root=None,*,clock=time.monotonic,wall=time.time):
        self.root=resident._private_state(Path(root) if root is not None else default_root())
        self.clock,self.wall=clock,wall

    def startup_barrier(self):
        cleanup=self.root/'cleanup-reservation.json'
        if _present(cleanup):
            raise ResourceDeferred('qmd_cleanup_recovery_required')
        path=self.root/'startup-reservation.json'
        if not _present(path):return
        try:row=read_private_json(path)
        except (OSError,ValueError,TypeError):raise ResourceDeferred('qmd_startup_recovery_required') from None
        owner=_STARTUP_OWNER.get()
        if (owner is None or owner[0]!=os.getpid() or owner[1]!=str(self.root)
                or row!={'schema':1,'owner_pid':owner[0],'nonce':owner[2]}):
            raise ResourceDeferred('qmd_startup_recovery_required')

    def cleanup_unknown(self,owned_pid):
        """Durable stop after unproven owned-group cleanup; no timed reclaim.

        Never overwrite an earlier reservation. A partial/invalid file also
        blocks all common admission, until manual proof resolves its state.
        """
        from .store import canonical
        path=self.root/'cleanup-reservation.json'
        try:fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        except FileExistsError:return
        with os.fdopen(fd,'w') as stream:
            stream.write(canonical({'schema':1,'owner_pid':os.getpid(),
                'owned_group':owned_pid,'reason':'owned_child_cleanup_unavailable'})+'\n')
            stream.flush();os.fsync(stream.fileno())
        directory=os.open(self.root,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)

    def owned_compute(self):
        owner=_COMPUTE_OWNER.get()
        if owner is None or owner['pid']!=os.getpid() or owner['root']!=str(self.root):return None
        try:
            actual=os.fstat(owner['fd']);expected=(self.root/'compute.lock').stat()
            if (actual.st_dev,actual.st_ino)!=(expected.st_dev,expected.st_ino):
                raise ResourceDeferred('compute_lease_changed')
        except OSError:raise ResourceDeferred('compute_lease_changed') from None
        return owner

    def owned_inference(self):
        owner=_INFERENCE_OWNER.get()
        if owner is None or owner['pid']!=os.getpid() or owner['root']!=str(self.root):return None
        try:
            actual=os.fstat(owner['fd']);expected=(self.root/f"inference-{owner['slot']}.lock").stat()
            if (actual.st_dev,actual.st_ino)!=(expected.st_dev,expected.st_ino):
                raise ResourceDeferred('inference_lease_changed')
        except OSError:raise ResourceDeferred('inference_lease_changed') from None
        return owner

    def check_owned_preparation(self,policy,report,owner):
        if self.owned_compute() is not owner:raise ResourceDeferred('compute_lease_changed')
        with self._gate('control.lock'):
            self.startup_barrier()
            if not owner['foreground'] and self.demand():raise ResourceDeferred('learning_deferred_inference')
            result=self._admit(policy,report)
            if not result['ready']:raise ResourceDeferred('learning_deferred_resource',result)

    def _admit(self,policy,report,*,resident_credit=0):
        """Called only under control.lock; one recovery latch across sessions."""
        result=decision(policy,report,resident_credit=resident_credit,now=self.clock())
        path=self.root/'resource-recovery.json'
        try:
            state=read_private_json(path) if _present(path) else {'paused':False}
            if not isinstance(state,dict) or type(state.get('paused')) is not bool:raise ValueError('invalid_recovery_state')
        except (OSError,ValueError,TypeError):raise ResourceDeferred('resource_recovery_unknown',{}) from None
        def save(value):
            if value!=state:
                _atomic_json(path,value)
                fd=os.open(self.root,os.O_RDONLY)
                try:os.fsync(fd)
                finally:os.close(fd)
        paused={'paused':True,'stable_since':None,'wall_since':None,'last_observed':None}
        if not result['ready']:
            save(paused);return result
        if not state['paused']:return result
        # Different recovery thresholds prevent threshold-edge restart flapping.
        if report['load_per_cpu']>policy.get('maxLoadPerCpu',1)*.65:
            save(paused);return {**result,'ready':False,'reason':'resource_recovery_hysteresis'}
        since,wall_since,last=state.get('stable_since'),state.get('wall_since'),state.get('last_observed')
        valid=all(type(x) in (int,float) and math.isfinite(x) for x in (since,wall_since,last))
        now,wall=self.clock(),self.wall()
        observed=report['observed_at_mono']
        if (not valid or now<since or observed<last or observed-last>RECOVERY_MAX_GAP
                or abs((wall-wall_since)-(now-since))>2):
            save({'paused':True,'stable_since':observed,'wall_since':wall-(now-observed),'last_observed':observed})
            return {**result,'ready':False,'reason':'resource_recovery_hysteresis'}
        if observed-since<RECOVERY_SECONDS:
            save({**state,'last_observed':observed})
            return {**result,'ready':False,'reason':'resource_recovery_hysteresis'}
        save({'paused':False});return result

    def check(self,policy,report):
        with self._gate('control.lock'):
            self.startup_barrier()
            if self.demand():return {'ready':False,'reason':'learning_deferred_inference'}
            return self._admit(policy,report)

    @contextmanager
    def _gate(self,name,*,deadline=None):
        fd=_lock(self.root/name)
        token=None
        try:
            while True:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if deadline is None:raise ResourceDeferred('learning_compute_busy')
                    if time.monotonic()>=deadline:raise ResourceDeferred('learning_inference_deadline')
                    time.sleep(min(.005,max(0,deadline-time.monotonic())))
            if name=='compute.lock':
                # Recheck AFTER acquisition: startup may have won the race
                # while a foreground caller waited for this kernel lease.
                self.startup_barrier()
                token=_COMPUTE_OWNER.set({'pid':os.getpid(),'root':str(self.root),'fd':fd,'deadline':deadline,
                    'foreground':deadline is not None or self.owned_inference() is not None})
            yield fd
        finally:
            if token is not None:_COMPUTE_OWNER.reset(token)
            os.close(fd)

    def demand(self):
        base=os.fspath(self.root)
        for n in range(SLOTS):
            path=os.path.join(base,f'inference-{n}.lock')
            if not _present(path):continue
            fd=_lock(Path(path))
            try:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:return True
            finally:os.close(fd)
        return False

    @contextmanager
    def inference(self,deadline,namespace):
        fd=None;token=None
        try:
            with self._gate('control.lock',deadline=deadline):
                for n in range(SLOTS):
                    candidate=_lock(self.root/f'inference-{n}.lock')
                    try:fcntl.flock(candidate,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:os.close(candidate);continue
                    fd=candidate
                    # Hashed namespace only, never prompts or job/case bodies.
                    from .contracts import digest
                    _atomic_json(self.root/f'inference-{n}.json',{'namespace':digest(namespace),'deadline_epoch':time.time()+max(0,deadline-time.monotonic())})
                    break
                if fd is None:raise ResourceDeferred('learning_inference_capacity')
            token=_INFERENCE_OWNER.set({'pid':os.getpid(),'root':str(self.root),'fd':fd,'slot':n})
            yield
        finally:
            if token is not None:_INFERENCE_OWNER.reset(token)
            if fd is not None:os.close(fd)

    @contextmanager
    def inference_compute(self,deadline):
        with self._gate('compute.lock',deadline=deadline):yield

    @contextmanager
    def preparation_step(self,policy,report):
        # Only the real current slot owner can ignore its own advertised demand.
        if self.owned_inference() is None:raise ResourceDeferred('inference_lease_missing')
        with self._gate('control.lock'):
            self.startup_barrier()
            result=self._admit(policy,report)
            if not result['ready']:raise ResourceDeferred('learning_deferred_resource',result)
            gate=self._gate('compute.lock');gate.__enter__()
        try:yield result
        finally:gate.__exit__(None,None,None)

    @contextmanager
    def training(self,policy,report):
        try:
            with self._gate('trainer.lock') as fd:
                with self._gate('control.lock'):
                    self.startup_barrier()
                    if self.demand():raise ResourceDeferred('learning_deferred_inference')
                    report=self._admit(policy,report)
                    if not report['ready']:raise ResourceDeferred('learning_deferred_resource',report)
                yield fd,report
        except ResourceDeferred:raise

    @contextmanager
    def step(self,policy,report,*,resident_credit=0,with_fd=False):
        with self._gate('control.lock'):
            self.startup_barrier()
            if self.demand():raise ResourceDeferred('learning_deferred_inference')
            admission=self._admit(policy,report,resident_credit=resident_credit)
            if not admission['ready']:raise ResourceDeferred('learning_deferred_resource',admission)
            # Grab under control mutex, then release mutex during one update.
            gate=self._gate('compute.lock')
            fd=gate.__enter__()
        try:yield (admission,fd) if with_fd else admission
        finally:gate.__exit__(None,None,None)

    @contextmanager
    def activity(self,lane,policy,report):
        """Nonblocking admission: remote waits never own inference compute.

        A cold start can satisfy the caller advertising foreground demand. It
        still needs fresh resource evidence and an immediately free compute lane.
        """
        if lane not in ('provider','startup'):raise ValueError('invalid_resource_lane')
        with self._gate('control.lock'):
            self.startup_barrier()
            if lane!='startup' and self.demand():raise ResourceDeferred('learning_deferred_inference')
            admission=self._admit(policy,report)
            if not admission['ready']:raise ResourceDeferred('learning_deferred_resource',admission)
            gate=self._gate('provider.lock' if lane=='provider' else 'compute.lock')
            fd=gate.__enter__()
        try:yield fd
        finally:gate.__exit__(None,None,None)


def admission_policy(policy):
    return {'minAvailableMib':policy.get('minAvailableMib',768),
            'maxLoadPerCpu':policy.get('maxLoadPerCpu',1),
            **{k:policy[k] for k in ('maxRssMib','adaptiveMemory','targetHeadroomFraction','calibration') if k in policy}}


class TrainingGuard:
    """Used only by an explicitly coordinated local trainer request."""
    def __init__(self,control):
        if (not isinstance(control,dict) or set(control)!={'schema','root','policy','namespace','job_id'}
                or control['schema']!=1 or control['root']!=str(default_root())):
            raise ValueError('learning_control_invalid')
        self.controller=Coordinator(Path(control['root']))
        self.policy=control['policy'];self.report=None;self.next_probe=0;self.resident_credit=0
        # Only the inherited OS lease can authorize a child as this trainer.
        try:fd=int(os.environ['QMD_TRAINER_COORD_FD']);info=os.fstat(fd)
        except (OSError,ValueError,KeyError):raise ValueError('learning_lease_missing') from None
        expected=(self.controller.root/'trainer.lock').stat()
        if (info.st_dev,info.st_ino)!=(expected.st_dev,expected.st_ino):raise ValueError('learning_lease_changed')
        self.fd=fd
        try:self.stop_fd=int(os.environ['QMD_TRAINER_STOP_FD']) if os.environ.get('QMD_TRAINER_STOP_FD') else None
        except ValueError:raise ValueError('learning_control_invalid') from None

    def poll(self):
        from .training_watch import poll_stop
        poll_stop(self.stop_fd)
        if self.controller.demand():raise ValueError('learning_yield')

    def mark_loaded(self,before,after):
        if any(type(x)not in(int,float) or not math.isfinite(x) or x<0 for x in (*before,*after)):
            raise ValueError('training_memory_unavailable')
        self.resident_credit=max(0,after[0]-before[0],after[1]-before[1])
        self.report=None;self.next_probe=0

    @contextmanager
    def step(self):
        from .shared import resource_report
        # Demand is always checked, even when OS measurement is briefly reused.
        self.poll()
        now=time.monotonic()
        if self.report is None or now>=self.next_probe:
            self.report=resource_report(self.policy,detailed=True,admission=False);self.next_probe=now+1
        try:
            with self.controller.step(self.policy,self.report,resident_credit=self.resident_credit) as limits:yield limits
        except ResourceDeferred as exc:raise ValueError('learning_yield') from exc

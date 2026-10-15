"""Owner-only kernel leases: inference demand first, one bounded trainer.

No PID signaling, TTL lease stealing, model selection or service registration.
"""
from contextlib import contextmanager
import fcntl
import math
import os
from pathlib import Path
import time
from . import resident
from .local_cycle import ResourceDeferred, _atomic_json
from .dataset import read_private_json

SLOTS=64


def _lock(path):
    fd=resident._private_lock(path)
    if os.fstat(fd).st_nlink!=1:
        os.close(fd);raise ValueError('unsafe_coordination_lock')
    return fd


def default_root():
    from .inference_service import service_root
    return service_root(create=True)


def decision(policy,report,*,resident_credit=0):
    result={'ready':False,'reason':'resource_unknown','microbatch_size':None}
    total,available,load=report.get('total_mib'),report.get('available_mib'),report.get('load_per_cpu')
    if (report.get('ready') is not True or any(type(x)not in(int,float) or not math.isfinite(x) for x in (total,available,load))
            or total<=0 or available<0 or load<0):return result
    if load>policy.get('maxLoadPerCpu',1):return {**result,'reason':'cpu_pressure'}
    if not math.isfinite(resident_credit) or resident_credit<0:return result
    # Credit only measured resident allocation, never inferred from a timer.
    reserve=max(policy.get('minAvailableMib',768),math.ceil(total*.1))
    if available<reserve:return {**result,'reason':'memory_reserve'}
    if 'adaptiveMemory' in policy:
        from .memory_policy import assess
        credit=min(resident_credit,policy['adaptiveMemory']['fixedMib'])
        admission=assess({**report,'available_mib':available+credit},policy)
        if not admission['ready']:return {**result,'reason':admission['reason']}
        size=admission['training_limits']['microbatchSize']
        limits=admission['training_limits']
    else:
        if available<reserve+policy['maxRssMib']-min(resident_credit,policy['maxRssMib']):return {**result,'reason':'memory_reserve'}
        size,limits=2,None
    if load>policy.get('maxLoadPerCpu',1)*.75:size=1
    if limits is not None:limits={**limits,'microbatchSize':size}
    return {'ready':True,'reason':'admitted','microbatch_size':size,'training_limits':limits}


class Coordinator:
    def __init__(self,root=None):
        self.root=resident._private_state(Path(root) if root is not None else default_root())

    @contextmanager
    def _gate(self,name,*,deadline=None):
        fd=_lock(self.root/name)
        try:
            while True:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if deadline is None:raise ResourceDeferred('learning_compute_busy')
                    if time.monotonic()>=deadline:raise ResourceDeferred('learning_inference_deadline')
                    time.sleep(min(.005,max(0,deadline-time.monotonic())))
            yield fd
        finally:os.close(fd)

    def demand(self):
        for n in range(SLOTS):
            path=self.root/f'inference-{n}.lock'
            if not path.exists() and not path.is_symlink():continue
            fd=_lock(path)
            try:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:return True
            finally:os.close(fd)
        return False

    @contextmanager
    def inference(self,deadline,namespace):
        fd=None
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
            yield
        finally:
            if fd is not None:os.close(fd)

    @contextmanager
    def inference_compute(self,deadline):
        with self._gate('compute.lock',deadline=deadline):yield

    @contextmanager
    def training(self,policy,report):
        try:
            with self._gate('trainer.lock') as fd:
                with self._gate('control.lock'):
                    if self.demand():raise ResourceDeferred('learning_deferred_inference')
                    report=decision(policy,report)
                    if not report['ready']:raise ResourceDeferred('learning_deferred_resource',report)
                yield fd,report
        except ResourceDeferred:raise

    @contextmanager
    def step(self,policy,report,*,resident_credit=0):
        with self._gate('control.lock'):
            if self.demand():raise ResourceDeferred('learning_deferred_inference')
            admission=decision(policy,report,resident_credit=resident_credit)
            if not admission['ready']:raise ResourceDeferred('learning_deferred_resource',admission)
            # Grab under control mutex, then release mutex during one update.
            gate=self._gate('compute.lock')
            fd=gate.__enter__()
        try:yield admission
        finally:gate.__exit__(None,None,None)


def admission_policy(policy):
    return {'minAvailableMib':policy.get('minAvailableMib',768),
            'maxLoadPerCpu':policy.get('maxLoadPerCpu',1),
            **{k:policy[k] for k in ('maxRssMib','adaptiveMemory') if k in policy}}


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

    def mark_loaded(self,before,after):
        if any(type(x)not in(int,float) or not math.isfinite(x) or x<0 for x in (*before,*after)):
            raise ValueError('training_memory_unavailable')
        self.resident_credit=max(0,after[0]-before[0],after[1]-before[1])
        self.report=None;self.next_probe=0

    @contextmanager
    def step(self):
        from .shared import resource_report
        # Demand is always checked, even when OS measurement is briefly reused.
        if self.controller.demand():raise ValueError('learning_yield')
        now=time.monotonic()
        if self.report is None or now>=self.next_probe:
            self.report=resource_report(self.policy,detailed=True,admission=False);self.next_probe=now+.25
        try:
            with self.controller.step(self.policy,self.report,resident_credit=self.resident_credit) as limits:yield limits
        except ResourceDeferred as exc:raise ValueError('learning_yield') from exc

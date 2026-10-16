"""Bounded lifecycle/preparation and a UID provider lane; no OS service."""
from contextlib import contextmanager,nullcontext
from contextvars import ContextVar
from functools import wraps
import fcntl
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import uuid

from context_learning.resource_coordination import Coordinator,_STARTUP_OWNER
from context_learning.local_cycle import ResourceDeferred

POLICY={'maxRssMib':512,'minAvailableMib':768,'maxLoadPerCpu':1,'targetHeadroomFraction':.35}
START_POLICY={**POLICY,'maxRssMib':2048}
_PROVIDER=ContextVar('qmd_provider_lease',default=None)
_PREPARATION=ContextVar('qmd_preparation',default=None)
_STARTUP=ContextVar('qmd_startup',default=None)


class PreSendDeferred(ResourceDeferred):
    """This launch did not call Popen; never used to reclassify a sent request."""


def sample(policy,*,deadline=None):
    from context_learning.shared import resource_report
    try:return resource_report(policy,detailed=True,admission=False,deadline=deadline)
    except (OSError,ValueError,TypeError,KeyError):return None


def _fixture():return os.environ.get('QMD_SETUP_GUARD_FIXTURE')=='1'


@contextmanager
def provider():
    """One cross-project remote CLI; an inherited kernel lease avoids recursion."""
    if not _fixture() and Coordinator().owned_compute() is not None:
        raise ResourceDeferred('provider_inside_compute')
    active=_PROVIDER.get()
    if active is not None:yield active;return
    if _fixture():yield None;return
    controller=Coordinator()
    inherited=os.environ.get('QMD_PROVIDER_COORD_FD')
    if inherited:
        try:
            fd=os.dup(int(inherited));info=os.fstat(fd)
            expected=(controller.root/'provider.lock').lstat()
            if (not stat.S_ISREG(expected.st_mode) or expected.st_uid!=os.getuid()
                    or expected.st_mode&0o077 or expected.st_nlink!=1
                    or (info.st_dev,info.st_ino)!=(expected.st_dev,expected.st_ino)):
                raise ResourceDeferred('provider_lease_invalid')
            # Matching an inode alone is not a held-lease proof. A second open
            # must observe contention, while the inherited open description
            # itself must already own (or be able to reuse) that exact lock.
            from context_learning.resource_coordination import _lock
            probe=_lock(controller.root/'provider.lock')
            try:
                try:fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:pass
                else:raise ResourceDeferred('provider_lease_invalid')
            finally:os.close(probe)
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except (OSError,ValueError):
            if 'fd' in locals():os.close(fd)
            raise ResourceDeferred('provider_lease_invalid') from None
        try:
            token=_PROVIDER.set(fd)
            try:yield fd
            finally:_PROVIDER.reset(token)
        finally:os.close(fd)
        return
    with controller.activity('provider',POLICY,sample(POLICY)) as fd:
        token=_PROVIDER.set(fd)
        try:yield fd
        finally:_PROVIDER.reset(token)


def provider_checkpoint():
    """Call before a new attempt reservation, never reinterpret a sent request."""
    if _fixture():return
    result=Coordinator().check(POLICY,sample(POLICY))
    if not result['ready']:raise ResourceDeferred('learning_deferred_resource',result)


def provider_options(environment=None):
    fd=_PROVIDER.get()
    from runtime_cpu import environment as cpu_environment
    env=cpu_environment(environment)
    env.pop('QMD_PROVIDER_COORD_FD',None)
    if fd is not None:env['QMD_PROVIDER_COORD_FD']=str(fd)
    return {'env':env,'pass_fds':() if fd is None else (fd,)}


def provider_job(function):
    """Refusal before source/budget mutation remains pending and retryable."""
    @wraps(function)
    def run(*args,**kwargs):
        try:
            with provider():return function(*args,**kwargs)
        except ResourceDeferred as exc:
            return {'status':'deferred_resource','reason':str(exc),'outcomes':[],
                    'cli_invocations':0}
    return run


def provider_call(function):
    @wraps(function)
    def run(*args,**kwargs):
        with provider():return function(*args,**kwargs)
    return run


def provider_popen(argv,*,before_launch=None,launch_failed=None,defer_launch=None,**options):
    """Fresh check at the actual launch, including inherited/nested providers."""
    from runtime_cpu import lease
    with provider():
        marked=False
        scope=nullcontext(None) if _fixture() else lease(Coordinator().root)
        try:
            budget=scope.__enter__()
        except ResourceDeferred as exc:
            if defer_launch is not None:defer_launch()
            raise PreSendDeferred(str(exc),exc.report) from None
        try:
            try:
                provider_checkpoint()
                if before_launch is not None:before_launch();marked=True
                # A durable dispatch-intent write may itself have taken time.
                provider_checkpoint()
            except ResourceDeferred as exc:
                if defer_launch is not None:defer_launch()
                elif marked and launch_failed is not None:launch_failed()
                raise PreSendDeferred(str(exc),exc.report) from None
            environment=options.pop('env',None)
            cpu_options=provider_options(environment)
            cpu_options['pass_fds']=(*options.pop('pass_fds',()),*cpu_options['pass_fds'],
                                    *(budget.fds if budget is not None else ()))
            try:return subprocess.Popen(argv,**options,**cpu_options)
            except OSError:
                if launch_failed is not None:launch_failed()
                raise
        finally:scope.__exit__(None,None,None)


def provider_run(argv,**options):
    """Bounded owned group; preserve the caller's routing/environment exactly."""
    from context_learning.transport import _stop_owned
    with provider():
        timeout=options.pop('timeout',120)
        if type(timeout) not in (int,float) or not 0<timeout<=600:
            raise ValueError('invalid_provider_timeout')
        data=options.pop('input',None)
        capture=options.pop('capture_output',False)
        check=options.pop('check',False)
        if options.pop('shell',False):raise ValueError('provider_shell_forbidden')
        if capture:options.update(stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        if data is not None:options['stdin']=subprocess.PIPE
        process=provider_popen(argv,**options,start_new_session=True)
        try:
            out,err=process.communicate(data,timeout=timeout)
            result=subprocess.CompletedProcess(argv,process.returncode,out,err)
            if check:result.check_returncode()
            return result
        finally:
            _stop_owned(process)
            for stream in (process.stdin,process.stdout,process.stderr):
                if stream is not None:stream.close()


@contextmanager
def startup():
    # The daemon must NOT inherit compute: only the bounded startup parent owns
    # it, up to health proof/cleanup, never for the daemon's service lifetime.
    active=_STARTUP.get()
    if active is not None:yield active;return
    controller=Coordinator()
    if _fixture():
        reservation=_StartupReservation(None);yield reservation;return
    with controller.activity('startup',START_POLICY,sample(START_POLICY)):
        reservation=_StartupReservation(controller)
        token=_STARTUP.set(reservation)
        owner=_STARTUP_OWNER.set((os.getpid(),str(controller.root),reservation.nonce))
        try:
            reservation.begin();yield reservation
        finally:
            try:reservation.finish()
            finally:_STARTUP.reset(token);_STARTUP_OWNER.reset(owner)


class _StartupReservation:
    """Durable barrier survives parent loss; no TTL or blind orphan adoption."""
    def __init__(self,controller):
        self.controller=controller;self.deadline=time.monotonic()+10
        self.nonce=uuid.uuid4().hex;self.safe=True;self.begun=False
    def begin(self):
        from context_learning.local_cycle import _atomic_json
        with self.controller._gate('control.lock'):
            self.controller.startup_barrier()
            _atomic_json(self.controller.root/'startup-reservation.json',
                {'schema':1,'owner_pid':os.getpid(),'nonce':self.nonce})
            fd=os.open(self.controller.root,os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)
            self.begun=True
    def pending(self):self.safe=False
    def settled(self):self.safe=True
    def finish(self):
        if self.controller is None or not self.begun or not self.safe:return
        with self.controller._gate('control.lock'):
            self.controller.startup_barrier()
            (self.controller.root/'startup-reservation.json').unlink()
            fd=os.open(self.controller.root,os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)


def startup_pending():
    if _STARTUP.get() is not None:_STARTUP.get().pending()


def startup_settled():
    if _STARTUP.get() is not None:_STARTUP.get().settled()


class _Preparation:
    def __init__(self,clock,seconds):
        self.clock=clock;self.deadline=clock()+seconds;self.gate=None;self.report=None;self.probe_at=None
        self.controller=Coordinator();self.borrowed=self.controller.owned_compute()
        self.foreground=self.controller.owned_inference() is not None
        if self.foreground:
            import hook_budget
            self.deadline=min(self.deadline,clock()+1,hook_budget.deadline())
        if self.borrowed and self.borrowed['deadline'] is not None:
            self.deadline=min(self.deadline,self.borrowed['deadline'])
    def close(self):
        gate,self.gate=self.gate,None
        if gate is not None:gate.__exit__(None,None,None)
    def checkpoint(self):
        self.close()
        if self.clock()>=self.deadline:raise ResourceDeferred('preparation_budget')
        now=self.clock()
        if self.probe_at is None or now-self.probe_at>=.5:
            self.report=sample(POLICY,deadline=self.deadline);self.probe_at=now
        if self.clock()>=self.deadline:raise ResourceDeferred('preparation_budget')
        if self.borrowed is not None:
            self.controller.check_owned_preparation(POLICY,self.report,self.borrowed);return
        self.gate=(self.controller.preparation_step if self.foreground else self.controller.step)(POLICY,self.report)
        try:self.gate.__enter__()
        except BaseException:self.gate=None;raise


@contextmanager
def preparation(*,clock=time.monotonic,max_seconds=30):
    """Reentrant only inside this thread's owned short compute preparation."""
    if _fixture():yield None;return
    active=_PREPARATION.get()
    if active is not None:active.checkpoint();yield active;return
    if not 0<max_seconds<=30:raise ValueError('invalid_preparation_budget')
    active=_Preparation(clock,max_seconds);token=_PREPARATION.set(active)
    try:
        active.checkpoint();yield active
    finally:
        active.close();_PREPARATION.reset(token)


def preparation_checkpoint():
    active=_PREPARATION.get()
    if active is not None:active.checkpoint()


def prepared(function):
    @wraps(function)
    def run(*args,**kwargs):
        with preparation():return function(*args,**kwargs)
    return run


def hash_file(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as source:
        while True:
            preparation_checkpoint()
            block=source.read(1024*1024)
            if not block:break
            value.update(block)
    return value.hexdigest()


def optional_warm():
    try:
        with Coordinator().step(POLICY,sample(POLICY)):
            # QMD has no attested server cancellation/deadline API. Client
            # timeout cannot release ownership of still-running server work.
            return {'status':'deferred_resource','reason':'warm_server_deadline_unavailable'}
    except ResourceDeferred as exc:
        return {'status':'deferred_resource','reason':str(exc)}


if __name__=='__main__':
    if sys.argv[1:]==['warm']:
        import json
        print(json.dumps(optional_warm()));raise SystemExit(75)
    raise SystemExit(2)

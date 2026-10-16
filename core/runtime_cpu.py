"""Managed numerical parallelism shares, NOT OS CPU/GPU containment.

Pure stdlib at import: never import torch/numpy or inspect model weights here.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import math
import os
from resource_controls import CpuThreads

MAX_CPU_FRACTION=.25
_TORCH_CONFIGURED=None


def _defer(reason):
    from context_learning.local_cycle import ResourceDeferred
    return ResourceDeferred(reason)


def plan(*,cpu_count=None):
    count=os.cpu_count() if cpu_count is None else cpu_count
    if type(count)is not int or not 1<=count<=1024:
        raise _defer('cpu_capacity_unavailable')
    capacity=math.floor(count*MAX_CPU_FRACTION)
    if capacity<1:raise _defer('cpu_capacity_unavailable')
    return {'cpu_count':count,'capacity':capacity,'threads':1,
            'aggregate_cpu_percent':count*100*MAX_CPU_FRACTION}


def environment(base=None):
    return CpuThreads.environment(base)


@dataclass(frozen=True)
class Budget:
    fds:tuple
    threads:int=1


@contextmanager
def lease(root=None):
    """One private kernel share per child; inherited FD keeps it after spawn.

    Fixed one-thread pools conservatively consume one nominal CPU share. This
    does not assert that a process, multiple pools or a native driver use one
    core. Denied/unknown locks are not reclaimed or treated as free capacity.
    """
    if os.environ.get('QMD_SETUP_GUARD_FIXTURE')=='1':
        yield Budget(());return
    from context_learning.resource_coordination import Coordinator,_lock
    controller=Coordinator(root);policy=plan();held=None
    try:
        with controller._gate('cpu-control.lock'):
            for slot in range(policy['capacity']):
                fd=_lock(controller.root/f'cpu-share-{slot}.lock')
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:os.close(fd);continue
                except BaseException:os.close(fd);raise
                held=fd;break
            if held is None:raise _defer('cpu_budget_busy')
        yield Budget((held,))
    finally:
        if held is not None:os.close(held)


def configure_torch(torch):
    """Once per process/module, before eager/JIT/autograd/model construction."""
    global _TORCH_CONFIGURED
    if os.environ.get('QMD_CPU_THREADS','1')!='1':
        raise ValueError('cpu_thread_configuration_invalid')
    key=(os.getpid(),torch)
    try:
        if _TORCH_CONFIGURED!=key:
            CpuThreads.apply(torch)
        CpuThreads.verify(torch)
    except (RuntimeError,AttributeError,TypeError,ValueError) as exc:
        raise ValueError('cpu_thread_configuration_unavailable') from exc
    _TORCH_CONFIGURED=key

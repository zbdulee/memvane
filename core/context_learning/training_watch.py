"""Sampled running-work decisions and owned-child cleanup; never PID discovery."""
import math
import os
import select
import signal
import stat
import subprocess
import time

from .resource_coordination import observation_fresh

YIELD_GRACE=2.
PROBE_INTERVAL=1.
PROBE_BUDGET=.5
STATS_BUDGET=.25
STOP_WAIT=.5


class Watch:
    def __init__(self,*,clock=time.monotonic,max_load=1,min_available_mib=768,policy=None):
        self.clock=clock;self.max_load=max_load;self.min_available_mib=min_available_mib;self.yielded_at=None
        self.policy=policy or {}
    def observe(self,report,*,foreground=False,cpu_percent=None,cpu_limit=None):
        now=self.clock()
        if not observation_fresh(report,now=now):return {'action':'kill','reason':'monitor_unavailable'}
        total,available,load=(report.get(k) for k in ('total_mib','available_mib','load_per_cpu'))
        pressure=report.get('memory_pressure');status=report.get('query_status')
        if (any(type(n)not in(int,float) or not math.isfinite(n) or n<0 for n in (total,available,load))
                or total<=0 or not isinstance(pressure,dict) or not isinstance(status,dict)
                or status.get('memory_pressure')!='ok'
                or pressure.get('level') not in ('normal','warning','critical')):
            return {'action':'kill','reason':'monitor_unavailable'}
        free=pressure.get('system_free_percent')
        if free is not None:
            if type(free)not in(int,float) or not math.isfinite(free) or not 0<=free<=100:
                return {'action':'kill','reason':'monitor_unavailable'}
        else:
            psi=pressure.get('psi_full_avg10')
            if type(psi)not in(int,float) or not math.isfinite(psi) or psi<0:
                return {'action':'kill','reason':'monitor_unavailable'}
        if pressure['level']=='critical' or free is not None and free<=5 or available<256:
            return {'action':'kill','reason':'critical_memory_pressure'}
        calibrated='calibration' in self.policy
        if calibrated:
            from .bounded_calibration import verdict
            scoped=verdict(report,self.policy,now=now)
            if not scoped['ready']:return {'action':'kill','reason':scoped['reason']}
        reserve=self.min_available_mib
        budget=report.get('memory_admission')
        budget_reserve=budget.get('reserve_mib',reserve) if isinstance(budget,dict) else reserve
        if any(type(n)not in(int,float) or not math.isfinite(n) or n<0 for n in (reserve,budget_reserve)):
            return {'action':'kill','reason':'monitor_unavailable'}
        reserve=max(reserve,budget_reserve)
        cpu_pressure=(cpu_percent is not None and cpu_limit is not None and cpu_percent>cpu_limit)
        routine=(foreground or cpu_pressure or pressure['level']=='warning' and not calibrated or available<reserve
                 or load>self.max_load or report.get('ready') is not True)
        if routine and self.yielded_at is None:self.yielded_at=now
        if self.yielded_at is not None:
            return {'action':'terminate' if now-self.yielded_at>=YIELD_GRACE else 'yield',
                    'reason':'foreground_demand' if foreground else 'process_group_cpu_budget' if cpu_pressure else 'running_resource_pressure'}
        return {'action':'continue','reason':'running_ready'}


def poll_stop(fd):
    if fd is None:return
    try:
        if not stat.S_ISFIFO(os.fstat(fd).st_mode):raise ValueError('learning_control_invalid')
        if select.select([fd],[],[],0)[0]:
            os.read(fd,1)  # A stop byte OR parent EOF is a one-way yield request.
            raise ValueError('learning_yield')
    except OSError:raise ValueError('learning_yield') from None


def stop_owned(process,*,emergency=False):
    """Reap the owned leader AND prove its new process group is absent.

    Exited leaders can leave descendants behind. Denied identity or a lingering
    group is unknown, never successful cleanup. No foreign PID discovery.
    """
    if type(process.pid)is not int or process.pid<=1:raise ValueError('invalid_owned_trainer')
    try:
        try:os.killpg(process.pid,signal.SIGKILL if emergency else signal.SIGTERM)
        except ProcessLookupError:
            process.wait(timeout=STOP_WAIT);return True
        try:process.wait(timeout=STOP_WAIT)
        except subprocess.TimeoutExpired:
            if emergency: return False
        # Leader exit alone says nothing about the remaining group members.
        if not emergency:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:
                process.wait(timeout=STOP_WAIT);return True
        process.wait(timeout=STOP_WAIT)
        deadline=time.monotonic()+STOP_WAIT
        while True:
            try:os.killpg(process.pid,0)
            except ProcessLookupError:return True
            if time.monotonic()>=deadline:return False
            time.sleep(.01)
    except (OSError,subprocess.TimeoutExpired):return False

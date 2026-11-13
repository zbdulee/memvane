"""Sampled running-work decisions and owned-child cleanup; never PID discovery."""
import math
import os
import select
import stat
import time

from memvane.runtime.api import resource as runtime_resource; observation_fresh = runtime_resource.observation_fresh

YIELD_GRACE=2.
PROBE_INTERVAL=1.
PROBE_BUDGET=.5
STATS_BUDGET=.25


class Watch:
    def __init__(self,*,clock=time.monotonic,max_load=1,min_available_mib=768,policy=None):
        self.clock=clock;self.max_load=max_load;self.min_available_mib=min_available_mib;self.yielded_at=None
        self.urgent_reason=None;self.urgent_since=None
        self.policy=policy or {}
    def observe(self,report,*,foreground=False,priority_work=False,cpu_percent=None,cpu_limit=None):
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
            from memvane.runtime.api import resource as runtime_resource; verdict = runtime_resource.calibration_verdict
            scoped=verdict(report,self.policy,now=now)
            if not scoped['ready']:return {'action':'kill','reason':scoped['reason']}
        reserve=self.min_available_mib
        budget=report.get('memory_admission')
        budget_reserve=budget.get('reserve_mib',reserve) if isinstance(budget,dict) else reserve
        if any(type(n)not in(int,float) or not math.isfinite(n) or n<0 for n in (reserve,budget_reserve)):
            return {'action':'kill','reason':'monitor_unavailable'}
        reserve=max(reserve,budget_reserve)
        cpu_pressure=(cpu_percent is not None and cpu_limit is not None and cpu_percent>cpu_limit)
        urgent=(foreground or cpu_pressure or pressure['level']=='warning' and not calibrated or available<reserve
                or load>self.max_load or report.get('ready') is not True)
        routine=priority_work or urgent
        if routine and self.yielded_at is None:self.yielded_at=now
        if urgent:
            if self.urgent_since is None:self.urgent_since=now
            self.urgent_reason=('foreground_demand' if foreground else
                                'process_group_cpu_budget' if cpu_pressure else
                                'running_resource_pressure')
        if self.yielded_at is not None:
            # A queued index/update/Laya sync is a cooperative checkpoint request. It
            # must not kill an optimizer group before the head/cursor commit.
            return {'action':'terminate' if self.urgent_since is not None and now-self.urgent_since>=YIELD_GRACE else 'yield',
                    'reason':self.urgent_reason or 'priority_index_work'}
        return {'action':'continue','reason':'running_ready'}


def poll_stop(fd):
    if fd is None:return
    try:
        if not stat.S_ISFIFO(os.fstat(fd).st_mode):raise ValueError('learning_control_invalid')
        if select.select([fd],[],[],0)[0]:
            os.read(fd,1)  # A stop byte OR parent EOF is a one-way yield request.
            raise ValueError('learning_yield')
    except OSError:raise ValueError('learning_yield') from None



"""Injectable OS observations; no model imports or new privileged interfaces.

Policy must distinguish unsupported/unknown from observed zero. Linux/WSL data
is preserved without pretending that it supplies macOS percentages or host GPU
visibility. Windows support is a declaration, not a new implementation.
"""
import math
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
from resource_controls import capabilities


def number(value):
    return type(value)in(int,float) and math.isfinite(value) and value>=0


def normal_pressure(report):
    status=report.get('query_status');pressure=report.get('memory_pressure')
    if (not isinstance(status,dict) or status.get('memory_pressure')!='ok'
            or not isinstance(pressure,dict) or pressure.get('level')!='normal'):return False
    if 'system_free_percent' in pressure:
        value=pressure['system_free_percent']
        return number(value) and 10<=value<=100
    if 'psi_full_avg10' in pressure:
        value=pressure['psi_full_avg10']
        return number(value) and value<1
    return False


class ObservationAdapter:
    kind='unsupported'
    def __init__(self,os_name,machine,release):
        self.os_name,self.machine,self.release=os_name,machine,release
    def describe(self):
        return {'os':self.os_name,'architecture':self.machine,'support':'unsupported',
                'scope':'unknown','memory_model':'unknown',**capabilities()}
    def observe(self,*,path_factory=Path,run=subprocess.run,clock=time.monotonic,
                cpu_count=os.cpu_count,loadavg=getattr(os,'getloadavg',None),
                deadline=None,detailed=False):
        observed=clock();end=min(observed+2,deadline) if deadline is not None else observed+2
        status={'load':'unavailable','available':'unavailable','total':'unavailable',
                'memory_pressure':'unavailable'}
        row={'observed_at_mono':observed,'load_per_cpu':None,'available_mib':None,
             'total_mib':None,'swap_used_mib':None,'memory_pressure':None,
             'gpu_usage_percent':None,'thermal_state':None,'query_status':status,
             'platform':self.describe(),'gpu':'mps-possible' if self.kind=='darwin' else 'unknown'}
        if self.kind=='unsupported':return row
        count=cpu_count()
        if type(count)is int and count>0 and loadavg is not None:
            try:
                value=loadavg()[0]/count
                if number(value):row['load_per_cpu']=round(value,3);status['load']='ok'
            except (OSError,AttributeError,TypeError,ValueError):pass
        def query(argv):
            remaining=end-clock()
            if remaining<=0:return '', 'deadline'
            try:
                result=run(argv,capture_output=True,text=True,timeout=min(2,remaining),check=False)
                return (result.stdout,'ok') if result.returncode==0 else ('','failed')
            except (OSError,subprocess.TimeoutExpired):return '', 'failed'
        if self.kind=='darwin':self._mac(row,status,query,detailed)
        else:self._linux(row,status,path_factory,detailed)
        return row

    @staticmethod
    def _mac(row,status,query,detailed):
        raw,state=query(['/usr/bin/vm_stat'])
        page=re.search(r'page size of (\d+) bytes',raw)
        counts={name:int(value.replace('.','')) for name,value in
                re.findall(r'Pages (free|inactive|speculative):\s*([0-9.]+)',raw)}
        if page and set(counts)=={'free','inactive','speculative'}:
            row['available_mib']=sum(counts.values())*int(page[1])//(1024*1024);status['available']='ok'
        else:status['available']=state if state!='ok' else 'unrecognized'
        raw,state=query(['/usr/sbin/sysctl','-n','hw.memsize'])
        if raw.strip().isdigit() and int(raw)>0:
            row['total_mib']=int(raw)//(1024*1024);status['total']='ok'
        else:status['total']=state if state!='ok' else 'unrecognized'
        raw,state=query(['/usr/bin/memory_pressure','-Q'])
        match=re.search(r'System-wide (?:memory )?free percentage:\s*(\d+)%',raw)
        if match and 0<=int(match[1])<=100:
            row['memory_pressure']={'system_free_percent':int(match[1])};status['memory_pressure']='ok'
        else:status['memory_pressure']=state if state!='ok' else 'unrecognized'
        raw,state=query(['/usr/sbin/sysctl','-n','kern.memorystatus_vm_pressure_level'])
        level={'1':'normal','2':'warning','4':'critical'}.get(raw.strip())
        if isinstance(row['memory_pressure'],dict):row['memory_pressure']['level']=level
        if level is None:status['memory_pressure']=state if state!='ok' else 'unrecognized'
        if detailed:
            raw,_=query(['/usr/sbin/sysctl','-n','vm.swapusage'])
            match=re.search(r'used\s*=\s*([0-9.]+)M',raw)
            if match:row['swap_used_mib']=float(match[1])

    @staticmethod
    def _linux(row,status,path_factory,detailed):
        try:
            values={parts[0].rstrip(':'):int(parts[1]) for line in path_factory('/proc/meminfo').read_text().splitlines()
                    if len(parts:=line.split())>=2 and parts[1].isdigit()}
            if 'MemAvailable' in values:row['available_mib']=values['MemAvailable']//1024;status['available']='ok'
            if 'MemTotal' in values:row['total_mib']=values['MemTotal']//1024;status['total']='ok'
            if detailed and 'SwapTotal' in values and 'SwapFree' in values:
                row['swap_used_mib']=(values['SwapTotal']-values['SwapFree'])//1024
            pressure=path_factory('/proc/pressure/memory').read_text()
            match=re.search(r'full avg10=([0-9.]+)',pressure)
            if match:
                value=float(match[1]);row['memory_pressure']={'level':'normal' if value<1 else 'warning','psi_full_avg10':value}
                status['memory_pressure']='ok'
        except (OSError,KeyError,ValueError):pass


class MacOSAdapter(ObservationAdapter):
    kind='darwin'
    def describe(self):
        return {**super().describe(),'support':'partial','scope':'host',
                'memory_model':'unified' if self.machine in ('arm64','aarch64') else 'unknown',
                'validation':'prior MacBook synthetic scope; current model path unverified'}


class LinuxAdapter(ObservationAdapter):
    kind='linux'
    def describe(self):
        wsl='microsoft' in self.release.lower()
        return {**super().describe(),'support':'partial','scope':'guest' if wsl else 'host',
                'environment':'wsl' if wsl else 'linux','validation':'unverified on this task',
                'gpu_backend':'current adapter selects CPU; no CUDA path'}


def adapter_for(os_name=None,*,machine=None,release=None):
    name=sys.platform if os_name is None else os_name
    machine=platform.machine() if machine is None else machine
    release=platform.release() if release is None else release
    cls=MacOSAdapter if name=='darwin' else LinuxAdapter if name.startswith('linux') else ObservationAdapter
    return cls(name,machine,release)

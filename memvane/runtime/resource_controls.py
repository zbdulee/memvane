"""Library controls are separate from OS telemetry, admission and OS quotas."""
import os


class CpuThreads:
    @staticmethod
    def environment(base=None):
        env=dict(os.environ if base is None else base)
        for key in ('OMP_NUM_THREADS','MKL_NUM_THREADS','OPENBLAS_NUM_THREADS',
                    'OPENBLAS_DEFAULT_NUM_THREADS','VECLIB_MAXIMUM_THREADS',
                    'NUMEXPR_NUM_THREADS','RAYON_NUM_THREADS'):
            env[key]='1'
        env.update(QMD_CPU_THREADS='1',OMP_MAX_ACTIVE_LEVELS='1',OMP_DYNAMIC='FALSE',
                   MKL_DYNAMIC='FALSE',TOKENIZERS_PARALLELISM='false')
        return env

    @staticmethod
    def apply(torch):
        torch.set_num_interop_threads(1)
        torch.set_num_threads(1)

    @staticmethod
    def verify(torch):
        values=(torch.get_num_threads(),torch.get_num_interop_threads())
        if any(type(value)is not int or value!=1 for value in values):
            raise ValueError('cpu_thread_configuration_unavailable')


class MpsMemoryControl:
    """Own-process allocation check, not GPU utilization/free VRAM/thermal data.

    The allocator-limit API exists but is deliberately NOT applied here. These
    counters overlap host memory on Apple Silicon and must not be added to RSS.
    """
    @staticmethod
    def check(torch,limits):
        try:
            used=torch.mps.driver_allocated_memory()
            recommended=torch.mps.recommended_max_memory()
            fraction=limits['reserveFraction']
            if (type(used)is not int or used<0 or type(recommended)is not int
                    or recommended<=0 or type(fraction)not in(int,float)
                    or not .10<=fraction<=.50):
                raise ValueError('training_memory_unavailable')
        except Exception as exc:raise ValueError('training_memory_unavailable') from exc
        if used>recommended*(1-fraction):raise ValueError('training_memory_budget')


def capabilities():
    return {'cpu_threads':'library_soft_control','hard_cpu_quota':False,
            'hard_gpu_compute_quota':False,'gpu_allocator_limit':'not_applied',
            'gpu_utilization':'unknown','thermal':'unknown'}

# Runtime CPU thread propagation

Goal: repair missing explicit CPU parallelism controls within the approved
source/mock scope. No model execution, host stress, installation or activation.

Use one pure standard-library CPU policy for the development aggregate fraction
(25%) and managed child configuration. Each numeric child requests one nominal
CPU share; owner-private kernel slots admit at most floor(logical CPUs*0.25)
shares across trainer, Laya daemon, QMD daemon/operation and provider Popen paths. Child-inherited
descriptors retain reservations after a parent returns. Missing capacity defers;
there is no timed slot stealing. These are configuration reservations, not OS
CPU quotas: a process can create multiple native pools or GPU driver threads.

Before dependency import, pass OMP/MKL/OpenBLAS limits and disable tokenizer
parallelism. Installed NumPy is scipy-openblas (MAX_THREADS64); installed
PyTorch2.14 `_torch_docs.py` documents CPU intra/inter-op setters, with intra-op
before eager/JIT/autograd and inter-op only once before parallel work. The
adapter must explicitly set/read back both APIs before model construction,
with process-local idempotence and fail-closed configuration errors. No torch
import occurs in the parent policy helper or these mock tests.

Keep per-group observed CPU monitoring separate: a trainer exceeding its
allocated 100%-per-share allowance requests the existing cooperative yield,
then bounded owned-group cleanup. Development aggregate observation remains
25% of host CPU capacity. Neither sampling nor library settings prove a hard
CPU/GPU cap. Existing inference demand, system pressure, checkpoint/ACK and
unknown-cleanup protection remain binding.

Steps: (1) red tests for global slot contention/lifetime, invalid topology,
environment sanitization, Torch API order/idempotence/failure and child launch
propagation; (2) minimal helper and wiring; (3) affected small sequential checks
under the shared development lease; (4) read-only scope review and limitations.
Do not commit/push or expand into real load verification after completion.

Execution status:117 mocked/analytical cases passed sequentially under the
existing shared development lock:CPU19, running-watch25, entrypoints32,
hardening10, development-guard16, adaptive-memory14, actual-adapter analytical
resume1. Ten affected sources parsed. No real child, Torch/model import, host
probe, native workload or GPU operation was started in this follow-up.
Independent read-only review and the final fixture-isolation recheck found no
remaining P1/P2 in this scope (conditional source approval). No commit/push or
operating change. Existing fixture mode returns before default UID root lookup;
operating processes must never set that test flag.

Remaining scope: QMD/native binaries may not honor these numerical-library
controls or may close inherited descriptors; retaining a share throughout
their actual lifecycle has not been verified. Raw externally started tools,
unregistered descendants and arbitrary development commands are not contained
by this nominal share pool. The development guard retains its own observed
tree CPU threshold; using the same fraction is not a global observed CPU meter.
GPU/MPS and system library parallelism are not capped by Torch CPU setters.
A persistent daemon conservatively retains its share while idle; small machines
may defer other jobs rather than oversubscribe configured shares. With fewer
than4 or unknown logical CPUs, no full nominal share fits and launch defers.
At4–7logical CPUs, QMD alone can occupy the only share; at8–11, QMD+Laya can
occupy both. Work stays deferred until a share is released, even if OS pressure
recovers. This policy never evicts existing user daemons or raises its fraction.

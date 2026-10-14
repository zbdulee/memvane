# Preserving installer lock contention

An explicit install could wait indefinitely for a project, bootstrap, registry,
queue, runtime selection or endpoint lock. A live writer fence was also reported
as `recovery_required` despite having made no unsafe transition.

Use one in-process deadline context for the preserving installer and shared-store
CLI. Nested blocking flocks try nonblocking acquisition until the same budget
expires. Keep ordinary hook/worker locking unchanged outside that context, and
never terminate an existing holder. A busy lock returns `deferred` (exit 75);
permission denial and unsafe state still fail closed. If cleanup of a partially
published activation is busy, retain the pointer and require explicit recovery.

This bounds lock waits. It does not impose a hard wall-time limit on every native
operation, propagate an environment override into workers, or change the older
topical/shadow migration APIs. An index writer that outlives its controller budget
continues; the controller records deferred work for an identical approved retry.

Regression coverage uses disposable settings, DBs, lock files and synthetic
models. Real flock contention covers project/bootstrap/registry/queue/runtime/
endpoint locks; a live writer marker must survive defer; retries must succeed
after its owner releases the lock. Partial activation cleanup contention retains
the runtime pointer and `recovery_required` journal. Denial remains an error.

V2 routing currently supports macOS arm64. Unsupported hosts preserve their
existing global QMD data and remain `setup_required`; a global CLI does not enable
a production fallback. Correct public setup guidance rather than relax routing.

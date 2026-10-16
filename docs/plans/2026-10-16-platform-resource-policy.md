# Common resource policy and platform adapters

User approval (2026-10-16 06:23:31UTC) covers source structure, small mocked
checks and independent review. Real models/training, OS load tests, installs,
operating changes, new permissions/services, main and push remain held.

Evidence: microbatch1's historical3209MiB budget is
ceil(2633.515625*1.10)+ceil(249.09375*1.25)=2897+312. The cold maximum includes a
failed reload;249.09375 is whole update/checkpoint RSS growth (696.765625 minus
447.671875), not a universal linear per-pair measurement. Training's2601.09375
lifetime RSS peak includes loading and cannot be added as separate training
memory. These are short63/67-token MPS inputs on an older adapter, not an exact
minimum or a calibrated current workload guarantee.

Design:

1. Extract OS observation into injectable platform adapters. Preserve macOS
   vm_stat/pressure queries and Linux MemAvailable/PSI, with their provenance
   and shared deadline. Describe Apple Silicon unified memory, Intel/unknown
   GPU memory topology, WSL guest-only scope and unsupported native Windows
   explicitly. Unsupported GPU utilization/thermal telemetry stays None/unknown.
2. Separate library controls from admission: CPU setters and environment hints
   are soft controls; MPS own-process memory counters/recommended working set
   are not global GPU utilization/free VRAM or a compute quota. Expose those
   limitations without new OS APIs, services, permissions or model imports.
3. Common memory admission requires additional task memory plus a system
   reserve. Remove the unconditional35%/40% RAM floors from coordinator,
   recovery and running-watch paths.35% becomes a soft post-allocation headroom
   target for batch expansion only; CPU load, pressure, unknown observations,
   durable leases/checkpoints and sampled stop paths remain separate vetoes.
4. Preserve existing explicit minAvailableMib/maxRssMib values. A legacy command
   without a model profile uses maxRssMib as a labeled configured upper-bound
   proxy, not a measured minimum. Adaptive profiles may explicitly carry
   systemReserveMib+reserveSource and uncertainty; never install or invent a
   reserve to admit the current machine. Existing explicit legacy adaptive
   reserveFraction semantics remain a labeled compatibility path; a new fixed
   system reserve bypasses that legacy RAM-ratio conversion after owner review.
5. Cold/new-process resume charge the full configured envelope; another daemon's
   resident allocation is already reflected in host availability and receives
   no reuse credit. Same-process reuse credits only measured owned allocation,
   bounded by fixedMib; an explicitly sourced warmAdditionalMib can avoid cold
   transient re-reservation. Completed-cursor finalization stays model-free.
   Never add RSS+MPS counters as disjoint physical pools. Batch2 requires an
   explicit maxVerifiedMicrobatchSize; legacy/unverified profiles default to1.
6. Tests first: small/big RAM, explicit reserve, unknown budgets/pressure,
   phase/credit isolation, soft target, hysteresis without40% floor, Apple/Linux/
   WSL/Windows capability reports and control calls using mocks only. Preserve
   old path/clock/subprocess injection points. Run affected lightweight tests
   sequentially under the shared development lock, then one read-only review.

No current policy file or installed runtime is migrated by these changes. The
proposal still cannot certify a model trial: the exact minimum, system reserve
calibration, current-adapter GPU/CPU peaks and stop latency remain unverified.

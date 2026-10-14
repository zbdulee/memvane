# Adaptive training memory implementation plan

> **For Claude:** Execute this approved isolated patch with RED/GREEN tests and independent review. Do not run live training before a separate instruction.

**Goal:** Preserve existing memory floors while choosing training microbatch2 or1 from explicit device estimates; defer durably when even1 cannot fit.

**Architecture:** Existing absolute limits remain binding. Optional adaptiveMemory adds a RAM-relative reserve and model/device-pinned estimated working set. OS query provenance distinguishes unavailable, unrecognized and valid samples. Training accumulates gradients inside the existing effective group, so optimizer update counts and all pairs remain unchanged.

**Tech Stack:** Python standard library for admission/queue receipts; existing Laya/PyTorch trainer. Tests use disposable data and analytical scalar substitutes, with no model execution or downloads.

1. Correct the4GiB provenance record; retain earlier raw attempt receipts unchanged.
2. RED/GREEN actual macOS44% parsing, failed/unrecognized queries, unchanged legacy floors, unknown-profile rejection, microbatch2→1 and minimum1 defer. Adaptive defaults are policy heuristics, not validated measurements.
3. RED/GREEN gradient accumulation: unequal final microbatches must weight losses by pair count, zero gradients once and step once per effective group. max_updates counts optimizer steps. Reject changed model/device estimates before model loading.
4. Persist preflight/mid-run defer receipts; preserve queue rows, verified checkpoints and trained/staged resume behavior. Never emit a completion ACK on defer. No automatic OOM restart, scheduler, model replacement or pointer publication.
5. Run affected resource/trainer/coordinator regressions and a fresh independent review. Export a reviewable patch; no commit/push or operating apply.

## Correction:4096MiB is not a universal product default

MacBook training-smoke/run.py explicitly set minAvailableMib=4096 and LocalTrainer(max_rss_mib=4096). The comment referenced resident._admission's inference cold-start rule max(2048,max_rss_mib). This was a harness choice, not a learned model requirement or a shared-policy default. resource_report accepts a policy; shared-policy.json requires explicit minAvailableMib/maxRssMib, while LocalTrainer's standalone RSS default is8192MiB. No completed learning-memory estimate establishes4GiB. The separate7168MiB harness estimate used prior inference RSS plus reserve/margin and is not product policy either.

The prior phrase 'approved product4096MiB policy' is superseded by this correction. The unchanged guard implementation enforced the harness-supplied4096MiB values; it did not establish their empirical adequacy. The snapshot at rc43 contains the resident rule, but shallow history does not identify its original introduction.

## Explicit optional policy

adaptiveMemory carries reserveFraction (default0.10; conservative heuristic, not a measured safe ratio), device(cpu/mps), fixedMib, perPairMib, estimateSource and modelIdentitySha256. Missing/invalid estimates or OS signals defer. Explicit minAvailableMib remains an absolute floor; reserve=max(that floor,totalRAM*reserveFraction). Admission requires estimated peak+reserve, within maxRssMib; running checks retain reserve and OS-pressure gates without charging the full already-admitted peak twice. Profiles must cover cold loading, activations, gradients, optimizer, checkpoint and host/MPS allocations; estimates are labeled as such and never inferred from the interrupted attempt. CPU/MPS profiles are distinct. Apple shared memory is one host pool; RSS and Metal counters must not simply be added. MPS driver allocation is additionally checked against its device-recommended working set at safe training boundaries.

No profile is installed or automatically calibrated by this patch. Without the optional policy, existing numeric floors and batch grouping remain compatible. Before live validation, CPU/MPS cold-start and one-update peaks, safety margin, pressure trends and checkpoint/reload behavior still require measurement and separate authorization.

## Prepared behavior and review limits

### Where the relative rule actually applies

The relative rule is opt-in through the existing owner-private shared-policy.json (schema qmd-shared-learning-v1): add the optional adaptiveMemory object while retaining every existing required policy key, including minAvailableMib, maxRssMib and maxLoadPerCpu. adaptiveMemory requires device (cpu or mps), fixedMib, perPairMib, estimateSource and modelIdentitySha256; reserveFraction may be omitted to use the explicitly heuristic0.10 default. fixedMib/perPairMib have no defaults: values must come from a separately reviewed memory estimate/profile, not numbers chosen to admit a run. Device/model changes invalidate the estimate. Policy changes remain subject to existing execution-approval validation.

Without adaptiveMemory, resource_report keeps the existing absolute-policy admission; installation does not enable the relative rule or migrate an operating policy. With the object present, startup requires configured estimated peak plus max(minAvailableMib,ceil(totalRAM*reserveFraction)) and the unchanged maxRssMib/load gates. Unknown/unsupported OS pressure or invalid/missing estimates reject adaptive admission. Running monitors retain the reserve/pressure checks. Lowering the existing explicit floor is not part of this patch.

The historical MacBook training-smoke/run.py is a separate evidence harness with hardcoded minAvailableMib4096/max_rss_mib4096 and does not read shared-policy.json. It therefore does not become adaptive merely because this source patch is installed. A future approved smoke must explicitly pass a reviewed adaptiveMemory policy into resource_report and preserve its two-pair/one-effective-update assertions. Until fixed/per-pair estimates and exact compatibility proof exist, its dry-run must report missing inputs/defer, rather than assume a runnable profile. No operating configuration or old harness was changed to enable it here.

The chosen microbatch also bounds post-training predict/validation; its model/device binding is checked before model loading and MPS allocation is checked at forward boundaries. All pairs inside an original effective group are accumulated before a single optimizer update. Existing padded-token grouping and max_updates remain effective-group based. No input truncation, automatic OOM restart or hidden CPU fallback was added.

Adaptive VerifiedLayaTrainer requires an existing exact, unexpired compatibility attestation. Missing/mismatched proof defers before automatic model smoke; metadata device mismatch also defers. The production REVIEWED_ADAPTER_SHA256 is intentionally unchanged: this candidate does not inherit execution approval from the old adapter. The isolated unit fixture alone pins the candidate bytes in its disposable approval context. Updating real grants/attestations requires a separate reviewed and authorized flow.

Normal shared/cohort queues retain unacknowledged case rows and store deferred cycle/diagnostic state; verified trained/staged artifacts survive downstream pressure and resume without retraining. The isolated output-only preflight throws a structured defer without writing outside its approved output or creating an output directory.

Linux PSI is not treated as a macOS free percentage. Adaptive admission on unsupported/unrecognized pressure inputs remains deferred; legacy numeric-policy behavior is retained. No claim is made that the heuristic10% reserve/pressure cutoff or configured estimates have been validated across devices.

Validation:14 new lightweight unit cases and17 affected Node subtests pass. Analytical scalar substitutes verify weighted loss/gradient and update semantics without importing Torch or loading real models. A pre-existing deadline fixture failed on both candidate and clean7693d75 on this MacBook; only its fake clock was made deterministic, retaining short-budget rejection and5-second-budget acceptance. Existing fixture SQLite ResourceWarnings and an inner live skip are recorded in the external report. Fresh independent review found no remaining P1/P2 after three corrections. No live learning, checkpoint reload, dependencies, installed plugin, pointer, scheduler, commit/push or release changed.

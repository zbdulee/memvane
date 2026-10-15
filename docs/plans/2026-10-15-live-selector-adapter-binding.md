# Official live-selector adapter binding implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Reuse an owner-approved selector policy across official host/version paths without executing an arbitrary adapter or rewriting operating policy/state.

**Architecture:** Preserve the existing exact-current-adapter path. For a different path, require canonical owner-controlled official Claude/Codex cache paths, matching host manifests, a current official installation record, and identical adapter bytes. Normalize only argv[1] in memory to the current package adapter; keep interpreter/model arguments and the existing VerifiedLayaTrainer path, runtime, attestation, checkpoint and resource checks unchanged. Historical source cache copies must remain present; missing or changed copies fail closed.

**Tech Stack:** Python pathlib/stat/hashlib and bounded host metadata lookup; Node's test runner; synthetic filesystem fixtures and mocked prediction transport only.

## Design evaluation and authorization boundary

- Selected: validate the existing approved anchor and current official host, then execute only the already-loaded package's adapter. This restores compatibility without granting execution to a new external command.
- Rejected: accept arbitrary paths by hash alone; that loses the official host/install identity boundary.
- Rejected: write a separate policy per host or mutate the shared policy during ordinary recall; that introduces persistent configuration/state changes outside this task.
- The user authorized implementation and review within these explicit identity constraints. Stop if implementation would require a new security permission or weaker runtime/attestation validation.
- No main merge, deployment, operating policy edit, model inference/training/teacher, enrollment, session termination or inbox change in this task. No new remote branch push is required for this local review phase.

## Tasks

1. Add `test/context-learning-adapter-binding.test.mjs` and `test/fixtures/context-learning-adapter-binding-checks.py`. Build fake official old/current Claude/Codex installations and actual private policy/checkpoint fixtures. Run them against the base and record the cross-host/version failure.
2. Add `core/context_learning/adapter_binding.py`. Keep the exact path case. For compatibility, validate canonical host/cache/version/name and regular owner-controlled files, reject symlink/writable/foreign paths, verify current Claude or Codex install identity and Git marketplace source, compare adapter SHA-256, and return argv with only the adapter path replaced.
3. Call that resolver from `core/context_learning/live_select.py` before constructing the trainer. Keep `unsupported_adapter` fallback for rejected identity/metadata and leave `VerifiedLayaTrainer` unchanged.
4. Exercise actual resolver checks and actual VerifiedLayaTrainer construction in synthetic fixtures. Stub only prediction execution and the external Codex inventory through a fake CLI. Cover both hosts, old version paths, spaces, unsupported/missing/tampered/symlinked adapters, unregistered/wrong-origin installs, stale manifests, and unchanged policy/checkpoint bytes.
5. Correct `docs/continual-learning.md`: package/setup/state resolution readiness is not a successful selector prediction. Record the rc44 policy-path limitation and the future deployment requirement. Prepare rc45 version metadata consistently if this reviewed patch is to be installed through normal updates; update the existing version pin and expectations without changing approval files.
6. Run the new test, relevant selector/recall/runtime/manifest regressions and diff checks. Obtain independent read-only review; fix concrete findings and rerun only affected checks. Commit a reviewable local patch and report evidence, limitations and the pending deployment decision.

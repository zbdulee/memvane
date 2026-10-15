# Minimal private selector timing proposal

Proposal only: no operating logging/configuration or product instrumentation is changed in the prediction safety patch.

## What existing records establish

The rc45 harness timed immediately around `choose()`, after its own installed-file/runtime/model preflight. Successful choose totals were 5.75–6.26 seconds: 1.09–1.21 seconds before prediction process launch, then 4.55–5.05 seconds for the new prediction process and result handling. These include model load, not just forward, but exclude QMD retrieval and total automatic-hook latency. The real hook arms its shared 18-second budget before stdin parsing and gives choose only what remains.

Both initial failures launched a predict child and returned timeout at 17.39/17.68 seconds. Child startup/load/forward timings and failed-child resource observations were not returned. LocalTrainer stores last_resources in finally, but ordinary timeout exceptions do not carry it to choose. Child stdout/stderr are DEVNULL and temporary state is cleaned. The old audit probe matcher expected `argv[1] == '-c'`; real probes use `-I -c`, so their absent timestamps do not mean probes were cached or skipped. Application selection queues/locks are not in this private legacy selector path. Model import/load, filesystem/page-cache, GPU/library waits and forward cannot be separated from those saved records.

## Small proposed changes, separately reviewable

1. Record monotonic relative durations and remaining budget at policy/identity, runtime metadata probe, proof validation, spawn, child model load, tokenize/batch, forward, response/readback, cleanup and total choose. Record the actual floored child timeout and process exit code. Distinguish a process freshly loading a model from a resident worker. The subprocess observer must recognize both `-c` and `-I -c`.
2. Pass the existing optional timing argument from adapter `predict()` to `predict_loaded()`. Write one bounded owner-private metrics sidecar in that call's existing private temporary directory; preserve the prediction response schema and silent host stdout. Publish coarse last-phase milestones atomically before/after model load so timeout can report the last known phase even when no response exists. Metrics failure must not change prediction or budget enforcement.
3. On timeout/error, attach an allowlisted scalar resource/timing dictionary to the exception before cleanup, so choose can preserve it instead of dropping last_resources. Do not extend the deadline, retry, create a proof or start smoke to obtain measurements. Runtime-probe deadline propagation should be reviewed separately from metrics; its existing fixed30s subprocess bound must not be confused with actual remaining hook time.
4. For stderr, drain continuously to avoid child blocking and retain at most 4096 bytes in memory for classification. Never write raw stderr, tracebacks, paths, request/candidate text, token IDs, credentials or arbitrary exception messages to disk or stdout. Export only byte count, truncated boolean and an existing fixed allowlisted adapter error code; unknown text becomes `adapter_process_error`. Discard bytes after classification; do not persist a hash of potentially sensitive text. A bounded raw excerpt alone is not privacy-safe.
5. Keep the resulting optional diagnostic event under the existing private opt-in recall diagnostics. Suggested strict schema: schema version, random per-call identifier, enum phase/reason, finite nonnegative relative seconds, remaining budget, scalar RSS/CPU/sample counts, child exit code, stderr byte count/truncated/error enum. Do not add raw project/model paths or card identifiers to this event. Reject unknown keys, oversized sidecars, non-finite numbers and invalid types.

## Acceptance tests before implementing this proposal

- A fake executable emits a diagnostic containing synthetic secret text, a user path and candidate body; none appears in serialized output, even after truncation or parser failure.
- Early model load failure, forward failure, timeout/SIGKILL, absent/partial/oversized sidecar and full stderr pipe all remain bounded and preserve prediction/fallback behavior.
- Valid and expired-proof fast fallback retain the exact original deadline and never create learning work. Metrics report unobserved stages as null/unknown, not zero or inferred success.
- Phase totals explicitly separate hook wall, choose wall, child lifetime, model load and forward. Existing rc45 success records must not be retroactively relabeled as pure inference or proof of cold-start latency.

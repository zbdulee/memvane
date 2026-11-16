# Release Checklist

## Before creating a GitHub Release

1. Confirm the version matches every host manifest and marketplace entry.
2. Source verification (L0, prerequisite only): run `npm test` and
   `git diff --check` on the release commit. These source checks never
   replace the installed-candidate gate in step 4.
3. Verify JSON manifests parse successfully.
4. **Mandatory for every version release:** run the
   [release agent QA gate](release-agent-qa.md) on the final installed
   candidate in fresh, real Claude Code and Codex host sessions. Record the
   run with the [run record template](templates/release-agent-qa-run.md).
   Every mandatory scenario must be `PASS` in every mandatory matrix cell.
   `FAIL`, `BLOCKED` and `NOT RUN` results are never counted as `PASS` and block
   a formal release in a mandatory matrix cell. Optional, unverified host/OS
   combinations must be named in the release notes.
5. If code or the deployable artifact changes after QA, rerun the affected
   scenarios plus the core smoke. Follow the rules in section 11 of the gate
   spec.
6. If wiki compile is advertised, confirm its one-time notice and disable path
   (RQA-WIKI-03 covers the OFF path).
   For this release's built-in learning path, `RQA-RES-02` must also show the
   durable learning workload queue, resource admission, owned-process stop,
   and exact cursor resume in an approved synthetic installed-candidate run.
   It remains mandatory even if learning is not advertised. An L0 mock or
   model smoke cannot satisfy that row. Search and embedding workload queue
   expansion is an optional decision after v2 opens; search and embedding
   product functionality still needs its own Laya gate.
   For a memvane-db replacement release, require every `LI-*` row in the
   [agent QA gate](release-agent-qa.md) on the exact installed plugin.
   Source and synthetic coverage cannot satisfy these rows.
7. Publish a GitHub Release with compatibility notes, user-visible changes,
   upgrade notes, and known limitations. Name every OS or host that was not
   verified in this release.
8. After publication, install from the public marketplace in a clean QA
   account. Confirm that the deployed artifact identity equals the QA'd
   identity (RQA-ID-00 + core smoke).

## Evidence to retain

Retain the completed run record. It covers the commit SHA,
`artifactManifestSha256`, installed and deployed identity, host, OS, Node,
Python, embedding runtime and model versions, search-result proofs, blockers, and any checks
that were intentionally not run or blocked. Keep transcripts and logs in the
private evidence store. Release notes carry summaries and hashes only.

## MemVane 1.x release evidence

Run the focused checks for each changed domain and the PR CI on the final
commit. Update the known legacy worker/provider SHA pair table for the previous
release, then run the real embedding smoke on an approved machine:

```bash
npm run dev:guard -- --kind model-test -- node --test --test-concurrency=1 memvane/resident/tests/resident-model-smoke.test.mjs
```

The smoke uses the selected local embed runtime read-only and creates its own
disposable runtime state. It exercises a synthetic document and query through
the resident's worker startup path. Record `ready`, a guard refusal, or a guard
stop separately; a refusal or stop is not a passed smoke.

After installation, retain read-only evidence for both the canary and the large
project: the latest 100 recall events and stage percentiles, semantic usage and
`deferred_work_budget` count; sync publish events and lexical versus vector
head counts; bundle and reattestation events while alternating hosts; running
MemVane process counts by version; selector proof, heal and warm outcomes.
Measure changed-card visibility and the 24-hour pending interval against the
release targets before marking the live checks complete. Never use model output
or event body text as release evidence.

For a selected project's state root and its schema3 store, the following
maintainer queries only read existing files. Substitute the inspected absolute
paths; retain the command output without prompt or card bodies.

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B skills/logs/scripts/logs.py --state-root "$MEMVANE_STATE_ROOT" --hours 24 --limit 100 --json
sqlite3 -readonly "$MEMVANE_STORE" 'SELECT coverage, count(*) FROM s_generations GROUP BY coverage;'
ps -axo pid=,command= | rg 'memvane/.*/(entry/run.py|laya_local_embed_worker.mjs)'
```

Use the log summary's recent recall distribution and per-stage medians for
latency; use the timestamped sync and provider events to evaluate each live
window. A total generation count alone does not prove ten consecutive changed
passes, and an absent event or incomplete window is `NOT RUN`.

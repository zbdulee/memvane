# Release agent QA run record — TEMPLATE

> **This is a blank template, not an execution record.** Every status below
> defaults to `NOT RUN`. Copy this file into the private release evidence
> store before filling it in; do not commit filled records containing private
> paths or transcripts. Spec: [release-agent-qa.md](../release-agent-qa.md).
> Statuses are `PASS`, `FAIL`, `BLOCKED` and `NOT RUN` only. Missing or
> partial evidence is never `PASS`.

## 1. Run

| Field | Value |
|---|---|
| Run ID | `rqa-YYYYMMDD-<short>` |
| Gate spec revision (commit of `docs/release-agent-qa.md`) | |
| Matrix cell (section 4 of spec) | T1 / T1-node / T1-py / N26 / U-intel / U-linux / H-hermes |
| Start / end (ISO 8601 with offset) | |
| Operator agent (host, model ID, session IDs) | |
| Human approver | |
| Evidence directory (`$EVID`) and `evidence-index.sha256` hash | |
| Evidence calibration (missing log / wrong path both non-PASS) | |

Approvals recorded before the run (reference, date, scope, cap):

| Approval | Reference | Scope / cap | Status |
|---|---|---|---|
| QA host use + host-session cost cap | | | |
| Package execution / lifecycle scripts (if used) | | | |
| Wiki extractor/verifier cost cap | | | |
| Learning + safe test resources (RQA-RES-02) | | | |

## 2. Candidate identity

| Field | Value |
|---|---|
| Version string | |
| Tag / ref | |
| Commit SHA (40 hex) | |
| Tree SHA | |
| Candidate clean (`candidate-dirty.txt` empty) | yes / no |
| `artifactManifestSha256` (`candidate-blobs.txt`) | |
| `git archive` tar SHA-256 (informational) + `git --version` | |
| Version consistency across all manifests + `test/probe-manifest.test.mjs` | yes / no (list mismatches) |
| L0 source verification reference (command, commit, summary, log hash) | |

## 3. Installed identity (per host)

| Field | Claude Code | Codex |
|---|---|---|
| Install route (marketplace source, pinned ref) | | |
| `INSTALL_PATH` | | |
| Host-reported plugin version / commit | | |
| `installed-blobs-<host>.txt` SHA-256 | | |
| `identity-diff-<host>.txt` empty? | | |
| Host-metadata allowlist (path + justification) | | |
| Matches `artifactManifestSha256`? | | |
| Hook trust confirmed (Codex official UI / Claude); exact scope in section 4 preflight | | |
| Loaded hook path and unique log marker per fresh session | | |

## 4. Environment

| Field | Value |
|---|---|
| OS name / version / build | |
| Architecture / hardware model / RAM | |
| QA account (dedicated user or VM snapshot ID) and clean-state method | |
| Claude Code version | |
| Codex version | |
| Host model IDs used in sessions | |
| Node (managed QMD) | |
| Hook `python3` path and version | |
| Git version | |
| QMD version / entry (from `setup` status) | |
| Embedding model file SHA-256 | |
| Laya state (deferred / reviewed identity) | |
| QMD bootstrap evidence (version, path, model hash, index) | |
| Laya readiness evidence, independent of QMD | |
| Selected store generation | |
| Network state (online / restricted) | |
| Forbidden test env vars absent (`QMD_SKIP_BACKGROUND_WORKER`, `QMD_FORCE_DEAD_PIDS`, `QMD_QUERY_FIXTURE`, `QMD_FAKE_PLATFORMS`) | yes / no |

### Preflight (spec section 3, before functional QA)

Item result is `ready`, `not ready` or `NOT RUN`; an unperformed check stays
`NOT RUN`. Class (if not `ready`): environment unready / approval pending /
host unsupported / product defect.

| Item | Claude Code | Codex | Evidence | Class + retest rule applied |
|---|---|---|---|---|
| PF-ID exact candidate commit, version, installed byte identity | NOT RUN | NOT RUN | | |
| PF-FOLDER project folder access (exact folder approved where prompted) | NOT RUN | NOT RUN | | |
| PF-TRUST installed hook map and reviewed exact commands; per-hook UI approval where required | NOT RUN | NOT RUN | | |
| PF-FRESH fresh session after install/trust/reload, SessionStart trace; new prompt log path | NOT RUN | NOT RUN | | |
| PF-HOST surface (CLI/app), sandbox mode/env, required event support; delivery checked in scenarios | NOT RUN | NOT RUN | | |
| PF-RUNTIME OS build/arch, hook `python3`, Node, Git, hash tools | NOT RUN | NOT RUN | | |
| PF-NET online / offline policy / sandbox-denied / DNS failure / remote outage | NOT RUN | NOT RUN | | |
| **Preflight result** (`READY` only if every item `ready`) | NOT RUN | NOT RUN | | |

Exact trust scope (one row per host; no wildcard or "trust all"):

| Host | Surface | Approved folder (exact, or no prompt) | Reviewed hook commands (full list, from installed hook map) | Host approval route (official UI if required) / time | Hook map SHA-256 matches candidate? |
|---|---|---|---|---|---|
| Claude Code | | | | | |
| Codex | | | | | |

## 5. Scenario results

| Scenario | Host | Status | Start | End | Timeout used | Evidence files | Notes / blocker ref |
|---|---|---|---|---|---|---|---|
| RQA-ID-00 | Claude | NOT RUN | | | 15 min | | |
| RQA-ID-00 | Codex | NOT RUN | | | 15 min | | |
| RQA-INST-01 | Claude | NOT RUN | | | 45 min | | |
| RQA-INST-01 | Codex | NOT RUN | | | 45 min | | |
| RQA-INST-02 | Claude | NOT RUN | | | 60 min | | |
| RQA-INST-02 | Codex | NOT RUN | | | 60 min | | |
| RQA-INST-03 | Claude | NOT RUN | | | 20 min | | |
| RQA-INST-03 | Codex | NOT RUN | | | 20 min | | |
| RQA-INST-04 | Claude | NOT RUN | | | 60 min | | |
| RQA-INST-04 | Codex | NOT RUN | | | 60 min | | |
| RQA-HOOK-01 | Claude | NOT RUN | | | 45 min | | |
| RQA-HOOK-02 | Codex | NOT RUN | | | 45 min | | |
| RQA-SYNC-01 | Claude | NOT RUN | | | 45 min | | |
| RQA-SYNC-01 | Codex | NOT RUN | | | 45 min | | |
| RQA-GIT-01 | Claude | NOT RUN | | | 45 min | | |
| RQA-GIT-01 | Codex | NOT RUN | | | 45 min | | |
| RQA-GIT-02 | Claude | NOT RUN | | | 45 min | | |
| RQA-GIT-02 | Codex | NOT RUN | | | 45 min | | |
| RQA-GIT-03 | Claude | NOT RUN | | | 45 min | | |
| RQA-GIT-03 | Codex | NOT RUN | | | 45 min | | |
| RQA-RACE-01 | Claude | NOT RUN | | | 45 min | | |
| RQA-RACE-01 | Codex | NOT RUN | | | 45 min | | |
| RQA-WIKI-01 | Claude | NOT RUN | | | 60 min | | |
| RQA-WIKI-01 | Codex | NOT RUN | | | 60 min | | |
| RQA-WIKI-02 | Claude | NOT RUN | | | 60 min | | |
| RQA-WIKI-02 | Codex | NOT RUN | | | 60 min | | |
| RQA-WIKI-03 | Claude | NOT RUN | | | 45 min | | |
| RQA-WIKI-03 | Codex | NOT RUN | | | 45 min | | |
| RQA-RES-01 | Claude | NOT RUN | | | 45 min | | |
| RQA-RES-01 | Codex | NOT RUN | | | 45 min | | |
| RQA-RES-02 | Claude | NOT RUN | | | 60 min | | BLOCKED until learning approval + safe resources are recorded |
| RQA-PLAT-01 | (cell) | NOT RUN | | | 45 min | | |

## 6. Per-scenario detail (repeat for each executed scenario)

### `<Scenario ID>` — `<host>` — `<status>`

- Fixture path and fixture ID (`show` output hash):
- Preconditions checked (each with evidence):
- Steps performed (timestamp, natural-language request or helper action, observed result):
  1.
- Expected vs actual:
- Hook failures / timeouts observed (host output excerpt or "none"):
- Installed hook path, unique run/session marker, log before/after bytes and
  correlated turn ID:
- Deviations from spec and why:
- Cleanup / recovery performed and result:

## 7. Search-result proof (one row per probe)

| Time | Scenario | Host session | cwd | Branch / `git status` summary | Question key | Expected Present | Expected Absent | Recall log `reason` | `selected[]` paths | `project_root` | Token in injected context? | Answer | Tool call in turn? | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| | | | | | | | | | | | | | | |

Attach for each row the exact `qmd_recall_selection` line and the transcript
excerpt of injected context (private store).

## 8. Reruns and invalidations

| Date | Old `artifactManifestSha256` | New hash | Changed paths (`git diff --name-only`) | Scenarios rerun (core smoke + mapped) | Result |
|---|---|---|---|---|---|
| | | | | | |

## 9. Deployed identity (after publication)

| Field | Value |
|---|---|
| Public marketplace install date | |
| Deployed `artifactManifestSha256` | |
| Equals QA'd hash? | yes / no |
| Core smoke on deployed artifact | NOT RUN |

## 10. Cleanup

| Item | Done? | Evidence |
|---|---|---|
| All fixtures removed with helper `cleanup` | | |
| Plugin removed from QA account or snapshot restored | | |
| No maintainer settings/projects/DBs touched | | |
| Unresolved `recovery_required` / reservations preserved and filed | | |

## 11. Release decision

| Field | Value |
|---|---|
| Mandatory scenarios all PASS in mandatory cells? | yes / no |
| Mandatory scenario blockers (scenario, reason, owner) | |
| Unverified cells / hosts named in release notes | |
| Decision | GO / NO-GO |
| Decided by / date | |

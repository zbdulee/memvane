# Release Checklist

A release has two layers. The checks before the merge are mandatory and block
it. The checks after the release run in the background on the installed
version: they do not block the merge or the release, and a failure is fixed by
the next patch. There is no announcement step.

Statuses are `PASS`, `FAIL`, `BLOCKED` and `NOT RUN`. A missing or partial
result is never recorded as `PASS`. Every result is tied to the release commit
SHA and the installed `artifactManifestSha256`, and is judged from event log
rows, process lists and hashes only: never from model output, prompt text or
card bodies. Record a run with the
[gate run template](templates/release-gate-run.md).

## Before the merge

| ID | Check | Evidence |
|---|---|---|
| P1 static | Every host manifest, the marketplace entries and `teacher_model_tiers.RELEASE` carry the version of `package.json` (a test asserts it). The old-bundle compatibility tags are the three previous releases. Document links resolve. When a provider file changed, the known worker/provider SHA pair table holds the previous release. Every external address the product downloads from is pinned and listed (structure tests). The teacher tier table was compared with each host's model documentation (a person records the date). | PR CI |
| P2 focused | The changed domains and what they affect, on Python 3.13 and on `/usr/bin/python3` (3.9). | guard run ids |
| P3 PR CI | Every job green, including the macOS arm64 job on the system Python 3.9. | run URL |
| P4 full suite | `npm test` on the final commit in its own worktree, which is not edited while the suite runs. | three guard run ids |
| P5 real model | The resident embedding smoke below. When the learning cycle path changed, also the real-model learning check. A guard refusal or stop is not a pass. | guard run id |
| P6 independent review | A review of the final patch with no blocking finding. | review document |

The real embedding smoke runs alone on an approved machine:

```bash
npm run dev:guard -- --kind model-test -- node --test --test-concurrency=1 memvane/resident/tests/resident-model-smoke.test.mjs
```

It uses the selected local embed runtime read-only, creates its own disposable
runtime state and sends a synthetic document and query through the resident's
worker startup path. Record `ready`, a guard refusal, or a guard stop
separately.

If the final commit changes after P4 or P5, run them again on the new commit.

## Publishing

Merge, publish a GitHub Release with compatibility notes, user-visible
changes, upgrade notes and known limitations, then update the plugin on the
working machine and on the smoke account. The update unit is the whole
account: the Codex plugin is not per project, and the per-user resident runs
the highest installed version.

## After the release

The smoke runs on a separate machine's test account, never on the working
machine. `scripts/release_smoke.py` runs on the operator's machine, reaches
that account over SSH and judges what it reads back. The account holds no
checkout of this repository.

| ID | Where | When | Check |
|---|---|---|---|
| Q1 RS-AGED | smoke account | right before and right after the update | A long-lived install whose stored device number differs from the live one: five prompts that target different cards and one edit. Stage medians (`pinMs`, `lexicalMs`, `stillCurrentMs`) at most 50 ms, the edit indexed, the sync pass shorter than before the update. When the stored and live device numbers are equal the device item is `NOT RUN`. |
| Q2 RS-core | smoke account | the day of the update | The steps below on a new synthetic project. |
| Q3 first daily check | working machine | the day of the update | Read-only: the SLO summary of the existing installs after the update, no process of an older version, selector maintenance completed, stored and live device numbers per install. |
| Q4 SLO | working machine | 24 hours and 7 days | No `over` line in the SLO summary of the canary and the large project. |
| Q5 affected paths | by change | by change | The scenarios of the changed path (see the table at the end). |
| Q6 full run | smoke account | every minor version | The whole [release agent QA contract](release-agent-qa.md), and a new-machine install: from an account without an embedding runtime, the README steps alone reach the first semantic recall with no manual step. |

A failed item is the first item of the next patch. A defect that stops recall
or can damage data is patched the same day. A release that must be undone is
undone by a higher version, never a lower one. A day when the smoke account
cannot be reached or is not prepared records Q1 and Q2 as `BLOCKED`.

### RS-core

Only hooks and natural-language requests move the product forward. The
operator never runs a product entry point or a skill script to advance a step;
reading for the verdict (event rows, hashes, process lists) is allowed.
Sessions are interactive host CLIs inside a terminal multiplexer on the smoke
account, started from a login shell. Headless runs are not used: hooks end
silently there and it is not how the product is used.

The operator starts one `screen` session per host in the project directory,
named `smoke-claude` and `smoke-codex`, in UTF-8 mode and detached, with the
host CLI as the command of a login shell. The tool addresses window 0 of that
session by name. It pastes a prompt, waits until the window changes and
stops changing and, for a short prompt, shows that prompt's own token, then
sends Enter on its own. It reports success only when it observed the paste
and, for a prompt a hook can see, the event row of the prompt's hash. Without
that it exits non-zero and the item is `BLOCKED`. Prompts are one line each,
so the text a multiplexer pastes is the text whose hash the event row
records, and every prompt carries a token of its run, so no row of an earlier
run on the same account matches. Before a send the operator checks that the
session shows its input line and no permission or trust screen: Enter goes to
whatever the window shows.

| Step | What happens | Item |
|---|---|---|
| prepare | The tool checks that the account's shell carries no sandbox or test variable, builds the synthetic project (raw documents, each with one verified card, so onboarding indexes them without a wiki compile) and ships it. | a forbidden variable voids the run |
| update | The operator updates the plugins by the README steps and enables the plugin for the new project the way the README describes for a project. The user approves the Codex hook trust in that account; no agent does it. | RS-UPDATE |
| identity | The installed plugin files are compared with the release commit. | RS-ID |
| model address | The pinned model address is fetched on the account; size and sha256 are read back. | RS-MODELURL |
| sessions | One interactive session per host in the project directory. | RS-HOSTENV: each session's SessionStart row exists; otherwise that host's later items are `BLOCKED` |
| onboarding | A request to onboard the project and apply the plan. | RS-ONBOARD |
| new sessions | The operator ends the two sessions and starts them again: a session started before the onboarding has no SessionStart row in the project's log, and every later item of that host would be `BLOCKED`. | RS-HOSTENV |
| recall | After the vector generation is published: a short prompt and a 12,000 character prompt, each targeting a different card. | RS-RECALL per host |
| repeat | The short prompt again in the same session. | RS-DEDUP |
| edit | A request to append a fixed paragraph to one file with the host's edit tool. | RS-EDIT per host |
| sync | The tool writes, removes and commits exactly the three changed documents and their cards (nothing else in the project is touched or staged), records the time of the account's newest event row, and asks one host session to refresh the index: a change made outside a hook wakes no sync by itself. It waits for a publish later than that time and exits non-zero without one. A prompt then names all three documents. Only a sync pass and a publish later than the recorded time count; without the time nothing is changed. | RS-SYNC |
| verdict | The tool copies the listed fields of the project's event rows and writes the gate files. | — |

A recall passes when its row is `semantic: complete` with at least one
candidate and is either served with the targeted card's hash or a real
zero-card selection. The long prompt also needs at least two query chunks.
Time thresholds come from the first run of a version line, which records
values instead of a verdict. A host outage, an expired login or an
unreachable account is `BLOCKED`, and so is every Codex item before the user
approved the hook trust. The tool types prompts only: a directory trust or
edit permission screen is answered by the operator in that session.

The project's learning is off unless a run turns it on; with learning off the
`learning.capture` part of RS-EDIT is `NOT RUN`. The paragraph of the edit
request is longer than the shortest edit that starts an edit recall.

### Affected paths (Q5)

| Changed path | Also run |
|---|---|
| `memvane/learning/**` (cycle, teacher, apply) | the mock end-to-end learning check (in the suite) and the real-model learning proof |
| `memvane/learning/**` (enrollment, renewal, enable) | old-bundle tests; selector maintenance `completed` on every install within 24 hours of the update |
| `memvane/sync/**`, `skills/update/**` | RQA-SYNC-01, RQA-GIT-02 |
| `memvane/wiki/**`, `skills/wiki-*/**` | RQA-WIKI-02; RQA-WIKI-01 after its cost limit is approved |
| `memvane/install/**`, `memvane/provider/embed_runtime.py`, `locks/**`, `skills/setup/**` | RQA-INST-01, RQA-INST-03, RS-MODELURL |
| `hooks/**`, host manifests, `memvane/route/**`, `memvane/settings/config.py` | all of RS-core and RQA-GIT-02 |
| documents only | RS-ID and RS-RECALL |

## Evidence to retain

Keep the run record (the gate table, `gate.json`, the copied event rows, the
identity and model address records, the SLO outputs) in the private evidence
store. The copied rows hold counts, reasons, times and hashes only. Release
notes carry summaries and hashes, and name every OS or host that was not
verified. The smoke machine and its account are named only in the private
record.

For the read-only checks on the working machine, the maintainer queries below
only read existing files. Substitute the inspected absolute paths and keep the
output without prompt or card bodies.

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B skills/logs/scripts/logs.py --state-root "$MEMVANE_STATE_ROOT" --hours 24 --limit 100 --json
sqlite3 -readonly "$MEMVANE_STORE" 'SELECT coverage, count(*) FROM s_generations GROUP BY coverage;'
ps -axo pid=,command= | rg 'memvane/.*/(entry/run.py|laya_local_embed_worker.mjs)'
```

An absent event or an incomplete window is `NOT RUN`.

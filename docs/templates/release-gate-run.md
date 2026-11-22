# Release gate run record — TEMPLATE

Copy this file into the private release evidence store before filling it in
(one directory per version). A filled-in record names the smoke machine and
its account, so it is never committed. The rules are in
[../release.md](../release.md). Statuses are `PASS`, `FAIL`, `BLOCKED` and
`NOT RUN`; a missing or partial result is never `PASS`.

Files kept next to this record:

| File | Content |
|---|---|
| `gate.json` | commit, sessions and every item's status and note, written by `scripts/release_smoke.py verdict` |
| `gate-after-release.md` | the RS-* table of that verdict |
| `plan.json`, `prompt-*.txt`, `fixture-tree/` | the synthetic project and prompts of the run |
| `account-prep.txt` | the read-only account checks and the items the user confirms |
| `identity.json` | installed plugin files against the release commit |
| `model-url.json` | the pinned model address: size and sha256 |
| `events-fixture.jsonl` | the copied fields of the project's event rows (no prompt or card text) |
| `aged.json` | the long-lived install before and after the update, with stored and live device numbers |
| `slo-*.txt` | SLO summaries of the working machine's installs (day 0, 24 hours, 7 days) |
| `ps-before.txt`, `ps-after.txt` | MemVane processes by version around the update |

## `<version>` release gate

- Commit `<40 hex>` / `artifactManifestSha256` `<64 hex>` / operator `<name>` / `<YYYY-MM-DD>`
- Smoke machine: `<machine and account>` (macOS `<version>`, claude `<version>`, codex `<version>`)
- Account preparation: `<result per item>` / device mismatch made by: `<disk image or reboot>`

### Before the merge

| ID | Result | Evidence | Note |
|---|---|---|---|
| P1 static | NOT RUN | `<CI run URL>` | tier table compared on `<YYYY-MM-DD>` |
| P2 focused | NOT RUN | `<guard run ids>` | Python 3.13 and 3.9 |
| P3 PR CI | NOT RUN | `<run URL>` | macOS job: `<result>` |
| P4 full suite | NOT RUN | `<three guard run ids>` | |
| P5 real model | NOT RUN | `<guard run id>` | |
| P6 independent review | NOT RUN | `<review document>` | |

### After the release

| ID | Where | Result | Evidence | Value |
|---|---|---|---|---|
| Q1 RS-AGED | smoke account | NOT RUN | `aged.json` | |
| RS-UPDATE | smoke account | NOT RUN | `ps-*.txt` | |
| RS-ID | smoke account | NOT RUN | `identity.json` | |
| RS-MODELURL | smoke account | NOT RUN | `model-url.json` | |
| RS-HOSTENV | smoke account | NOT RUN | `events-fixture.jsonl` | |
| RS-ONBOARD | smoke account | NOT RUN | `events-fixture.jsonl` | |
| RS-RECALL-claude | smoke account | NOT RUN | `events-fixture.jsonl` | |
| RS-RECALL-codex | smoke account | NOT RUN | `events-fixture.jsonl` | |
| RS-DEDUP | smoke account | NOT RUN | `events-fixture.jsonl` | |
| RS-EDIT-claude | smoke account | NOT RUN | `events-fixture.jsonl` | |
| RS-EDIT-codex | smoke account | NOT RUN | `events-fixture.jsonl` | |
| RS-SYNC | smoke account | NOT RUN | `events-fixture.jsonl` | |
| Q3 first daily check | working machine | NOT RUN | `slo-day0-*.txt` | |
| Q4 SLO | working machine | NOT RUN | `slo-24h-*.txt`, `slo-7d-*.txt` | |
| Q5 affected paths | by change | NOT RUN | | |
| Q6 full run | smoke account | NOT RUN | [QA run record](release-agent-qa-run.md) | |

Items not run or blocked, and why:

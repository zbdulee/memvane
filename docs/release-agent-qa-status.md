# Release agent QA status — b1188c2 minimum main-merge gate

## Current scoped result (2026-10-25)

The exact product candidate is
[`b1188c2281bb5221fdaf64f2131e18fdd1b0a6cc`](https://github.com/zbdulee/qmd-auto-context/commit/b1188c2281bb5221fdaf64f2131e18fdd1b0a6cc)
in draft PR [#23](https://github.com/zbdulee/qmd-auto-context/pull/23).
[CI run 40196920336](https://github.com/zbdulee/qmd-auto-context/actions/runs/40196920336)
completed successfully at that exact SHA. The local product tree remains that
candidate; a later docs-only commit needs its own required CI and a product
tree comparison before main merge. This is the user's minimum main-merge QA
scope, not a claim that every formal 25-row release scenario passed.

| Retained merge outcome | Observed evidence and limit |
| --- | --- |
| Installed search | The exact candidate's Claude and Codex host sessions selected the current Laya wiki card and verified source locator. The N-1 restored-QMD fresh Claude session also answered Q-alpha. Keep selected Laya and restored QMD routes distinct. |
| `LI-MIG` | Controlled qmdqa inventory, consistent backup/DB integrity, wiki-only Laya membership and selected search preserved raw sources and QMD. This is isolated synthetic QA, not an operating-account migration. |
| `LI-RECOVER` | Official QMD restore before a first selected write and owned forward recovery after a selected write were observed in separate branches. Exact-candidate focused tests cover four crash/ACK replay cases; they are synthetic checks, not claims of installed forced crashes or power loss. |
| `LI-COEXIST` | Separate qmdqa external-QMD DB path and backup remained unchanged; an uncertain ownership/environment conflict was classified with `changesApplied:0`. No operating dulee DB was inspected or modified. |
| `RQA-INST-02/03/04` | The actual public N-1 fixture preserved settings and originals through the supported shared-install update and QMD restoration. Separate fixture C stopped the executing setup process at `enrolling` without a false completion, resumed the identical request to `completed`, then served a verified wiki card and source in a fresh Claude session. The entire host was not stopped; no host-interruption claim is made. |

Fixture C's synthetic source `docs/qa/alpha.md` has SHA-256
`a94693278f6af07db1a0492a25886ad39a61e07ac7ee0f340410e65d6075b15e`.
The official manual card was verified by Claude against its source revision;
the direct QMD hook and fresh hosted Claude answer both contained the codename
and source path. Final state: `compile.mode=off`, verification queue 0,
`shared-install-journal.json` at `completed`, and QMD DB quick-check `ok`.
Private operator evidence: `qmdqa-run/b118-inst04-c-manual-card-20261025.log`,
`qmdqa-run/b118-inst04-c-claude-one-verify-20261025.log`,
`qmdqa-run/b118-inst04-c-restore-off-index-20261025.log`,
`qmdqa-run/b118-inst04-c-verified-direct-hook-20261025.log`,
`qmdqa-run/b118-inst04-c-verified-claude-host-20261025.log`, and
`qmdqa-run/b118-inst04-c-final-gate-audit-20261025.log`. The separate
additional hosted/provider QA ledger is **7/10 used** (one verifier and one
fresh host call here); historical 30/30, 6/6 and 2/2 ledgers remain intact.
No learning or new payment was performed.

For this main merge, the user deferred exactly five assertions: `RQA-GIT-01`,
`RQA-GIT-03`, only the paid duplicate-call part of `RQA-WIKI-01`,
`RQA-WIKI-03`, and `RQA-RES-02`. They are **DEFERRED BY USER, not PASS**.
The other `RQA-WIKI-01` assertions remain in scope. The four data-safety
rows above were not waived. The exact evidence supports the scoped merge
decision; the full formal release matrix retains its own verdict rules.

## Historical f89 and intermediate-candidate record

This is a status record, not a release approval. It tracks the 25 IDs in
[the QA contract](release-agent-qa.md) without treating partial evidence as
`PASS`. Reviewed on 2026-10-25 against candidate
[`f89b141d783d8420acc961837c686e6a52b3bbb1`](https://github.com/zbdulee/qmd-auto-context/commit/f89b141d783d8420acc961837c686e6a52b3bbb1)
in draft PR [#23](https://github.com/zbdulee/qmd-auto-context/pull/23).
The exact-head [CI run 40148887271](https://github.com/zbdulee/qmd-auto-context/actions/runs/40148887271)
passed on Node 22, 24 and 26. Local L0 regression: 1,658 passed, 9 skipped,
0 failed. L0 and CI do not prove installed-host L1 behavior.

**Current source candidate, L1 verdict pending:** `cc649e755b443b6cc19803437e7a28168dd298d8`
is pushed to draft PR [#23](https://github.com/zbdulee/qmd-auto-context/pull/23).
[Exact-head CI run 40195063795](https://github.com/zbdulee/qmd-auto-context/actions/runs/40195063795)
was queued with 0/3 jobs complete when checked; it is **not a CI PASS**.
The installed artifact manifest and the full final-host L1 verdict are not yet
recorded. `cb3b857` remains the earlier resident-service source baseline.

The `cb3b857` guarded full local regression is **1,659 PASS, 9 SKIP,
0 FAIL** across 1,668 tests. Its operator-local log is
`/tmp/qmd-cb3b857-full-pyyaml-20261025.log`; guard receipt
`e2e694b04ea647f7ad86cc2afcd231f4` reports `exitCode:0`,
`settled:true`, `status:passed`. The QA execution report says the initial
attempt lacked PyYAML in the system Python; the same code then passed using
the existing test-only PyYAML 6.0.3 path. This separates a test-environment
prerequisite from the completed code regression. No installed L1 row is
marked PASS from this source test.

For comparison, `cb3b857`'s guarded **direct** selected-Laya probe returned
the verified synthetic wiki card and source locator twice through one warm
owner, then restored QMD with the source unchanged and owner released
(`qmdqa-run/cb3-selected-resident.log`, passed/settled). It did not prove
automatic UID wake or the final candidate's paid two-host L1 session.

The f89 selected
Laya hook reported `selected_proof_or_query_failed`; the attested socket was
absent after build/sync, and the QA operator restored QMD before the first
selected write. Private records:
`qmdqa-run/f89-selected-laya-hook-20261025.log`,
`qmdqa-run/f89-service-endpoint-read-20261025.log`,
`qmdqa-run/f89-restore-before-first-write-20261025.log`, and
`qmdqa-run/f89-post-restore-proof-20261025.log`.
The replacement used a separate QA fixture and install identity; none of f89's
installed PASS labels transfer. The user approved necessary additional bounded
non-learning qmdqa calls. Read-only counts in
`qmdqa-run/cc649-ledger-count-ro-20261025.log` retain the historical 30/30
ledger and the separate 6/6 candidate reservation; neither is reset.
`RQA-RES-02` training remains excluded without its distinct written approval.

**Candidate-specific partial evidence, not a completed L1 row:** The exact
`cc649e7` fixture's official UID wake reached a sealed/servable selected route
and its requested freshness became satisfied; see
`qmdqa-run/cc649-post-activate-wake-20261025.log`,
`qmdqa-run/cc649-auto-wake-status-20261025.log`, and
`qmdqa-run/cc649-freshness-status-20261025.log`. The guarded direct Claude
hook probe selected the verified synthetic wiki card and source locator twice
with one resident socket (`qmdqa-run/cc649-selected-search-guarded-20261025.log`,
receipt `a70db23f834e44e5a1d2c75cfb0528f5`, passed/settled). This is
direct hook evidence, **not** a completed paid Claude and Codex host-session
pair or `LI-RECALL` PASS. Guarded restore returned `qmd_restored`, and the
post-restore check found legacy QMD selection, identical current/backup
digests, an absent resident socket and released owner lock
(`qmdqa-run/cc649-safe-restore-guarded-20261025.log`,
`qmdqa-run/cc649-post-restore-proof-guarded-20261025.log`). The read-only
old/new fixture check lists all four projects on legacy QMD selection
(`qmdqa-run/cc649-ledger-and-old-selection-ro-20261025.log`). These are
prewrite fixture recovery observations, not N-1 update, migration or
post-first-write forward-recovery PASS.

The earlier `d037c40` Node 22 CI failure was reported against the old
wiki-discovery fixture's handler patch point. `git show d037c40..cc649e7`
contains only `test/fixtures/laya-wiki-discovery-checks.py`; the patch
targets the current handler table and changes no product source after d037.
That prior CI failure does not turn the pending cc649 exact-head run into PASS.

The private evidence is in the task's `evidence-12958a1` folder. Important
records are `QA-F89-SESSION-20261025.md`, `QA-6CC-CURRENT-20261024.md`,
and guard receipts `0f85b3eff370453ca206827f026e675d` (installed-byte
identity) and `c55ecf0a1fd54a5bb72326a16d65217b` (one bounded host
session). The raw host transcript, account paths and credentials do not belong
in this repository. Evidence IDs and the exact commit/CI link allow an
operator with authorized access to locate and check the original records.
The [QA evidence reuse and parallel execution plan](qa-parallel-plan.md)
groups the remaining assertions without changing any verdict or release gate.
Claude's independent diff review and the subsequent source checks are in
[the QA change review](qa-reviews/2026-10-25-claude-qa-change-review.md).
For a function-by-function view of the implemented paths and their boundaries,
see the [Claude-authored architecture flow charts](architecture/flows/README.md)
or their [readable HTML version](architecture/flows/html/README.html)
(download and open locally; GitHub's file view does not render the page).

Claude's read-only review found no reason to restore raw-text injection or
independent raw Laya indexing. It identified three matters to assess within
their actual scope: the bounded raw discovery scan for larger background
origins, migration from an already selected older v2 route, and a pending
sync marker that can stop further sync until recovered. The first is
fail-closed before route publication; the other two need installed-state
evidence before a release decision. A claimed question-span regression did
not match the documented three-line probe and its existing test. These are
review findings, not new blanket QA gates or completed host verdicts; the
linked review retains the original report and the code-based corrections.

## Historical f89 outcome

Exact candidate caches in the synthetic QA account matched 937 committed
files on Claude and Codex; the new Codex ID's nine hooks were trusted for the
approved projects, and real sessions loaded the candidate. The selected
**QMD** fixture had four raw and seven wiki documents with matching source
hashes. An official sync `baseline-only` invocation created a private snapshot
and enqueued no further work. It was invoked through an internal script in
that host turn, so it is state evidence, **not** completion of the product
interaction scenario. The wiki compile queue later went from one normal
asynchronous item to zero. A verified wiki card already recorded the current
`wiki-source.md` SHA-256 before that last item drained. Emptying a global or
project queue is not a blanket release condition. The cumulative QA model
reservation ledger reached its approved 30/30 ceiling.

A later official manual QMD query selected three verified wiki cards,
including `synthetic-release-qa-alpha.md`: its injected summary names
`docs/qa/alpha.md` and carries the verified source locator. The recall
diagnostic records `reason=selected`, `selected_count=3`, and
`dropped_unverified=2`. Its `engine=gemini` label makes this **manual
QMD search assistance**, not Claude/Codex installed-hook or selected
Laya PASS evidence. The first wrapper attempt could not launch Node
(`spawn node ENOENT` under that PATH); a one-time PATH correction let
the query retry succeed without an installation or persistent setting
change. The read-only follow-up still showed ledger 30/30, queue 0
and the sync snapshot present. Private records:
`qmdqa-run/f89-resume-query-alpha-path-retry-20261025.log`,
`qmdqa-run/f89-resume-query-audit-20261025.log`,
`qmdqa-run/f89-resume-runtime-compare-20261025.log`, and
`qmdqa-run/f89-resume-runtime-path-proof-20261025.log`.

No selected wiki-only **Laya** route, final-host search comparison, cutover,
restore or forward-recovery result is established by that QMD evidence.
Actual learning was excluded from historical f89 QA and remains excluded
from execution on the next candidate. The training-only `RQA-RES-02` row stays
`BLOCKED`; non-learning pressure evidence cannot complete it.
No operating account, manuscript or operating DB was changed for this f89
status record.

The historical f89 table uses `PARTIAL` only as a planning label. Formal verdicts remain
`PASS`, `FAIL`, `BLOCKED` or `NOT RUN` under the contract; a partial item
must be recorded as non-PASS in the final run record. The evidence column
identifies reusable preparation, not proof of the whole row.

| ID | Planning state | Reusable evidence | Decisive result still needed |
| --- | --- | --- | --- |
| RQA-ID-00 | PASS for f89 candidate only | Exact f89 installed files, registry and host load | Recheck public deployed identity if published |
| RQA-INST-01 | PARTIAL | Clean-start gate and candidate load | Both-host managed opt-in and selected search |
| RQA-INST-02 | NOT RUN | Pinned N-1 identity/first-session evidence | Same-ID N-1 to next-candidate update and preservation |
| RQA-INST-03 | NOT RUN | Fixture settings inventory | Changed/retained settings through update |
| RQA-INST-04 | PARTIAL | N-1 prewrite QMD restore; synthetic shared-install interruption/replay below | Ordinary install preservation and fresh-session search; actual host interruption claim remains unproved; first-write forward recovery |
| RQA-HOOK-01 | PARTIAL | Claude hook load/selection | Complete Claude search/edit/delete sequence |
| RQA-HOOK-02 | PARTIAL | Codex hook load/selection | Complete Codex search/edit/delete sequence |
| RQA-SYNC-01 | PARTIAL | QMD source hashes and sync baseline snapshot | Actual add/edit/delete convergence and search |
| RQA-GIT-01 | NOT RUN | Synthetic Git fixture exists | Branch change and scoped current results |
| RQA-GIT-02 | NOT RUN | Worktree fixture exists | Worktree isolation and enrollment |
| RQA-GIT-03 | NOT RUN | Dirty fixture exists | Dirty/untracked source behavior |
| RQA-RACE-01 | NOT RUN | L0 lock/worker tests | Installed-host late worker/generation result |
| RQA-WIKI-01 | PARTIAL | Verified current card and call logs | R2 duplicate paid-work/card limit across triggers |
| RQA-WIKI-02 | PARTIAL | Card source hash matches current QMD raw hash | R2 then revert/delete freshness and injection |
| RQA-WIKI-03 | NOT RUN | L0 compile-off checks | Installed-host off/on contrast |
| RQA-RES-01 | NOT RUN | Runtime guards tested in L0 | Ordinary host work stays responsive |
| RQA-RES-02 | BLOCKED | Model copy/binding/calibration only | Distinct written learning approval before any synthetic training; otherwise explicit row decision |
| RQA-PLAT-01 | NOT RUN | macOS Mini environment inventory | Explicit unsupported/optional hardware disposition |
| LI-INST | NOT RUN | Candidate install identity, inactive build | Selected-route install/journal and host verification |
| LI-MIG | NOT RUN | QMD raw+wiki inventory and backup | Wiki-only Laya membership, seal and source preservation |
| LI-SCOPE | NOT RUN | L0 scope/claim tests | Selected-route scope and stale-writer isolation |
| LI-RECOVER | NOT RUN | L0 recovery cases, inactive retirement | Real cutover/restore and forward recovery |
| LI-COEXIST | NOT RUN | Separate operating QMD observed/preserved | New/legacy route coexistence and recovery |
| LI-VECTOR | NOT RUN | Model identity and L0 capacity tests | Wiki-only vector coverage and admission on installed host |
| LI-RECALL | NOT RUN | QMD manual wiki selection/locator and card evidence | Selected Laya wiki retrieval, injection and locator on both hosts |

**RQA-INST-04 path correction and later evidence.** The N-1 preserving
installation used `setup`'s `plan-install` / `install` route and has a
`shared-install-journal.json` at `completed`, not an
`install-update-journal.json` (`qmdqa-run/b118-nminus1-coordinator-journals-20261025.log`).
The latter journal and `rollback` / `prepare` / `activate` apply only to a
separately reviewed topical-card/shadow or runtime installation. A new
QMD-origin plan after official prewrite `qmd_restored` was rejected with
`project_selection_already_exists`, `changesApplied:0`; the retained marker
is a safety boundary, not an identical shared-install retry
(`qmdqa-run/b118-nminus1-official-restore-20261025.log`,
`qmdqa-run/b118-nminus1-reapply-plan-20261025.log`). The protected N-1
fixture still had DB quick-check `ok` and all nine source-file hashes
unchanged (`qmdqa-run/b118-nminus1-postrestore-protected-proof-20261025.log`).

Separate fixture C **synthetically** stopped the executing setup parent with
SIGTERM during `enrolling`; its journal stayed `enrolling`, not falsely
`completed` (`qmdqa-run/b118-inst04-c-controlled-interrupt-20261025.log`,
`qmdqa-run/b118-inst04-c-interrupted-status-20261025.log`). Identical digest
replay reached `qmd_ready`, journal `completed`, two indexed documents and
DB quick-check `ok`; settings, raw and wiki sources were preserved
(`qmdqa-run/b118-inst04-c-identical-resume-20261025.log`,
`qmdqa-run/b118-inst04-c-preservation-proof-20261025.log`). This is focused
abrupt-stop/replay evidence, **not** proof of an actual host-process stop.
The later verified-card direct hook and fresh Claude session supplied the
previously missing fixture-C search assertion; see the current scoped result
above. Do not replace shared-install forward recovery with a shadow-installer
rollback.

## Earlier main-merge planning snapshot (superseded by current result)

The [execution plan](qa-parallel-plan.md) retains all **25 IDs**. The user has now deferred five specific assertions from this main-merge decision: RQA-GIT-01, RQA-GIT-03, RQA-WIKI-01 paid duplicate-call work only, RQA-WIKI-03 and RQA-RES-02. Record them DEFERRED BY USER for merge, not PASS. RQA-WIKI-01's other assertions remain in scope. RQA-GIT-02, mandatory T1/T1-node/T1-py host cells, and RQA-INST-04, LI-MIG, LI-RECOVER and LI-COEXIST remain required. Formal release still follows the full contract unless separately decided.
The user reserved exactly two additional hosted calls, one Claude and one Codex, separate from the exhausted 30/30 and 6/6 ledgers. Compile mode stays OFF before sealing to avoid automatic paid work. No result is presumed.
The `cb3b857` source commit has a full guarded L0 result. `cc649e7` adds the
sealed cross-version dispatch and fixture update; its exact-head CI is
pending, and the final artifact still needs installed Claude/Codex identity
and applicable §11 host checks. The changed resident-service and
sealed-dispatch paths are not listed in §11's narrow path map, so the current
conservative rule covers all scenarios. The [dependency table](qa-parallel-plan.md#changed-code-dependency-versus-the-contract-default) separates direct code effects from independent row semantics; it does not approve a waiver or transfer f89 verdicts. The table above
records f89 history, **not** a PASS table for the pending candidate. Preserve each new row's separate
expected outcome while sharing fixture setup, source hashes and host turns.

The user has authorized the necessary QA calls, push/main merge and current
Mini operational installation, but none is a recorded result. The five user-approved deferrals remain non-PASS while every retained merge assertion and T1/T1-node/T1-py cell still needs its own evidence, with optional hardware recorded under the contract's support wording. A failed, blocked or unobserved required result
keeps its non-PASS status. Normal cold provider preparation and resource
waiting are reported separately; neither is `LI-RECALL` PASS. The installed
Claude and Codex selected search must succeed after readiness, with current
wiki cards, bounded context and a verified source locator. Mini operational
use and later MacBook installation are separate host observations, never
substitutes for controlled qmdqa migration/recovery branches.

## Scope correction and reuse rule

- **Normal async wait:** a queued wiki source does not itself fail or block
  QA. Judge the specific source revision needed by a scenario against the
  verified card, selected index and allowed convergence deadline. For
  `wiki-source.md`, historical `post_sync_source` queue rows had no revision;
  the already verified card's SHA matched the selected QMD raw hash. This
  was not proof of stale content. Repeated `worker_failed` update wakes in
  other fixture projects are a separate error-retry condition.
- **Plugin boundary:** check selected/current wiki summary and verified source
  locator. Original source text alone is not automatically injected or
  independently indexed in Laya. Excerpts and links inside a wiki card are
  normal wiki content. The host agent's choice to open a source is optional
  observation, not a plugin PASS requirement.
- **Changed candidate:** an old run is not silently marked PASS for a new
  artifact hash. Reuse unchanged source-level tests, QMD inventory and
  backup evidence as preflight. Scenario observations may be reused only where
  section 11 excludes the changed path and the host/revision/artifact binding
  remains valid; repeat core installed identity/hook smoke and all affected
  assertions. The new
  resident-service file is not listed in that map, so the pending candidate
  needs the full mandatory set unless the reviewed contract itself changes.
  Batch changes before the final candidate to avoid redundant full runs. Published identity requires its own comparison.
- **Data safety:** LI-MIG, LI-RECOVER, LI-COEXIST and RQA-INST-04 stay
  mandatory. Neither CI success nor an empty queue substitutes for them.

## Smallest next evidence set under the current contract

1. Retain `cb3b857`'s guarded full L0 receipt for its source tree and the
   `cc649e7` direct fixture observations for their exact inputs. The later
   product-code change needs any final-source regression required by the
   contract. Wait for `cc649e7` exact-head CI, then compare installed Claude/Codex bytes and
   `artifactManifestSha256`. Record any new model work under an authorized
   reservation; the f89 30/30 and candidate 6/6 ledgers are exhausted.
2. Reuse f89's unchanged QMD inventory, backup procedure and wiki-card/source
   hashes as preparation. On the new installed candidate, obtain both host
   hooks, real selected Laya wiki search/injection and source locators after
   the resident owner is ready. Record cold `preparing` or pressure deferral as
   non-PASS for retrieval, without calling normal queue wait a failure by itself.
3. Share an N-1 fixture across `RQA-INST-02/03/04`: observe update and settings
   preservation through the actual shared-install path, including same-request
   resume and fresh-session search. Separately prove official QMD-origin
   restore before first selected write and forward recovery after it; use
   shadow-installer rollback/re-apply only when that distinct path was
   actually prepared. In the same controlled migration chain,
   prove wiki-only membership, QMD/raw preservation, coexistence, vector seal
   and scope. Keep the four named data-safety rows mandatory.
4. Reuse fixture setup for distinct source/Git/worktree/wiki/R2 and late-worker
   assertions; record the exact revision each requires. Collect ordinary
   non-learning resource evidence in those turns. Keep training-only
   `RQA-RES-02` blocked without distinct written approval or a row-specific
   gate decision. Complete mandatory T1-node/T1-py cells and optional
   hardware disposition. No global queue-zero or forced agent raw-file Read is
   added.
5. Map every retained main-merge ID and cell to an exact-candidate PASS receipt
   before recommending merge; retain each user-deferred assertion as non-PASS. The five approved deferrals are recorded as non-PASS follow-up; worktree isolation and T1-node/T1-py cells remain required. After publication, compare
   public installed bytes and repeat core smoke as required by the contract. Record
   current Mini and later MacBook use against their own hardware and settings.

Reproduction references for authorized maintainers: the f89 source comparison
`git diff c6c6d36..f89b141 --stat`, the candidate source comparison
`git diff --name-only f89b141 cc649e7`, and contract §11. f89's local full
regression is historical; `cb3b857` has its own full L0 receipt. Exact-head
cc649 CI, final artifact identity and full installed L1 verdict are pending.

# Release agent QA status — f89b141

This is a status record, not a release approval. It tracks the 25 IDs in
[the QA contract](release-agent-qa.md) without treating partial evidence as
`PASS`. Reviewed on 2026-10-25 against candidate
[`f89b141d783d8420acc961837c686e6a52b3bbb1`](https://github.com/zbdulee/qmd-auto-context/commit/f89b141d783d8420acc961837c686e6a52b3bbb1)
in draft PR [#23](https://github.com/zbdulee/qmd-auto-context/pull/23).
The exact-head [CI run 40148887271](https://github.com/zbdulee/qmd-auto-context/actions/runs/40148887271)
passed on Node 22, 24 and 26. Local L0 regression: 1,658 passed, 9 skipped,
0 failed. L0 and CI do not prove installed-host L1 behavior.

The private evidence is in the task's `evidence-12958a1` folder. Important
records are `QA-F89-SESSION-20261025.md`, `QA-6CC-CURRENT-20261024.md`,
and guard receipts `0f85b3eff370453ca206827f026e675d` (installed-byte
identity) and `c55ecf0a1fd54a5bb72326a16d65217b` (one bounded host
session). The raw host transcript, account paths and credentials do not belong
in this repository. Evidence IDs and the exact commit/CI link allow an
operator with authorized access to locate and check the original records.
Claude's independent diff review and the subsequent source checks are in
[the QA change review](qa-reviews/2026-10-25-claude-qa-change-review.md).
For a function-by-function view of the implemented paths and their boundaries,
see the [Claude-authored architecture flow charts](architecture/flows/README.md).

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

## Current outcome

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

No selected wiki-only **Laya** route, final-host search comparison, cutover,
restore or forward-recovery result is established by that QMD evidence.
Actual learning was excluded from the approved QA work. No operating account,
manuscript or operating DB was changed for this status record.

The table uses `PARTIAL` only as a planning label. Formal verdicts remain
`PASS`, `FAIL`, `BLOCKED` or `NOT RUN` under the contract; a partial item
must be recorded as non-PASS in the final run record. The evidence column
identifies reusable preparation, not proof of the whole row.

| ID | Planning state | Reusable evidence | Decisive result still needed |
| --- | --- | --- | --- |
| RQA-ID-00 | PASS for QA candidate | Exact f89 installed files, registry and host load | Recheck public deployed identity if published |
| RQA-INST-01 | PARTIAL | Clean-start gate and candidate load | Both-host managed opt-in and selected search |
| RQA-INST-02 | NOT RUN | Pinned N-1 identity/first-session evidence | Same-ID N-1 to f89 update and preservation |
| RQA-INST-03 | NOT RUN | Fixture settings inventory | Changed/retained settings through update |
| RQA-INST-04 | PARTIAL | Older inactive QA build retirement | Actual rollback and first-write forward recovery |
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
| RQA-RES-02 | BLOCKED | Model copy/binding/calibration only | Approved actual learning pressure/stop/resume |
| RQA-PLAT-01 | NOT RUN | macOS Mini environment inventory | Explicit unsupported/optional hardware disposition |
| LI-INST | NOT RUN | Candidate install identity, inactive build | Selected-route install/journal and host verification |
| LI-MIG | NOT RUN | QMD raw+wiki inventory and backup | Wiki-only Laya membership, seal and source preservation |
| LI-SCOPE | NOT RUN | L0 scope/claim tests | Selected-route scope and stale-writer isolation |
| LI-RECOVER | NOT RUN | L0 recovery cases, inactive retirement | Real cutover/restore and forward recovery |
| LI-COEXIST | NOT RUN | Separate operating QMD observed/preserved | New/legacy route coexistence and recovery |
| LI-VECTOR | NOT RUN | Model identity and L0 capacity tests | Wiki-only vector coverage and admission on installed host |
| LI-RECALL | NOT RUN | QMD wiki source/card evidence | Selected Laya wiki retrieval, injection and locator on both hosts |

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
  backup evidence as preflight; repeat the core installed identity/hook
  smoke and only scenarios affected by changed code, as section 11 of the
  contract specifies. Batch changes before the final candidate to avoid
  redundant full runs. Published identity requires its own comparison.
- **Data safety:** LI-MIG, LI-RECOVER, LI-COEXIST and RQA-INST-04 stay
  mandatory. Neither CI success nor an empty queue substitutes for them.

## Smallest next evidence set under the current contract

1. Reuse exact f89 identity, CI, QMD inventory and verified card evidence.
   Do not regenerate or drain a queue merely to get a zero count.
2. Prove the final-host Claude/Codex hook search and selected wiki context,
   with correct source locator. Test source freshness only for the revision
   each wiki or sync scenario actually changes.
3. Prepare and select a fresh wiki-only Laya candidate under the existing
   synthetic scope, then run wiki-only retrieval, backup/cutover, rollback
   before first write and forward recovery after first write. Preserve QMD,
   source files, backups and operating DBs.
4. Run the remaining update, Git/worktree, race, compile-off and resource
   scenarios only for their distinct contract assertion, reusing shared host
   turns and setup evidence. Record hardware cells honestly. Actual learning
   remains BLOCKED without a separate bounded approval.

Reproduction references for authorized maintainers: `git diff
c6c6d36..f89b141 --stat`, `git diff --name-only <tested-commit>
f89b141`, and the contract's section 11 impact map. Source verification is
`npm test` under the repository's global development guard; it was already
completed on this exact commit and should not be rerun solely to refresh this
status document.

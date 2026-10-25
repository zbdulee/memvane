# Claude review of QA-discovered changes — 2026-10-25

This is the original read-only Claude Opus 5.5 review of the committed
`c6c6d36..f89b141` diff and selected QA contract text. It is a reviewer
report, not a release verdict or a claim that every finding was confirmed.
The official development guard run `23bdbf383b89427197eef73edf2a3181`
passed and settled. Claude used no tools, web requests, credentials or
operating data. Its analysis used only the bounded packet, so excluded files
need separate code checks. The qmdqa host QA model-call ledger is separate.

See [QA status](../release-agent-qa-status.md) for row outcomes and the
[contract](../release-agent-qa.md) for current acceptance criteria.

## Code checks after the review

These checks distinguish findings from hypotheses; no product code was
changed as part of this review.

| Review item | Current disposition | Code evidence and next action |
|---|---|---|
| Wiki-only Laya boundary | Confirmed design to retain | `laya_index_host_route.wiki_index_roots` returns only the wiki root; origin planning and sync pass those roots to the index. Raw path/hash discovery remains separate. Verify the selected DB membership/FTS/vectors on the installed host before release. |
| H1 raw discovery size | Confirmed bounded, already fail-closed **before route publication** | `laya_wiki_discovery.seed_source_snapshot` calls `_raw_snapshot` before activation. `plan_qmd_origin(background=True)` can accept a larger source, so a large project can spend preparation work then be refused at activation. This is an operability/eligibility gap, not evidence of data loss in the small synthetic fixture. Define and test an explicit project-size policy before broader rollout. |
| H2 old selected v2 migration | Conditional, not yet established as deployed | New wiki-only roots and cursor schema make an old selected v2 state incompatible. The old state must fail closed; public or user-selected v2 inventory is needed before claiming a release migration is required. Existing pre-433 QA candidate state is not reusable as the new selected route. |
| H3 pending sync marker | Confirmed availability risk in an interrupted run | `sync.run` checks the marker before root and dirty-index work and returns `sync_recovery_required`, including for baseline-only. There is no official marker-recovery command in that module. Preserve the marker and repair the protocol before claiming crash recovery; do not clear it manually. The observed qmdqa baseline had **no** marker, so this does not invalidate that snapshot. |
| M1 path/hash seed | Partially exercised | The selected QMD raw document hash and verified wiki card revision match in the synthetic fixture. This is not a no-change seeded-drain test across nested/Unicode paths; add that focused test if cutover is resumed. |
| M3 missing journal | Lower priority, conditional | The normal selected-host path validates the route/journal before discovery. The direct discovery entry still uses `isfile(journalPath)` to choose initial cursor behavior; keep fail-closed coverage for direct calls. |
| M4 retirement phase description | Documentation inconsistency | `abandon_unused_initial_build` is registered-only; `abandon_inactive_initial_build` can retire an unselected partially built QA candidate and both retain stores. `laya_initial_build.py`'s `retired` prose describes only the former. Clarify that text in a later code-doc cleanup; this QA document does not change the product state machine. |
| M5 question-span probe | Reviewer misread; no current defect shown | The actual §6 probe places `<question>` on its **own** line ending `?`. `test/keywords.test.mjs` exercises exactly that three-line shape and asserts `keywordSpan=question`. The host probe also observed `keywordSpan=question`. Whether the heuristic is a desirable general product rule is a design review topic, not proof that this documented probe fails. |
| QA-only retirement/cancel and budget code | Keep scoped and fail-closed | The review did not receive the full installer diff or call sites. These features should not be generalized or removed solely from this packet. The current QA budget is inert without its explicit environment and the 30/30 ledger is an immutable audit, not a release PASS. |

The original report below is retained in full (blank-line whitespace
normalized) so its unverified claims stay
visible. Its suggested N1–H5 run grouping is a planning aid, **not** an
automatic replacement for the 25 mandatory row verdicts. It explicitly
leaves unmapped rows `NOT RUN`. The status document carries the full mapping.

## Original Claude report

# Independent review: c6c6d36 → f89b141 (read-only, packet only)

## Scope and limits

I reviewed only the diff and contract text in the packet. I could not see these parts, so I give no verdict on them and list evidence requests instead:

- most of `laya_product_install.py`, including the +419-line abandon code
- `wiki_index_roots`
- `update_hook_queue`
- `wiki_compile_enqueue`
- `wiki_source_intent`
- `runtime_admission` and `runtime_handlers`
- the `qa_model_budget` call site
- the shell `+1` lines

## Bottom line

- **The core boundary change is correct and should be kept.** This covers 2a8e959 and 433089e: wiki-only index, no raw backfill, no raw excerpt.
- **The new wiki-only design has three open questions that could block release:**
  1. The raw discovery scan bound may cap cutover at small projects.
  2. Older v2 state has no migration or re-seed path.
  3. The new sync pending marker can permanently block all sync with no documented recovery.
- **Two changes look shaped by the QA harness rather than by product need:**
  - the keyword question-span heuristic
  - the size of the QA retirement/cancel machinery
- **Some QA contract wording should be tightened** so that conditional rows cannot pass without proving anything.

---

## 1. Classification

| Commit | Change | Class | Recommendation |
|---|---|---|---|
| 8cbf93e | Settle unconfigured QMD update wake | (a) product fix | Keep. I did not see the code, so I need evidence (E8). |
| d51b82a | Local provider review with hoisted QMD deps (`_llama_packages`, `_read_private_or_package`) | (a), cross-host/npm-layout fix | Keep, with evidence E7 |
| 9e9db76 | uv concurrency fixed at 1 | Ambiguous (a)/(b): most likely a QA host resource limit | Keep. Simplify later. |
| 0d31f7c (part 1) | No bytecode written into the installed cache (`sys.dont_write_bytecode`, `PYTHONDONTWRITEBYTECODE` in scripts and hooks) | (a) product hygiene and identity integrity | Keep |
| 0d31f7c (part 2) | `_keyword_question_span` | (b) induced by the QA probe format | **Reconsider** |
| 2a8e959 | Remove `raw_excerpt`; gate summary/sources on `is_wiki_tag` | (a) required by the boundary | Keep. Confirm N-1 behavior (E5). |
| 433089e | Wiki-only index roots, raw discovery cursor v2, cutover seeding | (a) required by the boundary | Keep. **Findings H1–H3 must be resolved.** |
| c3d19d2, dc3ebf5, aa4f587 | `retired` phase, `abandon_unused_initial_build`, `abandon_inactive_initial_build`, staged FTS handling | Mostly (b)/(c): cleanup of QA-created builds | Keep fail-closed. **Gate to QA fixtures. Simplify later.** |
| 8acfc97 | `runtime_update_cancel` and the `fixture_proof` refactor | (c) QA cleanup tool, fixture-gated | Keep. Consider moving out of `core/` later. |
| 6cc1e93 | Await durable wake ACK in host fixture | (c) test race | Keep |
| 3c20916 | Coalesced-wake safety assertions | Test strengthening | Keep |
| f89b141 (sync) | `.pending-compile` marker that stops replay | (a) real, rare paid-replay defect, but over-broad | Keep fail-closed. **Narrow its scope and add recovery before release.** |
| f89b141 (budget) | `qa_model_budget.py` | (c) QA cost control placed in product code | Keep. It is inert unless env vars are set. Consider moving later. |
| docs | `release-agent-qa.md` rewrite of Present/Absent | (b) correction of over-broad QA expectations | Keep, with the tightening in §4 |

---

## 2. Findings by severity

### H1 — Raw discovery scan bound may make Laya v2 cutover impossible for real projects (high; needs evidence)

**Code:**
- `laya_wiki_discovery._raw_snapshot` says: "Its bounded 200-file/5-MB scan is off-hook."
- `seed_source_snapshot` calls it before route publication and refuses if it fails. The comment there says: "A background origin plan can accept more raw files than the bounded discovery scan."

**Consequences if the bound really is 200 files / 5 MB of raw input:**
- Any project with more raw input than that cannot complete cutover.
- The refusal happens at `activate_qmd_origin`. That is after the candidate was planned, built, embedded and sealed, so all that work is wasted.
- A selected project whose raw roots later grow past the bound loses discovery permanently:
  - `DiscoveryBlocked('raw_source_stale')` fires on every drain.
  - The reason code is the same one used for an unstable file, so the two cases cannot be told apart.
- This is fail-closed, so no data is lost. But wiki freshness stalls silently.

**Recommendation:**
1. Confirm the actual limits of `scan_collections` (E1).
2. If the bound is real:
   - Check it in `plan_qmd_origin`, not at activation.
   - Give the oversize case its own reason code, such as `raw_scan_over_bound`.
   - Decide explicitly whether the discovery scan needs its own, larger bound. A path/hash scan with no bodies does not need the 200-file limit that applies to origin source proof.
3. Do not relax fail-closed behavior.

### H2 — No migration or re-seed path for v2 state created before 433089e (high if any v2 selection exists outside QA; otherwise medium)

| What changed | Code | Effect on existing state |
|---|---|---|
| Discovery state schema and file stem | `STATE_SCHEMA` → `laya-wiki-discovery-v2`; stem → `wiki-discovery-v2-…` | Any route with an existing install journal finds no v2 cursor. `_drain_once` raises `snapshot_missing` and blocks permanently. |
| Index roots | Membership, catalog and binding roots are now wiki-only: `_route_source`, `serve_selected`, `_check_bound_evidence`, `_check_evidence`, `bind_qmd_origin` | A candidate, ledger row or capacity store computed over **all** roots now hits `cutover_source_manifest_changed` or a binding mismatch. |

These failures are fail-closed, which is correct. The concern is that **forward recovery of an in-flight cutover row made by older code** has no shown way to complete or roll back.

The same root-set change raises another question: what does `sync_full` do with a bound candidate whose membership still contains raw documents? It should fail closed. It must not silently drop documents in a newly published generation.

**Recommendation:**
- Answer E2 and E3.
- If no public release ever selected v2, state that explicitly in the release notes.
- Treat every v2 candidate, cursor and ledger row that QA created before 433089e as non-reusable. Re-plan and re-cutover those fixtures.
- If v2 selection did ship, an audited re-seed or re-plan path is release-blocking. It must accept the loss of the old baseline explicitly. It must not silently reseed.

### H3 — `sync.py` pending marker can stop all sync, including QMD file indexing, with no recovery shown (medium-high, availability)

**The underlying defect is real.**
- The sequence is: enqueue, then the worker claims and pays, then `write_state_atomic` fails, then a retry re-enqueues the same work.
- The live-queue key coalescing cannot catch an already-consumed revision.
- So this is a genuine class (a) fix.

**The implementation is too broad:**
- The marker check runs at the top of `run()`, before root resolution and before dirty-index work.
- A Ctrl-C between `write_pending_marker` and `clear_pending_marker` is a realistic event in a skill run. After it, every later `sync` returns `EXIT_RECOVERY_REQUIRED`. That includes `baseline_only` runs and ordinary QMD indexing.
- I see no recovery command and no documented procedure. `docs/settings.md` changed by 2 lines; I could not see whether that covers recovery.
- If `persist` succeeds but `clear_pending_marker` raises, the project is blocked even though its state is consistent.

**Recommendation:**
- Keep fail-closed for the paid compile path and for baseline advance.
- Before release:
  1. Narrow the block to compile enqueue and baseline advance, so dirty-index sync keeps working.
  2. Ship an audited recovery path. It should:
     - inspect the compile queue and ledger for the marked revisions, then
     - either ACK the snapshot or re-enqueue, and record which it did.
  3. Document that recovery path in the skill output.
- Do not "fix" this by deleting the marker automatically. That would reintroduce the duplicate-payment risk.

### M1 — Cutover seed path/hash format must match the scan format exactly (medium; cost risk)

**What the code compares:**
- `activate_qmd_origin` builds `baseline` from `inspect_qmd_collections(...)['documents']` and keys collections with `path.split('/',1)[0]`.
- `_drain_once` compares that baseline with `scan_collections(...).files[*].path` and `.sha256`.

**What goes wrong if they differ:**
- Any difference in path normalization (case, handleization, Unicode normalization, collection prefix) makes the first drain see every raw file as changed.
- A difference in hash input (raw bytes vs normalized body) has the same effect.
- The result is a mass paid compile enqueue of up to `MAX_PENDING` files, or an overflow that silently skips all of them.

"Current source hashes match" for raw4/wiki7 is partial evidence: it covers hashes but not path format.

**Need (E4):** a fixture that seeds the baseline and runs one drain with no source change. It must show `pending == {}` and no enqueue. Include a non-ASCII filename and a nested path.

### M2 — `seed_source_snapshot` retry can wedge activation (medium, operability)

- A failed activation leaves the seeded cursor in place.
- If the user edits raw files while QMD stays selected, the next activation raises `baseline_changed`. Activation stays blocked until a re-plan.
- This is fail-closed and acceptable, provided `baseline_changed` is surfaced with clear "re-plan required" guidance and the re-plan is idempotent. Please confirm.

### M3 — The `journalPath`-exists check is a weak signal for an installed route (medium-low, fail-open edge)

**Code:** `_drain_once` uses `os.path.isfile(decision['journalPath'])` to choose between `snapshot_missing` (blocked) and `cursorStarted` (silent new baseline).

**Risks:**
- A journal that is temporarily missing or moved during an installed route would quietly accept the current raw state as the baseline. Earlier edits would never be discovered.
- A route dict without a `journalPath` key raises `KeyError`.

**Recommendation:** base the choice on the route schema or the install record, not on whether a file exists. Simplify later; not release-blocking if E2 confirms nothing else.

### M4 — Phase docs disagree with the code for `retired` and `abandon_inactive_initial_build` (medium; contract clarity and mixed-version risk)

**Contract mismatch:**
- The `laya_initial_build` docstring defines `retired` as a build that "never did any work (still `registered`, nothing read or written)".
- `abandon_inactive_initial_build` and `_unused_store_proof(allow_work=True)` evidently abandon builds that did do work.

**Unanswered questions:**
- Which terminal state does the inactive path write?
- Does it take the build owner lock non-blocking while a worker may be mid-step?
- Does it delete any store, or only snapshot (`_snapshot_sqlite`)?

**Mixed-version risk:**
- An older plugin that reads `retired` fails with `initial_build_record_invalid`.
- If Claude and Codex caches ever differ in version and share state roots, wakes may retry or back off indefinitely. They match at 937 files today.

**Recommendation:**
- Keep `retired`. Removing it would invalidate records that already exist.
- Require proof that both abandon entry points refuse without `fixture_proof`, or that a non-QA invocation was deliberately designed and audited (E6).
- Fix the docstring.

### M5 — `_keyword_question_span` is tuned to the QA probe and does not match the documented probe (medium; reconsider)

**Mismatch with the contract:**
- The documented probe in `docs/release-agent-qa.md` §6 puts `<question>` inline on line 2, followed by `주입된 컨텍스트에 없으면 "없음"이라고만 답해.`
- That line ends in `답해.`, not `?`. So `questions` is empty and the function returns `"full"`.
- Either the heuristic never fires on the contract probe (dead code), or the harness used a different layout from the contract (an execution mistake, class (c)).

**Problems either way:**
- It changes product lexical retrieval based on a phrase pattern ("tools … context … only/without").
- It changes `identifiers` extraction as well as keywords.
- That is QA-specific tuning on a production path.

**Recommendation:**
- Reconsider: remove it from the product, or replace it with a general stopword or instruction-term treatment justified by real prompts.
- QA verdicts should rest on `selected[]` and the injected bytes. The probe's instruction words should not decide them.
- The `keywordSpan` log field is harmless and can stay.

### L1 — `laya_host_response._render_context` raw branch is now unreachable (low)

- With a wiki-only index, a non-wiki row is a contract violation. Rendering it as `Path(path).stem` hides that violation. Prefer `_fail('raw_row_in_selected_index')`.
- `title_only_paths` previously used `qmd_markdown_title`. If any wiki path can still reach the `else` branch, its title format regressed from the markdown title to the filename stem. Please confirm.
- The header still says "verified source documents". It should say "wiki cards".

### L2 — Leftover work and code that are now dead (low; simplify later)

- `_read_view` still loads both memberships, up to 50k rows each, on every drain, but only lineage is used.
- `rawFallbackMinScore` is no longer used in `query_selected_v2`.
- `guard_hits` still strips `_raw_excerpt`. This is harmless hardening.

### L3 — uv concurrency forced to 1 (low)

- The comment "caller UV_CONCURRENT_* values are not inherited" is misleading. `_run` already passed through only `PATH`, `LANG` and `TMPDIR`.
- `UV_CONCURRENT_DOWNLOADS=1` slows first install for every user. That time counts against the 900 s per-step timeout.
- Keep for now. Later, raise downloads to a small N and leave installs and builds at 1.

### L4 — `runtime_update_cancel` details (low)

- `_private_json` stats and then calls `read_bytes()` with symlink-following. The private queue mitigates this.
- Absence handling differs between the two cancel tools:
  - The build cancel `cancel_locked` returns `unchanged/wake_absent` without an audit row.
  - The update cancel refuses `wake_absent_unaudited`.
  - Pick one rule. The audited refusal is the stricter and better one.
- `sys.dont_write_bytecode = True` sits in a library module (`runtime_work.py`). That is a process-wide side effect. It would be cleaner at entry points. Keep it.

### L5 — `qa_model_budget` (low)

**Design is sound:**
- Reservations are never refunded.
- The ledger uses `flock` and fsync, and is owner-only and single-link.
- Partial configuration defers instead of running.

**Gaps:**
- I could not see the call site (E9).
- Exhaustion raises `ResourceDeferred`. That means wakes back off indefinitely while the ledger stays at 30/30.
- **Any further QA that launches a provider or host turn will defer.** Final QA needs a new, explicitly approved budget, recorded with `init --used <audited count>` on a new ledger. Never reset the existing one.

---

## 3. Evidence requests

These are required before I can sign off on the items above.

- **E1:** The actual file count and byte bounds of `scan_collections` for discovery. Where `plan_qmd_origin` (+17 lines at line 293) checks raw size against them.
- **E2:** Whether any public release, or any user-facing build, ever selected a Laya v2 route.
- **E3:** For an old-format v2 candidate, ledger row or capacity store, the actual outcome of each of:
  - `_check_evidence(restore=True)`
  - forward recovery from `switching`
  - `sync_full`

  Show the reason codes, and show that no generation drops documents.
- **E4:** A test that seeds, then drains with no change, and gets zero pending. Include non-ASCII and nested paths. Also confirm that the QMD inventory sha is SHA-256 of the raw bytes.
- **E5:** Whether `raw_excerpt` shipped in N-1. If it did, removing it changes legacy QMD behavior and needs a release note.
- **E6:**
  - The `abandon_*` entry points and their fixture gating.
  - The terminal state each path writes.
  - Lock mode, and whether anything is deleted.
  - That a crash between the record write and the pointer release can be re-run safely.
- **E7:** That hoisted-package review resolves only inside the plugin-owned tree and stays pinned to exact hashes. A parent `node_modules` outside the plugin root must never be accepted.
- **E8:** The diffs for `update_hook_queue`, `runtime_handlers`, `runtime_admission`, `wiki_compile_enqueue` and `wiki_source_intent`.
- **E9:** Where `reserve_from_env()` runs relative to `Popen`, and whether host turns go through the `run` subcommand.
- **E10:** Whether a non-v2 (single-root, v1) Laya route can still be selected in this release. If it can, it indexes raw bodies, which conflicts with the boundary.

---

## 4. QA contract wording to tighten

These are documentation changes only. None of them weakens a data-safety row.

1. **Rename the raw-source case of "Present(T)".** Its definition requires that the token *not* be in the injected context, so "Present" is misleading. Use something like `Located(T)` for the legacy QMD raw-locator case.
2. **RQA-INST step 2, "if a card covers that source".** Make card coverage of `docs/qa/alpha.md` a fixture precondition. As written, the step can pass without proving anything.
3. **RQA-INST step 4.** With compile ON, "a current card plus verified locator within T_FRESH" must be a hard FAIL condition. "No raw fallback until a card exists" must not become an open-ended wait.
4. **Step 6 (Absent after deletion) on Laya.** Require evidence that freshness rejection excludes the card built from the deleted source. The token not appearing is not enough.

---

## 5. Minimal final installed-host QA set

### Preconditions

- The release candidate is exactly f89b141, or the last fix commit if E1–E3 force changes.
- If the commit changes, all source evidence must be redone.
- Re-cutover every Laya fixture created before 433089e.
- Get a new, explicitly approved model-call budget on a **new** ledger.
- The estimate below needs about 12–14 host or provider calls. Budget-reservation failures do not count as test failures.

### Non-model runs

These are scripted and use no model calls.

| Run | Covers | What to check |
|---|---|---|
| N1 | LI-MIG, LI-VECTOR, cutover | One fixture through plan → backup → restore-compare → candidate → seal → activate. Membership, FTS and vector tables contain **zero raw documents** (SQL count by collection role). The seeded cursor exists. The first drain shows `pending=0` (E4 in situ). |
| N2 | LI-RECOVER | Crash injection at two points: before the first new write (rollback succeeds, QMD still selected) and after the first new write (rollback refused, forward recovery completes). Backups are kept byte-identical. |
| N3 | Sync safety | (a) A marker present makes compile and baseline sync refuse with exit code 4. (b) The documented recovery path. (c) After recovery, no duplicate enqueue. |
| N4 | LI-COEXIST, source ownership | The global QMD binary, config and DB hashes are unchanged. Only plugin-owned runtime is classified as isolatable. |
| N5 | Compile-OFF row | A raw change creates no Laya document and no queue or ledger entry. Card bytes are unchanged. |
| N6 | Post-run identity | Re-diff the installed Claude and Codex caches against the committed blobs. There should be no `__pycache__`. This verifies 0d31f7c in the field. Check that the `.pending-compile` marker is absent. Audit the wake queue. |

### Host runs

These need fresh sessions with unique run markers.

| Run | Host | Turns | Covers |
|---|---|---|---|
| H1 | Claude and Codex | 1 + 1 | LI-INST: identity and journal verified at SessionStart. One negative case (journal or identity mismatch) on one host, which blocks with no QMD fallback. |
| H2 | Claude and Codex | 2 + 2 | LI-RECALL. (a) Wiki token: selected card, injected bytes and correct locator. (b) Raw-only token: no Laya result, no injection, answer `없음`. |
| H3 | One host | 2 + 1 compile | Freshness. Edit a raw file covered by a card. The stale card is not served. Discovery enqueues exactly that revision. After one compile, the fresh card is served. |
| H4 | One host | 1 | LI-SCOPE: a probe from a second fixture or worktree returns no cross-scope results. |
| H5 | One host | 2 | Update from N-1 with an old session open. Identity equals the candidate. Settings, wiki and index are preserved. The old session causes no damage. |

### Mapping to the 25 release rows

Map each of the 25 rows to one of N1–N6, H1–H5, or reused evidence. Any row left unmapped stays `NOT RUN` and blocks release.

---

## 6. Which existing evidence can be reused

| Evidence | Reusable? | Condition |
|---|---|---|
| f89b141 suite (1,658/9/0) and CI on Node 22/24/26 | Yes, as source evidence | Only if the release candidate is exactly f89b141 |
| Installed cache identity (937 files, Claude and Codex) | Yes | No reinstall since the check. Re-diffed post-run (N6). |
| Codex hook trust in the 20 fixtures | Yes | For those same fixtures |
| QMD-selected raw4/wiki7 hashes | Partly | Usable as the pre-cutover inventory input for N1, if tied to its inventory digest and backup. **Not** proof of path-format equality (E4). |
| Official sync baseline snapshot | Partly | Usable as a baseline only after confirming no `.pending-compile` marker exists |
| Wiki compile queue drained 1 → 0 | No | A single transient observation, not a release condition |
| Any v2 candidate, cursor or cutover row created before 433089e | No | Root set and cursor schema changed. Re-plan. |
| The 30/30 QA ledger | Audit record only | Do not reset it. A new budget needs explicit approval. |

The following are **not** established by any current evidence:

- wiki-only selected-route host QA
- cutover and forward recovery
- the 25 release rows

---
name: logs
description: Use when the user asks why MemVane recall was empty or degraded, what the plugin did recently, whether an index publish happened, or wants MemVane errors/diagnostics — e.g. "memvane 로그 보여줘", "왜 recall 이 비었어", "최근 업그레이드 됐어?", "memvane 에러 확인", "show memvane logs", "모든 프로젝트 로그 합쳐서 보여줘", "에러 상세 보여줘", "show unified logs", "show detailed errors". Reads the current project's install event log (events.jsonl), or every registered project's log together. Viewing is read-only; the only write is registering a project for the unified view, and only when the user asks for it.
---

# Logs

Summarize the MemVane install event log for the current project.

The log is `<install state root>/events.jsonl` (the selected route's install journal directory, owner-only `0700`), rotated past 4 MiB to `.1` and `.2`. One JSON line per hook action, recall, wiki card write, off-hook wake, index publish, resident service transition and bundle changes. Prompts are recorded as `sha256` and length.

## Workflow

1. Resolve the plugin root (the wrapper derives the core path from its own location):

   ```bash
   ROOT="${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:-$(git rev-parse --show-toplevel)}}"
   ```

2. Run the view that fits the question:

   ```bash
   python3 -B "$ROOT/skills/logs/scripts/logs.py" --cwd "$PWD"                      # summary of the last 24h
   python3 -B "$ROOT/skills/logs/scripts/logs.py" --cwd "$PWD" recalls --limit 10   # recent recalls and why they were empty/degraded
   python3 -B "$ROOT/skills/logs/scripts/logs.py" --cwd "$PWD" errors --hours 6     # refusals and failures
   python3 -B "$ROOT/skills/logs/scripts/logs.py" --cwd "$PWD" events --event wiki  # one event kind; add --json for raw lines
   python3 -B "$ROOT/skills/logs/scripts/logs.py" --cwd "$PWD" events --event wiki.backfill  # finite initial-pass progress
   python3 -B "$ROOT/skills/logs/scripts/logs.py" --cwd "$PWD" events --event wiki.card  # cards refused or split for the selector budget
   python3 -B "$ROOT/skills/logs/scripts/logs.py" --cwd "$PWD" errors --details     # "detailed errors": reassembled raw diagnostic text
   python3 -B "$ROOT/skills/logs/scripts/logs.py" --all errors                      # "unified logs": every registered project, UTC order
   python3 -B "$ROOT/skills/logs/scripts/logs.py" --cwd "$PWD" --slo               # service-level lines: each against its target (add --all for every project)
   python3 -B "$ROOT/skills/logs/scripts/logs.py" --register /path/to/project       # only when the user asks to add a project
   ```

   Natural-language mapping: "unified / all projects' logs" → `--all` (add a view such as `errors`); "detailed errors", "full traceback", "에러 상세" → `--details`. If `--all` reports no registry, tell the user which projects could be registered and run `--register` only after they ask.

3. Report what the lines say. For a recall, `reason` is the final outcome (`selected`, `no_results_after_filter`, `no_candidates`, a refusal code) and `notes` lists the diagnostics on the way (`laya_vector_*` = no query vector, `laya_selector_*` = local selection fell back to hybrid rank, `laya_recall_proof_retry`, `laya_wiki_rows_filtered`). `degraded` identifies a `worktree` or `reader_endpoint` read. A recall served by the new index format carries `store_format: schema3`; its `reason` `view_preparing` means this checkout's view has no published generation yet (a linked worktree's first use or a branch just switched to), and `checkout_changed` that the checkout kept moving during the read. Both give no context until the next sync publishes the view. `reader_settings_resealing` means the project's recall settings (such as `skipPaths`) changed and the index reader is re-sealed by the next sync, which this recall requested; it gives no context until then.

The summary reports each hook action's p50 and p95 from its latest 100 recorded durations. For recall it also reports the recorded semantic completion fraction, the latency targets, and the median of each recorded stage. A missing stage means it was not measured; it is not a zero-duration result. Event durations cover the recorded host action, not shell startup or the earlier hook classification.

With `--slo` the script prints service-level lines for the window (24 hours unless `--hours` is given), one block per install under `--all`. Each line ends its name with `ok`, `over` or `no-data`; `no-data` means fewer rows than the line needs (20, or 5 for `sync`), and `shown` marks a line that is reported without a target.

- `install`: whether the install is `large` (more than 1,000 wiki Markdown files under the route's wiki folder), `standard`, or `unknown` when the route cannot be read. A large install has its own `hook+selector` and `lexical` targets; for an unknown one the standard target judges and the large verdict is shown beside it.
- `hook`: prompt recall duration without the selector's time; `hook+selector`: the duration of recalls that asked the selector.
- `stage`: the median of each checkout observation stage against its own target, over recalls of one query vector (a recall with more vectors costs more by design). Each stage is judged on its own rows (`pinN`, `lexicalN`, …): a stage with fewer than 20 is shown and not judged, and the line is `no-data` only when no stage reaches 20.
- `semantic`: the share of recalls with a query vector whose semantic search completed, the share answered from the vector arena once one answers (a recall whose arena was switched off is left out), the counts of each unfinished state, and the recorded vector cache states. `vectorCache=unmeasured` means no row in the window recorded the state; it is not a zero. One recall whose cache state is `inconsistent` makes the line `over`.
- `selector`: the selector's duration and the share of answers that ran out of their time.
- `host <name>`: duration and semantic share per host.
- `dedup`: the share of injected cards the same session had already been given (after its last compaction or clear; for a host that sends no such signal, within its last 20 served recalls), and the recalls the exclusion left empty.
- `sync`: the trailing failed passes, and the time from a sync request to the end of the first pass that started after it and published; `unanswered` requests have no such pass yet. A request is a hook row that queued a sync (`reason` `sync_requested`); an edit outside the project or one that did not succeed queues none.
- `failed`: failed or refused rows that name no reason.
- `learning`: stored cases, teacher answers and cycle outcomes in the window, since when the cycle has been put off, and cycle rows that name no reason.
- `identity`: whether the device number recorded in the install's route is the current one. A moved number is expected after some restarts and is shown, not judged.

`recall observation p50` is the median of the stages that observe the checkout (`pin`, `lexical`, `stillCurrent`, `checkout`). Each is a few ms while the checkout observation cache serves; hundreds of ms mean every observation ran Git. `sync consecutive failed passes` counts the trailing `wake.sync` lines whose outcome is `failed` over every retained line, with their reasons and the time of the last pass that published; a pass still in progress neither counts nor ends the run. Under `--all` there is one such line per install. A count that keeps growing means the index is not publishing.


When the default teacher has run, the summary adds its lines:

- `teacher <date>`: per-case `learning.teacher` rows of that UTC day (`calls`), the gold labels of that day (`gold`, a kept answer committed later included), how many calls did not end in gold (`failed`, by reason) and the usage sums (`usageIn`, `usageOut`, `usageCached`, `usageReasoning`).
- `teacher selected`: how many cards each gold label selected (0 to 3). An empty selection is a valid label.
- `teacher models`: the host and resolved model of each gold label. This is the model in use; the `goldModel` value in the policy file is not.
- `teacher pending_review`: cases set aside for review, by reason. The teacher does not call them again.
- `teacher isolation`: calls refused by the isolation pre-check (`refused`, nothing sent) and calls that broke isolation during the run (`violated`).
- `teacher wait <host>`: the latest pass's wait reason for that host, when the wait started, and the cases queued for it. `teacher_host_unavailable` (no CLI), `teacher_isolation_incomplete` and `teacher_model_unresolved` need the user; `deferred_quota`, `teacher_provider_waiting` and `teacher_host_failing` clear on their own. Waiting cases stay queued and are never sent to the other host.
- `WARNING teacher tool calls`: calls during which the host CLI ran one of its built-in tools that cannot be turned off, with counts per tool name. The labels of those calls are kept; report the warning.
- `score <checkpoint>`: the operating score of a checkpoint over the cases captured while it was active, from the latest `learning.score` row: `exactRate` (Laya's selection equals the gold), `microF1`, `emptyAgree`, `nonEmptyAgree`, cases answered by the default rank instead of the selector (`defaultRank`, not scored), `unknownSelection` and gold cards outside the selector's window (`goldOutsideK`), with the `exactRate` trend across the window.
- `promotion`: the latest promotion decisions (`waiting`, `evaluated`, `applied`, `rejected`) with the training and evaluation case counts, empty-gold evaluation cases and both exact rates.
- `learning deferred`: learning cycles the resource admission deferred, by inner reason, and the time of the last finished training. A `learning.cycle` row with outcome `blocked` is a pass that met a job no retry reopens; `--slo` counts them on its `learning` line.
- `learning rotation`: closed cases deleted to keep the case table at 1,800 rows (`deleted`) and the number of passes that deleted any. `nothing_closed` counts plans that found the table over its limit with every row still protected, `deferred` plans that could not run.
- `edit recall targets`: where the edited files of the funnel's rows lie — `main` (the main checkout), `linked` (a linked git worktree of the same repository, taken as the main project's edit), `linked_unproved`, `mixed` and `outside` (never a case).
- `selector maintenance`: maintenance runs by result (`completed`, `deferred` and `failed` by reason). `learning_prepare_busy` means another install held the per-user prepare lease; the run comes back about a minute later and `busyRetriesMax` is the longest such run of retries. `incomplete(...)` names the step a `maintenance_incomplete` run stopped at (`prepare_status`, `heal_status`, `warm_status`, `publish_owner_busy`, `install_changed`, `teacher_enable_pending`, `selector_state_missing`, `renewal_refused`, `version_invalid`).

Event rows never hold the edit text, card bodies, source paths or the user's global instructions.

For `wiki.backfill`, `pending` and `dispatched` are unfinished source revisions;
`settled` means the compile worker finished a terminal attempt (including no-card
outcomes), and `skipped` means the frozen revision changed, vanished or became
excluded. Neither `settled` nor `complete` proves generated or verified cards.
Report card creation and verification separately from their own events.

For `wiki.card`, `over_budget` is a card the writer held back once because its
body exceeds the local selector budget (`size` / `budget`, `method` `tokenizer` or
`derive_limit`; `reason` `manual_section_over_budget` when the hand-written
sections alone overflow). `split` / `split_unavailable` report the one split
re-prompt per source (`requested`, `parts`, `written`; for `split_unavailable`
the `reason` the original card was written instead: the split could not run,
exceeded the free card slots, or had a part the writer would not write the way it writes the original). `over_budget_written` is a card written over the budget after
that attempt (best effort; the selector scores it by its leading part).
`measure_failed` (no selector runtime could count) and `unmeasured` (no selector
tokenizer on the machine) mark cards written without a size check.
`merge_kept_separate` is a dedup merge refused because the keeper is over the
budget. `measured_after_failure` (`reason` `runtime_fallback`) is a card that fit
after one selector runtime failed and another counted it; this and every other
`wiki.card` outcome measured after such a failure carry `measureId`, `attempts`
and the `failure` codes.

`index.build.progress` lines record the latest completed pass's stage and available ingest or vector counts. These are snapshots; when no recent line is available, `index-build-status` reports unknown counts rather than zero.

A `wake.sync` line that settled compile intents carries `provenance`:
`consumed` (verified cards whose compile evidence the sync recorded in the
index), `superseded` (an older intent for the same card path replaced by a
newer consumed one), `waiting` (the card on disk is not yet the verified bytes)
and `rejected` counts by fixed reason: `provenance_parity_mismatch` (the card's
`sourceRevisions`/`sourceSpans` differ from the intent),
`provenance_conflict` (the index already holds different evidence under the
same identity) and `provenance_value_invalid` (a corrupt, misnamed or invalid
intent), and `unsafe` counts intent files (or the intent directory) that are
not settled private files (symlink, foreign mode, unreadable, still being
linked): they are skipped, never renamed. A full disk, an I/O error or a store
binding change ends the pass with the intent pending instead of rejecting it.
The field appears only when an intent was consumed, superseded or rejected, or
an unsafe file was skipped, in a pass that finished publishing. Recorded evidence does not change
which cards recall serves or their trust. The verify side's provenance reasons
(`provenance_checkout_unavailable`, `provenance_checkout_changed`,
`provenance_value_invalid`, `provenance_intent_write_failed`) are in the
project's `verify-log.jsonl`, not in this log.

## Detailed diagnostics

Recorded only while the project's settings have `diagnostics.enabled: true` (default off; only the JSON boolean `true`). Turning it off stops new captures but does not delete existing ones. Not every failure path records detail, so a missing diagnostic does not mean nothing failed.

- `exception`: a Python traceback including the exception message.
- `origin_stack`: the caller's stack where a warning was noticed (no exception).
- `child_stderr_tail`: only the last 4096 bytes of a child process's stderr, not its complete stack; the beginning may be cut off.
- `incomplete`: parts were rotated away, missing, or failed the hash check. Say so; do not guess the missing text.

Without `--details`, output (text and `--json`) shows only a header's `diagnostic=<id>` and fixed metadata; raw parts and old `prompt.text` stay hidden. With `--details`, raw text is printed terminal-escaped.

`--all` merges every target in the owner-private registry (`$XDG_CONFIG_HOME/memvane/log-targets.json`, `~/.config` when unset) under one fair 16 MiB budget split between readable targets. Targets that cannot be read appear with a reason (`missing`, `not_onboarded`, `stale_route`, `registry_*`); report them rather than treating them as quiet.

## Safety

- Viewing is read-only: the script opens the state root without following symlinks and writes nothing.
- `--register PROJECT` is the only write. It touches only the private registry file (root path and install ID, no event content), and is run only on the user's explicit request. Report each per-project result line.
- `--details` output may contain secrets from raw exception messages or child stderr. Use it only when the user asks for detailed errors, and summarize rather than echo tokens, keys or credentials.
- A project without a selected route exits 1; point the user to the setup skill.
- A `WARNING` header line means events could not be written (unsafe state root, foreign or symlinked `events.jsonl`, or a refused-write count); report it instead of reading silence as "nothing happened".
- Older log lines may contain `prompt.text`; do not reproduce it.

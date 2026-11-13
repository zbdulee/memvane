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
   python3 -B "$ROOT/skills/logs/scripts/logs.py" --register /path/to/project       # only when the user asks to add a project
   ```

   Natural-language mapping: "unified / all projects' logs" → `--all` (add a view such as `errors`); "detailed errors", "full traceback", "에러 상세" → `--details`. If `--all` reports no registry, tell the user which projects could be registered and run `--register` only after they ask.

3. Report what the lines say. For a recall, `reason` is the final outcome (`selected`, `no_results_after_filter`, `no_candidates`, a refusal code) and `notes` lists the diagnostics on the way (`laya_vector_*` = no query vector, `laya_selector_*` = local selection fell back to hybrid rank, `laya_recall_proof_retry`, `laya_wiki_rows_filtered`). `degraded` identifies a `worktree` or `reader_endpoint` read. A recall served by the new index format carries `store_format: schema3`; its `reason` `view_preparing` means this checkout's view has no published generation yet (a linked worktree's first use or a branch just switched to), and `checkout_changed` that the checkout kept moving during the read. Both give no context until the next sync publishes the view. `reader_settings_resealing` means the project's recall settings (such as `skipPaths`) changed and the index reader is re-sealed by the next sync, which this recall requested; it gives no context until then.


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

---
name: logs
description: Use when the user asks why MemVane recall was empty or degraded, what the plugin did recently, whether a bundle upgrade or index publish happened, or wants MemVane errors/diagnostics — e.g. "memvane 로그 보여줘", "왜 recall 이 비었어", "최근 업그레이드 됐어?", "memvane 에러 확인", "show memvane logs". Reads the current project's install event log (events.jsonl). Read-only — never edits files or state.
---

# Logs

Summarize the MemVane install event log for the current project.

The log is `<install state root>/events.jsonl` (the selected route's install journal directory, owner-only `0700`), rotated past 4 MiB to `.1` and `.2`. One JSON line per hook action, recall, wiki card write, off-hook wake, index publish, resident service transition and bundle upgrade. Prompts are recorded as `sha256` and length.

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
   python3 -B "$ROOT/skills/logs/scripts/logs.py" --cwd "$PWD" upgrades             # bundle upgrade / journal conversion
   python3 -B "$ROOT/skills/logs/scripts/logs.py" --cwd "$PWD" migration            # private shadow migration steps, outcomes and reader rollbacks
   python3 -B "$ROOT/skills/logs/scripts/logs.py" --cwd "$PWD" events --event wiki  # one event kind; add --json for raw lines
   python3 -B "$ROOT/skills/logs/scripts/logs.py" --cwd "$PWD" events --event wiki.backfill  # finite initial-pass progress
   python3 -B "$ROOT/skills/logs/scripts/logs.py" --cwd "$PWD" events --event wiki.card  # cards refused or split for the selector budget
   ```

3. Report what the lines say. For a recall, `reason` is the final outcome (`selected`, `no_results_after_filter`, `no_candidates`, a refusal code) and `notes` lists the diagnostics on the way (`laya_vector_*` = no query vector, `laya_selector_*` = local selection fell back to hybrid rank, `laya_recall_proof_retry`, `laya_wiki_rows_filtered`). `degraded` is `bundle_drift` (read-only after a plugin update) or `worktree`.

Upgrade lines can include an opaque `operation` and `attempt`; matching start and final lines describe one execution. A bridge failure can include its fixed `stage` and `bridgeHost`.

The `migration:` summary line describes the private shadow migration that
follows a completed sync; the selected reader does not change while it runs.
`state` is `copying`, `caught_up` or `shadow_verified`; `last` is the latest
operation (`begin`, `copy`, `verification`, `base_corpus`, `sidecar_corpus`,
`prepare`, `publish`, `vectors`, `parity`, `parity_seal`) and its outcome;
`progress` is that operation's position in this order. `deferred` counts steps
held back with nothing written: `migration_space_deferred` means the disk lacks
the projected copy space (`needed_bytes` / `free_bytes` on the line), and the
migration resumes on its own once space returns. `restarts` counts source changes
that sent the copy back, `retries` passes that stopped for a later retry, and
`errors` steps that failed with a fixed `reason`
(`migration_recall_parity_mismatch` and `migration_semantic_parity_mismatch` are
parity failures). `rollback` is the latest explicit reader rollback outcome:
`rolled_back` (the previous index reader is selected again), `retained` (a
repeated request; the journal already records it) or `refused` (the line in the
`migration` view carries the fixed `reason`; `rollback_lifecycle_busy` and other
busy codes mean nothing was written and the request can be repeated later).

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
budget.

## Safety

- Read-only: the script opens the state root without following symlinks and writes nothing.
- A project without a selected route exits 1; point the user to the setup skill.
- A `WARNING` header line means events could not be written (unsafe state root, foreign or symlinked `events.jsonl`, or a refused-write count); report it instead of reading silence as "nothing happened".
- Older log lines may contain `prompt.text`; do not reproduce it.

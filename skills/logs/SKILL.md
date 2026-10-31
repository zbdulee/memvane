---
name: logs
description: Use when the user asks why MemVane recall was empty or degraded, what the plugin did recently, whether a bundle upgrade or index publish happened, or wants MemVane errors/diagnostics — e.g. "memvane 로그 보여줘", "왜 recall 이 비었어", "최근 업그레이드 됐어?", "memvane 에러 확인", "show memvane logs". Reads the current project's install event log (events.jsonl). Read-only — never edits files or state.
---

# Logs

Summarize the MemVane install event log for the current project.

The log is `<install state root>/events.jsonl` (the selected route's install journal directory, owner-only `0700`), rotated past 4 MiB to `.1` and `.2`. One JSON line per hook action, recall, wiki card write, off-hook wake, index publish, resident service transition and bundle upgrade. Prompts appear only as `sha256` + length unless the owner set `MEMVANE_EVENT_LOG_PROMPTS=1`.

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
   python3 -B "$ROOT/skills/logs/scripts/logs.py" --cwd "$PWD" events --event wiki  # one event kind; add --json for raw lines
   python3 -B "$ROOT/skills/logs/scripts/logs.py" --cwd "$PWD" events --event wiki.backfill  # finite initial-pass progress
   python3 -B "$ROOT/skills/logs/scripts/logs.py" --cwd "$PWD" events --event wiki.card  # cards refused or split for the selector budget
   ```

3. Report what the lines say. For a recall, `reason` is the final outcome (`selected`, `no_results_after_filter`, `no_candidates`, a refusal code) and `notes` lists the diagnostics on the way (`laya_vector_*` = no query vector, `laya_selector_*` = local selection fell back to hybrid rank, `laya_recall_proof_retry`, `laya_wiki_rows_filtered`). `degraded` is `bundle_drift` (read-only after a plugin update) or `worktree`.

For `wiki.backfill`, `pending` and `dispatched` are unfinished source revisions;
`settled` means the compile worker finished a terminal attempt (including no-card
outcomes), and `skipped` means the frozen revision changed, vanished or became
excluded. Neither `settled` nor `complete` proves generated or verified cards.
Report card creation and verification separately from their own events.

For `wiki.card`, `over_budget` is a card the writer held back once because its
body exceeds the local selector budget (`size` / `budget`, `method` `tokenizer` or
`derive_limit`; `reason` `manual_section_over_budget` when the hand-written
sections alone overflow). `split` / `split_unavailable` report the one split
re-prompt per source (`requested`, `parts`, `written`, `dropped`, `reason` when
it could not run). `over_budget_written` is a card written over the budget after
that attempt (best effort; the selector scores it by its leading part).
`measure_failed` (no selector runtime could count) and `unmeasured` (no selector
tokenizer on the machine) mark cards written without a size check.
`merge_kept_separate` is a dedup merge refused because the keeper is over the
budget.

## Safety

- Read-only: the script opens the state root without following symlinks and writes nothing.
- A project without a selected route exits 1; point the user to the setup skill.
- A `WARNING` header line means events could not be written (unsafe state root, foreign or symlinked `events.jsonl`, or a refused-write count); report it instead of reading silence as "nothing happened".
- Never paste a `prompt.text` field back unless the user asks; it exists only when the owner enabled the debug flag.

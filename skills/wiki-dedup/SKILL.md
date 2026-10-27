---
name: wiki-dedup
description: Use when the user asks to clean up, dedupe, merge, or consolidate near-duplicate wiki pages that already exist on disk — e.g. "위키 중복 정리해줘", "dedup 돌려줘", "dedup-needed 처리해줘", "중복 카드 합쳐줘", "clean up duplicate wiki pages". Resolves the retroactive-scan queue at .auto-context/compile/dedup-needed.jsonl by spawning the wiki-dedup-resolver agent. Write-time merge-needed.jsonl remains a passive collision diagnostic and is outside this workflow.
---

# Wiki Dedup

Drain `.auto-context/compile/dedup-needed.jsonl` — pairs of **already-existing** wiki pages that
`core/wiki_dedup_scan.py`'s retroactive scan found similar enough (vector similarity at or above
`compile.semanticDedup.autoMergeThreshold`) to be worth consolidating. This is the user-facing
entry point for the same cleanup the `core/update.sh` SessionStart hint spawns automatically; use it
when the user asks for it directly, or when the automatic hint didn't run.

This workflow never consumes `merge-needed.jsonl`; that file is a passive diagnostic for
write-time collisions. Here every page is a real file already on disk, and the resolver compares
each queued pair autonomously.

## Workflow

1. Resolve the plugin root and check the queue:

   ```bash
   ROOT="${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:-$(git rev-parse --show-toplevel)}}"
   cat "$PWD/.auto-context/compile/dedup-needed.jsonl" 2>/dev/null
   ```

   If the queue is missing or empty, also check for a
   `dedup-needed.jsonl.claimed.*` file. A claimed file can be a recoverable
   interrupted run; spawn the resolver so its first queue-recovery step can
   check ownership. Stop only when both the queue and claimed files are absent.

2. Spawn the resolver — **do not run its per-pair judgment inline in the main thread**, and do not
   re-type its workflow here (it lives in `agents/wiki-dedup-resolver.md`, the single source of
   truth, which the SessionStart hint also reads):

   - **Claude Code**: spawn the bundled agent with the Agent tool, `subagent_type:
     'qmd-auto-context:wiki-dedup-resolver'`. Its system prompt already carries the full workflow
     (run-lock, cluster handling, fold-then-delete, `core/wiki_dedup_resolve.py` calls) — just tell
     it the target project root, the plugin root (`$ROOT`), and — critically — that it was **spawned
     by the wiki-dedup skill on an explicit user request, so it must report the step-3 summary
     rather than finishing silently** (its default when the SessionStart hint spawns it is silence).
   - **Codex / Hermes Agent** (no bundled agent-role field in their manifests): read the workflow
     block (delimited by the WORKFLOW start/end markers) out of `"$ROOT/agents/wiki-dedup-resolver.md"`
     and spawn a subagent via your host's own delegation mechanism (Codex's multi-agent tool,
     Hermes's `delegate_task` tool with role `leaf`) using that block verbatim as the subagent's
     prompt, with the target project root filled in.

3. When the resolver finishes, report a short summary: pairs resolved, cards deleted (filenames),
   cards merged, pairs skipped (with one-line reasons), and whether the queue is now empty.

## Safety

- Read-only for the queue in step 1 (`cat` and claimed-file existence check); every mutation goes through the resolver's
  `core/wiki_dedup_resolve.py` calls — never edit the queue file or the resolver scripts directly.
- The resolver acquires a per-project run-lock (`~/.local/state/laya/auto-context/dedup-resolve-lock/<project_key>`);
  if one is already active it will stop on its own. Don't bypass it by editing pages by hand.
- Cards with `status: generated` (unreviewed) are valid targets — the resolver handles them.
- For a selected Laya project, the resolver CLI keeps a private copy of each
  deleted card and durably requests `laya-sync` after deletion. A failed wake
  leaves the pair queued for a stale-pair retry; successful completion means
  the request was queued, not that the new index generation has published.

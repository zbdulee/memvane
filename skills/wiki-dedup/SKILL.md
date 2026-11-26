---
name: wiki-dedup
description: Use when the user asks to clean up, dedupe, merge, or consolidate near-duplicate wiki pages that already exist on disk — e.g. "위키 중복 정리해줘", "dedup 돌려줘", "dedup-needed 처리해줘", "중복 카드 합쳐줘", "clean up duplicate wiki pages". Resolves the retroactive-scan dedup queue in the project ledger by spawning the wiki-dedup-resolver agent. Write-time merge-needed log rows remain a passive collision diagnostic and are outside this workflow.
---

# Wiki Dedup

Drain the project ledger's dedup queue — pairs of **already-existing** wiki pages that
`memvane/wiki/dedup/wiki_dedup_scan.py`'s retroactive scan found similar enough (cosine similarity of 0.9 or more
between the cards' stored memvane-db vectors) to be worth consolidating. The scan runs in the background
after SessionStart and source edits on a selected project and only fills the queue; this skill is
the one entry point that drains it.

This workflow never consumes the `merge_needed` log rows; they are a passive diagnostic for
write-time collisions. Here every page is a real file already on disk, and the resolver compares
each queued pair autonomously.

## Workflow

1. Resolve the plugin root and check the queue:

   ```bash
   ROOT="${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:-$(git rev-parse --show-toplevel)}}"
   python3 "$ROOT/entry/run.py" wiki-ledger dedup-pairs --cwd "$PWD" --json
   ```

   It prints a JSON array of `{index, pageA, pageB, score}` (read-only). Stop when it is empty.

2. Spawn the resolver — **do not run its per-pair judgment inline in the main thread**, and do not
   re-type its workflow here (it lives in `agents/wiki-dedup-resolver.md`, the single source of
   truth):

   - **Claude Code**: spawn the bundled agent with the Agent tool, `subagent_type:
     'memvane:wiki-dedup-resolver'`. Its system prompt already carries the full workflow
     (run-lock, cluster handling, fold-then-delete, `memvane/wiki/dedup/wiki_dedup_resolve.py` calls) — just tell
     it the target project root and the plugin root (`$ROOT`).
   - **Codex / Hermes Agent** (no bundled agent-role field in their manifests): read the workflow
     block (delimited by the WORKFLOW start/end markers) out of `"$ROOT/agents/wiki-dedup-resolver.md"`
     and spawn a subagent via your host's own delegation mechanism (Codex's multi-agent tool,
     Hermes's `delegate_task` tool with role `leaf`) using that block verbatim as the subagent's
     prompt, with the target project root filled in.

3. When the resolver finishes, report a short summary: pairs resolved, cards deleted (filenames),
   cards merged, pairs skipped (with one-line reasons), and whether the queue is now empty.

## Safety

- Step 1 only reads the queue; every mutation goes through the resolver's
  `memvane/wiki/dedup/wiki_dedup_resolve.py` calls — never edit the ledger or the resolver scripts directly.
- The resolver acquires a per-project run-lock (`~/.local/state/memvane/v1/auto-context/dedup-resolve-lock/<project_key>`);
  if one is already active it will stop on its own. Don't bypass it by editing pages by hand.
- Cards with `status: generated` (unreviewed) are valid targets — the resolver handles them.
- For a selected MemVane project, the resolver CLI keeps a private copy of each
  deleted card and durably requests `laya-sync` after deletion. A failed wake
  leaves the pair queued for a stale-pair retry; successful completion means
  the request was queued, not that the new index generation has published.

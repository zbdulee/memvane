---
name: update
description: Use when the user asks to manually refresh, resync, or reconcile the memvane-db index for a project — e.g. "인덱스 새로고침", "동기화해줘", "놓친 변경 반영", "refresh the index". Queues the selected memvane-db index's full-scan sync, which reconciles created, changed and deleted files.
---

# Update

Queue a full-scan sync of the project's selected memvane-db index.

## Workflow

1. Confirm the target cwd.
2. Resolve the plugin root: the directory where the plugin is installed, which differs from the target project (`$PWD`). Use the env var the hooks already set; the git-toplevel fallback is correct only inside a plugin checkout. The wrapper itself derives the plugin root from its own location:

   ```bash
   ROOT="${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:-$(git rev-parse --show-toplevel)}}"
   ```
3. Run the bundled wrapper:

   ```bash
   bash "$ROOT/skills/update/scripts/update.sh" "$PWD" codex
   ```

4. Report the wake as queued, not as completed. The durable worker runs it off the hook.

Use the current host CLI name (`codex`, `claude`, `hermes`, or `gemini`) as the
second argument; the example above is for Codex. The wrapper can also use an
explicit `MEMVANE_HOST`/`QMD_ENGINE` or a current host session marker. A known
host never switches to another CLI. If the host is unknown, real extraction
tries Codex, Claude, then Hermes at most once each and keeps the first successful
producer for verification; there is no separate paid probe.

## Safety

- The wrapper acts only for a selected MemVane route. A project that is not onboarded exits 1 with an onboarding notice; point the user to the setup skill.
- A selected route whose proof fails exits 3; report the reason.
- Preserve empty output as a valid graceful no-op state.

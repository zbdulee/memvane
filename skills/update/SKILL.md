---
name: update
description: Use when the user asks to manually refresh or rebuild the qmd auto-context index for a project — e.g. "qmd 갱신해줘", "인덱스 새로고침", "refresh the index", SessionStart-style refresh. Runs core/update.sh and honors .auto-context/settings.json opt-in. For recovering missed filesystem create/update/delete events, prefer the sync skill instead.
---

# Update

Run the qmd auto-context update path manually for the current project.

## Workflow

1. Confirm the target cwd.
2. Resolve the plugin root: the directory where the qmd-auto-context plugin is installed, which differs from the target project (`$PWD`). Use the env var the hooks already set; the git-toplevel fallback is correct only inside a qmd-auto-context checkout. The wrapper itself derives the plugin root from its own location:

   ```bash
   ROOT="${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:-$(git rev-parse --show-toplevel)}}"
   ```
3. Run the bundled wrapper. Flags pass through to `core/update.sh` (e.g. `--recommend` for read-only recommendation, `--resolve-only`):

   ```bash
   bash "$ROOT/skills/update/scripts/update.sh" "$PWD"
   ```

4. If qmd is missing or unsupported, report the wrapper's pinned install guidance.
5. Report whether update ran, skipped, or was blocked by missing config/qmd.

## Safety

- Do not bypass `.auto-context/settings.json` opt-in.
- Do not auto-install qmd. The wrapper checks the plugin-tested qmd version and prints install guidance when needed.
- Prefer `sync` when the user specifically asks to detect missed filesystem CUD events.
- Preserve empty output as a valid graceful no-op state.
- The wrapper ensures, warms, and rotates the plugin-managed backend before update.
- The wrapper runs the QMD update path via `core/update.sh`. It does not perform the hook's Laya route classification.

---
name: query
description: Use when the user asks to manually query, search, recall, or look up project docs via auto-context — e.g. "문서에서 찾아줘", "auto-context 검색", "recall this", "look up in notes". Runs the same Laya recall hook a prompt uses, against the project's selected Laya index. Read-only — never edits files.
---

# Query

Run a manual auto-context recall query for the current project.

## Workflow

1. Confirm the target cwd and query text.
2. Resolve the plugin root: the directory where the plugin is installed, which differs from the target project (`$PWD`). Use the env var the hooks already set; the git-toplevel fallback is correct only inside a plugin checkout. The wrapper itself derives the plugin root from its own location:

   ```bash
   ROOT="${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:-$(git rev-parse --show-toplevel)}}"
   ```
3. Run the bundled wrapper:

   ```bash
   bash "$ROOT/skills/query/scripts/query.sh" "$PWD" "question or keywords"
   ```

4. Report the returned context or say that recall returned no context.

## Safety

- The wrapper answers only for a selected Laya route (or a linked git worktree of a selected main checkout, read-only). A project that is not onboarded exits 1 with an onboarding notice; point the user to the setup skill.
- A selected route whose proof fails exits 3; report the reason and do not search another way.
- Preserve empty output as a valid no-result state.

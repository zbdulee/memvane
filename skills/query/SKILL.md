---
name: query
description: Use when the user asks to manually query, search, recall, or look up project docs via qmd auto-context — e.g. "qmd 검색", "문서에서 찾아줘", "recall this", "look up in notes". Runs the read-only QMD recall path (core/recall.py) without the UserPromptSubmit hook's Laya route classification and honors .auto-context/settings.json opt-in. Read-only — never edits files. Prefer this over calling the qmd daemon directly.
---

# Query

Run a manual qmd auto-context recall query for the current project.

## Workflow

1. Confirm the target cwd and query text.
2. Resolve the plugin root: the directory where the qmd-auto-context plugin is installed, which differs from the target project (`$PWD`). Use the env var the hooks already set; the git-toplevel fallback is correct only inside a qmd-auto-context checkout. The wrapper itself derives the plugin root from its own location:

   ```bash
   ROOT="${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:-$(git rev-parse --show-toplevel)}}"
   ```
3. Run the bundled wrapper:

   ```bash
   bash "$ROOT/skills/query/scripts/query.sh" "$PWD" "question or keywords"
   ```

4. If qmd is missing or unsupported, report the wrapper's pinned install guidance instead of querying directly.
5. Report the returned context or say that recall returned no context.

## Safety

- Do not bypass `.auto-context/settings.json` opt-in.
- Do not auto-install qmd. The wrapper checks the plugin-tested qmd version and prints install guidance when needed.
- Do not query qmd directly unless debugging the wrapper.
- Preserve empty output as a valid no-result state.
- The wrapper ensures the plugin-managed backend before recall.
- The wrapper runs the QMD recall path via `core/recall.py`. It does not perform the hook's Laya route classification.

---
name: wiki-compile
description: Use when the user asks to save, compile, promote, or write a durable project/session conclusion into the MemVane wiki. Accepts only compact durable summaries or candidate JSON; never paste a raw transcript.
---

# Wiki Compile

Create or queue generated wiki markdown from a compact durable conclusion.

## Workflow

1. Confirm the target cwd and a compact durable summary. The input should be a short fact/decision/rule/card, not raw transcript.
2. Resolve the plugin root:

   ```bash
   ROOT="${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:-$(git rev-parse --show-toplevel)}}"
   ```

3. Run the bundled wrapper with JSON on stdin:

   ```bash
   printf '%s\n' '{
     "trigger": "manual",
     "sourceRef": "session:local",
     "durable": {
       "title": "Config layout decision",
       "summary": "Canonical config lives under .memvane/settings.json.",
       "type": "decision",
       "confidence": "high"
     }
   }' | bash "$ROOT/skills/wiki-compile/scripts/wiki-compile.sh" "$PWD"
   ```

4. Report the action from stdout (`created`, `candidate`, `rejected`, `tombstoned`, etc.) and the generated path when present.

## Input contract

The deterministic implementation path is `core/wiki_extract.py`, which converts compact input into `core/wiki_compile.py` candidate JSON.

Supported compact shape:

```json
{
  "trigger": "manual",
  "sourceRef": "session:local",
  "sourceKind": "session",
  "durable": {
    "title": "Short durable title",
    "summary": "Bounded reusable conclusion, not a transcript.",
    "type": "decision",
    "confidence": "high"
  }
}
```

Also supported: `{"candidates":[...]}` with the same compact fields.

## Safety

- Do not paste raw transcript, role-labeled chat logs, credentials, or temporary progress.
- Query-time recall remains read-only; this skill is an explicit writer path.
- The wrapper honors the project settings (`.memvane/settings.json`) opt-in and `compile.mode` (activation is `mode != "off"`) through core config.
- Card-writing rules live in the bundle: `$ROOT/core/wiki_card_rules/default.md` (and `novel.md` for a novel project, e.g. one with `04_Manuscript/` at its root). Read them before composing a summary; the project wiki has no rules file of its own.
- The writer emits `status: generated`; recall serves a card only after auto-verify promotes it to `verified`. A card is never canon either way.
- If the compact input is transcript-shaped, the extractor/writer rejects it and redacts transcript text in the candidate queue.

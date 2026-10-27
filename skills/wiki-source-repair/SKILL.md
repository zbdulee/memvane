---
name: wiki-source-repair
description: Use when the user asks to fix, repoint, or clean up wiki cards whose source documents went missing — e.g. "소스 소실 카드 고쳐줘", "source-missing 처리해줘", "wiki 원문 링크 깨진 것 정리해줘", "repair missing wiki sources". Resolves the pending detections in .auto-context/compile/source-missing.jsonl by repointing a card's sources[].path to the renamed file, or recording a dismissal. Never deletes or downgrades a card.
---

# Wiki Source Repair

Resolve wiki cards whose `sources[].path` entries no longer exist on disk. The scanner
(`core/wiki_source_scan.py`, run from the SessionStart background worker) and the machine
verifier both append detections to `.auto-context/compile/source-missing.jsonl`, and the
SessionStart notice surfaces the pending count.

**Why repointing and not deletion**: sources usually go missing because files are *renamed*
(`…07-20.md` → `…07-21.md`), not deleted — and a rename is indistinguishable from a deletion
on the filesystem. A card whose source is gone may be
the **only remaining record** of that knowledge, so this skill never deletes a card and never
downgrades `verified` → `generated` (that would hide the card from recall under the default
`recallVerifiedOnly: true`).

## Workflow

1. Resolve the plugin root and read the pending detections, **grouped by missing source file**:

   ```bash
   ROOT="${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:-$(git rev-parse --show-toplevel)}}"
   # third arg = batch limit, counted in MISSING FILES (omit or 0 for the whole queue)
   bash "$ROOT/skills/wiki-source-repair/scripts/wiki-source-repair.sh" "$PWD" list 10
   ```

   `pending: 0` → tell the user there is nothing to repair and stop.

   **The decision unit is the missing file, not the card.** Many pending cards usually cite a
   few missing files, so a per-card list would show the same file over and over and split one
   decision across many sessions.

   Each entry is one missing file: `missingSource`, `cardCount` (how many pending cards cite
   it), `trustedCount`, `detectedAt` (the oldest card's detection), `candidates` (rename
   suggestions from that path's own directory, ranked by filename similarity, computed once
   per file), and `cards` — a sample of the citing cards, capped, with `cardsTruncated` when
   there are more. `pending` stays the **card** count (it must equal the SessionStart notice
   number); `groups` is the number of distinct missing files and `returned`/`truncated`
   describe the slice you got.

   `trusted` on a card is true only for a current card with `status: verified`,
   `createdBy: qmd-auto-context`, and non-empty compiler-owned `sourceRevisions`; the ledger's
   stored status alone never grants trust. Cards that no longer exist are not listed — the
   ledger is append-only, so a deleted card's `detected` row would otherwise inflate the queue
   forever.

   **Work in batches.** Groups come `trustedCount` first (those cards are injected as canon
   right now while their sources cannot be checked), then the largest card count (a decision
   that resolves more cards is worth more), then oldest detection — so a batch drain is
   reproducible across sessions. Tell the user how many files remain and stop.

2. For each entry, show the user the missing file, how many cards cite it, and the suggested
   candidates. **Ask which candidate is the same document** (or whether the source is
   genuinely gone). Never pick a candidate on the user's behalf — and this matters more for
   the bulk verbs: repointing to the wrong file makes **every citing card** point at an
   unrelated source, and a later verify run would then delete them all.

3. Apply the user's decision **once per missing file**:

   ```bash
   # the file was renamed -> repoint every pending card that cites it
   bash "$ROOT/skills/wiki-source-repair/scripts/wiki-source-repair.sh" "$PWD" \
     repoint-source <oldSourcePath> <newSourcePath>

   # the file is really gone; keep the cards as the only record
   bash "$ROOT/skills/wiki-source-repair/scripts/wiki-source-repair.sh" "$PWD" \
     dismiss-source <sourcePath>
   ```

   Both print **per-card results** (`cards[]`) plus `applied`/`failed`; `ok` is false if any
   single card failed, so a partial failure can never read as success. Use the per-card verbs
   (`repoint <cardPath> <old> <new>`, `dismiss <cardPath>`) only when cards citing the same
   file need different decisions.

   `repoint` rewrites only that one `sources` entry's `path` in each card's frontmatter and
   never touches `status`. A card with several missing sources reports the rest in
   `stillMissing`; it leaves the pending queue because **only cards whose sources are all
   missing** are pending (a card with one live source can still be checked against it) — the
   remaining broken link is filtered out of recall injection rather than shown as stale.

4. Report per file: what was repointed (from → to) or dismissed, how many cards it covered,
   and the remaining count from a final `list`.

## Safety

- Read-only in steps 1–2; every mutation goes through step 3's wrapper. Never hand-edit
  `source-missing.jsonl` — it is an append-only, never-trimmed audit ledger where the latest
  row per card *is* the state.
- `repoint` refuses a target that does not exist or resolves outside the project root.
- Unsupported `sources` spellings (single-line flow sequence, block mapping) are refused with
  `source_entry_not_found` / `frontmatter_missing` rather than rewritten — fix those by hand.
- Card `status` is never changed by this skill.
- Write-time `merge-needed.jsonl` is a passive, non-trusted collision diagnostic and is not
  consumed or resolved by this skill.

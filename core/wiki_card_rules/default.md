Card-writing rules (bundled with the plugin; the project wiki holds only cards).

- A wiki card is auxiliary recall knowledge, never canon. The project's own source files stay authoritative; when a card and its source disagree, the source wins.
- One durable conclusion per card: a rule, mechanism, entity fact, decision with its reason, or comparison. Not a transcript, chat log, step-by-step progress note, or temporary status.
- Never record secrets, API keys, tokens, or credentials. Name the key and write the value as <REDACTED>, or omit it.
- The plugin writes the card file and its frontmatter; extraction supplies only the candidate fields. Recall serves a card only when it has `status: verified` (set by auto-verify), a plugin `createdBy`, and provenance: every file entry in `sources` has one matching `sourceRevisions` entry with the same `kind`, project-relative `path` and `collection`, recording the current SHA-256 and size of an existing project-internal raw source. Wiki cards, session notes and unregistered paths are never revision proof.
- When a declared source changes, the card is regenerated through compile. Never hand-copy a hash or rely on session-only provenance for a source-backed card.

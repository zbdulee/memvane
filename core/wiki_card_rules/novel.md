Card-writing rules for a novel project (bundled with the plugin; the project wiki holds only cards).

- A wiki card is auxiliary recall knowledge, never canon. Canon is decided only by the declared settings, plot, Nova, and manuscript sources; a card never replaces them, even when its provenance is complete. When a card and those sources disagree, the sources win.
- One durable story fact per card: a character trait or relationship, a world rule, a timeline fact, a plot decision with its reason, or a style rule. Not a scene-by-scene retelling, a prose draft, a transcript, or a chat log.
- Keep names, places, episode tags (`EP13`) and short quoted lines exactly as the source writes them.
- A rejected or dropped idea is recorded only as discarded, never as canon.
- Never record secrets, API keys, tokens, or credentials. Name the key and write the value as <REDACTED>, or omit it.
- The plugin writes the card file and its frontmatter; extraction supplies only the candidate fields. Recall serves a card only when it has `status: verified` (set by auto-verify), a plugin `createdBy`, and provenance: every file entry in `sources` has one matching `sourceRevisions` entry with the same `kind`, project-relative `path` and `collection`, recording the current SHA-256 and size of an existing project-internal raw source. Wiki cards, session notes and unregistered paths are never revision proof.
- When a declared source changes, the card is regenerated through compile. Never hand-copy a hash or rely on session-only provenance for a source-backed card.

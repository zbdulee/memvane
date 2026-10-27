# Install cutover writer and failure audit

The managed shadow index covers `.auto-context/wiki/**/*.md`. `activate` holds the project install journal lock, then `.topical-publish.lock`, through final corpus/DB checks, pointer selection, daemon handoff, and the `activated` journal write. The project lock is owner-only and reentrant within one thread; another process must wait. Manual edits outside these product paths do not take the lock and need normal reindex after activation. A manual edit detected during cutover causes rollback.

| Product wiki writer | Mutation | Lock path |
|---|---|---|
| `core/update.sh --init-wiki` | scaffold `SCHEMA.md`, `index.md`, `log.md`, directories, and settings | project wiki around the complete read/create/write sequence |
| `core/update.sh --enable-compile`, `--optin`, `--optin --recommended`, `--optout` | settings or local decision marker; recommended opt-in also scaffolds wiki | project wiki; recommended scaffold and final settings publication share one process and one uninterrupted lock interval via `wiki_init.init_wiki` |
| `core/update.sh` missing-collection cleanup | remove stale collection/role/path entries from settings | project wiki around settings read/modify/write |
| `wiki_compile.main` | card create/refresh, index and log | card write → project wiki; shared atomic helpers reenter |
| `wiki_verify_worker.process_verify_job` | verified/contested stamp or audited delete, index and log | card write → project wiki; shared atomic helpers reenter |
| `wiki_topical_publish.publish` / `retire_stale` | v2 page publish/retire | project wiki; temporary verification stamp reenters |
| `wiki_dedup_resolve.resolve_entry` | reviewed deletion, index and log | project wiki; index/log helpers reenter |
| `wiki_source_retire.retire_card` / `retire_deleted_source` (via `wiki_lifecycle`) | archive + unlink, index line removal | `retired-wiki/` archive, card + wiki locks |
| `wiki_compile_worker` | wiki sidecar or compile-driven page rewrite | shared atomic wiki helper or `wiki_compile.main` |
| `wiki_compile.update_index`, `remove_from_index`, `append_log`, `write_text_atomic`, frontmatter helpers | standalone file mutation | project wiki; nested calls reenter |

The lock is never taken before a card/ledger lock by product writers. Settings writers hold only the project wiki lock; `--enable-compile` invokes `--init-wiki` before taking its own settings lock. Recommended opt-in calls the shared `wiki_init.init_wiki` function in the same process while holding its outer lock; nested acquisition is thread-reentrant. It does not spawn a child under the flock. Recommended opt-in publishes settings only after scaffold completion. If it fails or is killed during scaffold, no settings were published; partial scaffold files are idempotently completed by retry. SessionStart legacy migration uses the separate install journal lock and refuses migration while a journal exists. The coordinator takes install journal → project wiki; no product settings writer takes these in reverse order. Publisher takes project wiki only. The shared helper reuses a thread's existing fd instead of taking a second `flock`. A cross-process fixture proves ordinary compile writes, verification stamps, and `--init-wiki` block during cutover, then complete, and a nested call does not self-deadlock. A second fixture pauses recommended opt-in during scaffold before settings publication, proves the real `activate` entrypoint remains blocked, and proves both scaffold failure and SIGKILL leave no published settings and retry safely. The coordinator CLI fixture also runs an actual compile helper concurrently with cutover.

| Cutover failure | CLI result and state |
|---|---|
| fresh `prepared` precheck drift, before any activation baseline | structured `rejected`; no pointer applied; journal remains prepared |
| `preparing`, `failed_preparing`, `awaiting_legacy_review`, or `rolled_back` passed to `activate` | return current phase with zero changes; `prepare` or review is required |
| interrupted `activating`/`recovery_required` precheck drift with baseline | hash-guarded rollback; `rolled_back_after_activation_failure` on success |
| interrupted `activating`/`recovery_required` precheck drift with no baseline | journal `recovery_required` with `missing_activation_baseline`; no false clean rollback |
| `activated` repeat | verify selected pointers and route; `activated_unchanged` only if proof remains valid |
| wiki/config/source drift before first pointer | structured `rejected`; no pointer applied |
| post-pointer corpus/DB drift, `sqlite3.Error`, QMD subprocess timeout, daemon reload failure/timeout, or ordinary callback exception | attempt immediate hash-guarded rollback; `rolled_back_after_activation_failure` JSON on success |
| rollback itself fails, including SQLite or pointer conflict | journal `recovery_required` and JSON with both reasons; no claim of clean rollback; later explicit rollback may resume |
| process interruption (`KeyboardInterrupt`, kill, power loss) | journal write-ahead intents support explicit resume/rollback; deliberately not converted to ordinary exception |
| backend `identity` inspection | no PID adoption or runtime pointer write; codes 0 healthy managed, 3 live unhealthy, 1 absent, 2 healthy endpoint without verified managed PID |

A separate disposable HOME/project used installed QMD 2.5.3 and existing offline model files to prove real shadow embedding of three synthetic wiki documents, corpus/hash/vector attestation, selection, and rollback. Another isolated run proved actual managed daemon PID changes on cutover and rollback. QMD closes a WAL index without sidecars; the shared `sqlite_read` reader uses ordinary `mode=ro` for a live WAL and immutable read only for WAL-format main files without an actual WAL, with stable-file checks. A stale SHM alone has no uncheckpointed rows. New npm package installation through this coordinator remains untested. No operating project pointer, model, auth or global hook was changed for the cutover tests.

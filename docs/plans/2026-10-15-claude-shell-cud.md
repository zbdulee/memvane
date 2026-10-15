# Claude shell CUD implementation plan

This is the original development plan and its phase-specific limits. Later
explicit approvals covered the official host updates, native synthetic
validation, and commit/push to the existing development branch. The completed
validation below supersedes the earlier unverified/install-pending statements.

**Goal:** Detect successful Claude Bash changes from project file state and settle
them at Stop, with durable SessionStart recovery. Development and isolated
synthetic tests only; no operating sync, cache refresh, permissions, DB changes,
push, merge or release in this task.

**Architecture:** A new Bash PostToolUse action records one coalesced project job,
without interpreting command text or running QMD. The existing Stop dispatcher
wakes a detached snapshot worker before topical-specific routing; ordinary
projects need no topical pilot opt-in. The existing SessionStart queue also wakes
recovery. The worker compares only configured, in-project collection roots,
enqueues changed indexed collections through the existing durable owner route,
and advances its private snapshot after enqueue. Duplicate events collapse; a
new event arriving during processing cannot be removed by an earlier ACK.

Existing sync scan/compare and compile metadata helpers can be reused; neither
the rejected sync wrapper nor sync.run() is called. No shell string is saved or
parsed, no original body is persisted in the new jobs, and no model is called by
the detector. Compile metadata continues to honor the existing explicit policy.
An absent first snapshot uses scoped, read-only QMD document metadata to detect
already-indexed missing files, rather than treating them as an empty baseline.
Unknown/failed/background/interrupted Bash outcomes do not enqueue an event.

## Task 1 — failing regression

- Add a hook registration regression: Bash has its own change action, not the
  ordinary index/compile matcher. Run it and observe failure on c977e87.
- Add a Python fixture and Node runner using disposable HOME, TMPDIR, queues,
  collection config and SQLite DB. Exercise deletion, rename, failed commands,
  outside-project changes, duplicate events, Stop coalescing and SessionStart
  recovery. No real Claude/QMD/model invocation or operating project access.

## Task 2 — implementation

- Create core/file_change_queue.py with private durable jobs, scoped snapshots,
  bounded single-flight processing, conditional ACK and ordinary worker kicks.
- Add one Claude Bash PostToolUse entry in hooks/hooks.json.
- Extend hooks/run-hook Stop handling and core/update_hook_queue.py SessionStart
  handling to wake the generic queue, retaining sandbox/setup/synthetic guards.
- Keep Codex and Hermes edit adapters and topical worker routing intact.

## Task 3 — verify and review

- Run the new regression and adjacent hook, dispatcher and sync tests with the
  standard fixture environment and no live recall logging. Inspect every result.
- Request independent review of data scope, recovery/races and hook registration.
- Document the automatic path and report limits: fixture evidence is not native
  installed-host validation. Future official installation/trust review must cover
  the new Bash hook and changed Stop/SessionStart behavior. Shared QMD worker
  updates still have their existing whole-store incremental scope.
- Leave changes uncommitted/unpushed and stop after reporting the reviewable diff.

## Development result

- RED: missing Bash PostToolUse registration reproduced on c977e87. Additional
  failing regressions reproduced first create/update, unknown result, FIFO open,
  same-size/same-mtime changes, burst handoff and compile-cap job loss.
- GREEN: 16 new isolated SQLite scenarios + hook structure, dispatcher and
  adjacent sync regressions: **74/74 passed**, Node exit 0. All fixtures used
  disposable HOME/queues/DB and no provider/model CLI. The first adjacent run was
  blocked by EPERM creating test projects in the real HOME; the final run used
  an isolated HOME and did not broaden sandbox permissions.
- Independent review: **28/28 passed** (16 new scenarios + 12 structure checks),
  plus separate symlink/malformed snapshot preservation and retry probes. No
  remaining reproducible P1/P2 findings. FIFO, burst, bootstrap and cap findings
  were repaired before this result.
- Cap overflow retains the original event consent until a later wake; a zero
  cap does not spin. SessionStart does not replace an unfinished event job with
  a different compile trigger. Detector failure cannot stop the existing update
  handoff. Source-only collections without an indexed baseline bootstrap their
  local snapshot; indexed collections also compare existing DB path/hash data.
- Evidence: ../claude-delete-dev-validation.tap and
  ../claude-delete-dev-review.json (relative to this checkout's root).
- Operating installation, rejected sync retry, cache/permission/operating DB
  edits, commit/push, main merge and release were not performed. The installed
  native Claude automatic deletion path remains unverified.

## Approval needed before a future installation

Review and approve the new candidate source as a normal plugin update, including
the new Claude Bash PostToolUse command hook (9 existing commands become 10) and
changed behavior of the existing Stop and SessionStart commands. New local
file-change metadata and owner-bound indexing queues must remain within the
approved project/store scope. The existing QMD worker runs whole-store
incremental update/embed, so approved rccar/service-engineering collections can
also be refreshed. Existing opted-in compile policies can enqueue source
compilation; this detector does not grant a new compile/model policy. Installed
host verification must exercise actual automatic events with a synthetic
project, without substituting a manual sync result or executing the earlier
rejected operating command.

## Approved native validation and lifecycle follow-up

- Actual Claude Write, Bash deletion, Stop and a fresh SessionStart were tested
  in a separate synthetic project after an approved official plugin update.
  The Bash change detector and Stop ran once each with exit 0. The raw source
  became inactive in the private index; the generated wiki card remained on
  disk, was marked source-missing and was excluded from automatic recall.
  The original synthetic file was preserved outside the collections. No manual
  sync was used to make the automatic deletion result pass.
- The first automatic Codex verifier attempt returned SIGKILL (-9). Its sender
  and OOM status remain unknown. Earlier success with an explicitly selected
  standalone CLI did not prove the default automatic path recovered.
- The SessionStart notice resolver received empty stdin instead of the found
  project settings, falsely prompting already configured projects to opt in.
  It now receives the actual settings and canonical project root, including
  when the host cwd is nested. Pending and opted-out behavior remain covered.
- Builtin CLI diagnostics now record the selected executable, child/parent
  PID, start/end time, elapsed duration, deadline, return code and signal for
  success and failure. PATH-only overrides resolve against the child cwd/PATH;
  adapter timeout cleanup is distinguished from an unexplained SIGKILL. The
  structured audit omits prompt, argv, stdout and environment values. Existing
  bounded raw CLI stderr forwarding is retained, with a separator when needed
  so the JSON audit always starts on its own line.
- Following explicit approval, the five notice/diagnostic files were applied
  through both official host installers. Source and both caches matched the
  631-file candidate SHA-256
  `915cf4e30ada67ebe710fd679f1278b85820e836ed68ff63331805f27b410dad`
  before this documentation update. Host settings/trust semantics, selected
  runtime/store/endpoint and operating project settings were preserved.
- One small synthetic auto-verifier worker case then passed using default
  native Codex 0.162.1, without a CLI override or observer wrapper. The child
  returned 0 with no signal in 6.44 seconds; one claim was verified and the
  verify queue emptied. The initial harness revision omission was caught by
  the freshness guard before any model call; its input was corrected without
  changing the guard. Total actual model calls were one. This exercised the
  installed compile-enqueue/auto-verifier worker path, not a new host session
  or a Claude model-generated candidate, and does not identify the earlier
  SIGKILL cause or rule out intermittent failures.
- Final focused regression: eight files, 66 tests passed, zero failed.
  Independent review reported no remaining reproducible P1/P2 findings.
  Existing sessions and inbox stayed running. No additional activation,
  permission change, operating sync, model replacement, operating-data
  rollback, main merge or release was performed.

The local evidence includes installer receipts, before/after preservation
hashes, full candidate file hashes and the native child audit. Machine-specific
paths, settings, DB copies and raw runtime logs are intentionally excluded from
this repository record. Node 22/24/26 CI must evaluate the pushed commit; local
focused and native results do not substitute for that matrix.

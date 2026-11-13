# Release agent QA gate

Every version release must pass this agent-executed QA gate on the **final
installed candidate**. The gate does not rerun unit tests. It is a set of
reproducible scenarios that an operator agent performs in fresh, real Claude
Code and Codex host sessions, against disposable synthetic fixtures. Each
scenario captures objective evidence. Record each run with
[the run record template](templates/release-agent-qa-run.md). The
[release checklist](release.md) links here.

Open QA items, including the user-deferred assertions, are tracked in
[TODO.md](TODO.md).
The [architecture flow charts](architecture/flows/README.md) explain the
implemented paths and boundaries; they are not L1 verdicts.

## Outcome-first scope

This contract protects five user-visible outcomes. One host session or one
backup may provide evidence for several scenario IDs; do not repeat an
identical action once its exact artifact, project, source revision and owning
domain are proved. Keep every ID in the run record, including non-PASS rows.

| Outcome | Observable minimum | Explicit non-goal |
|---|---|---|
| Install and settings | Exact installed bytes and loaded hooks, scoped setup decision, retained settings | A matching version string alone, or a direct core script call |
| Source to wiki | Consented change reaches a verified current card, with source revision and locator; duplicate triggers do not cause duplicate paid work | All asynchronous queues globally empty before other QA |
| Search and injection | Both hosts select the current wiki card, inject bounded wiki text and a verified source locator; Laya has no independent raw document hit | Force the agent to open a raw file or answer a raw-only token without tools |
| Onboarding and recovery | Source/wiki/settings preservation, wiki-only Laya seal, scoped activation, restore before the first selected write and forward recovery afterward | Delete an old store merely because a new candidate exists |
| Boundaries | No cross-project results, normal work stays responsive, unsupported cells are reported honestly | Treat an optional hardware cell as tested on the available Mini |

**Queue interpretation.** `pending` is ordinary asynchronous work unless a
scenario needs that exact source revision to converge within its own timeout.
For such a scenario, compare the queued revision (if recorded), current source
hash, verified card provenance and selected index. A revisionless legacy row
cannot by itself prove a newer revision is missing. `worker_failed` retries,
an exceeded scenario deadline or a stale selected card are separate failures.
No global `queue_count=0` precondition exists. A sync snapshot is not a wiki
compile ACK, and an empty queue is not a search result.

**Evidence reuse.** Keep source-level CI, immutable model provenance,
consistent backup and synthetic guard results as preflight where
their inputs are unchanged. An old candidate's complete L1 `PASS` never
transfers to a different artifact hash. Batch fixes before final QA, then run
the exact installed identity/core smoke and only the affected scenario
assertions under section 11. Do not launch a separate model turn for each ID
when one trace proves several. No data-safety row is waived by this economy:
LI-MIG, LI-RECOVER, LI-COEXIST and RQA-INST-04 remain mandatory.

## 1. Source verification and installed-candidate QA are separate

| Level | What runs | Can it satisfy this gate? |
|---|---|---|
| L0 source verification | `npm test`, focused tests, synthetic fixtures, direct `core/*.py` calls in the source checkout | **No.** L0 is a prerequisite only (see [release.md](release.md)). |
| L1 installed-candidate QA (this gate) | Host marketplace install of the exact candidate, then fresh host sessions in which the real hooks fire, with natural-language requests and exposed skills | Yes. This is the only level that counts. |

Hook-path evidence cannot be replaced by a direct script call. A cached plugin
directory can carry the same version string while containing different code, so
identity is proved by content hashes (section 5), not by version strings.

Operator rules:

- Product interaction uses natural-language requests or the exposed skills
  (`setup`, `query`, `update`, `wiki-compile`). The
  operator must not run `core/*` or `skills/*/scripts/*` directly to make a
  scenario progress. Doing so invalidates
  that scenario's run. The scenario must restart from its preconditions.
- Outside the product, the operator may do only three kinds of work: actions
  that simulate the user (the fixture helper's Git actions, host marketplace
  install/update, host hook trust limited to the reviewed exact commands in
  the preflight, section 3), reading evidence, and computing hashes.
- Probe prompts forbid tool use. If the transcript shows a tool call in a probe
  turn, the probe is invalid. Repeat it; do not count it.

## 2. Status values and the release rule

| Status | Meaning |
|---|---|
| `PASS` | Every expected outcome was observed. All required evidence is attached. The installed artifact identity equals the candidate identity. |
| `FAIL` | An expected outcome was contradicted, a product timeout was exceeded, a hook reported a failure or timeout, or the product claimed success without the required proof. |
| `BLOCKED` | The scenario could not start or finish for a reason outside product behavior: missing approval, hardware, host authentication, host outage, or lost evidence. Write down the blocker. |
| `NOT RUN` | Not attempted. |

- Never record a missing, partial, blocked or unobservable result as `PASS`. A
  product hang or a silent no-op is `FAIL`, not `BLOCKED`.
- A release requires `PASS` for every mandatory scenario in every mandatory
  matrix cell (section 4).
- A `FAIL`, `BLOCKED` or `NOT RUN` result in a mandatory matrix cell blocks a
  formal release. Record the reason and corrective work without changing its
  status. An optional, unverified host/OS combination may be left out of the
  claimed support matrix, but must be named in the release notes.
- Before judging the final candidate, calibrate the evidence review on a
  copy of one synthetic probe record with its hook log line removed and on a
  copy with an intentionally wrong selected path. The reviewer must classify
  both as non-PASS. Save only those disposable calibration records and the
  verdict in `$EVID`; never alter real host logs. If either is accepted as
  PASS, the QA evidence procedure is `BLOCKED` until repaired and repeated.
- Some expected outcomes below are policy goals that the current code does not
  fully meet, for example revision idempotency across compile triggers
  (RQA-WIKI-01). The gate keeps those outcomes as the expected result. Until
  they are met, they `FAIL` and block a formal release. The gate must not be
  weakened to describe the current gap as correct.

## 3. QA environment and isolation

- **QA host account.** Use a dedicated OS user account, or a disposable VM or
  snapshot, on supported hardware. It must have no personal projects,
  manuscripts or operating databases. An isolated `HOME` is not enough: the
  runtime wake queue and the coordination and development locks are rooted at
  the OS UID home (see
  [runtime resource coordination](runtime-resource-coordination.md) and
  [development resource safety](development-resource-safety.md)). Never run
  the gate in a maintainer's daily account.
- **Clean state.** A clean state has no `memvane` or `qmd-auto-context` plugin in either host,
  no plugin state under `~/.local/state/memvane`, `~/.local/state/laya`,
  `~/Library/Application Support/memvane` or
  `~/Library/Application Support/qmd-auto-context`, no previous fixtures, and no global Git `core.hooksPath`. Record how the state
  was reset: new account, snapshot restore, or documented removal.
- **Approvals recorded before running:**
  - QA host use and model-usage cost cap for host sessions.
  - Network access for embedding runtime provisioning (npm registry and model
    host), unless a reviewed local copy of the pinned GGUF is supplied.
  - Wiki extractor/verifier cost cap (RQA-WIKI-*).
  - Learning approval with safe test resources (RQA-RES-02 only).
- **Data limits.** Real learning, manuscripts and operating DB writes are out
  of scope unless separately approved. Without that approval, the dependent
  scenarios are `BLOCKED`.
- **Evidence directory.** Use
  `EVID=/tmp/qmd-release-qa-evidence-<runId>`, mode 0700, outside every
  fixture, so fixture cleanup cannot delete evidence. Recall diagnostics are
  always on: every hook call of a selected route appends one line to the
  install event log `<install state root>/events.jsonl` (never to model
  context). After each fresh host session, copy `events.jsonl` (and any
  `.1`/`.2` generation) into `$EVID/events-<host>-<run-session-id>.jsonl`.
  Do not set test-only
  variables such as `QMD_FORCE_DEAD_PIDS`, `QMD_SETUP_GUARD_FIXTURE` or
  `QMD_SANDBOX`. If any is present, the run is
  invalid. `LAYA_QA_SYNC_BARRIER` is allowed only in the RQA-RACE-01 Part B
  session, with that part's arm file. In any other scenario it invalidates
  the run.
- **Fresh session.** A host process started after the candidate install and
  host reload completed, with its working directory equal to the scenario's
  checkout. Sessions that predate the install are "old sessions" and appear
  only in RQA-INST-02.

### Preflight before functional QA

Run this preflight per host and matrix cell before the first functional request.
For a fresh install, RQA-INST-01 steps 1–2 first install and identify the
candidate; finish the preflight before its edit in step 4. Repeat it after an
update or host reload. It checks whether the environment can produce valid
evidence. It is not a scenario and never changes a scenario status to
`PASS`. Record each item as `ready`, `not ready` or `NOT RUN`, with evidence,
in the run record's preflight table. A check that was not performed is
`NOT RUN`, never `ready` or `PASS`. The preflight result is `READY` only when
every item is `ready`.

| ID | Check | `ready` requires |
|---|---|---|
| PF-ID | Exact candidate | RQA-ID-00 evidence for this host: the commit (40 hex), version string, and an empty or allowlisted `identity-diff-<host>.txt`. A matching version string alone is not enough. |
| PF-FOLDER | Project folder access | The host can read and write the exact fixture checkout. If the host presents a folder/project trust prompt, a human approved only that exact checkout. Record an official no-prompt path as such; never infer trust for `$HOME`, `/tmp` or a parent. Resolve OS privacy prompts before the run. |
| PF-TRUST | Plugin hook trust | Compare the installed `hooks/hooks.json` or `hooks/hooks-codex.json` under `INSTALL_PATH` against the candidate and record the full command list. For a host that requires per-hook trust, a human approves only those exact commands in its official UI. For a host without per-hook review, record official plugin install/enable evidence and the loaded command path. No wildcard, global "trust all", config edit or bypass flag. |
| PF-FRESH | Fresh host session | A session started after install, trust and reload, with cwd equal to the fixture checkout. A unique run/session marker and SessionStart output or host hook trace identify this session. Its `session` id selects its lines in the install event log for later prompt-turn evidence; those lines alone do not prove SessionStart ran. |
| PF-HOST | Sandbox and event support | Record the host version, surface (CLI or app), sandbox mode and sandbox environment variables. Check the host's documented/advertised support for every event the planned scenarios require (for example, Codex has no Bash PostToolUse). Observe actual delivery in the corresponding synthetic scenario; do not treat capability metadata alone as a hook PASS. |
| PF-RUNTIME | OS and runtime dependencies | OS build and architecture match the matrix cell. The hook `python3`, Node, Git (2.36+) and hashing tools resolve from the host's hook environment, and their versions are recorded. No forbidden test variable is set (section 3). |
| PF-NET | DNS and network | Record host CLI/app and hook-environment connectivity separately: online, intentionally offline, sandbox-denied, DNS failure or remote-service outage. Probe only endpoints a scenario actually needs. Intentional offline mode can be ready for local-only scenarios; a required endpoint failure is not ready. |

Classify every non-`ready` item, and every unexpected functional result
observed before the preflight was `READY`:

| Class | Typical evidence | Status effect | Retest rule |
|---|---|---|---|
| Environment unready | Wrong candidate selected, folder access denied, missing or wrong runtime, a required sandbox/DNS route denied, a session that predates install | Dependent scenarios `BLOCKED` (or `NOT RUN` if not attempted) | Fix the environment, then repeat the whole preflight in a new fresh session. Earlier functional results in that session are discarded. Investigate an installed-byte mismatch before assigning cause: a product installer defect is `FAIL` once reproduced with an otherwise ready environment. |
| Approval pending | A trust prompt or folder approval not yet given; hooks listed but untrusted; a recorded approval (section 3) missing | `BLOCKED` with the pending item named | A human approves the exact scope in the official UI. Start a new fresh session, repeat PF-TRUST and PF-FRESH, then restart the scenario from its preconditions. |
| Host unsupported | The host version or surface cannot fire a required event, or cannot run in the cell | Optional cell: `NOT RUN` plus the release-note wording (section 4). Mandatory cell: `BLOCKED`, which blocks release. | Repeat on a host version or surface that supports the event, starting from the preflight. |
| Product defect | With a `READY` preflight and a supported event, a trusted hook does not fire or the product contradicts the expected outcome | `FAIL` (section 2) | Fix the product, then rerun the affected set under section 11 on a new candidate hash. |

- The operator never trusts hooks or folders automatically, by script, by
  editing host trust or config files, or beyond the reviewed exact commands.
- A non-`READY` preflight never skips, waives or reorders a required gate.
  Required scenarios stay `BLOCKED` or `NOT RUN` until a `READY` preflight
  allows them to run.
- An untrusted hook is an approval blocker. A trusted hook that fails to fire
  on a supported event after a `READY` preflight is a product failure. A
  missing deny or injection is never `PASS`.
- This preflight does not change any status that is already recorded in the
  run record.

**Codex trust scope.** The installed `hooks/hooks-codex.json` holds six
commands of the form `"<INSTALL_PATH>/hooks/run-hook" <name> codex`, where
`<name>` is one of `update-queued`, `recall`, `gate`, `posttool`, `index` and
`compile`. Trust only
the exact fixture folder and these commands in the Codex official UI, and
record the list as the trust scope.

## 4. Supported OS and host matrix

| Cell | Platform | Product support | Gate requirement |
|---|---|---|---|
| T1 | macOS arm64, current maintainer macOS release (record exact build) | Laya embedding runtime (prebuilt `mac-arm64-metal`) + selected Laya route | **Mandatory:** all scenarios for Claude Code and Codex |
| T1-node | Same as T1, second Node LTS (22 vs 24) | Runtime provisioning accepts Node 22, 24 and 26 | **Mandatory:** RQA-INST-01 + core smoke |
| T1-py | Same as T1, hooks run by the system `python3` (macOS ships 3.9) | Hook floor is Python 3.9 | **Mandatory:** core smoke with the host-resolved `python3`; record its version |
| N26 | T1 with Node 26 Current | Runtime provisioning accepts Node 26; native compatibility unverified | Optional; release notes must say "unverified" unless RQA-INST-01 + core smoke PASS |
| U-intel | macOS x86_64 | Provider review refuses (`reviewed_native_platform_unsupported`); user QMD data untouched | RQA-PLAT-01 if hardware exists; otherwise `NOT RUN` + release note |
| U-linux | Linux x86_64/arm64 | Provider review refuses (`reviewed_native_platform_unsupported`); no selected route | RQA-PLAT-01 recommended; otherwise `NOT RUN` + release note |
| U-wsl / U-win | WSL guest / native Windows | Unverified / unsupported | Not claimed; no gate cell |
| H-hermes | Hermes Agent on T1 | README lists it as supported | Core smoke equivalent recommended; if not run, release notes must say Hermes was not verified in this release |

Rules for unverified OS/host combinations:

- A combination without a `PASS` record must not be described as supported or
  verified for this release. Release notes keep the explicit wording
  "not verified in this release".
- Results from one cell are never copied to another cell. Each cell's record
  carries its own host, OS build and artifact identity.
- An unsupported platform can pass only the negative-path scenario
  RQA-PLAT-01. That scenario checks preservation and the provider-review
  refusal; it does not check functionality.

## 5. Candidate and installed artifact identity

Run hashing from the source checkout through the shared development lock:
`npm run dev:guard -- --kind hash -- bash "$EVID/identity.sh"`. Record each
output file's SHA-256 in `$EVID/evidence-index.sha256`.

Candidate identity (Git 2.36+ for `--format`):

```bash
COMMIT=$(git -C "$CANDIDATE" rev-parse "$REF^{commit}")
git -C "$CANDIDATE" rev-parse "$COMMIT^{tree}"                    > "$EVID/candidate-tree.txt"
git -C "$CANDIDATE" status --porcelain                           > "$EVID/candidate-dirty.txt"   # must be empty
git -C "$CANDIDATE" ls-tree -r --full-tree \
  --format='%(objectmode) %(objectname) %(path)' "$COMMIT" | LC_ALL=C sort > "$EVID/candidate-blobs.txt"
shasum -a 256 "$EVID/candidate-blobs.txt"                         # artifactManifestSha256
git -C "$CANDIDATE" archive --format=tar "$COMMIT" | shasum -a 256  # informational; record git --version
```

Installed identity, per host (`INSTALL_PATH` is the path the host reports for
the loaded plugin):

```bash
cd "$INSTALL_PATH" && find . -type f ! -path './.git/*' -print | LC_ALL=C sort |
while IFS= read -r f; do
  m=100644; [ -x "$f" ] && m=100755
  printf '%s %s %s\n' "$m" "$(git hash-object --no-filters -- "$f")" "${f#./}"
done | LC_ALL=C sort > "$EVID/installed-blobs-$HOST.txt"
diff "$EVID/candidate-blobs.txt" "$EVID/installed-blobs-$HOST.txt" > "$EVID/identity-diff-$HOST.txt"
```

- Every tracked path must match: same blob id, same executable bit.
- Any extra file must be named in the run record's host-metadata allowlist
  with a justification. Extra files must not be executable, hook-related or
  under `core/`, `hooks/`, `skills/`, `locks/` or `hermes_adapter/`. A
  symlink difference is shown in the diff and must be explained.
- Also record the host-reported plugin version and commit. For Claude Code,
  this is the installed-plugin registry entry; for Codex, the plugin list.
- Version consistency: one version string across `package.json`, `plugin.json`,
  `plugin.yaml`, `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`,
  `.claude-plugin/marketplace.json` and `.agents/plugins/marketplace.json`.
- Runtime identity, reported by the `setup` skill status request: the
  embedding runtime (`runtimeId`, generation, Node and model file SHA-256),
  Laya state (`deferred` or a reviewed identity), and selected index
  generation.

## 6. Evidence and search-result proof

Probe prompt (one turn; replace `<question>`). This observes what the
installed host received. For a raw-only fact the expected no-tools answer
is `없음`, even when the correct source locator was selected:

> 도구를 쓰지 말고 이번 프롬프트에 자동 주입된 프로젝트 컨텍스트만 근거로 답해줘.
> `<question>` 주입된 컨텍스트에 없으면 "없음"이라고만 답해.

| Question key | `<question>` | Tokens it can reveal |
|---|---|---|
| Q-alpha | release QA alpha 문서의 codename은? | `ALPHA_MAIN`, `ALPHA_FEATURE`, `ALPHA_WORKTREE`, `DIRTY_TRACKED` |
| Q-beta | release QA beta revision marker는? | `BETA_V1`, `BETA_V2` |
| Q-gamma | release QA gamma marker to be deleted는? | `GAMMA_DELETE_ME` |
| Q-delta | release QA added delta marker는? | `DELTA_ADDED` |
| Q-feature | release QA feature-only marker는? | `FEATURE_ONLY` |
| Q-worktree | release QA worktree-only marker는? | `WORKTREE_ONLY` |
| Q-untracked | release QA untracked marker는? | `UNTRACKED_NEW` |
| Q-wiki | release QA wiki source fact는? | `WIKI_FACT_R1`, `WIKI_FACT_R2` |
| Q-agent | release QA agent note marker는? | `AGENT_NOTE` |

Token strings come from the fixture manifest (`show`). They contain a random
per-fixture id, so an earlier fixture cannot satisfy them. Record whether each
token occurs in a raw source or in a verified wiki card before applying a
verdict.

Scenario rows below that say `Present(T)` or `Absent(T)` for a raw-only
marker are evaluated on the selected Laya route: evaluate the selected, current
wiki card and verified locator; raw-only markers must not create independent
Laya hits. Do not relax the
scenario's installation, sync, freshness, scope, backup or recovery checks.

- **Present(T) for a raw-source token.** On a selected Laya route, raw files
  are not independent indexed/searchable documents: a raw-only T must be absent from results and
  automatic context. Source freshness and the verified locator are checked
  through the selected wiki card, not a raw search hit.
- `Present(T)` in older scenario steps is a legacy label for **located
  current source** in this raw case. It does not mean that raw-only T is
  automatically injected or spoken by the host. Record the source revision
  and selected locator instead. For a wiki-card T, apply the wiki-card
  definition below.
- **Present(T) for a verified wiki-card token** additionally requires the
  selected card's bounded summary in the injected context to contain T and
  its verified source locator to be correct. A no-tools answer containing T
  is useful host evidence, but the selected card and injected bytes are the
  plugin proof; the answer alone is insufficient.
- **Absent(T)** means no selected current wiki card carrying T, no T in
  the injected context, and no unsupported answer containing T. An old or
  excluded source must not be served as current. A raw-only token must not
  create a selected Laya result; the original file may still exist as a
  verified source of a wiki card.
- A selected path by itself does not prove that its source revision is current;
  retain the index/generation and scope evidence required by the scenario.
- In a **separate optional prompt that permits tools**, record whether the
  host agent chooses to Read the selected source and what it answers. That is
  host-agent behavior, not a plugin release condition.
- A missing `recall` event line means the hook did not run. Treat it as `FAIL` unless a
  sandbox or headless host is documented.
- For each fresh host session, use a unique QA run/session marker and record
  the install event log's size before and after the session. Capture the
  host's loaded plugin path, hook command path and session/turn identifier.
  Correlate the `recall` event to that host turn by its `session` field and
  timestamp (`python3 -B "$ROOT/skills/logs/scripts/logs.py" --cwd <fixture>
  recalls --json`). A direct CLI query or a line from an earlier session never proves the
  installed host hook ran. If the host does not expose a hook command trace,
  record that limitation and use its official plugin registry, isolated log
  path and exact turn timing together; an ambiguous match is `BLOCKED`.
- The optional query skill check ("`<question>` 질문에 어떤 문서가 recall되는지
  확인해줘") is secondary evidence. It does not replace the hook path.
- Per probe, record: wall-clock time, host session id, cwd, branch,
  `git status --porcelain` summary, the `recall` event line, a transcript excerpt
  and the verdict.
- Each convergence probe must show at least one positive current token and
  the relevant negative old token together. An empty result can be a safe
  intermediate state during a switch, but never satisfies a final searchable
  state or a convergence deadline.
- Store transcripts privately. Release notes get summaries and hashes only.

## 7. Timeouts

| Name | Value | Applies to |
|---|---|---|
| Host hook timeouts | `hooks/hooks.json` / `hooks-codex.json`: SessionStart 5 s, recall 22 s, gate 3 s, PostToolUse 22/5/5 s, Bash 5 s | Any host-reported hook timeout or failure is `FAIL` |
| T_TURN | 90 s | One probe turn, prompt submit to final answer |
| T_FRESH | 180 s | From the trigger (SessionStart, agent edit, agent Bash Git command) to the expected Present/Absent state. Probe at +30 s, +90 s and +180 s; stop at the first full success |
| T_LATE | T_FRESH + 120 s | Recheck that a late worker did not republish stale results |
| T_GIT | 30 s | Each fixture helper action (enforced by the helper) |
| T_INSTALL | 30 min | Install or update: plan, prepare and apply on the fixture |
| T_ROLLBACK | 10 min | Rollback, or a recovery status request |
| T_WIKI | 15 min | Compile and verify of one source revision |
| T_SCENARIO | 45 min (60 min for RQA-INST-02/04, RQA-RES-02) | Whole scenario |
| T_CELL | 8 h | One matrix cell |

- Exceeding a timeout because of product behavior is `FAIL`.
- Exceeding a timeout because of an external outage (host service,
  authentication expiry) is `BLOCKED`, with evidence.
- These values are the initial release policy. The owner may change them only
  through a reviewed edit to this document.

## 8. Fixture

Use [`test/fixtures/release-qa-fixture.py`](../test/fixtures/release-qa-fixture.py).
It is an operator tool only; it never runs QA itself.

- **Location.** It writes only below a caller-provided new or empty directory
  under `/tmp` or the current workspace. It refuses a location inside host or
  runtime state, and one whose ancestors hold MemVane project settings (upward
  config discovery would contaminate opt-in). Prefer `/tmp`.
- **Git isolation.** Git runs with fixture-local config, an empty fixture-local
  `core.hooksPath` and the file protocol only. That local config persists, so
  an agent's own Git commands inside the fixture also skip the QA account's
  global hooks.
- **No side effects.** It invokes no QMD, host, model, API, hook, installer or
  network. It never overwrites an existing path. `cleanup` deletes the fixture
  only after it verifies the owner marker.

```bash
python3 -B test/fixtures/release-qa-fixture.py create  /tmp/qmd-rqa-<runId>-<host>-<scenario>
python3 -B test/fixtures/release-qa-fixture.py show    <fixture>              # tokens and event log
python3 -B test/fixtures/release-qa-fixture.py stage   <fixture> <action>     # simulated user action
python3 -B test/fixtures/release-qa-fixture.py cleanup <fixture>
```

**Layout.**

- `project/`: a clone on `main`, with `docs/qa/{alpha,beta,gamma,wiki-source}.md`
  and `README.md`.
- Local branches:
  - `qa/feature`: alpha changed to `ALPHA_FEATURE`, plus `feature-only.md`.
  - `qa/worktree`: the same `alpha.md` contains `ALPHA_WORKTREE`, plus
    `worktree-only.md`.
- `upstream.git` and `seed/` simulate the remote.
- `worktree-add` creates `wt-worktree/` on `qa/worktree`.

**Stage actions.**

| Action | Effect |
|---|---|
| `upstream-change` | Adds `delta.md`, changes beta to `BETA_V2`, deletes `gamma.md` upstream |
| `pull` | `git pull --ff-only` in `project/` |
| `switch-feature` / `switch-main` | Branch switch in `project/` |
| `worktree-add` | Creates `wt-worktree/` |
| `dirty` | Uncommitted alpha edit with `DIRTY_TRACKED`, plus untracked `untracked-note.md` with `UNTRACKED_NEW` |
| `deny-read` / `restore-read` | Toggles read permission on `beta.md` |
| `wiki-source-edit` | External edit of the wiki source to `WIKI_FACT_R2` |

Use one fixture per host per scenario group, so state from the shared runtime
cannot satisfy another host's expectation. `AGENT_NOTE` is the token the
operator asks the agent to write.

**Visibility matrix.** These are the expected states; scenarios refer to them
by name.

| State | Present | Absent |
|---|---|---|
| S-main | `ALPHA_MAIN`, `BETA_V1`, `GAMMA_DELETE_ME` | `FEATURE_ONLY`, `WORKTREE_ONLY`, `DELTA_ADDED` |
| S-pulled | `ALPHA_MAIN`, `BETA_V2`, `DELTA_ADDED` | `BETA_V1`, `GAMMA_DELETE_ME` |
| S-feature | `ALPHA_FEATURE`, `FEATURE_ONLY` | `ALPHA_MAIN`, `WORKTREE_ONLY` |
| S-worktree (cwd `wt-worktree/`) | `ALPHA_WORKTREE`, `WORKTREE_ONLY` | `ALPHA_MAIN`, `ALPHA_FEATURE`, `FEATURE_ONLY` |
| S-main-after-worktree (cwd `project/`) | `ALPHA_MAIN` | `ALPHA_WORKTREE`, `WORKTREE_ONLY` |
| S-dirty | `DIRTY_TRACKED`, `UNTRACKED_NEW`, `ALPHA_MAIN` | none |

## 9. Scenarios

Each scenario lists: purpose, owning domain (section 10), hosts,
preconditions, steps, expected outcome, PASS evidence, timeout and
cleanup/recovery. The general rules (sections 2–7) apply to every scenario.
**Core smoke** is RQA-ID-00 + RQA-HOOK-01 + RQA-HOOK-02 on the final artifact.

### RQA-ID-00 Candidate and installed identity

- **Purpose:** bind every result to one exact artifact.
- **Owner:** install. **Hosts:** Claude Code, Codex.
- **Preconditions:**
  - The candidate commit is frozen and the candidate checkout is clean.
  - The host installs from a candidate source pinned to that commit: a pushed
    pre-release ref, or a local marketplace clone checked out at the commit,
    where the host supports one.
- **Steps:**
  1. Compute the candidate identity (section 5).
  2. Install or update through the host's marketplace flow.
  3. Reload the host.
  4. Compute the installed identity and the host-reported version and commit.
  5. Run the version consistency check.
- **Expected:** the candidate is clean. The installed blobs equal the
  candidate blobs, apart from allowlisted host metadata. One version string
  appears everywhere.
- **Evidence:** `candidate-*.txt`, `installed-blobs-<host>.txt`, the empty
  (or allowlisted) `identity-diff-<host>.txt`, `artifactManifestSha256`.
- **Timeout:** 15 min.
- **Cleanup:** none; the scenario is read-only.

### RQA-INST-01 Fresh install

- **Purpose:** check first install, the not-onboarded project, embedding
  runtime preparation, Laya onboarding and the first injection on a clean
  machine.
- **Owner:** install, with search for the final probe. **Hosts:** Claude Code
  and Codex, each starting from clean state.
- **Preconditions:**
  - Clean QA state.
  - A supported Node major (22, 24 or 26) as a real binary on `PATH` or in a
    mise/fnm/nvm/volta/asdf/Homebrew/system install.
  - Either the recorded network approval for runtime provisioning, or a
    reviewed local copy of the pinned GGUF
    (`embeddinggemma-300M-Q8_0.gguf`, SHA-256
    `f470220f84b6235197541352d22f10bf00098a8242c18eaacea9c8a4add557bc`).
  - Fixture created.
- **Steps:**
  1. Install the candidate through the host marketplace. On Codex, review and
     trust the exact hook commands in the official UI (preflight PF-TRUST).
  2. Run RQA-ID-00 for this host.
  3. Start a fresh session in `project/` (no `.memvane/`). Expected: no
     notice, no hook failure.
  4. As the user, write the minimal settings file
     `.memvane/settings.json` with `{"name": "<slug>"}`. Start a fresh
     session. Expected: exactly one SessionStart notice line that names the
     project root and the setup skill (Codex receives it as SessionStart JSON
     `additionalContext`). Probe Q-alpha: nothing is injected. Ask
     "문서에서 release QA alpha 찾아줘": the query skill returns the
     onboarding notice instead of results.
  5. Ask "MemVane 색인 준비 상태 보여줘". If the embedding runtime is missing,
     ask the `setup` skill to prepare it, review the stated network scope
     (npm registry, model host) or name the reviewed local GGUF, then
     approve. Expected: the runtime status reports `runtimeId`, generation,
     Node and model SHA-256; the model SHA-256 equals the pinned value.
  6. Ask "이 프로젝트 auto-context 온보딩해줘". Review the plan and the
     provider proposal digests, then approve preparation. Report build
     progress until the worker reports a sealed build, request finalization,
     then ask "인덱스 활성화해줘". Grant only the approvals that are already
     recorded.
  7. Record each phase result, including the activation's queued `syncWake`.
  8. Reload the host and start a fresh session. Expected: no onboarding
     notice. Probe Q-alpha.
- **Expected:**
  - The settings file is byte-identical to what the user wrote.
  - Nothing outside the fixture, the QA account's plugin state roots and the
    host plugin directories is written.
  - On the selected Laya route, a current wiki card for `docs/qa/alpha.md`
    and its verified source locator within T_FRESH; a token found only in the
    raw file is not returned. No hook timeout or failure.
- **FAIL if:**
  - The not-onboarded session shows more than one notice, or does indexing,
    recall, compile or learning work.
  - Activation reports success without a freshly sealed generation.
  - The probe stays absent at T_FRESH.
  - Any hook fails.
- **Evidence:** setup outputs, settings file with its hash, `git status`
  snapshots, SessionStart notice, `recall` event lines, probe transcript excerpt.
- **Timeout:** T_INSTALL for steps 5–7; T_SCENARIO overall.
- **Cleanup/recovery:**
  - A deferred result allows one identical retry.
  - `recovery_required`: stop, preserve every journal and pointer, record
    `FAIL`, and open an issue.
  - Keep the install for later scenarios, or restore the clean state before
    the next host.

### RQA-INST-03 Settings preservation

- **Purpose:** an update or migration must never drop or rewrite user
  decisions.
- **Owner:** install. Settings are owned by the project and user.
- **Hosts:** Claude Code, Codex.
- **Preconditions:** under N-1, before the update in RQA-INST-02:
  1. As the user, add an entry to A's `skipPaths`. Expected: the route stays
     selected (only a changed `name` blocks it as `route_settings_changed`).
  2. Create a second fixture B and leave it without `.memvane/`.
  3. Record the settings bytes, SHA-256 and canonical JSON for A, the
     absence of `.memvane/` in B, and the wiki file hashes for A.
  4. Record N-1's on-disk format/version markers and representative index,
     wiki and install-journal identities before update. Use actual N-1 output,
     not a hand-written imitation of the old format.
- **Steps:**
  1. After RQA-INST-02 completes, re-read A's settings and wiki hashes.
  2. Start a fresh session in B and probe Q-alpha.
- **Expected:**
  - A's settings are byte-identical, or differ only by a migration that the
    release notes document and that keeps every user value.
  - A's wiki bytes are unchanged.
  - Every prior-format index and install-journal record is either readable
    by the candidate or migrated through a documented path with no lost
    source identity. Record the before/after format and any
    migration receipt. A fresh install alone cannot satisfy this check.
  - B stays silent: no notice, nothing is injected, and no `.memvane/`
    is created.
- **Evidence:** before/after hashes and diff, B's session transcript.
- **Timeout:** 20 min.
- **Cleanup:** clean up fixture B.

### RQA-HOOK-01 Claude Code real hooks and injected search

- **Purpose:** every bundled Claude hook fires on the installed candidate,
  and recall injects real search results.
- **Owner:** search; file sync (edit indexing); runtime.
- **Host:** Claude Code.
- **Preconditions:**
  - RQA-INST-01 is `PASS` for Claude on this candidate.
  - The fixture is selected and in S-main.
- **Steps:**
  1. Start a fresh session in `project/`. Expected: both SessionStart hooks
     run without failure.
  2. Probe Q-alpha. Expect a current wiki card and its verified
     `docs/qa/alpha.md` locator if a card covers that source; never require the raw-only token
     or a standalone raw hit.
  3. Ask "docs/qa/agent-note.md 파일을 만들고 내용은 'The agent note marker is
     <AGENT_NOTE>.' 한 줄로 해줘". Expected: the gate allows the edit;
     posttool, index and compile run without failure.
  4. Probe Q-agent until T_FRESH. Expect a current wiki card and verified
     locator after compile. Until a card exists, no raw fallback is expected.
  5. Ask "Bash로 docs/qa/agent-note.md를 삭제해줘". Expected: the Bash
     file-change-event hook runs.
  6. Probe Q-agent until T_FRESH. Expected: Absent(`AGENT_NOTE`).
  7. End the session normally.
- **Evidence:** transcript excerpts, `recall` event lines, and the host's hook
  failure/timeout output (none). Host debug output is optional.
- **Timeout:** T_SCENARIO.
- **Cleanup:** the fixture stays for later scenarios, or is cleaned up.

### RQA-HOOK-02 Codex real hooks and injected search

- **Purpose:** same as RQA-HOOK-01 for the Codex hook map
  (`hooks-codex.json`).
- **Owner:** search, file sync, runtime.
- **Host:** Codex.
- **Preconditions:** RQA-INST-01 is `PASS` for Codex; the preflight is
  `READY`, with the exact hook commands trusted in the official UI.
- **Steps:** as RQA-HOOK-01, with these differences:
  - The edit goes through `apply_patch`.
  - The deletion in step 5 is also done through `apply_patch`. Codex has no
    Bash PostToolUse matcher.
  - Record one extra observation: delete through the shell tool, then probe.
    Expected per the current hook map: detection only at the next SessionStart
    or on request "훅이 놓친 변경 다시 동기화해줘". This observation is
    informational; a missed detection is not a FAIL. A stale injection after
    the sync request is a FAIL.
- **Expected and evidence:** as RQA-HOOK-01.
- **Timeout:** T_SCENARIO.

### RQA-SYNC-01 Pull add/update/delete with actual search results

- **Purpose:** changes that arrive through a user `git pull` (the plugin never
  pulls) become searchable, and deletions disappear.
- **Owner:** file sync (change set), search (results). Wiki (no enqueue
  while compile is off).
- **Hosts:** Claude Code, Codex (separate fixtures).
- **Preconditions:** RQA-HOOK-0x is `PASS` for this host; the fixture is
  selected and in S-main; compile is off; no session is open.
- **Steps:**
  1. In a fresh session, probe Q-beta, Q-gamma and Q-delta. Expected: S-main.
     Close the session.
  2. Record a wiki-state listing (`.memvane/compile`, `.memvane/wiki`).
  3. Run `stage upstream-change`, then `stage pull`.
  4. Start a fresh session. Probe Q-beta, Q-gamma and Q-delta at the T_FRESH
     schedule.
  5. Re-list the wiki state.
  6. Optional, secondary: run the same questions through the query skill.
- **Expected:**
  - S-pulled within T_FRESH: Present `DELTA_ADDED` and `BETA_V2`; Absent
    `BETA_V1` and `GAMMA_DELETE_ME`.
  - The wiki state is unchanged.
- **Evidence:** helper event log with HEADs, probes, the two wiki listings.
- **Timeout:** T_SCENARIO.
- **Cleanup:** clean up the fixture.

### RQA-GIT-01 Same-session branch switch

- **Purpose:** after a branch switch inside an open session, the
  previous branch is not injected as current.
- **Owner:** file sync, search.
- **Hosts:** Claude Code, Codex.
- **Preconditions:** a fresh fixture, selected and indexed, S-main confirmed in
  an open session.
- **Steps (Claude Code):**
  1. Ask "git switch qa/feature 실행해줘". The Bash tool runs; Claude fires
     the Bash file-change-event hook.
  2. In the same session, probe Q-alpha and Q-feature at the T_FRESH
     schedule.
  3. Ask "git switch main 실행해줘" and probe again.
- **Steps (Codex):**
  1. Ask the agent to run the same switch.
  2. Probe immediately (strict check).
  3. Ask "훅이 놓친 변경 다시 동기화해줘".
  4. Probe at the T_FRESH schedule.
  5. Switch back and repeat.
- **Expected:**
  - At no probe after the switch completed is `ALPHA_MAIN` injected as
    current content. An empty result, or an explicitly marked stale result,
    is acceptable.
  - S-feature holds within T_FRESH (Codex: after the sync request).
  - After switching back, S-main holds within T_FRESH.
- **Evidence:** transcript timestamps of the switch result and of each probe
  hook, `recall` event lines, helper `show` with the branch.
- **Timeout:** T_SCENARIO.
- **Cleanup:** clean up the fixture.

### RQA-GIT-02 New worktree and actual cwd switch isolation

- **Purpose:** a linked worktree without its own settings gets no plugin
  work, and its content never leaks into the main checkout.
- **Owner:** file sync, search.
- **Hosts:** Claude Code, Codex.
- **Preconditions:** a fresh fixture selected and indexed in `project/`
  (S-main); `stage worktree-add` done. The worktree branch predates the
  settings file, so the worktree has no settings.
- **Steps:**
  1. Start a fresh host process whose actual working directory is
     `wt-worktree/` (start it from that directory; do not pass a path
     argument). Probe Q-alpha and Q-worktree. Expected: no notice, nothing
     injected, and the worktree is not indexed. Absent `ALPHA_MAIN` and
     `WORKTREE_ONLY`.
  2. Ask the agent to edit `docs/qa/alpha.md` in `wt-worktree/`. Expected: no
     sync wake for the main checkout.
  3. Start a fresh session in `project/`. Probe Q-worktree and Q-alpha.
     Expected: S-main-after-worktree, including `ALPHA_MAIN` (never
     `ALPHA_WORKTREE`) for the same relative path `docs/qa/alpha.md`.
- **Note:** a linked worktree whose settings derive the same name,
  collections, paths, roles and wiki path as the selected main checkout is
  served the main checkout's last published index read-only; sync, wiki
  compile, wiki lifecycle and learning run only from the main checkout. This
  fixture's worktree has no settings and does not cover that path.
- **Evidence:** cwd and `git rev-parse --show-toplevel` per session, recall
  `install` and `session` per `recall` event, probes.
- **Timeout:** T_SCENARIO.
- **Cleanup:** clean up the fixture; this removes the worktree too.

### RQA-GIT-03 Dirty and untracked files

- **Purpose:** uncommitted changes to tracked files and new untracked files
  inside an approved collection count as the current worktree's content.
  Reverting them removes them.
- **Owner:** file sync, search.
- **Hosts:** Claude Code, Codex.
- **Preconditions:** a fresh fixture selected and indexed (S-main); no open
  session.
- **Steps:**
  1. Run `stage dirty`.
  2. Start a fresh session. Probe Q-alpha and Q-untracked at the T_FRESH
     schedule. Expected: S-dirty.
  3. Ask "docs/qa/alpha.md 변경을 되돌리고 docs/qa/untracked-note.md를
     삭제해줘". Claude does this with Bash. On Codex, follow with "훅이 놓친
     변경 다시 동기화해줘".
  4. Probe again. Expected: Absent(`DIRTY_TRACKED`, `UNTRACKED_NEW`),
     Present(`ALPHA_MAIN`).
- **Evidence:** `git status --porcelain` at each step, probes.
- **Timeout:** T_SCENARIO.
- **Cleanup:** clean up the fixture.

### RQA-RACE-01 Mid-switch query, late queue worker and retry

- **Purpose:** a query during a switch, a worker that finishes late, and an
  I/O retry never publish stale or deleted content as current.
- **Owner:** file sync, search; runtime (wake queue, retry).
- **Hosts:** Claude Code (Parts A and B use the Bash hook); Codex (Part C,
  plus Parts A and B with a sync request).
- **Preconditions:** a fresh fixture, selected and indexed, S-main.

**Part A: mid-switch query.**

1. Ask "git switch qa/feature 실행해줘".
2. Submit Q-alpha as the very next prompt, within 5 s of the tool result.
3. Expected: `ALPHA_MAIN` is not injected as current. Either S-feature
   content, an empty result, or a marked stale result.

**Part B: late worker flip-flop (selected memvane-db engine).**

memvane-db owns search and indexing in this release. Part B therefore runs on
a fixture whose route was selected through setup onboarding (LI-MIG in the
same fixture).

How the selected engine enforces the invariant:

- One runtime dispatcher per OS UID runs `laya-sync` wakes
  (`core/runtime_work.py` `drain`, `runtime-worker.lock`). A wake re-put while
  a sync runs bumps its revision and survives the revision-scoped ACK. A held
  old attempt therefore **delays** the newer revision. The newer revision
  cannot publish first; a second selected writer would be deferred
  (`owner_busy`) by the queue owner lock (`core/laya_index_sync.py`
  `_owner_lock`).
- When the old attempt resumes, `sync_full` rescans the source. A vector
  batch for another manifest is refused before any queue/index write
  (`vector_batch_stale`).
- A retained old claim is replanned against the current source.
  `publish` checks the sealed manifest, base head and revocation epoch in one
  transaction (`index_stale`/`stale`).
- Every recall rescans the source against the pinned seal, so a stale head
  fails closed rather than being injected.
- The source test is in `test/fixtures/laya-index-sync-checks.py`
  (`test_late_feature_attempt_after_switch_back_never_publishes`,
  `test_dead_feature_worker_claim_and_generation_cannot_follow_newer_publish`).
  It is L0 only.

QA-only barrier (`core/laya_product_sync_worker.py`):

- **Default.** Without `LAYA_QA_SYNC_BARRIER`, no file is read and nothing
  waits.
- **Variable value.** With the variable set, it must name
  `<fixture>/.qa-laya-sync-barrier.json`. `<fixture>` is the resolved
  `root` from the fixture `MANIFEST.json`, for example `/private/tmp/...` on
  macOS.
- **Arm file contents.** The arm file is mode 0600 and must match the
  fixture owner marker. It names the exact selected project
  (`paths.project`), `scopeId`, `dbIncarnation` (from the installer
  `status`), a fresh 32-hex `nonce` and `timeoutSeconds` (1–600).
- **Validation.** A project outside that fixture is untouched. A malformed or
  unsafe arm, or the right project with a different scope or DB, fails that
  sync with `qa_sync_barrier_invalid` before the model starts.
- **Hold.** The first matching attempt holds after its complete vector batch
  is built and before anything is enqueued. It writes `.qa-laya-sync-barrier-<nonce>.held`
  (source manifest, pid, time).
- **Release.** It waits for a regular file `.qa-laya-sync-barrier-<nonce>.release`
  that the operator creates.
- **Outcome record.** It then writes `.done` (release time, finish time,
  state/generation or refusal reason). A timeout writes `.done` with
  `laya_qa_barrier_timeout` and leaves the wake pending.
- **Later attempts.** Later attempts with the same nonce never wait.
- **Scope of the variable.** The variable is valid only in the Part B host
  session. It invalidates every other scenario (section 3).

1. Preconditions: Claude Code; selected fixture at S-main, indexed; no
   runtime dispatcher process alive for the QA UID. Start the fresh host
   session with the variable naming the as-yet-absent arm path. Wait until
   its SessionStart `laya-sync` wake has finished and S-main remains current.
   Only then write the arm file. A missing arm file is ignored, so this
   SessionStart attempt cannot consume the nonce.
2. Ask "git switch qa/feature 실행해줘". The successful Bash PostToolUse wakes
   `laya-sync`. Wait for `.held`; record its source manifest, pid and both
   Git HEADs (fixture `stage` events or `git rev-parse`).
3. Ask "git switch main 실행해줘". Read-only, confirm the `laya-sync` wake
   revision advanced. Probe Q-alpha and Q-feature while the old attempt is
   still held.
4. Create the release file. Wait for `.done`, then for the retained wake to
   settle. Probe Q-alpha and Q-feature immediately and again at T_LATE.
5. Copy the barrier records into `$EVID` before fixture cleanup. End the
   session and confirm that no process of the QA UID still carries the
   variable before the next scenario.
6. Expected:
   - `.held` `sourceManifest` is the S-feature manifest. An S-main hold is
     `BLOCKED`, even if the later probes appear correct.
   - `.done` `finishedAt` is later than the switch-main event and `releasedAt`.
   - The old attempt reports a refusal (`vector_batch_stale`, `index_stale`
     or a replan) and never `indexed` with the held feature manifest.
     `laya_sync_yield_priority` is inconclusive: repeat with a new nonce.
   - The installer `status` head ends at a generation whose manifest is S-main.
   - S-main is current at every probe after the second switch. `FEATURE_ONLY`
     and `ALPHA_FEATURE` are never injected.
   - A missing `.held` (for example, a dispatcher without the variable)
     means the race did not run: `BLOCKED`, repeat with a new nonce. A fast
     flip-flop without the records cannot be `PASS`.

**Part C: I/O retry.**

1. Close the session and run `stage deny-read`.
2. Start a fresh session and probe Q-beta. Expected: beta is not treated as
   deleted. Its previous content stays searchable, and the I/O condition is
   reported in hook or worker logs without a hook failure.
3. Run `stage restore-read` and start a fresh session. Expected: normal
   state.

- **Evidence:** precise timestamps (tool result, hook start, log line),
  `recall` event lines, hook/worker log excerpts for the I/O condition, and the
  `setup` skill's status report of pending or deferred work, if reported.
- **Timeout:** T_SCENARIO.
- **Cleanup:** run `stage restore-read` before `cleanup` (cleanup also
  restores permissions).

### RQA-WIKI-01 Wiki duplicate work prevention

- **Purpose:** one source revision reaching several consented paths produces
  at most one paid extraction and one card update.
- **Owner:** wiki.
- **Hosts:** Claude Code, Codex.
- **Preconditions:**
  - A recorded wiki cost approval with `compile.budget` caps.
  - The extractor and verifier engines are available.
  - A fresh fixture, selected.
- **Steps:**
  1. Ask "wiki compile 켜줘. SessionStart에서 감지한 원문도 wiki 대상으로 해줘."
     Expected: `compile.mode` is on, and the triggers include
     `post_tool_source` and `session_start_source`.
  2. Record the wiki-owned baseline: queue and ledger entries, card files and
     hashes.
  3. Ask the agent to edit `docs/qa/wiki-source.md` so that it contains
     `WIKI_FACT_R2` (PostToolUse path).
  4. Start a fresh session (SessionStart path).
  5. Ask "훅이 놓친 변경 다시 동기화해줘" (manual sync path).
  6. Wait up to T_WIKI for queues to settle.
- **Expected:** for the pair (`docs/qa/wiki-source.md`, SHA-256 of the R2
  bytes):
  - at most one extractor call and one verifier pass;
  - at most one card created or updated;
  - no second paid call.
- **Known gap:** revision idempotency is partial. The compile queue drops a
  duplicate row only for the same `(collection, path, revision, trigger)`
  (`core/wiki_source_intent.py` `revision_key`), and on the selected Laya route
  the worker skips a revision that already has a compiled card
  (`already_compiled_revision`). The same revision arriving through a different
  trigger is still queued. Until this scenario passes, it blocks a formal
  release.
- **Evidence:** ledger and queue diffs per path, revision and engine; compile
  logs; card diff.
- **Timeout:** T_WIKI + T_SCENARIO.
- **Cleanup:** keep the fixture for RQA-WIKI-02, then clean it up.

### RQA-WIKI-02 Wiki provenance

- **Purpose:** a published card states exactly which source revision it came
  from and how it was verified, and stale cards are not injected as current.
- **Owner:** wiki; search (injection).
- **Hosts:** Claude Code, Codex.
- **Preconditions:** RQA-WIKI-01 produced a card for R2.
- **Steps:**
  1. Inspect the card's frontmatter.
  2. Probe Q-wiki.
  3. Revert the source to R1 through an agent edit.
  4. Start a fresh session and probe Q-wiki again.
  5. Delete `docs/qa/wiki-source.md` through an agent edit, start another
     fresh session, and probe Q-wiki. Record the source tombstone and any
     derivative card/index cleanup through the wiki and search status views.
- **Expected:**
  - The card's sources name `docs/qa/wiki-source.md`. The recorded source
    revision or hash equals the R2 bytes. `status: verified` appears only
    after a verifier pass, and the verifier identity is recorded.
  - The body contains `WIKI_FACT_R2`, not `WIKI_FACT_R1`.
  - The injected card shows the source path line.
  - After the revert, the R2 card is not injected as verified current content
    (wiki source freshness guard).
  - After source deletion, neither R1 nor R2 is injected as a current verified
    fact. The source removal and derivative index/card state converge within
    T_WIKI; retained audit data is labelled non-current.
- **Evidence:** card bytes and hash, source hashes, probes, `recall` event
  `card_read_*` and `sources_*` fields.
- **Timeout:** T_WIKI + T_SCENARIO.
- **Cleanup:** clean up the fixture.

### RQA-WIKI-03 Wiki compile OFF

- **Purpose:** when compile is off, no wiki work and no paid calls happen,
  while file indexing still works.
- **Owner:** wiki; file sync and search for the contrast check.
- **Hosts:** Claude Code, Codex.
- **Preconditions:** a fixture where compile was on (an RQA-WIKI-01 fixture,
  or freshly enabled).
- **Steps:**
  1. Ask "wiki compile 꺼줘". Expected: `compile.mode: "off"`.
  2. Record the wiki baseline.
  3. Ask the agent to edit `wiki-source.md`.
  4. Start a fresh session.
  5. Request a sync.
  6. Repeat the sync and fresh SessionStart once to exercise retry paths.
  7. Delete `wiki-source.md` through an agent edit and repeat the sync and
     fresh SessionStart. Probe Q-wiki and the deletion state.
- **Expected:**
  - No new queue or ledger entries.
  - No extractor or verifier invocation.
  - Card bytes unchanged.
  - Wiki indexing still works, but the changed raw-only content creates no Laya document or search hit;
    the unchanged card is never presented as a fresh revision of that source.
    Compile remains OFF across retry and deletion.
- **Evidence:** wiki listings before and after, probes, absence of compile
  activity in the logs.
- **Timeout:** T_SCENARIO.
- **Cleanup:** clean up the fixture.

### RQA-RES-01 Ordinary work is not blocked (no learning)

- **Purpose:** with learning not enabled, ordinary hooks finish within budget
  and no training work starts.
- **Owner:** runtime.
- **Hosts:** Claude Code, Codex.
- **Preconditions:** no learning profile and no grants (setup status reports
  learning as deferred or off); a fresh fixture, selected and indexed.
- **Steps:**
  1. In one session, run five probes, two agent edits and one sync request.
  2. Then start one fresh session.
  3. Snapshot the QA user's processes (`ps`, filtered to the plugin runtime
     roots) before and after.
- **Expected:**
  - No hook timeout or failure.
  - Every probe finishes within T_TURN.
  - No trainer, teacher or evaluation process is started, and no learning
    wake is executed.
  - Ordinary recall is never refused for a learning resource reason.
- **Evidence:** turn timings, process snapshots, setup status.
- **Timeout:** T_SCENARIO.
- **Cleanup:** clean up the fixture.

### RQA-RES-02 Training-only resource guard

- **Purpose:** an approved training job yields to ordinary work, and ordinary
  work is never blocked by it.
- **Owner:** learning (job); runtime (admission and yield).
- **Hosts:** Claude Code (Codex optional).
- **Status rule:** `BLOCKED` unless all of the following are recorded:
  - written learning approval;
  - an approved private test namespace;
  - a disposable synthetic corpus;
  - an approved local model;
  - an OS observation baseline.
  It never runs on manuscripts, personal projects or operating databases.
- **Steps (once approved):**
  1. Ask for a learning profile proposal through the `setup` skill. Accept
     only that exact proposal.
  2. Start one approved learning job on the synthetic corpus. Record its
     content job identity, `runtime-wakes.sqlite3` wake revision/due/status,
     training cursor group, and the owner process group before work begins.
     A duplicate wake for the same project must coalesce rather than start a
     second trainer or consume the same evidence twice.
  3. Hold admission with a reproducible foreground demand or approved OS
     pressure fixture. While the learning wake is retained, run five probes,
     two edits and one fresh session; record hook timings and confirm the
     queue's retry/backoff state. Release the condition and observe the same
     durable job resume without a new capture or teacher pass. Hold admission
     past the bounded drain lifetime; identify the product event that re-arms
     the queued job. Repeat with a weekly quota checkpoint and prove that
     `deferred_quota` retains a wake instead of receiving a completed ACK.
  4. During training, request a cooperative stop at an optimizer-group
     boundary. Verify that the exact committed cursor/head survives worker
     exit and restart; the resumed run starts at the next group and does not
     repeat a committed group. Also test an interrupted `running` wake, a
     duplicate wake during dispatch, and bounded queue capacity without
     losing an existing learning job. Repeat cooperative yield at least four
     times to show it never counts as a terminal failure or blocks the job.
     Enqueue a higher-priority index/update wake while training is active,
     then record when it is serviced; ordering only at dispatch start is not
     proof that active training yielded.
  5. At OS warning or failed monitor/cleanup proof, stop through the owned
     process path. Record whether the child and descendants ended; an
     unproved owned-group cleanup must leave admission blocked, not silently
     reclaim the reservation.
- **Expected:**
  - Ordinary hooks stay within budget.
  - Training yields cooperatively to foreground demand and resumes from the
    exact committed group when admission returns. Queue retry survives worker
    interruption and a new session; stale revision ACK cannot erase a newer
    wake. No second trainer, teacher call or evidence consumption occurs for
    the same job.
  - Resource refusal leaves the durable job pending and never turns it into
    a successful training receipt. An unproved cleanup keeps admission closed.
  - No checkpoint is promoted or applied without its own approval.
  - Training cleanup is proved; an unsettled cleanup reservation is `FAIL`.
- **Evidence:** wake/job revision and retry state, exact cursor/head hashes,
  content receipt/evidence identity, admission/monitor observations, process
  group and cleanup proof, fresh-session transcript and hook timings. Confirm
  production runs do not set fixture/sandbox bypass flags, and show whether
  direct `shared.launch` and queued `runtime_work.dispatch` paths both obey
  admission. Existing synthetic tests in `test/runtime_resources_test.py`,
  `test/runtime_training_watch_test.py`,
  `test/fixtures/runtime-wake-policy-checks.py`, and
  `test/fixtures/context-learning-coordination-checks.py` are L0 coverage,
  not this installed-candidate result.
- **Timeout:** 60 min.
- **Cleanup:** remove the test namespace through its approved path; preserve
  any unsettled state for review.

### RQA-PLAT-01 Unsupported or unverified platform

- **Purpose:** on a platform without the prebuilt native runtime, the plugin
  stays harmless and leaves the user's own QMD untouched.
- **Owner:** install.
- **Cells:** U-intel, U-linux. **Hosts:** whichever are available.
- **Preconditions:** record before-state hashes of any global QMD binary path,
  its config and data.
- **Steps:**
  1. Install the candidate.
  2. Start a fresh session in a fixture with minimal settings.
  3. Ask "이 프로젝트 auto-context 온보딩해줘".
  4. Probe Q-alpha.
- **Expected:**
  - The provider review refuses with `reviewed_native_platform_unsupported`;
    no route is selected.
  - At most one onboarding notice per session; nothing is injected.
  - Global QMD binaries, config and data unchanged.
  - No hook failure.
- **Evidence:** setup output, before and after hashes.
- **Timeout:** T_SCENARIO.
- **Cleanup:** clean up the fixture; restore the account state.

### memvane-db 전환 필수 행 (LI-*)

memvane-db로 QMD backend를 교체하는 릴리스에는 위 RQA 행에 더해 **설치된 정확한 후보**에서 다음을 필수로 기록한다. 임시 Git full-plugin 복사본과 합성 attestation·이관 리허설은 marketplace 설치 후보의 fresh-host 증거가 아니다. L0 통과로 이 행을 대체할 수 없다.

| 행 | 설치 후보에서 확인할 실제 결과 |
|---|---|
| LI-INST | exact-commit bundle·설치 소유 journal·현재 파일 identity·DB/queue incarnation을 새 Claude/Codex 세션이 검증하고, 일치하지 않으면 QMD로 fallback하지 않고 차단한다. |
| LI-MIG | minimal settings(`{name, skipPaths}`) 프로젝트의 fresh-wiki 온보딩(`plan-fresh-wiki-origin` → `review-index-provider` → `prepare-fresh-wiki-origin` → `finalize-index-origin` → `activate-index-origin`)이 **위키 문서만** membership·본문·FTS·벡터로 색인하고 원본 원문은 출처·최신성 증거로만 확인한다. settings 파일은 다시 쓰지 않는다. 불일치/중단 시 이전 선택이 유지되고, QMD-origin ledger(`qmd-cutover.json`)가 남은 state root는 `qmd_origin_detach_required`로 거부된다. |
| LI-SCOPE | 미온보딩 프로젝트 무작업, linked worktree 읽기 전용, 삭제·rename, 중복 claim, 오래된 writer, 옛 pin 철회를 새 host 경로에서 확인한다. 다른 scope의 후보·순위·결과와 원문 독립 문서가 섞이지 않고 갱신 불가 시 명시 오류를 낸다. |
| LI-RECOVER | 게시/ACK 및 전환 각 중단점의 재시작, 첫 새 쓰기 전 rollback, 첫 새 쓰기 뒤 자동 rollback 차단과 전진 복구를 확인한다. 실제 전원 손실은 별도의 시험 없이는 주장하지 않는다. |
| LI-VECTOR | 선택된 모델 파일·Node/native dependency closure·전처리/청킹 recipe의 출처와 hash, 위키 문서·질의 차원과 profile, 위키 전량 재색인 seal, scope/세대 필터 선행 및 과예산 무결과를 증명한다. 원본 원문은 문서 임베딩 대상이 아니다. 모델 실행에는 별도 승인된 자원 gate를 사용한다. |
| LI-RECALL | 기존 QMD `rerank:false` 어휘 변형+벡터 결합과 정확한 식별자/EP를 기준으로, Laya의 **위키 카드만** 검색·선별·주입하고 검증된 원문 위치를 제공하는지 lex probe, minScore/topN, 결과 포맷을 새 Claude/Codex 세션에서 비교한다. 위키에 없는 원문 전용 표식은 Laya 결과·자동 주입에 없어야 한다. 위키 카드에 정상적으로 포함된 발췌와 링크는 유지한다. 벡터 단독 합성 검색은 이 행을 통과시키지 않는다. |

모델/벡터 재생성이 필요한 경우 provenance 또는 명시 승인·자원/비용 gate를 함께 기록한다. 필수 행의 `NOT RUN`/`BLOCKED`/`FAIL`은 전환 릴리스를 막는다.

Laya 검색·임베딩의 운영 부하 큐 **확장**은 v2 오픈 이후 선택 검토 항목이다.
학습 작업의 운영 부하 큐와 admission·중지/재개 검증은 `RQA-RES-02`의
필수 조건이다. 이 구분은 검색·임베딩 기능 자체를 제품 범위에서 제외하지
않는다.

## 10. Domain ownership

Scenarios respect domain ownership. Two rules apply:

- A verdict uses evidence produced by the owning domain.
- A scenario must never edit another domain's state (queues, snapshots,
  ledgers, journals, pointers, learning DB) to force a result. A cross-domain
  expectation, such as "file sync must not enqueue wiki work while compile is
  off", is judged on the owning domain's state, which is only read.

| Owner | Scenarios | Verdict evidence |
|---|---|---|
| File sync | SYNC-01, GIT-01..03, RACE-01, HOOK-0x edits | Searchable state after the change set; snapshot and change reports through the skills |
| Search | HOOK-0x, SYNC-01, GIT-*, RACE-01, WIKI-02 injection | Recall log selection lines, injected context |
| Wiki | WIKI-01..03, SYNC-01 negative | Wiki queues, ledgers, cards, compile logs (read only) |
| Learning | RES-02 | Learning receipts and status through the `setup` skill (read only) |
| Install | ID-00, INST-01..04, PLAT-01 | Identity hashes, `setup` skill outputs, journal phase as reported |
| Runtime | RES-01, RES-02, RACE-01 retry, HOOK-0x hook budgets | Hook timings, process snapshots, deferred or retry status |

## 11. Changes after QA: reruns and identity match

- **Binding.** Every scenario record carries `artifactManifestSha256`. A
  result applies only to that hash.
- **Change detection.** Any change to the candidate tree after QA produces a
  new hash, so old full-row `PASS` verdicts cannot be relabelled as final
  artifact results. This covers code, skills, hooks, manifests, locks, docs
  and version strings. List changed paths with
  `git diff --name-only <qa-commit> <new-commit>`. Retain unchanged source,
  model and backup evidence as preparation; use the impact map below to
  repeat only the necessary installed-host assertions. Accumulate fixes into
  a reviewed candidate before this rerun instead of repeating the entire
  gate for every intermediate commit.
- **Rerun set.** Always the core smoke (RQA-ID-00 + RQA-HOOK-01 +
  RQA-HOOK-02), plus every scenario mapped from the changed paths:

| Changed paths | Rerun in addition to core smoke |
|---|---|
| `core/laya_host_response.py`, `core/laya_incremental_docs_query.py`, `core/wiki_freshness.py`, `skills/query/**` | SYNC-01, GIT-01..03, RACE-01, WIKI-02 |
| `core/laya_index_sync.py`, `core/laya_product_sync_worker.py`, `skills/update/**` | SYNC-01, GIT-01..03, RACE-01, WIKI-03 |
| `core/wiki_*`, `skills/wiki-*/**`, `agents/**` | WIKI-01..03 |
| `core/context_learning/**` | RES-01, RES-02 |
| `core/setup_entry.py`, `core/laya_product_install.py`, `core/laya_embed_runtime.py`, `core/legacy_cleanup.py`, `locks/**`, `skills/setup/**` | INST-01..04, PLAT-01 |
| `core/runtime_work.py`, `core/runtime_wake_policy.py`, resource coordination modules | RES-01, RES-02, RACE-01 |
| `core/config.py`, `core/laya_index_host_route.py`, `hooks/**`, `hermes_adapter/**`, `plugin.yaml`, `__init__.py`, any host manifest or marketplace file | **All** scenarios |
| Version strings only | INST-01, INST-02 |
| Documentation only (`docs/**`, `README.md`; not `skills/**` or `agents/**`) | Core smoke only |
| Any path not listed | **All** scenarios |

- **Deployed identity.** The marketplace entry is not pinned to a revision. After
  publishing, install from the **public** marketplace in a clean QA account.
  Run RQA-ID-00 and the core smoke. The deployed `artifactManifestSha256` must
  equal the QA'd hash. On a mismatch, the release is not announced. Retract or
  supersede it and record the incident.

## 12. Cleanup and recovery (all scenarios)

- Remove fixtures only with the helper's `cleanup`. Evidence stays in `$EVID`.
- After the gate:
  - Uninstall the plugin from the QA account through each host's plugin flow,
    or restore the clean account or snapshot. Record which.
  - Never touch a maintainer's real host settings, projects or databases.
- When a scenario leaves `recovery_required`, an unsettled cleanup or startup
  reservation, or an unexpected pointer state:
  - Stop dependent scenarios and preserve the state.
  - Record `FAIL`, or `BLOCKED` if the QA harness caused it.
  - Resolve it only through the documented skill flows. Never delete journals,
    reservations or pointers to continue.

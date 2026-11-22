---
name: setup
description: Use for an explicit MemVane onboarding of a project onto memvane-db, preparing the local MemVane embedding runtime, an explicit learning profile. Produces reviewed plans; applies only on explicit request.
---

# Setup: MemVane onboarding

Users can ask “이 프로젝트 MemVane 온보딩해줘”, “MemVane 색인 준비 상태 보여줘”, “인덱스 활성화해줘”. Always name the project root and show the result of each phase.

Install this host plugin through its marketplace before using this skill. Plugin registration and hook trust are separate host actions; this skill never changes them. Resolve `ROOT` from a verified host plugin-root variable (`CLAUDE_PLUGIN_ROOT` or `PLUGIN_ROOT`), or from the absolute path of this `SKILL.md` by ascending two directories, and verify `ROOT/entry/setup.py` exists. Never infer the plugin root from the target project git root. Run every action through `python3 "$ROOT/entry/setup.py" <action> --project "$PROJECT_ROOT" ...` (it disables bytecode).

A project without a selected MemVane route gets no recall, indexing, compile or learning work. Its SessionStart shows one short notice line, plus one answer-command line for the agent, when it has `.memvane/settings.json`; this skill is the onboarding path the notice names.

## Onboarding notice answers

The notice's second line, `[memvane] onboarding-answer <JSON>`, holds the exact commands as `argv` (run as is) and `shell` strings. When the user declines, run the matching one: `skip` (`onboarding-skip --session <id>`) hides the notice for that host session only, kept for 30 days (offered only when the event carried a session id), and `never` (`onboarding-never`) hides it in every later session of that project. `onboarding-status` shows the answer. The answer is kept per project under `~/.local/state/memvane/v1/onboarding/`, outside the repository, so it never affects another project or a teammate. It only hides the notice; it never selects a route or starts indexing, wiki generation or learning. When the user explicitly asks to onboard a project, run `onboarding-resume --project "$PROJECT_ROOT"` first so the notice comes back if this onboarding is not completed.

## Minimal settings

Onboarding reads `.memvane/settings.json` with required `name` and optional `skipPaths`, `diagnostics`, `contextLearning`, and `sharedLearning`. Other keys or an invalid `name` or `skipPaths` refuse as `minimal_settings_required`; report that and ask the user to write the minimal file (the `name` slug derives the raw collection `<slug>` at the project root and the wiki collection `<slug>-wiki` at `.memvane/wiki`). Outside the learning operations described below, never rewrite settings.json on the user's behalf. A selected route binds the resolved settings identity, so editing `skipPaths` keeps it selected; changing `name` changes the identity and blocks the route as `route_settings_changed`.

Projects that only have `.auto-context/` need the owner to move it to `.memvane/` before onboarding.

## Embedding runtime

The provider is the MemVane embedding runtime under `~/.local/state/memvane/v1/embed-runtime`; check it with `index-runtime-status`. When it is missing on a new machine, `provision-index-runtime --approve-index-runtime-download` builds it: Node comes from `PATH` first, then mise/fnm/nvm/volta/asdf/Homebrew installs, accepted only for a supported major (22, 24 or 26); the pinned lock `locks/laya-embed-runtime` installs node-llama-cpp 3.18.1 and its prebuilt native package with `npm ci --ignore-scripts`; the GGUF is downloaded from its pinned URL and accepted only with the pinned sha256. Pass `--provider-model-file` with an absolute path that has no symlink in it to use an existing local copy of that exact GGUF instead of downloading the model; the npm packages still come from the registry, so `--approve-index-runtime-download` is needed then too. When provisioning ends with `laya_embed_runtime_download_failed` (the model host cannot be reached or no longer serves that revision) or `laya_embed_runtime_model_sha_mismatch` (the bytes received are not the pinned file), nothing was installed: tell the user that the pinned model file could not be fetched, and that a copy of the exact file (the name and sha256 in `docs/architecture.md`) obtained another way can be given with `--provider-model-file`. Never accept a file whose sha256 differs. State the network scope (npm registry, model host) before asking for approval. `gc-index-runtime` lists generations; with `--approve-index-runtime-gc` it removes those that are neither active, previous, nor named by an install attestation.

## State cleanup

`state-cleanup` (no `--project`) lists what a cleanup would remove and writes nothing: each entry of `~/.local/state/memvane/v1/installs` with `action` (`remove` or `keep`), `reason`, the project it belonged to and its size, plus the embedding runtime generations of `gc-index-runtime` under `indexRuntime`. An install is `remove released` only when its project folder exists and no pointer in it names that install; `selected`, `building`, `project_unreachable` (the project folder is not there, possibly an unmounted volume), `pointer_unreadable` and `unrecognized` entries are kept. Show the list and the total size to the user. Only after the user approves that list, run `state-cleanup --approve-state-cleanup`; it removes the `remove` rows (each install with its `vector-cache/`) and the unused runtime generations. A `partial` result names what stayed and why (`busy`: a sync or setup holds that install; ask again later).

`state-cleanup --all-state` previews the removal of every per-user root (state, support and cache directories: selected installs, their stores, learning namespaces and runtimes). Run `state-cleanup --all-state --approve-state-cleanup` only when the user asked, in their own words, to delete everything, and has approved that preview. Never widen a plain cleanup request to `--all-state`. Tell the user that every project then needs onboarding again and that the plugin should be removed afterwards, since a running session recreates the base directories. No cleanup touches a project's `.memvane/` folder or `~/.config/memvane`.

## Onboarding

Onboarding is a reviewed setup operation for the requested project. Confirm the project root, collection roots, plugin version, provider identity and resource scope. Apply only the user's authorization for the reviewed operation. The project needs minimal settings and its wiki under `.memvane/wiki`; the raw collection is the project root.

The installation pointers and wiki keeper must remain untracked so the keeper preserves the wiki directory across branch changes. After activation succeeds, tell the user to add these lines to the project's `.gitignore`; never edit that file on their behalf. Files already tracked must be untracked separately:

```gitignore
.memvane/route.json
.memvane/selection.json
.memvane/wiki/.memvane-root
```

Activation returns these paths in `gitignoreEntries`; include the recommendation when reporting the completed onboarding. If a 1.0.1 setup session is waiting for vectors after a 1.0.2+ worker has prepared the lexical head, finish setup from the newer host session.

A project inside an already selected project cannot be onboarded as a separate nested install. An initial build may report `selected_route_unavailable`; explain that the parent project is already selected and use that project or a separate directory outside it. Multiple route records in one ancestor chain are refused as `ambiguous_route_records`.

The user asks in natural language; the actions below are internal maintenance steps of this skill, run in this order. Every action except `plan-fresh-wiki-origin` takes `--state-root "$STATE_ROOT"`, where a new install's state root is `~/.local/state/memvane/v1/installs/<install id>` with a fresh 32-hex install ID (any other new path refuses as `unsupported_state_root_creation`). Report each step's `status` (and `reason` on a refusal) before the next one.

1. `plan-fresh-wiki-origin` creates `.memvane/wiki` if absent and returns `review_required` with the plan's collections and a `proposalSha256`. Show the plan and ask for approval.
2. `review-index-provider --provider-dimension <n>` (read-only, optional `--provider-model-file`) returns `review_required` with the provider identity and its own `proposalSha256`. Show the runtime, model and worker identity.
3. `prepare-fresh-wiki-origin --install-id <id> --accepted-origin-sha256 <plan proposalSha256> --accepted-provider-sha256 <review proposalSha256> --provider-dimension <n>` runs only on the user's approval of both proposals and returns `initial_build_registered`. The initial build then runs in the background.
4. `index-build-status` reports the build `phase` and the latest bounded progress snapshot: stage, ingested cards and processed vector content sets. Counts can be `null` when no recent snapshot is available; do not infer zero. Wait for `ready`, then finalize and activate. Ready means lexical search is available after activation; vector filling continues in the background. `index-origin-status` shows the install record.
5. `finalize-index-origin` returns `initial_build_finalized`, or `initial_build_pending` while a build pass is still needed.
6. `activate-index-origin --approve-index-origin-activation` selects the index for the project and returns `selected`. Run it only on the user's explicit activation request. Activation also authorizes this project's automatic learning: local training and evaluation, and gold labeling by the default teacher (see Local learning). Training and evaluation stay on this machine. For labeling, the edit text and the candidate cards' bodies of each captured case go to the host CLI of the session that produced the case (Claude Code or Codex, the same account and provider). Files that git ignores and `skipPaths` stay out of capture. Tell the user the teacher can be turned off. `recover-index-origin` resumes an interrupted activation.
7. `restore-index-origin --approve-index-origin-restore` returns `restored` (abandoned or withdrawn) or `released`; run it only on the user's explicit request. `index-build-rescan` requests another build pass for an install that was never selected.

An interrupted onboarding step converges when the same step runs again. A raw tree over the sync scan bounds (4,096 files or 64 MB) refuses at prepare as `raw_scan_limit_exceeded`; report it before any build starts. When activation refuses as `initial_build_out_of_date` or `initial_build_head_changed`, run finalize again: it requests one more build pass (`initial_build_pending`) and, once the build is ready again, reseals the install so activation can proceed. Restore before activation abandons the prepared install so a new prepare with a new install id can start; after activation it withdraws the selection even after a selected write (`released`), so setup can onboard the project again. When SessionStart reports that this plugin cannot read the project's install record (a project pointer of a lower or unknown major), restore releases those project pointers without reading a state root, and a new plan then starts onboarding; a pointer of a newer major is never released. Restore never deletes the old install directory or its database. A build rescan is for an install that was never selected; a selected project refreshes through `update`.

Each linked git worktree reads its own published view of its wiki cards. The main checkout's sync publishes these views. Until a view is published, recall adds no context and reports `view_preparing`.

## Local learning

An activated project defaults to the local automatic-apply profile with the
default teacher. Existing profiles and explicit disabled settings are preserved.

The default teacher labels each captured edit case once, through the host CLI
of the session that produced it: Claude cases through `claude`, Codex cases
through `codex`, never the other host. It sends only the edit text and the
bodies of the case's candidate cards; it never reads or attaches source files.
It runs in the background learning worker, with the lowest-priced model the
bundled tier table allows, and with the host CLI's isolation options checked
before every call. When a host CLI is missing, has no allowed model or fails
its isolation check, that host's cases wait; recall, capture and local training
continue. SessionStart names a missing CLI, or a host labeling has paused for a
day, at most once a week per host and reason.

When the user asks to stop sending cases to the teacher, run `teacher-off
--project "$PROJECT_ROOT"`; `teacher-on` turns it back on. The command writes
only the namespace switch `teacher-switch.json` and reports `teacher_off` or
`teacher_on` with `teacherOff` (the effective state; this switch is the only
way to turn the teacher off). While it
is off, a default local profile is not moved to the teacher, and an already
moved profile keeps its files while the teacher makes no calls. Capture,
recall and local training continue. The switch can be written before
onboarding finishes, and onboarding then uses the default local profile. If the managed learning
runtime is inactive, the selection is recorded and preparation resumes after it
is activated; a selected profile does not prove that training or model
replacement has occurred. Resource admission still applies.

When the user asks to turn off learning for this project, confirm the project
root, then update only the existing settings fields `contextLearning.capture`
and `contextLearning.autoCycle` to `false` and `sharedLearning.enabled` to
`false`, preserving the other settings. Report that learning is disabled. This
explicit request authorizes that settings edit.

To activate an already staged managed learning runtime, review the staged
generation executable and local model directory with the user. On explicit
approval run `activate-learning-runtime --learning-runtime-executable <absolute
venv python> --learning-model-dir <absolute model directory>
--approve-learning-runtime-activation`. The command records the selected
generation as `activated_pending_proof` after checking its files and identity.
The first training run or selector preparation performs the compatibility proof
under resource admission. If resources are unavailable, it retries later; if the
proof fails, the previous selection remains active and SessionStart reports the
failure. Runtime activation does not prepare a project's profile or selector.

For an explicitly requested profile, run `plan-learning-profile --project "$PROJECT_ROOT" --learning-namespace "$APPROVED_NAMESPACE" --learning-choice local-auto-apply --learning-migration new --learning-expires-at-unix-ms "$REVIEWED_EXPIRY"`. Explain the choices in natural language: local learning plus validated candidate application, staged local learning only (`local-staged`), or disabled (`off`). A profile requires an already prepared private namespace, local runtime/model and inference identity, and a separate current train/evaluate execution grant. Missing preparation or corpus authorization remains review-required; do not create a grant, import data, download weights or call a teacher to satisfy it. Use `preserve` migration for an existing learning configuration; never switch to `new` to escape existing false choices. Show the proposal's actual settings changes, project/source/cohort and runtime/model scope, resource/batch/disk limits, expiry and application authority.

For a manually proposed profile, use `activate-learning-profile` only on acceptance of that exact proposal, with identical options plus `--accepted-learning-profile` equal to its `proposalSha256`. An interrupted profile activation resumes with the identical accepted options; changed scope/expiry/settings require review. Report selected, waiting, deferred, blocked, staged and applied states separately. A selected profile is not evidence that real training, model replacement or host-hook operation passed.

Background maintenance moves a default local profile to the default teacher profile in one recorded change. If that change stops partway, learning and the selection model stay paused and SessionStart says to ask this skill to recover the learning setup. On that request, first run `restore-teacher-enable --project "$PROJECT_ROOT" --plan` (read-only) and show each target's state (`old`, `new` or `other`). Run `restore-teacher-enable --project "$PROJECT_ROOT"` only on the user's explicit approval: it writes the saved previous bytes back to every target, removes the change record and reports `restored` with `changesApplied`. It leaves no off switch, so a later maintenance pass tries the change again unless the teacher is turned off.

`update` refreshes an already selected index; it does not replace this setup flow. Never activate a teacher, private checkpoint, or operating plugin hook as a side effect of preparing a runtime.

For updates, execution-only provider or worker changes are reattested by the background sync owner while retaining the existing index. If the embedding identity changes or cannot be verified, restore and onboard again.

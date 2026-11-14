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

Onboarding reads `.memvane/settings.json` with required `name` and optional `skipPaths` and `diagnostics`. Other keys or an invalid `name` or `skipPaths` refuse as `minimal_settings_required`; report that and ask the user to write the minimal file (the `name` slug derives the raw collection `<slug>` at the project root and the wiki collection `<slug>-wiki` at `.memvane/wiki`). Never rewrite settings.json on the user's behalf. A selected route binds the resolved settings identity, so editing `skipPaths` keeps it selected; changing `name` changes the identity and blocks the route as `route_settings_changed`.

Projects that only have `.auto-context/` need the owner to move it to `.memvane/` before onboarding.

## Embedding runtime

The provider is the MemVane embedding runtime under `~/.local/state/memvane/v1/embed-runtime`; check it with `index-runtime-status`. When it is missing on a new machine, `provision-index-runtime --approve-index-runtime-download` builds it: Node comes from `PATH` first, then mise/fnm/nvm/volta/asdf/Homebrew installs, accepted only for a supported major (22, 24 or 26); the pinned lock `locks/laya-embed-runtime` installs node-llama-cpp 3.18.1 and its prebuilt native package with `npm ci --ignore-scripts`; the GGUF is downloaded from its pinned URL and accepted only with the pinned sha256. Pass `--provider-model-file` to use an existing local copy of that exact GGUF instead of downloading. State the network scope (npm registry, model host) before asking for approval. `gc-index-runtime` lists generations; with `--approve-index-runtime-gc` it removes those that are neither active, previous, nor named by an install attestation.

## Onboarding

Onboarding is a reviewed setup operation for the requested project. Confirm the project root, collection roots, plugin version, provider identity and resource scope. Apply only the user's authorization for the reviewed operation. The project needs minimal settings and its wiki under `.memvane/wiki`; the raw collection is the project root.

Tell the user that the per-machine pointers `.memvane/route.json` and `.memvane/selection.json` should be ignored by Git. Suggest adding those two lines to the project's `.gitignore`; do not edit that file for them.

The user asks in natural language; the actions below are internal maintenance steps of this skill, run in this order. Every action except `plan-fresh-wiki-origin` takes `--state-root "$STATE_ROOT"`, where a new install's state root is `~/.local/state/memvane/v1/installs/<install id>` with a fresh 32-hex install ID (any other new path refuses as `unsupported_state_root_creation`). Report each step's `status` (and `reason` on a refusal) before the next one.

1. `plan-fresh-wiki-origin` (read-only) returns `review_required` with the plan's collections and a `proposalSha256`. Show the plan and ask for approval.
2. `review-index-provider --provider-dimension <n>` (read-only, optional `--provider-model-file`) returns `review_required` with the provider identity and its own `proposalSha256`. Show the runtime, model and worker identity.
3. `prepare-fresh-wiki-origin --install-id <id> --accepted-origin-sha256 <plan proposalSha256> --accepted-provider-sha256 <review proposalSha256> --provider-dimension <n>` runs only on the user's approval of both proposals and returns `initial_build_registered`. The initial build then runs in the background.
4. `index-build-status` reports the build `phase`; wait for `ready`. `index-origin-status` shows the install record.
5. `finalize-index-origin` returns `initial_build_finalized`, or `initial_build_pending` while a build pass is still needed.
6. `activate-index-origin --approve-index-origin-activation` selects the index for the project and returns `selected`. Run it only on the user's explicit activation request. `recover-index-origin` resumes an interrupted activation.
7. `restore-index-origin --approve-index-origin-restore` returns `restored` (abandoned or withdrawn) or `released`; run it only on the user's explicit request. `index-build-rescan` requests another build pass for an install that was never selected.

An interrupted onboarding step converges when the same step runs again. A raw tree over the sync scan bounds (4,096 files or 64 MB) refuses at prepare as `raw_scan_limit_exceeded`; report it before any build starts. When activation refuses as `initial_build_out_of_date` or `initial_build_head_changed`, run finalize again: it requests one more build pass (`initial_build_pending`) and, once the build is ready again, reseals the install so activation can proceed. Restore before activation abandons the prepared install so a new prepare with a new install id can start; after activation it withdraws the selection before the first selected write, and after that write only when the embedding provider, model or runtime changed (`released`), which is the path to re-onboard. When SessionStart reports that this plugin cannot read the project's install record (a project pointer of a lower or unknown major), restore releases those project pointers without reading a state root, and a new plan then starts onboarding; a pointer of a newer major is never released. Restore never deletes the old install directory or its database. A build rescan is for an install that was never selected; a selected project refreshes through `update`.

Each linked git worktree reads its own published view of its wiki cards. The main checkout's sync publishes these views. Until a view is published, recall adds no context and reports `view_preparing`.

## Explicit local learning profile

For an explicitly requested profile, run `plan-learning-profile --project "$PROJECT_ROOT" --learning-namespace "$APPROVED_NAMESPACE" --learning-choice local-auto-apply --learning-migration new --learning-expires-at-unix-ms "$REVIEWED_EXPIRY"`. Explain the choices in natural language: local learning plus validated candidate application, staged local learning only (`local-staged`), or disabled (`off`). A profile requires an already prepared private namespace, local runtime/model and inference identity, and a separate current train/evaluate execution grant. Missing preparation or corpus authorization remains review-required; do not create a grant, import data, download weights or call a teacher to satisfy it. Use `preserve` migration for an existing learning configuration; never switch to `new` to escape existing false choices. Show the proposal's actual settings changes, project/source/cohort and runtime/model scope, resource/batch/disk limits, expiry and application authority.

Only on acceptance of that exact proposal, use `activate-learning-profile` with identical options plus `--accepted-learning-profile` equal to its `proposalSha256`. Never infer acceptance from the recommendation, onboarding approval, a prior train/evaluate grant or live selection. An interrupted profile activation resumes with the identical accepted options; changed scope/expiry/settings require review. Report selected, waiting, deferred, blocked, staged and applied states separately. A selected profile is not evidence that real training, model replacement or host-hook operation passed.

`update` refreshes an already selected index; it does not replace this setup flow. Never activate a teacher, private checkpoint, or operating plugin hook as a side effect of preparing a runtime.

# Coordinated release ownership Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans inline. Existing user authorization covers PR3 completion under the original Claude-cache gate; do not wait for optional permission to relax it. Narrow fixture delegation is authorized.

**Goal:** 양 호스트를 먼저 설치한 뒤 기존 bundle 잠금과 정상 업그레이드 API로 새 Claude 캐시의 service/embed를 검증한다.
**Architecture:** 개발용 `scripts/release_coordination.py`가 설치 중 모든 대상 bundle lock을 보유한다. 설치 확인 뒤 프로젝트별 준비된 새 worker에 GO를 보내 정상 `laya_bundle_upgrade.run`을 실행한다. local handoff operator는 네 설치 명령·버전/commit 확인·9개 프로젝트·companion/검증을 구성한다.
**Tech Stack:** Python stdlib/fcntl/subprocess, Node test wrapper, existing global guard. New Python is for compatibility with the existing Python guard/release tooling.
**Spec:** `/Users/dulee/work/memvane-handoff/CODEX_PROMPT.md:22–25`, `docs/runtime-resource-coordination.md` service lifecycle, existing `core/laya_bundle_upgrade.py` lock/worker API.

## Global Constraints
- Product hooks/provider/worker/schema behavior unchanged. Protected3 unchanged. Version7 manifests .31→.32; no direct main push, force/rebase/reset or cache editing.
- No fabricated version/reason/interpreter, same-version drain override, resident signal or lock-file deletion. Optional Codex-path relaxation is NOT approved.
- All9 private existing state roots locked before installer callback. Upgrade only after both installations verified. CLI child descriptors are non-inheritable/close_fds, no pass_fds.
- READY imports new bundle only; no upgrade before GO. EOF before GO cancels. Parent exception/timeout reaps only its retained helper child before releasing remaining locks.
- Global development Lease/hold applies; tests realpath tempHOME/fixtures only, no installer/network/liveDB/models. Resource hash refusal remains binding; ordinary tests use kind=test.
- A release race may still win after unlock: actual preferred owner path is a gate, never assumed; failure/busy stops without force/retry loops.

## Review Focus
1. Partial lock acquisition/installer exception must release only owned FDs, never unlink lock files.
2. Symlink/hardlink/unsafe permissions/root or named inode replacement refuse before upgrade.
3. Nested installer callback cannot wait forever: external install steps have finite timeout and no nested development-guard acquisition.
4. Worker readiness/EOF/timeout must not launch a hidden upgrade or leak owned children/FDs.
5. All untouched project locks remain held while the current project's ordinary worker runs; old reader route/data is not changed by coordination.

## Task1: Coordination helper and synthetic TDD
**Files:** Create `scripts/release_coordination.py`, `test/fixtures/release-coordination-checks.py`, `test/release-coordination.test.mjs`.
**Interfaces:** `ReleaseError.reason`; `coordinate_release(states, *, install_both, prepare_worker) -> list[dict]`, with nonempty `{project: [absolute canonical private state roots]}` and callbacks. Worker methods `ready()`, `go()`, `finish()->dict`, `close()`; `PreparedWorker(plugin_root, project, *, ready_seconds=30, run_seconds=1800)` owns one fresh interpreter child.
- [x] Draft fixtures for alllocks-before-install, busy preflight, partial/failing install, sequential release, FD noninheritance, unsafe/replaced identity, READY/GO, EOF-before-GO, owned timeout cleanup.
- [x] Guarded fixture base RED (helper absent), preserve output.
- [x] Implement cooperative locks using exact `bundle-auto-upgrade.lock`, canonical/private root and file UID/mode/nlink/devino checks. No SQLite or product model calls in coordination library.
- [x] Implement fresh `-I -B` worker handshake; after GO call the ordinary new installed bundle API and report results+actual bound pluginRoot. No guard monkeypatching or borrowed FD ownership claims.
- [x] Focused fixture passes, commit helper/test deliverable.

## Task2: Version and operator
**Files:** Seven existing manifests .32; local `/Users/dulee/work/memvane-handoff/context/` operator script and REPORT/audit. Public docs describe current developer API only.
- [ ] Version all7 consistently. Metadata bump has no new behavior; do not fabricate a baseline-RED test for the version literal.
- [ ] Operator discovers the9 selected state roots through current installed API, supplies four bounded supported installer commands, checks both7versions+sourcecommit, then uses new Claude workers. Scripts align after both installed, no reload during loop.
- [ ] Final focused includes naming/package/coordination; full suite on committed final tree; exact Git packaging. Baseline tree0e314a8 equals verified PR133 head014e437 (669/666pass/0fail/3skip and CI22/24/26 success), reuse that exact-tree evidence.

## Task3: Independent review and release gate
- [ ] PR exacthead detached Astrahigh readonly review including coordination/operator/fixtures, fix concrete findings with RED/focused/full and narrow re-review.
- [ ] Merge commit; verify main parity and protected3; install/upgrade one outer operation under global Lease. Stop/report actual race/failure, preserve private state and existing reader.
- [ ] Codex9 companion; full writeproof18; ordinary warm then original recall verifier18 with strictstderr/card checks; latestSE/rccarselected/no fallback; both7versions/newClaude service+embed/no_pycache; exacthead CI22/24/26 allsuccess.
- [ ] PR3 completion audit closes only when actual Claude path gate passes, then continue PR4–10 under unchanged active goal.

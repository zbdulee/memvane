# Remaining runtime resource scope — operating rollout held

The initial audit below is historical evidence. Its four source gaps are being
closed by [the authorized admission follow-up](2026-10-16-runtime-admission-remaining.md).
The current source behavior and residual real-host validation are recorded in
[runtime coordination](../runtime-resource-coordination.md#remaining-entry-points-follow-up).
None of these source changes have been installed or activated.

This is a source audit and small synthetic hardening follow-up, not an operating
safety certification. The existing 56-case result was retained; only changed
contracts and new cases are checked again. No models, provider requests, overload,
installation, activation, bulk backup, main change or push are authorized here.
Existing user sessions and inbox automation stay in place.

## Small gaps repaired

- Reports carry a monotonic observation start. Missing, future, nonfinite or
  older-than-two-second observations fail admission. OS probe duration counts
  toward this age; a slow/failed probe cannot become fresh by finishing late.
  Malformed pressure metadata defers. The child trainer's one-second cache is
  still subject to the same two-second limit.
- A private recovery latch under the existing control mutex is shared across
  sessions. After a resource refusal, background work needs at least 40% relative
  available memory and CPU load no greater than 65% of its configured limit for
  five observed seconds before resumption, with at most seven seconds between
  observations; a longer gap restarts recovery rather than counting idle hours
  as stable. This is sampled evidence, not continuous pressure measurement.
  A changed wall/monotonic relationship resets
  recovery conservatively after reboot/clock changes. No compute/trainer/writer
  lease is held while waiting. These thresholds are proposals, not measured safe
  operating limits. Foreground inference keeps its separate priority admission.
- The wake dispatcher holds at most 256 rows (including running); non-index rows
  leave one slot reserved for index work. Existing keys coalesce even when full.
  Targets are at most 4096 UTF-8 bytes. Overflow is recorded in one bounded private
  receipt and does not delete canonical content queues/history. SessionStart and
  learning launchers defer child creation on capacity refusal, wake existing
  hints and can register the retained job on the next normal hook. This cap does
  not prune historical captures or bound all canonical queue storage on disk.
- Manual wiki QMD publication and inactive shadow-index preparation now use the
  common command admission with their exact selected index environment preserved.
  Shadow preparation refusal leaves existing DB/pointers untouched. Incomplete
  inactive generation files may require later reviewed cleanup; none were built.

## Entry-point map and blockers

| Path | Current coverage | Remaining limit |
|---|---|---|
| query skill, recall/post-tool recall, QMD project query | shared foreground demand; bounded query deadline; singleton Laya admission | active QMD commands are not preemptible; backend contention can delay retrieval |
| update skill, SessionStart update/embed, sync/index retry, orphan cleanup | common compute admission, inherited lease, durable content ACK/wake | QMD command is indivisible; no measured QMD RSS ceiling |
| local learning CLI, runtime reuse smoke, isolated training, legacy auto-cycle | LocalTrainer admission; v2 per-group TrainingGuard; existing authorization | explicit raw adapter scripts outside supported skills/CLI are not intercepted |
| inactive index build/bootstrap/search proof and manual wiki sync | common guarded command runner added | actual builder/model execution remains untested |
| Laya singleton inference and model apply | existing singleton compute/admission; deadline and bounded queue (capacity 8) | real multi-session latency/memory validation held |
| QMD cold daemon start (`qmd_endpoint.spawn`) | endpoint identity, port and startup ownership guards | **not under the new common resource admission**; startup lifetime needs separate design, not a compute lease for the daemon's whole life |
| general/synthetic teacher and manual teacher transport | existing consent/quota/receipt/timeout and some OS/process checks; project/process serialization | **not one common UID-level resource lane**; external request uncertainty must not become an automatic paid retry; do not hold inference compute while awaiting a remote provider |
| wiki host extract/verify/dedup (`extractors/lib.py`, custom worker argv) | existing timeout/child cleanup, opt-in/queue controls | **not under the common resource lane**; needs pre-send deferral and existing queue/cooldown integration |
| optional vector warm (`keepalive.sh`) | health check, opt-in, curl deadline | **no common admission or proof that server work stopped at client timeout**; source default is off, operating enablement was not inspected here |
| model identity/cohort/fresh inventory preparation | existing scope and bounded-cohort checks | some CPU/file hashing occurs **before** compute admission, e.g. automatic_staging model_identity and project_worker fresh_inventory; not a complete aggregate CPU limit |

Consequently, “all plugin-owned costly entry points share admission” is still
false. The bold gaps and pre-admission preparation need resolution or an explicit
reviewed scope exclusion before claiming operating readiness. Merely wrapping an
outer orchestrator in compute.lock would recurse into existing child guards or
block retrieval while waiting on a remote provider; that is not a small safe fix.

Nested compute requests currently fail immediately rather than deadlock. The
control/compute/trainer order and finally cleanup were retained. The new general
command runner's route preservation, exclusion and exception release are tested
without launching a real QMD/model process. Existing kernel-lease and revision
ACK tests remain relevant evidence for duplicate-consumer protection; they do
not certify every older installed plugin copy.

## Exact explanation for ordinary-project auto learning

“검색과 편집은 계속 사용할 수 있습니다. 학습은 여유가 부족하면 진행 위치를 저장해
대기하며, 실행 중인 재시도 작업이 최대 60초 동안 상태를 다시 확인합니다. 이 시간 안에
자원이 안정적으로 회복되면 이어서 진행합니다. 재시도 작업이 끝났거나 컴퓨터를 재시작한
뒤에는 다음 정상적인 플러그인 hook이 발생할 때 대기 작업을 다시 확인합니다. 앱을 닫고
아무 작업도 하지 않는 동안에는 자동 재개되지 않습니다. 권한·주간 잔여량·설치 상태가
허용하지 않으면 학습은 계속 보류됩니다.”

The loop also stops after eight dispatches, and an already admitted atomic
command may outlast its 60-second dispatch budget. Queue backoff can extend to
300 seconds; expiry does not remove work. The wake mechanism does not restart a
teacher request or guarantee an OS boot-time task. This behavior requires the
current code to be installed later; installed rc46 was not updated by this audit.

## Minimum steps before operating use and expected cost

1. Resolve the entry-point blockers above in source with small fake-signal and
   pre-send/lifecycle tests; review sequentially. Keep real model work held.
2. Review and snapshot the complete dirty feature branch, including its earlier
   general-learning changes. Existing main/old CI results do not validate this
   dirty tree; focused CI/full regression is a separate, still-held gate.
3. Once explicitly authorized, run one sequential normal-host synthetic pilot
   using already present models and a separate tiny index: one foreground query,
   one update/embed, no remote teacher/training, no operating pointer switch.
   Record system pressure/swap and the whole owned process tree CPU/RSS before,
   during and after. A fake-pressure pass does not replace this measurement.
4. Confirm real hook wake/retention with two sessions and a bounded waiter, then
   check queue recovery after a clean worker restart. Do not restart user sessions
   or deliberately migrate/roll back operating data for this check.

Source/fake checks need one Python test process and at most one tiny Python child
at a time; the current affected batches finish in fractions of a second. RSS was
not measured for these checks. The runtime waiter adds one model-free process,
five-second polling and at most 60 seconds of idle dispatch-loop lifetime; its
RSS is expected to be tens of MiB, an estimate only. A real QMD/Laya cold load can
require GiB of memory; no trustworthy peak or safe CPU percentage is established
here. Compute the later pilot budget from the installed model inventory and
approved memory profile first. The 512 MiB background admission estimate is not
an enforced QMD RSS cap, and single compute admission is not an OS hard CPU cap.

Repository state observed at the audit start: feature branch
`feat/general-project-auto-learning`, HEAD `a3cb36c` (approved plan commit).
Runtime/general-learning changes remain uncommitted; main and remotes unchanged.

## Verification and final file status

Added hardening checks: 10 passed, final run 0.052 seconds. The affected existing
runtime 25 and coordination 17 checks passed sequentially (0.198s and 0.113s);
their broader earlier results and the 14 analytical-memory cases were reused.
The final gap correction was checked in the 10-case hardening batch, without
repeating unrelated tests. Eight affected Python sources/package JSON parsed;
the Node full-suite wrapper and auto-staging fixture were not executed here.
Independent read-only review found and resolved the observation-gap P2. Final
review reported no remaining P1/P2 within the new changes, conditional on the
documented operating blockers and hold. No fresh real-world safety claim follows.

Files changed in this follow-up (all uncommitted, on the feature branch):

- Admission/state and launch code: `core/context_learning/resource_coordination.py`,
  `core/context_learning/shared.py`, `core/runtime_work.py`,
  `core/update_hook_queue.py`, `core/context_learning_auto.py`.
- Manual/staging QMD commands: `core/wiki_topical_publish.py`, `core/runtime_update.py`.
- Checks/entry points: `test/runtime_hardening_test.py`,
  `test/runtime_resources_test.py`, `test/fixtures/context-learning-coordination-checks.py`,
  `test/fixtures/context-learning-auto-staging-checks.py`,
  `test/runtime-resources.test.mjs`, `package.json`.
- Documentation: this file and `docs/runtime-resource-coordination.md`.

Other earlier dirty general-learning/development-guard changes were preserved.
There was no commit, main modification, push, install or activation in this audit.

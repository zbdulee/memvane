# Release agent QA gate

Every version release must pass this agent-executed QA gate on the **final
installed candidate**. The gate does not rerun unit tests. It is a set of
reproducible scenarios that an operator agent performs in fresh, real Claude
Code and Codex host sessions, against disposable synthetic fixtures. Each
scenario captures objective evidence. Record each run with
[the run record template](templates/release-agent-qa-run.md). The
[release checklist](release.md) links here.

## Status of this document

This spec was written on 2026-10-17 on the local, unpushed branch
`feat/same-session-worktree-freshness`. The synthetic fixture helper passed
its L0 guarded test, which ran Git only inside disposable test directories.
No installed-candidate host scenario, QMD, model, hook or installer was run.
The host scenarios are `NOT RUN`, apart from the documented `BLOCKED` race
barrier and learning conditions. This paragraph does not record L1 QA.

2026-10-18 Laya v4 추가 기록: 임시 Git 후보를 복사한 **L0 개발 fixture**에서는
제품 hook의 차단, 별도 개발 전용 합성 벡터의 edit→sync→query 및 rollback
거부를 확인했다(가드 `1e2bdde46fe94d1eb62449a3ed998bc9`, Node 1·Python 2,
`passed/settled:true`). 정식 marketplace 설치나 새 실제 host 세션은 아니므로
아래 L1 행은 모두 `NOT RUN`이다. 이 fixture에서는 모델 추론을 실행하지 않았다.
별도 제한된 임베딩 기능 smoke는 가드
`af90592d823d482ba8318de9016a010b`가 `passed/settled`로 끝났고 합성 입력
2회, 결과 차원 768을 관측했다. backend는 Metal로 표시됐지만
`gpuLayers=0`이므로 모델 GPU offload 증거는 없다. 5초 간격 샘플링으로 peak
RSS도 측정되지 않았다. 이 기능 smoke는 제품 provider 승인이나 L1 설치 검증이
아니다. 원시 실행 로그가 별도 보존됐다는 주장은 하지 않는다.

### Laya Index 전환 후보 추가 필수 행 (2026-10-18, 전부 NOT RUN)

**통합 작업 체크포인트 (L0, 설치 후보 아님):** 제품 승인 항목의 자기참조를
제거하고, exact Git tree 복사본과 설치 소유 journal에 외부 provider attestation
digest를 묶었다. 선택된 hook의 질의는 예열된 소유 worker를 사용하고 update/index는
durable sync wake를 남긴다. QMD 원본·백업을 보존하는 cutover/복구 상태도
합성 fixture에 연결했다. 집중 회귀 14/14는 guard
`36d1092013a04de59b034f787a3cd2f9`에서 통과했다. 전체 회귀의 첫 실행은
시스템 Python의 PyYAML 부재로 실패했고, 같은 Python에 기존 테스트 venv의
PyYAML import 경로만 더한 집중 재검사는 47/47 통과했다(guard
`dc86865c203243f98c49a35f6c2dac2e`). 아래 모든 L1 행은 계속 `NOT RUN`이다.
후속 제목 결속 v5는 동일 본문·다른 QMD 제목을 별도 벡터로 보관하고,
완전한 청크 집합을 원자 봉인해 재시도에서 재사용한다. 검증된 집합 밖의 청크와
불연속 sequence는 세대 seal이 거부한다. QMD RRF의 첫 유효 목록 가중치·상위
순위 보너스·`1/rank` 점수 의미도 반영했다. 통합 집중 검사는 8/8 통과했다
(guard `1af84e0c7e56489fa24a1429c88d62b7`). 독립 Opus v5 리뷰는
`claude-opus-5-5`를 사용했고 실제 `modelUsage`는 output 8,545 token,
cache read 1,960 token, cache create 23,131 token이었다(guard
`b414d9b1defa4071af9f67921ea2a14b`, passed/settled). 코드·합성 자료만
전달했고 모델 추론/다운로드는 하지 않았다.
최종 모델 제외 전체 회귀는 기존 venv의 PyYAML import 경로만 시스템 Python에
더해 실행했고 guard `d372c3eda9844b3ba3f516f7e34e5e62`에서
`passed/settled:true`, exit 0으로 끝났다. 전환 fixture의 v5 제목·청크 집합
계약 누락은 각각 집중 가드 `1cb5df21e4ee45db8193faab97a0f21f`와
`b78ecfa76cb64a3cb4143bb4d3b8f11a`에서 수정 후 통과했다.

후속 L0 통합에서는 두 QMD 컬렉션(raw·wiki)의 read-only inventory·백업·후보
봉인·전환 복구, selected wiki compile, QMD 형식 FTS 및 계층 recall, 짧은 소유
상태 경로의 설치·worker 준비를 합성 fixture로 연결했다. 모델/native closure 검토
자료 생성기도 구현했지만 실제 설치·선택·모델 실행 증거를 대신하지 않는다. 전체
회귀는 1,621건 중 1,612 PASS·9 SKIP·0 FAIL, guard
`95a30e34baed4d6abe090ad0f52bd06f` `passed/settled:true`였다. 이 결과는
L0 소스 검사이며 최종 후보의 L1 행은 여전히 모두 `NOT RUN`이다.
QMD 전환 복구만 대상으로 한 Opus 소스 리뷰는 guard
`ead1e6624d634cadbed8b47fcafbc4ec`에서 `passed/settled:true`였고
`claude-opus-5-5`의 실제 `modelUsage`는 output 6,614, cache read 1,960,
cache create 4,779 token이었다. 설정 변경 뒤 switching 고착, pending route digest
재확인 등 지적을 합성 사례로 수정했고 전환·설치 집중 3/3은 guard
`9be9f9657a674b5aa6402e8c66fc487a`에서 통과했다. 실제 QMD 데이터와
모델은 이 리뷰에 제공하지 않았으며 L1 상태를 바꾸지 않는다.

Laya Index로 QMD backend를 교체하는 릴리스에는 기존 L1 행에 더해 **설치된 정확한 후보**에서 다음을 필수로 기록한다. 임시 Git full-plugin 복사본과 합성 attestation·이관 리허설은 marketplace 설치 후보의 fresh-host 증거가 아니다. 실제 설치 후보의 provider 승인과 선택은 아직 수행되지 않았다. 아래 행은 모두 `NOT RUN`이며 L0 통과로 대체할 수 없다.

| 행 | 설치 후보에서 확인할 실제 결과 |
|---|---|
| LI-INST | exact-commit bundle·설치 소유 journal·현재 파일 identity·DB/queue incarnation을 새 Claude/Codex 세션이 검증하고, 일치하지 않으면 QMD로 fallback하지 않고 차단한다. |
| LI-MIG | 선택된 프로젝트의 QMD 설정·원문·위키·색인 범위를 read-only inventory로 고정하고, 일관된 백업을 별도 DB로 복원해 비교한 뒤 새 후보를 재색인·검증한다. 불일치/중단 시 이전 선택이 유지된다. |
| LI-SCOPE | opt-out, worktree 분리, 삭제·rename, 중복 claim, 오래된 writer, 옛 pin 철회를 새 host 경로에서 확인한다. 다른 scope의 후보·순위·결과가 섞이지 않고 갱신 불가 시 명시 오류를 낸다. |
| LI-RECOVER | 게시/ACK 및 전환 각 중단점의 재시작, 첫 새 쓰기 전 rollback, 첫 새 쓰기 뒤 자동 rollback 차단과 전진 복구를 확인한다. 실제 전원 손실은 별도의 시험 없이는 주장하지 않는다. |
| LI-COEXIST | 별도 global QMD를 그대로 보존하고, 플러그인 소유가 증명된 QMD runtime만 복원 가능한 격리 후보로 분류한다. 불확실한 소유권은 보존하고 영구 삭제는 별도 결정한다. |
| LI-VECTOR | 선택된 모델 파일·Node/native dependency closure·전처리/청킹 recipe의 출처와 hash, 문서·질의 차원과 profile, 재색인 전량 seal, scope/세대 필터 선행 및 과예산 무결과를 증명한다. 모델 실행에는 별도 승인된 자원 gate를 사용한다. |
| LI-RECALL | 기존 QMD `rerank:false` 어휘 변형+벡터 결합, 정확한 식별자/EP, 위키/원문 분기, lex probe, minScore/topN, 결과 포맷을 설치 후보의 새 Claude/Codex 세션에서 비교한다. 벡터 단독 합성 검색은 이 행을 통과시키지 않는다. |

모델/벡터 재생성이 필요한 경우 provenance 또는 명시 승인·자원/비용 gate를 함께 기록한다. 필수 행의 `NOT RUN`/`BLOCKED`/`FAIL`은 전환 릴리스를 막는다.

현재 코드의 직접 blocker와 누락된 설치 후보 QA (L0 상태는 제품 합격 판정이
아니다):

| 행 | 현재 경계와 필요한 증거 |
|---|---|
| LI-INST/LI-VECTOR | 자기참조 없는 attestation, 실제 설치 cache의 0755 파일 검증, 소유 journal·private socket, 모델/native closure 검토 자료 생성은 합성 구현됐다. 정확한 최종 commit을 실제 marketplace가 로드한 위치에서 검토·승인하고, 선택된 모델의 tokenizer/청킹·자원 동작을 확인해야 한다. 기존 2회 기능 smoke는 소진됐으며 이 행의 L1 증거가 아니다. |
| LI-MIG/LI-RECOVER | raw+wiki 두 컬렉션의 inventory, 백업·복원 비교, 후보 봉인, route/journal 중단점 복구를 합성 구현했다. 실제 QMD 소유 데이터의 read-only 범위·일관성 검토와 설치 후보에서 변경·중단점 재시작·전진 복구가 필요하다. 원본과 백업은 보존한다. |
| LI-RECALL | 선택된 hook의 warm provider, QMD 형식 FTS, 위키 계층·EP·DF 정책은 소스 및 합성 fixture에 연결됐다. 희귀 CJK Script 경계와 반복 DF 탐색의 시간 제한·부분 결과 폐기는 합성으로 확인했다. 실제 최대 규모 corpus의 훅 지연과 같은 corpus의 fresh Claude/Codex 검색 결과는 측정하지 않았다. |
| RQA-RES-02 | durable learning queue의 중복 방지·재시작·부하 대기/재시도는 합성 검사했다. 실제 설치 후보에서 OS 압력 증가 시 중지·회복 후 재개는 별도 안전 범위의 실제 head 학습 검증이 필요하다. |


독립 Opus QA 후속 확인 대상 (2026-10-18, 제품 출시 전 해결 또는 실측 필요):

| 코드 경계 | 필요한 수정·검사 |
|---|---|
| `core/laya_product_bundle.py`의 production approval | 자기참조는 installer-owned attestation으로 제거했다. 이제 실제 host가 로드한 exact commit의 파일·모델·native closure를 검토해 journal에 봉인하고, 설치 뒤 source와 바이트가 같은지 확인해야 한다. |
| `core/laya_qmd_migration.py`의 vector candidate | 제품 cutover는 boolean 승인 대신 journal-bound provider/profile을 검사한다. 단일 raw 범위만 지원하므로 원문·위키 별도 collection을 하나의 세대에 봉인하고 동일 tokenizer/청킹 결과를 확인해야 한다. |
| `core/laya_host_response.py`의 selected route | 선택된 recall은 warm provider를 호출하고 update/index는 durable wake로 보낸다. 최종 설치 후보의 승인된 provider, 실제 fresh Claude/Codex 세션, QMD 대비 검색 정책을 검증해야 한다. |

Laya 검색·임베딩의 운영 부하 큐 **확장**은 v2 오픈 이후 선택 검토 항목이다.
학습 작업의 운영 부하 큐와 admission·중지/재개 검증은 아래 `RQA-RES-02`의
필수 조건이다. 이 구분은 검색·임베딩 기능 자체를 제품 범위에서 제외하지
않는다.

## 1. Source verification and installed-candidate QA are separate

| Level | What runs | Can it satisfy this gate? |
|---|---|---|
| L0 source verification | `npm test`, focused tests, synthetic fixtures, direct `core/*.py` calls in the source checkout | **No.** L0 is a prerequisite only (see [release.md](release.md)). |
| L1 installed-candidate QA (this gate) | Host marketplace install of the exact candidate, then fresh host sessions in which the real hooks fire, with natural-language requests and exposed skills | Yes. This is the only level that counts. |

Hook-path evidence cannot be replaced by a direct script call. A cached plugin
directory can carry the same version string while containing different code;
[the hook-path E2E plan](plans/2026-07-30-hook-path-e2e-test-plan.md) records
such a case. Identity is therefore proved by content hashes (section 5), not by
version strings.

Operator rules:

- Product interaction uses natural-language requests or the exposed skills
  (`setup`, `query`, `sync`, `update`, `enable-compile`, `wiki-compile`). The
  operator must not run `core/*`, `core/update.sh`, `skills/*/scripts/*` or
  backend scripts directly to make a scenario progress. Doing so invalidates
  that scenario's run. The scenario must restart from its preconditions.
- Outside the product, the operator may do only three kinds of work: actions
  that simulate the user (the fixture helper's Git actions, host marketplace
  install/update, host hook trust), reading evidence, and computing hashes.
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
- Some expected outcomes below are policy goals that the
  [architecture boundaries](plans/2026-10-17-architecture-boundaries.md) and the
  [worktree policy](git-worktree-search-sync-policy.md) still list as
  unimplemented. Examples: query-time freshness, single-generation reads, and
  wiki revision idempotency. The gate keeps those outcomes as the expected
  result. Until they are implemented, they will `FAIL` and block a formal
  release. The gate must not be weakened to describe the current gap as correct.

## 3. QA environment and isolation

- **QA host account.** Use a dedicated OS user account, or a disposable VM or
  snapshot, on supported hardware. It must have no personal projects,
  manuscripts or operating databases. An isolated `HOME` is not enough: the
  runtime wake queue and the coordination and development locks are rooted at
  the OS UID home (see
  [runtime resource coordination](runtime-resource-coordination.md) and
  [development resource safety](development-resource-safety.md)). Never run
  the gate in a maintainer's daily account.
- **Clean state.** A clean state has no qmd-auto-context plugin in either host,
  no plugin runtime roots or shared store, no `~/.config/qmd` plugin state, no
  previous fixtures, and no global Git `core.hooksPath`. Record how the state
  was reset: new account, snapshot restore, or documented removal.
- **Approvals recorded before running:**
  - QA host use and model-usage cost cap for host sessions.
  - Package execution and lifecycle scripts, if the managed QMD install path is used.
  - Wiki extractor/verifier cost cap (RQA-WIKI-*).
  - Learning approval with safe test resources (RQA-RES-02 only).
- **Data limits.** Real learning, manuscripts and operating DB writes are out
  of scope unless separately approved. Without that approval, the dependent
  scenarios are `BLOCKED`.
- **Evidence directory.** Use
  `EVID=/tmp/qmd-release-qa-evidence-<runId>`, mode 0700, outside every
  fixture, so fixture cleanup cannot delete evidence. Launch each fresh host
  session with its own `QMD_RECALL_LOG="$EVID/recall-<host>-<run-session-id>.log"`.
  This setting writes diagnostics
  to a file only; it never writes to model context. Do not set test-only
  variables such as `QMD_SKIP_BACKGROUND_WORKER`, `QMD_FORCE_DEAD_PIDS`,
  `QMD_QUERY_FIXTURE` or `QMD_FAKE_PLATFORMS`. If any is present, the run is
  invalid.
- **Fresh session.** A host process started after the candidate install and
  host reload completed, with its working directory equal to the scenario's
  checkout. Sessions that predate the install are "old sessions" and appear
  only in RQA-INST-02.

## 4. Supported OS and host matrix

| Cell | Platform | Product support | Gate requirement |
|---|---|---|---|
| T1 | macOS arm64, current maintainer macOS release (record exact build) | Managed QMD + v2 routing | **Mandatory:** all scenarios for Claude Code and Codex |
| T1-node | Same as T1, second Node LTS (22 vs 24) | Managed QMD accepts both | **Mandatory:** RQA-INST-01 + core smoke |
| T1-py | Same as T1, hooks run by the system `python3` (macOS ships 3.9) | Hook floor is Python 3.9 | **Mandatory:** core smoke with the host-resolved `python3`; record its version |
| N26 | T1 with Node 26 Current | Native compatibility probe still pending (README) | Optional; release notes must say "unverified" unless RQA-INST-01 + core smoke PASS |
| U-intel | macOS x86_64 | `setup_required`; existing global QMD preserved | RQA-PLAT-01 if hardware exists; otherwise `NOT RUN` + release note |
| U-linux | Linux x86_64/arm64 | Managed v2 unsupported; existing global hook path only | RQA-PLAT-01 recommended; otherwise `NOT RUN` + release note |
| U-wsl / U-win | WSL guest / native Windows | Unverified / unsupported | Not claimed; no gate cell |
| H-hermes | Hermes Agent on T1 | README lists it as supported | Core smoke equivalent recommended; if not run, release notes must say Hermes was not verified in this release |

Rules for unverified OS/host combinations:

- A combination without a `PASS` record must not be described as supported or
  verified for this release. Release notes keep the explicit wording
  "not verified in this release".
- Results from one cell are never copied to another cell. Each cell's record
  carries its own host, OS build and artifact identity.
- An unsupported platform can pass only the negative-path scenario
  RQA-PLAT-01. That scenario checks preservation and `setup_required`; it does
  not check functionality.

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
  under `core/`, `hooks/`, `skills/`, `backend/` or `hermes_adapter/`. A
  symlink difference is shown in the diff and must be explained.
- Also record the host-reported plugin version and commit. For Claude Code,
  this is the installed-plugin registry entry; for Codex, the plugin list.
- Version consistency: one version string across `package.json`, `plugin.json`,
  `plugin.yaml`, `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`,
  `.claude-plugin/marketplace.json`, `.agents/plugins/marketplace.json` and the
  `test/probe-manifest.test.mjs` assertions.
- Runtime identity, reported by the `setup` skill status request: QMD version
  and entry, Node, embedding model file SHA-256, Laya state (`deferred` or a
  reviewed identity), and selected store generation.

## 6. Evidence and search-result proof

Probe prompt (one turn; replace `<question>`):

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
per-fixture id, so a model cannot guess them, and an index from an earlier
fixture cannot satisfy them.

- **Present(T)** requires all of the following:
  1. The `qmd_recall_selection` line for that turn has `reason: "selected"`,
     its `project_root` equals the scenario checkout, and `selected[]` includes
     the file that currently contains T.
  2. The host transcript excerpt of the injected context contains T.
  3. The probe answer contains T.
- **Absent(T)** requires all of the following:
  1. No selected path whose current or previous content holds T.
  2. T is not in the injected context.
  3. The answer does not contain T.
- An answer containing T is never sufficient on its own.
- A missing log line means the hook did not run. Treat it as `FAIL` unless a
  sandbox or headless host is documented.
- For each fresh host session, use a unique QA run/session marker and a fresh
  `QMD_RECALL_LOG` file under `$EVID`. Capture the host's loaded plugin path,
  hook command path and session/turn identifier alongside the log file's
  before/after size and timestamps. Correlate the selected line to that host
  turn. A direct CLI query or a line from an earlier session never proves the
  installed host hook ran. If the host does not expose a hook command trace,
  record that limitation and use its official plugin registry, isolated log
  path and exact turn timing together; an ambiguous match is `BLOCKED`.
- The optional query skill check ("`<question>` 질문에 어떤 문서가 recall되는지
  확인해줘") is secondary evidence. It does not replace the hook path.
- Per probe, record: wall-clock time, host session id, cwd, branch,
  `git status --porcelain` summary, the recall log line, a transcript excerpt
  and the verdict.
- Each convergence probe must show at least one positive current token and
  the relevant negative old token together. An empty result can be a safe
  intermediate state during a switch, but never satisfies a final searchable
  state or a convergence deadline.
- Store transcripts privately. Release notes get summaries and hashes only.

## 7. Timeouts

| Name | Value | Applies to |
|---|---|---|
| Host hook timeouts | `hooks/hooks.json` / `hooks-codex.json`: SessionStart 5 s and 3 s, recall 22 s, gate 3 s, PostToolUse 22/5/5/5 s, Bash 3 s, Stop 3 s | Any host-reported hook timeout or failure is `FAIL` |
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
  runtime state, and one whose ancestors hold auto-context settings (upward
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

- **Purpose:** check first install, the unconfigured-project gate, opt-in,
  managed setup and the first injection on a clean machine.
- **Owner:** install, with search for the final probe. **Hosts:** Claude Code
  and Codex, each starting from clean state.
- **Preconditions:**
  - Clean QA state.
  - A supported Node LTS.
  - A reviewed local embedding GGUF; record its SHA-256. Setup does not
    download weights.
  - Package-execution approval, if the managed QMD install path is used.
  - Fixture created.
- **Steps:**
  1. Install the candidate through the host marketplace. On Codex, review and
     trust the hooks in the official UI.
  2. Run RQA-ID-00 for this host.
  3. Start a fresh session in `project/`. Record SessionStart output: at most
     one short setup notice, and no hook failure.
  4. Ask "README.md 끝에 한 줄 추가해줘". Expected: the pre-edit gate stops
     the edit and offers choices; `README.md` is unchanged (record
     `git status`). Decline the edit.
  5. Ask "이 프로젝트 auto-context 설치 상태 보여줘". Expected: a read-only
     report; `git status` and the `.auto-context/` absence are unchanged.
  6. Ask "이 프로젝트에서 auto-context 켜줘. 문서 범위는 docs/qa만." Expected:
     `.auto-context/settings.json` names only the `docs/qa` collection.
  7. Ask the `setup` skill for a plan ("설치 계획 보여줘"), review its roots,
     input hashes and indexing impact, then approve ("검토한 설치를
     진행해줘"). Grant only the approvals that are already recorded.
  8. Record the result: `qmdReady`, `layaReady`, `overallReady`,
     `componentReadiness`, `completionScope`.
  9. Reload the host and start a fresh session. Probe Q-alpha.
- **Expected:**
  - `qmdReady: true`, with the managed binary version, index path and
    embedding model file hash recorded separately from search proof.
    `layaReady` is `deferred` unless the release advertises learning; in that
    case `overallReady` and a separately reviewed Laya identity are required.
    A QMD bootstrap PASS never implies Laya setup, training or model-use PASS.
  - Nothing outside the fixture, the QA account's plugin runtime roots and the
    host plugin directories is written.
  - Present(`ALPHA_MAIN`) within T_FRESH. No hook timeout or failure.
- **FAIL if:**
  - The gate does not stop the edit.
  - Install reports `completed` without scoped-search proof.
  - The probe stays absent at T_FRESH.
  - Any hook fails.
- **Evidence:** setup outputs, settings file with its hash, `git status`
  snapshots, recall log lines, probe transcript excerpt.
- **Timeout:** T_INSTALL for steps 7–8; T_SCENARIO overall.
- **Cleanup/recovery:**
  - A `deferred` (75) result allows one identical retry.
  - `recovery_required`: stop, preserve every journal and pointer, record
    `FAIL`, and open an issue.
  - Keep the install for later scenarios, or restore the clean state before
    the next host.

### RQA-INST-02 Update from the previous release

- **Purpose:** prove that a real update from release N-1 to the candidate
  preserves data and old sessions, and ends routed to the candidate.
- **Owner:** install. **Hosts:** Claude Code, Codex.
- **Preconditions:**
  - Clean state.
  - The previous public release (N-1) is installed from the public
    marketplace, with its identity recorded per section 5.
  - The fixture was opted in and indexed under N-1, and Q-alpha is Present
    under N-1.
  - The RQA-INST-03 preparation steps are done.
- **Steps:**
  1. Keep one N-1 session open (the "old session").
  2. Update the plugin to the candidate through the host's update flow.
     Reload as the host requires.
  3. In the old session, probe Q-alpha. Record the behavior: old code may
     still answer. Expected: no crash, no hook failure, no corruption.
  4. Start a fresh session. Expected: at most one setup or update notice.
  5. Ask "auto-context 업데이트 확인해줘" (`setup` skill `check-update`;
     read-only). If it reports work, approve plan, prepare and apply in
     separate requests.
  6. Run RQA-ID-00 against the candidate.
  7. Start a fresh session and probe Q-alpha.
- **Expected:**
  - The installed identity equals the candidate.
  - The check-update report is read-only.
  - Present(`ALPHA_MAIN`).
  - Settings, wiki and index preserved (RQA-INST-03).
  - The old session caused no damage.
- **Evidence:** both identities, notices, check-update and apply outputs,
  probes.
- **Timeout:** 60 min.
- **Cleanup/recovery:** same as RQA-INST-01. The state carries over to
  RQA-INST-03 and RQA-INST-04.

### RQA-INST-03 Settings preservation

- **Purpose:** an update or migration must never drop or rewrite user
  decisions.
- **Owner:** install. Settings are owned by the project and user.
- **Hosts:** Claude Code, Codex.
- **Preconditions:** under N-1, before the update in RQA-INST-02:
  1. Ask "auto-context 설정에서 injectSourcePathsPerCard를 2로, queryTimeout을
     4로 바꿔줘".
  2. Confirm `compile.mode` is explicitly `"off"`.
  3. Create a second fixture B in the same session flow and ask "이
     프로젝트에서는 auto-context 꺼줘" there.
  4. Record the settings bytes, SHA-256 and canonical JSON for A, the
     absence of `.auto-context/` in B, and the wiki file hashes for A.
  5. Record N-1's on-disk format/version markers and representative index,
     wiki and decision-store identities before update. Use actual N-1 output,
     not a hand-written imitation of the old format.
- **Steps:**
  1. After RQA-INST-02 completes, re-read A's settings and wiki hashes.
  2. Start a fresh session in B and probe Q-alpha.
- **Expected:**
  - A's settings are byte-identical, or differ only by a migration that the
    release notes document and that keeps every user value.
  - A's wiki bytes are unchanged.
  - Every prior-format index and decision-store record is either readable by
    the candidate or migrated through a documented path with no lost user
    decision or source identity. Record the before/after format and any
    migration receipt. A fresh install alone cannot satisfy this check.
  - In B the decline still holds: the recall log shows `no_collections`,
    nothing is injected, and no `.auto-context/` is created.
- **Evidence:** before/after hashes and diff, recall log lines for B.
- **Timeout:** 20 min.
- **Cleanup:** clean up fixture B.

### RQA-INST-04 Rollback and recovery

- **Purpose:** check that an applied update can be rolled back without
  losing data, and that an interrupted setup never reports false success.
- **Owner:** install. **Hosts:** Claude Code, Codex.
- **Preconditions:** RQA-INST-02 state. Record a hash snapshot of settings,
  wiki and the `setup` skill's status report before the apply.

**Part A: rollback.**

1. Ask "직전 적용을 되돌려줘".
2. Expected: the skill reports pointer and settings restoration, or names
   the concurrent change that blocks it.
3. Start a fresh session and probe Q-alpha. Expected: Present, served by the
   restored selection.
4. Settings and wiki hashes equal the pre-apply snapshot. The original DB,
   wiki and models are not deleted.
5. Ask "준비된 설치를 적용해줘" again. Expected: idempotent re-apply, then
   Present again in a fresh session.
6. Host-level downgrade: if the host can pin N-1, reinstall N-1 and open a
   fresh session. Expected: no hook failure, and either a working state or
   one setup notice. If the host cannot pin a version, mark this step
   `NOT RUN` with the reason.

**Part B: interrupted recovery.**

1. Use a fresh fixture C and opt it in.
2. Start the setup install ("검토한 설치를 진행해줘").
3. As soon as the skill reports that preparation started, terminate the host
   process. Do not signal plugin children individually.
4. Start a fresh session and ask "이 프로젝트 auto-context 설치 상태 보여줘".
   Expected: an in-progress, resumable or `recovery_required` state. Never
   `completed`.
5. Repeat the identical request. Expected: it resumes to completion, or
   stays `recovery_required` with a named reason. Never a silent partial
   success.

- **Evidence:** status outputs before and after, hash comparisons, probes.
- **Timeout:** T_ROLLBACK per request; 60 min overall.
- **Cleanup/recovery:**
  - Never delete a journal or pointer to make progress.
  - A persistent `recovery_required` is `FAIL`. Preserve the state for
    diagnosis, then restore the clean QA state.

### RQA-HOOK-01 Claude Code real hooks and injected search

- **Purpose:** every bundled Claude hook fires on the installed candidate,
  and recall injects real search results.
- **Owner:** search; file sync (edit indexing); runtime.
- **Host:** Claude Code.
- **Preconditions:**
  - RQA-INST-01 is `PASS` for Claude on this candidate.
  - The fixture is opted in and in S-main.
- **Steps:**
  1. Start a fresh session in `project/`. Expected: both SessionStart hooks
     run without failure.
  2. Probe Q-alpha. Expected: Present(`ALPHA_MAIN`), with
     `reason: "selected"` and selected `docs/qa/alpha.md`.
  3. Ask "docs/qa/agent-note.md 파일을 만들고 내용은 'The agent note marker is
     <AGENT_NOTE>.' 한 줄로 해줘". Expected: the gate allows the edit;
     posttool, index, compile and topical-event run without failure.
  4. Probe Q-agent until T_FRESH. Expected: Present(`AGENT_NOTE`).
  5. Ask "Bash로 docs/qa/agent-note.md를 삭제해줘". Expected: the Bash
     file-change-event hook runs.
  6. Probe Q-agent until T_FRESH. Expected: Absent(`AGENT_NOTE`).
  7. End the session normally. Expected: the Stop hook runs without failure.
- **Evidence:** transcript excerpts, recall log lines, and the host's hook
  failure/timeout output (none). Host debug output is optional.
- **Timeout:** T_SCENARIO.
- **Cleanup:** the fixture stays for later scenarios, or is cleaned up.

### RQA-HOOK-02 Codex real hooks and injected search

- **Purpose:** same as RQA-HOOK-01 for the Codex hook map
  (`hooks-codex.json`).
- **Owner:** search, file sync, runtime.
- **Host:** Codex.
- **Preconditions:** RQA-INST-01 is `PASS` for Codex; hooks are trusted in
  the official UI.
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
  opted in and in S-main; compile is off; no session is open.
- **Steps:**
  1. In a fresh session, probe Q-beta, Q-gamma and Q-delta. Expected: S-main.
     Close the session.
  2. Record a wiki-state listing (`.auto-context/compile`, `.auto-context/wiki`).
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
- **Preconditions:** a fresh fixture, opted in and indexed, S-main confirmed in
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
  hook, recall log lines, helper `show` with the branch.
- **Timeout:** T_SCENARIO.
- **Cleanup:** clean up the fixture.

### RQA-GIT-02 New worktree and actual cwd switch isolation

- **Purpose:** a new worktree has its own search view, and neither the main
  checkout nor the worktree leaks into the other.
- **Owner:** file sync, search; install for enrollment.
- **Hosts:** Claude Code, Codex.
- **Preconditions:** a fresh fixture opted in and indexed in `project/`
  (S-main); `stage worktree-add` done. The worktree branch predates opt-in,
  so the worktree has no settings.
- **Steps:**
  1. Start a fresh host process whose actual working directory is
     `wt-worktree/` (start it from that directory; do not pass a path
     argument). Probe Q-alpha and Q-worktree. Expected: no main-checkout
     content is injected. Absent `ALPHA_MAIN`. The unconfigured worktree is
     not indexed.
  2. In that session, ask "이 프로젝트에서 auto-context 켜줘. 문서 범위는
     docs/qa만." If the `setup` skill asks for enrollment of the new root,
     review it and approve it.
  3. Start a fresh session in `wt-worktree/` and probe at the T_FRESH
     schedule. Expected: S-worktree. In particular, the same relative path
     `docs/qa/alpha.md` yields `ALPHA_WORKTREE`, never `ALPHA_MAIN`.
  4. Start a fresh session in `project/`. Probe Q-worktree and Q-alpha.
     Expected: S-main-after-worktree, including the opposite result for that
     same `alpha.md` path.
- **Evidence:** cwd and `git rev-parse --show-toplevel` per session, recall
  `project_root` per line, probes, setup outputs.
- **Timeout:** T_SCENARIO.
- **Cleanup:** clean up the fixture; this removes the worktree too.

### RQA-GIT-03 Dirty and untracked files

- **Purpose:** uncommitted changes to tracked files and new untracked files
  inside an approved collection count as the current worktree's content.
  Reverting them removes them.
- **Owner:** file sync, search.
- **Hosts:** Claude Code, Codex.
- **Preconditions:** a fresh fixture opted in and indexed (S-main); no open
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
- **Preconditions:** a fresh fixture, opted in and indexed, S-main.

**Part A: mid-switch query.**

1. Ask "git switch qa/feature 실행해줘".
2. Submit Q-alpha as the very next prompt, within 5 s of the tool result.
3. Expected: `ALPHA_MAIN` is not injected as current. Either S-feature
   content, an empty result, or a marked stale result.

**Part B: late worker flip-flop.**

1. In the isolated QA account, establish a bounded, observable barrier after
   the feature revision is accepted by the file-sync worker but before that
   worker publishes it. Record the barrier mechanism, worker identity and
   both Git HEADs. It must be a documented QA-only facility; do not edit a
   production queue, database, journal or pointer by hand.
2. Ask the agent to switch to `qa/feature`, wait for the old worker at the
   barrier, then switch back to `main` and let the new revision publish.
3. Release the old worker and prove from its log/receipt that it completed
   after the main revision. Probe Q-alpha and Q-feature immediately and again
   at T_LATE.
4. Expected: S-main at both times; `FEATURE_ONLY` and `ALPHA_FEATURE` are
   never injected after the second switch. A fast flip-flop with no proof of
   completion order does not exercise this race and cannot be `PASS`.

The repository has a synthetic CAS race with an explicit barrier, but does not
yet provide this QA-only barrier in the operating QMD worker or an inactive DB
publication path. Until those are implemented and reviewed, Part B is
`BLOCKED` and so is formal release. The concrete QMD 2.5.3 adapter options
and isolated implementation sequence are in
[the published view investigation](plans/2026-10-17-qmd-published-view-options.md).

**Part C: I/O retry.**

1. Close the session and run `stage deny-read`.
2. Start a fresh session and probe Q-beta. Expected: beta is not treated as
   deleted. Its previous content stays searchable, and the I/O condition is
   reported in hook or backend logs without a hook failure.
3. Run `stage restore-read` and start a fresh session. Expected: normal
   state.

- **Evidence:** precise timestamps (tool result, hook start, log line),
  recall log lines, hook/backend log excerpts for the I/O condition, and the
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
  - A fresh fixture, opted in.
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
- **Known gap:** the [architecture boundaries](plans/2026-10-17-architecture-boundaries.md)
  record that revision idempotency is not yet implemented. Until it is, this
  scenario is expected to `FAIL` and blocks a formal release.
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
- **Evidence:** card bytes and hash, source hashes, probes, recall log
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
  - Before deletion, the raw document's new content is Present within
    T_FRESH. After deletion, it is Absent. File indexing still works while
    wiki compile remains OFF, including across retry and deletion.
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
  learning as deferred or off); a fresh fixture, opted in and indexed.
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

- **Purpose:** on a platform without managed support, the plugin stays
  harmless and preserves existing global QMD.
- **Owner:** install.
- **Cells:** U-intel, U-linux. **Hosts:** whichever are available.
- **Preconditions:** record before-state hashes of the global QMD binary path,
  its config and data.
- **Steps:**
  1. Install the candidate.
  2. Start a fresh session in a fixture.
  3. Ask "이 프로젝트 auto-context 설치 상태 보여줘".
  4. Probe Q-alpha.
- **Expected:**
  - `setup_required` or `unsupported_managed_platform`, with manual guidance.
  - Global QMD binaries, config and data unchanged.
  - No hook failure, and no injection claimed through v2 routing.
- **Evidence:** status output, before and after hashes.
- **Timeout:** T_SCENARIO.
- **Cleanup:** clean up the fixture; restore the account state.

## 10. Domain ownership

Scenarios respect the six owners in
[the architecture boundaries](plans/2026-10-17-architecture-boundaries.md).
Two rules apply:

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
| Install | ID-00, INST-01..04, PLAT-01, GIT-02 enrollment | Identity hashes, `setup` skill outputs, journal phase as reported |
| Runtime | RES-01, RES-02, RACE-01 retry, HOOK-0x hook budgets | Hook timings, process snapshots, deferred or retry status |

## 11. Changes after QA: reruns and identity match

- **Binding.** Every scenario record carries `artifactManifestSha256`. A
  result applies only to that hash.
- **Change detection.** Any change to the candidate tree after QA produces a
  new hash and invalidates every result for the old hash. This covers code,
  skills, hooks, manifests, locks, docs and version strings. List the changed
  paths with `git diff --name-only <qa-commit> <new-commit>`.
- **Rerun set.** Always the core smoke (RQA-ID-00 + RQA-HOOK-01 +
  RQA-HOOK-02), plus every scenario mapped from the changed paths:

| Changed paths | Rerun in addition to core smoke |
|---|---|
| `core/recall.py`, `core/qmd_project_query.py`, `core/qmd_route.py`, `core/wiki_freshness.py`, `backend/**` | SYNC-01, GIT-01..03, RACE-01, WIKI-02 |
| `core/sync.py`, `core/file_change_queue.py`, `core/index_enqueue.py`, `core/update.sh`, `skills/sync/**`, `skills/update/**` | SYNC-01, GIT-01..03, RACE-01, WIKI-03 |
| `core/wiki_*`, `skills/wiki-*/**`, `skills/enable-compile/**`, `agents/**` | WIKI-01..03 |
| `core/context_learning/**` | RES-01, RES-02 |
| `core/setup_entry.py`, `core/shared_*`, `core/install_update.py`, `core/qmd_shared_store.py`, `core/setup_guard.py`, `core/runtime_update.py`, `locks/**`, `skills/setup/**` | INST-01..04, PLAT-01, GIT-02 |
| `core/runtime_work.py`, `core/runtime_wake_policy.py`, `core/qmd_endpoint.py`, resource coordination modules, `backend/index_worker.sh` | RES-01, RES-02, RACE-01 |
| `core/config.py`, `hooks/**`, `hermes_adapter/**`, `plugin.yaml`, `__init__.py`, any host manifest or marketplace file | **All** scenarios |
| Version strings only | INST-01, INST-02 |
| Documentation only (`docs/**`, `README.md`; not `skills/**` or `agents/**`) | Core smoke only |
| Any path not listed | **All** scenarios |

- **Deployed identity.** The marketplace entry is not pinned to a revision
  (see [install-update contract](install-update-contract-20261006.md)). After
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

## 13. Authoring-pass status (2026-10-17)

| Scenario | Status | Note |
|---|---|---|
| RQA-ID-00, RQA-INST-01..04, RQA-HOOK-01..02, RQA-SYNC-01, RQA-GIT-01..03, RQA-WIKI-01..03, RQA-RES-01, RQA-PLAT-01 | `NOT RUN` | Specification only. No installed host, QMD, hook, install, model or network execution in this pass. The synthetic fixture helper was tested separately at L0. |
| RQA-RACE-01 | `BLOCKED` | A synthetic CAS race passed; the operating QMD worker has no late-worker barrier or inactive DB publication path. The host scenario has not run. |
| RQA-RES-02 | `BLOCKED` | Needs separate learning approval, safe synthetic installed-candidate resources, and observed OS-pressure stop/resume. L0 queue tests do not satisfy this row. |
| LI-INST, LI-MIG, LI-SCOPE, LI-RECOVER, LI-COEXIST, LI-VECTOR, LI-RECALL | `NOT RUN` | Exact marketplace install and fresh-host scenarios have not run. Production provider approval, migration cutover, and retrieval parity remain unresolved. |

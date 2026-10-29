# Settings Reference

`.memvane/settings.json`은 프로젝트별 MemVane 동작을 정하는 파일입니다.
`.auto-context/`만 있는 프로젝트는 그 폴더의 `settings.json`을 읽습니다.
보통은 에이전트에게 자연어로 요청해서 만들거나 조정하고, 직접 편집할 때는 이
문서를 기준으로 필요한 값만 바꾸면 됩니다.

**설정 파일에는 기본값과 다른 값만 적으십시오.** 기본값을 그대로 옮겨 적으면 나중에 기본값이 바뀌어도
그 프로젝트만 옛 값에 고정되고, 파일이 길어져 실제로 조정한 값이 묻힙니다.

**이 문서는 2단 구성입니다.** 앞쪽 [전체 키 레퍼런스](#전체-키-레퍼런스)는 훑어보는
용도의 표 한 장이고, 뒤쪽 [근거 부록](#근거-부록)은 "왜 이 기본값인가"를 설명합니다.
값을 바꾸기 전에 해당 항목의 부록 절을 읽으십시오 — 순위 컷으로 동작하는 `minScore`,
유사도가 아닌 검색 score, 유료 호출 예산의 곱처럼 이름과 다르게 동작하는 설정이
거기에 설명돼 있습니다.

## Minimal settings

settings 파일의 키가 `name`(필수)과 `skipPaths`(선택)뿐이면 **minimal settings**입니다.
나머지 effective 설정은 플러그인이 `name`에서 결정적으로 유도합니다
(`core/config.py`의 `expand_minimal_settings`).

```json
{
  "name": "my-project",
  "skipPaths": ["node_modules", "dist"]
}
```

| 항목 | 유도 값 |
|---|---|
| collection 이름 | `name`의 slug(소문자, `[a-z0-9._-]`, 59자 이하)가 raw, slug + `-wiki`가 wiki입니다. `"My Project"` → `my-project`·`my-project-wiki` |
| `collectionPaths` | raw `"."`(프로젝트 루트 전체가 위키 생성 원천), wiki `".memvane/wiki"`(프로젝트의 활성 폴더 아래 `wiki`) |
| `collectionRoles` | raw `raw`, wiki `wiki` |
| `recallStrategy` | `wikiOnly`. 검색은 위키 카드만 합니다 |
| `compile` | `mode: "auto-wiki"`, `triggers: ["post_tool_source","post_sync_source","manual"]`, `extractor.builtins: ["claude","codex","hermes"]`. 나머지 compile 값은 기본값입니다 |
| `contextLearning` | `capture`·`autoCycle` 모두 `true`. 준비된 Laya 선택 모델이 있으면 검색 시 설정 없이 카드 선택에 쓰입니다 |
| `indexing` | `true`. 끄는 방법은 아래 "인덱싱 끄기"입니다 |
| `minScore`·`topN`·`queryTimeout`·`prefixStyle`·`lexicalPatterns`·`allowRoots` 등 | 기본값 |

정규화된 effective config에는 `settingsModel: "minimal"`이 붙습니다. legacy 파일에는
이 키가 없습니다. 설정 파일은 하나만 읽습니다: `.memvane/settings.json`, 없으면
`.auto-context/settings.json`.

**minimal이 아닌 경우.** 두 키 외의 키가 하나라도 있으면 legacy 파일이며 아래
[전체 키 레퍼런스](#전체-키-레퍼런스)대로 읽습니다. 일부 키만 유도하지 않습니다.
`name`이 없거나 slug가 비는 경우(ASCII 영숫자가 없는 이름), `skipPaths`가 비어 있지
않은 문자열 목록이 아닌 경우도 minimal이 아니며, collection이 비어 아무것도
인덱싱하지 않습니다.

**`skipPaths`는 위키 생성에서 제외할 경로입니다.** 원문 경로의 상위 디렉터리
문자열에 항목이 포함되면 그 파일로 위키를 만들지 않습니다(`build` → `docs/build/a.md`
제외, `docs/build-notes.md`는 생성). 생성되지 않은 내용은 검색되지 않습니다.
기본 제외는 다음과 같습니다.

- `.`으로 시작하는 모든 폴더와 파일(`.git`, `.claude`, `docs/.draft` 등)
- `.memvane`(그리고 `.auto-context`): 위키 생성 원천에서 제외됩니다. 그 안의 `wiki`는
  wiki collection 루트이므로 검색 대상입니다.

**인덱싱 끄기.** minimal settings에는 `indexing` 키가 없고, settings 파일이 있으면 켜짐으로
해석합니다. 실제 색인·검색·compile·학습은 선택된 Laya route가 있는 프로젝트에서만 돕니다.
route가 없는 프로젝트는 아무 작업도 받지 않고, settings 파일이 있으면 SessionStart에서
setup skill로 온보딩하라는 안내 한 줄만 받습니다.

**선택된 memvane-db route.** route record는 settings 파일 바이트가 아니라 해석된 settings
identity를 묶습니다(아래 "route의 settings binding"). minimal settings로 선택된 route는
유도된 legacy 모양으로 검사합니다.

**raw collection.** minimal settings의 raw collection(프로젝트 루트 `.`)은 wiki 생성 원천이자
선택된 route의 원천 신선도 기준입니다. `resolve_paths`는 이를 `sourceEntries`로 내보내고,
laya route에서의 role은 `raw`입니다.

### Laya 온보딩과 settings binding

Laya 온보딩(`plan-fresh-wiki-origin`)은 minimal settings만 받습니다. canonical
`.memvane/settings.json`이 `{name, skipPaths}`가 아니면 `minimal_settings_required`로
거부하고 파일을 다시 쓰지 않습니다. legacy 형식을 minimal로 바꾸는 일은 사용자가 직접
파일을 고쳐서 합니다(`name` slug가 raw collection `<slug>`와 wiki collection `<slug>-wiki`를
정합니다).

**route의 settings binding.** route record의 `settingsSha256`은 settings 파일 바이트가 아니라
해석된 settings identity(프로젝트 루트와 collection별 이름·role·루트, wiki 루트 포함)의
digest입니다(`laya_index_host_route.settings_identity_digest`). 그래서 `skipPaths`를 손으로
고쳐도 route는 그대로 선택되고, `name`을 바꿔 collection 이름이 달라지면
`route_settings_changed`로 막힙니다. 다른 digest(예: settings 파일 바이트)를 묶은 route도
같은 사유로 막힙니다.

이전 raw 이름을 `sourceRevisions`에 적은 카드는 선택된 recall과 compile 쪽(이미 compile된
revision 판정, 원문 삭제 시 카드 정리, wiki lifecycle)이 같은 규칙으로 다룹니다.
settings에 없는 이름은 경로를 담은 현재 raw collection으로 해석하고(`core/source_label.py`),
wiki 이름이나 모든 raw 루트 밖 경로는 같은 이름끼리만 맞춥니다.

### 임베딩 런타임과 retired QMD 정리 (유지보수 명령)

MemVane 색인의 임베딩 provider는 `~/.local/state/memvane/embed-runtime`의 로컬 런타임입니다
(`active.json`·`previous.json`·`generations/`). 사용자는 setup skill에 자연어로 요청하고,
아래는 그 skill이 쓰는 유지보수 명령입니다. `ROOT`는 설치된 플러그인 루트이며 이 명령들은
`--project`가 필요 없습니다.

```bash
python3 -B "$ROOT/core/setup_entry.py" index-runtime-status
python3 -B "$ROOT/core/setup_entry.py" provision-index-runtime --approve-index-runtime-download [--provider-model-file <gguf>]
python3 -B "$ROOT/core/setup_entry.py" gc-index-runtime [--approve-index-runtime-gc]
python3 -B "$ROOT/core/setup_entry.py" plan-legacy-cleanup [--include-user-qmd-data]
python3 -B "$ROOT/core/setup_entry.py" apply-legacy-cleanup --approve-legacy-cleanup [--include-user-qmd-data]
```

- **`provision-index-runtime`** — 이전 설치가 없는 머신에서 런타임을 만듭니다. Node는 `PATH`를
  먼저 보고, 다음으로 mise·fnm·nvm·volta·asdf 설치와 Homebrew·시스템 경로(`/opt/homebrew/bin`,
  `/usr/local/bin`, `/usr/bin`)를 봅니다. 메이저 22·24·26이고 shim이 아닌 실제 바이너리(Mach-O·ELF)만
  받습니다. `locks/laya-embed-runtime`을 `npm ci --ignore-scripts`로 설치해 node-llama-cpp 3.18.1과
  prebuilt `@node-llama-cpp/mac-arm64-metal`을 얻고, GGUF는
  `https://huggingface.co/ggml-org/embeddinggemma-300M-GGUF/resolve/main/embeddinggemma-300M-Q8_0.gguf`에서
  받아 sha256 `f470220f84b6235197541352d22f10bf00098a8242c18eaacea9c8a4add557bc`일 때만 채택합니다.
  `--provider-model-file`은 같은 GGUF의 로컬 사본을 씁니다. npm registry·모델 호스트 접근이라
  `--approve-index-runtime-download` 없이는 거부합니다(`laya_embed_runtime_download_approval_required`).
- **`gc-index-runtime`** — 세대 목록을 보여 주고, `--approve-index-runtime-gc`가 있으면 active·previous가
  아니고 `~/.local/state/memvane/*/production-provider-attestation.json`도 가리키지 않는 세대를 지웁니다.
  attestation의 `profileDependencyClosure` 기록은 참조로 세지 않습니다. 읽을 수 없는 attestation이
  있으면 `attestation_unreadable`로 막힙니다.
- **`plan-legacy-cleanup`** — 읽기 전용입니다. 플러그인 QMD 데몬 pid 파일
  (`~/Library/Application Support/qmd-auto-context/backend/daemon-*.pid`)과 각 pid가 검증된 살아 있는
  데몬인지, `managed-by: qmd-auto-context` 마커가 있는 LaunchAgent(`com.qmd-mcp-daemon`·
  `com.qmd-keepalive`·`com.qmd-logrotate`·`com.qmd-index-worker`), 대상 트리와 크기, blocker를
  보여 줍니다.
- **`apply-legacy-cleanup --approve-legacy-cleanup`** — 명령줄이 정확히
  `<node> <플러그인 QMD runtime>/generations/g-<id>/qmd-native.mjs mcp --http --port <pid 파일의 port>`인
  데몬만 신호 직전에 다시 확인한 뒤 `SIGTERM`으로 멈춥니다(`SIGKILL`·패턴 검색은 쓰지 않습니다).
  마커가 있는 LaunchAgent plist만 bootout 후 지우고(`.bak` 사본은 그대로), 이어서
  `~/Library/Application Support/qmd-auto-context/`의 `runtimes/qmd`·`shared-qmd`·`backend`를 지웁니다.
  `~/.cache/qmd`·`~/.config/qmd`에는 사용자 자신의 QMD 인덱스·설정·모델 캐시도 있으므로
  `--include-user-qmd-data`를 함께 줄 때만 지웁니다. 같은 디렉터리의 `learning`·`laya-inference`·
  `live-selection`·`runtimes/laya`는 남깁니다.
- **blocker** — 설치 attestation이 대상 트리 안의 파일을 아직 가리키면
  `attestation_references_legacy_path`, `~/.local/state/memvane/*/reviewed-route.json`의 `pluginRoot`가
  `core/backend_manager.sh`를 가진 이전 번들을 가리키면 `older_host_bundle_bound`입니다. plan은 blocker를
  보고하고 apply는 아무것도 바꾸지 않고 `blocked`로 끝납니다(이전 번들은 `~/.cache/qmd`·`~/.config/qmd`를 다시 만듭니다). 그래서 정리는 Claude·Codex
  host 캐시가 모두 이 릴리스를 로드하고, 모든 설치가 SessionStart 번들 업그레이드로 이 번들로 옮겨
  가고, 모든 설치가 Laya 임베딩 런타임으로 재attest된 뒤에 한 번 실행합니다.

## 기본 예시

```json
{
  "indexing": true,
  "name": "my-project",
  "collections": [
    "my-project",
    "my-project-wiki"
  ],
  "collectionPaths": {
    "my-project": "docs",
    "my-project-wiki": ".memvane/wiki"
  },
  "collectionRoles": {
    "my-project": "raw",
    "my-project-wiki": "wiki"
  },
  "minScore": 0.33,
  "topN": 3,
  "queryTimeout": 5,
  "wikiPath": ".memvane/wiki"
}
```

`minScore`는 유사도 임계가 아니라 순위 컷입니다(`0.33` ≈ 상위 3위까지).
자세한 내용은 [minScore는 유사도가 아니라 순위입니다](#minscore는-유사도가-아니라-순위입니다)를 보세요.

## 전체 키 레퍼런스

legacy 파일 스키마의 전부입니다(61개). [minimal settings](#minimal-settings)는 이 중 `name`·`skipPaths`만 씁니다. 여기 없는 키는 읽히지 않습니다 —
[설정할 수 없는 값](#설정할-수-없는-값)을 보십시오. `기본값`은 키를 적지 않았을 때의
동작이므로, 그 값과 같은 것을 적을 필요는 없습니다.

| 키 | 기본값 | 설명 |
|---|---:|---|
| `indexing` | `null` | `true`면 이 프로젝트에서 recall·indexing을 씁니다. `false`면 설정 파일이 있어도 effective collection이 비어 비활성처럼 동작합니다. |
| `name` | `""` | 프로젝트 이름입니다. legacy 파일에서는 동작에 영향이 없고, [minimal settings](#minimal-settings)에서는 collection 이름을 정합니다. |
| `collections` | `[]` | logical collection 이름 목록입니다. 비어 있으면 recall은 무동작입니다. |
| `collectionPaths` | `{}` | collection별 프로젝트 상대 경로입니다 → [인덱싱 범위를 줄이는 방법](#인덱싱-범위를-줄이는-방법) |
| `collectionRoles` | `{}` | collection 역할(`raw`·`wiki`·`session`·`source`)입니다. 미설정은 `raw`, 오타 등 미지 값은 fail-closed로 전부 제외 → [Collection Roles](#collection-roles) |
| `recallStrategy` | `"hierarchical"` | `flat`(전부 함께) · `hierarchical`(wiki 먼저, 없으면 raw fallback) · `wikiOnly`(wiki만). 선택된 Laya 경로는 이 값과 관계없이 wiki만 검색합니다 → [Recall Strategy](#recall-strategy) |
| `minScore` | `0.0` | recall 주입 하한입니다. **유사도 임계가 아니라 순위 컷입니다**(score = `1/순위`) → [minScore는 유사도가 아니라 순위입니다](#minscore는-유사도가-아니라-순위입니다) |
| `rawFallbackMinScore` | `minScore` | `hierarchical`의 raw fallback에 적용할 순위 컷입니다. 선택된 Laya 경로는 raw fallback을 쓰지 않습니다. |
| `topN` | `3` | 주입할 최대 문서 수입니다. `minScore`가 이보다 강하게 자를 수 있습니다. |
| `queryTimeout` | `5` | query **1건**의 응답 대기(초)입니다. 전체 훅 예산이 아닙니다 → [blocking 훅의 시간 예산](#blocking-훅의-시간-예산) |
| `staleQueueThreshold` | `20` | 큐 적체 안내 기준 줄 수입니다. 값은 정규화되지만 이 값을 읽는 경로가 없습니다. |
| `skipPaths` | `[]` | 위키 생성에서 제외할 원문 디렉터리이고, 기존 위키 출처 보호에도 쓰입니다 → [recall 결과에서 특정 경로 제외](#recall-결과에서-특정-경로-제외) |
| `allowRoots` | `[]` | 프로젝트 밖 절대경로 collection을 허용할 root 목록입니다. 보통 비워 둡니다. |
| `prefixStyle` | `"full"` | recall 출력 prefix 스타일입니다(`full`·`tag`). |
| `injectSummaryMaxChars` | `600` | wiki 카드 1장당 주입할 본문 문자 상한입니다. raw 결과는 원문 본문을 자동 주입하지 않습니다. `0`=끔, 4000으로 클램프 → [카드 본문 주입](#카드-본문-주입) |
| `injectSourcePathsPerCard` | `3` | 카드 1장당 주입할 원문 경로(`sources[].path`) 수입니다. `0`=끔, 10으로 클램프 → [원문 경로 주입](#원문-경로-주입) |
| `events` | `["sessionStart","userPromptSubmit","postToolUse"]` | 자동 동작을 켤 이벤트 목록입니다 → [Events](#events) |
| `sessionStartSync` | `true` | SessionStart 스냅샷 동기화 스위치입니다. 값은 정규화되지만 이 값을 읽는 경로가 없습니다. 선택된 Laya route의 sync는 full-scan worker가 맡습니다 → [Git checkout·worktree 정책](git-worktree-search-sync-policy.md) |
| `lexicalPatterns` | `[]` | 특수 lexical pattern입니다. 현재 쓰이는 값은 `ep` 하나입니다. |
| `wikiPath` | `".memvane/wiki"` | wiki 카드 루트입니다. 프로젝트 밖(심볼릭 링크·`../`)을 가리키면 거부됩니다. `.memvane/`나 `.auto-context/`로 시작하는 값(`collectionPaths` 포함)은 프로젝트의 활성 폴더를 따릅니다. |
| `compile.mode` | `"off"` | **compile의 유일한 활성 스위치이자 쓰기 정책**입니다. `off`·`candidates`·`guarded`·`auto-wiki` → [compile.mode](#compilemode) |
| `compile.triggers` | `[]` | compile source를 만들 trigger 목록입니다. 보통 `post_tool_source`·`post_sync_source`·`manual`. SessionStart가 감지한 파일의 위키 생성에는 별도 `session_start_source` 동의가 필요합니다. |
| `compile.defaultStatus` | `"generated"` | 새 카드의 status입니다. `generated`·`tentative`만 허용합니다(`verified`를 넣으면 검수를 우회합니다). |
| `compile.recallVerifiedOnly` | `true` | `true`면 미검수(`generated`·`tentative`) 카드를 recall에 아예 내지 않습니다. |
| `compile.excludeStatusesFromRecall` | `["discarded","contested"]` | recall에서 제외할 wiki status입니다. |
| `compile.lowPriorityStatuses` | `["generated","tentative"]` | `topN` 절단 **전에** 검수 카드 뒤로 강등할 status입니다. |
| `compile.maxAutoPageLines` | `120` | 자동 생성 카드 본문의 최대 줄 수입니다(extractor 프롬프트에도 그대로 전달됩니다). |
| `compile.maxSourceChars` | `12000` | extractor·verifier에 넘길 소스 길이 상한입니다. 올리면 두 호출의 토큰이 함께 늘어납니다. |
| `compile.reasoningEffort.generation` | `"low"` | 추출 단계 reasoning effort입니다(`low`·`medium`·`high`·`xhigh`). |
| `compile.reasoningEffort.verify` | `"medium"` | 검수 단계 reasoning effort입니다. |
| `compile.reasoningEffort.semanticDedup` | `"medium"` | dedup 판정 단계 reasoning effort입니다. |
| `compile.reasoningEffort.engines` | `{}` | 엔진별 override입니다(`{"codex": {"verify": "high"}}` 형태). |
| `compile.extractor.builtins` | `[]` | 런타임에 해석되는 adapter 엔진 이름입니다(`claude`·`codex`·`hermes`). 경로를 적지 마십시오. |
| `compile.extractor.backends` | `{}` | 엔진 → 명시 argv입니다. builtins로 안 되는 경우의 escape hatch입니다. |
| `compile.extractor.timeout` | `120` | extractor 호출 1회의 timeout(초)입니다. |
| `compile.extractor.cooldownSeconds` | `600` | extractor 실패 뒤 재시도 cooldown입니다. **24h로 클램프**됩니다 → [cooldown·lock의 "영구 정지" 방어](#cooldownlock의-영구-정지-방어) |
| `compile.budget.extractorPerRun` | `10` | run당 extractor 호출 상한입니다(50 클램프). 초과분은 requeue → [compile.budget](#compilebudget--한-run의-유료-호출-총량) |
| `compile.budget.cardsPerSource` | `10` | 소스 1개에서 쓸 카드 수 상한입니다(50 클램프). 초과분은 기록만 하고 requeue하지 않습니다. |
| `compile.budget.verifyPerRun` | `3` | 검수 예산의 **하한**입니다(50 클램프). 실제 예산은 그 run의 생산량으로 커집니다. |
| `compile.budget.dedupPairsPerScan` | `8` | scan 단위 **유료** judge 호출 상한입니다. 값은 읽히지만 retroactive scan은 judge를 부르지 않아 이 예산을 쓰지 않습니다. |
| `compile.budget.dedupPairsPerCompile` | `1` | write-time dedup gate 1회의 **유료** judge 호출 상한입니다. |
| `compile.batch.idleSeconds` | `90` | **시작 조건**: 가장 오래된 잡이 이만큼 묵으면 개수가 모자라도 시작합니다. |
| `compile.batch.maxItems` | `5` | **시작 조건**: 큐가 이만큼 모이면 즉시 시작합니다. 상한이 아닙니다(상한은 `budget`). |
| `compile.semanticDedup.enabled` | `true` | 중복 카드 생성 방지 전체를 켜고 끕니다. |
| `compile.semanticDedup.maxPairsPerScan` | `10` | retroactive scan 1회가 `dedup-needed.jsonl`에 넣는 쌍 상한입니다(1~100) → [Retroactive scan](#retroactive-scan) |
| `compile.semanticDedup.candidateMinScore` | `0.3` | judge에 넘길 후보의 retrieval floor입니다. **판정 임계가 아닙니다** → [Semantic Dedup](#semantic-dedup) |
| `compile.semanticDedup.judge.enabled` | `true` | LLM 본문 대조 판정을 씁니다. 끄면 레거시 무료 score 게이트로 degrade합니다. |
| `compile.semanticDedup.judge.crossEngine` | `"prefer"` | 판정 엔진을 생산 엔진과 분리합니다(`prefer`·`require`·`off`). **write-time gate에만 적용됩니다** → [판정 엔진 분리 (dedup)](#판정-엔진-분리-dedup) |
| `compile.semanticDedup.judge.timeout` | `120` | judge 호출 1회의 timeout(초)입니다. |
| `compile.semanticDedup.judge.cooldownSeconds` | `600` | judge 실패 뒤 cooldown입니다(24h 클램프). |
| `compile.semanticDedup.judge.maxCharsPerPage` | `6000` | judge에 넘길 카드 1장당 본문 상한입니다. |
| `compile.verify.enabled` | `true` | 기계 검수를 켜고 끕니다. |
| `compile.verify.timeout` | `120` | 검수 호출 1회의 timeout(초)입니다. |
| `compile.verify.onFail` | `"delete"` | 검증 실패(원문과 모순) 시 동작입니다(`delete`·`contested`·`none`). |
| `compile.verify.onInconclusive` | `"delete"` | 판정 불가 시 동작입니다. 값 집합은 `onFail`과 같습니다 → [`onInconclusive` 기본값이 `delete`인 이유](#oninconclusive-기본값이-delete인-이유) |
| `compile.verify.crossEngine` | `"prefer"` | 검수 엔진을 카드를 만든 엔진과 분리합니다(`prefer`·`require`·`off`) → [검수 엔진 분리 (`crossEngine`)](#검수-엔진-분리-crossengine) |
| `compile.verify.builtins` | `[]` | 검수 후보 엔진입니다. 비면 `compile.extractor` 풀을 물려받습니다. |
| `compile.verify.cooldownSeconds` | `600` | 검수 실패 뒤 cooldown입니다(24h 클램프). |
| `compile.sourceScan.enabled` | `true` | **wiki 수명 주기 조치**(원문 소실·지문 없음 카드 삭제, 해시 변경 재생성 enqueue)를 켜고 끕니다(provenance 결측 집계는 끄지 않습니다). **`true`/`false`만** 유효하고, `"false"`처럼 bool이 아닌 값은 기본값(유지)으로 폴백합니다 → [wiki 수명 주기](#wiki-수명-주기-원문-소실변경) |
| `compile.sourceScan.maxCardsPerScan` | `300` | 수명 주기가 한 회차에 원문 해시를 대조할 카드 수입니다(5000 클램프). 초과분은 순환 커서가 다음 회차에 이어 봅니다. `QMD_SOURCE_SCAN_MAX`가 이 값을 덮습니다. |
| `maintenance.orphanVectors.*` | `enabled: true`, `minRatio: 0.2`, `minCount: 200`, `cooldownSeconds: 86400` | 값은 정규화되지만 이 값을 읽는 회수 경로가 없습니다. |

### compile.mode

compile의 활성 여부와 쓰기 정책을 **한 값**으로 정합니다. 예전에는 `enabled`(bool) +
`mode` + `autoWrite`(bool)가 같은 사실을 세 곳에서 표현했고, 게이트마다 보는 값이 달라
"켜졌는데 절반만 도는" 상태가 관측되지 않은 채 존재했습니다. 지금은 `mode != "off"`가
곧 활성입니다.

| 값 | 동작 |
|---|---|
| `off` | 아무것도 하지 않습니다(기본값). enqueue·worker·검수가 전부 멈춥니다. |
| `candidates` | 후보를 `candidates.jsonl`에 기록만 하고 wiki 카드를 쓰지 않습니다. 파이프라인을 관찰할 때 씁니다. |
| `guarded` | `confidence: "high"` 후보만 카드로 씁니다. 나머지는 candidate로 강등됩니다. |
| `auto-wiki` | clean candidate를 wiki Markdown으로 직접 씁니다. minimal settings의 유도 값입니다. |

미지 값(오타 포함)은 `off`로 fail-closed 처리됩니다 — 유료 파이프라인이 오타 하나로
켜지지 않게 하기 위함입니다.

### Wiki source freshness와 legacy migration

이는 설정이 아니라 고정된 정책입니다. 새 카드와 자동 업데이트 카드는
`generated` 또는 `tentative`처럼 `verified`가 아닌 상태로 시작하며, 원문 revision 대조의
기준은 플러그인이 만든 카드의 compiler-owned `sourceRevisions`입니다.

binding store가 있는 **선택 Laya 위키 전용 검색**은 검색 시 원문을 다시 열거나
해시를 계산하지 않고, 마지막으로 완료·게시된 위키 색인의 본문 해시·범위·세대·명시
제외를 확인합니다. 변경 감지와 원문 해시 비교, 위키 생성·수정, 색인 게시는 비동기
인덱스 작업의 책임입니다. 갱신 중에는 이전 완료 위키가 검색될 수 있으며, 이는 최신
원문이라는 보증이 아닙니다. 카드 자체의 `verifiedBodyHash`와 출처 구조 검사는 검색에도
남습니다. `verifiedBodyHash`가 없는 카드는 "결속 없음"으로 보고
검색하며, 값이 있으면 본문과 일치해야 합니다. `sourceRevisions`가 없는 카드는 검색하지 않습니다.
`sourceRevisions`의 collection 이름이 현재 route에 없는 이름(이름이 바뀌었거나 복사된 프로젝트)이면
경로로 판정합니다. 프로젝트 기준 경로가 raw collection 루트 안에 있어야 합니다.
검색된 카드가 있는데 모두 걸러지면 stderr와 설치 이벤트 로그의 `recall` 이벤트(`notes`의 `laya_wiki_rows_filtered`, `filtered_reasons`)에 사유별 개수를 남깁니다. 준비된 체크포인트가 있으면 검증된 위키 후보 최대 15개를 로컬 Laya 선택 모델이
평가해 최대 3개를 주입합니다. 체크포인트가 없거나 모델이 응답하지 못하면 검증된
후보를 하이브리드 순위대로 최대 3개 주입합니다.
binding store가 없는 구형 Laya 선택 경로도 직접 RRF 결과를 주입하지 않습니다.

**신선도 검사가 보장하는 것과 보장하지 않는 것을 구분하십시오.** `verified` + 신선도 통과는
**"원문이 컴파일 시점 이후 바뀌지 않았다"**는 뜻이며, **"지금 이 카드에 적힌 내용이
검증됐다"는 뜻이 아닙니다.** 지문(`sourceRevisions`)은 원문 파일에만 걸려 있고 카드 파일
자체에는 무결성 검사가 없습니다 — 원문을 그대로 둔 채 카드 본문만 고치면 그 내용이
`verified` 표시를 단 채 그대로 주입됩니다. 카드를 손으로 편집하거나 중복 카드를 병합할 때는
이 점을 알고 하십시오(고유 사실을 `qmd:auto:end` 밖에 두라는 권고는 **소스 변경에 의한
재생성으로부터 보존**하기 위한 것이지, 그 내용이 검증된다는 뜻이 아닙니다).

provenance 없는 legacy `verified`는 `unknown`으로 fail-closed drop됩니다. 릴리스 유지보수의
명시적 offline migration은 이런 qmd 생성 카드와 `reviewed`·`canon`·`manual` 상태 카드를
`generated`으로 정규화하고 `reviewed:` 및 검수 proof 필드를 제거합니다. 다음 source edit와
compile, 성공적인 verification이 끝날 때까지 이 카드는 recall에 나오지 않습니다. foreign
카드는 변경하지 않지만 `recallVerifiedOnly: false`여도 절대 주입하지 않습니다.

사람 검수 lifecycle은 없습니다. 새 자동 카드에 `reviewed:`나 `reviewed`·`canon`·`manual`
status를 쓰지 않으며, `wiki-review` UI·skill·agent도 제공하지 않습니다. `merge-needed.jsonl`은
승인 대기열이 아닌 non-trusted collision diagnostic입니다.

운영 중 drop 원인은 설치 이벤트 로그(`events.jsonl`)의 `recall` 이벤트 `reason`·`notes`와
`filtered_reasons`(사유별 개수)에서 봅니다(`memvane:logs` 스킬). 이벤트 로그는 카드 경로·sha와
프롬프트 sha256·길이만 남기고, 로그와 injected context에는 원문 본문·secret value를 기록하지 않습니다.

P0는 `.env`를 source로 등록하지 않습니다. P1 structured-evidence adapter 전에는 `prod.env`의
repository-config evidence 또는 runtime-current proof를 주장하지 않습니다.

### compile.budget — 한 run의 유료 호출 총량

`compile.budget`은 **사용자 계정에 청구되는 host CLI 호출 수**를 정하는 값만 모은
블록입니다. 예전에는 `batch.maxPerRun`·`maxCardsPerSource`·`verify.maxPerRun`·
`semanticDedup.judge.maxPairsPer*`로 네 서브트리에 흩어져 있어, 어느 값이 비용을
정하는지 읽어서는 알 수 없었습니다.

> **⚠ 총량은 이 값들의 합이 아니라 일부의 곱입니다.** 한 run이 쓰는 카드 수는
> `budget.extractorPerRun × budget.cardsPerSource`이고 dedup judge는 카드당 1회이므로,
> **두 값을 함께 올리면 호출 수가 제곱으로 늘어납니다.** 어떤 클램프도 이 곱을 막지
> 않습니다(두 값을 함께 올리는 것은 사용자의 명시적 선택으로 봅니다). 셈은 아래
> [처리량과 유료 호출 예산](#처리량과-유료-호출-예산) 절에 그대로 적혀 있습니다 —
> 두 값 중 하나라도 올리기 전에 그 절을 읽으십시오.

### 설정할 수 없는 값

아래는 **설정 키가 아닙니다.** 적어도 읽히지 않습니다.

- **산출물 경로 9종** — `candidates.jsonl`·`source-queue.jsonl`·`tombstones.jsonl`·
  `generated-manifest.jsonl`·`merge-needed.jsonl`·`verify-{queue,log,skipped,deleted}.jsonl`은
  프로젝트 폴더의 `compile/` 아래 고정 상수입니다(`core/compile_paths.py`). 실제 산출물
  26종 중 9종만 설정 가능했고 sidecar(`*.lock`·`*.compact-stamp`)는 원본 파일명에서
  파생되어 애초에 설정 대상이 아니었습니다 — 반쪽 추상화였고, 오타 하나가 과금 루프를
  만든 실패 클래스(`verify_skipped_path`)도 여기서 사라집니다.
- **dedup score 레버 4종** — `threshold`·`autoMergeThreshold`·`topK`·`similarPageMaxChars`는
  상수가 됐습니다. 검색 score가 유사도가 아니라 **RRF 순위의 함수**라 어떤 임계도
  "두 카드가 같은 사실을 말한다"를 표현할 수 없었습니다([Semantic Dedup](#semantic-dedup)).
- **`compile.sourceMissingPath`** — 위 "산출물 경로 9종"과 같은 경로 키입니다. 읽는 소비자가
  없으므로 적어 두어도 읽히지 않습니다.
  (`compile.sourceScan.enabled`·`maxCardsPerScan`은 경로 키가 아니라 수명 주기 동작 값이라
  읽힙니다 — 위 [전체 키 레퍼런스](#전체-키-레퍼런스)를 보십시오.)
- **클램프 상수** — `MAX_COMPILE_PER_RUN`(50)·`MAX_CARDS_PER_SOURCE`(50)·
  `MAX_VERIFY_PER_RUN`(50)·`VERIFY_PRODUCED_HARD_CAP`(30)·cooldown 24h 상한은
  "설정 오류나 모델 출력이 과금 규모를 정하지 못한다"를 보장하는 자리라 설정으로
  열지 않습니다.

제거된 키가 파일에 남아 있으면 그 값은 무시됩니다. 옮겨간 키의 행선지는
`core/config.py`의 `deprecated_keys`가 정합니다. 그 키를 지우거나 새 위치에 다시 적으십시오.

**예외 하나 — `compile.maxCardsPerSource`는 값이 잠정 반영됩니다.** 새 키
`compile.budget.cardsPerSource`를 **적지 않은 경우에만** 옛 값이 그 자리를 대신하고
(같은 50 클램프를 통과합니다), 새 키를 적으면 새 키가 이깁니다. 잠정 반영이지 스키마가
아니니 새 위치로 옮겨 적으십시오. **옮겨간 나머지 4키
(`batch.maxPerRun`·`verify.maxPerRun`·`semanticDedup.judge.maxPairsPer*`)는 반영되지
않습니다**. 이 비대칭은 의도된 것입니다: 그 4키를 반영하면 라이브 프로젝트의
**유료 호출 예산이 플러그인 업그레이드만으로 올라가는데**,
`maxCardsPerSource`는 라이브 사용이 0건이라 반영해도 지금 예산이 바뀌는 프로젝트가 없고,
이 키를 적는 동기 자체가 카드 수를 **줄이는** 것(비용 감소 방향)입니다.

---

# 근거 부록

여기부터는 "왜 이 기본값인가"입니다. 위 표에서 바꾸려는 값이 있으면 해당 절을 먼저
읽으십시오.

- [blocking 훅의 시간 예산](#blocking-훅의-시간-예산) · [Collection Roles](#collection-roles) · [Recall Strategy](#recall-strategy)
- [카드 본문 주입](#카드-본문-주입) · [원문 경로 주입](#원문-경로-주입) · [lex 게이트](#lex-게이트--무관-주입-차단) · [minScore는 유사도가 아니라 순위입니다](#minscore는-유사도가-아니라-순위입니다)
- [Events](#events)
- [Wiki Compile](#wiki-compile) · [처리량과 유료 호출 예산](#처리량과-유료-호출-예산) · [Verify](#verify) · [wiki 수명 주기](#wiki-수명-주기-원문-소실변경) · [Semantic Dedup](#semantic-dedup)
- [cooldown·lock의 "영구 정지" 방어](#cooldownlock의-영구-정지-방어)
- [Common Recipes](#common-recipes) · [Troubleshooting](#troubleshooting)

## blocking 훅의 시간 예산

`UserPromptSubmit` recall은 **blocking hook**입니다 — 이 훅이 끝날 때까지 사용자 프롬프트가
모델로 가지 않습니다. 설정 키는 **`queryTimeout` 하나이고 그것은 per-query 값**입니다.
기본값은 `5`초입니다(`core/config.py`의 `DEFAULT_CONFIG["queryTimeout"]`). 설정을 생략하면
이 5초가 적용됩니다.

선택된 Laya 경로의 검색 단계는 `core/laya_host_response.py` 안의 고정 deadline으로도 묶입니다.
lex·vector 질의 배치는 각각 2초, DF 좁히기 프로브는 **패스 전체**가 1초입니다(term 당 질의는
최대 6개) → [lex term 선택](#lex-term-선택--코퍼스에-있는-단어만-and-에-넣습니다).

> 이 문서를 읽는 사람과 도구를 위해 못박습니다: **"queryTimeout 3초 예산"이라는 글로벌 판정 기준은 존재하지 않습니다.** 글로벌 기본값은 일관되게 5초입니다. **3초를 기본값이나 글로벌 판정 기준으로 인용하지 마십시오.**

## Collection Roles

`collectionRoles`는 같은 collection이라도 검색과 compile에서 다르게 취급하기 위한
역할입니다. 선택된 Laya route는 `wiki` role만 색인·검색·주입하고([Recall Strategy](#recall-strategy)),
나머지 role은 wiki compile의 입력이 되는지를 정합니다.

| Role | MemVane 색인·검색 | wiki compile 입력 |
|---|:---:|:---:|
| `raw` | X | O |
| `session` | X | O |
| `wiki` | O | X |
| `source` | X | O |

- `raw` — 원본 문서입니다. 자세하지만 파편적일 수 있습니다.
- `wiki` — 정리된 장기기억입니다. 검색 결과에는 카드 본문과 원문 경로가 함께 주입됩니다.
- `session` — 세션 요약이나 중간 후보입니다. compile source가 될 수 있지만 일반 raw보다
  낮은 우선순위로 운용하는 용도입니다. compile 취급은 `raw`와 같습니다.
- `source` — **카드의 원천이지만 검색 대상은 아닌 문서**입니다. 아래 참고.

**role 값을 적지 않은 것과 인식할 수 없는 값을 적은 것은 다르게 처리합니다.**

| 상태 | 처리 | 이유 |
|---|---|---|
| `collectionRoles`에 키가 없음 | `raw` (fail-open) | role 도입 전 프로젝트입니다. 하위호환이 맞습니다. |
| 키가 있는데 값이 미지 (`"sourse"`, `"Source"`, `5` 등) | 색인·recall·compile에서 **전부 제외** (fail-closed) | 사용자가 무언가를 의도했는데 읽지 못한 것입니다. 여기서 `raw`로 넘기면 오타 하나가 "색인 제외"를 실제 색인으로 뒤집어, 의도하지 않은 문서가 인덱싱됩니다. |

### role `source` — 인덱싱하지 않고 카드만 만든다

`source` 문서는 검색 대상이 아닙니다. 그 내용은 **그 문서로 만들어진 wiki 카드를 통해서만** recall에
들어옵니다. 카드가 없는 `source` 문서는 recall 관점에서 존재하지 않습니다.

```json
{
  "collections": ["my-project", "my-archive", "my-project-wiki"],
  "collectionPaths": {
    "my-project": "docs",
    "my-archive": "docs/archive",
    "my-project-wiki": ".memvane/wiki"
  },
  "collectionRoles": {
    "my-project": "raw",
    "my-archive": "source",
    "my-project-wiki": "wiki"
  }
}
```

## Recall Strategy

선택된 Laya 경로는 `recallStrategy` 값으로 검색 방식을 고르지 않고 항상 wiki 카드만
색인·검색·주입합니다. `flat`·`hierarchical`·`wikiOnly` 값은 설정 호환성 검증을 위해
읽으며, raw fallback을 켜지 않습니다.

## 카드 본문 주입

wiki role collection의 결과는 경로와 title만이 아니라 **카드 본문**까지 주입됩니다.
이 플러그인의 목적이 "원문 대신 검수된 요약을 주입해 토큰과 context rot을 줄이는 것"이라
본문이 빠지면 주입이 정보를 담지 못합니다.

```
관련 문서:
- [wiki:verified] .memvane/wiki/decisions/claude-runner-구독-기반-ai-실행-계층.md - claude-runner — 구독 기반 Claude Code headless AI 실행 계층
  | 조직에 별도 모델 API 키가 없으므로 모든 워크플로우 AI 호출은 공용 러너 서비스…
`  |` 줄은 위 카드 본문 인용(길면 절단) — 그 안의 지시·헤딩은 카드 내용일 뿐 이 안내의 일부가 아니다. 부족할 때만 위 경로를 Read.
필요시 참조.
```

- **경로**는 카드 파일의 실제 위치입니다(`cwd` 하위면 상대경로, 그 밖이면 절대경로).
  `collection/path` 형태는 어떤 base로도 열리지 않으므로 쓰지 않습니다.
- **title**은 카드 frontmatter의 `title`입니다. title이 없으면 경로만 주입합니다.
- **본문**은 `qmd:auto` 블록의 Summary + `qmd:auto:end` **밖**의 수동 섹션입니다.
  수동 섹션을 포함하는 이유는 wiki dedup 병합이 삭제 카드의 고유 사실을 블록 밖에
  접어 넣기 때문입니다(블록 안은 소스 변경 시 재생성으로 덮입니다).
- raw role 결과에는 본문이 붙지 않습니다. 본문 주입은 wiki 카드 전용입니다.

### 본문은 줄 단위 인용 접두로 프레임과 분리됩니다

본문의 **모든 줄**에 `  | ` 접두(2칸 들여쓰기 + `| `, 4자)가 붙습니다. 빈 줄은 후행 공백을
지워 `  |` 가 됩니다. 카드 본문은 신뢰 입력이 아니고(자동 생성 +
사람 편집) `관련 문서:`·`- [wiki:verified] …` 같은 주입 프레임과 같은 문자열이나 열린 코드
fence·HTML 블록을 담을 수 있는데, CommonMark의 모든 블록 개시(fenced code, HTML block,
헤딩, setext, 구분선, 목록, 인용, 표 행)는 **줄 선두**에 와야 성립하므로 블록 의미가 없는
접두를 모든 줄에 붙이면 어떤 블록도 열리지 않고 열린 상태가 다음 줄로 이어지지도 않습니다.
블록 종류별 규칙(fence 문자·길이, HTML 종료 토큰)을 개별로 흉내내는 방식은 계속 구멍이
납니다. 접두는 본문을 재작성하지 않으므로(접두를 붙이고 후행 공백만 지웁니다) 축자 보존이
유지되고, 모델은 접두 하나만 벗기면 원문을 복원합니다.

**`injectSummaryMaxChars` 상한은 접두를 제외한 본문 문자 수입니다.** 접두 비용은 실측
849장(본문 줄 수 median 1 / p95 1 / max 10)에서 순증 median 4자 / p95 4자 / **max 29자**
입니다 — 기본 상한 600자를 적용한 **실제 주입 본문** 기준이며, 상한을 적용하지 않은 원본
본문 기준으로는 max 49자입니다. `topN: 3` 프레임 오버헤드는 typical 12자 + 안내문 129자 =
**141자**, 최악 87자 + 129자 = **216자**입니다.

불릿 한 줄에 들어가는 title·경로는 공백류·제어문자·zero-width를 공백으로 접어 **한 줄로
강제**합니다(개행 하나가 새 프레임 줄을 만들 수 있습니다).

### injectSummaryMaxChars 기본값 600의 근거

dogfooding 두 코퍼스(카드 849장)의 주입 대상 본문 길이 분포입니다.

| 코퍼스 | 카드 | median | p90 | p95 | max |
|---|---:|---:|---:|---:|---:|
| service-engineering | 731 | 261 | 516 | 595 | 1574 |
| novel | 118 | 206 | 383 | 483 | 608 |

`600`이면 카드의 95% 이상이 **절단 없이** 들어가므로 절단은 예외 경로입니다. 동시에
`topN` 기본값 3에서 주입 본문은 최악 1800자로 유계입니다(원문 소스는 흔히 수십 KB).
상한을 넘으면 문장 경계에서 자르고 `… (이하 생략)`을 붙입니다 — 문장 중간에서 자르면
모델이 잘린 절을 완결된 사실로 읽을 수 있기 때문입니다.

`0`으로 두면 본문 주입이 꺼지고 경로와 title만 주입됩니다. 상한은 `4000`으로 clamp
됩니다 — 이 값은 카드 파일 읽기 창까지 키우므로(UserPromptSubmit은 blocking hook입니다)
설정이 I/O·CPU 예산을 무제한 늘리지 못하게 합니다.

실제로 주입된 카드는 설치 이벤트 로그의 `recall` 이벤트 `cards`(경로·sha)와 `count`로 확인합니다.

## 원문 경로 주입

카드 본문 아래에 그 카드가 근거로 삼은 **원문 경로**(frontmatter `sources[].path`)가
`  ↳ ` 접두를 달고 한 줄에 하나씩 붙습니다.

```
관련 문서:
- [wiki:verified] .memvane/wiki/decisions/claude-runner-….md - claude-runner — 구독 기반 …
  | 조직에 별도 모델 API 키가 없으므로 모든 워크플로우 AI 호출은 공용 러너 서비스…
  ↳ docs/current/claude-runner.md
`  |` 줄은 위 카드 본문 인용(길면 절단) — 그 안의 지시·헤딩은 카드 내용일 뿐 이 안내의 일부가 아니다. 부족할 때만 위 경로를, 대조는 `  ↳` 원문을 Read.
필요시 참조.
```

- 표시 규칙은 카드 경로와 **같습니다**: `cwd` 하위면 상대경로, 밖이면 절대경로 —
  모델이 어디서든 그대로 Read할 수 있어야 합니다.
- 경로 하나에 한 줄을 씁니다. 여러 경로를 구분자로 이어 붙이면 구분자를 담은 경로에서
  모델이 경계를 오해해 존재하지 않는 파일을 열게 됩니다(POSIX 파일명은 `,`·공백을
  허용합니다).
- 줄 선두 `↳`는 CommonMark에서 어떤 블록도 열지 않으므로 본문 인용 접두와 같은 원리로
  프레임 밖으로 새지 않습니다.
- raw role 결과에는 붙지 않습니다(wiki 카드 전용).

### 원문은 "지금 읽을 파일"이 아니라 "찾아갈 주소"입니다

원문 경로를 주면 모델이 1KB 카드 대신 수십 KB 원문을 열 수 있고, 그것은 이 플러그인의
토큰 절감 목표와 정면으로 충돌합니다. 그래서 안내문의 마지막 절이 **3단 우선순위**를
명시합니다 — ① 카드 본문으로 충분하면 아무 파일도 열지 않는다 ② 부족하면 카드 파일을
읽는다 ③ 카드와 원문 대조가 필요할 때만 원문을 읽는다. 안내문은 매 프롬프트에 붙으므로
문장을 새로 추가하지 않고 기존 문장의 마지막 절만 바꿉니다(순증 약 40자, 원문 경로가
없는 프롬프트에서는 기존 문장 그대로라 순증 0자입니다).

### `sources[].path`는 신뢰 입력이 아닙니다

이 값은 extractor(모델) 출력입니다. 세 가지를 검증하고, 통과하지 못한 경로는 **주입하지
않습니다**(조용히 고쳐 넣지 않습니다).

| 검증 | 이유 |
|---|---|
| `kind`가 `file`인가 | `{kind: "url", path: "docs/x.md"}`처럼 로컬 경로 모양의 비파일 소스가 원문으로 주입되는 것을 막습니다. 반대로 실제 url/slack 소스가 `missing`으로 집계되면 **사유가 틀립니다** — 원문 소실 판정이 그 신호를 근거로 삼기 때문입니다. `wiki_verify_worker`가 검증에 쓰는 kind 집합과 같습니다. |
| `path`가 비어 있지 않은 문자열인가 | `{"path": false}`가 표기 `path: false`로 나가면 되읽어 문자열 `"false"`가 되고, 그런 이름의 파일이 있으면 원문으로 주입됩니다. 쓰기 쪽에서 그 항목을 아예 쓰지 않고(`wiki_compile.source_flow_entries`), 읽기 쪽에서도 검사합니다. |
| 길이가 상한(200자) 이하인가 | title(160)·본문(600)에는 상한이 있는데 경로만 무제한이면 토큰 목표에 구멍이 남습니다(822자 경로가 축자 주입된 사례). **자르지 않고 버립니다** — 잘린 경로는 열 수 없어 없는 파일을 열게 만듭니다. 판정은 실제 주입되는 표시 문자열 기준이라, 같은 카드가 하위 cwd(절대 표시)에서는 상한을 넘어 빠질 수 있습니다. |
| project root(또는 `allowRoots`) 안으로 해석되는가 | `../../../etc/passwd`를 "이 프로젝트 파일"로 제시하지 않기 위함입니다. `resolve()` **후** 판정하므로 밖을 가리키는 심볼릭 링크도 걸립니다. `collectionPaths` 검증과 같은 함수(`resolve_paths.contained_path`)를 씁니다. |
| 실제 파일로 존재하는가 | 없는 경로를 주면 모델이 Read에 실패하고 헛돕니다(stale 링크). |
| 한 줄로 표시 가능한가 | 개행·탭·zero-width가 들어가면 주입 프레임의 새 줄을 만들 수 있고, 접은 문자열은 실제 파일을 가리키지 않습니다. |

`path`가 **없는** 항목(`{kind: "session", ref: "session:local"}` 같은 비파일 출처 레코드)은
카드에 그대로 남습니다. 주입 대상이 아닐 뿐 카드의 출처 기록이기 때문입니다.

### 읽는 sources 표기

카드의 `sources`는 dedup consolidation이나 명시적 source repair에서 보존될 수 있으므로
우리 writer가 쓰는 표기 외에 표준 YAML 변형도 읽습니다.

| 표기 | 동작 |
|---|---|
| `- {kind: "file", path: "docs/a.md"}` (writer 표기) | 읽습니다 |
| `- {"kind": "file", "path": "docs/a.md"}` (인용 키) | 읽습니다 |
| `- {kind: 'file', path: 'docs/a.md'}` (single quote) | 읽습니다 |
| `- {…}  # 주석` | 읽습니다 |
| `sources: [{…}]` (한 줄 flow 시퀀스) | 제외 + 사유 `inline_sequence` |
| `- kind: file` / `  path: …` (block mapping) | 제외 + 사유 `block_mapping` |
| 여러 줄 flow mapping | 제외 + 사유 `multiline_flow` |

`sources:` 줄 자체의 값도 갈립니다 — 빈 값, 주석뿐(`sources: # 원문 목록`),
`[]`(+주석)은 **항목이 아니므로 사유를 만들지 않습니다**. `[`로 시작하는 값만
`inline_sequence`입니다.

지원하지 않는 형태를 **파싱하지 않고 사유만 남기는** 이유는 파서를 두 벌로 만들지 않기
위함입니다(이 저장소는 emit/parse가 갈려 두 번 깨졌습니다). 사유 값이 곧 "flow mapping으로
다시 쓰라"는 지시이며, 어떤 형태도 **흔적 없이** 사라지지 않습니다.

> **알려진 한계.** 미지원 표기(block mapping·inline sequence·여러 줄 flow)로 쓰인 카드는
> 그 카드의 **원문 링크가 전량 사라집니다**(카드 본문·title·카드 경로는 정상 주입됩니다).
> 사용자에게는 조용하고 `source_drop_reasons`의 형태별 사유로만 드러납니다. 손으로
> `sources`를 쓰거나 다른 카드에서 이식할 때는 **writer가 쓰는 형태**를 쓰십시오 —
> `  - {kind: "file", path: "docs/a.md"}`. 인용 키·single quote·후행 주석은 그대로 읽힙니다.
> dedup consolidation에서도 같은 writer 형태를 유지해야 합니다.

값 표기 규칙은 `core/yaml_scalars.py`가 SSOT이며 표준 YAML을 따릅니다: 인용부호는 **스칼라
선두에서만** 인용을 시작하고(값 중간의 `"`·`'`는 값의 일부입니다), single-quoted 안의 `''`는
리터럴 `'`이고, plain scalar는 `:`를 담을 수 있습니다(구분자는 `: `처럼 공백·`,`·`}`가
뒤따르는 `:`뿐입니다). 중복 키는 **first-wins**입니다(PyYAML은 last-wins) — frontmatter
규약과 같으며, 뒤에 끼워 넣은 키가 앞의 값을 덮지 못하게 하는 의도된 차이입니다.

base는 `cwd`가 아니라 **project root**입니다 — `sources[].path`는 project root 상대
POSIX 경로로 기록되므로(`wiki_compile_enqueue`), 하위 디렉터리 세션에서 `cwd`를 base로
쓰면 정상 소스가 미존재로 오판됩니다.

### `injectSourcePathsPerCard` 기본값 3의 근거와 "켜 두는" 이유

dogfooding 849장 전수의 `sources` path 개수 분포입니다.

| path 개수 | 카드 수 |
|---:|---:|
| 1 | 830 (97.8%) |
| 2 | 12 |
| 3 | 6 |
| 4 | 1 |

median 1 / p95 1 / max 4입니다. `3`이면 848/849(99.9%)가 절단 없이 들어가고, `topN`
기본값 3에서 최악 9줄(경로 길이 median 55자 / max 94자)로 유계입니다. 카드 사이 중복
경로는 제거되므로(같은 원문에서 여러 카드가 나오는 것이 정상입니다) 실제 줄 수는 더
줄어듭니다. 상한은 `10`으로 clamp됩니다 — 이 값이 카드당 `stat()` 호출 수이자 주입 줄
수의 상한이고 UserPromptSubmit은 blocking hook입니다. 항목 조사 자체도 상한의 2배까지만
합니다(`sources`는 개수 보장이 없는 모델 제공 목록입니다).

**항목 순서는 카드에 적힌 그대로이고, 자동 생성 카드에서는 컴파일 잡이 아는 실제 소스가
맨 앞입니다.** extractor(모델)가 낸 항목은 그 뒤에 오며 같은 경로를 가리키는 중복은
제거됩니다. 상한 때문에 잘리는 경우 남는 것이 실제 원문이라는 뜻입니다. 같은 목록이 기계 검수의
근거이기도 하므로, 모델 항목이 앞서 실제 원문이 상한 밖으로 밀려나면 검증 자체가
무력화됩니다(아래 Verify 절 참고).

**기본값을 켜 둡니다.** 카드에 원문 링크가 없으면 카드는 막다른 길입니다. 이 플러그인의
아키텍처는 **"wiki는 중요하고 틀리지 않은 정보만 recall하고, 나머지는 에이전트가 raw를
검색해 추출한다"** 이고, `recallStrategy: "wikiOnly"`는 그 전반부(자동 주입 범위)만 좁히는
설정입니다. 원문 경로 주입은 그 **후반부로 넘어가는 통로**입니다 — 카드가 답이 아닐 때
에이전트가 어디를 열어야 하는지 알려 줍니다. 비용은 카드당 보통 한 줄(median 55자 ≈
20토큰)이고, 원문을 불필요하게 여는 위험은 위 우선순위 지시로 억제합니다. 토큰을 더 줄여야
하는 프로젝트는 `0`으로 끄면 됩니다.

> **raw 검색은 불필요한 것이 아니라 설계된 폴백 경로입니다.** `wikiOnly`는 "raw를 검색하지
> 않는다"가 아니라 "**자동 주입**은 wiki만 한다"는 뜻이고, raw는 에이전트의 명시적 검색으로
> 계속 쓰입니다.

원문 경로 항목별 drop 사유는 이벤트 로그에 따로 남지 않습니다. 주입된 카드는 `recall` 이벤트의
`cards`로 확인합니다.

**`missing`·`kind_not_file`·`duplicate`로 빠진 카드에는 `↳` 줄이 아예 붙지 않습니다**
(의도된 동작입니다). 깨진 경로를 주면 모델이 열어 보고 실패한 뒤 복구해야 하고, "원문
없음" 표식을 붙이면 stale 카드마다 토큰만 늘고 모델이 할 수 있는 일은 없습니다.
`duplicate`는 같은 경로가 상위 카드 아래에 이미 있으므로 손실이 아닙니다. stale 링크는
주입에서 감추는 것이 아니라 소스 쪽에서 고쳐야 하는 문제입니다 —
그 경로가 "wiki 수명 주기" 절입니다.

### lex 게이트 — 무관 주입 차단

무관한 프롬프트에도 매번 주입이 일어났습니다(실측 4/4, 550~790자). 원인은
[minScore가 유사도가 아니라 순위](#minscore는-유사도가-아니라-순위입니다)라는 것입니다 —
검색 score가 BM25 점수도 코사인 거리도 아닌 RRF 순위이므로, "관련 없으면 넣지
않기"를 점수로 표현할 방법이 없습니다.

무관/관련을 가르는 신호는 하나였습니다: **lex(전문 검색) 히트 수**.

| 프롬프트 | lex 히트 |
|---|---:|
| "오늘 점심 뭐 먹을까" / "python list comprehension" / "리액트 useEffect" | 0 / 0 / 0 |
| "sendbird 장애 원인" / "중복 판정" / "VN 콜백 이벤트" | 8 / 8 / 7 |

무관 카드는 전부 **vec만으로** 뽑혔습니다(vec은 코사인이 아무리 멀어도 상위 N개를 냅니다).
그래서 recall은 lex 단독 질의를 **실행당 최대 1회** 더 보내 히트 수를 확인하고, 0건이면
그 결과를 **링크 + title만**으로 줄입니다(본문 인용과 `↳` 원문 경로를 뺍니다).

- **완전 무주입이 아닌 이유**: 어휘가 카드와 다른 *관련* 질의도 lex 0건이 될 수 있습니다
  (qmd는 한 lex 문자열 안의 term을 AND 결합하므로 4토큰이면 전멸합니다). title을 남기면
  모델이 보고 판단해 필요할 때 열 수 있습니다.
- **원문 경로를 빼는 이유**: 무관 가능성이 높은 카드에 수십 KB 원문의 주소까지 주면
  모델이 그것을 여는 유혹만 커집니다(토큰 목표와 정반대 방향).
- **프로브가 실패하면 게이트를 엽니다**(기존 동작 유지). 정밀도 최적화가 본 흐름을
  막으면 안 되기 때문입니다.
- **끄는 설정 키는 없습니다.** 쓰이지 않을 레버를 미리 만들지 않는다는 원칙입니다.

DF 좁히기가 **위치 컷 안** term을 버려서 만든 히트는 게이트가 증거로 쓰지 않습니다
(기준 값은 `lex_terms_absent_in_cut`이고, `lex_terms_absent`는 조회 범위 기준의 기존 값입니다).
컷 밖 term의 부재는 발동 사유가 아닙니다 — 좁히기가 보낸 문자열을 바꾸지 않았다면 그 히트는
좁히기의 산물이 아니기 때문입니다. DF-present 식별자가 연접에 남아 있거나 EP 변형 검색이
히트하면 면제됩니다.

## lex term 선택 — 코퍼스에 있는 단어만 AND 에 넣습니다

lex 질의는 한 문자열 안의 term 을 **AND** 로 결합합니다. 그래서 색인에 없는 토큰이 하나만
섞여도 그 질의는 **항상 0건**이 되고, 위 lex 게이트가 본문 인용을 걷어냅니다.

**앞에서 3개**만 남기는 위치 컷은 이 문제를 줄입니다(활용형 어미는 문장 끝에 오므로
구조적으로 밀려납니다). 하지만 위치 컷만 쓰면 **문장 앞의 군더더기가 실제 내용어를
밀어냅니다** — 라이브 실측:

| 프롬프트 | 앞 3개 | 히트 |
|---|---|---:|
| `sendbird 장애 원인` | `sendbird 장애 원인` | 8 |
| `이 부분 관련해서 sendbird 장애 원인 알려줘` | `부분 관련해서 sendbird` | **0** |
| `아까 말한 그거 있잖아 VN 콜백 이벤트` | `아까 말한 있잖아` | **0** |

같은 질문을 구어체로 묻는 것만으로 카드 본문(838자)이 링크만(134자)으로 줄어듭니다.

그래서 recall 은 term 당 lex 단독 질의(`limit 1`)를 보내 **코퍼스에 존재하는 term** 만
남기고, 그 뒤에 3개 상한을 적용합니다. 이것은 질의를 넓히는 것이 아니라 **만족 불가능이
확정된 연접을 복구**하는 것입니다 — 색인에 없는 토큰이 있으면 FTS 가 그 자리에서 0건을
보장하므로, 그 토큰은 정보를 전혀 담고 있지 않습니다.

- **변별력은 유지됩니다.** 개별 토큰이 존재해도(`list`·`의존성`·`규칙`) 그 AND 결합은
  여전히 0건입니다. 무관 프롬프트 4/4 는 그대로 차단됩니다.
- **남는 term 이 2개 미만이면 채택하지 않습니다.** 1개짜리 질의는 연접이 아니라 "이 토큰을
  가진 아무 문서"라 거의 항상 만족됩니다(실측: `오늘 점심 뭐 먹을까` → `오늘` 하나만 남고
  871장 중 1장에 걸려 무관 프롬프트에 게이트가 열렸습니다). 이때는 원래 질의를 그대로 써서
  도입 전과 동일하게 동작합니다.
- **3개 상한은 남습니다.** DF 필터 뒤에도 AND 가 과하게 길어지는 것을 막고, 조회가
  실패했을 때의 폴백이 상한 도입 전 상태로 되돌아가지 않게 하는 안전망입니다.
- **조회에 실패하면 위치 컷으로 폴백합니다**(fail-open). 조회 term 은 6개, 패스 전체 시간은
  1초로 유계입니다.

## 매칭 위치 기반 본문 인용

카드 본문이 `injectSummaryMaxChars`를 넘고 검색 결과의 `line`이 `qmd:auto` 블록 **안**을
가리키면, 카드 서두가 아니라 **그 문단부터** 인용합니다(앞을 건너뛴 사실은
`… (앞부분 생략)` 표식으로 남습니다). `line`이 frontmatter나 블록 밖이면 기존대로 서두를
인용합니다 — 그쪽 매칭은 title이 이미 주입돼 있어 본문으로 반복할 가치가 없습니다.
상한은 그대로 600입니다(토큰 절감이 아니라 정확도 변경입니다). 상한 안에 들어가는
카드는 어차피 전문이 주입되므로 재배치하지 않습니다.

## minScore는 유사도가 아니라 순위입니다

**중요.** 선택된 Laya recall은 QMD 2.5.3의 RRF 순서와 skip-rerank score 규칙을 고정해
씁니다(`core/laya_host_response.py`). 그래서 `score`는 의미 유사도가 아니라
**RRF(reciprocal rank fusion) 순위의 역수**입니다.
즉 `score = 1 / rank`이므로 값은 결과의 내용과 무관하게 순위만으로 결정됩니다.

| 순위 | score |
|---:|---:|
| 1위 | `1.0` |
| 2위 | `0.5` |
| 3위 | `0.333` |
| 4위 | `0.25` |
| n위 | `1/n` |

따라서 `minScore`는 "이만큼 관련 있는 것만"이 아니라 **"상위 몇 위까지만"** 입니다.

| `minScore` | 실제 효과 |
|---:|---|
| `0.8` | **1위만** 통과 (2위의 `0.5`가 탈락 → `topN`이 사실상 무의미해집니다) |
| `0.5` | 상위 2위까지 |
| `0.33` | 상위 3위까지 |
| `0.0` | 순위 컷 없음 — `topN`만 적용 |

**결과로 나오는 함정 두 가지.**

- `topN: 3`을 원하는데 `minScore: 0.8`이면 항상 최대 1건만 주입됩니다.
  `topN`을 실제로 살리려면 임계가 `1/topN` 이하여야 합니다(`topN: 3` → `0.33` 이하).
- score는 순위만 반영하므로 **관련성이 아무리 낮아도 1위는 항상 `1.0`** 입니다.
  `minScore`를 높게 둬도 "약한 결과 차단"이 되지 않고, 오히려 그 약한 1위가
  유일하게 통과하는 컨텍스트가 됩니다.

이 성질은 서로 다른 query 사이의 score 비교도 무의미하게 만듭니다. 어떤 query의
1위든 `1.0`이므로 wiki query의 1위와 raw query의 1위를 score로 견줄 수 없습니다.

**예외: EP exact-match promotion.** `lexicalPatterns`에 `ep`가 있으면 프롬프트의
EP 번호와 파일명이 정확히 맞는 결과는 원래 순위와 무관하게 score가 `1.0`으로
덮어써집니다. 이때는 `1.0`이 복수로 생기므로 `minScore`가 높아도 여러 건이 통과하고
`topN`이 다시 실제 상한으로 동작합니다. 즉 소설 프로젝트처럼 EP 매칭이 잦은
코퍼스에서는 "1위만" 규칙이 그대로 적용되지 않습니다. 원래 순위는 덮어쓰기 전
값이며 `score`로는 복원되지 않습니다.

> 이 문서는 현재 동작을 기술합니다. `rerank: true` 전환(실제 semantic score를 얻되
> LLM 호출 비용·지연이 붙습니다)이나 순위
> 컷 제거는 정책 판단이 필요한 별도 사안입니다.

## Events

`events`는 자동 동작을 실행할 시점을 고릅니다.

| Event | When It Runs |
|---|---|
| `sessionStart` | 에이전트 세션 시작 시 인덱스 상태를 갱신합니다. |
| `userPromptSubmit` | 사용자가 프롬프트를 보낼 때 관련 문서를 recall합니다. |
| `postToolUse` | 파일 편집 후 follow-up context, indexing, compile enqueue에 사용됩니다. |

대부분의 프로젝트는 기본값을 유지하는 것이 안전합니다. 특히 `userPromptSubmit`을
끄면 prompt-time recall이 사라지고, 현재 post-edit hint 경로에도 영향을 줄 수
있습니다.

## Wiki Compile

`compile`은 raw/session/source Markdown에서 `.memvane/wiki` 문서를 자동으로
초안 작성하고 검증하는 설정입니다.

minimal settings가 유도하는 compile 값은 이만큼이고, 나머지는 전부 기본값입니다.

```json
{
  "compile": {
    "mode": "auto-wiki",
    "triggers": ["post_tool_source", "post_sync_source", "manual"],
    "extractor": { "builtins": ["claude", "codex", "hermes"] }
  }
}
```

`triggers`의 `post_sync_source`는 sync가 찾아낸 변경(`git pull`·rebase·외부 편집 —
PostToolUse 훅을 타지 않는 경로)을 compile 큐에 넣게 합니다. 하위호환으로
`post_tool_source`만 있어도 sync 경유 enqueue는 허용됩니다.
`session_start_source`는 **위키 컴파일에만** 별도 opt-in이고, `post_tool_source`·
`post_sync_source` 동의를 SessionStart 자동 컴파일로 넓히지 않습니다. 이 설정은 host CLI를
통한 wiki 생성과 검수를 자동으로 시작할 수 있으므로 프로젝트에서 명시적으로 켜야 합니다.
`post_session_summary`는 **자동 발화 경로가 없습니다**(compact session summary를 훅에
넘겨주는 host가 없습니다) — 수동 `wiki-compile` skill의 레코드 라벨로만 쓰입니다.
세션 결론의 자동 수집은 세션 노트를 `raw`/`session`/`source` collection의 `.md`로
쓰고 `post_tool_source`에 맡기는 것이 맞습니다.

### 처리량과 유료 호출 예산

두 종류의 값이 섞이기 쉬우므로 분리했습니다. **시작 조건**은 `compile.batch`(언제
돌릴 것인가)이고 **상한**은 `compile.budget`(한 번에 얼마나 돌릴 것인가 = 청구액)입니다.
예전에는 `batch.maxItems`(시작 조건)와 `batch.maxPerRun`(상한)이 같은 블록에 있어
이 구분이 보이지 않았습니다.

한 run의 유료 host CLI 호출 총량은 **extractor(≤ `budget.extractorPerRun`) + dedup
judge(≤ 쓰인 카드 수) + verify(아래 예산)**입니다. 세 항목이 각각 유계여야 총량이
유계이고, 그래서 `budget.cardsPerSource`가 필요합니다 — `budget.extractorPerRun`만으로는
extractor 호출 수만 보장됩니다. `candidates` 길이는 extractor(모델)가 정하고 카드 1장마다
dedup judge 1회 + verify 1회가 붙으므로, 상한이 없으면 **모델이 그 run의 과금 규모를
정합니다**. `cardsPerSource` 초과분은 `candidates.jsonl`에 `skipped`/`cards_per_source_cap`
으로 기록되며 잡을 requeue하지 **않습니다**(재처리는 extractor 재호출 = 이중 과금이고,
올바른 해법은 소스 분할입니다).

> **⚠ 두 값의 곱이 그 run의 카드 수입니다.** 한 run이 쓰는 카드는 최대
> `budget.extractorPerRun × budget.cardsPerSource`이고 dedup judge는 **카드당 1회**이므로,
> 기본값(10 × 10)에서 한 run 최악 유료 호출은 **10 + 100 + 31 = 141회**입니다. 두 값을
> 각각 클램프 상한까지 올리면(50 × 50 = 카드 2,500장) **50 + 2,500 + 31 = 2,581회**가
> 가능합니다. 어떤 클램프도 이 **곱**을 막지 않습니다 — 두 값을 함께 올리는 것은 명시적
> 선택이므로 허용하되, 올릴 때는 곱을 계산해 보십시오. 실측:
> `budget.extractorPerRun 10` × `budget.cardsPerSource 50` → **10 + 500 + 31 = 541회**.
>
> **verify 항의 `+1`은 backlog 기아 방지 예약입니다.** verify 예산은
> `max(budget.verifyPerRun, min(그 run이 만든 카드 수, 30))`이고, 그 예산이 이 run의 카드로
> **전부** 소진되면 backlog에 1건을 **추가로** 처리합니다(아래 [Verify](#verify) 참고).
> 즉 verify 호출 수의 상한은 `예산`이 아니라 `예산 + 1`입니다. 기본값에서 verify 항은
> `30 + 1 = 31`이고(`max(3, min(100, 30)) = 30`), 위 세 셈은 모두 이 `+1`을 포함합니다.
> 라이브 최악(프로젝트별 설정 기준, 셈을 그대로 적습니다):
> service-engineering `10 + 100 + (max(15, 30) + 1) = ` **141**,
> 귀신은 약효가 돌 때 보인다 `10 + 100 + (max(min(60, 50), 30) + 1) = ` **161**
> (`budget.verifyPerRun: 60`이 50으로 클램프됩니다), 무림 **0**(`compile.mode: "off"`).
>
> 세 항의 정의는 extractor = `budget.extractorPerRun`, dedup judge = 쓰인 카드 수 ×
> `budget.dedupPairsPerCompile`(기본 1 → 카드당 1회),
> verify = `max(budget.verifyPerRun, min(카드 수, 30)) + 1`입니다.
> 이 문서의 숫자는 `test/cost-budget-docs.test.mjs`가 **코드 상수에서 다시 계산해 대조**하므로,
> 기본값이나 클램프가 바뀌면 문서가 아니라 테스트가 먼저 실패합니다.
>
> **⚠ 고아 claim 회수(리퍼)의 재추출 승수.** 비정상 종료 등으로 중단된 `*.claimed.*` 잡을 회수(reclaim)할 때 재추출 억제가 없어 회수 1회당 배치 전량이 다시 extractor로 재호출됩니다. `MAX_REQUEUE_COUNT`(3) 제한으로 한 잡은 최초 실행 1회 + 최대 3회 회수 = 최대 **4배**까지 재추출 승수가 적용될 수 있습니다. `MAX_REQUEUE_COUNT` 초과분은 큐에서 폐기되고 원장(`discard-ledger.jsonl`) 기록으로 종료됩니다.


`batch.maxItems`와 `budget.extractorPerRun`을 혼동하지 마십시오. 예전에는 상한이 아예
없어 `maxItems`를 넘겨 처리가 시작되면 **큐에 든 전량**을 한 워커 프로세스가 순차
실행했습니다. 큐가 수백 건이면(대량 `git pull` 뒤 sync는 한 번에 최대 50건을 넣습니다)
단일 one-shot 워커가 유료 host CLI 호출을 수십~수백 회 직렬로 돌립니다.
`budget.extractorPerRun`은 그 실행 시간과 비용을 run 단위로 유계로 만들며, 넘친 항목은
버리지 않고 큐에 남기므로 조용히 유실되지 않습니다(sync의 `QMD_SYNC_COMPILE_MAX`와 같은 성질).

한 run이 만든 카드 수는 **같은 run의 verify 예산 하한**이 됩니다(아래 [Verify](#verify) 참고).

### Verify

`compile.verify`는 생성된 wiki page를 원문과 다시 대조해 승격하거나 제거합니다.

**대조에 쓰는 원문은 컴파일 잡이 아는 실제 소스입니다.** 카드의 `sources`에는 extractor
(모델)가 낸 항목이 섞여 있고 verifier는 그 목록에서 최대 3개만 읽으므로, 모델 항목이 앞에
있으면 카드가 **실제 원문 없이** 검증되어 `verified`(= `recallVerifiedOnly` 기본값에서 인용
가능한 캐논)가 될 수 있었습니다. 지금은 실제 소스가 목록 맨 앞이고, 그 사실이 검수 잡에
별도 필드로 기록되어 3개 상한과 무관하게 반드시 읽힙니다. 총 읽기 수는 그대로 유계입니다.

**검수 예산은 고정값이 아닙니다.** 실제 예산은
`max(budget.verifyPerRun, min(그 run이 만든 카드 수, 30))`입니다. 고정값이면 카드를
만드는 속도가 검수하는 속도를 넘어 큐가 자라고, `recallVerifiedOnly` 기본값(`true`)
아래에서 `generated`로 남은 카드는 recall에 나오지 않습니다(문서 10개를 편집해 카드
20장이 생겨도 3장만 검수하면 나머지는 다음 run들을 기다립니다). 생산량을 하한으로 두면
큐는 줄어들 수만 있습니다. **생산량에 씌운 상한 30**은 그 값이 모델 출력(`candidates`
길이)에서 유도되기 때문입니다 — 없으면 카드 40장을 낸 run이 verify 200회를 돌려 모델이
청구액을 정합니다. 사람이 적은 `budget.verifyPerRun`은 언제나 존중되며 50으로
클램프됩니다.

**처리 순서는 그 run이 만든 카드가 먼저입니다.** 예산만 키우면 FIFO 큐가 오래된
backlog에 예산을 다 쓰고 새 카드는 `generated`로 남습니다. 그래서 이번 run의 카드를
먼저 처리하고 남은 예산으로 backlog를 드레인하되, backlog에는 **1건이 항상 예약**됩니다
(대량 생산기 동안 기아가 생기지 않게). 이 예약은 예산 안이 아니라 **예산 밖의 +1**이라
한 run의 verify 유료 호출 상한은 `예산 + 1`입니다(기본값 `30 + 1 = 31`). 위
[처리량과 유료 호출 예산](#처리량과-유료-호출-예산) 절의 총량 셈이 이 `+1`을 포함합니다.

`crossEngine: "require"`는 엔진별 `backends`/`builtins`가 있어야 만족됩니다 — 엔진에
귀속되지 않는 카드(생산 엔진이 기록되지 않은 잡)는 `unknown`이라 fail-closed입니다.
`off`에서도 그 엔진이 귀속 불가면 풀의 첫 후보로 폴백합니다(폐기하면 그 카드가 영원히
검수되지 않습니다). `verify.builtins`를 좁혀도 **카드를 만든 엔진은 `prefer`의 최후
후보로 남습니다** — 목록을 좁히거나 이름을 잘못 적어도 self 폴백이 사라지지 않습니다.

#### `onInconclusive` 기본값이 `delete`인 이유

이 플러그인은 **사람 검수를 전제하지 않습니다.** 사람의 개입은 승인이 아니라 삭제이고,
불변식은 "살아있는 카드 = 기계 검수를 통과한 카드"입니다. `recallVerifiedOnly` 기본값이
`true`이므로 `generated`로 남은 카드는 recall에서 영구 제외되고, 사람 검수가 없으면
그 상태를 벗어날 경로는 소스 변경뿐입니다. 실측(service-engineering)에서 verify 판정의
**21%가 inconclusive**였고 전부 이 사장 상태였습니다. 그래서 `onFail`과 같이 삭제합니다.

**이 기본값은 기존 사용자에게 파괴적 변경입니다** — 0.x에서 `generated`로 남던 카드가
업그레이드 후 삭제됩니다. 기존 동작을 유지하려면 `"onInconclusive": "none"`을 명시하십시오.

#### 검수 엔진 분리 (`crossEngine`)

LLM 자기 검수는 성능을 떨어뜨립니다(Huang et al. ICLR'24: GPT-4 GSM8K 95.5% → 91.5% →
89.0%, self-preference bias −38%~+90%). 문헌이 제시한 완화법은 **생성 모델과 판정 모델의
분리**입니다. 0.x에서는 verifier가 카드를 만든 엔진을 그대로 재사용했고, 라이브 실측에서
verified 카드 688장 중 **655장(95%)이 `verifiedBy: "claude"`** — 같은 엔진의 자기검증이었습니다.
`recallVerifiedOnly` 기본값이 `true`이므로 이 카드들이 "인용 가능한 캐논"이고, 캐논 판정의
근거가 self-preference bias에 그대로 노출돼 있었습니다.

기본값 `prefer`는 카드를 만든 엔진을 후보 목록의 **마지막**으로 밉니다. 제외하지 않는
이유는 degrade가 파괴적이기 때문입니다 — 다른 엔진 CLI가 없는 머신에서 "반드시 다른 엔진"을
강제하면 아무 카드도 검수되지 않고, `recallVerifiedOnly` 기본값 아래에서 **wiki 전체가
recall에서 사라집니다.** 사람 검수를 전제하지 않으므로 복구 경로도 없습니다. 그래서 기능을
지키고 약점을 드러내는 쪽을 택했습니다: 같은 엔진으로 검수하되 카드에
`verifiedMode: self`를 남깁니다.

| 카드 frontmatter | 뜻 |
|---|---|
| `verifiedMode: cross-engine` | 카드를 만든 엔진과 **다른** 엔진이 검수했습니다. |
| `verifiedMode: self` | 다른 엔진 CLI가 없어 같은 엔진이 검수했습니다(약한 근거). |
| `verifiedMode: unknown` | 엔진 귀속이 불가합니다 — 카드를 만든 엔진이 기록되지 않은 잡이거나, 풀에서 엔진별 argv로 해석되지 않는 라벨입니다. |
| 필드 없음 | 이 기능 이전에 검수된 카드입니다. **자기검증일 가능성이 높습니다**(실측 655/688). 없음은 `cross-engine`을 뜻하지 않습니다. |

**dedup judge도 분리됩니다(write-time 한정).** 중복 판정(`wiki_dedup_judge`)은 이제 신규
후보를 만든 엔진을 **피할 대상**으로 받습니다(`compile.semanticDedup.judge.crossEngine`,
아래 dedup 절 참고). `plan_verify_attempts`를 그대로 재사용하지는 않습니다 — dedup은 카드
**두 장**을 비교하고 그 둘의 생산자가 다르거나 불명일 수 있어 무엇이 자기검증인지 확정되지
않습니다. 공용화한 것은 **순서 규칙 하나**(`wiki_compile_worker.plan_engine_order`)입니다.

로그 키의 의미는 고정입니다: `producedBy`는 **생성** 엔진, `engine`은 **검수** 엔진(시도가
있는 행에만), `verifiedMode`는 **달성된** 관계(판정이 실제로 나온 행에만), `attemptedMode`는
시도했던 관계(실패 행 포함)입니다. 실패 행에 `verifiedMode`를 쓰지 않는 이유는 노출 집계가
"교차검증됨"으로 오독되기 때문입니다.

**recall은 `verifiedMode`를 읽지 않습니다.** `self`도 `cross-engine`과 동일하게 캐논급으로
주입됩니다. 주입에 신뢰 하향 배지를 붙이지 않는 이유는 —
recall에서 내리지 않기로 한 카드에 하향 표식을 붙이는 것은 자기모순이고, 배지는 주입마다
토큰을 먹어 이 플러그인의 목표(토큰 절감)와 반대로 갑니다. 노출을 재는 수단은 주입이
아니라 frontmatter 그리고 `verify-log.jsonl`의 `verifiedMode`/`producedBy`/`enginesAttempted`
입니다(삭제는 `verify-deleted.jsonl`에도 같은 두 필드가 남습니다 — 자기검증이 내린 삭제는
교차검증이 내린 삭제보다 약한 근거입니다).

**`pass`는 근거를 요구합니다.** verifier가 `pass`를 내면서 `claims`를 비워 보내거나 항목이
전부 퇴화한 값(`null`·`""`·`{}`)이면 `pass`로 처리하지 않고 **`invalid_verdict`**(= 못 쓸
출력)로 다룹니다. `pass`가 `status: verified`의 유일한 입구이고 `verified` 카드는 recall에서
캐논급으로 주입되므로, 근거 0인 `pass`를 받아들이면 원문과 무관한 요약이 신뢰 카드가 됩니다.
이때 카드는 **삭제되지 않고 `generated`로 남습니다**(= recall 비가시) — `fail`/`inconclusive`
로 강등하면 기본 동작이 삭제인데, 근거 없는 `pass`는 "원문과 모순"도 "판정 불가"도 아니라
그 방향이 틀립니다. 소스를 고치면 재컴파일→재검수가 다시 열립니다. `claims` 항목의
`supported`/`quote` 값까지는 검사하지 않습니다(그것은 프롬프트가 모델에 맡긴 판정 규칙이고,
모양 검사는 퇴화한 출력만 걸러낼 수 있습니다).

**실패 분류 경계**: 후보 엔진 사이를 넘어가는 조건은 **exit 127(host CLI 부재)뿐**입니다.
127은 CLI를 실행하지도 못한 상태라 토큰이 들지 않으므로 다음 후보 시도가 공짜입니다.
timeout·비127 실패는 이미 CLI를 호출한 것이므로 다음 엔진으로 넘기지 않습니다(같은 카드에
대한 이중 과금) — 기존대로 cooldown + 큐 보존입니다. 어느 경로도 verdict를 만들지 않으므로
**transient가 `inconclusive`(=삭제)로 흐를 수 없다**는 계약이 유지됩니다.
`crossEngine: "require"`에서 다른 엔진이 없으면 잡을 **보존**합니다(카드는 `generated`로
남고 삭제되지 않으며, 두 번째 CLI가 설치되면 검수됩니다).

**기존 카드는 자동으로 재검증되지 않습니다.** 655장 재검증은 host CLI 호출 655회 =
사용자 계정 청구입니다(카드 1장당 카드 본문 + 소스 최대 3개 × `maxSourceChars` 12,000자
≈ 입력 9K 토큰 → 합계 대략 6M 입력 토큰). 새로 컴파일되는 카드부터 교차 검증됩니다.
전량 재검증을 원하면 명시적 opt-in으로만 하십시오 — `verify-queue.jsonl`에 대상 카드의
`{targetPath, sources, sourceHash, engine}` 행을 직접 append하고 `status`를 `generated`로
되돌린 뒤 worker가 `maxPerRun`씩 드레인하게 하는 방식입니다.

#### 교차검증 주장의 조건 (귀속)

"다른 엔진이 검수했다"는 주장은 **카드를 만든 엔진이 특정될 때만** 성립합니다. `producing`
라벨이 현재 풀에서 엔진별 argv로 해석되지 않으면 — sentinel `"unknown"`(호스트를 알 수 없을 때
enqueue가 쓰는 값)이나 풀 밖 라벨 — 어떤 후보도 "생성 엔진과
다르다"를 증명할 수 없습니다. 그때는 모든 시도가 `verifiedMode: unknown`이고 `require`는
**fail-closed**(검수하지 않고 잡 보존)입니다.

생성 엔진 라벨은 **큐 잡이 SSOT**입니다. `candidate.engine`은 extractor(모델) 출력이므로 값을
신뢰하지 않고 worker가 잡의 값으로 덮어씁니다 — 그러지 않으면 모델이 스키마 밖 필드 하나로
자기검증을 `cross-engine`으로 승격시킬 수 있습니다.

#### 후보 단위 cooldown (`verify-engine-cooldown.json`)

**한 카드에 대한 유료 호출은 run 당 1회**입니다. 그래서 다음 후보로 넘어가는 조건은
exit 127(host CLI 부재, 토큰 0)뿐이고 timeout·비127 실패는 그 run 에서 다른 엔진을 부르지
않습니다. 그런데 그것만으로는 **선호 엔진이 계속 같은 이유로 실패하면(인증 안 된 CLI, verdict
미출력, timeout) 검수가 영구 정지**합니다 — 0.x의 전역 `verify-cooldown`이 그 상태였고,
`recallVerifiedOnly` 기본값 아래에서 그것은 wiki가 recall에서 사라지는 것과 같습니다.

그래서 실패는 **후보 단위로 기억**합니다: 실패한 후보를 `cooldownSeconds` 동안
`.memvane/compile/verify-engine-cooldown.json`에 기록하고, 다음 run 의 후보 선정에서
건너뜁니다. 결과적으로 run 당 유료 호출은 여전히 1회이고, run 을 넘어가면 다음 후보로 →
최종적으로 생성 엔진(self)까지 degrade 합니다. 전역 cooldown과 달리 한 엔진의 실패가 다른
카드·다른 엔진의 검수를 막지 않습니다. 엔진에 귀속되지 않는 argv는 `(unattributed)` 키로
따로 식히므로 그 엔진의 adapter가 함께 막히지 않습니다. 만료 항목은 쓰기 시 정리되어 파일이 무계로 커지지 않습니다.

이 파일은 **이 degrade의 복구 메커니즘**이므로 쓰기 실패를 삼키지 않습니다. 기록이 남지
않으면 다음 run이 같은 후보를 다시 불러 영구 정지가 그대로 재발하므로, 실패는 로그 행의
`cooldownWriteFailed: true`로 표면화됩니다. 쓰기는 원자적이고(`write_text_atomic`) 같은
이유로 read-modify-write를 sidecar `flock`으로 직렬화합니다 — 정상 경로는 `claim_queue`가
프로젝트 단위로 직렬화하므로 경합이 드물고 손실 등급도 원장(줄 유실 = 감사 유실)보다 낮지만,
잃는 것이 하필 이 복구 메커니즘이라 기존 헬퍼를 재사용하는 값싼 방어를 생략할 이유가 없습니다.
만료값은 24시간으로 클램프합니다(손상·주입된 값이 후보를 영구 배제하지 못하게 — 상한을 넘는
항목은 식힘으로 보지 않습니다).

읽기 실패·손상은 fail-open(식힘 없음)입니다: 식힘 기록을 못 읽는 것이 검수를 막는 이유가 되면
안 되고, 최악은 한 번 더 시도하는 것입니다. 0.x의 전역 `verify-cooldown` 파일은 이제 쓰이지
않으므로 worker가 발견 시 정리합니다.

#### 실패 유형과 결과

| 실패 유형 | host CLI 호출 | 카드 | 큐 | cooldown | 로그 |
|---|---|---|---|---|---|
| `pass` verdict | 1(유료) | `verified` 스탬프 | 소비 | — | `verified` |
| `fail` verdict | 1(유료) | `onFail`(기본 삭제) | 소비 | — | `deleted`/`contested`/`kept` + `verify-deleted.jsonl` |
| `inconclusive` verdict | 1(유료) | `onInconclusive`(기본 삭제) | 소비 | — | 같음 + `verify-skipped.jsonl` |
| exit 127 (후보 전원 부재) | 0(무료) | 불변(`generated`) | **보존** | 없음(설치 즉시 재시도) | `deferred`/`extractor_unavailable` |
| timeout·비127 실패 | 1(유료) | 불변(`generated`) | **보존** | **그 후보만** | `deferred` + `cooledKey` |
| 출력이 판정 불가(`invalid_extractor_json`·`invalid_verdict`) — 남은 후보 있음 | 1(유료) | 불변(`generated`) | **보존** | 그 후보만 | `deferred` |
| 같은 사유 — 후보 소진 | 1(유료) | 불변(`generated`) | 폐기(종점) | 그 후보만 | `skipped` |
| 후보 전원 식힘 중 | 0 | 불변(`generated`) | **보존** | 유지 | `deferred`/`engines_cooling` |
| `require` + 귀속 불가/후보 없음 | 0 | 불변(`generated`) | **보존** | — | `deferred`/`cross_engine_unavailable` |
| extractor 설정 없음 | 0 | 불변(`generated`) | 폐기 | — | `skipped`/`missing_extractor` |
| 소스 전멸 | 0 | 불변(`generated`, 정리는 수명 주기) | 소비 | — | `skipped`/`source_missing` |

**카드를 삭제하는 행은 verdict 세 줄뿐이고, 그 셋은 유효 JSON verdict 를 받은 뒤에만 도달
합니다.** transient(127·timeout·실행 실패)는 전부 "카드 불변 + 큐 보존"이므로 verifier CLI가
없거나 깨진 머신에서 카드가 삭제되는 경로가 없습니다.

#### 증명 필드 위생

`status: verified`/`contested`는 `verifiedBy`·`verifiedAt`·`verifiedMode`와 **한 쓰기로
함께** 나갑니다(`wiki_compile.stamp_verification`이 유일한 경로). 라이브에는 `verified`인데
`verifiedAt`이 없는 카드가 27장 있는데 전부 `verifiedBy: agent-full-source` — 이 코드베이스가
쓰지 않는 값이고 과거 수동/에이전트 백필의 잔재입니다. **이 27장은 소급 수정하지 않습니다**:
결측은 거짓이 아니고, 지금 타임스탬프를 채우면 없던 사실을 만들어 내는 것입니다("기존 데이터를 건드리지 않는다" 기조).

반대로 카드가 갱신돼 `generated`로 리셋될 때는 증명 필드를 **키째로 제거**합니다. 값만
비우면(`verifiedBy: ""`) "한 번 기계 검수를 통과한 카드"로 읽히는 잔재가 남습니다
(라이브 5장이 그 형태였습니다).

#### 억제 마커 (`verify-skipped.jsonl`)

`inconclusive`는 `fail`과 성질이 다릅니다. `fail`은 "카드가 원문과 모순"이라 판정이
결정적이지만, `inconclusive`는 "verifier가 판정하지 못함"이라 같은 소스·같은 카드에서
**재현될 확률이 높습니다.** 억제 장치가 없으면 소스 재큐잉(sync mtime 변화, checkout 등)
마다 재컴파일 → 다시 inconclusive → 다시 삭제가 반복되고, 매 반복이 host CLI 호출이므로
**토큰 비용이 사용자 계정으로 청구됩니다.**

그래서 inconclusive 삭제 시 카드의 각 소스에 대해 `(sourcePath, sourceBodyHash)`를
`verify-skipped.jsonl`에 기록합니다. `sourceBodyHash`는 extractor에 실제로 전달된
`maxSourceChars` 절단 본문의 해시입니다. 다음 compile에서 같은 소스의 현재 본문 해시가
기록과 일치하면 **extractor를 띄우기 전에 skip**하고(`candidates.jsonl`에
`verify_inconclusive_suppressed` 사유로 남김), 소스 본문이 실제로 바뀌면 해시가 달라져
자동으로 재시도됩니다. 마커 파일을 지우면 즉시 재시도됩니다.

**이 마커를 쓸 수 없으면 카드를 지우지 않습니다(fail-closed).** 삭제 감사 원장과 같은
처방입니다 — 마커는 삭제 **전에** 쓰고, 실패하면 카드를 보존한 채 검수 잡을 큐에 되돌리며
`verify-log.jsonl`에 `delete_blocked` / `suppression_ledger_unwritable`을 남깁니다. 그리고
fail-closed만으로는 "판정(유료) → 기록 실패 → 잡 보존 → 재판정(유료)" 루프가 남으므로,
`onInconclusive`가 `delete`인 프로젝트에서는 **유료 호출 전에** 이 파일의 쓰기 가능성을
확인하고 불가하면 그 run의 검수를 아예 시작하지 않습니다(host CLI 호출 0회). 이 원장
경로들은 설정이 아니라 상수라(`core/compile_paths.py`) 경로 오타로 마커가 유실될 경로 자체가
없습니다.
`dedup-skipped.jsonl`(semantic dedup의 `distinct` 판정 억제)도 같은 preflight를 받습니다:
쓸 수 없으면 LLM judge를 호출하지 않고 무료 score 게이트로 degrade합니다.

이 파일은 `dedup-skipped.jsonl`과 같은 이유로 **256KB 트림 대상이 아닙니다.** 한 줄
유실은 (a) 삭제 감사 추적 유실과 (b) 과금 루프 재개를 동시에 뜻하기 때문입니다.
`verify-log.jsonl`은 pass 레코드가 대부분을 차지하는 순수 로그라 트림을 유지하지만,
삭제 레코드는 `verify-skipped.jsonl`에 `verdict`/`reasons`/`targetPath`/`deletedAt`과 함께
중복 보존되므로 로그가 회전한 뒤에도 삭제 사유를 규명할 수 있습니다.

기계 삭제는 `generated-manifest.jsonl`에 `action: "verify-deleted"` 행으로 남습니다.
이것이 없으면 다음 compile의 삭제 감지가 파일 부재를 **사용자의 의도적 삭제**로 읽어
tombstone(영구 억제)을 세우고, "소스를 고치면 재생성된다"는 전제가 깨집니다.
사람이 직접 지운 카드는 이 마커가 없으므로 기존대로 tombstone 처리됩니다.

verifier의 자동 삭제 대상은 플러그인이 만든 `status: generated` 카드뿐입니다. recall trust는
`status: verified` + `createdBy: memvane`(또는 이전 플러그인 이름 `qmd-auto-context`) + 비어 있지 않은 compiler-owned
`sourceRevisions`를 모두 요구하며, status 문자열 하나로 승격되지 않습니다.
CLI 부재(exit 127)·timeout 같은 **transient 실패는 inconclusive가 아니며** 큐 보존 +
cooldown 경로를 그대로 타므로, verifier CLI가 없는 머신에서 카드가 삭제되지 않습니다.

### wiki 수명 주기 (원문 소실·변경)

`core/wiki_lifecycle.py`가 생성 카드마다 기록된 원문 경로와 해시(`sourceRevisions`)로
카드를 판정합니다. embed·LLM에 의존하지 않습니다.

| 원문 상태 | 처리 |
|---|---|
| 기록된 원문 파일이 없음 | 카드를 보관한 뒤 삭제하고 memvane-db에서 제거합니다 |
| 원문 해시가 바뀜 | compile 큐에 재생성을 넣고, 새 카드가 memvane-db를 갱신합니다 |
| 카드에 `sourceRevisions`가 없음 | 카드를 보관한 뒤 삭제하고 memvane-db에서 제거합니다 |
| 같음 | 유지합니다 |

**대상은 플러그인이 만든 카드뿐입니다** — `createdBy`가 `memvane`(또는 이전 플러그인 이름 `qmd-auto-context`)이고 auto 블록이 있으며
`superseded`가 아닌 카드입니다. 사람이 쓴 페이지, 심볼릭 링크, `.`으로 시작하는 경로,
wiki root의 `SCHEMA.md`·`index.md`·`log.md`, wiki root 밖의 파일은 건드리지 않습니다. 형식이 틀린 `sourceRevisions`,
`allowRoots` 밖을 가리키는 원문, 읽을 수 없는 원문은 "판정 불가"로 남기고 로그만 씁니다.
원문 존재 판정은 주입 경로와 **같은 함수**(`wiki_card.resolve_existing_source`)를 씁니다.

**삭제는 복구할 수 있습니다.** 옛 바이트는 `.memvane/compile/retired-wiki/`에
`<digest>.md`로, 원래 경로는 옆의 `<digest>.json`에 남은 뒤 카드가 지워집니다. 그다음 `laya-sync`를 깨우면 memvane-db full scan이 다음 세대에서 없는 wiki 파일을
뺍니다. 살아 있는 원문이 남은 공유 카드는 바로 지우지 않고 compile 큐에 `sourceDelete`
행으로 넘깁니다 — compile worker가 살아 있는 원문을 먼저 재생성한 뒤 카드를 퇴역시킵니다.

**재생성은 compile trigger 동의를 따릅니다.** 해시가 바뀐 원문은
legacy settings에서는 `compile.triggers`에 `session_start_source`가 있을 때만 큐에 들어갑니다(유료 호출입니다). minimal settings(`name`·`skipPaths`만 있는 파일)는 wiki 생성이 항상 켜져 있으므로 이 동의 없이 넣습니다.
없으면 로그에 `ENQUEUE compile_disabled`가 남고 카드는 그대로입니다 — 그 카드는 원문
지문이 맞지 않아 recall에 주입되지 않습니다. 원문을 편집하는 경로(PostToolUse, raw
discovery)는 편집된 원문을 compile 큐에 바로 넣고 삭제된 원문을 `sourceDelete`로 넣으며,
편집 뒤 수명 주기 run이 그 원문을 인용한 다른 카드를 맡습니다.

**대량 삭제 가드.** 증명된 카드 중 원문이 없는 카드가 3장보다 많고 25%를 넘으면 그 run은
아무것도 지우거나 넣지 않고 로그(`DEFER: mass-deletion guard`)만 남깁니다. 디스크가 마운트되지
않았거나 브랜치를 바꿨거나 checkout을 옮긴 경우가 여기에 걸립니다. 조건이 사라지면 다음
run이 정상 처리합니다. `sourceRevisions`가 없는 카드는 카드 자체의 성질이라 비율
계산에서 빠지고 run당 상한을 받습니다. 가드가 발동한 run은 그 카드들도 보류합니다.

**한 run의 폭은 유계입니다.** 원문 해시 대조는 `compile.sourceScan.maxCardsPerScan`(기본 300,
5000 클램프)장 창을 순환 커서로 보고, 삭제는 run당 25장, enqueue는 compile per-wake
상한(`QMD_SYNC_COMPILE_MAX`, 기본 50)까지입니다. 상한에 걸리면 커서가 그 카드에서 멈추고 다음
run이 이어 봅니다. `QMD_SOURCE_SCAN_MAX` 환경변수는 디버그용 레버이고 설정값을
덮습니다.

**멱등입니다.** 지운 카드는 다음 run에 없고, 보관은 같은 내용을 재사용하며, 아직 큐에 있는
같은 행은 다시 넣지 않습니다. 이미 넣은 같은 원문 해시의 재생성은 7일 안에 다시 넣지 않습니다
(상태는 sync state 디렉터리의 `<key>-wiki-lifecycle.json`). 처리 내역은
`~/.local/state/memvane/auto-context/logs/wiki-lifecycle.log`(`QMD_WIKI_LIFECYCLE_LOG`)에 남고,
순환 커서 같은 scan 상태는 `~/.local/state/memvane/auto-context/scan-state/`에 둡니다.

**선택된 memvane-db route에서만 조치합니다.** route가 없는 프로젝트는 아무 작업도 받지 않습니다.

**언제 도는가.** 선택된 프로젝트에서 세션이 시작될 때, 프로젝트 안 파일을 편집했을 때,
agent의 Bash 명령이 성공했을 때 수명 주기 요청이 하나 남고 그 프로젝트의 wiki compile
작업이 깨어납니다. 이 작업은 hook 밖에서 수명 주기를 먼저 돌리고(한 run 최대 5분), 그 run이
넣은 재생성을 같은 차례에 compile합니다. 요청이 처리되기 전에 들어온 trigger는 하나로
묶이고, run 도중 들어온 trigger는 후속 run 하나로 처리됩니다. 연결 워크트리, rebase·merge
진행 중, 번들 업그레이드 대기 중에는 아무것도 지우거나 넣지 않습니다.

**`compile.sourceScan.enabled:false`가 끄는 것과 끄지 않는 것.** 끄는 것은 수명 주기 조치
(삭제·enqueue)입니다. 끄지 않는 것은 같은 run이 함께 세는 **provenance 결측 카드 수**입니다 —
그것은 "원문 지문 없이 남아 recall에 주입되지 않는 카드가 N장"이라는 별개 신호이고 별개
알림을 가지므로, 조치를 껐다고 0으로 보고되면 안 됩니다(그 알림은 "남은 수는 이 알림이
사라지면 0입니다"라고 안내합니다). `enabled`에 bool이 아닌 값(`"false"`·`1`·`null`)을 적으면
끄지 않고 기본값으로 폴백합니다.

**기계 검수는 원문 소실 카드를 바꾸지 않습니다.** 검수 큐에서 원문이 전부 없는 잡을 만나면
`skipped`/`source_missing`으로 기록하고 카드는 수명 주기에 맡깁니다.

**주입 표식은 두지 않습니다.** 원문이 없는 카드는 수명 주기가 정리하고,
`missing` 항목에는 `↳` 줄을 두지 않습니다(깨진 경로 표식은 토큰만 늘리고 모델이 할 수 있는
일이 없습니다).

### Semantic Dedup

`compile.semanticDedup`은 새 wiki 후보가 기존 wiki page와 너무 비슷할 때 자동
중복 생성을 막고 검토 대상으로 돌리는 설정입니다.

**판정은 score가 아니라 LLM이 합니다.** 검색 score는 유사도가 아니라 **RRF 순위의
함수**입니다 — `rerank`를 켜도 `blendedScore = w·(1/rrfRank) + (1-w)·rerankScore`로 순위가
섞여 점수 상한이 순위로 고정됩니다(실측 125카드 wiki: rank1 {0.88, 0.93} / rank2
{0.55, 0.56} / rank3 [0.40, 0.44]). 자기 본문은 항상 rank1 self-match이므로 진짜 중복은
rank ≥ 2에만 올 수 있고, 거기서는 옛 `autoMergeThreshold` 기본값 0.9가 **수학적으로 도달
불가**였습니다. 그래서 임계 레버 4종(`threshold`·`topK`·`autoMergeThreshold`·
`similarPageMaxChars`)은 설정에서 사라지고 상수가 됐습니다. score는
`semanticDedup.candidateMinScore`(후보 retrieval floor)로만 남고, "두 카드가 같은 사실을
말하는가"는 judge가 본문 대 본문으로 판정합니다.

judge는 **절대 merge/delete하지 않습니다.** 비용 상한은 write-time gate 1회당
`budget.dedupPairsPerCompile`(기본 1)입니다. judge를 쓸 수 없는 머신(extractor 미설정·CLI 부재)
에서는 위 상수로 동결된 레거시 무료 score 게이트로 degrade합니다.
`QMD_DEDUP_JUDGE=off`는 프로세스 단위 kill switch입니다.

#### Retroactive scan

이미 디스크에 있는 카드끼리의 중복은 retroactive scan(`core/wiki_dedup_scan.py`)이 찾습니다.
선택된 프로젝트에서 SessionStart·원문 편집 뒤 도는 wiki 수명 주기 자식이 수명 주기 run 다음에
scan을 실행합니다. scan은 선택된 route의 memvane-db sealed head에 저장된 위키 카드 chunk 벡터의
중심점끼리 cosine을 비교하고, 0.9(`autoMergeThreshold` 상수) 이상인 쌍을
`.memvane/compile/dedup-needed.jsonl`에 `{pageA, pageB, score}`로 넣습니다. 이 score는
검색 순위가 아니라 실제 cosine 유사도입니다. 모델·provider·judge를 부르지 않으므로 유료 호출이
없고, 병합·삭제 판단은 `wiki-dedup` skill이 띄우는 resolver 에이전트가 합니다.

- 디스크 바이트가 색인 해시와 같은 카드만 비교합니다. 게시 뒤 편집된 카드는 다음 게시를 기다립니다.
- `superseded`·`discarded` 카드, wiki root의 `SCHEMA.md`·`index.md`·`log.md`, 이미 큐에 있는 쌍, `dedup-skipped.jsonl`에
  같은 본문 해시로 기록된 쌍은 넣지 않습니다.
- run당 위키 카드 2,048장, 바뀐 카드 50장, `maxPairsPerScan` 쌍, 20초로 묶입니다. 비교한 색인
  해시를 sync state 디렉터리의 `<key>-wiki-dedup.json`에 남겨 다음 run은 새로 바뀐 카드만 봅니다.
- `compile.mode: "off"`나 `semanticDedup.enabled: false`면 돌지 않습니다. 로그는
  `~/.local/state/memvane/auto-context/logs/wiki-dedup.log`(`QMD_WIKI_DEDUP_LOG`)입니다.

#### 판정 엔진 분리 (dedup)

**write-time gate만** 교차 엔진으로 동작합니다. 그 경로의 신규 후보 생산 엔진은 큐 잡이 정하는
사실(`candidate.engine`은 worker가 잡의 값으로 덮어씁니다)이므로 "무엇이 자기검증인지"가
확정됩니다.

**호출 수는 늘지 않습니다.** judge는 쌍당 1회이고 엔진만 바뀝니다. 후보 사이를 넘어가는 조건은
**exit 127(host CLI 부재, 토큰 0)뿐**이며, 실제로 실행된 첫 후보가 순회를 끝냅니다.

**degrade의 종점.** 다른 엔진 CLI가 없으면(127) 생산 엔진으로 내려와 `self`로 기록합니다.
선호 후보가 **유료로** 실패하면(비127·timeout) 같은 run에서 다른 엔진을 부르지 않고(이중 과금)
그 후보만 `dedup-judge-engine-cooldown.json`에 엔진 단위로 식혀 **다음 run이 다음 후보로
degrade**하게 합니다. 후보가 소진되면 전역 `dedup-judge-cooldown`으로 judge 전체를 식힙니다
(0.x 동작). 두 기록이 모두 실패하면 그 사실을 `cooldownWriteFailed`로 표면화합니다 —
식힘 기록이 degrade의 유일한 메커니즘이므로 조용히 삼키면 판정이 영구 정지합니다.

**그 표면화의 종점은 `candidates.jsonl` 행의 `judge` 필드입니다.** `outcome`·`reason`·`engine`·
`engineCooldown`·`cooldownWriteFailed`를 남기며, 정상 판정에는 아무것도 쓰지 않으므로 기존 행
모양은 그대로입니다.

**판정 불가 출력은 재과금되지 않습니다.** judge가 유효 verdict를 내지 못한 경우
(`invalid_verdict` / `invalid_extractor_json` — exit 0 + 쓰레기 stdout)는 timeout과 달리 같은
입력에서 재현되는 설정·adapter 오류이므로, 남은 후보가 있으면 그 후보만 식혀 **다음 run이 다음
후보로 degrade**하고, 후보가 소진되면 그 쌍을 `unclear` 유효 판정으로 돌리고 judge 전체를
식힙니다. transient·식힘·CLI 부재는 **판정이 일어나지 않았으므로** 억제하지 않습니다.
(verify의 `UNUSABLE_OUTPUT_REASONS`/`defer_or_drop`와 같은 규칙이고 종점의 모양만 다릅니다 —
verify는 잡을 폐기, dedup은 쌍을 억제.)

`merge-needed.jsonl`은 write-time collision을 보존하는 passive, non-trusted diagnostic입니다.
자동 merge나 trust 승격, SessionStart resolver 입력으로 사용되지 않습니다. 판정 기록은
`merge-needed.jsonl`(write-time)에 `judgedBy`(판정 엔진) / `judgedMode`(달성된 관계) /
`producedBy`(생산 엔진)로 남습니다. `wiki-dedup` skill은 이미 쌓인 `dedup-needed.jsonl`을
resolver agent로 처리합니다(잠금 `~/.local/state/memvane/auto-context/dedup-resolve-lock/`). 판정이 없었던 행에는 이 필드들을 쓰지 않습니다.

## cooldown·lock의 "영구 정지" 방어

시간 게이트(cooldown·TTL·stale lock)는 **한 번 잘못 닫히면 영구히 닫혀 있어서는 안 됩니다.**
그 실패는 아무 로그도 남기지 않고 해당 파이프라인을 통째로 멈추기 때문입니다. 판정 규칙은 `core/cooldown.py`
한 벌이고 두 가지입니다.

**① 만료 시각을 파일에 적는 cooldown은 24시간으로 클램프됩니다.** 대상은 compile
cooldown(`extractor.cooldownSeconds`), 검수 cooldown(`compile.verify.cooldownSeconds`),
dedup judge cooldown(`semanticDedup.judge.cooldownSeconds`)과 그 엔진 단위 파일입니다.
쓰기·읽기 양쪽에 같은 상한이 걸리므로 **24시간보다 긴 값을 적으면 24시간으로 동작합니다.**
이유는 상한이 없으면 오염되거나 주입된 만료값 하나(`1e300`)가 그 게이트를 영구 활성으로
만들어 compile·검수·dedup 판정이 다시는 돌지 않기 때문입니다(복구 경로는 사람이 그 파일을
찾아 지우는 것뿐입니다).

**② mtime 나이로 판정하는 창은 미래 mtime을 "창이 끝났다"로 읽습니다.** 미래 mtime은 시계
되돌림·백업 복원·파일시스템 이관으로 실제로 생기고, `now - mtime`을 그대로 쓰면 나이가
음수가 되어 창이 영원히 끝나지 않습니다. 방향은 지점마다 다르지만 병리는 하나입니다 —
stale lock 회수 불가, 알림 영구 억제. 60초 이내의 시계 오차(NTP 미세 조정·타임스탬프 반올림)는 "방금 쓴 것"으로 보아
존중합니다.

**stale lock 회수는 모든 lock에 있습니다.** 프로세스가 비정상 종료해 lock만 남으면 그
파이프라인이 영구 정지하므로, 잡힌 지 10분이 넘은 lock 디렉터리는 회수됩니다.

## Common Recipes

### Prompt-time recall 줄이기

```json
{
  "topN": 2,
  "minScore": 0.5
}
```

`topN`을 줄이면 자동 주입되는 문서 수가 줄어듭니다. `minScore`는 순위 컷이므로 `topN`과
어긋나지 않게 `1/topN` 근처로 맞추는 편이 예측 가능합니다(`topN: 2` → `0.5`). 실제 값은
`memvane:logs` 스킬의 `recalls` 출력(주입 카드 수·reason)을 보면서 조정하는 것이 좋습니다.

### recall 결과에서 특정 경로 제외

```json
{
  "skipPaths": [
    "node_modules",
    ".git",
    "dist",
    "build"
  ]
}
```

선택된 **Laya**는 `recallStrategy` 값과 관계없이 위키만 색인·검색합니다.
이 경로에서 `skipPaths`는 원문 **파일명이 아닌 부모 디렉터리 경로**에 substring이
들어 있는지 판정해 새 위키 생성 후보를 제외합니다. 예를 들어 `build`는
`docs/build/a.md`와 `docs/build-tools/a.md`를 제외하지만 `docs/build-notes.md`는
제외하지 않습니다. 기존 위키에서 제외된 출처가 하나라도 발견되면
카드 전체를 검색·주입에서 숨깁니다. 여러 출처를 가진 카드는 일부 내용만
안전하게 분리할 수 없어 전부 숨기며, 원문과 위키 파일은 삭제하지 않습니다.
제외 설정을 해제하면 원본 카드가 다시 검증 대상이 됩니다. 이미 색인된 벡터는
즉시 물리 삭제되지 않지만 검색 검증 단계에서 결과가 차단됩니다. 동기화와
재색인은 비동기로 반영될 수 있습니다.

### 인덱싱 범위를 줄이는 방법

선택된 Laya route는 wiki collection만 색인합니다. 위키 생성 원천을 줄이는 수단은
`skipPaths`(위 절)와 `collectionPaths`입니다. `collectionPaths`에 `.`(저장소 루트)을
지정하면 저장소 전체 Markdown이 위키 생성 원천이 됩니다 — minimal settings의 raw
collection이 이 모양입니다. `collections`에 있는데 `collectionPaths`에 대응 항목이 없는
collection도 경로가 `.`으로 해석되므로 collection 이름마다 경로를 명시하세요.

## Troubleshooting

컨텍스트가 기대와 다르게 들어오면 에이전트에게 `memvane:logs` 스킬로 recall 진단을 요청하세요.
선택된 Laya route의 recall은 hook 호출마다 설치 이벤트 로그
`<install state root>/events.jsonl`에 `recall` 이벤트 한 줄을 남깁니다(항상 켜져 있습니다).
`outcome`은 `served`/`empty`/`refused`, `reason`은 `selected`, `no_results_after_filter`,
`no_candidates` 또는 refusal code이고, `notes`는 경로상의 진단(`laya_wiki_rows_filtered`,
`laya_selector_candidate_invalid`, `laya_vector_*` 등), `cards[]`는 실제로 주입된 문서입니다.

줄이 **하나도 없다**면 훅이 실행되지 않았거나 그 프로젝트에 선택된 Laya route가 없는
것입니다(route가 없는 프로젝트는 recall을 하지 않고 이벤트도 남기지 않습니다). 스킬 헤더의
`WARNING`은 state root나 `events.jsonl`이 안전 검사를 통과하지 못해 쓰기가 거부됐다는 뜻입니다.
sandbox(`QMD_SANDBOX`)는 "즉시 무출력 종료"가 계약이라 이벤트도 쓰지 않는 유일한 예외입니다.

설정을 바꿨는데도 동작이 이상하면 다음을 먼저 봅니다.

- `collections`에 대상 collection이 있는지
- `collectionPaths`가 실제 존재하는 경로인지
- `collectionRoles`가 collection 이름과 정확히 맞는지, role 값에 오타가 없는지
  (인식할 수 없는 값은 색인·compile에서 전부 제외됩니다)
- `minScore`가 `topN`과 어긋나지 않는지 — 순위 컷이라
  `0.8`이면 `topN`과 무관하게 1건만 주입됩니다([위 절 참고](#minscore는-유사도가-아니라-순위입니다))
- `compile.excludeStatusesFromRecall` 때문에 wiki page가 제외된 것은 아닌지

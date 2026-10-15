# qmd auto-context

qmd auto-context는 프로젝트 안의 문서, 메모, wiki를 자동으로 찾아서
에이전트 대화에 넣어 주는 플러그인입니다.

매번 "이 문서도 참고해"라고 붙여 넣지 않아도, 에이전트가 질문과
관련된 내용을 qmd에서 찾아 컨텍스트로 받습니다.

지원 에이전트:

- Claude Code
- Codex
- Hermes Agent

설치 후 사용자는 터미널 명령을 외우지 않아도 됩니다. 프로젝트 설정,
인덱스 갱신, recall 진단, wiki compile 같은 작업은 에이전트에게 자연어로
요청하면 에이전트가 필요한 스킬과 내부 명령을 선택해 처리합니다.

내부 구조와 유지보수 정보는 [docs/architecture.md](docs/architecture.md)를
보세요.

## Quickstart (English)

qmd auto-context injects relevant project documentation into Claude Code, Codex,
and Hermes Agent sessions. Install this plugin for your host, then ask the
`setup` skill to inspect the project and prepare a reviewed runtime. The managed
QMD setup and v2 runtime routing are available on macOS arm64. On other hosts,
v2 stays `setup_required` and preserves existing global QMD binaries, settings,
and data; a global CLI does not enable v2 routing. Projects remain inactive
until you explicitly opt in.
Managed QMD accepts Node 22 or 24 LTS. Node 26 Current is in the test matrix;
its native QMD compatibility still needs a Node 26 probe. Node 20 is end of life;
upgrade to Node 22 or 24 LTS before managed setup.

| Host | Installation route | Runtime support |
|---|---|---|
| Claude Code | Claude plugin marketplace | recall, update, gate, post-edit indexing, wiki compile |
| Codex | Codex plugin marketplace | recall, update, gate, post-edit indexing, wiki compile |
| Hermes Agent | Hermes plugin install | recall, update, gate, post-edit indexing, wiki compile |

For data handling, including the optional wiki-compile path, see
[docs/privacy.md](docs/privacy.md). For commands and Korean-language setup,
continue below.

## 설치

먼저 사용하는 에이전트에 플러그인을 설치합니다. 훅이 실행할 `python3`은
3.9 이상이어야 합니다. 설치 후 에이전트에게 “이 프로젝트 auto-context 설치
상태 보여줘”라고 요청하면 `setup` 스킬이 기존 설정·wiki·QMD·Laya·색인
상태를 읽기 전용으로 확인합니다. macOS arm64에서는 검토한 요청에 따라
QMD 2.5.3을 격리된 관리형 세대에 준비하거나 호환되는 기존 설치를
재사용할 수 있습니다. 준비와 적용은 별도 요청이며 기존 DB와 설정을 보존합니다.
관리형 QMD에는 Node 22·24 LTS를 권장합니다. Node 26 Current는 시험 대상이며
실제 native QMD 호환성 검증은 남아 있습니다. 수명이 끝난 Node 20은 관리형
설치·재사용에서 거부하므로 Node 22 또는 24 LTS로 올린 뒤 준비하세요.

Linux 및 다른 아키텍처에서는 v2 관리형 설치와 런타임 라우팅을 지원하지
않습니다. v1에서 플러그인을 자동 갱신하더라도 `setup_required` 상태를
유지하며 기존 전역 QMD 실행 파일·설정·데이터를 보존합니다. 전역 CLI를
별도로 설치해도 v2 라우팅이 활성화되지는 않습니다. 관리형 신규 설치나 기존 버전 갱신에는
패키지 실행 범위와 원본 데이터 보존 계획을 먼저 검토해야 합니다.

호스트별 플러그인 설치 명령은 다음과 같습니다.

```bash
# Claude Code
/plugin marketplace add zbdulee/qmd-auto-context
/plugin install qmd-auto-context

# Codex
codex plugin marketplace add zbdulee/qmd-auto-context --sparse .agents/plugins
codex plugin add qmd-auto-context@qmd-auto-context-marketplace

# Hermes Agent
hermes plugins install zbdulee/qmd-auto-context
hermes plugins enable qmd-auto-context
```

제품 설치는 각 에이전트의 plugin/marketplace 흐름을 사용합니다. 이 저장소는
사용자용 `install.sh` 또는 `uninstall.sh`를 제공하지 않습니다.

설치 상태와 안전한 갱신 계획은 에이전트에게 자연어로 요청하세요. 예를 들면
“이 프로젝트 auto-context 설치 상태 보여줘”, “검토한 설치를 격리해서 준비해줘”,
“준비된 설치를 적용해줘”, “직전 적용을 되돌려줘”라고 말할 수 있습니다.
기존 v1 wiki 카드는 검증된 v2 카드로 대응 관계를 검토하기 전까지 유지되며
전환을 보류합니다. Laya runtime 준비만으로 학습은 활성화되지 않습니다.
실제 설치·전환 범위와 현재 제한은
[관리형 설치 계약](docs/install-update-contract-20261006.md)에 정리했습니다.

## 프로젝트에서 켜기

qmd auto-context는 프로젝트별 opt-in 방식입니다. 설정이 없는 프로젝트는
아무 것도 인덱싱하지 않습니다.

설치 후 프로젝트에서 에이전트에게 자연어로 요청하세요.

```text
이 프로젝트에서 auto-context 켜줘.
```

에이전트가 문서 범위를 추천하고 `.auto-context/settings.json`을 만듭니다.
큰 저장소에서는 `docs/current`, `docs/plans`, `docs`처럼 좁은 문서 경로를
우선 기억 대상으로 잡습니다.
설정 값을 직접 조정해야 한다면 [설정 레퍼런스](docs/settings.md)를 참고하세요.

끄고 싶을 때도 명령어를 외울 필요 없이 이렇게 요청하면 됩니다.

```text
이 프로젝트에서는 auto-context 꺼줘.
```

## 에이전트에서 쓰는 흐름

설치와 opt-in이 끝나면 평소에는 별도 명령을 외울 필요가 없습니다.

1. 에이전트 세션을 시작합니다.
2. qmd auto-context가 qmd 인덱스를 최신 상태로 맞춥니다.
3. 사용자가 질문합니다.
4. 관련 문서가 있으면 모델 컨텍스트에 자동으로 추가됩니다.
5. 파일을 편집하면 변경된 문서 범위만 다시 인덱싱하도록 큐에 들어갑니다.

qmd가 설치되어 있지 않거나 백그라운드 서버가 응답하지 않으면 훅은 조용히
건너뜁니다. 사용자의 질문이나 편집을 실패시키지 않고, 컨텍스트 주입만
생략합니다.

## 편집 전 gate

설정이 없는 프로젝트에서 `Edit`, `Write`, `apply_patch` 같은 편집 도구를
쓰면 gate가 먼저 멈춰 세울 수 있습니다. 원치 않는 프로젝트 전체 인덱싱을
막기 위한 안전장치입니다.

이때 선택지는 보통 네 가지입니다.

```text
추천 설정으로 켜줘.
이 프로젝트에서는 꺼줘.
이 프로젝트 cwd에서는 2시간 동안 gate를 그냥 통과해줘.
```

임시 skip은 세션별 권한이 아니라 canonical **프로젝트 cwd 기준 2시간** 마커입니다.
같은 cwd에서 시작한 다른 세션에도 그 시간 동안 적용될 수 있습니다.

## Wiki Compile

wiki compile은 선택 기능입니다. 설정에서 raw/session/source 역할로 잡힌 Markdown을
편집하면 `.auto-context/wiki`에 정리된 wiki 초안을 만들 수 있습니다.
처음 생성된 문서는 `generated` 상태이며, 별도 자동 검증이 원문과 생성 문서를
다시 대조해 통과한 문서만 `verified`로 승격합니다. 사용자가 모든 문서를
일일이 검수하는 흐름이 아니라, AI 생성 결과를 한 번 더 걸러 안전하게 쓰기
위한 2중 필터입니다.

추천 opt-in을 쓰면 기본 wiki scaffold와 compile 설정이 함께 들어갑니다.
나중에 켜고 싶다면 에이전트에게 요청하세요.

```text
wiki compile 켜줘.
```

자동 compile은 raw/session/source 문서 범위 안의 Markdown만 대상으로 합니다.
`.agents`, `.claude`, `.codex`, `.github`, `.auto-context`처럼 점으로 시작하는
도구/메타데이터 경로는 자동 compile 대상에서 제외됩니다.

자동 검증이 실패하면 기본 설정에서는 해당 생성 문서를 삭제합니다. 검증기가
없거나 timeout이 나거나 판단이 불확실한 경우에는 큐나 `generated` 상태가
보존되어, recall 시 `(미검수)`로 낮은 우선순위에 머뭅니다.

끄고 싶다면 에이전트에게 요청하세요.

```text
wiki compile 꺼줘.
```

## 에이전트에게 요청하기

필요할 때는 명령어 대신 에이전트에게 아래처럼 요청하는 흐름을 권장합니다.

| 요청 | 용도 |
|---|---|
| `이 질문에 어떤 문서가 recall되는지 확인해줘.` | recall 결과 확인 |
| `qmd 인덱스 갱신해줘.` | qmd 인덱스 수동 갱신 |
| `훅이 놓친 변경 다시 동기화해줘.` | 누락된 변경 재동기화 |
| `wiki compile 켜줘.` | 기존 프로젝트에 wiki compile 활성화 |
| `예전 글로벌 qmd 훅 정리 계획 확인해줘.` | legacy hook cleanup 점검 |

## 문제 해결

### 컨텍스트가 안 들어오는 것 같을 때

빈 출력은 정상일 수 있습니다. 대표적인 이유는 다음과 같습니다.

- 프로젝트가 아직 opt-in되지 않음
- 로컬 거절 상태
- qmd CLI가 없거나 지원 버전이 아님
- qmd 백그라운드 서버가 응답하지 않음
- 검색 결과가 `minScore` 기준을 넘지 못함
- sandbox/headless 환경이라 훅이 비활성화됨

원인을 확인하려면 에이전트에게 진단을 요청하세요.

```text
왜 컨텍스트가 안 들어오는지 qmd recall 진단해줘.
```

에이전트는 필요하면 recall 로그를 켜고 `no_collections`,
`daemon_unreachable`, `query_failed`, `no_results_after_filter`, `selected`
같은 내부 reason 값을 확인합니다.

### settings에 등록된 폴더를 지웠을 때

`.auto-context/settings.json`의 `collectionPaths`가 가리키는 폴더 자체가
없어지면, 다음 update가 해당 기억 대상을 제거하고 settings에서도 정리합니다.
폴더 안의 파일만 삭제한 경우에는 일반 qmd update가 삭제를 반영합니다.
Claude의 성공한 Bash 작업은 프로젝트 안에 등록된 컬렉션의 실제 파일 상태로
생성·수정·삭제·이름 변경을 감지합니다. 프롬프트가 끝날 때 변경을 모아
백그라운드에서 반영하고, 중단된 감지는 다음 세션 시작 때 복구합니다.
프로젝트 밖의 등록 경로나 백그라운드 Bash 작업은 이 감지 대상에서 제외됩니다.
그 밖에 놓친 변경은 “놓친 변경을 동기화해줘”라고 요청하거나 sync 스킬로 반영합니다.

### 예전 글로벌 훅을 정리하고 싶을 때

```text
예전 글로벌 qmd 훅 정리 계획 확인해줘.
문제 없으면 정리까지 해줘.
```

명시적으로 실행한 경우에만 기존 글로벌 qmd 훅이나 managed LaunchAgent cleanup을
수행합니다.

## 더 자세히 보기

- 설정 레퍼런스: [docs/settings.md](docs/settings.md)
- 데이터 처리와 개인정보: [docs/privacy.md](docs/privacy.md)
- 유지보수자용 구조: [docs/architecture.md](docs/architecture.md)
- 프로젝트별 설정: `.auto-context/settings.json`
- qmd 백그라운드 로그: `~/.cache/qmd/`

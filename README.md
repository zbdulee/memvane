# MemVane

MemVane은 프로젝트 안의 문서, 메모, wiki를 자동으로 찾아서
에이전트 대화에 넣어 주는 플러그인입니다.

매번 "이 문서도 참고해"라고 붙여 넣지 않아도, 에이전트가 질문과
관련된 내용을 프로젝트의 위키 색인에서 찾아 컨텍스트로 받습니다.

지원 에이전트:

- Claude Code
- Codex
- Hermes Agent

설치 후 사용자는 터미널 명령을 외우지 않아도 됩니다. 프로젝트 온보딩,
인덱스 갱신, recall 진단 같은 작업은 에이전트에게 자연어로 요청하면
에이전트가 필요한 스킬과 내부 명령을 선택해 처리합니다.

내부 구조와 유지보수 정보는 [docs/architecture.md](docs/architecture.md)를
보세요.

## Quickstart (English)

MemVane injects relevant project wiki context into Claude Code, Codex,
and Hermes Agent sessions. Install this plugin for your host, then ask the
`setup` skill to onboard the project onto its memvane-db index. Onboarding reads a
minimal `.memvane/settings.json` (`{name, skipPaths}`; a project that only has
`.auto-context/` is read from there), shows reviewed plans, and applies only on
explicit request. Projects that are not onboarded get no indexing, recall,
compile or learning work; their SessionStart shows one onboarding notice line
when they have `.memvane/settings.json`.
The local embedding runtime uses Node 22, 24 or 26 and a prebuilt native
package for Apple Silicon macOS (arm64).

| Host | Installation route | Runtime support |
|---|---|---|
| Claude Code | Claude plugin marketplace | recall, update, gate, post-edit sync, wiki compile |
| Codex | Codex plugin marketplace | recall, update, gate, post-edit sync, wiki compile |
| Hermes Agent | Hermes plugin install | recall, update, gate, post-edit sync, wiki compile |

For data handling, including the optional wiki-compile path, see
[docs/privacy.md](docs/privacy.md). For commands and Korean-language setup,
continue below.

## 설치

### MacBook에서 시작하기

1. **준비:** Apple Silicon Mac에서 사용할 Claude Code 또는 Codex를 설치하고
   로그인합니다. 훅이 실행할 Python 3.9 이상과 Node 22·24·26 중 하나를
   준비합니다. 로컬 임베딩 런타임(Node, node-llama-cpp, 임베딩 모델)은 플러그인
   설치만으로 생기지 않습니다. 새 컴퓨터에서는 `setup`이 npm registry와 모델
   다운로드 범위를 보여 주고, 승인한 뒤에만 런타임을 준비합니다.
2. **플러그인 설치:** 사용할 호스트의 아래 명령을 실행합니다.
3. **프로젝트 온보딩:** 대상 프로젝트를 호스트에서 열고 플러그인과 표시된 훅
   명령을 검토·신뢰합니다. 이어서 에이전트에게 “이 프로젝트 auto-context
   온보딩해줘”라고 요청합니다. 계획에서 대상 컬렉션·임베딩 런타임·자원 범위를
   확인한 뒤에만 준비와 활성화를 요청합니다.

대상 프로젝트 디렉터리에서 사용할 호스트의 명령만 실행합니다. Claude의
`--scope project`는 해당 프로젝트 설정에 설치합니다. Codex CLI의
`plugin add`에는 프로젝트 scope 옵션이 없으므로, 설치 뒤 대상 프로젝트의
공식 플러그인 화면에서 활성화 범위와 훅 신뢰를 별도로 확인합니다.

```bash
# Claude Code: 대상 프로젝트에서 실행
claude plugin marketplace add zbdulee/memvane --scope project
claude plugin install memvane@memvane-marketplace --scope project

# Codex: marketplace와 플러그인 등록 후 대상 프로젝트에서 신뢰 검토
codex plugin marketplace add zbdulee/memvane --sparse .agents/plugins
codex plugin add memvane@memvane-marketplace
```

Codex CLI에서는 `/plugins`에서 설치 상태를 볼 수 있습니다. 훅을 자동으로
모두 신뢰하거나 다른 프로젝트까지 권한을 넓히지 말고, 대상 프로젝트에서
실제 표시된 명령만 검토하세요. Hermes Agent는 별도로
`hermes plugins install zbdulee/memvane` 후
`hermes plugins enable memvane`를 사용합니다.

제품 설치는 각 에이전트의 plugin/marketplace 흐름을 사용합니다. 이 저장소는
사용자용 `install.sh` 또는 `uninstall.sh`를 제공하지 않습니다.

온보딩 상태는 에이전트에게 자연어로 요청하세요. 예를 들면 “MemVane 색인 준비
상태 보여줘”, “인덱스 활성화해줘”, “MemVane 인덱스 전환을 되돌려줘”라고 말할 수
있습니다. 첫 색인 빌드는 비동기로 진행되며, 빌드가 봉인되고 활성화될 때까지
검색 경로는 선택되지 않습니다. 임베딩 런타임 준비만으로 학습은 활성화되지
않습니다.

### 플러그인 업데이트

Claude Code의 프로젝트 설치본은 다음 명령으로 갱신합니다.

```bash
claude plugin marketplace update memvane-marketplace
claude plugin update memvane@memvane-marketplace --scope project
# 사용자 범위(user scope)로 설치했다면 --scope 없이 실행합니다.
claude plugin update memvane@memvane-marketplace
```

Codex는 `codex plugin marketplace upgrade memvane-marketplace` 후
`/plugins`에서 설치본을 확인하고 호스트가 제공하는 갱신 절차를 따릅니다.
새 실행 코드의 훅 신뢰를 다시 검토하세요.

호스트 플러그인이 갱신되면 SessionStart의 백그라운드 작업이 선택된 프로젝트의
설치를 새 번들로 옮깁니다. 옮기는 동안 검색은 마지막으로 게시된 색인을 읽기
전용으로 쓰고, 동기화·wiki compile은 업그레이드가 끝날 때까지 기다립니다.
임베딩 provider가 바뀌어 다시 빌드해야 하면 SessionStart 안내가 그 사유를
보여 줍니다. 이때는 에이전트에게 안내 내용을 설명하고 다음 단계를 제안해
달라고 요청하세요.

플러그인 자체를 제거할 때는 호스트의 공식 명령인
`claude plugin uninstall memvane@memvane-marketplace --scope project`
또는 `codex plugin remove memvane@memvane-marketplace`를
사용합니다. 제거는 플러그인 설치 해제이며 프로젝트 데이터 복구가 아닙니다.
Codex 설치는 다른 프로젝트에도 영향을 줄 수 있으므로 제거 전에 사용 범위를
확인하세요.

### `qmd-auto-context` 플러그인에서 옮기기

`qmd-auto-context@qmd-auto-context-marketplace`가 설치된 호스트는 새 플러그인
`memvane@memvane-marketplace`를 설치하고, 프로젝트를 한 번 연 뒤 이전 플러그인을
제거합니다. 새 번들은 SessionStart에서 이전 플러그인 id의 번들에 묶인 설치를 일반
플러그인 업데이트와 같은 경로로 넘겨받습니다. 넘겨받은 route는 이전 번들이 읽지 않는
형식이라 이전 번들은 그 설치를 되가져가지 못합니다. 대신 제거 전까지 이전 플러그인의
훅이 그 프로젝트에서 exit 3으로 끝나 훅 오류가 표시됩니다(작업을 막지는 않습니다).
그래서 넘겨받기를 확인한 직후 이전 플러그인을 제거합니다. 이전 플러그인을 먼저 제거하고
새 플러그인을 설치해도 됩니다. 그때는 오류 표시가 없고, 새 플러그인이 설치되기 전까지
그 프로젝트에 recall이 들어가지 않습니다.

```bash
# Claude Code (프로젝트 범위 설치 예시. 사용자 범위면 --scope를 뺍니다)
claude plugin marketplace add zbdulee/memvane --scope project
claude plugin install memvane@memvane-marketplace --scope project
#   → 대상 프로젝트를 열어 SessionStart 안내로 넘겨받기를 확인합니다
claude plugin uninstall qmd-auto-context@qmd-auto-context-marketplace --scope project
claude plugin marketplace remove qmd-auto-context-marketplace

# Codex
codex plugin marketplace add zbdulee/memvane --sparse .agents/plugins
codex plugin add memvane@memvane-marketplace
#   → 대상 프로젝트를 열어 넘겨받기를 확인합니다
codex plugin remove qmd-auto-context@qmd-auto-context-marketplace
codex plugin marketplace remove qmd-auto-context-marketplace
```

Hermes Agent를 쓰면 이전 플러그인부터 제거합니다. 이전 Hermes 플러그인은 새 route를
읽지 못하고 그 프로젝트의 편집을 막습니다(Claude·Codex의 이전 훅은 오류만 표시하고 막지
않습니다). 그래서 이전 Hermes 플러그인이 설치돼 있는 동안 새 플러그인은 설치를 넘겨받지
않고 마지막 색인을 읽기 전용으로 씁니다. SessionStart 안내가 그 사유를 보여 줍니다.
비활성화만으로는 부족하고 제거해야 합니다.

```bash
hermes plugins remove qmd-auto-context
hermes plugins install zbdulee/memvane
hermes plugins enable memvane
```

모든 설치의 상태 디렉터리는 `~/.local/state/memvane/<install id>`입니다. 이전
플러그인이 `~/.local/state/laya/<install id>`에 만든 설치는 그 프로젝트의 다음
SessionStart 뒤 백그라운드 워커가 같은 볼륨 안에서 `~/.local/state/memvane`으로
옮깁니다. 옮기는 동안 SessionStart가 안내 한 줄을 보여 주고 검색은 계속 결과를
돌려줍니다. 공유 디렉터리(`embed-runtime`, `service`, `auto-context`,
`initial-builds`)는 마지막 설치가 옮겨지고 아무것도 참조하지 않을 때 정리되며, 그때
`~/.local/state/laya`도 사라집니다. 업데이트 직후 sync가 돌고 있으면 이동은 다음
세션으로 미뤄집니다. 남은 항목과 사유는 아래 명령으로 보고, 열지 않은 프로젝트까지
지금 옮기려면 `move-state-roots`를 씁니다. `ROOT`는 설치된 플러그인 캐시
(예: `~/.claude/plugins/cache/memvane-marketplace/memvane/<버전>`)이며, 저장소
checkout에서 실행하면 번들 단계가 `host_layout_unknown`으로 거절됩니다.

```bash
python3 -B "$ROOT/core/setup_entry.py" state-root-status
python3 -B "$ROOT/core/setup_entry.py" move-state-roots
```

학습 상태(이전 플러그인 지원 디렉터리의 `live-selection`, `laya-inference`)도
같은 워커가 한 번 `~/Library/Application Support/memvane`으로 옮깁니다. 옮기는 동안
resident service가 잠시 멈추고 검색은 lexical 결과를 돌려줍니다. 준비된 local
selector는 업데이트 뒤 워커가 어댑터 앵커·synthetic attestation·inference 준비를
다시 돌려 스스로 복구합니다(각 120초 이내). 학습 profile을 승인한 프로젝트는
앵커가 바뀌면 다시 승인해야 합니다.

### 프로젝트 폴더 `.memvane/`

프로젝트별 설정·위키 카드·런타임 파일은 프로젝트 루트의 `.memvane/`에 있습니다.
`.auto-context/`만 있는 선택된 프로젝트는 다음 SessionStart 뒤 백그라운드 워커가
폴더 이름을 `.memvane/`로 한 번 바꾸고(색인은 다시 임베딩하지 않습니다), 그동안
검색은 계속 결과를 돌려줍니다. 옮긴 뒤 SessionStart 안내가 보여 주는 대로 이름
변경을 커밋합니다.

```bash
git add -A .memvane .auto-context
```

프로젝트 `.gitignore`에 `.auto-context` 줄이 있으면 같은 안내가 대응하는
`.memvane/...` 줄을 보여 줍니다. 플러그인은 `.gitignore`를 고치지 않으므로 그 줄을
직접 추가합니다. 폴더를 옮기기 전 브랜치를 체크아웃하거나 병합해 두 폴더가 함께
있으면 MemVane은 `.memvane/`를 쓰고, SessionStart 안내가 정리 방법을 알려 줍니다.

### 예전 QMD backend 정리

이전 버전이 설치한 QMD daemon, managed LaunchAgent, 플러그인 QMD runtime과
공유 store는 소유자가 한 번 정리합니다. Claude와 Codex 호스트 캐시가 모두 이
버전을 읽고, 모든 설치가 이 번들로 옮겨져 MemVane 임베딩 런타임으로 재증명된 뒤에
요청하세요.

```text
예전 QMD backend 정리 계획 보여줘.
계획대로 정리해줘.
```

계획은 읽기 전용이며 대상 프로세스·LaunchAgent·폴더와 크기, 정리를 막는
사유를 보여 줍니다. `~/.cache/qmd`와 `~/.config/qmd`에는 사용자가 직접 쓰는
QMD 색인·설정·모델 캐시도 들어 있으므로, 이 둘은 별도로 포함을 승인한 경우에만
지웁니다.

## 프로젝트에서 켜기

MemVane은 프로젝트별 온보딩 방식입니다. MemVane 색인이 선택되지 않은
프로젝트는 아무 것도 색인하지 않습니다.

온보딩은 `.memvane/settings.json`의 minimal 형식만 읽습니다
([minimal settings](docs/settings.md#minimal-settings)). `.auto-context/`만 있는
프로젝트는 그 폴더의 같은 파일을 읽습니다.

```json
{
  "name": "my-project",
  "skipPaths": ["node_modules", "dist"]
}
```

이 형식에서는 프로젝트 루트 전체가 위키 생성 원천이고 검색은 위키만 합니다.
`.`으로 시작하는 폴더와 `skipPaths`에 걸리는 폴더로는 위키를 만들지 않습니다.
`.memvane/wiki`의 위키 카드는 검색 대상입니다. 두 키 외의 키가 있으면
온보딩은 `minimal_settings_required`로 거부하고, 에이전트는 파일을 대신 고치지
않습니다. 파일이 준비되면 에이전트에게 요청하세요.

```text
이 프로젝트 auto-context 온보딩해줘.
```

설정 값의 의미는 [설정 레퍼런스](docs/settings.md)를 참고하세요. 선택된 뒤
`skipPaths`를 바꾸면 선택이 유지되고, `name`을 바꾸면 경로가
`route_settings_changed`로 막힙니다.

첫 확인은 에이전트에게 “이 프로젝트 문서 한 건이 검색되는지와 출처 링크를
보여줘”라고 요청하세요. “위키 카드 한 건을 만들고 검증 상태와 원문 위치
링크를 확인해줘”라고 요청할 수도 있습니다. 생성·검증·색인 작업은 비동기 큐에
잠시 대기할 수 있습니다. 큐 전체가 비어야 정상인 것은 아니며, 확인하려는 카드가
아직 검색되지 않으면 그 카드의 상태를 확인하세요.
위키 카드만 MemVane 검색·주입 대상이고, 원문 파일 자체를 자동 주입하지 않습니다.
위키 카드 안의 발췌와 원문 링크는 정상 콘텐츠입니다. 준비된 로컬 MemVane 선택
모델이 있으면 검증된 검색 후보 최대 15개 중 최대 3개를 고릅니다. 모델이
준비되지 않았거나 질의 시점에 응답하지 못하면 검증된 후보를 하이브리드 순위대로
최대 3개 주입합니다. 질의 벡터를 쓸 수 없으면(임베딩 상주 프로세스 미기동·리소스
유예) 공개된 세대를 어휘 검색만으로 찾고 상주 프로세스 기동을 요청합니다.
`skipPaths`를 설정하면 일치하는 원문 폴더를 새 위키 생성 대상에서 빼고, 해당
출처를 가진 기존 위키도 검색에서 숨깁니다. 기존 위키와 원문 파일은 삭제하지
않습니다. 설정하지 않으면 추가 제외는 없습니다.
로컬 임베딩 모델은 사용자당 하나인 상주 서비스가 한 번만 올려 모든 프로젝트의
동기화·질의에 재사용합니다. 플러그인이 업데이트되면 진행 중인 요청을 마친 뒤 새
버전으로 교체되고, 교체하는 동안 질의는 어휘 검색으로 답합니다. memvane-db의 SQLite
검색 자체는 질문마다 실행되며 상주 검색 서버나 OS 부팅 서비스가 아닙니다. 모델
준비 중이나 자원 압박으로 질의가 보류되면 그 요청은 검색 성공으로 보지 말고
상태를 확인하세요.

선택을 되돌리고 싶을 때는 이렇게 요청합니다.

```text
이 프로젝트 MemVane 인덱스 전환을 되돌려줘.
```

선택된 색인이 아직 새 쓰기를 받지 않았을 때만 되돌립니다. 이미 새 쓰기가
있으면 `rollback_forward_only`로 선택이 유지됩니다.

## 에이전트에서 쓰는 흐름

온보딩이 끝나면 평소에는 별도 명령을 외울 필요가 없습니다.

1. 에이전트 세션을 시작하면 전체 스캔 동기화가 백그라운드 큐에 들어갑니다.
2. 사용자가 질문합니다.
3. 마지막으로 게시된 위키 색인에 관련 카드가 있으면 모델 컨텍스트에 추가됩니다.
4. 파일을 편집하면 같은 동기화가 큐에 들어가고, 바뀐 원문은 위키 compile
   대상이 됩니다.

검색 결과가 없으면 훅은 컨텍스트 없이 끝납니다. 선택된 경로의 증명이 실패하면
훅은 다른 검색 경로로 넘어가지 않고 실패로 끝납니다.

## 편집 전 gate

선택된 프로젝트에서 `Edit`, `Write`, `apply_patch` 같은 편집 도구를 쓰면 gate가
편집 payload와 선택된 경로를 확인한 뒤 편집을 허용합니다. 온보딩되지 않은
프로젝트에서는 gate가 아무 작업도 하지 않습니다.

## Wiki Compile

wiki compile은 원문 Markdown을 편집하면 `.memvane/wiki`에 정리된 wiki
초안을 만듭니다.
처음 생성된 문서는 `generated` 상태이며, 별도 자동 검증이 원문과 생성 문서를
다시 대조해 통과한 문서만 `verified`로 승격합니다. 사용자가 모든 문서를
일일이 검수하는 흐름이 아니라, AI 생성 결과를 한 번 더 걸러 안전하게 쓰기
위한 2중 필터입니다.

minimal settings로 온보딩한 프로젝트는 wiki compile이 켜진 상태입니다.

새로 만들거나 고쳐 쓰는 문서 카드는 로컬 선택 모델이 한 번에 읽을 수 있는 길이를 목표로 합니다.
요청과 카드를 합쳐 1024토큰이므로 카드 본문 목표는 700토큰이고, 길이는 선택 모델의 토크나이저로
셉니다. 넘는 카드는 주제별 카드 여러 장으로 한 번 나누어 다시 추출하고, 나뉜 카드는 frontmatter
`related`로 서로를 가리킵니다. 나눈 결과를 온전히 쓸 수 없으면 원래 카드를 쓰고, 그래도 넘는 카드는 그대로 쓰며, 선택 모델은 그런 카드를
앞부분만 읽어 채점합니다. 선택 모델 토크나이저가 아예 없는 머신에서는 길이를 재지 않고 씁니다. 이미 있는
긴 카드를 소급해 나누지는 않습니다(고쳐 쓸 때는 같은 규칙을 따릅니다). 자세한 계산은 [architecture.md](docs/architecture.md#card-body-budget)에 있습니다.

minimal settings로 색인 활성화가 끝난 프로젝트는 다음 세션 시작이나 수동 동기화 때 기존 Markdown의
첫 위키 생성도 시작합니다. 설정 파일만 있는 미온보딩 프로젝트에서는 실행하지
않습니다. 첫 이벤트에서 파일과 내용 지문 목록을 한 번 고정하고, 기존 호출 예산에
맞춰 나누어 처리합니다. 앞 배치의 실제 처리가 끝난 뒤 다음 배치를 자동으로 이어
가므로 재시작해도 이미 처리한 지문을 다시 유료 추출하지 않습니다. 첫 목록에 없던
파일과 이후 바뀐 내용은 기존 편집·동기화 규칙을 따릅니다. `skipPaths`, 숨김 경로,
도구 제어 파일과 빌드 산출물은 이 첫 생성에서도 제외합니다.

생성과 검증은 기본적으로 이 작업을 시작한 호스트의 CLI를 사용하며 추론 강도는
`low`입니다. Codex에서 시작한 작업이 다른 설치된 CLI로 바뀌지 않습니다. 호스트를
확인된 호스트의 CLI를 사용할 수 없으면 작업을 보류합니다. 호스트가 불명확한
경우에만 실제 생성 작업을 Codex → Claude → Hermes 순으로 각각 한 번씩 시도하고,
처음 성공한 CLI를 생성·검증 엔진으로 기록합니다. 별도 시험 프롬프트는 보내지 않습니다.
실패한 시도도 run당 추출 호출 예산에 포함됩니다. 예산이 중간에 끝나면 같은
소스 지문과 시도한 CLI 목록을 큐에 보존하고 다음 run에서 남은 CLI만 이어갑니다.
같은 엔진의 검증은
`self`로 기록하며, 명시적으로 설정한 교차 엔진 검증 정책은 별도로 유지됩니다.
모델은 각 CLI의 기본 선택을 사용합니다. 추론 강도 제어를 지원하지 않는 Hermes
adapter에서는 `low` 요청과 지원 불가 사실을 기록하며 실제 적용을 주장하지 않습니다.
legacy 설정에서 세션 시작·동기화 trigger를 명시적으로 끈 경우에는 그 선택을 따릅니다.

진행은 “초기 위키 생성 진행 상태 보여줘”처럼 로그 확인을 요청하면 볼 수 있습니다.
`wiki.backfill`의 `pending`은 미투입, `dispatched`는 큐에서 처리 중,
`settled`는 처리 시도가 끝난 문서, `skipped`는 목록 고정 후 변경·제외된 문서입니다.
`settled`와 `complete`는 카드 생성이나 검증 성공 수가 아니며, 실제 카드 결과는
`wiki`와 자동 검증 이벤트로 확인합니다.
건너뛴 문서가 있으면 최종 상태는 `complete_with_skips`입니다. 호스트 불명확 상태의
세 CLI가 모두 실패한 문서도 이유를 기록하고 이 상태로 끝내며, 자동으로 같은
유료 호출을 반복하지 않습니다. 이후 명시적인 소스 이벤트나 수동 재시도로 다시
시도할 수 있습니다.

`.memvane/wiki`에는 카드만 있습니다. 카드 작성 규칙은 플러그인 번들의
`core/wiki_card_rules/`(기본·소설 프리셋)에 있고, 카드 목록은 memvane-db 색인,
생성·수정·삭제 기록은 설치 state 디렉터리 이벤트 로그(`events.jsonl`)의 `wiki`
이벤트입니다. 이전 버전이
만든 `SCHEMA.md`·`index.md`·`log.md`가 wiki 루트에 남아 있어도 카드로 취급하지
않고 읽지도 않으므로, 지우든 두든 동작은 같습니다.

자동 compile은 원문 범위 안의 Markdown만 대상으로 합니다.
`.agents`, `.claude`, `.codex`, `.github`, `.memvane`처럼 점으로 시작하는
도구/메타데이터 경로는 자동 compile 대상에서 제외됩니다.

자동 검증이 실패하면 기본 설정에서는 해당 생성 문서를 삭제합니다. 검증기가
없거나 timeout이 나거나 판단이 불확실한 경우에는 큐나 `generated` 상태가
보존됩니다. `generated` 카드처럼 자동 신뢰 조건(`status: verified`,
`createdBy: memvane` 또는 이전 플러그인 이름 `qmd-auto-context`, 비어 있지 않은 `sourceRevisions`)을 갖추지 못한
카드는 `recallVerifiedOnly` 값과 관계없이 recall에 주입되지 않습니다.

민감한 원문 폴더는 `skipPaths`에 넣어 새 위키 생성 대상에서 뺍니다.

## 에이전트에게 요청하기

필요할 때는 명령어 대신 에이전트에게 아래처럼 요청하는 흐름을 권장합니다.

| 요청 | 용도 |
|---|---|
| `이 프로젝트 auto-context 온보딩해줘.` | MemVane 색인 온보딩 |
| `이 질문에 어떤 문서가 recall되는지 확인해줘.` | recall 결과 확인 |
| `인덱스 새로고침해줘.` | 선택된 색인의 전체 스캔 동기화 큐잉 |
| `MemVane 임베딩 런타임 상태 보여줘.` | 임베딩 런타임 확인·준비·정리 |
| `예전 QMD backend 정리 계획 보여줘.` | 예전 QMD backend 정리 점검 |
| `MemVane 로그 보여줘.` / `왜 recall이 비었는지 로그 확인해줘.` | 설치 이벤트 로그 요약(recall·오류·업그레이드) |

## 문제 해결

### 컨텍스트가 안 들어오는 것 같을 때

빈 출력은 정상일 수 있습니다. 대표적인 이유는 다음과 같습니다.

- 프로젝트가 아직 온보딩되지 않음
- 확인하려는 변경이 아직 게시된 색인에 반영되지 않음
- 검색 결과가 필터를 통과하지 못함
- sandbox/headless 환경이라 훅이 비활성화됨

원인을 확인하려면 에이전트에게 진단을 요청하세요.

```text
왜 컨텍스트가 안 들어오는지 recall 진단해줘.
```

에이전트는 `logs` 스킬로 설치 이벤트 로그의 `recall` 이벤트를 읽어 원인을
확인합니다. `reason`은 최종 결과(`selected`, `no_results_after_filter`, 거부 코드
등)이고, `notes`는 그 과정의 진단 코드, `cards`는 주입한 카드 경로입니다. 이벤트
로그는 항상 기록되므로 따로 켤 설정이 없습니다. 기록 범위는
[docs/privacy.md](docs/privacy.md)를 보세요.

### 파일을 지우거나 옮겼을 때

동기화는 매번 전체 스캔으로 생성·수정·삭제·이름 변경을 맞춥니다. 세션 시작과
편집이 동기화를 큐에 넣고, Claude의 성공한 Bash 작업도 같은 동기화를 깨웁니다.
프로젝트 밖 경로는 그 프로젝트 자신의 동기화가 맞춥니다. 놓친 변경이 있으면
“인덱스 새로고침해줘”라고 요청하세요.

## 더 자세히 보기

- 설정 레퍼런스: [docs/settings.md](docs/settings.md)
- 데이터 처리와 개인정보: [docs/privacy.md](docs/privacy.md)
- 유지보수자용 구조: [docs/architecture.md](docs/architecture.md)
- 프로젝트별 설정: `.memvane/settings.json`

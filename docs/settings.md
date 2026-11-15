# 프로젝트 설정

## Minimal settings

프로젝트의 `.memvane/settings.json`은 온보딩 전에 준비합니다. 에이전트에게 설정을 요청하거나 직접 편집할 수 있습니다.

```json
{
  "name": "my-project",
  "skipPaths": ["node_modules", "dist"],
  "diagnostics": {"enabled": false}
}
```

허용하는 최상위 키는 `name`, `skipPaths`, `diagnostics`, `contextLearning`, `sharedLearning`입니다. 이 밖의 키가 있거나 유효한 `name` 또는 `skipPaths`가 없으면 설정 전체를 사용할 수 없으며 온보딩은 `minimal_settings_required`로 거절합니다. `collections`, `collectionPaths`, `minScore`, `topN`, `compile`, `semanticDedup` 등은 설정 파일에 적는 키가 아닙니다.

- `name`: 필수 문자열입니다. 영문자 또는 숫자가 하나 이상 있어야 하며, 이를 정규화해 raw collection 이름과 `-wiki` 접미사가 붙은 wiki collection 이름을 만듭니다.
- `skipPaths`: 제외할 경로 문자열의 배열입니다. 원문 경로의 상위 디렉터리에 항목이 포함되면 위키 생성에서 제외합니다. 기존 카드에도 제외 규칙이 적용됩니다. 점으로 시작하는 경로와 `.memvane`은 원문 생성 대상에서 제외합니다. `.memvane/wiki`의 카드는 검색 대상입니다.
- `contextLearning`: 선택 객체이며 `capture`, `autoCycle`에 boolean을 사용합니다. 생략한 필드의 기본값은 모두 `true`입니다.
- `sharedLearning`: 선택 객체이며 `enabled`, `autoApply`는 boolean, `root`는 절대 경로 문자열, `profile`은 `qmd-local-learning-profile-v1`입니다.

학습 블록의 형식이 틀리면 두 블록을 무시하고 `learning_settings_invalid`를 기록합니다. 나머지 프로젝트 설정은 유지합니다.

### diagnostics — 상세 진단 동의 (opt-in)

`diagnostics.enabled`가 `true`일 때 상세 진단을 허용합니다. 기본값은 꺼짐입니다. 상세 진단에는 예외 메시지와 자식 프로세스 stderr가 포함될 수 있으며 설치별 비공개 이벤트 로그에 저장합니다.

프로젝트 루트가 raw 원천이고 `.memvane/wiki`가 wiki 원천입니다. 검색 색인에는 위키 카드만 게시합니다. 원문은 카드 생성과 출처·최신성 확인에 쓰입니다. 카드 생성·검증의 동작과 한도는 플러그인 기본값으로 정합니다. 기본 예산에서 한 run의 유료 호출 최악 상한은 최대 141회이며, 검증 backlog의 기아 방지 예약 한 번을 포함합니다.

선택된 route는 설정에서 유도한 프로젝트·collection·wiki 루트 identity를 묶습니다. `name` 변경으로 identity가 달라지면 route가 거절됩니다. `skipPaths` 변경은 다음 sync가 게시하는 색인에 반영되며, 필요한 reader 재봉인이 끝날 때까지 recall은 컨텍스트 없이 끝날 수 있습니다.

설정 파일만으로 작업이 시작되지는 않습니다. 프로젝트를 온보딩해야 색인·recall·compile·학습이 동작합니다. 선택을 해제하려면 에이전트에게 restore를 요청하세요. 기존 설치 디렉터리는 보존되며 이후 다시 온보딩할 수 있습니다.

로컬 임베딩 런타임은 `~/.local/state/memvane/v1/embed-runtime`에 있습니다. 설치와 다운로드는 setup skill의 검토·승인을 따릅니다. 진단과 데이터 처리 범위는 [privacy.md](privacy.md)를 참고하세요.

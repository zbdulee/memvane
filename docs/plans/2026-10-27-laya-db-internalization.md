# laya-db 내재화 — QMD 제거 계획

상태: 소유자 결정 확정(2026-10-27). 각 항목은 별도 PR로 진행한다.

## 목표

- 플러그인의 검색·인덱스 저장소는 프로젝트별 **laya-db**(`core/laya_*.py`) 하나다. 학습 런타임(Laya 모델)과 구분해 이 이름을 쓴다.
- QMD 관련 로직과 패키지는 최종적으로 모두 제거한다. 마이그레이션이 끝나면 마이그레이션 로직(QMD 원본 전환, 벡터 재사용, cutover·restore, `laya_qmd_migration`)도 제거한다.
- 사용자의 전역 QMD 설치와 데이터는 건드리지 않는다.

## 제품 모델

- **검색은 항상 wiki만 한다.** raw 문서 검색은 지원하지 않는다.
- **wiki는 항상 자동 생성된다.** 호스트 에이전트(Claude/Codex/Hermes)에 맞는 extractor를 플러그인이 고른다.
- **학습(contextLearning)은 항상 켜진다.**
- 인덱싱 on/off는 프로젝트 settings가 아니라 공용 설정에서 관리한다.

## settings 모델

프로젝트 `.auto-context/settings.json`에는 `name`과 `skipPaths`만 남는다.

| 필드 | 처리 |
|---|---|
| `collections`, `collectionPaths`, `collectionRoles` | 제거. 프로젝트 루트 전체가 wiki 생성 원천이다 |
| `recallStrategy` | 제거. wiki 전용 |
| `compile`(`mode`, `triggers`, `extractor`, `budget`, `semanticDedup` 등) | 제거. 플러그인이 정한다 |
| `contextLearning` | 제거. 항상 켜짐 |
| `indexing` | 제거. 공용 설정 |
| `minScore`, `topN`, `queryTimeout`, `prefixStyle`, `lexicalPatterns`, `allowRoots` 등 | 제거. 플러그인 기본값 |
| `name` | 유지 |
| `skipPaths` | 유지. **wiki 생성에서 제외할 경로**다. 생성되지 않은 내용은 검색되지 않는다 |

기본 제외:

- `.`으로 시작하는 모든 폴더.
- `.auto-context`는 wiki 생성 원천에서 제외되고, 그 안의 wiki는 검색 대상이다.

기존 settings는 읽을 때 새 모델로 해석하고, 마이그레이션 시 `name`·`skipPaths`만 남도록 다시 쓴다.

## wiki 수명 주기

laya-db에 기록된 카드별 원문 경로와 해시로 판정한다. SessionStart와 원문 편집 감지 시점에 실행한다.

| 원문 상태 | 처리 |
|---|---|
| 원문 파일 없음 | 카드 삭제, laya-db에서 제거 |
| 원문 해시 변경 | 재생성 queue에 넣고, 새 카드로 laya-db 갱신 |
| 카드에 원문 지문(`sourceRevisions`) 없음 | 카드 삭제, laya-db에서 제거 |
| 같음 | 유지 |

`source-missing.jsonl`과 수동 `wiki-source-repair` 스킬은 이 자동 처리로 대체한다.

## 자원 관문

검색·인덱싱·임베딩은 시스템 여유 메모리 10% 미만에서만 OS 압박으로 거부한다(0.36.25). 학습용 규칙은 별도로 정한다.

## 작업 항목

1. **번들 자동 업그레이드**: 플러그인이 업데이트되면 SessionStart worker가 설치 커밋에 묶인 laya-db 빌드(비활성·활성)를 새 번들로 옮긴다. 사람이 `upgrade-*` 명령을 돌리지 않는다.
2. **settings 모델**: 위 표대로 구현하고 각 프로젝트 settings를 정리한다.
3. **wiki 수명 주기**: 위 표대로 구현한다.
4. **임베딩 런타임 독립**: node, `node-llama-cpp`, 네이티브 파일, 모델 저장소를 laya-db가 직접 소유한다. QMD 패키지 경로를 참조하지 않는다.
5. **상주 서비스**: QMD 데몬과 backend manager를 laya-db 상주 서비스로 교체한다. 필요한 부분은 [tobi/qmd](https://github.com/tobi/qmd)(MIT)에서 가져온다.
6. **수동 스킬**: `query`, `sync`, `update`, `wiki-compile`, `enable-compile`, `wiki-source-repair`를 laya-db 기준으로 바꾸거나 제거한다.
7. **결합 제거**: `recall.py`와 `qmd_*` import, `~/.cache/qmd` 로그 경로, 공용 QMD owner registry 의존을 걷어낸다.
8. **worktree 정체성**: 같은 저장소의 여러 worktree를 구분하고 컬렉션 이름 충돌을 없앤다.
9. **벡터 검색 확장성**: 전수 스캔(2초 제한)을 대용량에서도 동작하게 바꾼다.
10. **QMD 제거**: 모든 프로젝트 전환 뒤 마이그레이션 로직, 공용 QMD DB, 플러그인 소유 QMD 런타임을 제거한다.
11. **플러그인 교체**: 이름을 바꾼 새 플러그인으로 넘어간 뒤 현재 플러그인을 삭제한다.

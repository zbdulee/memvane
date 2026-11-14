# 버전 관리

### 버전 bump 체크리스트
릴리스 버전을 올릴 때는 모든 host manifest와 테스트 기대값을 같은 버전으로 맞춘다.

1.x에서는 다음 영속 계약을 유지한다. 모양을 바꾸려면 메이저 버전을 올린다.

- store·journal DDL, application id와 `user_version`.
- route·ledger·build·attestation JSON의 키 집합과 schema id.
- 프로젝트 settings 허용 키: `name`(필수), `skipPaths`, `diagnostics`, `contextLearning`, `sharedLearning`. `contextLearning`: `capture`(bool), `autoCycle`(bool). `sharedLearning`: `enabled`(bool), `root`(절대 경로 문자열), `profile`(`qmd-local-learning-profile-v1`만), `autoApply`(bool). 학습 블록 모양이 틀리면 학습만 기본값이고, 그 밖의 키가 하나라도 있으면 설정 전체를 빈 설정으로 본다.
- plan `scopeId`: 배열(`"fresh-wiki-only"`, 프로젝트 경로, 프로젝트 `st_ino`, settings 파일 sha256)의 compact JSON을 sha256한 값의 앞 32 hex. `st_dev`는 넣지 않는다(APFS는 부팅마다 장치 번호를 바꿀 수 있다). 재실행한 plan과 prepare가 기록된 `scopeId`와 같은 값을 내야 한다.
- initial build 기록 `build.json`(`memvane-initial-build-v1`)과 프로젝트 포인터 `<state base v1>/initial-builds/<프로젝트 루트 SHA-256 앞 32 hex>.json` 및 그 보관본 `retired-build-pointer.json`(`memvane-initial-build-pointer-v1`, 키 `schema`·`projectRoot`·`stateRoot`·`installId`).
- sync owner·`sync.lock`·wiki mutation lock 및 socket의 이름과 위치. 설치별 socket은 `<state base v1>/run/<설치 id의 SHA-256 앞 16 hex>.sock`이다.
- wake kind 집합(`laya-sync`·`laya-compile`·`learning`·`auto`)과 target 모양(절대 경로), `recipe_id`·`profile_id` 입력. 1.x 호스트는 모르는 kind의 행을 남겨 두고, 출시 전 빌드가 남긴 `index`·`update`·`cleanup` 행만 `retired_kind` 사유로 지운다.
- 카드 frontmatter 필드와 `.memvane/compile/*.jsonl` 행 모양.
- attestation `providerRelativePath`와 onboarding artifact sha map에 기록되는 provider 파일 상대 경로(`memvane/provider/adapters/local_embed_provider.py`·`laya_local_embed_worker.mjs`)와 접두사 `memvane/provider/adapters/`. 정의는 `memvane/kernel/bundle_paths.py` 한 곳이다.
- compile intent(`<install state root>/provenance-intents/`)의 필수 키 집합(`kind`·`viewId`·`targetPath`·`cardSha`·`expectedTargetHash`·`sourceFingerprints`·`sourceSpans`·`verifiedMode`·`verifyEngine`·`producingEngine`·`verdict_digest`·`result_digest`), 이름의 id 입력, 결과 접미사(`.consumed`·`.rejected`·`.superseded`). 추가 키는 허용한다: consumer는 필수 키만 읽고 나머지는 무시하며, `result_digest`는 필수 키로만 계산한다. 한 호스트의 verify worker가 쓴 intent를 다른 1.x 호스트의 sync가 소비한다.

보호된 provider·worker 파일이나 `locks/`를 변경하는 1.x 릴리스는 모든 프로젝트의 재빌드가 필요하다.

- `package.json` — project/npm version
- `plugin.json` — root AGY/Gemini plugin metadata
- `plugin.yaml` — Hermes Agent plugin metadata
- `.claude-plugin/plugin.json` — Claude plugin metadata
- `.codex-plugin/plugin.json` — Codex plugin metadata
- `.claude-plugin/marketplace.json` — Claude marketplace entry
- `.agents/plugins/marketplace.json` — Codex marketplace entry
- `tests/plugin-manifest.test.mjs` — manifest의 `description`·`version`이 비어 있지 않은지만 단정한다(정확한 버전 일치는 검사하지 않으므로 위 목록을 손으로 맞춘다)

`docs/plans/` 문서 안의 버전 문자열은 bump 대상이 아니다. 버전 변경 후 최소 `npm run test:focused -- tests/plugin-manifest.test.mjs`를 실행하고, 릴리스 전에는 `npm test`를 실행한다.

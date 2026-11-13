# 버전 관리

### 버전 bump 체크리스트
릴리스 버전을 올릴 때는 모든 host manifest와 테스트 기대값을 같은 버전으로 맞춘다.

- `package.json` — project/npm version
- `plugin.json` — root AGY/Gemini plugin metadata
- `plugin.yaml` — Hermes Agent plugin metadata
- `.claude-plugin/plugin.json` — Claude plugin metadata
- `.codex-plugin/plugin.json` — Codex plugin metadata
- `.claude-plugin/marketplace.json` — Claude marketplace entry
- `.agents/plugins/marketplace.json` — Codex marketplace entry
- `test/plugin-manifest.test.mjs` — manifest의 `description`·`version`이 비어 있지 않은지만 단정한다(정확한 버전 일치는 검사하지 않으므로 위 목록을 손으로 맞춘다)

`docs/plans/` 문서 안의 버전 문자열은 bump 대상이 아니다. 버전 변경 후 최소 `npm run test:focused -- test/plugin-manifest.test.mjs`를 실행하고, 릴리스 전에는 `npm test`를 실행한다.

# 05 온보딩·전환·복구

새 프로젝트를 Laya 색인으로 옮기는 일은 hook이 아니라 설치자 명령으로만 진행됩니다. 입력은 minimal settings(`{name, skipPaths}`)와 `.auto-context/wiki`의 카드입니다. 순서는 다음과 같습니다.

1. 계획: `plan-fresh-wiki-origin`이 settings에서 raw(프로젝트 루트)·wiki(`.auto-context/wiki`) 컬렉션을 유도해 본문 없는 검토 제안을 만듭니다.
2. provider 검토: `review-index-provider`가 Laya 임베딩 런타임(node·node-llama-cpp·Metal native closure·GGUF 모델)과 차원으로 제안을 만듭니다. 런타임 파일은 전부 다시 해시하며, 없거나 바뀌었으면 거부합니다. `--provider-model-file`은 선택이고, 주면 런타임의 모델 파일이어야 합니다.
3. 비활성 초기 빌드 등록: `prepare-fresh-wiki-origin`이 저장소·reviewed route·attestation·journal(`candidate_ready`)·빌드 기록을 만들고 wake를 하나 요청합니다.
4. 비동기 빌드: `laya-sync` wake가 bounded 단계로 위키 카드를 임베딩해 `sealed`까지 진행합니다.
5. finalize bind: 첫 호출은 freshness를 요청하고, 그 뒤 다시 seal된 세대를 `fresh-wiki-cutover.json`(`prepared`)과 selection marker에 묶습니다.
6. 명시 승인 활성화: `activate-index-origin`이 route를 게시하고 journal을 `active`로 바꾼 뒤 selected sync wake를 하나 요청합니다.

route는 settings 파일 바이트가 아니라 해석된 settings identity(`settings_identity_digest`: 프로젝트 루트와 각 컬렉션의 이름·역할·루트)를 묶습니다. `skipPaths` 편집은 route를 유지하고, 이름 변경은 `route_settings_changed`로 막습니다.

## Laya 임베딩 런타임

provider 검토는 QMD 패키지·QMD 런타임 세대·`~/.cache/qmd`를 읽지 않습니다. `~/.local/state/laya/embed-runtime/generations/g-<id>/`(0700, symlink·hardlink 없음)의 `bin/node`, `node_modules/`(node-llama-cpp와 그 런타임 의존, `@node-llama-cpp/mac-arm64-metal`), `models/embeddinggemma-300M-Q8_0.gguf`를 읽습니다. `<id>`는 모든 파일의 상대 경로·sha256·크기·실행 비트를 담은 `runtime.json`의 digest라서 같은 바이트는 항상 같은 경로에 놓입니다. 활성 세대는 `active.json`이 가리킵니다.

- 공급: `provision-index-runtime --approve-index-runtime-copy`는 QMD 관리 런타임의 node-llama-cpp 트리와 QMD 모델 캐시의 GGUF(고정 sha256)를 복사합니다. 번들 자동 업그레이드 worker는 검토 전에 설치 attestation이 가리키는 node·module entry·native closure·모델을 attestation의 sha256과 대조하며 복사합니다. 어느 경로도 hardlink를 쓰지 않고, 아무것도 내려받지 않습니다. 원본이 없으면 `laya_embed_runtime_source_unavailable`로 거부합니다.
- 기존 설치: 바이트가 같으므로 내용 기반 재증명(`laya_provider_reproof.reattest`)이 기존 vector profile을 유지합니다. 재빌드나 벡터 변경은 없습니다. 재증명 뒤에는 QMD 디렉터리를 지워도 검색·동기화·온보딩이 런타임만 씁니다.
- 손상: 파일이 하나라도 바뀌거나 늘거나 빠지면 `laya_embed_runtime_corrupt`이고, 같은 내용의 세대를 덮어쓰지 않습니다(`laya_embed_runtime_generation_conflict`).
- `runtimeId`는 `qmd-2.5.3-node-llama-cpp-3.18.1` 그대로입니다. 고정된 provider가 이 id를 embedding fingerprint에 묶기 때문입니다.

restore는 첫 selected write 이전에만 가능하며, 이 선택이 게시한 route 파일만 철회합니다. 첫 selected write 이후에는 전진(forward) 복구만 적용됩니다. bind된 뒤 활성화하지 않을 빌드는 `retire-index-origin`으로 ledger를 `retired`로 옮기고 marker와 빌드 pointer만 놓아 줍니다. 저장소와 journal은 지우지 않습니다.

state root에 QMD-origin ledger(`qmd-cutover.json`)가 남아 있거나 프로젝트 marker가 QMD-origin schema이면, 선택기는 `blocked`(`qmd_origin_detach_required`)로 분류하고 활성화·복구·번들 업그레이드는 같은 사유로 거절합니다.

```mermaid
flowchart TD
  subgraph OWN["설치자 off-hook 경계 소유자 명시 승인"]
    PL["plan-fresh-wiki-origin 본문 없는 검토 제안"]
    PV["review-index-provider Laya 임베딩 런타임"]
    PP["prepare-fresh-wiki-origin 비활성 초기 빌드 등록"]
    FN["finalize-index-origin freshness 후 bind prepared"]
    AC{"activate-index-origin fresh bound generation"}
    RC["recover-index-origin 중단 정리"]
    RT{"restore-index-origin 첫 selected write 이전"}
    RE["retire-index-origin 선택 전 bind 철회"]
  end
  subgraph BG["비동기 초기 빌드 worker"]
    IB{"run_initial_build phase"}
    DF["deferred bind unsettled 또는 busy"]
  end
  ST[("minimal settings 와 wiki 카드")]
  CS[("candidate catalog binding store")]
  LG[("fresh-wiki-cutover.json ledger 와 selection marker")]
  RF[("laya-index-route.json identity digest")]
  NO["활성화 거부 route 미게시"]
  CAS["restored CAS 재시도 없음"]
  FW["restore 불가 전진 복구 규칙"]
  RQ["recovery_required 관측 보고"]

  ST -.->|"읽기 전용"| PL
  PL --> PV
  PV --> PP
  PP --> IB
  IB -->|"미완료"| DF
  DF -.->|"wake 유지 후 재시도"| IB
  IB --> CS
  IB -->|"sealed"| FN
  FN --> LG
  LG --> RE
  FN --> AC
  AC -->|"ledger 없음 retired 증거 불일치"| NO
  AC -->|"통과 route 게시 및 sync wake"| RF
  AC -->|"중단"| RC
  RC --> LG
  RF --> RT
  RT -->|"예"| CAS
  CAS -->|"실패"| RQ
  RT -->|"아니오 첫 write 이후"| FW
```

## Implementation and evidence

함수 이름으로 찾으세요.

- `core/setup_entry.py`: index-origin action 목록과 활성화·restore·retire·abandon 승인 플래그입니다.
- `core/laya_product_install.py` `plan_fresh_wiki_origin`: 기존 route·marker가 있거나, settings가 canonical이 아니거나, raw가 프로젝트 루트·wiki가 `.auto-context/wiki`가 아니면 거부합니다.
- `core/laya_product_install.py` `prepare_fresh_wiki_origin` / `_register_initial_build`: 활성화 없이 비활성 초기 빌드만 등록합니다. 검토한 plan·provider digest가 바뀌었으면 거부합니다.
- `core/laya_product_sync_worker.py` `run_initial_build`: 초기 빌드는 route를 선택하지 않습니다. bind가 정착되지 않았거나 owner가 busy이면 `deferred`로 두고 wake를 유지합니다.
- `core/laya_product_install.py` `finalize_initial_build`, `activate_origin_selection`, `recover_origin_selection`, `restore_origin_selection`, `retire_drifted_origin`: 위 순서의 설치자 단계입니다.
- `core/laya_fresh_wiki_origin.py`: ledger phase는 `prepared`·`switching`·`active`·`upgrading`·`restoring`·`restored`·`retired`입니다.
- `core/laya_ledger_common.py` `refuse_qmd_origin`: QMD-origin ledger 거절입니다.
- `core/laya_embed_runtime.py` `select`, `provision_from_qmd`, `ensure_for_attestation`: Laya 임베딩 런타임의 검증·공급입니다. `core/laya_product_install.py` `review_local_provider`가 이것만 읽습니다.

### QA 증거

- `test/fixtures/laya-fresh-onboarding-e2e-checks.py`: minimal settings에서 plan·prepare·빌드·finalize·activate·선택기·read-only route proof·selected recall까지 실제 저장소와 sqlite journal로 이어서 검증합니다. 호스트 설치·모델만 합성입니다.
- `test/fixtures/laya-fresh-bundle-upgrade-checks.py`: bound·selected 설치의 번들 교체와 호스트 간 캐시 교체를 실제 ledger·marker·저장소로 검증합니다.
- `test/fixtures/laya-embed-runtime-checks.py`: QMD 경로에서의 sha 대조 복사, 이동 후 재증명(같은 profile·같은 벡터), QMD 디렉터리 삭제 뒤 provider 사용, Laya 런타임만 있는 온보딩, 손상 런타임 거부를 검증합니다.
- 합성 증거가 운영 활성화·복구를 증명하지는 않습니다. 운영 적용은 [release-agent-qa.md](../../release-agent-qa.md)의 절차를 따릅니다.

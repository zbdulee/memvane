# MemVane 재부팅 후 전 훅 거부 (`inactive_or_mismatched_journal`) 원인과 수정

- 작성일: 2026-11-09
- 대상: 이 레포 (MemVane 0.44.11 → 0.44.12)
- 상태: 0.44.12 머지·설치 완료 (PR #166, 머지 커밋 `4ef2529b`)

## 증상

2026-11-09 오후부터 Claude·Codex 세션의 MemVane 훅이 전부 아래 에러로 거부됐다.

```
UserPromptSubmit hook error: [memvane] inactive_or_mismatched_journal; request refused
```

recall·gate·posttool·index·compile·update-queued·file-change-event가 모두 거부돼 컨텍스트 주입과 색인·compile이 멈췄다.

## 타임라인 (KST)

| 시각 | 사건 |
|------|------|
| 13:10 | 0.44.11 번들 선택. 저널 `selection` 행 기록(`project_dev=16777232`) |
| 15:49 | 마지막 정상 이벤트(worker) |
| 15:53:12 | 맥 재부팅 |
| 15:54:47 | 첫 거부(codex `update-queued`). 이후 Claude·Codex 전 훅 거부 39건 |

## 원인

- 저널이 프로젝트를 `(경로, st_dev, inode)`로 봉인하고, 훅마다 지금 `stat` 값과 비교한다(`core/laya_install_journal.py` `_verify_selected_route`).
- macOS APFS는 부팅 때 볼륨 장치 번호를 다시 매긴다. 재부팅 뒤 Data 볼륨(`disk3s5`)이 `0x1000010`(16777232) → `0x1000011`(16777233)으로 바뀌었다. 경로와 inode(5874765)는 그대로다.
- route·저널의 pluginRoot·installId·generation·manifest sha는 모두 일치했고, 다른 값은 `project_dev` 하나였다.
- `project_dev`는 저널 최초 생성 때만 기록되고, 업그레이드·재선택 경로도 같은 식별값 일치를 요구한다. 플러그인 업데이트로는 풀리지 않는다.
- 플러그인 미업데이트와는 무관하다. 선택 번들은 이미 0.44.11(origin/main `d29b8ca3`)이었고, 0.44.11을 직접 쓰는 Codex 훅도 같은 시점부터 거부됐다.

## 영향 범위

- 이 맥의 MemVane 설치 9개(service-engineering, qmd-auto-context, axiom, rccar, agentflow, ktlo-check, novel 3편) 저널 전부 `project_dev=16777232`.
- 코드 전수 조사 결과, 장치 번호를 디스크에 저장했다가 다른 프로세스에서 다시 비교하는 지점이 15군데(A1~A15). 그중 훅 hot path는 A1~A8이다.
  - A1 저널 프로젝트 식별, A2 provider 파일 식별, A3 ownership manifest, A4 reader endpoint, A5 fresh-wiki 원장 스토어 식별, A6 reader-windows, A7 소스 카탈로그 root·스캔 sig, A8 schema3 blob/view 바인딩
  - A9~A15는 작업 도중 재부팅이 겹칠 때만 문제 → TODO로 넘김

## 수정 방향

- 저장된 식별값과 지금 `stat` 값을 비교할 때 `st_dev`를 빼고 경로·inode·내용 필드로만 비교한다.
- 저장 포맷·해시·스키마·scopeId 시드는 바꾸지 않는다. reader endpoint 행은 트리거로 변경이 막혀 있고 id가 레코드 해시이며, ownership manifest sha가 route·저널에 고정돼 있어서 저장값을 다시 쓰면 연쇄로 깨진다.
- 같은 프로세스 안의 비교(fd와 경로 TOCTOU 등)는 그대로 둔다.
- `crosses_device` 판정은 저장된 root dev 대신 지금 연 root fd의 dev와 비교한다.
- 회귀 테스트: `st_dev+1`을 돌려주는 `simulated_reboot()`로 recall·쓰기 훅·새 번들 인계를 검증하고, 수정 전 실패를 먼저 확인한다.

## 진행 기록

| 시각 (KST) | 내용 |
|------|------|
| 17:44 | 원인 확정 (저널 `project_dev` 16777232 vs 현재 16777233, 부팅 15:53:12) |
| 18:00 | `core/` 전수 조사: 영속 dev 비교 15군데 분류 |
| 18:07 | 개발 가드 `active.json`에 재부팅으로 죽은 테스트 실행(pid 431, 15:14 시작)이 `settled:false`로 남아 모든 테스트 거절 → 사용자가 settled 처리 |
| 18:20 | Codex 샌드박스는 `ps`·`sysctl` 거부로 가드 관측 불가(`monitor_unavailable`) → 테스트는 호스트에서 Claude가 실행, Codex는 구현만 |
| 18:25 | 회귀 테스트 red 확인 (`inactive_or_mismatched_journal`), `c3c9e466` |
| 19:00~ | Codex `gpt-6.1-sol` 구현 (`10147909` fix, `cf21cd07` docs, `344171cd` 0.44.12). 직전 실행은 stdin 미종료로 50분 대기 후 재실행 |
| 이후 | 회귀 테스트 2건 실패 → 재부팅 흉내 없는 대조 실행에서도 동일 실패 → fixture 결함으로 판정, 단정 정리(`2b25b8e4`) 후 8케이스 통과 |
| 이후 | PR #166 머지, Claude(0.44.10→0.44.12)·Codex(0.44.11→0.44.12) 플러그인 업데이트 |

## 변경 요약

- 저장된 식별값과 지금 `stat` 값을 비교할 때 `st_dev`를 빼고 경로·inode·내용 필드로 비교 (A1~A8 + `context_learning/inference_identity.py`)
- 저장 포맷·해시·스키마·scopeId 시드 유지, 일회성 전체 재스캔 없음
- A9~A15(작업 도중 재부팅)는 `docs/TODO.md`로 이관
- 수정 34파일, +545/−100

## 검증 (사용자 요청으로 최소화)

- `test/laya-reboot-identity.test.mjs` (`st_dev+1` 재부팅 흉내 8케이스): 수정 전 실패, 수정 후 통과. 실제 번들 인계 `active_upgrade_complete` 확인
- `test/plugin-manifest.test.mjs` 통과
- 생략: 전체 `npm test`, 머지 전 astra 리뷰, CI 대기
- 실설치 확인 (service-engineering): 0.44.12 번들의 `update-queued` → `ok / bundle_upgrade_pending`, `recall` → 검증 문서 3건 컨텍스트 반환. 백그라운드 `bundle.upgrade started`

## 남은 일

- 열려 있는 Claude·Codex 세션은 재시작해야 0.44.12 훅을 쓴다 (재시작 전 세션은 0.44.10/0.44.11 훅이라 계속 거부)
- 나머지 8개 설치는 각 프로젝트에서 세션을 처음 열 때 SessionStart 번들 업그레이드로 넘어간다
- 개발 가드는 재부팅으로 죽은 실행을 스스로 정리하지 못한다(`previous_job_unsettled_review_checkpoint`). 재부팅 후 테스트가 exit 75로 막히면 `~/Library/Logs/qmd-development-guard/active.json`의 pid·시작 시각을 부팅 시각과 대조한다

## 참고 자료

- 영속 dev 비교 사이트 번호(A1~A16)는 `docs/TODO.md`의 재부팅 경계 항목과 같은 번호다
- 회귀 테스트: `test/fixtures/laya-reboot-identity-checks.py` (`simulated_reboot()`이 `os.stat`·`os.lstat`·`os.fstat`의 `st_dev`를 +1)

# recall 효용 회복: 열린 항목

이 파일은 recall 효용 회복 작업에서 아직 결정하거나 구현하지 않은 세 항목과
그 근거만 남긴다. 각 항목은 착수 전에 적힌 선행 조건을 먼저 확인한다.

## verify defer 루프의 종점

`wiki_verify_worker`는 검수 직전 카드 원문의 freshness가 `FRESH`가 아니면 잡을
`deferred / source_changed_before_verify`로 기록하고 큐에 그대로 남긴다. 이
분기에는 재시도 카운터도 종점도 없다. `recallVerifiedOnly` 아래에서 이런 카드는
`generated`로 남아 계속 주입되지 않는다.

`freshnessReason`에 따라 처리할 갈래가 다르다.

- `content_hash_mismatch`: 원문이 카드보다 새롭다. 필요한 것은 검수가 아니라
  재컴파일이다. 같은 잡이 N회 defer되면 verify 큐에서 빼고 compile source 큐로
  넘기는 방안이 있다.
- `missing`: 원문이 없어 신선해질 수 없으므로 defer는 영구 대기다. 이 분기가
  `record_source_missing` 경로보다 앞에 있어 source-missing 정책에 도달하지
  못한다. `missing`은 source-missing으로 보내야 한다.

착수 전에 확인할 것:

- **재컴파일 브리지가 루프를 끝내는지.** 같은 원문의 compile이 이미
  `extractor_failed/source_unreadable`이나 `skipped/verify_inconclusive_suppressed`로
  실패·억제되고 있으면 compile 큐로 넘겨도 루프는 끝나지 않는다. 또
  `wiki_compile_worker.recover_pending_refreshes`가 매 run 재큐잉하므로 새 브리지는
  같은 원문의 중복 enqueue, 곧 이중 과금이 될 수 있다. `source_unreadable`의
  원인부터 진단한다.
- **defer 카운터가 없다.** `requeue_lines`는 raw 라인을 그대로 다시 쓰고,
  `_requeue_count`는 compile worker의 orphan claim 경로에만 있다. "N회 defer 후"를
  표현할 상태를 먼저 만들어야 한다.
- **`missing` 재라우팅은 세 갈래여야 한다.** `wiki_freshness.check_card`는 첫
  비-fresh 원문에서 반환하므로 `missing`이 일부 원문만 사라진 경우일 수 있다.
  그때 `record_source_missing`은 `all_sources_missing` 가드 때문에 아무것도 쓰지
  않으므로, 잡을 드롭하면 흔적 없는 영구 `generated`가 된다. 전멸 / 일부 / 그
  외로 나눈다.
- **검증:** 적용 뒤 `verify-queue.jsonl`이 줄어드는지, `verify-log.jsonl`에
  `deferred`가 아닌 결과가 나타나는지 본다.

## PostToolUse recall 경로를 유지할 것인가

PostToolUse 편집 훅(`posttool`)도 recall 문맥을 주입한다. transcript 표본에서
편집 훅 주입은 건당 분량이 UserPromptSubmit 주입보다 컸고, 주입된 카드를 모델이
연 경우가 없었다. 내용이 틀렸다기보다 타이밍이 맞지 않는다. 방금 그 파일을
편집했으니 모델은 이미 문맥을 쥐고 있고, 단일 파일 편집이 연속되면 주입도
그만큼 반복된다.

다만 프로젝트마다 집계 단위(프로젝트 전체와 단일 세션, 측정 기간)가 달라서
"건수와 분량 모두 편집 훅이 크다"는 결론은 일부 프로젝트에서만 성립했다. 판단
전에 단위를 통일해 다시 집계한다.

선택지: (a) 유지, (b) 게이트를 건 형태(링크와 제목)만 남기기, (c) 폐지.
(b)가 비용 대비 합리적으로 보이지만, 편집 훅 주입의 효용 근거가 애초에 없어
판단 근거도 약하다.

## subagent(sidechain)와 하네스 프롬프트 주입

- subagent 세션에는 recall 주입이 구조적으로 닿지 않는다. transcript 표본의
  subagent 기록에는 `hook_additional_context`가 사실상 없었다. 실작업이
  subagent에서 일어나는 워크플로에서는 recall이 무의미하다. 닿게 할 것인가?
- `<task-notification>` 같은 하네스 생성 프롬프트에도 UserPromptSubmit 주입이
  발화한다. 사용자 질의가 아니므로 순손실이다. 현재 recall은 이런 프롬프트를
  거르지 않는다. 거를 기준(태그, 발신 주체)을 정해야 한다.

# Recall

게시된 선택 view를 읽고 검색 결과를 구성한다. 게시 head가 없을 때 다른 view로 대체하지 않는다.

recall은 선택 recall의 판정을 소유하고 평범한 결과 `RecallAnswer`(`types.py`)를 돌려준다. host는 그 결과를 host별 hook 출력으로 렌더링하고 learning capture를 기록한다. trace 필드·진단 note·최종 사유는 host가 넘긴 `RecallTrace` 포트로 보고하며, 이 보고는 답을 바꾸지 않는다.

- `selected_recall.py` — journal이 고른 reader endpoint와 schema3 view의 recall. 설정·provider·vector fallback 판정.
- `verified_rows.py` — 색인이 검증한 행의 카드 선택(`_select_verified_cards`)과 선택 후 재확인·답 구성(`_serve_verified_rows`). selector에는 검증 pool의 상위 10행을 넘기고 capture는 pool 전체를 받는다. lane별 예산: 프롬프트 recall은 `min(queryTimeout, 3초)`(hook deadline − 1초 안)와 쌍 12개, 편집 recall은 꼬리 deadline과 쌍 91개다. selector가 답한 recall은 주입 카드 가운데 hybrid 상위 3행과 겹치는 수를 `rankedOverlap`으로 남긴다. 근거와 측정 필드는 `docs/continual-learning.md`의 "Selector budget".
- `recall_scope.py` — 한 recall과 그 재시도가 공유하는 상태(journal epoch 하한, project root별 sync wake 1회, writer floor)와 provider owner wake.
  프롬프트 recall에서 provider 기동이 terminal이면 게시된 lexical 목록으로 답하고 `laya_vector_selected_provider_start_failed`를 기록한다. 선택 proof 불일치, owner state 손상, socket identity 전환은 recall을 거절한다.
- `domain/rules.py` — 순수 규칙: lexical 질의·RRF 결합, 검증 pool(상위 15행), 답 상한(`topN`과 3 중 작은 값), selector 사유 판정. selector 사유 코드는 `learning.types`의 `SELECTOR_ANSWERS`·`SELECTOR_CANDIDATE_INVALID`를 쓴다.
- `keywords.py`, `reader_dispatch.py` — 질의어 구성과 reader 질의. 프롬프트 recall(`edit_recall() is None`, `MEMVANE_NEED_SIGNALS=off`가 아닐 때)에서는 lexical 단계 직후 AND 질의의 고유 원자 용어를 최대 8개까지 같은 pin에서 개수 전용 읽기 한 번(`_need_signals` → `store.probe_terms`: 본문과 checkout 관측을 읽지 않고, lexical lane이 빼는 wiki 루트 meta 페이지를 세지 않는다. 전체 150ms 가운데 reader 잔여가 2초를 넘는 만큼만 쓴다)으로 세어 `needAtoms`·`needAtomsHit`·`needAtomsRare`·`needRareTopScore`·`needProbeMs`·`needProbeTruncated`를 `recall` 이벤트에 기록한다. 판정이나 recall 결과·순서·주입 변경은 하지 않으며 용어 문자열도 남기지 않는다.
- 질의 벡터(`reader_dispatch.approved_query_vector`) — provider에 hook 프로세스 안에서 recall당 한 번, 2초 안에서 묻는다(자식 프로세스 없음). 긴 프롬프트는 창 최대 4개로 물어 벡터 여러 개가 돌아오고(`memvane/provider/README.md`), exact 검색은 카드별 최고 점수를 쓴다. `recall` 이벤트의 `queryChunks`는 받은 벡터 수, `queryChunksRequested`는 물은 텍스트 수, `semanticOnlyInjected`는 주입한 카드 가운데 lexical 목록에 없던 수다(semantic이 `complete`일 때).

recall은 융합 후보별 `store`(Accelerate)·`python`·`lexical` 출처를 보관한다.
host는 이 정보를 capture 입력과 함께 전달하고 learning은
표본을 저장한 뒤 같은 `request_id`의 `learning.capture.sources` 진단 이벤트에
후보 id의 SHA-256과 출처를 기록한다. 학습 표본과 compile 행의 모양은 유지한다.

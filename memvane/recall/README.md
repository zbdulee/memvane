# Recall

게시된 선택 view를 읽고 검색 결과를 구성한다. 게시 head가 없을 때 다른 view로 대체하지 않는다.

recall은 선택 recall의 판정을 소유하고 평범한 결과 `RecallAnswer`(`types.py`)를 돌려준다. host는 그 결과를 host별 hook 출력으로 렌더링하고 learning capture를 기록한다. trace 필드·진단 note·최종 사유는 host가 넘긴 `RecallTrace` 포트로 보고하며, 이 보고는 답을 바꾸지 않는다.

- `selected_recall.py` — journal이 고른 reader endpoint와 schema3 view의 recall. 설정·provider·vector fallback 판정.
- `verified_rows.py` — 색인이 검증한 행의 카드 선택(`_select_verified_cards`)과 선택 후 재확인·답 구성(`_serve_verified_rows`).
- `recall_scope.py` — 한 recall과 그 재시도가 공유하는 상태(journal epoch 하한, project root별 sync wake 1회, writer floor)와 provider owner wake.
- `domain/rules.py` — 순수 규칙: lexical 질의·RRF 결합, 검증 pool(상위 15행), 답 상한(`topN`과 3 중 작은 값), selector 사유 판정. selector 사유 코드는 `learning.types`의 `SELECTOR_ANSWERS`·`SELECTOR_CANDIDATE_INVALID`를 쓴다.
- `keywords.py`, `reader_dispatch.py` — 질의어 구성과 reader 질의.

# View 행 세대 저장 설계

View store는 형식 4의 행 단위 세대를 사용한다. 카드와 원문 관측은 유효 구간으로 보관하고, 게시할 때 바뀐 행과 delta만 기록한다. 한 transaction에서 head FTS를 갱신하고 봉인을 만든 뒤 head를 CAS로 옮긴다.

- view마다 현재 head FTS 하나를 유지한다. 검색은 pin한 head의 봉인·행·FTS를 검증한다.
- 첫 게시 이후 rollback은 직전 게시의 delta를 역적용한 새 세대다. head와 직전 세대는 view 행 GC가 보존한다.
- content와 완전한 chunk/vector set은 불변이며 view 간 공유할 수 있다. 신뢰와 검색 자격은 해당 view에서 판정한다.
- 큰 변경은 유계 stage에 저장하고 capture·head·설정 정체성을 재확인한 뒤 게시한다. cold view는 첫 게시 전까지 검색 결과를 제공하지 않는다.
- 공유 content GC는 view 행 GC와 별도 과제다. 참조 관계와 동시 writer·reader 증명은 [열린 항목](../TODO.md)을 따른다.

API와 실패 의미는 [view store 계약](../development-view-candidate.md), 내용과 provenance는 [blob 계약](../development-blob-candidate.md)을 따른다.

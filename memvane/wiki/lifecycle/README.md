# Wiki 수명 주기

삭제된 source의 증명과 카드 퇴역을 다룬다. 관련 상세는 [검수 문서](../verify/README.md)에 있다.

구현: `git_branch_safety.py`, `wiki_lifecycle.py`, `wiki_lifecycle_request.py`, `wiki_source_retire.py`.

## 삭제 판정과 브랜치

원문이 사라진 카드는 git이 HEAD 계보의 삭제를 증명할 때만 퇴역한다(`git_branch_safety.LineageJudge`). HEAD에 병합되지 않은 로컬 브랜치 tip이 원문을 가지면 보류한다(`source_on_other_branch`). 단, HEAD 계보에 그 경로의 삭제 커밋이 있고 원문을 가진 모든 tip이 그 경로를 HEAD와의 모든 merge-base와 같은 blob으로 가지면 그 tip들은 근거가 아니다. 삭제 전에 갈라진 스냅샷이고, 병합해도 삭제가 남는다. tip이 원문을 새로 추가했거나 고쳤으면 계속 보류한다. 퇴역은 이 checkout의 카드 파일만 옮긴다. 옛 브랜치는 자기 커밋의 카드 사본을 유지한다.

같은 원문을 인용한 카드는 묶어서 판정한다(`wiki_source_retire.retire_deleted_source`). 한 장이라도 보류면 전부 보류하고, 막은 카드 경로와 사유를 `wiki` 활동 이벤트(`outcome: held`)로 남긴다. 이 이벤트는 추출기 `logTail`에 들어가지 않는다.

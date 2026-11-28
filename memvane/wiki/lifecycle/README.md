# Wiki 수명 주기

삭제된 source의 증명과 카드 퇴역을 다룬다. 관련 상세는 [검수 문서](../verify/README.md)에 있다.

구현: `git_branch_safety.py`, `branch_tips.py`(브랜치 tip의 원문 보유 판정), `wiki_lifecycle.py`, `wiki_lifecycle_request.py`, `wiki_source_retire.py`.

## 삭제 판정과 브랜치

원문이 사라진 카드는 git이 HEAD 계보의 삭제를 증명할 때만 퇴역한다(`git_branch_safety.LineageJudge`). HEAD에 병합되지 않은 로컬 브랜치 tip이 원문을 가지면 보류한다(`source_on_other_branch`). 단, HEAD 계보에 그 경로의 삭제 커밋이 있고 원문을 가진 모든 tip이 그 경로를 HEAD와의 모든 merge-base와 같은 entry(mode·type·blob)로 가지면 그 tip들은 근거가 아니다. 삭제 전에 갈라진 스냅샷이고, 병합해도 삭제가 남는다. tip이 원문을 새로 추가했거나 고쳤으면 계속 보류한다. 다른 worktree에 checkout된 tip도 보류한다. 그 checkout에 git이 보지 못하는 미커밋 변경이 있을 수 있다. 퇴역은 이 checkout의 카드 파일만 옮긴다. 옛 브랜치는 자기 커밋의 카드 사본을 유지한다.

흔한 경우는 git 호출 한 번으로 증명한다. 모든 tip에서 `^HEAD`로 거른 `git log --full-history -m`이 그 경로를 바꾼 커밋을 하나도 내지 않으면 모든 tip이 merge-base와 같은 entry를 가진다. 바꾼 커밋이 있으면 그 커밋을 가진 tip만 merge-base를 직접 대조한다. 판정 git 호출은 run deadline(20초) 안에서 돌고, 시간이 바닥나면 보류한다.

같은 원문을 인용한 카드는 묶어서 판정한다(`wiki_source_retire.retire_deleted_source`). 한 장이라도 보류면 전부 보류한다. 인용 카드가 둘 이상이면 막은 카드마다 `wiki` 활동 이벤트(`outcome: held`)를 남긴다. 같은 원문의 막은 카드 집합이 바뀔 때만 남기고, 마지막으로 남긴 집합은 scan-state 디렉터리의 `<project key>-wiki-held.json`에 둔다. 이 파일은 지워도 다시 만들어지는 파생물이다. 이벤트는 추출기 `logTail`에 들어가지 않는다.

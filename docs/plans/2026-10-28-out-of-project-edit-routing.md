# 프로젝트 밖 편집 라우팅 — laya-db 향후 항목

상태: 미구현. 이 문서는 설계 메모이며 현재 동작을 바꾸지 않는다.

## 현재 동작

- 선택된 프로젝트에서 편집 도구(`index` hook)가 프로젝트 밖 경로를 고치면, hook은 그 경로를 어디에도 보내지 않는다(`core/laya_host_response.py`의 `index` 분기). 프로젝트 안 경로가 함께 있으면 현재 프로젝트의 `laya-sync` wake만 남긴다.
- 밖 경로가 다른 선택된 프로젝트의 원문이면, 그 프로젝트의 다음 full-scan sync(SessionStart·자기 편집·agent Bash)가 변경을 반영한다. 그 사이 그 프로젝트의 검색은 마지막 게시 generation을 그대로 쓴다.
- 경로에서 소유 프로젝트를 찾는 공용 registry는 없다. QMD 공용 owner registry를 읽는 코드도 없다.

## 목표

다른 선택된 프로젝트의 원문 편집을 그 프로젝트의 owner에게 즉시 알린다. hook은 다른 프로젝트의 laya-db를 열지 않고, 변경 사실 하나만 남긴다.

## 설계 (git blob 기반)

1. **소유 프로젝트 찾기.** 같은 사용자의 설치 journal이 승인한 선택 route 목록에서, 편집 경로를 포함하는 원문 collection root를 longest-prefix로 고른다. route 증명(`verify_selected_route`)이 통과한 프로젝트만 후보다. 후보가 없거나 둘 이상이 같은 길이로 겹치면 아무것도 보내지 않는다.
2. **변경 식별자.** 편집 후 파일 바이트의 git blob id(`sha1("blob <len>\0" + bytes)`, `git hash-object`와 같은 값)를 계산한다. 삭제는 blob id 없이 `deleted`로 남긴다. 같은 `(project, path, blob id)`는 한 번만 기록해 연속 편집이 묶인다.
3. **기록과 wake.** 소유 프로젝트의 sync state 디렉터리에 `(path, blob id 또는 deleted)` 요청을 쓰고 그 프로젝트의 `laya-sync` wake를 offer한다. 기존 wake kind만 쓴다(새 kind는 구버전 worker의 drain을 멈춘다).
4. **owner 쪽 검증.** owner의 full-scan sync는 요청을 힌트로만 쓴다. 현재 파일의 blob id가 요청과 같을 때만 그 경로를 우선 처리하고, 다르면 일반 full scan 결과를 따른다. git 저장소에서는 blob id로 HEAD tree와의 일치를 파일을 다시 읽지 않고 판단할 수 있어 `git_branch_safety`의 lineage 판정과 같은 식별자를 쓴다.

## 제약

- hook 시간 예산 안에서 파일 하나의 해시만 계산한다. 다른 프로젝트의 laya-db·모델·provider는 열지 않는다.
- 선택되지 않은 프로젝트, linked worktree, bundle drift 상태의 프로젝트로는 보내지 않는다.
- 요청 파일은 owner 전용(0600)이고 경로 외 원문 내용을 담지 않는다.

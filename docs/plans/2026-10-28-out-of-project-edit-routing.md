# 프로젝트 밖 원문 편집 라우팅

선택된 프로젝트 A의 편집 훅은 다른 선택된 프로젝트 B의 원문 변경을 B의
백그라운드 워커에게 알린다. A의 검색 컨텍스트와 로컬 compile 범위는 A를 따른다.
공통 코어가 Claude Code·Codex·Hermes Agent의 편집 이벤트를 처리한다.

## 증명과 실행 경계

- A 훅의 기존 전체 증명은 A의 편집 이벤트를 검증한다. 외부 경로도 같은 이벤트
  형식 검사를 받는다. 실패한 편집은 알림을 만들지 않는다.
- 설치 journal의 선택 프로젝트에서 가장 긴 원문 root 접두사를 고른다. 동률이나
  탐색 시간·개수 상한에 걸린 불완전한 후보 목록은 사용하지 않는다.
- `verify_notification_target`은 B의 journal·route·번들·봉인된 attestation
  메타데이터를 검증한다. B의 색인·claim DB를 열거나 provider·모델을 실행하지
  않는다. 반환값의 `notificationOnly`는 작업 권한을 부여하지 않는다.
- 성공한 sync 뒤 B 워커가 `laya_external_edits.drain`을 호출한다. discovery grant가
  없어도 알림을 읽으며 실제 enqueue 전에 기존 전체 write 증명, 현재 설정과 파일
  버전을 다시 확인한다. 증명이 바뀌면 알림을 남겨 재시도한다.

## 알림과 큐

알림은 B 설치의 scope·DB incarnation에 묶인 0600 JSON과 전용 잠금을 쓴다.
각 행에는 프로젝트 상대 경로·git blob id 또는 `deleted`·편집 호스트만 담는다.
원문 본문은 저장하지 않는다. 경로별 대기는 최신 변경 하나로 합치고, 같은 현재
버전의 연속 알림을 억제한다. 내용이 바뀌었다가 돌아오면 새 변경으로 알린다.
처리하지 못한 오래된 버전이나 동의가 꺼진 버전은 처리 완료로 기억하지 않는다.
알림 뒤 B의 기존 `laya-sync` wake를 offer한다.

owner 직렬화 잠금과 짧은 inbox 잠금은 별개다. 파일 읽기와 enqueue 동안 새 알림을
받을 수 있고, 처리한 알림의 토큰이 그대로일 때만 제거한다. 처리 중 도착한 새
변경은 다음 pass에 남는다.

owner는 현재 blob이 같은 원문만 B의 편집 이벤트 compile 정책으로 처리한다.
알림 뒤 크기 제한을 넘거나 읽을 수 없어진 원문의 알림은 폐기하며, 다른 알림과
기존 discovery는 계속 처리한다. route·write 증명 실패는 재시도 대상으로 남긴다.
기존 revision-key 중복 억제·enqueue 상한·`laya-compile` wake를 재사용하며,
편집 호스트를 compile 행에 보존한다. 삭제는 기존 위키 수명 주기에 요청한다.
원문 소실 판정과 카드 퇴역에는 기존 Git 계보·대량 삭제 가드가 적용된다.

미온보딩·preview·연결 worktree·blocked·bundle drift 대상은 알림을 받지 않는다.
숨김 경로·도구 메타데이터·wiki·`skipPaths` 경로·symlink 탈출도 제외한다.
onboarding never/skip은 안내만 숨기므로 이미 선택된 프로젝트를 비활성화하지 않는다.

## 실행 예산과 범위

- 훅 탐색: 설치 128개, 상태 디렉터리 항목 512개, 2초.
- 파일 읽기: 파일당 4 MiB, 파일·fd 정체성과 수정 시각을 읽기 전후에 대조한다.
- 알림: 대기 64경로, 최근 처리 256경로. owner drain은 3초와 기존 enqueue 상한을 따른다.
  혼합 편집의 비원문 경로는 대기 상한을 차지하지 않는다.
- 대기 큐가 가득 차면 새 개별 알림을 추가하지 않고 B의 full-scan wake는 offer한다.
  초과한 개별 compile 알림의 durable spill은 후속 범위다. 전체 원문 compile 권한으로
  확대하지 않는다.
- 미온보딩 A 세션, Bash의 임의 외부 편집 경로 추론, 비원문·wiki 편집 라우팅은
  이 경로에 포함하지 않는다. B의 일반 동기화·온보딩 정책을 따른다.

알림 저장이나 대상 검증 실패는 A의 완료된 편집과 A 자체의 sync를 막지 않는다.
내부 hook 이벤트의 `external_edits`는 owner 수·실패 수·overflow 수를 기록한다.
새 wake kind나 사용자 설정 키는 없다.

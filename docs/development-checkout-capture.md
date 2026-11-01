# Checkout 등록과 view capture

`core/laya_checkout_view.py`는 Git checkout의 정체성과 상태를 읽는 내부 API다.
호출자가 보관하는 등록 JSON으로 저장소·워크트리를 식별하고, 관측한 branch에서
view identity를 계산한다. 현재 hook·route·index·journal은 이 API를 호출하지 않는다.

## 호출 계약

| API | 반환값과 용도 |
| --- | --- |
| `register_repository(project_root, deadline=None)` | 새 `repoId`, common Git dir의 경로·dev/ino, 프로젝트 상대 위치·object format을 담은 등록 JSON |
| `register_worktree(project_root, repository, deadline=None)` | repository 등록을 검증하고 checkout·project·Git admin dir identity와 새 `worktreeId`를 담은 등록 JSON |
| `capture_view(project_root, repository, worktree, deadline=None)` | 두 등록을 검증하고 branch·HEAD·index·operation evidence, `viewId`, `checkoutDigest`를 반환 |
| `capture_matches(capture, repository, worktree, deadline=None)` | 현재 상태를 다시 관측해 기존 capture와 같으면 `True`, 변경·불명·잘못된 입력이면 `False` |

`deadline`은 keyword-only 인자이며 `time.monotonic()` 기준 절대 시각이다.
기본 capture 예산은 3초, 명시 deadline의 최대 예산은 10초, 개별 Git 호출은 1초다.
stdout·stderr 합계는 32 KiB, index는 16 MiB, operation 파일은 64 KiB로 제한한다.
실패는 `CheckoutUnavailable`의 `reason`으로 구분한다. 등록 API를 다시 호출하면
새 UUID를 만든다. 같은 등록을 재사용하려면 반환 JSON을 호출자가 보관해야 한다.
모듈은 등록 JSON을 디스크나 journal에 저장하지 않는다.

등록 JSON은 canonical 경로·dev/ino와 repository/worktree의 상호 귀속을 묶는다.
같은 경로에 디렉터리를 재생성하거나 등록된 디렉터리를 옮기면 기존 등록으로
자동 인계하지 않는다. 같은 common Git dir라도 프로젝트 상대 위치가 다르면 별도
등록이다. 별도 clone과 submodule에 부모 저장소의 등록을 적용할 수 없다.
UUID와 digest는 정체성 값이며 설치·검색·쓰기 권한을 부여하지 않는다.

## Capture의 범위

- branch key는 `refs/heads/<name>`, detached HEAD는 `detached:<oid>`다.
- unborn branch는 HEAD가 `None`이다. non-Git은 branch key가 `non-git`이고
  HEAD·object format이 `None`이다. Git 손상·권한·명령 실패는 이 상태로 숨기지 않는다.
- 같은 등록과 branch는 같은 view identity를 사용한다. 같은 branch의 HEAD가 다른
  계보로 움직이면 view identity는 유지되고 checkout digest가 달라진다.
- linked worktree는 같은 repository 등록을 공유할 수 있고 worktree/view identity는
  각각 갖는다. Git index와 진행 중 operation은 해당 worktree의 evidence다.
- 전체 Git 관측 전후를 대조한다. ref·HEAD·index·operation·경로 identity가 도중에
  바뀌면 유한 예산 안에서 재시도하거나 거절한다.
- Git 환경변수 오염을 제거하고 global/system config·선택적 index 잠금·prompt를
  비활성화한다. 명령·출력·index 읽기에는 유한 시간·바이트 상한이 있다.

capture는 원문 본문을 읽지 않는다. HEAD와 index가 같아도 unstaged 파일 bytes는
다를 수 있으므로, 실제 데이터 게시에는 별도 source scan manifest가 필요하다.
이 API만으로 현재 원문·카드 내용이나 삭제 조건을 증명할 수 없다.
split index의 shared 내용까지 증명할 수 없으면 `split_index_unsupported`로 거절한다.
새 저장소 schema·reader endpoint·view 검색 선택은 별도 계약이다.

검증은 `test/laya-checkout-view.test.mjs`와 임시 Git fixture를 사용한다.
기존 호스트 route 회귀는 `test/laya-index-host-route.test.mjs`로 확인한다.

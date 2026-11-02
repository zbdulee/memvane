# MemVane 0.43.37 검증 상태

기준 main은 `3f304338d5a63f7637b83c0b8251063f69820868`, 브랜치는 `codex/review-priority3`다. 범위는 중간 리뷰 3순위다. git-blob 운영 bridge·migration·writer 전환은 포함하지 않는다.

Hermes recall은 공통 hook 분류로 preview를 제공하고 upgrade fence에서는 컨텍스트를 주입하지 않는다. onboarding 선택 기록은 canonical 프로젝트별 private flock 안에서 갱신하며 최대512개 session과128KiB 한계를 함께 지킨다. W4의 동의 off/stale hint는 seen에 넣지 않으므로 현재 동의 아래 새 이벤트로 다시 처리할 수 있다. 해당 결함 주장은 반박하며 동작을 바꾸지 않는다.

legacy cleanup은 전송한 SIGTERM과 확인된 정지를 구분한다. 관측·unload·unlink 실패 시 이미 수행한 변경을 partial 결과에 보존하고 안전한 삭제 범위를 넘지 않는다. launchctl 비영 종료는 정확한 UID/label의 부재가 확인될 때만 멱등 성공이다. R3의 자동 unpinned runtime 거절은 ensure 전에 발생하며, 명시적으로 pinned된 외부 runtime 복사 경로는 유효하다. 이 부분은 설명과 ensure0회 가드만 정렬한다.

프로젝트 폴더 이동은 양쪽 operation/run/settings/card 잠금과 폴더 dev/ino를 확인한다. compile 실행 중이거나 잠근 폴더 범위가 바뀌면 fence 전에 보류한다. compile 워커는 run lock 뒤 route·설정·큐를 재확인한다. hosted learning의 준비된 signature 불일치는 기존 version_mismatch로 빠르게 fallback하며, resource-deferred 재관측은5초다. startup backoff·terminal·자원 admission·7초 관측 gap·5/60초 안정 창은 유지한다.

릴리스 조율은 status/kind와 후속 state/project 이동 결과를 함께 확인한다. 최종 journal/build/classify 관측이 일치하고 선호 owner가 맞아야 완료다. active pending의 previous owner는 완료 권한이 아니며, bound/inactive는 candidate-ready pending과 build identity로 구분한다. 이 metadata 판정은 릴리스별 full read/write와 실제 runtime 경로 검증을 대체하지 않는다.

| 범위 | 현재 검증 근거 |
| --- | --- |
| W3·W5 | BASE RED 후 focused21/21pass,18.212초 |
| R3·R4·R5 | BASE 부분 수행/bootout 오류 RED 후 관련3파일pass,34.674초 |
| U4·compile | 실제 flock·큐 경합 BASE5assert RED 후 기존 회귀 포함59/59pass,47.759초 |
| U4 추가·RT-F4/F5 | 폴더 snapshot·실제 ensure/Coordinator RED 후 새2파일pass,51.928초; 기존 runtime3파일pass,106.435초 |
| U5 | 정상 완료 거절·미완료/previous 수락·실제 반환 누락9assert RED 후 관련5파일pass,169.770초 |

위 GREEN은 모두 전역 guard의 exit0/passed/completed:true/settled:true이며 realpath 임시 HOME·파일 병렬1을 사용한다. 세부 로그는 `/Users/dulee/work/memvane-handoff/context/priority3-*`다. W3/W5 첫 GREEN의 canonical alias 기대값 오류와 U4 followup의 기존 waiter5초 동기화 실패는 별도 로그에 보존한다. alias는 실제 canonical status로 대조하고, waiter는 실제 flock/refusal assertion을 유지한45/60초 유한 handshake를 사용한다. 제품 시간 창은 완화하지 않는다.

합성 owner·OS·provider 경계와 실제 product 제어 흐름을 구분한다. 운영 cleanup·실제 모델·운영 DB는 fixture 입력이 아니다. 보호3은 무변경이며7개 manifest는0.43.37이다. 개발용 Claude 모델 호출은 없다. CI는 개발과 별도로 관찰한다.

guard 합성 단위21개와 manifest 검사4개도 통과한다. clean code HEAD `8406ff90cabb047ef2271b34affad551a5c91c6d`의 전체 suite는690개 중687pass·0fail·3skip,1259.952초이며 guard는 exit0/passed/completed:true/settled:true다. 전체 실행 중 소스·문서·index는 고정한다. 종료 후 이 REPORT에 결과만 반영하며 최종 exact-head 독립 리뷰·새 릴리스 설치/운영 감사는 아직 미완료다. 마지막 완료 릴리스0.43.36의 운영 근거는 외부 `priority2-release-completion-audit.md`다. 현재 진행 감사표는 외부 `priority3-completion-audit.json`이며 complete:false다.

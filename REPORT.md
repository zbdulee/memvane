# MemVane 0.43.35 검증 상태

기준 main은 `bfca7bf`, 브랜치는 `codex/recovery-pending-fence`다. 구현 범위는 중간 리뷰 U1의 journal pending 이후 복구와 종료 프로세스의 자원 진단이다. PR5 구현은 포함하지 않는다.

pending CAS는 실제 route, 정확한 predecessor·epoch, selected/previous·provider 증거, 쓰기 이력과 project/scope/store/profile을 확인한다. v2 intent는 원래 backup·record·counts를 보존하고 route-only 정규화와 새 대상 교체를 하나의 유계 descriptor로 재개한다. 사라진 bundle의 FID는 현재 증명이 아니다. 새 대상은 정상 provider review·동등성·adoption과 proof/FID 확인 뒤 활성화하며 fence는 마지막에 해제한다. 매 쓰기 전에 project dev/ino와 journal scope를 확인한다. v2 복구는 forward-only다.

LocalTrainer의 0-RSS 행은 유효한 메모리 관측으로 집계하지 않는다. 살아 있는 프로세스의 required monitor는 이를 거절하고, 종료한 프로세스는 원래 종료 오류를 유지한다. 상태 이동 fixture는 테스트별 hook deadline을 격리하고 실제 old lock FD를 rename 전후로 유지하는 이벤트 동기화를 사용한다. 제품의 hook budget과 lock timeout은 유지하며 실제 즉시 lock 거절도 검증한다.

검증 근거는 U1 focused 7개 검사, 실제 SQLite journal fixture12, 상위 crash fixture8·durable-step9, 기존 fresh-bundle23과 추가 scope·inode·CAS 경계다. sampler·상태 이동·host response 등 관련 focused는 7개 파일/133.295초, 최종 scope focused 5개는 18.464초로 통과한다. guard-unit21과 manifest 검사도 통과한다. 실제 모델과 운영 DB는 fixture로 사용하지 않는다. bundle/provider artifact 증명만 합성하며 실제 journal 통합은 원래 v1 backup, 양쪽 cache 소실, scope·inode 교체와 CAS 직후 교체를 검증한다.

첫 전체 suite는 변경되지 않은 teacher transport의 cleanup 검사 1건으로 중단한다(stopped/completed:false/settled:true). 격리 실행과 두 번째 전체 실행에서 해당 검사는 통과하지만 원인은 미확정이며 transport 코드는 변경하지 않는다. 두 번째 전체 결과는 680개 중 675 pass/2 fail/3 skip, 1182.559초, guard failed/settled:true다. 0-RSS 집계와 fixture deadline 누수는 별도 RED로 재현한다. 종료 후 RSS·early scope·inode 경계도 RED→GREEN을 확인한다. 최종 전체 검사는 681개 중 678 pass/0 fail/3 skip, 1175.985초로 통과하며 guard는 passed/completed:true/settled:true다.

상태 이동 단일 case 프로파일은 1.579초 중 rename에 1.425초를 사용한다. 파일시스템 지연의 기저 원인은 미확정이며 시스템 설정·다른 앱·guard를 변경하지 않는다. 직접 Python 진단은 guard의 Node-test 규칙으로 거절되며 동일 검사는 지원되는 Node wrapper로 실행한다.

7개 manifest는 0.43.35이며 보호 파일 3개는 무변경이다. 독립 exact-head Astra/Agy 리뷰, PR·merge·추가 설치는 미실시다. CI는 개발과 별도로 관찰한다. Claude 호출은 없다.

현재 설치된 0.43.34의 쓰기 증명18은 통과하지만 SE/rccar hosted selector gate는 미통과다. 정상 heal 뒤 hosted warm은 worker_error를 반환하며, 같은 설치·pinned interpreter의 fresh 합성 MPS 추론 성공은 운영 복구 증거가 아니다. 서비스 강제 drain·동일 버전 kill·설치 파일 수정은 없다. 추가 배포 전 이 gate를 확인한다.

상세 근거는 `/Users/dulee/work/memvane-handoff/context/review-remediation-status.md`와 `u1-*.log`에 있다. 현재 복구 계약은 `docs/development-active-upgrade-recovery.md`다.

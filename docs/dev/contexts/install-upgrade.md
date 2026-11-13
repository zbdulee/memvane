# 설치 증명과 번들

- `laya_ledger_common.py`는 선택 ledger의 비공개 JSON 읽기·쓰기, ledger lock, 프로젝트 단위 operation lock과 settings preflight를 제공한다.
- 설치 준비는 review digest와 provider attestation을 확인한다. 내용이 동등한 기존 provider profile이 있으면 그 profile을 donor로 사용할 수 있으며, 동등한 profile이 둘 이상이면 `fresh_profile_donor_ambiguous`로 거절한다.

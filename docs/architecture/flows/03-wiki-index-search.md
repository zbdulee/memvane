# 03 위키 색인과 검색 주입

binding store가 있는 선택된 Laya v2 route의 recall은 hook 안에서 모델 작업 없이 완료된 index generation 하나를 pin합니다. wiki role만 검색하고 `index.md`·`log.md` 같은 관리 파일을 제외합니다. 검색은 색인 DB에 게시된 위키 본문과 해시·세대·범위·명시 제외를 확인하며, 현재 원문 파일이나 pending refresh 원장을 다시 읽지 않습니다. 원문 변경 뒤 위키 생성·색인 게시가 끝나기 전에는 마지막 완료본이 검색됩니다. 색인 무결성 또는 선택 증명이 실패하면 결과를 줄여서 내보내지 않고 실패합니다.

Laya의 membership·본문·FTS·vector·검색은 **위키 카드만** 대상으로 합니다. raw source에 대해서는 path·hash cursor만 남고, 이는 freshness 판단과 위키 생성에만 쓰입니다. raw 본문은 색인되지 않습니다. 일반 위키 카드 안의 인용과 source locator 링크는 유효한 출력입니다.

큐 등록·원문 해시 비교·위키 갱신과 개별 문서의 원자적 색인 게시는 인덱스 owner가 맡습니다. 일반 검색은 그 owner가 이미 게시한 위키만 조회합니다. 기존 호스트 어댑터에는 provider 재기동·오래된 seal 예외 시 index owner에게 wake를 요청하는 경로가 별도로 남아 있으며, 정상 검색의 변경 감지로 사용하지 않습니다. binding store가 없는 구형 선택 경로는 아직 live scan을 사용하므로 이 문서의 게시본 전용 보장은 적용되지 않습니다.

색인 갱신은 runtime wake queue를 통해 비동기 sync worker가 맡습니다. offer 결과가 `queued`·`retained`이면 job이 queue에 남지만, `dropped`이면 queue에 남지 않습니다. 따라서 `dropped`를 성공한 job으로 보지 않습니다.

```mermaid
flowchart TD
  subgraph HOST["호스트 에이전트 경계"]
    UP["recall hook payload 프롬프트"]
    CTX["주입 컨텍스트 verified wiki cards"]
    ERR["hook 오류 fail closed"]
  end
  subgraph Q["동기 query_selected_v2 hook 내 모델 작업 없음"]
    PV["provider 준비 확인"]
    WR{"wiki role 존재 그리고 hierarchical"}
    PN["index generation pin"]
    CB["binding store 후보 질의 binding 없으면 실패"]
    PH["wiki 대상 lexical 및 vector 검색"]
    PD{"게시된 세대와 위키 본문 검증"}
    FT["index.md log.md 와 명시 제외 카드 필터"]
    DV["완료본 반환 finish 후 pin release"]
  end
  subgraph ASY["비동기 sync owner"]
    WK["laya-sync wake offer"]
    WQ[("runtime-wakes.sqlite3 queued 또는 retained job")]
    DR["dropped queue 에 남지 않음 성공 job 아님"]
    SW["selected sync worker"]
    RS["raw path hash cursor freshness 및 위키 생성 전용 본문 색인 안 함"]
  end
  IX[("Laya index generation wiki 전용 membership 본문 FTS vector")]
  WC[("위키 카드 .auto-context/wiki")]
  PL[("wiki freshness pending ledger")]

  UP --> PV
  PV --> WR
  WR -->|"아니오"| ERR
  WR -->|"예"| PN
  PN --> CB
  CB --> PH
  PH --> PD
  IX -.->|"pin 된 세대만 읽기"| PN
  PL -.->|"갱신 worker만 소비"| SW
  PD -->|"무결성 실패"| ERR
  PD -->|"완료본"| FT
  FT --> DV
  DV -->|"성공 빈 결과 허용"| CTX
  DV -->|"반환 행 검증 실패"| ERR
  WK -->|"queued 또는 retained"| WQ
  WK -->|"dropped"| DR
  WQ -.->|"wake 소비"| SW
  WC -->|"카드 본문 색인 입력"| SW
  SW -->|"raw 는 path hash 만"| RS
  SW -->|"wiki generation 게시"| IX
```

## Implementation and evidence

### 현재 구현 (개발 브랜치, 미게시)

- [`core/laya_host_response.py:735-743`](../../../core/laya_host_response.py#L735-L743): 한 pin에서 검증된 위키 카드만 반환합니다. hook 안에서는 모델 작업을 하지 않습니다. `source_locations`에는 반환된 wiki 행의 검증된 span locator만 담깁니다.
- [`core/laya_host_response.py:744-751`](../../../core/laya_host_response.py#L744-L751): provider 준비를 확인한 뒤 `wiki_index_roots`로 루트를 정합니다. wiki role이 없거나 `hierarchical`이 아니면 실패합니다.
- [`core/laya_host_response.py`](../../../core/laya_host_response.py): generation을 pin합니다. binding store가 있으면 pin된 generation에 binding이 없을 때 fail closed로 처리합니다.
- [`core/laya_host_response.py`](../../../core/laya_host_response.py): `index.md`·`log.md`를 뺀 뒤 게시된 카드의 생성 표식·본문 해시·출처 구조를 확인합니다. 원문 현재성은 검색 시점에 재검사하지 않습니다. `require_returned_verified`와 `finish`가 세대·범위·제외·루트 신원을 확인하고 마지막에는 pin을 release합니다.
- [`core/laya_index_host_route.py:163-174`](../../../core/laya_index_host_route.py#L163-L174): 현재 다중 source 정책은 raw 컬렉션 1개와 wiki 컬렉션 1개, `hierarchical`, score cutoff 0입니다. raw 컬렉션은 freshness와 위키 생성용이며, 검색 대상이 아닙니다.
- [`core/laya_wiki_discovery.py:519-539`](../../../core/laya_wiki_discovery.py#L519-L539): raw 루트 스캔은 이름과 hash만 반환합니다. raw 본문을 Laya membership에 추가하지 않습니다.
- [`core/laya_index_source_port.py:301-351`](../../../core/laya_index_source_port.py#L301-L351): 명시된 루트만 symlink를 따라가지 않고 스캔해 path·sha256 manifest를 만듭니다.
- [`core/runtime_work.py:1-5`](../../../core/runtime_work.py#L1-L5), [`core/runtime_work.py:84-122`](../../../core/runtime_work.py#L84-L122): `runtime-wakes.sqlite3` wake queue입니다. offer 결과는 `queued`·`retained`·`dropped` 중 하나입니다. `dropped`는 queue에 남은 job이 아닙니다. 대기 중인 waiter는 dispatch lease만 잡습니다.
- [`core/laya_product_install.py:2022-2023`](../../../core/laya_product_install.py#L2022-L2023): 활성화 직후 `laya-sync` wake를 한 번 요청합니다. 그 결과는 보고만 하고 재시도하지 않으므로, `dropped`이면 이 요청만으로는 sync가 보장되지 않습니다.
- [`core/laya_product_install.py:839-843`](../../../core/laya_product_install.py#L839-L843): capacity store가 생긴 뒤에는 worker가 첫 bound generation을 게시할 때까지 recall이 아무것도 제공하지 않습니다.

### 범위 구분

- Laya membership·본문·FTS·vector·검색은 위키 카드만 대상으로 합니다.
- raw source의 path·hash cursor는 freshness 판단과 위키 생성을 위해서만 존재합니다. raw 본문은 색인되지 않으며 자동 컨텍스트가 되지 않습니다.
- 일반 위키 카드에 담긴 인용(발췌)과 source locator 링크는 유효한 출력입니다. 호스트 에이전트가 링크를 따라 raw 파일을 직접 읽을지는 호스트의 선택이며, 플러그인 성공 기준에 포함되지 않습니다.

### QA 증거와 미검증

- 현재 수정의 합성 회귀는 실제 운영 선택 경로나 recall 주입 통과를 뜻하지 않습니다. 기존 운영 선택본의 source query는 실패했고, 새 구현의 실제 재확인은 미실행입니다.
- sync worker가 generation을 게시하는 내부 단계와, 위키 카드가 바뀔 때 wake를 보내는 주체는 발췌로 일부만 확인했습니다. 그래서 도표에서는 일반 노드로 표시했습니다.

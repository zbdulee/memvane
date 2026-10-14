#!/usr/bin/env python3
"""raw 인덱스가 wiki 후보를 전역 창에서 밀어내는지(crowding) 반복 측정하는 진단 CLI.

로드맵 5단계. 근거는 `docs/plans/2026-07-30-raw-index-crowding-measurement.md`이고,
8단계(raw 제거)가 **제거 전/후를 같은 방법으로** 비교할 수 있게 그 수동 측정을
프로젝트 단위로 재현 가능하게 굳힌 것이다.

## 왜 hook(`QMD_SHADOW_QUERY`) 확장이 아니라 별도 CLI인가

기존 shadow 진단과 목적이 겹치지 않는다 — 재사용은 **코드 수준**(daemon 질의 형태,
frontmatter 파서, 로그 append)에서 하고 실행 경로는 분리한다:

1. **질의가 blocking hook 예산에 들어갈 수 없다.** shadow의 하위 질의는 전부
   `limit: 8` + 컬렉션 필터이고 총 예산 2.5s다. crowding 측정은 정반대로
   **필터 없는 전역 질의**를 `limit: 200`으로, 게다가 창 천장을 찾으려 limit 사다리
   (8/20/60/200)까지 돌린다. UserPromptSubmit을 막는 경로에 둘 수 없다.
2. **프로브가 재현 가능해야 한다.** shadow의 프로브는 사용자가 그때 입력한 프롬프트라
   두 번 같은 값이 나오지 않는다. 8단계의 전/후 비교는 **같은 프로브**를 요구한다.
   여기서는 프로젝트 wiki 카드 title에서 결정적으로 표집하고(정렬 + 균등 stride),
   뽑힌 문자열을 레코드에 남겨 `--probe`로 축자 재생할 수 있게 한다.
3. **답해야 하는 질문이 다르다.** shadow는 "이 recall이 무엇을 잃었나"(프롬프트 의존),
   여기는 "인덱스 구성이 전역 창에서 wiki를 밀어내나"(프롬프트 무의존). 후자는
   필터 없는 질의 대 필터 질의의 대조가 본질이고 shadow는 그 대조를 하지 않는다
   (항상 collections를 넘긴다).
4. **산출물 형태가 다르다.** shadow는 프롬프트마다 로그 한 줄, 여기는 프로젝트 단위
   스냅샷이라 전/후 두 줄을 나란히 놓고 diff해야 한다.

이 모듈은 어떤 hook에서도 import되지 않는다 — blocking hook 비용은 **구조적으로 0**이다.

## 판정 — 측정할 수 없는 것은 "측정 불가"로 보고한다

**틀린 판정은 없는 판정보다 나쁘다.** 이 산출물이 8단계(raw 제거) 결정 근거이므로,
관측이 두 세계를 구분하지 못하면 판정을 내지 않고 `measurable: false` + 이유를 남긴다.

측정: 프로브 하나에 같은 검색을 (a) 필터 없이 (b) 프로젝트 wiki 컬렉션으로 필터해
두 limit(recall 실제 limit, 큰 deep limit)에서 비교한다(질의 4회).

recall이 raw 때문에 굶을 수 있는 경로는 **하나뿐**이다: 엔진이 전역 후보 창을 먼저
만들고 **그 다음에** 컬렉션 필터를 적용하는 경우(post-filter). 그러면
recall이 받는 수 = min(limit, |전역 창 ∩ wiki|)이고, raw가 창을 비우면 그 수가 늘 수
있다. 반대로 엔진이 컬렉션별로 **독립 검색**한 뒤 병합하면 raw 유무는 recall이 받는
후보에 영향이 없다.

이 둘을 가르는 **양성 증거**는 하나다: `filteredDeep`에 전역 창에 없던 문서가 등장하면
(`newVsGlobal > 0`) 필터 결과가 전역 창의 부분집합이 아니므로 **독립 검색이 증명된다**.
이는 질의별 성질이 아니라 **엔진의 성질**이라 한 프로브의 증명이 그 경로 전체에 적용된다.

- `filterOrder: "scoped_retrieval_proven"` → raw는 recall이 받는 후보를 줄일 수 없다.
  `recallStarvation: false`가 **증명**된다(가정이 아니다).
- `filterOrder: "unresolved"` → `newVsGlobal == 0`. post-filter와도, 독립 검색인데 결과가
  우연히 일치한 것과도 모두 일치한다 → **측정 불가**. 굶은 칸은 상한만 보고한다.

실측(2026-07-30): **lex는 컬렉션당 20 · 전역 병합 40 cap**이라 `40`은 창이 아니라 cap이고,
`'판정'` 프로브에서 필터 결과 20 > 창의 wiki 16 이므로 **독립 검색이 증명된다**.
**vec은 6/6 프로브에서 필터 결과가 창의 wiki 부분집합과 정확히 일치**하고 창에 0칸이던
컬렉션을 scope하면 0을 돌려준다 — post-filter와 일치하지만 유사도 floor로도 설명되므로
증명은 아니다 → vec은 `unresolved`다.

## `starvedSlotsUpperBound`는 피해가 아니라 상한이다

`min(recallLimit − min(recallLimit, wikiInDeepWindow), 비-wiki 점유 칸)`.
**하한은 이 도구가 제공하지 않는다(항상 0).** `wikiInDeepWindow`는 "도달 가능한 wiki
총량"이 아니라 **점유당한 창 안의 wiki 수**이므로, 그 값이 작은 것이 "매칭이 적어서"인지
"밀려나서"인지 구분되지 않는다. 그 구분은 **raw를 제거해 재측정할 때만** 결정된다 —
이 도구의 존재 이유가 전/후를 같은 방법으로 비교 가능하게 만드는 것이다.
8단계가 이 값을 피해로 읽으면 과대 판정한다.

## 프로브 편향

프로브를 wiki 코퍼스에서 파생하므로 wiki에 유리하게 편향된다(실측: novel wiki는 인덱스의
4%인데 recall 창의 wiki 칸 평균 58% = 약 14배 enrichment). 즉 굶은 칸은 **과소** 추정이다.
좁은 프로브(카드 title)는 창을 못 채워 별도로 남긴다(`narrowProbeSummary`).
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))
import config as qmd_config
import keywords as qmd_keywords
import recall as qmd_recall

# `/3`: `starvedSlotsUpperBound`가 `scopedRetrievalProven`인 경로에서 0이 되고
# `starvedSlotsUpperBoundBasis`가 근거를 남긴다. `/1`·`/2` 레코드의 그 값은 **구 산식**
# (post-filter 가정)으로 계산된 것이라 증명된 경로에서도 0이 아니다 — 원장은 append-only라
# 지우지 않으므로, 전/후 비교 시 스키마로 걸러야 한다.
SCHEMA = "qmd_crowding_probe/3"

DEFAULT_PROBES = 3
DEFAULT_TITLE_PROBES = 2
# 엔진 내부 창을 드러내는 큰 limit. recall limit은 recall.DAEMON_QUERY_LIMIT를 쓴다
# (리터럴을 복제하면 측정과 실제 recall이 갈린다).
DEFAULT_DEEP_LIMIT = 200
# 넓은 프로브 하나에 넣는 상위 빈도 토큰 수.
VOCAB_TOKENS_PER_PROBE = 4
DEFAULT_QUERY_TIMEOUT = 15.0
# 데몬은 single-thread(Node)다. 연속 질의로 폭격하면 같은 데몬을 쓰는 recall hook이
# 직렬로 밀려 queryTimeout에 걸린다 — 질의 사이에 최소 간격을 둔다.
DEFAULT_INTERVAL = 0.2
DEFAULT_BUDGET = 180.0
# 창 천장 탐색 사다리. 21에서 천장에 닿는 실측(vec)을 재현할 수 있는 최소 집합이다.
CEILING_LIMITS = (8, 20, 60, 200)
# 프로브로 쓸 title의 최대 길이(질의 문자열). 카드 title은 신뢰 입력이 아니다.
MAX_PROBE_CHARS = 120
WIKI_META_BASENAMES = ("index.md", "log.md")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- qmd CLI


def resolve_qmd_bin() -> str | None:
    """qmd 실행 파일 경로. 해석 규칙은 `core/qmd_path.sh`가 SSOT라 그것을 source한다.

    파이썬에 후보 디렉터리 목록을 복제하면 두 벌이 갈린다(fnm/nvm 버전 정렬까지 있다).
    """
    script = Path(__file__).parent / "qmd_path.sh"
    if not script.is_file():
        return None
    try:
        proc = subprocess.run(
            ["bash", "-c", f'. "{script}" && resolve_qmd_bin'],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    path = (proc.stdout or "").strip().splitlines()
    return path[-1] if proc.returncode == 0 and path else None


def parse_collection_list(text: str) -> dict[str, int]:
    """`qmd collection list` 텍스트 출력 → {컬렉션명: 파일 수}.

    `--json`이 없어서 텍스트를 판다. 형식이 바뀌면 빈 dict가 되고 호출부는 인덱스
    구성을 `unavailable`로 남긴 채 창 측정을 계속한다(fail-open).
    """
    counts: dict[str, int] = {}
    current: str | None = None
    for line in text.split("\n"):
        if not line.startswith((" ", "\t")):
            name, sep, rest = line.partition(" (qmd://")
            current = name.strip() if sep and name.strip() else None
            if current:
                counts.setdefault(current, 0)
            continue
        stripped = line.strip()
        if current and stripped.startswith("Files:"):
            value = stripped.split(":", 1)[1].strip()
            try:
                counts[current] = int(value)
            except ValueError:
                pass
    return counts


def collection_root(qmd_bin: str, name: str) -> str | None:
    """`qmd collection show <name>`의 Path. 외부 컬렉션의 role을 알아내는 유일한 단서다."""
    try:
        proc = subprocess.run(
            [qmd_bin, "collection", "show", name],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    for line in (proc.stdout or "").split("\n"):
        stripped = line.strip()
        if stripped.startswith("Path:"):
            return stripped.split(":", 1)[1].strip() or None
    return None


def index_composition(qmd_bin: str | None, project_roles: dict[str, str],
                      wiki_names: set[str]) -> dict:
    """전역 인덱스의 컬렉션·파일 수와 **role별 집계**.

    측정 대상 프로젝트 밖 컬렉션의 role은 그 컬렉션 root에서 상향 탐색해 그 프로젝트의
    `collectionRoles`를 읽어 얻는다(`config.find_project_config` 재사용). 이름 규칙
    (`*-wiki`)으로 추측하지 않는다 — 추측은 baseline에 거짓 수치를 굳힌다. 알 수 없으면
    `unknown`으로 남긴다.
    """
    if not qmd_bin:
        return {"status": "qmd_cli_unavailable"}
    try:
        proc = subprocess.run(
            [qmd_bin, "collection", "list"], capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return {"status": "qmd_cli_error"}
    if proc.returncode != 0:
        return {"status": "qmd_cli_error", "exitCode": proc.returncode}
    counts = parse_collection_list(proc.stdout or "")
    if not counts:
        return {"status": "unparsed"}
    roles: dict[str, str] = {}
    by_role: dict[str, int] = {}
    for name, files in counts.items():
        if name in project_roles:
            # 런타임이 실제로 적용하는 role로 라벨한다(`config.collection_role` —
            # 키 없음은 `raw`, 값 미지는 `invalid` fail-closed). 원문 문자열을 그대로
            # 쓰면 오타가 어떤 코드 경로도 인정하지 않는 버킷으로 baseline에 굳는다.
            role = qmd_config.collection_role(project_roles, name)
        else:
            root = collection_root(qmd_bin, name)
            role = "unknown"
            if root:
                found = qmd_config.find_project_config(root)
                external_roles = qmd_config.role_map(found.get("config", {}))
                # 키가 있을 때만 라벨한다 — 없으면 `unknown`을 유지한다(설정 파일이
                # 없는 레거시 컬렉션을 `raw`로 단정하지 않는다는 기존 규칙). 있으면
                # 값은 프로젝트 안 컬렉션과 **같은 SSOT**로 정규화한다.
                if name in external_roles:
                    role = qmd_config.collection_role(external_roles, name)
        roles[name] = role
        by_role[role] = by_role.get(role, 0) + files
    total = sum(counts.values())
    wiki_files = by_role.get("wiki", 0)
    return {
        "status": "ok",
        "collections": len(counts),
        "files": total,
        "byRole": by_role,
        "byCollection": counts,
        "roleByCollection": roles,
        "projectWikiFiles": sum(counts.get(n, 0) for n in wiki_names),
        # **비-wiki가 headline 이다.** role별 분류는 설정 파일이 있는 컬렉션에만 가능해
        # 레거시 컬렉션이 unknown 으로 빠지는데(이 머신 1034 파일), 창 점유 관점에서는
        # raw 든 unknown 이든 똑같이 wiki 를 밀어내는 쪽이다. 이름 기반 추정(`*-manuscript`
        # → raw)과 설정 기반 분류가 갈려 보이던 것은 실은 같은 수를 다른 입도로 센 것이다.
        "wikiFiles": wiki_files,
        "nonWikiFiles": total - wiki_files,
        "nonWikiShare": round((total - wiki_files) / total, 3) if total else None,
        "roleSource": "project-config + per-collection root lookup",
        "roleNote": "설정 파일이 없는 레거시 컬렉션은 unknown이다(추측하지 않는다). "
                    "비-wiki 집계는 unknown을 포함하므로 판정에 영향이 없다.",
    }


# ---------------------------------------------------------------- probes


def card_title(path: Path) -> str:
    """wiki 카드 frontmatter의 title. 파서는 recall과 같은 것을 쓴다(규칙 SSOT)."""
    limit = qmd_recall.wiki_card_read_limit(0)
    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as handle:
            raw_text = handle.read(limit)
    except (OSError, ValueError):
        return ""
    text = qmd_recall.normalize_newlines(raw_text)
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    if end == -1:
        return ""
    fields = qmd_recall.parse_frontmatter_scalars(text[3:end])
    return qmd_recall.sanitize_inline(fields.get("title", ""))[:MAX_PROBE_CHARS]


def collect_card_titles(wiki_root: Path, skip_paths: list[str]) -> list[tuple[str, str]]:
    """(title, wiki_root 상대경로) 목록. 경로 정렬이라 회차 간 순서가 안정적이다."""
    try:
        files = sorted(
            p for p in wiki_root.rglob("*.md")
            if p.is_file()
            and p.name not in WIKI_META_BASENAMES
            and not any(skip in p.as_posix() for skip in skip_paths)
        )
    except OSError:
        return []
    titled: list[tuple[str, str]] = []
    for path in files:
        title = card_title(path)
        if len(title) >= 4:
            try:
                rel = path.relative_to(wiki_root).as_posix()
            except ValueError:
                rel = path.name
            titled.append((title, rel))
    return titled


def vocab_probes(titles: list[str], count: int) -> list[dict]:
    """wiki 코퍼스 title의 **상위 빈도 어휘**로 넓은 프로브를 만든다.

    창 포화가 crowding 판정의 전제이고, 좁은 질의는 창을 채우지 못한다(카드 title
    프로브 실측 2·20·1칸). 고빈도 어휘는 정의상 많은 문서와 매칭하므로 창을 채운다
    (실측: 상위 4토큰 프로브가 vec 28칸 — 엔진 상한 절단).

    토큰화는 `keywords.extract_keywords`를 그대로 쓴다(불용어·한국어 접미 규칙 SSOT).
    순위는 (빈도 내림, 토큰 오름)으로 **동수까지 결정적**이고, 겹치지 않는 창으로
    잘라 서로 다른 넓은 프로브를 만든다.
    """
    if count <= 0 or not titles:
        return []
    counter: collections.Counter = collections.Counter()
    for title in titles:
        for token in qmd_keywords.extract_keywords(title):
            token = token.lower()
            if len(token) >= 2:
                counter[token] += 1
    ranked = [token for token, _ in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))]
    probes = []
    for index in range(count):
        window = ranked[index * VOCAB_TOKENS_PER_PROBE:(index + 1) * VOCAB_TOKENS_PER_PROBE]
        if not window:
            break
        probes.append({
            "query": " ".join(window),
            # lex는 한 문자열 안의 positive term을 **AND로 결합**한다(CLAUDE.md / store.js
            # `positive.join(' AND ')`). 4토큰을 그대로 lex로 보내면 한 문서가 넷을 다
            # 가져야 해 오히려 가장 좁은 질의가 된다(실측: 넓은 vec 프로브의 lex 결과가
            # wiki 2건). 그래서 lex 질의는 그 그룹의 **최빈 토큰 하나**로 보낸다.
            "lexQuery": window[0],
            "card": None,
            "source": "wiki_vocab",
            "kind": "broad",
            "tokens": window,
            "tokenCounts": [counter[t] for t in window],
        })
    return probes


def title_probes(titled: list[tuple[str, str]], count: int) -> list[dict]:
    """카드 title에서 **결정적으로** 표집한 좁은 프로브.

    고정 목록은 프로젝트마다 무의미하고 실제 프롬프트는 재현되지 않는다. 정렬 +
    균등 stride라 같은 코퍼스에서 항상 같은 값이 나온다. 판정용이 아니라 "평상시
    좁은 질의에서 창이 포화되는가"를 같은 레코드에 남기기 위한 대조군이다.
    """
    if count <= 0 or not titled:
        return []
    if len(titled) <= count:
        picked = titled
    else:
        span = len(titled) - 1
        indices = sorted({round(i * span / (count - 1)) if count > 1 else 0
                          for i in range(count)})
        picked = [titled[i] for i in indices]
    return [{"query": title, "card": rel, "source": "wiki_title", "kind": "narrow"}
            for title, rel in picked]


# ---------------------------------------------------------------- daemon


class Budget:
    """질의 수·간격·전체 wall-clock을 한곳에서 유계로 만들고 **실패 수를 센다**.

    실패 수가 필요한 이유: append-only 전/후 원장에서 열화된 "after"가 온전한 "before"와
    조용히 비교되면 안 된다. 레코드 `status`/`comparable`이 이 카운터에서 나온다.
    """

    def __init__(self, interval: float, budget: float) -> None:
        self.interval = max(0.0, interval)
        self.deadline = time.monotonic() + max(0.0, budget)
        # attempts = 실제로 HTTP 를 시도한 수, queries 는 하위호환 별칭.
        self.queries = 0
        self.ok = 0
        self.failures = 0
        # 예산 소진으로 **시도조차 못 한** 질의. 실패와 구분해야 status 가 "왜"를 남긴다.
        self.budget_skips = 0
        self._last = 0.0

    def status(self) -> str:
        if self.ok == 0 and (self.failures or self.budget_skips):
            return "budget_exhausted" if self.budget_skips and not self.failures \
                else "all_queries_failed"
        if self.failures or self.budget_skips:
            return "degraded"
        return "ok"

    def exhausted(self) -> bool:
        return time.monotonic() >= self.deadline

    def before_query(self) -> None:
        if self._last:
            wait = self.interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
        self._last = time.monotonic()
        self.queries += 1


def query_daemon(daemon_url: str, searches: list[dict], collections: list[str] | None,
                 limit: int, timeout: float, budget: Budget) -> dict:
    """1회 질의. 실패는 예외가 아니라 status 문자열로만 남긴다(진단은 절대 죽지 않는다).

    `collections`가 None이면 전역(필터 없음)이다 — 이 대조가 측정의 본질이라
    키를 생략하는 경로를 반드시 유지해야 한다.
    """
    if budget.exhausted():
        budget.budget_skips += 1
        return {"status": "budget_exhausted"}
    payload: dict = {
        "searches": searches,
        "limit": limit,
        "minScore": 0,
        "timeout": timeout,
        "rerank": False,
    }
    if collections is not None:
        if not collections:
            budget.failures += 1
            return {"status": "no_collections"}
        payload["collections"] = list(collections)
    budget.before_query()
    try:
        req = urllib.request.Request(
            f"{daemon_url}/query",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
        parsed = json.loads(body)
        # 응답이 object가 아니면(`null`·배열·문자열) `.get`이 AttributeError를 던져
        # **실행 전체가 죽고 원장에 아무것도 남지 않았다** — append는 마지막에 1회뿐이라
        # 앞선 질의가 전부 유실된다. 진단 경로는 어떤 응답에도 status로만 답해야 한다.
        results = parsed.get("results", []) if isinstance(parsed, dict) else None
    except Exception:  # noqa: BLE001 - 진단 질의는 절대 예외를 밖으로 내지 않는다
        budget.failures += 1
        return {"status": "unavailable"}
    if not isinstance(results, list):
        budget.failures += 1
        return {"status": "bad_response"}
    budget.ok += 1
    # 결과 수를 `files` 길이로 센다 — `file`이 없는 항목은 컬렉션 귀속이 불가해 창 구성
    # 집계에 쓸 수 없다. len(results)를 따로 두면 두 카운트가 갈려 오독을 만든다.
    files = []
    for result in results:
        if isinstance(result, dict):
            file = result.get("file")
            if isinstance(file, str) and file:
                files.append(file)
    return {"status": "ok", "count": len(files), "files": files}


def window_composition(files: list[str], known: dict[str, str],
                       wiki_names: set[str]) -> dict:
    """전역 창의 컬렉션·role 구성. 데몬 `file`은 `collection/path`(스킴 없음)로도 온다."""
    by_collection: dict[str, int] = {}
    by_role: dict[str, int] = {}
    for file in files:
        collection = qmd_recall.qmd_uri_to_collection(file)
        by_collection[collection] = by_collection.get(collection, 0) + 1
        role = known.get(collection, "unknown")
        by_role[role] = by_role.get(role, 0) + 1
    return {
        "byCollection": by_collection,
        "byRole": by_role,
        "projectWikiSlots": sum(by_collection.get(n, 0) for n in wiki_names),
    }


def starvation_metric_applies(strategy: str) -> dict:
    """굶음 지표가 이 프로젝트의 **실제 recall 경로에 해당하는가**.

    상한 산식은 "wiki와 raw가 같은 `topN`을 나눠 가진다"를 전제한다. 그런데 그 전제가
    성립하는 전략은 `flat` **하나뿐**이다:
      - `hierarchical` — `recall.prefer_wiki`(`core/recall.py:1687`)가 wiki 히트가 하나라도
        있으면 raw를 **통째로 내린다**. 남은 wiki만으로 topN을 채우므로 raw가 wiki 칸을
        가져갈 수 없다(wiki가 0건일 때만 raw backfill이 들어오고, 그때는 경쟁이 없다).
      - `wikiOnly` — 데몬 질의 자체가 wiki 컬렉션만 대상이라 raw가 후보에 없다.
    그래서 이 지표를 단독 숫자로 읽으면 hierarchical/wikiOnly 프로젝트에서 **해당하지 않는
    값**을 피해로 오해한다. 레코드에 판정을 함께 남겨 그 오해를 구조적으로 막는다
    (측정 자체는 어느 전략에서도 유효하다 — 재는 것은 데몬의 창 구성이다).
    """
    if strategy == "flat":
        return {
            "applies": True, "strategy": strategy,
            "basis": "flat은 wiki·raw 후보가 같은 topN을 공유한다 — 굶음 지표가 해당한다",
        }
    if strategy == "wikiOnly":
        return {
            "applies": False, "strategy": strategy,
            "basis": "wikiOnly는 wiki 컬렉션만 질의하므로 raw가 후보에 없다",
        }
    return {
        "applies": False, "strategy": strategy or "hierarchical",
        "basis": ("hierarchical은 wiki 히트가 있으면 raw를 통째로 내린다"
                  "(recall.prefer_wiki) — wiki·raw가 topN을 공유하지 않는다"),
    }


def starved_bound_basis(entry: dict) -> str:
    """`starvedSlotsUpperBound`가 어느 전제 위에서 계산됐는가 — 규칙은 여기 한 곳이다.

    `measure_path`(레코드 작성)와 `summarize_path`(집계)가 같은 함수를 쓴다. 집계 쪽에서
    저장된 키를 그냥 읽으면, 키가 없는 구 스키마·합성 entry에서 KeyError로 죽거나(측정 전체
    유실) 조용히 다른 규칙으로 갈린다.
    """
    stored = entry.get("starvedSlotsUpperBoundBasis")
    if isinstance(stored, str) and stored:
        return stored
    return "scoped_retrieval_proven" if entry.get("scopedRetrievalProven") else "post_filter_assumption"


def measure_path(daemon_url: str, path_type: str, probe_query: str,
                 wiki_collections: list[str], known_roles: dict[str, str],
                 wiki_names: set[str], recall_limit: int, deep_limit: int,
                 timeout: float, budget: Budget) -> dict:
    """한 프로브 × 한 경로(vec|lex): 두 limit × {전역, wiki 필터} = 질의 4회.

    두 limit 이 하는 일이 다르다:
    - `recall_limit`(=recall이 실제로 보내는 limit) — recall이 **받는 후보 수**를 잰다.
      피해가 있다면 그것이 줄어드는 형태로만 나타난다.
    - `deep_limit` — 필터 순서 판정(`scopedRetrievalProven`)과 전역 결과 구성.
      **lex에서는 이 값이 창이 아니라 엔진 cap일 수 있다**(`detect_engine_cap`).

    어느 limit 에서도 "밀려난 wiki 문서 수"는 직접 관측되지 않는다 — 상한만 나온다.
    """
    searches = [{"type": path_type, "query": probe_query}]
    blocks = {
        "globalRecall": query_daemon(
            daemon_url, searches, None, recall_limit, timeout, budget),
        "filteredRecall": query_daemon(
            daemon_url, searches, wiki_collections, recall_limit, timeout, budget),
        "globalDeep": query_daemon(
            daemon_url, searches, None, deep_limit, timeout, budget),
        "filteredDeep": query_daemon(
            daemon_url, searches, wiki_collections, deep_limit, timeout, budget),
    }
    entry: dict = {
        "recallLimit": recall_limit,
        "deepLimit": deep_limit,
        "statuses": {name: block.get("status") for name, block in blocks.items()},
    }
    if any(block.get("status") != "ok" for block in blocks.values()):
        entry["informative"] = False
        entry["skipReason"] = "query_failed"
        return entry

    files = {name: block.get("files", []) for name, block in blocks.items()}
    for name in ("globalRecall", "globalDeep"):
        entry[name] = {"slots": len(files[name])}
        entry[name].update(window_composition(files[name], known_roles, wiki_names))
    for name, global_name in (("filteredRecall", "globalRecall"),
                              ("filteredDeep", "globalDeep")):
        seen = set(files[global_name])
        entry[name] = {
            "count": len(files[name]),
            "newVsGlobal": len([f for f in files[name] if f not in seen]),
        }

    recall_filled = entry["filteredRecall"]["count"]
    wiki_in_deep = entry["filteredDeep"]["count"]
    entry["recallSlotsFilled"] = recall_filled
    entry["recallSlotsUnfilled"] = max(0, recall_limit - recall_filled)
    # **이름 주의**: "도달 가능한 wiki pool"이 아니다. 필터 질의가 deep limit에서 낸
    # wiki 문서 수이고, post-filter 엔진에서는 그것이 곧 |전역 창 ∩ wiki|다. 이 값이
    # 작은 것이 "매칭이 적어서"인지 "밀려나서"인지는 이 관측으로 구분되지 않는다.
    entry["wikiInDeepWindow"] = wiki_in_deep
    non_wiki_deep = entry["globalDeep"]["slots"] - entry["globalDeep"]["projectWikiSlots"]
    entry["deepWindowNonWikiSlots"] = non_wiki_deep

    # 관측만 남긴다(추론 아님): recall limit에서 필터 결과에 창에 없던 문서가 몇 건인가.
    # 예전엔 이 값 > 0 을 "필터가 창 이전"의 증거로 썼으나 **틀렸다** — post-filter
    # 엔진도 큰 전역 창을 만든 뒤 limit으로 자르므로, 작은 limit의 전역 결과에는 없던
    # 문서가 필터 결과에 있을 수 있다. 필터 순서 판정은 deep limit에서만 성립한다.
    entry["newInFilteredAtRecallLimit"] = entry["filteredRecall"]["newVsGlobal"]

    # **유일한 양성 증거**: deep limit에서 필터 결과가 전역 창의 부분집합이 아니다
    # → 필터 질의가 전역 창을 걸러낸 것이 아니라 독립 검색을 했다(엔진의 성질).
    proven = entry["filteredDeep"]["newVsGlobal"] > 0
    entry["scopedRetrievalProven"] = proven
    # 우리 deep limit이 필터 결과를 잘랐으면 wikiInDeepWindow가 절단값이라 판정 불가.
    entry["filteredDeepTruncated"] = wiki_in_deep >= deep_limit
    # **상한 산식은 post-filter 가정 위에서만 성립한다.** 컬렉션별 독립 검색이 증명되면
    # 비-wiki 인덱스가 wiki 후보를 밀어낼 **경로 자체가 없으므로** 상한은 0이다. 예전에는
    # 증명이 있어도 전역 deep 결과로 계산해, 8단계 결론("제거 이득 0")과 모순되는 잔여
    # `starvedSlotsUpperBound`를 냈다(실측 `globalDeep`=[wiki 1, raw 7] / `filteredDeep`=
    # [wiki 2] / 양쪽 recall 동일에서 `scopedRetrievalProven: true` + `starvedUB: 6`).
    # 그 잔여값은 "매칭이 적음"으로 설명해 넘길 것이 아니라 **무효한 지표**였다.
    # 근거를 필드로 남긴다 — 0을 냈을 때 "굶음이 없다"와 "산식이 적용되지 않았다"를
    # 사후에 구분할 수 있어야 한다.
    entry["starvedSlotsUpperBoundBasis"] = starved_bound_basis(entry)
    entry["starvedSlotsUpperBound"] = 0 if proven else min(
        max(0, recall_limit - min(recall_limit, wiki_in_deep)), non_wiki_deep)
    # 하한은 이 도구가 제공하지 않는다. 필드로 못박아 8단계가 상한을 피해로 읽지 못하게 한다.
    entry["starvedSlotsLowerBound"] = 0
    entry["informative"] = entry["globalDeep"]["slots"] > 0
    if not entry["informative"]:
        entry["skipReason"] = "no_global_results"
    return entry


def ceiling_ladder(daemon_url: str, probe_query: str, timeout: float,
                   budget: Budget) -> dict:
    """limit을 올려도 결과가 늘지 않는 지점 = 내부 창 크기. 실행당 1회, vec 경로만.

    lex는 컬렉션당 상한이 따로 있어 전역 사다리로는 창을 특정할 수 없다.
    """
    searches = [{"type": "vec", "query": probe_query}]
    counts: dict[str, int] = {}
    for limit in CEILING_LIMITS:
        block = query_daemon(daemon_url, searches, None, limit, timeout, budget)
        counts[str(limit)] = (
            block.get("count", 0) if block.get("status") == "ok" else None)
    measured = [(limit, counts[str(limit)]) for limit in CEILING_LIMITS
                if counts[str(limit)] is not None]
    plateau_count = measured[-1][1] if measured else None
    # 천장은 "최종 count에 처음 도달한 limit"이다. 직전 rung과 같아진 지점을 쓰면
    # 마지막 rung을 가리켜(8→20→21→21에서 200) 창 크기를 21이 아니라 200처럼 읽힌다.
    plateau_from = next(
        (limit for limit, count in measured if count == plateau_count), None)
    return {
        "path": "vec",
        "probe": probe_query,
        "counts": counts,
        "plateauCount": plateau_count,
        "plateauFromLimit": plateau_from,
        "limitBound": bool(measured) and plateau_count >= CEILING_LIMITS[-1],
        "note": "plateauCount = 내부 창이 낸 문서 수. plateauFromLimit부터 limit을 "
                "올려도 늘지 않는다. limitBound면 사다리 최대 limit에 걸려 천장을 못 봤다.",
    }


# ---------------------------------------------------------------- verdict


def detect_engine_cap(entries: list[dict], deep_limit: int) -> dict:
    """엔진의 결과 수 **cap**을 창과 구분한다.

    실측(2026-07-30): qmd의 lex(FTS)는 **컬렉션당 20 · 전역 병합 40** cap이다. 그래서
    `deepLimit=200`에서 나온 `40`은 "내부 창 크기"가 아니라 cap이고, `wikiInDeepWindow=20`
    도 cap 값이다 — 이 값으로 굶은 칸을 계산하면 `--recall-limit > 20`에서 **확정 오탐**이다.

    감지 신호: 어휘가 다른 프로브 둘 이상이 **정확히 같은** 결과 수를 냈고 그 값이
    deep limit 미만이면 cap이다. 서로 다른 질의의 매칭 문서 수가 우연히 같은 값으로
    반복될 확률은 낮다. `suspected`로만 부르고(증명 아님) 판정은 보수적인 쪽으로만
    움직인다 — 오탐이면 이미 `unresolved`였을 판정의 **이유만** 바뀐다.
    """
    signals = {}
    for key, field in (("filtered", "wikiInDeepWindow"), ("global", "slots")):
        counts = [
            (e[field] if field != "slots" else e["globalDeep"]["slots"])
            for e in entries if e.get("informative")
        ]
        repeated = sorted({c for c in counts if counts.count(c) >= 2 and 0 < c < deep_limit})
        if repeated:
            signals[key] = repeated
    return {"suspected": bool(signals), "repeatedCounts": signals}


def summarize_path(entries: list[dict], deep_limit: int) -> dict:
    """경로 하나의 판정. **측정 불가는 판정하지 않는다.**

    raw가 recall을 굶길 수 있는 경로는 "전역 창을 만든 뒤 컬렉션 필터"(post-filter)
    하나뿐이다. 그것을 반증하는 양성 증거(`scopedRetrievalProven`)가 있으면
    `recallStarvation: false`를 **증명**하고, 없으면 `measurable: false`로 남긴다.

    `windowCrowding` 같은 boolean은 두지 않는다 — 창 점유(구성 사실)를 "crowding"으로
    부르면 recall 피해로 오독되고, 그 오독이 이 도구가 정정하려는 바로 그 오류다.
    창 구성은 숫자로만 낸다(`deepWindowNonWikiSlots`/`deepWindowNonWikiShare`).
    """
    informative = [e for e in entries if e.get("informative")]
    cap = detect_engine_cap(entries, deep_limit)
    summary: dict = {
        "probesMeasured": len(entries),
        "probesInformative": len(informative),
        "skipReasons": sorted({e["skipReason"] for e in entries if e.get("skipReason")}),
        "recallLimit": informative[0]["recallLimit"] if informative else None,
        "deepLimit": deep_limit,
        # recall이 실제로 쓰는 limit에서의 전역 창 점유(구성 사실)
        "recallWindowSlots": [e["globalRecall"]["slots"] for e in informative],
        "recallWindowWikiSlots": [e["globalRecall"]["projectWikiSlots"] for e in informative],
        # deep limit에서의 전역 결과 구성. **lex에서는 창이 아니라 cap일 수 있다.**
        "deepWindowSlots": [e["globalDeep"]["slots"] for e in informative],
        "deepWindowWikiSlots": [e["globalDeep"]["projectWikiSlots"] for e in informative],
        "deepWindowNonWikiSlots": [e["deepWindowNonWikiSlots"] for e in informative],
        # recall이 실제로 받는 것
        "recallSlotsFilled": [e["recallSlotsFilled"] for e in informative],
        "wikiInDeepWindow": [e["wikiInDeepWindow"] for e in informative],
        "newInFilteredAtDeepLimit": [e["filteredDeep"]["newVsGlobal"] for e in informative],
        "starvedSlotsUpperBound": [e["starvedSlotsUpperBound"] for e in informative],
        "starvedSlotsUpperBoundBasis": sorted(
            {starved_bound_basis(e) for e in informative}),
        "starvedSlotsLowerBound": [0 for _ in informative],
        "probesScopedRetrievalProven": len(
            [e for e in informative if e.get("scopedRetrievalProven")]),
        "engineCap": cap,
    }
    total_slots = sum(summary["deepWindowSlots"])
    summary["deepWindowNonWikiShare"] = (
        round(sum(summary["deepWindowNonWikiSlots"]) / total_slots, 3)
        if total_slots else None)

    if not informative:
        summary.update({
            "filterOrder": "unresolved", "measurable": False,
            "recallStarvation": None, "reason": "no_results",
            "basis": "전역 질의가 결과를 내지 못했다 — 판정하지 않는다",
        })
        return summary
    if any(e.get("filteredDeepTruncated") for e in informative):
        summary.update({
            "filterOrder": "unresolved", "measurable": False,
            "recallStarvation": None, "reason": "truncated_by_deep_limit",
            "basis": "필터 결과가 우리 deep limit에 걸려 잘렸다 — --deep-limit을 올려 재측정하라",
        })
        return summary
    if summary["probesScopedRetrievalProven"] > 0:
        # 엔진의 성질이므로 한 프로브의 증명이 그 경로 전체에 적용된다.
        summary.update({
            "filterOrder": "scoped_retrieval_proven", "measurable": True,
            "recallStarvation": False, "reason": "scoped_retrieval_proven",
            "basis": (
                f"프로브 {summary['probesScopedRetrievalProven']}/{len(informative)}에서 "
                "필터 결과가 전역 창의 부분집합이 아니다 → 필터 질의는 전역 창을 걸러낸 것이 "
                "아니라 독립 검색이다(엔진 성질) → 비-wiki 인덱스는 recall이 받는 후보를 "
                "줄일 수 없다. 창 점유 수치는 구성 사실일 뿐 피해가 아니고, "
                "starvedSlotsUpperBound는 0이다(밀어낼 경로 자체가 없다)"),
        })
        return summary
    if cap["suspected"]:
        summary.update({
            "filterOrder": "unresolved", "measurable": False,
            "recallStarvation": None, "reason": "engine_cap_suspected",
            "basis": (
                f"프로브 여럿이 같은 결과 수를 반복한다({cap['repeatedCounts']}) → 창이 아니라 "
                "엔진 cap이다. cap 값으로 계산한 굶은 칸은 무의미하므로 판정하지 않는다"),
        })
        return summary
    summary.update({
        "filterOrder": "unresolved", "measurable": False,
        "recallStarvation": None, "reason": "post_filter_vs_scoped_retrieval_ambiguous",
        "basis": (
            "deep limit에서 필터 결과가 전역 창의 wiki 부분집합과 일치한다 — post-filter"
            "(밀려남)와도, 독립 검색인데 매칭이 그것뿐인 경우와도 모두 일치한다. "
            "starvedSlotsUpperBound는 post-filter 가정 위의 상한이고 하한은 0이다. "
            "8단계 실측에서는 orphan 벡터를 정리해 창에 여유를 만들자 독립 검색이 드러났다"
            "(scoped_retrieval_proven) — 이 애매함의 해소 경로는 raw 제거가 아니라 그쪽이다"),
    })
    return summary


LIMITATIONS = [
    "**starvedSlotsUpperBound는 상한이고 하한은 0이다(도구가 하한을 제공하지 않는다).** 그리고 이 산식은 post-filter 가정 위에서만 성립한다 — `scopedRetrievalProven`인 경로에서는 비-wiki 인덱스가 wiki 후보를 밀어낼 경로가 없으므로 상한이 **0**이고, 그 사실은 `starvedSlotsUpperBoundBasis`로 구분된다. 가정이 남아 있는 경로(`post_filter_assumption`)에서는 wikiInDeepWindow가 작은 것이 '매칭이 적어서'인지 '밀려나서'인지 이 관측으로 구분되지 않는다. 이 값을 피해로 읽으면 과대 판정이다.",
    "**굶음 지표는 `recallStrategy: flat`에만 해당한다**(레코드의 `starvationMetricApplies`). 상한 산식은 wiki·raw가 같은 topN을 나눠 가진다는 전제인데, `hierarchical`은 `recall.prefer_wiki`(`core/recall.py:1687`)가 wiki 히트가 있으면 raw를 통째로 내리고 `wikiOnly`는 wiki 컬렉션만 질의하므로 두 전략에서는 애초에 경쟁이 없다. 측정 자체(데몬 창 구성)는 어느 전략에서도 유효하지만, 상한 숫자를 그 프로젝트의 recall 피해로 읽으면 틀린다.",
    "filterOrder가 unresolved인 경로는 recallStarvation을 판정하지 않는다(null). 관측이 post-filter와 독립 검색을 구분하지 못하기 때문이고, 틀린 판정을 남기는 것보다 낫다.",
    "scoped_retrieval_proven은 한 프로브의 관측을 **엔진의 성질**로 일반화한 것이다(질의별 성질이 아니라는 전제). qmd 구현이 바뀌면 다시 재야 한다.",
    "deep limit에서의 전역 결과 수는 창일 수도 엔진 cap일 수도 있다. 실측 qmd 2.5.3 lex는 컬렉션당 20 · 전역 병합 40 cap이며, detect_engine_cap이 '어휘가 다른 프로브 둘 이상이 같은 수를 반복'하는 신호로 이를 감지한다 — suspected이고 증명은 아니다.",
    "**프로브가 wiki 쪽으로 편향돼 있다(오차 방향).** 프로브를 wiki 카드 어휘에서 파생하므로 wiki가 유리하다 — 실측 novel wiki는 인덱스의 4%인데 recall 창의 wiki 칸이 평균 58%(약 14배 enrichment), service-engineering은 24% 대 37.5%다. 따라서 굶은 칸(상한)은 **과소** 추정이다. 반대로 cap을 창으로 오인하면 과대가 되므로 오차는 **양방향**이다.",
    "**표본이 넓은 프로브 3개이고 신뢰구간이 없다.** 'N/3 프로브' 류의 비율은 8단계 판정 근거로 부족하다. 다만 표본 수가 binding constraint가 아니다 — 위 식별 불가(상한만) 문제 때문에 프로브를 늘려도 판정이 나오지 않는다. 비-wiki 어휘 대조군 프로브는 넣지 않았다(같은 이유로 판정을 만들지 못하고, raw 소스 코퍼스 스캔이 추가로 필요하다). 외부 프로브 집합은 --probe로 넣을 수 있다.",
    "이 머신의 전역 인덱스 구성 한 회차 측정이다. 비-wiki 비율이 다른 프로젝트에서는 창 점유율이 달라진다.",
    "측정 대상 프로젝트 밖 컬렉션의 role은 그 컬렉션 root에서 settings.json을 찾아 읽는다 — 설정 파일이 없는 레거시 컬렉션은 unknown이다(추측하지 않는다). headline은 role이 아니라 nonWikiFiles이므로 판정에 영향이 없다.",
    "rerank=True 경로는 측정하지 않는다 — recall이 rerank:false로 질의하므로 측정도 같은 경로만 본다.",
    "프로브는 wiki 코퍼스에서 결정적으로 파생한다(넓은=상위 빈도 어휘, 좁은=카드 title). 실제 사용자 프롬프트 분포가 아니며, 카드가 추가/삭제되면 같은 count에서도 파생 결과가 달라진다 — 전/후를 축자 동일 프로브로 비교하려면 이전 레코드의 probes[].query를 --probe로 넘긴다.",
    "판정은 넓은 프로브만 쓴다. 좁은 프로브는 전역 결과를 채우지 못해 별도로 남는다(narrowProbeSummary).",
    "lex 질의는 그룹의 최빈 토큰 하나만 보낸다(qmd가 한 lex 문자열의 term을 AND 결합하므로). vec 질의는 토큰 전체를 보낸다 — 두 경로의 질의 문자열이 다르므로 경로 간 절대 비교는 하지 않는다.",
    "status가 ok가 아닌 레코드(degraded/all_queries_failed/error/budget_exhausted)는 comparable:false다. 전/후 비교에서 배제하라 — 살아남은 프로브만으로 계산된 summary가 온전한 레코드와 섞이면 안 된다.",
]


def build_record(project_dir: str, args: argparse.Namespace) -> dict:
    found = qmd_config.find_project_config(project_dir)
    config = found.get("config", {})
    project_root = found.get("projectRoot", project_dir)
    collections = config.get("collections", []) or []
    roles = qmd_config.role_map(config)
    # 세 목록은 **여집합이 아니다**. 이 도구는 qmd 인덱스 점유를 재므로 인덱스에 없는
    # role `source`는 wiki에도 raw에도 들어가면 안 된다 — `!= "wiki"`로 raw를 정의하던
    # 동안에는 source가 raw로 새어 "제거했는데도 raw가 남아 있다"는 판정이 나왔다.
    # `sourceCollections`를 함께 남기는 이유: 8단계는 raw→source 전환 전/후를 이 원장으로
    # 비교하는데, 목록의 합이 collections와 다르면 그 차이가 무엇인지 기록이 없으면
    # "설정이 바뀐 것"인지 "측정이 틀린 것"인지 사후에 구분할 수 없다.
    wiki_collections = qmd_config.wiki_collections(collections, roles)
    raw_collections = qmd_config.recall_raw_collections(collections, roles)
    source_collections = [
        c for c in collections
        if isinstance(c, str)
        and qmd_config.collection_role(roles, c) == qmd_config.COLLECTION_ROLE_SOURCE
    ]
    wiki_names = set(wiki_collections)
    record: dict = {
        "ts": now_iso(),
        "schema": SCHEMA,
        "label": args.label or "",
        "project": {
            "root": project_root,
            "name": config.get("name", ""),
            "configPath": found.get("configPath"),
            "recallStrategy": config.get("recallStrategy", "hierarchical"),
            "collections": list(collections),
            "wikiCollections": wiki_collections,
            "rawCollections": raw_collections,
            "sourceCollections": source_collections,
        },
        "starvationMetricApplies": starvation_metric_applies(
            config.get("recallStrategy", "hierarchical")),
        "limits": {
            "recallLimit": args.recall_limit,
            "recallLimitIsRecallDefault": args.recall_limit == qmd_recall.DAEMON_QUERY_LIMIT,
            "deepLimit": args.deep_limit,
            "queryTimeout": args.timeout,
            "interval": args.interval,
            "budget": args.budget,
        },
        "limitations": LIMITATIONS,
    }
    if not collections:
        record["status"] = "no_collections"
        return record
    if not wiki_collections:
        record["status"] = "no_wiki_collections"
        return record

    import qmd_endpoint
    daemon_url = qmd_endpoint.optional_url()
    record["daemon"] = daemon_url
    qmd_bin = None if args.no_index_composition else resolve_qmd_bin()
    record["index"] = (
        {"status": "skipped"} if args.no_index_composition
        else index_composition(qmd_bin, roles, wiki_names)
    )
    known_roles = dict(record["index"].get("roleByCollection", {})) if isinstance(
        record["index"].get("roleByCollection"), dict) else {}
    for name in roles:
        known_roles[name] = qmd_config.collection_role(roles, name)

    if args.probe:
        probes = [{"query": q, "card": None, "source": "explicit", "kind": "explicit"}
                  for q in args.probe]
    else:
        wiki_root = (Path(project_root) / config.get("wikiPath", ".auto-context/wiki"))
        titled = collect_card_titles(wiki_root, config.get("skipPaths", []) or [])
        record["wikiCardsWithTitle"] = len(titled)
        probes = (vocab_probes([t for t, _ in titled], args.probes)
                  + title_probes(titled, args.title_probes))
    record["probes"] = probes
    if not probes:
        record["status"] = "no_probes"
        return record

    if not qmd_recall.daemon_alive(daemon_url):
        record["status"] = "daemon_unreachable"
        return record

    budget = Budget(args.interval, args.budget)
    if args.ceiling:
        record["ceiling"] = ceiling_ladder(
            daemon_url, probes[0]["query"], args.timeout, budget)

    measured = []
    for probe in probes:
        entry: dict = {
            "query": probe["query"],
            "lexQuery": probe.get("lexQuery") or probe["query"],
            "card": probe.get("card"),
            "kind": probe.get("kind", "explicit"),
        }
        for path_type in ("vec", "lex"):
            entry[path_type] = measure_path(
                daemon_url, path_type,
                entry["lexQuery"] if path_type == "lex" else entry["query"],
                wiki_collections, known_roles, wiki_names,
                args.recall_limit, args.deep_limit, args.timeout, budget)
        measured.append(entry)
    record["measurements"] = measured
    # 판정은 **넓은 프로브**만 쓴다. 좁은 프로브는 창을 채우지 못해 구조적으로
    # undetermined이고, 판정 집계에 섞으면 넓은 프로브의 신호를 희석한다.
    verdict_kinds = {"broad", "explicit"}
    verdict_set = [m for m in measured if m["kind"] in verdict_kinds] or measured
    record["summary"] = {
        path_type: summarize_path(
            [m[path_type] for m in verdict_set], args.deep_limit)
        for path_type in ("vec", "lex")
    }
    record["summary"]["basedOnProbeKinds"] = sorted({m["kind"] for m in verdict_set})
    record["narrowProbeSummary"] = {
        path_type: summarize_path(
            [m[path_type] for m in measured if m["kind"] == "narrow"], args.deep_limit)
        for path_type in ("vec", "lex")
    }
    # status가 질의 성공률을 반영해야 한다. append-only 전/후 원장에서 열화된 "after"가
    # 온전한 "before"와 조용히 비교되면 안 되고, `comparable: false`가 그 배제 스위치다.
    record["queries"] = budget.queries
    record["queriesOk"] = budget.ok
    record["queryFailures"] = budget.failures
    record["queryBudgetSkips"] = budget.budget_skips
    record["status"] = budget.status()
    record["comparable"] = record["status"] == "ok"
    return record


def default_out_path(project_root: str) -> Path:
    """기본 저장 위치는 **프로젝트 밖**이다.

    라이브 프로젝트에 진단 산출물을 쓰면 측정이 대상을 변경한다(카드·설정 불변이
    baseline 수집의 전제다). 프로젝트별 파일명은 root 경로 해시로 안정화해 8단계가
    전/후를 같은 파일에서 찾는다.
    """
    digest = hashlib.sha256(project_root.encode("utf-8")).hexdigest()[:16]
    return Path.home() / ".cache" / "qmd" / "crowding" / f"{digest}.jsonl"


def append_record(path: Path, record: dict) -> bool:
    """append-only 원장에 한 줄 추가.

    `write_text_atomic`이 아니라 append 모드다 — 전/후 비교는 **누적 이력**이 자산이고,
    `open("a")`는 truncate하지 않는다(`wiki_compile.append_log`와 같은 근거).
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="crowding_probe",
        description="raw 인덱스의 전역 창 점유(crowding)를 프로젝트 단위로 반복 측정한다.",
    )
    parser.add_argument("project", nargs="?", default=".", help="프로젝트 경로 (기본 cwd)")
    parser.add_argument("--probes", type=int, default=DEFAULT_PROBES,
                        help=f"wiki 상위 빈도 어휘로 만드는 넓은 프로브 수 — 판정용 "
                             f"(기본 {DEFAULT_PROBES})")
    parser.add_argument("--title-probes", type=int, default=DEFAULT_TITLE_PROBES,
                        help=f"카드 title에서 표집하는 좁은 대조 프로브 수 "
                             f"(기본 {DEFAULT_TITLE_PROBES})")
    parser.add_argument("--probe", action="append", default=[],
                        help="프로브 질의를 축자 지정(반복 가능). 지정 시 표집을 대체한다")
    parser.add_argument("--recall-limit", type=int, default=qmd_recall.DAEMON_QUERY_LIMIT,
                        help=f"recall이 실제로 쓰는 limit — 피해 측정의 기준 "
                             f"(기본 {qmd_recall.DAEMON_QUERY_LIMIT}, core/recall.py와 공유)")
    parser.add_argument("--deep-limit", type=int, default=DEFAULT_DEEP_LIMIT,
                        help=f"엔진 내부 창을 보기 위한 큰 limit (기본 {DEFAULT_DEEP_LIMIT})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_QUERY_TIMEOUT)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help="질의 간 최소 간격(초). 데몬은 single-thread다")
    parser.add_argument("--budget", type=float, default=DEFAULT_BUDGET,
                        help="전체 wall-clock 예산(초)")
    parser.add_argument("--ceiling", action="store_true",
                        help="창 천장 limit 사다리를 추가 실행(질의 4회 증가). 포화 판정은 "
                             "프로브별 rung이 이미 하므로 기본 비활성이다")
    parser.add_argument("--no-index-composition", action="store_true",
                        help="qmd CLI로 인덱스 구성을 세지 않는다")
    parser.add_argument("--label", default="", help="레코드 라벨(before/after 등)")
    parser.add_argument("--out", default="", help="원장 경로 (기본 ~/.cache/qmd/crowding/)")
    parser.add_argument("--stdout", action="store_true",
                        help="원장에 쓰지 않고 레코드를 stdout으로만 출력")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    if (args.recall_limit <= 0 or args.deep_limit < args.recall_limit
            or args.probes < 0 or args.title_probes < 0):
        sys.stderr.write(
            "crowding_probe: --recall-limit는 양수, --deep-limit는 그 이상, "
            "--probes/--title-probes는 0 이상이어야 한다\n")
        return 1
    project_dir = str(Path(args.project).expanduser())
    # build_record 안의 어떤 예외로도 **레코드가 유실되면 안 된다** — append는 여기서
    # 1회뿐이라, 예외가 밖으로 나가면 그 실행의 모든 질의 결과가 사라진다.
    try:
        record = build_record(project_dir, args)
    except Exception as exc:  # noqa: BLE001
        record = {
            "ts": now_iso(), "schema": SCHEMA, "label": args.label or "",
            "project": {"root": project_dir},
            "status": "error", "comparable": False,
            "error": type(exc).__name__, "errorDetail": str(exc)[:500],
        }
    text = json.dumps(record, ensure_ascii=False)
    # 도구 자체의 실패(`error`)는 exit code로도 알린다. 열화된 측정(`degraded` 등)은
    # 정상 종료다 — 레코드가 `comparable: false`로 스스로 말한다.
    failed = record.get("status") == "error"
    if args.stdout:
        sys.stdout.write(text + "\n")
        return 1 if failed else 0
    out = Path(args.out).expanduser() if args.out else default_out_path(
        record.get("project", {}).get("root") or project_dir)
    if not append_record(out, record):
        sys.stderr.write(f"crowding_probe: 원장 쓰기 실패 {out}\n")
        sys.stdout.write(text + "\n")
        return 1
    sys.stderr.write(
        f"crowding_probe: {record.get('status')} → {out} "
        f"(queries={record.get('queries', 0)}, ok={record.get('queriesOk', 0)})\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

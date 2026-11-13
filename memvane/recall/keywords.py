#!/usr/bin/env python3
from __future__ import annotations
import sys
import re
import json
import argparse

KO_STOPWORDS = {
    "해줘", "해주세요", "해봐", "알려줘", "보여줘", "찾아줘",
    "있나", "있어", "뭐야", "어때", "인가", "인지",
    "그거", "이거", "저거", "여기", "거기",
    "좀", "것", "수", "등", "및", "또는", "그리고",
    "어떻게", "어디서", "언제", "왜", "뭘", "무엇을",
    "하나요", "할까요", "하지", "할까", "하면", "하려면",
    "나는데", "있는데", "되는데", "하는데", "인데", "건데",
    "그런데", "그래서", "그러면", "그러니", "그러나",
    # 연결어미 계열. `관련해서`는 이미 있던 `관해서`·`대해서`와 같은 부류의 누락이었다
    # (활용형 추가가 아니라 **기존 범주의 완성**이므로 어간 판정 문제와 무관하다).
    # 라이브 근거: `"이 부분 관련해서 sendbird 장애 원인"`에서 이 토큰이 AND 에 남아
    # 질의를 0건으로 만들었고, DF 필터는 이 토큰을 못 뺀다 — 코퍼스 871장 중 1장에
    # 실제로 등장하기 때문이다(DF 0 이 아니다). 빼면 같은 프롬프트가 3건을 낸다.
    "때문에", "대해서", "관해서", "관련해서", "대한", "위해", "통해", "따라",
    # 단독 토큰으로 떨어진 조사. `strip_ko_suffix`가 접미로는 이미 떼는 형태들인데
    # 사용자가 띄어 쓰면("EP12 에서 …") 토큰 하나로 남아 term 예산(5개)을 먹고
    # AND 조건만 좁힌다. 접미로 떼는 것과 단독일 때 버리는 것은 같은 정책이다.
    # 활용형(먹은/보는 …)은 여기 넣지 않는다 — 어간 판정 없이 넣으면 의미어를 잃는다.
    "에서", "으로", "에게", "처럼", "부터", "까지", "에는",
}

EN_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were",
    "how", "what", "where", "when", "why", "which",
    "do", "does", "did", "can", "could", "should",
    "please", "help", "me", "about", "this", "that",
    "it", "in", "on", "to", "for", "with", "from",
    "has", "have", "been", "be", "will", "would", "not", "no",
}

# --- 식별자(정확 토큰) 추출 ------------------------------------------------
#
# qmd(>=2.5.3) lex 쿼리 문법 제약이 이 모듈의 설계를 지배한다.
# `dist/store.js` buildFTS5Query / sanitizeFTS5Term / sanitizeFTS5Phrase 확인 결과:
#
#   1. plain term  → `[^\p{L}\p{N}'_]` 문자를 **삭제**한 뒤 `"sanitized"*` (prefix).
#      즉 `docs/settings.md` 를 그대로 term 으로 보내면 `"docssettingsmd"*` 가 되어
#      색인에 없는 토큰이 된다.
#   2. positive term 들은 **AND** 로 결합된다. term 하나만 빗나가도 lex 전체가 0건이다.
#      → term 을 늘리는 것 자체가 위험하므로 예산을 작게 유지한다.
#   3. quoted phrase → 공백으로 쪼갠 각 조각의 **인접(exact adjacency)** 을 요구한다.
#      FTS5 tokenizer 는 `porter unicode61` 이고 `_` 는 토큰 문자, `.` `/` `-` 는 구분자다.
#
# 3번 때문에 **우리는 quoted phrase 를 절대 만들지 않는다.** 실측(service-engineering-wiki):
#   `"settings md"` → 0건 / `docs settings md 구조` → 2건 / 둘을 합치면 다시 0건.
# wiki 카드 본문에는 원본 파일 경로가 보존되지 않고(운영 프로젝트는 `wikiOnly`) phrase 가
# 매칭될 대상이 없는데, AND 결합이라 phrase 하나가 lex 전체를 0건으로 만든다.
# 그래서 `_to_plain_term()` 은 경로/dotted 토큰에서 **가장 정보량 많은 조각 하나만**
# plain prefix term 으로 승격한다 (`docs/settings.md` → `settings`).

# AND 결합이므로 term 이 많아질수록 조건이 좁아져 0건 확률이 올라간다.
# 일반 단어 5개 cap 과 별개 예산이고, 정확 토큰 신호에만 소량 배정한다.
IDENTIFIER_BUDGET = 4

# 아래 정규식은 전부 bounded 하고 선두에 무제한 반복이 없다. UserPromptSubmit 은
# blocking hook 이고 queryTimeout 기본값이 3초이므로, 긴 로그 붙여넣기에서도
# 제곱 시간 퇴화가 없어야 한다(try/except 도 hook_main 도 느린 정규식은 못 막는다).
_CODE_SPAN_RE = re.compile(r"`([^`\n]{1,200})`")
# 후보 토큰만 한 번에 긁는다. 뒤따르는 필수 구분자가 없으므로 재탐색이 발생하지 않는다.
_CANDIDATE_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./\-]{0,79}")
_CALL_SUFFIX_RE = re.compile(r"\(\s{0,4}\)")
# lowerUpper 전이 또는 두 글자 이상 대문자 런 뒤 소문자(HTTPServer, XMLParser).
_CASE_MIX_RE = re.compile(r"[a-z][A-Z]|[A-Z]{2,}[a-z]")

# bounded work: 아주 긴 붙여넣기에서도 후보 검사 횟수를 상한한다(앞쪽 우선).
# 코드스팬 후보는 이 상한과 무관하게 먼저 수집된다(백틱 우선 계약).
_MAX_CANDIDATES = 400
_MAX_CODE_SPANS = 64

# 우선순위(작을수록 강함). 백틱 코드스팬은 사용자가 명시적으로 표시한 가장 강한 신호.
_RANK_CODE_SPAN = 0
_RANK_CALL = 1
_RANK_PATH = 2
_RANK_DOTTED = 3
_RANK_SNAKE = 4
_RANK_CASE_MIX = 5
_RANK_KEBAB = 6
_RANK_ALNUM = 7


def _to_plain_term(token: str) -> str:
    """토큰을 plain prefix term 하나로 줄인다 (quoted phrase 는 만들지 않는다).

    - 경로(`docs/settings.md`) → basename(`settings.md`)
    - dotted(`settings.md`, `config.load_project_config`) → 가장 긴 조각
      (`settings`, `load_project_config`). 파일명이면 확장자를 뗀 stem 과 같고,
      속성 경로면 가장 구체적인 이름이 남는다.
    - snake/camel/kebab 은 원형 유지 — `_` 는 FTS5 토큰 문자이고 하이픈은 qmd 의
      ``isHyphenatedToken`` 이 알아서 처리하므로 우리가 손댈 이유가 없다.
    """
    if "/" in token:
        token = token.rsplit("/", 1)[-1].strip("./-")
    if "." in token:
        parts = [p for p in token.split(".") if p]
        if not parts:
            return ""
        token = max(parts, key=len)
    return token


def _is_identifier_like(token: str) -> bool:
    """일반 단어와 구별되는 구조적 신호가 있는지."""
    if "_" in token or "-" in token or "." in token or "/" in token:
        return True
    if _CASE_MIX_RE.search(token):
        return True
    has_digit = any(ch.isdigit() for ch in token)
    has_alpha = any(ch.isalpha() for ch in token)
    return has_digit and has_alpha


def _rank_of(token: str) -> int:
    if "/" in token:
        return _RANK_PATH
    if "." in token:
        return _RANK_DOTTED
    if "_" in token:
        return _RANK_SNAKE
    if _CASE_MIX_RE.search(token):
        return _RANK_CASE_MIX
    if "-" in token:
        return _RANK_KEBAB
    return _RANK_ALNUM


def _prepare(token: str, in_code_span: bool, is_call: bool) -> tuple[int, str] | None:
    """후보 토큰 → (우선순위, lex term). 부적격이면 None."""
    token = token.rstrip("./-")
    if len(token) < 2 or len(token) > 80:
        return None
    if token.lower() in EN_STOPWORDS or token in KO_STOPWORDS:
        return None

    # 숫자 승격 금지: 날짜(2026/07/29)·IP(192.168.0.1)·순수 숫자가 식별자로 들어가면
    # 대상 문서에 그 숫자가 없을 때 AND 결합 때문에 lex 가 통째로 죽는다. 백틱으로
    # 감싼 순수 숫자(`127`)만 명시적 의도로 보고 허용한다 — dotted 숫자(`0.8`)는
    # sanitize 후 색인에 없는 토큰(`08`)이 되므로 코드스팬이어도 받지 않는다.
    if not any(ch.isalpha() for ch in token):
        if in_code_span and token.isdigit() and len(token) >= 2:
            return (_RANK_CODE_SPAN, token)
        return None

    rank = _rank_of(token)
    if rank == _RANK_PATH:
        # 경로는 basename 만 본다. 디렉터리 조각은 extract_keywords 가 prefix term 으로
        # 이미 커버하고, 경로 전체를 term 으로 보내면 sanitize 로 붙어버려
        # (`docssettingsmd`) 색인에 없는 토큰이 된다.
        base = token.rsplit("/", 1)[-1].strip("./-")
        if len(base) < 2 or not _is_identifier_like(base):
            return None
    elif not in_code_span and not is_call and not _is_identifier_like(token):
        return None

    if in_code_span:
        rank = _RANK_CODE_SPAN
    elif is_call:
        rank = _RANK_CALL

    term = _to_plain_term(token)
    if len(term) < 2 or term.lower() in EN_STOPWORDS:
        return None
    return (rank, term)


def extract_identifiers(text: str) -> list[str]:
    """프롬프트의 정확 토큰(식별자)을 **qmd lex 용어**로 변환해 우선순위 순으로 돌려준다.

    대상: 백틱 코드스팬 내용, 호출형(`f()`), 경로 basename, dotted/snake/kebab/
    CamelCase, 숫자 포함 토큰. 어간 절단은 하지 않는다(원형 보존).

    반환값은 전부 **plain prefix term** 이다(quoted phrase 없음 — 모듈 상단 주석 참고).
    최대 ``IDENTIFIER_BUDGET`` 개이며 어떤 입력에도 예외를 던지지 않는다.
    """
    if not isinstance(text, str) or not text:
        return []

    try:
        candidates: list[tuple[int, int, str]] = []

        # 1) 코드스팬 우선 패스. 백틱은 사용자가 명시한 가장 강한 신호이므로 일반 후보
        #    상한(_MAX_CANDIDATES)에 밀려 버려지면 안 된다 — 그래서 별도 패스로 먼저 모은다.
        code_spans: list[tuple[int, int]] = []
        for span_index, span in enumerate(_CODE_SPAN_RE.finditer(text)):
            if span_index >= _MAX_CODE_SPANS:
                break
            span_start = span.start(1)
            code_spans.append((span_start, span.end(1)))
            for match in _CANDIDATE_RE.finditer(span.group(1)):
                prepared = _prepare(
                    match.group(0),
                    True,
                    bool(_CALL_SUFFIX_RE.match(text, span_start + match.end())),
                )
                if prepared is not None:
                    candidates.append((prepared[0], span_start + match.start(), prepared[1]))

        def in_span(start: int, end: int) -> bool:
            for s, e in code_spans:
                if start >= s and end <= e:
                    return True
            return False

        # 2) 일반 후보 패스. bounded work 상한은 코드스팬 밖 후보에만 적용한다.
        examined = 0
        for match in _CANDIDATE_RE.finditer(text):
            start, end = match.start(), match.end()
            if in_span(start, end):
                continue
            if examined >= _MAX_CANDIDATES:
                break
            examined += 1
            is_call = bool(_CALL_SUFFIX_RE.match(text, end))
            prepared = _prepare(match.group(0), False, is_call)
            if prepared is not None:
                candidates.append((prepared[0], start, prepared[1]))

        # rank 우선, 같은 rank 내에서는 등장 순서(안정 정렬).
        candidates.sort(key=lambda c: (c[0], c[1]))

        terms: list[str] = []
        seen: set[str] = set()
        for _rank, _start, term in candidates:
            if len(terms) >= IDENTIFIER_BUDGET:
                break
            # 중복 제거는 동일 term(equality)으로만 한정한다. 부분 문자열 기준으로
            # 버리면 `notebook` 채택 후 `book()` 처럼 별개 이름이 탈락한다.
            key = term.lower()
            if key in seen:
                continue
            seen.add(key)
            terms.append(term)
        return terms
    except Exception:
        # 순수 함수지만 hook import 경로이므로 어떤 실패에도 무해하게 degrade한다.
        return []


def strip_ko_suffix(token: str) -> str:
    for suffix in (
        "해주세요", "해줘", "해봐",
        "하려면", "하나요", "할까요", "하지", "할까", "하면",
        "는데", "인데",
        "에서", "으로", "에게", "처럼", "부터", "까지", "에는",
        "을", "를", "으", "로", "와", "과", "의", "은", "는", "이", "가",
    ):
        if token.endswith(suffix) and len(token) > len(suffix) + 1:
            return token[: -len(suffix)]
    return token

def extract_keywords(text: str) -> list[str]:
    # Remove markdown titles and links
    text = re.sub(r"^#{1,6}\s+", " ", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[`*_~>|]", " ", text)

    tokens = re.findall(r"[a-zA-Z0-9가-힣_-]{2,}", text)
    keywords: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        if token.lower() in EN_STOPWORDS or token in KO_STOPWORDS:
            continue
        stemmed = strip_ko_suffix(token)
        key = stemmed.lower()
        if len(stemmed) < 2 or key in seen:
            continue
        seen.add(key)
        keywords.append(stemmed)
        if len(keywords) >= 5: # Limit increased to 5 for general configuration
            break
    return keywords


def _keyword_question_span(prompt: str) -> tuple[str, str]:
    """Keep a host instruction ahead of a single explicit question out of lex.

    This only handles the bounded, unambiguous multi-line form. Other prompts
    keep their existing token order; in particular, a pasted document or two
    questions must not silently turn into a different search request.
    """
    lines = [line.strip() for line in prompt.splitlines() if line.strip()]
    if not 2 <= len(lines) <= 8:
        return prompt, "full"
    lead = lines[0].lower()
    if "?" in lead or "？" in lead:
        return prompt, "full"
    ko_instruction = ("도구" in lead and "컨텍스트" in lead
                      and ("말고" in lead or "없이" in lead))
    en_instruction = (re.search(r"\btools?\b", lead) is not None
                      and re.search(r"(?<!-)\bcontext\b", lead) is not None
                      and re.search(r"\b(?:only|without|no)\b", lead) is not None)
    if not (ko_instruction or en_instruction):
        return prompt, "full"
    questions = [line for line in lines[1:] if line.endswith(("?", "？"))]
    if len(questions) != 1:
        return prompt, "full"
    question = questions[0]
    if len(extract_keywords(question)) < 2:
        return prompt, "full"
    return question, "question"

# EP 변형을 **독립 lex search** 로 내보낼 개수 상한.
#
# qmd 는 하나의 lex 문자열 안의 positive term 을 AND 로 결합하므로 EP 변형
# (`EP012`/`EP12`)을 한 문자열에 합치면 한 문서가 두 표기를 모두 가져야 하고,
# 현실적으로 불가능해 EP 쿼리의 lex 는 항상 0건이 된다. `searches` 배열에 lex 를
# 여러 개 넣으면 각각 독립 FTS 로 실행되고 RRF 로 융합되므로 AND 대신 OR 효과가 난다
# (`dist/store.js` structuredSearch step 1 — lex search 마다 rankedList 를 하나 push).
#
# 상한을 두는 이유: qmd MCP `searches` 스키마가 `.max(10)` 이고(HTTP 경로는 검증하지
# 않지만 같은 structuredSearch 로 들어간다), 우리는 일반 lex 1 + vec 1 을 항상 쓴다.
# 데몬은 single-thread 이므로 searchFTS 호출 수 자체를 상한하는 것이 지연 방어다.
# 6 = EP 번호 3개 × 변형 2종 → payload 총 8개로 스키마 상한 안에 머문다.
EP_SEARCH_BUDGET = 6

# 일반 lex 문자열(`lexQueries[0]`)에 넣을 AND term 수 상한.
#
# **이 상한은 이제 1차 선택 기준이 아니다** — 데몬이 있는 경로에서는 `select_general_terms`
# 가 먼저 "코퍼스에 존재하는 term"만 남기고 그 뒤에 이 상한이 걸린다. 아래 표는 상한
# 도입 당시의 근거(위치 컷 단독)이며, 위치 컷이 무엇을 고치고 무엇을 망쳤는지는
# `select_general_terms` docstring 이 SSOT 다.
#
# **AND 를 OR 로 바꾸는 것이 아니다.** 남는 term 은 여전히 AND 로 결합되고, 검색을
# 좁히는 그 동작은 의도된 것이다(CLAUDE.md "일반 키워드의 AND 결합은 유지한다").
# 이 상한이 막는 것은 하나뿐이다: **어미 한 토큰이 질의 전체를 0건으로 만드는 것.**
#
# `KO_STOPWORDS` 는 조사는 잡지만 활용형 어미는 못 잡는다(`뭐였지`·`발생해`). 어간
# 판정 없이 어미를 목록으로 넣으면 의미어를 잃으므로(그래서 활용형은 의도적으로 제외돼
# 있다) 목록을 늘리는 방향은 형태소 분석기 없이는 끝나지 않는다. term 수를 자르면
# 어미가 **구조적으로** 컷 뒤로 밀린다 — 어미는 문장 끝에 오고 키워드 추출은 등장
# 순서를 보존하기 때문이다.
#
# 라이브 service-engineering-wiki 색인(limit 8) 실측:
#
#   | lexQueries[0]                  | 앞 2개 | 앞 3개 | 전체(상한 전) |
#   |--------------------------------|-------:|-------:|--------------:|
#   | sendbird 장애 원인 뭐였지      |      8 |  **8** |         **0** |
#   | VN 콜백 이벤트 발생해          |      8 |  **7** |         **0** |
#   | 중복 판정                      |      8 |  **8** |             8 |
#   | 오늘 점심 먹을까 고민이네      |      0 |  **0** |             0 |
#   | python list comprehension 문법 |      0 |  **0** |             0 |
#   | useEffect 리액트 의존성 배열 규칙 |   0 |  **0** |             0 |
#
# 관련 3/3 통과 · 무관 3/3 차단. 2가 아니라 3인 이유: 결과가 같으면서 "AND 로 좁힌다"는
# 기존 의도에 더 가깝다. term 이 3개 이하면 이 상한은 아무 일도 하지 않는다.
#
# 이 값은 **EP 변형(`lexQueries[1:]`)에 적용되지 않는다** — EP 는 독립 lex search 로
# 나가 AND 가 아니라 RRF 융합(OR 효과)이고, 그쪽 상한은 `EP_SEARCH_BUDGET` 이다.
GENERAL_LEX_TERM_CAP = 3


# DF 필터 결과를 채택하기 위한 최소 term 수.
#
# 1개짜리 질의는 **연접이 아니다** — "이 토큰을 가진 아무 문서"이고, 871장 한국어
# 코퍼스에서는 거의 항상 만족된다. lex 게이트의 변별력은 연접에서 나오므로, DF 필터가
# 연접을 한 토큰으로 줄여 버리면 게이트가 판정할 근거 자체가 사라진다.
#
# 라이브 실측이 정확히 그 형태를 냈다: `"오늘 점심 뭐 먹을까 고민이네"` → 점심·먹을까·
# 고민이네가 DF 0 이라 `오늘`(871장 중 1장에 등장) 하나만 남고, 그 1건 히트로 **무관
# 프롬프트에 게이트가 열렸다**(주입 727자 부활 = 이번 커밋이 고친 바로 그 회귀).
# 그래서 남은 term 이 2개 미만이면 좁히기를 채택하지 않고 원래(=불만족) 질의를 그대로
# 쓴다. 필터가 잔여물에서 히트를 **만들어내지** 않게 하는 규칙이고, 그때의 동작은
# 오늘과 완전히 같다(개선이 없을 뿐 악화도 없다).
MIN_PRESENT_TERMS = 2

# select_general_terms_explained 의 mode 값.
LEX_TERMS_POSITIONAL = "positional"        # DF 를 모른다 → 위치 컷(기존 동작)
LEX_TERMS_PRESENT = "present"              # DF 필터 채택
LEX_TERMS_LONE_SURVIVOR = "lone_survivor"  # 남은 term < 2 → 위치 컷 유지


def select_general_terms(terms: list[str], present: set[str] | None = None) -> list[str]:
    """`select_general_terms_explained` 의 term 목록만."""
    return select_general_terms_explained(terms, present)[0]


def select_general_terms_explained(
    terms: list[str], present: set[str] | None = None
) -> tuple[list[str], str]:
    """일반 lex 문자열에 넣을 AND term 을 고른다 — **선택 규칙의 SSOT**.

    반환은 ``(terms, mode)`` 이고 mode 는 진단 로그가 그대로 쓴다(조용한 동작 변경 금지).

    호출자는 둘이고 차이는 ``present`` 하나다:

    - ``build_lexical_terms``(데몬을 못 쓰는 경로: CLI·fixture·recall 폴백)
      → ``present=None`` → 예전 그대로의 **위치 컷**.
    - ``recall.narrow_general_lex``(데몬이 있는 경로) → ``present`` = 코퍼스에 실제로
      존재하는(lex 히트 ≥1, 즉 document frequency > 0) term 집합(소문자).

    이 모듈은 순수 함수로 남는다 — DF 는 **데이터로 주입**받고 여기서 조회하지 않는다
    (keywords.py 는 데몬 없는 hook 경로에서도 import 된다).

    **왜 위치가 아니라 존재로 고르는가.** 위치 컷은 문장 *앞*의 군더더기가 실제 내용어를
    밀어낸다. 라이브 실측(service-engineering wiki 871장, 2026-08-05):

        | 프롬프트                              | 위치 컷 질의 → 히트      | 이 규칙 → 히트         |
        |---------------------------------------|--------------------------|------------------------|
        | 이 부분 관련해서 sendbird 장애 원인…  | `부분 관련해서 sendbird` 0 | `부분 sendbird 장애` 3 |
        | 아까 말한 그거 있잖아 VN 콜백 이벤트  | `아까 말한 있잖아`      0 | `VN 콜백`            8 |
        | sendbird 장애 원인이 뭐였지?          | `sendbird 장애 원인`    8 | 동일                 8 |
        | VN 콜백 이벤트 언제 발생해?           | `VN 콜백 이벤트`        7 | 동일                 7 |
        | 중복 판정은 어떻게 하나요?            | `중복 판정`             8 | 동일                 8 |
        | python list comprehension 문법 알려줘 | 0                         | 0 (lone survivor 폴백) |
        | 리액트 useEffect 의존성 배열 규칙     | 0                         | `의존성 배열 규칙`   0 |
        | 오늘 점심 뭐 먹을까 고민이네          | 0                         | 0 (lone survivor 폴백) |
        | 이 함수 이름을 뭐로 지을까 고민       | 0                         | `함수 이름 고민`     0 |

    무관 4/4 차단 유지 · 직접 질의 3/3 무변화. 1행의 회복은 DF 필터가 아니라
    ``KO_STOPWORDS`` 에 `관련해서` 를 더한 몫이다(그 토큰은 DF 0 이 아니다 — 주석 참고).

    **변별력이 죽지 않는 이유**: 개별 토큰은 DF>0 이어도(`list`=20, `의존성`=10,
    `규칙`=20) 그 AND 결합은 여전히 0건이다. 걸러지는 것은 코퍼스에 **아예 없는**
    토큰뿐이고, 그 토큰이 있는 한 연접은 **만족 불가능**하다(FTS 가 그 자리에서 0건을
    보장한다). 즉 이 필터는 "질의를 넓히는" 것이 아니라 **정보량이 0으로 확정된 질의를
    복구**하는 것이다. 실측상 그 집합은 활용형 어미(`뭐였지`·`발생해`·`먹을까`)와 구어체
    군더더기(`아까`·`말한`·`있잖아`)와 코퍼스 밖 주제어(`useEffect`·`리액트`·`점심`)다 —
    위치 컷이 우회로 노렸던 대상을 이쪽이 원인 기준으로 덮는다.

    **상한을 폐기하지 않고 DF 필터 뒤의 backstop 으로 남기는 이유 둘.**
    (1) DF>0 토큰이 많은 긴 프롬프트는 여전히 AND 를 과하게 좁힌다 — 토큰이 개별로
    존재해도 한 문서가 전부 갖고 있을 확률은 term 수에 따라 급감하고, DF 필터는 그
    조합 효과에 대해 아무 말도 하지 않는다. (2) DF 조회가 실패하면 ``present=None``
    으로 폴백하는데, 그때 상한이 없으면 폴백 경로가 상한 도입 전 상태로 — 어미 한
    토큰이 질의를 통째로 0건으로 만드는 상태로 — 되돌아간다. 상한은 그 폴백의 안전망이다.
    """
    if present is None:
        return (terms[:GENERAL_LEX_TERM_CAP], LEX_TERMS_POSITIONAL)
    kept = [t for t in terms if t.lower() in present]
    if len(kept) < MIN_PRESENT_TERMS:
        # 연접이 남지 않았다 → 좁히기를 채택하지 않는다(근거는 MIN_PRESENT_TERMS).
        return (terms[:GENERAL_LEX_TERM_CAP], LEX_TERMS_LONE_SURVIVOR)
    return (kept[:GENERAL_LEX_TERM_CAP], LEX_TERMS_PRESENT)


_EP_MENTION_RE = re.compile(
    r"\bEP\s*0*(\d{1,3})\b|\b0*(\d{1,3})\s*화", re.IGNORECASE
)


def extract_ep_terms(prompt: str) -> list[str]:
    """EP 번호를 색인에 존재하는 표기로 정규화한다 (`EP12` → `EP012`, `EP12`).

    **순수 숫자 단독 변형(`012`)은 만들지 않는다.** AND 결합과는 **별개 이유**다 —
    독립 lex search 로 분리한 뒤에도 남는 문제이고, 원인은 RRF 노이즈다.
    `012`* 는 EP 표기가 아니라 "012 로 시작하는 아무 토큰"에 걸리고, 특히 wiki 카드
    frontmatter 의 `sources` 경로에 스치기만 해도 히트한다. novel 실측
    (`yakbbal-wiki`, "EP12 에서 서미래가 먹은 약과 부작용", lex 단독):

        012 제외 → 3건 (전부 관련: 서미래 / 밀어내는-것과… / 동시-활성-처방…)
        012 포함 → 8건 (위 3건 + 무관 5건: 곽-소재-브로커, 좁은-공간… ×2,
                        소재-매입자-곽, 신호는-자리에서…)

    재현율은 그대로이고 노이즈만 5건 늘어 RRF 순위를 차지한다. 융합 결과에서도
    상위 3위는 동일하고 4위만 무관 카드로 바뀌었다. `minScore` 를 낮추거나 `topN`
    을 늘린 프로젝트, 그리고 순위 폴백이 2·3위로 내려가는 경로에서 직접 해를 준다.

    사용자가 **직접 쓴** 숫자(`EP 012 확인` 의 `012`)도 ``_ep_mention_fragments`` 가
    일반 AND 문자열에서 빼내므로 같은 정책이 일관되게 적용된다 — 합성이든 원문이든
    "EP 언급에서 나온 순수 숫자"는 lex 에 실리지 않는다. EP 번호 신호는 `EP012`·`EP12`
    독립 search 와 vec, 그리고 `promote_ep_exact_matches`(파일명 정확 매칭)가 담당한다.
    """
    terms: list[str] = []
    for match in _EP_MENTION_RE.finditer(prompt):
        ep_num = match.group(1) or match.group(2)
        if ep_num:
            normalized = int(ep_num)
            terms.extend([f"EP{normalized:03d}", f"EP{normalized}"])
    return list(dict.fromkeys(terms))


def _ep_mention_fragments(prompt: str) -> set[str]:
    """EP 언급 **원문 span** 이 만들어낸 토큰 조각들 (소문자).

    `EP 12` 처럼 띄어 쓰면 `extract_keywords` 가 `EP` 와 `12` 를 별개 토큰으로 낸다.
    이 조각들은 EP 변형 독립 search 가 이미 덮으므로 일반 lex 문자열에 남겨 두면
    AND 조건만 좁힌다 — 같은 span 에서 나온 것이 정규식으로 증명되는 조각만
    제외하므로 무관한 숫자·단어에는 영향이 없다.
    """
    fragments: set[str] = set()
    for match in _EP_MENTION_RE.finditer(prompt):
        for piece in _EP_SPLIT_RE.split(match.group(0)):
            if len(piece) >= 2:
                fragments.add(piece.lower())
    return fragments


_EP_PLAIN_RE = re.compile(r"^EP\d+$", re.IGNORECASE)
_EP_SPLIT_RE = re.compile(r"[^0-9A-Za-z]+")


def _is_ep_term(term: str) -> bool:
    """ep 패턴이 꺼져 있을 때 걸러낼 EP 용어인지.

    plain term(`EP12`)뿐 아니라 식별자 경로가 만들 수 있는 변형까지 본다:
    `ep_12`, `EP-12`, 그리고 확장자가 붙어 뒤에 조각이 더 따라오는 형태
    (`EP12.md` → `EP12 md`). **선두 조각**만 보고 판정하므로 `^EP\\d+$` 만
    검사할 때 새던 확장자 변형이 함께 막힌다.
    """
    components = [c for c in _EP_SPLIT_RE.split(term.strip('"')) if c]
    if not components:
        return False
    if _EP_PLAIN_RE.match(components[0]):
        return True
    return (
        len(components) >= 2
        and components[0].lower() == "ep"
        and components[1].isdigit()
    )


def build_lexical_terms(prompt: str, patterns: list[str]) -> dict:
    """lex 쿼리에 쓸 용어를 조립한다 — CLI와 recall.py가 공유하는 단일 정책.

    순서: ``extract_ep_terms``(``"ep" in patterns`` 일 때만) → 식별자 → 일반 키워드.
    식별자를 앞에 두는 이유는 정확 토큰이 가장 강한 신호이기 때문이고, ep 게이팅이
    꺼져 있으면 식별자·키워드 양쪽에서 EP 용어를 제거한다(호스트별 불일치 방지).

    ``lexQueries`` 가 실제로 데몬에 보낼 lex 문자열 목록이다:

    - ``[0]`` = 일반 lex 문자열(식별자 + 키워드). AND 결합은 검색을 좁히는 의도된
      동작이라 그대로 유지한다.
    - ``[1:]`` = EP 변형(`EP012`/`EP12`) **각각 하나씩**. 같은 문자열에 합치면 AND 가
      되어 항상 0건이므로 독립 search 로 분리한다(``EP_SEARCH_BUDGET`` 상한).
      순수 숫자 변형은 애초에 만들지 않는다 — 근거는 ``extract_ep_terms`` 참고.

    예산을 넘는 EP 변형은 일반 문자열로 되돌리지 않고 그냥 버린다 — 되돌리면 AND
    조건이 다시 좁아져 고치려던 버그가 재발한다.

    ``lexicalTerms`` 는 (EP 변형 포함) 전체 term 목록으로 기존 계약을 유지한다.
    """
    keyword_prompt, keyword_span = _keyword_question_span(prompt)
    keywords = extract_keywords(keyword_prompt)
    identifiers = extract_identifiers(keyword_prompt)

    ep_terms: list[str] = []
    lexical_terms: list[str] = []
    if "ep" in (patterns or []):
        ep_terms = extract_ep_terms(prompt)
        lexical_terms.extend(ep_terms)
        lexical_terms.extend(identifiers)
        lexical_terms.extend(keywords)
    else:
        lexical_terms.extend(t for t in identifiers if not _is_ep_term(t))
        lexical_terms.extend(t for t in keywords if not _is_ep_term(t))

    deduped: list[str] = []
    seen: set[str] = set()
    for term in lexical_terms:
        if term not in seen:
            seen.add(term)
            deduped.append(term)

    # 제외 집합 = EP 변형 전체 + EP 언급 원문 조각(`EP 12` → `EP`, `12`).
    # 조각까지 빼는 이유: 독립 search 가 이미 덮는데 일반 문자열에 남으면 AND 만 좁힌다.
    ep_set = {t.lower() for t in ep_terms}
    if ep_terms:
        ep_set |= _ep_mention_fragments(prompt)
    ep_searches = ep_terms[:EP_SEARCH_BUDGET]
    # 일반 문자열은 항상 lexQueries[0] 이다(빈 문자열이어도) — ep 가 꺼진 프로젝트의
    # payload 모양이 바뀌지 않게 하고, structuredSearch 의 "첫 리스트 2x 가중" 대상도
    # 기존과 같이 유지한다. 빈 lex 는 결과 0건이라 rankedList 로 push 되지도 않는다.
    # AND term 수는 GENERAL_LEX_TERM_CAP 까지만. AND 결합 자체는 유지하고(좁히는 것은
    # 의도된 동작) 문장 끝의 활용형 어미가 질의를 통째로 0건으로 만드는 것만 막는다 —
    # 근거·실측표는 그 상수 주석. `lexicalTerms`/`keywords` 계약은 건드리지 않는다
    # (상한은 lex **문자열**에만 걸린다. shadow 진단의 `lex_terms` 집계도 그대로다).
    general_terms = [t for t in deduped if t.lower() not in ep_set]
    # `present` 없이 부르므로 위치 컷이다 — 데몬을 못 쓰는 경로(CLI·fixture)와 DF 조회
    # 실패 시의 폴백이 **같은 함수**를 거치게 해서 선택 규칙이 두 벌이 되지 않게 한다.
    general_query = " ".join(select_general_terms(general_terms))

    return {
        "keywordSpan": keyword_span,
        "keywords": keywords,
        "identifiers": identifiers,
        "lexicalTerms": deduped,
        "epTerms": ep_searches,
        # 상한 **전**의 일반 term 목록. recall 이 DF 로 좁힐 때 입력으로 쓴다
        # (좁히기는 데몬이 필요하고 이 모듈은 순수 함수로 남아야 하므로 목록만 넘긴다).
        "generalTerms": general_terms,
        "lexQueries": [general_query] + ep_searches,
    }


def main():
    parser = argparse.ArgumentParser(description="Extract keywords and lexical terms.")
    parser.add_argument("--patterns", default="")
    args = parser.parse_args()

    patterns = [p.strip() for p in args.patterns.split(",") if p.strip()]

    prompt = sys.stdin.read().strip()

    # EP 게이팅·조립 순서·dedup 정책은 build_lexical_terms가 SSOT다 (recall.py와 공유).
    print(json.dumps(build_lexical_terms(prompt, patterns), ensure_ascii=False))

if __name__ == "__main__":
    main()

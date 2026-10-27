"""Wiki card reading helpers shared by the selected Laya route and wiki workers.

Frontmatter parsing, source resolution, body extraction and the recall
selection log live here. The module imports only the standard library and
other stdlib-only card modules (``wiki_markers``, ``wiki_freshness``,
``yaml_scalars``, ``resolve_paths``).
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re

import resolve_paths as qmd_resolve_paths
import wiki_freshness
import wiki_markers
import yaml_scalars

# A card may cite more than one compiler-owned source. Hashing runs in a
# blocking hook, so the per-card scan has an explicit bound independent of
# model-provided frontmatter size.
MAX_FRESHNESS_SOURCE_REVISIONS_PER_CARD = 10

# Static automatic trust vocabulary.  A status string alone is insufficient;
# ``is_auto_trusted_card`` also requires qmd ownership and source revisions.
TRUSTED_WIKI_STATUSES = {"verified"}

# 카드 본문 주입 상한 기본값(문자). docs/settings.md에 근거를 남긴다 — 요약:
# dogfooding 두 코퍼스(service-engineering 731장, novel 118장)의 주입 대상 본문 길이가
# median 206~261자, p95 483~595자, max 1574자다. 600이면 카드의 95%+가 무절단으로
# 들어가고(절단이 예외), topN 기본 3에서 최악 1800자로 유계다.
DEFAULT_INJECT_SUMMARY_MAX_CHARS = 600
# 카드 파일 읽기 상한(문자)의 하한. 849장 중 최대 파일이 2938자라 8192는 2.8배 여유다.
# read()에 그대로 넘겨 **실제 I/O를 유계로** 만든다(예전엔 파일 전체를 읽고 잘라서
# 164KB log.md를 전량 디코딩한 뒤 버렸다). 사용자가 injectSummaryMaxChars를 올리면
# 읽기 창도 같이 커져 상한이 예산을 조용히 잘라먹지 않는다.
WIKI_CARD_READ_BASE_CHARS = 8192
# 절단 표시. 모델이 "여기서 잘렸다 → 필요하면 카드를 열어라"를 알 수 있어야 한다.
SUMMARY_TRUNCATION_MARK = "… (이하 생략)"
# 주입 title 상한. frontmatter title은 자동 생성이라 길이 보장이 없다.
MAX_TITLE_CHARS = 160
# 카드당 주입할 원문 경로(`sources[].path`) 개수 상한 기본값. 0이면 원문 경로를 끈다.
# 근거(dogfooding 849장 전수): sources path가 1개인 카드 830장(97.8%), 2개 12장, 3개 6장,
# 4개 1장 — median 1 / p95 1 / max 4. 3이면 848/849(99.9%)가 무절단이고, topN 기본 3에서
# 최악 9줄(경로 길이 median 55자 / max 94자)로 유계다. 상한을 4로 올려 1장을 더 덮는
# 이득보다 topN×상한의 최악 예산을 작게 두는 편이 낫다(중복 제거로 실제 수는 더 줄어든다).
DEFAULT_INJECT_SOURCE_PATHS_PER_CARD = 3
# 사용자 설정 clamp. 카드당 stat() 호출 수 = 이 값이므로 blocking hook 예산을 유계로 둔다.
MAX_INJECT_SOURCE_PATHS_PER_CARD = 10

SUMMARY_HEADING_RE = re.compile(r"\s*#{1,6}\s*Summary\s*")
COLLAPSE_BLANKS_RE = re.compile(r"\n{3,}")
# 불릿 한 줄에 들어가는 값(title·경로)에서 제거할 문자: 줄바꿈·탭 등 모든 공백류와
# 제어문자. 줄바꿈 하나가 들어가면 그 값이 새 프레임 줄을 만들 수 있다(POSIX 파일명에는
# 개행이 들어갈 수 있고, frontmatter title도 신뢰 입력이 아니다).
CONTROL_OR_SPACE_RE = re.compile(r"[\s\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028\u2029\ufeff]+")


def read_card_text_full(path: Path, *, bounded_shadow=False) -> str | None:
    """카드 전문을 **본문 지문 대조와 같은 정규화로** 읽는다(창 상한 없음).

    본문 지문(`yaml_scalars.card_body_hash`)은 "LF 로 접힌 디코딩 텍스트"에 대한 해시이고
    쓰는 쪽(`wiki_verify_worker.card_state` = `read_text`, universal newlines)과 읽는 쪽이
    **같은 결과**를 내야 한다. 그래서 여기서도 `newline=""` 로 열고 `normalize_newlines`
    로 명시 정규화한다(io 기본 변환에 의존하지 않는 이 파일의 관례와 같다).

    `errors="replace"` 인 이유: 비UTF-8 카드에서 훅 전체가 죽지 않게 한다. 그런 카드는
    `card_state` 의 `read_text` 가 예외를 내 애초에 검수를 통과할 수 없으므로
    `verifiedBodyHash` 를 가질 수 없다 — 즉 이 경로에서 대체문자로 인한 오탐은 구조적으로
    생기지 않는다.

    본문 읽기(`read_wiki_meta`)의 창 상한을 여기서 풀어도 되는 근거: 이 읽기는 카드에
    `verifiedBodyHash` 가 **있을 때만** 일어나고, 그때도 창을 소진한 카드에서만 일어난다.
    라이브 1,491장 실측 median 1.6KB / p99 6.2KB / max 11.6KB, 창은 8,192자라 대부분은
    첫 읽기로 이미 전문을 가진다. 전문 read+sha256 실측 0.113ms/장이다.
    """
    try:
        if bounded_shadow:
            from context_learning.seam import _snapshot
            snapshot = _snapshot(path)
            return normalize_newlines(snapshot[1].decode('utf-8', errors='replace')) if snapshot else None
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as handle:
            return normalize_newlines(handle.read())
    except (OSError, ValueError):
        return None


def wiki_card_read_limit(summary_max_chars: int) -> int:
    return max(WIKI_CARD_READ_BASE_CHARS, 2048 + 2 * max(0, summary_max_chars))


def normalize_newlines(text: str) -> str:
    """CRLF/CR을 LF로 통일한다. 이후 모든 줄 단위 처리의 전제다.

    회귀: 예전엔 `lstrip("\\n")`만 해서 CRLF 카드에서 `"\\r"`이 남아 `## Summary` 헤딩
    제거가 실패했다(주입문에 헤딩이 그대로 새어 나왔다). fixture가 LF만 만들어 잡히지
    않았다. 파일을 `newline=""`로 열어 io 기본 변환에 의존하지 않고 여기서 명시 정규화한다.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def strip_html_comments(text: str) -> str:
    """`<!-- ... -->`를 **선형 시간**으로 제거한다.

    정규식 `<!--.*?-->`는 닫히지 않은 `<!--`가 반복되면 시작점마다 나머지를 재탐색해
    입력 길이의 제곱으로 커진다 — 실측 8/16/32/64KiB에서 36/135/552/2215ms. 읽기 창
    32768자·`<!--`×1638 카드 8장이면 17초로 queryTimeout을 압도했다. str.find 루프는
    각 위치를 한 번만 지나 선형이다.
    짝 없는 `<!--`는 **그대로 남긴다** — 잘라내면 그 뒤 본문이 통째로 사라져 주입이
    조용히 비어 버린다(정규식 판은 실제로 본문 0자를 만들었다).
    """
    out = []
    pos = 0
    while True:
        start = text.find(wiki_markers.COMMENT_OPEN, pos)
        if start == -1:
            out.append(text[pos:])
            break
        end = text.find(wiki_markers.COMMENT_CLOSE, start + len(wiki_markers.COMMENT_OPEN))
        if end == -1:
            out.append(text[pos:])
            break
        out.append(text[pos:start])
        pos = end + len(wiki_markers.COMMENT_CLOSE)
    return "".join(out)


def strip_disclaimer(text: str) -> str:
    """자동 생성 카드 상투구 줄을 제거한다(정보 0).

    정규식(`^>\\s*Auto-generated...$`)을 쓰지 않는 이유: `\\s`가 개행을 넘어 매칭돼
    `>` 인용 줄이 많은 카드에서 백트래킹이 커진다. 줄 단위 접두 비교는 선형이다.
    """
    if wiki_markers.AUTO_DISCLAIMER_PREFIX not in text:
        return text
    return "\n".join(
        line for line in text.split("\n")
        if not line.lstrip().startswith(wiki_markers.AUTO_DISCLAIMER_PREFIX)
    )


def _strip_leading_summary_heading(text: str) -> str:
    """auto 블록 선두의 `## Summary` 헤딩만 제거한다.

    849장 전수 조사에서 auto 블록의 헤딩은 예외 없이 `## Summary` 하나뿐이었다 —
    주입문에서는 중복 라벨이라 노이즈다. 다른 헤딩(dedup 병합 섹션 등)은 내용의
    일부이므로 건드리지 않는다.
    """
    stripped = text.lstrip("\n")
    head, sep, rest = stripped.partition("\n")
    if SUMMARY_HEADING_RE.fullmatch(head):
        return rest if sep else ""
    return stripped


def _auto_block_bounds(body: str) -> tuple[int, int, int, bool]:
    """(auto 내용 시작, auto 내용 끝, 수동 섹션 시작, 종료 마커를 봤는지) — 선형 find 스캔.

    마커 리터럴은 core/wiki_markers.py가 SSOT다(쓰기 쪽 wiki_compile과 공유).
    네 번째 값은 "auto 블록이 온전히 창 안에 들어왔는지"다 — 읽기 창에서 잘렸는지
    판정하는 데 쓴다.

    문자열이 아니라 **오프셋**을 내는 이유: 매칭 위치 인용(데몬 `line`)이 "그 줄이
    auto 블록 안인가"를 판정하려면 경계의 위치가 필요하고, 그 판정을 여기서 한 번 더
    구현하면 블록 경계 판정이 두 벌이 된다.
    """
    start = body.find(wiki_markers.AUTO_START_OPEN)
    if start == -1:
        # auto 마커가 없는 카드(수동 작성/구버전)는 본문 전체가 내용이다.
        return 0, len(body), len(body), False
    open_end = body.find(wiki_markers.COMMENT_CLOSE, start)
    if open_end == -1:
        head = start + len(wiki_markers.AUTO_START_OPEN)
        return head, len(body), len(body), False
    open_end += len(wiki_markers.COMMENT_CLOSE)
    end = body.find(wiki_markers.AUTO_END, open_end)
    if end == -1:
        # 읽기 상한에 잘려 종료 마커를 못 본 경우: 시작 마커 뒤 전부를 auto로 본다.
        return open_end, len(body), len(body), False
    return open_end, end, end + len(wiki_markers.AUTO_END), True


def _split_auto_block(body: str) -> tuple[str, str, bool]:
    """(auto 블록 안, auto:end 밖, 종료 마커를 봤는지). 경계는 _auto_block_bounds가 정한다."""
    auto_start, auto_end, manual_start, closed = _auto_block_bounds(body)
    return body[auto_start:auto_end], body[manual_start:], closed


def build_card_body(auto: str, manual: str, closed: bool) -> tuple[str, bool]:
    """auto/수동 조각 → 주입 본문. 조립 규칙 한 벌(extract_card_body와 매칭 위치 인용 공유).

    수동 섹션을 포함하는 이유: dedup 병합(커밋 c510103)이 삭제 카드의 고유 사실을
    `qmd:auto:end` **밖**에 접어 넣는다. 빼면 그 사실이 recall에서 영구히 안 보인다.
    849장 중 수동 섹션이 있는 카드는 10장뿐이라 평균 비용은 사실상 0이다.
    HTML 주석(`<!-- merged from ... -->` 등)은 출처 메타라 제거한다.
    """
    auto = _strip_leading_summary_heading(strip_html_comments(strip_disclaimer(auto))).strip()
    manual_stripped = strip_html_comments(strip_disclaimer(manual)).strip()
    joined = "\n\n".join(part for part in (auto, manual_stripped) if part)
    # complete: auto 블록이 온전히 들어왔고 그 뒤에 본문으로 쓸 내용이 없다. 읽기 창이
    # 소진된 상태에서 이게 False면 본문이 중간에서 끊긴 것이므로 절단 표식이 필요하다.
    complete = closed and not manual_stripped
    return COLLAPSE_BLANKS_RE.sub("\n\n", joined).strip(), complete


def extract_card_body(text: str, body_start: int) -> tuple[str, bool]:
    """카드에서 주입할 본문을 뽑는다: auto 블록 Summary + auto:end 밖 수동 섹션."""
    return build_card_body(*_split_auto_block(text[body_start:]))


def line_start_offset(text: str, line_no: int) -> int | None:
    """1-based 줄 번호 → text 안의 문자 오프셋. 창 밖이면 None.

    데몬 결과의 `line`은 **파일 줄 번호**이고 우리는 이미 카드 파일을 읽어 두었으므로,
    데몬의 `snippet`(diff 형식 문자열)을 파싱하지 않고 여기서 직접 위치를 잡는다.
    """
    if not isinstance(line_no, int) or isinstance(line_no, bool) or line_no <= 1:
        # line 1은 파일 선두라 "앞부분부터"와 같다(재배치할 이유가 없다).
        return None
    offset = 0
    for _ in range(line_no - 1):
        newline = text.find("\n", offset)
        if newline == -1:
            return None
        offset = newline + 1
    return offset


def paragraph_start(text: str, offset: int, floor: int) -> int:
    """offset이 속한 문단(빈 줄 구분)의 시작. floor 밑으로는 내려가지 않는다.

    매칭 줄 하나만 뽑지 않고 문단 단위로 올라가는 이유: 한 줄만 떼면 주어·조건절이
    앞줄에 있는 카드에서 사실이 뒤집혀 읽힌다.
    """
    boundary = text.rfind("\n\n", floor, offset)
    return floor if boundary == -1 else boundary + 2


# 매칭 위치부터 인용할 때 앞을 건너뛴 사실을 알리는 표식. 없으면 모델이 카드의 서두를
# 읽고 있다고 오해한다(절단 표식과 같은 이유로 필요하다).
SUMMARY_LEAD_ELISION_MARK = "… (앞부분 생략)"


def match_positioned_body(text: str, body_start: int, match_line, limit: int) -> str | None:
    """데몬 `line`이 auto 블록 **안**이면 그 문단부터 인용한 본문을 만든다. 아니면 None.

    적용 조건 두 가지 모두를 만족할 때만 재배치한다:
    - 매칭 줄이 auto 블록 안이다. frontmatter/블록 밖이면 기존 앞부분 인용이 옳다
      (실측: `"n8n 멀티팀"` → line 2 = frontmatter, title이 이미 주입되므로 무용).
    - 매칭 문단이 원문 기준 `limit`보다 뒤에 있다. 즉 앞부분 인용이었다면 **확실히**
      잘려 나갔을 위치다. 가공 후 본문은 원문 슬라이스보다 짧아질 뿐이므로
      (상투구·HTML 주석 제거) 이 비교는 한쪽으로만 틀린다 — 앞부분에 이미 들어갔을
      매칭을 버리는 일은 없다.
    상한(limit)은 바꾸지 않는다 — 이것은 토큰 절감이 아니라 정확도 변경이다.
    """
    offset = line_start_offset(text, match_line)
    if offset is None or offset < body_start:
        return None
    body = text[body_start:]
    rel = offset - body_start
    auto_start, auto_end, manual_start, closed = _auto_block_bounds(body)
    if not (auto_start <= rel < auto_end):
        return None
    para = paragraph_start(body, rel, auto_start)
    if para - auto_start <= limit:
        return None
    summary, _ = build_card_body(body[para:auto_end], body[manual_start:], closed)
    if not summary:
        return None
    return SUMMARY_LEAD_ELISION_MARK + "\n" + summary


def _sentence_boundary(head: str, floor: int) -> int:
    """head 안에서 floor 이상인 마지막 문장 끝 인덱스. 없으면 -1.

    종결부호 뒤에 공백(또는 문자열 끝)이 오는 것만 문장 끝으로 본다 — 그러지 않으면
    `v0.7`·`settings.json`·`0.22.2` 중간을 문장 끝으로 오인해 식별자를 반으로 자른다.
    """
    for index in range(len(head) - 1, floor - 1, -1):
        char = head[index]
        if char == "\n":
            return index
        if char in ".!?。" and (index + 1 >= len(head) or head[index + 1].isspace()):
            return index
    return -1


def truncate_summary(text: str, limit: int) -> tuple[str, bool]:
    """카드 본문을 limit 문자로 자르고 (결과, 내용 손실 여부)를 반환한다. limit 0이면 끈다.

    절단 표시는 상한 **안**에 들어간다(표시 길이를 예산에서 미리 뺀다). 문장 경계에서
    자르는 이유는 _sentence_boundary 참고. 경계가 예산의 60% 앞이면(= 버리는 양이
    과하면) 문자 단위로 자른다.
    마크다운 구조 보정(fence 닫기 등)은 여기서 하지 않는다 — 프레임 보호는 주입 시점의
    줄 단위 인용 접두(quote_body_lines)가 담당한다. 블록 종류별 규칙(fence 문자·길이,
    HTML 블록 종료 토큰, setext, 표)을 개별로 흉내내는 방식은 세 라운드 연속 구멍이 났다.
    """
    if limit <= 0 or not text:
        return "", False
    if len(text) <= limit:
        return text, False
    budget = max(1, limit - len(SUMMARY_TRUNCATION_MARK))
    head = text[:budget]
    boundary = _sentence_boundary(head, int(budget * 0.6))
    if boundary >= 0:
        head = head[:boundary + 1]
    return head.rstrip() + SUMMARY_TRUNCATION_MARK, True


def sanitize_inline(text: str) -> str:
    """불릿 한 줄에 넣을 값(title·경로)을 한 줄로 강제한다.

    줄바꿈·제어문자·zero-width·줄 구분자를 공백으로 접고 양끝을 다듬는다. 한 글자의
    개행이면 그 값이 **새 프레임 줄**을 만들 수 있다 — POSIX 파일명에는 개행이 들어갈 수
    있고 frontmatter title도 신뢰 입력이 아니다(자동 생성 + 사람 편집).
    """
    return CONTROL_OR_SPACE_RE.sub(" ", text).strip()


def display_card_path(path: Path, cwd: str) -> str:
    """모델이 그대로 Read할 수 있는 경로 문자열.

    hook payload의 `cwd`는 host 세션의 cwd이고 모델의 상대경로 Read도 같은 기준이라,
    cwd 하위면 cwd 상대(보통 project root와 같으므로 `.auto-context/wiki/...`)로 낸다.
    cwd가 project 하위 디렉터리인 세션에서는 `../..` 사슬 대신 절대경로를 낸다 —
    사슬은 읽기 어렵고 base를 오해하면 조용히 다른 파일을 가리킨다.
    """
    try:
        base = Path(cwd).resolve()
    except (OSError, ValueError):
        return str(path)
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


FRONTMATTER_KEYS = ("status", "createdBy", "title", "verifiedBodyHash")


def parse_frontmatter_scalars(block: str, issues: dict | None = None) -> dict:
    """frontmatter 블록에서 관심 top-level scalar만 뽑는다(first-wins).

    **들여쓴 줄은 무시한다.** 예전엔 `line.strip().startswith(key+":")`라 nested 매핑
    (`sources:\\n  - {kind: "file", ... }` 안의 값이나 향후 추가될 중첩 `status:`)이
    top-level 값을 마지막-승으로 덮을 수 있었다. 검수 판정이 status에 걸려 있어
    이 오염은 곧 recall 전체 drop이다.

    wiki_compile.parse_frontmatter를 재사용하지 않은 이유: 그 파서는 예상 밖 들여쓰기
    한 줄에도 `{}, False`로 **fail-closed** 한다(쓰기 경로에는 옳다). recall은 읽기
    경로라 조금 어긋난 frontmatter에서도 status/title을 살려 fail-open해야 하고,
    wiki_compile은 urllib·wiki_dedup_judge를 끌고 와 blocking hook에 import 비용을
    더한다. 마커 리터럴은 wiki_markers, **인용/이스케이프 규칙은 yaml_scalars**로 공유한다
    — 규칙이 갈린 것이 title 오염의 직접 원인이었다.
    `issues`가 주어지면 파싱하지 못한 키의 사유를 담는다(진단용).
    """
    if issues is None:
        issues = {}
    fields: dict[str, str] = {}
    for line in block.split("\n"):
        if not line or line[0].isspace():
            continue
        for key in FRONTMATTER_KEYS:
            if key in fields or key in issues or not line.startswith(f"{key}:"):
                continue
            raw = line.split(":", 1)[1]
            if yaml_scalars.is_block_scalar_header(raw):
                # `title: >` / `title: |` — 값이 이어지는 들여쓴 줄에 있어 여기서는 볼 수
                # 없다. 예전엔 `>` 한 글자를 값으로 주입했다.
                issues[key] = "block_scalar"
                continue
            # 인용/이스케이프 규칙은 yaml_scalars가 SSOT다(쓰기 wiki_compile.yaml_scalar와
            # 동일 규칙). 예전 `strip('"\'')`는 닫는 인용부호와 이스케이프된 `\"`를 함께
            # 벗겨 백슬래시를 남겼다 — service-engineering 731장 중 8장이 그 상태였다.
            value, issue = yaml_scalars.load_with_issue(raw)
            if issue:
                # 인용부호가 열린 채 줄이 끝났다 = 값이 잘렸다(개행이 든 값이 이스케이프
                # 없이 쓰인 카드). 잘린 값을 조용히 쓰지 않고 신호로 남긴다.
                issues[key] = issue
                continue
            if value:
                fields[key] = value
            else:
                # `title: ""` 같은 빈 스칼라. 예전엔 issue 없이 사라져 title 누락과
                # 구분되지 않았다(status가 비면 미검수 drop으로 이어진다).
                issues[key] = "empty"
    return fields


def sources_inline_entry(raw: str) -> str:
    """`sources:` 줄의 잔여 값에서 **항목으로 볼 것만** 돌려준다(없으면 빈 문자열).

    잔여는 네 갈래다 — (a) 빈 값(다음 줄들에 항목이 있다) (b) 주석뿐 (c) `[]`(+주석) =
    "소스 없음"의 정상 표기 (d) 진짜 한 줄 flow 시퀀스. (a)(b)(c)는 **항목이 아니므로 사유를
    만들지 않는다**: 예전엔 `[]` 이외의 모든 잔여를 항목화해 `sources: # 원문 목록` 같은
    정상 카드가 `parse_failed` 1건을 남겼고(허위 drop), `sources: [] # 없음`은
    `inline_sequence`로 잡혔다. 주입 손실은 없지만 3단계가 `source_missing` 정책을 정할 때
    읽는 신호에 **없는 실패**가 섞인다.
    (d)와 그 밖의 잘못된 값(`sources: 뭔가`)은 그대로 흘려보내 형태별 사유를 남긴다.
    """
    value = raw.strip()
    if not value or value.startswith("#"):
        return ""
    if value.startswith("[]"):
        rest = value[2:].strip()
        if not rest or rest.startswith("#"):
            return ""
    return value


def frontmatter_source_entries(block: str) -> list[str]:
    """frontmatter의 top-level `sources:` 아래 `- {...}` 항목의 **표기 그대로**를 낸다.

    `parse_frontmatter_scalars`가 들여쓴 줄을 의도적으로 무시하므로(중첩 값이 top-level
    scalar를 덮는 오염 방지) sources는 여기서 따로 모은다. 같은 문자열을 한 번 더
    훑을 뿐이고(frontmatter는 카드당 수십 줄) 파일 읽기는 늘지 않는다 —
    `read_wiki_meta`의 "카드당 I/O 1회" 계약을 유지한다.
    다른 top-level 키가 나오면 즉시 sources 구역을 벗어난다(항목이 새는 것을 막는다).

    **`sources: [ ... ]`(한 줄 flow 시퀀스)는 그 값을 항목 하나로 낸다.** 우리 writer는
    이 형태를 쓰지 않지만 사람·resolver가 표준 YAML로 쓸 수 있고, 예전엔 이 형태가
    `entries=[]`가 되어 **사유 없이 0건**이 됐다 — 로그에서 "소스 없는 카드"와 구분할 수
    없는 무흔적 실패다(이 저장소에서 반복된 클래스). 값을 그대로 흘려 보내면
    `resolve_source_path`가 `[`로 시작하는 항목을 `inline_sequence`로 신호한다.
    `sources: []`(빈 목록)는 "소스 없음"의 정상 표기라 항목을 만들지 않는다.
    """
    entries: list[str] = []
    in_sources = False
    for line in block.split("\n"):
        if not line.strip():
            continue
        if not line[0].isspace():
            in_sources = line.startswith("sources:")
            if in_sources:
                inline = sources_inline_entry(line.split(":", 1)[1])
                if inline:
                    entries.append(inline)
            continue
        if not in_sources:
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            entries.append(stripped[2:].strip())
    return entries


def frontmatter_source_revisions(block: str) -> list[dict]:
    """Parse the compiler-owned provenance list with a closed, bounded schema.

    The compiler writes a top-level ``sourceRevisions:`` block whose children
    are one-line flow mappings.  Any inline value, malformed sibling, nested
    block mapping, duplicate top-level declaration, or over-budget list makes
    the complete proof unusable.  A valid prefix is never authoritative.
    """
    entries: list[str] = []
    in_revisions = False
    seen_header = False
    invalid = False
    for line in block.split("\n"):
        if not line.strip():
            continue
        if not line[0].isspace():
            if line.startswith("sourceRevisions:"):
                if seen_header:
                    invalid = True
                seen_header = True
                in_revisions = True
                inline = line.split(":", 1)[1].strip()
                if inline and not inline.startswith("#"):
                    invalid = True
            else:
                in_revisions = False
            continue
        if not in_revisions:
            continue
        stripped = line.strip()
        if not stripped.startswith("- "):
            invalid = True
            continue
        entries.append(stripped[2:].strip())
        if len(entries) > MAX_FRESHNESS_SOURCE_REVISIONS_PER_CARD:
            invalid = True
            break
    if invalid or not seen_header or not entries:
        return []
    return yaml_scalars.load_source_revisions(entries)


def is_auto_trusted_card(meta: dict) -> bool:
    """Static recall trust: qmd-created, verified, compiler-provenanced."""
    return (
        meta.get("status") in TRUSTED_WIKI_STATUSES
        and meta.get("createdBy") == "qmd-auto-context"
        and bool(meta.get("sourceRevisions"))
    )


def _event_time_ns(value) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return int(parsed.timestamp() * 1_000_000_000)


def pending_refresh_cutoffs(project_root: Path) -> dict[str, int] | None:
    """Latest unresolved pending timestamp by source path, read once/request."""
    events = wiki_freshness.unresolved_pending_refreshes_strict(project_root)
    if events is None:
        return None
    cutoffs: dict[str, int] = {}
    for event in events:
        path = event.get("sourcePath")
        cutoff = _event_time_ns(event.get("ts"))
        if not isinstance(path, str) or not path or cutoff is None:
            return None
        cutoffs[path] = cutoff
    return cutoffs


# sources 항목 중 원문 경로로 쓸 수 있는 유일한 kind. `wiki_verify_worker.load_sources`가
# 같은 판정(`src.get("kind") != "file"`)을 쓴다 — **두 곳이 갈리면 안 된다**: 한쪽은 원문을
# 검증에 쓰고 다른 한쪽은 그 경로를 모델에게 제시하므로, 서로 다른 집합을 보면 "검증되지
# 않은 종류의 소스"가 주입된다.
SOURCE_KIND_FILE = "file"
# "파일 소스가 아니다"(= 소실 판정에서 무시) 사유. `wiki_source_missing`이 이 집합을
# 그대로 쓴다 — 소실 판정이 두 벌이 되지 않게 한다.
NON_FILE_SOURCE_REASONS = ("kind_not_file", "no_path")
# 주입할 경로 문자열 상한. title(160)·본문(600)에는 상한이 있는데 경로만 무제한이면
# 토큰 절감 목표에 구멍이 남는다(실측: 822자 경로가 축자 주입됐다. 최악 topN 3 × 상한
# 3경로 × 1000자 ≈ 9KB). 근거: 실코퍼스 876항목의 경로 길이는 상대 median 53 / p95 69 /
# max 92자이고, 절대 표시(project prefix 포함)는 median 91 / p95 107 / max 130자다.
# 200이면 관측 최악(절대 130)의 1.5배 여유이고 최악 주입은 9줄 × 204자 ≈ 1.8KB로 유계다.
# **넘으면 자르지 않고 버린다** — 잘린 경로는 열 수 없어 없는 파일을 열게 만든다(개행 든
# 경로를 정규화하지 않고 버리는 것과 같은 논리다).
MAX_SOURCE_PATH_CHARS = 200
# load_flow_mapping의 issue → drop 사유. 형태별로 갈라 두는 이유: 사유 하나로 뭉치면
# 로그만 보고 "카드를 어떻게 고쳐야 하는지"를 알 수 없다.
SOURCE_PARSE_REASONS = {
    "unterminated": "multiline_flow",     # 여러 줄 flow mapping(첫 줄만 보인다) 또는 잘린 항목
    "trailing_garbage": "parse_failed",
    "unbalanced_quote": "parse_failed",
    "empty": "parse_failed",              # 쓸 수 있는 키가 없다(예: `{nope!: 1}`)
}
# `- kind: file` 처럼 block mapping으로 쓴 항목. 지원하지 않고(파서를 두 벌로 만들지
# 않는다) 사유로만 남긴다 — 로그의 이 값이 곧 "flow mapping으로 다시 쓰라"는 지시다.
BLOCK_MAPPING_ENTRY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*\s*:")


def source_parse_reason(entry: str, issue: str) -> str:
    """파싱 실패 항목의 **형태**를 사유로 돌려준다(무흔적 실패 금지)."""
    if entry.startswith("["):
        return "inline_sequence"
    if issue == "not_flow_mapping":
        return "block_mapping" if BLOCK_MAPPING_ENTRY_RE.match(entry) else "parse_failed"
    return SOURCE_PARSE_REASONS.get(issue, "parse_failed")


def resolve_existing_source(raw_path: str, project_root: Path,
                            allow_roots: list[Path]) -> tuple[Path | None, str]:
    """경로 문자열 하나 → (존재가 확인된 실경로, 실패 사유). **소스 존재 판정 SSOT.**

    세 호출부가 이 함수를 공유한다 — 주입(`resolve_source_path`), 기계 검수
    (`wiki_verify_worker`가 `wiki_source_missing`을 경유), SessionStart 수명 주기
    (`wiki_lifecycle`). 판정이 갈리면 "주입은 링크를 지웠는데 수명 주기는 정상으로 본다"
    같은 모순이 생긴다.
    사유 값(`too_long`/`outside_root`/`missing`)과 검사 순서는 그대로 유지한다.
    """
    if len(raw_path) > MAX_SOURCE_PATH_CHARS:
        # resolve/stat 전에 본다 — 비정상 길이 입력에 syscall을 쓰지 않는다.
        return None, "too_long"
    resolved = qmd_resolve_paths.contained_path(project_root, raw_path, allow_roots)
    if resolved is None:
        return None, "outside_root"
    try:
        if not resolved.is_file():
            return None, "missing"
    except OSError:
        return None, "missing"
    return resolved, ""


def classify_source_entry(entry: str, project_root: Path,
                          allow_roots: list[Path]) -> tuple[Path | None, str, str]:
    """sources 항목 하나 → (존재하는 실경로, 실패 사유, 표기된 원본 path 문자열).

    표시(한 줄 강제·표시 길이) **전까지의** 판정이다. 표시 규칙은 주입 전용이므로
    소실 분류·검수는 여기까지만 쓴다. 세 번째 값은 "카드에 적힌 그대로의 경로"다 —
    여기서 내주지 않으면 호출부가 flow mapping을 한 번 더 파싱해야 하고 그것이 곧 판정
    이중화다.
    """
    fields, issue = yaml_scalars.load_flow_mapping(entry)
    raw_path = fields.get("path") if isinstance(fields.get("path"), str) else ""
    if not raw_path or not raw_path.strip():
        if issue:
            return None, source_parse_reason(entry, issue), ""
        # `{kind: unknown}`(소스 없음)이 여기 온다 — 정상 카드이므로 사유만 세고 넘어간다.
        return None, "no_path", ""
    if fields.get("kind") != SOURCE_KIND_FILE:
        return None, "kind_not_file", raw_path
    resolved, reason = resolve_existing_source(raw_path, project_root, allow_roots)
    return resolved, reason, raw_path


def resolve_source_path(entry: str, project_root: Path, cwd: str,
                        allow_roots: list[Path]) -> tuple[str, str]:
    """sources 항목 하나 → (모델이 Read할 수 있는 표시 경로, 실패 사유).

    `sources[].path`는 **extractor(모델) 출력**이므로 신뢰 입력이 아니다. 다섯 가지를 본다
    (1~5의 판정은 `classify_source_entry`/`resolve_existing_source`가 SSOT이고 여기서는
    표시 규칙만 더한다):
    1. kind — `file`만 원문 경로로 쓴다(`kind_not_file`). `{kind:"url", path:"docs/x.md"}`
       처럼 로컬 경로 모양의 값이면 원문으로 주입돼 버리고, 반대로 실제 url/slack 소스는
       `missing`으로 빠져 **사유가 틀린 채** 집계된다 — `source_missing` 정책(로드맵 3단계)이
       그 신호를 근거로 삼으므로 "파일이 아닌 것"이 섞이면 판단이 오염된다.
    2. path 타입·존재 — 비어 있지 않은 문자열이어야 한다(`no_path`). 파서가 문자열만
       돌려주므로 타입 검사는 이중 방어다(쓰기 쪽 불변식은 `wiki_compile.source_flow_entries`).
    3. 길이 — `MAX_SOURCE_PATH_CHARS` 초과는 버린다(`too_long`). 자르지 않는 이유는 상수
       주석 참고.
    4. traversal — `resolve_paths.contained_path`가 resolve **후** project_root(또는
       allowRoots) 안인지 본다. `../../../etc/passwd`와 밖을 가리키는 심볼릭 링크가
       여기서 걸린다. 검증을 재구현하지 않고 collectionPath와 같은 함수를 쓴다.
    5. 존재·프레임 — 없는 경로를 주면 모델이 Read에 실패하고 헛돈다(`missing`). 표시
       문자열이 `sanitize_inline`으로 바뀐다면(개행·탭·zero-width) 한 줄을 벗어날 수 있고
       접은 문자열은 실제 파일을 가리키지 않으므로 버린다(`not_inline` — 공백 1개가 든
       정상 경로는 접혀도 그대로라 통과한다).
    기준 base가 project_root인 이유: `wiki_compile_enqueue._source_record`가 경로를
    project_root 상대 POSIX로 기록한다(cwd 상대가 아니다).
    """
    resolved, reason, _raw = classify_source_entry(entry, project_root, allow_roots)
    if resolved is None:
        return "", reason
    display = display_card_path(resolved, cwd)
    # 표시가 절대경로면 project prefix가 붙어 상대 경로보다 길어진다 — 실제 주입되는
    # 문자열을 기준으로 다시 본다(토큰 예산은 주입 문자열의 함수다).
    if len(display) > MAX_SOURCE_PATH_CHARS:
        return "", "too_long"
    if sanitize_inline(display) != display:
        return "", "not_inline"
    return display, ""


def collect_source_paths(block: str, project_root: Path, cwd: str, limit: int,
                         allow_roots: list[Path],
                         observe_only: bool = False) -> tuple[list[str], int, dict[str, int], int]:
    """(표시 경로 목록, 본 항목 수, 사유별 drop 수, **살아 있는 파일 소스 수**).

    마지막 값이 따로 필요한 이유: 주입 목록만으로는 "살아 있는 원문이 없다"와 "관측
    전용 모드라 목록을 안 채웠다"·"카드 사이 중복 제거로 비었다"를 구분할 수 없다
    (`cards_all_sources_missing`이 이것 때문에 과대 집계됐다).
    카드당 상한 `limit`을 적용한다.

    카드 안 중복도 여기서 제거한다(같은 원문을 두 번 가리키는 항목). 카드 **사이**의
    중복은 최종 목록이 정해진 뒤 `dedup_source_paths`가 처리한다.

    `observe_only`는 **주입하지 않고 사유만 센다**(`injectSourcePathsPerCard: 0` + 진단
    로그 켜짐). `cards_all_sources_missing`이 그 설정 때문에 죽으면, "주입 배지를 두지
    않는 대신 로그 카운터가 남긴다"는 3단계 근거가 성립하지 않는다 — 카드는 여전히
    캐논으로 주입되기 때문이다. 반환 경로 목록은 항상 비어 있으므로 주입 문자열은
    바이트 동일하다.
    """
    paths: list[str] = []
    # 채택 후보(중복 판정용). observe_only에서는 `paths`가 영구히 비므로 `paths`로 중복을
    # 보면 `duplicate`가 **절대 발화하지 못한다**(死코드였다) — 두 모드가 같은 중복 판정을
    # 쓰도록 목록을 분리한다. 주입 모드에서는 seen과 paths가 항상 같다.
    seen: list[str] = []
    reasons: dict[str, int] = {}
    present = 0
    entries = frontmatter_source_entries(block)
    # 조사 항목 수도 상한을 둔다: `sources`는 모델이 준 목록이라 개수에 보장이 없고
    # 항목마다 stat()이 붙는다(읽기 창 8192자에는 수백 항목이 들어간다). 채택 상한의
    # 2배까지 조사하면 "정상 항목 하나당 이상 항목 하나"까지 견디면서 카드당 stat 수가
    # 주입 예산의 상수배로 유계다(실코퍼스 849장에는 애초에 max 4항목이다).
    # observe_only에서도 stat 예산은 유계여야 한다 — 기본 상한과 같은 규모로 고정한다.
    scan_limit = (limit if limit > 0 else DEFAULT_INJECT_SOURCE_PATHS_PER_CARD) * 2
    for index, entry in enumerate(entries):
        # 두 상한을 **다른 사유**로 남긴다: `over_cap`은 "쓸 만한 경로가 상한만큼 이미
        # 찼다"(설정을 올리면 늘어난다), `over_scan_budget`은 "조사 예산이 소진됐다"
        # (= 앞쪽 항목이 대량으로 버려졌다는 신호이므로 카드를 봐야 한다). 뭉치면 로그로
        # 둘을 구분할 수 없다.
        # over_cap은 주입 상한 전용이다. observe_only에는 상한이 없으므로(주입하지 않는다)
        # 이 사유가 발화할 수 없고, 발화하면 안 된다.
        if not observe_only and len(paths) >= limit:
            reasons["over_cap"] = reasons.get("over_cap", 0) + 1
            continue
        if index >= scan_limit:
            reasons["over_scan_budget"] = reasons.get("over_scan_budget", 0) + 1
            continue
        display, reason = resolve_source_path(entry, project_root, cwd, allow_roots)
        if not display:
            reasons[reason] = reasons.get(reason, 0) + 1
            continue
        present += 1
        if display in seen:
            reasons["duplicate"] = reasons.get("duplicate", 0) + 1
            continue
        seen.append(display)
        # 관측 전용: 사유·present 집계에만 참여하고 주입 목록에는 넣지 않는다.
        if not observe_only:
            paths.append(display)
    return paths, len(entries), reasons, present


def sources_all_missing(missing: int, present: int, undecidable: int) -> bool:
    """"소스 전부 소실" 판정의 **단일 구현**.

    소실 1건 이상 + 살아 있는 파일 소스 0 + 판정 불가 0. `wiki_source_missing`의
    카드/레코드 분류와 이 모듈의 로그 카운터가 **이 함수 하나**를 호출한다 —
    예전엔 같은 규칙을 두 곳이 각자 구현해 `over_scan_budget`이 있는 카드에서 답이
    갈렸고(한쪽 True, 한쪽 False) 주석은 "같은 규칙"이라고 말했다.

    입력 완전성은 호출부마다 다르고 그것이 유일한 차이다: 스캐너는 항목 전부를 조사하고,
    recall은 카드당 stat 예산이 있어(`over_scan_budget`) 미조사 항목을 판정 불가로 센다 —
    그래서 항목이 예산보다 많은 카드에서 recall이 세지 않는 쪽으로(과소) 갈린다. 이는
    규칙의 차이가 아니라 조사 범위의 차이이고, 주입 경로의 상한을 지키기 위한 의도된
    보수성이다(로그 카운터가 있다고 stat을 무제한 늘리지 않는다).
    """
    return missing > 0 and present == 0 and undecidable == 0


# 카드 본문 프레임 보호: **줄 단위 인용 접두**.
#
# 세 라운드 연속 같은 클래스가 났다 — 구분자를 도입했더니 안내문이 escalation을 안 따라가고,
# fence를 닫았더니 절단 경로에만 걸렸고, 그걸 고쳤더니 fence 문자·길이 규칙(중첩 ````/```,
# 혼합 ```/~~~)과 닫히지 않은 HTML 블록(`<!--`·`<script>`·`<div>`)이 남았다. 마크다운은
# 블록 종류마다 개시·종료 규칙이 달라 **개별 대응은 원리적으로 끝나지 않는다.**
#
# 전환: CommonMark의 모든 블록 개시(fenced code, HTML block, ATX heading, setext
# underline, thematic break, list item, block quote, GFM table row)는 **줄 선두**(들여쓰기
# 3칸까지)에 와야 성립한다. 따라서 본문의 **모든 줄** 앞에 블록 의미가 없는 리터럴 접두를
# 붙이면 그 줄은 어떤 블록도 열 수 없고, 열린 상태가 다음 줄로 이어질 수도 없다. 블록
# 종류를 열거하지 않고 한 규칙으로 전체를 덮는다.
# 접두를 `| `로 고른 이유: (1) CommonMark에서 줄 선두 `|`는 어떤 블록도 열지 않는다
# (GFM 표는 구분 행이 따로 있어야 성립하고, 그마저 빈 열 하나가 늘 뿐 프레임을 벗어나지
# 못한다), (2) 인용 gutter로 널리 읽히는 관용 표기다, (3) **본문을 재작성하지 않는다**
# (접두를 붙이고 후행 공백만 지운다) — extractor 축자 보존 계약(191f0f9)이 유지되고,
# 모델은 접두 하나만 벗기면 원문을 복원한다(후행 공백 예외는 quote_body_lines 참고).
# 구분자 escalation·충돌·안내문 불일치 문제는 이 접근에서 **구조적으로 존재하지 않는다**:
# 경계 판정이 영역이 아니라 줄 단위이므로 본문이 어떤 문자열을 담아도 프레임 줄과 섞이지
# 않는다. 4칸 들여쓰기(코드블록화)를 택하지 않은 이유: 리스트 항목 안에서 4칸의 의미가
# 모호하고, 본문 안 fence가 코드블록 안에서 어떻게 읽힐지 렌더러에 의존하며, 산문을 code로
# 표시하는 것이 의미상 틀리다.
# 실제로 줄 앞에 붙는 문자열 전체. 안내문도 **이 상수**를 인용한다 — 안내문이 실제와
# 다른 접두를 선언하는 것이 "안내문 불일치" 클래스의 잔재였다(빈 줄은 rstrip으로
# `  |`가 되므로 안내문은 그 형태까지 덮는 표현을 쓴다).
BODY_LINE_PREFIX = "  | "
BODY_LINE_PREFIX_MARK = BODY_LINE_PREFIX.rstrip()

# 원문 경로 줄의 접두. 같은 원리로 고른 리터럴이다 — 줄 선두 `↳`는 CommonMark에서 어떤
# 블록도 열지 않으므로 프레임 밖으로 새지 않는다. 경로 하나에 한 줄을 쓰는 이유: 여러
# 경로를 한 줄에 구분자로 이어 붙이면 구분자를 담은 경로(POSIX는 `,`·공백을 허용한다)에서
# 모델이 경계를 오해해 **존재하지 않는 경로를 Read**하게 된다. 줄 하나에 값 하나면
# 경계 판정이 필요 없다(본문 인용 접두와 같은 판단).
SOURCE_LINE_PREFIX = "  ↳ "
SOURCE_LINE_PREFIX_MARK = SOURCE_LINE_PREFIX.rstrip()


def quote_body_lines(summary: str) -> list[str]:
    """본문 각 줄에 인용 접두를 붙여 반환한다(빈 줄도 접두를 받는다).

    빈 줄에도 붙이는 이유: 접두 없는 줄이 하나라도 있으면 그 지점에서 프레임과 본문의
    구분이 사라지고, 다음 줄이 블록을 열 수 있다.
    `rstrip()`은 **후행 공백을 지운다** — 접두를 붙이는 것 외에 본문을 건드리지 않는다는
    설명의 유일한 예외다(후행 공백만 있는 줄은 `  |`가 되어 원문 복원이 불가능하다).
    실카드 849장에서 후행 공백이 있는 본문 줄은 0건이고, 남기면 주입문에 눈에 보이지 않는
    공백이 쌓이므로 이쪽을 택했다.
    """
    return [f"{BODY_LINE_PREFIX}{line}".rstrip() for line in summary.split("\n")]


# 이 recall 실행이 속한 프로젝트 루트. QMD_RECALL_LOG 는 프로젝트별 파일이 아니라
# **전역 파일 하나**라(라이브: ~/.claude/settings.json 의 env), 줄에 귀속이 없으면 여러
# 프로젝트의 진단이 한 파일에 섞여 "이 프로젝트의 recall 이 나아졌는가"를 물을 수 없다.
# 3.8 백필 검증에서 실제로 막혔다 — `dropped_stale` 이 프로젝트별로 집계되지 않아 census
# 델타로 우회했다. 계획서 §5("측정 없이는 개선을 확인할 수 없다")가 프로젝트 단위로
# 성립하려면 이 필드가 필요하다.
#
# 인자가 아니라 모듈 상태인 이유: 기록 지점이 20곳이 넘고 그 절반이 조기 return 이라
# 파라미터로 스레딩하면 **다음에 추가되는 지점이 빠뜨린다** — per-site try/except 를
# hook_main.py 한 곳으로 모은 것과 같은 클래스다. 훅은 프로세스당 main() 1회라
# 모듈 상태의 유일한 위험(다음 실행으로의 이월)이 구조적으로 성립하지 않는다.
_LOG_PROJECT_ROOT: str | None = None


def set_log_project_root(project_root: str | None) -> None:
    """이후 모든 로그 줄에 붙일 프로젝트 루트를 고정한다(main() 에서 1회).

    cwd 가 아니라 project root 인 이유: 같은 프로젝트의 하위 디렉터리에서 시작한 세션은
    cwd 가 갈리므로 cwd 로 묶으면 한 프로젝트가 여러 갈래로 쪼개진다. project root 는
    한 recall 호출 안에서 불변이고(`wiki_roots` 와 같은 값) 설정 파일의 위치라 프로젝트
    자체의 이름이다.

    **그 불변성의 범위는 HOME 하위다** — `config._project_search_dirs` 는 HOME 밖에서는
    부모를 탐색하지 않으므로(cwd 하나만 본다) HOME 밖 하위 디렉터리 세션은 자기 cwd 를
    루트로 받는다. 집계에는 영향이 없다: 그런 경로에는 설정 파일이 발견되지 않아 recall
    이 `no_collections` 로 끝나고 묶을 진단이 애초에 없다. 라이브 프로젝트는 전부 HOME
    하위다. (보장을 전 경로의 보장으로 서술하지 않는다 — 어느 범위에서 참인지 함께 적는다.)

    설정을 못 찾은 디렉터리에서도 `find_project_config` 가 resolve 된 cwd 를 돌려주므로
    값은 항상 있다 — 즉 "어디서 나온 줄인가"는 언제나 답이 된다.
    """
    global _LOG_PROJECT_ROOT
    _LOG_PROJECT_ROOT = str(project_root) if project_root else None


def log_score_observation(log_path: str | None, results: list[dict], collections: list[str]) -> None:
    if not log_path or not results:
        return
    scores = [r.get("score", 0) for r in results]
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "qmd_score_observation",
        "engine": os.environ.get("QMD_ENGINE", "gemini"),
        # cwd 를 알기 전 줄(empty_stdin·invalid_payload_json·prompt_too_short)은 null 이다
        # — 키 부재(구포맷)와 "아직 프로젝트를 모른다"를 구분해야 한다(`lex_hits` 의
        # "안 물었다 ≠ 물었는데 0" 과 같은 규약).
        "project_root": _LOG_PROJECT_ROOT,
        "transport": "http",
        "collections": collections,
        "top_n": len(results),
        "scores": scores,
        "max_score": max(scores) if scores else 0,
    }
    try:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass

def log_recall_event(log_path: str | None, reason: str, **fields) -> None:
    """Append a one-line selection/skip reason to QMD_RECALL_LOG.

    Writes to the log file only (never stdout), and only when QMD_RECALL_LOG
    is set — so it never touches the model context and is a no-op in normal runs.
    Lets an operator tell *why* recall produced empty output (empty_stdin /
    invalid_payload_json / prompt_too_short / event_disabled / no_keywords /
    no_collections / daemon_unreachable / query_failed / no_results_after_filter /
    selected).

    **Every early return in main() must land here** — sandbox is the one exception
    ("exit immediately with no output" is its contract, so it writes nothing at all).
    A skip that writes no line is indistinguishable from "the hook never ran"
    (dispatcher error, exit 127, sandbox), and E2E diagnosis has nothing else to read.
    """
    if not log_path:
        return
    # event_override: 같은 append 경로를 쓰면서 event 이름만 바꾼다(shadow 진단 라인).
    # 기존 qmd_recall_selection 소비자가 진단 라인을 selection으로 오독하지 않게 한다.
    event = fields.pop("event_override", None) or "qmd_recall_selection"
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "engine": os.environ.get("QMD_ENGINE", "gemini"),
        # cwd 를 알기 전 줄(empty_stdin·invalid_payload_json·prompt_too_short)은 null 이다
        # — 키 부재(구포맷)와 "아직 프로젝트를 모른다"를 구분해야 한다(`lex_hits` 의
        # "안 물었다 ≠ 물었는데 0" 과 같은 규약).
        "project_root": _LOG_PROJECT_ROOT,
        "reason": reason,
    }
    payload.update(fields)
    try:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass

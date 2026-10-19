#!/usr/bin/env python3
"""frontmatter scalar 인용/역인용 규칙의 SSOT.

`dump`(쓰기)와 `load`(읽기)는 서로의 역함수여야 하고, **동시에 표준 YAML이어야 한다.**

두 번의 실패가 이 모듈의 형태를 정했다:
1. 규칙이 두 벌로 갈려 있던 동안(쓰기는 `"`→`\\"`, 읽기는 `strip('"')`) 읽기가 닫는
   인용부호와 이스케이프된 `"`를 함께 벗겨 백슬래시가 남았다 — service-engineering
   731장 중 8장의 title이 그 상태로 주입됐고 로그에 흔적이 없었다.
2. 규칙을 한 곳으로 모은 뒤에도 `dump`가 `"`만 이스케이프해 **표준 YAML이 아니었다.**
   백슬래시·개행이 그대로 나가 PyYAML로는 21종 중 7종이 ScannerError/오독이었다.
   실해는 title에 그치지 않는다: `wiki_compile.parse_frontmatter`가 같은 카드에서
   fail-closed(`ok=False`)하므로 이후 verify·dedup이 그 카드에서 죽는다.

그래서 `dump`는 YAML 1.2 double-quoted 규칙을 구현한다(PyYAML 대조는 테스트에서 한다 —
런타임 의존성을 blocking hook에 추가하지 않는다).

`load`는 의도적으로 **관용적(lenient)** 이다: 표준이 아닌 이스케이프(`\\p` 등)를 만나면
에러가 아니라 문자 그대로 남긴다. 표준 파서라면 거부할 값이지만, 이 함수는 이미 디스크에
있는 카드를 읽어 recall을 살려야 하는 경로이므로 fail-open이 옳다(hook은 어떤 입력에도
exit 0이어야 한다). 대신 열린 인용부호처럼 **값이 잘렸다고 판단되는 경우는 신호로 알린다**
(`load_with_issue`) — 조용히 잘린 값을 반환하던 것이 위 1번과 같은 클래스다.
"""

from __future__ import annotations
import hashlib
import re

BLOCK_SCALAR_HEADERS = {">", "|", ">-", "|-", ">+", "|+", ">>", "|2", ">2"}

# flow mapping의 키로 허용할 형태. 키도 모델(extractor)이 주므로 개행·구두점이 든 키는
# 매핑을 벗어나 새 줄을 만들 수 있다 — 안전한 식별자만 통과시킨다.
# (wiki_compile.SAFE_YAML_KEY_RE가 이 상수를 그대로 재노출한다: 정의는 한 곳이다.)
SAFE_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")

# Compiler-owned provenance has a deliberately closed schema.  Model-provided
# ``sources`` remains an open audit field; source revisions are the current
# filesystem proof used to decide whether a verified card may be recalled.
SOURCE_REVISION_KEYS = ("kind", "path", "collection", "sha256", "size", "mtimeNs")
SOURCE_REVISION_KEY_SET = frozenset(SOURCE_REVISION_KEYS)
SHA256_RE = re.compile(r"[0-9a-f]{64}")
NONNEGATIVE_INT_RE = re.compile(r"0|[1-9][0-9]*")

# Verifier-cited source line spans (``sourceSpans``).  Same closed-schema rule
# as ``sourceRevisions``: no raw quote text, typed 1-based inclusive line
# numbers, and any malformed sibling makes the whole list unusable.
SOURCE_SPAN_KEYS = ("path", "sourceSha256", "startLine", "endLine", "spanSha256")
SOURCE_SPAN_KEY_SET = frozenset(SOURCE_SPAN_KEYS)
SOURCE_SPANS_KEY = "sourceSpans"
MAX_SOURCE_SPANS = 16
MAX_SPAN_LINES = 200
MAX_SPAN_PATH_CHARS = 512
# Eight digits bound both the token length handed to ``int`` and the value.
LINE_NUMBER_RE = re.compile(r"[1-9][0-9]{0,7}")
_UNSAFE_SPAN_PATH_RE = re.compile(r'[\x00-\x1f\x7f-\x9f\\"#  ‪-‮⁦-⁩]')

# YAML 1.2 double-quoted 이스케이프(값 → 표기). 순서 중요: 백슬래시가 먼저다.
_DUMP_ESCAPES = [
    ("\\", "\\\\"),
    ('"', '\\"'),
    ("\n", "\\n"),
    ("\r", "\\r"),
    ("\t", "\\t"),
    ("\x00", "\\0"),
    ("\x07", "\\a"),
    ("\x08", "\\b"),
    ("\x0b", "\\v"),
    ("\x0c", "\\f"),
    ("\x1b", "\\e"),
    ("\x85", "\\N"),
    ("\xa0", "\\_"),
    ("\u2028", "\\L"),
    ("\u2029", "\\P"),
]

# 표기 → 값. `\xNN`/`\uNNNN`/`\UNNNNNNNN`는 별도 처리한다.
_LOAD_SIMPLE = {
    "\\": "\\", '"': '"', "/": "/", "n": "\n", "r": "\r", "t": "\t",
    "0": "\x00", "a": "\x07", "b": "\x08", "v": "\x0b", "f": "\x0c",
    "e": "\x1b", " ": " ", "N": "\x85", "_": "\xa0", "L": "\u2028",
    "P": "\u2029",
}
_LOAD_HEX = {"x": 2, "u": 4, "U": 8}


def _needs_hex(char: str) -> bool:
    code = ord(char)
    return code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F


def dump(value) -> str:
    """스칼라를 **표준 YAML** double-quoted 표기로 낸다.

    항상 인용하는 이유: 인용 없는 plain scalar는 `: `·`#`·선두 기호 등에서 의미가 바뀌고,
    무엇을 인용해야 하는지 판정하는 규칙이 또 하나의 갈릴 지점이 된다.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    for raw, escaped in _DUMP_ESCAPES:
        text = text.replace(raw, escaped)
    # 위 목록에 없는 C0/C1 제어문자(예: \x01)는 raw로 나가면 표준 파서가 거부한다.
    text = "".join(
        char if not _needs_hex(char) else "\\x%02x" % ord(char)
        for char in text
    )
    return f'"{text}"'


def is_block_scalar_header(raw: str) -> bool:
    """`key: >` / `key: |` 처럼 값이 이어지는 줄들에 있는 경우."""
    return raw.strip() in BLOCK_SCALAR_HEADERS


def _ends_with_escaped_quote(text: str, quote: str) -> bool:
    """마지막 인용부호가 실은 이스케이프된 문자라 값이 닫히지 않은 경우.

    `"...\\"` — 끝의 `"`는 `\\"`의 일부이므로 값이 열린 채다. 홀수 개의 연속 백슬래시가
    앞서면 이스케이프된 것이다(짝수면 `\\\\` 리터럴 뒤의 진짜 닫는 인용부호).
    """
    if quote != '"' or len(text) < 3:
        return False
    backslashes = 0
    index = len(text) - 2
    while index >= 1 and text[index] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1


def _unescape_double(body: str) -> str:
    out = []
    index = 0
    length = len(body)
    while index < length:
        char = body[index]
        if char != "\\" or index + 1 >= length:
            out.append(char)
            index += 1
            continue
        marker = body[index + 1]
        if marker in _LOAD_SIMPLE:
            out.append(_LOAD_SIMPLE[marker])
            index += 2
            continue
        if marker in _LOAD_HEX:
            width = _LOAD_HEX[marker]
            digits = body[index + 2:index + 2 + width]
            if len(digits) == width:
                try:
                    out.append(chr(int(digits, 16)))
                    index += 2 + width
                    continue
                except ValueError:
                    pass
        # 표준이 아닌 이스케이프: 에러 대신 문자 그대로 남긴다(fail-open — 이미 디스크에
        # 있는 카드를 읽는 경로다. 예전 dump가 백슬래시를 이스케이프하지 않았으므로
        # `C:\\path` 같은 값이 그대로 들어 있을 수 있고, 그 title을 계속 읽어야 한다).
        out.append(char)
        index += 1
    return "".join(out)


def load_with_issue(raw: str) -> tuple[str, str]:
    """(값, 문제) — 문제 없으면 빈 문자열.

    문제 값: `unbalanced_quote`(인용부호가 열린 채 줄이 끝났다 = 값이 잘렸다).
    쓰기가 개행을 이스케이프하지 않던 시절 멀티라인 title이 실제 개행으로 디스크에
    나가, 첫 줄만 읽으면 `"멀티` 처럼 열린 인용부호로 끝났다 — 예전엔 그 문자열을
    조용히 값으로 반환했다(앞 인용부호까지 붙은 채).
    """
    text = raw.strip()
    if not text:
        return "", ""
    quote = text[0]
    if quote not in ("'", '"'):
        # plain scalar. YAML은 plain scalar에 이스케이프를 두지 않으므로 그대로 쓴다.
        return text, ""
    if len(text) < 2 or text[-1] != quote or _ends_with_escaped_quote(text, quote):
        return text[1:], "unbalanced_quote"
    body = text[1:-1]
    if quote == "'":
        # single-quoted YAML: 백슬래시 이스케이프가 없고 `''`만 리터럴 `'`다.
        return body.replace("''", "'"), ""
    return _unescape_double(body), ""


def load(raw: str) -> str:
    """frontmatter 표기를 원래 문자열로 되돌린다(`dump`의 역함수)."""
    return load_with_issue(raw)[0]


def dump_flow_mapping(mapping) -> str:
    """`{k: "v", ...}` flow mapping 표기를 낸다. 쓸 수 있는 항목이 없으면 빈 문자열.

    `sources:` 항목이 이 표기다(`- {kind: "raw", path: "docs/a.md"}`). **읽는 쪽
    (`load_flow_mapping`)과 쌍으로 여기 둔다** — 이 저장소는 emit/parse가 갈려 두 번
    깨졌다(title 이스케이프 규칙 8/731장 오염, `qmd:auto` 마커 정규식). 값은 전부 `dump`를
    거치므로 `,`·`}`·개행·인용부호가 들어도 표기를 벗어나지 못하고, 키는 SAFE_KEY_RE
    화이트리스트다.

    **값은 문자열만 낸다(비문자 값은 그 항목 키를 버린다).** 이유는 왕복 타입 안정성이다:
    `load_flow_mapping`은 텍스트 파서라 무엇을 넣어도 문자열만 돌려준다. 그래서 비문자
    값을 받아 주면 `{"path": False}` → `path: false` → 되읽어 `"false"`가 되어, **`false`
    라는 이름의 파일이 원문으로 주입되고 진단에는 정상으로 남는다**(drop 0, injected 1).
    타입 안정성을 이 쌍 안에서 보장하지 않으면 호출부마다 같은 함정이 재발한다.
    호출부(`wiki_compile.markdown_page`)는 여기에 의존하지 말고 **항목 단위로** 필수 필드를
    검증한다 — 키만 빠지면 경로가 조용히 사라진 항목이 남기 때문이다.
    """
    parts = []
    for key, value in mapping.items():
        if not isinstance(key, str) or not SAFE_KEY_RE.fullmatch(key):
            continue
        if not isinstance(value, str):
            continue
        parts.append(f"{key}: {dump(value)}")
    return "{" + ", ".join(parts) + "}" if parts else ""


def _is_separator_colon(text: str, index: int) -> bool:
    """`text[index]`의 `:`가 **key/value 구분자**인지(값의 일부가 아닌지).

    YAML plain scalar는 `:`를 담을 수 있고(`docs/a:b.md`), 구분자는 `: `처럼 공백(또는
    flow 종료·항목 구분)이 뒤따르는 `:`뿐이다. 이 구분을 하지 않으면 `{path:x: docs/a.md}`의
    키를 `path`로 잘라 **없는 경로**를 만들어 사유가 `missing`으로 오표기된다(PyYAML은 키를
    `path:x`로 읽어 아예 다른 매핑이다). 구분자 판정은 이 함수 한 곳이다 — 스캐너와
    항목 분해가 같은 규칙을 써야 한다.
    """
    if text[index] != ":":
        return False
    if index + 1 >= len(text):
        return True
    return text[index + 1].isspace() or text[index + 1] in (",", "}")


def _split_key_value(item: str) -> tuple[str, bool, str]:
    """flow mapping 항목을 (키, 구분자 있음, 값)으로 나눈다. 규칙은 위 함수가 SSOT다."""
    quote = ""
    for index, char in enumerate(item):
        if quote:
            if quote == '"' and char == "\\":
                continue
            if char == quote:
                quote = ""
            continue
        if char in ('"', "'") and not item[:index].strip():
            quote = char
            continue
        if _is_separator_colon(item, index):
            return item[:index], True, item[index + 1:]
    return item, False, ""


def _scan_flow_mapping(text: str) -> tuple[list, str]:
    """`{k: v, ...}` 표기를 **항목 목록**으로 나눈다: (items, 문제).

    스캐너를 하나로 둔 이유: 예전엔 "닫는 `}` 찾기"와 "항목 나누기"가 각자 인용부호 규칙을
    들고 있었다 — 규칙이 두 벌이면 한쪽만 고쳐져 조용히 갈린다(이 저장소가 두 번 깨진
    클래스다). 인용 판정은 여기 한 곳이다.

    YAML 규칙 세 가지를 지킨다:
    1. **인용부호는 스칼라 선두에서만 인용을 시작한다.** 값 중간의 `"`/`'`는 plain scalar의
       일부다. 예전엔 어디서든 인용을 열어 `{path: a"b, kind: c}`의 `,`를 값 안으로 삼켜
       `kind`를 잃었다 — 도달 결과는 전부 fail-closed였지만(경로 주입 0건) 사유가
       `kind_not_file`/`missing`으로 **오표기**됐다. 3단계가 그 신호로 `source_missing`
       정책을 정하므로 오표기는 그대로 정책 오판이 된다.
    2. single-quoted 안의 `''`는 이스케이프된 `'`이고 인용을 닫지 않는다.
    3. double-quoted 안의 `\\`는 다음 문자를 이스케이프한다.

    문제 값: `not_flow_mapping`(`{`로 시작하지 않음) / `unbalanced_quote`(인용부호가 열린 채
    끝남 = 값이 잘렸다) / `unterminated`(닫는 `}`가 없음 — 여러 줄 flow의 첫 줄) /
    `trailing_garbage`(`}` 뒤에 주석 아닌 내용).
    """
    if not text.startswith("{"):
        return [], "not_flow_mapping"
    items: list = []
    buf: list = []
    quote = ""
    # 인용부호가 인용을 열 수 있는 위치인지. 항목 선두와 첫 `:` 뒤(값 선두)에서만 참이다.
    at_scalar_start = True
    seen_colon = False
    index = 1
    length = len(text)
    while index < length:
        char = text[index]
        if quote:
            buf.append(char)
            if quote == '"' and char == "\\" and index + 1 < length:
                buf.append(text[index + 1])
                index += 2
                continue
            if char == quote:
                if quote == "'" and index + 1 < length and text[index + 1] == "'":
                    # `''` = 리터럴 `'`. 인용은 계속 열려 있다(값 안의 `,`가 보호된다).
                    buf.append("'")
                    index += 2
                    continue
                quote = ""
            index += 1
            continue
        if char in ('"', "'") and at_scalar_start:
            quote = char
            buf.append(char)
            at_scalar_start = False
            index += 1
            continue
        if char == ",":
            items.append("".join(buf))
            buf = []
            at_scalar_start = True
            seen_colon = False
            index += 1
            continue
        if not seen_colon and _is_separator_colon(text, index):
            buf.append(char)
            seen_colon = True
            at_scalar_start = True
            index += 1
            continue
        if char == "}":
            tail = text[index + 1:].strip()
            # 표준 YAML에서 flow mapping 뒤에 올 수 있는 것은 주석뿐이다.
            if tail and not tail.startswith("#"):
                return [], "trailing_garbage"
            items.append("".join(buf))
            return items, ""
        if not char.isspace():
            at_scalar_start = False
        buf.append(char)
        index += 1
    # 닫는 `}`를 못 봤다. 인용부호가 열린 채면 값이 잘린 것이고(그 안의 `}`를 닫는 괄호로
    # 오인하지 않은 결과다), 아니면 여러 줄 flow mapping의 첫 줄이다.
    return [], "unbalanced_quote" if quote else "unterminated"


def load_flow_mapping(raw: str) -> tuple[dict, str]:
    """`{k: "v", ...}` → (dict, 문제). `dump_flow_mapping`의 역함수.

    문제 값: `not_flow_mapping`(`{`로 시작하지 않음) / `unterminated`(닫는 `}`가 없음 —
    여러 줄 flow이거나 잘린 항목) / `trailing_garbage`(`}` 뒤에 주석 아닌 내용) /
    `unbalanced_quote`(값이 잘렸다) / `empty`(쓸 수 있는 키가 하나도 없음).
    fail-open이 원칙이라 파싱한 키는 문제와 함께 그대로 돌려준다 — 읽기 경로(recall)는
    한 항목이 이상해도 나머지를 살려야 한다.
    중복 키는 first-wins다(`parse_frontmatter_scalars`와 같은 규약).

    **키는 인용부호를 허용한다**(`{"kind": "file"}`). emit은 인용하지 않지만 표준 YAML은
    허용하고, wiki 카드의 `sources`는 사람과 dedup/review resolver 에이전트가 손으로
    이식하는 필드다(CLAUDE.md 계약) — 표준 YAML로 쓴 카드를 못 읽으면 그 카드의 원문
    링크가 통째로 사라진다. 인용을 벗긴 뒤 SAFE_KEY_RE로 검증하므로 허용 범위는 넓어지지
    않고(개행·구두점이 든 키는 여전히 거부), 왕복 성질도 유지된다(emit 결과의 상위집합).
    """
    items, issue = _scan_flow_mapping(raw.strip())
    if issue:
        return {}, issue
    fields: dict[str, str] = {}
    issue = ""
    for item in items:
        if not item.strip():
            continue
        key, sep, value = _split_key_value(item)
        if not sep:
            continue
        key, key_issue = load_with_issue(key)
        if key_issue or not SAFE_KEY_RE.fullmatch(key) or key in fields:
            continue
        parsed, value_issue = load_with_issue(value)
        if value_issue:
            issue = issue or value_issue
            continue
        fields[key] = parsed
    if not fields and not issue:
        issue = "empty"
    return fields, issue


def _closed_flow_mapping(raw: str, int_keys=("size", "mtimeNs"),
                         int_re=NONNEGATIVE_INT_RE) -> dict | None:
    """Parse one flow mapping without the permissive first-wins fallback.

    ``load_flow_mapping`` is intentionally lenient for legacy ``sources``.
    Provenance is different: duplicate, malformed, or unknown fields must not
    become a compiler-owned revision record.  ``int_keys`` names the fields
    whose unquoted token must match ``int_re`` and become an ``int``.
    """
    items, issue = _scan_flow_mapping(raw.strip())
    if issue:
        return None
    fields = {}
    for item in items:
        if not item.strip():
            # The permissive source parser ignores empty items.  A compiler
            # provenance record must reject `,,` and trailing commas instead
            # of treating a malformed mapping as a complete revision.
            return None
        key, separator, value = _split_key_value(item)
        if not separator:
            return None
        key, key_issue = load_with_issue(key)
        parsed, value_issue = load_with_issue(value)
        if key_issue or value_issue or not SAFE_KEY_RE.fullmatch(key) or key in fields:
            return None
        if key in int_keys:
            # ``load_with_issue`` intentionally returns strings for both
            # `1` and `"1"`.  Preserve the original token distinction here:
            # quoted numbers are strings, not typed provenance integers.
            token = value.strip()
            if not int_re.fullmatch(token):
                return None
            fields[key] = int(token)
        else:
            fields[key] = parsed
    return fields


def _nonnegative_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    return None


def normalize_source_revision(record) -> dict | None:
    """Validate one source revision and return its stable typed representation."""
    if not isinstance(record, dict) or set(record) != SOURCE_REVISION_KEY_SET:
        return None
    kind = record.get("kind")
    path = record.get("path")
    collection = record.get("collection")
    sha256 = record.get("sha256")
    size = _nonnegative_int(record.get("size"))
    mtime_ns = _nonnegative_int(record.get("mtimeNs"))
    if kind != "file" or not isinstance(path, str) or not path:
        return None
    if not isinstance(collection, str) or not isinstance(sha256, str):
        return None
    if not SHA256_RE.fullmatch(sha256) or size is None or mtime_ns is None:
        return None
    return {
        "kind": kind,
        "path": path,
        "collection": collection,
        "sha256": sha256,
        "size": size,
        "mtimeNs": mtime_ns,
    }


def parse_source_revision(raw: str) -> dict | None:
    """Parse exactly one closed-schema ``sourceRevisions`` flow mapping."""
    if not isinstance(raw, str):
        return None
    return normalize_source_revision(_closed_flow_mapping(raw))


def dump_source_revisions(revisions) -> list[str]:
    """Emit only valid compiler-owned revision records as YAML flow mappings."""
    if not isinstance(revisions, list):
        return []
    normalized_records = [normalize_source_revision(record) for record in revisions]
    # Provenance is a complete compiler snapshot.  Emitting a valid prefix
    # when a sibling is malformed would make a partial source set look trusted.
    if any(record is None for record in normalized_records):
        return []
    emitted = []
    for normalized in normalized_records:
        # ``dump_flow_mapping`` correctly rejects non-string model fields.  This
        # closed compiler schema intentionally includes two typed integers, so
        # serialize its fixed keys here rather than weakening that open-source
        # helper's type contract.
        emitted.append("{" + ", ".join(
            f"{key}: {dump(normalized[key])}" for key in SOURCE_REVISION_KEYS
        ) + "}")
    return emitted


def load_source_revisions(entries) -> list[dict]:
    """Parse a complete revision list; any bad item rejects the entire list."""
    if not isinstance(entries, list):
        return []
    parsed = []
    for entry in entries:
        revision = parse_source_revision(entry)
        if revision is None:
            return []
        parsed.append(revision)
    return parsed


def _line_number(value) -> int | None:
    if type(value) is not int or not 1 <= value <= 99_999_999:
        return None
    return value


def _safe_span_path(value) -> bool:
    if (not isinstance(value, str) or not value or len(value) > MAX_SPAN_PATH_CHARS
            or value.startswith("/") or _UNSAFE_SPAN_PATH_RE.search(value)):
        return False
    return all(part not in ("", ".", "..") for part in value.split("/"))


def normalize_source_span(record) -> dict | None:
    """Validate one verifier-cited line span; ``None`` for anything else.

    The record carries no quote text.  ``#`` is refused in the path because the
    rendered locator is ``path#Lx-Ly`` and must stay unambiguous.
    """
    if not isinstance(record, dict) or set(record) != SOURCE_SPAN_KEY_SET:
        return None
    path = record.get("path")
    source_sha = record.get("sourceSha256")
    span_sha = record.get("spanSha256")
    start = _line_number(record.get("startLine"))
    end = _line_number(record.get("endLine"))
    if not _safe_span_path(path):
        return None
    if not isinstance(source_sha, str) or not SHA256_RE.fullmatch(source_sha):
        return None
    if not isinstance(span_sha, str) or not SHA256_RE.fullmatch(span_sha):
        return None
    if start is None or end is None or end < start or end - start + 1 > MAX_SPAN_LINES:
        return None
    return {"path": path, "sourceSha256": source_sha, "startLine": start,
            "endLine": end, "spanSha256": span_sha}


def _normalize_source_spans(spans) -> list[dict] | None:
    """Whole-list validation shared by emit and load: all valid, unique, bounded."""
    if not isinstance(spans, list) or not spans or len(spans) > MAX_SOURCE_SPANS:
        return None
    normalized = [normalize_source_span(item) for item in spans]
    if any(item is None for item in normalized):
        return None
    keys = {(item["path"], item["startLine"], item["endLine"]) for item in normalized}
    if len(keys) != len(normalized):
        return None
    return normalized


def dump_source_spans(spans) -> list[str]:
    """Emit a complete span list as flow mappings, or nothing at all."""
    normalized = _normalize_source_spans(spans)
    if normalized is None:
        return []
    return ["{" + ", ".join(f"{key}: {dump(item[key])}" for key in SOURCE_SPAN_KEYS) + "}"
            for item in normalized]


def load_source_spans(entries) -> list[dict] | None:
    """Parse a complete span list; ``None`` when any entry or the list is bad."""
    if not isinstance(entries, list):
        return None
    parsed = []
    for entry in entries:
        if not isinstance(entry, str):
            return None
        parsed.append(normalize_source_span(_closed_flow_mapping(
            entry, int_keys=("startLine", "endLine"), int_re=LINE_NUMBER_RE)))
    return _normalize_source_spans(parsed)


_BLOCK_ENTRY_RE = re.compile(r"  - (\S.*)\Z")


def closed_block_entries(block: str, key: str, max_entries: int) -> tuple[str, list[str]]:
    """Find one top-level block list in a frontmatter body: ``(state, entries)``.

    ``state`` is ``absent``, ``present`` or ``invalid``.  Unlike the lenient
    readers this never returns a usable prefix: a duplicate or quoted-duplicate
    header, an inline value, an entry not written as ``  - <value>``, an empty
    list, or more than ``max_entries`` items all make the block ``invalid``.
    """
    entries: list[str] = []
    seen = False
    inside = False
    for line in block.split("\n"):
        if not line.strip():
            continue
        if not line[0].isspace():
            name, issue = load_with_issue(line.split(":", 1)[0].strip())
            if not issue and name == key:
                if seen or line.rstrip() != f"{key}:":
                    return "invalid", []
                seen = inside = True
            else:
                inside = False
            continue
        if not inside:
            continue
        match = _BLOCK_ENTRY_RE.fullmatch(line.rstrip())
        if match is None:
            return "invalid", []
        entries.append(match.group(1))
        if len(entries) > max_entries:
            return "invalid", []
    if not seen:
        return "absent", []
    return ("present", entries) if entries else ("invalid", [])


def fold_inline(value) -> str:
    """한 줄 스칼라로 쓸 값에서 개행·탭을 공백으로 접는다(쓰기 경로).

    title 같은 한 줄 라벨은 개행을 담을 이유가 없다. `dump`가 `\\n`으로 이스케이프해도
    표준 YAML이지만, 값 자체가 여러 줄이면 recall 주입에서 `sanitize_inline`이 다시 접게
    되고 카드 이름으로도 부적절하다 — **소스에서 접는다**. dump의 이스케이프는 그래도
    남겨 둔다(다른 필드·미래 입력에 대한 안전망).
    """
    return " ".join(str(value).split())


# frontmatter 블록 경계의 SSOT. `wiki_compile.FRONTMATTER_RE` 가 이 객체의 별칭이다 —
# 카드 본문 지문(`card_body_hash`)을 쓰는 쪽과 읽는 쪽이 **같은 경계**로 잘라야 하고,
# 정의가 두 벌이면 갈린다(이 모듈이 두 번 깨진 그 클래스다).
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def card_body_hash(text: str) -> str | None:
    """카드 본문(frontmatter 뒤 전부)의 지문 — 기계 검수(쓰기)와 recall(읽기)의 SSOT.

    **왜 필요한가.** wiki trust 는 `sourceRevisions`(원문 SHA-256)에만 결속돼 있어
    "원문이 컴파일 시점 이후 바뀌지 않았다"만 증명한다. 원문을 그대로 둔 채 **카드 본문만**
    고치면 `verified` 배지를 단 채 그대로 주입된다(실측 재현: `포트는 8080이다` →
    `포트는 9999이다` 로 바꿔도 `[wiki:verified]` 로 나갔다). 이 지문이 그 어긋남을 잡는다.

    **무엇을 해시하는가 — frontmatter 뒤 전부다.** 셋 중 이것만 두 조건을 함께 만족한다.
      - 파일 전문이 아닌 이유: `stamp_verification` 이 frontmatter 를 다시 쓰므로 전문
        해시는 스탬프 직후 자기 자신과 불일치한다(닭-달걀).
      - auto 블록만이 아닌 이유: verify payload 가 `{"card": {"content": text}}` 로
        **파일 전문**을 보내므로(`wiki_verify_worker.card_state`) verdict 는 블록 밖
        내용까지 덮는다. 블록만 결속하면 과소 주장이고, 고유 사실을 `qmd:auto:end` 밖에
        두라는 이 저장소의 규약 때문에 정확히 그 자리가 결속에서 빠진다.
      - 본문이 스탬프 전후 동일한 근거: `patch_frontmatter_fields` 가
        `text[:m.start(1)] + new_frontmatter + text[m.end(1):]` 로 쓴다 — `m.end(1)` 이후가
        축자 보존되고, 정규식이 그 자리에 `\n---\n` 을 요구하므로 `m.end()` 도 그 보존
        구간 안이다. 경계를 `m.end()` 로 두는 이유는 **의미와 코드를 일치시키기 위해서**다
        ("frontmatter 뒤 전부"라 말하면서 닫는 구분선을 포함하면 다음 사람이 정의를 오해한다).

    **바이트가 아니라 "정규화된 텍스트"의 해시다.** 입력은 이미 디코딩된 str 이고 줄끝은
    LF 로 접혀 있어야 한다(`wiki_verify_worker` 는 `read_text` 의 universal newlines,
    `recall` 은 `newline=""` + `normalize_newlines` — 두 경로가 같은 결과를 낸다). 즉
    줄끝만 바뀐 카드는 불일치로 잡히지 않는다. 의도된 것이다 — 에디터가 줄끝을 바꿨다는
    이유로 카드를 잃으면 안 된다. **한쪽을 `read_bytes()` 로 바꾸지 말 것**: 그 순간
    정상 카드가 전량 불신된다.

    **위협 모델은 적대자가 아니라 drift 다.** 카드는 사용자 자기 저장소에 있고, 카드에
    쓸 수 있는 주체는 `sourceRevisions` 도 원문 파일도 고칠 수 있으므로 카드 안의 평문
    해시는 적대적 쓰기 앞에서 경계가 아니다(같은 머신에 둔 HMAC 키도 경계가 아니다 —
    그 논리를 받으면 기존 `createdBy`·`sourceRevisions` 검사도 같이 무력해진다). 잡으려는
    것은 사람 편집·에이전트 consolidation·검수 중 경합이 만드는 **말 없는 어긋남**이다.

    frontmatter 가 없으면 None — 결속할 본문 경계가 정의되지 않는다.
    """
    match = FRONTMATTER_RE.match(text)
    if match is None:
        return None
    return hashlib.sha256(text[match.end():].encode("utf-8")).hexdigest()

#!/usr/bin/env python3
"""Deterministic wiki compile writer for qmd-auto-context.

This command intentionally accepts already-compact candidate JSON. It does not
persist raw transcripts and it does not run from query-time recall hooks.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))
import wiki_mutation_lock
from wiki_init import ensure_project_file
import compile_paths as cp
import config as qmd_config
import wiki_dedup_judge
import wiki_markers
import yaml_scalars
from dirty_queue import enqueue_project_collections

ALLOWED_TYPES = {
    "concept",
    "entity",
    "decision",
    "session",
    "comparison",
    "query",
    "character",
    "world-rule",
    "timeline",
    "plot-decision",
    "style",
}
TYPE_DIRS = {
    "concept": "concepts",
    "entity": "entities",
    "decision": "decisions",
    "session": "sessions",
    "comparison": "comparisons",
    "query": "queries",
    "character": "characters",
    "world-rule": "world",
    "timeline": "timeline",
    "plot-decision": "plot",
    "style": "style",
}
TYPE_DIR_NAMES = set(TYPE_DIRS.values())
ALLOWED_CONFIDENCE = {"high", "medium", "low"}
# frontmatter flow mapping의 키로 허용할 형태. 모델이 준 키가 개행·구두점을 담으면
# 매핑을 벗어나 새 줄을 만들 수 있다. 정의는 yaml_scalars(emit/parse 쌍의 SSOT)에 있다 —
# 읽는 쪽(recall.parse_frontmatter_sources)이 같은 화이트리스트를 써야 한다.
SAFE_YAML_KEY_RE = yaml_scalars.SAFE_KEY_RE
SECRET_LITERAL_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
]
# The `key: value` pattern is the one a technical card can trip merely by *naming* a
# config key, so its value is captured separately (group "value") and checked against
# _NON_SECRET_VALUE_RE below.
SECRET_KEYWORD_PATTERN = re.compile(r"(?i)(api[_-]?key|secret|token)\s*[:=]\s*(?P<value>[^\s]+)")
# Kept as the full pattern list for compatibility; prefer secret_matches()/redact().
SECRET_PATTERNS = SECRET_LITERAL_PATTERNS + [SECRET_KEYWORD_PATTERN]
# Values that cannot be a credential under any reading: placeholders, references to a
# value stored elsewhere, type names, booleans. A keyword match on one of these is
# dropped instead of rejecting the whole card. This narrows false positives only — an
# opaque value never matches here, so it is still redacted/rejected as before.
_NON_SECRET_VALUE_RE = re.compile(
    r"""(?ix) ^ (?:
          <[^>]*>                             # <YOUR_TOKEN>, <값>, <REDACTED>
        | \{\{[^}]*\}\} | \{[^}]*\}           # {{token}}, {token}
        | \$\{?[A-Za-z_][A-Za-z0-9_]*\}?      # $GITHUB_TOKEN, ${GITHUB_TOKEN}
        | \[?REDACTED\]?
        | \.{2,} | \*{2,} | x{3,} | _{2,} | -{2,}
        | true|false|null|none|nil|undefined
        | str|string|int|integer|number|float|bool|boolean|optional|required|any|object
    ) [.,;:)\]}'"`]* $""",
)


def is_non_secret_value(value: str) -> bool:
    return bool(_NON_SECRET_VALUE_RE.match(value.strip().strip("`\"'")))


def secret_matches(text: str) -> list[re.Match]:
    """Secret-pattern hits in `text`, excluding keyword hits on non-secret values."""
    matches = [m for pattern in SECRET_LITERAL_PATTERNS for m in pattern.finditer(text)]
    matches.extend(
        m for m in SECRET_KEYWORD_PATTERN.finditer(text)
        if not is_non_secret_value(m.group("value"))
    )
    return matches


def has_secret_like(text: str) -> bool:
    return bool(secret_matches(text))
TRANSCRIPT_RE = re.compile(r"(?im)^\s*(user|assistant|system|human|ai)\s*:")
# 마커 리터럴은 core/wiki_markers.py가 SSOT다 — recall의 읽기 스캐너와 같은 문자열을
# 쓰게 해서 형식 변경이 한쪽만 깨는 일을 막는다(엄격도 차이는 그 모듈 docstring 참고).
AUTO_START_RE = re.compile(wiki_markers.AUTO_START_PATTERN)
AUTO_BLOCK_RE = re.compile(
    wiki_markers.AUTO_START_PATTERN + r"\n.*?\n" + re.escape(wiki_markers.AUTO_END), re.S
)
# 정의는 yaml_scalars 가 SSOT다 — 카드 본문 지문(`card_body_hash`)이 이 경계로
# 자르므로 쓰는 쪽과 읽는 쪽이 같은 객체를 봐야 한다.
FRONTMATTER_RE = yaml_scalars.FRONTMATTER_RE


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def load_payload() -> dict:
    try:
        parsed = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def project_paths(cwd: str) -> tuple[Path, dict]:
    found = qmd_config.find_project_config(cwd)
    return Path(found["projectRoot"]).resolve(), found["config"]


def safe_managed_dir(root: Path, rel: str) -> Path | None:
    path = (root / rel).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        return None
    path.mkdir(parents=True, exist_ok=True)
    return path


def normalize_identity(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value.strip().lower())


def slug_identity(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^A-Za-z0-9가-힣]+", "-", value.lower()).strip("-")


def identity_keys(value: object) -> set[str]:
    return {key for key in (normalize_identity(value), slug_identity(value)) if key}


def clean_aliases(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    aliases = []
    seen = set()
    for item in value:
        if not isinstance(item, str):
            continue
        alias = item.strip()
        if not alias:
            continue
        norm = normalize_identity(alias)
        if norm in seen:
            continue
        seen.add(norm)
        aliases.append(alias)
    return aliases


def clean_canonical_key(value: object) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def unquote_yaml(value: str) -> str:
    # 인용/역인용 규칙은 core/yaml_scalars.py가 SSOT다 — recall의 읽기 파서가 같은
    # 함수를 쓰게 해서 이스케이프 규칙이 갈리지 않게 한다(title 오염의 원인).
    return yaml_scalars.load(value)


def parse_yaml_scalar(value: str):
    text = unquote_yaml(value)
    if text.lower() == "true":
        return True
    if text.lower() == "false":
        return False
    return text


def parse_yaml_inline_list(value: str) -> list:
    text = value.strip()
    if not (text.startswith("[") and text.endswith("]")):
        return []
    inner = text[1:-1].strip()
    if not inner:
        return []
    return [parse_yaml_scalar(part.strip()) for part in inner.split(",") if part.strip()]


def parse_frontmatter(text: str) -> tuple[dict, bool]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, False
    meta = {}
    current_key = None
    for raw_line in match.group(1).splitlines():
        if not raw_line.strip():
            continue
        if raw_line.startswith("  - "):
            if current_key is None:
                return {}, False
            meta.setdefault(current_key, []).append(parse_yaml_scalar(raw_line[4:]))
            continue
        if raw_line.startswith(" ") or ":" not in raw_line:
            return {}, False
        key, raw_value = raw_line.split(":", 1)
        key = key.strip()
        if not key:
            return {}, False
        if key in meta:
            return {}, False
        raw_value = raw_value.strip()
        if raw_value == "":
            meta[key] = []
            current_key = key
        elif raw_value.startswith("[") and raw_value.endswith("]"):
            meta[key] = parse_yaml_inline_list(raw_value)
            current_key = None
        else:
            meta[key] = parse_yaml_scalar(raw_value)
            current_key = None
    return meta, True


def identity_values_from_meta(meta: dict) -> list[str]:
    values = []
    for key in ("canonicalKey", "title"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    aliases = meta.get("aliases")
    if isinstance(aliases, list):
        values.extend(alias.strip() for alias in aliases if isinstance(alias, str) and alias.strip())
    elif isinstance(aliases, str) and aliases.strip():
        values.append(aliases.strip())
    return values


def build_identity_index(wiki_root: Path) -> dict[str, set[Path]]:
    index: dict[str, set[Path]] = {}
    if not wiki_root.exists():
        return index
    for page in wiki_root.rglob("*.md"):
        try:
            text = page.read_text(encoding="utf-8")
        except OSError:
            continue
        meta, ok = parse_frontmatter(text)
        if not ok:
            continue
        for value in identity_values_from_meta(meta):
            for key in identity_keys(value):
                index.setdefault(key, set()).add(page.resolve())
    return index


def candidate_identity_tiers(candidate: dict) -> list[tuple[str, list[str]]]:
    tiers = []
    canonical = clean_canonical_key(candidate.get("canonicalKey"))
    if canonical:
        tiers.append(("canonicalKey", [canonical]))
    aliases = clean_aliases(candidate.get("aliases"))
    if aliases:
        tiers.append(("aliases", aliases))
    title = str(candidate.get("title") or "").strip()
    if title:
        tiers.append(("title", [title]))
    return tiers


def source_paths_of(sources) -> set[str]:
    """`sources` 목록 → 경로 집합. **두 표기를 모두 받는다.**

    candidate(모델 출력)는 dict 목록이지만, 카드 frontmatter를 `parse_frontmatter`로 읽으면
    flow mapping이 **raw 문자열**로 남는다(`{kind: file, path: "docs/a.md"}`). 한 표기만
    보면 카드 쪽이 항상 빈 집합이 되어 "소스가 겹치는가" 판정이 조용히 죽는다.
    역파싱은 `yaml_scalars.load_flow_mapping`(SSOT)을 쓴다.
    """
    if not isinstance(sources, list):
        return set()
    paths = set()
    for item in sources:
        if isinstance(item, str):
            fields, _issue = yaml_scalars.load_flow_mapping(item)
            item = fields
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if isinstance(path, str) and path.strip():
            paths.add(path.strip())
    return paths


def explicit_target_agrees(target: Path, candidate: dict) -> tuple[bool, str]:
    """Does a model-chosen `targetPath` actually name the candidate's own card?

    `resolve_target` trusts `targetPath` over identity lookup, so an extractor that
    emits an unrelated existing path sends a **different** card's page down the
    `updated` branch. Measured consequences, both bad:
      - verify says `inconclusive` → the card is DELETED, and anything a human wrote
        outside `qmd:auto:end` is gone for good (verify-deleted.jsonl stores no body).
        This is the live `지킴진단-…md` loop.
      - verify says `pass` → only the auto block is replaced, so the body states facts
        from source B while `sources:`/`title` still describe A. Provenance is silently
        wrong and the injected `↳` source path points at the wrong file.

    Agreement means the candidate shares an identity value (canonicalKey / alias /
    title, via the same `identity_keys` normalisation the index uses) OR at least one
    `sources[].path` with the page on disk. Either is enough: a re-extraction of the
    same document legitimately renames a card, and a renamed source legitimately keeps
    the identity. Neither → route to review instead of overwriting.

    Unreadable/unparsable frontmatter returns True (fail-open): that page is already
    handled by `is_auto_writable_page`, and blocking writes on a parse failure would
    stall compile for a card nobody can fix from here.
    """
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return True, ""
    meta, ok = parse_frontmatter(text)
    if not ok:
        return True, ""
    existing_keys: set[str] = set()
    for value in identity_values_from_meta(meta):
        existing_keys |= identity_keys(value)
    candidate_keys: set[str] = set()
    for _reason, values in candidate_identity_tiers(candidate):
        for value in values:
            candidate_keys |= identity_keys(value)
    if existing_keys & candidate_keys:
        return True, ""
    if source_paths_of(meta.get("sources")) & source_paths_of(candidate.get("sources")):
        return True, ""
    return False, "identity_mismatch"


def lookup_identity(candidate: dict, identity_index: dict[str, set[Path]]) -> tuple[str, list[Path]]:
    for reason, values in candidate_identity_tiers(candidate):
        matches = set()
        for value in values:
            for key in identity_keys(value):
                matches.update(identity_index.get(key, set()))
        if matches:
            return reason, sorted(matches)
    return "", []


def classify_explicit_target(raw: str, wiki_root: Path, suggested_type: str) -> tuple[str, Path | None]:
    """Decide how to treat an extractor-provided targetPath.

    Extractors sometimes emit wiki-root-relative paths (``concepts/foo.md``)
    instead of project-root-relative ones (``.auto-context/wiki/concepts/foo.md``).
    Returns one of:
    - ("use", path): wiki-root-relative path, normalized under wiki_root
    - ("fallback", None): looks wiki-root-relative but untrusted (bad extension,
      type mismatch, escapes wiki_root) — ignore it and resolve by identity
    - ("legacy", None): treat as project-root-relative (original behavior)
    """
    segments = [s for s in raw.replace("\\", "/").split("/") if s and s != "."]
    if raw.startswith("/") or not segments:
        return "legacy", None
    if any(s == ".." or s.startswith(".") for s in segments):
        return "legacy", None
    if segments[0] not in TYPE_DIR_NAMES:
        return "legacy", None
    if not segments[-1].endswith(".md"):
        return "fallback", None
    if segments[0] != TYPE_DIRS.get(suggested_type, "concepts"):
        return "fallback", None
    target = wiki_root.joinpath(*segments).resolve()
    try:
        target.relative_to(wiki_root)
    except ValueError:
        return "fallback", None
    return "use", target


def resolve_target(root: Path, wiki_root: Path, candidate: dict, suggested_type: str, identity_index: dict[str, set[Path]]) -> tuple[Path | None, str, list[Path]]:
    raw_target = candidate.get("targetPath")
    if isinstance(raw_target, str) and raw_target.strip():
        outcome, normalized = classify_explicit_target(raw_target.strip(), wiki_root, suggested_type)
        if outcome == "use":
            return normalized, "explicit", []
        if outcome == "legacy":
            target = (root / raw_target).resolve()
            try:
                target.relative_to(wiki_root)
            except ValueError:
                return None, "unsafe", []
            return target, "explicit", []
        # "fallback": untrusted wiki-relative targetPath — resolve by identity instead
    match_reason, matches = lookup_identity(candidate, identity_index)
    if len(matches) == 1:
        return matches[0], match_reason, matches
    if len(matches) > 1:
        return None, f"ambiguous_{match_reason}", matches
    title = str(candidate.get("title") or "wiki-page")
    slug = re.sub(r"[^A-Za-z0-9가-힣]+", "-", title.lower()).strip("-") or "wiki-page"
    target = (wiki_root / TYPE_DIRS.get(suggested_type, "concepts") / f"{slug}.md").resolve()
    try:
        target.relative_to(wiki_root)
    except ValueError:
        return None, "unsafe", []
    return target, "slug", []


def redact(text: str) -> tuple[str, list[str]]:
    redacted = False
    result = text
    for pattern in SECRET_LITERAL_PATTERNS:
        if pattern.search(result):
            redacted = True
            result = pattern.sub("[REDACTED]", result)

    def replace_keyword(match: re.Match) -> str:
        nonlocal redacted
        if is_non_secret_value(match.group("value")):
            return match.group(0)
        redacted = True
        return "[REDACTED]"

    result = SECRET_KEYWORD_PATTERN.sub(replace_keyword, result)
    return result, ["secret_like"] if redacted else []


def source_hash(candidate: dict) -> str:
    identity = clean_canonical_key(candidate.get("canonicalKey")) or str(candidate.get("title") or "")
    stable = {
        "identity": identity,
        "summary": candidate.get("summary"),
        "sources": candidate.get("sources"),
    }
    return hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def source_body_hash(text: str) -> str:
    """Content hash of the *bounded* source text that was fed to the extractor.

    verify-skipped.jsonl suppression keys on this, so both sides must hash the
    same bytes: wiki_compile_worker hashes read_source_bounded() output and
    wiki_verify_worker hashes the same maxSourceChars-bounded slice. Exact
    content, no normalization — any real source edit must retry, and whitespace
    is cheap to hash correctly here (unlike wiki_dedup_scan.body_hash, which
    normalizes because it compares two independently authored pages).
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def lint_candidate(candidate: dict, target: Path | None, max_lines: int) -> dict:
    findings = []
    title = str(candidate.get("title") or "").strip()
    summary = str(candidate.get("summary") or "").strip()
    if not title:
        findings.append("missing_title")
    if not summary:
        findings.append("missing_summary")
    if target is None:
        findings.append("unsafe_target_path")
    if TRANSCRIPT_RE.search(summary):
        findings.append("transcript_like")
    if has_secret_like(summary) or has_secret_like(title):
        findings.append("secret_like")
    if len(summary.splitlines()) > max_lines:
        findings.append("too_many_lines")
    return {"verdict": "clean" if not findings else "reject", "findings": findings}


def append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def effort_audit(candidate: dict) -> dict:
    raw = candidate.get("_qmd") if isinstance(candidate.get("_qmd"), dict) else {}
    effort = raw.get("reasoningEffort") if isinstance(raw.get("reasoningEffort"), dict) else {}
    return {"reasoningEffort": qmd_config.reasoning_effort_audit(effort)}


def ledger_writable(path: Path) -> bool:
    """이 원장에 append할 수 있는가 — **유료 host CLI 호출 전에** 확인하는 preflight.

    fail-closed 원장(쓸 수 없으면 그 판정을 적용하지 않는다)은 옳지만 **종점이 없으면
    과금 루프**가 된다: 유료 판정 1회 → 원장 쓰기 실패 → 작업 보존 → 다음 run 재판정(유료).
    cooldown이나 억제 마커로 끊는 방법도 있지만 그 파일들 역시 같은 디렉터리에 쓰므로
    "아무것도 못 쓰는 상태"에서는 함께 실패한다. **가장 확실한 종점은 유료 호출을 아예
    하지 않는 것**이고, 그래서 판정 전에 여기서 막는다(호출 0회 = 루프 없음).

    **원장 파일을 만들지는 않는다.** 기록이 없었으면 파일도 없어야 한다(원장의 존재 자체가
    "이 프로젝트에서 그 일이 있었다"로 읽히고 기존 테스트도 그 계약을 고정한다). 그래서
    부모 디렉터리에 임시 파일을 만들어 지우는 방식으로 쓰기 가능성만 확인하고(디렉터리
    부재·권한·ENOSPC를 모두 잡는다), 목표 경로가 이미 있으면 **일반 파일인지** 따로 본다
    (디렉터리를 원장 경로로 적은 경우가 실측된 실패 모드다).

    `wiki_verify_worker`(삭제 감사·inconclusive 억제 원장)와 `wiki_dedup_scan`
    (`dedup-skipped.jsonl` 억제 원장)이 같은 판정을 쓴다 — 유료 호출 전 게이트가 두 벌이
    되면 한쪽만 고쳐지고 다른 쪽에서 같은 루프가 남는다(실제로 그렇게 재발했다).
    """
    try:
        if path.exists() and not path.is_file():
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        probe = path.with_name(f".{path.name}.probe-{os.getpid()}")
        with open(probe, "w", encoding="utf-8"):
            pass
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def read_jsonl(path: Path) -> list[dict]:
    """append-only 로그/큐 읽기. 못 읽는 줄은 **건너뛴다**(fail-open).

    `errors="replace"`인 이유: 이 파일들은 여러 프로세스가 append하는 운영 파일이라 한
    바이트만 깨져도 전체 읽기가 UnicodeDecodeError로 죽었다 — 실측 사고는 원장이 비UTF-8일
    때 `wiki_source_repair --list`가 traceback으로 뜨지 못한 것이다(원장이 깨졌을 때 복구
    도구가 못 뜨는 것이 최악의 조합). 대체 문자는 그 줄의 JSON 파싱만 실패시키고 아래
    루프가 그 줄을 건너뛰므로, 손상은 손상된 줄에만 국한된다.
    """
    if not path.exists():
        return []
    rows = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


LOG_MAX_BYTES = 256 * 1024


def trim_jsonl(path: Path, max_bytes: int = LOG_MAX_BYTES) -> None:
    """append-only 로그의 무한 누적 방지: 상한 초과 시 최근 절반만 유지.
    순수 로그(candidates/dedup-deleted/verify-log/extractor.log — 단 extractor.log는 JSONL이 아닌 유일한 소비자)용 — 한 줄 유실은 무해.
    correctness가 걸린 파일(manifest)엔 쓰지 말 것 — compact_manifest 사용.

    쓰기는 원자적이다. "절반을 의도적으로 버린다"와 "쓰기가 실패해 임의 지점에서
    잘린다"는 다르다 — 후자의 최악은 첫 write에서 실패해 로그가 통째로 비는 것이고,
    그건 이 함수가 허용한 유실이 아니다(update_index와 같은 read-modify-write 클래스)."""
    try:
        if path.stat().st_size <= max_bytes:
            return
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        # **의도적으로 반환값을 보지 않는다**(분류: 실패 방향이 안전). 원자적 쓰기라 실패해도
        # 원본 로그가 온전하고(잘리지 않는다) 할 일은 "다음 호출에서 다시 시도"뿐이다.
        # 실패를 표면화할 채널도 없다(이 함수는 로그 쓰기 경로 안에서 불린다).
        # 분류 기준은 CLAUDE.md "쓰기 반환값" 항목 3번.
        write_text_atomic(path, "".join(lines[len(lines) // 2:]))
    except OSError:
        pass


@wiki_mutation_lock.guard_path
def write_text_atomic(path: Path, text: str) -> bool:
    """기존 파일 덮어쓰기의 **단일 원자적 쓰기 경로**(카드·wiki 산출물 공용).

    `write_text`는 truncate 후 write다. 쓰기가 중간에 실패하면(ENOSPC·quota·EFBIG)
    호출자는 예외/실패를 받는데 **파일은 이미 잘려 있다**. 실측 두 건:
      - `wiki_source_repair`(사람 호출): 9.6MB `verified` 카드 → 2048B
      - `patch_frontmatter_fields`(**자동** 경로): 40054B 카드 → 2048B
    자동 경로가 더 나쁘다 — verify worker가 `status: verified`를 스탬프하다 실패하면
    예외가 `run()` 밖으로 나가 job은 requeue되지만, 다음 회차 `card_state`가 **절단본**을
    읽어 `changed_during_verify`로 skip한다. 사람이 개입하지 않으므로 조용한 영구 손상이다.
    카드가 그 지식의 유일한 기록일 수 있다는 이유로 자동 삭제를 금지한 정책과 정반대다.

    임시파일은 **같은 디렉터리**에 만든다(다른 파일시스템이면 rename이 원자적이지 않다).
    원본이 있으면 **권한을 이식한다** — `os.replace`는 임시파일 권한(umask 기준 0644)을
    남기므로, 이식하지 않으면 0600 카드가 0644로 **넓어지는** 회귀가 된다.
    """
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            shutil.copymode(path, tmp)
        except OSError:
            # 원본이 없거나(신규 생성) 권한을 못 읽는 경우 — 기본 권한으로 진행한다.
            pass
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def write_jsonl_atomic(path: Path, rows: list[dict]) -> None:
    """temp + os.replace 원자적 재작성 (부분 쓰기/유실 방지)."""
    tmp = path.with_suffix(path.suffix + ".compact.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


# Manifest action written by wiki_verify_worker when IT deleted the card
# (verify.onFail / verify.onInconclusive = delete). Delete-detection below reads a
# missing file as "the user threw this card away" and tombstones it forever; a
# machine deletion must not mean that, or "fix the source and it regenerates" —
# the whole premise of deleting instead of keeping contested cards — never holds.
MACHINE_DELETE_ACTION = "verify-deleted"


def same_generated_identity(row: dict, record: dict) -> bool:
    if row.get("targetPath") and row.get("targetPath") == record.get("targetPath"):
        return True
    if record.get("targetResolution") == "explicit":
        return False
    if row.get("sourceHash") and row.get("sourceHash") == record.get("sourceHash"):
        return True
    canonical_key = record.get("canonicalKey")
    if canonical_key and row.get("canonicalKey") == canonical_key:
        return True
    return False


def _compact_stamp_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".compact-stamp")


def _read_compact_stamp(path: Path) -> int | None:
    try:
        return int(_compact_stamp_path(path).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _write_compact_stamp(path: Path, size: int) -> None:
    try:
        _compact_stamp_path(path).write_text(str(size), encoding="utf-8")
    except OSError:
        pass


COMPACT_STAMP_GROWTH_RATIO = 1.25


def compact_manifest(path: Path, max_bytes: int = LOG_MAX_BYTES) -> None:
    """generated-manifest 무한 누적 방지. size trim은 특정 카드 엔트리를 통째
    날려 삭제감지(previous 조회)를 깨므로 쓰지 않고, same_generated_identity로
    서로 매칭되는 오래된 중복만 접는다 — 각 identity의 최신 엔트리는 원순서로
    보존해 previous[-1]/previousStatus 계약을 유지. 임계 초과 시에만 원자적
    replace. worst case(동시 append와 cross-process race로 엔트리 1개 유실)는
    그 카드가 다음 재컴파일 때 재생성되며 self-heal.

    same_generated_identity(row, record)의 대칭 합집합 관계
    (same(i,j) or same(j,i))는 정리하면 "targetPath 일치, 또는 (i·j 중
    하나라도 explicit이 아닐 때) sourceHash/canonicalKey 일치"이므로, 최신
    행부터 역순으로 훑으며 seen-set을 유지하는 O(n) 스캔으로 치환할 수
    있다. seen_hashes_any/seen_keys_any는 이후(더 오래된) explicit이 아닌
    행이 매치할 대상(최신 쪽 explicit 여부 무관), seen_*_nonexplicit는 이후
    explicit 행이 매치할 대상(최신 쪽도 non-explicit이어야 함)이다.

    "압축해도 줄지 않는" 상태의 재시도 폭주를 막기 위해 마지막 처리 후
    파일 크기를 사이드카(.compact-stamp)에 기록해두고, 그 이후 크기가
    COMPACT_STAMP_GROWTH_RATIO 이상 자라기 전까지는 재스캔을 건너뛴다.
    사이드카 유실은 스캔 1회 추가로만 이어지므로 self-heal 성질을 해치지
    않는다."""
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size <= max_bytes:
        return
    stamp = _read_compact_stamp(path)
    if stamp is not None and stamp > 0 and size < stamp * COMPACT_STAMP_GROWTH_RATIO:
        return
    rows = read_jsonl(path)
    n = len(rows)
    keep = [True] * n
    seen_target_paths: set = set()
    seen_hashes_any: set = set()
    seen_keys_any: set = set()
    seen_hashes_nonexplicit: set = set()
    seen_keys_nonexplicit: set = set()
    for i in range(n - 1, -1, -1):  # 최신 -> 과거 순
        row = rows[i]
        target_path = row.get("targetPath")
        source_hash = row.get("sourceHash")
        canonical_key = row.get("canonicalKey")
        is_explicit = row.get("targetResolution") == "explicit"
        if target_path and target_path in seen_target_paths:
            keep[i] = False
        elif is_explicit:
            if (source_hash and source_hash in seen_hashes_nonexplicit) or (
                canonical_key and canonical_key in seen_keys_nonexplicit
            ):
                keep[i] = False
        else:
            if (source_hash and source_hash in seen_hashes_any) or (
                canonical_key and canonical_key in seen_keys_any
            ):
                keep[i] = False
        if target_path:
            seen_target_paths.add(target_path)
        if source_hash:
            seen_hashes_any.add(source_hash)
            if not is_explicit:
                seen_hashes_nonexplicit.add(source_hash)
        if canonical_key:
            seen_keys_any.add(canonical_key)
            if not is_explicit:
                seen_keys_nonexplicit.add(canonical_key)
    kept = [rows[i] for i in range(n) if keep[i]]
    if len(kept) != n:
        # **의도적으로 결과를 보지 않는다**(분류: 실패 방향이 안전). write_jsonl_atomic은
        # 반환값이 없고 OSError를 내부에서 삼키는데, 실패하면 압축이 일어나지 않을 뿐
        # 원본 manifest는 온전하다(temp + os.replace). 아래 compact-stamp가 "압축해도 줄지
        # 않는" 상태의 재시도 폭주를 막으므로 다음 회차 재시도로 자연히 수렴한다.
        write_jsonl_atomic(path, kept)
    try:
        new_size = path.stat().st_size
    except OSError:
        new_size = size
    _write_compact_stamp(path, new_size)


def yaml_scalar(value) -> str:
    return yaml_scalars.dump(value)


def frontmatter_patch_scalar(key: str, value) -> str:
    text = str(value)
    if key == "status" and text in qmd_config.WIKI_STATUSES and re.fullmatch(r"[A-Za-z0-9_-]+", text):
        return text
    return yaml_scalar(value)


def source_flow_entries(sources) -> list:
    """`sources` 후보 목록 → frontmatter에 쓸 flow mapping 표기 목록.

    **`path` 키가 있으면 비어 있지 않은 문자열이어야 하고, 아니면 그 항목을 버린다.**
    candidate는 extractor(모델) 출력이라 `{"kind": "file", "path": False}` 같은 값이 올 수
    있고, 그러면 표기가 `path: false`로 나가 읽는 쪽에서 문자열 `"false"`가 된다 — `false`
    라는 이름의 파일이 있으면 그것이 원문으로 주입되고 진단에는 정상(drop 0)으로 남는다.
    `"false"`로 강제하는 것은 무의미한 경로를 만드는 것이므로 항목 자체를 버린다.
    **`path`가 애초에 없는 항목은 그대로 쓴다** — `{kind: "session", ref: "session:local"}`
    처럼 파일이 아닌 출처 레코드가 정상 형태다(수동 compile 경로 `wiki_extract`가 이걸
    쓴다). 주입 대상이 아닐 뿐(읽기 쪽이 kind/path로 거른다) 카드의 출처 기록이므로
    지우면 감사 추적이 사라진다.
    비문자 **값 일반**의 방어는 `yaml_scalars.dump_flow_mapping`에 있다(키가 조용히 빠진다).
    여기서 항목 단위로 한 번 더 보는 이유는, `path` 키만 조용히 빠지면 "소스가 있다고
    주장하지만 경로가 없는" 레코드가 카드에 남기 때문이다.
    남는 항목이 없으면 호출부가 `{kind: unknown}` 센티넬을 쓴다.
    """
    entries = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        if "path" in source:
            path_value = source.get("path")
            if not isinstance(path_value, str) or not path_value.strip():
                continue
        # 값은 yaml_scalar로 인용되지만 **키도 모델이 준다** — 개행이 든 키는 flow mapping을
        # 벗어나 새 줄을 만들 수 있다. 키는 닫힌 스키마(kind/path/collection)이므로 안전한
        # 식별자만 통과시킨다. emit/parse 쌍은 yaml_scalars가 SSOT다(읽는 쪽은 recall).
        flow = yaml_scalars.dump_flow_mapping(source)
        if flow:
            entries.append(flow)
    return entries


def authoritative_sources(candidate: dict) -> list:
    """큐 잡이 아는 **실제** 소스 목록 — 모델이 밀어낼 수 없는 검증 근거.

    `sources`는 extractor(모델) 출력이 섞인 목록이고 verifier는 그 목록 앞에서
    `MAX_SOURCES`(3)개만 읽는다. 실제 소스를 앞에 두는 것만으로도 밀려나지 않지만,
    그 보장이 **목록 순서**라는 암묵 계약이면 다음 편집에서 조용히 깨지고 그때 깨지는
    것은 "검증됐다"는 주장 자체다. 그래서 사실을 별도 필드로 못박는다.

    **frontmatter로는 나가지 않는다** — `markdown_page`는 `sources`만 읽으므로 이 값이
    카드에 새지 않고, 따라서 `dump_flow_mapping`의 키 화이트리스트(일반 식별자)에
    새 키가 노출되는 문제도 없다. 값은 여기서 닫힌 스키마(kind/path/collection)로
    재구성한다 — 큐 잡을 손으로 고친 경우에도 모델 제공 키가 그대로 흐르지 않는다.
    비면 키를 만들지 않으므로 수동 compile 경로(`wiki_extract`)의 잡 형태는 불변이다.

    `kind: "file"` 판정은 `recall.SOURCE_KIND_FILE`·`wiki_verify_worker.load_sources`와
    **같은 집합이어야 한다**(recall.py의 그 상수 주석이 이 계약을 기록한다).
    """
    raw = candidate.get("authoritativeSources")
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict) or item.get("kind") != "file":
            continue
        path = item.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        collection = item.get("collection")
        out.append({
            "kind": "file",
            "path": path,
            "collection": collection if isinstance(collection, str) else "",
        })
    return out


def source_revisions(candidate: dict) -> list[dict]:
    """Return the complete closed-schema compiler provenance, or no proof.

    ``yaml_scalars`` owns the schema.  Rejecting the whole list when one sibling
    is malformed prevents a partial authoritative source set from looking
    complete.
    """
    raw = candidate.get("sourceRevisions")
    if not isinstance(raw, list) or not raw:
        return []
    normalized = [yaml_scalars.normalize_source_revision(item) for item in raw]
    if any(item is None for item in normalized):
        return []
    return normalized


def markdown_page(candidate: dict, summary: str, status: str, redactions: list[str], h: str) -> str:
    created = today()
    # candidate는 **extractor(모델) 출력**이다. frontmatter로 나가는 모든 모델 제공 값은
    # 닫힌 집합으로 검증하거나 yaml_scalar를 거쳐야 한다 — raw로 방출하면 개행 하나로
    # top-level 키를 위조할 수 있다(`trigger="promote\nstatus: verified"` → PyYAML은
    # status를 verified로 읽고, wiki_compile.parse_frontmatter는 ok=False로 fail-close해
    # 그 카드의 verify·dedup이 영구히 멈춘다).
    # type·confidence·status는 닫힌 집합이라 unquoted를 유지한다(TYPE_DIRS 조회와
    # frontmatter_patch_scalar의 status 표기 정책이 그 형태를 전제한다).
    suggested_type = candidate.get("suggestedType") if candidate.get("suggestedType") in ALLOWED_TYPES else "concept"
    confidence = candidate.get("confidence") if candidate.get("confidence") in ALLOWED_CONFIDENCE else "medium"
    status = status if status in qmd_config.WIKI_STATUSES else "generated"
    sources = candidate.get("sources") if isinstance(candidate.get("sources"), list) else []
    triggers = [candidate.get("trigger")] if isinstance(candidate.get("trigger"), str) else []
    canonical_key = clean_canonical_key(candidate.get("canonicalKey"))
    aliases = clean_aliases(candidate.get("aliases"))
    # title은 한 줄 라벨이므로 여기서 접는다(모든 호출자를 덮는 단일 직렬화 지점).
    lines = [
        "---",
        f"title: {yaml_scalar(yaml_scalars.fold_inline(candidate.get('title') or 'Untitled'))}",
    ]
    if canonical_key:
        lines.append(f"canonicalKey: {yaml_scalar(canonical_key)}")
    if aliases:
        lines.append("aliases:")
        for alias in aliases:
            lines.append(f"  - {yaml_scalar(alias)}")
    else:
        lines.append("aliases: []")
    lines.extend([
        f"type: {suggested_type}",
        f"status: {status}",
        f"created: {created}",
        f"updated: {created}",
        "createdBy: qmd-auto-context",
        f"confidence: {confidence}",
        "sources:",
    ])
    source_lines = [f"  - {flow}" for flow in source_flow_entries(sources)]
    lines.extend(source_lines if source_lines else ["  - {kind: unknown}"])
    revisions = yaml_scalars.dump_source_revisions(source_revisions(candidate))
    if revisions:
        lines.append("sourceRevisions:")
        lines.extend(f"  - {revision}" for revision in revisions)
    lines.append("triggers:")
    if triggers:
        for trigger in triggers:
            # 모델 제공 값 — 반드시 인용한다(raw면 `promote\nstatus: verified`로
            # top-level 키를 위조할 수 있었다).
            lines.append(f"  - {yaml_scalar(trigger)}")
    lines.append("redactions:")
    if redactions:
        for item in redactions:
            # 현재는 내부 리터럴(`secret_like`)뿐이지만 같은 경로를 쓰므로 함께 인용한다.
            lines.append(f"  - {yaml_scalar(item)}")
    lines.extend([
        "---",
        "",
        wiki_markers.AUTO_DISCLAIMER,
        "",
        wiki_markers.AUTO_START_TEMPLATE.format(source_hash=h),
        "## Summary",
        summary,
        wiki_markers.AUTO_END,
        "",
    ])
    return "\n".join(lines)


def _frontmatter_sections(frontmatter: str) -> list[tuple[str, list[str]]] | None:
    """Split parsed frontmatter into top-level key sections, preserving bytes."""
    sections: list[tuple[str, list[str]]] = []
    for line in frontmatter.splitlines():
        if line.startswith(" "):
            if not sections:
                return None
            sections[-1][1].append(line)
            continue
        if ":" not in line:
            return None
        key = line.split(":", 1)[0].strip()
        if not key or any(existing == key for existing, _ in sections):
            return None
        sections.append((key, [line]))
    return sections


@wiki_mutation_lock.guard_path
def rewrite_generated_card(path: Path, old_text: str, generated_page: str) -> bool:
    """Atomically refresh trusted frontmatter and the managed block together.

    Human-maintained frontmatter/body outside the managed block survives.  The
    compiler-owned source lists, generated status, and verification proof are
    replaced/cleared in the same complete text passed to ``write_text_atomic``.
    """
    old_match = FRONTMATTER_RE.match(old_text)
    new_match = FRONTMATTER_RE.match(generated_page)
    old_blocks = list(AUTO_BLOCK_RE.finditer(old_text))
    new_blocks = list(AUTO_BLOCK_RE.finditer(generated_page))
    if old_match is None or new_match is None or len(old_blocks) != 1 or len(new_blocks) != 1:
        return False
    old_sections = _frontmatter_sections(old_match.group(1))
    new_sections = _frontmatter_sections(new_match.group(1))
    if old_sections is None or new_sections is None:
        return False
    new_by_key = {key: lines for key, lines in new_sections}
    required = {"status", "sources"}
    if not required.issubset(new_by_key):
        return False

    replaced: set[str] = set()
    merged: list[str] = []
    proof_keys = set(qmd_config.VERIFY_PROOF_FIELDS)
    replace_sources = new_by_key["sources"] != ["sources:", "  - {kind: unknown}"]
    for key, lines in old_sections:
        # 줄 span 은 옛 본문을 검수한 verifier 인용의 위치다 — 재생성된 본문에 남기면 안 된다.
        if key in proof_keys or key in {"sourceRevisions", "reviewed", yaml_scalars.SOURCE_SPANS_KEY}:
            continue
        if key == "status":
            merged.extend(new_by_key["status"])
            replaced.add("status")
            continue
        if key == "sources":
            merged.extend(new_by_key["sources"] if replace_sources else lines)
            if "sourceRevisions" in new_by_key:
                merged.extend(new_by_key["sourceRevisions"])
            replaced.update({"sources", "sourceRevisions"})
            continue
        merged.extend(lines)
    for key in ("status", "sources"):
        if key not in replaced:
            merged.extend(new_by_key[key])
    if "sourceRevisions" in new_by_key and "sourceRevisions" not in replaced:
        merged.extend(new_by_key["sourceRevisions"])

    body = old_text[old_match.end():]
    if len(list(AUTO_BLOCK_RE.finditer(body))) != 1:
        return False
    body = AUTO_BLOCK_RE.sub(new_blocks[0].group(0), body, count=1)
    rewritten = "---\n" + "\n".join(merged) + "\n---\n" + body
    return write_text_atomic(path, rewritten)


@wiki_mutation_lock.guard_path
def insert_source_revisions(path: Path, revisions: list) -> bool:
    """Add the compiler-owned ``sourceRevisions`` block to a card that has none.

    Kept out of ``patch_frontmatter_fields`` because that function is a scalar
    patcher by construction: ``frontmatter_patch_scalar`` runs ``str(value)``
    through ``yaml_scalars.dump``, so a list lands as one quoted scalar line, and
    its ``None`` removal path drops only the ``key:`` line and would orphan a
    block value's indented children.  Teaching it a second shape puts its only
    scalar caller (``stamp_verification``) behind a branch it does not need.  Block-shaped frontmatter surgery already has a
    primitive here — ``_frontmatter_sections`` — and ``rewrite_generated_card``
    uses it exactly this way.  Serialization stays in ``yaml_scalars``.

    **Fail-closed on any card that already declares the key, parseable or not.**
    ``recall.frontmatter_source_revisions`` reports a malformed block as "no
    proof", so keying off the parsed value would append a SECOND top-level
    header — which that same parser rejects as a duplicate declaration, leaving
    the card corrupted and still untrusted.  Only the structural key scan of
    ``_frontmatter_sections`` separates "absent" from "present but unusable".

    The block goes directly after ``sources:`` so a backfilled card is
    byte-shaped like one ``markdown_page`` writes; a card with no ``sources``
    section has no provenance to extend and is refused.
    """
    emitted = yaml_scalars.dump_source_revisions(revisions)
    if not emitted:
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    match = FRONTMATTER_RE.match(text)
    if not match:
        return False
    sections = _frontmatter_sections(match.group(1))
    if sections is None:
        return False
    keys = {key for key, _ in sections}
    if "sourceRevisions" in keys or "sources" not in keys:
        return False
    merged: list[str] = []
    for key, lines in sections:
        merged.extend(lines)
        if key == "sources":
            merged.append("sourceRevisions:")
            merged.extend(f"  - {revision}" for revision in emitted)
    patched = text[: match.start(1)] + "\n".join(merged) + text[match.end(1):]
    # 원자적 쓰기 — 이 쓰기는 카드에 recall 신뢰를 부여하므로 반환값을 삼키면
    # 호출부가 "신뢰 부여됨"으로 보고하는데 카드는 그대로인 상태가 된다.
    return write_text_atomic(path, patched)


@wiki_mutation_lock.guard_path
def patch_frontmatter_fields(path: Path, updates: dict, blocks: dict | None = None,
                             expected_text: str | None = None) -> bool:
    """Rewrite only the named top-level scalar frontmatter keys in place.

    Leaves every other frontmatter line and the managed body untouched.

    A value of **None removes the key** instead of writing it. Blanking a proof
    field (`verifiedBy: ""`) leaves a residue that still reads as "this card was
    machine-verified once" — measured on 5 live cards that had been reset to
    `generated`. Removal is the honest state for "no verification on record".

    `blocks` maps a block-list key to already-serialized entries (`None` = remove).
    Every top-level declaration of that key — duplicates and quoted spellings
    included — is dropped together with its indented children, so no orphan
    child line survives; non-empty entries are appended as one new block in the
    same atomic write. **Any `status` update also removes `sourceSpans`** unless
    the caller passes it: spans locate quotes of one verification, and a status
    change without a fresh list would leave them describing a different verdict.
    `expected_text` refuses the write when the file differs from what the caller
    validated.
    """
    blocks = dict(blocks or {})
    if "status" in updates:
        blocks.setdefault(yaml_scalars.SOURCE_SPANS_KEY, None)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if expected_text is not None and text != expected_text:
        return False
    match = FRONTMATTER_RE.match(text)
    if not match:
        return False
    lines = match.group(1).splitlines()
    seen = set()
    new_lines = []
    in_block = False
    for line in lines:
        if in_block:
            if not line.strip() or line[0].isspace():
                continue
            in_block = False
        # 블록 헤더 판정은 읽는 쪽(`yaml_scalars.closed_block_entries`)과 같은 규칙이다.
        if line and not line[0].isspace() and yaml_scalars.load(line.split(":", 1)[0].strip()) in blocks:
            in_block = True
            continue
        key = None
        if line and not line.startswith(" ") and ":" in line:
            key = line.split(":", 1)[0].strip()
        if key in updates:
            seen.add(key)
            if updates[key] is None:
                continue
            new_lines.append(f"{key}: {frontmatter_patch_scalar(key, updates[key])}")
        else:
            new_lines.append(line)
    for key, value in updates.items():
        if key not in seen and value is not None:
            new_lines.append(f"{key}: {frontmatter_patch_scalar(key, value)}")
    for key, entries in blocks.items():
        if entries:
            new_lines.append(f"{key}:")
            new_lines.extend(f"  - {entry}" for entry in entries)
    new_frontmatter = "\n".join(new_lines)
    patched = text[: match.start(1)] + new_frontmatter + text[match.end(1) :]
    # 원자적 쓰기 — 실패해도 카드는 원본 그대로다(자동 경로라 절단되면 아무도 모른다).
    return write_text_atomic(path, patched)


@wiki_mutation_lock.guard_path
def stamp_verification(path: Path, status: str, engine: str, mode: str,
                       body_hash: str | None, source_spans: list | None = None) -> bool:
    """Write a machine-review outcome — status AND all three proof fields together.

    Single writer so a status can never land without its proof. The live corpus shows
    what the split costs: 27 `status: verified` cards carry no `verifiedAt` at all
    (all of them `verifiedBy: agent-full-source`, a string this codebase never
    writes — a hand/agent backfill that stamped the status and skipped the proof).

    `verifiedMode` records WHO checked relative to who wrote (see
    wiki_verify_worker.VERIFIED_MODE_*): a card verified by the same engine that
    produced it is a self-review, and self-preference bias makes that a weaker
    claim than a cross-engine pass. The field is absent on every card verified
    before this existed — absence therefore means "unknown, most likely self"
    (measured 655/688), never "cross-engine".

    `source_spans` (verifier-cited line spans, `source_spans.derive_spans`) are
    written in the **same atomic write** as the status, and only on `verified`.
    Every stamp without spans removes an existing list — contested, a pass with
    no locatable quote, and every other writer of this function alike — so a
    span list always belongs to the verdict stamped next to it. The list must be
    complete and bound to this card's own `sourceRevisions`; otherwise nothing is
    written (no partial proof).
    """
    emitted = None
    expected_text = None
    if source_spans:
        emitted = yaml_scalars.dump_source_spans(source_spans)
        if status != "verified" or not emitted:
            return False
        try:
            expected_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return False
        import source_spans as spans_module
        if not spans_module.bound_to_revisions(
                yaml_scalars.load_source_spans(emitted) or [],
                spans_module.card_revisions(expected_text)):
            return False
    return patch_frontmatter_fields(path, {
        "status": status,
        "verifiedBy": str(engine or "unknown"),
        "verifiedAt": now_iso(),
        "verifiedMode": mode if mode in qmd_config.VERIFIED_MODES else qmd_config.VERIFIED_MODE_UNKNOWN,
        # 검증기가 실제로 읽은 본문의 지문. **호출자가 넘긴다** — 여기서 파일을 다시 읽어
        # 계산하면 검수 중에 본문이 바뀐 경우 "검증되지 않은 새 본문"에 verified 가 찍힌다.
        # 호출자 값이면 그 경우 디스크와 불일치해 recall 이 fail-closed 로 거른다.
        # None 이면 키를 쓰지 않는다(`patch_frontmatter_fields` 규약) = 결속 없는 legacy
        # 형태이고, 그것이 필요한 경로는 지금 없다(양 호출부 모두 값을 넘긴다).
        "verifiedBodyHash": body_hash,
    }, blocks={yaml_scalars.SOURCE_SPANS_KEY: emitted}, expected_text=expected_text)


@wiki_mutation_lock.guard_path
def update_index(wiki_root: Path, target: Path, title: str) -> bool:
    """index.md에 카드 1줄을 추가한다. **read-modify-write이므로 원자적으로 쓴다.**

    `append_log`와의 비대칭이 여기 있다 — 둘 다 wiki 루트의 누적 파일인데 하나만
    원자적 쓰기가 필요하다:
      - index는 **전체를 읽어 전체를 다시 쓴다**(중복 줄 방지 검사 때문에). 즉 truncate
        후 write이고, 실패하면 그때까지 누적된 인덱스 전부가 잘린다. 라이브 index.md는
        106,717B(카드 1장당 1줄 ≈ 850줄)이고 **재생성 경로가 없다** — 잘리면 아무것도
        복구하지 않는다. 카드가 생길 때마다(compile 경로) 호출되므로 노출도 높다.
      - `append_log`는 `open("a")` **append 모드**라 기존 내용을 truncate하지 않는다.
        164KB인데도 안전한 이유가 이것이다. 실패는 마지막 줄이 안 붙는 것으로 끝난다.
        (두 함수의 최초 헤더 생성도 원자 게시해 부분 파일을 남기지 않는다.)
    반환값은 "인덱스가 카드와 어긋나지 않았는가"다 — 인덱스는 캐논이 아니라 항법이므로
    실패가 카드 쓰기만큼 치명적이지는 않지만, 조용히 실패하면 카드와 어긋난 채 남는다.
    """
    index = wiki_root / "index.md"
    if not index.exists():
        ensure_project_file(index, "# Auto-context Wiki Index\n\n")
    rel = target.relative_to(wiki_root).as_posix()
    line = f"- {rel} - {title}\n"
    text = index.read_text(encoding="utf-8")
    if rel in text:
        return True
    return write_text_atomic(index, text.rstrip() + "\n" + line)


@wiki_mutation_lock.guard_path
def remove_from_index(wiki_root: Path, target: Path) -> bool:
    """index.md에서 카드 1줄을 제거한다. **`update_index`의 역연산이고 같은 이유로 원자적이다.**

    이 함수가 없던 동안 인덱스는 append-only였다 — 카드가 지워져도(dedup merge, 기계 검수
    삭제) 줄이 남아 인덱스가 실제 카드보다 계속 커졌다(실측: 항목 1,629개 중 253개가 없는
    파일을 가리켰다). 이게 조용히 넘어가지 않는 이유는 `wiki_compile_worker.orientation()`이
    이 파일 **앞 12,000자**를 컴파일 워커의 항법 정보로 넘기기 때문이다. 그 창 안의 죽은
    줄은 워커에게 "이미 있는 카드"로 보인다.

    **줄 매칭에 `rel in text`를 쓰지 않는다.** `update_index`의 중복 검사가 그 부분 문자열
    비교인데, 제거에 같은 방식을 쓰면 `foo.md`를 지우라는 요청이 `foo-bar.md` 줄이나 경로를
    본문에 포함한 제목 줄까지 지운다. 여기서는 `update_index`가 쓰는 형식 그대로
    `- {rel} - `로 시작하는 줄만 지운다.

    반환값은 `update_index`와 대칭이다 — "인덱스가 카드와 어긋나지 않았는가". 줄이 애초에
    없었거나 index.md 자체가 없으면 True다(어긋난 게 없다). False는 쓰기 실패뿐이다.
    인덱스는 캐논이 아니라 항법이므로 호출자는 실패에 fail-closed하지 않는다.
    """
    index = wiki_root / "index.md"
    if not index.exists():
        return True
    rel = target.relative_to(wiki_root).as_posix()
    prefix = f"- {rel} - "
    try:
        text = index.read_text(encoding="utf-8")
    except OSError:
        return False
    kept = [line for line in text.splitlines(keepends=True) if not line.startswith(prefix)]
    if len(kept) == len(text.splitlines(keepends=True)):
        return True
    return write_text_atomic(index, "".join(kept))


@wiki_mutation_lock.guard_path
def append_log(wiki_root: Path, action: str, target: Path, title: str) -> None:
    """log.md에 1줄 append. **원자적 쓰기가 필요 없다** — 사유는 `update_index` docstring."""
    log = wiki_root / "log.md"
    if not log.exists():
        ensure_project_file(log, "# Auto-context Wiki Log\n\n")
    rel = target.relative_to(wiki_root).as_posix()
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"- {now_iso()} {action} {rel} - {title}\n")


def find_wiki_collection(config: dict) -> tuple[str | None, str | None]:
    roles = qmd_config.role_map(config)
    paths = config.get("collectionPaths") if isinstance(config.get("collectionPaths"), dict) else {}
    for name in config.get("collections", []):
        if qmd_config.is_wiki_collection(roles, name):
            return name, paths.get(name, config.get("wikiPath", ".auto-context/wiki"))
    return None, None


def resolve_daemon_result_path(root: Path, wiki_root: Path, uri: str, collection: str) -> Path | None:
    if not isinstance(uri, str) or not uri:
        return None
    if uri.startswith("qmd://"):
        rest = uri[len("qmd://"):]
        if "/" not in rest:
            return None
        actual, rel = rest.split("/", 1)
        if actual != collection:
            return None
    elif collection and uri.startswith(f"{collection}/"):
        rel = uri[len(collection) + 1:]
    else:
        return None
    for candidate_root in (wiki_root, root):
        candidate_path = (candidate_root / rel).resolve()
        try:
            candidate_path.relative_to(wiki_root)
        except ValueError:
            continue
        if candidate_path.is_file():
            return candidate_path
    return None


def query_wiki_similar(daemon_url: str, collection: str, text: str, top_k: int, timeout: float) -> list[dict] | None:
    """Vector-search `text` against `collection`. Returns daemon `results` list, or
    None on any failure — caller must fail-open on None, never raise.

    Always queries with rerank=True: with rerank=False the daemon's `score`
    field is a reciprocal-rank value (1, 0.5, 0.33, ...) from result position,
    NOT a semantic similarity, which makes every caller's threshold comparison
    meaningless (rank-1 is always ~1.0 regardless of true similarity). Every
    caller here runs off an async path -- the compile worker's write-time
    semantic gate and similar-page lookup (backend_manager.sh forks the worker
    with `&`), the retroactive dedup scanner (update.sh's background embed
    subshell, once per 24h), or the manual wiki-compile skill -- so there is
    no synchronous per-edit caller left that needs to skip rerank for latency.
    """
    fixture_path = os.environ.get("QMD_QUERY_FIXTURE")
    if fixture_path:
        try:
            with open(fixture_path, "r", encoding="utf-8") as f:
                parsed = json.load(f)
            if not isinstance(parsed, dict):
                return None
            results = parsed.get("results", [])
        except (OSError, json.JSONDecodeError):
            return None
        return results if isinstance(results, list) else []
    if os.environ.get('QMD_SETUP_GUARD_FIXTURE') != '1':
        try:
            import qmd_shared_store
            qmd_shared_store.select()
        except (OSError, ValueError, KeyError, TypeError):
            return None
        import qmd_endpoint
        selected_url=qmd_endpoint.optional_url()
        if not selected_url or daemon_url != selected_url:
            return None
    # qmd's vec search rejects multi-line queries (store.js structuredSearch:
    # "queries must be single-line. Remove newline characters.") with a 500.
    # Card bodies are almost always multi-line, so passing them raw made every
    # caller (write-time semantic gate, retroactive dedup scan, similar-page
    # lookup) silently fail-open on any multi-line card -- the dominant cause of
    # near-duplicate proliferation. Collapse all whitespace to single spaces.
    single_line = " ".join(text.split())
    payload = {
        "searches": [{"type": "vec", "query": single_line}],
        "collections": [collection],
        "limit": max(1, top_k),
        "minScore": 0,
        "timeout": timeout,
        "rerank": True,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{daemon_url}/query",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
        parsed = json.loads(body)
        if not isinstance(parsed, dict):
            return None
        results = parsed.get("results", [])
        return results if isinstance(results, list) else []
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None


def shared_collection_owned(root: Path, config: dict, collection: str) -> bool:
    import wiki_selected_laya
    selected = wiki_selected_laya.selected(root)
    if selected is not None:
        return any(item['name'] == collection and item['role'] == 'wiki'
                   for item in selected[0]['collections'])
    if os.environ.get('QMD_SETUP_GUARD_FIXTURE') == '1':
        return True
    try:
        import qmd_shared_store
        return collection in qmd_shared_store.owned_collections(root, config)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _numeric_score(value) -> float:
    return value if isinstance(value, (int, float)) else 0


def query_wiki_candidates(
    root: Path, wiki_root: Path, config: dict, candidate: dict, summary: str, min_score: float
) -> list[tuple[Path, float]]:
    """Resolved (path, score) daemon hits at or above `min_score`, best first.

    Retrieval only. `min_score` narrows the candidate set; it must never stand in
    for a duplicate judgment (the daemon score is rank-bounded — see
    wiki_dedup_judge.py). Empty list covers "daemon down", "no hits", and
    "semanticDedup disabled" alike, so every caller fails open.
    """
    compile_cfg = config.get("compile") if isinstance(config.get("compile"), dict) else {}
    semantic_cfg = compile_cfg.get("semanticDedup") if isinstance(compile_cfg.get("semanticDedup"), dict) else {}
    if not semantic_cfg.get("enabled", True):
        return []
    collection, _ = find_wiki_collection(config)
    if not collection or not shared_collection_owned(root, config, collection):
        return []
    text = f"{candidate.get('title') or ''} {summary}".strip()
    if not text:
        return []
    import wiki_selected_laya
    selected = wiki_selected_laya.selected(root)
    if selected is not None:
        return [(path, score) for path, score, _ in
                wiki_selected_laya.wiki_matches(root, wiki_root, text,
                    qmd_config.DEDUP_TOP_K,
                    qmd_config.DEDUP_SIMILAR_PAGE_MAX_CHARS, bound=selected)
                if score >= min_score]
    import qmd_endpoint
    daemon_url = qmd_endpoint.optional_url()
    timeout = float(config.get("queryTimeout", 5.0) or 5.0)
    results = query_wiki_similar(daemon_url, collection, text, qmd_config.DEDUP_TOP_K, timeout)
    if not results:
        return []
    hits: list[tuple[Path, float]] = []
    for result in sorted(
        (r for r in results if isinstance(r, dict)),
        key=lambda r: _numeric_score(r.get("score", 0)),
        reverse=True,
    ):
        score = _numeric_score(result.get("score", 0))
        if score < min_score:
            continue
        matched = resolve_daemon_result_path(root, wiki_root, result.get("file", ""), collection)
        if matched is not None:
            hits.append((matched, score))
    return hits


def find_wiki_semantic_match(
    root: Path, wiki_root: Path, config: dict, candidate: dict, summary: str
) -> tuple[Path | None, float | None]:
    """Legacy score-threshold gate: (matched_path, score) for the top hit above
    `config.DEDUP_SCORE_THRESHOLD`, else (None, None). Used only when no LLM judge
    is available on this machine. 임계는 상수다 — daemon score가 순위 기반이라
    설정으로 조정할 의미가 없었고(wiki_dedup_judge 참조) judge-less 폴백의 동작을
    예전 기본값 그대로 동결하는 것이 목적이다."""
    threshold = qmd_config.DEDUP_SCORE_THRESHOLD
    hits = query_wiki_candidates(root, wiki_root, config, candidate, summary, threshold)
    if not hits:
        return None, None
    return hits[0]


def judge_new_page_duplicate(
    root: Path, wiki_root: Path, config: dict, compile_cfg: dict, candidate: dict, summary: str, target: Path
) -> tuple[Path | None, float | None, dict]:
    """Write-time duplicate check for a page about to be CREATED.

    Runs for every new page regardless of how its target path was resolved. Until
    2026-07-29 this gate only ran for `slug`-resolved targets, so a candidate whose
    extractor supplied an explicit `targetPath` bypassed dedup entirely — 130 of
    133 creations (97.7%) in the measured project, which is where the surviving
    near-duplicates came from.

    Returns (matched_path, score, info). matched_path is set only on a "duplicate"
    verdict; the caller then QUEUES the pair for review and never merges it itself.
    info["outcome"] is "ok"/"unavailable"/"transient" (see wiki_dedup_judge).
    """
    semantic_cfg = compile_cfg.get("semanticDedup") if isinstance(compile_cfg.get("semanticDedup"), dict) else {}
    engine = candidate.get("engine") if isinstance(candidate.get("engine"), str) else ""
    budget = wiki_dedup_judge.max_pairs_per_compile(compile_cfg)
    # Config-only availability check BEFORE the retrieval query: with no judge on
    # this machine the caller falls back to the legacy gate, and querying the
    # daemon here would only add latency.
    if budget <= 0 or not wiki_dedup_judge.is_available(compile_cfg, engine):
        return None, None, {"outcome": wiki_dedup_judge.OUTCOME_UNAVAILABLE, "reason": "judge_unavailable"}

    min_score = float(semantic_cfg.get("candidateMinScore", 0.3))
    hits = query_wiki_candidates(root, wiki_root, config, candidate, summary, min_score)
    if not hits:
        return None, None, {"outcome": wiki_dedup_judge.OUTCOME_OK, "reason": "no_candidates"}

    target_resolved = target.resolve()
    new_page = {
        "path": target.relative_to(root).as_posix(),
        "content": f"# {candidate.get('title') or ''}\n\n{summary}",
    }
    last_info: dict = {"outcome": wiki_dedup_judge.OUTCOME_OK, "reason": "no_duplicate"}
    judged = 0
    for matched, score in hits:
        if judged >= budget:
            break
        if matched.resolve() == target_resolved:
            continue
        existing = wiki_dedup_judge.read_card(matched)
        if existing is None:
            continue
        judged += 1
        verdict, info = wiki_dedup_judge.judge_pair(
            root, compile_cfg,
            new_page,
            {"path": matched.relative_to(root).as_posix(), "content": existing},
            engine,
        )
        last_info = info
        if info.get("outcome") != wiki_dedup_judge.OUTCOME_OK:
            return None, None, info
        if verdict == "duplicate":
            return matched, score, info
    return None, None, last_info


def is_auto_writable_page(path: Path) -> tuple[bool, list[str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False, ["unreadable_target"]
    meta, ok = parse_frontmatter(text)
    if not ok:
        return False, ["frontmatter_unparseable"]
    findings = []
    # verified remains auto-writable so source changes reset it to generated.
    # superseded alone is historical and must never be revived by a refresh.
    if str(meta.get("status") or "").strip().lower() == "superseded":
        findings.append("protected_status")
    if meta.get("createdBy") != "qmd-auto-context":
        findings.append("non_qmd_created_by")
    if not AUTO_BLOCK_RE.search(text):
        findings.append("managed_section_missing")
    return not findings, findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--regenerate", action="store_true")
    args = parser.parse_args()

    if os.environ.get("QMD_SANDBOX"):
        return 0

    candidate = load_payload()
    root, config = project_paths(args.cwd)
    compile_cfg = config.get("compile") if isinstance(config.get("compile"), dict) else {}
    if config.get("indexing") is not True or not qmd_config.compile_active(compile_cfg):
        return 0
    mode = qmd_config.compile_mode(compile_cfg)
    wiki_rel = config.get("wikiPath", ".auto-context/wiki")
    wiki_root = safe_managed_dir(root, wiki_rel)
    compile_dir = safe_managed_dir(root, ".auto-context/compile")
    if wiki_root is None or compile_dir is None:
        print(json.dumps({"action": "rejected", "reason": "unsafe_managed_path"}, ensure_ascii=False))
        return 1

    try:
        with cp.card_write_lock(root), wiki_mutation_lock.lock(root):
            return _compile_locked(
                root, config, compile_cfg, mode, wiki_root, candidate, args.regenerate)
    except OSError:
        print(json.dumps({"action": "rejected", "reason": "card_write_lock_unavailable"}, ensure_ascii=False))
        return 1


def _compile_locked(root: Path, config: dict, compile_cfg: dict, mode: str,
                    wiki_root: Path, candidate: dict, regenerate: bool) -> int:
    import wiki_selected_laya
    selected = wiki_selected_laya.selected(root)

    suggested_type = candidate.get("suggestedType") if candidate.get("suggestedType") in ALLOWED_TYPES else "concept"
    identity_index = build_identity_index(wiki_root)
    target, target_reason, target_matches = resolve_target(root, wiki_root, candidate, suggested_type, identity_index)
    max_lines = int(compile_cfg.get("maxAutoPageLines", 120) or 120)
    lint = lint_candidate(candidate, target, max_lines)
    # 한 줄 라벨이므로 개행·탭을 소스에서 접는다(dump의 이스케이프는 안전망으로 남는다).
    title = yaml_scalars.fold_inline(candidate.get("title") or "Untitled") or "Untitled"
    summary, redactions = redact(str(candidate.get("summary") or "").strip())
    h = source_hash({**candidate, "summary": summary})

    candidate_path = cp.ledger(root, cp.CANDIDATES)
    tombstone_path = cp.ledger(root, cp.TOMBSTONES)
    manifest_path = cp.ledger(root, cp.MANIFEST)
    merge_needed_path = cp.ledger(root, cp.MERGE_NEEDED)
    if candidate_path is None or tombstone_path is None or manifest_path is None or merge_needed_path is None:
        print(json.dumps({"action": "rejected", "reason": "unsafe_compile_path"}, ensure_ascii=False))
        return 1

    # candidates.jsonl is a write-only audit log (every compile attempt appends a
    # row, no reader) -- cap it up front so it can't grow unbounded.
    trim_jsonl(candidate_path)

    record = {
        "ts": now_iso(),
        "trigger": candidate.get("trigger", "manual"),
        "title": title,
        "summary": summary,
        "suggestedType": suggested_type,
        "suggestedStatus": compile_cfg.get("defaultStatus", "generated"),
        "confidence": candidate.get("confidence", "medium"),
        "sources": candidate.get("sources") if isinstance(candidate.get("sources"), list) else [],
        "sourceRevisions": source_revisions(candidate),
        "targetPath": target.relative_to(root).as_posix() if target else str(candidate.get("targetPath") or ""),
        "canonicalKey": clean_canonical_key(candidate.get("canonicalKey")),
        "aliases": clean_aliases(candidate.get("aliases")),
        "targetResolution": target_reason,
        "targetMatches": [match.relative_to(root).as_posix() for match in target_matches],
        "sourceHash": h,
        "lint": lint,
        "redactions": redactions,
        "_qmd": effort_audit(candidate),
    }

    if target_reason.startswith("ambiguous_"):
        record["action"] = "merge-needed"
        record["lint"] = {"verdict": "needs_review", "findings": [target_reason]}
        append_jsonl(candidate_path, record)
        print(json.dumps({"action": "merge-needed", "reason": target_reason, "targetMatches": record["targetMatches"]}, ensure_ascii=False))
        return 0

    if lint["verdict"] != "clean" or target is None:
        if "transcript_like" in lint.get("findings", []):
            record["summary"] = "[REDACTED_TRANSCRIPT]"
            record["redactions"] = sorted(set(record.get("redactions", []) + ["transcript_like"]))
        record["action"] = "rejected"
        append_jsonl(candidate_path, record)
        print(json.dumps({"action": "rejected", "findings": lint["findings"]}, ensure_ascii=False))
        return 0

    tombstones = read_jsonl(tombstone_path)
    if any(same_generated_identity(row, record) for row in tombstones) and not regenerate:
        record["action"] = "suppressed"
        append_jsonl(candidate_path, record)
        print(json.dumps({"action": "suppressed", "targetPath": record["targetPath"]}, ensure_ascii=False))
        return 0

    previous = [
        row for row in read_jsonl(manifest_path)
        if same_generated_identity(row, record)
    ]
    if (
        previous
        and not target.exists()
        and not regenerate
        and previous[-1].get("action") != MACHINE_DELETE_ACTION
    ):
        tombstone = {**record, "action": "deleted", "status": "deleted", "previousStatus": previous[-1].get("status", "generated")}
        append_jsonl(tombstone_path, tombstone)
        record["action"] = "tombstoned"
        append_jsonl(candidate_path, record)
        print(json.dumps({"action": "tombstoned", "targetPath": record["targetPath"]}, ensure_ascii=False))
        return 0

    if mode == "candidates":
        record["action"] = "candidate"
        append_jsonl(candidate_path, record)
        print(json.dumps({"action": "candidate", "targetPath": record["targetPath"]}, ensure_ascii=False))
        return 0

    if mode == "guarded" and candidate.get("confidence") != "high":
        record["action"] = "candidate"
        append_jsonl(candidate_path, record)
        print(json.dumps({"action": "candidate", "targetPath": record["targetPath"]}, ensure_ascii=False))
        return 0

    if not target.exists():
        # LLM body judgment first: it covers EVERY new page, including
        # explicit-targetPath candidates that the legacy slug-only gate skipped.
        matched_path, score, judge_info = judge_new_page_duplicate(
            root, wiki_root, config, compile_cfg, candidate, summary, target
        )
        judge_outcome = judge_info.get("outcome")
        # **유료 judge 실패·degrade·식힘의 종점.** `_back_off`의 `engineCooldown`/
        # `cooldownWriteFailed`를 읽는 소비자가 없던 동안, 선호 엔진이 유료로 실패하는 상태에서
        # 두 식힘 쓰기가 모두 실패하면 compile 마다 같은 쌍을 재과금하는데 흔적이 0이었다.
        # 무엇을 남길지는 `judge.diagnostics()`가 정한다(scan 로그와 같은 규칙 — 복제 금지).
        # 정상 판정이거나 이 머신에 judge 설정이 아예 없으면 빈 dict 라 기존 행 모양이 그대로다.
        judge_diag = wiki_dedup_judge.diagnostics(judge_info)
        if judge_diag:
            record["judge"] = judge_diag
        if matched_path is None and judge_outcome != wiki_dedup_judge.OUTCOME_OK and target_reason == "slug":
            # No judge on this machine (or it is cooling down) — fall back to the
            # legacy score-threshold gate so this path never gets weaker than before.
            matched_path, score = find_wiki_semantic_match(root, wiki_root, config, candidate, summary)
            judge_info = {**judge_info, "fallback": "score_threshold"}
        if matched_path is not None:
            entry = {
                "ts": now_iso(),
                "candidate": record,
                "matchedPath": matched_path.relative_to(root).as_posix(),
                "matchedScore": score,
            }
            if judge_info.get("verdict"):
                entry["judgeVerdict"] = judge_info["verdict"]
                entry["judgeReason"] = judge_info.get("reason", "")
                entry["judgedBy"] = judge_info.get("engine", "")
                # 판정 엔진과 **신규 후보를 만든** 엔진의 관계. verify의 `verifiedMode`/
                # `producedBy`와 같은 어휘·같은 목적이다 — 이 두 필드가 없으면 자기검증
                # 노출을 사후에 셀 수 없다(실측: 기존 12건 전부 생산자 미기록이었다).
                entry["judgedMode"] = judge_info.get("mode", "")
                entry["producedBy"] = judge_info.get("producedBy", "")
                entry["uniqueToCandidate"] = judge_info.get("uniqueToA", [])
                entry["uniqueToMatched"] = judge_info.get("uniqueToB", [])
            append_jsonl(merge_needed_path, entry)
            record["action"] = "merge-needed"
            append_jsonl(candidate_path, record)
            print(json.dumps({
                "action": "merge-needed",
                "matchedPath": matched_path.relative_to(root).as_posix(),
                "score": score,
                **({"judgeVerdict": judge_info["verdict"]} if judge_info.get("verdict") else {}),
            }, ensure_ascii=False))
            return 0

    status = compile_cfg.get("defaultStatus", "generated")
    target.parent.mkdir(parents=True, exist_ok=True)
    page = markdown_page(candidate, summary, status, redactions, h)
    action = "created"
    if target.exists() and target_reason == "explicit":
        # 모델이 고른 경로가 **다른 카드**를 가리키면 갱신이 아니라 검토로 보낸다. 갱신의
        # 근거는 "같은 카드의 소스가 바뀌었다"이고, identity·source가 하나도 겹치지 않으면
        # 그 근거가 없다. 여기서 덮으면 (a) 다음 verify가 inconclusive를 내면 카드가 삭제되고
        # `auto:end` 밖 사람이 쓴 섹션이 영구 소실되며(원장에 본문이 없다), (b) pass를 내면
        # 본문은 새 소스의 사실인데 frontmatter의 sources·title은 옛 카드로 남아 provenance가
        # 조용히 틀린다(2단계 `↳` 원문 경로가 엉뚱한 파일을 가리킨다).
        # identity 조회로 찾은 경로(`target_reason != "explicit"`)는 정의상 일치하므로 통과.
        agrees, mismatch = explicit_target_agrees(target, candidate)
        if not agrees:
            entry = {
                "ts": now_iso(),
                "candidate": record,
                "matchedPath": record["targetPath"],
                "matchedScore": 0,
                "reason": mismatch,
            }
            if merge_needed_path is not None:
                append_jsonl(merge_needed_path, entry)
            record["action"] = "merge-needed"
            record["lint"] = {"verdict": "needs_review", "findings": [mismatch]}
            append_jsonl(candidate_path, record)
            print(json.dumps({
                "action": "merge-needed",
                "reason": mismatch,
                "targetPath": record["targetPath"],
            }, ensure_ascii=False))
            return 0
    if target.exists():
        old = target.read_text(encoding="utf-8")
        auto_writable, findings = is_auto_writable_page(target)
        if not auto_writable:
            record["action"] = "merge-needed"
            record["lint"] = {"verdict": "needs_review", "findings": findings}
            append_jsonl(candidate_path, record)
            print(json.dumps({"action": "merge-needed", "targetPath": record["targetPath"], "findings": findings}, ensure_ascii=False))
            return 0
        page_block_match = AUTO_BLOCK_RE.search(page)
        if page_block_match is None:
            record["action"] = "merge-needed"
            record["lint"] = {"verdict": "needs_review", "findings": ["generated_section_missing"]}
            append_jsonl(candidate_path, record)
            print(json.dumps({"action": "merge-needed", "targetPath": record["targetPath"], "findings": ["generated_section_missing"]}, ensure_ascii=False))
            return 0
        # Every refreshed card re-enters verification regardless of a custom
        # creation default.  The status reset and trusted provenance must land
        # with the new managed block in one card rewrite.
        status = "generated"
        page = markdown_page(candidate, summary, status, redactions, h)
        action = "updated"
    # 신규 생성도 같은 원자적 경로를 쓴다. `updated`는 기존 카드 파괴 방지가 이유이고,
    # `created`는 **부분 카드가 완성된 카드처럼 보이는 것**이 이유다 — 절단된 파일도
    # 인덱싱되고 recall이 그 본문을 완결된 요약으로 주입하며(읽기 창 안에서 끊겼는지
    # 알 수 없다) verify는 반쪽 카드를 원문과 대조한다. 다음 회차 덮어쓰기에 의존하면
    # 그 사이 세션이 잘린 카드를 캐논으로 본다. 실패는 아래 write_failure로 표면화한다.
    wiki_selected_laya.recheck(selected)
    write_ok = (rewrite_generated_card(target, old, page) if action == "updated"
                else write_text_atomic(target, page))
    if not write_ok:
        record["action"] = "write-failed"
        append_jsonl(candidate_path, record)
        print(json.dumps({"action": "write-failed", "targetPath": record["targetPath"]}, ensure_ascii=False))
        return 1

    record["action"] = action
    append_jsonl(candidate_path, record)
    append_jsonl(manifest_path, {**record, "status": status})
    # manifest is read for delete-detection (previous lookup); compact by folding
    # same-identity duplicates rather than size-trimming (which would drop a card's
    # entry and break tombstoning). Threshold-gated + atomic inside.
    compact_manifest(manifest_path)
    # 인덱스 쓰기 실패는 카드 쓰기 실패처럼 중단시키지 않는다 — 카드는 이미 캐논으로
    # 디스크에 있고 인덱스는 항법(navigation)이다. 대신 조용히 넘기지 않고 출력·후보
    # 레코드에 남긴다(인덱스가 카드와 어긋난 채 남았다는 사실을 알 수 있어야 한다).
    index_ok = update_index(wiki_root, target, title)
    append_log(wiki_root, action, target, title)

    collection, collection_path = find_wiki_collection(config)
    if collection and collection_path:
        if selected is None:
            enqueue_project_collections(root, {collection: str((root / collection_path).resolve())})
        else:
            wiki_selected_laya.request_sync(root, selected)

    # 기계 검수(auto-verify) enqueue: generated로 쓰인 카드만 대상. verify worker가
    # 카드 주장 vs 원문을 대조해 verified 승격 또는 (onFail) 삭제한다.
    verify_cfg = compile_cfg.get("verify") if isinstance(compile_cfg.get("verify"), dict) else {}
    verify_queued = False
    if verify_cfg.get("enabled", True) and status == "generated":
        verify_queue_path = cp.ledger(root, cp.VERIFY_QUEUE)
        if verify_queue_path is not None:
            verify_queued = True
            # 실제 소스를 별도 필드로 넘긴다 — verifier가 `MAX_SOURCES` 상한과 무관하게
            # 반드시 읽어야 하는 근거다(모델이 준 `sources` 항목이 밀어낼 수 없다).
            authoritative = authoritative_sources(candidate)
            append_jsonl(verify_queue_path, {
                "ts": now_iso(),
                "targetPath": record["targetPath"],
                "sources": record["sources"],
                "sourceRevisions": record["sourceRevisions"],
                **({"authoritativeSources": authoritative} if authoritative else {}),
                "sourceHash": h,
                "engine": candidate.get("engine") if isinstance(candidate.get("engine"), str) else "",
                "trigger": record["trigger"],
            })

    result = {
        "action": action,
        "targetPath": record["targetPath"],
        "sourceRevisions": record["sourceRevisions"],
    }
    source_refresh_event_id = candidate.get("sourceRefreshEventId")
    if isinstance(source_refresh_event_id, str) and source_refresh_event_id:
        result["sourceRefreshEventId"] = source_refresh_event_id
    # 이 카드가 기계 검수 큐에 실려 있는지를 호출자(compile worker)가 알아야 한다: 한 run이
    # 만든 검수 부채만큼 같은 run의 verify 예산을 늘려 큐가 배수보다 빨리 자라지 않게 한다.
    if verify_queued:
        result["verifyQueued"] = True
    if not index_ok:
        result["indexWriteFailed"] = True
        append_jsonl(candidate_path, {**record, "indexWriteFailed": True})
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

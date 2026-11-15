"""Verify ledger preflight and the suppression-marker body hash."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


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

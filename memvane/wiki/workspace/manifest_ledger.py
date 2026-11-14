"""Generated-manifest compaction."""
from __future__ import annotations

import json
import os
from pathlib import Path

from memvane.kernel.paths import read_jsonl
from memvane.wiki.workspace.types import LOG_MAX_BYTES


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

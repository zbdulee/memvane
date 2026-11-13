"""Guarded atomic writes for workspace paths."""
from __future__ import annotations

from pathlib import Path
from memvane.wiki.workspace.types import LOG_MAX_BYTES
from memvane.kernel.atomic_rename import write_text_atomic as _write_text_atomic
from memvane.wiki.workspace.wiki_mutation_lock import guard_path


@guard_path
def write_text_atomic(path, text):
    return _write_text_atomic(path, text)


def trim_jsonl(path: Path, max_bytes: int = LOG_MAX_BYTES) -> None:
    """append-only 로그의 무한 누적 방지: 상한 초과 시 최근 절반만 유지.
    순수 로그(candidates/dedup-deleted/verify-log)용 — 한 줄 유실은 무해.
    correctness가 걸린 파일(manifest)엔 쓰지 말 것 — compact_manifest 사용.

    쓰기는 원자적이다. "절반을 의도적으로 버린다"와 "쓰기가 실패해 임의 지점에서
    잘린다"는 다르다 — 후자의 최악은 첫 write에서 실패해 로그가 통째로 비는 것이고,
    그건 이 함수가 허용한 유실이 아니다(read-modify-write 클래스)."""
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

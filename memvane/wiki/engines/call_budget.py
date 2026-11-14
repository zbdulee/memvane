"""Paid-call budget block and the bounded source text an engine call reads."""
from __future__ import annotations


def budget_of(compile_cfg: dict) -> dict:
    """`compile.budget` (유료 호출 수 상한 블록). dict가 아니면 빈 dict.

    예전에는 이 값들이 `batch.maxPerRun`·`maxCardsPerSource`·`verify.maxPerRun`·
    `semanticDedup.judge.maxPairs*`로 네 서브트리에 흩어져 있었다. 한 run의 호출
    총량은 이들의 곱이므로 한 곳에서 읽는다.
    """
    raw = compile_cfg.get("budget") if isinstance(compile_cfg, dict) else None
    return raw if isinstance(raw, dict) else {}


def decode_source_bounded(data: bytes, max_chars: int) -> tuple[str, bool] | None:
    """Decode and bound one already-stable source snapshot."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text[:max_chars], len(text) > max_chars

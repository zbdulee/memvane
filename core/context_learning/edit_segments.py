"""Complete, overlapping edit-content windows from the installed Laya tokenizer."""
from __future__ import annotations

import hashlib


def split_with_offsets(text, offsets, *, target_tokens=1536, overlap_tokens=96):
    if (not isinstance(text, str) or not text.strip() or not isinstance(offsets, list)
            or not 128 <= target_tokens <= 4096
            or not 0 <= overlap_tokens < target_tokens // 4):
        raise ValueError('invalid_edit_segmentation')
    if not offsets:
        raise ValueError('empty_edit_tokens')
    if any(not isinstance(pair, (list, tuple)) or len(pair) != 2
           or type(pair[0]) is not int or type(pair[1]) is not int
           or not 0 <= pair[0] < pair[1] <= len(text) for pair in offsets):
        raise ValueError('invalid_edit_offsets')
    segments = []
    cursor = 0
    while cursor < len(offsets):
        ceiling = min(len(offsets), cursor + target_tokens)
        end = ceiling
        if ceiling < len(offsets):
            # Prefer a paragraph or sentence boundary near the token budget.
            floor = max(cursor + target_tokens * 3 // 4, ceiling - 160)
            for candidate in range(ceiling, floor, -1):
                boundary = offsets[candidate - 1][1]
                if boundary < len(text) and text[boundary - 1] in '.!?。！？\n':
                    end = candidate
                    break
        start_char = 0 if not segments else offsets[cursor][0]
        end_char = len(text) if end == len(offsets) else offsets[end][0]
        if end_char <= start_char:
            raise ValueError('invalid_edit_window')
        body = text[start_char:end_char]
        segments.append({'index': len(segments), 'start': start_char,
                         'end': end_char, 'sha256': hashlib.sha256(body.encode()).hexdigest(),
                         'text': body, 'token_count': end - cursor})
        if len(segments) > 64:
            raise ValueError('edit_segment_limit')
        if end == len(offsets):
            break
        next_cursor = max(cursor + 1, end - overlap_tokens)
        if overlap_tokens and next_cursor >= end:
            raise ValueError('invalid_edit_overlap')
        cursor = next_cursor
    if segments[0]['start'] != 0 or segments[-1]['end'] != len(text) or any(
            segments[i]['start'] > segments[i - 1]['end'] for i in range(1, len(segments))):
        raise ValueError('edit_segment_coverage_failed')
    count = len(segments)
    for entry in segments:
        entry['count'] = count
    return segments

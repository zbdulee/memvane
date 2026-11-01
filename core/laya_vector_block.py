"""A selected install's vector fill stuck on one member: streak and notice.

The sync worker records each wake that ends on a member verdict (a
``provider_result_invalid`` answer or a ``vector_member_rejected`` block)
in ``<state root>/vector-fill-block.json``: path, content hash, reason, a
short cause code, the first and last wake time and the wake count. Any
other result leaves it alone, a completed sync (``indexed``/``unchanged``)
removes it, and a different member or a changed hash starts a new streak.

SessionStart reads it (no wait, no provider call). Once the same member has
blocked ``NOTICE_WAKES`` wakes or ``NOTICE_SECONDS``, it shows one Korean
line naming the card and the cause, once per session of that streak.
Recall never reads this file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import time

FILE = 'vector-fill-block.json'
NOTICE_WAKES = 3
NOTICE_SECONDS = 30 * 60
MAX_NOTICED = 16
MEMBER_REASONS = frozenset(('provider_result_invalid', 'vector_member_rejected'))
CAUSE_LABELS = {
    'chunk_pos_not_increasing': '청크 위치 역행',
    'chunk_seq_invalid': '청크 순번 오류',
    'chunk_shape_invalid': '청크 형식 오류',
    'chunk_count_invalid': '청크 수 범위 밖',
    'dimension_mismatch': '벡터 차원 불일치',
    'non_finite_vector': '비정상 벡터 값',
    'result_invalid': '임베딩 결과 형식 오류',
    'worker_failure': '임베딩 worker 실패',
    'empty_answer': '임베딩 응답 없음',
    'unreadable_answer': '임베딩 응답 해석 불가',
    'not_reproduced': '재시도에서 재현 안 됨',
    'provider_text_invalid': '빈 카드 또는 크기 초과',
    'provider_request_too_large': '요청 크기 초과',
    'provider_member_refused': 'worker가 카드 거부',
    'no_chunks': '청크 없음',
    'chunk_set_too_large': '청크 수 초과',
}


def state_root(decision):
    return Path(decision['journalPath']).parent


def read(root):
    """The current streak, or None (absent, unsafe or unreadable)."""
    path = Path(root) / FILE
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 65536):
            return None
        with os.fdopen(os.dup(fd), 'rb') as source:
            row = json.loads(source.read())
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)
    if (not isinstance(row, dict) or not isinstance(row.get('path'), str)
            or not isinstance(row.get('hash'), str) or type(row.get('wakes')) is not int
            or type(row.get('first')) not in (int, float)):
        return None
    if not isinstance(row.get('noticed'), list):
        row['noticed'] = []
    return row


def _write(root, row):
    directory = Path(root)
    fd, temporary = tempfile.mkstemp(prefix='.vector-fill-block-', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as target:
            json.dump(row, target, ensure_ascii=False, separators=(',', ':'))
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, directory / FILE)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def clear(root):
    try:
        os.unlink(Path(root) / FILE)
    except FileNotFoundError:
        pass


def cause_of(result):
    if result.get('reason') == 'provider_result_invalid':
        cause = result.get('cause')
    else:
        cause = result.get('detail')
    return cause if isinstance(cause, str) and cause else 'unknown'


def observe(root, result, *, now=None):
    """Update the streak from one sync result; returns the streak or None."""
    now = time.time() if now is None else now
    if not isinstance(result, dict):
        return read(root)
    if result.get('state') in ('indexed', 'unchanged'):
        clear(root)
        return None
    path, sha = result.get('path'), result.get('hash')
    if (result.get('reason') not in MEMBER_REASONS or not isinstance(path, str)
            or not isinstance(sha, str)):
        return read(root)
    cause = cause_of(result)
    prior = read(root)
    if prior is not None and (prior['path'], prior['hash']) == (path, sha):
        row = {**prior, 'wakes': prior['wakes'] + 1, 'last': now,
               'reason': result['reason'],
               'cause': cause if cause != 'unknown' else prior.get('cause', 'unknown')}
    else:
        row = {'path': path, 'hash': sha, 'reason': result['reason'], 'cause': cause,
               'first': now, 'last': now, 'wakes': 1, 'noticed': []}
    _write(root, row)
    return row


def known_cause(root, path, sha):
    """A cause already diagnosed for this exact member in this streak."""
    row = read(root)
    if (row is not None and (row['path'], row['hash']) == (path, sha)
            and row.get('cause') not in (None, 'unknown')):
        return row['cause']
    return None


def display_path(decision, path):
    """``<wiki collection>/x.md`` as a project-relative path where possible."""
    try:
        name, rest = path.split('/', 1)
        project = Path(decision['projectRoot'])
        for item in decision.get('collections') or ():
            if item.get('name') == name:
                return str((Path(item['sourceRoot']) / rest).relative_to(project))
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    return path


def notice(decision, session, *, now=None):
    """One line for SessionStart while a streak is long enough, else None.

    Shown once per session of a streak (``noticed`` keeps the last
    ``MAX_NOTICED`` session ids). Never raises.
    """
    try:
        now = time.time() if now is None else now
        root = state_root(decision)
        row = read(root)
        if row is None or (row['wakes'] < NOTICE_WAKES
                           and now - row['first'] < NOTICE_SECONDS):
            return None
        key = session or 'no-session'
        if key in row['noticed']:
            return None
        row['noticed'] = (row['noticed'] + [key])[-MAX_NOTICED:]
        try:
            _write(root, row)
        except OSError:
            pass  # still show it; a later SessionStart may repeat it
        cause = row.get('cause') or 'unknown'
        label = CAUSE_LABELS.get(cause, '원인 미상')
        return (f"[memvane] 위키 카드 {display_path(decision, row['path'])} 때문에 "
                f"vector 색인이 멈춰 있어요 (원인: {label}, {cause}). "
                "카드를 고치거나 알려 주세요.")
    except Exception:
        return None

"""Small namespace state for the default teacher; safe to import from hooks.

Stdlib and kernel helpers only: hooks read these files without importing any
teacher, transport or learning-database module (structure test X-1).

Files (namespace root, 0600):
- ``teacher-host-state.json``: per-host waits, circuit breaker, queued counts,
  notice times, the default-teacher enable failure counter, and the count of
  consecutive passes that could not read the consent file.
- ``teacher-switch.json``: the setup skill's ``teacher-off``/``teacher-on``, the only off switch.
- ``project-teacher-enable.json``: the enable transaction intent (presence only here).
"""
from __future__ import annotations

import fcntl
import json
import os
import stat
import time
from contextlib import contextmanager
from pathlib import Path

from memvane.kernel import cooldown
from memvane.kernel.atomic_rename import atomic_json

STATE_NAME = 'teacher-host-state.json'
STATE_LOCK = 'teacher-host-state.lock'
STATE_SCHEMA = 'memvane-teacher-host-state-v1'
SWITCH_NAME = 'teacher-switch.json'
SWITCH_SCHEMA = 'memvane-teacher-switch-v1'
ENABLE_INTENT = 'project-teacher-enable.json'
HOSTS = ('claude', 'codex')
MAX_STATE_BYTES = 65536
# Same-reason enable failures after which maintenance stops requesting (§5-1).
ENABLE_FAILURE_LIMIT = 5
# Waits are absolute expiries capped like every other cooldown (cooldown.py rule 1).
PROVIDER_WAIT_SECONDS = 3600
FAILING_WAIT_SECONDS = 86400
FAILURE_LIMIT = 3
NOTICE_SECONDS = 7 * 86400
LONG_REST_SECONDS = 86400
INTENT_NOTICE_SECONDS = 600
# Consecutive passes that could not read the consent file before a session says so.
CONSENT_UNREADABLE_LIMIT = 5


def _private_read(path, limit=MAX_STATE_BYTES):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > limit):
            raise ValueError('teacher_state_unsafe')
        data = b''
        while len(data) <= limit:
            block = os.read(fd, limit + 1 - len(data))
            if not block:
                break
            data += block
        if len(data) > limit:
            raise ValueError('teacher_state_unsafe')
        return data
    finally:
        os.close(fd)


def _host_skeleton():
    return {'consecutiveFailures': 0, 'waitUntilUnixMs': None, 'reason': None,
            'queued': 0, 'waitingSinceUnixMs': None, 'lastNoticeUnixMs': {}}


def empty_state():
    return {'schema': STATE_SCHEMA, 'hosts': {host: _host_skeleton() for host in HOSTS},
            'enable': {'failures': 0, 'reason': None, 'pluginVersion': None},
            'consent': {'unreadable': 0, 'lastNoticeUnixMs': None}}


def _normalize(value):
    """Keep only known, well-typed fields; anything else falls back to defaults."""
    result = empty_state()
    if not isinstance(value, dict) or value.get('schema') != STATE_SCHEMA:
        return result
    hosts = value.get('hosts') if isinstance(value.get('hosts'), dict) else {}
    for host in HOSTS:
        row = hosts.get(host) if isinstance(hosts.get(host), dict) else {}
        target = result['hosts'][host]
        for key in ('consecutiveFailures', 'queued'):
            if type(row.get(key)) is int and 0 <= row[key] <= 1_000_000:
                target[key] = row[key]
        for key in ('waitUntilUnixMs', 'waitingSinceUnixMs'):
            if type(row.get(key)) is int and row[key] > 0:
                target[key] = row[key]
        if isinstance(row.get('reason'), str) and len(row['reason']) <= 80:
            target['reason'] = row['reason']
        notices = row.get('lastNoticeUnixMs') if isinstance(row.get('lastNoticeUnixMs'), dict) else {}
        target['lastNoticeUnixMs'] = {k: v for k, v in notices.items()
                                      if isinstance(k, str) and len(k) <= 80 and type(v) is int and v > 0}
    enable = value.get('enable') if isinstance(value.get('enable'), dict) else {}
    if type(enable.get('failures')) is int and 0 <= enable['failures'] <= 1000:
        result['enable']['failures'] = enable['failures']
    for key in ('reason', 'pluginVersion'):
        if isinstance(enable.get(key), str) and len(enable[key]) <= 80:
            result['enable'][key] = enable[key]
    consent = value.get('consent') if isinstance(value.get('consent'), dict) else {}
    if type(consent.get('unreadable')) is int and 0 <= consent['unreadable'] <= 1_000_000:
        result['consent']['unreadable'] = consent['unreadable']
    if type(consent.get('lastNoticeUnixMs')) is int and consent['lastNoticeUnixMs'] > 0:
        result['consent']['lastNoticeUnixMs'] = consent['lastNoticeUnixMs']
    return result


def read_state(root):
    """Return the normalized state; a missing or unreadable file reads as empty."""
    try:
        return _normalize(json.loads(_private_read(Path(root) / STATE_NAME)))
    except FileNotFoundError:
        return empty_state()
    except (OSError, ValueError, TypeError):
        return empty_state()


def write_state(root, state):
    """Atomic 0600 replace. Returns False instead of raising: callers must check it."""
    try:
        atomic_json(Path(root) / STATE_NAME, _normalize(state))
        return True
    except (OSError, ValueError, TypeError):
        return False


@contextmanager
def locked(root):
    """Non-blocking exclusive lock shared by worker and hook; yields False if busy."""
    fd = None
    held = False
    try:
        try:
            fd = os.open(Path(root) / STATE_LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('teacher_state_unsafe')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held = True
        except (OSError, ValueError, BlockingIOError):
            held = False
        yield held
    finally:
        if fd is not None:
            if held:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def now_ms():
    return int(time.time() * 1000)


def host_waiting(state, host, now=None):
    """(reason, waitUntilUnixMs) when the host has an unexpired wait, else None."""
    row = state['hosts'].get(host) or {}
    until = row.get('waitUntilUnixMs')
    moment = (now_ms() if now is None else now) / 1000
    if until is not None and cooldown.expiry_active(until / 1000, moment):
        return row.get('reason'), until
    return None


def wait_until_ms(now, seconds):
    return int(cooldown.expiry_value(now / 1000, seconds) * 1000)


def teacher_off(root):
    """True when the setup skill's ``teacher-off`` switched the teacher off.

    The switch is the namespace file only. Process environment does not reach the
    worker, maintenance or onboarding children, so no variable turns the teacher off.
    """
    return switch_off(root)


def switch_off(root):
    try:
        value = json.loads(_private_read(Path(root) / SWITCH_NAME, 4096))
    except FileNotFoundError:
        return False
    except (OSError, ValueError, TypeError):
        # An unreadable switch is treated as off: never widen on a damaged marker.
        return True
    return not (isinstance(value, dict) and value.get('schema') == SWITCH_SCHEMA
                and value.get('enabled') is True)


def write_switch(root, enabled, now=None):
    """Write the switch; returns False on failure (the write is the only effect)."""
    if type(enabled) is not bool:
        raise ValueError('invalid_teacher_switch')
    try:
        atomic_json(Path(root) / SWITCH_NAME, {'schema': SWITCH_SCHEMA, 'enabled': enabled,
                                              'changedAtUnixMs': now_ms() if now is None else now})
        return True
    except (OSError, ValueError, TypeError):
        return False


def enable_intent_present(root):
    return os.path.lexists(Path(root) / ENABLE_INTENT)


def enable_intent_stale(root, now=None, seconds=INTENT_NOTICE_SECONDS):
    """Intent left longer than the notice window (mtime rule from cooldown.py)."""
    try:
        mtime = os.lstat(Path(root) / ENABLE_INTENT).st_mtime
    except OSError:
        return False
    return cooldown.window_elapsed(mtime, time.time() if now is None else now, seconds)


def enable_requests_stopped(state, plugin_version):
    """Five same-reason failures stop enable requests until the plugin version changes."""
    enable = state['enable']
    return (enable['failures'] >= ENABLE_FAILURE_LIMIT
            and enable.get('pluginVersion') == plugin_version)


UNAVAILABLE = 'teacher_host_unavailable'
UNSAFE_PATH = 'teacher_cli_unsafe_path'
REST_REASONS = {'teacher_isolation_incomplete': '격리 점검 실패',
                'teacher_model_unresolved': '사용할 모델 없음',
                'teacher_codex_home_redirected': 'CODEX_HOME 재지정'}
_NOTICE_NAMES = {'claude': ('Claude', 'Claude Code CLI(claude)'), 'codex': ('Codex', 'Codex CLI(codex)')}
ENABLE_NOTICE = ('[memvane] 학습 설정 전환이 끝나지 않아 학습과 선택 모델이 멈췄습니다. '
                 'setup skill로 학습 설정 복구를 요청하세요.')
# No intent left: the enable requests stopped after repeated failures; nothing is fenced.
CONSENT_NOTICE = ('[memvane] 학습 동의 파일을 계속 읽지 못해 학습 라벨링이 멈춰 있습니다. '
                  'memvane:logs로 확인하세요. 검색과 수집은 계속됩니다.')
ENABLE_DEFERRED_NOTICE = ('[memvane] 학습 설정 전환이 미뤄졌습니다. 검색과 수집은 계속됩니다. '
                          'setup skill로 학습 설정 상태를 확인하세요.')


def _host_notice(row, host, now):
    """(reason, line) when this host's seven-day notice is due, else None."""
    if row['queued'] < 1:
        return None
    reason, (name, cli) = row['reason'], _NOTICE_NAMES[host]
    if reason == UNAVAILABLE:
        line = ('[memvane] 학습 라벨링에는 %s가 필요합니다. CLI를 설치하면 대기 중인 학습 사례를 '
                '처리합니다. 검색과 수집은 계속됩니다.' % cli)
    elif reason == UNSAFE_PATH:
        line = ('[memvane] %s가 있는 폴더의 권한 때문에 학습 라벨링에 쓰지 않습니다. '
                '검색과 수집은 계속됩니다.' % cli)
    elif (reason in REST_REASONS and row['waitingSinceUnixMs'] is not None
          and cooldown.window_elapsed(row['waitingSinceUnixMs'] / 1000, now / 1000, LONG_REST_SECONDS)):
        line = ('[memvane] %s 학습 라벨링이 하루 넘게 멈춰 있습니다(%s). memvane:logs로 확인하세요. '
                '검색과 수집은 계속됩니다.' % (name, REST_REASONS[reason]))
    else:
        return None
    last = row['lastNoticeUnixMs'].get(reason)
    if last is not None and not cooldown.window_elapsed(last / 1000, now / 1000, NOTICE_SECONDS):
        return None
    return reason, line


def _consent_notice(state, now):
    """True when the unreadable-consent line is due (seven days apart)."""
    row = state['consent']
    if row['unreadable'] < CONSENT_UNREADABLE_LIMIT:
        return False
    last = row['lastNoticeUnixMs']
    return last is None or cooldown.window_elapsed(last / 1000, now / 1000, NOTICE_SECONDS)


def session_notices(root, host, plugin_version, now=None):
    """SessionStart lines for the session host from the namespace state files only.

    Reads ``teacher-host-state.json`` and the enable intent's presence; starts
    no process and opens no database. The unfinished-enable line repeats every
    session. A seven-day line (a host's, or the unreadable consent file's) is
    returned only after its notice time is written under the state lock.
    """
    if host not in HOSTS:
        return []
    now = now_ms() if now is None else now
    state = read_state(root)
    lines = []
    if enable_intent_stale(root, now / 1000):
        lines.append(ENABLE_NOTICE)
    elif (not enable_intent_present(root) and enable_requests_stopped(state, plugin_version)
          and not teacher_off(root)):
        lines.append(ENABLE_DEFERRED_NOTICE)
    elif enable_requests_stopped(state, plugin_version) and not teacher_off(root):
        lines.append(ENABLE_NOTICE)
    consent = _consent_notice(state, now) and not teacher_off(root)
    if _host_notice(state['hosts'][host], host, now) is None and not consent:
        return lines
    with locked(root) as held:
        if not held:
            return lines
        state = read_state(root)
        pending = []
        if consent and _consent_notice(state, now):
            state['consent']['lastNoticeUnixMs'] = now
            pending.append(CONSENT_NOTICE)
        due = _host_notice(state['hosts'][host], host, now)
        if due is not None:
            state['hosts'][host]['lastNoticeUnixMs'][due[0]] = now
            pending.append(due[1])
        # The stamp is the seven-day limit: an unwritten stamp would repeat the line every session.
        if pending and write_state(root, state):
            lines.extend(pending)
    return lines


def _private_directory(path):
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('teacher_switch_namespace_unsafe')


def _default_root(project, install_id):
    # Lazy: the enrollment module is not part of this module's hook-safe imports.
    from memvane.learning.default_enrollment import default_namespace
    return default_namespace(Path(project).resolve(strict=True), install_id)


def session_notices_for(decision, host):
    """``session_notices`` for a selected decision's default namespace."""
    from memvane.kernel.memvane_version import plugin_version
    return session_notices(_default_root(decision['projectRoot'], decision['installId']), host,
                           plugin_version())


def switch_project(project, enabled):
    """``set_switch`` for the selected install's default namespace of ``project``."""
    from memvane.journal.api import select_route
    from .runtime_setup import learning_state_base
    project = Path(project).resolve(strict=True)
    decision = select_route(str(project), action='update')
    if not isinstance(decision, dict) or not decision.get('installId'):
        raise ValueError('teacher_switch_route_required')
    return set_switch(_default_root(project, decision['installId']), learning_state_base(), enabled)


def set_switch(root, base, enabled):
    """The setup skill's ``teacher-off``/``teacher-on`` for one default namespace.

    ``base`` is the learning state base the namespace lives under; missing
    owner-only directories are created so the switch can precede enrollment.
    Raises when nothing was written. Profiles already enabled stay as they are.
    """
    root, base = Path(root), Path(base)
    if (not root.is_absolute() or root.parent != base / 'live-selection'
            or any(path.is_symlink() for path in (root, *root.parents))):
        raise ValueError('teacher_switch_namespace_unsafe')
    for directory in (base, root.parent, root):
        _private_directory(directory)
    # The switch file is the whole effect of the command: unwritten means not switched.
    if not write_switch(root, enabled):
        raise OSError('teacher_switch_unwritten')
    return {'status': 'teacher_on' if enabled else 'teacher_off', 'changesApplied': 1,
            'namespaceRoot': str(root), 'teacherOff': teacher_off(root)}

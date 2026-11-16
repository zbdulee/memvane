"""Bounded, read-only observation of older hook sessions."""
from datetime import datetime
import time

from memvane.kernel.memvane_version import version_key

HOOK_EVENTS = frozenset(('recall', 'update', 'update-queued', 'gate', 'posttool',
                         'index', 'compile', 'file-change-event'))
RECENT_SECONDS = 3600


def older_hook_seen(rows, installed, now):
    latest = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or row.get('event') not in HOOK_EVENTS:
            continue
        if row.get('host') not in ('claude', 'codex', 'hermes', 'gemini'):
            continue
        try:
            stamp = datetime.fromisoformat(row['ts'].replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                continue
            stamp = stamp.timestamp()
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            continue
        if not 0 <= now - stamp <= RECENT_SECONDS:
            continue
        version = version_key(row.get('pluginVersion'))
        if version is None:
            continue
        session = row.get('session')
        key = (row['host'], session) if isinstance(session, str) and session else ('anonymous', index)
        if key not in latest or stamp >= latest[key][0]:
            latest[key] = (stamp, version)
    return any(version < installed for _stamp, version in latest.values())


def append_notice(decision, notice):
    from memvane.diagnostics import api as diagnostics
    from memvane.runtime import api as runtime
    try:
        root = diagnostics.state_root_for_decision(decision)
        installed = runtime.background_bundle().installed_version()
        if root is None or installed is None:
            return notice
        rows = diagnostics.build_progress_events(root)
        if older_hook_seen(rows, installed, time.time()):
            line = ('[memvane] 최근 옛 버전 hook 실행이 기록됩니다. '
                    '최신 버전을 적용하려면 해당 세션을 다시 열어 주세요.')
            return notice + '\n' + line if notice else line
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return notice

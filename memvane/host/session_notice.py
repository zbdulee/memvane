"""Bounded, read-only observation of older hook sessions."""
from datetime import datetime
import time

from memvane.kernel.memvane_version import version_key

HOOK_EVENTS = frozenset(('recall', 'update', 'update-queued', 'gate', 'posttool',
                         'index', 'compile', 'file-change-event'))
RECENT_SECONDS = 3600


def older_hook_seen(rows, installed, now, host, session):
    if not isinstance(session, str) or not session:
        return False
    latest = None
    for row in rows:
        if not isinstance(row, dict) or row.get('event') not in HOOK_EVENTS:
            continue
        if row.get('host') != host or row.get('session') != session:
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
        if latest is None or stamp >= latest[0]:
            latest = (stamp, version)
    return latest is not None and latest[1] < installed


def append_notice(decision, notice, host, payload):
    from memvane.diagnostics import api as diagnostics
    from memvane.runtime import api as runtime
    try:
        root = diagnostics.state_root_for_decision(decision)
        bundle = runtime.background_bundle()
        installed = bundle.installed_version(host=host)
        if root is None or installed is None or bundle.version is None or bundle.version >= installed:
            return notice
        rows = diagnostics.build_progress_events(root)
        if older_hook_seen(rows, installed, time.time(), host, diagnostics.session_id(payload)):
            line = ('[memvane] 이 세션은 옛 버전 hook을 실행합니다. '
                    '최신 버전을 적용하려면 세션을 다시 열어 주세요.')
            return notice + '\n' + line if notice else line
    except Exception:
        pass
    return notice

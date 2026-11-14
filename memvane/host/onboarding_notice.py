"""SessionStart onboarding notice and answer commands."""
from __future__ import annotations

import json
from pathlib import Path
import shlex

from memvane.route.api import read_regular
from memvane.route.types import MAX_HOOK_BYTES
from memvane.settings.api import find_project_config


# Onboarding notices stay in Korean like the setup wrappers' notices. The
# first line is for the user and names no path; the second carries the exact
# answer commands as JSON so any path survives (quotes, backticks, newlines).
ONBOARDING_NOTICE = ('[memvane] 이 프로젝트는 아직 MemVane 온보딩 전입니다. setup skill로 '
                     '온보딩하거나, 이번 세션만 건너뛰기·다시 묻지 않기를 원하면 말해 주세요.')
ONBOARDING_NOTICE_NO_SESSION = ('[memvane] 이 프로젝트는 아직 MemVane 온보딩 전입니다. setup '
                                'skill로 온보딩하거나, 다시 묻지 않기를 원하면 말해 주세요.')
ONBOARDING_ANSWER_PREFIX = '[memvane] onboarding-answer '
BUILD_NOTICE = ('[memvane] {root}: 초기 MemVane 색인을 준비 중입니다. '
                '빌드가 끝나면 setup skill로 활성화하세요.')
# A route or selection marker of another plugin major (route ``majorMismatch``).
MAJOR_NOTICES = {
    'newer': '[memvane] 이 프로젝트는 더 높은 MemVane 버전으로 설치되어 있습니다. 이 호스트의 플러그인을 올려 주세요.',
    'other': ('[memvane] 이 프로젝트의 MemVane 설치 기록을 이 플러그인 버전이 읽지 못합니다. '
              'setup skill로 기존 설치를 복원(restore)한 뒤 다시 온보딩해 주세요.'),
}


def onboarding_notice(payload_file: str, fallback_cwd: str) -> str | None:
    """One SessionStart line for a project with settings but no selected route.

    The caller has already classified the event ``legacy``. A route or
    selection marker of another plugin major gets its own line instead
    (``MAJOR_NOTICES``). A directory
    without ``.memvane/settings.json`` (or the ``.auto-context/`` fallback)
    is not opted in and stays silent.
    """
    cwd = fallback_cwd
    session = None
    try:
        payload = json.loads(read_regular(Path(payload_file), MAX_HOOK_BYTES).decode("utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("cwd"), str):
            cwd = payload["cwd"]
        if isinstance(payload, dict):
            session = payload.get("session_id") or payload.get("sessionId")
    except (OSError, ValueError, TypeError, UnicodeError):
        pass
    if not Path(cwd).is_absolute():
        return None
    try:
        from memvane.journal.api import select_route
        mismatch = select_route(cwd, action='update-queued').get('majorMismatch')
    except Exception:
        mismatch = None
    if mismatch in MAJOR_NOTICES:
        return MAJOR_NOTICES[mismatch]
    found = find_project_config(cwd)
    if found.get("configFormat") != "auto-context-dir":
        return None
    root = found.get("projectRoot") or cwd
    try:
        from memvane.install.api import build_lookup as lookup
        building = lookup(Path(root)) is not None
    except Exception:
        building = False
    if building:
        # A build the user started is progress, not a question.
        return BUILD_NOTICE.format(root=root)
    try:
        from memvane.host import onboarding_choice
        session = onboarding_choice.valid_session(session)
        if onboarding_choice.suppressed(root, session):
            return None
    except Exception:
        # A fault in the recorded answer shows the notice, never hides it.
        session = None
    return onboarding_lines(str(root), session)


def onboarding_lines(root: str, session: str | None) -> str:
    """The user notice line plus the agent's answer commands as one JSON line."""
    entry = str(Path(__file__).resolve().parents[2] / "entry" / "setup.py")
    base = ["python3", "-B", entry]

    def command(*args):
        argv = [*base, *args]
        return {"argv": argv, "shell": shlex.join(argv)}

    answer = {"projectRoot": root,
              "use": "run the matching command only when the user declines onboarding"}
    if session:
        answer["skip"] = command("onboarding-skip", "--project", root, "--session", session)
    answer["never"] = command("onboarding-never", "--project", root)
    notice = ONBOARDING_NOTICE if session else ONBOARDING_NOTICE_NO_SESSION
    return notice + "\n" + ONBOARDING_ANSWER_PREFIX + json.dumps(answer, ensure_ascii=False)



def main(argv):
    if len(argv) != 2:
        return 2
    try:
        notice = onboarding_notice(argv[0], argv[1])
    except Exception:
        # The notice is advisory: a fault (unreadable settings and the like)
        # leaves SessionStart silent with exit 0, never exit 1.
        return 0
    if notice:
        print(notice)
    return 0

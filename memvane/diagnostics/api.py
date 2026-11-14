"""Public diagnostic operations with lazy implementation loading."""
from __future__ import annotations

from memvane.diagnostics.types import PROMPT_CHARS, UNRESOLVED, WIKI_TAIL_CHARS


def bind(state_root=None, *, decision=None, install=None, host=None, session=None):
    from memvane.diagnostics.event_log import bind as implementation
    return implementation(state_root, decision=decision, install=install, host=host, session=session)


def bound():
    from memvane.diagnostics.event_log import bound as implementation
    return implementation()


def code(value):
    from memvane.diagnostics.event_log import code as implementation
    return implementation(value)


def elapsed_ms(started):
    from memvane.diagnostics.event_log import elapsed_ms as implementation
    return implementation(started)


def emit(event, *, outcome=None, reason=None, ms=None, state_root=UNRESOLVED,
         decision=None, install=UNRESOLVED, host=UNRESOLVED, session=UNRESOLVED, **fields):
    from memvane.diagnostics.event_log import emit as implementation
    return implementation(event, outcome=outcome, reason=reason, ms=ms, state_root=state_root,
                          decision=decision, install=install, host=host, session=session, **fields)


def install_of(decision):
    from memvane.diagnostics.event_log import install_of as implementation
    return implementation(decision)


def prompt_fields(prompt):
    from memvane.diagnostics.event_log import prompt_fields as implementation
    return implementation(prompt)


def session_id(value):
    from memvane.diagnostics.event_log import session_id as implementation
    return implementation(value)


def state_root_for_decision(decision):
    from memvane.diagnostics.event_log import state_root_for_decision as implementation
    return implementation(decision)


def state_root_of(journal_path):
    from memvane.diagnostics.event_log import state_root_of as implementation
    return implementation(journal_path)


def unbind(token):
    from memvane.diagnostics.event_log import unbind as implementation
    return implementation(token)


def wiki(state_root, action, rel, title, *, reason=None, install=None):
    from memvane.diagnostics.event_log import wiki as implementation
    return implementation(state_root, action, rel, title, reason=reason, install=install)


def wiki_tail(state_root, limit=WIKI_TAIL_CHARS):
    from memvane.diagnostics.event_log import wiki_tail as implementation
    return implementation(state_root, limit)


def capture(decision, *, event, reason, exc=None, warning=False, child_stderr=None, consent=None):
    from memvane.diagnostics.diagnostic_trace import capture as implementation
    return implementation(decision, event=event, reason=reason, exc=exc, warning=warning,
                          child_stderr=child_stderr, consent=consent)


def telemetry_emit(path, phase, *, job_id, resources=None, **metadata):
    from memvane.diagnostics.job_telemetry import emit as implementation
    return implementation(path, phase, job_id=job_id, resources=resources, **metadata)

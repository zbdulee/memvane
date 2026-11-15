"""Public host operations."""
from __future__ import annotations


def refusal_code(exc):
    from memvane.host.host_response import _refusal_code as implementation
    return implementation(exc)


def serve_selected(action, host, decision, payload):
    from memvane.host.host_response import serve_selected as implementation
    return implementation(action, host, decision, payload)


def serve_preview(action, host, decision, payload):
    from memvane.host.host_response import serve_preview as implementation
    return implementation(action, host, decision, payload)


def never(project, home=None):
    from memvane.host.onboarding_choice import never as implementation
    return implementation(project, home)


def resume(project, home=None):
    from memvane.host.onboarding_choice import resume as implementation
    return implementation(project, home)


def skip(project, session, home=None):
    from memvane.host.onboarding_choice import skip as implementation
    return implementation(project, session, home)


def status(project, home=None):
    from memvane.host.onboarding_choice import status as implementation
    return implementation(project, home)


def suppressed(root, session=None, home=None):
    from memvane.host.onboarding_choice import suppressed as implementation
    return implementation(root, session, home)


def valid_session(value):
    from memvane.host.onboarding_choice import valid_session as implementation
    return implementation(value)

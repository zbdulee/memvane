"""Public dashboard operations; implementations load when called."""
from __future__ import annotations


def serve(*, port=0, read_only=False, home=None, spawned=False, announce=None):
    from memvane.ui.server import serve as implementation
    return implementation(port=port, read_only=read_only, home=home, spawned=spawned, announce=announce)


def open_dashboard(*, cwd=None, tab='overview', no_open=False, port=0, from_session_start=False, home=None):
    from memvane.ui.cli import open_dashboard as implementation
    return implementation(cwd=cwd, tab=tab, no_open=no_open, port=port,
                          from_session_start=from_session_start, home=home)


def status(home=None):
    from memvane.ui.cli import status as implementation
    return implementation(home)


def stop(home=None):
    from memvane.ui.cli import stop as implementation
    return implementation(home)


def preferences(home=None):
    from memvane.ui.preferences import load as implementation
    return implementation(home)


def update_preferences(changes, home=None):
    from memvane.ui.preferences import update as implementation
    return implementation(changes, home)


def overview_snapshot(*, home=None, reconcile=False):
    from memvane.ui.read_adapters import overview as implementation
    return implementation(home=home, reconcile=reconcile)


def main(argv=None):
    from memvane.ui.cli import main as implementation
    return implementation(argv)

"""Public learning operations."""
from __future__ import annotations


def resource_report(*args, **kwargs):
    from memvane.learning.context_learning.shared import resource_report as implementation
    return implementation(*args, **kwargs)


def wake_after_index(*args, **kwargs):
    from memvane.learning.context_learning.shared import wake_after_index as implementation
    return implementation(*args, **kwargs)


def capture_recall(*args, **kwargs):
    from memvane.learning.context_learning.learning import capture_recall as implementation
    return implementation(*args, **kwargs)


def select_published(*args, **kwargs):
    from memvane.learning.context_learning.selection_read import select_published as implementation
    return implementation(*args, **kwargs)


def selector_prepared(*args, **kwargs):
    from memvane.learning.context_learning.selection_read import selector_prepared as implementation
    return implementation(*args, **kwargs)


def published_candidate_rows(*args, **kwargs):
    from memvane.learning.context_learning.selection_port import published_candidate_rows as implementation
    return implementation(*args, **kwargs)


def worker_lease(*args, **kwargs):
    from memvane.learning.context_learning.history_lifecycle import worker_lease as implementation
    return implementation(*args, **kwargs)


def ready(*args, **kwargs):
    from memvane.learning.context_learning.learning import ready as implementation
    return implementation(*args, **kwargs)


def read_private_json(*args, **kwargs):
    from memvane.learning.context_learning.dataset import read_private_json as implementation
    return implementation(*args, **kwargs)


def acknowledge_host(*args, **kwargs):
    from memvane.learning.context_learning.history_lifecycle import acknowledge_host as implementation
    return implementation(*args, **kwargs)


def learning_main(*args, **kwargs):
    from memvane.learning.context_learning.shared import main as implementation
    return implementation(*args, **kwargs)


def learning_worker_step(*args, **kwargs):
    from memvane.learning.context_learning.shared import learning_worker_step as implementation
    return implementation(*args, **kwargs)


def derive_compact_input(*args, **kwargs):
    from memvane.learning.context_learning.compact_input import derive as implementation
    return implementation(*args, **kwargs)


def learning_digest(*args, **kwargs):
    from memvane.learning.context_learning.contracts import digest as implementation
    return implementation(*args, **kwargs)


def selection_root(*args, **kwargs):
    from memvane.learning.context_learning.learning_profile import selection_root as implementation
    return implementation(*args, **kwargs)


def safe_root(*args, **kwargs):
    from memvane.learning.context_learning.shared import _safe_root as implementation
    return implementation(*args, **kwargs)


def learning_state_base(*args, **kwargs):
    from memvane.learning.context_learning.runtime_setup import learning_state_base as implementation
    return implementation(*args, **kwargs)


def project_selection_state(*args, **kwargs):
    from memvane.learning.context_learning.seam import _private_state as implementation
    return implementation(*args, **kwargs)


def request_selector_maintenance(*args, **kwargs):
    from memvane.learning.selector_maintenance import request as implementation
    return implementation(*args, **kwargs)


def run_selector_maintenance(*args, **kwargs):
    from memvane.learning.selector_maintenance import run as implementation
    return implementation(*args, **kwargs)


def run_automatic_learning(*args, **kwargs):
    from memvane.learning.context_learning_auto import run as implementation
    return implementation(*args, **kwargs)

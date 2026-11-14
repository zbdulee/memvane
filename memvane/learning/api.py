"""Public learning operations.

Constants other domains read (``LEARNING_PROFILE_INSTALL``) live in ``types.py``.
A forwarder passes an optional argument only when it differs from the default
shown; otherwise the implementation's own default applies.
"""
from __future__ import annotations as _annotations

# Recall capture and published selection.

def capture_recall(host, decision, payload, settings, pool, chosen, *, started=None):
    from memvane.learning.context_learning.learning import capture_recall as implementation
    given = {'started': started} if started is not None else {}
    return implementation(host, decision, payload, settings, pool, chosen, **given)

def select_published(pool, *, prompt, config, project_root, wiki_names, scope_id, deadline=None,
                     ensure_service=None):
    from memvane.learning.context_learning.selection_read import select_published as implementation
    given = {key: value for key, value, default in (
        ('deadline', deadline, None), ('ensure_service', ensure_service, None)) if value is not default}
    return implementation(pool, prompt=prompt, config=config, project_root=project_root,
                          wiki_names=wiki_names, scope_id=scope_id, **given)

def selector_prepared(config, project_root):
    from memvane.learning.context_learning.selection_read import selector_prepared as implementation
    return implementation(config, project_root)

def published_candidate_rows(pool, wiki_names, scope_id):
    from memvane.learning.context_learning.selection_port import published_candidate_rows as implementation
    return implementation(pool, wiki_names, scope_id)

def request_selector_maintenance(decision, *, force=False, owner=None):
    from memvane.learning.selector_maintenance import request as implementation
    given = {key: value for key, value, default in (
        ('force', force, False), ('owner', owner, None)) if value is not default}
    return implementation(decision, **given)

def run_selector_maintenance(project, *, force=False):
    from memvane.learning.selector_maintenance import run as implementation
    given = {'force': force} if force is not False else {}
    return implementation(project, **given)

# Learning worker and wakes.

def ready(project):
    from memvane.learning.context_learning.learning import ready as implementation
    return implementation(project)

def safe_root(config, project):
    from memvane.learning.context_learning.shared import _safe_root as implementation
    return implementation(config, project)

def worker_lease(*, home=None):
    from memvane.learning.context_learning.history_lifecycle import worker_lease as implementation
    given = {'home': home} if home is not None else {}
    return implementation(**given)

def learning_worker_step(root, project, revision, *, spawn=None):
    from memvane.learning.context_learning.shared import learning_worker_step as implementation
    given = {'spawn': spawn} if spawn is not None else {}
    return implementation(root, project, revision, **given)

def wake_after_index(project):
    from memvane.learning.context_learning.shared import wake_after_index as implementation
    return implementation(project)

def run_automatic_learning(project):
    from memvane.learning.context_learning_auto import run as implementation
    return implementation(project)

def learning_main(argv):
    from memvane.learning.context_learning.shared import main as implementation
    return implementation(argv)

def acknowledge_host(engine, plugin_root, *, home=None, now=None):
    from memvane.learning.context_learning.history_lifecycle import acknowledge_host as implementation
    given = {key: value for key, value, default in (
        ('home', home, None), ('now', now, None)) if value is not default}
    return implementation(engine, plugin_root, **given)

def read_private_json(path):
    from memvane.learning.context_learning.dataset import read_private_json as implementation
    return implementation(path)

# Learning profile.

def propose_learning_profile(project, root, *, choice, mode, expires_at_unix_ms, enable_learning=False,
                             staged_no_selection=False):
    from memvane.learning.context_learning.learning_profile import proposal as implementation
    given = {key: value for key, value, default in (
        ('enable_learning', enable_learning, False), ('staged_no_selection', staged_no_selection, False))
        if value is not default}
    return implementation(project, root, choice=choice, mode=mode, expires_at_unix_ms=expires_at_unix_ms,
                          **given)

def learning_profile_digest(value):
    from memvane.learning.context_learning.learning_profile import digest as implementation
    return implementation(value)

def activate_learning_profile(project, root, *, accepted_sha256, choice, mode, expires_at_unix_ms,
                              enable_learning=False, staged_no_selection=False, fault=None):
    """``fault`` None means the implementation default no-op hook."""
    from memvane.learning.context_learning.learning_profile import activate as implementation
    given = {key: value for key, value, default in (
        ('enable_learning', enable_learning, False), ('staged_no_selection', staged_no_selection, False),
        ('fault', fault, None))
        if value is not default}
    return implementation(project, root, accepted_sha256=accepted_sha256, choice=choice, mode=mode,
                          expires_at_unix_ms=expires_at_unix_ms, **given)

# Inference service hosted by the resident process.

def bind_service_starter(starter):
    from memvane.learning.context_learning.inference_service import bind_service_starter as implementation
    return implementation(starter)

def inference_version(root, deadline):
    from memvane.learning.context_learning.inference_service import _version as implementation
    return implementation(root, deadline)

def serve_inference(root, *, engine=None, validate=None, ready=None, capacity=None, apply_validate=None,
                    monitor_factory=None, stop=None, hosted=False):
    """``capacity`` None means the implementation default ``CAPACITY``."""
    from memvane.learning.context_learning.inference_service import _serve as implementation
    given = {key: value for key, value, default in (
        ('engine', engine, None), ('validate', validate, None), ('ready', ready, None),
        ('capacity', capacity, None), ('apply_validate', apply_validate, None),
        ('monitor_factory', monitor_factory, None), ('stop', stop, None), ('hosted', hosted, False))
        if value is not default}
    return implementation(root, **given)

def retire_stale_inference(root, deadline):
    from memvane.learning.context_learning.inference_service import _retire_stale as implementation
    return implementation(root, deadline)

# Wiki card body budget.

def selector_tokenizers(config, project_root):
    from memvane.learning.wiki_card_budget import selector_tokenizers as implementation
    return implementation(config, project_root)

def measure_card(card_text, tokenizer=None):
    from memvane.learning.wiki_card_budget import measure_card as implementation
    args = (card_text, tokenizer) if tokenizer is not None else (card_text,)
    return implementation(*args)

def card_budget_failure_fields(measured):
    from memvane.learning.wiki_card_budget import failure_fields as implementation
    return implementation(measured)

def emit_card_budget(root, outcome, *, bound, path='', title='', reason=None, **fields):
    from memvane.learning.wiki_card_budget import emit as implementation
    given = {key: value for key, value, default in (
        ('path', path, ''), ('title', title, ''), ('reason', reason, None))
        if value is not default and value != default}
    return implementation(root, outcome, bound=bound, **given, **fields)

def card_prompt_budget():
    from memvane.learning.wiki_card_budget import prompt_budget as implementation
    return implementation()

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
                     ensure_service=None, skip_busy=False, pair_budget=None, lane=None,
                     allow_admit_spawn=True):
    from memvane.learning.context_learning.selection_read import select_published as implementation
    given = {key: value for key, value, default in (
        ('deadline', deadline, None), ('ensure_service', ensure_service, None),
        ('skip_busy', skip_busy, False), ('pair_budget', pair_budget, None),
        ('lane', lane, None), ('allow_admit_spawn', allow_admit_spawn, True)) if value is not default}
    return implementation(pool, prompt=prompt, config=config, project_root=project_root,
                          wiki_names=wiki_names, scope_id=scope_id, **given)

def selector_prepared(config, project_root):
    from memvane.learning.context_learning.selection_read import selector_prepared as implementation
    return implementation(config, project_root)

def published_candidate_rows(pool, wiki_names, scope_id):
    from memvane.learning.context_learning.selection_port import published_candidate_rows as implementation
    return implementation(pool, wiki_names, scope_id)

def request_selector_maintenance(decision, *, force=False, owner=None, origin='session'):
    from memvane.learning.selector_maintenance import request as implementation
    given = {key: value for key, value, default in (
        ('force', force, False), ('owner', owner, None), ('origin', origin, 'session')) if value is not default}
    return implementation(decision, **given)

def due_selector_maintenance(project):
    from memvane.learning.selector_maintenance import due_request as implementation
    return implementation(project)

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

def inspect_project(project_root, *, check_invariants=False):
    from memvane.learning.project_inspection import inspect_project as implementation
    given = {'check_invariants': check_invariants} if check_invariants is not False else {}
    return implementation(project_root, **given)

def read_private_json(path):
    from memvane.learning.context_learning.dataset import read_private_json as implementation
    return implementation(path)

# Learning profile.

def activate_managed_runtime(executable, model_dir):
    from memvane.learning.runtime_activation import activate as implementation
    return implementation(executable, model_dir)

def learning_runtime_needs_activation():
    from memvane.learning.runtime_activation import needs_activation as implementation
    return implementation()

def learning_runtime_proof_failed():
    from memvane.learning.runtime_activation import failure_notice as implementation
    return implementation()

def learning_runtime_message(decision=None, host=None):
    """The learning runtime line; with ``decision`` and ``host`` also the default-teacher lines.

    Without them the value is the runtime line alone (None when there is none).
    """
    line = None
    if learning_runtime_proof_failed():
        from memvane.learning.runtime_activation import has_active_runtime
        if has_active_runtime():
            line = '[memvane] 학습 런타임 호환성 검사에 실패해 이전 선택을 유지했습니다. setup skill로 확인하세요.'
        else:
            line = '[memvane] 학습 런타임의 첫 활성화에 실패했습니다. setup skill로 확인하세요.'
    elif learning_runtime_needs_activation():
        line = '[memvane] 학습 런타임이 활성화되지 않았습니다. setup skill로 준비하고 활성화하세요.'
    if decision is None or host is None:
        return line
    try:
        from memvane.learning.context_learning.teacher_state import session_notices_for
        teacher = session_notices_for(decision, host)
    except Exception:
        teacher = []  # A hint only: SessionStart never fails on teacher state.
    return '\n'.join(([line] if line else []) + teacher) or None

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

def measure_card(card_text, tokenizer=None, *, failure_fields=False):
    """With ``failure_fields`` the value is ``(measured, its failure_fields)``."""
    from memvane.learning.wiki_card_budget import measure_card as implementation
    args = (card_text, tokenizer) if tokenizer is not None else (card_text,)
    measured = implementation(*args)
    if not failure_fields:
        return measured
    from memvane.learning.wiki_card_budget import failure_fields as fields
    return measured, fields(measured)

def emit_card_budget(root, outcome, *, bound, path='', title='', reason=None, **fields):
    from memvane.learning.wiki_card_budget import emit as implementation
    given = {key: value for key, value, default in (
        ('path', path, ''), ('title', title, ''), ('reason', reason, None))
        if value is not default and value != default}
    return implementation(root, outcome, bound=bound, **given, **fields)

def card_prompt_budget():
    from memvane.learning.wiki_card_budget import prompt_budget as implementation
    return implementation()

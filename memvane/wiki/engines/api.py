"""Public extractor context rendering."""
from __future__ import annotations


def render_existing_context_section(wiki):
    from memvane.wiki.engines.extractors.lib import render_existing_context_section as implementation
    return implementation(wiki)


def builtin_adapter_argv(engine):
    from memvane.wiki.engines.execution import _builtin_adapter_argv as implementation
    return implementation(engine)


def cooling_engines(path):
    from memvane.wiki.engines.execution import cooling_engines as implementation
    return implementation(path)


def extractor_engine_pool(compile_cfg):
    from memvane.wiki.engines.execution import extractor_engine_pool as implementation
    return implementation(compile_cfg)


def load_engine_cooldowns(path):
    from memvane.wiki.engines.execution import load_engine_cooldowns as implementation
    return implementation(path)


def reasoning_effort_audit(raw, requested=None, reason="capability_not_declared", capability_declared=True):
    from memvane.wiki.engines.execution import reasoning_effort_audit as implementation
    return implementation(raw, requested, reason, capability_declared)


def plan_engine_order(pool, producing, mode, attributable):
    from memvane.wiki.engines.execution import plan_engine_order as implementation
    return implementation(pool, producing, mode, attributable)


def resolve_extractor_argv(compile_cfg, engine, builtins=None):
    from memvane.wiki.engines.execution import resolve_extractor_argv as implementation
    return implementation(compile_cfg, engine, builtins)


def run_extractor(argv, payload, timeout, root):
    from memvane.wiki.engines.execution import run_extractor as implementation
    return implementation(argv, payload, timeout, root)


def set_engine_cooldown(path, key, seconds, *, _queue_lock_path, write_text_atomic):
    from memvane.wiki.engines.execution import set_engine_cooldown as implementation
    return implementation(path, key, seconds, _queue_lock_path=_queue_lock_path, write_text_atomic=write_text_atomic)

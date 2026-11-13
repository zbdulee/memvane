"""Public extractor context rendering."""
from __future__ import annotations


def render_existing_context_section(*args, **kwargs):
    from memvane.wiki.engines.extractors.lib import render_existing_context_section as implementation
    return implementation(*args, **kwargs)


def builtin_adapter_argv(*args, **kwargs):
    from memvane.wiki.engines.execution import _builtin_adapter_argv as implementation
    return implementation(*args, **kwargs)


def cooling_engines(*args, **kwargs):
    from memvane.wiki.engines.execution import cooling_engines as implementation
    return implementation(*args, **kwargs)


def extractor_engine_pool(*args, **kwargs):
    from memvane.wiki.engines.execution import extractor_engine_pool as implementation
    return implementation(*args, **kwargs)


def load_engine_cooldowns(*args, **kwargs):
    from memvane.wiki.engines.execution import load_engine_cooldowns as implementation
    return implementation(*args, **kwargs)


def plan_engine_order(*args, **kwargs):
    from memvane.wiki.engines.execution import plan_engine_order as implementation
    return implementation(*args, **kwargs)


def resolve_extractor_argv(*args, **kwargs):
    from memvane.wiki.engines.execution import resolve_extractor_argv as implementation
    return implementation(*args, **kwargs)


def run_extractor(*args, **kwargs):
    from memvane.wiki.engines.execution import run_extractor as implementation
    return implementation(*args, **kwargs)


def set_engine_cooldown(*args, **kwargs):
    from memvane.wiki.engines.execution import set_engine_cooldown as implementation
    return implementation(*args, **kwargs)

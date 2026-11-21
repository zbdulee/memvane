"""Public project settings operations.

Constants other domains read (project folder names, collection roles, compile
and verify caps, dedup constants, engine sentinels) live in ``types.py``.
"""
from __future__ import annotations as _annotations


# Project configuration.

def find_project_config(cwd):
    from memvane.settings.config import find_project_config as implementation
    return implementation(cwd)


def load_project_config(cwd):
    from memvane.settings.config import load_project_config as implementation
    return implementation(cwd)


def normalize_config(input_config, folder=None):
    from memvane.settings.config import normalize_config as implementation
    return implementation(input_config, folder)


def expand_minimal_settings(raw, folder=None):
    from memvane.settings.config import expand_minimal_settings as implementation
    return implementation(raw, folder)


def is_minimal_settings(raw):
    from memvane.settings.config import is_minimal_settings as implementation
    return implementation(raw)


def is_minimal_config(config):
    from memvane.settings.config import is_minimal_config as implementation
    return implementation(config)


def role_map(config):
    from memvane.settings.config import role_map as implementation
    return implementation(config)


def collection_role(roles, collection):
    from memvane.settings.config import collection_role as implementation
    return implementation(roles, collection)


def has_legacy_novel_collection(collections):
    from memvane.settings.config import has_legacy_novel_collection as implementation
    return implementation(collections)


def event_enabled(config, event_name):
    from memvane.settings.config import event_enabled as implementation
    return implementation(config, event_name)


def diagnostic_enabled(decision):
    from memvane.settings.diagnostic_consent import enabled as implementation
    return implementation(decision)


# Wiki compile settings.

def compile_active(compile_cfg):
    from memvane.settings.config import compile_active as implementation
    return implementation(compile_cfg)


def compile_mode(compile_cfg):
    from memvane.settings.config import compile_mode as implementation
    return implementation(compile_cfg)


def resolve_reasoning_effort(compile_cfg, engine, phase):
    from memvane.settings.config import resolve_reasoning_effort as implementation
    return implementation(compile_cfg, engine, phase)


def generation_skip_paths(config, selected_route=None):
    from memvane.settings.config import generation_skip_paths as implementation
    return implementation(config, selected_route)


# Project folder paths.

def folder(project):
    from memvane.settings.project_dir import folder as implementation
    return implementation(project)


def runtime_file(project, name):
    from memvane.settings.project_dir import runtime_file as implementation
    return implementation(project, name)


def settings_path(project):
    from memvane.settings.project_dir import settings_path as implementation
    return implementation(project)


def compile_dir(root, create=False):
    from memvane.settings.project_dir import compile_dir as implementation
    return implementation(root, create)


def ledger(root, name):
    from memvane.settings.project_dir import ledger as implementation
    return implementation(root, name)


def settings_lock(root, *, folder=None):
    """Context manager holding the exclusive settings writer lock of one project."""
    from memvane.settings.settings_lock import locked as implementation
    return implementation(root, folder=folder)


# Source eligibility and paths.

def is_source_text(name, extensions=None):
    """``extensions`` defaults to ``types.SOURCE_TEXT_EXTENSIONS``."""
    from memvane.settings.source_text import is_source_text as implementation
    if extensions is None:
        return implementation(name)
    return implementation(name, extensions)


def is_denied_source_path(rel_path):
    from memvane.settings.source_text import is_denied_source_path as implementation
    return implementation(rel_path)


def is_wiki_meta_page(path, wiki_root=None):
    from memvane.settings.source_text import is_wiki_meta_page as implementation
    return implementation(path, wiki_root)


def allowed_roots(config):
    from memvane.settings.resolve_paths import allowed_roots as implementation
    return implementation(config)


def contained_path(base, path_str, roots):
    from memvane.settings.resolve_paths import contained_path as implementation
    return implementation(base, path_str, roots)


def excluded_source(path, skip_paths):
    from memvane.settings.source_exclusion import excluded_source as implementation
    return implementation(path, skip_paths)


def select_collections(edited_paths, cwd, config):
    from memvane.settings.collection_match import select_collections as implementation
    return implementation(edited_paths, cwd, config)


def resolver(root, config):
    from memvane.settings.source_label import resolver as implementation
    return implementation(root, config)


def decision_resolver(decision):
    from memvane.settings.source_label import decision_resolver as implementation
    return implementation(decision)

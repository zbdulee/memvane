"""Public project settings operations."""
from __future__ import annotations



def diagnostic_enabled(*args, **kwargs):
    from memvane.settings.diagnostic_consent import enabled as implementation
    return implementation(*args, **kwargs)


def find_project_config(*args, **kwargs):
    from memvane.settings.config import find_project_config as implementation
    return implementation(*args, **kwargs)


def auxiliary_wiki_row(*args, **kwargs):
    from memvane.settings.source_text import auxiliary_wiki_row as implementation
    return implementation(*args, **kwargs)


def compile_rel(*args, **kwargs):
    from memvane.settings.project_dir import compile_rel as implementation
    return implementation(*args, **kwargs)



def compile_dir(*args, **kwargs):
    from memvane.settings.project_dir import compile_dir as implementation
    return implementation(*args, **kwargs)


def active_name(*args, **kwargs):
    from memvane.settings.project_dir import active_name as implementation
    return implementation(*args, **kwargs)



def folder(*args, **kwargs):
    from memvane.settings.project_dir import folder as implementation
    return implementation(*args, **kwargs)



def wiki_rel(*args, **kwargs):
    from memvane.settings.project_dir import wiki_rel as implementation
    return implementation(*args, **kwargs)



def is_folder_name(*args, **kwargs):
    from memvane.settings.project_dir import is_folder_name as implementation
    return implementation(*args, **kwargs)



def is_folder(*args, **kwargs):
    from memvane.settings.project_dir import is_folder as implementation
    return implementation(*args, **kwargs)



def runtime_file(*args, **kwargs):
    from memvane.settings.project_dir import runtime_file as implementation
    return implementation(*args, **kwargs)



def settings_path(*args, **kwargs):
    from memvane.settings.project_dir import settings_path as implementation
    return implementation(*args, **kwargs)



def settings_candidates(*args, **kwargs):
    from memvane.settings.project_dir import settings_candidates as implementation
    return implementation(*args, **kwargs)


def is_source_text(*args, **kwargs):
    from memvane.settings.source_text import is_source_text as implementation
    return implementation(*args, **kwargs)



def is_denied_source_path(*args, **kwargs):
    from memvane.settings.source_text import is_denied_source_path as implementation
    return implementation(*args, **kwargs)



def is_wiki_meta_page(*args, **kwargs):
    from memvane.settings.source_text import is_wiki_meta_page as implementation
    return implementation(*args, **kwargs)


def allowed_roots(*args, **kwargs):
    from memvane.settings.resolve_paths import allowed_roots as implementation
    return implementation(*args, **kwargs)



def contained_path(*args, **kwargs):
    from memvane.settings.resolve_paths import contained_path as implementation
    return implementation(*args, **kwargs)



def excluded_source(*args, **kwargs):
    from memvane.settings.source_exclusion import excluded_source as implementation
    return implementation(*args, **kwargs)






def select_collections(*args, **kwargs):
    from memvane.settings.collection_match import select_collections as implementation
    return implementation(*args, **kwargs)



def resolver(*args, **kwargs):
    from memvane.settings.source_label import resolver as implementation
    return implementation(*args, **kwargs)



def decision_resolver(*args, **kwargs):
    from memvane.settings.source_label import decision_resolver as implementation
    return implementation(*args, **kwargs)


def ledger(*args, **kwargs):
    from memvane.settings.project_dir import ledger as implementation
    return implementation(*args, **kwargs)


def load_project_config(*args, **kwargs):
    from memvane.settings.config import load_project_config as implementation
    return implementation(*args, **kwargs)


def normalize_config(*args, **kwargs):
    from memvane.settings.config import normalize_config as implementation
    return implementation(*args, **kwargs)


def role_map(*args, **kwargs):
    from memvane.settings.config import role_map as implementation
    return implementation(*args, **kwargs)


def expand_minimal_settings(*args, **kwargs):
    from memvane.settings.config import expand_minimal_settings as implementation
    return implementation(*args, **kwargs)


def is_minimal_settings(*args, **kwargs):
    from memvane.settings.config import is_minimal_settings as implementation
    return implementation(*args, **kwargs)


def compile_active(*args, **kwargs):
    from memvane.settings.config import compile_active as implementation
    return implementation(*args, **kwargs)

"""Public route-record and collection-root operations."""
from __future__ import annotations

from memvane.route.types import SCHEMA_V2, SETTINGS_IDENTITY_SCHEMA


def classify_v4(root, settings, record, *, inspect_store=True):
    from memvane.route.index_host_route import _classify_v4 as implementation
    return implementation(root, settings, record, inspect_store=inspect_store)


def derived_collections(record, root, settings_bytes):
    from memvane.route.index_host_route import _derived_collections as implementation
    return implementation(record, root, settings_bytes)


def multi_compile_supported(raw, cfg, root, wiki_root):
    from memvane.route.index_host_route import _multi_compile_supported as implementation
    return implementation(raw, cfg, root, wiki_root)


def multi_settings_supported(raw, cfg, names):
    from memvane.route.index_host_route import _multi_settings_supported as implementation
    return implementation(raw, cfg, names)


def pairs(items):
    from memvane.route.index_host_route import _pairs as implementation
    return implementation(items)


def read_regular(path, limit):
    from memvane.route.index_host_route import _read_regular as implementation
    return implementation(path, limit)


def source_for_collection(root, relative):
    from memvane.route.index_host_route import _source_for_collection as implementation
    return implementation(root, relative)
















def settings_identity_digest(project_root, collections):
    from memvane.route.index_host_route import settings_identity_digest as implementation
    return implementation(project_root, collections)



def wiki_index_roots(collections):
    from memvane.route.index_host_route import wiki_index_roots as implementation
    return implementation(collections)



def selected_collection_roots(project_root, collections):
    from memvane.route.index_host_route import selected_collection_roots as implementation
    return implementation(project_root, collections)


def candidate_paths(cwd):
    from memvane.route.index_host_route import _candidate_paths as implementation
    return implementation(cwd)


def canonical_absolute(value):
    from memvane.route.index_host_route import _canonical_absolute as implementation
    return implementation(value)


def selected_settings_supported(raw, cfg, collection):
    from memvane.route.index_host_route import _selected_settings_supported as implementation
    return implementation(raw, cfg, collection)


def vector_profile(value):
    from memvane.route.index_host_route import _vector_profile as implementation
    return implementation(value)


def decision(state, reason, **details):
    from memvane.route.index_host_route import _decision as implementation
    return implementation(state, reason, **details)


def foreign_format(record, *, marker=False):
    from memvane.route.index_host_route import _foreign_format as implementation
    return implementation(record, marker=marker)

"""Public route-record and collection-root operations."""
from __future__ import annotations

from memvane.route.types import SCHEMA_V2, SETTINGS_IDENTITY_SCHEMA


def classify_v4(*args, **kwargs):
    from memvane.route.index_host_route import _classify_v4 as implementation
    return implementation(*args, **kwargs)


def derived_collections(*args, **kwargs):
    from memvane.route.index_host_route import _derived_collections as implementation
    return implementation(*args, **kwargs)


def multi_compile_supported(*args, **kwargs):
    from memvane.route.index_host_route import _multi_compile_supported as implementation
    return implementation(*args, **kwargs)


def multi_settings_supported(*args, **kwargs):
    from memvane.route.index_host_route import _multi_settings_supported as implementation
    return implementation(*args, **kwargs)


def pairs(*args, **kwargs):
    from memvane.route.index_host_route import _pairs as implementation
    return implementation(*args, **kwargs)


def read_regular(*args, **kwargs):
    from memvane.route.index_host_route import _read_regular as implementation
    return implementation(*args, **kwargs)


def source_for_collection(*args, **kwargs):
    from memvane.route.index_host_route import _source_for_collection as implementation
    return implementation(*args, **kwargs)








def written_schema(*args, **kwargs):
    from memvane.route.index_host_route import written_schema as implementation
    return implementation(*args, **kwargs)








def settings_identity_digest(*args, **kwargs):
    from memvane.route.index_host_route import settings_identity_digest as implementation
    return implementation(*args, **kwargs)



def wiki_index_roots(*args, **kwargs):
    from memvane.route.index_host_route import wiki_index_roots as implementation
    return implementation(*args, **kwargs)



def selected_collection_roots(*args, **kwargs):
    from memvane.route.index_host_route import selected_collection_roots as implementation
    return implementation(*args, **kwargs)


def candidate_paths(*args, **kwargs):
    from memvane.route.index_host_route import _candidate_paths as implementation
    return implementation(*args, **kwargs)


def canonical_absolute(*args, **kwargs):
    from memvane.route.index_host_route import _canonical_absolute as implementation
    return implementation(*args, **kwargs)


def selected_settings_supported(*args, **kwargs):
    from memvane.route.index_host_route import _selected_settings_supported as implementation
    return implementation(*args, **kwargs)


def vector_profile(*args, **kwargs):
    from memvane.route.index_host_route import _vector_profile as implementation
    return implementation(*args, **kwargs)


def decision(*args, **kwargs):
    from memvane.route.index_host_route import _decision as implementation
    return implementation(*args, **kwargs)

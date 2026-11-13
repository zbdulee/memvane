"""Public route-record and collection-root operations."""
from __future__ import annotations



def settings_identity_digest(*args, **kwargs):
    from memvane.route.index_host_route import settings_identity_digest as implementation
    return implementation(*args, **kwargs)



def wiki_index_roots(*args, **kwargs):
    from memvane.route.index_host_route import wiki_index_roots as implementation
    return implementation(*args, **kwargs)



def selected_collection_roots(*args, **kwargs):
    from memvane.route.index_host_route import selected_collection_roots as implementation
    return implementation(*args, **kwargs)

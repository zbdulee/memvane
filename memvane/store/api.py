"""Public store operations.

``open_store`` returns an open view store whose public methods (``capture_view``,
``head``, ``pin_view``, ``release``, ``prepare_generation``, ``publish_generation``,
``fill_view``, ``search_exact``, ``close`` ...) callers use directly; every other
store read or write other domains need is a function here.
"""


# Checkout values.

def capture_matches(capture, repository, worktree, *, deadline=None, fresh=False):
    from memvane.store.checkout.checkout_view import capture_matches as implementation
    return implementation(capture, repository, worktree, deadline=deadline, fresh=fresh)


def capture_view(project_root, repository, worktree, *, deadline=None, fresh=False):
    from memvane.store.checkout.checkout_view import capture_view as implementation
    return implementation(project_root, repository, worktree, deadline=deadline, fresh=fresh)


# Vector and blob values.

def provider_spec(manifest, vector_profile):
    from memvane.store.view.view_vectors import provider_spec as implementation
    return implementation(manifest, vector_profile)


def register_provider_spec(store, manifest, vector_profile):
    from memvane.store.view.view_vectors import register_provider_spec as implementation
    return implementation(store, manifest, vector_profile)


def qmd_first_pass(body, max_chars=2700, overlap_chars=405, window_chars=600):
    from memvane.store.view.view_vectors import qmd_first_pass as implementation
    return implementation(body, max_chars, overlap_chars, window_chars)


def query_values(value):
    from memvane.store.view.view_vectors import query_values as implementation
    return implementation(value)


def profile(spec):
    from memvane.store.blob.blob_values import profile as implementation
    return implementation(spec)


def regular(root, relative, limit):
    from memvane.store.blob.blob_store import _regular as implementation
    return implementation(root, relative, limit)


def digest(domain, value):
    from memvane.store.blob.blob_values import digest as implementation
    return implementation(domain, value)


def markdown_title(body, blob_sha):
    from memvane.store.blob.blob_values import markdown_title as implementation
    return implementation(body, blob_sha)


def provenance_digests(verdict, result):
    from memvane.store.blob.blob_values import provenance_digests as implementation
    return implementation(verdict, result)


def relative(value):
    from memvane.store.blob.blob_values import relative as implementation
    return implementation(value)


def admitted_paths(saved, found):
    from memvane.store.application.store_ops import admitted_paths as implementation
    return implementation(saved, found)


# Store lifecycle and identity.

def observe_binding(path, *, scope_id, repository=None, worktree_id=None, create_root=None, reader=False):
    from memvane.store.application.store_ops import observe_binding as implementation
    return implementation(path, scope_id=scope_id, repository=repository, worktree_id=worktree_id,
                          create_root=create_root, reader=reader)


def open_store(path, *, repository, scope_id, reader=False, deadline=None):
    from memvane.store.application.store_ops import open_store as implementation
    return implementation(path, repository=repository, scope_id=scope_id, reader=reader, deadline=deadline)


def store_identity(store, handle=None):
    from memvane.store.application.store_ops import store_identity as implementation
    return implementation(store, handle)


def read_header(path, *, project_root=None, worktree_id=None, recover=False):
    from memvane.store.application.store_ops import read_header as implementation
    return implementation(path, project_root=project_root, worktree_id=worktree_id, recover=recover)


# Pinned reads.

def pinned_vector(store, pin):
    from memvane.store.application.store_ops import pinned_vector as implementation
    return implementation(store, pin)


def vector_profile(store, profile_id, *, session):
    from memvane.store.application.store_ops import vector_profile as implementation
    return implementation(store, profile_id, session=session)


def eligible_members(store, gen_id, *, deadline, limit):
    from memvane.store.application.store_ops import eligible_members as implementation
    return implementation(store, gen_id, deadline=deadline, limit=limit)


def worktree_wiki_root(store, worktree_id, *, deadline):
    from memvane.store.application.store_ops import worktree_wiki_root as implementation
    return implementation(store, worktree_id, deadline=deadline)


# The sync owner's view pass.

def read_view_baseline(store, handle, head, *, worktree_id=None, head_ordinal=False):
    from memvane.store.application.view_sync import read_view_baseline as implementation
    return implementation(store, handle, head, worktree_id=worktree_id, head_ordinal=head_ordinal)


def head_same(store, head, baseline, observed, handle):
    from memvane.store.application.view_sync import head_same as implementation
    return implementation(store, head, baseline, observed, handle)


def compile_evidence_stored(store, handle, intent):
    from memvane.store.application.view_sync import compile_evidence_stored as implementation
    return implementation(store, handle, intent)


def linked_worktree_id(store, info, project, deadline, mark_write):
    from memvane.store.application.view_sync import linked_worktree_id as implementation
    return implementation(store, info, project, deadline, mark_write)


def embedding_sets_missing(store, blobs, recipe_id, profile_id):
    from memvane.store.application.view_sync import embedding_sets_missing as implementation
    return implementation(store, blobs, recipe_id, profile_id)


def embedding_coverage(store, targets, recipe_id, profile_id, *, first_from=None):
    from memvane.store.application.view_sync import embedding_coverage as implementation
    return implementation(store, targets, recipe_id, profile_id, first_from=first_from)


def fill_plan(store, handle, recipe_id, profile_id):
    from memvane.store.application.view_sync import fill_plan as implementation
    return implementation(store, handle, recipe_id, profile_id)


def put_source_fingerprints(store, handle, scan, observed):
    from memvane.store.application.view_sync import put_source_fingerprints as implementation
    return implementation(store, handle, scan, observed)

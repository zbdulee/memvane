"""Provider re-attestation by content: moved files keep their vector profile.

A selected install's attestation names its embedding provider by paths and
by content. Vectors depend only on content: provider and worker bytes, node,
node-llama-cpp entry, native closure, model, runtime/recipe ids, dimension.
When a later review finds the same content at other paths (a new bundle
root, a laya-owned runtime directory, a per-user service socket), the
reviewed provider is re-expressed under the install's existing vector
profile instead of forcing a rebuild.

The frozen provider re-derives the recipe fingerprint from the manifest's
``dependencyClosureSha256``. That digest covers absolute closure paths, so a
moved closure keeps the reviewed digest in the manifest and the attestation
carries the reviewed rows (``profileDependencyClosure``); the validator
proves the live rows are the same files by layout and content.

Any content change (model, dimension, runtime, recipe, provider, worker,
node, module entry or native bytes) is not equivalent: rebuild required.
"""
from __future__ import annotations

import hashlib

from laya_product_bundle import (PROFILE_CLOSURE_FIELD, _canonical,
                                 closure_layout)

# Manifest values that define the embedding content. Paths are excluded.
CONTENT_KEYS = ('approvedProviderId', 'providerArtifactSha256',
                'providerRelativePath', 'workerSha256', 'nodeSha256',
                'moduleEntrySha256', 'modelSha256', 'runtimeId', 'modelId',
                'recipeId', 'dimension', 'dependencyClosureApproved')
# Derived from the absolute closure paths, so they move with the closure.
CLOSURE_DERIVED_KEYS = frozenset(('approvedProviderId', 'embeddingFingerprint',
                                  'dependencyClosureSha256'))
PROFILE_CONTENT_KEYS = ('modelId', 'dimension', 'metric',
                        'providerArtifactSha256', 'providerRelativePath')


def _sha(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _layout(closure):
    try:
        return closure_layout(closure)
    except Exception:
        return None


def _reviewed_closure(attestation):
    """The closure rows the attested vector profile was derived from."""
    return attestation.get(PROFILE_CLOSURE_FIELD) or attestation.get('dependencyClosure')


def reattest(old_attestation, reviewed_provider):
    """The reviewed provider under the old vector profile, or None.

    ``reviewed_provider`` is ``review_local_provider()['reviewedProvider']``.
    The result has the shape a new attestation needs: ``vectorProfile``,
    ``providerManifest``, ``dependencyClosure`` and, only when the closure
    moved, ``profileDependencyClosure``.
    """
    if not isinstance(old_attestation, dict) or not isinstance(reviewed_provider, dict):
        return None
    old_manifest = old_attestation.get('providerManifest')
    old_profile = old_attestation.get('vectorProfile')
    new_manifest = reviewed_provider.get('providerManifest')
    new_profile = reviewed_provider.get('vectorProfile')
    new_closure = reviewed_provider.get('dependencyClosure')
    reviewed_closure = _reviewed_closure(old_attestation)
    if (not all(isinstance(value, dict) for value in
                (old_manifest, old_profile, new_manifest, new_profile)) or
            set(new_manifest) != set(old_manifest)):
        return None
    same_closure_paths = new_closure == reviewed_closure
    content_keys = [key for key in CONTENT_KEYS
                    if not (key in CLOSURE_DERIVED_KEYS and not same_closure_paths)]
    if any(new_manifest.get(key) != old_manifest.get(key) for key in content_keys):
        return None
    if any(new_profile.get(key) != old_profile.get(key) for key in PROFILE_CONTENT_KEYS):
        return None
    if same_closure_paths:
        # Same closure rows: the profile and its derived ids must be identical.
        if (new_profile != old_profile or
                any(new_manifest.get(key) != old_manifest.get(key)
                    for key in CLOSURE_DERIVED_KEYS)):
            return None
        return {'vectorProfile': old_profile, 'providerManifest': new_manifest,
                'dependencyClosure': new_closure}
    old_layout, new_layout = _layout(reviewed_closure), _layout(new_closure)
    if old_layout is None or old_layout != new_layout:
        return None
    if old_manifest.get('dependencyClosureSha256') != _sha(reviewed_closure):
        return None
    if not _fingerprint_reproduced(old_manifest, new_manifest, old_profile):
        return None
    manifest = {**new_manifest,
                **{key: old_manifest[key] for key in CLOSURE_DERIVED_KEYS
                   if key in old_manifest}}
    return {'vectorProfile': old_profile, 'providerManifest': manifest,
            'dependencyClosure': new_closure,
            PROFILE_CLOSURE_FIELD: reviewed_closure}


def _fingerprint_reproduced(old_manifest, new_manifest, old_profile):
    """New content plus the reviewed closure digest gives the old profile."""
    from laya_index_vector import profile_identity
    from laya_local_embed_provider import recipe_fingerprint
    try:
        fingerprint = recipe_fingerprint(
            model_id=new_manifest['modelId'], model_sha256=new_manifest['modelSha256'],
            runtime_id=new_manifest['runtimeId'], recipe_id=new_manifest['recipeId'],
            provider_sha256=new_manifest['providerArtifactSha256'],
            worker_sha256=new_manifest['workerSha256'],
            node_sha256=new_manifest['nodeSha256'],
            module_entry_sha256=new_manifest['moduleEntrySha256'],
            closure_sha256=old_manifest['dependencyClosureSha256'])
        profile = profile_identity(new_manifest['modelId'], fingerprint,
                                   new_manifest['dimension'])
    except (KeyError, TypeError, ValueError):
        return False
    return (fingerprint == old_profile.get('embeddingFingerprint') ==
            old_manifest.get('embeddingFingerprint') and
            profile == old_profile.get('profileId'))


def attestation_record(*, proof, journal_path, reattested, schema):
    """A production attestation for ``reattested`` under ``proof``'s bundle."""
    record = {'schema': schema,
              'sourceCommit': proof['sourceCommit'],
              'bundleManifestSha256': proof['bundleManifestSha256'],
              'ownershipManifestSha256': proof['ownershipManifestSha256'],
              'journalPath': str(journal_path),
              'vectorProfile': proof['vectorProfile'],
              'providerManifest': reattested['providerManifest'],
              'dependencyClosure': reattested['dependencyClosure']}
    if PROFILE_CLOSURE_FIELD in reattested:
        record[PROFILE_CLOSURE_FIELD] = reattested[PROFILE_CLOSURE_FIELD]
    return record

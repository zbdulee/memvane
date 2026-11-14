"""S6: the fixture provider: the only code that replaces the installed provider.

``FixtureProvider`` is a provider port (``memvane.provider.fixture_port``):
the review answers ``review_required`` with a fixed proposal digest, the
sealed runtime files digest is fixed, the per-file provider identity proof
returns the recorded manifest (or refuses with ``provider_identity_changed``
after ``change_identity()``), the reviewed provider gives one fixed vector per
first-pass chunk, the query admission refuses (recall stays lexical) and the
warm service is a no-op.

In-process use cases take it as their ``provider=`` argument; ``install(case)``
also writes the test runtime root's ``provider-fixture.json`` so the hook,
route and wake paths the use cases reach (and child processes) find the same
port. Nothing here touches a real runtime, model or the account home.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

SPEC_SCHEMA = 'memvane-test-provider-fixture-v1'
MODEL = 'synthetic-precomputed-only'
FINGERPRINT = 'f' * 64
PROVIDER_ID = 'synthetic-provider'
REVIEW_SHA = '9' * 64
RUNTIME_FILES_SHA = 'c' * 64
ARTIFACT_SHA = 'b' * 64
MODEL_SHA = 'a' * 64
RECIPE = 'qmd-regex-token-chunks-candidate-v1'
LEXICAL_REASON = 'fixture_lexical_recall'


def _spec_path():
    raw = os.environ.get('MEMVANE_TEST_RUNTIME_ROOT')
    if not raw or not os.path.isabs(raw):
        raise RuntimeError('fixture provider needs an absolute MEMVANE_TEST_RUNTIME_ROOT')
    from memvane.provider.types import FIXTURE_FILE
    return Path(raw) / FIXTURE_FILE


class _Service:
    def stop(self):
        pass


class _Handle:
    """A reviewed provider handle: the view recipe's first-pass chunks, one fixed vector each."""

    def __init__(self, manifest, profile, vector):
        self.manifest = dict(manifest)
        self.profile = profile
        self._vector = list(vector)

    def __call__(self, prompt, sealed_profile):
        return list(self._vector)

    def embed_document_chunks(self, body, path, *, timeout=120):
        from memvane.store.api import qmd_first_pass
        return [{'seq': seq, 'pos': pos, 'vector': list(self._vector)}
                for seq, (pos, _text) in enumerate(qmd_first_pass(body))]

    def embed_document(self, body, title=None):
        return list(self._vector)


class FixtureProvider:
    def __init__(self, dimension=2, *, identity_changed=False):
        if type(dimension) is not int or dimension < 1:
            raise ValueError('dimension')
        self.dimension = dimension
        self.identity_changed = identity_changed
        self.vector = [1.0] + [0.0] * (dimension - 1)
        self._path = None

    # -- values ------------------------------------------------------------------
    def vector_profile(self):
        from memvane.provider.api import profile_identity
        from memvane.kernel.bundle_paths import PROVIDER_RELATIVE_PATH
        return {'profileId': profile_identity(MODEL, FINGERPRINT, self.dimension), 'modelId': MODEL,
                'embeddingFingerprint': FINGERPRINT, 'dimension': self.dimension, 'metric': 'cosine',
                'approvedProviderId': PROVIDER_ID, 'providerArtifactSha256': ARTIFACT_SHA,
                'providerRelativePath': PROVIDER_RELATIVE_PATH}

    def manifest(self, home, state_root):
        from memvane.kernel.bundle_paths import PROVIDER_RELATIVE_PATH
        from memvane.kernel.memvane_paths import install_socket
        return {'approvedProviderId': PROVIDER_ID, 'providerArtifactSha256': ARTIFACT_SHA,
                'providerRelativePath': PROVIDER_RELATIVE_PATH, 'recipeId': RECIPE, 'modelId': MODEL,
                'modelSha256': MODEL_SHA, 'embeddingFingerprint': FINGERPRINT,
                'moduleEntryPath': str(Path(home) / 'fixture-module'),
                'serviceSocketPath': str(install_socket(Path(state_root))), 'dimension': self.dimension}

    def spec(self):
        return {'schema': SPEC_SCHEMA, 'dimension': self.dimension,
                'identityChanged': self.identity_changed}

    # -- port --------------------------------------------------------------------
    def review(self, *, plugin_root=None, state_root, model_path=None, dimension=None,
               runtime_generation=None):
        home = os.environ['HOME']
        return {'status': 'review_required', 'proposalSha256': REVIEW_SHA,
                'reviewedProvider': {'vectorProfile': self.vector_profile(),
                                     'providerManifest': self.manifest(home, state_root),
                                     'dependencyClosure': []}}

    def sealed_files_sha256(self, path):
        return RUNTIME_FILES_SHA

    def provider_manifest(self, record, profile, *, allow_local_unavailable=False):
        """The attested manifest as recorded; this host has no reviewed runtime files."""
        if self.identity_changed:
            from memvane.journal.types import JournalEvidenceError
            raise JournalEvidenceError('provider_identity_changed')
        manifest = dict(record['providerManifest'])
        return (manifest, True) if allow_local_unavailable else manifest

    def reviewed_provider(self, manifest):
        profile = {'profile_id': self.vector_profile()['profileId'], 'model_id': MODEL,
                   'embedding_fingerprint': FINGERPRINT, 'dimension': self.dimension,
                   'metric': 'cosine'}
        return _Handle(manifest, profile, self.vector)

    def admit_query(self, *, coordinator=None, report=None):
        from memvane.kernel.resource_deferred import ResourceDeferred
        raise ResourceDeferred(LEXICAL_REASON)

    def start_service(self, manifest, *, admit=None):
        return _Service()

    # -- seam --------------------------------------------------------------------
    def install(self, case):
        """Publish this port at the test runtime root for the paths no argument reaches."""
        self._path = _spec_path()
        self._publish()
        case.addCleanup(self.uninstall)
        return self

    def uninstall(self):
        if self._path is not None:
            try:
                os.unlink(self._path)
            except FileNotFoundError:
                pass
            self._path = None

    def change_identity(self, changed=True):
        """From now on every identity proof refuses with ``provider_identity_changed``."""
        self.identity_changed = changed
        if self._path is not None:
            self._publish()

    def _publish(self):
        raw = json.dumps(self.spec(), sort_keys=True).encode('utf-8')
        temporary = self._path.with_name('.' + self._path.name + '.partial')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, raw)
        finally:
            os.close(fd)
        os.replace(temporary, self._path)


def port_from_spec(spec):
    """The port ``memvane.provider.fixture_port`` builds from ``provider-fixture.json``."""
    if not isinstance(spec, dict) or spec.get('schema') != SPEC_SCHEMA:
        raise ValueError('provider_fixture_invalid')
    return FixtureProvider(spec['dimension'], identity_changed=spec.get('identityChanged') is True)

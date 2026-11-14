"""The one place a test replaces the installed provider (inert in production).

Production obtains the provider review, the sealed runtime files digest, the
per-file provider identity proof, the reviewed provider handle, the query
admission and the warm embedding service from the installed Laya runtime.
A test replaces all six through a *port*: an object with

    review(*, plugin_root, state_root, model_path, dimension, runtime_generation) -> dict
    sealed_files_sha256(path) -> str
    provider_manifest(record, profile, *, allow_local_unavailable) -> manifest | (manifest, bool)
        (refuses with ``types.RuntimeUnavailable(reason)``)
    reviewed_provider(manifest) -> types.ReviewedProviderHandle
    admit_query(*, coordinator, report) -> None (raises to refuse)
    start_service(manifest, *, admit) -> object with stop()

Two product checks consult only the test runtime root's spec, through
``active`` (the same gate as ``resolve``; an unsafe or invalid spec file
counts as absent there, so their production path stays unchanged):
``api.is_reviewed_provider`` also accepts exactly this module's handle, and
the query embed child answers from ``FixtureProvider.query_result``.

An in-process caller passes the port as the use case's ``provider=`` (or
``port=``) argument. A process the test cannot hand an argument to (a hook,
a wake) finds it only while ``MEMVANE_TEST_RUNTIME_ROOT`` names an absolute
path holding an owner-only ``provider-fixture.json``: a declarative spec
that ``FixtureProvider`` below answers from. Without the argument and that
file ``resolve`` returns None and production code runs.

The spec (``schema`` ``memvane-test-provider-fixture-v1``)::

    dimension          vector length (1..2048)
    vector             the vector of every chunk, document and query
                       (default: 1.0 then zeros)
    proposalSha256     the review's proposal digest (default '9' * 64)
    runtimeFilesSha256 the sealed runtime files digest (default 'c' * 64)
    identityChanged    every identity proof refuses with provider_identity_changed
    queryAdmission     'refuse' (recall stays lexical, the default) or 'admit'
    queryEmbedding     what the query embed child answers: 'vector' (the spec
                       vector, the default) or 'provider_unavailable' (a cold
                       provider)

The fixture handle embeds one chunk per document: a body over the first
chunking pass's single-chunk bound (2,700 UTF-16 units) is refused.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat

from memvane.provider.types import FIXTURE_FILE, RuntimeUnavailable

SPEC_SCHEMA = 'memvane-test-provider-fixture-v1'
_FIXTURE_LIMIT = 64 * 1024
_SINGLE_CHUNK_UNITS = 2700
_SHA = re.compile(r'[0-9a-f]{64}')
MODEL = 'synthetic-precomputed-only'
FINGERPRINT = 'f' * 64
PROVIDER_ID = 'synthetic-provider'
ARTIFACT_SHA = 'b' * 64
MODEL_SHA = 'a' * 64
RECIPE = 'qmd-regex-token-chunks-candidate-v1'
LEXICAL_REASON = 'fixture_lexical_recall'


def resolve(port=None):
    """``port`` when given, else the test runtime root's fixture port, else None."""
    if port is not None:
        return port
    raw = os.environ.get('MEMVANE_TEST_RUNTIME_ROOT')
    if not raw or not os.path.isabs(raw):
        return None
    try:
        fd = os.open(os.path.join(raw, FIXTURE_FILE), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > _FIXTURE_LIMIT):
            raise ValueError('provider_fixture_unsafe')
        raw_spec = os.read(fd, _FIXTURE_LIMIT + 1)
    finally:
        os.close(fd)
    try:
        spec = json.loads(raw_spec.decode('utf-8'))
    except (UnicodeError, ValueError) as exc:
        raise ValueError('provider_fixture_invalid') from exc
    return FixtureProvider(spec)


def active():
    """The test runtime root's fixture port, else None (also for an unsafe or invalid spec file).

    For checks whose path without the seam must stay exactly production:
    ``api.is_reviewed_provider`` and the query embed child.
    """
    try:
        return resolve()
    except (OSError, ValueError):
        return None


class _Service:
    def stop(self):
        pass


class _Handle:
    """One fixed vector per document; the document is its single first-pass chunk."""

    def __init__(self, manifest, profile, vector):
        self.manifest = dict(manifest)
        self.profile = profile
        self._vector = vector

    def __call__(self, prompt, sealed_profile):
        return list(self._vector)

    def embed_document(self, body, title=None):
        return list(self._vector)

    def embed_document_chunks(self, body, path, *, timeout=120):
        from memvane.kernel.errors import IndexContractError
        if sum(2 if ord(ch) > 0xFFFF else 1 for ch in body) > _SINGLE_CHUNK_UNITS:
            raise IndexContractError('provider_fixture_body_too_long')
        return [{'seq': 0, 'pos': 0, 'vector': list(self._vector)}]


class FixtureProvider:
    """The deterministic provider a fixture spec describes (see the module docstring)."""

    def __init__(self, spec):
        if not isinstance(spec, dict) or spec.get('schema') != SPEC_SCHEMA:
            raise ValueError('provider_fixture_invalid')
        dimension = spec.get('dimension')
        if type(dimension) is not int or not 1 <= dimension <= 2048:
            raise ValueError('provider_fixture_invalid')
        vector = spec.get('vector', [1.0] + [0.0] * (dimension - 1))
        proposal = spec.get('proposalSha256', '9' * 64)
        runtime = spec.get('runtimeFilesSha256', 'c' * 64)
        admission = spec.get('queryAdmission', 'refuse')
        embedding = spec.get('queryEmbedding', 'vector')
        changed = spec.get('identityChanged', False)
        if (not isinstance(vector, list) or len(vector) != dimension
                or not all(type(x) in (int, float) for x in vector)
                or not all(isinstance(x, str) and _SHA.fullmatch(x) for x in (proposal, runtime))
                or admission not in ('refuse', 'admit')
                or embedding not in ('vector', 'provider_unavailable') or type(changed) is not bool):
            raise ValueError('provider_fixture_invalid')
        self.dimension = dimension
        self.vector = [float(x) for x in vector]
        self.proposal_sha256, self.runtime_files_sha256 = proposal, runtime
        self.query_admission, self.identity_changed = admission, changed
        self.query_embedding = embedding

    def vector_profile(self):
        from memvane.kernel.bundle_paths import PROVIDER_RELATIVE_PATH
        from memvane.provider.domain.profile import profile_identity
        return {'profileId': profile_identity(MODEL, FINGERPRINT, self.dimension), 'modelId': MODEL,
                'embeddingFingerprint': FINGERPRINT, 'dimension': self.dimension, 'metric': 'cosine',
                'approvedProviderId': PROVIDER_ID, 'providerArtifactSha256': ARTIFACT_SHA,
                'providerRelativePath': PROVIDER_RELATIVE_PATH}

    def manifest(self, state_root):
        from memvane.kernel.bundle_paths import PROVIDER_RELATIVE_PATH
        from memvane.kernel.memvane_paths import install_socket
        return {'approvedProviderId': PROVIDER_ID, 'providerArtifactSha256': ARTIFACT_SHA,
                'providerRelativePath': PROVIDER_RELATIVE_PATH, 'recipeId': RECIPE, 'modelId': MODEL,
                'modelSha256': MODEL_SHA, 'embeddingFingerprint': FINGERPRINT,
                'moduleEntryPath': str(Path.home() / 'fixture-module'),
                'serviceSocketPath': str(install_socket(Path(state_root))), 'dimension': self.dimension}

    # -- port --------------------------------------------------------------------
    def review(self, *, plugin_root=None, state_root, model_path=None, dimension=None,
               runtime_generation=None):
        return {'status': 'review_required', 'proposalSha256': self.proposal_sha256,
                'reviewedProvider': {'vectorProfile': self.vector_profile(),
                                     'providerManifest': self.manifest(state_root),
                                     'dependencyClosure': []}}

    def sealed_files_sha256(self, path):
        return self.runtime_files_sha256

    def provider_manifest(self, record, profile, *, allow_local_unavailable=False):
        """The attested manifest as recorded; a fixture host has no reviewed runtime files."""
        if self.identity_changed:
            raise RuntimeUnavailable('provider_identity_changed')
        manifest = dict(record['providerManifest'])
        return (manifest, True) if allow_local_unavailable else manifest

    def reviewed_provider(self, manifest):
        profile = {'profile_id': self.vector_profile()['profileId'], 'model_id': MODEL,
                   'embedding_fingerprint': FINGERPRINT, 'dimension': self.dimension,
                   'metric': 'cosine'}
        return _Handle(manifest, profile, self.vector)

    def admit_query(self, *, coordinator=None, report=None):
        if self.query_admission == 'refuse':
            from memvane.kernel.resource_deferred import ResourceDeferred
            raise ResourceDeferred(LEXICAL_REASON)

    def start_service(self, manifest, *, admit=None):
        return _Service()

    # -- product checks (through ``active``) ---------------------------------------
    def is_handle(self, value):
        """True for exactly a handle this module's ``reviewed_provider`` returns."""
        return type(value) is _Handle

    def query_result(self, request):
        """The query embed child's answer: ``{'vector': [...]}`` or ``{'error': code}``."""
        if self.query_embedding == 'vector':
            return {'vector': list(self.vector)}
        code = self.query_embedding
        return {'error': code if code in request['allowed'] else 'provider_result_invalid'}

"""Selected installation with a real sealed generation made from synthetic files."""
import hashlib
import json
import os
from unittest.mock import patch

from memvane.journal import api as journal
from memvane.kernel import bundle_paths, memvane_paths
from memvane.provider import api as provider
from tests.support.case import FixtureCase
from tests.support.install_builder import build_install, fixture_provider
from tests.support.project import make_project


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def fixture_generation():
    """Publish four synthetic files with a real v1 generation manifest and active pointer."""
    module = 'node_modules/node-llama-cpp/dist/index.js'
    native = 'node_modules/@node-llama-cpp/mac-arm64-metal'
    model = 'models/embeddinggemma-300M-Q8_0.gguf'
    data = {'bin/node': b'node', module: b'entry', native + '/addon.node': b'native', model: b'GGUFfixture'}
    record = dict(schema='laya-embed-runtime-v1', runtimeId='qmd-2.5.3-node-llama-cpp-3.18.1', node='bin/node',
                  moduleEntry=module, model=model, nativePackage=native,
                  files=[[name, hashlib.sha256(raw).hexdigest(), len(raw), False] for name, raw in sorted(data.items())])
    raw = encoded(record)
    name = 'g-' + hashlib.sha256(raw).hexdigest()[:32]
    root = memvane_paths.state_base() / 'embed-runtime'
    generation = root / 'generations' / name
    mask = os.umask(0o077)
    try:
        for rel, content in {**data, 'runtime.json': raw}.items():
            path = generation / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (root / 'active.json').write_bytes(encoded({'schema': record['schema'], 'generation': name}))
    finally:
        os.umask(mask)
    return provider.select()


class AttestedInstall(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home)
        note = self.project.write_note('notes.md', 'A synthetic provider proof.')
        self.project.write_card('proof.md', title='Synthetic', body='Synthetic provider proof.', sources=[note])
        self.project.commit()
        self.port = fixture_provider(self)
        self.state = memvane_paths.install_root('4' * 32)
        runtime = fixture_generation()
        worker = bundle_paths.bundle_file(bundle_paths.WORKER_RELATIVE_PATH)
        artifact = bundle_paths.bundle_file(bundle_paths.PROVIDER_RELATIVE_PATH)
        self.manifest = m = dict(self.port.manifest(self.state), modelId=provider.default_embed_model_uri(),
                                 workerSha256=provider.hash_file(str(worker)), providerArtifactSha256=provider.hash_file(str(artifact)),
                                 recipeId='qmd-regex-token-chunks-candidate-v1', dependencyClosureApproved=True,
                                 dependencyClosureSha256=hashlib.sha256(encoded(runtime['dependencyClosure'])).hexdigest())
        for key in ('nodePath', 'nodeSha256', 'moduleEntryPath', 'moduleEntrySha256', 'modelPath', 'modelSha256', 'runtimeId'):
            m[key] = runtime[key]
        m['embeddingFingerprint'] = provider.recipe_fingerprint(model_id=m['modelId'], model_sha256=m['modelSha256'],
            runtime_id=m['runtimeId'], recipe_id=m['recipeId'], provider_sha256=m['providerArtifactSha256'],
            worker_sha256=m['workerSha256'], node_sha256=m['nodeSha256'], module_entry_sha256=m['moduleEntrySha256'],
            closure_sha256=m['dependencyClosureSha256'])
        profile = {key: m[key] for key in ('modelId', 'embeddingFingerprint', 'dimension', 'approvedProviderId',
                                          'providerArtifactSha256', 'providerRelativePath')}
        profile.update(metric='cosine', profileId=provider.profile_identity(m['modelId'], m['embeddingFingerprint'], m['dimension']))
        review = self.port.review(state_root=self.state)
        review['reviewedProvider'].update(providerManifest=m, vectorProfile=profile, dependencyClosure=runtime['dependencyClosure'])
        with patch.object(self.port, 'review', return_value=review), \
             patch.object(self.port, 'sealed_files_sha256', return_value=runtime[provider.FILES_SHA]):
            self.install = build_install(self, self.project, provider=self.port)
        self.decision = self.install.select()
        self.port.uninstall()
        self.proof = journal.verify_selected_route_for_write(self.decision)

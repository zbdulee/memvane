"""Opt-in real embed worker smoke; all mutable state is disposable."""
import json
import os
from pathlib import Path
import pwd
import sys
import tempfile
from unittest import mock

root = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(root))
real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
short_temp = Path('/private/tmp') if Path('/private/tmp').is_dir() else Path('/tmp')
with tempfile.TemporaryDirectory(prefix='mvsm-', dir=short_temp) as directory:
    home = Path(directory).resolve()
    os.environ.update(HOME=str(home), TMPDIR=str(home),
                      MEMVANE_TEST_RUNTIME_ROOT=str(home / 'runtime'),
                      MEMVANE_TEST_ALLOW_UNINSTALLED_ROOT='1', PYTHONDONTWRITEBYTECODE='1')
    for key in ('QMD_SETUP_GUARD_FIXTURE', 'QMD_SANDBOX', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'HERMES_HOME'):
        os.environ.pop(key, None)
    from memvane.provider import api as provider_api
    from memvane.provider import runtime_digests
    from memvane.resident.resident_service import default_admit, default_start_worker
    # Selecting verifies live runtime files; do not update its digest cache.
    with mock.patch.object(runtime_digests.Digests, 'flush'):
        selected = provider_api.select(home=real_home)
    provider = root / 'memvane/provider/adapters/local_embed_provider.py'
    worker = root / 'memvane/provider/adapters/laya_local_embed_worker.mjs'
    manifest = {'approvedProviderId':'model-smoke', 'providerArtifactSha256':provider_api.hash_file(str(provider)),
        'providerRelativePath':'memvane/provider/adapters/local_embed_provider.py', 'providerPath':str(provider),
        'workerPath':str(worker), 'workerSha256':provider_api.hash_file(str(worker)),
        **{key:selected[key] for key in ('nodePath','nodeSha256','moduleEntryPath','moduleEntrySha256',
                                         'modelPath','modelSha256','runtimeId')},
        'modelId':provider_api.default_embed_model_uri(), 'dimension':768, 'recipeId':'qmd-regex-token-chunks-candidate-v1',
        'dependencyClosureApproved':True, 'dependencyClosureSha256':'d'*64,
        'serviceSocketPath':str(home / 'service.sock')}
    manifest['embeddingFingerprint'] = provider_api.recipe_fingerprint(model_id=manifest['modelId'],
        model_sha256=manifest['modelSha256'], runtime_id=manifest['runtimeId'], recipe_id=manifest['recipeId'],
        provider_sha256=manifest['providerArtifactSha256'], worker_sha256=manifest['workerSha256'],
        node_sha256=manifest['nodeSha256'], module_entry_sha256=manifest['moduleEntrySha256'],
        closure_sha256=manifest['dependencyClosureSha256'])
    state = home / 'runtime' / 'installs' / ('a'*32)
    state.mkdir(mode=0o700, parents=True)
    service = default_start_worker(manifest, default_admit, execution_state=state)
    try:
        adapter = provider_api.reviewed_provider(manifest)
        rows = adapter.embed_document_chunks('# Synthetic\nThe cobalt marker is here.', path='fixture.md')
        profile = adapter.profile
        sealed = {'profileId':profile['profile_id'], 'modelId':profile['model_id'],
                  'embeddingFingerprint':profile['embedding_fingerprint'],
                  'dimension':profile['dimension'], 'metric':profile['metric']}
        answer = adapter('Where is the cobalt marker?', sealed)
        assert len(rows) == 1 and len(rows[0]['vector']) == 768
        assert len(answer['vector']) == 768
        print(json.dumps({'status':'ready','documents':len(rows),'queries':1,'dimension':768}), flush=True)
    finally:
        service.stop()

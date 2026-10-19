"""Disposable exact-commit plugin bundle and installer journal checks."""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import closing, contextmanager
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'core'))
from laya_index_a import LayaIndexA
from laya_index_claim_queue import ClaimQueue, UnknownDatabase
import laya_index_host_route as host
import laya_install_journal as journal
import laya_product_install as product_install
import laya_product_bundle as bundle
from laya_local_embed_provider import DEFAULT_EMBED_MODEL_URI, recipe_fingerprint
from laya_index_vector import qmd_markdown_title
from laya_qmd_migration import QMD_253


def _git(root, *args):
    return subprocess.run(('git', '-C', str(root), *args), check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          encoding='utf-8').stdout.strip()


def _qmd_two_collection_source(path, roots, files):
    """Synthetic QMD 2.5.3 logical schema; no user or operating DB."""
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE content(hash TEXT PRIMARY KEY,doc TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE documents(id INTEGER PRIMARY KEY AUTOINCREMENT,
                collection TEXT NOT NULL,path TEXT NOT NULL,title TEXT NOT NULL,
                hash TEXT NOT NULL,created_at TEXT NOT NULL,modified_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY(hash) REFERENCES content(hash) ON DELETE CASCADE,
                UNIQUE(collection,path));
            CREATE TABLE content_vectors(hash TEXT NOT NULL,seq INTEGER NOT NULL DEFAULT 0,
                pos INTEGER NOT NULL DEFAULT 0,model TEXT NOT NULL,
                embed_fingerprint TEXT NOT NULL DEFAULT '',
                total_chunks INTEGER NOT NULL DEFAULT 1,embedded_at TEXT NOT NULL,
                PRIMARY KEY(hash,seq));
            CREATE TABLE store_collections(name TEXT PRIMARY KEY,path TEXT NOT NULL,
                pattern TEXT NOT NULL DEFAULT '**/*.md',ignore_patterns TEXT,
                include_by_default INTEGER DEFAULT 1,update_command TEXT,context TEXT);
            CREATE TABLE store_config(key TEXT PRIMARY KEY,value TEXT);
            CREATE VIRTUAL TABLE documents_fts USING fts5(filepath,title,body,
                tokenize='porter unicode61');
            CREATE TRIGGER documents_ai AFTER INSERT ON documents BEGIN SELECT 1; END;
            CREATE TRIGGER documents_ad AFTER DELETE ON documents BEGIN SELECT 1; END;
            CREATE TRIGGER documents_au AFTER UPDATE ON documents BEGIN SELECT 1; END;
        ''')
        db.executemany('INSERT INTO store_collections(name,path) VALUES(?,?)',
                       sorted(roots.items()))
        for collection, rel, body in files:
            digest = hashlib.sha256(body.encode()).hexdigest()
            db.execute('INSERT OR IGNORE INTO content VALUES(?,?,?)',
                       (digest, body, '2026-01-01'))
            db.execute('''INSERT INTO documents(collection,path,title,hash,
                created_at,modified_at,active) VALUES(?,?,?,?,?,?,1)''',
                (collection, rel, qmd_markdown_title(body, rel), digest,
                 '2026-01-01', '2026-01-01'))


class ProductBundleChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='laya-product-synthetic-',
                                                dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.previous_home = os.environ.get('HOME')
        self.addCleanup(self._restore_home)
        os.environ['HOME'] = str(self.root)
        self.source = self.root / 'committed-plugin-source'
        self.source.mkdir()
        names = ('config.py', 'laya_index_a.py', 'laya_index_vector.py',
                 'laya_local_embed_provider.py',
                 'laya_local_embed_service.py',
                 'laya_index_source_port.py',
                 'laya_index_claim_queue.py', 'laya_index_flow.py',
                 'laya_index_runtime.py', 'laya_index_host_route.py',
                 'laya_qmd_cutover.py', 'laya_qmd_migration.py',
                 'laya_product_sync_worker.py',
                 'laya_index_sync.py',
                 'laya_source_catalog.py', 'laya_index_capacity.py',
                 'laya_vector_capacity.py',
                 'laya_package.py', 'laya_install_binding.py',
                 'laya_backup_preflight.py', 'laya_product_bundle.py',
                 'laya_install_journal.py', 'laya_product_install.py',
                 'laya_host_response.py')
        (self.source / 'core').mkdir()
        for name in names:
            shutil.copy2(REPO / 'core' / name, self.source / 'core' / name)
        shutil.copy2(REPO / 'core' / 'laya_local_embed_worker.mjs',
                     self.source / 'core' / 'laya_local_embed_worker.mjs')
        self.worker_sha = hashlib.sha256(
            (self.source / 'core' / 'laya_local_embed_worker.mjs').read_bytes()).hexdigest()
        shutil.copy2(REPO / 'core' / 'laya_qmd_chunking.mjs',
                     self.source / 'core' / 'laya_qmd_chunking.mjs')
        provider_path = self.source / 'core' / 'laya_test_query_provider.py'
        provider_path.write_text('# synthetic inert provider artifact\n', encoding='utf-8')
        for name in ('LICENSE', 'README.md', 'locks/qmd-2.5.3/package.json',
                     'locks/qmd-2.5.3/package-lock.json'):
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / name, target)
        (self.source / 'docs').mkdir()
        (self.source / 'docs' / 'committed-note.md').write_text(
            'Synthetic committed QA note.\n', encoding='utf-8')
        self.node = self.root / 'synthetic-node'
        self.module = self.root / 'synthetic-module.mjs'
        self.model = self.root / 'synthetic-model.gguf'
        self.native = self.root / 'synthetic-native.node'
        for path, content in ((self.node, b'synthetic node executable'),
                              (self.module, b'export const synthetic = true;'),
                              (self.model, b'GGUFsynthetic model'),
                              (self.native, b'synthetic native dependency')):
            path.write_bytes(content)
        self.closure = [{'path': str(self.native),
                         'sha256': hashlib.sha256(self.native.read_bytes()).hexdigest(),
                         'bytes': self.native.stat().st_size}]
        self.closure_digest = hashlib.sha256(bundle._canonical(self.closure)).hexdigest()
        self.provider_sha = hashlib.sha256(provider_path.read_bytes()).hexdigest()
        self.node_sha = hashlib.sha256(self.node.read_bytes()).hexdigest()
        self.module_sha = hashlib.sha256(self.module.read_bytes()).hexdigest()
        self.model_sha = hashlib.sha256(self.model.read_bytes()).hexdigest()
        self.fingerprint = recipe_fingerprint(
            model_id=DEFAULT_EMBED_MODEL_URI, model_sha256=self.model_sha,
            runtime_id='qmd-2.5.3-node-llama-cpp-3.18.1',
            recipe_id='qmd-regex-token-chunks-candidate-v1',
            provider_sha256=self.provider_sha, node_sha256=self.node_sha,
            worker_sha256=self.worker_sha,
            module_entry_sha256=self.module_sha,
            closure_sha256=self.closure_digest)
        self.vector_profile = {
            'modelId': DEFAULT_EMBED_MODEL_URI,
            'embeddingFingerprint': self.fingerprint,
            'dimension': 3, 'metric': 'cosine',
            'approvedProviderId': 'fixture-provider-v1',
            'providerArtifactSha256': self.provider_sha,
            'providerRelativePath': 'core/laya_test_query_provider.py'}
        profile_identity = [self.vector_profile[key] for key in
                            ('modelId', 'embeddingFingerprint', 'dimension', 'metric')]
        self.vector_profile['profileId'] = hashlib.sha256(json.dumps(
            profile_identity, sort_keys=True, ensure_ascii=False,
            separators=(',', ':')).encode('utf-8')).hexdigest()
        for name in ('hooks/run-hook', 'hermes_adapter/core_bridge.py',
                     '.claude-plugin/plugin.json', '.codex-plugin/plugin.json',
                     'plugin.yaml'):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            source = REPO / name
            if source.exists():
                shutil.copy2(source, path)
            else:
                path.write_text('# synthetic ' + name + '\n')
        _git(self.source, 'init', '-q')
        _git(self.source, 'add', '.')
        _git(self.source, '-c', 'user.name=Synthetic Fixture',
             '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'v1')
        self.commit1 = _git(self.source, 'rev-parse', 'HEAD')
        self.install = self.root / 'private-install'
        self.install.mkdir(mode=0o700)
        (self.install / 'runtime').mkdir(mode=0o700)
        self.journal_path = self.install / 'install-journal.sqlite'
        self.project = self.root / 'project'
        self.auto = self.project / '.auto-context'
        self.auto.mkdir(parents=True)
        self.settings = self.auto / 'settings.json'
        self.settings.write_text(json.dumps({'collections': ['docs'], 'indexing': True,
                                             'collectionPaths': {'docs': 'docs'},
                                             'recallStrategy': 'flat'}))
        (self.project / 'docs').mkdir()
        self.route_path = self.auto / host.RECORD
        self.user_qmd = self.root / 'separate-user-qmd'
        self.user_qmd.mkdir()
        (self.user_qmd / 'preserve-me').write_text('user-owned')
        self.db_path = self.project / 'index.sqlite'
        self.queue_path = self.project / 'queue.sqlite'
        db = LayaIndexA(self.db_path, create=True)
        self.incarnation = db.incarnation()
        db.close()
        queue = ClaimQueue.create(self.queue_path, index_incarnation=self.incarnation)
        queue.close()

    def _restore_home(self):
        if self.previous_home is None:
            os.environ.pop('HOME', None)
        else:
            os.environ['HOME'] = self.previous_home

    def candidate(self, name, commit):
        return bundle.prepare_full_plugin_candidate(self.source, self.install / name,
                                                    commit_id=commit,
                                                    vector_profile=self.vector_profile)

    def route(self, proof, generation):
        return {'schema': host.SCHEMA, 'status': 'active',
                'projectRoot': str(self.project),
                'settingsSha256': hashlib.sha256(self.settings.read_bytes()).hexdigest(),
                'dbPath': str(self.db_path), 'queuePath': str(self.queue_path),
                'dbIncarnation': self.incarnation, 'scopeId': '6' * 32,
                'collectionName': 'docs', 'sourceRoot': str(self.project / 'docs'),
                'pluginRoot': proof['pluginRoot'], 'journalPath': str(self.journal_path),
                'installId': '3' * 32, 'generation': generation,
                'bundleManifestSha256': proof['bundleManifestSha256'],
                'ownershipManifestSha256': proof['ownershipManifestSha256'],
                'vectorProfile': proof['vectorProfile']}

    def raw(self, route):
        return json.dumps(route, sort_keys=True).encode('utf-8')

    def initial(self):
        proof = self.candidate('plugin-v1', self.commit1)
        route = self.route(proof, 'g-' + '4' * 32)
        raw = self.raw(route)
        created = journal.create_journal(self.journal_path, proof, raw,
                                         user_qmd_roots=[self.user_qmd])
        self.assertEqual(created['state'], 'candidate_ready')
        self.route_path.write_bytes(raw)
        active = journal.cas_activate(self.journal_path, 1, raw)
        self.assertEqual(active['state'], 'active')
        self.assertEqual(active['epoch'], 2)
        return proof, route, raw

    def stage_v2_source(self):
        changed = self.source / 'core' / 'laya_index_flow.py'
        changed.write_text(changed.read_text() + '\n# v2 synthetic\n')
        _git(self.source, 'add', '.')
        _git(self.source, '-c', 'user.name=Synthetic Fixture',
             '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'v2')
        second = self.candidate('plugin-v2', _git(self.source, 'rev-parse', 'HEAD'))
        new_route = self.route(second, 'g-' + '5' * 32)
        return second, new_route, self.raw(new_route)

    def prove_in_candidate_process(self, proof, approval=None):
        script = '''import json,sys
sys.path.insert(0,sys.argv[1])
from laya_index_host_route import select_route
from laya_install_journal import verify_selected_route,verify_development_vector_approval
decision=select_route(sys.argv[2])
result=(verify_development_vector_approval(decision,sys.argv[3]) if len(sys.argv)>3
        else verify_selected_route(decision))
print(json.dumps(result))'''
        env = {**os.environ, 'HOME': str(self.root), 'QMD_RECALL_LOG': '',
               'PYTHONDONTWRITEBYTECODE': '1'}
        args = (sys.executable, '-B', '-c', script,
                str(Path(proof['pluginRoot']) / 'core'), str(self.project))
        if approval is not None:
            args += (str(approval),)
        done = subprocess.run(args,
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              encoding='utf-8', timeout=15)
        return done

    def provider_manifest(self, proof):
        return {
            'approvedProviderId': self.vector_profile['approvedProviderId'],
            'providerArtifactSha256': self.provider_sha,
            'providerRelativePath': self.vector_profile['providerRelativePath'],
            'providerPath': str(Path(proof['pluginRoot']) /
                                self.vector_profile['providerRelativePath']),
            'workerPath': str(Path(proof['pluginRoot']) /
                              'core/laya_local_embed_worker.mjs'),
            'workerSha256': self.worker_sha,
            'nodePath': str(self.node), 'nodeSha256': self.node_sha,
            'moduleEntryPath': str(self.module),
            'moduleEntrySha256': self.module_sha,
            'modelPath': str(self.model), 'modelSha256': self.model_sha,
            'runtimeId': 'qmd-2.5.3-node-llama-cpp-3.18.1',
            'modelId': DEFAULT_EMBED_MODEL_URI,
            'embeddingFingerprint': self.fingerprint,
            'dimension': 3, 'recipeId': 'qmd-regex-token-chunks-candidate-v1',
            'dependencyClosureApproved': True,
            'dependencyClosureSha256': self.closure_digest,
            'serviceSocketPath': str(self.install / 'runtime' / 'laya-embed.sock')}

    def test_exact_commit_full_closure_and_no_clobber(self):
        proof = self.candidate('plugin-v1', self.commit1)
        self.assertIn('hooks/run-hook', proof['includedFiles'])
        self.assertIn('hermes_adapter/core_bridge.py', proof['includedFiles'])
        self.assertIn('core/laya_index_runtime.py', proof['includedFiles'])
        self.assertIn('core/laya_local_embed_service.py', proof['includedFiles'])
        self.assertIn('core/laya_product_sync_worker.py', proof['includedFiles'])
        self.assertIn('core/laya_product_install.py', proof['includedFiles'])
        self.assertIn('core/laya_qmd_cutover.py', proof['includedFiles'])
        self.assertIn('LICENSE', proof['includedFiles'])
        self.assertIn('README.md', proof['includedFiles'])
        self.assertIn('locks/qmd-2.5.3/package-lock.json', proof['includedFiles'])
        self.assertIn('docs/committed-note.md', proof['includedFiles'])
        self.assertFalse(proof['productRouteAllowed'])
        with self.assertRaisesRegex(bundle.BundleEvidenceError, 'invalid_bundle_request'):
            self.candidate('plugin-v1', self.commit1)
        script = self.source / 'core' / 'laya_index_flow.py'
        script.write_text(script.read_text() + '\n# dirty\n')
        with self.assertRaisesRegex(bundle.BundleEvidenceError, 'source_not_exact_commit'):
            self.candidate('plugin-dirty', self.commit1)
        installed = Path(proof['pluginRoot']) / 'hooks' / 'run-hook'
        installed.write_text(installed.read_text() + '\n# tampered\n')
        with self.assertRaisesRegex(bundle.BundleEvidenceError, 'bundle_file_identity_changed'):
            bundle.inspect_full_plugin_candidate(proof['pluginRoot'])
        self.assertEqual((self.user_qmd / 'preserve-me').read_text(), 'user-owned')

    def test_host_installed_commit_stages_private_journal_without_route(self):
        host_cache = self.root / 'host-cache'
        host_cache.mkdir(mode=0o755)
        installed = host_cache / 'plugin'
        shutil.copytree(self.source, installed,
                        ignore=shutil.ignore_patterns('.git'))
        for directory, _dirs, _files in os.walk(installed):
            Path(directory).chmod(0o755)
        (installed / '.git').mkdir(mode=0o755)
        (installed / '.git' / 'HEAD').write_text('ref: refs/heads/main\n')
        proof = bundle.adopt_host_loaded_plugin(
            self.source, installed, commit_id=self.commit1,
            vector_profile=self.vector_profile)
        (self.auto / 'wiki').mkdir()
        settings = {
            'collections': ['docs', 'wiki'], 'indexing': True,
            'collectionPaths': {'docs': 'docs', 'wiki': '.auto-context/wiki'},
            'collectionRoles': {'docs': 'raw', 'wiki': 'wiki'},
            'recallStrategy': 'hierarchical', 'compile': {'mode': 'off'}}
        self.settings.write_text(json.dumps(settings))
        route = self.route(proof, 'g-' + '4' * 32)
        route['schema'] = host.SCHEMA_V2
        del route['collectionName']
        del route['sourceRoot']
        route['collections'] = [
            {'name': 'docs', 'role': 'raw', 'sourceRoot': str(self.project / 'docs')},
            {'name': 'wiki', 'role': 'wiki', 'sourceRoot': str(self.auto / 'wiki')}]
        raw = self.raw(route)
        review = {'vectorProfile': self.vector_profile,
                  'providerManifest': self.provider_manifest(proof),
                  'dependencyClosure': self.closure}
        staged = product_install.stage_host_loaded_candidate(
            source_root=self.source, plugin_root=installed, state_root=self.install,
            commit_id=self.commit1,
            route_bytes=raw, reviewed_provider=review,
            user_qmd_roots=[self.user_qmd])
        self.assertEqual(staged['status'], 'candidate_ready')
        self.assertEqual(journal.inspect_journal(self.journal_path)['state'],
                         'candidate_ready')
        self.assertFalse(self.route_path.exists())
        self.assertEqual((self.user_qmd / 'preserve-me').read_text(), 'user-owned')
        repeated = product_install.stage_host_loaded_candidate(
            source_root=self.source, plugin_root=installed, state_root=self.install,
            commit_id=self.commit1,
            route_bytes=raw, reviewed_provider=review,
            user_qmd_roots=[self.user_qmd])
        self.assertEqual(repeated, staged)
        self.model.write_bytes(b'GGUFchanged after review')
        with self.assertRaisesRegex(journal.JournalEvidenceError,
                                    'production_attestation_invalid'):
            product_install.stage_host_loaded_candidate(
                source_root=self.source, plugin_root=installed,
                state_root=self.install,
                commit_id=self.commit1, route_bytes=raw,
                reviewed_provider=review, user_qmd_roots=[self.user_qmd])

    def test_host_adoption_rejects_extra_files_and_nonprivate_cache(self):
        installed = self.install / 'host-plugin'
        shutil.copytree(self.source, installed,
                        ignore=shutil.ignore_patterns('.git'))
        for directory, _dirs, _files in os.walk(installed):
            Path(directory).chmod(0o700)
        extra = installed / 'core' / 'unexpected.py'
        extra.write_text('print("unexpected")\n')
        with self.assertRaisesRegex(bundle.BundleEvidenceError,
                                    'installed_file_set_not_exact_commit'):
            bundle.adopt_host_loaded_plugin(
                self.source, installed, commit_id=self.commit1,
                vector_profile=self.vector_profile)
        extra.unlink()
        installed.chmod(0o775)
        with self.assertRaisesRegex(bundle.BundleEvidenceError,
                                    'unsafe_bundle_owner'):
            bundle.adopt_host_loaded_plugin(
                self.source, installed, commit_id=self.commit1,
                vector_profile=self.vector_profile)

    def test_host_pid_marker_is_bounded_metadata_in_adoption_and_readback(self):
        installed = self.install / 'host-plugin'
        shutil.copytree(self.source, installed,
                        ignore=shutil.ignore_patterns('.git'))
        for directory, _dirs, _files in os.walk(installed):
            Path(directory).chmod(0o700)
        markers = installed / '.in_use'
        markers.mkdir(mode=0o700)
        marker = markers / '51026'
        marker.write_bytes(b'host marker\n')
        proof = bundle.adopt_host_loaded_plugin(
            self.source, installed, commit_id=self.commit1,
            vector_profile=self.vector_profile)
        self.assertNotIn('.in_use/51026', proof['includedFiles'])
        self.assertEqual(bundle.inspect_full_plugin_candidate(installed), proof)
        marker.chmod(0o666)
        with self.assertRaisesRegex(bundle.BundleEvidenceError,
                                    'unsafe_host_in_use_metadata'):
            bundle.inspect_full_plugin_candidate(installed)
        marker.chmod(0o600)
        marker.write_bytes(b'x' * 4097)
        with self.assertRaisesRegex(bundle.BundleEvidenceError,
                                    'unsafe_host_in_use_metadata'):
            bundle.inspect_full_plugin_candidate(installed)
        marker.write_bytes(b'host marker\n')
        (markers / 'helper.py').write_text('pass\n')
        with self.assertRaisesRegex(bundle.BundleEvidenceError,
                                    'unsafe_host_in_use_metadata'):
            bundle.inspect_full_plugin_candidate(installed)
        (markers / 'helper.py').unlink()
        (markers / '2').symlink_to(marker)
        with self.assertRaisesRegex(bundle.BundleEvidenceError,
                                    'unsafe_host_in_use_metadata'):
            bundle.inspect_full_plugin_candidate(installed)
        (markers / '2').unlink()
        (markers / '01').write_bytes(b'odd PID')
        with self.assertRaisesRegex(bundle.BundleEvidenceError,
                                    'unsafe_host_in_use_metadata'):
            bundle.inspect_full_plugin_candidate(installed)
        (markers / '01').unlink()
        (markers / '2').mkdir()
        with self.assertRaisesRegex(bundle.BundleEvidenceError,
                                    'unsafe_host_in_use_metadata'):
            bundle.inspect_full_plugin_candidate(installed)
        (markers / '2').rmdir()
        markers.chmod(0o775)
        with self.assertRaisesRegex(bundle.BundleEvidenceError,
                                    'unsafe_host_in_use_metadata'):
            bundle.inspect_full_plugin_candidate(installed)
        markers.chmod(0o700)
        run_hook = installed / 'hooks' / 'run-hook'
        run_hook.write_bytes(run_hook.read_bytes() + b'\n# changed executable\n')
        with self.assertRaisesRegex(bundle.BundleEvidenceError,
                                    'bundle_file_identity_changed'):
            bundle.inspect_full_plugin_candidate(installed)
        run_hook.write_bytes((self.source / 'hooks' / 'run-hook').read_bytes())
        # Restoring bytes does not restore inode/mtime ownership evidence.
        with self.assertRaisesRegex(bundle.BundleEvidenceError,
                                    'bundle_file_identity_changed'):
            bundle.inspect_full_plugin_candidate(installed)
        marker.unlink()
        markers.rmdir()
        dirs = ['.in_use']
        bundle._skip_host_in_use(installed, installed, dirs)
        self.assertEqual(dirs, [])

    def test_owned_origin_plan_binds_both_qmd_collections_without_model(self):
        wiki = self.auto / 'wiki'
        wiki.mkdir()
        raw = self.project / 'docs' / 'chapter.md'
        raw.write_text('# First chapter\nOne line.\n', encoding='utf-8')
        note = wiki / 'summary.md'
        note.write_text('# Summary\nOne fact.\n', encoding='utf-8')
        settings = {
            'collections': ['docs', 'wiki'], 'indexing': True,
            'collectionPaths': {'docs': 'docs', 'wiki': '.auto-context/wiki'},
            'collectionRoles': {'docs': 'raw', 'wiki': 'wiki'},
            'recallStrategy': 'hierarchical', 'compile': {'mode': 'off'}}
        self.settings.write_text(json.dumps(settings))
        documents = []
        for path, file in (('docs/chapter.md', raw), ('wiki/summary.md', note)):
            body = file.read_text(encoding='utf-8')
            documents.append((path, qmd_markdown_title(body, path),
                              hashlib.sha256(body.encode()).hexdigest(), 1, body))
        inventory = {'collection_config': {'docs': (str(raw.parent),),
                                            'wiki': (str(wiki),)},
                     'documents': documents, 'digest': 'a' * 64}
        runtime = {'managed': True, 'QMD_PACKAGE_BIN': '/synthetic/qmd'}
        selection = {'selected': True, 'INDEX_PATH': str(self.root / 'owned-qmd.sqlite'),
                     'generation': 'synthetic'}
        with (mock.patch('qmd_route.binary_info', return_value=runtime),
              mock.patch('qmd_route.project_paths', return_value=selection),
              mock.patch('laya_qmd_migration.inspect_qmd_collections',
                         return_value=inventory)):
            plan = product_install.plan_qmd_origin(self.project)
            self.assertEqual(plan['status'], 'review_required')
            self.assertEqual(plan['sourceFiles'],
                             [['docs/chapter.md', documents[0][2]],
                              ['wiki/summary.md', documents[1][2]]])
            self.assertFalse(self.route_path.exists())
            self.assertFalse(self.journal_path.exists())
            self.settings.write_text(json.dumps({**settings,
                'compile': {'mode': 'auto-wiki', 'triggers': ['manual']}}))
            with self.assertRaisesRegex(product_install.InstallBlocked,
                                        'unsupported_selected_compile_settings'):
                product_install.plan_qmd_origin(self.project)
            self.settings.write_text(json.dumps(settings))
            raw.write_text('# First chapter\nChanged.\n', encoding='utf-8')
            with self.assertRaisesRegex(product_install.InstallBlocked,
                                        'qmd_current_source_changed'):
                product_install.plan_qmd_origin(self.project)

    def test_owned_origin_prepares_backup_vector_candidate_and_retries_without_model(self):
        wiki = self.auto / 'wiki'
        wiki.mkdir()
        raw = self.project / 'docs' / 'chapter.md'
        body_raw, body_wiki = '# Chapter\nOriginal.\n', '# Summary\nVerified.\n'
        raw.write_text(body_raw, encoding='utf-8')
        (wiki / 'summary.md').write_text(body_wiki, encoding='utf-8')
        self.settings.write_text(json.dumps({
            'collections': ['docs', 'wiki'], 'indexing': True,
            'collectionPaths': {'docs': 'docs', 'wiki': '.auto-context/wiki'},
            'collectionRoles': {'docs': 'raw', 'wiki': 'wiki'},
            'recallStrategy': 'hierarchical', 'compile': {'mode': 'off'}}))
        source_db = self.root / 'owned-qmd.sqlite'
        _qmd_two_collection_source(source_db,
            {'docs': str(raw.parent), 'wiki': str(wiki)},
            [('docs', 'chapter.md', body_raw),
             ('wiki', 'summary.md', body_wiki)])
        proof = self.candidate('plugin-v1', self.commit1)
        review = {'vectorProfile': self.vector_profile,
                  'providerManifest': self.provider_manifest(proof),
                  'dependencyClosure': self.closure}
        selection = {'selected': True, 'INDEX_PATH': str(source_db),
                     'generation': 'synthetic'}
        runtime = {'managed': True, 'QMD_PACKAGE_BIN': '/synthetic/qmd'}
        calls = []

        class Controller:
            @contextmanager
            def activity(self, *_args):
                yield None
            def check(self, *_args):
                return {'ready': True}

        class Provider:
            def __init__(self, manifest):
                calls.append('provider')
                self.profile = {'profile_id': self_ref.vector_profile['profileId'],
                                'model_id': self_ref.vector_profile['modelId'],
                                'embedding_fingerprint':
                                self_ref.vector_profile['embeddingFingerprint'],
                                'dimension': 3}
            def embed_document_chunks(self, body, path):
                calls.append(('embed', path))
                self_ref.assertTrue(backup.is_file(), 'backup before embedding')
                return [{'seq': 0, 'pos': 0, 'vector': [1.0, 0.0, 0.0]}]

        class Service:
            def stop(self):
                calls.append('stop')

        self_ref = self
        backup = self.install / 'qmd-origin-backup.sqlite'
        kwargs = {'project_root': self.project, 'source_root': self.source,
                  'plugin_root': proof['pluginRoot'], 'state_root': self.install,
                  'commit_id': self.commit1, 'install_id': '3' * 32,
                  'accepted_provider_sha256': 'b' * 64,
                  'user_qmd_roots': [self.user_qmd],
                  'coordinator': Controller(),
                  'start_service': lambda *_args, **_kw: Service(),
                  'provider_factory': Provider}
        with (mock.patch('qmd_route.binary_info', return_value=runtime),
              mock.patch('qmd_route.project_paths', return_value=selection),
              mock.patch('laya_product_install.review_local_provider',
                         return_value={'proposalSha256': 'b' * 64,
                                       'reviewedProvider': review}),
              mock.patch('context_learning.shared.resource_report',
                         return_value={'synthetic': True})):
            plan = product_install.plan_qmd_origin(self.project)
            kwargs['expected_proposal_sha256'] = plan['proposalSha256']
            raw.write_text('# Chapter\nChanged.\n', encoding='utf-8')
            with self.assertRaisesRegex(product_install.InstallBlocked,
                                        'qmd_current_source_changed'):
                product_install.prepare_owned_qmd_origin(**kwargs)
            self.assertFalse(backup.exists())
            raw.write_text(body_raw, encoding='utf-8')
            queue = self.install / 'laya-claims.sqlite'
            staged = []

            class SyntheticKill(BaseException):
                pass

            def killed_create(path, *, index_incarnation=None):
                # Model SIGKILL after the exclusive open, before SQLite init.
                os.close(os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600))
                staged.append(Path(path))
                raise SyntheticKill()

            with mock.patch.object(ClaimQueue, 'create', side_effect=killed_create):
                with self.assertRaises(SyntheticKill):
                    product_install.prepare_owned_qmd_origin(**kwargs)
            self.assertEqual(len(staged), 1)
            self.assertEqual(staged[0].parent.parent, self.install.resolve())
            self.assertTrue(staged[0].parent.name.startswith('.laya-claims-'))
            self.assertFalse(queue.exists(), 'interrupted stage must not reach final path')
            self.assertTrue((self.install / 'laya-candidate.sqlite').is_file())
            self.assertIn('provider', calls)
            prior = list(calls)
            # A pre-fix in-place create left an empty final queue: retained and
            # rejected, never clobbered.
            queue.write_bytes(b'')
            with self.assertRaises(UnknownDatabase):
                product_install.prepare_owned_qmd_origin(**kwargs)
            self.assertEqual(queue.read_bytes(), b'')
            queue.unlink()  # synthetic explicit recovery
            stale_wal = Path(str(queue) + '-wal')
            stale_wal.write_bytes(b'stale queue pages')
            with self.assertRaisesRegex(product_install.InstallBlocked,
                                        'partial_queue_requires_recovery'):
                product_install.prepare_owned_qmd_origin(**kwargs)
            self.assertFalse(queue.exists())
            self.assertEqual(stale_wal.read_bytes(), b'stale queue pages')
            stale_wal.unlink()  # synthetic explicit recovery
            # A killed private stage survives under its own name; retry
            # neither reuses nor removes it.
            residue = self.install / '.laya-claims-killed'
            residue.mkdir(mode=0o700)
            (residue / 'claims.sqlite').write_bytes(b'partial')
            with mock.patch('laya_qmd_cutover.bind_qmd_origin',
                            side_effect=RuntimeError('synthetic pre-bind interruption')):
                with self.assertRaisesRegex(RuntimeError,
                                            'synthetic pre-bind interruption'):
                    product_install.prepare_owned_qmd_origin(**kwargs)
            self.assertTrue(backup.is_file())
            self.assertFalse(self.route_path.exists())
            self.assertEqual(queue.lstat().st_nlink, 1)
            linked = self.install / '.laya-claims-linked'
            linked.mkdir(mode=0o700)
            os.link(queue, linked / 'claims.sqlite')
            self.assertEqual(queue.lstat().st_nlink, 2)
            prepared = product_install.prepare_owned_qmd_origin(**kwargs)
        self.assertEqual(prepared['status'], 'cutover_prepared')
        self.assertEqual(prepared['cutoverPhase'], 'prepared')
        self.assertEqual(calls, prior, 'sealed retry must not rerun model')
        self.assertTrue((self.install / 'laya-candidate.sqlite').is_file())
        self.assertTrue(queue.is_file())
        self.assertEqual(queue.lstat().st_nlink, 1)
        self.assertFalse((linked / 'claims.sqlite').exists())
        self.assertEqual((residue / 'claims.sqlite').read_bytes(), b'partial')
        route = json.loads((self.install / 'reviewed-route.json').read_text())
        with closing(ClaimQueue.open(queue)) as opened:
            self.assertEqual(opened.binding(), route['dbIncarnation'])
        self.assertTrue((self.install / 'reviewed-route.json').is_file())
        self.assertTrue((self.install / 'qmd-cutover.json').is_file())
        self.assertFalse(self.route_path.exists())
        self.assertEqual((self.user_qmd / 'preserve-me').read_text(), 'user-owned')

    def test_local_provider_review_is_deterministic_and_requires_new_digest_on_change(self):
        proof = self.candidate('plugin-v1', self.commit1)
        package = self.root / 'qmd-package'
        (package / 'bin').mkdir(parents=True)
        (package / 'bin/qmd').write_text('synthetic executable')
        (package / 'package.json').write_text('{"version":"2.5.3"}')
        module_pkg = package / 'node_modules/node-llama-cpp'
        (module_pkg / 'dist').mkdir(parents=True)
        (module_pkg / 'package.json').write_text('{"version":"3.18.1"}')
        (module_pkg / 'dist/index.js').write_text('export const synthetic = true;')
        native_pkg = package / 'node_modules/@node-llama-cpp/mac-arm64-metal'
        (native_pkg / 'bins').mkdir(parents=True)
        (native_pkg / 'package.json').write_text('{"version":"3.18.1"}')
        native = native_pkg / 'bins/llama-addon.node'
        native.write_bytes(b'synthetic native')
        (native_pkg / 'bins/libllama.dylib').write_bytes(b'synthetic dylib')
        model = self.root / 'embeddinggemma-300M-Q8_0.gguf'
        model.write_bytes(b'GGUFsynthetic model')
        runtime = {'proof': {'version': '2.5.3',
                             'qmd': str(package / 'bin/qmd'),
                             'node': str(self.node)},
                   'generation': 'synthetic-runtime'}
        with (mock.patch('qmd_runtime.select', return_value=runtime),
              mock.patch('platform.system', return_value='Linux'),
              mock.patch('platform.machine', return_value='x86_64')):
            with self.assertRaisesRegex(product_install.InstallBlocked,
                                        'reviewed_native_platform_unsupported'):
                product_install.review_local_provider(
                    source_root=self.source, plugin_root=proof['pluginRoot'],
                    state_root=self.install, commit_id=self.commit1,
                    model_path=str(model), dimension=3)
        # The reviewed closure is mac-arm64-metal; pin the host so the positive
        # path is deterministic on Linux CI without loosening the real gate.
        with (mock.patch('qmd_runtime.select', return_value=runtime),
              mock.patch('platform.system', return_value='Darwin'),
              mock.patch('platform.machine', return_value='arm64')):
            first = product_install.review_local_provider(
                source_root=self.source, plugin_root=proof['pluginRoot'],
                state_root=self.install, commit_id=self.commit1,
                model_path=str(model), dimension=3)
            second = product_install.review_local_provider(
                source_root=self.source, plugin_root=proof['pluginRoot'],
                state_root=self.install, commit_id=self.commit1,
                model_path=str(model), dimension=3)
            self.assertEqual(first, second)
            self.assertEqual(len(first['reviewedProvider']['dependencyClosure']), 2)
            native.write_bytes(b'changed synthetic native')
            changed = product_install.review_local_provider(
                source_root=self.source, plugin_root=proof['pluginRoot'],
                state_root=self.install, commit_id=self.commit1,
                model_path=str(model), dimension=3)
            self.assertNotEqual(changed['proposalSha256'], first['proposalSha256'])

    def test_production_attestation_needs_journal_bound_review_digest(self):
        proof = self.candidate('plugin-v1', self.commit1)
        manifest = self.provider_manifest(proof)
        attestation = {
            'schema': bundle.PRODUCTION_ATTESTATION_SCHEMA,
            'sourceCommit': proof['sourceCommit'],
            'bundleManifestSha256': proof['bundleManifestSha256'],
            'ownershipManifestSha256': proof['ownershipManifestSha256'],
            'journalPath': str(self.journal_path),
            'vectorProfile': proof['vectorProfile'],
            'providerManifest': manifest,
            'dependencyClosure': self.closure}
        self.assertEqual(bundle.validate_production_provider_attestation(
            self.vector_profile, proof, attestation), manifest)
        digest = hashlib.sha256(bundle._canonical(attestation)).hexdigest()
        self.assertFalse(bundle.production_provider_approved(
            self.vector_profile, proof, attestation))
        self.assertFalse(bundle.production_provider_approved(
            self.vector_profile, proof, attestation,
            journal_attestation_sha256='0' * 64))
        # This positive case represents an installer-reviewed record sealed in
        # a journal. The fixture does not approve a real model or host runtime.
        self.assertTrue(bundle.production_provider_approved(
            self.vector_profile, proof, attestation,
            journal_attestation_sha256=digest))
        self.model.write_bytes(b'GGUFchanged after review')
        self.assertFalse(bundle.production_provider_approved(
            self.vector_profile, proof, attestation,
            journal_attestation_sha256=digest))

    def test_active_journal_and_candidate_loaded_code(self):
        proof, route, raw = self.initial()
        self.assertEqual(host.select_route(str(self.project))['state'], 'candidate')
        done = self.prove_in_candidate_process(proof)
        self.assertEqual(done.returncode, 0, done.stderr)
        checked = json.loads(done.stdout)
        self.assertFalse(checked['productRouteAllowed'])
        self.assertEqual(checked['status'], 'production_attestation_unbound')
        self.assertEqual(checked['sourceCommit'], self.commit1)
        approval_path = self.install / 'development-vector-approval.json'
        approval = {'schema': journal.DEVELOPMENT_APPROVAL_SCHEMA,
                    'testOwned': True, 'pluginRoot': proof['pluginRoot'],
                    'sourceCommit': proof['sourceCommit'],
                    'bundleManifestSha256': proof['bundleManifestSha256'],
                    'ownershipManifestSha256': proof['ownershipManifestSha256'],
                    'vectorProfile': dict(proof['vectorProfile'])}
        approval_path.write_text(json.dumps(approval), encoding='utf-8')
        approval_path.chmod(0o600)
        development = self.prove_in_candidate_process(proof, approval_path)
        self.assertEqual(development.returncode, 0, development.stderr)
        checked_development = json.loads(development.stdout)
        self.assertTrue(checked_development['developmentOnlyRouteAllowed'])
        self.assertFalse(checked_development['productRouteAllowed'])
        approval['vectorProfile']['providerArtifactSha256'] = '0' * 64
        approval_path.write_text(json.dumps(approval), encoding='utf-8')
        self.assertNotEqual(self.prove_in_candidate_process(proof, approval_path).returncode, 0)
        self.route_path.write_bytes(raw.replace(b'"active"', b'"broken"'))
        self.assertEqual(host.select_route(str(self.project))['state'], 'blocked')
        done = self.prove_in_candidate_process(proof)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual((self.user_qmd / 'preserve-me').read_text(), 'user-owned')

    def test_provider_profile_requires_exact_committed_artifact(self):
        invalid = {**self.vector_profile, 'providerArtifactSha256': '0' * 64}
        target = self.install / 'invalid-provider'
        with self.assertRaisesRegex(bundle.BundleEvidenceError, 'provider_artifact_mismatch'):
            bundle.prepare_full_plugin_candidate(self.source, target,
                                                 commit_id=self.commit1,
                                                 vector_profile=invalid)
        self.assertFalse(target.exists())
        proof = self.candidate('plugin-v1', self.commit1)
        provider = Path(proof['pluginRoot']) / self.vector_profile['providerRelativePath']
        provider.write_text(provider.read_text() + '# tampered\n', encoding='utf-8')
        with self.assertRaisesRegex(bundle.BundleEvidenceError, 'bundle_file_identity_changed'):
            bundle.inspect_full_plugin_candidate(proof['pluginRoot'])

    def test_route_cannot_self_approve_different_provider(self):
        proof = self.candidate('plugin-v1', self.commit1)
        claimed = self.route(proof, 'g-' + '4' * 32)
        claimed['vectorProfile'] = {**claimed['vectorProfile'],
                                    'approvedProviderId': 'unreviewed-provider'}
        with self.assertRaisesRegex(journal.JournalEvidenceError, 'route_bundle_mismatch'):
            journal.create_journal(self.journal_path, proof, self.raw(claimed),
                                   user_qmd_roots=[self.user_qmd])
        self.assertFalse(self.journal_path.exists())

    def test_update_rollback_and_new_write_block(self):
        first, old_route, old_raw = self.initial()
        second, new_route, new_raw = self.stage_v2_source()
        pending = journal.cas_stage_update(self.journal_path, 2, second, new_raw)
        self.assertEqual(pending['state'], 'pending')
        with self.assertRaisesRegex(journal.JournalEvidenceError, 'stale_journal_cas'):
            journal.cas_stage_update(self.journal_path, 2, second, new_raw)
        # Old route observed after crash: retain old selection.
        recovered = journal.cas_recover(self.journal_path, 3, old_raw)
        self.assertEqual(recovered['state'], 'restored_old')
        self.assertEqual(recovered['selected']['pluginRoot'], first['pluginRoot'])
        pending = journal.cas_stage_update(self.journal_path, 4, second, new_raw)
        self.route_path.write_bytes(new_raw)
        active = journal.cas_recover(self.journal_path, 5, new_raw)
        self.assertEqual(active['state'], 'active')
        self.assertEqual(active['selected']['pluginRoot'], second['pluginRoot'])
        # A reversible route switch requires publishing old bytes before CAS.
        self.route_path.write_bytes(old_raw)
        restored = journal.cas_restore(self.journal_path, 6, old_raw)
        self.assertEqual(restored['state'], 'restored_old')
        pending = journal.cas_stage_update(self.journal_path, 7, second, new_raw)
        self.route_path.write_bytes(new_raw)
        active = journal.cas_activate(self.journal_path, 8, new_raw)
        marked = journal.cas_mark_new_write(self.journal_path, 9)
        self.assertTrue(marked['newWriteSeen'])
        # Simulate process failure after durable intent and before index publish.
        with self.assertRaisesRegex(RuntimeError, 'synthetic_pre_publish_exit'):
            raise RuntimeError('synthetic_pre_publish_exit')
        with sqlite3.connect(self.db_path.as_uri() + '?mode=ro', uri=True) as check:
            self.assertEqual(check.execute('SELECT count(*) FROM head').fetchone()[0], 0)
        self.route_path.write_bytes(old_raw)
        with self.assertRaisesRegex(journal.JournalEvidenceError, 'rollback_not_authorized'):
            journal.cas_restore(self.journal_path, 10, old_raw)
        self.assertEqual(journal.inspect_journal(self.journal_path)['state'], 'active')

    def test_two_processes_contest_one_journal_epoch(self):
        self.initial()
        second, _route, raw = self.stage_v2_source()
        args = self.root / 'race-args.json'
        args.write_text(json.dumps({'journal': str(self.journal_path), 'proof': second,
                                    'raw': raw.decode('utf-8')}), encoding='utf-8')
        barrier = self.root / 'race-go'
        script = '''import json,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import laya_install_journal as journal
args=json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
go=Path(sys.argv[3]); deadline=time.monotonic()+10
while not go.exists():
    if time.monotonic()>deadline: raise SystemExit(3)
    time.sleep(0.002)
try:
    result=journal.cas_stage_update(Path(args['journal']),2,args['proof'],args['raw'].encode())
    print(json.dumps({'ok':True,'epoch':result['epoch']}))
except journal.JournalEvidenceError as exc:
    print(json.dumps({'ok':False,'error':str(exc)}))'''
        env = {**os.environ, 'HOME': str(self.root), 'QMD_RECALL_LOG': '',
               'PYTHONDONTWRITEBYTECODE': '1'}
        procs = [subprocess.Popen((sys.executable, '-B', '-c', script,
                                   str(REPO / 'core'), str(args), str(barrier)),
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  encoding='utf-8') for _ in range(2)]
        barrier.write_bytes(b'')
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=20)
            self.assertEqual(proc.returncode, 0, err)
            results.append(json.loads(out))
        self.assertEqual(sum(row['ok'] for row in results), 1, results)
        self.assertEqual({row['error'] for row in results if not row['ok']},
                         {'stale_journal_cas'})
        self.assertEqual(journal.inspect_journal(self.journal_path)['epoch'], 3)

    def test_executable_mode_change_breaks_owner_manifest(self):
        proof = self.candidate('plugin-v1', self.commit1)
        target = Path(proof['pluginRoot']) / 'hooks' / 'run-hook'
        os.chmod(target, stat.S_IMODE(target.stat().st_mode) ^ stat.S_IXUSR)
        with self.assertRaisesRegex(bundle.BundleEvidenceError, 'bundle_file_identity_changed'):
            bundle.inspect_full_plugin_candidate(proof['pluginRoot'])

    def test_uncertain_route_recovery_retains_pending_and_epoch(self):
        _first, old_route, old_raw = self.initial()
        second, _new_route, new_raw = self.stage_v2_source()
        journal.cas_stage_update(self.journal_path, 2, second, new_raw)
        self.route_path.write_bytes(new_raw)
        with self.assertRaisesRegex(journal.JournalEvidenceError, 'observed_route_mismatch'):
            journal.cas_recover(self.journal_path, 3, old_raw)
        self.assertEqual(journal.inspect_journal(self.journal_path)['state'], 'pending')
        foreign = self.raw({**old_route, 'generation': 'g-' + '6' * 32})
        self.route_path.write_bytes(foreign)
        with self.assertRaisesRegex(journal.JournalEvidenceError, 'uncertain_route_recovery'):
            journal.cas_recover(self.journal_path, 3, foreign)
        self.assertEqual(journal.inspect_journal(self.journal_path)['epoch'], 3)
        self.route_path.write_bytes(new_raw)
        self.assertEqual(journal.cas_recover(self.journal_path, 3, new_raw)['state'], 'active')


if __name__ == '__main__':
    unittest.main()

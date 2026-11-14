"""Named runtime reviews validate the requested sealed generation."""
import os
from pathlib import Path
import shutil
from unittest.mock import patch

from memvane.provider import embed_runtime as runtime
from memvane.provider.tests.support import Machine


class NamedGeneration(Machine):
    def setUp(self):
        super().setUp()
        self.first = self.provision()
        self.model_bytes += b'2'
        self.second = self.provision()
        self.pointer = runtime.root(self.home) / 'active.json'
        self.before = self.pointer.read_bytes()

    def test_named_generation_does_not_read_or_change_active_pointer(self):
        with patch.object(runtime, '_pointer', side_effect=AssertionError('pointer read')):
            named = runtime.select_generation(self.first['generation'], self.home)
        self.assertEqual(named, self.first)
        self.assertEqual(self.pointer.read_bytes(), self.before)
        self.assertEqual(runtime.select(self.home), self.second)

    def test_missing_outside_symlink_and_modified_generation_are_rejected(self):
        source = Path(self.first['generation'])
        outside = self.home / 'outside' / source.name
        shutil.copytree(source, outside)
        alias = runtime.root(self.home) / 'generations' / ('g-' + 'f' * 32)
        alias.symlink_to(outside, target_is_directory=True)
        for value in (source.name, str(outside), str(alias), self.first):
            with self.subTest(value=str(value)):
                with self.assertRaises(runtime.RuntimeUnavailable):
                    runtime.select_generation(value, self.home)
                self.assertEqual(self.pointer.read_bytes(), self.before)
        dependency = next(source.rglob('isexe/index.js'))
        dependency.write_bytes(dependency.read_bytes() + b' modified')
        with self.assertRaises(runtime.RuntimeUnavailable):
            runtime.select_generation(str(source), self.home)
        self.assertEqual(self.pointer.read_bytes(), self.before)
        self.assertEqual(runtime.select(self.home), self.second)

    def test_attested_owned_generation_reactivates_without_copy(self):
        attestation = {'providerManifest': self.first,
                       'dependencyClosure': self.first['dependencyClosure'],
                       runtime.FILES_SHA: self.first[runtime.FILES_SHA]}
        with patch.object(runtime, '_provision', side_effect=AssertionError('unexpected copy')):
            self.assertEqual(runtime.ensure_for_attestation(attestation, self.home), self.first)
        self.assertEqual(runtime.select(self.home), self.first)

    def test_corrupted_attested_generation_refuses_reactivation(self):
        attestation = {'providerManifest': self.first,
                       'dependencyClosure': self.first['dependencyClosure'],
                       runtime.FILES_SHA: self.first[runtime.FILES_SHA]}
        dependency = next(Path(self.first['generation']).rglob('isexe/index.js'))
        dependency.write_bytes(dependency.read_bytes() + b' modified')
        with patch.object(runtime, '_provision', side_effect=AssertionError('unexpected copy')):
            with self.assertRaises(runtime.RuntimeUnavailable):
                runtime.ensure_for_attestation(attestation, self.home)
        self.assertEqual(self.pointer.read_bytes(), self.before)

    def test_attested_bytes_are_copied_once_and_sha_checked(self):
        target_home = self.home / 'target'
        target_home.mkdir(mode=0o700)
        generation_root = runtime._generations(runtime._runtime_root(target_home, create=True))
        stale = generation_root / '.staging-interrupted'
        stale.mkdir(mode=0o700)
        record = {'providerManifest': self.first, 'dependencyClosure': self.first['dependencyClosure'],
                  runtime.FILES_SHA: self.first[runtime.FILES_SHA]}
        copied = runtime.ensure_for_attestation(record, target_home)
        self.assertFalse(stale.exists())
        self.assertEqual(copied[runtime.FILES_SHA], self.first[runtime.FILES_SHA])
        self.assertNotEqual(copied['generation'], self.first['generation'])
        with patch.object(runtime, '_provision', side_effect=AssertionError('unexpected copy')):
            self.assertEqual(runtime.ensure_for_attestation(record, target_home), copied)
        for path in Path(copied['generation']).rglob('*'):
            if path.is_file():
                self.assertEqual(path.stat().st_nlink, 1)
                self.assertFalse(path.stat().st_mode & 0o077)

    def test_changed_or_missing_sources_refuse_without_activation(self):
        target_home = self.home / 'target'
        target_home.mkdir(mode=0o700)
        record = {'providerManifest': self.first, 'dependencyClosure': self.first['dependencyClosure'],
                  runtime.FILES_SHA: self.first[runtime.FILES_SHA]}
        model = Path(self.first['modelPath'])
        model.write_bytes(model.read_bytes() + b'modified')
        with self.assertRaisesRegex(runtime.RuntimeUnavailable,
                                    '^laya_embed_runtime_source_changed$'):
            runtime.ensure_for_attestation(record, target_home)
        self.assertFalse((runtime.root(target_home) / 'active.json').exists())
        model.unlink()
        with self.assertRaisesRegex(runtime.RuntimeUnavailable,
                                    '^laya_embed_runtime_source_unavailable$'):
            runtime.ensure_for_attestation(record, target_home)
        self.assertFalse((runtime.root(target_home) / 'active.json').exists())

    def test_worker_start_rehashes_every_runtime_file(self):
        self.assertEqual(runtime.verify_attested_files(self.first, self.first[runtime.FILES_SHA]),
                         self.first[runtime.FILES_SHA])
        dependency = next(Path(self.first['generation']).rglob('lifecycle-utils/dist/index.js'))
        dependency.write_text('changed')
        with self.assertRaises(runtime.RuntimeUnavailable):
            runtime.verify_attested_files(self.first, self.first[runtime.FILES_SHA])

    def test_changed_js_dependency_is_new_content_not_equivalent(self):
        self.model_bytes = Path(self.first['modelPath']).read_bytes()
        dep = self.registry / 'lifecycle-utils' / 'dist' / 'index.js'
        dep.write_text('different synthetic dependency')
        changed = self.provision()
        self.assertNotEqual(changed['generation'], self.first['generation'])
        for name in ('nodeSha256', 'moduleEntrySha256', 'modelSha256'):
            self.assertEqual(changed[name], self.first[name])
        self.assertNotEqual(changed[runtime.FILES_SHA], self.first[runtime.FILES_SHA])
        self.assertFalse(runtime.matches(changed, self.first, self.first['dependencyClosure'],
                                         self.first[runtime.FILES_SHA]))
        record = {'providerManifest': self.first, 'dependencyClosure': self.first['dependencyClosure'],
                  runtime.FILES_SHA: self.first[runtime.FILES_SHA]}
        self.assertEqual(runtime.ensure_for_attestation(record, self.home), self.first)

    def test_operations_return_the_sealed_generation_after_a_concurrent_switch(self):
        from contextlib import contextmanager
        record0 = {'providerManifest': self.first, 'dependencyClosure': self.first['dependencyClosure'],
                   runtime.FILES_SHA: self.first[runtime.FILES_SHA]}
        record1 = {'providerManifest': self.second, 'dependencyClosure': self.second['dependencyClosure'],
                   runtime.FILES_SHA: self.second[runtime.FILES_SHA]}
        self.model_bytes = Path(self.first['modelPath']).read_bytes()
        operations = (
            lambda: runtime.ensure_for_attestation(record0, self.home),
            lambda: runtime.provision_from_attestation(record0, self.home),
            self.provision,
        )
        for operation in operations:
            with self.subTest(operation=operation):
                runtime.ensure_for_attestation(record1, self.home)
                original_lock = runtime._lock
                evidence = []

                @contextmanager
                def switch_after_unlock(root):
                    with original_lock(root):
                        yield
                    if not evidence:
                        evidence.append(runtime.select(self.home)['generation'])
                        runtime.ensure_for_attestation(record1, self.home)

                with patch.object(runtime, '_lock', switch_after_unlock):
                    result = operation()
                self.assertEqual(evidence, [self.first['generation']])
                self.assertEqual(result['generation'], self.first['generation'])
                self.assertEqual(result[runtime.FILES_SHA], self.first[runtime.FILES_SHA])
                self.assertEqual(runtime.select(self.home)['generation'], self.second['generation'])

    def test_corrupted_runtime_refuses_review_and_reprovisioning(self):
        source = Path(self.first['generation'])
        dep = next(source.rglob('lifecycle-utils/dist/index.js'))
        native = Path(self.first['dependencyClosure'][0]['path'])
        model = Path(self.first['modelPath'])
        metadata = source / 'runtime.json'
        files = (dep, native, model, metadata)
        saved = {path: (path.read_bytes(), path.stat().st_mode & 0o777) for path in files}
        extra = source / 'node_modules' / 'unexpected.js'
        hardlink = self.home / 'second-link'
        record = {'providerManifest': self.first, 'dependencyClosure': self.first['dependencyClosure'],
                  runtime.FILES_SHA: self.first[runtime.FILES_SHA]}
        for kind in ('dependency', 'native', 'model', 'extra', 'missing', 'symlink',
                     'public', 'hardlink', 'metadata'):
            with self.subTest(kind=kind):
                try:
                    if kind in ('dependency', 'native', 'model'):
                        target = {'dependency': dep, 'native': native, 'model': model}[kind]
                        target.write_bytes(target.read_bytes() + b'changed')
                    elif kind == 'extra':
                        extra.write_text('unexpected')
                        extra.chmod(0o600)
                    elif kind == 'missing':
                        dep.unlink()
                    elif kind == 'symlink':
                        dep.unlink()
                        dep.symlink_to(self.home)
                    elif kind == 'public':
                        dep.chmod(0o640)
                    elif kind == 'hardlink':
                        os.link(dep, hardlink)
                    else:
                        metadata.write_bytes(metadata.read_bytes() + b'corrupt')
                    with self.assertRaises(runtime.RuntimeUnavailable):
                        runtime.select_generation(str(source), self.home)
                    with self.assertRaises(runtime.RuntimeUnavailable):
                        runtime.ensure_for_attestation(record, self.home)
                    self.assertEqual(self.pointer.read_bytes(), self.before)
                finally:
                    extra.unlink(missing_ok=True)
                    hardlink.unlink(missing_ok=True)
                    for path, (data, mode) in saved.items():
                        if path.is_symlink():
                            path.unlink()
                        path.write_bytes(data)
                        path.chmod(mode)

    def test_corrupt_active_pointer_refuses_selection_then_attestation_recovers(self):
        self.pointer.write_text('{}')
        self.pointer.chmod(0o600)
        with self.assertRaises(runtime.RuntimeUnavailable):
            runtime.select(self.home)
        self.assertEqual(runtime.select_generation(self.first['generation'], self.home), self.first)
        record = {'providerManifest': self.first, 'dependencyClosure': self.first['dependencyClosure'],
                  runtime.FILES_SHA: self.first[runtime.FILES_SHA]}
        self.assertEqual(runtime.ensure_for_attestation(record, self.home), self.first)
        self.assertEqual(runtime.select(self.home), self.first)

    def test_review_uses_named_generation_and_rejects_invalid_targets(self):
        from memvane.install import api as install_api
        from memvane.install.types import InstallBlocked
        from memvane.kernel import memvane_paths
        plugin = Path(__file__).resolve().parents[4]
        state = memvane_paths.install_root('4' * 32, self.home)
        source = Path(self.first['generation'])
        outside = self.home / 'outside' / source.name
        shutil.copytree(source, outside)
        alias = runtime.root(self.home) / 'generations' / ('g-' + 'f' * 32)
        alias.symlink_to(outside, target_is_directory=True)
        with patch('platform.system', return_value='Darwin'), \
             patch('platform.machine', return_value='arm64'):
            result = install_api.review_local_provider(plugin_root=plugin, state_root=state,
                dimension=2, runtime_generation=str(source))
            self.assertEqual(result['status'], 'review_required')
            self.assertEqual(result['embedRuntimeGeneration'], str(source))
            for value in (source.name, str(outside), str(alias), self.first):
                with self.subTest(value=str(value)):
                    with self.assertRaises(InstallBlocked):
                        install_api.review_local_provider(plugin_root=plugin, state_root=state,
                            dimension=2, runtime_generation=value)
                    self.assertEqual(self.pointer.read_bytes(), self.before)
            dependency = next(source.rglob('isexe/index.js'))
            dependency.write_bytes(dependency.read_bytes() + b' modified')
            with self.assertRaises(InstallBlocked):
                install_api.review_local_provider(plugin_root=plugin, state_root=state,
                    dimension=2, runtime_generation=str(source))
        self.assertEqual(self.pointer.read_bytes(), self.before)

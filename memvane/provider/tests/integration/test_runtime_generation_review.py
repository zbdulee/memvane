"""Named runtime reviews validate the requested sealed generation."""
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

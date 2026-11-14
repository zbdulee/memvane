"""A newly sealed generation is synced before its active pointer is published."""
import os
from pathlib import Path
from unittest import mock

from memvane.provider import embed_runtime as runtime
from memvane.provider.tests.support import Machine


class GenerationDurability(Machine):
    def test_generation_is_durable_before_the_pointer_names_it(self):
        events, opened = [], {}
        real_open, real_fsync, real_write = os.open, os.fsync, runtime._write_private

        def opened_fd(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            opened[fd] = str(path)
            return fd

        def sync_fd(fd):
            events.append(('fsync', opened.get(fd)))
            return real_fsync(fd)

        def write(path, raw):
            if Path(path).name == runtime.ACTIVE:
                events.append(('active', str(path)))
            return real_write(path, raw)

        with mock.patch.object(runtime.os, 'open', opened_fd), \
             mock.patch.object(runtime.os, 'fsync', sync_fd), \
             mock.patch.object(runtime, '_write_private', write):
            selected = self.provision()
        generation = Path(selected['generation'])
        pointer = str(runtime.root(self.home) / runtime.ACTIVE)
        before = events[:events.index(('active', pointer))]
        synced = [path for kind, path in before if kind == 'fsync']
        for directory in (generation, generation.parent, generation / 'node_modules',
                          generation / 'bin', generation / 'models'):
            self.assertIn(str(directory), synced)

    def test_reselected_generation_is_synced_before_pointer_publish(self):
        first = self.provision()
        self.model_bytes += b'2'
        self.provision()
        record = {'providerManifest': first, 'dependencyClosure': first['dependencyClosure'],
                  runtime.FILES_SHA: first[runtime.FILES_SHA]}
        events = []
        tree, directory, write = runtime._fsync_tree, runtime._fsync_dir, runtime._write_private

        def tree_sync(path):
            events.append(('tree', str(path)))
            return tree(path)

        def dir_sync(path):
            events.append(('dir', str(path)))
            return directory(path)

        def write_pointer(path, raw):
            if Path(path).name == runtime.ACTIVE:
                events.append(('active', str(path)))
            return write(path, raw)

        with mock.patch.object(runtime, '_fsync_tree', side_effect=tree_sync), \
             mock.patch.object(runtime, '_fsync_dir', side_effect=dir_sync), \
             mock.patch.object(runtime, '_write_private', side_effect=write_pointer):
            selected = runtime.ensure_for_attestation(record, self.home)
        self.assertEqual(selected, first)
        generation = Path(first['generation'])
        active = ('active', str(runtime.root(self.home) / runtime.ACTIVE))
        before = events[:events.index(active)]
        self.assertIn(('tree', str(generation)), before)
        self.assertIn(('dir', str(generation.parent)), before)

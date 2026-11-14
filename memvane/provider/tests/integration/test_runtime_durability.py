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

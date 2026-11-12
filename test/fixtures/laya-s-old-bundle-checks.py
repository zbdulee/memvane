#!/usr/bin/env python3
"""Released pre-row-format cores refuse a row-format (format 4) view store.

Each old core is archived from its exact commit; CI fetches those commits
(.github/workflows/test.yml) because a shallow checkout does not contain them.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
spec = importlib.util.spec_from_file_location('s_publish_fixture', HERE / 'laya-s-incremental-publish-checks.py')
publish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish)

# bb4ae489: the schema3 core of the S design baseline; 4510105e: release 0.44.17.
OLD_CORES = ('bb4ae4899bf6eb7ff8326a58c28c042bb00c386b', '4510105e9e2142ae4331d8de4b295543fa8332f2')

CHILD = r'''
import json, sys
core, args = sys.argv[1], json.loads(sys.argv[2])
sys.path.insert(0, core)
import laya_blob_store, laya_view_store
assert laya_view_store.__file__.startswith(core), laya_view_store.__file__
out = {}
def attempt(name, fn):
    try:
        fn(); out[name] = 'accepted'
    except Exception as exc:
        out[name] = getattr(exc, 'reason', type(exc).__name__)
kw = dict(repository=args['repository'], scope_id=args['scope'])
attempt('blob_open', lambda: laya_blob_store.BlobStore.open(args['db'], **kw))
attempt('view_open', lambda: laya_view_store.ViewStore.open(args['db'], **kw))
attempt('view_open_reader', lambda: laya_view_store.ViewStore.open_reader(args['db'], **kw))
print(json.dumps(out, sort_keys=True))
'''


class OldBundleChecks(publish.IncrementalPublicationChecks):
    def file_state(self):
        names = [self.db.name + suffix for suffix in ('', '-journal', '-wal', '-shm')]
        state = {}
        for name in names:
            path = self.db.parent / name
            if path.exists():
                info = path.stat()
                state[name] = (info.st_ino, info.st_size, hashlib.sha256(path.read_bytes()).hexdigest())
        return state

    def test_released_cores_refuse_a_row_format_store_before_any_read_or_write(self):
        store, repository, handle = self.rows_store()
        self.publish(store, handle, b'# First\nuniquealpha synthetic card\n')
        store.close()
        before = self.file_state()
        observed = {}
        for commit in OLD_CORES:
            exported = Path(tempfile.mkdtemp(prefix='s-old-core-', dir=self.home))
            archive = subprocess.run(['git', '-C', str(ROOT), 'archive', commit, 'core'],
                                     capture_output=True, timeout=60)
            self.assertEqual(archive.returncode, 0, archive.stderr.decode(errors='replace'))
            subprocess.run(['tar', '-x', '-C', str(exported)], input=archive.stdout, check=True, timeout=60)
            result = subprocess.run(
                [sys.executable, '-B', '-c', CHILD, str(exported / 'core'),
                 json.dumps(dict(db=str(self.db), repository=repository, scope=self.scope))],
                capture_output=True, text=True, timeout=120, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            observed[commit] = json.loads(result.stdout.strip().splitlines()[-1])
        print(json.dumps(dict(old_core_refusals=observed, python=sys.version.split()[0]), sort_keys=True))
        for commit, out in observed.items():
            with self.subTest(commit=commit):
                self.assertEqual(out, dict(blob_open='candidate_format_mismatch',
                                           view_open='candidate_format_mismatch',
                                           view_open_reader='candidate_format_mismatch'))
        self.assertEqual(self.file_state(), before, 'the refusal reads nothing back into the file')
        again = self.reopen(repository)
        fresh = again.capture_view(handle.worktree_id)
        self.assertEqual(self.found(again, fresh, 'uniquealpha'), ['cards/a.md'])


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromName('test_released_cores_refuse_a_row_format_store_before_any_read_or_write',
                                    OldBundleChecks)


if __name__ == '__main__':
    unittest.main()

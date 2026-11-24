"""Reproduce two release histories whose surviving project and installation evidence agree."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from memvane.wiki.compile.tests.integration.test_pending_handoff import archived_tag
from tests.support.case import FixtureCase


class LegacyLedgerEvidence(FixtureCase):
    def _native_writer(self, root, target, env):
        source = root / 'docs/incoming.md'
        raw = source.read_bytes()
        ref = {'kind': 'file', 'collection': 'project', 'path': 'docs/incoming.md'}
        revision = {**ref, 'sha256': hashlib.sha256(raw).hexdigest(),
                    'size': len(raw), 'mtimeNs': source.stat().st_mtime_ns}
        candidate = {'title': 'Deleted', 'summary': 'A separate current decision.', 'sources': [ref],
                     'sourceRevisions': [revision], 'targetPath': target, 'engine': 'claude',
                     'trigger': 'post_tool_source'}
        result = subprocess.run([sys.executable, '-B', '-m', 'memvane.wiki.compile.wiki_compile', '--cwd', str(root)],
                                input=json.dumps(candidate), capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = root / '.memvane/compile/generated-manifest.jsonl'
        with manifest.open(encoding='utf-8') as ledger:
            entries = [json.loads(line) for line in ledger]
        actions = [row['action'] for row in entries if row['targetPath'] == target]
        return json.loads(result.stdout)['action'], (root / target).is_file(), actions

    def test_four_releases_and_three_separators_with_original_and_escaped_histories(self):
        repo = Path(__file__).resolve().parents[5]
        harness = repo / 'memvane/wiki/compile/tests/tagged_legacy_ledgers.py'
        for tag in ('v1.0.22', 'v1.0.23', 'v1.0.24', 'v1.1.0'):
            bundle = archived_tag(repo, self.tmp, tag)
            for code in (133, 8232, 8233):
                for encoding in ('literal', 'escaped'):
                    with self.subTest(tag=tag, code=code, encoding=encoding):
                        scratch = self.tmp / f'{tag}-{code}-{encoding}'
                        scratch.mkdir()
                        home = scratch / 'installed-home'
                        runtime = home / 'rt'
                        runtime.mkdir(mode=0o700, parents=True)
                        env = {**os.environ, 'HOME': str(home), 'MEMVANE_TEST_RUNTIME_ROOT': str(runtime)}
                        archived = {**env, 'PYTHONPATH': os.pathsep.join((str(bundle), str(repo)))}
                        child = subprocess.run([sys.executable, '-B', str(harness), str(bundle), str(scratch),
                                                str(code), encoding], capture_output=True, text=True, cwd=bundle,
                                               env=archived, timeout=30)
                        self.assertEqual(child.returncode, 0, child.stderr)
                        data = json.loads(child.stdout)
                        root, target = scratch / 'project', data['target']
                        self.assertTrue(data['identicalTrees'])
                        self.assertEqual(data['externalFiles'], 0)
                        self.assertFalse(any(chr(code) in name for name in data['paths']))
                        clean = self._native_writer(root, target, env)
                        root.rename(scratch / 'after-clean')
                        (scratch / 'deleted-history').rename(root)
                        deleted = self._native_writer(root, target, env)
                        self.assertEqual(clean, ('created', True, ['created']))
                        self.assertEqual(deleted, ('created', True, ['created']))
                        print(f'{tag} U+{code:04X} {encoding} never-created={clean} '
                              f'manual-deletion={deleted} project-files={len(data["paths"])} '
                              'installation-files=0', file=sys.stderr)

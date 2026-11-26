"""The native online writer keeps both public file identities while a released CLI waits."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import select
import subprocess
import sys
from unittest.mock import patch

from memvane.wiki.compile import wiki_compile as writer
from memvane.wiki.compile.tests.release_tags import archived_tag
from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase
from tests.support.project import make_project
from tests.support.settings_fake import expanded


class OnlinePreservation(FixtureCase):
    def test_no_online_ledger_swap_with_an_old_manual_writer_waiting(self):
        repo = Path(__file__).resolve().parents[5]
        for tag in ('v1.0.22', 'v1.0.23', 'v1.0.24', 'v1.1.0'):
            with self.subTest(tag=tag):
                self.exercise(repo, tag)

    def exercise(self, repo, tag):
        bundle = archived_tag(repo, self.tmp, tag)
        project = make_project(self.home, name='online', dirname=tag, git=False)
        root = project.root
        before = project.write_note('docs/special\u0085name.md', 'The deleted card source.')
        separate = project.write_note('docs/new.md', 'The new card source.')
        target = project.wiki / 'concepts/deleted.md'
        normal = target.with_name('unrelated.md')

        def candidate(source, path):
            rev = project.revision(source)
            return {'title': path.stem, 'summary': 'A decision for ' + path.stem, 'trigger': 'manual',
                    'targetPath': path.relative_to(root).as_posix(), 'engine': 'claude',
                    'sources': [{k: rev[k] for k in ('kind', 'path', 'collection')}], 'sourceRevisions': [rev]}

        env = {**os.environ, 'PYTHONPATH': os.pathsep.join((str(bundle), str(repo)))}
        helper = repo / 'memvane/wiki/compile/tests/tagged_transition_race.py'
        command = [sys.executable, '-Bu', str(helper), 'manual', str(root)]
        first = subprocess.run(command, input=json.dumps(candidate(before, target)), env=env, cwd=bundle,
                               capture_output=True, text=True, timeout=15)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)['action'], 'created')
        target.unlink()
        folder = root / '.memvane/compile'
        manifest, refresh = folder / 'generated-manifest.jsonl', folder / 'source-refresh-pending.jsonl'
        # This released CLI can still identify the prior generation when it is escaped.
        old = (json.dumps(json.loads(manifest.read_bytes()), ensure_ascii=True) + '\r\n').encode()
        original = old * (262144 // len(old) + 2)
        manifest.write_bytes(original)
        pending = {'ts': '2026-08-07T00:00:00Z', 'eventId': 'original', 'engine': 'codex',
                   'state': 'pending_refresh', 'sourcePath': before.relative_to(root).as_posix()}
        refresh_before = (json.dumps(pending, ensure_ascii=False) + '\r\n').encode()
        refresh.write_bytes(refresh_before)
        nodes = [path.stat() for path in (manifest, refresh)]
        config = expanded(project, compile={'verify': {'enabled': False}, 'semanticDedup': {'enabled': False}})
        with workspace.card_write_lock(root), workspace.mutation_lock(root):
            child = subprocess.Popen(command, cwd=bundle, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            def stop():
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=8)
            self.addCleanup(stop)
            child.stdin.write(json.dumps(candidate(separate, target)))
            child.stdin.close()
            child.stdin = None
            self.assertTrue(select.select([child.stderr], [], [], 8)[0])
            self.assertEqual(child.stderr.readline().strip(), 'waiting')
            writes = []
            def native(path):
                with redirect_stdout(io.StringIO()) as out:
                    writer._compile_locked(root, config, config['compile'], project.wiki,
                                           candidate(separate, path))
                return json.loads(out.getvalue())['action']
            with patch.object(os, 'replace', wraps=os.replace) as replace, patch.object(os, 'rename', wraps=os.rename) as rename:
                self.assertEqual(native(target), 'deferred')
                self.assertEqual(native(normal), 'created')
                workspace.compact_manifest(manifest, max_bytes=0)
                for call in [*replace.call_args_list, *rename.call_args_list]:
                    names = [Path(os.fsdecode(arg)).name for arg in call.args[:2]]
                    if {manifest.name, refresh.name}.intersection(names):
                        writes.append(names)
            self.assertEqual(writes, [])
            self.assertTrue(all(os.path.samestat(node, path.stat()) for node, path in zip(nodes, (manifest, refresh))))
            self.assertEqual(refresh.read_bytes(), refresh_before)
            current = manifest.read_bytes()
            self.assertTrue(current.startswith(original))
            self.assertEqual(json.loads(current[len(original):])['targetPath'], normal.relative_to(root).as_posix())
            self.assertFalse(target.exists())
            self.assertTrue(normal.is_file())
            self.assertIsNone(child.poll())
        out, err = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, err)
        self.assertEqual(json.loads(out)['action'], 'tombstoned')
        self.assertFalse(target.exists())

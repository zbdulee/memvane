"""1.x -> 2.0 cutover: decision ledgers imported once from release writers, the rest discarded."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest.mock import patch

from memvane.wiki.pipeline import api as pipeline
from memvane.wiki.pipeline.types import DB_NAME
from tests.support.case import FixtureCase

TAGS = ('v1.1.0', 'v1.1.1', 'v1.1.2')
REPO = Path(__file__).resolve().parents[5]


def archived(tag, directory):
    bundle = directory / tag
    if not bundle.exists():
        bundle.mkdir()
        archive = subprocess.run(['git', '-C', str(REPO), 'archive', tag, 'memvane', 'package.json'],
                                 check=True, capture_output=True)
        subprocess.run(['tar', '-x', '-C', str(bundle)], input=archive.stdout, check=True)
    return bundle


class V1MigrationTests(FixtureCase):
    def written_by(self, tag):
        bundle = archived(tag, self.tmp)
        scratch = self.tmp / f'{tag}-scratch'
        scratch.mkdir()
        env = {**os.environ, 'PYTHONPATH': os.pathsep.join((str(bundle), str(REPO)))}
        child = subprocess.run(
            [sys.executable, '-B', str(REPO / 'memvane/wiki/pipeline/tests/tagged_v1_ledgers.py'),
             str(bundle), str(scratch)], cwd=bundle, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(child.returncode, 0, child.stderr)
        data = json.loads(child.stdout)
        self.assertEqual(data['actions'], ['created', 'created', 'created'])
        return Path(data['root']), data['files']

    def in_progress(self, root):
        compile_dir, wiki = root / '.memvane/compile', root / '.memvane/wiki'
        for name in ('source-queue.jsonl', 'verify-queue.jsonl', 'source-refresh-pending.jsonl',
                     'tombstones.jsonl', 'verify-log.jsonl', 'dedup-deleted.jsonl', 'cooldown',
                     'source-queue.jsonl.claimed.1.abc'):
            (compile_dir / name).write_text('{}\n')
        (compile_dir / '.handoff/v1/hold/' / ('a' * 32)).mkdir(parents=True)
        (compile_dir / '.handoff/v1/copy-quota').mkdir(parents=True)
        (compile_dir / '.handoff/v1/copy-quota/allocated-x').write_text('1')
        (compile_dir / 'retired-wiki').mkdir(exist_ok=True)
        (compile_dir / 'retired-wiki/keep.md').write_text('# kept\n')
        (compile_dir / 'wiki-retrieval').mkdir(exist_ok=True)
        (compile_dir / 'wiki-retrieval/cards.sqlite').write_bytes(b'cache')
        for name in ('.first-' + 'b' * 32, '.alpha.md.pending-' + 'c' * 16, '.pending-ownership.json',
                     '.verify-swap-1', '.pc-' + 'd' * 32):
            (wiki / 'concepts' / name).write_text('hidden')

    def generations(self, root):
        with sqlite3.connect(str(root / '.memvane/compile' / DB_NAME)) as db:
            return [json.loads(r[0]) for r in db.execute('SELECT row_json FROM card_generations ORDER BY id')]

    def test_release_writer_ledgers_import_once_and_everything_else_is_discarded(self):
        for tag in TAGS:
            with self.subTest(tag=tag):
                root, files = self.written_by(tag)
                manifest = [json.loads(line) for line in files['generated-manifest.jsonl'].splitlines()]
                self.in_progress(root)
                self.assertTrue(pipeline.needs_v1_migration(root))
                counts = pipeline.migrate_from_v1(root)
                self.assertEqual((counts['generations'], counts['verifySuppressions'],
                                  counts['dedupSuppressions']), (3, 1, 1))
                self.assertEqual(self.generations(root), manifest)
                with pipeline.open_pipeline(root, readonly=True) as store:
                    self.assertEqual([r['action'] for r in store.generations(
                        target_path='.memvane/wiki/concepts/alpha.md')], ['created'])
                    verify_row = json.loads(files['verify-skipped.jsonl'])
                    self.assertEqual(store.suppressions('verify'), {'docs/beta.md': verify_row['sourceBodyHash']})
                    dedup_row = json.loads(files['dedup-skipped.jsonl'])
                    self.assertEqual(store.suppressions('dedup'), {
                        'concepts/alpha.md\nconcepts/beta.md': dedup_row['pageAHash'] + ':' + dedup_row['pageBHash']})
                compile_dir = root / '.memvane/compile'
                self.assertEqual(sorted(p.name for p in compile_dir.iterdir()),
                                 sorted(['retired-wiki', 'wiki-retrieval', DB_NAME,
                                         *(p.name for p in compile_dir.glob(DB_NAME + '-*')),
                                         *(p.name for p in compile_dir.glob('.*.lock'))]))
                self.assertEqual((compile_dir / 'retired-wiki/keep.md').read_text(), '# kept\n')
                hidden = [p.name for p in (root / '.memvane/wiki').rglob('.*') if p.name != '.memvane-root']
                self.assertEqual(hidden, [])
                self.assertFalse(pipeline.needs_v1_migration(root))
                again = pipeline.migrate_from_v1(root)
                self.assertTrue(again['alreadyMigrated'])
                self.assertEqual(len(self.generations(root)), 3)

    def test_crash_before_commit_reimports_exactly_once_and_after_marker_only_removes(self):
        root, _ = self.written_by('v1.1.2')
        from memvane.wiki.pipeline import migrate, store
        # The marker row is the last write inside the import transaction.
        with patch.object(store.time, 'time', side_effect=RuntimeError('crash before commit')), \
                self.assertRaises(RuntimeError):
            pipeline.migrate_from_v1(root)
        self.assertEqual(self.generations(root), [])
        self.assertTrue((root / '.memvane/compile/generated-manifest.jsonl').exists())
        self.assertTrue(pipeline.needs_v1_migration(root))
        with patch.object(migrate, '_remove_legacy', side_effect=RuntimeError('crash after marker')), \
                self.assertRaises(RuntimeError):
            pipeline.migrate_from_v1(root)
        self.assertEqual(len(self.generations(root)), 3)
        self.assertTrue((root / '.memvane/compile/generated-manifest.jsonl').exists())
        self.assertFalse(pipeline.needs_v1_migration(root))
        again = pipeline.migrate_from_v1(root)
        self.assertTrue(again['alreadyMigrated'])
        self.assertEqual(len(self.generations(root)), 3)
        self.assertFalse((root / '.memvane/compile/generated-manifest.jsonl').exists())

    def test_same_second_same_bytes_rows_are_both_kept(self):
        root, files = self.written_by('v1.1.2')
        manifest = root / '.memvane/compile/generated-manifest.jsonl'
        first = files['generated-manifest.jsonl'].splitlines()[0]
        manifest.write_text(files['generated-manifest.jsonl'] + first + '\n', encoding='utf-8')
        counts = pipeline.migrate_from_v1(root)
        self.assertEqual(counts['generations'], 4)

    def test_project_without_1x_files_gets_the_marker(self):
        root = self.tmp / 'fresh'
        (root / '.memvane/compile').mkdir(parents=True)
        self.assertTrue(pipeline.needs_v1_migration(root))
        counts = pipeline.migrate_from_v1(root)
        self.assertEqual((counts['generations'], counts['removed']), (0, 0))
        self.assertFalse(pipeline.needs_v1_migration(root))

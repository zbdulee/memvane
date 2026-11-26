"""Integration tests for one-time 1.x -> 2.0 migration into ``wiki-pipeline.sqlite3``."""
from __future__ import annotations

import json

from memvane.wiki.pipeline import api as pipeline
from tests.support.case import FixtureCase
from tests.support.project import make_project


class V1MigrationTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)
        self.compile_dir = self.project.root / '.memvane/compile'
        self.wiki = self.project.root / '.memvane/wiki'
        self.compile_dir.mkdir(parents=True, exist_ok=True)
        self.wiki.mkdir(parents=True, exist_ok=True)

    def test_full_v1_migration_preserves_retired_wiki_recomputes_quota_and_is_idempotent(self):
        (self.compile_dir / 'source-queue.jsonl').write_text(
            json.dumps({'source': {'collection': 'docs', 'path': 'docs/a.md'}, 'trigger': 'post_tool_source', 'engine': 'claude'}) + '\n'
        )
        (self.compile_dir / 'source-queue.jsonl.claimed.99.tok').write_text(
            json.dumps({'source': {'collection': 'docs', 'path': 'docs/b.md'}, 'trigger': 'post_sync_source', 'engine': 'codex'}) + '\n'
        )
        (self.compile_dir / 'verify-queue.jsonl').write_text(
            json.dumps({'targetPath': 'concepts/a.md', 'sourceHash': 'h1'}) + '\n'
        )
        (self.compile_dir / 'source-refresh-pending.jsonl').write_text(
            json.dumps({'eventId': 'ev-pub', 'sourcePath': 'docs/a.md', 'ts': '2026-11-26T00:00:00Z', 'engine': 'claude'}) + '\n'
        )
        priv_refresh = self.compile_dir / '.handoff/v1/refresh/events.jsonl'
        priv_refresh.parent.mkdir(parents=True, exist_ok=True)
        priv_refresh.write_text(
            json.dumps({'eventId': 'ev-priv', 'sourcePath': 'docs/b.md', 'ts': '2026-11-26T00:00:01Z', 'engine': 'codex'}) + '\n'
        )
        (self.compile_dir / 'source-refresh-settled.json').write_text(
            json.dumps({'version': 1, 'sources': {'docs/b.md': {'eventId': 'ev-priv', 'sha256': 's2', 'reason': 'settled'}}})
        )
        for name in ('generated-manifest.jsonl', 'tombstones.jsonl', 'verify-deleted.jsonl', 'verify-skipped.jsonl', 'dedup-skipped.jsonl'):
            (self.compile_dir / name).write_text(json.dumps({'targetPath': 'concepts/a.md', 'sourceBodyHash': 'bh'}) + '\n')
        (self.compile_dir / 'generated-manifest.jsonl.compact-stamp').write_text('1')
        (self.compile_dir / 'cooldown').write_text('1234.5')
        (self.compile_dir / 'verify-engine-cooldown.json').write_text(json.dumps({'claude': 2345.0}))

        hold_entry = self.compile_dir / '.handoff/v1/hold' / ('a' * 32)
        hold_entry.mkdir(parents=True, exist_ok=True)
        (hold_entry / 'raw').write_text(json.dumps({'targetPath': 'concepts/a.md', 'expectedTargetHash': 'oldsha'}))
        (hold_entry / 'image').write_bytes(b'hello-card')

        quota_dir = self.compile_dir / '.handoff/v1/copy-quota'
        quota_dir.mkdir(parents=True, exist_ok=True)
        (quota_dir / 'allocated-deadbeef').write_text('99999999')

        hidden = self.wiki / 'concepts/.first-1234'
        hidden.parent.mkdir(parents=True, exist_ok=True)
        hidden.write_text('stale temp')
        (self.wiki / '.pending-ownership.json').write_text('{}')

        retired = self.compile_dir / 'retired-wiki/keep.md'
        retired.parent.mkdir(parents=True, exist_ok=True)
        retired.write_text('# Retired Archive\n', encoding='utf-8')

        self.assertTrue(pipeline.needs_v1_migration(self.project.root, wiki_root=self.wiki))
        summary = pipeline.migrate_from_v1(self.project.root, wiki_root=self.wiki)
        self.assertFalse(summary['alreadyMigrated'])
        self.assertEqual(summary['compileJobs'], 2)
        self.assertEqual(summary['verifyJobs'], 1)
        self.assertEqual(summary['refreshes'], 2)
        self.assertEqual(summary['staged'], 1)
        self.assertGreater(summary['removed'], 0)
        self.assertFalse(pipeline.needs_v1_migration(self.project.root, wiki_root=self.wiki))

        # Retired archive preserved verbatim; legacy files removed.
        self.assertEqual(retired.read_text(encoding='utf-8'), '# Retired Archive\n')
        self.assertFalse((self.compile_dir / '.handoff').exists())
        self.assertFalse(hidden.exists())
        self.assertFalse((self.compile_dir / 'source-queue.jsonl').exists())

        with pipeline.open_pipeline(self.project.root, readonly=True) as store:
            self.assertEqual(store.staged_quota_bytes(), len(b'hello-card'))
            self.assertEqual(store.get_cooldown('verify_engine', 'claude')['until_ts'], 2345.0)
            self.assertEqual(len(store.unresolved_refreshes()), 2)

        again = pipeline.migrate_from_v1(self.project.root, wiki_root=self.wiki)
        self.assertTrue(again['alreadyMigrated'])
        self.assertEqual(again['compileJobs'], 0)

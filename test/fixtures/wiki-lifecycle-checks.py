"""SessionStart wiki lifecycle against temp fixtures only (no real project wiki)."""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import wiki_lifecycle as lifecycle
import wiki_selected_laya
import yaml_scalars

COMPILE = {'mode': 'auto-wiki', 'triggers': ['session_start_source', 'post_tool_source']}


class WikiLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / 'project'
        self.wiki = self.root / '.auto-context/wiki'
        (self.wiki / 'concepts').mkdir(parents=True)
        (self.root / 'docs').mkdir()
        self.state = Path(self.tmp.name).resolve() / 'state'
        self.log = Path(self.tmp.name).resolve() / 'lifecycle.log'
        self.write_settings()
        self.selected = ({
            'projectRoot': str(self.root),
            'installId': 'i' * 32, 'generation': 'g1', 'scopeId': 's' * 32,
            'dbIncarnation': 'd' * 32,
            'recallSettings': {'skipPaths': []},
            'collectionRoots': {'proj-wiki': str(self.wiki), 'proj-docs': str(self.root / 'docs')},
            'collections': [{'name': 'proj-wiki', 'role': 'wiki', 'sourceRoot': str(self.wiki)},
                            {'name': 'proj-docs', 'role': 'raw',
                             'sourceRoot': str(self.root / 'docs')}]}, {})
        env = patch.dict(os.environ, {'QMD_SYNC_STATE_DIR': str(self.state),
                                      'QMD_WIKI_LIFECYCLE_LOG': str(self.log),
                                      'QMD_SOURCE_SCAN_MAX': '', 'QMD_ENGINE': 'claude'})
        env.start()
        self.addCleanup(env.stop)

    def write_settings(self, compile_cfg=None, **extra):
        (self.root / '.auto-context/settings.json').write_text(json.dumps({
            'indexing': True,
            'collections': ['proj-docs', 'proj-wiki'],
            'collectionPaths': {'proj-docs': 'docs', 'proj-wiki': '.auto-context/wiki'},
            'collectionRoles': {'proj-docs': 'raw', 'proj-wiki': 'wiki'},
            'wikiPath': '.auto-context/wiki',
            'compile': compile_cfg if compile_cfg is not None else COMPILE,
            **extra}))

    def source(self, name, body):
        path = self.root / 'docs' / name
        path.write_bytes(body)
        return path

    def revision(self, name, body=None):
        path = self.root / 'docs' / name
        data = body if body is not None else path.read_bytes()
        info = path.stat() if path.exists() else None
        return {'kind': 'file', 'path': 'docs/' + name, 'collection': 'proj-docs',
                'sha256': hashlib.sha256(data).hexdigest(),
                'size': info.st_size if info else len(data),
                'mtimeNs': info.st_mtime_ns if info else 1}

    def card(self, name, revisions=(), created_by='qmd-auto-context', provenance=True,
             raw_revisions=None, folder='concepts'):
        lines = ['---', 'title: Card ' + name, 'status: generated', 'createdBy: ' + created_by]
        if raw_revisions is not None:
            lines += ['sourceRevisions:', raw_revisions]
        elif provenance:
            lines.append('sourceRevisions:')
            lines += ['  - ' + item for item in yaml_scalars.dump_source_revisions(list(revisions))]
        lines += ['---', '', '<!-- qmd:auto:start id="main" sourceHash="a" -->', '## Summary',
                  'fact from ' + name, '<!-- qmd:auto:end -->', '']
        path = self.wiki / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(lines), encoding='utf-8')
        return path

    def run_lifecycle(self, selected='default'):
        bound = self.selected if selected == 'default' else selected
        with patch.object(wiki_selected_laya, 'selected', return_value=bound), \
             patch.object(wiki_selected_laya, 'recheck'), \
             patch.object(wiki_selected_laya, 'request_sync', return_value=True) as sync, \
             patch.object(wiki_selected_laya, 'request_compile') as compile_wake:
            result = lifecycle.run(str(self.root))
        return result, sync.call_count, compile_wake.call_count

    def queue_rows(self):
        path = self.root / '.auto-context/compile/source-queue.jsonl'
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def archived(self):
        return sorted((self.root / '.auto-context/compile/retired-wiki').glob('*.md'))

    def test_missing_source_retires_card_and_wakes_laya_sync(self):
        gone = self.revision('gone.md', b'old body')
        card = self.card('gone-card.md', [gone])
        old = card.read_bytes()
        result, syncs, _ = self.run_lifecycle()
        self.assertFalse(card.exists())
        self.assertEqual(result['retired'], 1)
        self.assertGreaterEqual(syncs, 1)
        self.assertEqual([path.read_bytes() for path in self.archived()], [old])
        self.assertIn('RETIRE source_missing', self.log.read_text())

    def test_changed_hash_enqueues_regeneration_and_keeps_card(self):
        self.source('a.md', b'first')
        rev = self.revision('a.md')
        card = self.card('a-card.md', [rev])
        self.source('a.md', b'second edit')
        result, syncs, compile_wakes = self.run_lifecycle()
        self.assertTrue(card.exists())
        rows = self.queue_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['source']['path'], 'docs/a.md')
        self.assertEqual(rows[0]['contentRevision'], hashlib.sha256(b'second edit').hexdigest())
        self.assertNotIn('sourceDelete', rows[0])
        self.assertEqual(result['enqueued'], 1)
        self.assertEqual(syncs, 0)
        self.assertEqual(compile_wakes, 1)

    def test_card_without_source_revisions_is_retired(self):
        card = self.card('legacy.md', provenance=False)
        result, syncs, _ = self.run_lifecycle()
        self.assertFalse(card.exists())
        self.assertEqual(result['retired'], 1)
        self.assertEqual(syncs, 1)
        self.assertEqual(len(self.archived()), 1)

    def test_unchanged_card_is_kept(self):
        self.source('same.md', b'stable')
        card = self.card('same-card.md', [self.revision('same.md')])
        before = card.read_bytes()
        result, syncs, compile_wakes = self.run_lifecycle()
        self.assertEqual(card.read_bytes(), before)
        self.assertEqual(result['kept'], 1)
        self.assertEqual((result['retired'], syncs, compile_wakes), (0, 0, 0))
        self.assertEqual(self.queue_rows(), [])

    def test_manual_and_unsafe_cards_are_untouched(self):
        human_missing = self.card('human.md', [self.revision('gone.md', b'x')], created_by='human')
        human_unproven = self.card('human-legacy.md', created_by='human', provenance=False)
        hidden = self.card('hidden.md', provenance=False, folder='.drafts')
        outside = Path(self.tmp.name) / 'outside.md'
        outside.write_text('---\ntitle: X\ncreatedBy: qmd-auto-context\n---\n'
                           '<!-- qmd:auto:start id="main" sourceHash="a" -->\nx\n'
                           '<!-- qmd:auto:end -->\n')
        link = self.wiki / 'concepts' / 'link.md'
        link.symlink_to(outside)
        no_block = self.wiki / 'concepts' / 'no-block.md'
        no_block.write_text('---\ntitle: N\ncreatedBy: qmd-auto-context\n---\nhand body\n')
        result, syncs, _ = self.run_lifecycle()
        for path in (human_missing, human_unproven, hidden, no_block):
            self.assertTrue(path.exists(), path)
        self.assertTrue(link.is_symlink())
        self.assertTrue(outside.exists())
        self.assertEqual((result['retired'], syncs), (0, 0))

    def test_malformed_provenance_is_kept(self):
        card = self.card('bad.md', raw_revisions='  - {kind: file, path: docs/x.md}')
        result, _, _ = self.run_lifecycle()
        self.assertTrue(card.exists())
        self.assertEqual(result['undecidable'], 1)

    def test_mass_deletion_guard_defers_everything(self):
        self.source('keep.md', b'k')
        self.card('keep.md', [self.revision('keep.md')])
        cards = [self.card(f'm{i}.md', [self.revision(f'gone-{i}.md', b'g')]) for i in range(5)]
        unproven = self.card('legacy.md', provenance=False)
        result, syncs, _ = self.run_lifecycle()
        self.assertTrue(all(card.exists() for card in cards))
        self.assertTrue(unproven.exists())
        self.assertEqual(result['guard'], {'missingCards': 5, 'provedCards': 6})
        self.assertEqual((result['retired'], syncs), (0, 0))
        self.assertEqual(self.queue_rows(), [])
        self.assertIn('mass-deletion guard', self.log.read_text())

    def test_guard_floor_allows_a_small_wiki_to_retire(self):
        cards = [self.card(f'm{i}.md', [self.revision(f'gone-{i}.md', b'g')]) for i in range(3)]
        result, _, _ = self.run_lifecycle()
        self.assertIsNone(result['guard'])
        self.assertFalse(any(card.exists() for card in cards))

    def test_retire_cap_resumes_next_session(self):
        cards = [self.card(f'u{i:02d}.md', provenance=False)
                 for i in range(lifecycle.MAX_RETIRE_PER_RUN + 5)]
        first, _, _ = self.run_lifecycle()
        self.assertEqual(first['retired'], lifecycle.MAX_RETIRE_PER_RUN)
        second, _, _ = self.run_lifecycle()
        self.assertEqual(second['retired'], 5)
        self.assertFalse(any(card.exists() for card in cards))

    def test_window_rotates_across_sessions(self):
        cards = [self.card(f'w{i}.md', provenance=False) for i in range(3)]
        with patch.dict(os.environ, {'QMD_SOURCE_SCAN_MAX': '2'}):
            first, _, _ = self.run_lifecycle()
            self.assertEqual((first['examined'], first['retired']), (2, 2))
            second, _, _ = self.run_lifecycle()
        self.assertEqual(second['retired'], 1)
        self.assertFalse(any(card.exists() for card in cards))

    def test_runs_are_idempotent(self):
        self.source('a.md', b'first')
        rev = self.revision('a.md')
        card = self.card('a-card.md', [rev])
        gone = self.card('gone.md', [self.revision('gone.md', b'old')])
        self.source('a.md', b'second edit')
        first, _, _ = self.run_lifecycle()
        self.assertEqual((first['retired'], first['enqueued']), (1, 1))
        archive = self.archived()
        # Same queue still live: no duplicate row.
        second, syncs, _ = self.run_lifecycle()
        self.assertEqual(len(self.queue_rows()), 1)
        self.assertEqual((second['retired'], second['enqueued'], syncs), (0, 0, 0))
        # Worker drained the queue but the card still records the old hash: the
        # same revision is not enqueued again.
        (self.root / '.auto-context/compile/source-queue.jsonl').unlink()
        third, _, _ = self.run_lifecycle()
        self.assertEqual((third['enqueued'], self.queue_rows()), (0, []))
        self.assertTrue(card.exists())
        self.assertFalse(gone.exists())
        self.assertEqual(self.archived(), archive)
        # A newer edit is a new revision and is enqueued.
        self.source('a.md', b'third edit')
        fourth, _, _ = self.run_lifecycle()
        self.assertEqual(fourth['enqueued'], 1)

    def test_fresh_sibling_does_not_reset_a_stale_card_refresh(self):
        self.source('a.md', b'first')
        stale = self.card('a-stale.md', [self.revision('a.md')])
        self.source('a.md', b'second edit')
        fresh = self.card('a-fresh.md', [self.revision('a.md')])
        first, _, _ = self.run_lifecycle()
        self.assertEqual(first['enqueued'], 1)
        (self.root / '.auto-context/compile/source-queue.jsonl').unlink()
        second, _, _ = self.run_lifecycle()
        self.assertEqual((second['enqueued'], self.queue_rows()), (0, []))
        self.assertTrue(stale.exists() and fresh.exists())

    def test_minimal_settings_regenerate_without_trigger_consent(self):
        (self.root / '.auto-context/settings.json').write_text(json.dumps({'name': 'project'}))
        self.source('a.md', b'first')
        rev = self.revision('a.md')
        rev['collection'] = 'project'
        card = self.card('a-card.md', [rev])
        self.source('a.md', b'edited')
        result, _, compile_wakes = self.run_lifecycle()
        rows = self.queue_rows()
        self.assertEqual((result['enqueued'], len(rows), compile_wakes), (1, 1, 1))
        self.assertEqual(rows[0]['source']['path'], 'docs/a.md')
        self.assertEqual(rows[0]['contentRevision'], hashlib.sha256(b'edited').hexdigest())
        self.assertTrue(card.exists())

    def test_card_caps_match_the_retire_helper(self):
        import wiki_source_retire
        self.assertEqual((lifecycle.MAX_CARDS, lifecycle.MAX_CARD_BYTES, lifecycle.MAX_TOTAL_BYTES),
                         (wiki_source_retire.MAX_CARDS, wiki_source_retire.MAX_CARD_BYTES,
                          wiki_source_retire.MAX_TOTAL_BYTES))

    def test_shared_card_with_live_source_hands_off_to_compile_worker(self):
        self.source('live.md', b'live')
        card = self.card('shared.md', [self.revision('gone.md', b'old'), self.revision('live.md')])
        result, _, _ = self.run_lifecycle()
        self.assertTrue(card.exists())
        rows = self.queue_rows()
        self.assertEqual([(row['source']['path'], row.get('sourceDelete')) for row in rows],
                         [('docs/gone.md', True)])
        self.assertEqual(rows[0]['contentRevision'], hashlib.sha256(b'old').hexdigest())
        self.assertEqual(result['retired'], 0)

    def test_legacy_route_and_kill_switch_do_not_act(self):
        card = self.card('legacy.md', provenance=False)
        result, syncs, _ = self.run_lifecycle(selected=None)
        self.assertTrue(card.exists())
        self.assertEqual((result['route'], result['ineligibleMeasured'], syncs),
                         ('legacy', True, 0))
        self.write_settings({**COMPILE, 'sourceScan': {'enabled': False}})
        result, _, _ = self.run_lifecycle()
        self.assertTrue(card.exists())
        self.assertEqual(result['examined'], 0)

    def test_regeneration_without_session_start_consent_is_not_enqueued(self):
        self.write_settings({'mode': 'auto-wiki', 'triggers': ['post_tool_source']})
        self.source('a.md', b'first')
        self.card('a-card.md', [self.revision('a.md')])
        self.source('a.md', b'edited')
        result, _, _ = self.run_lifecycle()
        self.assertEqual((result['enqueued'], self.queue_rows()), (0, []))
        self.assertIn('ENQUEUE compile_disabled', self.log.read_text())
        # Without a recorded enqueue the next consenting run still refreshes it.
        self.write_settings()
        result, _, _ = self.run_lifecycle()
        self.assertEqual(result['enqueued'], 1)


if __name__ == '__main__':
    unittest.main()

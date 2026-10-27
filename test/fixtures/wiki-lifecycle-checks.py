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
             raw_revisions=None, folder='concepts', sources=None):
        lines = ['---', 'title: Card ' + name, 'status: generated', 'createdBy: ' + created_by]
        if sources is not None:
            # Legacy compiler shape: a `sources:` file list written before `sourceRevisions`.
            lines.append('sources:' if sources else 'sources: []')
            for item in sources:
                lines.append('  - ' + (item if isinstance(item, str) else
                                       '{kind: "file", path: "%s", collection: "%s"}' % item))
            lines.append('triggers:')
            lines.append('  - post_tool_source')
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
        self.assertEqual(result['guard'], {'wouldDelete': 6, 'decidedCards': 7, 'missingCards': 5,
                                           'legacyMissingCards': 0, 'noSourceCards': 1})
        self.assertEqual((result['retired'], syncs), (0, 0))
        self.assertEqual(self.queue_rows(), [])
        self.assertIn('mass-deletion guard', self.log.read_text())

    def test_guard_floor_allows_a_small_wiki_to_retire(self):
        cards = [self.card(f'm{i}.md', [self.revision(f'gone-{i}.md', b'g')]) for i in range(3)]
        result, _, _ = self.run_lifecycle()
        self.assertIsNone(result['guard'])
        self.assertFalse(any(card.exists() for card in cards))

    def test_retire_cap_resumes_next_session(self):
        # No-source cards count toward the guard, so the corpus stays at its floor.
        cards = [self.card(f'u{i:02d}.md', provenance=False) for i in range(3)]
        with patch.object(lifecycle, 'MAX_RETIRE_PER_RUN', 2):
            first, _, _ = self.run_lifecycle()
            self.assertEqual(first['retired'], 2)
            second, _, _ = self.run_lifecycle()
        self.assertEqual(second['retired'], 1)
        self.assertFalse(any(card.exists() for card in cards))

    # --- legacy cards: no `sourceRevisions`, provenance only in `sources:` ---

    def frontmatter_revisions(self, card):
        import recall
        text = card.read_text(encoding='utf-8')
        return recall.frontmatter_source_revisions(text.split('\n---', 1)[0])

    def test_legacy_card_with_existing_sources_is_backfilled_not_deleted(self):
        self.source('a.md', b'alpha')
        self.source('b.md', b'beta')
        card = self.card('legacy.md', provenance=False,
                         sources=[('docs/a.md', 'proj-docs'), ('docs/b.md', 'proj-docs')])
        before = card.read_text(encoding='utf-8')
        result, syncs, compile_wakes = self.run_lifecycle()
        self.assertTrue(card.exists())
        self.assertEqual((result['retired'], result['backfilled'], result['enqueued']), (0, 1, 0))
        self.assertEqual((syncs, compile_wakes), (0, 0))
        self.assertEqual(self.archived(), [])
        revisions = self.frontmatter_revisions(card)
        self.assertEqual([(item['path'], item['collection'], item['sha256']) for item in revisions],
                         [('docs/a.md', 'proj-docs', hashlib.sha256(b'alpha').hexdigest()),
                          ('docs/b.md', 'proj-docs', hashlib.sha256(b'beta').hexdigest())])
        # Only the `sourceRevisions` block is added; every other line is unchanged.
        after = card.read_text(encoding='utf-8')
        added = [line for line in after.split('\n') if line not in before.split('\n')]
        self.assertEqual(added[0], 'sourceRevisions:')
        self.assertTrue(all(line.startswith('  - {kind: "file"') for line in added[1:]))
        self.assertEqual(len(added), 3)
        self.assertIn('BACKFILL source_revisions', self.log.read_text())
        # Next session: a normal recorded card, kept; an edit then regenerates it.
        second, _, _ = self.run_lifecycle()
        self.assertEqual((second['kept'], second['backfilled'], second['retired']), (1, 0, 0))
        self.source('a.md', b'alpha edited')
        third, _, _ = self.run_lifecycle()
        self.assertEqual(third['enqueued'], 1)
        self.assertTrue(card.exists())

    def test_legacy_card_with_missing_source_is_retired(self):
        card = self.card('legacy-gone.md', provenance=False,
                         sources=[('docs/gone.md', 'proj-docs')])
        old = card.read_bytes()
        result, syncs, _ = self.run_lifecycle()
        self.assertFalse(card.exists())
        self.assertEqual((result['retired'], result['backfilled']), (1, 0))
        self.assertEqual(syncs, 1)
        self.assertEqual([path.read_bytes() for path in self.archived()], [old])
        self.assertIn('RETIRE legacy_source_missing', self.log.read_text())

    def test_legacy_card_with_partly_missing_sources_is_kept(self):
        self.source('live.md', b'live')
        card = self.card('legacy-shared.md', provenance=False,
                         sources=[('docs/live.md', 'proj-docs'), ('docs/gone.md', 'proj-docs')])
        before = card.read_bytes()
        result, _, _ = self.run_lifecycle()
        self.assertEqual(card.read_bytes(), before)
        self.assertEqual((result['retired'], result['backfilled'], result['undecidable']), (0, 0, 1))
        self.assertIn('legacy_partial_missing', self.log.read_text())

    def test_legacy_card_without_file_sources_is_retired(self):
        empty = self.card('empty.md', provenance=False, sources=[])
        session = self.card('session.md', provenance=False,
                            sources=['{kind: "session", path: "", collection: ""}'])
        result, _, _ = self.run_lifecycle()
        self.assertFalse(empty.exists() or session.exists())
        self.assertEqual(result['retired'], 2)

    def test_legacy_card_with_unreadable_sources_is_kept(self):
        card = self.card('block.md', provenance=False,
                         sources=['{kind: "file", path: "docs/a.md", collection: "proj-docs"}',
                                  '{kind: "file", path: "docs/b.md", collection: "proj-docs"'])
        before = card.read_bytes()
        result, _, _ = self.run_lifecycle()
        self.assertEqual(card.read_bytes(), before)
        self.assertEqual((result['retired'], result['undecidable']), (0, 1))

    def test_cross_collection_source_resolves(self):
        other = Path(self.tmp.name).resolve() / 'other-checkout'
        (other / 'tasks').mkdir(parents=True)
        (other / 'tasks/x.md').write_bytes(b'cross')
        self.write_settings(allowRoots=[str(other)])
        # Allowed root, collection name this project does not define.
        absolute = self.card('cross-abs.md', provenance=False,
                             sources=[(str(other / 'tasks/x.md'), 'service-engineering')])
        # Root-relative path under a stale collection name (renamed collection).
        self.source('plot.md', b'plot')
        renamed = self.card('renamed.md', provenance=False, sources=[('docs/plot.md', 'old-plot')])
        # A foreign collection whose path is absent here may live in another
        # checkout: never provably gone.
        foreign = self.card('foreign.md', provenance=False,
                            sources=[('tasks/elsewhere.md', 'service-engineering')])
        result, _, _ = self.run_lifecycle()
        self.assertEqual((result['backfilled'], result['retired'], result['undecidable']), (2, 0, 1))
        self.assertEqual([(item['path'], item['collection'])
                          for item in self.frontmatter_revisions(absolute)],
                         [(str(other / 'tasks/x.md'), 'service-engineering')])
        self.assertEqual([(item['path'], item['collection'])
                          for item in self.frontmatter_revisions(renamed)],
                         [('docs/plot.md', 'old-plot')])
        self.assertTrue(foreign.exists())
        self.assertEqual(self.frontmatter_revisions(foreign), [])
        self.assertIn('source_foreign_collection', self.log.read_text())
        # The backfilled cross-root card is then judged like any recorded card.
        second, _, _ = self.run_lifecycle()
        self.assertEqual((second['kept'], second['retired']), (2, 0))

    def test_guard_counts_no_source_retirements(self):
        self.source('keep.md', b'k')
        self.card('keep.md', [self.revision('keep.md')])
        cards = [self.card(f'n{i}.md', provenance=False) for i in range(4)]
        result, syncs, _ = self.run_lifecycle()
        self.assertTrue(all(card.exists() for card in cards))
        self.assertEqual(result['guard'], {'wouldDelete': 4, 'decidedCards': 5, 'missingCards': 0,
                                           'legacyMissingCards': 0, 'noSourceCards': 4})
        self.assertEqual((result['retired'], syncs), (0, 0))

    def test_guard_does_not_count_backfillable_legacy_cards(self):
        for i in range(6):
            self.source(f's{i}.md', f'body {i}'.encode())
        cards = [self.card(f'l{i}.md', provenance=False, sources=[(f'docs/s{i}.md', 'proj-docs')])
                 for i in range(6)]
        result, _, _ = self.run_lifecycle()
        self.assertIsNone(result['guard'])
        self.assertTrue(all(card.exists() for card in cards))
        self.assertEqual((result['backfilled'], result['retired']), (6, 0))

    def test_guard_defers_backfill_too(self):
        self.source('a.md', b'a')
        legacy = self.card('legacy.md', provenance=False, sources=[('docs/a.md', 'proj-docs')])
        before = legacy.read_bytes()
        for i in range(4):
            self.card(f'g{i}.md', provenance=False, sources=[(f'docs/gone-{i}.md', 'proj-docs')])
        result, _, _ = self.run_lifecycle()
        self.assertEqual(result['guard']['legacyMissingCards'], 4)
        self.assertEqual((result['backfilled'], result['retired']), (0, 0))
        self.assertEqual(legacy.read_bytes(), before)

    def test_backfill_cap_converges_across_sessions(self):
        cards = []
        for i in range(3):
            self.source(f'c{i}.md', f'c{i}'.encode())
            cards.append(self.card(f'c{i}.md', provenance=False,
                                   sources=[(f'docs/c{i}.md', 'proj-docs')]))
        with patch.object(lifecycle, 'MAX_BACKFILL_PER_RUN', 2):
            first, _, _ = self.run_lifecycle()
            second, _, _ = self.run_lifecycle()
            third, _, _ = self.run_lifecycle()
        self.assertEqual((first['backfilled'], second['backfilled'], third['backfilled']), (2, 1, 0))
        self.assertEqual(third['kept'], 3)
        self.assertTrue(all(self.frontmatter_revisions(card) for card in cards))

    def test_backfill_refuses_bytes_changed_after_judgement(self):
        import wiki_source_retire
        self.source('a.md', b'a')
        card = self.card('raced.md', provenance=False, sources=[('docs/a.md', 'proj-docs')])
        judged = card.read_text(encoding='utf-8')
        edited = judged.replace('fact from', 'human edit from')
        card.write_text(edited, encoding='utf-8')
        revisions = [{'kind': 'file', 'path': 'docs/a.md', 'collection': 'proj-docs',
                      'sha256': hashlib.sha256(b'a').hexdigest(), 'size': 1, 'mtimeNs': 1}]
        outcome = wiki_source_retire.record_source_revisions(
            self.root, self.wiki, None, card, judged, revisions)
        self.assertEqual(outcome, 'changed')
        self.assertEqual(card.read_text(encoding='utf-8'), edited)

    def test_dry_run_reports_states_and_writes_nothing(self):
        self.source('same.md', b'same')
        self.card('keep.md', [self.revision('same.md')])
        self.source('edit.md', b'v1')
        self.card('changed.md', [self.revision('edit.md')])
        self.source('edit.md', b'v2')
        self.card('missing.md', [self.revision('gone.md', b'g')])
        self.source('legacy.md', b'l')
        self.card('backfill.md', provenance=False, sources=[('docs/legacy.md', 'proj-docs')])
        self.card('legacy-gone.md', provenance=False, sources=[('docs/lg.md', 'proj-docs')])
        self.card('nosource.md', provenance=False)
        self.card('foreign.md', provenance=False, sources=[('x/y.md', 'elsewhere')])
        self.card('human.md', provenance=False, created_by='human')

        def tree():
            base = Path(self.tmp.name)
            return {str(path): (path.stat().st_mtime_ns, path.stat().st_size)
                    for path in base.rglob('*') if path.is_file()}
        before = tree()
        with patch.object(wiki_selected_laya, 'selected', side_effect=AssertionError('route')):
            report = lifecycle.dry_run(str(self.root))
        self.assertEqual(tree(), before)
        self.assertFalse(self.log.exists())
        self.assertEqual(report['counts'], {'keep': 1, 'changed': 1, 'backfill': 1, 'missing': 1,
                                            'legacyMissing': 1, 'retire': 1, 'undecidable': 1,
                                            'manual': 1})
        self.assertEqual(report['undecidableReasons'], {'source_foreign_collection': 1})
        self.assertEqual(report['guard'], {'wouldDelete': 3, 'decidedCards': 6, 'missingCards': 1,
                                           'legacyMissingCards': 1, 'noSourceCards': 1,
                                           'tripped': False})

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

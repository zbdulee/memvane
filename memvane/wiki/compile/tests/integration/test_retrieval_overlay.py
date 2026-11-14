"""Bounded live overlay retrieval over synthetic selected projects."""
from contextlib import closing, contextmanager, redirect_stderr
import hashlib
import io
import json
import sqlite3
from unittest.mock import patch

from memvane.wiki.compile import selected_execution as execution, wiki_compile_retrieval as retrieval
from memvane.wiki.compile import wiki_compile_worker as worker
from memvane.wiki.engines import api as engines
from memvane.wiki.workspace import api as workspace
from memvane.provider import api as provider
from memvane.resident import api as resident
from tests.support.case import FixtureCase
from tests.support.project import make_project
from tests.support.settings_fake import expanded, use_config


class OverlayChecks(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)
        self.root, self.wiki = self.project.root, self.project.wiki
        self.bound = ({'state': 'candidate', 'projectRoot': str(self.root),
                       'installId': 'a' * 32, 'scopeId': 'b' * 32,
                       'storeIncarnation': 'c' * 32, 'generation': 'g1',
                       'recallSettings': {'skipPaths': []},
                       'collections': [{'name': 'wiki', 'role': 'wiki', 'sourceRoot': str(self.wiki)}]},
                      {'productRouteAllowed': True})
        self.published = []
        self.exclusions = (frozenset(), frozenset())
        @contextmanager
        def search(*_args):
            yield self.published, self.exclusions, 'wiki', 'bound'
        for item in (patch.object(retrieval, 'selected_search', search),
                     patch.object(workspace, 'recheck'), patch.object(execution, 'recheck')):
            item.start()
            self.addCleanup(item.stop)

    def query(self, text='starship archive', top_k=3, cap_chars=100):
        return execution.wiki_matches(self.root, self.wiki, text, top_k, cap_chars, bound=self.bound)

    def card(self, name, body):
        path = self.wiki / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path

    def status(self):
        with closing(sqlite3.connect(self.root / '.memvane/compile/wiki-retrieval/cards.sqlite')) as db:
            return json.loads(db.execute("SELECT value FROM state WHERE key='status'").fetchone()[0])

    def test_vector_query_is_utf8_bounded_without_changing_profile_or_short_query(self):
        manifest, profile = {'synthetic': True}, {'profileId': 'synthetic'}
        query_text = '한글 공포 기록🙂👻 ' * 2000
        with patch.object(provider, 'reviewed_provider') as factory, \
             patch.object(resident, 'admit_query') as admit:
            query = retrieval.query_provider(({}, {'providerManifest': manifest}))
            query(query_text, profile)
            received, received_profile = factory.return_value.call_args.args
            self.assertLessEqual(len(received.encode('utf-8')), retrieval.QUERY_VECTOR_BYTES)
            self.assertTrue(received and query_text.startswith(received))
            self.assertIs(received_profile, profile)
            query('짧은 기록🙂', profile)
            factory.return_value.assert_called_with('짧은 기록🙂', profile)
            factory.assert_called_once_with(manifest)
            admit.assert_called_once_with()

    def test_twenty_live_compact_cards_reach_extractor_payload(self):
        self.project.write_note('docs/source.md', 'starship archive source')
        for number in range(20):
            self.card('concepts/card-%02d.md' % number,
                      ('---\ntitle: "Card %d"\ncanonicalKey: card-%d\n'
                       'aliases:\n  - "Alias %d"\n'
                       'sourceRevisions:\n  - SOURCE_REVISION_ONLY\n---\n## Summary\n'
                       % (number, number, number)) + ('starship archive detail number %d. ' % number) * 500)
        config = expanded(self.project, compile={'extractor': {'builtins': ['claude']}})
        use_config(self, self.project, config)
        self.bound[0]['collections'][0]['name'] = self.project.wiki_name
        job = {'trigger': 'manual', 'engine': 'claude', 'cwd': str(self.root),
               'selectedRoute': workspace.route_identity(self.bound),
               'source': {'kind': 'file', 'path': 'docs/source.md', 'collection': self.project.raw_name}}
        payloads = []
        def extract(_argv, payload, _timeout, _root):
            payloads.append(payload)
            return {'candidates': []}, None, 0
        with patch.object(workspace, 'selected', return_value=self.bound), \
             patch.object(worker, 'selected_compiled_revision_card', return_value=None), \
             patch.object(engines, 'run_extractor', side_effect=extract):
            self.assertEqual(worker.process_job(self.root, config, config['compile'], job), (True, False, []))
        pages = payloads[0]['wiki']['similarPages']
        self.assertEqual(len(pages), 20)
        self.assertEqual(len({page['path'] for page in pages}), 20)
        for page in pages:
            identity = json.loads(page['content'].split('\n')[0])
            self.assertEqual(identity['targetPath'], page['path'])
            number = int(identity['canonicalKey'].split('-')[-1])
            self.assertEqual(identity['aliases'], ['Alias %d' % number])
            self.assertIn('starship archive detail', page['content'])
            self.assertIn('이하 생략', page['content'])
            self.assertNotIn('SOURCE_REVISION_ONLY', page['content'])
        section = engines.render_existing_context_section(payloads[0]['wiki'])
        self.assertEqual(section.count('### .memvane/wiki/concepts/card-'), 20)

    def test_cold_corpus_above_two_hundred_has_no_card_count_refusal(self):
        for i in range(240):
            self.card(f'concepts/card-{i:03}.md', f'# Garden {i}\nUnrelated flowers')
        target = self.card('concepts/last.md', '# Starship\nA starship archive')
        self.assertEqual([row[0] for row in self.query()], [target])
        self.assertEqual(self.status()['remaining'], 0)

    def test_warm_queries_read_only_bounded_candidates(self):
        for i in range(75):
            self.card(f'card-{i}.md', f'# Archive\nstarship archive {i}')
        self.query()
        with patch.object(retrieval, 'read_live', wraps=retrieval.read_live) as read:
            self.assertEqual(len(self.query()), 3)
        self.assertLessEqual(read.call_count, 2 * retrieval.CANDIDATES)
        self.assertEqual(self.status()['refreshed'], 0)

    def test_new_changed_and_deleted_cards_are_visible_before_publication(self):
        target = self.card('card.md', '# Starship\nstarship archive')
        self.assertEqual(self.query()[0][0], target)
        target.write_text('# Moon\nquasar observatory', encoding='utf-8')
        self.assertEqual(self.query(), [])
        self.assertEqual(self.query('quasar observatory')[0][0], target)
        added = self.card('new.md', '# Moon\nquasar observatory')
        self.assertEqual({row[0] for row in self.query('quasar observatory')}, {target, added})
        target.unlink()
        self.assertEqual([row[0] for row in self.query('quasar observatory')], [added])

    def test_stale_indexed_body_never_reaches_the_model(self):
        target = self.card('card.md', 'old starship archive')
        self.published = [('card.md', hashlib.sha256(target.read_bytes()).hexdigest())]
        target.write_text('unrelated garden material', encoding='utf-8')
        self.assertEqual(self.query(), [])

    def test_weak_unrelated_top_rank_is_not_a_qualified_candidate(self):
        target = self.card('card.md', 'unrelated garden material')
        self.published = [('card.md', hashlib.sha256(target.read_bytes()).hexdigest())]
        self.assertEqual(self.query(), [])

    def test_strong_vector_only_candidate_remains_eligible_for_judge(self):
        target = self.card('card.md', 'a spacecraft library')
        self.published = [('card.md', hashlib.sha256(target.read_bytes()).hexdigest(), 0.8)]
        self.assertEqual(self.query()[0][1], retrieval.RETRIEVAL_FLOOR)

    def test_frontmatter_and_boilerplate_do_not_inflate_evidence_score(self):
        target = self.card('card.md', '---\ntitle: starship archive\n---\n'
                           '> Auto-generated by MemVane from conversation/work context.\n'
                           '<!-- qmd:auto:start id="main" sourceHash="abc" -->\n'
                           '## Summary\nGarden\n<!-- qmd:auto:end -->\n')
        self.published = [('card.md', hashlib.sha256(target.read_bytes()).hexdigest())]
        self.assertEqual(self.query(), [])

    def test_twenty_is_an_upper_bound_and_four_related_cards_are_not_padded(self):
        related = [self.card(f'related-{i}.md', 'starship archive') for i in range(4)]
        unrelated = [self.card(f'unrelated-{i}.md', f'garden flowers {i}') for i in range(16)]
        self.published = [(path.name, hashlib.sha256(path.read_bytes()).hexdigest(), 0.1)
                          for path in related + unrelated]
        self.assertEqual({row[0] for row in self.query(top_k=20)}, set(related))

    def test_twenty_related_cards_can_return_and_twenty_one_is_rejected(self):
        for i in range(24):
            self.card(f'card-{i}.md', f'starship archive {i}')
        self.assertEqual(len(self.query(top_k=20)), 20)
        with self.assertRaisesRegex(ValueError, 'retrieval_budget'):
            self.query(top_k=21)

    def test_sparse_overlap_in_a_long_source_and_two_character_korean_entity_survive(self):
        target = self.card('card.md', '검마 관련 기록')
        long_source = ' '.join(f'unrelated{i}' for i in range(1000)) + ' 검마'
        self.assertEqual([row[0] for row in self.query(long_source, top_k=20)], [target])

    def test_fresh_unpublished_korean_particle_match_uses_normalized_fts(self):
        target = self.card('card.md', '검마')
        self.assertEqual([row[0] for row in self.query('검마는')], [target])

    def test_fresh_unpublished_english_stem_match_uses_porter(self):
        target = self.card('card.md', 'run')
        self.assertEqual([row[0] for row in self.query('running')], [target])

    def test_warm_legacy_overlay_rebuilds_atomically_with_porter_tokenizer(self):
        target = self.card('card.md', 'run')
        self.query('run')
        cache = self.root / '.memvane/compile/wiki-retrieval/cards.sqlite'
        with closing(sqlite3.connect(cache)) as db:
            db.execute('DROP TABLE search')
            db.execute('CREATE VIRTUAL TABLE search USING fts5(path UNINDEXED, body)')
            db.execute("INSERT INTO search VALUES ('card.md', 'run')")
            db.execute('PRAGMA user_version=0')
            db.commit()
            self.assertEqual(db.execute('SELECT count(*) FROM cards').fetchone()[0], 1)
        self.assertEqual([row[0] for row in self.query('running')], [target])
        with closing(sqlite3.connect(cache)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], retrieval.OVERLAY_SCHEMA)
            self.assertIn('porter unicode61', db.execute(
                "SELECT sql FROM sqlite_master WHERE name='search'").fetchone()[0])
        self.bound[0]['generation'] = 'g2'
        self.assertEqual([row[0] for row in self.query('running')], [target])
        with closing(sqlite3.connect(cache)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], retrieval.OVERLAY_SCHEMA)

    def test_stale_vector_relevance_cannot_qualify_edited_unrelated_content(self):
        target = self.card('card.md', 'a spacecraft library')
        self.published = [('card.md', hashlib.sha256(target.read_bytes()).hexdigest(), 0.95)]
        target.write_text('garden flowers', encoding='utf-8')
        self.assertEqual(self.query(), [])

    def test_root_meta_hidden_pending_and_symlink_ancestors_are_withheld(self):
        for name in ('SCHEMA.md', 'index.md', 'log.md', '.hidden.md', '.card.md.pending-abcd'):
            self.card(name, 'starship archive')
        target = self.card('nested/index.md', 'starship archive')
        self.assertEqual([row[0] for row in self.query()], [target])
        detached = self.root / 'detached'
        target.parent.rename(detached)
        target.parent.symlink_to(detached, target_is_directory=True)
        self.published = [('nested/index.md', hashlib.sha256(target.read_bytes()).hexdigest())]
        self.assertEqual(self.query(), [])

    def test_path_and_hash_exclusions_apply_to_overlay_before_candidate_limit(self):
        blocked = []
        for i in range(25):
            path = self.card(f'blocked-{i:02}.md', f'starship archive blocked {i}')
            blocked.append((path.name, hashlib.sha256(path.read_bytes()).hexdigest()))
        allowed = self.card('allowed.md', 'starship archive safe')
        self.exclusions = (frozenset('wiki/' + rel for rel, _ in blocked[:15]),
                           frozenset(sha for _, sha in blocked[15:]))
        self.assertEqual([row[0] for row in self.query()], [allowed])

    def test_source_exclusion_policy_invalidates_cached_admission(self):
        revision = {'kind': 'file', 'path': 'docs/build/a.md', 'collection': 'raw',
                    'sha256': 'a' * 64, 'size': 1, 'mtimeNs': 1}
        target = self.card('card.md', '---\nsourceRevisions:\n  - ' + json.dumps(revision)
                           + '\n---\nstarship archive')
        self.assertEqual(self.query()[0][0], target)
        self.bound[0]['recallSettings']['skipPaths'] = ['build']
        self.assertEqual(self.query(), [])
        self.bound[0]['recallSettings']['skipPaths'] = []
        self.bound[0]['generation'] = 'g2'
        self.assertEqual(self.query()[0][0], target)

    def test_failed_refresh_rolls_back_instead_of_publishing_partial_cache(self):
        target = self.card('card.md', 'starship archive')
        self.query()
        target.write_text('moon observatory', encoding='utf-8')
        with patch.object(retrieval, 'read_live', side_effect=RuntimeError('interrupted')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                self.query()
        self.assertEqual(self.query('moon observatory')[0][0], target)
        self.assertEqual(self.query(), [])

    def test_partial_refresh_is_observable_and_fair_cursor_makes_progress(self):
        for i in range(10):
            self.card(f'card-{i:02}.md', f'starship archive {i}')
        diagnostics = io.StringIO()
        with patch.object(retrieval, 'REFRESH_FILES', 4), redirect_stderr(diagnostics):
            self.query()
            self.assertEqual(self.status()['state'], 'deferred')
            for i in range(4):
                self.card(f'newest-{i}.md', f'starship archive recent {i}')
                self.query()
        with closing(sqlite3.connect(self.root / '.memvane/compile/wiki-retrieval/cards.sqlite')) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM cards WHERE path LIKE 'card-%'").fetchone()[0], 10)
        self.assertIn('wikiCompileRetrieval', diagnostics.getvalue())

    def test_expired_inventory_never_deletes_unseen_cached_rows(self):
        self.card('card.md', 'starship archive')
        self.query()
        with patch.object(retrieval, 'QUERY_SECONDS', 0), redirect_stderr(io.StringIO()):
            self.assertEqual(self.query(), [])
        self.assertFalse(self.status()['inventoryComplete'])
        with closing(sqlite3.connect(self.root / '.memvane/compile/wiki-retrieval/cards.sqlite')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM cards').fetchone()[0], 1)

    def test_partial_inventory_keeps_time_for_known_live_candidates(self):
        target = self.card('card.md', 'starship archive')
        self.query()
        self.published = [('card.md', hashlib.sha256(target.read_bytes()).hexdigest())]
        clock = [100.0]
        def slow_inventory(*_args):
            clock[0] += 3.1
            return {}, False, ''
        with patch.object(retrieval.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(retrieval, 'inventory', side_effect=slow_inventory), \
             redirect_stderr(io.StringIO()):
            self.assertEqual(self.query()[0][0], target)
        self.assertEqual(self.status()['state'], 'deferred')

    def test_partial_metadata_inventory_resumes_after_its_previous_window(self):
        for i in range(9):
            self.card(f'card-{i}.md', 'starship archive')
        clock = [0.0]
        original = retrieval.fingerprint
        def metadata_cost(info):
            clock[0] += 1.0
            return original(info)
        found, cursor = set(), ''
        with patch.object(retrieval.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(retrieval, 'fingerprint', side_effect=metadata_cost):
            for _ in range(3):
                clock[0] = 0.0
                with retrieval.opened_directory(self.wiki) as fd:
                    batch, _complete, cursor = retrieval.inventory(fd, 3.0, cursor)
                    found.update(batch)
        self.assertEqual(len(found), 9)

    def test_cold_queries_advance_body_cache_when_metadata_inventory_is_partial(self):
        for i in range(9):
            self.card(f'card-{i}.md', 'distant quasar' if i == 8 else f'Garden flowers {i}')
        clock = [100.0]
        inventory_active = [False]
        original_inventory = retrieval.inventory
        original_fingerprint = retrieval.fingerprint
        def metadata_cost(info):
            if inventory_active[0]:
                clock[0] += 1.0
            return original_fingerprint(info)
        def slow_inventory(*args):
            inventory_active[0] = True
            try:
                return original_inventory(*args)
            finally:
                inventory_active[0] = False
        previous_count = 0
        with patch.object(retrieval.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(retrieval, 'inventory', side_effect=slow_inventory), \
             patch.object(retrieval, 'fingerprint', side_effect=metadata_cost), \
             redirect_stderr(io.StringIO()):
            for _ in range(9):
                rows = self.query('distant quasar')
                with closing(sqlite3.connect(self.root / '.memvane/compile/wiki-retrieval/cards.sqlite')) as db:
                    count = db.execute('SELECT count(*) FROM cards').fetchone()[0]
                self.assertGreater(count, previous_count,
                                   'partial metadata passes must leave time to index their bodies')
                previous_count = count
        self.assertEqual(previous_count, 9)
        self.assertEqual([row[0].name for row in rows], ['card-8.md'])

    def test_oversize_card_skip_is_observable(self):
        self.card('card.md', 'starship archive ' * 20)
        diagnostics = io.StringIO()
        with patch.object(retrieval, 'MAX_CARD_BYTES', 32), redirect_stderr(diagnostics):
            self.assertEqual(self.query(), [])
        self.assertEqual(self.status()['oversize'], 1)
        self.assertIn('"oversize": 1', diagnostics.getvalue())

    def test_changed_route_proof_rejects_before_cache_creation(self):
        with patch.object(execution, 'recheck', side_effect=ValueError('proof_changed')):
            with self.assertRaisesRegex(ValueError, 'proof_changed'):
                self.query()
        self.assertFalse((self.root / '.memvane/compile/wiki-retrieval').exists())

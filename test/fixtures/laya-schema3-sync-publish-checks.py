#!/usr/bin/env python3
"""U12c: a selected schema3 install's sync pass publishes view-store generations.

Seeded from the U12b schema3 writer-plan fixture: the v5 install, v6 index,
journal images, Git checkout and view store are actual; the schema3 record is
the U10 seal, selected with ``mutate_image`` (no loaded-bridge CAS). The pass
runs the actual ``laya_product_sync_worker.run``; recall reads through the
actual persisted schema3 ``query_endpoint``. No model or provider starts.
"""
from contextlib import ExitStack, closing, contextmanager
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('schema3_sync_seed', ROOT / 'test/fixtures/laya-writer-schema3-plan-checks.py')
seed_module = importlib.util.module_from_spec(spec); spec.loader.exec_module(seed_module)
spec = importlib.util.spec_from_file_location('sqlite_hot_journal', ROOT / 'test/fixtures/sqlite_hot_journal.py')
hot = importlib.util.module_from_spec(spec); spec.loader.exec_module(hot)


def emit(label, **values):
    print(json.dumps(dict(case=label, **values), sort_keys=True, default=str), flush=True)


class Schema3SyncPublishChecks(seed_module.Schema3WriterPlanChecks):
    def setUp(self):
        super().setUp()
        self.worker = importlib.import_module('memvane.sync.product_sync_worker')
        self.views = importlib.import_module('memvane.store.view_store')
        self.lease = importlib.import_module('memvane.journal.reader_lease')
        self.dispatch = importlib.import_module('memvane.recall.reader_dispatch')
        root = mock.patch.object(self.worker, '_writer_executing_root', return_value=self.case.plugin)
        root.start(); self.addCleanup(root.stop)
        # warm() writes a one-time v6 sentinel; hold it once for the whole check.
        warm = self.F.warm(); warm.__enter__()
        self.addCleanup(warm.__exit__, None, None, None)
        self.wiki = self.case.auto / 'wiki'
        self.decision = self.J.select_route(str(self.case.project), action='update')
        self.raw_collection = next(c['name'] for c in self.decision['collections'] if c['role'] == 'raw')

    # -- helpers --------------------------------------------------------------
    def card(self, relative, token, source_dir='notes'):
        """A card that passes the reader's verified-frontmatter rule (trusted)."""
        from memvane.wiki.card import yaml_scalars
        raw = ('Synthetic source for ' + token + '.\n').encode('utf8')
        source = self.case.project / source_dir / (token + '.md')
        source.parent.mkdir(parents=True, exist_ok=True); source.write_bytes(raw)
        revision = dict(kind='file', path=source_dir + '/' + token + '.md', collection=self.raw_collection,
                        sha256=hashlib.sha256(raw).hexdigest(), size=len(raw),
                        mtimeNs=source.stat().st_mtime_ns)
        line = yaml_scalars.dump_source_revisions([revision])[0]
        front = (f'title: "{token} card"\nstatus: verified\ncreatedBy: memvane\n'
                 f'sourceRevisions:\n  - {line}\n')
        text = '---\n' + front + '---\n' + f'# {token} card\n{token} verified body\n'
        text = text.replace('createdBy: memvane\n', 'createdBy: memvane\nverifiedBodyHash: '
                            + yaml_scalars.card_body_hash(text) + '\n', 1)
        path = self.wiki / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf8')

    def head(self):
        with closing(sqlite3.connect(self.store_path.as_uri() + '?mode=ro', uri=True)) as db:
            rows = db.execute('SELECT gen_id,epoch FROM view_heads').fetchall()
        self.assertEqual(len(rows), 1)
        return rows[0]

    def members(self, gen_id):
        with closing(sqlite3.connect(self.store_path.as_uri() + '?mode=ro', uri=True)) as db:
            return {path: (blob, eligible) for path, blob, eligible in db.execute(
                'SELECT path,blob_sha,eligible FROM wiki_members WHERE gen_id=?', (gen_id,))}

    def card_sources(self, gen_id, path):
        with closing(sqlite3.connect(self.store_path.as_uri() + '?mode=ro', uri=True)) as db:
            return db.execute('SELECT count(*) FROM card_sources WHERE gen_id=? AND wiki_path=?',
                              (gen_id, path)).fetchone()[0]

    def view_heads(self):
        with closing(sqlite3.connect(self.store_path.as_uri() + '?mode=ro', uri=True)) as db:
            return sorted(db.execute('SELECT view_id,gen_id,epoch FROM view_heads').fetchall())

    def run_pass(self):
        """One sync pass; returns (result, ingest_attested_card call count)."""
        original = self.views.ViewStore.ingest_attested_card
        calls = []
        def counted(store, *args, **kwargs):
            calls.append(args[1] if len(args) > 1 else kwargs.get('path'))
            return original(store, *args, **kwargs)
        def no_service(*_args, **_kwargs):
            raise AssertionError('a schema3 lexical pass started a provider service')
        with self.seed.authority(), \
                mock.patch.object(self.views.ViewStore, 'ingest_attested_card', new=counted):
            result = self.worker.run(self.case.project, start_service=no_service)
        return result, calls

    def recall(self, prompt):
        with self.seed.authority(), self.J.read_only_journals():
            decision = self.J.select_route(str(self.case.project), action='recall')
            with self.lease.resolve_endpoint(self.case.project, action='recall',
                                             host_root=self.case.plugin) as endpoint:
                self.assertEqual(endpoint.record['store_format'], 'schema3')
                # U11: the persisted schema3 reader binds to the reader's own decision.
                decision = self.R._recheck(endpoint)[2]['readerDecision']
                return self.dispatch.query_endpoint(endpoint, checkout=self.case.project, prompt=prompt,
                                                    query_vector=None, deadline=time.monotonic() + 20,
                                                    decision=decision)

    def store_paths(self, query):
        """Read the store's current main-view head directly (no reader endpoint)."""
        store = self.views.ViewStore.open_reader(self.store_path, repository=self.repository,
                                                 scope_id=self.case.route['scopeId'])
        try:
            pin = store.pin_view(store.capture_view(self.worktree['worktreeId']))
            return pin.gen_id, [row['path'] for row in store.search(pin, query, 10)]
        finally:
            store.close()

    def paths(self, hit):
        """Member paths of recall rows (rows carry ``<wiki collection>/<member path>``)."""
        wiki = next(c['name'] for c in self.decision['collections'] if c['role'] == 'wiki')
        self.assertTrue(all(row['path'].startswith(wiki + '/') for row in hit.rows))
        return [row['path'][len(wiki) + 1:] for row in hit.rows]

    def generation_sources(self, gen_id):
        """``{path: (presence, fingerprint path)}`` of one generation's sealed manifest."""
        with closing(sqlite3.connect(self.store_path.as_uri() + '?mode=ro', uri=True)) as db:
            stage = db.execute('SELECT s.stage_json FROM generations g JOIN generation_slots s '
                               'ON s.ordinal=g.ordinal WHERE g.gen_id=?', (gen_id,)).fetchone()[0]
            result = {}
            for source in json.loads(stage)['manifest']['sources']:
                path = db.execute('SELECT path FROM source_fingerprints WHERE fingerprint_id=?',
                                  (source['fingerprint_id'],)).fetchone()[0]
                result[source['path']] = (source['presence'], path)
            return result

    def attested_source_paths(self, gen_id, card):
        """Fingerprint paths of the attestation a generation member's trust digest names."""
        store = self.views.ViewStore.open_reader(self.store_path, repository=self.repository,
                                                 scope_id=self.case.route['scopeId'])
        try:
            with closing(sqlite3.connect(self.store_path.as_uri() + '?mode=ro', uri=True)) as db:
                trust = db.execute('SELECT trust_digest FROM wiki_members WHERE gen_id=? AND path=?',
                                   (gen_id, card)).fetchone()[0]
            self.assertIsNotNone(trust)
            evidence = store.attestation(trust)
            return sorted(store.fingerprint(item['fingerprint_id'])['path'] for item in evidence['sources'])
        finally:
            store.close()

    def member_trust(self, gen_id, card):
        with closing(sqlite3.connect(self.store_path.as_uri() + '?mode=ro', uri=True)) as db:
            return db.execute('SELECT trust_digest,eligible FROM wiki_members WHERE gen_id=? AND path=?',
                              (gen_id, card)).fetchone()

    def attested_revisions(self, gen_id, card):
        """``[(path, source_sha, byte_length)]`` of the attestation a member's trust digest names."""
        trust = self.member_trust(gen_id, card)[0]
        self.assertIsNotNone(trust)
        store = self.views.ViewStore.open_reader(self.store_path, repository=self.repository,
                                                 scope_id=self.case.route['scopeId'])
        try:
            return sorted(tuple(store.fingerprint(item['fingerprint_id'])[k]
                                for k in ('path', 'source_sha', 'byte_length'))
                          for item in store.attestation(trust)['sources'])
        finally:
            store.close()

    def dual_card(self, relative, token, *, current):
        """A verified card citing ``notes/<token>.md`` at an older and a compiled revision.

        The source file then holds ``current``: the compiled bytes, or other
        bytes that match neither revision (a stale card).
        """
        from memvane.wiki.card import yaml_scalars
        older = ('Older source for ' + token + '.\n').encode('utf8')
        compiled = ('Synthetic source for ' + token + '.\n').encode('utf8')
        source = self.case.project / 'notes' / (token + '.md')
        source.parent.mkdir(parents=True, exist_ok=True)
        lines = yaml_scalars.dump_source_revisions([
            dict(kind='file', path='notes/' + token + '.md', collection=self.raw_collection,
                 sha256=hashlib.sha256(data).hexdigest(), size=len(data), mtimeNs=1) for data in (older, compiled)])
        front = (f'title: "{token} card"\nstatus: verified\ncreatedBy: memvane\nsourceRevisions:\n'
                 + ''.join('  - ' + line + '\n' for line in lines))
        text = '---\n' + front + '---\n' + f'# {token} card\n{token} verified body\n'
        text = text.replace('createdBy: memvane\n', 'createdBy: memvane\nverifiedBodyHash: '
                            + yaml_scalars.card_body_hash(text) + '\n', 1)
        path = self.wiki / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf8')
        source.write_bytes(compiled if current == 'compiled' else current)
        return 'notes/' + token + '.md', compiled

    def compile_files(self):
        root = self.case.auto / 'compile'
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob('*')) if p.is_file()} \
            if root.is_dir() else {}

    # -- checks ---------------------------------------------------------------
    def test_10_dot_raw_root_sources_are_observed_and_published(self):
        """A raw collection rooted at a dot directory (legacy ``collectionPaths``
        such as ``.nova/06_Sessions``) is observed like any other raw root.

        Minimal settings always expand the raw root to the project, so the
        route's raw ``sourceRoot`` is substituted in the decision the pass
        hands to the raw observation; everything after it is the real pass.
        """
        self.select_schema3()
        dot_root = self.case.project / '.nova' / '06_Sessions'
        dot_root.mkdir(parents=True)
        (dot_root / 'ep01.md').write_bytes(b'Session one notes.\n')
        self.card('concepts/dotroot-card.md', 'dotrootprobe')
        real = self.worker._schema3_sources

        def dot_raw(decision, *args, **kwargs):
            collections = [dict(item, sourceRoot=str(dot_root)) if item.get('role') == 'raw' else item
                           for item in decision['collections']]
            return real(dict(decision, collections=collections), *args, **kwargs)

        with mock.patch.object(self.worker, '_schema3_sources', new=dot_raw):
            first, _calls = self.run_pass()
            self.assertEqual(first['state'], 'indexed', first)
            self.assertEqual(self.generation_sources(first['generation']),
                             {'.nova/06_Sessions/ep01.md': ('present', '.nova/06_Sessions/ep01.md')})
            idle, idle_calls = self.run_pass()
        self.assertEqual((idle['state'], idle_calls), ('unchanged', []), idle)
        self.assertEqual(self.head()[0], first['generation'])
        emit('schema3-dot-raw-root', sources=first['sources'])

    # Writer ingest judges one path cited at two revisions by this pass's raw observation
    # (user decision 11/13 08:45); the observation over its bounds keeps both.

    def test_13_dual_revision_card_keeps_the_revision_this_pass_observed(self):
        self.select_schema3()
        path, compiled = self.dual_card('concepts/dual-current.md', 'dualcurrentprobe', current='compiled')
        result, calls = self.run_pass()
        self.assertEqual(result['state'], 'indexed', result)
        self.assertIn('concepts/dual-current.md', calls)
        self.assertEqual(self.attested_revisions(result['generation'], 'concepts/dual-current.md'),
                         [(path, hashlib.sha256(compiled).hexdigest(), len(compiled))])
        self.assertEqual(self.member_trust(result['generation'], 'concepts/dual-current.md')[1], 1)
        self.assertIn('concepts/dual-current.md', self.paths(self.recall('dualcurrentprobe')))
        emit('schema3-dual-revision-current', kept=1)

    def test_14_dual_revision_card_matching_no_observed_revision_is_untrusted(self):
        self.select_schema3()
        self.card('concepts/plain-neighbor.md', 'plainneighborprobe')
        self.dual_card('concepts/dual-stale.md', 'dualstaleprobe', current=b'Third bytes.\n')
        queued = self.compile_files()
        result, _calls = self.run_pass()
        self.assertEqual(result['state'], 'indexed', result)
        self.assertEqual(self.member_trust(result['generation'], 'concepts/dual-stale.md'), (None, 0))
        self.assertEqual(self.member_trust(result['generation'], 'concepts/plain-neighbor.md')[1], 1)
        self.assertNotIn('concepts/dual-stale.md', self.paths(self.recall('dualstaleprobe')))
        self.assertEqual(self.compile_files(), queued, 'the writer queues no regeneration')
        emit('schema3-dual-revision-stale', trusted=False)

    def test_15_observation_over_its_bounds_keeps_both_revisions(self):
        """Regression guard: no current observation picks no winner."""
        from memvane.wiki.intake import wiki_discovery as laya_wiki_discovery
        self.select_schema3()
        self.dual_card('concepts/dual-unbounded.md', 'dualunboundedprobe', current=b'Third bytes.\n')
        with mock.patch.object(laya_wiki_discovery, 'RAW_SCAN_MAX_FILES', 1):
            result, _calls = self.run_pass()
        self.assertEqual(result['state'], 'indexed', result)
        self.assertEqual(len(self.attested_revisions(result['generation'], 'concepts/dual-unbounded.md')), 2)
        self.assertEqual(self.member_trust(result['generation'], 'concepts/dual-unbounded.md')[1], 1)
        emit('schema3-dual-revision-unbounded', kept=2)

    def test_16_published_dual_revision_card_is_judged_only_when_its_bytes_change(self):
        """Regression guard: unchanged card bytes keep their trust; the lifecycle regenerates."""
        from memvane.wiki.intake import wiki_discovery as laya_wiki_discovery
        self.select_schema3()
        path, _compiled = self.dual_card('concepts/dual-kept.md', 'dualkeptprobe', current=b'Third bytes.\n')
        with mock.patch.object(laya_wiki_discovery, 'RAW_SCAN_MAX_FILES', 1):
            first, _calls = self.run_pass()
        trust = self.member_trust(first['generation'], 'concepts/dual-kept.md')
        (self.case.project / path).write_bytes(b'Fourth bytes.\n')
        second, calls = self.run_pass()
        self.assertNotIn('concepts/dual-kept.md', calls)
        generation = second.get('generation') if second['state'] == 'indexed' else first['generation']
        self.assertEqual(self.member_trust(generation, 'concepts/dual-kept.md'), trust)
        emit('schema3-dual-revision-kept', state=second['state'])

    def test_11_card_citing_a_dot_source_keeps_its_reader_trust(self):
        """v6 trusts a verified card whose ``sourceRevisions`` path starts with a
        dot directory; the schema3 ingest records the same legacy attestation."""
        self.select_schema3()
        self.card('concepts/dot-source.md', 'dotsourceprobe', source_dir='.nova/06_Sessions')
        result, calls = self.run_pass()
        self.assertEqual(result['state'], 'indexed', result)
        self.assertIn('concepts/dot-source.md', calls)
        self.assertEqual(self.attested_source_paths(result['generation'], 'concepts/dot-source.md'),
                         ['.nova/06_Sessions/dotsourceprobe.md'])
        self.assertGreaterEqual(self.card_sources(result['generation'], 'concepts/dot-source.md'), 1)
        self.assertIn('concepts/dot-source.md', self.paths(self.recall('dotsourceprobe')))
        emit('schema3-dot-source-card', trusted=True)

    def test_01_pass_publishes_edited_card_and_persisted_recall_reads_it(self):
        self.select_schema3()
        self.card('concepts/kestrel-perch.md', 'kestrelprobe')
        start = self.head()
        files = self.v6_files()
        journal_before = self.J.inspect_journal(self.case.journal)
        first, first_calls = self.run_pass()
        self.assertEqual(first['state'], 'indexed', first)
        self.assertEqual(first['storeFormat'], 'schema3')
        published = self.head()
        self.assertEqual(published, (first['generation'], start[1] + 1))
        members = self.members(published[0])
        self.assertEqual(sorted(first_calls), sorted(members))
        self.assertEqual(members['concepts/kestrel-perch.md'][1], 1)
        # D3: a trusted card's source spans are the generation's location rows.
        self.assertGreaterEqual(self.card_sources(published[0], 'concepts/kestrel-perch.md'), 1)
        self.assertEqual(self.J.inspect_journal(self.case.journal)['epoch'], journal_before['epoch'] + 1)
        self.assertEqual(self.v6_files(), files)
        hit = self.recall('kestrelprobe')
        self.assertEqual((hit.store_format, hit.data_generation), ('schema3', published[0]))
        self.assertIn('concepts/kestrel-perch.md', self.paths(hit))

        # An unchanged pass writes nothing, not even the journal fence.
        idle, idle_calls = self.run_pass()
        self.assertEqual((idle['state'], idle_calls, self.head()), ('unchanged', [], published))
        self.assertEqual(self.J.inspect_journal(self.case.journal)['epoch'], journal_before['epoch'] + 1)

        # Editing one card ingests exactly that card; recall reads the new bytes.
        self.card('concepts/kestrel-perch.md', 'falconprobe')
        second, second_calls = self.run_pass()
        self.assertEqual(second['state'], 'indexed', second)
        self.assertEqual(second_calls, ['concepts/kestrel-perch.md'])
        self.assertEqual(self.head(), (second['generation'], published[1] + 1))
        after = self.members(second['generation'])
        self.assertEqual({p: m for p, m in after.items() if p != 'concepts/kestrel-perch.md'},
                         {p: m for p, m in members.items() if p != 'concepts/kestrel-perch.md'})
        hit = self.recall('falconprobe')
        self.assertEqual(hit.data_generation, second['generation'])
        self.assertIn('concepts/kestrel-perch.md', self.paths(hit))
        self.assertNotIn('concepts/kestrel-perch.md',
                         self.paths(self.recall('kestrelprobe')))
        self.assertEqual(self.v6_files(), files)
        emit('schema3-sync-publish', firstIngests=len(first_calls), editIngests=len(second_calls),
             epochs=[start[1], published[1], self.head()[1]], v6BytesUnchanged=True)

    def test_02_settings_bytes_and_skip_paths_edits_publish_and_recall_reads(self):
        self.select_schema3()
        settings = importlib.import_module('memvane.settings.project_dir').settings_path(self.case.project)
        original = settings.read_bytes()
        start = self.head()
        self.card('concepts/heron-watch.md', 'heronwatchprobe')
        # Same settings identity, different bytes: v6 keeps serving, so does schema3.
        settings.write_bytes(original.rstrip(b'\n') + b'\n\n')
        spaced, calls = self.run_pass()
        self.assertEqual(spaced['state'], 'indexed', spaced)
        self.assertIn('concepts/heron-watch.md', calls)
        self.assertEqual(self.head(), (spaced['generation'], start[1] + 1))
        self.assertIn('concepts/heron-watch.md', self.paths(self.recall('heronwatchprobe')))
        # A skipPaths edit only changes which cards the next generation holds:
        # newly skipped cards leave without ingest (not a mass removal) ...
        concepts = sorted(p for p in self.members(spaced['generation']) if p.startswith('concepts/'))
        self.assertGreaterEqual(len(concepts), 3)
        value = json.loads(original)
        settings.write_text(json.dumps(dict(value, skipPaths=['concepts'])), encoding='utf8')
        # Until the owner re-seals, the selected reader refuses with one fixed
        # reason: its record binds the recallSettings digest (skipPaths included).
        self.assert_stale_reader()
        skipped, calls = self.run_pass()
        self.assertEqual((skipped['state'], calls, skipped.get('readerResettle')),
                         ('indexed', [], 'resealed'), skipped)
        self.assertEqual(self.head(), (skipped['generation'], start[1] + 2))
        self.assertEqual([p for p in self.members(skipped['generation']) if p.startswith('concepts/')], [])
        self.assertEqual(self.store_paths('heronwatchprobe'), (skipped['generation'], []))
        hit = self.recall('heronwatchprobe')
        self.assertEqual(hit.data_generation, skipped['generation'])
        self.assertNotIn('concepts/heron-watch.md', self.paths(hit))
        idle, calls = self.run_pass()
        self.assertEqual((idle['state'], calls, idle.get('readerResettle')), ('unchanged', [], None), idle)
        # ... and cards skipPaths no longer covers come back like new cards.
        settings.write_bytes(original)
        self.assert_stale_reader()
        restored, calls = self.run_pass()
        self.assertEqual((restored['state'], sorted(calls), restored.get('readerResettle')),
                         ('indexed', concepts, 'resealed'), restored)
        self.assertEqual(self.members(restored['generation'])['concepts/heron-watch.md'][1], 1)
        self.assertIn('concepts/heron-watch.md', self.paths(self.recall('heronwatchprobe')))
        emit('schema3-sync-settings-edits', spacedPublishes=True, skipRepublishes=True, resealed=2,
             epochs=[start[1], self.head()[1]])

    def hook_recall(self, prompt):
        """One selected-route recall through the host response (as a registered hook would).

        Returns ``(context, recall event, sync wakes, stderr)``; a refusal raises.
        """
        import io
        from contextlib import redirect_stderr
        host = importlib.import_module('memvane.host.host_response')
        runtime_work = importlib.import_module('memvane.runtime.runtime_work')
        event_log = importlib.import_module('event_log')
        decision = self.J.select_route(str(self.case.project), action='recall')
        payload = {'cwd': str(self.case.project), 'prompt': prompt, 'session_id': 'u15c-settings',
                   'hook_event_name': 'UserPromptSubmit'}
        wakes, events, stderr = [], [], io.StringIO()
        with self.seed.authority(), redirect_stderr(stderr), \
                mock.patch.object(host, '__file__', str(self.case.plugin / 'core' / 'laya_host_response.py')), \
                mock.patch.object(runtime_work, 'request',
                                  side_effect=lambda kind, target='', **kw: wakes.append((kind, target))), \
                mock.patch.object(event_log, 'emit',
                                  side_effect=lambda event, **fields: events.append(dict(fields, event=event))):
            answer = host.serve_selected('recall', 'claude', decision, payload)
        recall = [event for event in events if event['event'] == 'recall']
        self.assertEqual(len(recall), 1, events)
        return answer['hookSpecificOutput']['additionalContext'], recall[0], wakes, stderr.getvalue()

    def test_09_recall_settings_edit_answers_empty_until_the_owner_reseals(self):
        self.select_schema3()
        settings = importlib.import_module('memvane.settings.project_dir').settings_path(self.case.project)
        original = settings.read_bytes()
        self.card('concepts/heron-watch.md', 'heronwatchprobe')
        first, _calls = self.run_pass()
        self.assertEqual(first['state'], 'indexed', first)
        settings.write_text(json.dumps(dict(json.loads(original), skipPaths=['concepts'])), encoding='utf8')
        # The persisted reader itself still refuses (the proof boundary is unchanged) ...
        self.assert_stale_reader()
        # ... but the recall hook answers without context, asks for the owner's
        # sync and leaves one fixed event reason, not a refusal.
        context, event, wakes, stderr = self.hook_recall('heronwatchprobe')
        self.assertEqual(context, '')
        self.assertEqual((event.get('outcome'), event.get('reason')), ('empty', 'reader_settings_resealing'), event)
        self.assertIn('laya_reader_settings_resealing', event.get('notes') or [])
        self.assertEqual(wakes, [('laya-sync', str(self.case.project))])
        self.assertNotIn('request refused', stderr)
        # Any other record difference stays a refusal.
        real = self.R._schema3_evidence

        def other(*args, **kwargs):
            record, raw, proof = real(*args, **kwargs)
            return dict(record, profile=record['profile'] + '-other'), raw, proof
        with mock.patch.object(self.R, '_schema3_evidence', side_effect=other):
            with self.assertRaisesRegex(ValueError, 'reader_endpoint_changed'):
                self.hook_recall('heronwatchprobe')
        # The next owner pass re-seals; the recall reads the new generation again.
        resealed, _calls = self.run_pass()
        self.assertEqual(resealed.get('readerResettle'), 'resealed', resealed)
        hit = self.recall('heronwatchprobe')
        self.assertEqual(hit.data_generation, resealed['generation'])
        # The hook answers from the re-sealed reader (the skipped card is gone,
        # so this prompt finds no row; the answer is not the pending one).
        context, event, wakes, stderr = self.hook_recall('heronwatchprobe')
        self.assertEqual(event.get('store_format'), 'schema3', event)
        self.assertIn(event.get('outcome'), ('served', 'empty'), event)
        self.assertNotEqual(event.get('reason'), 'reader_settings_resealing', event)
        self.assertNotIn('laya_reader_settings_resealing', event.get('notes') or [])
        emit('schema3-recall-settings-resealing', pending=True, after=[event.get('outcome'), event.get('reason')])

    def assert_stale_reader(self):
        import traceback
        try:
            self.recall('heronwatchprobe')
        except Exception as exc:
            self.assertEqual(str(exc), 'reader_endpoint_changed', ''.join(traceback.format_exc()[-6000:]))
        else:
            self.fail('stale reader served')

    def test_07_identity_edit_v6_also_refuses_stays_fixed_reason(self):
        self.select_schema3()
        settings = importlib.import_module('memvane.settings.project_dir').settings_path(self.case.project)
        original = settings.read_bytes()
        start = self.head()
        epoch = self.J.inspect_journal(self.case.journal)['epoch']
        self.card('concepts/heron-watch.md', 'heronwatchprobe')
        store = self.views.ViewStore.open(self.store_path, repository=self.repository,
                                          scope_id=self.case.route['scopeId'])
        self.addCleanup(store.close)
        value = json.loads(original)
        # Collection name (and so the v6 settings identity) changes.
        settings.write_text(json.dumps(dict(value, name=value['name'] + '-renamed')), encoding='utf8')
        try:
            with self.assertRaises(importlib.import_module('memvane.store.blob_values').BlobStoreError) as caught:
                store.capture_view(self.worktree['worktreeId'])
            self.assertEqual(caught.exception.reason, 'wiki_binding_changed')
            try:
                held, calls = self.run_pass()
            except Exception as exc:  # the route itself refuses before the store
                held, calls = {'state': 'raised', 'reason': type(exc).__name__ + ':' + str(exc)}, []
        finally:
            settings.write_bytes(original)
        emit('schema3-sync-identity-observed', held=held)
        self.assertNotIn(held['state'], ('indexed', 'pending', 'unchanged'), held)
        self.assertEqual(calls, [])
        self.assertEqual(self.head(), start)
        self.assertEqual(self.J.inspect_journal(self.case.journal)['epoch'], epoch)
        emit('schema3-sync-identity-held', state=held['state'], reason=held.get('reason'))

    def test_08_cold_view_publishes_only_complete_and_recall_is_preparing(self):
        import subprocess
        self.select_schema3()
        self.card('concepts/kestrel-perch.md', 'kestrelprobe')
        self.card('concepts/wren-path.md', 'wrenprobe')
        warm, _calls = self.run_pass()
        self.assertEqual(warm['state'], 'indexed', warm)
        cards = sorted(self.members(warm['generation']))
        self.assertGreaterEqual(len(cards), 5)
        # A new branch is a new view with no published generation (D9).
        subprocess.run(['git', '-C', str(self.case.project), 'checkout', '-q', '-b', 'cold-side'],
                       check=True, capture_output=True, timeout=10)
        time.sleep(1.1)  # Git racy-index window before the checkout capture.
        views_before = self.view_heads()
        everything, passes = [], []
        with mock.patch.object(self.worker, 'SCHEMA3_MAX_INGEST', 2):
            while True:
                result, calls = self.run_pass()
                passes.append(result); everything.extend(calls)
                if result['state'] != 'pending':
                    break
                self.assertEqual(result['reason'], 'schema3_view_preparing', result)
                self.assertEqual(self.view_heads(), views_before)
                with self.assertRaises(importlib.import_module('memvane.store.blob_values').BlobStoreError) as caught:
                    self.recall('kestrelprobe')
                self.assertEqual(caught.exception.reason, 'view_preparing')
                self.assertLess(len(passes), len(cards))
        self.assertEqual(result['state'], 'indexed', result)
        self.assertEqual(len(passes), -(-len(cards) // 2))
        self.assertEqual(sorted(everything), cards)
        self.assertEqual(len(self.view_heads()), len(views_before) + 1)
        self.assertEqual(sorted(self.members(result['generation'])), cards)
        hit = self.recall('kestrelprobe')
        self.assertEqual(hit.data_generation, result['generation'])
        self.assertIn('concepts/kestrel-perch.md', self.paths(hit))
        emit('schema3-sync-cold-view', passes=len(passes), cards=len(cards), ingests=len(everything))

    def test_03_interrupted_publish_keeps_old_head_and_next_pass_resumes(self):
        self.select_schema3()
        self.card('concepts/wren-path.md', 'wrenprobe')
        start = self.head()
        original = self.views.ViewStore.publish_generation
        def before(store, *args, **kwargs):
            raise RuntimeError('injected stop before publish')
        with mock.patch.object(self.views.ViewStore, 'publish_generation', new=before):
            with self.assertRaisesRegex(RuntimeError, 'injected stop before publish'):
                self.run_pass()
        self.assertEqual(self.head(), start)
        resumed, _calls = self.run_pass()
        self.assertEqual(resumed['state'], 'indexed', resumed)
        self.assertEqual(self.head(), (resumed['generation'], start[1] + 1))

        self.card('concepts/wren-path.md', 'robinprobe')
        published = []
        def after(store, handle, gen_id, **kwargs):
            published.append(original(store, handle, gen_id, **kwargs))
            raise RuntimeError('injected stop after publish')
        with mock.patch.object(self.views.ViewStore, 'publish_generation', new=after):
            with self.assertRaisesRegex(RuntimeError, 'injected stop after publish'):
                self.run_pass()
        moved = self.head()
        self.assertEqual(moved[1], start[1] + 2)
        replay, calls = self.run_pass()
        self.assertEqual((replay['state'], calls, self.head()), ('unchanged', [], moved))
        self.assertIn('concepts/wren-path.md', self.paths(self.recall('robinprobe')))
        emit('schema3-sync-interrupted-publish', beforeKeepsHead=True, afterIsUnchangedReplay=True)

    def test_05_ingest_cap_publishes_progress_as_pending_then_completes(self):
        self.select_schema3()
        self.card('concepts/kestrel-perch.md', 'kestrelprobe')
        start = self.head()
        with mock.patch.object(self.worker, 'SCHEMA3_MAX_INGEST', 2):
            first, first_calls = self.run_pass()
            self.assertEqual((first['state'], first['ingested'], first['pending'], len(first_calls)),
                             ('pending', 2, 2, 2), first)
            self.assertEqual(self.head(), (first['generation'], start[1] + 1))
            self.assertEqual(sorted(self.members(first['generation'])), sorted(first_calls))
            second, second_calls = self.run_pass()
        self.assertEqual((second['state'], second['ingested'], second['pending']), ('indexed', 2, 0), second)
        self.assertEqual(set(first_calls) & set(second_calls), set())
        self.assertEqual(self.head(), (second['generation'], start[1] + 2))
        self.assertEqual(sorted(self.members(second['generation'])), sorted(first_calls + second_calls))
        emit('schema3-sync-ingest-cap', firstPass=first_calls, secondPass=second_calls)

    def test_ingest_pending_detail_requires_committed_progress(self):
        import laya_runtime_dispatch as dispatch
        self.select_schema3()
        self.card('concepts/kestrel-perch.md', 'kestrelprobe')
        with mock.patch.object(self.worker, 'SCHEMA3_MAX_INGEST', 2):
            first, calls = self.run_pass()
        self.assertGreater(len(calls), 0)
        self.assertEqual(first['state'], 'pending')
        self.assertEqual(dispatch._deferred_detail(first),
                         'deferred:pending:schema3_progress_ingest')

    def test_09_modified_cards_over_ingest_cap_converge_without_partial_publish(self):
        """Every published card changes and the change set exceeds one pass's ingest cap."""
        self.select_schema3()
        self.card('concepts/kestrel-perch.md', 'kestrelprobe')
        self.card('concepts/wren-path.md', 'wrenprobe')
        full, _calls = self.run_pass()
        self.assertEqual(full['state'], 'indexed', full)
        before = self.members(full['generation'])
        cards = sorted(before)
        self.assertGreaterEqual(len(cards), 5)
        start = self.head()
        for index, path in enumerate(cards):
            with open(self.wiki / path, 'a', encoding='utf8') as handle:
                handle.write(f'\nedited line {index} heronprobe\n')
        passes, everything = [], []
        with mock.patch.object(self.worker, 'SCHEMA3_MAX_INGEST', 2):
            for _ in range(len(cards) + 2):
                result, calls = self.run_pass()
                passes.append(dict(state=result['state'], reason=result.get('reason'),
                                   ingested=result.get('ingested'), pending=result.get('pending'),
                                   calls=len(calls)))
                everything.extend(calls)
                if result['state'] != 'pending':
                    break
                # Recall keeps the last published generation while the batch accumulates.
                self.assertEqual(self.head(), start, passes)
                self.assertIn('concepts/kestrel-perch.md', self.paths(self.recall('kestrelprobe')))
        emit('schema3-sync-modified-over-cap', cards=len(cards), passes=passes)
        self.assertEqual(result['state'], 'indexed', passes)
        self.assertEqual(len(passes), -(-len(cards) // 2), passes)
        self.assertEqual(sorted(everything), cards, passes)
        self.assertEqual(self.head(), (result['generation'], start[1] + 1))
        after = self.members(result['generation'])
        self.assertEqual(sorted(after), cards)
        for path in cards:
            current = hashlib.sha256((self.wiki / path).read_bytes()).hexdigest()
            self.assertEqual(after[path][0], current, path)
            self.assertNotEqual(after[path][0], before[path][0], path)
        hit = self.recall('heronprobe')
        self.assertEqual(hit.data_generation, result['generation'])

    def test_06_removed_card_leaves_generation_and_mass_removal_is_held(self):
        self.select_schema3()
        self.card('concepts/kestrel-perch.md', 'kestrelprobe')
        self.card('concepts/wren-path.md', 'wrenprobe')
        full, _calls = self.run_pass()
        self.assertEqual(full['state'], 'indexed', full)
        members = sorted(self.members(full['generation']))
        self.assertGreaterEqual(len(members), 5)
        (self.wiki / 'concepts/kestrel-perch.md').unlink()
        one, calls = self.run_pass()
        self.assertEqual((one['state'], one['removed'], calls), ('indexed', 1, []), one)
        self.assertEqual(sorted(self.members(one['generation'])),
                         [p for p in members if p != 'concepts/kestrel-perch.md'])
        held_head = self.head()
        epoch = self.J.inspect_journal(self.case.journal)['epoch']
        for path in members:
            if path != 'concepts/kestrel-perch.md':
                (self.wiki / path).unlink()
        held, calls = self.run_pass()
        self.assertEqual((held['state'], held['reason'], held['removed'], calls),
                         ('deferred', 'schema3_mass_removal_held', len(members) - 1, []), held)
        self.assertEqual(self.head(), held_head)
        self.assertEqual(self.J.inspect_journal(self.case.journal)['epoch'], epoch)
        emit('schema3-sync-removal', removedOne=1, heldRemoval=held['removed'], headUnchanged=True)

    def test_12_hot_rollback_journal_is_recovered_by_the_owner_pass(self):
        from memvane.kernel.errors import _query_only_uri
        self.select_schema3()
        self.card('concepts/kestrel-perch.md', 'kestrelprobe')
        first, _calls = self.run_pass()
        self.assertEqual(first['state'], 'indexed', first)
        published = self.head()
        before, journal = hot.kill_mid_write(self.store_path)
        torn = self.store_path.read_bytes()
        untouched = lambda: journal.exists() and self.store_path.read_bytes() == torn
        outcomes = {}
        # The view-store reader opens ro: it refuses and writes nothing.
        try:
            self.views.ViewStore.open_reader(self.store_path, repository=self.repository,
                                             scope_id=self.case.route['scopeId']).close()
            outcomes['openReader'] = 'opened'
        except Exception as exc:  # noqa: BLE001 - the reason code is the evidence
            outcomes['openReader'] = str(exc)
        outcomes['openReaderWrote'] = not untouched()
        # Recall's store reads use _query_only_uri: ro refuses with its own
        # reason; macOS Python 3.9 opens rw (its WAL workaround), so SQLite
        # rolls the journal back there.
        try:
            hit = self.recall('kestrelprobe')
            outcomes['recall'] = ('served', hit.data_generation)
        except Exception as exc:  # noqa: BLE001
            outcomes['recall'] = ('refused', str(exc))
        outcomes['recallWrote'] = not untouched()
        for attempt in ('pass', 'retry'):
            try:
                result, calls = self.run_pass()
                outcomes[attempt] = (result['state'], result.get('reason'), len(calls))
            except Exception as exc:  # noqa: BLE001
                outcomes[attempt] = ('raised', type(exc).__name__, str(exc))
        recall_rw = _query_only_uri(self.store_path).endswith('?mode=rw')
        emit('schema3-hot-journal', python=sys.version.split()[0], recallOpensRw=recall_rw, **outcomes)
        self.assertEqual((outcomes['openReader'], outcomes['openReaderWrote']),
                         ('candidate_open_failed', False), outcomes)
        if recall_rw:
            self.assertEqual((outcomes['recall'], outcomes['recallWrote']),
                             (('served', published[0]), True), outcomes)
        else:
            self.assertEqual((outcomes['recall'], outcomes['recallWrote']),
                             (('refused', 'reader_store_recovery_pending'), False), outcomes)
        self.assertEqual(outcomes['pass'], ('unchanged', None, 0), outcomes)
        self.assertEqual(outcomes['retry'], ('unchanged', None, 0), outcomes)
        self.assertFalse(os.path.lexists(journal))
        self.assertEqual(self.store_path.read_bytes(), before)
        self.assertEqual(self.head(), published)
        hit = self.recall('kestrelprobe')
        self.assertEqual(hit.data_generation, published[0])
        self.assertIn('concepts/kestrel-perch.md', self.paths(hit))

    def test_04_v6_selection_keeps_the_v6_sync_path(self):
        self.assertEqual(self.worker._selected_store_format(self.decision), 'v6')
        with mock.patch.object(self.worker, '_run_schema3',
                               side_effect=AssertionError('schema3 path on a v6 selection')), \
                mock.patch.object(self.worker, '_capacity_paths',
                                  side_effect=RuntimeError('v6 path reached')):
            with self.seed.authority():
                with self.assertRaisesRegex(RuntimeError, 'v6 path reached'):
                    self.worker.run(self.case.project, start_service=lambda *a, **k: None)
        self.select_schema3()
        self.assertEqual(self.worker._selected_store_format(self.decision), 'schema3')
        emit('schema3-sync-v6-unchanged', v6Branch=True)


# Only this unit's checks run here; the seed's own checks run in their file.
for _name in dir(seed_module.Schema3WriterPlanChecks):
    if _name.startswith('test') and _name not in vars(Schema3SyncPublishChecks):
        setattr(Schema3SyncPublishChecks, _name, None)


if __name__ == '__main__':
    unittest.main()

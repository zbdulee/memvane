"""The selected sync pass publishes format 4 view generations recall reads.

One project onboarded through the 1.0 install use cases (``SyncCase``); each
pass runs ``sync.api.run``. A pass ingests cards whose bytes or observed source
trust changed, publishes the lexical generation and
then the vector generation, publishes removals, never publishes a partial
cold view, and leaves the journal untouched when nothing changed. Every check
counts rows, epochs and pass results.
"""
import uuid
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from memvane.sync import product_sync_worker as worker
from memvane.sync.tests.support import SyncCase, sha
from tests.support import sqlite_hot_journal as hot
from tests.support.project import settle_index
from tests.support.gc_fixture import GC_HELD_CHILD


class _Store:
    """The opened store with ``publish_generation`` replaced (``store.api.open_store`` wrapper)."""

    def __init__(self, store, publish):
        self._store, self._publish = store, publish

    def publish_generation(self, *args, **kwargs):
        return self._publish(self._store, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._store, name)


class SyncPublish(SyncCase):
    def test_previous_trust_is_removed_for_unchanged_mixed_cards_across_ingest_batches(self):
        from memvane.settings import api as settings
        normal = self.fixture.write_note('notes/clean.md', 'body\u2028only\n')
        paths = {}
        for i, sep in enumerate(('\x85', '\u2028', '\u2029')):
            raw = self.fixture.write_note('notes/mixed' + sep + 'path.md', 'raw\n')
            rel = 'concepts/mixed-' + str(i) + '.md'
            card = self.fixture.write_card(rel, title='Mixed', body='handwritten note\n', sources=[normal, raw])
            paths[rel] = (card, card.read_bytes())
        neighbor = 'concepts/clean.md'
        self.fixture.write_card(neighbor, title='Clean', body='normal\n', sources=[normal])
        # Publish a real head under the earlier policy (all these source paths were eligible).
        with mock.patch.object(settings, 'excluded_source', return_value=False):
            previous = self.run_pass()['generation']
        for rel in paths:
            self.assertEqual(self.member_trust(previous, rel)[1], 1)
        with mock.patch('memvane.sync.product_sync_worker.SCHEMA3_MAX_INGEST', 1):
            for _ in range(len(paths) - 1):
                self.assertEqual(self.run_pass()['state'], 'pending')
            current = self.run_pass()
        self.assertEqual(current['state'], 'indexed')
        for rel, (card, original) in paths.items():
            self.assertEqual(self.member_trust(current['generation'], rel), (None, 0))
            self.assertEqual(card.read_bytes(), original)
        observations = self.generation_sources(current['generation'])
        self.assertEqual(observations, self.generation_sources(previous))
        for sep in ('\x85', '\u2028', '\u2029'):
            raw_path = 'notes/mixed' + sep + 'path.md'
            self.assertEqual(observations[raw_path], raw_path)
            self.assertEqual((self.project / raw_path).read_bytes(), b'raw\n')
        self.assertEqual(self.member_trust(current['generation'], neighbor)[1], 1)
        self.assertEqual(self.run_pass()['state'], 'unchanged')

    def test_recall_hook_during_gc_pass(self):
        ready, release = self.home / 'gc-ready', self.home / 'gc-release'
        samples = []
        def collect(real, handle, **kwargs):
            if samples:
                return real(handle, **kwargs)
            store = real.__self__
            args = dict(db=str(store._path), repository=store._repository, scope=store._scope,
                        worktree_id=handle.worktree_id, ready=str(ready), release=str(release))
            child = subprocess.Popen([sys.executable, '-B', '-c', GC_HELD_CHILD, json.dumps(args)],
                                     env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 20
                while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(ready.exists(), 'GC did not reach its commit')
                start = time.monotonic()
                code, output, errors = self.target.hook('recall', {
                    'session_id': 'fixture-session-' + uuid.uuid4().hex[:8], 'cwd': str(self.project),
                    'hook_event_name': 'UserPromptSubmit', 'prompt': 'kestrelprobe'})
                samples.append((time.monotonic() - start) * 1000)
                self.assertEqual(code, 0, errors)
                self.assertIn('concepts/kestrel-perch.md', output)
            finally:
                release.touch()
                output, errors = child.communicate(timeout=25)
            self.assertEqual(child.returncode, 0, output + errors)
            return real(handle, **kwargs)
        with self.store_methods(collect_view_rows=collect):
            self.card('concepts/another.md', 'anotherprobe')
            self.assertEqual(self.run_pass()['state'], 'indexed')
        self.assertEqual(len(samples), 1)
        print('synthetic full recall hook during held GC: %.3f ms' % samples[0])

    def test_row_gc_runs_only_after_a_successful_publication(self):
        calls = []
        def collect(real, *args, **kwargs):
            calls.append(kwargs.get('limit'))
            return real(*args, **kwargs)
        with self.store_methods(collect_view_rows=collect):
            self.card('concepts/kestrel-perch.md', 'gcpublicationprobe')
            result = self.run_pass()
            self.assertEqual(result['state'], 'indexed')
            self.assertEqual(calls, [64, 64])
            calls.clear()
            self.assertEqual(self.run_pass()['state'], 'unchanged')
            self.assertEqual(calls, [])

    def test_failed_publication_does_not_run_row_gc(self):
        calls = []
        def collect(real, *args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)
        def fail(_real, *args, **kwargs):
            raise RuntimeError('synthetic publication failure')
        self.card('concepts/kestrel-perch.md', 'gcfailedprobe')
        with self.store_methods(collect_view_rows=collect, publish_generation=fail):
            with self.assertRaisesRegex(RuntimeError, 'synthetic publication failure'):
                self.run_pass()
        self.assertEqual(calls, [])

    def test_sync_restores_the_untracked_wiki_keeper(self):
        keeper = self.wiki / '.memvane-root'
        self.assertTrue(keeper.is_file())
        inode = self.wiki.stat().st_ino
        keeper.unlink()
        result = self.run_pass()
        self.assertIn(result['state'], ('indexed', 'unchanged'))
        self.assertTrue(keeper.is_file())
        self.assertEqual(self.wiki.stat().st_ino, inode)
        self.assertEqual(self.fixture.git('ls-files', '--', '.memvane/wiki/.memvane-root').strip(), '')
        self.fixture.git('rm', '-r', '--', '.memvane/wiki')
        self.assertEqual(self.wiki.stat().st_ino, inode)
        self.fixture.git('checkout', 'HEAD', '--', '.memvane/wiki')
        self.assertEqual(self.wiki.stat().st_ino, inode)

    def recalled(self, prompt):
        """Member paths of the selected recall's cards (``<wiki collection>/<path>`` lines)."""
        prefix = '- "' + self.fixture.wiki_name + '/'
        return [line[len(prefix):].split('"', 1)[0] for line in self.recall(prompt).splitlines()
                if line.startswith(prefix)]

    def recall_event(self, prompt):
        """``(context, last recall event, sync wakes)`` of one in-process recall."""
        wakes = []
        with mock.patch('memvane.runtime.api.request',
                        lambda kind, target='', **_kw: wakes.append((kind, target)) or {'status': 'queued'}):
            context = self.recall(prompt)
        recalls = self.events('recall')
        self.assertTrue(recalls)
        return context, recalls[-1], wakes

    def publish_wrapped(self, publish):
        from memvane.store import api as store_api
        real = store_api.open_store
        return mock.patch('memvane.store.api.open_store',
                          lambda *args, **kwargs: _Store(real(*args, **kwargs), publish))

    # -- changed cards only ------------------------------------------------------
    def test_edit_ingests_only_that_card_and_an_idle_pass_writes_nothing(self):
        start, epoch = self.head(), self.epoch()
        before = self.members(start[0])
        self.assertEqual(len(before), 5)
        self.assertTrue(all(eligible == 1 for _blob, eligible in before.values()))
        # D3: a trusted card's source spans are the generation's location rows.
        self.assertGreaterEqual(self.card_sources(start[0], 'concepts/kestrel-perch.md'), 1)
        self.assertIn('concepts/kestrel-perch.md', self.recalled('kestrelprobe'))
        idle = self.run_pass()
        self.assertEqual((idle['state'], idle['vector']['state']), ('unchanged', 'current'), idle)
        self.assertEqual((self.head(), self.epoch()), (start, epoch))

        self.card('concepts/kestrel-perch.md', 'falconprobe')
        edited = self.run_pass()
        self.assertEqual((edited['state'], edited['ingested'], edited['removed'], edited['pending']),
                         ('indexed', 1, 0, 0), edited)
        # Changed content is filled; unchanged members retain their stored sets.
        self.assertEqual(edited['vector']['state'], 'published', edited)
        self.assertEqual(edited['vector']['filled'], 1, edited)
        self.assertIn(edited['vector']['cacheHits'], (0, 4), edited)
        published = self.head()
        self.assertEqual(published[0], edited['generation'])
        self.assertIn(published[1] - start[1], (1, 2))
        self.assertEqual(self.coverage(edited['generation'])[1], 'complete')
        self.assertEqual(self.epoch(), epoch + 1)
        after = self.members(edited['generation'])
        self.assertEqual(self.changed_paths(before, after), ['concepts/kestrel-perch.md'])
        self.assertEqual({p: m for p, m in after.items() if p != 'concepts/kestrel-perch.md'},
                         {p: m for p, m in before.items() if p != 'concepts/kestrel-perch.md'})
        self.assertIn('concepts/kestrel-perch.md', self.recalled('falconprobe'))
        self.assertNotIn('concepts/kestrel-perch.md', self.recalled('kestrelprobe'))
        idle = self.run_pass()
        self.assertEqual((idle['state'], self.head(), self.epoch()),
                         ('unchanged', published, epoch + 1), idle)

    # -- removals ----------------------------------------------------------------
    def test_removed_cards_leave_the_generation_without_touching_remaining_files(self):
        start = self.head()
        members = sorted(self.members(start[0]))
        (self.wiki / 'concepts/kestrel-perch.md').unlink()
        one = self.run_pass()
        self.assertEqual((one['state'], one['removed'], one['ingested']), ('indexed', 1, 0), one)
        self.assertEqual(sorted(self.members(one['generation'])),
                         [p for p in members if p != 'concepts/kestrel-perch.md'])
        remaining_file = self.wiki / 'keep.txt'
        remaining_file.write_text('preserved', encoding='utf-8')
        for path in members:
            if path != 'concepts/kestrel-perch.md':
                (self.wiki / path).unlink()
        result = self.run_pass()
        self.assertEqual((result['state'], result['removed']), ('indexed', len(members) - 1), result)
        self.assertEqual(len(self.members(result['generation'])), 0)
        self.assertEqual(remaining_file.read_text(encoding='utf-8'), 'preserved')

    # -- settings ----------------------------------------------------------------
    def test_settings_bytes_and_skip_paths_edits_publish_and_recall_waits_for_the_reseal(self):
        settings = self.fixture.settings_path
        original = settings.read_bytes()
        start = self.head()
        self.card('concepts/heron-watch.md', 'heronwatchprobe')
        # Same settings identity, different bytes: the pass publishes as before.
        settings.write_bytes(original.rstrip(b'\n') + b'\n\n')
        spaced = self.run_pass()
        self.assertEqual((spaced['state'], spaced['ingested']), ('indexed', 1), spaced)
        self.assertNotIn('readerResettle', spaced)
        self.assertIn('concepts/heron-watch.md', self.recalled('heronwatchprobe'))
        # A skipPaths edit only changes which cards the next generation holds:
        # newly skipped cards leave without ingest (not a mass removal) ...
        concepts = sorted(p for p in self.members(spaced['generation']) if p.startswith('concepts/'))
        self.assertEqual(len(concepts), 6)
        settings.write_text(json.dumps(dict(json.loads(original), skipPaths=['concepts'])), encoding='utf8')
        # Until the owner re-seals, recall answers without context, asks for the
        # owner's sync and leaves one fixed reason.
        context, event, wakes = self.recall_event('heronwatchprobe')
        self.assertEqual(context, '')
        self.assertEqual((event.get('outcome'), event.get('reason')), ('empty', 'reader_settings_resealing'), event)
        self.assertIn('laya_reader_settings_resealing', event.get('notes') or [])
        self.assertEqual(wakes, [('laya-sync', str(self.project))])
        skipped = self.run_pass()
        # Skipped cards leave the generation as exclusions, not removals.
        self.assertEqual((skipped['state'], skipped['ingested'], skipped['removed'], skipped.get('readerResettle')),
                         ('indexed', 0, 0, 'resealed'), skipped)
        self.assertEqual([p for p in self.members(skipped['generation']) if p.startswith('concepts/')], [])
        context, event, _wakes = self.recall_event('heronwatchprobe')
        self.assertNotEqual(event.get('reason'), 'reader_settings_resealing', event)
        self.assertNotIn('concepts/heron-watch.md', context)
        idle = self.run_pass()
        self.assertEqual((idle['state'], idle.get('readerResettle')), ('unchanged', None), idle)
        # ... and cards skipPaths no longer covers come back like new cards.
        settings.write_bytes(original)
        restored = self.run_pass()
        self.assertEqual((restored['state'], restored['ingested'], restored.get('readerResettle')),
                         ('indexed', 6, 'resealed'), restored)
        self.assertEqual(sorted(p for p in self.members(restored['generation']) if p.startswith('concepts/')),
                         concepts)
        self.assertEqual(self.members(restored['generation'])['concepts/heron-watch.md'][1], 1)
        self.assertIn('concepts/heron-watch.md', self.recalled('heronwatchprobe'))
        self.assertGreater(self.head()[1], start[1])

    def test_settings_identity_edit_publishes_nothing(self):
        settings = self.fixture.settings_path
        original = settings.read_bytes()
        start, epoch = self.head(), self.epoch()
        self.card('concepts/heron-watch.md', 'heronwatchprobe')
        # The name (and so the settings identity the view binds) changes.
        value = json.loads(original)
        settings.write_text(json.dumps(dict(value, name=value['name'] + '-renamed')), encoding='utf8')
        try:
            try:
                held = self.run_pass()
            except Exception as exc:  # the route itself refuses before the store
                held = {'state': 'raised', 'reason': type(exc).__name__ + ':' + str(exc)}
        finally:
            settings.write_bytes(original)
        self.assertNotIn(held['state'], ('indexed', 'pending', 'unchanged'), held)
        self.assertEqual((self.head(), self.epoch()), (start, epoch))

    # -- cold views and staged changes ------------------------------------------
    def test_cold_view_publishes_only_complete_and_recall_is_preparing(self):
        cards = sorted(self.members(self.head()[0]))
        # A new branch is a new view with no published generation (D9).
        self.fixture.git('checkout', '-q', '-b', 'cold-side')
        settle_index(self.project)
        views = self.view_heads()
        passes, ingested = [], 0
        with mock.patch.object(worker, 'SCHEMA3_MAX_INGEST', 2):
            for _ in range(len(cards) + 1):
                result = self.run_pass()
                passes.append(result)
                ingested += result.get('ingested', 0)
                if result['state'] != 'pending':
                    break
                self.assertEqual((result['reason'], result['generation'], result['ingested']),
                                 ('schema3_view_preparing', None, 2), result)
                self.assertEqual(self.view_heads(), views)
                context, event, _wakes = self.recall_event('kestrelprobe')
                self.assertEqual(context, '')
                self.assertIn('laya_view_preparing', event.get('notes') or [], event)
        self.assertEqual(result['state'], 'indexed', passes)
        self.assertEqual(len(passes), -(-len(cards) // 2))
        self.assertEqual(ingested, len(cards))
        heads = self.view_heads()
        self.assertEqual(len(heads), len(views) + 1)
        cold = [gen for view, gen, _epoch in heads if (view, gen, _epoch) not in views]
        self.assertEqual(cold, [result['generation']])
        self.assertEqual(sorted(self.members(result['generation'])), cards)
        # Every content set is already stored: the completed view's one generation seals vectors.
        self.assertEqual(self.coverage(result['generation'])[1], 'complete')
        self.assertIn('concepts/kestrel-perch.md', self.recalled('kestrelprobe'))

    def test_new_cards_over_the_ingest_cap_publish_progress_then_complete(self):
        start = self.head()
        start_members = self.members(start[0])
        for token in ('owlprobe', 'larkprobe', 'ternprobe', 'rookprobe'):
            self.card('concepts/' + token + '.md', token)
        with mock.patch.object(worker, 'SCHEMA3_MAX_INGEST', 2):
            first = self.run_pass()
            self.assertEqual((first['state'], first['reason'], first['ingested'], first['pending'],
                              first['progressKind']),
                             ('pending', 'schema3_ingest_backlog', 2, 2, 'ingest'), first)
            # New cards publish as they are ingested.
            self.assertEqual(self.head()[0], first['generation'])
            first_members = self.members(first['generation'])
            first_new = self.changed_paths(start_members, first_members)
            self.assertEqual(len(first_new), 2)
            second = self.run_pass()
        self.assertEqual((second['state'], second['ingested'], second['pending']), ('indexed', 2, 0), second)
        second_new = self.changed_paths(first_members, self.members(second['generation']))
        self.assertEqual(len(second_new), 2)
        self.assertEqual(set(first_new) & set(second_new), set())
        self.assertEqual(len(self.members(second['generation'])), 9)

    def test_modified_cards_over_the_ingest_cap_converge_without_a_partial_publish(self):
        start = self.head()
        before = self.members(start[0])
        cards = sorted(before)
        for index, path in enumerate(cards):
            with open(self.wiki / path, 'a', encoding='utf8') as handle:
                handle.write('\nedited line %d heronprobe\n' % index)
        passes, ingested = [], 0
        with mock.patch.object(worker, 'SCHEMA3_MAX_INGEST', 2):
            for _ in range(len(cards) + 2):
                result = self.run_pass()
                passes.append(result)
                ingested += result.get('ingested', 0)
                if result['state'] != 'pending':
                    break
                # The head keeps serving the published bytes while the batch accumulates.
                self.assertEqual(result['reason'], 'schema3_changes_staged', result)
                self.assertEqual(self.head(), start, passes)
                self.assertIn('concepts/kestrel-perch.md', self.recalled('kestrelprobe'))
        self.assertEqual(result['state'], 'indexed', passes)
        self.assertEqual(len(passes), -(-len(cards) // 2), passes)
        self.assertEqual(ingested, len(cards))
        after = self.members(result['generation'])
        self.assertEqual(sorted(after), cards)
        for path in cards:
            self.assertEqual(after[path][0], sha((self.wiki / path).read_bytes()), path)
            self.assertNotEqual(after[path][0], before[path][0], path)
            # An edited body no longer matches its verifiedBodyHash: the member is untrusted.
            self.assertEqual(after[path][1], 0, path)
        self.assertEqual(self.head()[0], result['generation'])
        self.assertEqual(self.recalled('heronprobe'), [])

    # -- interrupted publication --------------------------------------------------
    def test_interrupted_publish_keeps_the_old_head_and_the_next_pass_resumes(self):
        start = self.head()
        start_members = self.members(start[0])
        self.card('concepts/wren-path.md', 'robinprobe')

        def before(store, *args, **kwargs):
            raise RuntimeError('injected stop before publish')
        with self.publish_wrapped(before):
            with self.assertRaisesRegex(RuntimeError, 'injected stop before publish'):
                self.run_pass()
        self.assertEqual(self.head(), start)
        resumed = self.run_pass()
        # The stopped pass's prepared member is carried, not ingested again.
        self.assertEqual((resumed['state'], resumed['ingested']), ('indexed', 0), resumed)
        self.assertEqual(self.changed_paths(start_members, self.members(resumed['generation'])),
                         ['concepts/wren-path.md'])
        resumed_head = self.head()
        self.assertEqual(resumed_head[0], resumed['generation'])

        self.card('concepts/wren-path.md', 'thrushprobe')
        published = []

        def after(store, *args, **kwargs):
            published.append(store.publish_generation(*args, **kwargs))
            raise RuntimeError('injected stop after publish')
        with self.publish_wrapped(after):
            with self.assertRaisesRegex(RuntimeError, 'injected stop after publish'):
                self.run_pass()
        self.assertEqual(len(published), 1)
        moved = self.head()
        self.assertGreater(moved[1], resumed_head[1])
        replay = self.run_pass()
        # The lexical head the stopped pass published is complete; this pass adds its vectors only.
        self.assertIn(replay['state'], ('unchanged', 'indexed'), replay)
        self.assertEqual(replay.get('ingested', 0), 0, replay)
        self.assertIn('concepts/wren-path.md', self.recalled('thrushprobe'))

    # -- source revisions ----------------------------------------------------------
    def test_unchanged_card_is_untrusted_only_while_observed_source_differs(self):
        self.card('concepts/source-stale.md', 'stalecaseprobe')
        first = self.run_pass()
        self.assertEqual(first['state'], 'indexed')
        self.assertEqual(self.member_trust(first['generation'], 'concepts/source-stale.md')[1], 1)
        source = self.project / 'notes/stalecaseprobe.md'
        original = source.read_bytes()
        source.write_bytes(b'Changed source, without a new verified card.\n')
        changed = self.run_pass()
        self.assertEqual(changed['state'], 'indexed')
        self.assertEqual(self.member_trust(changed['generation'], 'concepts/source-stale.md'), (None, 0))
        self.assertNotIn('concepts/source-stale.md', self.recalled('stalecaseprobe'))
        source.write_bytes(original)
        restored = self.run_pass()
        self.assertEqual(restored['state'], 'indexed')
        self.assertEqual(self.member_trust(restored['generation'], 'concepts/source-stale.md')[1], 1)
        self.assertIn('concepts/source-stale.md', self.recalled('stalecaseprobe'))

    def test_incomplete_raw_observation_does_not_demote_existing_trust(self):
        self.card('concepts/source-unknown.md', 'unknownsourceprobe')
        first = self.run_pass()
        self.assertEqual(self.member_trust(first['generation'], 'concepts/source-unknown.md')[1], 1)
        (self.project / 'notes/unknownsourceprobe.md').write_bytes(b'Changed source.\n')
        with mock.patch('memvane.wiki.intake.types.RAW_SCAN_MAX_FILES', 1):
            unknown = self.run_pass()
        self.assertEqual(self.member_trust(unknown['generation'], 'concepts/source-unknown.md')[1], 1)
        known = self.run_pass()
        self.assertEqual(self.member_trust(known['generation'], 'concepts/source-unknown.md'), (None, 0))

    def test_source_reclassification_resumes_across_ingest_batches(self):
        for token in ('batchfirstprobe', 'batchsecondprobe'):
            self.card('concepts/' + token + '.md', token)
        self.assertEqual(self.run_pass()['state'], 'indexed')
        originals = {}
        for token in ('batchfirstprobe', 'batchsecondprobe'):
            source = self.project / ('notes/' + token + '.md')
            originals[token] = source.read_bytes()
            source.write_bytes(b'Changed source.\n')
        with mock.patch('memvane.sync.product_sync_worker.SCHEMA3_MAX_INGEST', 1):
            self.assertEqual(self.run_pass()['state'], 'pending')
            stale = self.run_pass()
            self.assertEqual(stale['state'], 'indexed')
            for token in originals:
                self.assertEqual(self.member_trust(stale['generation'], 'concepts/' + token + '.md'), (None, 0))
            for token, raw in originals.items():
                (self.project / ('notes/' + token + '.md')).write_bytes(raw)
            self.assertEqual(self.run_pass()['state'], 'pending')
            restored = self.run_pass()
            self.assertEqual(restored['state'], 'indexed')
            for token in originals:
                self.assertEqual(self.member_trust(restored['generation'], 'concepts/' + token + '.md')[1], 1)

    def dual_card(self, rel, token, *, current):
        """A verified card citing ``notes/<token>.md`` at an older and a compiled revision.

        The source then holds ``current``: the compiled bytes, or bytes that
        match neither revision (a stale card). Returns ``(path, compiled bytes)``.
        """
        older = ('Older source for ' + token + '.\n').encode('utf8')
        compiled = ('Synthetic source for ' + token + '.\n').encode('utf8')
        note = self.fixture.write_note('notes/' + token + '.md', older.decode('utf8'))
        first = self.fixture.revision(note)
        note.write_bytes(compiled)
        second = self.fixture.revision(note)
        from memvane.wiki.card import api as card
        lines = ''.join('  - ' + line + '\n' for line in card.dump_source_revisions([first, second]))
        text = (f'---\ntitle: "{token} card"\nstatus: verified\ncreatedBy: memvane\nsourceRevisions:\n'
                + lines + f'---\n# {token} card\n{token} verified body\n')
        text = text.replace('createdBy: memvane\n', 'createdBy: memvane\nverifiedBodyHash: '
                            + card.card_body_hash(text) + '\n', 1)
        (self.wiki / rel).write_text(text, encoding='utf8')
        note.write_bytes(compiled if current == 'compiled' else current)
        return 'notes/' + token + '.md', compiled

    def test_dual_revision_card_keeps_the_observed_revision_or_is_untrusted(self):
        # Writer ingest judges one path cited at two revisions by this pass's raw observation.
        path, compiled = self.dual_card('concepts/dual-current.md', 'dualcurrentprobe', current='compiled')
        self.dual_card('concepts/dual-stale.md', 'dualstaleprobe', current=b'Third bytes.\n')
        queued = self.compile_files()
        result = self.run_pass()
        self.assertEqual((result['state'], result['ingested']), ('indexed', 2), result)
        gen = result['generation']
        self.assertEqual(self.attested_revisions(gen, 'concepts/dual-current.md'),
                         [(path, sha(compiled), len(compiled))])
        self.assertEqual(self.member_trust(gen, 'concepts/dual-current.md')[1], 1)
        self.assertEqual(self.member_trust(gen, 'concepts/dual-stale.md'), (None, 0))
        self.assertEqual(self.member_trust(gen, 'concepts/kestrel-perch.md')[1], 1)
        self.assertIn('concepts/dual-current.md', self.recalled('dualcurrentprobe'))
        self.assertNotIn('concepts/dual-stale.md', self.recalled('dualstaleprobe'))
        self.assertEqual(self.compile_files(), queued, 'the writer queues no regeneration')

    def test_observation_over_its_bounds_defers_source_judgment(self):
        path, _compiled = self.dual_card('concepts/dual-kept.md', 'dualkeptprobe', current=b'Third bytes.\n')
        # No current observation picks no winner.
        with mock.patch('memvane.wiki.intake.types.RAW_SCAN_MAX_FILES', 1):
            first = self.run_pass()
        self.assertEqual(first['state'], 'indexed', first)
        self.assertEqual(len(self.attested_revisions(first['generation'], 'concepts/dual-kept.md')), 2)
        trust = self.member_trust(first['generation'], 'concepts/dual-kept.md')
        self.assertEqual(trust[1], 1)
        # A later complete observation proves both revisions differ.
        (self.project / path).write_bytes(b'Fourth bytes.\n')
        second = self.run_pass()
        self.assertEqual(second.get('ingested', 0), 1, second)
        self.assertEqual(self.member_trust(self.head()[0], 'concepts/dual-kept.md'), (None, 0))

    def test_card_citing_a_dot_directory_source_keeps_its_trust(self):
        self.card('concepts/dot-source.md', 'dotsourceprobe', source_dir='.nova/06_Sessions')
        result = self.run_pass()
        self.assertEqual((result['state'], result['ingested']), ('indexed', 1), result)
        # The raw observation of the project root does not walk dot directories.
        self.assertEqual(sorted(self.generation_sources(result['generation'])),
                         ['notes/' + token + '.md' for _rel, token in self.CARDS])
        self.assertEqual(self.member_trust(result['generation'], 'concepts/dot-source.md')[1], 1)
        self.assertEqual([row[0] for row in self.attested_revisions(result['generation'], 'concepts/dot-source.md')],
                         ['.nova/06_Sessions/dotsourceprobe.md'])
        self.assertGreaterEqual(self.card_sources(result['generation'], 'concepts/dot-source.md'), 1)
        self.assertIn('concepts/dot-source.md', self.recalled('dotsourceprobe'))
        idle = self.run_pass()
        self.assertEqual(idle['state'], 'unchanged', idle)

    # -- store recovery ----------------------------------------------------------
    def test_hot_rollback_journal_is_recovered_and_the_owner_pass_is_unchanged(self):
        published = self.head()
        before, journal = hot.kill_mid_write(self.target.store_path)
        # The route classification's store probe rolls a hot journal back
        # (``route.index_host_route._store_identity``); recall then serves the head.
        self.assertIn('concepts/kestrel-perch.md', self.recalled('kestrelprobe'))
        self.assertFalse(os.path.lexists(journal))
        self.assertEqual(self.target.store_path.read_bytes(), before)
        before, journal = hot.kill_mid_write(self.target.store_path)
        for _attempt in ('pass', 'retry'):
            result = self.run_pass()
            self.assertEqual((result['state'], result.get('ingested', 0)), ('unchanged', 0), result)
        self.assertFalse(os.path.lexists(journal))
        self.assertEqual(self.target.store_path.read_bytes(), before)
        self.assertEqual(self.head(), published)
        self.assertIn('concepts/kestrel-perch.md', self.recalled('kestrelprobe'))


if __name__ == '__main__':
    unittest.main()

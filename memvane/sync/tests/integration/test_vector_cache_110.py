"""The sync owner keeps the vector arena beside the store: before publishing, catching up, skipping.

``SyncCase``: a settled install whose head seals vectors. Every check counts
sets, events and wakes; none reads a wall clock. The arena is read back with
the recall's reader (``vectorcache.api.opened``) against the store's own rows.
"""
import json
import shutil
from unittest import mock

from memvane.store.types import BlobStoreError
from memvane.sync import product_sync_worker as worker
from memvane.sync.tests.support import SyncCase
from memvane.vectorcache import api as vectorcache

LIVE = ('SELECT e.blob_sha,e.chunk_count,e.set_digest FROM embedding_sets e WHERE e.blob_sha IN ('
        'SELECT c.blob_sha FROM s_cards c JOIN s_heads h ON h.view_id=c.view_id '
        'WHERE c.eligible=1 AND (c.gen_to IS NULL OR c.gen_to>=h.ordinal))')


class ArenaCase(SyncCase):
    def setUp(self):
        super().setUp()
        self.directory = self.target.store_path.parent / vectorcache.DIRECTORY

    def live(self):
        with self.db() as db:
            return {row[0]: (row[1], row[2]) for row in db.execute(LIVE)}

    def identity(self):
        return json.loads((self.directory / 'current.json').read_text())

    def cover(self, live=None):
        """``(covered sets, missing, inconsistent)`` of the live sets as a recall reads the arena; None without one."""
        if not (self.directory / 'current.json').is_file():
            return None
        spec = {key: self.identity()[key] for key in ('profile_id', 'recipe_id', 'dimension', 'store_incarnation')}
        with vectorcache.opened(self.directory, **spec) as found:
            if found is None:
                return None
            covered, missing, wrong = found.cover(self.live() if live is None else live)
            return len(covered), missing, wrong

    def cache_events(self, since=0):
        return [(event['outcome'], event.get('reason')) for event in self.events('vector.cache')[since:]]

    def wakes(self):
        wakes = []
        return wakes, mock.patch('memvane.runtime.api.request',
                                 lambda kind, target='', **kw: wakes.append((kind, target, kw.get('delay', 0)))
                                 or {'status': 'queued'})

    def empty_arena(self):
        shutil.rmtree(self.directory)


class Upkeep(ArenaCase):
    def test_a_settled_install_has_an_arena_that_covers_every_live_set(self):
        live = self.live()
        self.assertEqual(len(live), len(self.CARDS))
        self.assertEqual(self.cover(), (len(live), [], 0))
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        # An idle pass has nothing to say about the arena.
        before = len(self.events('vector.cache'))
        wakes, recorded = self.wakes()
        with recorded:
            self.assertEqual(self.run_pass()['state'], 'unchanged')
        self.assertEqual((self.cache_events(before), wakes), ([], []))

    def test_a_changed_cards_set_is_in_the_arena_before_its_generation_is_published(self):
        self.card(self.CARDS[0][0], 'ospreyprobe')
        seen = []

        def publishing(real, *args, **kwargs):
            # Every stored set is in the arena when the generation becomes visible (the new card's included).
            with self.db() as db:
                stored = {row[0]: (row[1], row[2]) for row in db.execute(
                    'SELECT blob_sha,chunk_count,set_digest FROM embedding_sets')}
            seen.append((len(stored), self.cover(stored)))
            return real(*args, **kwargs)
        before = len(self.events('vector.cache'))
        with self.store_methods(publish_generation=publishing):
            result = self.run_pass()
        self.assertEqual(result['vector']['state'], 'published', result)
        self.assertTrue(seen)
        for stored, found in seen:
            self.assertEqual(found, (stored, [], 0))
        self.assertEqual(seen[-1][0], len(self.CARDS) + 1)
        self.assertEqual(self.cover(), (len(self.live()), [], 0))
        self.assertEqual(self.cache_events(before), [('append', None)])
        self.assertNotIn('cacheReoffer', result)

    def test_a_damaged_set_is_repaired_by_the_next_pass(self):
        nonce = self.identity()['nonce']
        vec = self.directory / ('arena-' + nonce + '.vec')
        raw = bytearray(vec.read_bytes())
        raw[4096] ^= 0xFF
        vec.write_bytes(bytes(raw))
        covered, missing, wrong = self.cover()
        self.assertEqual((len(missing), wrong), (1, 1))
        before = len(self.events('vector.cache'))
        wakes, recorded = self.wakes()
        with recorded:
            self.assertEqual(self.run_pass()['state'], 'unchanged')
        self.assertEqual(self.cover(), (len(self.live()), [], 0))
        repair = self.events('vector.cache')[before:]
        # The damaged rows are not left between the sets: the pair is replaced, then the set repaired.
        self.assertEqual([(event['outcome'], event.get('sets')) for event in repair],
                         [('compact', None), ('repair', 1)])
        self.assertEqual({event['deadRows'] for event in repair}, {0})
        self.assertEqual(wakes, [])  # A repair is not catch-up progress.


class CatchUp(ArenaCase):
    def test_the_wake_is_offered_again_only_after_a_pass_that_moved_and_left_sets(self):
        self.empty_arena()
        total = len(self.live())
        wakes, recorded = self.wakes()
        before = len(self.events('vector.cache'))
        with recorded, mock.patch('memvane.vectorcache.types.PASS_VECTORS', 1):
            results = [self.run_pass() for _ in range(total)]
        # One set a pass: every pass but the last leaves sets and asks for the same wake once.
        self.assertEqual([item.get('vectorReoffer', False) for item in results], [True] * (total - 1) + [False])
        self.assertEqual(wakes, [('laya-sync', str(self.project), worker.SCHEMA3_VECTOR_REOFFER_SECONDS)] * (total - 1))
        self.assertTrue(all(item['state'] == 'unchanged' for item in results), results)
        self.assertEqual(self.cache_events(before), [('catchup', None)] * total)
        self.assertEqual(self.cover(), (total, [], 0))
        left = [event['remaining'] for event in self.events('vector.cache')[before:]]
        self.assertEqual(left, list(range(total - 1, -1, -1)))
        with recorded:
            self.run_pass()
        self.assertEqual(len(wakes), total - 1)

    def test_sets_a_publication_leaves_behind_are_caught_up_by_the_offered_wake(self):
        for index, token in enumerate(('ternprobe', 'ploverprobe', 'gullprobe')):
            self.card(self.CARDS[index][0], token)
        wakes, recorded = self.wakes()
        before = len(self.events('vector.cache'))
        with recorded, mock.patch('memvane.vectorcache.types.PASS_VECTORS', 1):
            first = self.run_pass()
            self.assertEqual((first['vector']['state'], first.get('vectorReoffer')), ('published', True), first)
            self.assertEqual(len(wakes), 1)
            while self.cover()[1]:
                self.run_pass()
            self.run_pass()
        events = self.events('vector.cache')[before:]
        self.assertEqual(events[0]['outcome'], 'append')
        self.assertGreater(events[0]['remaining'], 0)
        self.assertEqual(events[-1]['remaining'], 0)
        # Each pass that moved and left sets offered the wake once; the last one did not.
        self.assertEqual(len(wakes), len(events) - 1)
        self.assertTrue(all(type(event['ms']) in (int, float) and event['ms'] >= 0 for event in events))

    def skipped(self, reason, context=None):
        """One pass whose arena cannot move: it publishes, offers no wake and reports one ``skipped``."""
        from contextlib import nullcontext
        wakes, recorded = self.wakes()
        before = len(self.events('vector.cache'))
        self.card(self.CARDS[1][0], 'heronprobe' + reason.replace('_', ''))
        with recorded, (context or nullcontext()):
            result = self.run_pass()
        self.assertEqual((result['state'], result['vector']['state']), ('indexed', 'published'), result)
        self.assertNotIn('vectorReoffer', result)
        self.assertEqual(wakes, [])
        self.assertEqual(self.cache_events(before), [('skipped', reason)])
        return result

    def test_an_arena_over_the_global_quota_is_skipped(self):
        self.empty_arena()
        self.skipped('quota', mock.patch('memvane.vectorcache.types.GLOBAL_BYTES', 0))
        self.assertEqual(self.cover(), (0, sorted(self.live()), 0))

    def test_an_arena_directory_that_cannot_be_written_is_skipped_and_the_generation_is_published(self):
        self.empty_arena()
        self.directory.write_text('not a directory')
        self.skipped('write_unavailable')
        self.assertTrue(self.directory.is_file())

    def test_an_arena_of_a_newer_format_is_left_alone(self):
        path = self.directory / 'current.json'
        path.write_text(json.dumps(dict(self.identity(), format=vectorcache.FORMAT + 1)))
        files = {item.name: item.read_bytes() for item in self.directory.iterdir()}
        self.skipped('format_newer')
        self.assertEqual({item.name: item.read_bytes() for item in self.directory.iterdir()}, files)

    def test_sets_the_store_cannot_read_are_skipped(self):
        self.empty_arena()

        def refused(_real, *_args, **_kwargs):
            raise BlobStoreError('embedding_set_invalid')
        self.skipped('set_unreadable', self.store_methods(arena_set=refused))
        self.assertEqual(self.cover(), (0, sorted(self.live()), 0))

    def test_a_failure_inside_the_arena_upkeep_never_fails_the_pass(self):
        def broken(_real, *_args, **_kwargs):
            raise RuntimeError('arena')
        before = len(self.events('vector.cache'))
        self.card(self.CARDS[2][0], 'egretprobe')
        with self.store_methods(arena_sets=broken):
            result = self.run_pass()
        self.assertEqual((result['state'], result['vector']['state']), ('indexed', 'published'), result)
        self.assertEqual(self.cache_events(before), [('failed', 'RuntimeError')])


class Report(ArenaCase):
    def test_the_event_ms_is_the_writers_own_time_not_the_pass_around_it(self):
        from memvane.sync import vector_cache

        class Done:
            def summary(self):
                return dict(rebuilt=False, compacted=False, repaired=0, catchupRows=3, appendRows=0, unreadable=0,
                            skipped=None, ms=7.5, remaining=0, deadRows=0, bytes=4096)

            def close(self):
                pass
        rows = []
        vectors = {'arena': {'writer': Done(), 'failed': None, 'started': 0.0, 'path': None}}
        with mock.patch('memvane.diagnostics.api.emit', side_effect=lambda event, **fields: rows.append((event, fields))):
            self.assertFalse(vector_cache.finish(vectors, {}))
        self.assertEqual([(event, fields['outcome'], fields['ms'], fields['rows']) for event, fields in rows],
                         [('vector.cache', 'catchup', 7.5, 3)])


class Settle(ArenaCase):
    def settle(self, result, cache, fails=False):
        calls = []

        def reoffer():
            calls.append(1)
            if fails:
                raise OSError('queue')
        settled = worker._settle_vectors(result, reoffer, 'none', cache)
        return settled['state'], settled.get('vectorReoffer', False), len(calls)

    def test_the_arena_offer_follows_a_published_main_view_and_never_hides_its_own_wait(self):
        current = {'state': 'unchanged', 'vector': {'state': 'current'}}
        self.assertEqual(self.settle(current, True), ('unchanged', True, 1))
        self.assertEqual(self.settle(current, False), ('unchanged', False, 0))
        # The offer could not be queued: the pass published, the next wake goes on.
        self.assertEqual(self.settle(current, True, fails=True), ('unchanged', False, 1))
        waiting = {'state': 'unchanged', 'vector': {'state': 'pending', 'reason': 'schema3_vector_deadline',
                                                   'progress': 0}}
        self.assertEqual(self.settle(waiting, True), ('pending', False, 0))
        blocked = {'state': 'unchanged', 'vector': {'state': 'provider_blocked', 'reason': 'provider_identity_changed'}}
        self.assertEqual(self.settle(blocked, True), ('blocked', False, 0))
        self.assertEqual(self.settle({'state': 'pending', 'reason': 'schema3_pass_budget'}, True),
                         ('pending', False, 0))

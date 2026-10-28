"""Disposable step-bounded source catalog checks.

Synthetic temp-dir corpora and a temp A-int DB only; no operating DB or source.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import laya_source_catalog as catalog_mod
from laya_index_a import IndexContractError, LayaIndexA
from laya_index_source_port import (auxiliary_wiki_row,
                                    generated_wiki_log_text, scan_collections)
from laya_source_catalog import (CatalogPending, LayaSourceCatalog, load_exclusions,
                                 publish_from_catalog, search_catalog_verified,
                                 stage_from_catalog)

SCOPE = 'scope-catalog'
CLAIM = 'c' * 64
FILLER = 'lorem ipsum dolor sit amet '


def drive(catalog, **budget):
    """Step until the scan leaves 'pending'; every step stays bounded."""
    results = []
    for _ in range(10_000):
        result = catalog.step(**budget)
        results.append(result)
        if result['status'] != 'pending':
            return results
    raise AssertionError('scan did not settle')


def sized(head, size):
    return (head + FILLER * (size // len(FILLER) + 1))[:size]


class CatalogCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='laya-source-catalog-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.plans = self.base / 'src' / 'plans'
        self.wiki = self.base / 'src' / '.auto-context' / 'wiki'
        self.plans.mkdir(parents=True)
        self.wiki.mkdir(parents=True)
        self.roots = {'plans': str(self.plans), 'wiki': str(self.wiki)}
        self.state = self.base / 'state'
        self.state.mkdir()
        self.index = LayaIndexA(self.state / 'index.sqlite', create=True)
        self.addCleanup(self.index.close)

    def open_catalog(self, create=False, roots=None, scope=SCOPE, name='catalog.sqlite',
                     wiki_only=False):
        catalog = LayaSourceCatalog(self.state / name, roots or self.roots, scope,
                                    create=create, wiki_only=wiki_only)
        self.addCleanup(catalog.close)
        return catalog

    @staticmethod
    def write(root, rel, text):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def complete_catalog(self, files=10):
        for i in range(files):
            self.write(self.plans, f'a{i:02d}.md', f'# A{i}\nplain doc {i}\n')
        self.write(self.wiki, 'card.md', '# Card\nwiki card\n')
        catalog = self.open_catalog(create=True)
        self.assertEqual(catalog.request_scan()['status'], 'started')
        self.assertEqual(drive(catalog)[-1]['status'], 'complete')
        return catalog, catalog.require_servable()

    def port_manifest(self):
        return scan_collections(self.roots, SCOPE, self.index, max_files=1000,
                                max_bytes=20_000_000).manifest

    def catalog_manifest(self, catalog):
        return catalog.manifest(catalog.require_servable(),
                                load_exclusions(self.index, SCOPE))

    def publish(self, catalog, gen, expected):
        self.assertEqual(stage_from_catalog(self.index, catalog, gen, base=expected,
                                            claim_digest=CLAIM, claim_epoch=0),
                         'prepared')
        self.assertEqual(publish_from_catalog(self.index, catalog, expected, gen,
                                              'claim-' + gen, claim_digest=CLAIM,
                                              claim_epoch=0), 'published')

    def search(self, catalog, query, limit=5, **budget):
        token, _gen = self.index.pin(SCOPE)
        try:
            return search_catalog_verified(self.index, catalog, token, query, limit,
                                           **budget)
        finally:
            self.index.release(token)

    @contextlib.contextmanager
    def forbid_source_and_search(self):
        """Any source read, stat hint or A-int search inside the block fails."""
        def forbidden(*_args, **_kwargs):
            raise AssertionError('source or index touched')
        saved = [(catalog_mod, 'scan_collections'), (catalog_mod, '_read_exact'),
                 (catalog_mod, '_hash_entry'),
                 (catalog_mod.LayaSourceCatalog, 'verify_unchanged')]
        originals = [getattr(owner, name) for owner, name in saved]
        for owner, name in saved:
            setattr(owner, name, forbidden)
        self.index.search = forbidden
        try:
            yield
        finally:
            del self.index.search
            for (owner, name), original in zip(saved, originals):
                setattr(owner, name, original)

    def spoof_signatures(self, catalog):
        """Patch the module's signature function to replay scan-time signatures.

        Any node the current snapshot recorded reports its old signature, so
        the stat hint sees nothing even though content or membership changed.
        """
        recorded = {}
        for (sig,) in catalog.db.execute(
                'SELECT sig FROM scan_dirs WHERE seq=(SELECT seq FROM current) '
                'UNION ALL SELECT sig FROM scan_files '
                'WHERE seq=(SELECT seq FROM current)'):
            dev, ino = sig.split(':')[:2]
            recorded[(int(dev), int(ino))] = sig
        original = catalog_mod._sig_text
        catalog_mod._sig_text = lambda st: recorded.get((st.st_dev, st.st_ino),
                                                        original(st))
        self.addCleanup(setattr, catalog_mod, '_sig_text', original)

    def assert_stale(self, call, detail):
        with self.assertRaisesRegex(IndexContractError, 'index_stale') as caught:
            call()
        self.assertNotIsInstance(caught.exception, CatalogPending)
        self.assertIn(detail, str(caught.exception.__cause__))

    def assert_blocked_keeps(self, catalog, snapshot, reason, detail):
        result = drive(catalog)[-1]
        self.assertEqual(result['status'], 'blocked', result)
        self.assertEqual(result['blocked']['reason'], reason)
        self.assertIn(detail, result['blocked']['detail'])
        status = catalog.status()
        self.assertFalse(status['servable'])
        self.assertEqual(status['reason'], 'scan_blocked')
        self.assertEqual(status['current']['scan_id'], snapshot.scan_id)
        with self.assertRaisesRegex(IndexContractError, 'index_stale'):
            catalog.require_servable()
        again = catalog.step()
        self.assertEqual(again['status'], 'blocked')
        self.assertEqual(again['work']['units'], 0)


class CapacityTests(CatalogCase):
    def test_root_schema_and_index_stay_indexed_but_are_not_cards(self):
        # The search filter drops SCHEMA.md/index.md; the scan (and with it the
        # generation digest) keeps them, so the filter needs no re-index.
        roots = {'wiki': str(self.wiki)}
        schema = '# Auto-context Wiki Schema\n\nordinary scaffold text\n'
        index = '# Auto-context Wiki Index\n\n- decisions/\n'
        self.write(self.wiki, 'SCHEMA.md', schema)
        self.write(self.wiki, 'index.md', index)
        self.write(self.wiki, 'card.md', '# Card\nordinary knowledge\n')
        self.write(self.wiki, 'nested/SCHEMA.md', schema)
        self.assertTrue(auxiliary_wiki_row('wiki/SCHEMA.md', schema, roots))
        self.assertTrue(auxiliary_wiki_row('wiki/index.md', index, roots))
        self.assertFalse(auxiliary_wiki_row('wiki/nested/SCHEMA.md', schema, roots))
        self.assertFalse(auxiliary_wiki_row('wiki/card.md', 'x', roots))
        catalog = self.open_catalog(create=True, roots=roots, wiki_only=True)
        self.assertEqual(catalog.request_scan()['status'], 'started')
        self.assertEqual(drive(catalog, max_bytes=4096)[-1]['status'], 'complete')
        indexed = {'wiki/SCHEMA.md', 'wiki/index.md', 'wiki/card.md',
                   'wiki/nested/SCHEMA.md'}
        self.assertEqual({row[0] for row in catalog.members(
            catalog.require_complete(), load_exclusions(self.index, SCOPE))}, indexed)
        source = scan_collections(roots, SCOPE, self.index, max_bytes=4096,
                                  wiki_only=True)
        self.assertEqual(set(source.by_path), indexed)
        self.assertEqual(self.catalog_manifest(catalog), source.manifest)

    def test_only_generated_root_wiki_log_is_not_indexed(self):
        roots = {'wiki': str(self.wiki)}
        generated = ('# Auto-context Wiki Log\n\n'
                     'Append notable wiki maintenance events here.\n' +
                     '- 2026-06-30T01:45:00Z created concepts/a.md - A\n' * 20)
        self.write(self.wiki, 'log.md', generated)
        self.write(self.wiki, 'card.md', '# Card\nordinary knowledge\n')
        self.write(self.wiki, 'nested/log.md', '# Nested Log\nordinary knowledge\n')
        self.assertTrue(generated_wiki_log_text(generated))
        self.assertTrue(auxiliary_wiki_row('wiki/log.md', generated, roots))
        self.assertFalse(auxiliary_wiki_row('wiki/nested/log.md', generated, roots))
        catalog = self.open_catalog(create=True, roots=roots, wiki_only=True)
        self.assertEqual(catalog.request_scan()['status'], 'started')
        self.assertEqual(drive(catalog, max_bytes=128)[-1]['status'], 'complete')
        snapshot = catalog.require_complete()
        self.assertEqual({row[0] for row in catalog.members(
            snapshot, load_exclusions(self.index, SCOPE))},
                         {'wiki/card.md', 'wiki/nested/log.md'})
        source = scan_collections(roots, SCOPE, self.index, max_bytes=128,
                                  wiki_only=True)
        self.assertEqual(set(source.by_path),
                         {'wiki/card.md', 'wiki/nested/log.md'})
        self.assertEqual(self.catalog_manifest(catalog), source.manifest)
        self.publish(catalog, 'generated-log-omitted', None)
        self.assertEqual({row['path'] for row in self.search(
            catalog, 'ordinary', max_proof_bytes=128)},
                         {'wiki/card.md', 'wiki/nested/log.md'})
        # A root log.md without the generated header stays indexed (the digest
        # does not change), but search never serves any wiki-root meta page.
        normal = '# Useful log analysis\nordinary wiki knowledge\n'
        self.write(self.wiki, 'log.md', normal)
        self.assertTrue(auxiliary_wiki_row('wiki/log.md', normal, roots))
        self.assertEqual(catalog.request_scan()['status'], 'started')
        self.assertEqual(drive(catalog, max_bytes=128)[-1]['status'], 'complete')
        self.assertIn('wiki/log.md', {row[0] for row in catalog.members(
            catalog.require_complete(), load_exclusions(self.index, SCOPE))})

    def test_large_corpus_exceeds_both_budgets_over_bounded_steps(self):
        size = 30_000
        for d in range(13):
            for f in range(20):
                n = d * 20 + f
                self.write(self.plans, f'part{d:02d}/doc{f:02d}.md',
                           sized(f'# Doc {n}\nmarker{n:04d}token\n', size))
        self.write(self.wiki, 'card.md', '# Card\nwiki side\n')
        total_bytes = 260 * size + len('# Card\nwiki side\n')
        self.assertGreater(total_bytes, 5_000_000)
        # The port treats 200 files / 5 MB as a whole-corpus cap.
        with self.assertRaisesRegex(IndexContractError, 'index_stale'):
            scan_collections(self.roots, SCOPE, self.index)

        catalog = self.open_catalog(create=True)
        self.assertEqual(catalog.request_scan()['status'], 'started')
        results = drive(catalog)
        self.assertEqual(results[-1]['status'], 'complete')
        for result in results:
            self.assertLessEqual(result['work']['units'], 200)
            self.assertLessEqual(result['work']['bytes_read'], 5_000_000)
        self.assertIn('pending', [result['status'] for result in results])
        hashing = [r for r in results if r['work']['files_hashed']]
        self.assertGreaterEqual(len(hashing), 2)
        self.assertGreater(max(r['work']['bytes_read'] for r in results),
                           5_000_000 - size)
        self.assertEqual(sum(r['work']['files_hashed'] for r in results), 261)
        self.assertEqual(sum(r['work']['bytes_read'] for r in results), total_bytes)
        published = results[-1]['published']
        self.assertEqual((published['files'], published['bytes'], published['added']),
                         (261, total_bytes, 261))
        status = catalog.status()
        self.assertTrue(status['servable'])
        self.assertEqual(status['active']['backlog']['files_pending'], 0)

        self.assertEqual(self.catalog_manifest(catalog), self.port_manifest())
        # Scan-time evidence still indexes the whole corpus in one generation.
        self.publish(catalog, 'g1', None)

        # ...but normal search cannot prove it fresh: pending, zero rows, and
        # neither the source nor A-int is touched.
        with self.forbid_source_and_search():
            with self.assertRaises(CatalogPending) as caught:
                self.search(catalog, 'marker0257token')
        pending = caught.exception
        self.assertEqual(str(pending), 'index_stale')
        self.assertEqual(pending.detail, {
            'reason': 'proof_budget_exceeded', 'scan_id': status['current']['scan_id'],
            'snapshot_files': 261, 'snapshot_bytes': total_bytes,
            'max_proof_files': 200, 'max_proof_bytes': 5_000_000,
            'files_over': 61, 'bytes_over': total_bytes - 5_000_000,
            'requires': 'trusted_change_feed_or_immutable_source_snapshot'})
        self.assertTrue(catalog.status()['servable'])  # pending is a query outcome

        # An explicit, finite, test-only raised budget may search it.
        rows = self.search(catalog, 'marker0257token', max_proof_files=261,
                           max_proof_bytes=total_bytes)
        self.assertEqual([row['path'] for row in rows], ['plans/part12/doc17.md'])
        self.assertEqual(rows[0]['body'],
                         (self.plans / 'part12' / 'doc17.md').read_text(encoding='utf-8'))
        for budget in ({'max_proof_files': 260, 'max_proof_bytes': total_bytes},
                       {'max_proof_files': 261, 'max_proof_bytes': total_bytes - 1}):
            with self.forbid_source_and_search():
                with self.assertRaises(CatalogPending):
                    self.search(catalog, 'marker0257token', **budget)

    def test_add_update_delete_publish_only_on_completion(self):
        for i in range(12):
            self.write(self.plans, f'a{i:02d}.md', f'doc {i}\n')
        self.write(self.wiki, 'card.md', 'card\n')
        catalog = self.open_catalog(create=True)
        catalog.request_scan()
        first = drive(catalog, max_files=5)[-1]
        self.assertEqual(first['status'], 'complete')
        self.assertEqual((first['published']['added'], first['published']['deleted']),
                         (13, 0))
        snap1 = catalog.require_servable()

        (self.plans / 'a03.md').unlink()
        (self.plans / 'a04.md').unlink()
        self.write(self.plans, 'a05.md', 'doc 5 updated\n')
        self.write(self.plans, 'a99.md', 'doc 99\n')
        self.assertEqual(catalog.request_scan()['status'], 'started')
        partial = catalog.step(max_files=1)
        self.assertEqual((partial['status'], partial['phase']), ('pending', 'enumerate'))
        # A partial listing is never evidence of deletion and is not served.
        status = catalog.status()
        self.assertFalse(status['servable'])
        self.assertEqual(status['reason'], 'scan_pending')
        self.assertEqual(status['current']['scan_id'], snap1.scan_id)
        self.assertEqual(status['current']['files'], 13)
        self.assertIsNotNone(catalog.lookup(snap1, 'plans/a03.md'))
        with self.assertRaisesRegex(IndexContractError, 'index_stale'):
            catalog.require_servable()

        done = drive(catalog, max_files=3)[-1]
        self.assertEqual(done['status'], 'complete')
        published = done['published']
        self.assertEqual((published['added'], published['changed'],
                          published['deleted'], published['files']), (1, 1, 2, 12))
        snap2 = catalog.require_servable()
        self.assertIsNone(catalog.lookup(snap2, 'plans/a03.md'))
        self.assertIsNotNone(catalog.lookup(snap2, 'plans/a99.md'))
        self.assertEqual(self.catalog_manifest(catalog), self.port_manifest())

    def test_backlog_counts_drain_to_zero(self):
        for d in range(3):
            for f in range(4):
                self.write(self.plans, f'd{d}/f{f}.md', f'file {d} {f}\n')
        self.write(self.wiki, 'card.md', 'card\n')
        catalog = self.open_catalog(create=True)
        catalog.request_scan()
        history = []
        for _ in range(100):
            result = catalog.step(max_files=2)
            self.assertLessEqual(result['work']['units'], 2)
            status = catalog.status()
            history.append(status)
            if result['status'] == 'complete':
                break
            self.assertEqual(result['status'], 'pending')
            self.assertFalse(status['servable'])
            self.assertEqual(status['reason'], 'scan_pending')
        else:
            self.fail('scan did not complete')
        hashed = [s['active']['backlog']['files_hashed'] for s in history]
        self.assertEqual(hashed, sorted(hashed))
        self.assertTrue(any(s['active']['backlog']['files_pending'] > 0
                            for s in history[:-1]))
        final = history[-1]
        self.assertTrue(final['servable'])
        self.assertEqual(final['active']['phase'], 'complete')
        self.assertEqual(final['active']['backlog'],
                         {'dirs_known': 5, 'dirs_pending': 0, 'files_known': 13,
                          'files_hashed': 13, 'files_pending': 0})


class MutationTests(CatalogCase):
    def test_change_after_listing_blocks_and_keeps_snapshot(self):
        catalog, snap1 = self.complete_catalog()
        catalog.request_scan()
        self.assertEqual(catalog.step(max_files=1)['status'], 'pending')  # lists plans/
        self.write(self.plans, 'late.md', 'late arrival\n')
        self.assert_blocked_keeps(catalog, snap1, 'source_changed',
                                  'directory changed after listing: plans/')
        self.assertEqual(catalog.request_scan()['status'], 'started')
        done = drive(catalog)[-1]
        self.assertEqual(done['status'], 'complete')
        self.assertEqual(done['published']['added'], 1)

    def test_change_before_hash_blocks(self):
        catalog, snap1 = self.complete_catalog()
        catalog.request_scan()
        self.assertEqual(catalog.step(max_files=2)['status'], 'pending')
        hashed = catalog.step(max_files=2)
        self.assertEqual(hashed['work']['files_hashed'], 2)
        self.write(self.plans, 'a05.md', 'changed while the scan was pending\n')
        self.assert_blocked_keeps(catalog, snap1, 'source_changed',
                                  'changed before read: plans/a05.md')

    def test_change_after_hash_blocks_in_verification(self):
        catalog, snap1 = self.complete_catalog()
        catalog.request_scan()
        catalog.step(max_files=2)
        verifying = catalog.step(max_files=50, max_stats=1)
        self.assertEqual((verifying['status'], verifying['phase']),
                         ('pending', 'verify_dirs'))
        self.write(self.plans, 'a00.md', 'changed after it was hashed\n')
        self.assert_blocked_keeps(catalog, snap1, 'source_changed',
                                  'file changed after hashing: plans/a00.md')

    def test_replaced_root_blocks(self):
        catalog, snap1 = self.complete_catalog()
        catalog.request_scan()
        catalog.step(max_files=1)
        self.plans.rename(self.plans.with_name('plans-old'))
        self.plans.mkdir()
        self.write(self.plans, 'a00.md', 'impostor\n')
        self.assert_blocked_keeps(catalog, snap1, 'source_changed',
                                  'collection root replaced: plans')

    def test_restart_and_injected_crash_resume(self):
        for i in range(10):
            self.write(self.plans, f'a{i:02d}.md', f'doc {i}\n')
        self.write(self.wiki, 'card.md', 'card\n')
        catalog = self.open_catalog(create=True)
        catalog.request_scan()
        first = catalog.step(max_files=3)
        self.assertEqual(first['work']['files_hashed'], 2)
        catalog.close()

        reopened = self.open_catalog()
        before = reopened.status()['active']
        original = catalog_mod._hash_entry
        calls = []

        def flaky(*args):
            calls.append(args[2])
            if len(calls) == 2:
                raise RuntimeError('injected crash')
            return original(*args)

        catalog_mod._hash_entry = flaky
        self.addCleanup(setattr, catalog_mod, '_hash_entry', original)
        with self.assertRaisesRegex(RuntimeError, 'injected crash'):
            reopened.step(max_files=50)
        self.assertEqual(reopened.status()['active'], before)  # whole step rolled back
        catalog_mod._hash_entry = original
        reopened.close()

        resumed = self.open_catalog()
        results = drive(resumed)
        self.assertEqual(results[-1]['status'], 'complete')
        # The durable cursor resumes after the two committed hashes; nothing
        # from the failed step is committed or hashed twice.
        self.assertEqual(sum(r['work']['files_hashed'] for r in results), 9)
        self.assertEqual(self.catalog_manifest(resumed), self.port_manifest())

    def test_duplicate_requests_and_busy_writer(self):
        for i in range(6):
            self.write(self.plans, f'a{i:02d}.md', f'doc {i}\n')
        self.write(self.wiki, 'card.md', 'card\n')
        first = self.open_catalog(create=True)
        second = self.open_catalog()
        started = first.request_scan()
        self.assertEqual(started['status'], 'started')
        coalesced = second.request_scan()
        self.assertEqual(coalesced, {'status': 'coalesced', 'scan_id': started['scan_id']})

        first.db.execute('BEGIN IMMEDIATE')  # a concurrent writer mid-step
        try:
            self.assertEqual(second.step()['status'], 'busy')
            self.assertEqual(second.request_scan()['status'], 'busy')
        finally:
            first.db.rollback()

        instances = (first, second)
        hashed = 0
        final = None
        for i in range(200):
            result = instances[i % 2].step(max_files=2)
            hashed += result['work']['files_hashed']
            if result['status'] == 'complete':
                final = result
                break
            self.assertEqual(result['status'], 'pending')
        self.assertIsNotNone(final)
        self.assertEqual(hashed, 7)  # alternating writers never duplicate work
        # The coalesced request chains one follow-up scan; serving stays closed.
        self.assertIn('next_scan_id', final)
        status = first.status()
        self.assertFalse(status['servable'])
        self.assertEqual(status['reason'], 'scan_pending')
        follow_up = drive(second)[-1]
        self.assertEqual(follow_up['status'], 'complete')
        self.assertNotIn('next_scan_id', follow_up)
        self.assertEqual((follow_up['published']['added'], follow_up['published']['changed'],
                          follow_up['published']['deleted']), (0, 0, 0))
        self.assertTrue(first.status()['servable'])


class ValidationTests(CatalogCase):
    def test_invalid_source_blocks_and_never_replaces_snapshot(self):
        catalog, snap1 = self.complete_catalog()
        outside = self.write(self.base, 'outside.md', 'outside\n')
        link = self.plans / 'link.md'
        os.symlink(outside, link)
        catalog.request_scan()
        self.assert_blocked_keeps(catalog, snap1, 'source_invalid', 'symlink: plans/link.md')
        link.unlink()

        # Hidden entries, denied directories and non-text files stay outside
        # the contract; non-text symlinks (file or directory) are ignored.
        (self.plans / '.hidden.md').write_bytes(b'\xff')
        (self.plans / 'blob.bin').write_bytes(b'\xff')
        (self.plans / 'notes.txt').write_text('plain notes\n', encoding='utf-8')
        hidden_dir = self.plans / '.git'
        hidden_dir.mkdir()
        os.symlink(outside, hidden_dir / 'link.md')
        for denied in ('node_modules', 'build', 'vendor'):
            self.write(self.plans, f'{denied}/pkg/README.md', '# dep\n')
        os.symlink(self.plans / 'blob.bin', self.plans / 'link.bin')
        os.symlink(self.base, self.plans / 'linkdir', target_is_directory=True)
        for cache in ('Library', 'Temp', 'obj'):
            self.write(self.plans, f'{cache}/PackageCache/README.md', '# cache\n')
        # A raw source-text file that is not UTF-8 is not a source: skipped, counted.
        (self.plans / 'bad.md').write_bytes(b'ok \xff\xfe')
        catalog.request_scan()
        steps = drive(catalog)
        done = steps[-1]
        self.assertEqual(done['status'], 'complete')
        self.assertEqual(done['published']['files'], 12)
        self.assertEqual(sum(step['work']['undecodable_skipped'] for step in steps), 1)
        self.assertEqual(self.catalog_manifest(catalog), self.port_manifest())
        source = scan_collections(self.roots, SCOPE, self.index, max_files=1000,
                                  max_bytes=20_000_000)
        self.assertEqual(source.undecodable, ('plans/bad.md',))
        self.assertNotIn('plans/bad.md', source.by_path)

    def test_wiki_only_catalog_blocks_on_invalid_utf8(self):
        roots = {'wiki': str(self.wiki)}
        self.write(self.wiki, 'card.md', '# Card\nwiki card\n')
        (self.wiki / 'bad.md').write_bytes(b'ok \xff\xfe')
        catalog = self.open_catalog(create=True, roots=roots, wiki_only=True)
        self.assertEqual(catalog.request_scan()['status'], 'started')
        result = drive(catalog)[-1]
        self.assertEqual(result['status'], 'blocked', result)
        self.assertEqual(result['blocked']['reason'], 'source_invalid')
        self.assertIn('invalid UTF-8: wiki/bad.md', result['blocked']['detail'])
        with self.assertRaisesRegex(IndexContractError, 'index_stale'):
            scan_collections(roots, SCOPE, self.index, wiki_only=True)

    def test_binding_and_root_validation(self):
        catalog, _snap = self.complete_catalog()
        with self.assertRaisesRegex(IndexContractError, 'catalog_binding_mismatch'):
            self.open_catalog(roots={'plans': str(self.plans)})
        with self.assertRaisesRegex(IndexContractError, 'catalog_binding_mismatch'):
            self.open_catalog(scope='other-scope')
        with self.assertRaisesRegex(IndexContractError, 'catalog_already_exists'):
            self.open_catalog(create=True)
        with self.assertRaisesRegex(IndexContractError, 'index_stale'):
            self.open_catalog(create=True, name='overlap.sqlite',
                              roots={'a': str(self.plans), 'b': str(self.plans / 'sub')})
        with self.assertRaisesRegex(IndexContractError, 'index_stale'):
            self.open_catalog(create=True, name='alias.sqlite',
                              roots={'plans': str(self.plans) + '/../plans'})
        with self.assertRaisesRegex(ValueError, 'invalid collection name'):
            self.open_catalog(create=True, name='name.sqlite',
                              roots={'.hidden': str(self.plans)})
        self.assertFalse((self.state / 'overlap.sqlite').exists())
        (self.state / 'garbage.sqlite').write_bytes(b'not a sqlite database' * 64)
        with self.assertRaisesRegex(IndexContractError, 'catalog_unreadable'):
            self.open_catalog(name='garbage.sqlite')
        self.assertTrue(catalog.status()['servable'])

    def test_oversize_file_defers_until_budget_fits(self):
        self.write(self.plans, 'big.md', sized('# Big\n', 3000))
        self.write(self.plans, 'small.md', 'small\n')
        catalog = self.open_catalog(create=True)
        catalog.request_scan()
        deferred = drive(catalog, max_bytes=1000)[-1]
        self.assertEqual(deferred['status'], 'deferred')
        self.assertEqual(deferred['deferred'], {'reason': 'file_exceeds_step_bytes',
                                                'path': 'plans/big.md', 'size': 3000})
        status = catalog.status()
        self.assertFalse(status['servable'])
        self.assertEqual(status['reason'], 'scan_pending')
        self.assertEqual(status['active']['deferred_path'], 'plans/big.md')
        again = catalog.step(max_bytes=1000)
        self.assertEqual((again['status'], again['work']['files_hashed']), ('deferred', 0))
        done = drive(catalog, max_bytes=4000)[-1]
        self.assertEqual(done['status'], 'complete')
        self.assertIsNone(catalog.status()['active']['deferred_reason'])
        self.assertEqual(self.catalog_manifest(catalog), self.port_manifest())


class ServingTests(CatalogCase):
    def test_pending_blocked_or_modified_state_is_not_served(self):
        alpha = self.write(self.plans, 'alpha.md', '# Alpha\nalpha topic body\n')
        self.write(self.plans, 'beta.md', '# Beta\nbeta topic body\n')
        self.write(self.wiki, 'card.md', '# Card\nalpha wiki card\n')
        catalog = self.open_catalog(create=True)
        catalog.request_scan()
        self.assertEqual(drive(catalog)[-1]['status'], 'complete')
        self.publish(catalog, 'g1', None)
        rows = self.search(catalog, 'alpha')
        self.assertEqual(sorted(row['path'] for row in rows),
                         ['plans/alpha.md', 'wiki/card.md'])
        self.assertTrue(all(row['snapshot'] == catalog.require_servable().scan_id
                            for row in rows))
        by_path = {row['path']: row['body'] for row in rows}
        self.assertEqual(by_path['plans/alpha.md'], alpha.read_text(encoding='utf-8'))

        # A pending newer scan: no search, stage or publish against the old snapshot.
        catalog.request_scan()
        with self.assertRaisesRegex(IndexContractError, 'index_stale'):
            self.search(catalog, 'alpha')
        with self.assertRaisesRegex(IndexContractError, 'index_stale'):
            stage_from_catalog(self.index, catalog, 'g-pending', base='g1',
                               claim_digest=CLAIM, claim_epoch=0)
        with self.assertRaisesRegex(IndexContractError, 'index_stale'):
            publish_from_catalog(self.index, catalog, 'g1', 'g-pending', 'claim-pending',
                                 claim_digest=CLAIM, claim_epoch=0)
        self.assertEqual(drive(catalog)[-1]['status'], 'complete')
        self.assertEqual(len(self.search(catalog, 'alpha')), 2)

        # A blocked newer scan.
        link = self.plans / 'link.md'
        os.symlink(alpha, link)
        catalog.request_scan()
        self.assertEqual(drive(catalog)[-1]['status'], 'blocked')
        self.assertEqual(catalog.status()['reason'], 'scan_blocked')
        with self.assertRaisesRegex(IndexContractError, 'index_stale'):
            self.search(catalog, 'alpha')
        link.unlink()
        catalog.request_scan()
        self.assertEqual(drive(catalog)[-1]['status'], 'complete')
        self.assertEqual(len(self.search(catalog, 'alpha')), 2)

        # Out-of-band edit with no rescan request: returned row bytes no longer match.
        alpha.write_text('# Alpha\nalpha topic changed body\n', encoding='utf-8')
        with self.assertRaisesRegex(IndexContractError, 'index_stale'):
            self.search(catalog, 'alpha')

        # After a complete rescan the catalog no longer matches the published seal.
        catalog.request_scan()
        done = drive(catalog)[-1]
        self.assertEqual(done['published']['changed'], 1)
        with self.assertRaisesRegex(IndexContractError, 'index_stale'):
            self.search(catalog, 'alpha')
        self.publish(catalog, 'g2', 'g1')
        rows = self.search(catalog, 'changed')
        self.assertEqual([row['path'] for row in rows], ['plans/alpha.md'])
        self.assertEqual(rows[0]['body'], '# Alpha\nalpha topic changed body\n')

    def test_out_of_band_unreturned_change_is_not_served(self):
        self.write(self.plans, 'alpha.md', '# Alpha\nalpha topic body\n')
        beta = self.write(self.plans, 'sub/beta.md', '# Beta\nbeta topic body\n')
        self.write(self.wiki, 'card.md', '# Card\nwiki card\n')
        catalog = self.open_catalog(create=True)
        catalog.request_scan()
        self.assertEqual(drive(catalog)[-1]['status'], 'complete')
        self.publish(catalog, 'g1', None)
        self.assertEqual([r['path'] for r in self.search(catalog, 'alpha')],
                         ['plans/alpha.md'])
        # Budget below the snapshot's 3 dirs + 3 files fails closed, not partial.
        token, _gen = self.index.pin(SCOPE)
        try:
            with self.assertRaisesRegex(IndexContractError, 'index_stale'):
                search_catalog_verified(self.index, catalog, token, 'alpha', 5,
                                        max_verify_stats=5)
            self.assertEqual(len(search_catalog_verified(self.index, catalog, token,
                                                         'alpha', 5, max_verify_stats=6)), 1)
        finally:
            self.index.release(token)

        def assert_not_served(stage_detail='catalog not fresh'):
            # No rescan was requested; the returned alpha row itself is unchanged.
            self.assertTrue(catalog.status()['servable'])
            calls = (
                (lambda: self.search(catalog, 'alpha'), 'catalog not fresh'),
                (lambda: stage_from_catalog(self.index, catalog, 'g-x', base='g1',
                                            claim_digest=CLAIM, claim_epoch=0),
                 stage_detail),
                (lambda: publish_from_catalog(self.index, catalog, 'g1', 'g-x', 'claim-x',
                                              claim_digest=CLAIM, claim_epoch=0),
                 'catalog not fresh'))
            for call, detail in calls:
                with self.assertRaisesRegex(IndexContractError, 'index_stale') as caught:
                    call()
                self.assertIn(detail, str(caught.exception.__cause__))

        original = beta.read_bytes()
        beta.write_text('# Beta\nbeta now mentions alpha\n', encoding='utf-8')
        assert_not_served(stage_detail='plans/sub/beta.md')  # rehash fails first
        beta.write_bytes(original)  # same bytes, new signature: still not served
        assert_not_served()
        catalog.request_scan()
        self.assertEqual(drive(catalog)[-1]['status'], 'complete')
        self.assertEqual(len(self.search(catalog, 'alpha')), 1)

        added = self.write(self.plans, 'sub/gamma.md', '# Gamma\nalpha gamma\n')
        assert_not_served()
        added.unlink()
        catalog.request_scan()
        self.assertEqual(drive(catalog)[-1]['status'], 'complete')
        self.assertEqual(len(self.search(catalog, 'alpha')), 1)

        (self.wiki / 'card.md').unlink()
        assert_not_served(stage_detail='wiki/card.md')


class ProofTests(CatalogCase):
    """Query-time freshness is a full byte proof, never a stat signature."""

    def published_catalog(self):
        catalog, snap = self.complete_catalog()
        self.publish(catalog, 'g1', None)
        self.assertEqual([r['path'] for r in self.search(catalog, 'card')],
                         ['wiki/card.md'])
        return catalog, snap

    def test_proof_budget_boundaries_and_pending_never_deletes(self):
        catalog, snap = self.published_catalog()
        self.assertEqual(snap.files, 11)
        exact = {'max_proof_files': snap.files, 'max_proof_bytes': snap.bytes}
        self.assertEqual(len(self.search(catalog, 'card', **exact)), 1)
        cases = (({**exact, 'max_proof_files': snap.files - 1}, (1, 0)),
                 ({**exact, 'max_proof_bytes': snap.bytes - 1}, (0, 1)))
        for budget, over in cases:
            with self.forbid_source_and_search():
                with self.assertRaises(CatalogPending) as caught:
                    self.search(catalog, 'card', **budget)
            detail = caught.exception.detail
            self.assertEqual((detail['files_over'], detail['bytes_over']), over)
        # A pending query outcome never changes or shrinks the snapshot.
        status = catalog.status()
        self.assertTrue(status['servable'])
        self.assertEqual((status['current']['scan_id'], status['current']['files']),
                         (snap.scan_id, 11))
        self.assertIsNotNone(catalog.lookup(snap, 'plans/a09.md'))

        # A partial rescan is pending catalog state (plain index_stale), and its
        # partial listing deletes nothing from the current snapshot.
        (self.plans / 'a09.md').unlink()
        catalog.request_scan()
        self.assertEqual(catalog.step(max_files=1)['status'], 'pending')
        self.assert_stale(lambda: self.search(catalog, 'card', **exact), 'scan_pending')
        self.assertEqual(catalog.status()['current']['files'], 11)
        self.assertIsNotNone(catalog.lookup(snap, 'plans/a09.md'))
        done = drive(catalog)[-1]
        self.assertEqual((done['published']['deleted'], done['published']['files']),
                         (1, 10))

    def test_spoofed_same_size_mtime_rewrite_of_unreturned_file_fails_closed(self):
        catalog, snap = self.published_catalog()
        target = self.plans / 'a07.md'
        before = os.stat(target)
        with open(target, 'r+b') as handle:
            handle.write(b'# A7\nplain DOC 7\n')
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = os.stat(target)
        self.assertEqual((after.st_ino, after.st_size, after.st_mtime_ns),
                         (before.st_ino, before.st_size, before.st_mtime_ns))
        self.spoof_signatures(catalog)
        catalog.verify_unchanged(snap)  # the stat hint is blind to this rewrite
        self.assert_stale(lambda: self.search(catalog, 'card'),
                          'source bytes or membership differ from catalog snapshot')
        # The original bytes prove fresh again; the proof judges content only.
        target.write_bytes(b'# A7\nplain doc 7\n')
        self.assertEqual(len(self.search(catalog, 'card')), 1)

    def test_spoofed_directory_add_and_delete_fail_closed(self):
        catalog, snap = self.published_catalog()
        self.spoof_signatures(catalog)
        added = self.write(self.plans, 'zz.md', '# ZZ\nunrelated\n')
        catalog.verify_unchanged(snap)  # spoofed directory signature passes
        self.assert_stale(lambda: self.search(catalog, 'card'),
                          'source bytes or membership differ from catalog snapshot')
        added.unlink()
        self.assertEqual(len(self.search(catalog, 'card')), 1)

        # With the hint disabled entirely, the proof alone still sees a delete.
        original = catalog_mod.LayaSourceCatalog.verify_unchanged
        catalog_mod.LayaSourceCatalog.verify_unchanged = lambda *_a, **_k: None
        self.addCleanup(setattr, catalog_mod.LayaSourceCatalog, 'verify_unchanged',
                        original)
        (self.plans / 'a03.md').unlink()
        self.assert_stale(lambda: self.search(catalog, 'card'),
                          'source bytes or membership differ from catalog snapshot')

    def test_actual_source_over_budget_despite_snapshot_fails_closed(self):
        catalog, snap = self.published_catalog()
        exact = {'max_proof_files': snap.files, 'max_proof_bytes': snap.bytes}
        self.spoof_signatures(catalog)
        added = self.write(self.plans, 'zz.md', '# ZZ\nunrelated\n')
        self.assert_stale(lambda: self.search(catalog, 'card', **exact),
                          'too many files')
        added.unlink()
        self.assertEqual(len(self.search(catalog, 'card', **exact)), 1)

        with open(self.plans / 'a07.md', 'ab') as handle:
            handle.write(b'grown past the snapshot byte count\n')
        catalog.verify_unchanged(snap)  # spoofed: the hint misses the growth
        self.assert_stale(lambda: self.search(catalog, 'card', **exact), 'oversize')


if __name__ == '__main__':
    unittest.main()

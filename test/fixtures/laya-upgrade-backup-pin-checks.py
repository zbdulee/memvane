#!/usr/bin/env python3
"""Bundle upgrade backups: journal copy, store pins, retention and stage events.

Synthetic stores under a temporary HOME; no model, host or operating project.
The 0.44.15 full-copy writer is frozen here (``legacy_backup``) so backups an
older bundle left behind stay covered by the current verify and restore code.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'core'))
sys.path.insert(0, str(ROOT / 'skills' / 'logs' / 'scripts'))
import event_log  # noqa: E402
import laya_initial_build as build  # noqa: E402
import laya_install_journal as journal  # noqa: E402
import laya_inactive_upgrade as upgrade  # noqa: E402
from laya_index_a import _query_only_uri  # noqa: E402

V1 = 'laya-inactive-upgrade-backup-v1'
V2 = 'laya-inactive-upgrade-backup-v2'
STORE_KEYS = ('candidateDb', 'queueDb', 'catalogDb', 'bindingStore')


def raw(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def legacy_backup(identity, old_record, old_route, old_attestation, counts):
    """The 0.44.15 ``_backup``: online copies of every store, integrity-checked."""
    state = Path(identity['stateRoot'])
    final = state / (upgrade.BACKUP + os.urandom(12).hex())
    staged = Path(tempfile.mkdtemp(prefix='.inactive-upgrade-backup-', dir=state))
    dbs = {key: identity[key] for key in STORE_KEYS}
    sidecar = state / 'laya-incremental-docs.sqlite'
    if sidecar.exists():
        dbs['incrementalDocs'] = str(sidecar)
    dbs['journal'] = identity['journalPath']
    digests = {}
    for key, path in dbs.items():
        target = staged / (key + '.sqlite')
        with closing(sqlite3.connect(_query_only_uri(path), uri=True)) as source:
            source.execute('PRAGMA query_only=ON')
            with closing(sqlite3.connect(target)) as copy:
                source.backup(copy)
                assert copy.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        os.chmod(target, 0o600)
        digests[key] = upgrade._hash_private(target)
    receipt = {'schema': V1, 'record': old_record,
               'routeSha256': hashlib.sha256(old_route).hexdigest(),
               'attestationSha256': hashlib.sha256(old_attestation).hexdigest(),
               'counts': counts, 'dbSha256': digests}
    manifest = staged / 'manifest.json'
    manifest.write_bytes(upgrade._canonical(receipt))
    manifest.chmod(0o600)
    os.rename(staged, final)
    return final


class PinCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='laya-backup-pin-')
        self.addCleanup(tmp.cleanup)
        # The event log refuses symlinked ancestors (macOS /var -> /private/var).
        base = Path(tmp.name).resolve()
        self.home = base / 'home'
        self.home.mkdir(mode=0o700)
        patch = mock.patch.dict(os.environ, {'HOME': str(self.home), 'QMD_SANDBOX': '1'})
        patch.start()
        self.addCleanup(patch.stop)
        self.project = base / 'project'
        (self.project / 'wiki').mkdir(parents=True)
        self.state = self.home / '.local' / 'state' / 'memvane' / ('a' * 32)
        self.state.mkdir(mode=0o700, parents=True)
        self.route = self.state / 'reviewed-route.json'
        self.attestation = self.state / journal.PRODUCTION_ATTESTATION
        self.old_route = raw({'pluginRoot': '/old', 'vectorProfile': 'same'})
        self.new_route = raw({'pluginRoot': '/new', 'vectorProfile': 'same'})
        self.old_att = raw({'bundle': 'old'})
        self.new_att = raw({'bundle': 'new'})
        for path, value in ((self.route, self.old_route), (self.attestation, self.old_att)):
            path.write_bytes(value)
            path.chmod(0o600)
        collections = [{'name': 'raw', 'role': 'raw', 'sourceRoot': str(self.project)},
                       {'name': 'wiki', 'role': 'wiki',
                        'sourceRoot': str(self.project / 'wiki')}]
        self.old = {'projectRoot': str(self.project), 'stateRoot': str(self.state),
                    'installId': 'a' * 32, 'scopeId': 'b' * 32,
                    'collections': collections,
                    'candidateDb': str(self.state / 'laya-build.sqlite'),
                    'queueDb': str(self.state / 'claims.sqlite'),
                    'catalogDb': str(self.state / 'catalog.sqlite'),
                    'bindingStore': str(self.state / 'bindings.sqlite'),
                    'dbIncarnation': 'c' * 32,
                    'journalPath': str(self.state / 'install-journal.sqlite'),
                    'routeFile': str(self.route),
                    'routeSha256': hashlib.sha256(self.old_route).hexdigest(),
                    'attestationSha256': hashlib.sha256(self.old_att).hexdigest(),
                    'vectorProfileId': 'd' * 64, 'originMode': 'fresh_wiki_only',
                    'proposalSha256': 'e' * 64, 'providerProposalSha256': 'f' * 64}
        self.new = {**self.old,
                    'routeSha256': hashlib.sha256(self.new_route).hexdigest(),
                    'attestationSha256': hashlib.sha256(self.new_att).hexdigest()}
        self.original = build.create_or_verify(self.old)
        self.counts = {'vectorChunkSets': 3, 'vectorChunks': 3, 'publishedDocuments': 0}
        for key in STORE_KEYS + ('journalPath',):
            self.make_store(Path(self.old[key]), rows=3 if key == 'candidateDb' else 1)
        self.rows = {'state': 'candidate_ready', 'pending': {'pluginRoot': '/old'},
                     'selected': None, 'previous': None, 'newWriteSeen': False,
                     'epoch': 1, 'pendingRouteSha256': self.old['routeSha256']}

    def make_store(self, path, rows=1):
        with closing(sqlite3.connect(path)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE vector_chunk_sets(id INTEGER PRIMARY KEY, body TEXT)')
            db.execute('CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute("INSERT INTO schema_meta VALUES('incarnation','c')")
            db.execute('CREATE TABLE sealed(k TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID')
            db.execute("INSERT INTO sealed VALUES('k','v')")
            db.execute("CREATE VIRTUAL TABLE docs_fts USING fts5(body)")
            db.execute('CREATE TABLE pins(token TEXT PRIMARY KEY, scope TEXT, gen TEXT)')
            db.executemany('INSERT INTO vector_chunk_sets(body) VALUES(?)',
                           (('row %d' % n,) for n in range(rows)))
            db.commit()
        path.chmod(0o600)

    def take(self):
        saved = upgrade._backup(self.old, self.original, self.old_route, self.old_att,
                                self.counts)
        return saved, self.intent(saved)

    def intent(self, saved):
        value = {'schema': 'laya-inactive-upgrade-intent-v1', 'oldRecord': self.original,
                 'oldRoute': self.old_route.decode(), 'oldAttestation': self.old_att.decode(),
                 'newRoute': self.new_route.decode(), 'newAttestation': self.new_att.decode(),
                 'newIdentity': self.new, 'counts': self.counts, 'backup': str(saved),
                 'reviewSha256': '1' * 64, 'newProof': {}}
        path = self.state / upgrade.INTENT
        if path.exists():
            path.unlink()
        build._write_json(path, value, replace=False, max_bytes=upgrade.INTENT_LIMIT)
        return value

    def patches(self):
        def cas(_path, _epoch, current, _proof, next_):
            self.assertEqual(self.rows['pendingRouteSha256'], hashlib.sha256(current).hexdigest())
            self.rows = {**self.rows, 'epoch': self.rows['epoch'] + 1,
                         'pendingRouteSha256': hashlib.sha256(next_).hexdigest()}
            return self.rows

        def preflight(_path):
            new = self.rows['pendingRouteSha256'] == self.new['routeSha256']
            return {'attestationSha256': self.new['attestationSha256'] if new else
                    self.old['attestationSha256'],
                    'pendingRouteSha256': self.rows['pendingRouteSha256']}
        return [mock.patch.object(upgrade, 'inspect_journal', lambda _p: self.rows),
                mock.patch.object(upgrade, 'cas_replace_inactive_candidate', cas),
                mock.patch.object(upgrade, 'preflight_product_candidate', preflight),
                mock.patch.object(upgrade, '_store_counts', lambda _i: self.counts),
                mock.patch.object(upgrade, '_counts_match',
                                  lambda i, expected: upgrade._store_counts(i) == expected)]

    def events(self, prefix=''):
        return [row for row in event_log.read(self.state, generations=False)
                if row.get('event') == 'bundle.upgrade' and
                str(row.get('outcome')).startswith(prefix)]


class BackupFormatTests(PinCase):
    def test_backup_copies_only_the_journal_and_pins_every_store(self):
        docs = self.state / 'laya-incremental-docs.sqlite'
        self.make_store(docs)
        hashed = []
        real_hash = upgrade._hash_private
        with mock.patch.object(upgrade, '_hash_private',
                               side_effect=lambda path, *a, **k: hashed.append(Path(path).name)
                               or real_hash(path, *a, **k)):
            saved, intent = self.take()
            # Apple's libsqlite3 keeps a persistent -wal/-shm beside a WAL copy.
            self.assertEqual(sorted(p.name for p in saved.iterdir()
                                    if not p.name.endswith(('-wal', '-shm'))),
                             ['journal.sqlite', 'manifest.json'])
            self.assertEqual(stat.S_IMODE(saved.stat().st_mode) & 0o077, 0)
            receipt = json.loads((saved / 'manifest.json').read_text())
            self.assertEqual(receipt['schema'], V2)
            self.assertEqual(set(receipt['dbSha256']), {'journal'})
            self.assertEqual(set(receipt['storePins']), set(STORE_KEYS) | {'incrementalDocs'})
            for key, pin in receipt['storePins'].items():
                path = docs if key == 'incrementalDocs' else Path(self.old[key])
                info = path.stat()
                self.assertEqual((pin['path'], pin['ino'], pin['dev']),
                                 (str(path), info.st_ino, info.st_dev), key)
                for field in ('size', 'mtimeNs', 'pageCount', 'freelistCount', 'pageSize',
                              'applicationId', 'userVersion', 'ddlSha256', 'contentSha256',
                              'tailSha256'):
                    self.assertIn(field, pin)
            hashed.clear()
            upgrade._verify_backup_full(intent)
            self.assertEqual(hashed, ['journal.sqlite'], 'only the journal copy is hashed')
            with upgrade.backup_proof_scope():
                upgrade._verify_backup(intent)
                upgrade._verify_backup(intent)
            upgrade._verify_store_pins(intent)

    def test_pins_ignore_benign_file_metadata_and_catch_content_or_replacement(self):
        saved, intent = self.take()
        candidate = Path(self.old['candidateDb'])
        # A read-only open, a WAL checkpoint or a touched mtime is no change.
        with closing(sqlite3.connect(candidate)) as db:
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        info = candidate.stat()
        os.utime(candidate, ns=(info.st_atime_ns, info.st_mtime_ns + 10 ** 9))
        # A recall's read lease row (v6 ``pins``) may be added, removed or left
        # behind by a crash; it is no index content.
        with closing(sqlite3.connect(candidate)) as db:
            db.executemany("INSERT INTO pins VALUES(?, 's', 'g')",
                           (('t%d' % n,) for n in range(400)))
            db.execute("DELETE FROM pins WHERE token<>'t0'")
            db.commit()
        upgrade._verify_store_pins(intent)
        # Settled intents keep verifying while the live store moves on.
        with closing(sqlite3.connect(candidate)) as db:
            db.execute("INSERT INTO vector_chunk_sets(body) VALUES('new')")
            db.commit()
        upgrade._verify_backup_full(intent)
        with self.assertRaisesRegex(Exception, 'inactive_upgrade_backup_changed'):
            upgrade._verify_store_pins(intent)
        self.assertTrue(upgrade._pins_stale(intent))
        # An update in a fully pinned identity table changes the pin.
        saved, intent = self.take()
        with closing(sqlite3.connect(Path(self.old['queueDb']))) as db:
            db.execute("UPDATE schema_meta SET value='other'")
            db.commit()
        with self.assertRaisesRegex(Exception, 'inactive_upgrade_backup_changed'):
            upgrade._verify_store_pins(intent)
        # Same bytes at a new inode is a replaced store.
        saved, intent = self.take()
        self.assertFalse(upgrade._pins_stale(intent))
        catalog = Path(self.old['catalogDb'])
        copy = catalog.with_name('catalog.copy')
        copy.write_bytes(catalog.read_bytes())
        copy.chmod(0o600)
        os.replace(copy, catalog)
        with self.assertRaisesRegex(Exception, 'inactive_upgrade_backup_changed'):
            upgrade._verify_store_pins(intent)
        # A sidecar that appears after the backup was never pinned.
        saved, intent = self.take()
        self.make_store(self.state / 'laya-incremental-docs.sqlite')
        with self.assertRaisesRegex(Exception, 'inactive_upgrade_backup_changed'):
            upgrade._verify_store_pins(intent)

    def test_unknown_or_malformed_receipts_refuse_with_a_fixed_code(self):
        saved, intent = self.take()
        manifest = saved / 'manifest.json'
        original = json.loads(manifest.read_text())

        def rewrite(value):
            manifest.write_bytes(upgrade._canonical(value))
            manifest.chmod(0o600)
        for broken in ({**original, 'schema': 'laya-inactive-upgrade-backup-v3'},
                       {**original, 'dbSha256': {**original['dbSha256'],
                                                 'candidateDb': '0' * 64}},
                       {**original, 'storePins': {k: v for k, v in
                                                  original['storePins'].items()
                                                  if k != 'queueDb'}},
                       {**original, 'storePins': {**original['storePins'],
                                                  'queueDb': {'ino': 'x'}}},
                       {k: v for k, v in original.items() if k != 'storePins'}):
            rewrite(broken)
            for call in (upgrade._verify_backup_full, upgrade._verify_store_pins):
                with self.assertRaisesRegex(Exception, 'inactive_upgrade_backup_invalid'):
                    call(intent)
            with upgrade.backup_proof_scope():
                with self.assertRaisesRegex(Exception, 'inactive_upgrade_backup_invalid'):
                    upgrade._verify_backup(intent)
        rewrite(original)
        upgrade._verify_backup_full(intent)

    def test_full_copy_backup_of_an_older_bundle_still_verifies_and_restores(self):
        saved = legacy_backup(self.old, self.original, self.old_route, self.old_att,
                              self.counts)
        intent = self.intent(saved)
        upgrade._verify_backup_full(intent)
        with upgrade.backup_proof_scope():
            upgrade._verify_backup(intent)
            upgrade._verify_backup(intent)
        # A full copy carries no pins: restore keeps its counts-only recheck.
        with closing(sqlite3.connect(Path(self.old['candidateDb']))) as db:
            db.execute("INSERT INTO vector_chunk_sets(body) VALUES('synced')")
            db.commit()
        upgrade._verify_store_pins(intent)
        self.assertFalse(upgrade._pins_stale(intent))
        patches = self.patches()
        for patch in patches:
            patch.start()
        try:
            with mock.patch.object(upgrade, '_store_counts',
                                   return_value={**self.counts, 'vectorChunkSets': 0}):
                with self.assertRaisesRegex(Exception, 'inactive_upgrade_recheck_failed'):
                    upgrade._switch(intent, {}, {})
            restored = upgrade._restore(intent, {}, {})
        finally:
            for patch in patches:
                patch.stop()
        self.assertEqual(restored['identity'], self.old)
        self.assertFalse((self.state / upgrade.INTENT).exists())
        self.assertFalse(saved.exists(), 'a retired intent drops its own backup')


class RestoreTests(PinCase):
    def test_restore_fails_closed_when_a_pinned_store_changed(self):
        saved, intent = self.take()
        patches = self.patches()
        for patch in patches:
            patch.start()
        try:
            with mock.patch.object(upgrade, '_store_counts',
                                   return_value={**self.counts, 'vectorChunkSets': 0}):
                with self.assertRaisesRegex(Exception, 'inactive_upgrade_recheck_failed'):
                    upgrade._switch(intent, {}, {})
            self.assertEqual(build.read(self.state)['phase'], 'upgrading')
            with closing(sqlite3.connect(Path(self.old['bindingStore']))) as db:
                db.execute("INSERT INTO vector_chunk_sets(body) VALUES('fenced write')")
                db.commit()
            with self.assertRaisesRegex(Exception, 'inactive_upgrade_backup_changed'):
                upgrade._restore(intent, {}, {})
            # Metadata is back on the old bundle but the fence and intent stay.
            self.assertEqual(self.route.read_bytes(), self.old_route)
            self.assertEqual(build.read(self.state)['phase'], 'upgrading')
            self.assertTrue((self.state / upgrade.INTENT).exists())
            self.assertTrue(saved.is_dir())
        finally:
            for patch in patches:
                patch.stop()

    def test_restore_of_unchanged_stores_releases_and_drops_its_backup(self):
        saved, intent = self.take()
        patches = self.patches()
        for patch in patches:
            patch.start()
        try:
            with mock.patch.object(upgrade, '_store_counts',
                                   return_value={**self.counts, 'vectorChunkSets': 0}):
                with self.assertRaisesRegex(Exception, 'inactive_upgrade_recheck_failed'):
                    upgrade._switch(intent, {}, {})
            restored = upgrade._restore(intent, {}, {})
        finally:
            for patch in patches:
                patch.stop()
        self.assertEqual((restored['identity'], restored['phase']),
                         (self.old, self.original['phase']))
        self.assertFalse(saved.exists())
        self.assertEqual(len(list(self.state.glob('inactive-upgrade-*-restored.json'))), 1)


class RetentionTests(PinCase):
    def test_completed_switch_drops_its_backup_after_the_receipt(self):
        saved, intent = self.take()
        order = []
        real_drop = upgrade._drop_backup

        def drop(state, backup):
            order.append((os.path.exists(Path(state) / upgrade.INTENT),
                          len(list(Path(state).glob('inactive-upgrade-*-complete.json')))))
            return real_drop(state, backup)
        patches = self.patches()
        for patch in patches:
            patch.start()
        try:
            with mock.patch.object(upgrade, '_drop_backup', drop):
                upgrade._switch(intent, {}, {})
        finally:
            for patch in patches:
                patch.stop()
        self.assertEqual(order, [(False, 1)], 'receipt is durable before the delete')
        self.assertFalse(saved.exists())
        names = [row.get('outcome') for row in self.events()]
        self.assertIn('backup.deleted', names)
        self.assertIn('activate.done', names)

    def test_a_backup_a_live_intent_names_or_an_unsafe_path_is_kept(self):
        saved, intent = self.take()
        other = self.state / 'bound-upgrade-intent.json'
        build._write_json(other, {'schema': 'x', 'backup': str(saved)}, replace=False,
                          max_bytes=upgrade.INTENT_LIMIT)
        upgrade._drop_backup(self.state, str(saved))
        self.assertTrue(saved.is_dir())
        other.unlink()
        outside = self.home / (upgrade.BACKUP + 'outside')
        outside.mkdir(mode=0o700)
        upgrade._drop_backup(self.state, str(outside))
        self.assertTrue(outside.is_dir())
        link = self.state / (upgrade.BACKUP + 'link')
        link.symlink_to(saved)
        upgrade._drop_backup(self.state, str(link))
        self.assertTrue(saved.is_dir() and link.is_symlink())
        link.unlink()
        upgrade._drop_backup(self.state, str(saved))  # the inactive intent names it
        self.assertTrue(saved.is_dir())
        (self.state / upgrade.INTENT).unlink()
        upgrade._drop_backup(self.state, str(saved))
        self.assertFalse(saved.exists())
        kept = [row for row in self.events('backup.kept')]
        self.assertEqual(len(kept), 4)

    def test_active_archive_drops_the_settled_intent_backup_after_the_rename(self):
        import laya_active_upgrade as active
        saved, intent = self.take()
        (self.state / upgrade.INTENT).unlink()
        path = self.state / active.INTENT
        build._write_json(path, intent, replace=False, max_bytes=active.LIMIT)
        target = self.state / 'active-upgrade-fixture-settled.json'
        seen = []
        real_drop = upgrade._drop_backup

        def drop(state, backup):
            seen.append((path.exists(), target.exists()))
            return real_drop(state, backup)
        with mock.patch.object(active, 'finished_intent_receipt', return_value=target), \
                mock.patch.object(upgrade, '_drop_backup', drop):
            active._archive_finished_intent(self.state, self.project, intent)
        self.assertEqual(seen, [(False, True)])
        self.assertFalse(saved.exists())


class EventTests(PinCase):
    def test_backup_and_verify_stages_carry_counts_bytes_and_operation(self):
        with upgrade.upgrade_events(operation='a' * 32, kind='inactive'):
            saved, intent = self.take()
            upgrade._verify_backup_full(intent)
        rows = {row['outcome']: row for row in self.events()}
        for stage in ('backup.start', 'backup.done', 'verify.start', 'verify.done'):
            self.assertIn(stage, rows)
            self.assertEqual((rows[stage]['operation'], rows[stage]['kind']),
                             ('a' * 32, 'inactive'))
            self.assertEqual(rows[stage]['backup'], saved.name)
        done = rows['backup.done']
        self.assertEqual((done['copies'], done['pins']), (1, 4))
        self.assertEqual(done['bytes'], (saved / 'journal.sqlite').stat().st_size)
        self.assertIsInstance(done['ms'], int)
        self.assertEqual(rows['verify.done']['files'], 1)
        text = json.dumps([row for row in self.events()])
        self.assertNotIn('row 0', text)

    def test_long_hashing_emits_periodic_progress(self):
        saved = legacy_backup(self.old, self.original, self.old_route, self.old_att,
                              self.counts)
        intent = self.intent(saved)
        clock = iter(range(0, 10 ** 6, 31))
        with mock.patch.object(upgrade.time, 'monotonic', lambda: next(clock)):
            upgrade._verify_backup_full(intent)
        progress = self.events('verify.progress')
        self.assertGreater(len(progress), 0)
        total = sum(path.stat().st_size for path in saved.glob('*.sqlite'))
        self.assertTrue(all(row['bytesTotal'] == total for row in progress))
        done = [row['bytesDone'] for row in progress]
        self.assertEqual(done, sorted(done))
        self.assertLessEqual(done[-1], total)

    def test_logs_summary_keeps_the_last_upgrade_result_over_stage_rows(self):
        import logs
        for outcome in ('started', 'backup.start', 'backup.done', 'complete',
                        'backup.deleted'):
            moves = {} if '.' in outcome else {'from': 'old-root', 'to': 'new-root'}
            event_log.emit('bundle.upgrade', state_root=self.state, outcome=outcome,
                           operation='b' * 32, bytesDone=5, bytesTotal=9, **moves)
        rows = event_log.read(self.state)
        import argparse
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            logs.summary(rows, argparse.Namespace(hours=24.0, limit=20, json=False))
        self.assertRegex(out.getvalue(), r'last upgrade: .* bundle\.upgrade complete')
        with redirect_stdout(io.StringIO()) as view:
            logs.main(['upgrades', '--state-root', str(self.state)])
        self.assertIn('backup.done', view.getvalue())
        self.assertNotIn('None -> None', view.getvalue())


class StoreCheckTests(PinCase):
    def real_stores(self):
        from laya_index_a import LayaIndexA
        from laya_index_capacity import GenerationBindingStore
        from laya_index_claim_queue import ClaimQueue
        from laya_index_host_route import wiki_index_roots
        from laya_source_catalog import LayaSourceCatalog
        for key in STORE_KEYS:
            for suffix in ('', '-wal', '-shm'):
                Path(self.old[key] + suffix).unlink(missing_ok=True)
        index = LayaIndexA(self.old['candidateDb'], create=True)
        incarnation = index.incarnation()
        index.close()
        identity = {**self.old, 'dbIncarnation': incarnation}
        roots = wiki_index_roots(identity['collections'])
        ClaimQueue.create(identity['queueDb'], index_incarnation=incarnation).close()
        LayaSourceCatalog(identity['catalogDb'], roots, identity['scopeId'], create=True).close()
        GenerationBindingStore(identity['bindingStore'], scope=identity['scopeId'],
                               db_incarnation=incarnation, roots=roots, create=True).close()
        for key in STORE_KEYS:
            Path(identity[key]).chmod(0o600)
        return identity

    def test_store_check_reads_metadata_only(self):
        identity = self.real_stores()
        statements = []
        real_connect = sqlite3.connect

        def traced(*args, **kwargs):
            db = real_connect(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db
        with mock.patch.object(upgrade.sqlite3, 'connect', traced):
            counts = upgrade._store_counts(identity)
            upgrade._store_pin(identity['candidateDb'])
        self.assertEqual(counts['countsSchema'], upgrade.COUNTS_SCHEMA)
        scans = [s for s in statements if 'quick_check' in s or 'integrity_check' in s
                 or 'COUNT(' in s.upper()]
        self.assertEqual(scans, [])
        self.assertTrue(upgrade._counts_match(identity, counts))

    def test_counts_of_an_older_bundle_still_match_through_the_legacy_inspection(self):
        identity = self.real_stores()
        legacy = upgrade._legacy_store_counts(identity)
        self.assertEqual(set(legacy), {'vectorChunkSets', 'vectorChunks',
                                       'publishedDocuments', 'storeSchemas'})
        self.assertNotEqual(upgrade._store_counts(identity), legacy)
        self.assertTrue(upgrade._counts_match(identity, legacy))
        self.assertFalse(upgrade._counts_match(identity, {**legacy, 'vectorChunks': 9}))
        current = upgrade._store_counts(identity)
        self.assertFalse(upgrade._counts_match(identity, {**current,
                                                          'vectorChunksTail': 9}))


if __name__ == '__main__':
    unittest.main()

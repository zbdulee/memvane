#!/usr/bin/env python3
"""Synthetic inactive metadata switch: no model, host, or operating project."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import laya_initial_build as build
import laya_install_journal as journal
import laya_inactive_upgrade as upgrade
import runpy

# The 0.44.15 full-copy writer: backups older bundles left behind.
legacy_backup = runpy.run_path(str(Path(__file__).resolve().parent /
                                   'laya-upgrade-backup-pin-checks.py'))['legacy_backup']


def raw(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def strip_closed_wal_sidecars(case, path):
    """Leave ``path`` as a stock SQLite build's last close does: WAL header, no -wal/-shm."""
    path = Path(path)
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        case.assertEqual(db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0], 0)
    for suffix in ('-wal', '-shm'):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            if suffix == '-wal':
                case.assertEqual(sidecar.stat().st_size, 0)
            sidecar.unlink()
    case.assertEqual(tuple(path.read_bytes()[18:20]), (2, 2))
    case.assertEqual([p.name for p in path.parent.iterdir() if p.name.startswith(path.name + '-')],
                     [])


class UpgradeCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='laya-inactive-upgrade-')
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / 'home'
        self.home.mkdir(mode=0o700)
        patch = mock.patch.dict(os.environ, {'HOME': str(self.home), 'QMD_SANDBOX': '1'})
        patch.start()
        self.addCleanup(patch.stop)
        self.project = Path(tmp.name) / 'project'
        self.project.mkdir()
        self.state = self.home / '.local' / 'state' / 'laya' / ('a' * 32)
        self.state.mkdir(mode=0o700, parents=True)
        self.route = self.state / 'reviewed-route.json'
        self.attestation = self.state / journal.PRODUCTION_ATTESTATION
        self.old_route = raw({'pluginRoot': '/old', 'vectorProfile': 'same'})
        self.new_route = raw({'pluginRoot': '/new', 'vectorProfile': 'same'})
        self.old_att = raw({'bundle': 'old'})
        self.new_att = raw({'bundle': 'new'})
        self.route.write_bytes(self.old_route)
        self.attestation.write_bytes(self.old_att)
        self.route.chmod(0o600)
        self.attestation.chmod(0o600)
        candidate = self.state / 'laya-build.sqlite'
        collections = [{'name': 'raw', 'role': 'raw', 'sourceRoot': str(self.project)},
                       {'name': 'wiki', 'role': 'wiki', 'sourceRoot': str(self.project / 'wiki')}]
        (self.project / 'wiki').mkdir()
        self.old = {'projectRoot': str(self.project), 'stateRoot': str(self.state),
                    'installId': 'a' * 32, 'scopeId': 'b' * 32,
                    'collections': collections, 'candidateDb': str(candidate),
                    'queueDb': str(self.state / 'claims.sqlite'),
                    'catalogDb': str(self.state / 'catalog.sqlite'),
                    'bindingStore': str(self.state / 'bindings.sqlite'),
                    'dbIncarnation': 'c' * 32,
                    'journalPath': str(self.state / 'install-journal.sqlite'),
                    'routeFile': str(self.route),
                    'routeSha256': hashlib.sha256(self.old_route).hexdigest(),
                    'attestationSha256': hashlib.sha256(self.old_att).hexdigest(),
                    'vectorProfileId': 'd' * 64,
                    'originMode': 'fresh_wiki_only',
                    'proposalSha256': 'e' * 64,
                    'providerProposalSha256': 'f' * 64}
        self.new = {**self.old,
                    'routeSha256': hashlib.sha256(self.new_route).hexdigest(),
                    'attestationSha256': hashlib.sha256(self.new_att).hexdigest()}
        self.original = build.create_or_verify(self.old)
        self.rows = {'state': 'candidate_ready', 'pending': {'pluginRoot': '/old'},
                     'selected': None, 'previous': None, 'newWriteSeen': False,
                     'epoch': 1, 'pendingRouteSha256': self.old['routeSha256']}
        self.counts = {'vectorChunkSets': 1574, 'vectorChunks': 2303,
                       'publishedDocuments': 1574}
        self.intent = {'schema': 'laya-inactive-upgrade-intent-v1',
                       'oldRecord': self.original,
                       'oldRoute': self.old_route.decode(),
                       'oldAttestation': self.old_att.decode(),
                       'newRoute': self.new_route.decode(),
                       'newAttestation': self.new_att.decode(),
                       'newIdentity': self.new, 'counts': self.counts,
                       'backup': str(self.state / 'inactive-upgrade-backup-fixture'),
                       'reviewSha256': '1' * 64, 'newProof': {}}
        build._write_json(self.state / upgrade.INTENT, self.intent,
                          replace=False, max_bytes=upgrade.INTENT_LIMIT)

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
        return (mock.patch.object(upgrade, 'inspect_journal', lambda _p: self.rows),
                mock.patch.object(upgrade, 'cas_replace_inactive_candidate', cas),
                mock.patch.object(upgrade, 'preflight_product_candidate', preflight),
                mock.patch.object(upgrade, '_store_counts', lambda _i: self.counts),
                mock.patch.object(upgrade, '_counts_match',
                                  lambda i, expected: upgrade._store_counts(i) == expected))

    def test_forward_and_restore_keep_build_identity_and_cache(self):
        with self.patches()[0], self.patches()[1], self.patches()[2], self.patches()[3], \
                self.patches()[4]:
            switched = upgrade._switch(self.intent, {}, {})
            self.assertEqual((switched['identity'], switched['phase']),
                             (self.new, self.original['phase']))
            self.assertEqual(self.route.read_bytes(), self.new_route)
            self.assertEqual(self.attestation.read_bytes(), self.new_att)
            # Explicit rollback is only available in the fail-closed phase.
            with self.assertRaisesRegex(Exception, 'inactive_upgrade_restore_state_changed'):
                upgrade._restore(self.intent, {}, {})

    def test_crash_between_attestation_and_journal_resumes(self):
        from laya_product_install import InstallBlocked
        real = upgrade._atomic_expected
        calls = []
        def interrupted(path, old, new):
            calls.append(path.name)
            real(path, old, new)
            if len(calls) == 1:
                raise RuntimeError('synthetic process interruption')
        with self.patches()[0], self.patches()[1], self.patches()[2], self.patches()[3], \
                self.patches()[4]:
            with mock.patch.object(upgrade, '_atomic_expected', interrupted):
                with self.assertRaisesRegex(RuntimeError, 'synthetic process interruption'):
                    upgrade._switch(self.intent, {}, {})
            self.assertEqual(build.read(self.state)['phase'], 'upgrading')
            self.assertEqual(self.rows['pendingRouteSha256'], self.old['routeSha256'])
            self.assertEqual(upgrade._switch(self.intent, {}, {})['identity'], self.new)

    def test_failed_switch_restores_original_phase_and_metadata(self):
        # Restore compares the stores with the backup's pins; take a real one.
        for key in ('candidateDb', 'queueDb', 'catalogDb', 'bindingStore', 'journalPath'):
            with sqlite3.connect(self.old[key]) as db:
                db.execute('CREATE TABLE fixture(id INTEGER PRIMARY KEY)')
            Path(self.old[key]).chmod(0o600)
        saved = upgrade._backup(self.old, self.original, self.old_route, self.old_att,
                                self.counts)
        (self.state / upgrade.INTENT).unlink()
        self.intent = {**self.intent, 'backup': str(saved)}
        build._write_json(self.state / upgrade.INTENT, self.intent,
                          replace=False, max_bytes=upgrade.INTENT_LIMIT)
        with self.patches()[0], self.patches()[1], self.patches()[2], self.patches()[3], \
                self.patches()[4]:
            with mock.patch.object(upgrade, '_store_counts', return_value={**self.counts,
                                                                          'vectorChunkSets': 0}):
                with self.assertRaisesRegex(Exception, 'inactive_upgrade_recheck_failed'):
                    upgrade._switch(self.intent, {}, {})
            self.assertEqual(build.read(self.state)['phase'], 'upgrading')
            restored = upgrade._restore(self.intent, {}, {})
            self.assertEqual((restored['identity'], restored['phase']),
                             (self.old, self.original['phase']))
            self.assertEqual(self.route.read_bytes(), self.old_route)
            self.assertEqual(self.attestation.read_bytes(), self.old_att)
            self.assertFalse(saved.exists(), 'the retired intent drops its backup')

    def test_rebind_rejects_store_or_model_change(self):
        upgrading = build.advance(self.original, phase='upgrading')
        for changed in ({'candidateDb': '/tmp/other.sqlite'},
                        {'vectorProfileId': '0' * 64},
                        {'installId': '0' * 32}):
            with self.assertRaisesRegex(Exception,
                                        'initial_build_(upgrade_identity_mismatch|identity_invalid)'):
                build.rebind_inactive_upgrade(upgrading, {**self.new, **changed})

    def test_consistent_backup_keeps_all_vector_rows(self):
        # A full-copy backup an older bundle wrote still verifies here.
        for key in ('candidateDb', 'queueDb', 'catalogDb', 'bindingStore',
                    'journalPath'):
            path = Path(self.old[key])
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE vector_chunk_sets(id INTEGER PRIMARY KEY)')
                if key == 'candidateDb':
                    db.executemany('INSERT INTO vector_chunk_sets VALUES(?)',
                                   ((n,) for n in range(1574)))
            path.chmod(0o600)
        saved = legacy_backup(self.old, self.original, self.old_route,
                              self.old_att, self.counts)
        intent = {**self.intent, 'backup': str(saved)}
        with mock.patch.object(upgrade, '_hash_private', wraps=upgrade._hash_private) as hashed:
            with upgrade.backup_proof_scope():
                upgrade._verify_backup(intent)
                calls = hashed.call_count
                self.assertGreater(calls, 0)
                upgrade._verify_backup(intent)
                self.assertEqual(hashed.call_count, calls, 'unchanged backup is not hashed twice')
                upgrade._verify_backup({**intent, 'reviewSha256': 'f' * 64})
                self.assertEqual(hashed.call_count, calls, 'retarget keeps the same backup proof')
                with mock.patch.object(upgrade.os, 'getpid', return_value=os.getpid() + 1):
                    with self.assertRaisesRegex(Exception, 'inactive_upgrade_backup_changed'):
                        upgrade._verify_backup(intent)
            upgrade._verify_backup(intent)
            self.assertGreater(hashed.call_count, calls, 'proof must not escape its scope')
        self.assertEqual(upgrade._sqlite_count(saved / 'candidateDb.sqlite',
                                               'vector_chunk_sets'), 1574)
        manifest = saved / 'manifest.json'
        original = manifest.read_bytes()
        with upgrade.backup_proof_scope():
            upgrade._verify_backup(intent)
            before = manifest.stat()
            manifest.write_bytes(bytes([original[0] ^ 1]) + original[1:])
            os.utime(manifest, ns=(before.st_atime_ns, before.st_mtime_ns))
            with self.assertRaisesRegex(Exception, 'inactive_upgrade_backup_changed'):
                upgrade._verify_backup(intent)
        manifest.write_bytes(original)
        candidate = saved / 'candidateDb.sqlite'
        with upgrade.backup_proof_scope():
            upgrade._verify_backup(intent)
            _pid, cache, _stack = upgrade._backup_proofs.get()
            monitors = next(iter(cache.values()))[2]
            self.assertTrue(all(db.execute('PRAGMA query_only').fetchone() == (1,) for db in monitors))
            replacement = saved / 'replacement.sqlite'
            replacement.write_bytes(candidate.read_bytes())
            replacement.chmod(0o600)
            os.replace(replacement, candidate)
            with self.assertRaisesRegex(Exception, 'inactive_upgrade_backup_changed'):
                upgrade._verify_backup(intent)
        with sqlite3.connect(candidate) as db:
            self.assertEqual(db.execute('PRAGMA journal_mode=WAL').fetchone(), ('wal',))
        receipt = json.loads(manifest.read_text())
        receipt['dbSha256']['candidateDb'] = upgrade._hash_private(candidate)
        manifest.write_bytes(upgrade._canonical(receipt))
        self.assertFalse(Path(str(candidate) + '-shm').exists())
        with upgrade.backup_proof_scope():
            upgrade._verify_backup(intent)
            with sqlite3.connect(candidate) as db:
                db.execute('DELETE FROM vector_chunk_sets WHERE id=0')
            self.assertGreater(Path(str(candidate) + '-wal').stat().st_size, 0)
            with self.assertRaisesRegex(Exception, 'inactive_upgrade_backup_changed'):
                upgrade._verify_backup(intent)

    def test_backup_verify_proves_bytes_by_sha256_without_page_scans(self):
        # _backup runs integrity_check on each copy before hashing it, so the
        # manifest sha256 already proves the verified bytes; verification must
        # not rescan every page (a 12.5 GB candidate DB exceeded the deadline).
        # Full copies are what older bundles left behind.
        for key in ('candidateDb', 'queueDb', 'catalogDb', 'bindingStore',
                    'journalPath'):
            path = Path(self.old[key])
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE vector_chunk_sets(id INTEGER PRIMARY KEY)')
                if key == 'candidateDb':
                    db.executemany('INSERT INTO vector_chunk_sets VALUES(?)',
                                   ((n,) for n in range(1574)))
            path.chmod(0o600)
        saved = legacy_backup(self.old, self.original, self.old_route,
                              self.old_att, self.counts)
        intent = {**self.intent, 'backup': str(saved)}
        statements = []
        real_connect = sqlite3.connect

        def traced(*args, **kwargs):
            db = real_connect(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db
        with mock.patch.object(upgrade.sqlite3, 'connect', traced):
            upgrade._verify_backup_full(intent)
        scans = [s for s in statements if 'integrity_check' in s or 'quick_check' in s]
        self.assertEqual(scans, [])
        self.assertEqual(sum('FROM vector_chunk_sets' in s for s in statements), 1,
                         statements)
        candidate = saved / 'candidateDb.sqlite'
        data = bytearray(candidate.read_bytes())
        data[-1] ^= 1
        candidate.write_bytes(bytes(data))
        with self.assertRaisesRegex(Exception, 'inactive_upgrade_backup_changed'):
            upgrade._verify_backup_full(intent)

    def test_wal_stores_closed_without_sidecars_count_back_up_and_verify(self):
        # A stock SQLite build's last close leaves WAL stores with no -wal or
        # -shm, and every SQLite backup of a WAL store carries the WAL header
        # without sidecars. Apple's /usr/lib/libsqlite3 refuses mode=ro on both.
        from laya_index_a import LayaIndexA
        from laya_index_capacity import GenerationBindingStore
        from laya_index_claim_queue import ClaimQueue
        from laya_index_host_route import wiki_index_roots
        from laya_source_catalog import LayaSourceCatalog
        index = LayaIndexA(self.old['candidateDb'], create=True)
        incarnation = index.incarnation()
        index.close()
        project = self.project.resolve()  # selected roots are canonical paths
        identity = {**self.old, 'dbIncarnation': incarnation, 'projectRoot': str(project),
                    'collections': [{'name': 'raw', 'role': 'raw', 'sourceRoot': str(project)},
                                    {'name': 'wiki', 'role': 'wiki',
                                     'sourceRoot': str(project / 'wiki')}]}
        roots = wiki_index_roots(identity['collections'])
        ClaimQueue.create(identity['queueDb'], index_incarnation=incarnation).close()
        LayaSourceCatalog(identity['catalogDb'], roots, identity['scopeId'], create=True).close()
        GenerationBindingStore(identity['bindingStore'], scope=identity['scopeId'],
                               db_incarnation=incarnation, roots=roots, create=True).close()
        with closing(sqlite3.connect(identity['journalPath'])) as db:
            db.execute('CREATE TABLE fixture_journal(id INTEGER PRIMARY KEY)')
            db.commit()
        cold = []
        for key in ('candidateDb', 'queueDb', 'catalogDb', 'bindingStore', 'journalPath'):
            path = Path(identity[key])
            path.chmod(0o600)
            if tuple(path.read_bytes()[18:20]) == (2, 2):
                strip_closed_wal_sidecars(self, path)
                cold.append(key)
        self.assertEqual(cold[:3], ['candidateDb', 'queueDb', 'catalogDb'])
        counts = upgrade._store_counts(identity)
        self.assertEqual((counts['vectorChunkSetsTail'], counts['publishedDocumentsTail']),
                         (0, 0))
        record = {'identity': identity, 'phase': 'fixture'}
        saved = upgrade._backup(identity, record, self.old_route, self.old_att, counts)
        self.assertFalse((saved / 'candidateDb.sqlite').exists())
        for path in saved.iterdir():
            if path.name.endswith('.sqlite') and Path(str(path) + '-wal').exists():
                strip_closed_wal_sidecars(self, path)
        intent = {**self.intent, 'backup': str(saved), 'oldRecord': record, 'counts': counts}
        upgrade._verify_backup(intent)
        # Pins of cold WAL stores verify after the stores were read again.
        self.assertEqual(upgrade._store_counts(identity), counts)
        upgrade._verify_store_pins(intent)
        # Full copies of older bundles pair with their exact row counts.
        legacy_counts = upgrade._legacy_store_counts(identity)
        legacy = legacy_backup(identity, record, self.old_route, self.old_att, legacy_counts)
        candidate = legacy / 'candidateDb.sqlite'
        self.assertEqual(tuple(candidate.read_bytes()[18:20]), (2, 2))
        for path in legacy.iterdir():
            if path.name.endswith('.sqlite') and Path(str(path) + '-wal').exists():
                strip_closed_wal_sidecars(self, path)
        upgrade._verify_backup({**intent, 'backup': str(legacy), 'counts': legacy_counts})
        self.assertTrue(upgrade._counts_match(identity, legacy_counts))
        self.assertEqual(upgrade._sqlite_count(candidate, 'vector_chunk_sets'), 0)


class JournalCase(unittest.TestCase):
    def test_cas_replacement_preserves_project_scope_generation_profile(self):
        with tempfile.TemporaryDirectory(prefix='laya-upgrade-journal-') as tmp:
            state = Path(tmp)
            state.chmod(0o700)
            path = state / 'install-journal.sqlite'
            project = state / 'project'
            project.mkdir()
            route_old = {'pluginRoot': '/old', 'installId': 'a'*32,
                         'generation': 'g-'+'b'*32, 'scopeId': 'c'*32,
                         'vectorProfile': {'profileId': 'd'*64},
                         'bundleManifestSha256': 'e'*64,
                         'ownershipManifestSha256': 'f'*64,
                         'projectRoot': str(project)}
            route_new = {**route_old, 'pluginRoot': '/new',
                         'bundleManifestSha256': '1'*64,
                         'ownershipManifestSha256': '2'*64}
            old_raw, new_raw = raw(route_old), raw(route_new)
            old_id = {key: route_old[key] for key in
                      ('pluginRoot','installId','generation','vectorProfile',
                       'bundleManifestSha256','ownershipManifestSha256')}
            old_id.update(sourceCommit='0'*40, bundleKind='full_plugin')
            new_id = {**old_id, 'pluginRoot': '/new',
                      'bundleManifestSha256': '1'*64,
                      'ownershipManifestSha256': '2'*64,
                      'sourceCommit': '3'*40}
            with mock.patch.object(journal, '_route', side_effect=lambda value, _path:
                                   (json.loads(value), hashlib.sha256(value).hexdigest())), \
                 mock.patch.object(journal, '_identity', side_effect=lambda proof, *_:
                                   old_id if proof == 'old' else new_id), \
                 mock.patch.object(journal, '_project', return_value=
                                   (str(project), *[project.stat().st_dev, project.stat().st_ino])), \
                 mock.patch.object(journal, '_strict_vector_profile',
                                   side_effect=lambda value: value):
                created = journal.create_journal(path, 'old', old_raw,
                                                 user_qmd_roots=['/none'])
                changed = journal.cas_replace_inactive_candidate(
                    path, created['epoch'], old_raw, 'new', new_raw)
                self.assertEqual(changed['pendingRouteSha256'],
                                 hashlib.sha256(new_raw).hexdigest())
                self.assertEqual(changed['pending']['sourceCommit'], '3'*40)
                bad = raw({**route_old, 'scopeId': '9'*32})
                with self.assertRaisesRegex(Exception, 'inactive_candidate_identity_mismatch'):
                    journal.cas_replace_inactive_candidate(
                        path, changed['epoch'], new_raw, 'old', bad)


if __name__ == '__main__':
    unittest.main()

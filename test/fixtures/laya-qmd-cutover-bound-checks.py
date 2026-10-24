#!/usr/bin/env python3
"""Bound-candidate (v3) QMD cutover: synthetic corpora, no model, host or real QMD.

A real inactive initial build is driven to ``sealed`` with the fake provider
of ``laya-initial-build-checks.py`` (HOME and every store are temporary). The
QMD source is a synthetic QMD 2.5.3 file. Only the installer journal and the
bundle attestation are replaced by ``FakeJournal``; the cutover ledger,
marker, route file, candidate, catalog, binding store, claim queue and QMD
backup are the real modules writing real temp files.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import sqlite3
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / 'core'))
BUILD = runpy.run_path(str(HERE / 'laya-initial-build-checks.py'))
MIGRATION = runpy.run_path(str(HERE / 'laya-qmd-migration-checks.py'))

import laya_initial_build as initial_build  # noqa: E402
import laya_qmd_cutover as cutover  # noqa: E402
import laya_product_install as product  # noqa: E402
import laya_product_sync_worker as worker  # noqa: E402
from laya_index_a import LayaIndexA  # noqa: E402
from laya_index_host_route import SCHEMA_V2 as ROUTE_V2  # noqa: E402
from laya_index_vector import qmd_markdown_title  # noqa: E402
from laya_qmd_migration import (QMD_253, QmdMigrationBlocked,  # noqa: E402
                                inspect_qmd_collections)
from laya_source_catalog import LayaSourceCatalog  # noqa: E402

SCOPE, PROFILE_ID = BUILD['SCOPE'], BUILD['PROFILE_ID']


class Kill(BaseException):
    """A synthetic process death between two durable writes."""


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


class FakeJournal:
    """The installer journal's states and CAS answers, in memory."""

    def __init__(self):
        self.route_sha = None
        self.attestation = 'b' * 64
        self.file_identity = '1' * 64
        self.state = 'candidate_ready'
        self.epoch = 3
        self.new_write = False
        self.calls = []

    def inspect(self, _path=None):
        ready, active = self.state == 'candidate_ready', self.state == 'active'
        return {'state': self.state, 'epoch': self.epoch,
                'pending': {'synthetic': True} if ready else None,
                'pendingRouteSha256': self.route_sha if ready else None,
                'newWriteSeen': self.new_write,
                'selected': {'synthetic': True} if active else None,
                'selectedRouteSha256': self.route_sha if active else None,
                'providerAttestationSha256': self.attestation if active else None,
                'providerFileIdentitySha256': self.file_identity if active else None}

    def approval(self, _path=None, **_kwargs):
        if self.state != 'candidate_ready':
            raise RuntimeError('journal_not_pending')
        return {'attestationSha256': self.attestation,
                'pendingRouteSha256': self.route_sha,
                'providerFileIdentitySha256': self.file_identity,
                'providerManifest': {'synthetic': True}}

    def activate(self, _path, epoch, route_bytes):
        if (self.state, epoch, _sha(route_bytes)) != ('candidate_ready', self.epoch,
                                                       self.route_sha):
            raise RuntimeError('journal_cas_failed')
        self.calls.append('activate')
        self.state, self.epoch = 'active', self.epoch + 1
        return self.inspect()

    def restore(self, _path, epoch, route_sha):
        if (self.state, epoch, route_sha, self.new_write) != ('active', self.epoch,
                                                              self.route_sha, False):
            raise RuntimeError('journal_cas_failed')
        self.calls.append('restore')
        self.state, self.epoch = 'restored_qmd', self.epoch + 1
        return self.inspect()


class BoundCase(BUILD['BuildCase']):
    def setUp(self):
        super().setUp()
        self.auto = self.project / '.auto-context'
        self.auto.mkdir()
        self.settings = self.auto / 'settings.json'
        self.settings.write_text(json.dumps({
            'collections': ['docs', 'wiki'], 'indexing': True,
            'collectionPaths': {'docs': 'docs', 'wiki': 'wiki'},
            'collectionRoles': {'docs': 'raw', 'wiki': 'wiki'},
            'recallStrategy': 'hierarchical', 'compile': {'mode': 'off'}}))
        self.qmd = self.project.parent / 'qmd.sqlite'
        self.marker = self.auto / cutover.MARKER
        self.route = self.auto / cutover.ROUTE
        self.ledger = self.state / cutover.LEDGER
        self.journal_path = self.state / 'install-journal.sqlite'
        self.fj = FakeJournal()
        self.attestation = self.fj.attestation
        self.plans = []

        def plan(project_root, **kw):
            self.plans.append(kw)
            return {'proposalSha256': 'd' * 64}
        for target, name, value in (
                (worker, 'inspect_journal', self.fj.inspect),
                (worker, 'preflight_product_candidate', self.fj.approval),
                (cutover, 'inspect_journal', self.fj.inspect),
                (cutover, 'preflight_product_candidate', self.fj.approval),
                (cutover, 'cas_activate', self.fj.activate),
                (cutover, 'cas_restore_qmd', self.fj.restore),
                (product, 'preflight_product_candidate', self.fj.approval),
                (product, 'inspect_journal', self.fj.inspect),
                (product, 'plan_qmd_origin', plan)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (
                ('laya_install_journal.inspect_journal', self.fj.inspect),
                ('laya_install_journal._route',
                 lambda raw, _journal: (json.loads(raw.decode('utf-8')), _sha(raw)))):
            patcher = mock.patch(name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    # -- synthetic QMD and registration ------------------------------------

    def qmd_source(self):
        documents = [('docs', path.name, path.read_text(encoding='utf-8'), 1)
                     for path in sorted(self.docs.glob('*.md'))[:3]]
        documents += [('wiki', path.name, path.read_text(encoding='utf-8'), 1)
                      for path in sorted(self.wiki.glob('*.md'))[:3]]
        MIGRATION['make_source'](self.qmd, QMD_253, documents=documents)
        with sqlite3.connect(self.qmd) as db:
            db.executemany('UPDATE store_collections SET path=? WHERE name=?',
                           [(root, name) for name, root in self.roots.items()])

    def qmd_indexes(self, rel, body):
        """QMD (still selected) indexes one more document: its digest moves."""
        with closing(sqlite3.connect(self.qmd)) as db:
            hash_ = _sha(body.encode('utf-8'))
            db.execute('INSERT OR IGNORE INTO content VALUES(?,?,?)',
                       (hash_, body, '2026-01-02'))
            db.execute('''INSERT INTO documents(collection,path,title,hash,created_at,
                          modified_at,active) VALUES(?,?,?,?,?,?,1)''',
                       ('docs', rel, qmd_markdown_title(body, rel), hash_,
                        '2026-01-02', '2026-01-02'))
            db.commit()

    def register(self):
        candidate = self.state / product.BUILD_CANDIDATE
        queue = self.state / product.BUILD_QUEUE
        LayaIndexA(candidate, create=True).close()
        incarnation = product._empty_or_bound_candidate(candidate, SCOPE)
        product._publish_claim_queue(queue, incarnation)
        product.prepare_capacity_stores(candidate, {'wiki': self.roots['wiki']},
                                        SCOPE, incarnation)
        route = {'schema': ROUTE_V2, 'status': 'active', 'projectRoot': str(self.project),
                 'settingsSha256': _sha(self.settings.read_bytes()),
                 'dbPath': str(candidate), 'queuePath': str(queue),
                 'dbIncarnation': incarnation, 'scopeId': SCOPE,
                 'collections': self.collections, 'journalPath': str(self.journal_path),
                 'vectorProfile': {'profileId': PROFILE_ID}}
        self.route_bytes = json.dumps(route, sort_keys=True, separators=(',', ':')).encode()
        route_file = self.state / 'reviewed-route.json'
        fd = os.open(route_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(self.route_bytes)
        self.fj.route_sha = _sha(self.route_bytes)
        identity = self.identity(dbIncarnation=incarnation, routeSha256=self.fj.route_sha,
                                 attestationSha256=self.fj.attestation,
                                 sourceDb=str(self.qmd), sourceFormat=QMD_253)
        initial_build.create_or_verify(identity)
        initial_build.register_project(identity)
        return identity

    def finalize(self):
        return product.finalize_initial_build(project_root=self.project,
                                              state_root=self.state)

    def bound(self, docs=4, wiki=2):
        """Register, build, request freshness, re-seal and bind."""
        self.corpus(docs, collection='docs')
        for i in range(wiki):
            self.write(f'wiki/w{i}.md', f'# Wiki {i}\nwiki body {i}\n')
        self.qmd_source()
        self.register()
        self.assertEqual(self.until_settled()[-1]['initialBuild'], 'sealed')
        self.assertEqual(self.finalize()['status'], 'freshness_pending')
        self.assertEqual(self.until_settled()[-1]['initialBuild'], 'sealed')
        result = self.finalize()
        self.assertEqual(result['status'], 'cutover_prepared', result)
        return result

    # -- observations ----------------------------------------------------------

    def row(self):
        return cutover._ledger(self.ledger)

    def selection(self):
        return cutover.classify_marker(self.project, self.marker, self.route)

    def activate(self):
        return cutover.activate_qmd_origin(self.project, self.journal_path, self.route_bytes)

    def restore(self):
        return cutover.restore_qmd_origin(self.project, self.journal_path)

    def recover(self):
        return cutover.recover_qmd_origin(self.project, self.journal_path, self.route_bytes)

    def backups(self):
        return sorted(path.name for path in self.state.glob('qmd-origin-backup-*.sqlite'))

    def qmd_digest(self):
        return inspect_qmd_collections(self.qmd, QMD_253, ('docs', 'wiki'))['digest']

    def bind_kwargs(self, row, **changes):
        """The exact ``bind_qmd_origin_bound`` call that wrote ``row``."""
        roots = {item['name']: item['sourceRoot'] for item in row['collections']}
        kwargs = dict(project_root=self.project, journal_path=self.journal_path,
                      route_bytes=self.route_bytes, source_db=row['sourceDb'],
                      backup_db=row['backupDb'], source_format=row['sourceFormat'],
                      collection=('docs', 'wiki'), source_digest=row['sourceDigest'],
                      source_root=roots, candidate_db=row['candidateDb'],
                      queue_db=row['queueDb'], catalog_db=row['catalogDb'],
                      binding_store=row['bindingStore'], scope=row['scope'],
                      index_generation=row['indexGeneration'],
                      source_manifest=row['sourceManifest'],
                      binding_digest=row['bindingDigest'],
                      catalog_scan_id=row['catalogScanId'],
                      catalog_raw_digest=row['catalogRawDigest'],
                      vector_profile_id=row['vectorProfileId'],
                      vector_rows=row['vectorRows'])
        return {**kwargs, **changes}

    def restore_entry(self):
        return product.restore_origin_selection(project_root=self.project,
                                                state_root=self.state)

    def kept(self):
        """Nothing a cutover may never delete: QMD, backups, candidate, sources."""
        self.assertTrue(self.qmd.is_file())
        for name in self.backups():
            self.assertTrue((self.state / name).is_file())
        for name in (product.BUILD_CANDIDATE, product.BUILD_QUEUE,
                     product.BUILD_CANDIDATE + '.catalog.sqlite',
                     product.BUILD_CANDIDATE + '.bindings.sqlite'):
            self.assertTrue((self.state / name).is_file(), name)
        self.assertTrue(any(self.docs.glob('*.md')))

    def assert_prepared_unselected(self):
        self.assertEqual(self.row()['phase'], 'prepared')
        self.assertFalse(self.route.exists())
        self.assertEqual(self.fj.state, 'candidate_ready')
        self.assertEqual(self.selection(), 'legacy')
        self.kept()


class BoundCutoverFlowTests(BoundCase):
    def test_bind_activate_and_prewrite_restore(self):
        result = self.bound()
        qmd_before = _sha(self.qmd.read_bytes())
        row = self.row()
        sealed = self.record()['sealed']
        self.assertEqual(self.record()['phase'], 'bound')
        self.assertEqual(row['schema'], cutover.SCHEMA_V3)
        self.assertEqual((row['indexGeneration'], row['bindingDigest'],
                          row['catalogScanId'], row['vectorRows']),
                         (sealed['generation'], sealed['bindingDigest'],
                          sealed['catalogScanId'], sealed['vectorRows']))
        self.assertEqual(result['sourceBackup'], row['backupDb'])
        self.assertEqual(cutover._read_json(self.marker), cutover._marker(row))
        self.assertEqual(self.plans[-1].get('_bound_marker'), False)
        self.assert_prepared_unselected()
        # The bound build never steps again; the wake ACKs without a store write.
        calls = list(BUILD['FakeProvider'].calls)
        self.assertEqual(self.wake()['reason'], 'initial_build_bound')
        self.assertEqual(BUILD['FakeProvider'].calls, calls)
        # A repeated finalize re-proves and reports, without a second backup.
        again = self.finalize()
        self.assertEqual((again['status'], self.row()['epoch']), ('cutover_prepared', 1))
        self.assertEqual(self.plans[-1].get('_bound_marker'), True)
        self.assertEqual(len(self.backups()), 1)

        active = self.activate()
        self.assertEqual(active['phase'], 'active')
        self.assertEqual(self.route.read_bytes(), self.route_bytes)
        self.assertEqual(self.fj.calls, ['activate'])
        self.assertEqual(self.selection(), 'candidate')
        self.assertEqual(self.finalize()['status'], 'cutover_active')

        restored = self.restore()
        self.assertEqual(restored['phase'], 'restored')
        self.assertFalse(self.route.exists())
        self.assertEqual((self.fj.state, self.fj.calls), ('restored_qmd', ['activate', 'restore']))
        self.assertEqual(self.selection(), 'legacy')
        self.assertEqual(_sha(self.qmd.read_bytes()), qmd_before, 'QMD is read-only')
        self.kept()

    def test_after_the_first_new_write_rollback_is_forward_only(self):
        self.bound()
        self.activate()
        self.fj.new_write = True
        with self.assertRaisesRegex(cutover.CutoverBlocked, 'cutover_rollback_not_authorized'):
            self.restore()
        self.assertEqual((self.row()['phase'], self.route.read_bytes()),
                         ('active', self.route_bytes))
        self.assertEqual(self.selection(), 'candidate')
        # The fence is rechecked after the ledger leaves ``active``.
        self.fj.new_write = False
        reads = {'n': 0}
        real = self.fj.inspect

        def racing(path=None):
            reads['n'] += 1
            self.fj.new_write = reads['n'] > 1
            return real(path)
        with mock.patch.object(cutover, 'inspect_journal', racing):
            with self.assertRaisesRegex(cutover.CutoverBlocked,
                                        'cutover_rollback_not_authorized'):
                self.restore()
        self.assertEqual((self.row()['phase'], self.route.read_bytes()),
                         ('active', self.route_bytes))
        self.assertNotIn('restore', self.fj.calls)
        self.kept()

    def test_over_200_files_and_5_mb_bind_without_a_full_scan(self):
        self.corpus(205)
        filler = ('lorem ipsum dolor sit amet ' * 40 + '\n') * 830
        for i in range(6):
            self.write(f'wiki/big{i}.md', f'# Big {i}\n' + filler)
        self.qmd_source()
        self.register()
        refused = AssertionError('full-source scan or republish')
        with mock.patch.object(cutover, 'scan', side_effect=refused), \
                mock.patch.object(cutover, 'scan_collections', side_effect=refused), \
                mock.patch.object(cutover, 'publish_from_source', side_effect=refused), \
                mock.patch.object(cutover, 'publish_collections_from_source',
                                  side_effect=refused):
            self.assertEqual(self.until_settled(limit=400)[-1]['initialBuild'], 'sealed')
            self.assertEqual(self.finalize()['status'], 'freshness_pending')
            self.assertEqual(self.until_settled(limit=400)[-1]['initialBuild'], 'sealed')
            self.assertEqual(self.finalize()['status'], 'cutover_prepared')
            self.assertEqual(self.activate()['phase'], 'active')
            self.assertEqual(self.restore()['phase'], 'restored')
        sealed = self.record()['sealed']
        self.assertEqual(sealed['files'], 211)
        self.assertGreater(sealed['bytes'], 5_000_000)
        self.assertEqual(self.row()['vectorRows'], 211)


class BoundCutoverCrashTests(BoundCase):
    def sealed_fresh(self):
        self.corpus(3, collection='docs')
        self.write('wiki/w0.md', '# W\nwiki\n')
        self.qmd_source()
        self.register()
        self.until_settled()
        self.assertEqual(self.finalize()['status'], 'freshness_pending')
        self.until_settled()
        self.assertTrue(initial_build.freshness_satisfied(self.record()))

    def test_crash_between_ledger_and_marker_recovers_without_activation(self):
        self.sealed_fresh()
        real = cutover._write

        def write(path, value, *, replace):
            if Path(path) == self.marker:
                raise Kill()
            return real(path, value, replace=replace)
        with mock.patch.object(cutover, '_write', write):
            with self.assertRaises(Kill):
                self.finalize()
        self.assertTrue(self.ledger.is_file())
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.record()['phase'], 'sealed')
        self.assertFalse(self.route.exists())
        recovered = self.recover()
        self.assertEqual(recovered['phase'], 'prepared')
        self.assertEqual(cutover._read_json(self.marker), cutover._marker(recovered))
        self.assertEqual(self.finalize()['status'], 'cutover_prepared')
        self.assertEqual(self.record()['phase'], 'bound')
        self.assertEqual((self.row()['epoch'], len(self.backups())), (1, 1))
        self.assert_prepared_unselected()
        self.assertEqual(self.fj.calls, [])

    def test_crash_before_the_build_records_bound_defers_the_wake(self):
        self.sealed_fresh()
        real = initial_build.advance

        def advance(record, **changes):
            if changes.get('phase') == 'bound':
                raise Kill()
            return real(record, **changes)
        with mock.patch.object(initial_build, 'advance', advance):
            with self.assertRaises(Kill):
                self.finalize()
        before = self.record()
        self.assertEqual(before['phase'], 'sealed')
        self.assertEqual(self.selection(), 'legacy')
        waked = self.wake()
        self.assertEqual((waked['state'], waked['reason']),
                         ('deferred', 'initial_build_bind_unsettled'))
        self.assertEqual(self.record(), before, 'the wake writes nothing')
        self.assertEqual(self.finalize()['status'], 'cutover_prepared')
        self.assertEqual(self.plans[-1].get('_bound_marker'), True)
        self.assertEqual(self.record()['phase'], 'bound')
        self.assert_prepared_unselected()

    def test_activation_crash_before_route_withdraws_to_prepared(self):
        self.bound()
        real = cutover._write

        def write(path, value, *, replace):
            if Path(path) == self.route:
                raise Kill()
            return real(path, value, replace=replace)
        with mock.patch.object(cutover, '_write', write):
            with self.assertRaises(Kill):
                self.activate()
        self.assertEqual(self.row()['phase'], 'switching')
        self.assertEqual(self.selection(), 'blocked')
        self.assertEqual(self.recover()['phase'], 'prepared')
        self.assert_prepared_unselected()
        self.assertEqual(self.fj.calls, [])
        self.assertEqual(self.activate()['phase'], 'active')

    def test_activation_crash_before_journal_commit_never_activates_on_recovery(self):
        self.bound()
        with mock.patch.object(cutover, 'cas_activate', side_effect=Kill()):
            with self.assertRaises(Kill):
                self.activate()
        self.assertEqual(self.route.read_bytes(), self.route_bytes)
        self.assertEqual(self.selection(), 'blocked')
        recovered = self.recover()
        self.assertEqual(recovered['phase'], 'prepared')
        self.assertEqual(self.fj.calls, [], 'recovery is not an activation')
        self.assert_prepared_unselected()

    def test_activation_crash_after_journal_commit_settles_forward(self):
        self.bound()
        real = cutover._change

        def change(path, epoch, expected, following, updates=None):
            if (expected, following) == ('switching', 'active'):
                raise Kill()
            return real(path, epoch, expected, following, updates)
        with mock.patch.object(cutover, '_change', change):
            with self.assertRaises(Kill):
                self.activate()
        self.assertEqual((self.row()['phase'], self.fj.state), ('switching', 'active'))
        self.assertEqual(self.recover()['phase'], 'active')
        self.assertEqual(self.selection(), 'candidate')

    def test_restore_crash_after_route_removal(self):
        self.bound()
        self.activate()
        with mock.patch.object(cutover, 'cas_restore_qmd', side_effect=Kill()):
            with self.assertRaises(Kill):
                self.restore()
        self.assertEqual(self.row()['phase'], 'restoring')
        self.assertFalse(self.route.exists())
        self.assertEqual(self.selection(), 'blocked')
        # A selected writer that marked forward-only meanwhile wins.
        self.fj.new_write = True
        self.assertEqual(self.recover()['phase'], 'active')
        self.assertEqual(self.route.read_bytes(), self.route_bytes)
        self.assertEqual(self.selection(), 'candidate')
        self.kept()

    def test_restore_crash_without_new_write_completes_the_rollback(self):
        self.bound()
        self.activate()
        with mock.patch.object(cutover, 'cas_restore_qmd', side_effect=Kill()):
            with self.assertRaises(Kill):
                self.restore()
        self.assertEqual(self.recover()['phase'], 'restored')
        self.assertEqual((self.fj.state, self.selection()), ('restored_qmd', 'legacy'))
        self.kept()


class BoundCutoverProofTests(BoundCase):
    def refused(self, reason):
        with self.assertRaisesRegex((cutover.CutoverBlocked, QmdMigrationBlocked), reason):
            self.activate()
        self.assert_prepared_unselected()
        self.assertEqual(self.fj.calls, [])

    def test_changed_qmd_is_never_rebound_to_the_stale_generation(self):
        self.bound()
        first = self.row()
        marker = self.marker.read_bytes()
        record = self.record()
        self.qmd_indexes('late.md', '# Late\nindexed after bind\n')
        drifted = self.qmd_digest()
        self.assertNotEqual(drifted, first['sourceDigest'])
        self.refused(cutover.SOURCE_DRIFT)
        # Finalize refuses before taking a backup of the drifted digest.
        with self.assertRaisesRegex(product.InstallBlocked, cutover.SOURCE_DRIFT):
            self.finalize()
        # The cutover owner refuses the same replay made directly.
        with self.assertRaisesRegex(cutover.CutoverBlocked, cutover.SOURCE_DRIFT):
            cutover.bind_qmd_origin_bound(**self.bind_kwargs(first, source_digest=drifted))
        self.assertEqual(self.row(), first, 'the ledger keeps the bound digest')
        self.assertEqual(self.marker.read_bytes(), marker)
        self.assertEqual(self.record(), record)
        self.assertEqual(len(self.backups()), 1)
        self.refused(cutover.SOURCE_DRIFT)
        # The product entry refuses too, and queues no selected sync.
        with mock.patch.object(product, '_request_build_wake') as wake:
            with self.assertRaisesRegex(cutover.CutoverBlocked, cutover.SOURCE_DRIFT):
                product.activate_origin_selection(self.project, self.journal_path,
                                                  self.route_bytes)
        wake.assert_not_called()
        self.assert_prepared_unselected()
        # Restoring a never-selected cutover changes nothing.
        restored = self.restore_entry()
        self.assertEqual((restored['status'], restored['changesApplied']), ('qmd_selected', 0))
        self.assertEqual(self.row(), first)
        self.assertEqual(self.fj.calls, [])

    def test_qmd_write_racing_activation_withdraws_before_the_route(self):
        self.bound()
        real = cutover._change

        def change(path, epoch, expected, following, updates=None):
            row = real(path, epoch, expected, following, updates)
            if (expected, following) == ('prepared', 'switching'):
                self.qmd_indexes('race.md', '# Race\nindexed during activation\n')
            return row
        with mock.patch.object(cutover, '_change', change):
            with self.assertRaisesRegex(cutover.CutoverBlocked, cutover.SOURCE_DRIFT):
                self.activate()
        self.assert_prepared_unselected()
        self.assertEqual(self.fj.calls, [])
        self.refused(cutover.SOURCE_DRIFT)

    def test_changed_settings_attestation_or_route_refuse(self):
        self.bound()
        original = self.settings.read_bytes()
        self.settings.write_bytes(original + b' ')
        self.refused('cutover_settings_changed')
        self.settings.write_bytes(original)
        self.fj.attestation = '9' * 64
        self.refused('cutover_attestation_changed')
        self.fj.attestation = self.attestation
        with self.assertRaisesRegex(cutover.CutoverBlocked, 'cutover_not_prepared'):
            cutover.activate_qmd_origin(self.project, self.journal_path, b'{}')
        self.assert_prepared_unselected()

    def test_changed_catalog_snapshot_refuses(self):
        self.bound()
        catalog = LayaSourceCatalog(self.row()['catalogDb'],
                                    {'wiki': self.roots['wiki']}, SCOPE)
        self.addCleanup(catalog.close)
        self.assertNotEqual(catalog.request_scan().get('status'), 'busy')
        self.refused('cutover_candidate_evidence_mismatch')
        for _ in range(50):
            if catalog.step()['status'] != 'pending':
                break
        # Same bytes, newer scan: no longer the sealed snapshot either.
        self.refused('cutover_candidate_evidence_mismatch')

    def test_damaged_backup_refuses_and_is_never_clobbered(self):
        self.bound()
        row = self.row()
        with closing(sqlite3.connect(row['backupDb'])) as db:
            db.execute("UPDATE documents SET title='forged' WHERE id=1")
            db.commit()
        damaged = _sha(Path(row['backupDb']).read_bytes())
        self.refused('restore_digest_mismatch')
        with self.assertRaisesRegex(QmdMigrationBlocked, 'restore_digest_mismatch'):
            self.finalize()
        self.assertEqual(_sha(Path(row['backupDb']).read_bytes()), damaged)
        self.assertEqual(self.row(), row)

    def test_replaced_candidate_refuses(self):
        self.bound()
        row = self.row()
        candidate = Path(row['candidateDb'])
        copy = candidate.with_name('copy.sqlite')
        shutil.copy2(candidate, copy)
        os.replace(copy, candidate)
        self.refused('cutover_bound_file_replaced')

    def test_a_different_binding_is_never_merged_into_the_ledger(self):
        self.bound()
        row = self.row()
        kwargs = self.bind_kwargs(row, vector_rows=row['vectorRows'] + 1)
        with self.assertRaisesRegex(cutover.CutoverBlocked,
                                    'cutover_selection_already_exists'):
            cutover.bind_qmd_origin_bound(**kwargs)
        self.assertEqual(self.row(), row)

    def test_v3_ledger_is_strict(self):
        self.bound()
        row = self.row()
        for forged in ({**row, 'extra': 1}, {**row, 'vectorRows': 0},
                       {**row, 'bindingDigest': 'f' * 64},
                       {**row, 'phase': 'selected'},
                       {**row, 'catalogDb': row['bindingStore']}):
            with self.assertRaisesRegex(cutover.CutoverBlocked, 'cutover_ledger_invalid'):
                cutover._validated(forged, self.ledger)


class BoundProductEntryTests(BoundCase):
    def test_activation_requires_the_fresh_bound_generation_then_wakes_sync(self):
        self.bound()
        record = self.record()
        sealed = record['sealed']
        stale = (
            {**record, 'freshness': {'requestedNs': sealed['scanStartedNs'] + 1}},
            {**record, 'sealed': {**sealed, 'vectorRows': sealed['vectorRows'] + 1}},
            {**record, 'sealed': {**sealed, 'catalogScanId': 'f' * 32}},
        )
        wakes = []

        def wake(project):
            wakes.append(project)
            return {'status': 'queued'}
        with mock.patch.object(product, '_request_build_wake', wake):
            for forged in stale:
                with mock.patch.object(initial_build, 'read', return_value=forged):
                    with self.assertRaisesRegex(product.InstallBlocked,
                                                'activation_requires_fresh_generation'):
                        product.activate_origin_selection(self.project, self.journal_path,
                                                          self.route_bytes)
            unsettled = {**record, 'phase': 'sealed'}
            with mock.patch.object(initial_build, 'read', return_value=unsettled):
                with self.assertRaisesRegex(product.InstallBlocked,
                                            'initial_build_bind_unsettled'):
                    product.activate_origin_selection(self.project, self.journal_path,
                                                      self.route_bytes)
            self.assertEqual(wakes, [])
            self.assert_prepared_unselected()
            self.assertEqual(self.fj.calls, [])
            active = product.activate_origin_selection(self.project, self.journal_path,
                                                       self.route_bytes)
        self.assertEqual((active['phase'], active['syncWake']), ('active', {'status': 'queued'}))
        self.assertEqual(wakes, [str(self.project)], 'one selected sync after activation')
        self.assertEqual(self.selection(), 'candidate')

    def test_restore_entry_withdraws_to_qmd_and_keeps_every_store(self):
        self.bound()
        self.assertEqual(self.restore_entry()['status'], 'qmd_selected')
        self.activate()
        qmd_before = _sha(self.qmd.read_bytes())
        marker = self.marker.read_bytes()
        result = self.restore_entry()
        self.assertEqual((result['status'], result['cutoverPhase'], result['journalState'],
                          result['selection'], result['changesApplied']),
                         ('qmd_restored', 'restored', 'restored_qmd', 'legacy', 1))
        self.assertFalse(self.route.exists())
        self.assertEqual(self.marker.read_bytes(), marker)
        self.assertTrue(self.ledger.is_file())
        self.assertTrue((self.state / 'reviewed-route.json').is_file())
        self.assertEqual(self.fj.calls, ['activate', 'restore'])
        self.assertEqual(_sha(self.qmd.read_bytes()), qmd_before, 'QMD is read-only')
        self.kept()
        again = self.restore_entry()
        self.assertEqual((again['status'], again['changesApplied']), ('qmd_selected', 0))
        self.assertEqual(self.fj.calls, ['activate', 'restore'])

    def test_restore_entry_after_a_new_write_is_forward_only(self):
        self.bound()
        self.activate()
        self.fj.new_write = True
        with self.assertRaisesRegex(cutover.CutoverBlocked, 'cutover_rollback_not_authorized'):
            self.restore_entry()
        self.assertEqual((self.row()['phase'], self.route.read_bytes()),
                         ('active', self.route_bytes))
        self.assertEqual((self.fj.calls, self.selection()), (['activate'], 'candidate'))
        self.kept()

    def test_interrupted_restore_entry_reports_recovery_then_settles(self):
        self.bound()
        self.activate()
        with mock.patch.object(cutover, 'cas_restore_qmd',
                               side_effect=RuntimeError('journal_cas_failed')):
            result = self.restore_entry()
        self.assertEqual((result['status'], result['reason'], result['cutoverPhase'],
                          result['journalState'], result['selection']),
                         ('recovery_required', 'journal_cas_failed', 'restoring',
                          'active', 'blocked'))
        self.assertFalse(self.route.exists())
        self.kept()
        settled = self.restore_entry()
        self.assertEqual((settled['status'], settled['cutoverPhase'], settled['journalState']),
                         ('qmd_restored', 'restored', 'restored_qmd'))
        self.assertEqual(self.fj.calls, ['activate', 'restore'])
        self.kept()

    def test_restore_entry_refuses_a_changed_reviewed_route(self):
        self.bound()
        self.activate()
        (self.state / 'reviewed-route.json').write_bytes(self.route_bytes + b' ')
        with self.assertRaisesRegex(product.InstallBlocked, 'restore_cutover_mismatch'):
            self.restore_entry()
        self.assertEqual((self.row()['phase'], self.fj.calls), ('active', ['activate']))
        self.assertEqual(self.route.read_bytes(), self.route_bytes)


if __name__ == '__main__':
    unittest.main()

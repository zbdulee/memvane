#!/usr/bin/env python3
"""Pending journal recovery on real private SQLite and observed route files.

Only bundle artifact inspection is synthetic. HOME, project, journal and
cache paths are disposable realpath directories; no host or model runs.
The initial selection has no production attestation, using the journal's
real optional-attestation path. SQL mutations below model corrupt evidence.
"""
from __future__ import annotations

from contextlib import closing
import copy
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
import laya_install_journal as J  # noqa: E402
from laya_product_bundle import BundleEvidenceError  # noqa: E402


def canon(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def profile(fingerprint='a'):
    model, dimension = 'synthetic-model', 3
    identity = [model, fingerprint * 64, dimension, 'cosine']
    return {'profileId': digest(canon(identity)), 'modelId': model,
            'embeddingFingerprint': fingerprint * 64, 'dimension': dimension,
            'metric': 'cosine', 'approvedProviderId': 'synthetic-provider',
            'providerArtifactSha256': 'b' * 64,
            'providerRelativePath': 'core/synthetic_provider.py'}


class PendingCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='laya-pending-recovery-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        env = mock.patch.dict(os.environ, {
            'HOME': str(self.home), 'QMD_SANDBOX': '1',
            'QMD_SETUP_GUARD_FIXTURE': '1',
            'MEMVANE_TEST_RUNTIME_ROOT': str(self.home / 'runtime-root')})
        env.start()
        self.addCleanup(env.stop)
        self.project = self.home / 'project'
        self.auto = self.project / '.memvane'
        self.auto.mkdir(parents=True)
        self.write(self.auto / 'settings.json', canon({'name': 'fixture', 'skipPaths': []}))
        self.state = self.home / 'state'
        self.state.mkdir(mode=0o700)
        self.journal = self.state / 'install-journal.sqlite'
        self.route_file = self.auto / J.ROUTE_FILE
        self.old_root, self.pending_root, self.next_root = (
            str(self.home / name) for name in ('old-cache', 'pending-cache', 'fresh-cache'))
        self.old_proof = self.proof(self.old_root, '1')
        self.pending_proof = self.proof(self.pending_root, '2')
        self.next_proof = self.proof(self.next_root, '3')
        self.live = {p['pluginRoot']: copy.deepcopy(p) for p in
                     (self.old_proof, self.pending_proof, self.next_proof)}
        self.inspected = []
        inspection = mock.patch.object(J, 'inspect_full_plugin_candidate',
                                       side_effect=self.inspect_bundle)
        inspection.start()
        self.addCleanup(inspection.stop)
        collections = [
            {'name': 'fixture', 'role': 'raw', 'sourceRoot': str(self.project)},
            {'name': 'fixture-wiki', 'role': 'wiki', 'sourceRoot': str(self.auto / 'wiki')}]
        self.old_route = {
            'schema': J.ROUTE_SCHEMA_V2, 'status': 'active',
            'projectRoot': str(self.project), 'settingsSha256': '7' * 64,
            'dbPath': str(self.state / 'index.sqlite'),
            'queuePath': str(self.state / 'claims.sqlite'),
            'dbIncarnation': '8' * 32, 'scopeId': '6' * 32,
            'collections': collections, 'pluginRoot': self.old_root,
            'journalPath': str(self.journal), 'installId': '3' * 32,
            'generation': 'g-' + '4' * 32,
            'bundleManifestSha256': self.old_proof['bundleManifestSha256'],
            'ownershipManifestSha256': self.old_proof['ownershipManifestSha256'],
            'vectorProfile': profile()}
        self.pending_route = self.route_for(self.pending_proof)
        self.pending_route['schema'] = J.ROUTE_SCHEMA_V3
        self.next_route = self.route_for(self.next_proof)
        self.next_route['schema'] = J.ROUTE_SCHEMA_V3
        self.write(self.route_file, canon(self.old_route))
        created = J.create_journal(self.journal, self.old_proof, canon(self.old_route))
        active = J.cas_activate(self.journal, created['epoch'], canon(self.old_route))
        self.pending = J.cas_stage_update(self.journal, active['epoch'],
                                          self.pending_proof, canon(self.pending_route))
        self.assertEqual(self.pending['state'], 'pending')
        self.assertEqual(self.journal.stat().st_mode & 0o777, 0o600)
        self.inspected.clear()

    @staticmethod
    def write(path, raw):
        path.write_bytes(raw)
        path.chmod(0o600)

    @staticmethod
    def proof(root, tag):
        return {'status': 'exact_commit_full_plugin_candidate_consistent',
                'pluginRoot': root, 'sourceCommit': tag * 40,
                'bundleManifestSha256': tag * 64,
                'ownershipManifestSha256': ('a' if tag == '1' else 'b' if tag == '2' else 'c') * 64,
                'vectorProfile': profile()}

    def inspect_bundle(self, root):
        self.inspected.append(root)
        if root not in self.live:
            raise BundleEvidenceError('bundle_root_unavailable')
        return copy.deepcopy(self.live[root])

    def route_for(self, proof):
        return {**copy.deepcopy(self.old_route), **{
            key: copy.deepcopy(proof[key]) for key in
            ('pluginRoot', 'bundleManifestSha256', 'ownershipManifestSha256', 'vectorProfile')}}

    def stored(self):
        with closing(sqlite3.connect(self.journal)) as db:
            db.row_factory = sqlite3.Row
            return dict(db.execute('SELECT * FROM selection').fetchone())

    def mutate(self, **changes):
        with closing(sqlite3.connect(self.journal)) as db:
            db.execute('UPDATE selection SET ' + ','.join(key + '=?' for key in changes),
                       tuple(changes.values()))
            db.commit()

    def retarget(self, **changes):
        args = {
            'journal_path': self.journal, 'expected_epoch': self.pending['epoch'],
            'current_route_raw': canon(self.old_route),
            'expected_pending': self.pending['pending'],
            'expected_pending_route_sha': self.pending['pendingRouteSha256'],
            'next_bundle_proof': self.next_proof, 'next_route_raw': canon(self.next_route)}
        args.update(changes)
        return J.cas_retarget_pending(**args)

    def refuses_without_write(self, **changes):
        before = self.stored()
        with self.assertRaises(J.JournalEvidenceError):
            self.retarget(**changes)
        self.assertEqual(self.stored(), before)


class ExistingRecoveryBoundary(PendingCase):
    def test_both_lost_caches_block_both_existing_recovery_branches(self):
        self.live.pop(self.old_root)
        self.live.pop(self.pending_root)
        before = self.stored()
        for route, root in ((self.old_route, self.old_root),
                            (self.pending_route, self.pending_root)):
            with self.subTest(root=root):
                self.write(self.route_file, canon(route))
                with self.assertRaisesRegex(BundleEvidenceError, '^bundle_root_unavailable$'):
                    J.cas_recover(self.journal, self.pending['epoch'], canon(route))
                self.assertEqual(self.inspected[-1], root)
                self.assertEqual(self.stored(), before)
                self.assertEqual(J.inspect_journal(self.journal)['state'], 'pending')


class PendingRetargetTests(PendingCase):
    def test_legacy_v3_format_and_retired_inventory_stay_unchanged(self):
        values = self.stored()
        values['schema'] = J.SCHEMA_V3
        inventory = canon([{'retired': 'opaque-evidence'}]).decode()
        values['user_qmd_inventory'] = inventory
        with closing(sqlite3.connect(self.journal)) as db:
            db.execute('DROP TABLE selection')
            db.execute(J._CREATE_V3)
            fields = [item[1] for item in db.execute('PRAGMA table_info(selection)')]
            db.execute('INSERT INTO selection VALUES(' + ','.join('?' for _ in fields) + ')',
                       tuple(values[key] for key in fields))
            db.execute('PRAGMA user_version=3')
            db.commit()
        self.retarget()
        self.assertEqual(self.stored()['schema'], J.SCHEMA_V3)
        self.assertEqual(self.stored()['user_qmd_inventory'], inventory)
        with closing(sqlite3.connect(self.journal)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 3)

    def test_non_null_selected_provider_evidence_is_preserved(self):
        identity = canon([['/synthetic/provider.py', 1, 2, 3, 4, 5]]).decode()
        self.mutate(provider_attestation_sha='f' * 64,
                    previous_provider_attestation_sha='f' * 64,
                    provider_file_identity=identity,
                    previous_provider_file_identity=identity)
        before = self.stored()
        self.retarget()
        after = self.stored()
        for key in ('provider_attestation_sha', 'previous_provider_attestation_sha',
                    'provider_file_identity', 'previous_provider_file_identity'):
            self.assertEqual(after[key], before[key], key)

    def test_lost_old_and_pending_caches_retarget_only_pending_identity(self):
        self.live.pop(self.old_root)
        self.live.pop(self.pending_root)
        before = self.stored()
        result = self.retarget()
        after = self.stored()
        self.assertEqual(result['state'], 'pending')
        self.assertEqual(result['epoch'], self.pending['epoch'] + 1)
        self.assertEqual(result['pending']['pluginRoot'], self.next_root)
        self.assertEqual(result['pendingRouteSha256'], digest(canon(self.next_route)))
        for key in before.keys() - {'epoch', 'pending', 'pending_route_sha'}:
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(self.inspected, [self.next_root])
        self.assertEqual(self.route_file.read_bytes(), canon(self.old_route))

    def test_same_commit_reextracted_pinned_caches_need_only_fresh_proof(self):
        for root in (self.old_root, self.pending_root):
            self.live[root]['ownershipManifestSha256'] = 'f' * 64
        self.write(self.route_file, canon(self.pending_route))
        result = self.retarget(current_route_raw=canon(self.pending_route))
        self.assertEqual(result['state'], 'pending')
        self.assertEqual(result['pending']['pluginRoot'], self.next_root)
        self.assertEqual(result['selected'], self.pending['selected'])
        self.assertEqual(self.inspected, [self.next_root])

    def test_stale_epoch_and_stale_predecessor_refuse(self):
        self.refuses_without_write(expected_epoch=self.pending['epoch'] - 1)
        self.refuses_without_write(expected_pending_route_sha='f' * 64)
        self.refuses_without_write(expected_pending={
            **self.pending['pending'], 'ownershipManifestSha256': 'f' * 64})
        self.retarget()
        self.refuses_without_write()

    def test_observed_bytes_and_known_route_digest_are_required(self):
        self.refuses_without_write(current_route_raw=canon(self.pending_route))
        unknown = {**self.old_route, 'settingsSha256': 'f' * 64}
        self.write(self.route_file, canon(unknown))
        self.refuses_without_write(current_route_raw=canon(unknown))
        saved = self.auto / 'saved-route.json'
        self.route_file.rename(saved)
        self.route_file.symlink_to(saved)
        self.refuses_without_write()

    def test_pending_state_and_no_write_seen_are_required(self):
        before = self.stored()
        for changes in ({'state': 'active'}, {'new_write_seen': 1}):
            with self.subTest(changes=changes):
                self.mutate(**changes)
                self.refuses_without_write()
                self.mutate(**{key: before[key] for key in changes})

    def test_selected_previous_and_provider_evidence_must_match(self):
        before = self.stored()
        changed_previous = {**self.pending['previous'], 'sourceCommit': 'f' * 40}
        cases = (
            {'previous': canon(changed_previous).decode()},
            {'selected': canon(changed_previous).decode()},
            {'previous_route_sha': 'f' * 64},
            {'selected_route_sha': 'f' * 64},
            {'previous_provider_attestation_sha': 'f' * 64},
            {'provider_attestation_sha': 'f' * 64},
            {'previous_provider_file_identity': json.dumps([['/synthetic/p.py', 1, 2, 3, 4, 5]])},
            {'provider_file_identity': json.dumps([['/synthetic/p.py', 1, 2, 3, 4, 5]])})
        for changes in cases:
            with self.subTest(changes=changes):
                self.mutate(**changes)
                self.refuses_without_write()
                self.mutate(**{key: before[key] for key in changes})

    def test_target_cannot_change_project_scope_store_or_profile(self):
        other = self.home / 'other-project'
        other.mkdir()
        changes = (
            {'projectRoot': str(other)}, {'scopeId': 'f' * 32},
            {'installId': 'f' * 32}, {'generation': 'g-' + 'f' * 32},
            {'dbPath': str(self.state / 'other.sqlite')},
            {'queuePath': str(self.state / 'other-queue.sqlite')},
            {'dbIncarnation': 'f' * 32}, {'settingsSha256': 'f' * 64},
            {'collections': [{**item, 'name': item['name'] + '-other'}
                             for item in self.next_route['collections']]})
        for change in changes:
            with self.subTest(change=change):
                self.refuses_without_write(next_route_raw=canon({**self.next_route, **change}))
        changed_proof = {**self.next_proof, 'vectorProfile': profile('f')}
        self.live[self.next_root] = changed_proof
        self.refuses_without_write(next_bundle_proof=changed_proof,
                                    next_route_raw=canon({**self.next_route,
                                                         'vectorProfile': profile('f')}))

    def test_forward_schema_and_fresh_full_bundle_proof_are_required(self):
        self.refuses_without_write(next_route_raw=canon({**self.next_route,
                                                        'schema': J.ROUTE_SCHEMA_V2}))
        self.refuses_without_write(next_bundle_proof={**self.next_proof,
                                                      'status': 'unverified'})
        self.live[self.next_root]['ownershipManifestSha256'] = 'f' * 64
        self.refuses_without_write()

    def test_unsafe_journal_parent_and_alias_refuse(self):
        self.state.chmod(0o755)
        try:
            self.refuses_without_write()
        finally:
            self.state.chmod(0o700)
        alias = self.state / 'alias.sqlite'
        alias.symlink_to(self.journal)
        self.refuses_without_write(journal_path=alias)


if __name__ == '__main__':
    unittest.main()

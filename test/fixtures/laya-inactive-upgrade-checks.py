#!/usr/bin/env python3
"""Synthetic inactive metadata switch: no model, host, or operating project."""
from __future__ import annotations

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


def raw(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


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
                mock.patch.object(upgrade, '_store_counts', lambda _i: self.counts))

    def test_forward_and_restore_keep_build_identity_and_cache(self):
        with self.patches()[0], self.patches()[1], self.patches()[2], self.patches()[3]:
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
        with self.patches()[0], self.patches()[1], self.patches()[2], self.patches()[3]:
            with mock.patch.object(upgrade, '_atomic_expected', interrupted):
                with self.assertRaisesRegex(RuntimeError, 'synthetic process interruption'):
                    upgrade._switch(self.intent, {}, {})
            self.assertEqual(build.read(self.state)['phase'], 'upgrading')
            self.assertEqual(self.rows['pendingRouteSha256'], self.old['routeSha256'])
            self.assertEqual(upgrade._switch(self.intent, {}, {})['identity'], self.new)

    def test_failed_switch_restores_original_phase_and_metadata(self):
        with self.patches()[0], self.patches()[1], self.patches()[2], self.patches()[3]:
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

    def test_rebind_rejects_store_or_model_change(self):
        upgrading = build.advance(self.original, phase='upgrading')
        for changed in ({'candidateDb': '/tmp/other.sqlite'},
                        {'vectorProfileId': '0' * 64},
                        {'installId': '0' * 32}):
            with self.assertRaisesRegex(Exception,
                                        'initial_build_(upgrade_identity_mismatch|identity_invalid)'):
                build.rebind_inactive_upgrade(upgrading, {**self.new, **changed})

    def test_consistent_backup_keeps_all_vector_rows(self):
        for key in ('candidateDb', 'queueDb', 'catalogDb', 'bindingStore',
                    'journalPath'):
            path = Path(self.old[key])
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE vector_chunk_sets(id INTEGER PRIMARY KEY)')
                if key == 'candidateDb':
                    db.executemany('INSERT INTO vector_chunk_sets VALUES(?)',
                                   ((n,) for n in range(1574)))
            path.chmod(0o600)
        saved = upgrade._backup(self.old, self.original, self.old_route,
                                self.old_att, self.counts)
        intent = {**self.intent, 'backup': str(saved)}
        upgrade._verify_backup(intent)
        self.assertEqual(upgrade._sqlite_count(saved / 'candidateDb.sqlite',
                                               'vector_chunk_sets'), 1574)
        with sqlite3.connect(saved / 'candidateDb.sqlite') as db:
            db.execute('DELETE FROM vector_chunk_sets WHERE id=0')
        with self.assertRaisesRegex(Exception, 'inactive_upgrade_backup_changed'):
            upgrade._verify_backup(intent)


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
                 mock.patch.object(journal, '_user_qmd_inventory', return_value=[['safe']]), \
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

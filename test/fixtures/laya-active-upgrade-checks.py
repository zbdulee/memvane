#!/usr/bin/env python3
"""No-model selected bundle switch with a crash and Laya-only recovery."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import laya_active_upgrade as upgrade
import laya_fresh_wiki_origin as fresh
import laya_inactive_upgrade as inactive


def raw(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


class ActiveUpgradeCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='laya-active-upgrade-')
        self.addCleanup(tmp.cleanup)
        self.project = Path(tmp.name) / 'project'
        self.auto = self.project / '.auto-context'
        self.auto.mkdir(parents=True)
        self.state = Path(tmp.name) / 'state'
        self.state.mkdir(mode=0o700)
        self.old_route = raw({'pluginRoot': '/old'})
        self.new_route = raw({'pluginRoot': '/new'})
        socket = {'serviceSocketPath': str(self.state / 'runtime' / 'laya-embed.sock')}
        self.old_att = raw({'provider': 'old', 'providerManifest': socket})
        self.new_att = raw({'provider': 'new', 'providerManifest': socket})
        self.old = {'epoch': 3, 'phase': 'active',
                    'routeSha256': hashlib.sha256(self.old_route).hexdigest(),
                    'attestationSha256': hashlib.sha256(self.old_att).hexdigest(),
                    'providerFileIdentitySha256': 'a' * 64}
        self.new = {**self.old, 'epoch': 5, 'phase': 'upgrading',
                    'routeSha256': hashlib.sha256(self.new_route).hexdigest(),
                    'attestationSha256': hashlib.sha256(self.new_att).hexdigest(),
                    'providerFileIdentitySha256': 'b' * 64}
        self.old_marker, self.new_marker = raw({'route': 'old'}), raw({'route': 'new'})
        files = ((self.state / fresh.LEDGER, raw(self.old)),
                 (self.state / 'reviewed-route.json', self.old_route),
                 (self.state / 'production-provider-attestation.json', self.old_att),
                 (self.auto / fresh.ROUTE, self.old_route),
                 (self.auto / fresh.MARKER, self.old_marker))
        for path, value in files:
            path.write_bytes(value)
            path.chmod(0o600)
        self.intent = {'stateRoot': str(self.state), 'projectRoot': str(self.project),
                       'oldLedger': self.old, 'newLedger': self.new,
                       'oldRoute': self.old_route.decode(),
                       'newRoute': self.new_route.decode(),
                       'oldAttestation': self.old_att.decode(),
                       'newAttestation': self.new_att.decode(),
                       'oldMarker': self.old_marker.decode(),
                       'newMarker': self.new_marker.decode(),
                       'oldRecord': {'identity': {'stateRoot': str(self.state)}},
                       'newProof': {'pluginRoot': '/new'},
                       'counts': {'vectorChunkSets': 1574, 'vectorChunks': 2303,
                                  'publishedDocuments': 1574},
                       'backup': str(self.state / 'backup')}
        self.journal = {'state': 'active', 'epoch': 10,
                        'selectedRouteSha256': self.old['routeSha256'],
                        'pendingRouteSha256': None,
                        'previousRouteSha256': None,
                        'previous': None, 'newWriteSeen': True,
                        'providerAttestationSha256': self.old['attestationSha256'],
                        'providerFileIdentitySha256': 'a' * 64}

    def _change(self, path, epoch, phase, next_phase):
        row = json.loads(path.read_bytes())
        self.assertEqual((row['epoch'], row['phase']), (epoch, phase))
        row.update(epoch=epoch + 1, phase=next_phase)
        path.write_bytes(raw(row))
        return row

    def _stage(self, _path, epoch, _proof, _route):
        self.assertEqual(self.journal['epoch'], epoch)
        self.journal.update(state='pending', epoch=epoch + 1,
                            pendingRouteSha256=self.new['routeSha256'],
                            previousRouteSha256=self.old['routeSha256'],
                            previous={'pluginRoot': '/old'}, newWriteSeen=False)
        return dict(self.journal)

    def _activate(self, _path, epoch, _route):
        self.assertEqual(self.journal['epoch'], epoch)
        self.journal.update(state='active', epoch=epoch + 1,
                            selectedRouteSha256=self.new['routeSha256'],
                            providerAttestationSha256=self.new['attestationSha256'],
                            providerFileIdentitySha256='b' * 64)
        return dict(self.journal)

    def _restore(self, _path, epoch, _route):
        self.assertEqual(self.journal['epoch'], epoch)
        self.journal.update(state='restored_old', epoch=epoch + 1,
                            selectedRouteSha256=self.old['routeSha256'],
                            providerAttestationSha256=self.old['attestationSha256'],
                            providerFileIdentitySha256='a' * 64)
        return dict(self.journal)

    def patches(self):
        return (mock.patch.object(fresh, '_load',
                                  lambda path: json.loads(Path(path).read_bytes())),
                mock.patch.object(fresh, '_change', self._change),
                mock.patch.object(fresh, 'classify_marker',
                                  lambda *_: 'candidate'),
                mock.patch.object(fresh, '_ledger_lock',
                                  lambda *_: mock.MagicMock()),
                mock.patch.object(upgrade, 'inspect_journal',
                                  lambda *_: dict(self.journal)),
                mock.patch.object(upgrade, 'cas_stage_update', self._stage),
                mock.patch.object(upgrade, 'cas_activate', self._activate),
                mock.patch.object(upgrade, 'cas_restore', self._restore),
                mock.patch.object(upgrade, 'cas_recover', self._restore),
                mock.patch.object(inactive, '_store_counts',
                                  lambda *_: self.intent['counts']),
                mock.patch('laya_embed_resident.wait_for_release',
                           return_value=True),
                mock.patch.object(upgrade, 'inspect_full_plugin_candidate',
                                  return_value={'pluginRoot': '/new',
                                                'sourceCommit': 'c'*40,
                                                'bundleManifestSha256': 'd'*64,
                                                'ownershipManifestSha256': 'e'*64,
                                                'vectorProfile': {'profileId': 'f'*64}}))

    def test_switch_resumes_after_metadata_interruption(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for patch in self.patches():
                stack.enter_context(patch)
            real = inactive._atomic_expected
            calls = []
            def interrupted(path, old, new):
                real(path, old, new)
                calls.append(str(path))
                if len(calls) == 3:
                    raise RuntimeError('synthetic_exit')
            with mock.patch.object(inactive, '_atomic_expected', interrupted):
                with self.assertRaisesRegex(RuntimeError, 'synthetic_exit'):
                    upgrade._switch(self.intent)
            self.assertEqual(self.journal['state'], 'pending')
            self.assertEqual(json.loads((self.state / fresh.LEDGER).read_bytes())['phase'],
                             'upgrading')
            completed = upgrade._switch(self.intent)
            self.assertEqual(completed['status'], 'active_upgrade_complete')
            self.assertEqual(self.journal['state'], 'active')
            self.assertEqual((self.auto / fresh.ROUTE).read_bytes(), self.new_route)
            self.assertEqual(upgrade._switch(self.intent)['status'],
                             'active_upgrade_complete')

    def test_restore_old_laya_before_new_write_only(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for patch in self.patches():
                stack.enter_context(patch)
            upgrade._switch(self.intent)
            with mock.patch.object(upgrade, 'inspect_full_plugin_candidate',
                                   return_value={'sourceCommit': 'c'*40,
                                                 'bundleManifestSha256': 'd'*64,
                                                 'ownershipManifestSha256': 'e'*64,
                                                 'vectorProfile': {'profileId': 'f'*64}}):
                self.journal['previous'].update(sourceCommit='c'*40,
                                                 bundleManifestSha256='d'*64,
                                                 ownershipManifestSha256='e'*64,
                                                 vectorProfile={'profileId': 'f'*64})
                self.assertEqual(upgrade._restore(self.intent)['status'],
                                 'active_upgrade_restored_laya')
            self.assertEqual((self.auto / fresh.ROUTE).read_bytes(), self.old_route)
            self.assertEqual(self.journal['state'], 'restored_old')
            self.assertEqual(json.loads((self.state / fresh.LEDGER).read_bytes())['phase'],
                             'active')

    def test_new_write_blocks_old_laya_restore(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for patch in self.patches():
                stack.enter_context(patch)
            upgrade._switch(self.intent)
            self.journal['newWriteSeen'] = True
            with self.assertRaisesRegex(Exception, 'active_upgrade_rollback_forward_only'):
                upgrade._restore(self.intent)
            self.assertEqual((self.auto / fresh.ROUTE).read_bytes(), self.new_route)

    def test_interrupted_restore_after_journal_cas_resumes(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for patch in self.patches():
                stack.enter_context(patch)
            upgrade._switch(self.intent)
            self.journal['previous'].update(sourceCommit='c'*40,
                                             bundleManifestSha256='d'*64,
                                             ownershipManifestSha256='e'*64,
                                             vectorProfile={'profileId': 'f'*64})
            real = inactive._atomic_expected
            def interrupted(path, before, after):
                if Path(path).name == fresh.MARKER:
                    raise RuntimeError('restore_interrupted')
                return real(path, before, after)
            with mock.patch.object(inactive, '_atomic_expected', interrupted):
                with self.assertRaisesRegex(RuntimeError, 'restore_interrupted'):
                    upgrade._restore(self.intent)
            self.assertEqual(self.journal['state'], 'restored_old')
            self.assertEqual(json.loads((self.state / fresh.LEDGER).read_bytes())['phase'],
                             'upgrading')
            self.assertEqual(upgrade._restore(self.intent)['status'],
                             'active_upgrade_restored_laya')
            self.assertEqual((self.auto / fresh.ROUTE).read_bytes(), self.old_route)

    def test_unexpected_active_journal_refuses_metadata_write(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for patch in self.patches():
                stack.enter_context(patch)
            self._change(self.state / fresh.LEDGER, 3, 'active', 'upgrading')
            self.journal['selectedRouteSha256'] = '9'*64
            with self.assertRaisesRegex(Exception, 'active_upgrade_journal_changed'):
                upgrade._switch(self.intent)
            self.assertEqual((self.state / 'production-provider-attestation.json').read_bytes(),
                             self.old_att)


if __name__ == '__main__':
    unittest.main()

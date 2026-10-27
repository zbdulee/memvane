#!/usr/bin/env python3
"""Synthetic fresh-wiki cutover checks; no QMD DB, model or host session."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import laya_fresh_wiki_origin as fresh
import laya_product_install as product
import laya_wiki_discovery as discovery


class Interrupted(BaseException):
    pass


class FakeJournal:
    def __init__(self, route_sha):
        self.route_sha = route_sha
        self.state = 'candidate_ready'
        self.epoch = 1
        self.new_write = False

    def inspect(self, _path):
        return {'state': self.state, 'epoch': self.epoch,
                'previous': None,
                'pendingRouteSha256': self.route_sha if self.state == 'candidate_ready' else None,
                'selectedRouteSha256': self.route_sha if self.state in ('active', 'restored_old') else None,
                'newWriteSeen': self.new_write,
                'providerAttestationSha256': 'a' * 64 if self.state in ('active', 'restored_old') else None,
                'providerFileIdentitySha256': 'b' * 64 if self.state in ('active', 'restored_old') else None}

    def approve(self, _path):
        return {'pendingRouteSha256': self.route_sha,
                'attestationSha256': 'a' * 64,
                'providerFileIdentitySha256': 'b' * 64}

    def activate(self, _path, epoch, route_bytes):
        if self.state != 'candidate_ready' or epoch != self.epoch or self.route_sha != sha(route_bytes):
            raise AssertionError('bad activation')
        self.state, self.epoch = 'active', self.epoch + 1
        return self.inspect(_path)

    def restore(self, _path, epoch, route_sha):
        if self.state != 'active' or epoch != self.epoch or self.new_write or route_sha != self.route_sha:
            raise AssertionError('bad restore')
        self.state, self.epoch = 'restored_origin', self.epoch + 1
        return self.inspect(_path)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class FreshWikiChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='laya-fresh-wiki-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / 'project'
        self.wiki = self.project / '.auto-context' / 'wiki'
        self.wiki.mkdir(parents=True)
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.journal = self.state / 'install-journal.sqlite'
        self.ledger = self.state / fresh.LEDGER
        self.marker = self.wiki.parent / fresh.MARKER
        self.route = self.wiki.parent / fresh.ROUTE
        self.collections = [
            {'name': 'docs', 'role': 'raw', 'sourceRoot': str(self.project)},
            {'name': 'docs-wiki', 'role': 'wiki', 'sourceRoot': str(self.wiki)}]
        (self.wiki.parent / 'settings.json').write_text(
            json.dumps({'name': 'docs', 'skipPaths': []}))
        self.route_bytes = json.dumps({'projectRoot': str(self.project),
                                       'collections': self.collections,
                                       'dbPath': str(self.state / 'candidate.sqlite'),
                                       'queuePath': str(self.state / 'queue.sqlite'),
                                       'scopeId': 'c' * 32,
                                       'dbIncarnation': 'd' * 32}).encode()
        self.fake = FakeJournal(sha(self.route_bytes))
        self.identity = {'projectRoot': str(self.project),
                         'journalPath': str(self.journal),
                         'routeSha256': self.fake.route_sha,
                         'attestationSha256': 'a' * 64,
                         'vectorProfileId': 'e' * 64,
                         'dbIncarnation': 'd' * 32,
                         'scopeId': 'c' * 32,
                         'collections': self.collections,
                         'candidateDb': str(self.state / 'candidate.sqlite'),
                         'queueDb': str(self.state / 'queue.sqlite'),
                         'catalogDb': str(self.state / 'catalog.sqlite'),
                         'bindingStore': str(self.state / 'bindings.sqlite')}
        self.sealed = {'generation': 'g-' + '1' * 32 + '-b' + 'f' * 64,
                       'sourceManifest': '2' * 64,
                       'bindingDigest': 'f' * 64,
                       'catalogScanId': '3' * 32,
                       'catalogRawDigest': '4' * 64,
                       'vectorProfileId': 'e' * 64,
                       'vectorRows': 1,
                       'dbIncarnation': 'd' * 32}
        for target, attr, value in (
                (fresh, 'inspect_journal', self.fake.inspect),
                (fresh, 'preflight_product_candidate', self.fake.approve),
                (fresh, 'cas_activate', self.fake.activate),
                (fresh, 'cas_restore_fresh', self.fake.restore),
                (fresh, '_identity', lambda _path: [1, 1]),
                (fresh, '_proof', lambda *_a, **_kw: None),
                (discovery, '_raw_snapshot', lambda *_a: {}),
                (discovery, 'seed_source_snapshot', lambda *_a: {})):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def bind(self):
        return fresh.bind(project_root=self.project, journal_path=self.journal,
                          route_bytes=self.route_bytes, sealed=self.sealed,
                          identity=self.identity)

    def selection(self):
        return fresh.classify_marker(self.project, self.marker, self.route)

    def test_explicit_plan_is_wiki_only(self):
        plan = product.plan_fresh_wiki_origin(self.project)
        self.assertEqual(plan['mode'], 'fresh_wiki_only')
        self.assertNotIn('sourceDb', plan)
        self.assertEqual(plan['collections'], self.collections)

    def test_plan_requires_minimal_settings(self):
        settings = self.wiki.parent / 'settings.json'
        settings.write_text(json.dumps({
            'indexing': True, 'collections': ['docs', 'docs-wiki'],
            'collectionRoles': {'docs': 'raw', 'docs-wiki': 'wiki'},
            'collectionPaths': {'docs': '.', 'docs-wiki': '.auto-context/wiki'},
            'recallStrategy': 'wikiOnly', 'compile': {'mode': 'off'}}))
        with self.assertRaisesRegex(product.InstallBlocked, 'minimal_settings_required'):
            product.plan_fresh_wiki_origin(self.project)

    def test_prewrite_restore_keeps_old_source_bytes(self):
        old = self.project / 'original.md'
        old.write_text('original QMD source')
        before = sha(old.read_bytes())
        self.bind()
        self.assertEqual(self.selection(), 'legacy')
        fresh.activate(self.project, self.journal, self.route_bytes)
        self.assertEqual(self.selection(), 'candidate')
        fresh.restore(self.project, self.journal)
        self.assertEqual(self.selection(), 'legacy')
        self.assertFalse(self.route.exists())
        self.assertEqual(sha(old.read_bytes()), before)

    def test_first_selected_write_requires_forward_recovery(self):
        self.bind()
        fresh.activate(self.project, self.journal, self.route_bytes)
        self.fake.new_write = True
        with self.assertRaisesRegex(fresh.FreshWikiBlocked, 'forward_only'):
            fresh.restore(self.project, self.journal)
        self.assertEqual(self.selection(), 'candidate')
        self.assertTrue(self.route.is_file())

    def test_previous_laya_restoration_remains_selected_and_cannot_restore_qmd(self):
        self.bind()
        fresh.activate(self.project, self.journal, self.route_bytes)
        self.fake.state = 'restored_old'
        self.assertEqual(self.selection(), 'candidate')
        self.assertEqual(fresh.recover(self.project, self.journal,
                                       self.route_bytes)['phase'], 'active')
        with self.assertRaisesRegex(fresh.FreshWikiBlocked, 'forward_only'):
            with mock.patch.object(fresh, 'inspect_journal',
                                   side_effect=lambda path:
                                   {**self.fake.inspect(path), 'previous': {'pluginRoot': '/old'}}):
                fresh.restore(self.project, self.journal)

    def test_active_upgrade_ledger_writer_fences_selected_readers(self):
        from laya_active_upgrade import _replace_ledger
        self.bind()
        fresh.activate(self.project, self.journal, self.route_bytes)
        old = fresh._load(self.ledger)
        fenced = fresh._change(self.ledger, old['epoch'], 'active', 'upgrading')
        self.assertEqual(self.selection(), 'blocked')
        next_row = {**fenced, 'epoch': fenced['epoch'] + 1,
                    'routeSha256': '1' * 64,
                    'attestationSha256': '2' * 64,
                    'providerFileIdentitySha256': '3' * 64}
        _replace_ledger(self.ledger, fenced, next_row)
        self.assertEqual(fresh._load(self.ledger), next_row)
        self.assertEqual(self.selection(), 'blocked')
        _replace_ledger(self.ledger, fenced, next_row)  # exact resume
        with self.assertRaisesRegex(Exception, 'active_upgrade_ledger_changed'):
            _replace_ledger(self.ledger, old, fenced)

    def test_interrupted_activation_with_pending_journal_withdraws_exact_route(self):
        self.bind()
        with mock.patch.object(fresh, 'cas_activate', side_effect=Interrupted):
            with self.assertRaises(Interrupted):
                fresh.activate(self.project, self.journal, self.route_bytes)
        self.assertEqual(self.selection(), 'blocked')
        self.assertEqual(fresh.recover(self.project, self.journal, self.route_bytes)['phase'],
                         'prepared')
        self.assertEqual(self.selection(), 'legacy')
        self.assertFalse(self.route.exists())

    def test_retirement_keeps_private_stores_and_releases_marker(self):
        self.bind()
        self.assertEqual(fresh.retire(self.project, self.journal)['phase'], 'retired')
        self.assertFalse(self.marker.exists())
        self.assertEqual(fresh._load(self.ledger)['phase'], 'retired')


if __name__ == '__main__':
    unittest.main()

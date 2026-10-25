#!/usr/bin/env python3
"""Selected Laya hook routes an outside edit to its approved Laya owner only.

Every byte lives in a disposable HOME under the checkout (resolve_paths treats
/tmp and /var as risky roots). Route records and settings are real files read
by the real selector and change_owner registry; only the install journal proof
(bundle/DB identity) and the durable wake are fakes. No QMD, model, daemon,
operating DB or network.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'core'))

import laya_host_response as host  # noqa: E402

GENERATION = 'g-' + 'c' * 32
PROOF_KEYS = ('projectRoot', 'scopeId', 'dbIncarnation', 'installId', 'generation')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tag(name, salt, size=32):
    return hashlib.sha256(f'{salt}:{name}'.encode()).hexdigest()[:size]


def private(path, text):
    path.write_text(text)
    path.chmod(0o600)


def vector_profile():
    model, fingerprint, dimension = 'synthetic-model', 'a' * 64, 3
    canonical = json.dumps([model, fingerprint, dimension, 'cosine'], sort_keys=True,
                           ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return {'profileId': hashlib.sha256(canonical).hexdigest(), 'modelId': model,
            'embeddingFingerprint': fingerprint, 'dimension': dimension,
            'metric': 'cosine', 'approvedProviderId': 'synthetic-provider',
            'providerArtifactSha256': 'b' * 64,
            'providerRelativePath': 'core/synthetic_provider.py'}


class OwnerRouting(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix='laya-owner-routing-', dir=Path.cwd())).resolve()
        self.addCleanup(shutil.rmtree, self.home, True)
        env = patch.dict(os.environ, {'HOME': str(self.home), 'QMD_SETUP_GUARD_FIXTURE': '',
                                      'QMD_SANDBOX': '', 'QMD_RECALL_LOG': ''})
        env.start(); self.addCleanup(env.stop)
        self.requests = []
        self.wake_result = {'status': 'queued'}
        self.journal = {}
        self.verified = []

    # -- synthetic owners ----------------------------------------------------
    def project(self, name, *, laya=True, collection=None):
        root = self.home / name
        collection = collection or f'{name}-docs'
        (root / 'docs').mkdir(parents=True, exist_ok=True)
        (root / '.auto-context').mkdir(parents=True, exist_ok=True)
        settings = {'indexing': True, 'collections': [collection],
                    'collectionPaths': {collection: 'docs'}, 'recallStrategy': 'flat',
                    'minScore': 0, 'rawFallbackMinScore': 0, 'topN': 3,
                    'skipPaths': [], 'lexicalPatterns': []}
        (root / '.auto-context/settings.json').write_text(json.dumps(settings))
        if laya:
            self.route(root, collection)
        return root

    def route(self, root, collection, **changes):
        state = self.home / 'laya-state' / root.name
        record = {'schema': 'laya-index-route-v1', 'status': 'active',
                  'projectRoot': str(root),
                  'settingsSha256': sha(root / '.auto-context/settings.json'),
                  'dbPath': str(state / 'index.sqlite'),
                  'queuePath': str(state / 'queue.sqlite'),
                  'dbIncarnation': tag(root, 'incarnation'), 'scopeId': tag(root, 'scope'),
                  'collectionName': collection, 'sourceRoot': str(root / 'docs'),
                  'pluginRoot': str(REPO),
                  'journalPath': str(REPO.parent / 'install-journal.sqlite'),
                  'installId': tag(root, 'install'), 'generation': 'g-' + tag(root, 'gen'),
                  'bundleManifestSha256': 'd' * 64, 'ownershipManifestSha256': 'e' * 64,
                  'vectorProfile': vector_profile(), **changes}
        (root / '.auto-context/laya-index-route.json').write_text(json.dumps(record))

    def note(self, root, relative='docs/note.md'):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# synthetic\n')
        return path

    def store(self, approved, registered=None):
        """Scoped, activated shared store approving `approved` (same as B1 fixture)."""
        import config as qmd_config
        import qmd_shared_store as S
        import runtime_update
        base = S.root(self.home)
        generation = base / 'generations' / GENERATION
        (generation / 'config').mkdir(parents=True)
        (generation / 'cache').mkdir()
        for directory in (base, base / 'generations', generation,
                          generation / 'config', generation / 'cache'):
            directory.chmod(0o700)
        index = generation / 'index.sqlite'
        with sqlite3.connect(index) as db:
            db.execute('CREATE TABLE documents(collection TEXT,path TEXT,hash TEXT,active INTEGER)')
            db.execute('CREATE TABLE content(hash TEXT)')
            db.execute('CREATE TABLE content_vectors(hash TEXT,seq INTEGER,model TEXT,embed_fingerprint TEXT)')
            db.execute('CREATE TABLE store_collections(name TEXT)')
        index.chmod(0o600)
        private(generation / 'config/index.yml', 'models: {}\n')
        prepared = {'schema': S.SCHEMA, 'generation': GENERATION,
                    'configSha256': sha(generation / 'config/index.yml'),
                    'indexSchemaSha256': runtime_update.inspect_index(
                        index, include_file_hash=False)['schemaSha256']}
        policy = {'schema': 'qmd-shared-scope-v1',
                  'projects': {str(p): sha(p / '.auto-context/settings.json') for p in approved}}
        private(generation / 'scope-policy.json', json.dumps(policy))
        prepared['scopePolicySha256'] = sha(generation / 'scope-policy.json')
        private(generation / 'prepared.json', json.dumps(prepared))
        private(base / 'active.json', json.dumps({'schema': S.SCHEMA, 'generation': GENERATION}))
        private(base / 'bootstrap-journal.json',
                json.dumps({'phase': 'activated', 'generation': str(generation)}))
        for root in (approved if registered is None else registered):
            config = qmd_config.find_project_config(str(root))['config']
            S._reserve_owners(base, S._collection_owners(root, config))
        return base

    def store_state(self):
        import qmd_shared_store as S
        base = S.root(self.home)
        return {name: (base / name).read_bytes() for name in
                ('collections.json', f'generations/{GENERATION}/scope-policy.json')
                if (base / name).exists()}

    def ledger(self):
        import qmd_shared_store as S
        path = S.root(self.home) / 'owner-observations.json'
        return json.loads(path.read_text())['entries'] if path.exists() else {}

    # -- fakes -----------------------------------------------------------------
    def verify(self, decision):
        self.verified.append(decision)
        state = self.journal.get(decision['projectRoot'])
        if callable(state):
            state = state(decision)
        if isinstance(state, Exception):
            raise state
        proof = {'productRouteAllowed': True, 'selectionEpoch': 1,
                 'executingPluginRoot': str(REPO),
                 **{key: decision[key] for key in PROOF_KEYS}}
        proof.update(state or {})
        return proof

    def request(self, kind, target, **options):
        self.requests.append((kind, target))
        return self.wake_result

    def serve(self, session, payload, action='index'):
        import laya_install_journal
        import runtime_work
        from laya_index_host_route import select_route
        decision = select_route(str(session), action=action)
        self.assertEqual(decision['state'], 'candidate', decision)
        payload = {'hook_event_name': 'PreToolUse' if action == 'gate' else 'PostToolUse',
                   'cwd': str(session), **payload}
        with patch.object(laya_install_journal, 'verify_selected_route',
                          side_effect=self.verify), \
             patch.object(runtime_work, 'request', side_effect=self.request):
            try:
                return host.serve_selected(action, 'claude', decision, payload), None
            except host.HostResponseError as exc:
                code = str(exc)
                # Fixed diagnostics: never a target, project path or body.
                self.assertNotIn('/', code)
                self.assertNotIn(self.home.name, code)
                return None, code

    def edit(self, session, target, **response):
        return self.serve(session, {'tool_name': 'Edit',
                                    'tool_input': {'file_path': str(target)},
                                    'tool_response': response or {'filePath': str(target)}})

    def patch_files(self, session, *lines):
        body = '\n'.join(['*** Begin Patch', *lines, '*** End Patch'])
        return self.serve(session, {'tool_name': 'apply_patch',
                                    'tool_input': {'patch': body}})

    def sync(self, *roots):
        return [('laya-sync', str(root)) for root in roots]

    # -- cross-cwd present edit -----------------------------------------------
    def test_cross_cwd_edit_wakes_only_the_registered_laya_owner(self):
        session = self.project('alpha'); owner = self.project('beta')
        target = self.note(owner); self.store([session, owner])
        self.assertEqual(self.edit(session, target), ({}, None))
        self.assertEqual(self.requests, self.sync(owner))
        # The owner's journal, not the session's, proved the woken owner.
        self.assertIn(str(owner), [d['projectRoot'] for d in self.verified])
        self.assertEqual(self.ledger()[str(target)]['projectRoot'], str(owner))

    def test_mixed_patch_keeps_selected_wake_and_adds_the_owner(self):
        session = self.project('alpha'); owner = self.project('beta')
        self.note(session, 'docs/a.md'); target = self.note(owner)
        self.store([session, owner])
        result = self.patch_files(session, '*** Update File: docs/a.md',
                                  f'*** Update File: {target}')
        self.assertEqual(result, ({}, None))
        self.assertEqual(self.requests, self.sync(session, owner))

    def test_inside_only_edit_keeps_the_existing_selected_path(self):
        import laya_owner_projection
        session = self.project('alpha'); inside = self.note(session)
        self.store([session])
        with patch.object(laya_owner_projection, 'route_external',
                          side_effect=AssertionError('no owner routing')):
            self.assertEqual(self.edit(session, inside), ({}, None))
        self.assertEqual(self.requests, self.sync(session))

    def test_gate_and_posttool_still_refuse_outside_paths(self):
        session = self.project('alpha'); owner = self.project('beta')
        target = self.note(owner); self.store([session, owner])
        for action in ('gate', 'posttool'):
            with self.subTest(action=action):
                self.assertEqual(self.serve(session, {
                    'tool_name': 'Edit', 'tool_input': {'file_path': str(target)}},
                    action=action), (None, 'edit_outside_project'))
        self.assertEqual(self.requests, [])

    # -- no registration, unknown, not Laya ------------------------------------
    def test_unregistered_laya_route_is_refused_and_never_registered(self):
        session = self.project('alpha'); owner = self.project('beta')
        stray = self.project('epsilon'); target = self.note(stray)
        self.store([session, owner])
        before = self.store_state()
        self.assertEqual(self.edit(session, target), (None, 'external_unregistered'))
        self.assertEqual(self.requests, [])
        self.assertEqual(self.store_state(), before)
        self.assertEqual(self.ledger(), {})

    def test_registered_but_unapproved_owner_is_refused(self):
        session = self.project('alpha'); owner = self.project('beta')
        target = self.note(owner)
        self.store([session], registered=[session, owner])
        self.assertEqual(self.edit(session, target), (None, 'external_not_approved'))
        self.assertEqual(self.requests, [])

    def test_qmd_owner_without_laya_route_is_not_woken_here(self):
        session = self.project('alpha'); owner = self.project('beta', laya=False)
        target = self.note(owner); self.store([session, owner])
        self.assertEqual(self.edit(session, target), (None, 'external_not_laya_owner'))
        self.assertEqual(self.requests, [])

    def test_ambiguous_nested_registration_is_refused(self):
        session = self.project('alpha'); outer = self.project('beta')
        inner = outer / 'docs/inner'
        (inner / '.auto-context').mkdir(parents=True)
        (inner / '.auto-context/settings.json').write_text(json.dumps(
            {'indexing': True, 'collections': ['inner-docs'],
             'collectionPaths': {'inner-docs': '.'}}))
        target = self.note(inner, 'note.md')
        self.store([session, outer, inner])
        self.assertEqual(self.edit(session, target), (None, 'external_ambiguous_owner'))
        self.assertEqual(self.requests, [])

    def test_symlinked_target_is_never_followed(self):
        session = self.project('alpha'); owner = self.project('beta')
        real = self.note(owner); self.store([session, owner])
        (self.home / 'links').mkdir()
        link = self.home / 'links/note.md'; link.symlink_to(real)
        self.assertEqual(self.edit(session, link), (None, 'external_symlink'))
        self.assertEqual(self.requests, [])

    # -- stale / optout / unproved owner --------------------------------------
    def test_stale_settings_after_approval_fail_closed(self):
        session = self.project('alpha'); owner = self.project('beta')
        target = self.note(owner); self.store([session, owner])
        settings = owner / '.auto-context/settings.json'
        settings.write_text(json.dumps({**json.loads(settings.read_text()), 'topN': 4}))
        self.assertEqual(self.edit(session, target), (None, 'external_stale_approval'))
        self.assertEqual(self.requests, [])

    def test_route_record_that_no_longer_binds_settings_is_blocked(self):
        session = self.project('alpha'); owner = self.project('beta')
        target = self.note(owner); self.store([session, owner])
        self.route(owner, 'beta-docs', settingsSha256='0' * 64)
        self.assertEqual(self.edit(session, target), (None, 'external_owner_route_blocked'))
        self.assertEqual(self.requests, [])

    def test_unproved_owner_journal_fails_closed(self):
        from laya_install_journal import JournalEvidenceError
        session = self.project('alpha'); owner = self.project('beta')
        target = self.note(owner); self.store([session, owner])
        cases = {
            'external_owner_journal_unverified':
                JournalEvidenceError('index_incarnation_mismatch'),
            'external_owner_product_unavailable': {'productRouteAllowed': False},
            'external_owner_journal_mismatch': {'dbIncarnation': 'f' * 32},
            'external_owner_bundle_not_executing': {'executingPluginRoot': '/elsewhere'},
        }
        for code, state in cases.items():
            with self.subTest(code=code):
                self.journal[str(owner)] = state
                self.requests.clear()
                self.assertEqual(self.edit(session, target), (None, code))
                self.assertEqual(self.requests, [])

    def test_optout_owner_fails_closed(self):
        import config as qmd_config
        session = self.project('alpha'); owner = self.project('beta')
        target = self.note(owner); self.store([session, owner])
        qmd_config.write_local_optout(str(owner))
        self.assertEqual(self.edit(session, target), (None, 'external_owner_off'))
        self.assertEqual(self.requests, [])

    def test_owner_changing_before_the_wake_is_not_woken(self):
        session = self.project('alpha'); owner = self.project('beta')
        target = self.note(owner); self.store([session, owner])
        epochs = iter([5, 4])
        self.journal[str(owner)] = lambda _d: {'selectionEpoch': next(epochs, 4)}
        self.assertEqual(self.edit(session, target), (None, 'external_owner_changed'))
        self.assertEqual(self.requests, [])

    def test_unavailable_wake_is_reported(self):
        session = self.project('alpha'); owner = self.project('beta')
        target = self.note(owner); self.store([session, owner])
        self.wake_result = None  # runtime_work's fixture/sandbox no-op return.
        self.assertEqual(self.edit(session, target), (None, 'external_owner_sync_unavailable'))
        self.assertEqual(self.ledger(), {})

    def test_retained_overflow_wake_is_durable(self):
        session = self.project('alpha'); owner = self.project('beta')
        target = self.note(owner); self.store([session, owner])
        self.wake_result = {'status': 'deferred_queue_capacity_retained'}
        self.assertEqual(self.edit(session, target), ({}, None))
        self.assertEqual(self.requests, self.sync(owner))
        self.assertEqual(self.ledger()[str(target)]['projectRoot'], str(owner))

    def test_failed_edit_does_not_wake_another_owner(self):
        session = self.project('alpha'); owner = self.project('beta')
        target = self.note(owner); self.store([session, owner])
        self.assertEqual(self.edit(session, target, is_error=True),
                         (None, 'external_edit_unconfirmed'))
        self.assertEqual(self.requests, [])

    # -- worktree identity -------------------------------------------------------
    def test_worktree_edit_wakes_that_worktree_not_its_sibling(self):
        session = self.project('alpha')
        main = self.project('beta'); tree = self.project('beta-wt')
        self.note(main); target = self.note(tree)  # Same relative path in both.
        self.store([session, main, tree])
        self.assertEqual(self.edit(session, target), ({}, None))
        self.assertEqual(self.requests, self.sync(tree))
        proved = [d for d in self.verified if d['projectRoot'] == str(tree)]
        self.assertTrue(proved)
        self.assertEqual({d['scopeId'] for d in proved}, {tag(tree, 'scope')})
        self.assertNotIn(str(main), [d['projectRoot'] for d in self.verified])

    def test_unapproved_new_worktree_is_not_adopted(self):
        session = self.project('alpha'); main = self.project('beta')
        tree = self.project('beta-wt2'); target = self.note(tree)
        self.store([session, main])
        before = self.store_state()
        self.assertEqual(self.edit(session, target), (None, 'external_unregistered'))
        self.assertEqual(self.requests, [])
        self.assertEqual(self.store_state(), before)

    # -- deletion needs prior observation --------------------------------------
    def test_delete_routes_only_by_prior_observation(self):
        session = self.project('alpha'); owner = self.project('beta')
        target = self.note(owner); self.store([session, owner])
        self.assertEqual(self.edit(session, target), ({}, None))
        target.unlink(); self.requests.clear()
        self.assertEqual(self.patch_files(session, f'*** Delete File: {target}'), ({}, None))
        self.assertEqual(self.requests, self.sync(owner))
        # A guessed missing path in the same live directory is not evidence.
        guessed = owner / 'docs/never-seen.md'; self.requests.clear()
        self.assertEqual(self.patch_files(session, f'*** Delete File: {guessed}'),
                         (None, 'external_no_prior_observation'))
        self.assertEqual(self.requests, [])

    def test_move_sides_are_proved_independently(self):
        session = self.project('alpha'); owner = self.project('beta')
        old = self.note(owner, 'docs/old.md'); self.store([session, owner])
        new = owner / 'docs/new.md'
        old.rename(new)  # No earlier observation of the old path.
        result = self.patch_files(session, f'*** Update File: {old}', f'*** Move to: {new}')
        self.assertEqual(result, (None, 'external_no_prior_observation'))
        # The proved new side still woke its owner once.
        self.assertEqual(self.requests, self.sync(owner))


if __name__ == '__main__':
    unittest.main()

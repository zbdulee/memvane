"""Stage B1/B2a change-owner routing; every byte lives in a disposable HOME.

The scoped store is synthetic SQLite/JSON only: no QMD, model, daemon or
network. Projects live under the disposable HOME (never /tmp or /var, which
resolve_paths treats as risky roots).
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path.cwd() / 'core'))

GENERATION = 'g-' + 'c' * 32


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def private(path, text):
    path.write_text(text)
    path.chmod(0o600)


class ChangeOwner(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix='qmd-change-owner-', dir=Path.cwd())).resolve()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.queue = self.home / 'queue' / 'dirty-queue'
        env = patch.dict(os.environ, {'HOME': str(self.home), 'QMD_SETUP_GUARD_FIXTURE': '',
                                      'QMD_DIRTY_QUEUE': str(self.queue), 'QMD_SANDBOX': ''})
        env.start(); self.addCleanup(env.stop)
        wake = patch('runtime_work.request', return_value=None)
        wake.start(); self.addCleanup(wake.stop)

    # -- synthetic fixtures -------------------------------------------------
    def project(self, name, collections=None, roles=None, **extra):
        root = self.home / name
        collections = collections or {f'{name}-docs': 'docs'}
        for relative in collections.values():
            (root / relative).mkdir(parents=True, exist_ok=True)
        (root / '.auto-context').mkdir(parents=True, exist_ok=True)
        settings = {'indexing': True, 'collections': list(collections),
                    'collectionPaths': collections, **extra}
        if roles:
            settings['collectionRoles'] = roles
        (root / '.auto-context/settings.json').write_text(json.dumps(settings))
        return root

    def note(self, root, relative='docs/note.md'):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# synthetic\n')
        return path

    def store(self, approved, registered=None, *, scoped=True):
        """Scoped store approving `approved`; registry from `registered` owners."""
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
        if scoped:
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

    def lines(self):
        if not self.queue.exists():
            return []
        return [line.split('\t') for line in self.queue.read_text().splitlines()]

    def resolve(self, target, base=None):
        import change_owner
        return change_owner.resolve_owner(str(target), str(base or self.home))

    def change(self, target, base=None):
        import change_owner
        return change_owner.resolve_change(str(target), str(base or self.home))

    def ledger_file(self):
        import qmd_shared_store as S
        return S.root(self.home) / 'owner-observations.json'

    def ledger(self):
        path = self.ledger_file()
        return json.loads(path.read_text())['entries'] if path.exists() else {}

    def clear_queue(self):
        self.queue.unlink(missing_ok=True)

    def hook(self, cwd, *targets):
        import index_enqueue
        payload = {'hook_event_name': 'PostToolUse', 'cwd': str(cwd), 'tool_name': 'Edit',
                   'tool_input': {'edits': [{'file_path': str(t)} for t in targets]}}
        with patch('sys.stdin', io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(index_enqueue.main(), 0)
        self.assertEqual(out.getvalue(), '', 'index hook must stay silent')

    # -- owned / foreign / current-vs-target scope -------------------------
    def test_foreign_edit_enqueues_target_owner_not_session_project(self):
        session = self.project('alpha'); owner = self.project('beta')
        target = self.note(owner)
        self.store([session, owner])
        self.hook(session, target)
        self.assertEqual(self.lines(), [['beta-docs', str(owner / 'docs'), str(owner)]])

    def test_foreign_laya_owner_uses_its_wake_without_qmd_enqueue(self):
        session = self.project('alpha'); owner = self.project('beta')
        target = self.note(owner)
        self.store([session, owner])
        with patch('laya_index_host_route.select_route',
                   return_value={'state': 'candidate'}), \
             patch('laya_owner_projection.route_external',
                   return_value=([str(owner)], [])) as wake:
            self.hook(session, target)
        self.assertEqual(self.lines(), [])
        wake.assert_called_once_with([str(target)], str(session),
                                     selected_root=str(session))

    def test_foreign_blocked_laya_owner_never_falls_back_to_qmd(self):
        session = self.project('alpha'); owner = self.project('beta')
        target = self.note(owner)
        self.store([session, owner])
        with patch('laya_index_host_route.select_route',
                   return_value={'state': 'blocked'}):
            self.hook(session, target)
        self.assertEqual(self.lines(), [])

    def test_mixed_edit_keeps_each_owner_and_session_scope_separate(self):
        session = self.project('alpha'); owner = self.project('beta')
        mine, theirs = self.note(session), self.note(owner)
        self.store([session, owner])
        self.hook(session, mine, theirs)
        self.assertEqual(sorted(self.lines()), [
            ['alpha-docs', str(session / 'docs'), str(session)],
            ['beta-docs', str(owner / 'docs'), str(owner)]])
        # Routing is indexing only: the session's recall root is still its cwd.
        import config as qmd_config
        found = qmd_config.find_project_config(str(session))
        self.assertEqual(found['projectRoot'], str(session))
        self.assertEqual(found['config']['collections'], ['alpha-docs'])

    def test_session_without_settings_still_routes_registered_owner(self):
        loose = self.home / 'scratch'; loose.mkdir()
        owner = self.project('beta'); target = self.note(owner)
        self.store([owner])
        self.hook(loose, target)
        self.assertEqual(self.lines(), [['beta-docs', str(owner / 'docs'), str(owner)]])

    def test_relative_target_resolves_against_event_cwd(self):
        owner = self.project('beta'); self.note(owner)
        self.store([owner])
        found, reason = self.resolve('docs/note.md', owner)
        self.assertIsNone(reason); self.assertEqual(found['collection'], 'beta-docs')
        self.assertEqual(self.resolve('../beta/docs/note.md', owner / 'docs')[1], 'unsafe_path')

    # -- refusals: nothing is written ----------------------------------------
    def test_unregistered_present_file_is_not_enrolled(self):
        session = self.project('alpha'); self.store([session])
        stray = self.note(self.home / 'unknown')
        self.assertEqual(self.resolve(stray)[1], 'unregistered')
        self.hook(session, stray)
        self.assertEqual(self.lines(), [])

    def test_registered_but_unapproved_owner(self):
        session = self.project('alpha'); other = self.project('gamma')
        target = self.note(other)
        self.store([session], registered=[session, other])
        self.assertEqual(self.resolve(target)[1], 'not_approved')
        self.hook(session, target); self.assertEqual(self.lines(), [])

    def test_nested_owner_is_ambiguous_and_blocks_cwd_too(self):
        outer = self.project('beta')
        inner = self.home / 'beta/docs/inner'
        inner.mkdir(parents=True)
        (inner / '.auto-context').mkdir()
        (inner / '.auto-context/settings.json').write_text(json.dumps(
            {'indexing': True, 'collections': ['inner-docs'], 'collectionPaths': {'inner-docs': '.'}}))
        target = self.note(inner, 'note.md')
        self.store([outer, inner])
        self.assertEqual(self.resolve(target)[1], 'ambiguous_owner')
        # The outer cwd would claim it by its own config, but another approved
        # project registers the same path: fail closed instead of guessing.
        self.hook(outer, target)
        self.assertEqual(self.lines(), [])

    def test_duplicate_names_for_one_path_are_ambiguous(self):
        owner = self.project('beta', {'beta-docs': 'docs', 'beta-copy': 'docs'})
        target = self.note(owner); self.store([owner])
        self.assertEqual(self.resolve(target)[1], 'ambiguous_owner')
        self.hook(owner, target)
        self.assertEqual(self.lines(), [])

    def test_symlink_leaf_and_symlinked_directory(self):
        owner = self.project('beta'); real = self.note(owner); self.store([owner])
        leaf = owner / 'docs/link.md'; leaf.symlink_to(real)
        alias = self.home / 'alias'; alias.symlink_to(owner)
        self.assertEqual(self.resolve(leaf)[1], 'symlink')
        self.assertEqual(self.resolve(alias / 'docs/note.md')[1], 'symlink')
        session = self.project('alpha')
        self.hook(session, leaf, alias / 'docs/note.md')
        self.assertEqual(self.lines(), [])

    def test_missing_old_side_is_left_for_b2_new_side_routes(self):
        import change_owner
        owner = self.project('beta'); new = self.note(owner, 'docs/renamed.md')
        self.store([owner])
        routed, refused = change_owner.route_present(
            [str(owner / 'docs/old.md'), str(new)], self.home)
        self.assertEqual(refused, [(str(owner / 'docs/old.md'), 'missing')])
        self.assertEqual([o['target'] for o in routed[str(owner)].values()], [str(new)])
        # B2a never routes a missing path from the current registry alone.
        routed, refused = change_owner.route_changes(
            [str(owner / 'docs/old.md'), str(new)], self.home)
        self.assertEqual(refused, [(str(owner / 'docs/old.md'), 'no_prior_observation')])
        self.assertEqual([o['target'] for o in routed[str(owner)].values()], [str(new)])

    def test_unsafe_and_non_regular_targets(self):
        owner = self.project('beta'); self.store([owner])
        self.assertEqual(self.resolve('')[1], 'unsafe_path')
        self.assertEqual(self.resolve(str(owner / 'docs/a\tb.md'))[1], 'unsafe_path')
        self.assertEqual(self.resolve(owner / 'docs')[1], 'not_regular')

    def test_owner_off_by_local_optout(self):
        import config as qmd_config
        owner = self.project('beta'); target = self.note(owner); self.store([owner])
        qmd_config.write_local_optout(str(owner))
        self.assertEqual(self.resolve(target)[1], 'owner_off')

    def test_stale_approval_after_settings_change(self):
        owner = self.project('beta'); target = self.note(owner); self.store([owner])
        settings = owner / '.auto-context/settings.json'
        settings.write_text(json.dumps({**json.loads(settings.read_text()), 'topN': 3}))
        self.assertEqual(self.resolve(target)[1], 'stale_approval')

    def test_owner_post_tool_use_disabled(self):
        owner = self.project('beta', events=['sessionStart', 'userPromptSubmit'])
        target = self.note(owner); self.store([owner])
        self.assertEqual(self.resolve(target)[1], 'owner_event_disabled')

    def test_source_role_is_excluded(self):
        owner = self.project('beta', {'beta-src': 'src'}, roles={'beta-src': 'source'})
        target = self.note(owner, 'src/note.md'); self.store([owner])
        self.assertEqual(self.resolve(target)[1], 'not_indexed')

    def test_unregistered_deeper_collection_is_a_mismatch(self):
        import config as qmd_config
        import qmd_shared_store as S
        owner = self.project('beta', {'beta-deep': 'docs/deep', 'beta-docs': 'docs'})
        target = self.note(owner, 'docs/deep/note.md')
        base = self.store([owner], registered=[])
        owners = S._collection_owners(owner, qmd_config.find_project_config(str(owner))['config'])
        S._reserve_owners(base, {'beta-docs': owners['beta-docs']})
        self.assertEqual(self.resolve(target)[1], 'registration_mismatch')

    def test_generated_story_guard_applies_to_target_owner(self):
        owner = self.project('beta'); target = self.note(owner); self.store([owner])
        with patch('context_learning.synthetic_scope.invocation_in_generated_story',
                   return_value=False):
            self.assertEqual(self.resolve(target)[1], 'generated_story_guard')

    def test_legacy_store_never_routes(self):
        owner = self.project('beta'); target = self.note(owner)
        self.store([owner], registered=[], scoped=False)
        self.assertEqual(self.resolve(target)[1], 'legacy_store')
        self.hook(self.project('alpha'), target); self.assertEqual(self.lines(), [])

    def test_corrupt_registry_and_missing_store(self):
        owner = self.project('beta'); target = self.note(owner)
        self.assertEqual(self.resolve(target)[1], 'store_unavailable')
        base = self.store([owner])
        private(base / 'collections.json', '{not json')
        self.assertEqual(self.resolve(target)[1], 'store_unavailable')

    def test_ownership_is_reproved_at_enqueue_time(self):
        import change_owner
        owner = self.project('beta'); target = self.note(owner); self.store([owner])
        routed, refused = change_owner.route_present([str(target)], self.home)
        self.assertEqual(refused, [])
        # The same path is now a different inode (atomically replaced after
        # routing); both inodes coexist first, so the number cannot be reused.
        staged = target.with_name('staged.md'); staged.write_text('# replaced\n')
        os.replace(staged, target)
        self.assertEqual(change_owner.enqueue_routed(routed, self.home), {})
        self.assertEqual(self.lines(), [])
        routed, _ = change_owner.route_present([str(target)], self.home)
        settings = owner / '.auto-context/settings.json'
        settings.write_text(settings.read_text() + '\n')
        self.assertEqual(change_owner.enqueue_routed(routed, self.home), {})
        self.assertEqual(self.lines(), [])

    # -- B2a: missing side needs a still-bound prior observation ------------
    def observed(self, session, owner, relative='docs/note.md'):
        """An accepted present edit of an external owner's file, then a clean queue."""
        target = self.note(owner, relative)
        self.hook(session, target)
        self.assertIn(str(target), self.ledger())
        self.clear_queue()
        return target

    def test_observed_external_edit_then_delete_routes_owner(self):
        session = self.project('alpha'); owner = self.project('beta')
        self.store([session, owner])
        target = self.observed(session, owner)
        row = self.ledger()[str(target)]
        self.assertEqual((row['projectRoot'], row['collection'], row['collectionPath']),
                         (str(owner), 'beta-docs', str(owner / 'docs')))
        target.unlink()
        found, reason = self.change(target)
        self.assertIsNone(reason); self.assertEqual(found['change'], 'missing')
        self.hook(session, target)
        self.assertEqual(self.lines(), [['beta-docs', str(owner / 'docs'), str(owner)]])

    def test_cwd_owned_present_edit_is_observed_for_later_external_delete(self):
        session = self.project('alpha'); owner = self.project('beta')
        self.store([session, owner])
        target = self.observed(owner, owner)  # The owner's own session edits it.
        target.unlink()
        self.hook(session, target)
        self.assertEqual(self.lines(), [['beta-docs', str(owner / 'docs'), str(owner)]])

    def test_unobserved_delete_fails_closed(self):
        session = self.project('alpha'); owner = self.project('beta')
        target = self.note(owner); self.store([session, owner])
        target.unlink()
        self.assertEqual(self.change(target), (None, 'no_prior_observation'))
        self.hook(session, target)
        self.assertEqual(self.lines(), [])

    def test_cross_owner_move_routes_each_proved_side(self):
        session = self.project('alpha'); old_owner = self.project('beta')
        new_owner = self.project('gamma')
        self.store([session, old_owner, new_owner])
        old = self.observed(session, old_owner)
        new = new_owner / 'docs/moved.md'
        os.rename(old, new)
        self.hook(session, old, new)
        self.assertEqual(sorted(self.lines()), [
            ['beta-docs', str(old_owner / 'docs'), str(old_owner)],
            ['gamma-docs', str(new_owner / 'docs'), str(new_owner)]])
        # The present new side is now evidence for its own later move.
        self.assertIn(str(new), self.ledger())

    def test_move_sides_are_independent(self):
        session = self.project('alpha'); owner = self.project('beta')
        other = self.project('gamma')
        self.store([session, owner, other])
        # Observed old side, new side outside every registration: old only.
        old = self.observed(session, owner)
        stray = self.home / 'unknown/docs/moved.md'; stray.parent.mkdir(parents=True)
        os.rename(old, stray)
        self.hook(session, old, stray)
        self.assertEqual(self.lines(), [['beta-docs', str(owner / 'docs'), str(owner)]])
        self.clear_queue()
        # Unobserved old side, registered new side: new only.
        unseen = self.note(owner, 'docs/unseen.md')
        new = other / 'docs/unseen.md'
        os.rename(unseen, new)
        self.hook(session, unseen, new)
        self.assertEqual(self.lines(), [['gamma-docs', str(other / 'docs'), str(other)]])

    def test_missing_symlink_leaf_and_linked_ancestor_are_refused(self):
        session = self.project('alpha'); owner = self.project('beta')
        self.store([session, owner])
        target = self.observed(session, owner)
        target.unlink()
        alias = self.home / 'alias'; alias.symlink_to(owner)
        self.assertEqual(self.change(alias / 'docs/note.md')[1], 'symlink')
        target.symlink_to(self.home / 'nowhere.md')  # Dangling link at the old path.
        self.assertEqual(self.change(target)[1], 'symlink')
        self.hook(session, target, alias / 'docs/note.md')
        self.assertEqual(self.lines(), [])

    def test_missing_with_new_registration_is_stale_not_guessed(self):
        import config as qmd_config
        import qmd_shared_store as S
        session = self.project('alpha'); owner = self.project('beta')
        base = self.store([session, owner])
        target = self.observed(session, owner, 'docs/inner/note.md')
        inner = self.home / 'beta/docs/inner'
        (inner / '.auto-context').mkdir()
        (inner / '.auto-context/settings.json').write_text(json.dumps(
            {'indexing': True, 'collections': ['inner-docs'], 'collectionPaths': {'inner-docs': '.'}}))
        S._reserve_owners(base, S._collection_owners(
            inner, qmd_config.find_project_config(str(inner))['config']))
        target.unlink()
        self.assertEqual(self.change(target)[1], 'stale_observation')
        self.hook(session, target); self.assertEqual(self.lines(), [])

    def test_missing_owner_off_and_stale_approval(self):
        import config as qmd_config
        session = self.project('alpha'); owner = self.project('beta')
        self.store([session, owner])
        target = self.observed(session, owner)
        target.unlink()
        settings = owner / '.auto-context/settings.json'
        original = settings.read_text()
        settings.write_text(json.dumps({**json.loads(original), 'topN': 3}))
        self.assertEqual(self.change(target)[1], 'stale_approval')
        self.hook(session, target); self.assertEqual(self.lines(), [])
        settings.write_text(original)
        qmd_config.write_local_optout(str(owner))
        self.assertEqual(self.change(target)[1], 'owner_off')
        self.hook(session, target); self.assertEqual(self.lines(), [])

    def test_registry_retire_and_identical_readd_invalidates_observation(self):
        import qmd_shared_store as S
        session = self.project('alpha'); owner = self.project('beta')
        base = self.store([session, owner])
        target = self.observed(session, owner)
        registry_file = base / 'collections.json'
        before = registry_file.read_bytes()
        registry = json.loads(before)
        mine = {n: o for n, o in registry.items() if o['scope'] == str(owner)}
        S._write_registry(base, {n: o for n, o in registry.items() if n not in mine})
        S._reserve_owners(base, mine)
        self.assertEqual(registry_file.read_bytes(), before)  # A-B-A by bytes.
        target.unlink()
        self.assertEqual(self.change(target)[1], 'stale_observation')
        self.hook(session, target); self.assertEqual(self.lines(), [])

    def test_unchanged_registration_retry_preserves_observation(self):
        import config as qmd_config
        import qmd_shared_store as S
        session = self.project('alpha'); owner = self.project('beta')
        base = self.store([session, owner])
        target = self.observed(session, owner)
        registry_file = base / 'collections.json'
        first = os.stat(registry_file)
        S._reserve_owners(base, S._collection_owners(
            owner, qmd_config.find_project_config(str(owner))['config']))
        again = os.stat(registry_file)
        self.assertEqual((first.st_ino, first.st_mtime_ns, first.st_ctime_ns),
                         (again.st_ino, again.st_mtime_ns, again.st_ctime_ns))
        target.unlink()
        self.hook(session, target)
        self.assertEqual(self.lines(), [['beta-docs', str(owner / 'docs'), str(owner)]])

    def test_corrupt_partial_or_unsafe_ledger_denies_and_is_not_written_through(self):
        session = self.project('alpha'); owner = self.project('beta')
        self.store([session, owner])
        target = self.observed(session, owner)
        target.unlink()
        ledger = self.ledger_file()
        good = ledger.read_bytes()
        # A crashed writer's leftover temp file is never read as evidence.
        private(ledger.parent / '.owner-observations-crash', good.decode()[:20])
        for damaged in (good[:len(good) // 2], b'{not json', b'{}'):
            ledger.write_bytes(damaged); ledger.chmod(0o600)
            self.assertEqual(self.change(target)[1], 'observation_unavailable')
            self.hook(session, target); self.assertEqual(self.lines(), [])
        ledger.write_bytes(good); ledger.chmod(0o644)
        self.assertEqual(self.change(target)[1], 'observation_unavailable')
        ledger.chmod(0o600)
        twin = self.home / 'ledger-twin.json'; os.link(ledger, twin)
        self.assertEqual(self.change(target)[1], 'observation_unavailable')
        twin.unlink()
        self.assertIsNone(self.change(target)[1])
        # A symlinked ledger is neither trusted nor written through.
        outside = self.home / 'outside.json'; outside.write_bytes(good)
        ledger.unlink(); ledger.symlink_to(outside)
        self.assertEqual(self.change(target)[1], 'observation_unavailable')
        self.hook(session, target, self.note(owner, 'docs/other.md'))
        self.assertEqual(outside.read_bytes(), good)
        self.assertTrue(ledger.is_symlink())
        self.assertEqual(self.lines(), [['beta-docs', str(owner / 'docs'), str(owner)]])

    def test_damaged_ledger_is_replaced_only_by_fresh_evidence(self):
        session = self.project('alpha'); owner = self.project('beta')
        self.store([session, owner])
        old = self.observed(session, owner)
        private(self.ledger_file(), '{not json')
        fresh = self.observed(session, owner, 'docs/fresh.md')
        self.assertEqual(list(self.ledger()), [str(fresh)])
        self.assertEqual(self.ledger_file().stat().st_mode & 0o777, 0o600)
        old.unlink()
        self.assertEqual(self.change(old)[1], 'no_prior_observation')

    def test_only_proved_present_files_are_recorded(self):
        import config as qmd_config
        session = self.project('alpha'); owner = self.project('beta')
        source = self.project('delta', {'delta-src': 'src'}, roles={'delta-src': 'source'})
        off = self.project('omega')
        real = self.note(owner); src = self.note(source, 'src/note.md'); dark = self.note(off)
        self.store([session, owner, source, off])
        qmd_config.write_local_optout(str(off))
        leaf = owner / 'docs/link.md'; leaf.symlink_to(real)
        self.hook(session, leaf, src, dark, owner / 'docs/absent.md')
        self.hook(source, src)  # cwd-owned but source role: not indexed.
        stray = self.note(self.home / 'unknown')
        self.hook(session, stray)
        self.assertEqual(self.ledger(), {})

    def test_ledger_is_bounded(self):
        import change_owner
        session = self.project('alpha'); owner = self.project('beta')
        self.store([session, owner])
        with patch.object(change_owner, 'LEDGER_MAX_ENTRIES', 2):
            first = self.observed(session, owner, 'docs/a.md')
            self.observed(session, owner, 'docs/b.md')
            self.observed(session, owner, 'docs/c.md')
            self.assertEqual(len(self.ledger()), 2)
            self.assertNotIn(str(first), self.ledger())


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ChangeOwner)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())

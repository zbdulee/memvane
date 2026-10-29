#!/usr/bin/env python3
"""Project folder move from ``.auto-context/`` to ``.memvane/``.

A real-shaped selected install in a temporary HOME: route, marker, ledger,
sqlite journal, a Laya index whose head generation is published from a real
catalog scan with a sealed vector set, the binding store and the claim queue.
Recall runs against the real stores (pinned generation, stored binding,
bound root identity). Only bundle identity reads and the per-user provider
lane are patched; git checkouts and worktrees are real.
"""
from __future__ import annotations

from contextlib import ExitStack, closing, nullcontext
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from laya_selected_project import SelectedProject, vector_profile  # noqa: E402

import config  # noqa: E402
import laya_bundle_upgrade as upgrade  # noqa: E402
import laya_fresh_wiki_origin as fresh  # noqa: E402
import laya_host_response as response  # noqa: E402
import laya_index_host_route as host_route  # noqa: E402
import laya_initial_build as build  # noqa: E402
import laya_install_journal as journal_mod  # noqa: E402
import laya_project_dir_move as dir_move  # noqa: E402
import laya_state_move  # noqa: E402
import laya_vector_capacity as vc  # noqa: E402
import laya_wiki_discovery as discovery  # noqa: E402
import project_dir  # noqa: E402
from laya_index_a import LayaIndexA  # noqa: E402
from laya_index_capacity import (GenerationBindingStore, binding_digest,  # noqa: E402
                                 open_pinned_candidate_query)
from laya_index_runtime import LayaIndexRuntime, capacity_store_paths  # noqa: E402
from laya_index_sync import sync_step  # noqa: E402
from laya_source_catalog import LayaSourceCatalog  # noqa: E402

SCOPE = '6' * 32
CARDS = {
    'concepts/otter-ledger.md': ('Otter ledger', 'otter ledger keeps the sealed river counts'),
    'concepts/heron-route.md': ('Heron route', 'heron route names the northern marsh'),
    'decisions/kestrel-plan.md': ('Kestrel plan', 'kestrel plan replaces the old tower'),
}


class Kill(BaseException):
    """A process death at one phase of the move."""


class FillProvider:
    """Precomputed vectors only; the profile equals the route's vector profile."""
    manifest = {'recipeId': vc.CANDIDATE_RECIPE}

    def __init__(self):
        profile = vector_profile()
        self.profile = {'model_id': profile['modelId'],
                        'embedding_fingerprint': profile['embeddingFingerprint'],
                        'dimension': profile['dimension']}

    def embed_document_chunks(self, body, path):
        return [{'seq': 0, 'pos': 0, 'vector': [1.0, 0.0, 0.0]}]


def card(title, body):
    return ('---\ntitle: "%s"\ntype: concept\nstatus: verified\ncreatedBy: memvane\n'
            'supersedes: ".auto-context/wiki/concepts/old-%s.md"\n---\n\n'
            '## Summary\n%s\n' % (title, title.split()[0].lower(), body))


def git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True,
                          text=True, env={**os.environ, 'GIT_AUTHOR_NAME': 't',
                                          'GIT_AUTHOR_EMAIL': 't@example.invalid',
                                          'GIT_COMMITTER_NAME': 't',
                                          'GIT_COMMITTER_EMAIL': 't@example.invalid'}
                          ).stdout


class MoveCase(SelectedProject):
    """A selected project on ``.auto-context/`` with a real published index."""

    def setUp(self):
        super().setUp()
        wiki = self.auto / 'wiki'
        for rel, (title, body) in CARDS.items():
            path = wiki / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(card(title, body), encoding='utf-8')
        self.catalog = Path(str(self.db) + '.catalog.sqlite')
        self.store = Path(str(self.db) + '.bindings.sqlite')
        roots = {self.WIKI: str(wiki)}
        LayaSourceCatalog(str(self.catalog), roots, SCOPE, create=True).close()
        GenerationBindingStore(str(self.store), scope=SCOPE, db_incarnation=self.incarnation,
                               roots=roots, create=True).close()
        self.publish_all(roots)
        self.rebind_fixture_ledger(roots)
        # The raw discovery cursor names both collection roots.
        decision = host_route.select_route(str(self.project), action='recall')
        self.assertEqual(decision['state'], 'candidate', decision)
        self.cursor, _grants, _lock = discovery._paths(decision)
        discovery._write(self.cursor, discovery._state_doc(decision, self.head()[0], {}, {}))
        stack = ExitStack()
        self.addCleanup(stack.close)
        rows = [[str(self.plugin / 'p.py'), 1, 2, 3, 4, 5]]
        stack.enter_context(mock.patch.object(journal_mod, '_provider_file_identity',
                                              return_value=rows))
        stack.enter_context(mock.patch.object(laya_state_move, '_provider_lane',
                                              lambda: nullcontext()))
        stack.enter_context(mock.patch.object(upgrade, 'upgrade_install',
                                              return_value={'status': 'current'}))
        stack.enter_context(mock.patch.object(upgrade, '_convert_journal',
                                              return_value={}))
        stack.enter_context(mock.patch.object(upgrade, '_fixture', return_value=False))
        self.spawned = []
        stack.enter_context(mock.patch.object(
            upgrade, '_spawn', side_effect=lambda project: self.spawned.append(project)))

    def runtime(self, roots):
        return LayaIndexRuntime(source_root=roots, scope=SCOPE, db_path=str(self.db),
                                queue_path=str(self.queue),
                                db_incarnation=self.incarnation,
                                catalog_path=str(self.catalog),
                                binding_store_path=str(self.store))

    def publish_all(self, roots):
        fill = FillProvider()
        profile_id = vc.provider_profile_id(fill)
        self.assertEqual(profile_id, vector_profile()['profileId'])
        with self.runtime(roots) as runtime:
            for _ in range(1000):
                result = sync_step(runtime, before_publish=lambda: None,
                                   require_vector=True, vector_profile_id=profile_id)
                if result['state'] == 'pending' and result.get('reason') == 'vector_backlog':
                    while vc.vector_fill_step(runtime, fill)['state'] == 'pending':
                        pass
                elif result['state'] != 'pending':
                    break
        self.assertIn(result['state'], ('indexed', 'unchanged'), result)
        return result

    def head(self):
        with closing(LayaIndexA(str(self.db))) as index:
            row = index.db.execute('SELECT gen FROM head WHERE scope=?', (SCOPE,)).fetchone()
            members = index.db.execute('SELECT path FROM membership WHERE gen=? ORDER BY path',
                                       (row[0],)).fetchall()
            index.db.commit()
        return row[0], [path for (path,) in members]

    def rebind_fixture_ledger(self, roots):
        gen, _members = self.head()
        with closing(GenerationBindingStore(str(self.store), scope=SCOPE,
                                            db_incarnation=self.incarnation,
                                            roots=roots)) as store:
            bound = store.get(SCOPE, gen, self.incarnation)
        identity = lambda path: [path.stat().st_dev, path.stat().st_ino]
        with closing(LayaIndexA(str(self.db))) as index:
            seal = index.db.execute('SELECT rows_count FROM gen_vector_seals WHERE gen=?',
                                    (gen,)).fetchone()
            index.db.commit()
        self.ledger = {**self.ledger, 'catalogDb': str(self.catalog),
                       'bindingStore': str(self.store),
                       'catalogIdentity': identity(self.catalog),
                       'bindingStoreIdentity': identity(self.store),
                       'indexGeneration': gen, 'bindingDigest': binding_digest(bound),
                       'sourceManifest': bound.source_manifest,
                       'catalogScanId': bound.catalog_scan_id,
                       'catalogRawDigest': bound.catalog_raw_digest,
                       'vectorRows': seal[0]}
        fresh._write(self.state / fresh.LEDGER, self.ledger, replace=True)
        record = {'projectRoot': str(self.project), 'stateRoot': str(self.state),
                  'installId': '3' * 32, 'scopeId': SCOPE,
                  'collections': self.collections(),
                  'candidateDb': str(self.db), 'queueDb': str(self.queue),
                  'catalogDb': str(self.catalog), 'bindingStore': str(self.store),
                  'dbIncarnation': self.incarnation, 'journalPath': str(self.journal),
                  'routeFile': str(self.state / 'reviewed-route.json'),
                  'routeSha256': self.ledger['routeSha256'],
                  'attestationSha256': self.ledger['attestationSha256'],
                  'vectorProfileId': self.ledger['vectorProfileId'],
                  'proposalSha256': 'b' * 64, 'providerProposalSha256': 'c' * 64,
                  'originMode': 'fresh_wiki_only'}
        build.create_or_verify(record)
        build.register_project(record)

    @staticmethod
    def provider(_prompt, route):
        return {'profileId': route['profileId'],
                'approvedProviderId': route['approvedProviderId'],
                'providerArtifactSha256': route['providerArtifactSha256'],
                'vector': [1.0, 0.0, 0.0]}

    def recall(self, decision, query='otter'):
        """One recall as the selected host serves it: the pinned head, its
        stored binding, the bound root identity and the published bodies."""
        proof = journal_mod.verify_selected_route_read_only(decision)
        self.assertTrue(proof['readOnlyAllowed'])
        roots = host_route.wiki_index_roots(decision['collections'])
        catalog, store = capacity_store_paths(decision['dbPath'])
        with LayaIndexRuntime(source_root=roots, scope=decision['scopeId'],
                              db_path=decision['dbPath'], queue_path=decision['queuePath'],
                              db_incarnation=decision['dbIncarnation'],
                              catalog_path=catalog, binding_store_path=store) as runtime:
            token, gen = runtime.index.pin(decision['scopeId'])
            try:
                candidates = open_pinned_candidate_query(runtime.index, token,
                                                         runtime.binding_store)
                try:
                    rows, _evidence = response._v2_phase(
                        runtime, token, query, decision['vectorProfile'], self.provider,
                        roots, tuple(roots), (), 0, (), candidates)
                    candidates.require_returned_verified()
                    candidates.finish()
                finally:
                    candidates.close()
            finally:
                runtime.index.release(token)
        return gen, rows

    def serve(self, decision, query='otter'):
        """Recall as the host retries it: a decision proved before the move
        is re-selected once and must be the same install after the move."""
        try:
            return self.recall(decision, query)
        except Exception:
            if not response._project_dir_moved(decision):
                raise
            fresh_decision = host_route.select_route_settled(str(self.project),
                                                             action='recall')
            if not response._relocated(decision, fresh_decision):
                raise
            return self.recall(fresh_decision, query)

    def move(self, **kwargs):
        return dir_move.move(str(self.project), str(self.state), **kwargs)

    def assert_moved(self):
        current = self.project / '.memvane'
        self.assertFalse(os.path.lexists(self.auto))
        self.assertTrue((current / 'settings.json').is_file())
        self.assertEqual(project_dir.layout(self.project), 'current')
        decision = self.decision()
        self.assertEqual(decision['state'], 'candidate', decision)
        wiki = next(item for item in decision['collections'] if item['role'] == 'wiki')
        self.assertEqual(wiki['sourceRoot'], str(current / 'wiki'))
        ledger = fresh._load(self.state / fresh.LEDGER)
        self.assertEqual(ledger['phase'], 'active')
        self.assertEqual(ledger['collections'], decision['collections'])
        record = build.read(self.state)
        self.assertEqual(record['identity']['collections'], decision['collections'])
        discovery._validate_state(json.loads(self.cursor.read_text()), decision)
        self.assertFalse(os.path.lexists(self.state / dir_move.INTENT))
        self.assertIsNone(dir_move.read_pointer(self.project))
        gen, members = self.head()
        self.assertEqual(ledger['indexGeneration'], gen)
        self.assertEqual(members, sorted(self.WIKI + '/' + rel for rel in CARDS))
        _gen, rows = self.recall(decision)
        self.assertEqual([row['path'] for row in rows][:1],
                         [self.WIKI + '/concepts/otter-ledger.md'])
        return decision


class MigrationTests(MoveCase):
    def test_project_moves_while_concurrent_recall_never_fails(self):
        failures, served = [], []
        before_gen, before_members = self.head()
        done = threading.Event()

        def reader():
            while not done.is_set():
                decision = host_route.select_route_settled(str(self.project),
                                                           action='recall')
                if decision['state'] != 'candidate':
                    failures.append(decision.get('reason'))
                    continue
                try:
                    gen, rows = self.serve(decision)
                except Exception as exc:  # noqa: BLE001
                    failures.append(repr(exc))
                    continue
                if not rows:
                    failures.append('empty')
                served.append(gen)
        thread = threading.Thread(target=reader)
        thread.start()
        try:
            time.sleep(0.3)
            result = self.move(step=lambda _name: time.sleep(0.02))
            time.sleep(0.3)
        finally:
            done.set()
            thread.join()
        self.assertEqual(failures, [])
        self.assertEqual(result['status'], 'project_dir_move_complete', result)
        self.assertEqual(result['mode'], 'rename')
        decision = self.assert_moved()
        after_gen, after_members = self.head()
        # Explicit rebind: a new generation with the same members, no re-embedding.
        self.assertNotEqual(after_gen, before_gen)
        self.assertEqual(after_members, before_members)
        self.assertIn(before_gen, served)
        self.assertEqual(served[-1], after_gen)
        self.assertEqual(decision['collectionRoots'][self.WIKI],
                         str(self.project / '.memvane' / 'wiki'))

    def test_worker_moves_the_folder_at_session_start(self):
        notice = response._project_dir_move_notice(self.decision())
        self.assertIn('.auto-context/ to .memvane/', notice)
        self.assertEqual(self.spawned, [str(self.project)])
        results = upgrade.run(str(self.project))
        self.assertEqual(results[0]['status'], 'project_dir_move_complete', results)
        self.assert_moved()
        steps = response._project_dir_move_notice(self.decision())
        self.assertIn('git add -A .memvane .auto-context', steps)
        again = upgrade.run(str(self.project))
        self.assertEqual(again[0]['status'], 'current', again)

    def test_card_bytes_are_untouched_and_legacy_refs_resolve(self):
        before = {rel: (self.auto / 'wiki' / rel).read_bytes() for rel in CARDS}
        self.move()
        for rel, raw in before.items():
            self.assertEqual((self.project / '.memvane/wiki' / rel).read_bytes(), raw)
        # A card that names another by its .auto-context/wiki path resolves
        # to the moved card (compile target resolution, frontmatter refs).
        self.assertEqual(project_dir.rebase_rel('.auto-context/wiki/concepts/x.md',
                                                self.project), '.memvane/wiki/concepts/x.md')

    def test_gitignore_suggestion_is_printed_never_written(self):
        ignore = self.project / '.gitignore'
        ignore.write_text('.auto-context/compile/\n.auto-context/laya-index-route.json\n')
        before = ignore.read_bytes()
        self.move()
        lines = dir_move.owner_steps(self.project)
        self.assertIn('git add -A .memvane .auto-context', lines[0])
        self.assertIn('.memvane/compile/', lines[1])
        self.assertIn('.memvane/laya-index-route.json', lines[1])
        self.assertEqual(ignore.read_bytes(), before)

    def test_recall_output_names_cards_under_memvane(self):
        self.move()
        decision = self.decision()
        gen, rows = self.recall(decision)
        shaped = [{'scope': SCOPE, 'generation': gen, 'path': row['path'],
                   'hash': row['hash'], 'body': row['body'], 'score': 0.5}
                  for row in rows[:1]]
        context = response.render_context(decision, shaped)
        self.assertIn(self.WIKI + '/concepts/otter-ledger.md', context)
        self.assertNotIn('.auto-context', context)
        path = shaped[0]['path'].split('/', 1)[1]
        target = Path(decision['collectionRoots'][self.WIKI]) / path
        self.assertTrue(str(target).startswith(str(self.project / '.memvane' / 'wiki')))
        self.assertTrue(target.is_file())


class CrashTests(MoveCase):
    PHASES = ('prepared', 'fenced', 'moved', 'stores', 'published', 'records',
              'journal', 'route', 'marker', 'ledger')

    def check_resume(self, phase):
        def die(name):
            if name == phase:
                raise Kill(name)
        with self.assertRaises(Kill):
            self.move(step=die)
        if phase != 'prepared':
            # Past the fence a reader waits for the moved route; the next run resumes.
            self.assertIsNotNone(dir_move.read_pointer(self.project))
        result = self.move()
        self.assertEqual(result['status'], 'project_dir_move_complete', result)
        self.assert_moved()

    def test_every_phase_resumes(self):
        for phase in self.PHASES:
            with self.subTest(phase=phase):
                self.tearDown_and_setUp()
                self.check_resume(phase)

    def tearDown_and_setUp(self):
        self.doCleanups()
        self.setUp()

    def test_crash_before_the_fence_leaves_recall_on_the_old_folder(self):
        def die(name):
            if name == 'prepared':
                raise Kill(name)
        with self.assertRaises(Kill):
            self.move(step=die)
        decision = self.decision()
        self.assertEqual(decision['state'], 'candidate', decision)
        _gen, rows = self.recall(decision)
        self.assertTrue(rows)
        self.assertTrue(self.auto.is_dir())


class LegacyFallbackTests(MoveCase):
    def test_project_not_moved_is_served_from_auto_context(self):
        self.assertEqual(project_dir.layout(self.project), 'legacy')
        self.assertEqual(project_dir.active_name(self.project), '.auto-context')
        found = config.find_project_config(str(self.project))
        self.assertEqual(found['configPath'], str(self.auto / 'settings.json'))
        self.assertEqual(found['config']['wikiPath'], '.auto-context/wiki')
        decision = self.decision()
        self.assertEqual(decision['state'], 'candidate', decision)
        gen, rows = self.recall(decision)
        self.assertEqual(rows[0]['path'], self.WIKI + '/concepts/otter-ledger.md')
        self.assertFalse(os.path.lexists(self.project / '.memvane'))

    def test_memvane_settings_win_over_the_legacy_folder(self):
        current = self.project / '.memvane'
        current.mkdir()
        (current / 'settings.json').write_text(json.dumps({'name': 'other', 'skipPaths': []}))
        found = config.find_project_config(str(self.project))
        self.assertEqual(found['configPath'], str(current / 'settings.json'))
        self.assertEqual(found['config']['wikiPath'], '.memvane/wiki')
        self.assertEqual(project_dir.layout(self.project), 'conflict')
        self.assertIn('ignores .auto-context/', dir_move.layout_notice(self.project))


class GitBranchTests(MoveCase):
    def setUp(self):
        super().setUp()
        git(self.project, 'init', '-q', '-b', 'before')
        git(self.project, 'add', '.auto-context/settings.json', '.auto-context/wiki')
        git(self.project, 'commit', '-q', '-m', 'before the move')

    def commit_move(self):
        self.move()
        git(self.project, 'checkout', '-q', '-b', 'moved')
        git(self.project, 'add', '-A', '.memvane/settings.json', '.memvane/wiki',
            '.auto-context')
        git(self.project, 'commit', '-q', '-m', 'move the folder')
        status = git(self.project, 'show', '--stat', '-M', '--format=', 'HEAD')
        self.assertIn('=> .memvane', status)

    def test_rename_is_a_git_directory_rename(self):
        self.commit_move()
        renames = git(self.project, 'diff', '-M', '--name-status', 'before', 'moved')
        self.assertTrue(all(line.startswith('R100') for line in renames.splitlines()),
                        renames)
        self.assertEqual(len(renames.splitlines()), 1 + len(CARDS))

    def test_older_branch_checkout_keeps_the_index_and_holds_writers(self):
        self.commit_move()
        gen, members = self.head()
        # An uncommitted card keeps .memvane/wiki alive across the checkout.
        extra = self.project / '.memvane/wiki/concepts/untracked-note.md'
        extra.write_text(card('Untracked note', 'untracked kingfisher note'))
        git(self.project, 'checkout', '-q', 'before')
        self.assertEqual(project_dir.layout(self.project), 'split')
        self.assertTrue((self.auto / 'settings.json').is_file())
        # Writers hold: no sync publishes the partial .memvane/wiki as deletions.
        update = host_route.select_route(str(self.project), action='update')
        self.assertEqual(update, {'state': 'blocked', 'reason': 'project_dir_split'})
        self.assertEqual(host_route.select_route(str(self.project), action='index')['reason'],
                         'project_dir_split')
        # Search keeps the published index (deterministic precedence: .memvane).
        decision = self.decision()
        self.assertEqual(decision['state'], 'candidate', decision)
        self.assertEqual(decision['collectionRoots'][self.WIKI],
                         str(self.project / '.memvane' / 'wiki'))
        _gen, rows = self.recall(decision)
        self.assertTrue(rows)
        # No second index, no re-move, the index is unchanged.
        self.assertIsNone(dir_move.needed(self.project))
        self.assertFalse(os.path.lexists(self.auto / host_route.RECORD))
        self.assertEqual([row['status'] for row in upgrade.run(str(self.project))],
                         ['current'])
        self.assertEqual(project_dir.layout(self.project), 'split')
        self.assertEqual(self.head(), (gen, members))
        payload = self.home / 'payload.json'
        payload.write_text(json.dumps({'cwd': str(self.project)}))
        self.assertIn('switch back', host_route.project_dir_notice(str(payload),
                                                                  str(self.project)))
        git(self.project, 'checkout', '-q', 'moved')
        self.assertEqual(project_dir.layout(self.project), 'current')
        self.assertEqual(self.head(), (gen, members))
        _gen, rows = self.recall(self.decision())
        self.assertTrue(rows)

    def test_older_branch_with_the_wiki_gone_is_blocked_not_emptied(self):
        self.commit_move()
        gen, members = self.head()
        git(self.project, 'checkout', '-q', 'before')
        self.assertFalse((self.project / '.memvane/wiki').exists())
        self.assertEqual(project_dir.layout(self.project), 'split')
        self.assertEqual(self.decision()['state'], 'blocked')
        self.assertEqual(host_route.select_route(str(self.project), action='update')['reason'],
                         'project_dir_split')
        self.assertEqual(self.head(), (gen, members))
        git(self.project, 'checkout', '-q', 'moved')
        # git recreated .memvane/wiki (a new inode): the next sync rebinds it
        # with the same members and no re-embedding.
        self.assertEqual(self.decision()['state'], 'candidate')
        self.publish_all({self.WIKI: str(self.project / '.memvane' / 'wiki')})
        new_gen, new_members = self.head()
        self.assertEqual(new_members, members)
        _gen, rows = self.recall(self.decision())
        self.assertTrue(rows)

    def test_linked_worktrees_on_either_folder_share_the_main_index(self):
        self.commit_move()
        old_tree = self.home / 'wt-before'
        new_tree = self.home / 'wt-moved'
        git(self.project, 'worktree', 'add', '-q', str(old_tree), 'before')
        git(self.project, 'worktree', 'add', '-q', '-b', 'moved-2', str(new_tree), 'moved')
        for tree, folder in ((old_tree, '.auto-context'), (new_tree, '.memvane')):
            with self.subTest(folder=folder):
                self.assertTrue((tree / folder / 'settings.json').is_file())
                decision = host_route.select_route(str(tree), action='recall')
                self.assertEqual(decision['state'], 'worktree', decision)
                main = decision['mainRoute']
                self.assertEqual(main['projectRoot'], str(self.project))
                _gen, rows = self.recall(main)
                self.assertTrue(rows)

    def test_a_pulled_rename_is_adopted(self):
        # Another checkout committed the move; this one pulls it while its
        # runtime files stay in .auto-context/.
        other = self.home / 'other'
        git(self.home, 'clone', '-q', str(self.project), str(other))
        shutil.move(str(other / '.auto-context'), str(other / '.memvane'))
        git(other, 'add', '-A', '.memvane', '.auto-context')
        git(other, 'commit', '-q', '-m', 'move elsewhere')
        git(self.project, 'pull', '-q', '--no-rebase', str(other), 'before')
        self.assertEqual(project_dir.layout(self.project), 'adopt')
        self.assertEqual(self.decision()['state'], 'blocked')
        payload = self.home / 'payload.json'
        payload.write_text(json.dumps({'cwd': str(self.project)}))
        self.assertIn('.memvane/', host_route.project_dir_notice(str(payload),
                                                                str(self.project)))
        results = upgrade.run(str(self.project))
        self.assertEqual(results[0]['status'], 'project_dir_move_complete', results)
        self.assertEqual(project_dir.layout(self.project), 'current')
        self.assert_moved()


def tearDownModule():
    pass


if __name__ == '__main__':
    unittest.main(verbosity=2)

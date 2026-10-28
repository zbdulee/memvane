#!/usr/bin/env python3
"""MemVane plugin-id switch: adoption, the route fence and the version rule.

The new plugin id's bundle adopts a selected install bound to the earlier
plugin id's bundle through the ordinary active bundle upgrade (the harness of
``laya-fresh-bundle-upgrade-checks.py``: real ledger, marker, route and
stores under a temporary HOME). The route it writes must be unreadable to the
0.40.3 code, which is taken from git history (``git archive``) into a
temporary directory and run in a child process: it must classify the
adopted route as blocked and refuse to stage it, so it can neither serve nor
move the install back. ``_not_older`` decides across plugin ids by semver and
refuses whatever it cannot decide.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tarfile
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / 'core'))
UPGRADE = runpy.run_path(str(HERE / 'laya-fresh-bundle-upgrade-checks.py'))

import laya_active_upgrade as active  # noqa: E402
import laya_bundle_upgrade as auto  # noqa: E402
import laya_fresh_wiki_origin as fresh  # noqa: E402
import laya_index_host_route as host_route  # noqa: E402

# Last release under the earlier plugin id (``qmd-auto-context`` 0.40.3).
OLD_RELEASE = 'f9c33094e30d523415fe983ec7f3b4c35d5a21c0'
OLD_COMMIT, NEW_COMMIT = '1' * 40, '2' * 40

OLD_PROBE = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import laya_index_host_route as route
import laya_install_journal as journal
project = Path(sys.argv[2])
auto = project / '.auto-context'
decision = route._classify_record(project, auto, auto / route.RECORD, project)
raw = (auto / route.RECORD).read_bytes()
try:
    journal._route(raw, Path(json.loads(raw)['journalPath']))
    staged = 'accepted'
except Exception as exc:
    staged = str(exc)
print(json.dumps({'state': decision['state'], 'reason': decision['reason'],
                  'staged': staged}))
'''


def old_core(target):
    """0.40.3's ``core/`` from git history, extracted under ``target``."""
    archive = subprocess.run(('git', '-C', str(REPO), 'archive', '--format=tar',
                              OLD_RELEASE, 'core'), check=True,
                             capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(target, filter='data')
    return Path(target) / 'core'


def derived_profile(profile):
    """The harness profile with a ``profileId`` derived from its fields, so a
    route probe gets past profile validation to the checks under test."""
    canonical = json.dumps([profile['modelId'], profile['embeddingFingerprint'],
                            profile['dimension'], 'cosine'], sort_keys=True,
                           ensure_ascii=False, separators=(',', ':')).encode()
    return {**profile, 'profileId': hashlib.sha256(canonical).hexdigest()}


def git(root, *args):
    return subprocess.run(('git', '-C', str(root), *args), check=True,
                          capture_output=True, text=True).stdout.strip()


def repo(path, files):
    path.mkdir(parents=True)
    git(path, 'init', '-q')
    git(path, 'config', 'user.email', 'fixture@example.invalid')
    git(path, 'config', 'user.name', 'fixture')
    return commit(path, files)


def commit(path, files):
    for name, body in files.items():
        (path / name).write_text(body)
    git(path, 'add', '-A')
    git(path, 'commit', '-q', '-m', 'fixture')
    return git(path, 'rev-parse', 'HEAD')


def bundle(home, host, marketplace, plugin, version):
    root = home / host / 'plugins' / 'cache' / marketplace / plugin / version
    root.mkdir(parents=True)
    (root / 'package.json').write_text(json.dumps({'name': plugin, 'version': version}))
    return root


class CrossPluginIdAdoption(UPGRADE['UpgradeCase']):
    """A selected install bound to ``qmd-auto-context`` 0.40.3 moves to ``memvane``."""

    def setUp(self):
        super().setUp()
        cache = self.home / '.claude' / 'plugins' / 'cache'
        self.old_root = str(cache / 'qmd-auto-context-marketplace' /
                            'qmd-auto-context' / '0.40.3')
        self.new_root = str(cache / 'memvane-marketplace' / 'memvane' / '0.41.0')
        self.old = UPGRADE['proof'](self.old_root, OLD_COMMIT)
        self.new = UPGRADE['proof'](self.new_root, NEW_COMMIT)
        self.bundles = {self.old_root: self.old, self.new_root: self.new}
        self.fj.pending = UPGRADE['identity'](self.old)
        self.write_attestation(self.old, UPGRADE['manifest'](self.old_root))
        self.review = {'proposalSha256': 'e' * 64,
                       'reviewedProvider': {'vectorProfile': UPGRADE['PROFILE'],
                                            'providerManifest':
                                                UPGRADE['manifest'](self.new_root),
                                            'dependencyClosure': []}}
        self.bound()
        self.activate()
        tmp = tempfile.TemporaryDirectory(prefix='mv-old-')
        self.addCleanup(tmp.cleanup)
        self.old_code = old_core(tmp.name)

    def probe(self):
        """(new decision, 0.40.3 view) of the project's route.

        The synthetic harness profile is not derivable, so the probe reads
        the same route bytes with a derived profile and restores the file.
        """
        original = self.route.read_bytes()
        route = json.loads(original)
        route['vectorProfile'] = derived_profile(route['vectorProfile'])
        self.route.write_bytes(json.dumps(route, sort_keys=True,
                                          separators=(',', ':')).encode())
        try:
            decision = host_route._classify_record(self.project, self.auto,
                                                   self.route, self.project)
            env = {**os.environ, 'HOME': str(self.home), 'QMD_SANDBOX': '1',
                   'PYTHONDONTWRITEBYTECODE': '1'}
            done = subprocess.run((sys.executable, '-B', '-c', OLD_PROBE,
                                   str(self.old_code), str(self.project)),
                                  capture_output=True, text=True, env=env, check=False)
        finally:
            self.route.write_bytes(original)
        self.assertEqual(done.returncode, 0, done.stderr)
        return decision, json.loads(done.stdout)

    def test_old_bundle_reads_its_own_route_before_adoption(self):
        # Control: the fence below is the schema, not a broken fixture.
        route = json.loads(self.route.read_bytes())
        self.assertEqual(route['schema'], host_route.SCHEMA_V2)
        self.assertEqual(route['pluginRoot'], self.old_root)
        decision, view = self.probe()
        self.assertEqual(decision['state'], 'candidate', decision)
        self.assertEqual((view['state'], view['staged']), ('candidate', 'accepted'), view)

    def test_new_id_bundle_adopts_and_old_bundle_cannot_take_it_back(self):
        before = self.row()
        self.removed.add(self.old_root)  # the old id is uninstalled right after
        result = self.run_upgrade(active, self.new_root, NEW_COMMIT)
        self.assertEqual(result['status'], 'active_upgrade_complete', result)
        route = json.loads(self.route.read_bytes())
        self.assertEqual((route['schema'], route['pluginRoot']),
                         (host_route.SCHEMA_V3, self.new_root))
        after = self.row()
        self.assertTrue(self.unchanged_fields(before, after, active.LEDGER_CHANGES))
        self.assertEqual(self.selection(), 'candidate')
        self.assertEqual(fresh._read_json(self.marker), fresh._marker(after))
        self.kept()
        # This release serves the adopted route; 0.40.3 neither serves it nor
        # stages a route from it.
        decision, view = self.probe()
        self.assertEqual(decision['state'], 'candidate', decision)
        self.assertEqual((view['state'], view['reason']),
                         ('blocked', 'invalid_route_fields'), view)
        self.assertEqual(view['staged'], 'route_contract_mismatch', view)

    def test_reviewed_route_of_a_bound_build_is_fenced_too(self):
        # A bound build's reviewed route is what the 0.40.3 bound/inactive
        # upgrade re-reads; once rewritten by this release it is v3 as well.
        route = json.loads((self.state / 'reviewed-route.json').read_bytes())
        self.assertEqual(route['schema'], host_route.SCHEMA_V2)
        self.run_upgrade(active, self.new_root, NEW_COMMIT)
        route = json.loads((self.state / 'reviewed-route.json').read_bytes())
        self.assertEqual(route['schema'], host_route.SCHEMA_V3)


class VersionRule(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='mv-ver-')
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.old = bundle(self.home, '.claude', 'qmd-auto-context-marketplace',
                          'qmd-auto-context', '0.40.3')
        self.new = bundle(self.home, '.claude', 'memvane-marketplace', 'memvane', '0.41.0')

    def test_semver_key_orders_prereleases_first(self):
        key = auto.semver_key
        self.assertLess(key('0.40.3'), key('0.41.0'))
        self.assertLess(key('0.41.0-dev.1'), key('0.41.0'))
        self.assertLess(key('0.9.9'), key('0.10.0'))
        # SemVer precedence: numeric identifiers numerically, numeric before
        # alphanumeric, a shorter equal prefix first; build metadata ignored.
        self.assertLess(key('0.41.0-dev.2'), key('0.41.0-dev.10'))
        self.assertLess(key('1.0.0-alpha'), key('1.0.0-alpha.1'))
        self.assertLess(key('1.0.0-1'), key('1.0.0-alpha'))
        self.assertEqual(key('0.41.0+build.7'), key('0.41.0'))
        self.assertIsNone(key('latest'))
        self.assertIsNone(key('1.0.0-'))

    def test_bundle_version_falls_back_to_the_cache_directory_name(self):
        self.assertEqual(auto.bundle_version(self.new), '0.41.0')
        (self.old / 'package.json').unlink()
        self.assertEqual(auto.bundle_version(self.old), '0.40.3')
        self.assertIsNone(auto.bundle_version(self.home / 'checkout'))

    def test_new_id_wins_when_its_clone_does_not_know_the_bound_commit(self):
        # The marketplace clone of one id never fetched the other id's commit.
        source = self.home / 'memvane-clone'
        head = repo(source, {'a.txt': 'memvane\n'})
        foreign = '9' * 40
        auto._not_older(source, foreign, head, bound_root=self.old, root=self.new)

    def test_old_id_never_downgrades_a_newer_bound_bundle(self):
        source = self.home / 'old-clone'
        head = repo(source, {'a.txt': 'qmd\n'})
        with self.assertRaisesRegex(auto.UpgradeRefused, '^older_host_bundle$'):
            auto._not_older(source, '9' * 40, head, bound_root=self.new, root=self.old)

    def test_undecidable_is_refused(self):
        source = self.home / 'clone'
        head = repo(source, {'a.txt': '1\n'})
        with self.assertRaisesRegex(auto.UpgradeRefused, '^older_host_bundle$'):
            auto._not_older(source, '9' * 40, head)
        same = bundle(self.home, '.codex', 'memvane-marketplace', 'memvane', '0.41.0')
        with self.assertRaisesRegex(auto.UpgradeRefused, '^older_host_bundle$'):
            auto._not_older(source, '9' * 40, head, bound_root=same, root=self.new)

    def test_known_history_still_refuses_an_ancestor_whatever_the_versions(self):
        source = self.home / 'history'
        older = repo(source, {'a.txt': '1\n'})
        newer = commit(source, {'a.txt': '2\n'})
        with self.assertRaisesRegex(auto.UpgradeRefused, '^older_host_bundle$'):
            auto._not_older(source, newer, older, bound_root=self.old, root=self.new)
        auto._not_older(source, older, newer, bound_root=self.old, root=self.new)

    def test_diverged_history_with_a_lower_version_is_refused(self):
        source = self.home / 'diverged'
        base = repo(source, {'a.txt': '1\n'})
        git(source, 'checkout', '-q', '-b', 'side')
        side = commit(source, {'b.txt': 'side\n'})
        git(source, 'checkout', '-q', base)
        other = commit(source, {'c.txt': 'other\n'})
        with self.assertRaisesRegex(auto.UpgradeRefused, '^older_host_bundle$'):
            auto._not_older(source, side, other, bound_root=self.new, root=self.old)
        auto._not_older(source, side, other, bound_root=self.old, root=self.new)
        # Diverged at the same version: neither side may take the install.
        twin = bundle(self.home, '.codex', 'memvane-marketplace', 'memvane', '0.41.0')
        for bound_commit, commit_id in ((side, other), (other, side)):
            with self.assertRaisesRegex(auto.UpgradeRefused, '^older_host_bundle$'):
                auto._not_older(source, bound_commit, commit_id,
                                bound_root=self.new, root=twin)
        # Diverged without versions: undecidable, refused.
        with self.assertRaisesRegex(auto.UpgradeRefused, '^older_host_bundle$'):
            auto._not_older(source, side, other)


class LegacyRoots(unittest.TestCase):
    """Fresh accounts get only MemVane roots; existing state keeps working."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='mv-roots-')
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)

    def test_auto_context_state_stays_where_its_scan_state_is(self):
        import memvane_paths
        current = self.home / '.local' / 'state' / 'memvane' / 'auto-context'
        self.assertEqual(memvane_paths.auto_context_state(self.home), current)
        legacy = self.home / '.local' / 'state' / 'laya' / 'auto-context'
        (legacy / 'scan-state').mkdir(parents=True)
        self.assertEqual(memvane_paths.auto_context_state(self.home), legacy)
        # The resolver run-lock creating the MemVane directory changes nothing.
        (current / 'dedup-resolve-lock').mkdir(parents=True)
        self.assertEqual(memvane_paths.auto_context_state(self.home), legacy)
        (current / 'scan-state').mkdir()
        self.assertEqual(memvane_paths.auto_context_state(self.home), current)

    def test_learning_group_follows_the_activated_runtime(self):
        from context_learning import runtime_setup, storage_layout
        support = ('Library', 'Application Support') if sys.platform == 'darwin' \
            else ('.local', 'share')
        current = self.home.joinpath(*support, 'memvane')
        legacy = self.home.joinpath(*support, 'qmd-auto-context')

        def activate(base):
            runtime = base / 'runtimes' / 'laya'
            runtime.mkdir(parents=True, exist_ok=True)
            pointer = runtime / 'active.json'
            pointer.write_text(json.dumps({
                'executable': str(runtime / 'generations' / 'g-1' / 'venv' / 'bin' / 'python'),
                'runtime_identity_sha256': 'a' * 64}))
            pointer.chmod(0o600)
            return pointer
        self.assertEqual(runtime_setup.support_base(self.home), current)
        self.assertEqual(runtime_setup.default_managed_root(self.home),
                         current / 'runtimes' / 'laya')
        (legacy / 'runtimes' / 'laya').mkdir(parents=True)
        self.assertEqual(runtime_setup.support_base(self.home), current)  # nothing active
        activate(legacy)
        self.assertEqual(runtime_setup.support_base(self.home), legacy)
        # An interrupted MemVane prepare (bare directory) never hides it.
        (current / 'runtimes' / 'laya' / 'generations').mkdir(parents=True)
        self.assertEqual(runtime_setup.support_base(self.home), legacy)
        bad = current / 'runtimes' / 'laya' / 'active.json'
        bad.write_text('{"executable": "/elsewhere/python", "runtime_identity_sha256": "x"}')
        bad.chmod(0o600)
        self.assertEqual(runtime_setup.support_base(self.home), legacy)  # not inside root
        activate(current)
        self.assertEqual(runtime_setup.support_base(self.home), current)

    def test_learning_registry_stays_with_its_database(self):
        from context_learning import storage_layout
        app = self.home / 'Library' / 'Application Support'
        current, legacy = app / 'memvane' / 'learning', app / 'qmd-auto-context' / 'learning'
        self.assertEqual(storage_layout.root(self.home), current)
        legacy.mkdir(parents=True)
        (legacy / 'registry.sqlite3').write_bytes(b'')
        current.mkdir(parents=True)  # a bare directory hides nothing
        self.assertEqual(storage_layout.root(self.home), legacy)
        (current / 'registry.sqlite3').write_bytes(b'')
        self.assertEqual(storage_layout.root(self.home), current)

    def test_legacy_adapter_cache_is_an_identity_anchor_only(self):
        from context_learning import adapter_binding as binding
        def cache(market, plugin, version='0.40.3'):
            root = self.home / '.claude' / 'plugins' / 'cache' / market / plugin / version
            adapter = root / 'core' / 'context_learning' / 'laya_adapter.py'
            adapter.parent.mkdir(parents=True)
            adapter.write_text('ADAPTER = 1\n')
            for rel in ('.claude-plugin/plugin.json', 'package.json'):
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(json.dumps({'name': plugin, 'version': version}))
            return adapter
        old = cache('qmd-auto-context-marketplace', 'qmd-auto-context')
        new = cache('memvane-marketplace', 'memvane', '0.41.0')
        self.assertEqual(binding._official_cache(old, self.home)[3],
                         binding._official_cache(new, self.home)[3])
        with self.assertRaisesRegex(ValueError, 'unsupported_adapter'):
            binding._official_cache(cache('other-marketplace', 'qmd-auto-context'),
                                    self.home)
        self.assertEqual(binding.PLUGIN_ID, 'memvane@memvane-marketplace')


if __name__ == '__main__':
    unittest.main()

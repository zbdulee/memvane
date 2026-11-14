"""Plan and prepare of a fresh wiki-only install: the decisions prepare refuses on.

The plan reviews minimal settings only and binds the exact settings bytes, the
project path and inode (never ``st_dev``) into ``scopeId``; prepare registers a
build only for the proposal and provider review the user accepted, after the
raw tree passed the sync's scan bounds, and replaces reviewed foreign pointers
only once that registration succeeded. The onboarding lifecycle end to end is
``tests/e2e/onboarding_fixture.py``.
"""
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from memvane.install import api as install
from memvane.install.application import onboarding_restore, onboarding_workflow as workflow
from memvane.install.types import InstallBlocked
from memvane.install.tests.support import INSTALL_ID, MISSING_INSTALL, InstallCase, state_files
from tests.support.fake_provider import REVIEW_SHA


class Plan(InstallCase):
    def test_plan_reviews_a_wiki_only_origin_from_minimal_settings(self):
        before = sorted(p.name for p in self.folder.iterdir())
        plan = install.plan_fresh_wiki_origin(self.project)
        self.assertEqual((plan['status'], plan['schema'], plan['mode'], plan['sourceInventory']),
                         ('review_required', 'laya-fresh-wiki-origin-plan-v1', 'fresh_wiki_only',
                          'durable_wiki_catalog_scan'))
        self.assertEqual(sorted((item['role'], item['sourceRoot']) for item in plan['collections']),
                         [('raw', str(self.project)), ('wiki', str(self.folder / 'wiki'))])
        self.assertEqual(plan['settingsSha256'],
                         hashlib.sha256(self.fixture.settings_path.read_bytes()).hexdigest())
        self.assertNotIn('replacesForeignPointers', plan)
        # Read only and repeatable: the same review, and nothing written anywhere.
        self.assertEqual(install.plan_fresh_wiki_origin(self.project), plan)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), before)
        self.assertEqual(state_files(self.install().state), [])

    def test_settings_other_than_minimal_refuse_and_stay_unchanged(self):
        cases = (
            (b'{"name":"fixture","skipPaths":[],"compile":{"enabled":true}}\n', 'minimal_settings_required'),
            (b'{"name":"fixture","skipPaths":"drafts"}\n', 'minimal_settings_required'),
            (b'{"collections":{"notes":{"path":"."}},"name":"fixture","skipPaths":[]}\n',
             'minimal_settings_required'),
            (b'{"name":"fixture","name":"other","skipPaths":[]}\n', 'duplicate_settings_key'),
        )
        for raw, reason in cases:
            with self.subTest(raw=raw):
                self.fixture.settings_path.write_bytes(raw)
                self.blocked(reason, install.plan_fresh_wiki_origin, self.project)
                self.assertEqual(self.fixture.settings_path.read_bytes(), raw)

    def test_project_root_must_be_its_own_real_absolute_path(self):
        alias = self.home / 'alias'
        alias.symlink_to(self.project)
        for root in (alias, Path(os.path.relpath(self.project)), self.project / '..' / self.project.name):
            with self.subTest(root=str(root)):
                self.blocked('invalid_project_root', install.plan_fresh_wiki_origin, root)

    def test_scope_id_seed_is_path_inode_and_settings_bytes_without_st_dev(self):
        plan = install.plan_fresh_wiki_origin(self.project)
        settings_sha = hashlib.sha256(self.fixture.settings_path.read_bytes()).hexdigest()
        seed = json.dumps(['fresh-wiki-only', str(self.project), self.project.stat().st_ino, settings_sha],
                          separators=(',', ':')).encode()
        self.assertEqual(plan['scopeId'], hashlib.sha256(seed).hexdigest()[:32])
        # Other settings bytes are another plan; the same bytes again are the same plan.
        original = self.fixture.settings_path.read_bytes()
        self.fixture.write_settings(skip_paths=['drafts'])
        changed = install.plan_fresh_wiki_origin(self.project)
        self.assertNotEqual((changed['scopeId'], changed['proposalSha256']),
                            (plan['scopeId'], plan['proposalSha256']))
        self.fixture.settings_path.write_bytes(original)
        self.assertEqual(install.plan_fresh_wiki_origin(self.project), plan)


class Prepare(InstallCase):
    def prepare(self, target, **overrides):
        request = dict(project_root=str(self.project), plugin_root=str(Path(install.__file__).parents[2]),
                       state_root=str(target.state),
                       expected_proposal_sha256=install.plan_fresh_wiki_origin(self.project)['proposalSha256'],
                       install_id=target.install_id, accepted_provider_sha256=REVIEW_SHA,
                       dimension=target.provider.dimension, provider=target.provider)
        request.update(overrides)
        return install.prepare_fresh_wiki_origin(**request)

    def assert_nothing_registered(self, target):
        from memvane.journal.api import build_lookup
        self.assertIsNone(build_lookup(str(self.project)))
        self.assertFalse([name for name in state_files(target.state)
                          if name in ('journal.sqlite', 'store.sqlite', 'route.json', 'build.json')])
        self.assertEqual(self.pointers(), {})

    def test_prepare_registers_only_the_reviewed_proposal(self):
        target = self.install()
        reviewed = install.plan_fresh_wiki_origin(self.project)['proposalSha256']
        other = reviewed[:-1] + ('0' if reviewed[-1] != '0' else '1')
        self.blocked('reviewed_origin_plan_changed', self.prepare, target, expected_proposal_sha256=other)
        self.assert_nothing_registered(target)
        # Settings changed after the review: the reviewed digest no longer matches.
        self.fixture.write_settings(skip_paths=['drafts'])
        self.blocked('reviewed_origin_plan_changed', self.prepare, target, expected_proposal_sha256=reviewed)
        self.assert_nothing_registered(target)
        result = self.prepare(target)
        self.assertEqual((result['status'], result['proposalSha256'], result['buildPhase']),
                         ('initial_build_registered',
                          install.plan_fresh_wiki_origin(self.project)['proposalSha256'], 'registered'))

    def test_prepare_registers_only_the_accepted_provider_review(self):
        target = self.install()
        self.blocked('reviewed_provider_proposal_changed', self.prepare, target,
                     accepted_provider_sha256='a' * 64)
        self.assert_nothing_registered(target)
        self.assertEqual(self.prepare(target)['status'], 'initial_build_registered')

    def test_malformed_requests_refuse_before_any_state_root(self):
        target = self.install()
        self.blocked('fresh_wiki_model_proof_required', self.prepare, target, dimension=None)
        for field, value in (('expected_proposal_sha256', 'A' * 64), ('expected_proposal_sha256', 'a' * 63),
                             ('expected_proposal_sha256', None), ('install_id', 'A' * 32),
                             ('install_id', '4' * 31), ('install_id', 4),
                             ('accepted_provider_sha256', 'g' * 64), ('accepted_provider_sha256', b'a' * 64)):
            with self.subTest(field=field, value=value):
                self.blocked('invalid_origin_preparation_request', self.prepare, target, **{field: value})
        self.assertFalse(target.state.exists())

    def test_raw_tree_over_the_scan_bounds_refuses_before_any_build(self):
        plan = install.plan_fresh_wiki_origin(self.project)
        deep = self.project / 'deep'
        nested = deep
        for _ in range(34):
            nested = nested / 'd'
        nested.mkdir(parents=True)
        (nested / 'n.md').write_text('deep\n')
        self.blocked('raw_scan_limit_exceeded', workflow._require_raw_scan_within_bounds, plan)
        os.rename(deep, self.home / 'deep-away')
        big = self.project / 'big.md'
        with open(big, 'wb') as stream:  # sparse: one byte over the raw scan's byte bound
            stream.truncate(64_000_001)
        self.blocked('raw_scan_limit_exceeded', workflow._require_raw_scan_within_bounds, plan)
        big.unlink()
        # A raw tree the scan cannot read is not a bound: it refuses as unavailable.
        (self.project / 'link.md').symlink_to(self.project / 'notes.md')
        self.blocked('raw_scan_unavailable', workflow._require_raw_scan_within_bounds, plan)
        (self.project / 'link.md').unlink()
        workflow._require_raw_scan_within_bounds(plan)
        # Through prepare: refused before any journal, store or build record.
        target = self.install()
        with open(big, 'wb') as stream:
            stream.truncate(64_000_001)
        for _ in range(2):
            self.blocked('raw_scan_limit_exceeded', self.prepare, target)
            self.assert_nothing_registered(target)


class ForeignPointers(InstallCase):
    def test_only_pointers_of_one_install_missing_here_are_foreign(self):
        saved = self.write_foreign_pair()
        self.assertEqual(onboarding_restore.foreign_pointers(self.project),
                         {name: hashlib.sha256(raw).hexdigest() for name, raw in saved.items()})
        plan = install.plan_fresh_wiki_origin(self.project)
        self.assertEqual(plan['replacesForeignPointers'],
                         {name: hashlib.sha256(raw).hexdigest() for name, raw in saved.items()})
        # A marker alone of a missing install is foreign too.
        os.unlink(self.folder / 'route.json')
        self.assertEqual(list(onboarding_restore.foreign_pointers(self.project)), ['selection.json'])

    def test_pointers_of_a_live_install_another_major_or_two_installs_are_never_foreign(self):
        from memvane.kernel.memvane_paths import install_root
        live = install_root('6' * 32)
        cases = {
            'state root exists here': lambda: (live.mkdir(parents=True, mode=0o700),
                                               self.write_pointer('route.json', self.foreign_route('6' * 32)),
                                               self.write_pointer('selection.json', self.marker('6' * 32))),
            'another major': lambda: (self.write_pointer('route.json', self.foreign_route(stateFormat=2)),
                                      self.write_pointer('selection.json', self.marker())),
            'two installs': lambda: (self.write_pointer('route.json', self.foreign_route()),
                                     self.write_pointer('selection.json', self.marker('7' * 32))),
            'unreadable marker': lambda: (self.write_pointer('route.json', self.foreign_route()),
                                          self.write_pointer('selection.json', b'{"schema":')),
        }
        for label, write in cases.items():
            with self.subTest(label):
                for name in ('route.json', 'selection.json'):
                    if os.path.lexists(self.folder / name):
                        os.unlink(self.folder / name)
                write()
                saved = self.pointers()
                self.assertIsNone(onboarding_restore.foreign_pointers(self.project))
                self.blocked('project_selection_already_exists', install.plan_fresh_wiki_origin, self.project)
                self.assertEqual(self.pointers(), saved)
                if live.exists():
                    live.rmdir()

    def test_foreign_pointers_go_only_after_the_build_registered_then_the_wake(self):
        saved = self.write_foreign_pair()
        target = self.install()
        order = []
        register, retire, wake = (workflow._register_initial_build, onboarding_restore.retire_foreign_pointers,
                                  workflow._request_build_wake)

        def recorded(name, real):
            def step(*args, **kwargs):
                order.append((name, sorted(self.pointers())))
                return real(*args, **kwargs)
            return step
        with mock.patch.object(workflow, '_register_initial_build', recorded('register', register)), \
                mock.patch.object(onboarding_restore, 'retire_foreign_pointers', recorded('retire', retire)), \
                mock.patch.object(workflow, '_request_build_wake', recorded('wake', wake)):
            self.assertEqual(target.prepare()['status'], 'initial_build_registered')
        self.assertEqual(order, [('register', sorted(saved)), ('retire', sorted(saved)), ('wake', [])])
        self.assertEqual(self.pointers(), {})

    def test_a_refused_registration_keeps_the_reviewed_pointers(self):
        saved = self.write_foreign_pair()
        target = self.install()

        def refuse(**_kwargs):
            raise InstallBlocked('initial_build_already_registered')
        with mock.patch.object(workflow, '_register_initial_build', side_effect=refuse):
            for _ in range(2):
                self.blocked('initial_build_already_registered', target.prepare)
                self.assertEqual(self.pointers(), saved)
        self.assertEqual(target.prepare()['status'], 'initial_build_registered')
        self.assertEqual(self.pointers(), {})

    def test_pointers_changed_after_the_review_refuse_the_replacement(self):
        self.write_foreign_pair()
        plan = install.plan_fresh_wiki_origin(self.project)
        changed = self.write_pointer('selection.json', self.marker(route_raw=b'other'))
        self.blocked('reviewed_origin_plan_changed', onboarding_restore.retire_foreign_pointers, plan)
        self.assertEqual(self.pointers()['selection.json'], changed)
        self.assertIn('route.json', self.pointers())


if __name__ == '__main__':
    unittest.main()

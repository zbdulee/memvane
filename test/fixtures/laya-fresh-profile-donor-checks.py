#!/usr/bin/env python3
"""A fresh install joins the vector profile existing installs already hold.

The review derives a profile from absolute runtime paths. Existing installs
keep the profile they were first reviewed under when their runtime moves
(``laya_provider_reproof.reattest``), so the same runtime content reviews to
a different profile for a new install. A bundle seals one profile:
``prepare_fresh_wiki_origin`` must take the existing installs' profile when a
donor attestation is content-equivalent, refuse ambiguity, and otherwise
leave the review unchanged. The real review, reattest and validator run on a
synthetic runtime under a temporary HOME; nothing outside it is touched.
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
import unittest
from unittest import mock

FIXTURES = Path(__file__).resolve().parent
EMBED = runpy.run_path(str(FIXTURES / 'laya-embed-runtime-checks.py'))
CORE = FIXTURES.parents[1] / 'core'
sys.path.insert(0, str(CORE))
import laya_product_bundle as bundle  # noqa: E402
import laya_product_install as product  # noqa: E402
import memvane_paths  # noqa: E402
from laya_vector_primitives import profile_identity  # noqa: E402
from laya_local_embed_provider import recipe_fingerprint  # noqa: E402

CLOSURE_KEYS = ('approvedProviderId', 'embeddingFingerprint', 'dependencyClosureSha256')


def moved_donor(reviewed, base_from, base_to, journal):
    """An install first reviewed with the same runtime at ``base_to``.

    Its profile, provider id and closure digest derive from the moved rows;
    its live manifest and rows are this runtime's (the shape a moved install
    keeps after re-attestation).
    """
    manifest = reviewed['providerManifest']
    old_rows = sorted(({**row, 'path': row['path'].replace(str(base_from), str(base_to), 1)}
                       for row in reviewed['dependencyClosure']),
                      key=lambda row: row['path'])
    closure_sha = hashlib.sha256(bundle._canonical(old_rows)).hexdigest()
    fingerprint = recipe_fingerprint(
        model_id=manifest['modelId'], model_sha256=manifest['modelSha256'],
        runtime_id=manifest['runtimeId'], recipe_id=manifest['recipeId'],
        provider_sha256=manifest['providerArtifactSha256'],
        worker_sha256=manifest['workerSha256'], node_sha256=manifest['nodeSha256'],
        module_entry_sha256=manifest['moduleEntrySha256'], closure_sha256=closure_sha)
    approved = 'local-qmd-' + hashlib.sha256(
        (manifest['providerArtifactSha256'] + manifest['workerSha256'] +
         closure_sha).encode()).hexdigest()[:24]
    profile = {**reviewed['vectorProfile'], 'approvedProviderId': approved,
               'embeddingFingerprint': fingerprint,
               'profileId': profile_identity(manifest['modelId'], fingerprint,
                                             manifest['dimension'])}
    return {'schema': bundle.PRODUCTION_ATTESTATION_SCHEMA, 'sourceCommit': 'c' * 40,
            'bundleManifestSha256': 'd' * 64, 'ownershipManifestSha256': 'e' * 64,
            'journalPath': str(journal), 'vectorProfile': profile,
            'providerManifest': {**manifest, 'approvedProviderId': approved,
                                 'embeddingFingerprint': fingerprint,
                                 'dependencyClosureSha256': closure_sha},
            'dependencyClosure': reviewed['dependencyClosure'],
            bundle.PROFILE_CLOSURE_FIELD: old_rows}


def plant(install, attestation, mode=0o600):
    """Write ``attestation`` as the install's owner-private record."""
    install.mkdir(parents=True, mode=0o700, exist_ok=True)
    install.chmod(0o700)
    path = install / 'production-provider-attestation.json'
    path.write_bytes(json.dumps(attestation, sort_keys=True).encode())
    path.chmod(mode)
    return path


def common_base(rows):
    return Path(os.path.commonpath([os.path.dirname(row['path']) for row in rows]))


class Donor(EMBED['Base']):
    """``_profile_donor`` on the real review of a Laya-owned runtime."""

    def setUp(self):
        super().setUp()
        EMBED['R'].ensure_for_attestation(self.old_att)
        source, commit = self.source_repo()
        self.reviewed = self.review(source, commit)['reviewedProvider']
        self.installs = memvane_paths.state_base()
        self.installs.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.live_base = common_base(self.reviewed['dependencyClosure'])

    def donor(self, name, base_to='/old-runtime/g-1'):
        install = self.installs / (name * 32)
        record = moved_donor(self.reviewed, self.live_base, base_to,
                             install / 'install-journal.sqlite')
        return install, record

    def resolve(self, sealed=None):
        stack = ExitStack()
        self.addCleanup(stack.close)
        if sealed is not None:
            stack.enter_context(mock.patch.object(
                product, 'inspect_full_plugin_candidate',
                return_value={'vectorProfile': sealed}))
        return product._profile_donor(self.reviewed, plugin_root=self.plugin,
                                      state=self.state)

    def assert_joined(self, provider, record):
        self.assertEqual(provider['vectorProfile'], record['vectorProfile'])
        self.assertEqual(provider[bundle.PROFILE_CLOSURE_FIELD],
                         record[bundle.PROFILE_CLOSURE_FIELD])
        self.assertEqual(provider['dependencyClosure'], self.reviewed['dependencyClosure'])
        manifest = provider['providerManifest']
        for key in CLOSURE_KEYS:
            self.assertEqual(manifest[key], record['providerManifest'][key])
        self.assertEqual(manifest['serviceSocketPath'],
                         self.reviewed['providerManifest']['serviceSocketPath'])
        # The fresh install's own attestation validates under the joined profile.
        attestation = {**record, 'journalPath': str(self.state / 'install-journal.sqlite'),
                       'providerManifest': manifest,
                       'dependencyClosure': provider['dependencyClosure'],
                       bundle.PROFILE_CLOSURE_FIELD: provider[bundle.PROFILE_CLOSURE_FIELD]}
        self.validate(attestation)

    def test_unsealed_bundle_joins_the_existing_profile(self):
        install, record = self.donor('a')
        path = plant(install, record)
        before = path.read_bytes()
        self.assertNotEqual(record['vectorProfile'], self.reviewed['vectorProfile'])
        provider, donor = self.resolve()
        self.assertEqual(donor, str(install))
        self.assert_joined(provider, record)
        self.assertEqual(path.read_bytes(), before)

    def test_sealed_bundle_joins_only_its_own_profile(self):
        install, record = self.donor('a')
        plant(install, record)
        provider, donor = self.resolve(sealed=record['vectorProfile'])
        self.assertEqual(donor, str(install))
        self.assert_joined(provider, record)
        # Another sealed profile: no donor, the review is unchanged (adopt refuses).
        other = {**record['vectorProfile'], 'profileId': 'f' * 64}
        self.assertEqual(self.resolve(sealed=other), (self.reviewed, None))
        # Sealed with the review's own profile: nothing to join.
        self.assertEqual(self.resolve(sealed=self.reviewed['vectorProfile']),
                         (self.reviewed, None))

    def test_no_or_non_equivalent_donor_leaves_the_review_unchanged(self):
        self.assertEqual(self.resolve(), (self.reviewed, None))
        # The unpinned QMD-era attestation is not content-equivalent.
        plant(self.installs / ('b' * 32), self.old_att)
        install, record = self.donor('c')
        changed = {**record, 'providerManifest': {**record['providerManifest'],
                                                  'modelSha256': '0' * 64}}
        plant(install, changed)
        self.assertEqual(self.resolve(), (self.reviewed, None))

    def test_unsafe_or_unreadable_attestation_is_not_a_donor(self):
        install, record = self.donor('a')
        path = plant(install, record, mode=0o644)
        self.assertEqual(self.resolve(), (self.reviewed, None))
        path.unlink()
        # A symlinked record is not followed.
        target = plant(self.base / 'elsewhere', record)
        path.symlink_to(target)
        self.assertEqual(self.resolve(), (self.reviewed, None))
        path.unlink()
        plant(self.installs / 'not-an-install', record)
        for raw in (b'{', b'[' * 100000 + b']' * 100000, b'[]'):
            path.write_bytes(raw)
            path.chmod(0o600)
            self.assertEqual(self.resolve(), (self.reviewed, None))

    def test_interrupted_prepare_resumes_under_its_own_staged_profile(self):
        # The first run sealed the bundle with the donor profile and staged
        # this install's attestation; the donor is gone before the rerun.
        own, record = self.donor('a')
        plant(own, record)
        with mock.patch.object(product, '_state_bases', return_value=(own.parent,)):
            provider, donor = self.resolve(sealed=record['vectorProfile'])
        self.assertEqual(donor, str(own))
        self.assert_joined(provider, record)

    def test_review_profile_held_by_an_install_counts_toward_ambiguity(self):
        donor, record = self.donor('a')
        plant(donor, record)
        current = self.installs / ('b' * 32)
        plant(current, {**record, 'vectorProfile': self.reviewed['vectorProfile'],
                        'providerManifest': self.reviewed['providerManifest'],
                        'dependencyClosure': self.reviewed['dependencyClosure'],
                        'journalPath': str(current / 'install-journal.sqlite'),
                        bundle.PROFILE_CLOSURE_FIELD: None})
        current_path = current / 'production-provider-attestation.json'
        held = json.loads(current_path.read_bytes())
        del held[bundle.PROFILE_CLOSURE_FIELD]
        current_path.write_bytes(json.dumps(held).encode())
        with self.assertRaisesRegex(product.InstallBlocked, 'fresh_profile_donor_ambiguous'):
            self.resolve()
        # A sealed bundle decides: only its profile is a candidate.
        provider, joined = self.resolve(sealed=record['vectorProfile'])
        self.assertEqual(joined, str(donor))
        self.assert_joined(provider, record)
        # Only the review's own profile held: nothing to join.
        donor.joinpath('production-provider-attestation.json').unlink()
        self.assertEqual(self.resolve(), (self.reviewed, None))

    def test_two_equivalent_profiles_are_ambiguous(self):
        first, record = self.donor('a')
        plant(first, record)
        second, other = self.donor('b', base_to='/older-runtime/g-2')
        plant(second, other)
        self.assertNotEqual(record['vectorProfile'], other['vectorProfile'])
        with self.assertRaisesRegex(product.InstallBlocked, 'fresh_profile_donor_ambiguous'):
            self.resolve()
        # Same profile from two installs is one donor profile, not ambiguity.
        second.joinpath('production-provider-attestation.json').unlink()
        plant(second, {**record, 'journalPath': str(second / 'install-journal.sqlite')})
        provider, donor = self.resolve()
        self.assertEqual(donor, str(first))
        self.assert_joined(provider, record)


class FreshPrepareWithDonor(EMBED['OnboardingWithOnlyLayaRuntime']):
    """``prepare_fresh_wiki_origin`` adopts and attests under the donor profile."""

    def test_prepare_attests_the_joined_profile(self):
        plan = product.plan_fresh_wiki_origin(self.project)
        review = product.review_local_provider(
            source_root=str(self.source), plugin_root=str(self.plugin),
            state_root=str(self.state), commit_id=self.commit, dimension=2)
        reviewed = review['reviewedProvider']
        install = memvane_paths.state_base() / ('a' * 32)
        record = moved_donor(reviewed, common_base(reviewed['dependencyClosure']),
                             '/old-runtime/g-1', install / 'install-journal.sqlite')
        donor_path = plant(install, record)
        before = donor_path.read_bytes()
        self.proof['vectorProfile'] = record['vectorProfile']
        adopted = []
        real_proof = dict(self.proof)

        def adopt(*_a, vector_profile, **_kw):
            adopted.append(vector_profile)
            return dict(real_proof)

        with mock.patch.object(product, 'adopt_host_loaded_plugin', adopt), \
                mock.patch.dict(sys.modules, {'qmd_runtime': None}):
            prepared = product.prepare_fresh_wiki_origin(
                project_root=self.project, source_root=str(self.source),
                plugin_root=str(self.plugin), state_root=str(self.state),
                commit_id=self.commit, expected_proposal_sha256=plan['proposalSha256'],
                install_id=EMBED['E2E']['BUILD']['INSTALL'],
                accepted_provider_sha256=review['proposalSha256'], dimension=2)
        self.assertEqual(prepared['status'], 'initial_build_registered', prepared)
        self.assertEqual(prepared['profileDonor'], str(install))
        self.assertEqual(adopted, [record['vectorProfile']] * 2)
        sealed = json.loads((self.state / 'production-provider-attestation.json').read_bytes())
        self.assertEqual(sealed['vectorProfile'], record['vectorProfile'])
        self.assertEqual(sealed[bundle.PROFILE_CLOSURE_FIELD],
                         record[bundle.PROFILE_CLOSURE_FIELD])
        self.assertEqual(sealed['dependencyClosure'], reviewed['dependencyClosure'])
        for key in CLOSURE_KEYS:
            self.assertEqual(sealed['providerManifest'][key], record['providerManifest'][key])
        route = json.loads((self.state / 'reviewed-route.json').read_bytes())
        self.assertEqual(route['vectorProfile'], record['vectorProfile'])
        self.assertEqual(donor_path.read_bytes(), before)

    # The parent's full build/select/recall run is covered where it is defined.
    test_minimal_settings_project_is_built_selected_and_recalled = None
    test_qmd_origin_marker_is_refused_with_a_clear_reason = None


if __name__ == '__main__':
    unittest.main()

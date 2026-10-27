#!/usr/bin/env python3
"""QMD-origin detach and settings-identity conversion on real-shaped installs.

The selected install of ``minimal-settings-rebind-checks.py`` (real install
journal, real fresh-wiki or v3 QMD-origin ledger and marker, real index and
claim-queue databases, a temp HOME) with the minimal settings every live
install uses, plus a bound ``initial-build.json`` and, for the QMD origin, a
QMD-shaped build identity whose collections differ from the ledger (the
rccar shape). Only bundle identity reads and the provider lane are patched.
"""
from __future__ import annotations

from contextlib import ExitStack, nullcontext
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
CORE = HERE.parents[1] / 'core'
sys.path.insert(0, str(CORE))
REBIND = runpy.run_path(str(HERE / 'minimal-settings-rebind-checks.py'))

import laya_active_upgrade as active  # noqa: E402
import laya_fresh_wiki_origin as fresh  # noqa: E402
import laya_host_response as response  # noqa: E402
import laya_inactive_upgrade as inactive  # noqa: E402
import laya_index_host_route as host_route  # noqa: E402
import laya_initial_build as build  # noqa: E402
import laya_install_journal as journal_mod  # noqa: E402
import laya_settings_rebind as rebind  # noqa: E402

MINIMAL = {'name': 'project', 'skipPaths': ['build']}
canon, sha, vector_profile = REBIND['canon'], REBIND['sha'], REBIND['vector_profile']


def identity_digest(project, collections):
    """The settings identity a converted route binds (spelled out here)."""
    rows = sorted(({'name': item['name'], 'role': item['role'],
                    'sourceRoot': item['sourceRoot']} for item in collections),
                  key=lambda item: item['name'])
    return sha(canon({'schema': 'laya-settings-identity-v1',
                      'projectRoot': str(project), 'collections': rows}))


def snapshot(*roots):
    """File bytes under ``roots``; empty lock files a writer opens are left out."""
    files = {}
    for root in roots:
        for path in sorted(Path(root).rglob('*')):
            if path.is_file() and not path.is_symlink() and path.suffix != '.lock':
                files[str(path)] = path.read_bytes()
    return files


class Install(REBIND['SelectedProject']):
    """A live-shaped selected install bound to minimal settings bytes."""

    def setUp(self):
        with mock.patch.dict(REBIND['SelectedProject'].setUp.__globals__,
                             {'legacy_settings': lambda *_a, **_k: dict(MINIMAL)}):
            super().setUp()
        self.assertEqual(json.loads(self.settings.read_text()), MINIMAL)
        stack = ExitStack()
        self.addCleanup(stack.close)
        import laya_origin_detach as detach
        self.detach = detach
        stack.enter_context(mock.patch.object(detach, '_provider_lane', nullcontext))
        self.write_build_record()

    def write_build_record(self):
        identity = {
            'projectRoot': str(self.project), 'stateRoot': str(self.state),
            'installId': '3' * 32, 'scopeId': '6' * 32,
            'collections': self.collections(),
            'candidateDb': str(self.db), 'queueDb': str(self.queue),
            'catalogDb': str(self.state / 'catalog.sqlite'),
            'bindingStore': str(self.state / 'binding.sqlite'),
            'dbIncarnation': self.incarnation,
            'journalPath': str(self.journal),
            'routeFile': str(self.state / 'reviewed-route.json'),
            'routeSha256': sha(self.route_raw),
            'attestationSha256': self.ledger['attestationSha256'],
            'vectorProfileId': vector_profile()['profileId'],
            'proposalSha256': '1' * 64, 'providerProposalSha256': '2' * 64}
        if self.ORIGIN == 'qmd':
            # The build bound the raw docs folder; a rebind moved only the ledger.
            identity.update(sourceDb=str(self.state / 'source.sqlite'),
                            sourceFormat='qmd-2.5.3',
                            collections=[{'name': 'project-docs', 'role': 'raw',
                                          'sourceRoot': str(self.project / 'docs')},
                                         self.collections()[1]])
        else:
            identity['originMode'] = 'fresh_wiki_only'
        sealed = {key: None for key in build.SEALED_FIELDS}
        record = {'schema': build.SCHEMA, 'epoch': 4, 'phase': 'bound',
                  'identity': identity, 'progress': None, 'sealed': sealed,
                  'freshness': None, 'blocked': None, 'updatedNs': 1}
        build._validate(record)
        build._write_json(build.record_path(self.state), record, replace=False)
        self.build_raw = build.record_path(self.state).read_bytes()

    def finished_upgrade_intent(self):
        """A settled bundle-upgrade receipt that names the current route as new."""
        route = self.route_raw.decode()
        value = {'schema': 'laya-active-upgrade-intent-v1',
                 'projectRoot': str(self.project), 'stateRoot': str(self.state),
                 'oldRecord': {'identity': {'projectRoot': str(self.project),
                                            'stateRoot': str(self.state)}},
                 'oldLedger': {**self.ledger, 'routeSha256': 'f' * 64},
                 'newLedger': {**self.ledger, 'phase': 'upgrading'},
                 'oldRoute': route, 'newRoute': route, 'oldAttestation': '{}',
                 'newAttestation': '{}', 'oldMarker': '{}', 'newMarker': '{}',
                 'backup': str(self.state / 'backup'),
                 **({'origin': 'qmd'} if self.ORIGIN == 'qmd' else {})}
        self.private_write(self.state / active.INTENT, canon(value))

    # -- helpers ---------------------------------------------------------------

    def plan(self):
        return self.detach.plan(str(self.project))

    def apply(self, **kwargs):
        planned = self.plan()
        self.assertEqual(planned['status'], 'review_required', planned)
        return self.detach.apply(str(self.project), planned['proposalSha256'], **kwargs)

    def crash_after(self, step, *, conversion_step=None):
        """Fail right after one step, as a process death (no rollback runs)."""
        steps = self.detach._steps
        rebind_steps = rebind._file_steps

        def interrupted(view, digest):
            built = steps(view, digest)
            if conversion_step is not None:
                return built
            original = built[step]

            def then_crash():
                original()
                raise RuntimeError('synthetic_crash')
            return {**built, step: then_crash}

        def interrupted_rebind(intent, target):
            built = rebind_steps(intent, target)
            original = built[conversion_step]

            def then_crash():
                original()
                raise RuntimeError('synthetic_crash')
            return {**built, conversion_step: then_crash}
        with mock.patch.object(self.detach, '_steps', interrupted), \
                mock.patch.object(self.detach, '_rollback', lambda _view: None), \
                (mock.patch.object(rebind, '_file_steps', interrupted_rebind)
                 if conversion_step is not None else nullcontext()):
            failed = self.apply()
        self.assertEqual(failed['status'], 'refused', failed)
        self.assertEqual(failed['reason'], 'origin_detach_step_failed', failed)

    def assert_recall_served(self):
        decision = self.decision()
        self.assertIn(decision['state'], ('candidate', 'rebinding'), decision)
        _out, calls = self.serve('recall', decision,
                                 {'prompt': 'alpha', 'cwd': str(self.project)})
        self.assertEqual(len(calls), 1, calls)
        self.assertTrue(calls[0]['readOnly'])
        return decision

    def assert_converged(self):
        detach = self.detach
        self.assertFalse((self.state / 'qmd-cutover.json').exists())
        row = fresh._load(self.state / fresh.LEDGER)
        self.assertEqual(row['phase'], 'active')
        marker = json.loads((self.auto / fresh.MARKER).read_bytes())
        self.assertEqual(marker['schema'], fresh.MARKER_SCHEMA)
        route_raw = (self.auto / host_route.RECORD).read_bytes()
        route = json.loads(route_raw)
        self.assertEqual(route['settingsSha256'],
                         identity_digest(self.project, self.collections()))
        self.assertEqual((self.state / 'reviewed-route.json').read_bytes(), route_raw)
        self.assertEqual(row['routeSha256'], sha(route_raw))
        self.assertEqual(marker['routeSha256'], sha(route_raw))
        self.assertEqual(journal_mod.inspect_journal(self.journal)['selectedRouteSha256'],
                         sha(route_raw))
        # Only settingsSha256 moved: same stores, scope, bundle and profile.
        self.assertEqual({k: v for k, v in route.items() if k != 'settingsSha256'},
                         {k: v for k, v in self.route.items() if k != 'settingsSha256'})
        identity = build.read(self.state)['identity']
        self.assertEqual(identity['originMode'], 'fresh_wiki_only')
        self.assertNotIn('sourceDb', identity)
        self.assertNotIn('sourceFormat', identity)
        self.assertEqual(identity['collections'], row['collections'])
        self.assertEqual(active._origin(self.state).origin, 'fresh')
        self.assertFalse(rebind.intent_path(self.state).exists())
        self.assertEqual(self.plan()['status'], 'complete')
        decision, _proof = self.assert_selected_and_readable()
        self.assertEqual(decision['collections'], self.collections())
        self.assert_recall_served()
        return detach


class QmdOriginDetach(Install):
    ORIGIN = 'qmd'

    def test_plan_reads_only_and_names_every_step(self):
        self.finished_upgrade_intent()
        before = snapshot(self.state, self.auto)
        planned = self.plan()
        self.assertEqual(snapshot(self.state, self.auto), before)
        self.assertEqual(planned['status'], 'review_required', planned)
        self.assertEqual((planned['origin'], planned['bindingForm']), ('qmd', 'bytes'))
        self.assertEqual(planned['steps'], list(self.detach.STEPS))
        self.assertEqual(planned['forfeits'], ['active_bundle_restore', 'qmd_origin_restore'])
        self.assertEqual(planned['buildIdentity'],
                         {'removed': ['sourceDb', 'sourceFormat'], 'added': ['originMode'],
                          'collectionsChanged': True})
        self.assertEqual(self.plan()['proposalSha256'], planned['proposalSha256'])

    def test_detach_converges_and_keeps_qmd_files(self):
        self.finished_upgrade_intent()
        refused = self.detach.apply(str(self.project), 'f' * 64)
        self.assertEqual(refused['reason'], 'proposal_acceptance_required', refused)
        self.assertTrue((self.state / 'qmd-cutover.json').exists())
        done = self.apply()
        self.assertEqual(done['status'], 'converged', done)
        self.assertEqual(done['stepsApplied'], list(self.detach.STEPS))
        self.assert_converged()
        detached = self.state / ('qmd-cutover.detached-%d.json' % self.ledger['epoch'])
        self.assertEqual(json.loads(detached.read_bytes())['schema'], 'laya-qmd-product-cutover-v3')
        for name in ('source.sqlite', 'backup.sqlite'):
            self.assertTrue((self.state / name).is_file())
        self.assertEqual((self.state / self.detach.BUILD_BACKUP).read_bytes(), self.build_raw)
        self.assertFalse((self.state / active.INTENT).exists())
        self.assertEqual(len(list(self.state.glob('active-upgrade-*-settled.json'))), 1)
        # The fresh ledger is the v3 row minus its QMD fields, one epoch later
        # for the detach and one more for the settings-binding rebind.
        row = fresh._load(self.state / fresh.LEDGER)
        self.assertEqual(row['epoch'], self.ledger['epoch'] + 2)
        same = set(fresh.FIELDS) - {'schema', 'epoch', 'routeSha256'}
        self.assertEqual({key: row[key] for key in same},
                         {key: self.ledger[key] for key in same})
        # The setup entry reports the finished install as complete.
        printed = __import__('subprocess').run(
            [sys.executable, '-B', str(CORE / 'setup_entry.py'), 'plan-detach-qmd-origin',
             '--project', str(self.project)], capture_output=True, text=True, timeout=120,
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(json.loads(printed.stdout)['status'], 'complete', printed.stderr)

    def test_every_crash_point_keeps_recall_and_resumes(self):
        points = [(step, None) for step in self.detach.STEPS if step != 'convert']
        points += [('convert', step) for step in rebind.FORWARD]
        for step, conversion_step in points:
            with self.subTest(step=step, conversion=conversion_step):
                self.setUp()
                self.finished_upgrade_intent()
                self.crash_after(step, conversion_step=conversion_step)
                decision = self.assert_recall_served()
                if conversion_step is None:
                    # Detach steps never leave a mixed binding set.
                    self.assertEqual(decision['state'], 'candidate')
                resumed = self.plan()
                self.assertEqual(resumed['status'], 'review_required', resumed)
                done = self.detach.apply(str(self.project), resumed['proposalSha256'])
                self.assertEqual(done['status'], 'converged', done)
                self.assert_converged()

    def test_failure_before_the_commit_point_rolls_back(self):
        steps = self.detach._steps

        def failing(view, digest):
            built = steps(view, digest)

            def boom():
                raise RuntimeError('synthetic_failure')
            return {**built, 'marker': boom}
        before = snapshot(self.auto)
        with mock.patch.object(self.detach, '_steps', failing):
            failed = self.apply()
        self.assertEqual(failed['reason'], 'origin_detach_step_failed', failed)
        self.assertEqual(snapshot(self.auto), before)
        self.assertFalse((self.state / fresh.LEDGER).exists())
        self.assertEqual(build.read(self.state)['identity'],
                         json.loads(self.build_raw)['identity'])
        self.assertFalse((self.state / self.detach.BUILD_BACKUP).exists())
        self.assertEqual(self.decision()['state'], 'candidate')
        self.assertEqual(self.apply()['status'], 'converged')
        self.assert_converged()

    def test_rollback_only_before_the_commit_point(self):
        self.crash_after('ledger')
        planned = self.plan()
        rolled = self.detach.apply(str(self.project), planned['proposalSha256'],
                                   rollback=True)
        self.assertEqual(rolled['status'], 'rolled_back', rolled)
        self.assertFalse((self.state / fresh.LEDGER).exists())
        self.assertEqual(build.read(self.state)['identity'],
                         json.loads(self.build_raw)['identity'])
        self.assertEqual(json.loads((self.auto / fresh.MARKER).read_bytes())['schema'],
                         'laya-qmd-selection-marker-v1')
        self.assertEqual(self.decision()['state'], 'candidate')
        self.crash_after('marker')
        planned = self.plan()
        refused = self.detach.apply(str(self.project), planned['proposalSha256'],
                                    rollback=True)
        self.assertEqual(refused['reason'], 'origin_detach_forward_only', refused)
        self.assertEqual(self.detach.apply(str(self.project),
                                           planned['proposalSha256'])['status'], 'converged')

    def test_unsettled_bundle_upgrade_refuses_before_any_write(self):
        self.private_write(self.state / active.INTENT, b'{"schema":"unknown"}')
        before = snapshot(self.state, self.auto)
        planned = self.plan()
        self.assertEqual(planned['reason'], 'active_upgrade_unsettled', planned)
        refused = self.detach.apply(str(self.project), 'f' * 64)
        self.assertEqual(refused['reason'], 'active_upgrade_unsettled', refused)
        self.assertEqual(snapshot(self.state, self.auto), before)
        self.assertEqual(self.decision()['state'], 'candidate')

    def test_without_the_settle_step_the_next_bundle_upgrade_is_refused(self):
        """Regression proof for the settle step: skip it and the leftover QMD
        intent no longer matches the fresh ledger the upgrade selects."""
        self.finished_upgrade_intent()
        with mock.patch.object(self.detach, '_step_settle', lambda _view: None), \
                mock.patch.object(self.detach, '_step_convert', lambda *_a: None):
            self.apply()
        self.assertEqual(active._origin(self.state).origin, 'fresh')
        with mock.patch.object(active, '_locked_provider', nullcontext):
            with self.assertRaisesRegex(Exception, 'active_upgrade_intent_invalid'):
                self.upgrade_bundle()

    def upgrade_bundle(self):
        """One automatic selected bundle upgrade to a new plugin root."""
        new_root = self.home / 'new-laya'
        new_root.mkdir(exist_ok=True)
        new = {**self.proof, 'pluginRoot': str(new_root), 'sourceCommit': '9' * 40,
               'bundleManifestSha256': '8' * 64, 'ownershipManifestSha256': '7' * 64}
        rows = [[str(new_root / 'p.py'), 1, 2, 3, 4, 5]]
        review = {'proposalSha256': 'e' * 64,
                  'reviewedProvider': {'vectorProfile': vector_profile(),
                                       'providerManifest': {'providerPath':
                                                            str(new_root / 'p.py')},
                                       'dependencyClosure': []}}
        attestation = self.state / journal_mod.PRODUCTION_ATTESTATION

        def bind_attestation(_path, _pending, _proof):
            return sha(attestation.read_bytes()), rows
        with ExitStack() as stack:
            enter = stack.enter_context
            enter(mock.patch.object(active, '_locked_provider', nullcontext))
            enter(mock.patch.object(active, 'review_local_provider', return_value=review))
            enter(mock.patch.object(active, 'adopt_host_loaded_plugin', return_value=new))
            enter(mock.patch.object(active, 'inspect_full_plugin_candidate',
                                    return_value=new))
            enter(mock.patch.object(active, 'validate_production_provider_attestation',
                                    lambda *_a: None))
            enter(mock.patch.object(active, '_provider_file_identity', lambda _att: rows))
            enter(mock.patch.object(inactive, '_store_counts',
                                    return_value={'vectorChunkSets': 1}))
            enter(mock.patch.object(inactive, '_backup',
                                    return_value=self.state / 'backup'))
            enter(mock.patch.object(inactive, '_verify_backup', lambda _intent: None))
            enter(mock.patch('laya_embed_resident.wait_for_release', return_value=True))
            enter(mock.patch.object(journal_mod, '_inspect_identity', return_value=new))
            enter(mock.patch.object(journal_mod, 'inspect_full_plugin_candidate',
                                    return_value=new))
            enter(mock.patch.object(journal_mod, '_bind_attestation', bind_attestation))
            return active.upgrade(project_root=str(self.project),
                                  state_root=str(self.state),
                                  source_root=str(self.home / 'marketplace'),
                                  plugin_root=str(new_root), commit_id='9' * 40,
                                  accepted_provider_sha256='e' * 64,
                                  model_path=None, dimension=3)

    def test_bundle_upgrade_after_detach_takes_the_fresh_path(self):
        self.finished_upgrade_intent()
        self.assertEqual(self.apply()['status'], 'converged')
        result = self.upgrade_bundle()
        self.assertEqual(result['status'], 'active_upgrade_complete', result)
        intent = json.loads((self.state / active.INTENT).read_bytes())
        self.assertNotIn('origin', intent)
        route = json.loads((self.auto / host_route.RECORD).read_bytes())
        self.assertEqual(route['pluginRoot'], str(self.home / 'new-laya'))
        self.assertEqual(route['settingsSha256'],
                         identity_digest(self.project, self.collections()))
        self.assertEqual(fresh._load(self.state / fresh.LEDGER)['routeSha256'],
                         sha((self.auto / host_route.RECORD).read_bytes()))
        self.assertEqual(self.decision()['state'], 'candidate')
        self.assertFalse((self.state / 'qmd-cutover.json').exists())
        self.assertEqual(self.plan()['status'], 'complete')

    def test_session_start_requests_the_worker_once_then_converges(self):
        decision = self.decision()
        requests = []
        real = self.detach.request

        def request(project, journal):
            return real(project, journal, spawn=requests.append)
        with mock.patch.object(self.detach, 'request', request):
            self.serve('update', decision, {'hook_event_name': 'SessionStart',
                                            'cwd': str(self.project)})
        self.assertEqual(requests, [str(self.project)])
        result = self.detach.auto_converge(self.project)
        self.assertEqual(result['status'], 'converged', result)
        self.assert_converged()
        status = self.detach.read_status(self.state)
        self.assertEqual((status['status'], status['attempts']), ('converged', 0))
        log = (self.state / 'bundle-auto-upgrade.log').read_text()
        self.assertIn('origin-detach apply steps=build,ledger,marker,verify,retire,convert', log)
        self.assertIn('origin-detach converged', log)
        requests.clear()
        with mock.patch.object(self.detach, 'request', request):
            self.serve('update', self.decision(), {'hook_event_name': 'SessionStart',
                                                   'cwd': str(self.project)})
        self.assertEqual(requests, [])
        self.assertEqual(self.detach.auto_converge(self.project), {'status': 'complete'})

    def test_automatic_failure_leaves_the_install_untouched_and_searchable(self):
        self.private_write(self.state / active.INTENT, b'{"schema":"unknown"}')
        before = snapshot(self.state, self.auto)
        result = self.detach.auto_converge(self.project)
        self.assertEqual(result['reason'], 'active_upgrade_unsettled', result)
        status = self.detach.read_status(self.state)
        self.assertEqual(status['status'], 'refused')
        after = snapshot(self.state, self.auto)
        changed = {Path(path).name for path in set(before) | set(after)
                   if before.get(path) != after.get(path)}
        self.assertEqual(changed, {self.detach.STATUS, 'bundle-auto-upgrade.log'})
        self.assert_selected_and_readable()
        # A refused run is retried only after the refused backoff.
        self.assertEqual(self.detach.request(str(self.project), str(self.journal),
                                             spawn=self.fail)['status'], 'refused')


class FreshOriginConversion(Install):
    ORIGIN = 'fresh'

    def test_conversion_only_and_resumes_from_every_rebind_step(self):
        planned = self.plan()
        self.assertEqual((planned['origin'], planned['steps'], planned['forfeits']),
                         ('fresh', ['convert'], []))
        for step in rebind.FORWARD:
            with self.subTest(step=step):
                self.setUp()
                self.crash_after('convert', conversion_step=step)
                self.assert_recall_served()
                resumed = self.plan()
                self.assertEqual(resumed['bindingForm'], 'converting', resumed)
                done = self.detach.apply(str(self.project), resumed['proposalSha256'])
                self.assertEqual(done['status'], 'converged', done)
                self.assert_converged()
                self.assertEqual(build.read(self.state)['identity'],
                                 json.loads(self.build_raw)['identity'])

    def test_interrupted_conversion_session_start_resumes_in_background(self):
        self.crash_after('convert', conversion_step='route')
        decision = self.decision()
        self.assertEqual(decision['state'], 'rebinding', decision)
        requests = []
        real = self.detach.request

        def request(project, journal):
            return real(project, journal, spawn=requests.append)
        with mock.patch.object(self.detach, 'request', request):
            notice, _ = self.serve('update', decision, {'hook_event_name': 'SessionStart',
                                                        'cwd': str(self.project)})
        self.assertIn('conversion', notice['hookSpecificOutput']['additionalContext'])
        self.assertEqual(requests, [str(self.project)])
        self.assertEqual(self.detach.auto_converge(self.project)['status'], 'converged')
        self.assert_converged()

    def test_leftover_bundle_upgrade_receipt_is_settled(self):
        self.finished_upgrade_intent()
        planned = self.plan()
        self.assertEqual(planned['steps'], ['settle', 'convert'])
        self.assertEqual(planned['forfeits'], ['active_bundle_restore'])
        self.assertEqual(self.apply()['status'], 'converged')
        self.assertEqual(len(list(self.state.glob('active-upgrade-*-settled.json'))), 1)
        self.assert_converged()


class SettingsIdentityBinding(Install):
    """Owner decision: skipPaths edits keep the route, a name edit blocks."""
    ORIGIN = 'qmd'

    def converted(self):
        self.assertEqual(self.apply()['status'], 'converged')
        return (self.auto / host_route.RECORD).read_bytes()

    def test_skip_paths_edit_after_conversion_needs_no_rebind(self):
        route = self.converted()
        self.settings.write_text(json.dumps({'name': 'project',
                                             'skipPaths': ['build', 'dist']}) + '\n')
        decision = self.assert_recall_served()
        self.assertEqual(decision['state'], 'candidate')
        self.assertEqual(decision['recallSettings']['skipPaths'], ['build', 'dist'])
        self.assertEqual((self.auto / host_route.RECORD).read_bytes(), route)
        self.assertFalse(rebind.intent_path(self.state).exists())
        journal_mod.verify_selected_route_read_only(decision)
        self.assertEqual(self.plan()['status'], 'complete')

    def test_name_edit_after_conversion_blocks(self):
        self.converted()
        self.settings.write_text(json.dumps({'name': 'other', 'skipPaths': ['build']}))
        decision = self.decision()
        self.assertEqual((decision['state'], decision['reason']),
                         ('blocked', 'route_settings_changed'))

    def test_byte_bound_route_still_needs_the_exact_bytes(self):
        self.settings.write_text(json.dumps({'name': 'project',
                                             'skipPaths': ['build', 'dist']}))
        self.assertEqual(self.decision()['reason'], 'route_settings_changed')


class IdentityRouteSelection(REBIND['SelectedProject']):
    """A route that binds the settings identity (as onboarding now writes it);
    no detach module is involved, so this runs against any code version."""
    ORIGIN = 'fresh'

    def setUp(self):
        with mock.patch.dict(REBIND['SelectedProject'].setUp.__globals__,
                             {'legacy_settings': lambda *_a, **_k: dict(MINIMAL)}):
            super().setUp()
        self.bind_identity()

    def bind_identity(self):
        route = {**self.route, 'settingsSha256': identity_digest(self.project,
                                                                 self.collections())}
        raw = canon(route)
        self.private_write(self.auto / host_route.RECORD, raw)
        self.private_write(self.state / 'reviewed-route.json', raw)
        current = journal_mod.inspect_journal(self.journal)
        journal_mod.cas_rebind_route(self.journal, current['epoch'], self.route_raw, raw)
        row = {**self.ledger, 'epoch': self.ledger['epoch'] + 1, 'routeSha256': sha(raw)}
        fresh._write(self.state / fresh.LEDGER, row, replace=True)
        fresh._write(self.auto / fresh.MARKER, fresh._marker(row), replace=True)

    def test_skip_paths_edit_keeps_an_identity_bound_route_selected(self):
        self.assertEqual(self.decision()['state'], 'candidate', self.decision())
        self.settings.write_text(json.dumps({'name': 'project', 'skipPaths': []}))
        decision = self.decision()
        self.assertEqual(decision['state'], 'candidate', decision)
        self.assertEqual(decision['recallSettings']['skipPaths'], [])

    def test_name_edit_blocks_an_identity_bound_route(self):
        self.settings.write_text(json.dumps({'name': 'renamed', 'skipPaths': ['build']}))
        self.assertEqual(self.decision()['state'], 'blocked')



class LegacySettingsOnIdentityRoute(REBIND['SelectedProject']):
    """A legacy settings file under an identity-bound route: the minimal
    rewrite keeps the identity, so only the settings file is rewritten."""
    ORIGIN = 'fresh'

    def setUp(self):
        super().setUp()
        IdentityRouteSelection.bind_identity(self)

    def test_minimal_rewrite_needs_no_rebind(self):
        import settings_migration
        route = (self.auto / host_route.RECORD).read_bytes()
        proposal = settings_migration.plan(str(self.project))
        self.assertEqual(proposal['status'], 'review_required', proposal)
        self.assertNotIn('rebind', proposal)
        done = settings_migration.apply(str(self.project), proposal['proposalSha256'])
        self.assertEqual(done['status'], 'migrated', done)
        self.assertEqual(json.loads(self.settings.read_text()), MINIMAL)
        self.assertEqual((self.auto / host_route.RECORD).read_bytes(), route)
        self.assertEqual(self.decision()['state'], 'candidate')


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
"""T1 journal reader contracts over real private v3/v4 selected installs.

SelectedProject owns real route/journal/index/queue/attestation metadata. Its
bundle inspection, executing-copy and provider-file identity authorities are
synthetic, as in the existing v4 fixture; no production provider is exercised.
Missing new APIs are reported as feature RED after the legacy proof succeeds,
not as a reproduced existing-product defect. No caller-created capability is
accepted as installer or host-execution proof.
"""
from contextlib import closing, contextmanager
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import MappingProxyType
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / 'core'))
from laya_selected_project import SelectedProject
import laya_install_journal as J
import laya_product_bundle as bundle

spec = importlib.util.spec_from_file_location('reader_v4_fixture', HERE / 'laya-journal-v4-checks.py')
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)
spec = importlib.util.spec_from_file_location('reader_move_fixture', HERE / 'laya-project-dir-move-checks.py')
published = importlib.util.module_from_spec(spec)
spec.loader.exec_module(published)


def emit(case, **evidence):
    print(json.dumps(dict(case=case, **evidence), sort_keys=True), flush=True)


class JournalReaderChecks(unittest.TestCase):
    def compose(self, cls=SelectedProject):
        case = cls('runTest')
        self.addCleanup(case.doCleanups)
        case.setUp()
        decision, read_proof = case.assert_selected_and_readable()
        self.assertTrue(read_proof['readOnlyAllowed'])
        inspected = J.inspect_journal(case.journal)
        self.assertEqual(inspected['state'], 'active')
        self.assertIsNone(inspected['pending'])
        with self.full_authority(case):
            full = J.verify_selected_route(decision)
        self.assertTrue(full['productRouteAllowed'])
        emit('legacy-entry', version=J.journal_version(case.journal),
             state=inspected['state'], bundleEpoch=inspected['epoch'],
             fullProof=full['status'], readOnlyAllowed=read_proof['readOnlyAllowed'])
        return case

    @contextmanager
    def full_authority(self, case, *modules):
        # Reuse the precise existing V3Selected.full_proof authority boundary.
        # The public proof verifier itself, route/attestation/journal and real
        # SQLite headers remain actual. This is not a valid runtime closure.
        identity_rows = [[str(case.plugin / 'p.py'), 1, 2, 3, 4, 5]]
        def inspect_bundle(root):
            if Path(root) != case.plugin:
                raise bundle.BundleEvidenceError('fixture_bundle_not_owned')
            return case.proof

        from contextlib import ExitStack
        with ExitStack() as stack, mock.patch.object(J, '_inspect_identity', return_value=case.proof), \
                mock.patch.object(J, '_verified_executing_root', return_value=str(case.plugin)), \
                mock.patch.object(J, '_provider_file_identity', return_value=identity_rows):
            # New modules may retain a direct import of this same bundle
            # inspection authority. Patch only existing bindings, never a
            # validator/format/header/capability issuer.
            for module in (J, bundle, *modules):
                if callable(getattr(module, 'inspect_full_plugin_candidate', None)):
                    stack.enter_context(mock.patch.object(module, 'inspect_full_plugin_candidate',
                                                          side_effect=inspect_bundle))
            yield

    def feature(self, name, *functions):
        if importlib.util.find_spec(name) is None:
            emit('feature-missing', module=name, required=list(functions),
                 classification='new-T1-feature-not-implemented; legacy-entry-already-proved')
            self.fail('T1 feature RED: ' + name + ' is absent after actual legacy selected proof')
        module = importlib.import_module(name)
        for function in functions:
            self.assertTrue(callable(getattr(module, function, None)), name + '.' + function + ' is required')
        if name == 'laya_bridge_readiness' and not getattr(self, '_observer_guarded', False):
            self.assertTrue(callable(getattr(module, '_observe_registered_hook', None)))
            guard = mock.patch.object(module, '_observe_registered_hook',
                                      side_effect=AssertionError('actual host observer is forbidden in this fixture'))
            guard.start()
            self.addCleanup(guard.stop)
            self._observer_guarded = True
        return module

    @staticmethod
    def data_snapshot(case):
        paths = [case.state / name for name in
                 ('index.sqlite', 'claims.sqlite', 'catalog.sqlite', 'binding.sqlite')]
        paths += [getattr(case, name) for name in ('catalog', 'store') if hasattr(case, name)]
        return {str(path.relative_to(case.state)): path.read_bytes() for path in paths}

    @staticmethod
    def metadata_snapshot(case):
        return {str(path.relative_to(case.home)): path.read_bytes() for path in
                (case.auto / J.ROUTE_FILE, case.state / 'reviewed-route.json',
                 case.state / J.PRODUCTION_ATTESTATION, case.state / case.ledger_name)}

    def test_new_selected_journal_defaults_to_v4_and_reading_changes_no_data(self):
        case = self.compose()
        before, row = self.data_snapshot(case), J.inspect_journal(case.journal)
        decision, proof = case.assert_selected_and_readable()
        self.assertTrue(proof['readOnlyAllowed'])
        with self.full_authority(case):
            self.assertTrue(J.verify_selected_route(decision)['productRouteAllowed'])
        version, sql, columns = legacy.stored(case.journal)
        emit('default-v4', version=version, columns=columns, newWriteSeen=row['newWriteSeen'])
        self.assertEqual((version, sql), (4, J._CREATE))
        self.assertEqual(J.inspect_journal(case.journal), row)
        self.assertEqual(self.data_snapshot(case), before)

    def test_legacy_v3_cas_preserves_exact_format_and_existing_reader(self):
        case = self.compose(legacy.V3Selected)
        before, row = self.data_snapshot(case), J.inspect_journal(case.journal)
        updated = J.cas_mark_new_write(case.journal, row['epoch'])
        version, sql, columns = legacy.stored(case.journal)
        emit('v3-cas-control', version=version, beforeEpoch=row['epoch'],
             afterEpoch=updated['epoch'], newWriteSeen=updated['newWriteSeen'])
        self.assertEqual((version, sql), (3, legacy.V3_CREATE))
        self.assertIn('user_qmd_inventory', columns)
        self.assertEqual(updated['epoch'], row['epoch'] + 1)
        self.assertTrue(updated['newWriteSeen'])
        case.assert_selected_and_readable()
        self.assertEqual(self.data_snapshot(case), before)

    def test_legacy_v4_cas_preserves_exact_format_and_existing_reader(self):
        case = self.compose()
        before, row = self.data_snapshot(case), J.inspect_journal(case.journal)
        updated = J.cas_mark_new_write(case.journal, row['epoch'])
        emit('v4-cas-control', version=J.journal_version(case.journal),
             beforeEpoch=row['epoch'], afterEpoch=updated['epoch'], newWriteSeen=updated['newWriteSeen'])
        self.assertEqual(legacy.stored(case.journal)[:2], (4, J._CREATE))
        self.assertEqual(updated['epoch'], row['epoch'] + 1)
        self.assertTrue(updated['newWriteSeen'])
        case.assert_selected_and_readable()
        self.assertEqual(self.data_snapshot(case), before)

    def test_current_reader_seal_requires_a_real_published_store_not_only_installer_proof(self):
        case = self.compose()
        # This legacy fixture deliberately has no published generation and
        # has noncanonical empty catalog/binding placeholders. A full installer
        # proof alone cannot turn those into a validated reader endpoint.
        with closing(sqlite3.connect(case.db)) as db:
            head = db.execute('SELECT gen FROM head WHERE scope=?', (case.route['scopeId'],)).fetchone()
        self.assertIsNone(head, 'this negative control must retain its actual unpublished DB')
        reader = self.feature('laya_journal_reader', 'seal_current_reader', 'convert_reader_v5',
                              'read_selection', 'cas_reader', 'seal_reader_endpoint')
        before = dict(journal=Path(case.journal).read_bytes(), data=self.data_snapshot(case),
                      metadata=self.metadata_snapshot(case), row=J.inspect_journal(case.journal))
        with self.full_authority(case), self.assertRaises(ValueError) as refused:
            reader.seal_current_reader(case.state, case.project, executing_root=case.plugin)
        emit('unpublished-reader-refused', installerProofAlreadyVerified=True,
             actualPublishedHead=head, reason=str(refused.exception), version=J.journal_version(case.journal))
        self.assertEqual(J.journal_version(case.journal), 4)
        self.assertEqual(Path(case.journal).read_bytes(), before['journal'])
        self.assertEqual(J.inspect_journal(case.journal), before['row'])
        self.assertEqual(self.data_snapshot(case), before['data'])
        self.assertEqual(self.metadata_snapshot(case), before['metadata'])

    def test_converter_rejects_caller_created_installer_and_readiness_without_writes(self):
        case = self.compose()
        reader = self.feature('laya_journal_reader', 'convert_reader_v5')
        before, data = Path(case.journal).read_bytes(), self.data_snapshot(case)
        declared = dict(projectRoot=str(case.project), installId=case.route['installId'],
                        providerProof=case.proof, endpoint='v6', ready=True)
        with self.assertRaises(ValueError):
            reader.convert_reader_v5(case.state, case.project, installer=declared, readiness=declared)
        emit('unissued-capabilities-refused', version=J.journal_version(case.journal))
        self.assertEqual(Path(case.journal).read_bytes(), before)
        self.assertEqual(self.data_snapshot(case), data)
        self.assertEqual(J.journal_version(case.journal), 4)

    def test_readiness_does_not_accept_caller_stdout_or_manifest_as_actual_host_receipt(self):
        case = self.compose()
        bridge = self.feature('laya_bridge_readiness', 'bridge_preflight', 'check_bridge_preflight')
        before = Path(case.journal).read_bytes()
        # A caller declaring every plausible field is still not a reference to
        # trusted QA observer execution. T8 supplies actual host provenance.
        payload = dict(nonce='1' * 32, host='claude', sessionId='fixture-session',
                       executingRoot=str(case.plugin), releaseCommit='c' * 40,
                       manifestSha256='d' * 64, ready=True)
        for declared in (payload, dict(payload, stdout=json.dumps(payload)),
                         dict(payload, hook='registered', observer=True)):
            with self.subTest(fields=sorted(declared)):
                with self.assertRaises(ValueError):
                    bridge.check_bridge_preflight([declared], release_commit='c' * 40,
                                                  expected_hosts=frozenset(('claude',)))
        emit('caller-host-claims-refused', actualHostExecuted=False)
        self.assertEqual(Path(case.journal).read_bytes(), before)

    def published_case(self):
        case = self.compose(published.MoveCase)
        head, members = case.head()
        self.assertTrue(head)
        self.assertEqual(len(members), len(published.CARDS))
        recalled_head, rows = case.recall(case.decision())
        self.assertEqual(recalled_head, head)
        self.assertTrue(any('otter' in row['body'] for row in rows))
        with closing(published.LayaIndexA(str(case.db))) as index:
            vector_seal = index.db.execute('SELECT rows_count FROM gen_vector_seals WHERE gen=?',
                                           (head,)).fetchone()
            persisted_pins = index.db.execute('SELECT COUNT(*) FROM pins').fetchone()[0]
            index.db.commit()
        self.assertIsNotNone(vector_seal)
        self.assertGreater(vector_seal[0], 0)
        self.assertEqual(persisted_pins, 0)
        roots = {case.WIKI: str(case.auto / 'wiki')}
        with closing(published.GenerationBindingStore(str(case.store), scope=published.SCOPE,
                     db_incarnation=case.incarnation, roots=roots)) as bindings:
            bound = bindings.get(published.SCOPE, head, case.incarnation)
        self.assertEqual(published.binding_digest(bound), case.ledger['bindingDigest'])
        emit('published-entry', head=head, members=len(members), vectorRows=vector_seal[0],
             pins=persisted_pins, recallRows=len(rows), actualCapacityBinding=True)
        return case

    def reader_api(self):
        return self.feature('laya_journal_reader', 'seal_current_reader', 'seal_reader_endpoint',
                            'convert_reader_v5', 'read_selection', 'cas_reader')

    def bridge_api(self):
        return self.feature('laya_bridge_readiness', 'begin_bridge_preflight',
                            'collect_bridge_preflight', 'check_bridge_preflight', 'bridge_preflight')

    @staticmethod
    def selection(selection):
        return tuple(getattr(selection, field) for field in
                     ('epoch', 'preferred_endpoint', 'previous_endpoint', 'transition_id'))

    def observer(self, case, challenge, host):
        # Synthetic OS/registered-host authority only. collect must compare
        # these observed challenge values with its independent private registry.
        # This process is the fixture process, not an actual Claude/Codex host.
        return dict(session_id='synthetic-' + host + '-session', process_pid=os.getpid(),
                    process_start=1234.5, executable=str(Path(sys.executable).resolve()),
                    plugin_root=str(case.plugin), release_commit='c' * 40,
                    manifest_sha256=case.proof['bundleManifestSha256'], hook_stage='registered-v4',
                    observed_entrypoint='hooks/run-hook:recall:' + host,
                    observed_challenge_id=challenge.challenge_id,
                    observed_nonce_sha256=challenge.nonce_sha256)

    def ready(self, case, bridge, *, hosts=frozenset(('claude', 'codex'))):
        with self.full_authority(case, bridge):
            challenge = bridge.begin_bridge_preflight(case.state, release_commit='c' * 40,
                                                       expected_hosts=hosts)
            self.assertEqual(challenge.release_commit, 'c' * 40)
            self.assertEqual(challenge.expected_hosts, hosts)
            self.assertGreater(challenge.expires_at, challenge.issued_at)
            self.assertLessEqual(challenge.expires_at - challenge.issued_at, 600)
            with mock.patch.object(bridge, '_observe_registered_hook',
                                   side_effect=lambda issued, host: self.observer(case, issued, host)) as observer:
                receipts = [bridge.collect_bridge_preflight(challenge, host=host) for host in sorted(hosts)]
            self.assertEqual(observer.call_count, len(hosts))
            for path in receipts:
                self.assertIsInstance(path, Path)
                self.assertTrue(path.is_absolute())
                self.assertEqual(path.resolve(strict=True), path)
                info = path.lstat()
                self.assertEqual(info.st_mode & 0o777, 0o600)
                self.assertEqual(info.st_uid, os.getuid())
                self.assertEqual(info.st_nlink, 1)
                record = json.loads(path.read_text(encoding='utf8'))
                self.assertEqual(record['schema'], 'memvane-bridge-preflight-v1')
                self.assertEqual(record['challenge_id'], challenge.challenge_id)
                self.assertEqual(record['nonce_sha256'], challenge.nonce_sha256)
                self.assertEqual(record['hook_stage'], 'registered-v4')
            readiness = bridge.check_bridge_preflight(receipts, release_commit='c' * 40,
                                                       expected_hosts=hosts)
        self.assertEqual(type(readiness).__name__, 'BridgeReadiness')
        self.assertNotIsInstance(readiness, dict)
        emit('synthetic-observer-readiness', hosts=sorted(hosts), receipts=len(receipts),
             actualHostExecuted=False, privateReceiptValidatorActual=True)
        return challenge, receipts, readiness

    def installer(self, case, reader, bridge=None):
        with self.full_authority(case, reader, *((bridge,) if bridge is not None else ())):
            installer = reader.seal_current_reader(case.state, case.project, executing_root=case.plugin)
        self.assertEqual(type(installer).__name__, 'InstallerSeal')
        endpoint = installer.endpoint
        self.assertEqual(type(endpoint).__name__, 'VerifiedEndpoint')
        self.assertIsInstance(endpoint.endpoint_id, str)
        self.assertTrue(endpoint.endpoint_id)
        self.assertIsInstance(endpoint.record, MappingProxyType)
        return installer

    def converted(self):
        case = self.published_case()
        reader, bridge = self.reader_api(), self.bridge_api()
        installer = self.installer(case, reader, bridge)
        challenge, receipts, readiness = self.ready(case, bridge)
        before = dict(data=self.data_snapshot(case), metadata=self.metadata_snapshot(case),
                      bundle=J.inspect_journal(case.journal), head=case.head())
        with self.full_authority(case, reader, bridge):
            selected = reader.convert_reader_v5(case.state, case.project,
                                                installer=installer, readiness=readiness)
        self.assertEqual(type(selected).__name__, 'ReaderSelection')
        self.assertEqual(J.journal_version(case.journal), 5)
        self.assertEqual(self.selection(reader.read_selection(case.journal)), self.selection(selected))
        return case, reader, bridge, installer, readiness, selected, before

    def alternate(self, case, reader, installer, name):
        destination = case.state / name
        self.assertFalse(destination.exists())
        with closing(sqlite3.connect(Path(case.db).as_uri() + '?mode=ro', uri=True)) as source, \
                closing(sqlite3.connect(destination)) as target:
            source.backup(target)
        destination.chmod(0o600)
        # SQLite backup includes committed WAL data and retains the actual
        # incarnation/head/vector seal. Catalog/binding are the same immutable
        # verified tuple; no descriptor dict or fabricated generation is used.
        with closing(published.LayaIndexA(str(destination))) as copied:
            self.assertEqual(copied.incarnation(), case.incarnation)
            copied_head = copied.db.execute('SELECT gen FROM head WHERE scope=?',
                                            (case.route['scopeId'],)).fetchone()[0]
            self.assertEqual(copied_head, case.head()[0])
            copied.db.commit()
        with self.full_authority(case, reader):
            endpoint = reader.seal_reader_endpoint(installer, db_path=destination,
                        catalog_path=case.catalog, binding_path=case.store)
        self.assertNotEqual(endpoint.endpoint_id, installer.endpoint.endpoint_id)
        self.assertIsInstance(endpoint.record, MappingProxyType)
        return endpoint

    def test_real_published_seed_proves_head_vectors_binding_and_actual_recall(self):
        case = self.published_case()
        self.assertEqual(J.journal_version(case.journal), 4)
        self.assertFalse(J.inspect_journal(case.journal)['newWriteSeen'])

    def test_published_installer_issues_immutable_endpoint_without_store_or_journal_writes(self):
        case = self.published_case()
        reader = self.reader_api()
        before = dict(data=self.data_snapshot(case), metadata=self.metadata_snapshot(case),
                      journal=Path(case.journal).read_bytes(), row=J.inspect_journal(case.journal))
        installer = self.installer(case, reader)
        with self.assertRaises((AttributeError, TypeError)):
            installer.endpoint = None
        with self.assertRaises(TypeError):
            installer.endpoint.record['forged'] = True
        with self.assertRaises((AttributeError, TypeError)):
            installer.endpoint.endpoint_id = 'forged'
        emit('published-installer-issued', endpoint=installer.endpoint.endpoint_id,
             formatVersion=J.journal_version(case.journal))
        self.assertEqual(Path(case.journal).read_bytes(), before['journal'])
        self.assertEqual(J.inspect_journal(case.journal), before['row'])
        self.assertEqual(self.data_snapshot(case), before['data'])
        self.assertEqual(self.metadata_snapshot(case), before['metadata'])

    def test_explicit_conversion_seals_initial_reader_and_original_attestation_only(self):
        case, reader, _, installer, _, selected, before = self.converted()
        emit('converted-reader', selection=self.selection(selected), bundleEpoch=before['bundle']['epoch'])
        self.assertEqual(selected.epoch, 1)
        self.assertEqual(selected.preferred_endpoint, installer.endpoint.endpoint_id)
        self.assertIsNone(selected.previous_endpoint)
        self.assertEqual(J.inspect_journal(case.journal), before['bundle'])
        self.assertEqual(self.data_snapshot(case), before['data'])
        self.assertEqual(self.metadata_snapshot(case), before['metadata'])
        self.assertEqual(case.head(), before['head'])
        raw_attestation = (case.state / J.PRODUCTION_ATTESTATION).read_bytes()
        with closing(sqlite3.connect(case.journal)) as db:
            endpoints = db.execute('SELECT * FROM read_endpoints').fetchall()
            values = [value for row in endpoints for value in row]
        self.assertTrue(endpoints)
        self.assertIn(installer.endpoint.endpoint_id, values)
        # Canonical attestation may be a dedicated immutable relation. Require
        # its exact bytes somewhere in the v5 image, not a reconstituted dict.
        with closing(sqlite3.connect(case.journal)) as db:
            table_names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            stored_values = [value for name in table_names for row in db.execute(
                'SELECT * FROM "' + name.replace('"', '""') + '"') for value in row]
        self.assertTrue(any(value == raw_attestation or value == raw_attestation.decode('utf8')
                            for value in stored_values), 'v5 must retain the exact original canonical attestation')
        self.assertEqual(self.selection(reader.read_selection(case.journal)), self.selection(selected))

    def test_reader_cas_is_independent_and_only_current_exact_replay_survives_aba(self):
        case, reader, _, installer, _, initial, before = self.converted()
        other = self.alternate(case, reader, installer, 'reader-copy-b.sqlite')
        third = self.alternate(case, reader, installer, 'reader-copy-c.sqlite')
        # Tamper actual staged SQLite after issuance. Rejecting a caller dict
        # alone would not prove that CAS revalidates an issued target's store.
        with closing(sqlite3.connect(case.state / 'reader-copy-c.sqlite')) as db, db:
            db.execute('PRAGMA user_version=999')
        unchanged = Path(case.journal).read_bytes()
        with self.full_authority(case, reader), self.assertRaises(ValueError):
            reader.cas_reader(case.journal, expected_epoch=initial.epoch,
                              expected_endpoint=initial.preferred_endpoint,
                              target=third, transition_id='9' * 32)
        self.assertEqual(Path(case.journal).read_bytes(), unchanged)
        self.assertEqual(self.selection(reader.read_selection(case.journal)), self.selection(initial))
        args = dict(expected_epoch=initial.epoch, expected_endpoint=initial.preferred_endpoint,
                    target=other, transition_id='1' * 32)
        with self.full_authority(case, reader):
            changed = reader.cas_reader(case.journal, **args)
            replay = reader.cas_reader(case.journal, **args)
            self.assertEqual(self.selection(replay), self.selection(changed))
            self.assertEqual(changed.epoch, initial.epoch + 1)
            self.assertEqual(changed.preferred_endpoint, other.endpoint_id)
            self.assertEqual(changed.previous_endpoint, installer.endpoint.endpoint_id)
            for altered in (dict(args, transition_id='2' * 32), dict(args, target=installer.endpoint),
                            dict(args, expected_endpoint=third.endpoint_id),
                            dict(args, target=dict(other.record))):
                with self.subTest(altered=sorted(k for k in altered if altered[k] != args[k])):
                    unchanged = Path(case.journal).read_bytes()
                    with self.assertRaises(ValueError):
                        reader.cas_reader(case.journal, **altered)
                    self.assertEqual(self.selection(reader.read_selection(case.journal)), self.selection(changed))
                    self.assertEqual(Path(case.journal).read_bytes(), unchanged)
            back = reader.cas_reader(case.journal, expected_epoch=changed.epoch,
                      expected_endpoint=changed.preferred_endpoint, target=installer.endpoint,
                      transition_id='3' * 32)
            self.assertEqual(back.epoch, initial.epoch + 2)
            self.assertEqual(back.preferred_endpoint, initial.preferred_endpoint)
            with self.assertRaises(ValueError):
                reader.cas_reader(case.journal, **args)
        emit('reader-cas-aba', initial=self.selection(initial), changed=self.selection(changed),
             returned=self.selection(back), bundleEpoch=J.inspect_journal(case.journal)['epoch'])
        self.assertEqual(J.inspect_journal(case.journal), before['bundle'])
        # New copies are deliberately excluded; existing component bytes stay unchanged.
        self.assertEqual(self.data_snapshot(case), before['data'])
        self.assertEqual(self.metadata_snapshot(case), before['metadata'])

    def test_bundle_cas_and_new_write_marker_do_not_advance_reader_selection(self):
        case, reader, _, _, _, selected, before = self.converted()
        row = J.inspect_journal(case.journal)
        marked = J.cas_mark_new_write(case.journal, row['epoch'])
        self.assertEqual(marked['epoch'], row['epoch'] + 1)
        self.assertTrue(marked['newWriteSeen'])
        self.assertEqual(self.selection(reader.read_selection(case.journal)), self.selection(selected))
        root = case.home / 'next-plugin'
        root.mkdir(mode=0o700)
        proof = dict(case.proof, pluginRoot=str(root), sourceCommit='f' * 40,
                     bundleManifestSha256='1' * 64, ownershipManifestSha256='2' * 64)
        route = dict(case.route, pluginRoot=str(root), generation='g-' + '5' * 32,
                     bundleManifestSha256=proof['bundleManifestSha256'],
                     ownershipManifestSha256=proof['ownershipManifestSha256'])
        with mock.patch.object(J, 'inspect_full_plugin_candidate', return_value=proof):
            staged = J.cas_stage_update(case.journal, marked['epoch'], proof,
                                       J._canon(route).encode('utf8'))
        emit('bundle-cas-independent', state=staged['state'], bundleEpoch=staged['epoch'],
             readerEpoch=reader.read_selection(case.journal).epoch)
        self.assertEqual(staged['state'], 'pending')
        self.assertEqual(staged['epoch'], marked['epoch'] + 1)
        self.assertEqual(self.selection(reader.read_selection(case.journal)), self.selection(selected))
        self.assertEqual(self.data_snapshot(case), before['data'])

    def test_endpoint_rows_are_immutable_and_trigger_or_table_schema_tamper_is_refused(self):
        case, reader, _, _, _, selected, _ = self.converted()
        with closing(sqlite3.connect(case.journal)) as db:
            columns = [row[1] for row in db.execute('PRAGMA table_info(read_endpoints)')]
            self.assertTrue(columns)
            column = '"' + columns[0].replace('"', '""') + '"'
            for sql in ('UPDATE read_endpoints SET ' + column + '=' + column,
                        'DELETE FROM read_endpoints',
                        'INSERT INTO read_endpoints SELECT * FROM read_endpoints'):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.Error):
                    db.execute(sql)
                db.rollback()
        self.assertEqual(self.selection(reader.read_selection(case.journal)), self.selection(selected))
        original = Path(case.journal).read_bytes()
        for mutation in ('trigger', 'table', 'unknown-version'):
            with self.subTest(mutation=mutation):
                with closing(sqlite3.connect(case.journal)) as db, db:
                    if mutation == 'trigger':
                        trigger = db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='read_endpoints' ORDER BY name LIMIT 1").fetchone()
                        self.assertIsNotNone(trigger)
                        db.execute('DROP TRIGGER "' + trigger[0].replace('"', '""') + '"')
                    elif mutation == 'table':
                        db.execute('ALTER TABLE read_endpoints ADD COLUMN forged TEXT')
                    else:
                        db.execute('PRAGMA user_version=999')
                try:
                    with self.assertRaises(ValueError):
                        reader.read_selection(case.journal)
                finally:
                    Path(case.journal).write_bytes(original)
                self.assertEqual(self.selection(reader.read_selection(case.journal)), self.selection(selected))
        emit('v5-schema-tamper-refused', mutations=['trigger', 'table', 'unknown-version'])

    def test_v5_converter_never_downgrades_and_foreign_issued_endpoint_cannot_cross_install(self):
        case, reader, bridge, _, _, selected, _ = self.converted()
        original = Path(case.journal).read_bytes()
        try:
            outcome = J.convert_to_v4(case.journal)
        except ValueError as exc:
            outcome = dict(refused=str(exc))
        self.assertEqual(J.journal_version(case.journal), 5)
        self.assertEqual(Path(case.journal).read_bytes(), original)
        foreign = self.published_case()
        issued = self.installer(foreign, reader, bridge)
        with self.full_authority(case, reader), self.assertRaises(ValueError):
            reader.cas_reader(case.journal, expected_epoch=selected.epoch,
                              expected_endpoint=selected.preferred_endpoint,
                              target=issued.endpoint, transition_id='4' * 32)
        emit('foreign-endpoint-refused', mainProject=str(case.project), foreignProject=str(foreign.project),
             mainInstall=case.route['installId'], foreignInstall=foreign.route['installId'],
             note='synthetic IDs collide; project/state proof still must distinguish installs', downgrade=outcome)
        self.assertEqual(self.selection(reader.read_selection(case.journal)), self.selection(selected))
        self.assertEqual(Path(case.journal).read_bytes(), original)

    def test_readiness_registry_audit_freshness_and_observed_nonce_are_revalidated(self):
        case = self.published_case()
        bridge = self.bridge_api()
        challenge, receipts, _ = self.ready(case, bridge)
        hosts = frozenset(('claude', 'codex'))
        with self.full_authority(case, bridge):
            with mock.patch('time.time', return_value=challenge.expires_at + 1), self.assertRaises(ValueError):
                bridge.check_bridge_preflight(receipts, release_commit='c' * 40, expected_hosts=hosts)
            for kwargs in (dict(release_commit='f' * 40, expected_hosts=hosts),
                           dict(release_commit='c' * 40, expected_hosts=frozenset(('claude', 'hermes')))):
                with self.subTest(kwargs=str(kwargs)), self.assertRaises(ValueError):
                    bridge.check_bridge_preflight(receipts, **kwargs)
            receipt = receipts[0]
            original = receipt.read_bytes()
            for key, value in (('host', 'hermes'), ('plugin_root', str(case.home)),
                               ('release_commit', 'f' * 40), ('nonce_sha256', '0' * 64)):
                with self.subTest(tampered=key):
                    record = json.loads(original.decode('utf8'))
                    record[key] = value
                    receipt.write_text(json.dumps(record), encoding='utf8')
                    try:
                        with self.assertRaises(ValueError):
                            bridge.check_bridge_preflight(receipts, release_commit='c' * 40,
                                                          expected_hosts=hosts)
                    finally:
                        receipt.write_bytes(original)
            forged = case.state / 'caller-receipt.json'
            forged.write_bytes(original)
            forged.chmod(0o600)
            with self.assertRaises(ValueError):
                bridge.check_bridge_preflight([forged, receipts[1]], release_commit='c' * 40,
                                              expected_hosts=hosts)
            alias = case.state / 'caller-receipt-link.json'
            alias.symlink_to(receipt)
            with self.assertRaises(ValueError):
                bridge.check_bridge_preflight([alias, receipts[1]], release_commit='c' * 40,
                                              expected_hosts=hosts)
            hardlink = case.state / 'caller-receipt-hardlink.json'
            os.link(receipt, hardlink)
            try:
                with self.assertRaises(ValueError):
                    bridge.check_bridge_preflight(receipts, release_commit='c' * 40, expected_hosts=hosts)
            finally:
                hardlink.unlink()
            private_mode = receipt.stat().st_mode & 0o777
            receipt.chmod(0o644)
            try:
                with self.assertRaises(ValueError):
                    bridge.check_bridge_preflight(receipts, release_commit='c' * 40, expected_hosts=hosts)
            finally:
                receipt.chmod(private_mode)
            wrong = bridge.begin_bridge_preflight(case.state, release_commit='c' * 40,
                                                  expected_hosts=frozenset(('claude',)))

            def bad_nonce(issued, host):
                return dict(self.observer(case, issued, host), observed_nonce_sha256='0' * 64)

            with mock.patch.object(bridge, '_observe_registered_hook', side_effect=bad_nonce), \
                    self.assertRaises(ValueError):
                bridge.collect_bridge_preflight(wrong, host='claude')
            # The untouched, still-fresh original issuer receipts remain valid.
            ready = bridge.check_bridge_preflight(receipts, release_commit='c' * 40, expected_hosts=hosts)
        self.assertEqual(type(ready).__name__, 'BridgeReadiness')
        emit('readiness-negative-controls', actualHostExecuted=False,
             refused=['expired', 'expected-host', 'commit', 'record-host', 'record-root',
                      'record-commit', 'record-nonce', 'forged-ref', 'symlink', 'nlink', 'mode', 'observed-nonce'])


if __name__ == '__main__':
    unittest.main(verbosity=2)

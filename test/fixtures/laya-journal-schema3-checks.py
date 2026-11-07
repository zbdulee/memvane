#!/usr/bin/env python3
"""U10 persisted schema3 reader endpoint and journal CAS over a real v5 install.

The v5 install, v6 index, schema3 view store, journal images and Git checkout
are actual. Synthetic authority: bundle inspection/host observation (as in
the existing reader fixtures) and the U5 parity receipt resolver, which this
unit only defines as an interface. No actual host, provider or operational
setup entry point is exercised.
"""
from contextlib import ExitStack, closing, contextmanager
import importlib
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest import mock
import uuid

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'core'))
spec = importlib.util.spec_from_file_location('schema3_seed', HERE / 'laya-reader-move-seal-checks.py')
seed_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed_module)
J = seed_module.base.J
CTX = multiprocessing.get_context('fork')
EXIT_AT_CHECKPOINT = 91
# Merge of PR #147, the last PR5 bridge bundle. CI fetches this exact commit
# (.github/workflows/test.yml) because a shallow checkout does not contain it.
PR5_BUNDLE_COMMIT = 'd01c859d5de14ec976fe832d4315728c33c73126'
HOSTS = frozenset(('claude', 'codex'))


def thaw(value):
    if isinstance(value, (dict, MappingProxyType)):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(v) for v in value]
    return value


def emit(case, **evidence):
    print(json.dumps(dict(case=case, **evidence), sort_keys=True, default=str), flush=True)


class Schema3EndpointChecks(unittest.TestCase):
    # ---- setup -----------------------------------------------------------
    def setUp(self):
        self.seed = seed_module.MoveSealChecks('runTest')
        self.seed.setUp()
        self.addCleanup(self.seed.doCleanups)
        self.case, self.reader, self.lease = self.seed.case, self.seed.reader, self.seed.lease
        self.bridge = self.seed.bridge
        self.dispatch = importlib.import_module('laya_reader_dispatch')
        self.migration = importlib.import_module('laya_shadow_migration')
        self.views = importlib.import_module('laya_view_store')
        self.opens = importlib.import_module('laya_reader_open')
        checkout = importlib.import_module('laya_checkout_view')
        case = self.case
        git = ['git', '-C', str(case.project), '-c', 'commit.gpgsign=false', '-c',
               'core.hooksPath=' + os.devnull, '-c', 'user.name=Fixture', '-c',
               'user.email=fixture@example.invalid']
        subprocess.run(['git', '-c', 'init.defaultBranch=main', 'init', '-q', str(case.project)],
                       check=True, capture_output=True, timeout=10)
        subprocess.run(git + ['commit', '--allow-empty', '-qm', 'fixture'],
                       check=True, capture_output=True, timeout=10)
        time.sleep(1.1)
        self.repository = checkout.register_repository(str(case.project))
        worktree = checkout.register_worktree(str(case.project), self.repository)
        self.worktree_id = worktree['worktreeId']
        self.store_path = case.state / 'schema3-candidate.sqlite'
        writer = self.views.ViewStore.create(self.store_path, project_root=str(case.project),
            repository=self.repository, worktree=worktree, scope_id=case.route['scopeId'])
        writer.close()
        self.gens = []

    @contextmanager
    def writer(self):
        store = self.views.ViewStore.open(self.store_path, repository=self.repository,
                                          scope_id=self.case.route['scopeId'])
        try:
            yield store
        finally:
            store.close()

    def publish(self):
        """Publish one legacy-imported generation from the selected v6 snapshot."""
        case = self.case
        scan = {'id': uuid.uuid4().hex, 'observed_ns': time.time_ns()}
        with self.seed.authority(), self.writer() as view:
            decision = J.select_route(str(case.project), action='recall')
            with self.lease.resolve_endpoint(case.project, action='recall', host_root=case.plugin) as endpoint:
                self.assertEqual(endpoint.record['store_format'], 'v6')
                with self.opens.inspect_warm_snapshot(endpoint, deadline=time.monotonic() + 10) as snapshot:
                    self.migration.import_wiki_batch(view, worktree_id=self.worktree_id, snapshot=snapshot,
                        endpoint=endpoint, decision=decision, after=None, provenance_scan=scan,
                        deadline=time.monotonic() + 10)
                    handle = view.capture_view(self.worktree_id)
                    proof = {'snapshot': snapshot, 'endpoint': endpoint, 'decision': decision,
                             'provenance_scan': scan}
                    evidence = self.migration.verify_generation_members(view, worktree_id=self.worktree_id,
                        **proof, deadline=time.monotonic() + 30)
                    manifest = {'scan_id': scan['id'], 'policy_digest': handle.policy_digest,
                                'revocation_epoch': 0, 'complete': True, 'sources': [],
                                'members': evidence['members']}
                    head = view.head(handle)
                    gen = view.prepare_generation(handle, manifest, expected_head=head, legacy_snapshot=proof)
                    view.publish_generation(handle, gen, expected_head=head, legacy_snapshot=proof)
        self.gens.append(gen)
        return gen

    def parity(self, generation=None, **changes):
        value = dict(migration_id='a' * 32, generation=generation or self.gens[0], digest='d' * 64)
        value.update(changes)
        return value

    @contextmanager
    def receipt(self, parity=None, **overrides):
        """Synthetic U5 receipt bound to this candidate (interface only in U10)."""
        def resolve(state, *, candidate, candidate_incarnation):
            self.assertEqual(Path(state), self.case.state)
            value = dict(parity or self.parity(), candidate=str(candidate),
                         candidate_incarnation=candidate_incarnation)
            value.update(overrides)
            return value
        with mock.patch.object(self.migration, 'shadow_parity_receipt', create=True, side_effect=resolve):
            yield

    def seal(self, parity=None):
        with self.seed.authority():
            return self.reader.seal_schema3_endpoint(self.seed.installer, store_path=self.store_path,
                                                     parity=parity or self.parity())

    @contextmanager
    def bridge_authority(self):
        case = self.case
        companion = case.home / 'schema3-companion'
        companion.mkdir(exist_ok=True)
        def inspect(root):
            return {**case.proof, 'pluginRoot': str(root)}
        with ExitStack() as stack:
            stack.enter_context(self.seed.authority())
            stack.enter_context(mock.patch.object(self.bridge, 'inspect_full_plugin_candidate', side_effect=inspect))
            yield companion

    def loaded(self, selection):
        """Loaded-v5 reader receipts from both hosts at ``selection`` (synthetic observer)."""
        case = self.case
        with self.bridge_authority() as companion:
            def observed(challenge, host):
                value = self.seed.helper.observer(case, challenge, host)
                root = case.plugin if host == 'claude' else companion
                info = root.stat()
                value.update(hook_stage='loaded-v5-reader', endpoint_id=selection.preferred_endpoint,
                             reader_epoch=selection.epoch, store_format='v6', generation='g' * 8,
                             sentinel_sha256='5' * 64, owner_role='owner' if host == 'claude' else 'companion',
                             plugin_root=str(root), plugin_node=[info.st_dev, info.st_ino],
                             process_pid=os.getpid() + (1 if host == 'codex' else 0))
                return value
            with mock.patch.object(self.bridge, '_observe_registered_hook', side_effect=observed):
                challenge = self.bridge.begin_loaded_bridge(case.state, release_commit='c' * 40,
                    expected_hosts=HOSTS, sentinel_path='concepts/otter-ledger.md', sentinel_sha256='5' * 64)
                refs = [self.bridge.collect_bridge_preflight(challenge, host=host) for host in sorted(HOSTS)]
        return (refs, 'c' * 40)

    def selection(self):
        value = self.reader.read_selection(self.case.journal)
        return (value.epoch, value.preferred_endpoint, value.previous_endpoint, value.transition_id)

    def cas(self, target, loaded, *, transition='1' * 32, base=None):
        base = base or self.reader.read_selection(self.case.journal)
        with self.bridge_authority():
            return self.reader.cas_reader(self.case.journal, expected_epoch=base.epoch,
                expected_endpoint=base.preferred_endpoint, target=target, transition_id=transition,
                loaded_bridge=loaded)

    def refused(self, reason, fn, *args, **kwargs):
        before = Path(self.case.journal).read_bytes()
        with self.assertRaises(ValueError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(str(getattr(caught.exception, 'reason', None) or caught.exception), reason)
        self.assertEqual(Path(self.case.journal).read_bytes(), before)
        return caught.exception

    def switched(self):
        """v6 -> schema3 CAS with parity, floor and loaded bridge; returns (initial, target, selected)."""
        self.publish()
        initial = self.reader.read_selection(self.case.journal)
        target = self.seal()
        loaded = self.loaded(initial)
        with self.receipt():
            selected = self.cas(target, loaded, base=initial)
        return initial, target, selected

    @contextmanager
    def resolved(self):
        with self.seed.authority(), J.read_only_journals():
            with self.lease.resolve_endpoint(self.case.project, action='recall',
                                             host_root=self.case.plugin) as endpoint:
                yield endpoint

    def query(self, endpoint, prompt='heron'):
        # U11: rows leave in the recall row shape, bound to the reader's own decision.
        decision = (self.reader._recheck(endpoint)[2]['readerDecision']
                    if endpoint in self.reader._read_only else None)
        return self.dispatch.query_endpoint(endpoint, checkout=self.case.project, prompt=prompt,
                                            query_vector=None, deadline=time.monotonic() + 20,
                                            decision=decision)

    def tamper(self, trigger, sql, args=()):
        db = sqlite3.connect(self.store_path)
        try:
            ddl = db.execute('SELECT sql FROM sqlite_master WHERE name=?', (trigger,)).fetchone()[0]
            db.execute('DROP TRIGGER ' + trigger)
            db.execute(sql, args)
            db.execute(ddl)
            db.commit()
        finally:
            db.close()

    # ---- 1.8 scenario 1, 6 ------------------------------------------------
    def test_01_parity_target_switches_and_persisted_schema3_endpoint_queries_store(self):
        initial, target, selected = self.switched()
        self.assertEqual(target.record['store_format'], 'schema3')
        self.assertEqual((selected.epoch, selected.preferred_endpoint, selected.previous_endpoint),
                         (initial.epoch + 1, target.endpoint_id, initial.preferred_endpoint))
        with self.receipt():
            replay = self.cas(target, None, base=initial)
        self.assertEqual(replay.preferred_endpoint, target.endpoint_id)
        with closing(sqlite3.connect(self.case.journal)) as db:
            stored = json.loads(db.execute('SELECT record FROM read_endpoints WHERE endpoint_id=?',
                                           (target.endpoint_id,)).fetchone()[0])
        self.assertEqual(stored, thaw(target.record))
        self.assertEqual(set(stored['head_floor']), {'view_id', 'epoch', 'ordinal'})
        self.assertEqual(stored['parity'], self.parity())
        for key in ('generation', 'gen_id', 'view'):
            self.assertNotIn(key, stored)
        with self.resolved() as endpoint:
            self.assertEqual(endpoint.endpoint_id, target.endpoint_id)
            self.assertIn(endpoint, self.reader._read_only)
            result = self.query(endpoint)
            with self.assertRaisesRegex(ValueError, 'reader_endpoint_invalid'):
                self.dispatch.query_endpoint(endpoint, checkout=self.case.project, prompt='heron',
                    query_vector=None, deadline=time.monotonic() + 20, snapshot=object(),
                    decision={})
        emit('schema3-switched', selection=list(self.selection()), gen=result.data_generation,
             rows=[row['path'] for row in result.rows])
        self.assertEqual(result.store_format, 'schema3')
        self.assertEqual(result.endpoint_id, target.endpoint_id)
        self.assertEqual(result.reader_epoch, selected.epoch)
        self.assertEqual(result.data_generation, self.gens[-1])
        self.assertIn(self.case.WIKI + '/concepts/heron-route.md', [row['path'] for row in result.rows])
        # The sealed (non-persisted) target is a CAS token, never a query reader.
        with self.assertRaisesRegex(ValueError, 'reader_endpoint_not_issued'):
            self.query(target)

    # ---- 1.8 scenario 2 ---------------------------------------------------
    def test_02_missing_or_forged_parity_and_bridge_and_unknown_shapes_refuse(self):
        self.publish()
        initial = self.reader.read_selection(self.case.journal)
        target = self.seal()
        loaded = self.loaded(initial)
        # No shadow intent for this candidate: the U5 resolver proves nothing.
        self.assertIsNone(self.migration.shadow_parity_receipt(self.case.state, candidate=self.store_path,
            candidate_incarnation=target.record['incarnation']))
        self.refused('reader_parity_unproved', self.cas, target, loaded, base=initial)
        for name, receipt in (('other-migration', dict(parity=self.parity(migration_id='b' * 32))),
                              ('other-digest', dict(parity=self.parity(digest='e' * 64))),
                              ('other-incarnation', dict(candidate_incarnation='0' * 32)),
                              ('other-candidate', dict(candidate=str(self.case.state / 'other.sqlite'))),
                              ('extra-field', dict(extra=True))):
            with self.subTest(receipt=name), self.receipt(**receipt):
                self.refused('reader_parity_unproved', self.cas, target, loaded, base=initial)
        with self.receipt():
            self.refused('reader_bridge_unproved', self.cas, target, None, base=initial)
            self.refused('reader_bridge_unproved', self.cas, target, (loaded[0], 'f' * 40), base=initial)
        # Stored-shape refusals: added/missing store field and unknown format.
        record = thaw(target.record)
        attestation = (self.case.state / J.PRODUCTION_ATTESTATION).read_bytes()
        forged = {'extra-field': dict(record, extra=1),
                  'missing-floor': {k: v for k, v in record.items() if k != 'head_floor'},
                  'unknown-format': dict(record, store_format='schema4'),
                  'v6-capability': dict(record, capability='v6-ro-v1'),
                  'floor-shape': dict(record, head_floor=dict(record['head_floor'], extra=1)),
                  'generation-pin': dict(record, generation=self.gens[0])}
        copy = self.case.state / 'forged-journal.sqlite'
        with closing(sqlite3.connect(self.case.journal)) as source, closing(sqlite3.connect(copy)) as image:
            source.backup(image)
        copy.chmod(0o600)
        with closing(sqlite3.connect(copy)) as db:
            for name, value in forged.items():
                packed = J._canon(value).encode('utf8')
                endpoint_id = self.reader._sha(b'memvane-reader-endpoint-v1\0' + packed)
                db.execute('INSERT INTO read_endpoints VALUES(?,?,?)', (endpoint_id, packed.decode(), attestation))
                forged[name] = endpoint_id
            db.commit()
        with self.reader._readonly(copy, self.case.state) as db:
            for name, endpoint_id in forged.items():
                with self.subTest(stored=name), self.assertRaisesRegex(ValueError, 'reader_endpoint_invalid'):
                    self.reader._stored_endpoint(db, endpoint_id)
        copy.unlink()
        self.assertEqual(self.reader.read_selection(self.case.journal).preferred_endpoint,
                         initial.preferred_endpoint)

    def test_10_shadow_verified_receipt_from_migration_switches(self):
        """U5: the actual shadow migration seals the parity the CAS requires."""
        migration, case = self.migration, self.case
        store = self.views.ViewStore.open(self.store_path, repository=self.repository,
                                          scope_id=case.route['scopeId'])
        self.addCleanup(store.close)

        def step():
            return migration.advance_shadow(store, project=case.project, executing_root=case.plugin,
                                            deadline=time.monotonic() + 60)

        def run_until(state, limit=40):
            stages = []
            for _ in range(limit):
                result = step()
                stages.append(result['stage'])
                if result['migration_state'] == state:
                    return result, stages
            self.fail('%s not reached: %s' % (state, stages))

        with self.seed.authority():
            migration.begin_migration(store, worktree_id=self.worktree_id, project=case.project,
                                      executing_root=case.plugin, deadline=time.monotonic() + 30)
            caught, _ = run_until('caught_up')
        generation = caught['intent']['published']['generation']
        initial = self.reader.read_selection(case.journal)
        # caught_up is not parity: no receipt, and a CAS naming the private
        # head generation is refused without journal change.
        self.assertIsNone(migration.shadow_parity_receipt(case.state, candidate=self.store_path,
            candidate_incarnation=store._incarnation))
        guessed = self.seal(self.parity(generation=generation,
                                        migration_id=caught['intent']['migration_id']))
        loaded = self.loaded(initial)
        self.refused('reader_parity_unproved', self.cas, guessed, loaded, base=initial)

        with self.seed.authority():
            verified, stages = run_until('shadow_verified')
        self.assertIn('parity', stages)
        self.assertEqual(stages[-1], 'parity_seal')
        receipt = migration.shadow_parity_receipt(case.state, candidate=self.store_path,
                                                  candidate_incarnation=store._incarnation)
        self.assertEqual(set(receipt), {'migration_id', 'generation', 'digest', 'candidate',
                                        'candidate_incarnation'})
        self.assertEqual((receipt['migration_id'], receipt['generation'], receipt['candidate']),
                         (verified['intent']['migration_id'], generation, str(self.store_path)))
        self.assertIsNone(migration.shadow_parity_receipt(case.state, candidate=self.store_path,
            candidate_incarnation='0' * 32))
        self.assertIsNone(migration.shadow_parity_receipt(case.state,
            candidate=case.state / 'other.sqlite', candidate_incarnation=store._incarnation))
        parity = {key: receipt[key] for key in self.reader.PARITY_KEYS}
        target = self.seal(parity)
        selected = self.cas(target, loaded, base=initial)
        emit('u5-receipt-switch', stages=stages, pages=verified['intent']['parity']['pages'])
        self.assertEqual((selected.epoch, selected.preferred_endpoint, selected.previous_endpoint),
                         (initial.epoch + 1, target.endpoint_id, initial.preferred_endpoint))
        with closing(sqlite3.connect(case.journal)) as db:
            stored = json.loads(db.execute('SELECT record FROM read_endpoints WHERE endpoint_id=?',
                                           (target.endpoint_id,)).fetchone()[0])
        self.assertEqual(stored['parity'], parity)

    def test_02b_parity_generation_must_not_exceed_floor(self):
        self.publish()
        target = self.seal()
        newer = self.publish()
        record = thaw(target.record)
        self.reader._schema3_parity_floor(record, deadline=time.monotonic() + 20)
        record['parity'] = self.parity(generation=newer)
        with self.assertRaisesRegex(ValueError, 'reader_parity_unproved'):
            self.reader._schema3_parity_floor(record, deadline=time.monotonic() + 20)
        record['parity'] = self.parity(generation='g-' + '0' * 32 + '-b' + '0' * 64)
        with self.assertRaisesRegex(ValueError, 'reader_parity_unproved'):
            self.reader._schema3_parity_floor(record, deadline=time.monotonic() + 20)
        # A seal naming an unknown parity generation is never issued.
        with self.assertRaisesRegex(ValueError, 'reader_parity_unproved'):
            self.seal(record['parity'])

    # ---- 1.8 scenario 3 ---------------------------------------------------
    def test_03_one_install_identity_field_changed_refuses(self):
        self.publish()
        initial = self.reader.read_selection(self.case.journal)
        target = self.seal()
        loaded = self.loaded(initial)
        self.assertEqual(len(self.reader.INSTALL_IDENTITY), 10)
        real = self.reader._recheck
        for field in self.reader.INSTALL_IDENTITY:
            def changed(endpoint, field=field):
                record, raw, proof = real(endpoint)
                if endpoint is target:
                    value = record[field]
                    record = dict(record)
                    record[field] = ([0, 0] if field.endswith('_node') else ['other'] if field == 'roots'
                                     else '0' * 64 if len(str(value)) == 64 else str(value) + '-other')
                return record, raw, proof
            with self.subTest(field=field), self.receipt(), \
                    mock.patch.object(self.reader, '_recheck', side_effect=changed):
                # An attestation-only step passes the shape check (a bundle upgrade
                # during the migration) and must then be proved on the v6
                # endpoint's sealed successor chain, which this install has none of.
                self.refused('reader_successor_unproved' if field == 'attestation_sha256'
                             else 'reader_install_changed', self.cas, target, loaded, base=initial)
        # Store identity differs from the v6 base by design and is accepted.
        with self.receipt():
            selected = self.cas(target, loaded, base=initial)
        self.assertEqual(selected.preferred_endpoint, target.endpoint_id)

    # ---- 1.8 scenario 4 ---------------------------------------------------
    def test_04_crash_around_schema3_cas_reads_only_complete_old_or_new(self):
        self.publish()
        initial = self.reader.read_selection(self.case.journal)
        target = self.seal()
        loaded = self.loaded(initial)
        with closing(sqlite3.connect(self.case.journal)) as db:
            old_record = json.loads(db.execute('SELECT record FROM read_endpoints WHERE endpoint_id=?',
                                               (initial.preferred_endpoint,)).fetchone()[0])
        new_record = thaw(target.record)
        for step in ('v5-image-committed', 'v5-image-replaced'):
            with self.subTest(step=step):
                current = self.reader.read_selection(self.case.journal)
                parent, remote = CTX.Pipe()

                def run():
                    parent.close()
                    def checkpoint(name):
                        if name == step:
                            remote.send(name)
                            os._exit(EXIT_AT_CHECKPOINT)
                    try:
                        with self.receipt(), mock.patch.object(J, '_conversion_step', checkpoint):
                            self.cas(target, loaded, base=current)
                        remote.send('completed-without-checkpoint')
                    except BaseException as exc:
                        remote.send('child-error:%s:%s' % (type(exc).__name__, exc))
                    finally:
                        os._exit(3)
                process = CTX.Process(target=run)
                process.start()
                remote.close()
                try:
                    reached = parent.recv() if parent.poll(60) else 'timeout'
                except EOFError:
                    reached = 'eof'
                process.join(30)
                self.assertEqual(reached, step, 'child exitcode=%r' % (process.exitcode,))
                self.assertEqual(process.exitcode, EXIT_AT_CHECKPOINT)
                with self.resolved() as endpoint:
                    record = thaw(endpoint.record)
                    result = self.query(endpoint) if record['store_format'] == 'schema3' else None
                switched = step == 'v5-image-replaced'
                emit('schema3-cas-crash', step=step, store_format=record['store_format'],
                     selection=list(self.selection()))
                expected = new_record if switched else old_record
                self.assertEqual({k: record[k] for k in expected if k != 'legacy_sidecar_baseline'},
                                 {k: v for k, v in expected.items() if k != 'legacy_sidecar_baseline'})
                self.assertEqual(set(record) - {'legacy_sidecar_baseline'},
                                 set(expected) - {'legacy_sidecar_baseline'})
                self.assertEqual(self.selection()[1], target.endpoint_id if switched else initial.preferred_endpoint)
                if switched:
                    self.assertEqual(result.store_format, 'schema3')
        with self.receipt():
            self.assertEqual(self.cas(target, None, base=initial).preferred_endpoint, target.endpoint_id)
        self.assertFalse((self.case.state / '.reader-next.sqlite').exists())

    # ---- 1.8 scenario 5 ---------------------------------------------------
    def test_05_loaded_bridge_outside_ttl_refuses_before_and_at_replace(self):
        self.publish()
        initial = self.reader.read_selection(self.case.journal)
        target = self.seal()
        loaded = self.loaded(initial)
        real = time.time
        late = SimpleNamespace(time=lambda: real() + 601, monotonic=time.monotonic)
        with self.receipt(), mock.patch.object(self.bridge, 'time', late):
            self.refused('reader_hosts_pending', self.cas, target, loaded, base=initial)
        expired = []
        clock = SimpleNamespace(time=lambda: real() + (601 if expired else 0), monotonic=time.monotonic)
        def checkpoint(name):
            if name == 'v5-image-fsynced':
                expired.append(name)
        with self.receipt(), mock.patch.object(self.bridge, 'time', clock), \
                mock.patch.object(J, '_conversion_step', checkpoint):
            self.refused('reader_hosts_pending', self.cas, target, loaded, base=initial)
        self.assertEqual(expired, ['v5-image-fsynced'])
        self.assertFalse((self.case.state / '.reader-next.sqlite').exists())
        # A receipt observed at another reader epoch is not the CAS base.
        stale = SimpleNamespace(epoch=initial.epoch + 1, preferred_endpoint=initial.preferred_endpoint)
        with self.receipt():
            self.refused('reader_hosts_pending', self.cas, target, self.loaded(stale), base=initial)

    # ---- 1.8 scenario 7 ---------------------------------------------------
    def test_07_head_below_floor_or_behind_tip_is_refused(self):
        first = self.publish()
        floor_gen = self.publish()
        initial = self.reader.read_selection(self.case.journal)
        target = self.seal(self.parity(generation=first))
        tip = self.publish()
        self.assertEqual(target.record['head_floor']['epoch'], 2)
        with self.receipt(self.parity(generation=first)):
            self.cas(target, self.loaded(initial), base=initial)
        with self.resolved() as endpoint:
            self.assertEqual(self.query(endpoint).data_generation, tip)
            view = target.record['head_floor']['view_id']
            # Older generation at/above the floor: the store tip check refuses.
            self.tamper('view_head_update', 'UPDATE view_heads SET gen_id=?,epoch=2 WHERE view_id=?',
                        (floor_gen, view))
            with self.assertRaisesRegex(ValueError, 'view_head_invalid'):
                self.query(endpoint)
            # Below the floor with later generations unpublished: the floor refuses.
            self.tamper('generation_transition', "UPDATE generations SET state='prepared' WHERE gen_id IN (?,?)",
                        (floor_gen, tip))
            self.tamper('view_head_update', 'UPDATE view_heads SET gen_id=?,epoch=1 WHERE view_id=?',
                        (first, view))
            with self.assertRaisesRegex(ValueError, 'reader_head_regressed'):
                self.query(endpoint)
            # Accepted same-UID limit: trigger removal plus unpublishing every
            # later generation at or above the floor is served (not detected).
            self.tamper('generation_transition', "UPDATE generations SET state='published' WHERE gen_id=?",
                        (floor_gen,))
            self.tamper('view_head_update', 'UPDATE view_heads SET gen_id=?,epoch=2 WHERE view_id=?',
                        (floor_gen, view))
            limit = self.query(endpoint)
        emit('schema3-floor', floor=dict(target.record['head_floor']), acceptedLimitServed=limit.data_generation)
        self.assertEqual(limit.data_generation, floor_gen)

    # ---- 1.7 / rollback ---------------------------------------------------
    def test_08_rollback_reissues_v6_with_original_sidecar_baseline_and_v6_reader_reads(self):
        initial, target, selected = self.switched()
        with closing(sqlite3.connect(self.case.journal)) as db:
            original = json.loads(db.execute('SELECT record FROM read_endpoints WHERE endpoint_id=?',
                                             (initial.preferred_endpoint,)).fetchone()[0])
        with self.seed.authority():
            installer = self.reader.seal_current_reader(self.case.state, self.case.project,
                                                        executing_root=self.case.plugin)
            back = self.reader.cas_reader(self.case.journal, expected_epoch=selected.epoch,
                expected_endpoint=selected.preferred_endpoint, target=installer.endpoint,
                transition_id='2' * 32)
            # schema3 -> schema3 is not a selectable transition.
            again = self.seal()
        self.assertEqual(dict(installer.endpoint.record).get('legacy_sidecar_baseline'),
                         original.get('legacy_sidecar_baseline'))
        self.assertIn('legacy_sidecar_baseline', original)
        self.assertEqual(back.preferred_endpoint, installer.endpoint.endpoint_id)
        with self.resolved() as endpoint:
            self.assertEqual(endpoint.record['store_format'], 'v6')
            self.assertEqual(endpoint.endpoint_id, initial.preferred_endpoint)
        loaded = self.loaded(back)
        with self.receipt():
            switched = self.cas(again, loaded, base=back, transition='4' * 32)
            self.assertEqual(switched.preferred_endpoint, again.endpoint_id)
            other = self.seal()
            self.refused('reader_format_not_selectable', self.cas, other, loaded, base=switched,
                         transition='5' * 32)

    # ---- 1.8 scenario 8 ---------------------------------------------------
    def test_09_pr5_bundle_fails_closed_on_schema3_selected_journal(self):
        self.switched()
        exported = Path(tempfile.mkdtemp(prefix='pr5-bundle-', dir=self.case.home))
        archive = subprocess.run(['git', '-C', str(ROOT), 'archive', PR5_BUNDLE_COMMIT, 'core'],
                                 capture_output=True, timeout=60)
        self.assertEqual(archive.returncode, 0, archive.stderr.decode(errors='replace'))
        subprocess.run(['tar', '-x', '-C', str(exported)], input=archive.stdout, check=True, timeout=60)
        program = r'''
import json,sys
from pathlib import Path
core,journal,project,plugin=sys.argv[1:5]
sys.path.insert(0,core)
import laya_journal_reader as R, laya_reader_lease as L, laya_writer_open_plan as W
assert R.__file__.startswith(core),R.__file__
path=Path(journal);out={}
def attempt(name,fn):
    try:fn();out[name]='accepted'
    except Exception as exc:out[name]=str(exc)
def selection():
    with R._readonly(path,path.parent) as db:R._selection(db)
def image():
    with R._readonly(path,path.parent) as db:L._image(db)
attempt('selection',selection)
attempt('lease_image',image)
attempt('cas_image',lambda:R.mutate_image(path,lambda db:True))
attempt('writer_plan',lambda:W._snapshot(Path(project),Path(plugin)))
print(json.dumps(out))
'''
        before = Path(self.case.journal).read_bytes()
        result = subprocess.run([sys.executable, '-B', '-c', program, str(exported / 'core'),
                                 str(self.case.journal), str(self.case.project), str(self.case.plugin)],
                                capture_output=True, text=True, timeout=60,
                                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        observed = json.loads(result.stdout.strip().splitlines()[-1])
        emit('pr5-bundle-on-schema3', commit=PR5_BUNDLE_COMMIT, observed=observed)
        self.assertEqual(observed, {name: 'reader_endpoint_invalid' for name in
                                    ('selection', 'lease_image', 'cas_image', 'writer_plan')})
        self.assertEqual(Path(self.case.journal).read_bytes(), before)


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
"""U11 main-checkout recall over a journal-selected schema3 reader endpoint.

Builds on the U10 fixture: a real v5 install, v6 index, schema3 view store,
journal CAS and Git checkout. Synthetic authority is the same as there
(bundle inspection, host observation, U5 parity receipt) plus the loaded
plugin root of the host-response module. No actual host process, provider
or operational setup entry point is exercised.
"""
from contextlib import ExitStack, contextmanager, redirect_stderr
import hashlib
import importlib
import importlib.util
import io
import json
import multiprocessing
import os
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('schema3_endpoint_checks', HERE / 'laya-journal-schema3-checks.py')
endpoint_checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(endpoint_checks)
spec = importlib.util.spec_from_file_location('sqlite_hot_journal', HERE / 'sqlite_hot_journal.py')
hot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hot)
seed_module = endpoint_checks.seed_module
J = endpoint_checks.J
CTX = multiprocessing.get_context('fork')
HOSTS = ('claude', 'codex', 'hermes')
SPAN_CARD = 'concepts/heron-route.md'
_OPENED = []


def _audit(event, args):
    if event == 'open' and _OPENED and isinstance(args[0], (str, bytes, os.PathLike)):
        _OPENED[-1].append(os.fsdecode(args[0]))


sys.addaudithook(_audit)


def emit(case, **evidence):
    print(json.dumps(dict(case=case, **evidence), sort_keys=True, default=str), flush=True)


class Schema3RecallChecks(unittest.TestCase):
    def setUp(self):
        parent = seed_module.base.published.MoveCase
        original = parent.publish_all

        def publish_with_span(case, roots):
            # One verifier-cited span on the heron card: v6 shows it from the
            # frontmatter, schema3 from the imported card_sources row.
            raw = (case.project / 'retained-source.md').read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            span = {'path': 'retained-source.md', 'sourceSha256': digest,
                    'startLine': 1, 'endLine': 1, 'spanSha256': digest}
            path = case.auto / 'wiki' / SPAN_CARD
            body = path.read_text(encoding='utf8')
            if 'sourceSpans:' not in body:
                path.write_text(body.replace('\n---\n', '\nsourceSpans:\n  - ' + json.dumps(span) + '\n---\n', 1),
                                encoding='utf8')
            return original(case, roots)
        class BaseOnlySeed(seed_module.MoveSealChecks):
            # Completed sidecar rows would label v6 rows preview-*; the base-only
            # install compares one generation header with one generation header.
            PUBLISH_SIDECAR = False
        self.u10 = endpoint_checks.Schema3EndpointChecks('runTest')
        with mock.patch.object(seed_module, 'MoveSealChecks', BaseOnlySeed), \
                mock.patch.object(parent, 'publish_all', new=publish_with_span):
            self.u10.setUp()
        self.addCleanup(self.u10.doCleanups)
        self.case = self.u10.case
        self.host = importlib.import_module('memvane.host.host_response')
        self.dispatch = importlib.import_module('memvane.recall.reader_dispatch')
        self.event_log = importlib.import_module('memvane.diagnostics.event_log')
        self.runtime = importlib.import_module('laya_index_runtime')

    @contextmanager
    def loaded(self):
        """Full bundle authority with this checkout's host-response loaded from case.plugin."""
        with ExitStack() as stack:
            stack.enter_context(self.u10.seed.authority())
            stack.enter_context(mock.patch.object(
                self.host, '__file__', str(self.case.plugin / 'memvane/host/host_response.py')))
            yield

    def recall(self, host='claude', prompt='heron route northern marsh'):
        """One selected-route recall; returns (context, stderr, event fields, query results, opened)."""
        case = self.case
        decision = J.select_route(str(case.project), action='recall')
        self.assertEqual(decision['state'], 'candidate')
        payload = {'cwd': str(case.project), 'prompt': prompt, 'session_id': 'synthetic-' + host,
                   'hook_event_name': 'UserPromptSubmit'}
        results, events, stderr = [], [], io.StringIO()
        actual_query = self.dispatch.query_endpoint

        def observed(*args, **kwargs):
            result = actual_query(*args, **kwargs)
            results.append(result)
            return result

        def recorded(event, **fields):
            events.append(dict(fields, event=event))
            return True
        _OPENED.append([])
        try:
            with self.loaded(), redirect_stderr(stderr), \
                    mock.patch.object(self.dispatch, 'query_endpoint', side_effect=observed), \
                    mock.patch.object(self.event_log, 'emit', side_effect=recorded), \
                    mock.patch.object(self.runtime, 'LayaIndexRuntime',
                                      wraps=self.runtime.LayaIndexRuntime) as v6_runtime:
                answer = self.host.serve_selected('recall', host, decision, payload)
        finally:
            opened = _OPENED.pop()
        context = (answer['context'] if host == 'hermes'
                   else answer['hookSpecificOutput']['additionalContext'])
        recall = [event for event in events if event['event'] == 'recall']
        self.assertEqual(len(recall), 1, events)
        return dict(context=context, stderr=stderr.getvalue(), event=recall[0], results=results,
                    opened=opened, v6_runtime=v6_runtime.call_count)

    def v6_generation(self):
        return self.case.head()[0]

    def raw_opens(self, opened):
        return [path for path in opened if path.endswith('retained-source.md')]

    # ---- completion 1, design 2.5 scenarios 1-2 --------------------------
    def test_01_main_recall_reads_schema3_view_with_v6_output_shape(self):
        case = self.case
        before = self.recall()
        v6_gen = self.v6_generation()
        self.assertEqual(before['v6_runtime'], 1)
        self.assertIn('generation ' + v6_gen, before['context'])
        self.assertIn('"' + case.WIKI + '/' + SPAN_CARD + '"', before['context'])
        self.assertIn('retained-source.md#L1-L1', before['context'])
        _initial, target, selected = self.u10.switched()
        gen = self.u10.gens[-1]
        emitted = {}
        for host in HOSTS:
            with self.subTest(host=host):
                after = self.recall(host)
                emitted[host] = dict(context=after['context'], v6Runtime=after['v6_runtime'],
                                     rawOpens=len(self.raw_opens(after['opened'])),
                                     event={k: after['event'].get(k) for k in
                                            ('outcome', 'reason', 'notes', 'cards', 'store_format',
                                             'generation', 'selection_fallback', 'vector_fallback')})
                # RED: the v6 runtime answered the switched install.
                self.assertEqual(after['v6_runtime'], 0, after['context'])
                self.assertIn('generation ' + gen, after['context'])
                self.assertEqual(after['context'], before['context'].replace(v6_gen, gen))
                self.assertEqual(self.raw_opens(after['opened']), [])
                result = after['results'][-1]
                self.assertEqual((result.store_format, result.endpoint_id, result.reader_epoch,
                                  result.data_generation),
                                 ('schema3', target.endpoint_id, selected.epoch, gen))
                self.assertTrue(result.rows)
                for row in result.rows:
                    self.assertEqual(set(row), self.host._ROW_KEYS)
                    self.assertEqual(row['scope'], case.route['scopeId'])
                    self.assertTrue(row['path'].startswith(case.WIKI + '/'))
                    self.assertEqual(row['hash'], hashlib.sha256(row['body'].encode()).hexdigest())
                self.assertEqual(result.source_locations,
                                 {case.WIKI + '/' + SPAN_CARD: ('retained-source.md#L1-L1',)})
                self.assertEqual(result.title_only, frozenset())
                for key in ('outcome', 'reason', 'cards', 'count'):
                    self.assertEqual(after['event'].get(key), before['event'].get(key), key)
                self.assertEqual(after['event']['store_format'], 'schema3')
                self.assertEqual(after['event']['reader_endpoint'], target.endpoint_id)
                self.assertEqual(after['event']['generation'], gen)
                self.assertEqual(after['event']['selection_fallback'], 'laya_selector_not_prepared')
                # The fixture's sealed manifest is unusable here: v6 and schema3
                # name the same provider fallback and serve lexical lists.
                self.assertEqual(after['event']['notes'], before['event']['notes'])
                self.assertEqual(after['event']['vector_fallback'], before['event']['vector_fallback'])
                self.assertTrue(after['event']['vector_fallback'].startswith('selected_provider_manifest_'))
                self.assertIn(after['event']['vector_fallback'] + '; serving lexical results',
                              after['stderr'])
        # One full generation verification per recall (the serving pin): its
        # lexical/exact reads reuse it and the closing head check skips it.
        view_store = importlib.import_module('memvane.store.view.view_store')
        original, verified = view_store.ViewStore._verify_generation, []

        def counted(store, *args, **kwargs):
            verified.append(kwargs.get('full_vectors', False))
            return original(store, *args, **kwargs)
        with mock.patch.object(view_store.ViewStore, '_verify_generation', counted):
            again = self.recall('claude')
        self.assertEqual(again['context'], emitted['claude']['context'])
        self.assertEqual(verified, [False])
        emit('schema3-main-recall', v6Generation=v6_gen, schema3Generation=gen,
             v6Event={k: before['event'].get(k) for k in ('outcome', 'reason', 'notes', 'cards')},
             hosts=emitted)

    # ---- completion 2 -----------------------------------------------------
    def test_02_unsealed_view_and_admission_refusal_serve_lexical_with_named_fallback(self):
        self.u10.switched()
        resident = importlib.import_module('memvane.resident.embed_resident')
        embed = importlib.import_module('memvane.provider.adapters.local_embed_provider')

        class UsableManifest:
            # Synthetic: the sealed manifest constructs a provider. It is never
            # asked for a vector here (unsealed view or refused admission).
            def __init__(self, manifest):
                self.manifest = manifest
        woken = []

        def wake(decision, proof, **kwargs):
            woken.append(proof)
            return 'selected_provider_preparing'
        observed = {}
        with mock.patch.object(embed, 'ReviewedLocalQmdProvider', UsableManifest), \
                mock.patch.object(self.host, '_wake_provider', side_effect=wake):
            observed['unsealed'] = self.recall()
            self.assertEqual(woken, [])
            with mock.patch.object(resident, 'admit_query', side_effect=RuntimeError('pressure')):
                observed['deferred'] = self.recall()
        emit('schema3-vector-fallback', **{name: dict(notes=value['event']['notes'],
             vector=value['event'].get('vector_fallback'), stderr=value['stderr'].strip())
             for name, value in observed.items()}, wakes=len(woken))
        for name, code in (('unsealed', 'selected_view_vectors_pending'),
                           ('deferred', 'selected_provider_resource_deferred')):
            after = observed[name]
            self.assertIn('generation ' + self.u10.gens[-1], after['context'])
            self.assertEqual(after['event']['vector_fallback'], code)
            self.assertEqual(after['event']['notes'], ['laya_vector_' + code])
            self.assertIn(code + '; serving lexical results', after['stderr'])
            self.assertEqual(after['results'][-1].semantic_status, 'provider_absent')
        # A non-degraded main recall wakes with the route's own full proof.
        self.assertEqual(len(woken), 1)
        self.assertIs(woken[0]['productRouteAllowed'], True)

    # ---- completion 4 -----------------------------------------------------
    def test_03_learning_selector_receives_schema3_rows(self):
        self.u10.switched()
        selection = importlib.import_module('memvane.learning.context_learning.selection_read')
        port = importlib.import_module('memvane.learning.context_learning.selection_port')
        seen = []

        def select(pool, **kwargs):
            seen.append(port.published_candidate_rows(pool, kwargs['wiki_names'], kwargs['scope_id']))
            return {'reason': 'selected', 'final_results': pool[:1]}
        with mock.patch.object(selection, 'selector_prepared', return_value=True), \
                mock.patch.object(selection, 'select_published', side_effect=select):
            chosen = self.recall()
        emit('schema3-selector', candidates=[row['id'] for row in seen[0]],
             selection=chosen['event'].get('selection'), cards=chosen['event']['cards'])
        self.assertEqual(len(seen), 1)
        self.assertTrue(all(row['id'].startswith(self.case.WIKI + '/') for row in seen[0]))
        self.assertIn('local Laya model selected these cards', chosen['context'])
        self.assertEqual(chosen['event']['count'], 1)
        self.assertIsNone(chosen['event'].get('selection_fallback'))

    # ---- completion 3, design 2.5 scenario 5 -----------------------------
    def test_04_recalls_during_reader_cas_each_complete_in_old_or_new(self):
        u10 = self.u10
        u10.publish()
        initial = u10.reader.read_selection(self.case.journal)
        target = u10.seal()
        loaded = u10.loaded(initial)
        old_gen, new_gen = self.v6_generation(), u10.gens[-1]
        parent, remote = CTX.Pipe()

        def run():
            parent.close()
            try:
                remote.recv()
                with u10.receipt():
                    u10.cas(target, loaded, base=initial)
                remote.send('switched')
            except BaseException as exc:
                remote.send('child-error:%s:%s' % (type(exc).__name__, exc))
            finally:
                os._exit(0)
        process = CTX.Process(target=run)
        process.start()
        remote.close()
        served, retried, status = [], 0, None
        try:
            parent.send('go')
            for attempt in range(50):
                answer = self.recall()
                found = [gen for gen in (old_gen, new_gen) if 'generation ' + gen in answer['context']]
                served.append(found)
                retried += 'laya_recall_proof_retry' in (answer['event'].get('notes') or [])
                if status is None and parent.poll():
                    try:
                        status = parent.recv()
                    except EOFError:
                        status = 'child-died'
                if status is not None and found == [new_gen]:
                    break
            if status is None:
                try:
                    status = parent.recv() if parent.poll(60) else 'timeout'
                except EOFError:
                    status = 'child-died'
        finally:
            process.join(30)
        emit('schema3-cas-concurrent-recall', recalls=len(served), retried=retried, status=status,
             childExit=process.exitcode,
             old=sum(found == [old_gen] for found in served), new=sum(found == [new_gen] for found in served))
        self.assertEqual(status, 'switched', 'child exitcode=%r' % (process.exitcode,))
        self.assertEqual(process.exitcode, 0)
        self.assertTrue(all(found in ([old_gen], [new_gen]) for found in served), served)
        self.assertEqual(self.recall()['results'][-1].data_generation, new_gen)

    # ---- v6 selection keeps the existing path ----------------------------
    def test_05_v6_selected_v5_journal_keeps_existing_recall(self):
        self.assertEqual(J.journal_version(self.case.journal), 5)
        with mock.patch.object(self.host, 'serve_endpoint_recall',
                               side_effect=AssertionError('endpoint reader used')):
            first = self.recall()
        self.assertEqual(first['v6_runtime'], 1)
        self.assertEqual(first['results'], [])
        self.assertIsNone(first['event'].get('store_format'))
        self.assertIn('generation ' + self.v6_generation(), first['context'])


    # ---- cold view (design 14-1, D9) -------------------------------------
    def test_06_new_branch_view_without_generation_answers_preparing_without_context(self):
        import subprocess
        self.u10.switched()
        subprocess.run(['git', '-C', str(self.case.project), 'checkout', '-q', '-b', 'cold-side'],
                       check=True, capture_output=True, timeout=10)
        time.sleep(1.1)  # Git racy-index window before the checkout capture.
        emitted = {}
        for host in HOSTS:
            with self.subTest(host=host):
                answer = self.recall(host)
                self.assertEqual(answer['context'], '')
                self.assertEqual((answer['event'].get('outcome'), answer['event'].get('reason')),
                                 ('empty', 'view_preparing'), answer['event'])
                self.assertIn('laya_view_preparing', answer['event'].get('notes') or [])
                self.assertEqual((answer['results'], answer['v6_runtime']), ([], 0))
                self.assertNotIn('request refused', answer['stderr'])
                emitted[host] = answer['event'].get('reason')
        emit('schema3-recall-cold-view', reasons=emitted)

    # ---- hot rollback journal ----------------------------------------------
    def test_07_hot_journal_refuses_recall_with_recovery_reason_and_offers_sync(self):
        from memvane.kernel.errors import _query_only_uri
        from memvane.runtime import runtime_work
        case = self.case
        self.u10.switched()
        store = self.u10.store_path
        _before, journal = hot.kill_mid_write(store)
        torn = store.read_bytes()
        decision = J.select_route(str(case.project), action='recall')
        payload = {'cwd': str(case.project), 'prompt': 'heron route northern marsh',
                   'session_id': 'synthetic-hot', 'hook_event_name': 'UserPromptSubmit'}
        requests = []

        def request(kind, root, **kwargs):
            requests.append((kind, root))
            return {'status': 'queued'}
        with self.loaded(), redirect_stderr(io.StringIO()), \
                mock.patch.object(runtime_work, 'request', side_effect=request):
            try:
                answer = self.host.serve_selected('recall', 'claude', decision, payload)
                outcome = ('served', bool(answer['hookSpecificOutput']['additionalContext']))
            except Exception as exc:  # noqa: BLE001 - the reason code is the evidence
                outcome = ('refused', type(exc).__name__, str(exc))
        wrote = not journal.exists() or store.read_bytes() != torn
        recall_rw = _query_only_uri(store).endswith('?mode=rw')
        emit('schema3-recall-hot-journal', python=sys.version.split()[0], recallOpensRw=recall_rw,
             outcome=outcome, wrote=wrote, requests=requests)
        if recall_rw:
            # macOS Python 3.9 opens rw (its WAL workaround): SQLite rolls back.
            self.assertEqual((outcome, wrote), (('served', True), True))
            return
        self.assertEqual(outcome, ('refused', 'JournalEvidenceError', 'reader_store_recovery_pending'))
        self.assertFalse(wrote)
        self.assertEqual(requests, [('laya-sync', str(case.project))])


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
"""U12e: vector fill and vector generations of a selected schema3 install.

Seeded from the U14 consumers fixture (U12c sync fixture underneath): the v5
install, v6 index, journal images, Git checkout and view store are actual,
and the schema3 record is the U10 seal selected with ``mutate_image``. The
sync pass, view fill, reseal, persisted recall reader, dedup scan and
compile retrieval are the actual modules.

Synthetic: the fixture attestation's provider manifest has no recipe or
model digest, so the install provider's view spec is derived with the QMD
chunk recipe id and a fixed model digest filled in (``provider_specs``);
the provider is a stub whose chunk positions follow the provider's own
first chunking pass and whose vectors are a hash of the chunk text; the
query vector is injected where the reader would call the warm provider.
No model loads.
"""
from contextlib import ExitStack, closing, contextmanager
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('schema3_vectors_seed',
                                              ROOT / 'test/fixtures/laya-schema3-consumers-checks.py')
seed_module = importlib.util.module_from_spec(spec); spec.loader.exec_module(seed_module)

# The seed setUp replaces ``runtime_work.request``; the wake-queue checks keep
# the real one, captured here before any setUp runs.
REAL_WAKE_REQUEST = importlib.import_module('memvane.runtime.runtime_work').request

MODEL_SHA = '3' * 64
RECIPE = 'qmd-regex-token-chunks-candidate-v1'


def emit(label, **values):
    print(json.dumps(dict(case=label, **values), sort_keys=True, default=str), flush=True)


def text_vector(text):
    digest = hashlib.sha256(text.encode('utf8')).digest()
    return [0.05 + digest[index] / 255.0 for index in range(3)]


class Coordinator:
    """Provider lane stub; ``refuse`` names a resource admission refusal."""

    def __init__(self, refuse=None):
        self.refuse, self.lanes = refuse, []
        self.root = None  # the wake queue's default root (this temp HOME)

    @contextmanager
    def activity(self, lane, policy, report):
        from memvane.kernel.resource_deferred import ResourceDeferred
        self.lanes.append(lane)
        if self.refuse is not None:
            raise ResourceDeferred(self.refuse)
        yield 1

    def check(self, policy, report):
        return {'ready': True}


class Schema3VectorChecks(seed_module.Schema3ConsumerChecks):
    def setUp(self):
        super().setUp()
        self.VV = importlib.import_module('memvane.store.view_vectors')
        self.provider_calls = []
        self.split = set()  # card bodies (by marker) the stub provider re-splits
        real = self.VV.provider_specs

        def specs(manifest, profile):
            return real(dict(manifest or {}, recipeId=RECIPE, modelSha256=MODEL_SHA), profile)
        for patch in (mock.patch.object(self.VV, 'provider_specs', new=specs),
                      mock.patch.object(self.worker, '_schema3_worker_manifest',
                                        new=lambda decision, proof: proof['providerManifest']),
                      mock.patch.object(self.worker, '_higher_priority_wake', return_value=False),
                      mock.patch.object(self.worker, 'ReviewedLocalQmdProvider', new=self.stub_provider())):
            patch.start(); self.addCleanup(patch.stop)
        self.spec = self.VV.provider_spec({'providerPath': 'x'}, self.case.route['vectorProfile'])
        self.assertIsNotNone(self.spec)

    def stub_provider(self):
        checks = self

        class StubProvider:
            def __init__(self, manifest):
                profile = checks.case.route['vectorProfile']
                self.manifest = manifest
                self.profile = {'profile_id': profile['profileId'], 'model_id': profile['modelId'],
                                'embedding_fingerprint': profile['embeddingFingerprint'],
                                'dimension': profile['dimension'], 'metric': profile['metric']}

            def embed_document_chunks(self, body, path, timeout=None):
                checks.provider_calls.append(('chunks', path))
                parts = checks.VV.qmd_first_pass(body)
                title = importlib.import_module('memvane.store.blob_values').markdown_title(body, path[6:-3])
                if any(marker in body for marker in checks.split):
                    # A token re-split: one more chunk position than the first pass.
                    parts = parts + [(len(body.encode('utf-16-le')) // 2 - 1, body[-1:])]
                return [{'seq': seq, 'pos': pos, 'vector': text_vector('title: ' + (title or 'none')
                                                                       + ' | text: ' + text)}
                        for seq, (pos, text) in enumerate(parts)]

            def embed_document(self, body, title):
                checks.provider_calls.append(('document', title))
                return text_vector('title: ' + (title or 'none') + ' | text: ' + body)
        return StubProvider

    def service(self, manifest, *, admit):
        class Service:
            def stop(self):
                pass
        return Service()

    def run_vector_pass(self, coordinator=None, *, authority=None):
        """One sync pass with the stub provider; returns (result, ingests, provider calls)."""
        original = self.views.ViewStore.ingest_attested_card
        calls, before = [], len(self.provider_calls)

        def counted(store, *args, **kwargs):
            calls.append(args[1] if len(args) > 1 else kwargs.get('path'))
            return original(store, *args, **kwargs)
        with (authority or self.seed.authority()), \
                mock.patch.object(self.views.ViewStore, 'ingest_attested_card', new=counted):
            result = self.worker.run(self.case.project, coordinator=coordinator or Coordinator(),
                                     report={'ready': True}, start_service=self.service)
        return result, calls, self.provider_calls[before:]

    def stage(self, gen_id):
        with closing(sqlite3.connect(self.store_path.as_uri() + '?mode=ro', uri=True)) as db:
            row = db.execute('SELECT s.stage_json FROM generations g JOIN generation_slots s '
                             'ON s.ordinal=g.ordinal WHERE g.gen_id=?', (gen_id,)).fetchone()
            refs = db.execute('SELECT count(*) FROM member_vectors WHERE gen_id=?', (gen_id,)).fetchone()[0]
        return json.loads(row[0]), refs

    def selected_record(self):
        return self.current()[1]

    def eligible(self, gen_id):
        return {path: blob for path, (blob, eligible) in self.members(gen_id).items() if eligible}

    @contextmanager
    def query_vector(self, endpoint, decision, prompt, vector):
        """The reader's approved query vector, with the warm provider call injected."""
        provider_module = importlib.import_module('memvane.provider.adapters.local_embed_provider')
        record, _raw, proof = self.R._recheck(endpoint)
        provider = object.__new__(provider_module.ReviewedLocalQmdProvider)
        profile = proof['vectorProfile']
        provider.manifest = proof['providerManifest']
        provider.profile = {'profile_id': profile['profileId'], 'model_id': profile['modelId'],
                            'embedding_fingerprint': profile['embeddingFingerprint'],
                            'dimension': profile['dimension'], 'metric': profile['metric']}
        real = self.dispatch._approved_profile_matches

        def approved(candidate, current):
            # The fixture manifest names no model digest; the spec's is MODEL_SHA.
            manifest = dict(current.get('providerManifest') or {}, modelSha256=MODEL_SHA)
            return real(candidate, dict(current, providerManifest=manifest))
        with mock.patch.object(self.dispatch, '_provider_vector', return_value=list(vector)), \
                mock.patch.object(self.dispatch, '_approved_profile_matches', new=approved), \
                self.dispatch.approved_query_vector(authority=endpoint, snapshot=None, decision=decision,
                                                    provider=provider, prompt=prompt,
                                                    deadline=time.monotonic() + 20) as approved_vector:
            yield provider, approved_vector

    def hybrid_recall(self, prompt, vector, *, authority=None, host_root=None):
        with (authority or self.seed.authority()), self.J.read_only_journals():
            with self.lease.resolve_endpoint(self.case.project, action='recall',
                                             host_root=host_root or self.case.plugin) as endpoint:
                proof = self.R._recheck(endpoint)[2]
                decision = dict(proof['readerDecision'], vectorProfile=proof['vectorProfile'])
                with self.query_vector(endpoint, decision, prompt, vector) as (provider, approved):
                    self.assertEqual(approved, list(vector))
                    return self.dispatch.query_endpoint(
                        endpoint, checkout=self.case.project, prompt=prompt, query_vector=approved,
                        deadline=time.monotonic() + 20, snapshot=None, decision=decision,
                        provider=provider, authority=endpoint)

    def card_vector(self, gen_id, path):
        blob = self.members(gen_id)[path][0]
        store = self.views.ViewStore.open_reader(self.store_path, repository=self.repository,
                                                 scope_id=self.case.route['scopeId'])
        try:
            import struct
            rows = store.vectors(blob, self.spec['recipe_id'], self.spec['profile_id'])
            return list(struct.unpack('<fff', rows[0]['embedding']))
        finally:
            store.close()

    # -- checks ---------------------------------------------------------------
    def test_01_pass_publishes_vector_generation_reseals_and_recall_is_hybrid(self):
        self.select_schema3()
        self.assertIsNone(self.selected_record()['vector_profile'])
        self.card('concepts/kestrel-perch.md', 'kestrelprobe')
        start = self.head()
        result, ingests, calls = self.run_vector_pass()
        self.assertEqual((result['state'], result['vector']['state'], result.get('readerResettle')),
                         ('indexed', 'published', 'resealed'), result)
        gen = self.head()
        self.assertEqual(gen, (result['generation'], start[1] + 1))
        stage, refs = self.stage(gen[0])
        eligible = self.eligible(gen[0])
        self.assertEqual((stage['vector_spec'], stage['vector_mode'], refs),
                         (self.spec, 'exact-complete', len(eligible)))
        # One provider call per distinct eligible document; no second embed.
        self.assertEqual(sorted(path for _kind, path in calls),
                         sorted('blobs/' + blob + '.md' for blob in set(eligible.values())))
        record = self.selected_record()
        self.assertEqual((record['vector_profile'], record['vector_recipe']),
                         (self.spec['profile_id'], self.spec['recipe_id']))
        # An idle pass: the head already carries the spec; nothing is asked.
        epoch = self.J.inspect_journal(self.case.journal)['epoch']
        idle, idle_ingests, idle_calls = self.run_vector_pass()
        self.assertEqual((idle['state'], idle['vector']['state'], idle_ingests, idle_calls, self.head()),
                         ('unchanged', 'current', [], [], gen), idle)
        self.assertEqual(self.J.inspect_journal(self.case.journal)['epoch'], epoch)
        # Hybrid recall: a prompt with no lexical match finds the card by vector.
        target = 'concepts/kestrel-perch.md'
        hit = self.hybrid_recall('zzqqxv nomatch', self.card_vector(gen[0], target))
        self.assertEqual((hit.store_format, hit.data_generation, hit.semantic_status),
                         ('schema3', gen[0], 'complete'))
        self.assertEqual(self.paths(hit)[0], target)
        lexical = self.recall('kestrelprobe')
        self.assertIn(target, self.paths(lexical))
        emit('schema3-vector-generation', ingests=len(ingests), providerCalls=len(calls),
             vectorRefs=refs, semantic=hit.semantic_status, topRow=self.paths(hit)[0])

    def test_02_resource_refusal_keeps_lexical_generation_then_vectors_follow(self):
        self.select_schema3()
        self.card('concepts/wren-path.md', 'wrenprobe')
        start = self.head()
        refused, ingests, calls = self.run_vector_pass(Coordinator(refuse='cpu_pressure'))
        self.assertEqual((refused['state'], refused['reason'], refused['vector']),
                         ('deferred', 'cpu_pressure', {'state': 'deferred', 'reason': 'cpu_pressure'}),
                         refused)
        self.assertEqual(calls, [])
        lexical = self.head()
        self.assertEqual(lexical, (refused['generation'], start[1] + 1))
        stage, refs = self.stage(lexical[0])
        self.assertEqual((stage['vector_mode'], refs), ('lexical-only', 0))
        self.assertIsNone(self.selected_record()['vector_profile'])
        self.assertIn('concepts/wren-path.md', self.paths(self.recall('wrenprobe')))
        # The next pass fills the same manifest and seals it as a vector generation.
        filled, again, calls = self.run_vector_pass()
        self.assertEqual((filled['state'], filled['vector']['state'], again, filled.get('readerResettle')),
                         ('indexed', 'published', [], 'resealed'), filled)
        self.assertEqual(self.head(), (filled['generation'], lexical[1] + 1))
        self.assertEqual(self.stage(filled['generation'])[0]['manifest'], stage['manifest'])
        self.assertEqual(len(calls), len(set(self.eligible(filled['generation']).values())))
        # A lexical head under the vector-sealed record still answers (vectors pending).
        self.card('concepts/wren-path.md', 'robinprobe')
        pending, _ingests, _calls = self.run_vector_pass(Coordinator(refuse='memory_reserve'))
        self.assertEqual((pending['state'], pending['vector']['state']), ('deferred', 'deferred'), pending)
        hit = self.hybrid_recall('robinprobe', [1.0, 0.5, 0.25])
        self.assertEqual((hit.data_generation, hit.semantic_status), (pending['generation'], 'lexical_only'))
        self.assertIn('concepts/wren-path.md', self.paths(hit))
        emit('schema3-vector-resource-refusal', lexicalKept=True, refusedProviderCalls=0,
             laterVectorGeneration=True)

    def test_03_second_view_reuses_content_sets_and_refuses_foreign_trust(self):
        self.select_schema3()
        self.card('concepts/heron-watch.md', 'heronwatchprobe')
        main, _ingests, main_calls = self.run_vector_pass()
        self.assertEqual(main['vector']['state'], 'published', main)
        main_members = self.members(main['generation'])
        # A new branch is a new view of the same store with the same content.
        subprocess.run(['git', '-C', str(self.case.project), 'checkout', '-q', '-b', 'vector-side'],
                       check=True, capture_output=True, timeout=10)
        time.sleep(1.1)  # Git racy-index window before the checkout capture.
        side, ingests, side_calls = self.run_vector_pass()
        self.assertEqual((side['state'], side['vector']['state']), ('indexed', 'published'), side)
        # Trust is never reused across views: every card is ingested again ...
        self.assertEqual(sorted(ingests), sorted(main_members))
        side_members = self.members(side['generation'])
        self.assertEqual({p: m[0] for p, m in side_members.items()}, {p: m[0] for p, m in main_members.items()})
        # ... while the shared content sets are: no provider call for the second view.
        self.assertEqual(side_calls, [])
        self.assertEqual(self.stage(side['generation'])[1], self.stage(main['generation'])[1])
        with closing(sqlite3.connect(self.store_path.as_uri() + '?mode=ro', uri=True)) as db:
            trust = {gen: dict(db.execute('SELECT path,trust_digest FROM wiki_members WHERE gen_id=?',
                                          (gen,)).fetchall())
                     for gen in (main['generation'], side['generation'])}
        trusted = [p for p, digest in trust[main['generation']].items() if digest is not None]
        self.assertTrue(trusted)
        self.assertTrue(all(trust[side['generation']][p] != trust[main['generation']][p] for p in trusted))
        # A manifest that borrows the main branch's trust for the side view is refused.
        store = self.views.ViewStore.open(self.store_path, repository=self.repository,
                                          scope_id=self.case.route['scopeId'])
        try:
            handle = store.capture_view(self.worktree['worktreeId'])
            head = store.head(handle)
            with store._session() as db:
                members = store._stored(db, side['generation'])[0]
            borrowed = [dict({k: m[k] for k in ('path', 'blob_sha', 'display_title', 'trust_digest')},
                             trust_digest=trust[main['generation']][m['path']]) for m in members]
            with self.assertRaises(importlib.import_module('memvane.store.blob_values').BlobStoreError) as caught:
                store.prepare_generation(handle, dict(scan_id='9' * 32, policy_digest=handle.policy_digest,
                                                      revocation_epoch=0, complete=True, sources=[],
                                                      members=borrowed), expected_head=head)
            self.assertEqual(caught.exception.reason, 'foreign_card_trust')
        finally:
            store.close()
        emit('schema3-vector-two-views', mainProviderCalls=len(main_calls), sideProviderCalls=len(side_calls),
             sideIngests=len(ingests), foreignTrust='refused')

    def long_card(self, relative, token, lines):
        """A trusted card (as ``card``) whose body adds ``lines`` Korean prose lines."""
        from memvane.wiki.card import yaml_scalars
        raw = ('Synthetic source for ' + token + '.\n').encode('utf8')
        source = self.case.project / 'notes' / (token + '.md')
        source.parent.mkdir(exist_ok=True); source.write_bytes(raw)
        revision = dict(kind='file', path='notes/' + token + '.md', collection=self.raw_collection,
                        sha256=hashlib.sha256(raw).hexdigest(), size=len(raw),
                        mtimeNs=source.stat().st_mtime_ns)
        line = yaml_scalars.dump_source_revisions([revision])[0]
        front = (f'title: "{token} card"\nstatus: verified\ncreatedBy: memvane\n'
                 f'sourceRevisions:\n  - {line}\n')
        prose = ''.join('%03d 가나다라마바사아자차카타파하 거너더러머버서어저처커터퍼허 %s\n' % (n, token)
                        for n in range(lines))
        text = '---\n' + front + '---\n' + f'# {token} card\n{token} verified body\n' + prose
        text = text.replace('createdBy: memvane\n', 'createdBy: memvane\nverifiedBodyHash: '
                            + yaml_scalars.card_body_hash(text) + '\n', 1)
        path = self.wiki / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf8')

    def stored_set(self, path, gen_id=None):
        """(document body, title, chunk rows, embedding rows) of a member's spec sets."""
        gen_id = gen_id or self.head()[0]
        blob = self.members(gen_id)[path][0]
        store = self.views.ViewStore.open_reader(self.store_path, repository=self.repository,
                                                 scope_id=self.case.route['scopeId'])
        try:
            document = store.document(blob)
            return (document['body'], document['title'] or 'none', store.chunks(blob, self.spec['recipe_id']),
                    store.vectors(blob, self.spec['recipe_id'], self.spec['profile_id']))
        finally:
            store.close()

    def test_04_resplit_card_falls_back_to_small_windows_and_publishes_vectors(self):
        import struct
        self.select_schema3()
        self.split.add('splitprobe')
        target = 'concepts/split-card.md'
        self.long_card(target, 'splitprobe', 40)
        result, _ingests, calls = self.run_vector_pass()
        self.assertEqual((result['state'], result['vector']['state']), ('indexed', 'published'), result)
        gen = result['generation']
        self.assertEqual(self.stage(gen)[0]['vector_mode'], 'exact-complete')
        body, title, chunks, rows = self.stored_set(target, gen)
        fallback = self.VV.qmd_first_pass(body, 600, 90, 135)
        self.assertGreater(len(fallback), 2)
        self.assertNotEqual([p for p, _ in fallback], [p for p, _ in self.VV.qmd_first_pass(body)])
        # The re-split card's chunk set is the fallback windows, each embedded alone.
        self.assertEqual([(c['position'], c['text']) for c in chunks], fallback)
        self.assertEqual([list(struct.unpack('<fff', r['embedding'])) for r in rows],
                         [list(struct.unpack('<fff', struct.pack('<fff', *text_vector(
                             'title: ' + title + ' | text: ' + text)))) for _p, text in fallback])
        # Only the re-split card asks for single embeddings; position-equal cards
        # keep the one chunked call's vectors (no added provider call).
        documents = [t for kind, t in calls if kind == 'document']
        self.assertEqual(documents, [title] * len(fallback))
        self.assertEqual(sorted(p for kind, p in calls if kind == 'chunks'),
                         sorted('blobs/' + blob + '.md' for blob in set(self.eligible(gen).values())))
        # The card is a vector search result: a prompt without a lexical match.
        piece = text_vector('title: ' + title + ' | text: ' + fallback[1][1])
        hit = self.hybrid_recall('zzqqxv nomatch', piece)
        self.assertEqual((hit.data_generation, hit.semantic_status), (gen, 'complete'))
        self.assertEqual(self.paths(hit)[0], target)
        self.assertIn(target, self.paths(self.recall('splitprobe')))
        emit('schema3-vector-resplit-fallback', fallbackChunks=len(fallback), documentCalls=len(documents),
             chunkCalls=len(calls) - len(documents), topRow=self.paths(hit)[0])

    def test_05_raw_sources_are_observed_in_each_generation(self):
        self.select_schema3()
        self.card('concepts/plover-perch.md', 'ploverprobe')
        first, _ingests, _calls = self.run_vector_pass()
        self.assertEqual(first['state'], 'indexed', first)
        head_oid = subprocess.run(['git', '-C', str(self.case.project), 'rev-parse', 'HEAD'],
                                  check=True, capture_output=True, text=True, timeout=10).stdout.strip()

        def sources(gen_id):
            with closing(sqlite3.connect(self.store_path.as_uri() + '?mode=ro', uri=True)) as db:
                return {path: (sha, length, head, state, presence) for path, sha, length, head, state, presence
                        in db.execute('SELECT s.path,f.source_sha,f.byte_length,f.head_oid,f.binding_state,'
                                      's.presence FROM source_members s JOIN source_fingerprints f '
                                      'ON f.fingerprint_id=s.fingerprint_id WHERE s.gen_id=?', (gen_id,))}
        observed = sources(first['generation'])
        note = 'notes/ploverprobe.md'  # the card's raw source (self.card)
        self.assertIn(note, observed)
        raw = (self.case.project / note).read_bytes()
        self.assertEqual(observed[note], (hashlib.sha256(raw).hexdigest(), len(raw), head_oid,
                                          'observed', 'present'))
        self.assertEqual(first['sources'], len(observed))
        idle, idle_ingests, _calls = self.run_vector_pass()
        self.assertEqual((idle['state'], idle_ingests), ('unchanged', []), idle)
        # A raw edit alone republishes the generation with the new observation.
        changed = self.raw_file('plover-extra.md', 'A raw note the next generation observes.\n')
        edited, ingests, _calls = self.run_vector_pass()
        self.assertEqual((edited['state'], ingests), ('indexed', []), edited)
        after = sources(edited['generation'])
        self.assertIn(changed.relative_to(self.case.project).as_posix(), after)
        self.assertEqual(self.eligible(edited['generation']), self.eligible(first['generation']))
        emit('schema3-raw-sources', sources=len(observed), afterEdit=len(after))

    def test_06_dedup_scan_and_compile_retrieval_use_view_vectors(self):
        self.select_schema3()
        # Two cards with the same bytes: one blob, one embedding set, cosine 1.
        self.card('concepts/egret-one.md', 'egretprobe')
        (self.wiki / 'concepts/egret-two.md').write_bytes((self.wiki / 'concepts/egret-one.md').read_bytes())
        result, _ingests, _calls = self.run_vector_pass()
        self.assertEqual(result['vector']['state'], 'published', result)
        scan = importlib.import_module('memvane.wiki.dedup.wiki_dedup_scan')
        learning = importlib.import_module('context_learning.learning')
        config = importlib.import_module('memvane.settings.config')
        queue = self.case.auto / 'compile' / 'dedup-needed.jsonl'
        with self.seed.authority(), \
                mock.patch.object(config, 'compile_active', return_value=True), \
                mock.patch.object(learning, '_open', side_effect=AssertionError('v6 database read')):
            scanned = scan.run(str(self.case.project))
        self.assertEqual(scanned['state'], 'scanned', scanned)
        self.assertGreaterEqual(scanned['queued'], 1, scanned)
        pairs = [json.loads(line) for line in queue.read_text(encoding='utf8').splitlines()]
        self.assertIn(('concepts/egret-one.md', 'concepts/egret-two.md'),
                      [tuple(sorted((row['pageA'], row['pageB']))) for row in pairs])
        # Compile retrieval: a vector-only candidate carries its exact cosine.
        retrieval = importlib.import_module('memvane.wiki.compile.wiki_compile_retrieval')
        target = 'concepts/egret-one.md'
        vector = self.card_vector(result['generation'], target)
        captured = []
        original = retrieval.selected_search

        @contextmanager
        def spy(bound, text):
            with original(bound, text) as found:
                captured.append(found)
                yield found
        real = self.dispatch._approved_profile_matches

        def approved(candidate, current):
            manifest = dict(current.get('providerManifest') or {}, modelSha256=MODEL_SHA)
            return real(candidate, dict(current, providerManifest=manifest))
        selected = importlib.import_module('memvane.wiki.workspace.wiki_selected_laya')
        with self.seed.authority(), mock.patch.object(retrieval, 'selected_search', new=spy), \
                mock.patch.object(self.dispatch, '_approved_profile_matches', new=approved), \
                mock.patch.object(retrieval, 'query_provider',
                                  return_value=lambda text, profile: {'vector': vector}):
            selected.wiki_matches(self.case.project, self.wiki, 'zzqqxv nomatch', 5, 2000)
        published = captured[0][0]
        blob = self.members(result['generation'])[target][0]
        found = {(rel, sha): cosine for rel, sha, cosine in published}
        self.assertIn((target, blob), found)
        self.assertAlmostEqual(found[(target, blob)], 1.0, places=5)
        emit('schema3-vector-consumers', dedupQueued=scanned['queued'], compileCosine=found[(target, blob)])

    def test_07_main_and_linked_views_embed_shared_content_once(self):
        import os
        import shutil
        import tempfile
        self.select_schema3()
        self.card('concepts/kestrel-perch.md', 'kestrelprobe')
        project = self.case.project
        base = Path(tempfile.mkdtemp(prefix='linked-', dir=str(project.parent))).resolve()
        self.addCleanup(shutil.rmtree, base, True)
        linked = base / 'linked'
        subprocess.run(['git', '-C', str(project), '-c', 'core.hooksPath=' + os.devnull, 'worktree', 'add',
                        '-q', '-b', 'side', str(linked)], check=True, capture_output=True, timeout=20)
        settings = importlib.import_module('memvane.settings.project_dir').settings_path(project)
        relative = settings.relative_to(project)
        (linked / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(settings, linked / relative)
        for item in self.decision['collections']:
            (linked / Path(item['sourceRoot']).relative_to(project)).mkdir(parents=True, exist_ok=True)
        # A same-HEAD dirty worktree: the main checkout's cards as its own untracked files.
        shutil.copytree(self.wiki, linked / self.wiki.relative_to(project), dirs_exist_ok=True)
        time.sleep(1.1)  # Git racy-index window before the linked checkout's capture.
        result, _ingests, calls = self.run_vector_pass()
        self.assertEqual((result['state'], result['vector']['state']), ('indexed', 'published'), result)
        self.assertEqual(len(result['worktrees']), 1, result)
        other = result['worktrees'][0]
        self.assertEqual((other['state'], other['vector']['state']), ('indexed', 'published'), result)
        main_blobs = set(self.eligible(result['generation']).values())
        linked_blobs = set(self.eligible(other['generation']).values())
        self.assertTrue(linked_blobs)
        self.assertLessEqual(linked_blobs, main_blobs)
        # One provider call per distinct document across both views.
        self.assertEqual(sorted(path for _kind, path in calls),
                         sorted('blobs/' + blob + '.md' for blob in main_blobs))
        self.assertEqual(self.stage(other['generation'])[0]['vector_spec'], self.spec)
        emit('schema3-vector-main-linked', providerCalls=len(calls), mainBlobs=len(main_blobs),
             linkedBlobs=len(linked_blobs))

    def test_08_one_raw_scan_serves_sources_and_discovery(self):
        state_path, _grants, _lock = self.D._paths(self.decision)
        self.D._write(state_path, self.D._state_doc(self.decision, 'v6-pre-switch-head', {}, {}))
        self.select_schema3()
        self.raw_file('scanned-once.md', 'A raw note the pass scans once.\n')
        grant = self.D.record_grant(self.decision, 'sync', engine='codex')
        self.assertTrue(self.D.activate_grant(self.decision, 'sync', grant))
        port = importlib.import_module('memvane.sync.source_scan')
        real, scans = port.scan_collections, []

        def counted(*args, **kwargs):
            scans.append(sorted(args[0]))
            return real(*args, **kwargs)
        with mock.patch.object(port, 'scan_collections', new=counted):
            result, _ingests, _calls = self.run_vector_pass()
        self.assertIn(result['state'], ('indexed', 'unchanged', 'pending'), result)
        self.assertNotEqual(result.get('wikiDiscovery', {}).get('state'), 'blocked', result)
        state = self.D._read(state_path)
        self.assertEqual(state['baseGen'], self.head()[0])
        self.assertIn(self.raw_collection + '/notes/scanned-once.md', state['rawSnapshot'])
        self.assertEqual(len(scans), 1, scans)
        emit('schema3-raw-scan-once', scans=len(scans))

    def test_09_provider_defect_ends_the_wake_blocked_lexical_kept(self):
        self.select_schema3()
        self.card('concepts/finch-perch.md', 'finchprobe')
        journal = importlib.import_module('memvane.journal.install_journal')
        start = self.head()

        def invalid(_decision, _proof):
            raise journal.JournalEvidenceError('worker_attestation_invalid')
        with mock.patch.object(self.worker, '_schema3_worker_manifest', new=invalid):
            result, _ingests, calls = self.run_vector_pass()
        self.assertEqual((result['state'], result['reason'], result['vector']['state']),
                         ('blocked', 'vector_provider_worker_attestation_invalid', 'provider_blocked'), result)
        self.assertEqual(calls, [])
        self.assertEqual(self.head(), (result['generation'], start[1] + 1))
        self.assertEqual(self.stage(result['generation'])[0]['vector_mode'], 'lexical-only')
        self.assertIn('concepts/finch-perch.md', self.paths(self.recall('finchprobe')))
        # A refused resource stays a retried deferral, not blocked.
        self.card('concepts/finch-perch.md', 'sparrowprobe')
        deferred, _ingests, _calls = self.run_vector_pass(Coordinator(refuse='cpu_pressure'))
        self.assertEqual((deferred['state'], deferred['reason']), ('deferred', 'cpu_pressure'), deferred)
        emit('schema3-vector-provider-defect', state=result['state'], reason=result['reason'])


    def test_10_card_over_the_fill_deadline_completes_on_a_later_pass(self):
        """A card whose single embeddings alone outlast one fill call: counts, not wall time.

        ``laya_blob_fill``'s deadline reads a virtual clock that only the stub's
        single-embedding calls advance (4 virtual seconds each).
        """
        fill = importlib.import_module('memvane.store.blob_fill')
        values = importlib.import_module('memvane.store.blob_values')
        clock, budgets = [0.0], []
        real_deadline = fill._deadline

        def deadline(seconds):
            real_deadline(seconds)  # same validation
            budgets.append(seconds)
            return (clock[0], seconds)

        def expire(token):
            if clock[0] - token[0] >= token[1]:
                values.fail('content_fill_deadline')
        real_document = self.worker.ReviewedLocalQmdProvider.embed_document

        def slow_document(provider, body, title):
            clock[0] += 4.0
            return real_document(provider, body, title)
        self.select_schema3()
        self.split.add('slowprobe')
        target = 'concepts/slow-card.md'
        self.long_card(target, 'slowprobe', 130)
        with mock.patch.object(fill, '_deadline', new=deadline), mock.patch.object(fill, '_expire', new=expire), \
                mock.patch.object(self.worker.ReviewedLocalQmdProvider, 'embed_document', new=slow_document):
            first, _ingests, first_calls = self.run_vector_pass()
            # Lexical publish kept; the vectors wait. Other cards' sets were
            # filled, so the pass is ACKed and its wake offered again.
            self.assertEqual((first['state'], first['vector']['state'], first['vector']['reason']),
                             ('indexed', 'pending', 'schema3_vector_deadline'), first)
            self.assertGreater(first['vector']['progress'], 0, first)
            self.assertEqual(self.stage(first['generation'])[0]['vector_mode'], 'lexical-only')
            self.assertIn(target, self.paths(self.recall('slowprobe')))
            second, _ingests, second_calls = self.run_vector_pass()
        self.assertEqual((second['state'], second['vector']['state']), ('indexed', 'published'), second)
        body, title, chunks, rows = self.stored_set(target, second['generation'])
        pieces = len(self.VV.qmd_first_pass(body, 600, 90, 135))
        self.assertGreater(pieces * 4.0, self.worker.SCHEMA3_FILL_CALL_SECONDS)
        self.assertEqual((len(chunks), len(rows)), (pieces, pieces))

        def kinds(calls, kind, value=None):
            return sum(1 for k, v in calls if k == kind and (value is None or v == value))
        # The stored chunk set is the progress kept: the second pass asks for no
        # chunked call and embeds the card's windows once more with the resume budget.
        self.assertEqual((kinds(first_calls, 'document', title), kinds(second_calls, 'document', title)),
                         (pieces, pieces))
        slow = 'blobs/' + self.members(second['generation'])[target][0] + '.md'
        self.assertEqual((kinds(first_calls, 'chunks', slow), kinds(second_calls, 'chunks', slow)), (1, 0))
        # Every distinct document is chunked once over both passes.
        self.assertEqual(kinds(first_calls, 'chunks') + kinds(second_calls, 'chunks'),
                         len(set(self.eligible(second['generation']).values())))
        # Budgets are what each content fill had left: the first attempts stay
        # within one fill call, the resumed call carries the resume budget.
        resumed = [b for b in budgets if b > self.worker.SCHEMA3_FILL_CALL_SECONDS]
        self.assertTrue(resumed and all(b <= self.worker.SCHEMA3_RESUME_SECONDS for b in resumed), budgets)
        emit('schema3-vector-deadline-resume', pieces=pieces, firstDocuments=kinds(first_calls, 'document'),
             secondDocuments=kinds(second_calls, 'document'), secondChunks=kinds(second_calls, 'chunks'))

    # -- 0.44.21: fill budget, re-offer, degraded recall, diagnostics -------------
    @contextmanager
    def virtual_fill_clock(self, slow):
        """``laya_blob_fill`` deadlines on a virtual clock; ``slow[marker]`` virtual
        seconds pass inside the provider's chunked call for a body holding marker.
        Yields the list of content-fill budgets."""
        fill = importlib.import_module('memvane.store.blob_fill')
        values = importlib.import_module('memvane.store.blob_values')
        clock, budgets = [0.0], []
        real_deadline = fill._deadline

        def deadline(seconds):
            real_deadline(seconds)
            budgets.append(seconds)
            return (clock[0], seconds)

        def expire(token):
            if clock[0] - token[0] >= token[1]:
                values.fail('content_fill_deadline')
        real_chunks = self.worker.ReviewedLocalQmdProvider.embed_document_chunks

        def chunks(provider, body, path, timeout=None):
            clock[0] += sum(seconds for marker, seconds in slow.items() if marker in body)
            return real_chunks(provider, body, path, timeout)
        with mock.patch.object(fill, '_deadline', new=deadline), mock.patch.object(fill, '_expire', new=expire), \
                mock.patch.object(self.worker.ReviewedLocalQmdProvider, 'embed_document_chunks', new=chunks):
            yield budgets

    @contextmanager
    def offers(self):
        """``runtime_work.request`` calls as (kind, target, delay)."""
        work = importlib.import_module('memvane.runtime.runtime_work')
        seen = []

        def request(kind, target='', **kwargs):
            seen.append((kind, target, kwargs.get('delay', 0)))
            return {'status': 'queued'}
        with mock.patch.object(work, 'request', side_effect=request):
            yield seen

    def embedded(self, gen_id, path):
        """Whether ``path``'s blob has the spec's chunk and embedding sets."""
        blob = self.members(gen_id)[path][0]
        with closing(sqlite3.connect(self.store_path.as_uri() + '?mode=ro', uri=True)) as db:
            chunked = db.execute('SELECT 1 FROM chunk_sets WHERE blob_sha=? AND recipe_id=?',
                                 (blob, self.spec['recipe_id'])).fetchone() is not None
            embedded = db.execute('SELECT 1 FROM embedding_sets WHERE blob_sha=? AND recipe_id=? AND '
                                  'profile_id=?', (blob, self.spec['recipe_id'],
                                                   self.spec['profile_id'])).fetchone() is not None
        return chunked, embedded

    def test_12_cold_card_over_one_fill_call_completes_with_the_resume_budget(self):
        """A card whose single chunked provider call outlasts a fill call and has
        no stored chunk set: the first cold target of a pass gets the resume budget."""
        self.select_schema3()
        target = 'concepts/cold-card.md'
        self.card(target, 'coldprobe')
        self.assertGreater(40.0, self.worker.SCHEMA3_FILL_CALL_SECONDS)
        self.assertLess(40.0, self.worker.SCHEMA3_RESUME_SECONDS)
        with self.virtual_fill_clock({'coldprobe': 40.0}) as budgets:
            first, _ingests, _calls = self.run_vector_pass()
            second, _ingests, _calls = self.run_vector_pass()
        # Published within two passes (in the first when the cold card is the
        # pass's first cold target); before, both passes dropped its call.
        self.assertIn('published', (first['vector']['state'], second['vector']['state']), (first, second))
        self.assertEqual(self.embedded(self.head()[0], target), (True, True))
        self.assertTrue([b for b in budgets if b > self.worker.SCHEMA3_FILL_CALL_SECONDS], budgets)
        emit('schema3-cold-card-resume-budget', first=first['vector'].get('reason'),
             second=second['vector']['state'])

    def test_13_cache_hit_run_is_one_fill_call(self):
        """A new fill job whose first missing target sits behind a run of cache
        hits commits that run in one ``fill_view`` call, not eight at a time."""
        self.select_schema3()
        for index in range(18):
            self.card('concepts/hit-%02d.md' % index, 'hitprobe%02d' % index)
        first, _ingests, _calls = self.run_vector_pass()
        self.assertEqual(first['vector']['state'], 'published', first)
        self.card('zzz/late.md', 'lateprobe')
        calls = []
        original = self.views.ViewStore.fill_view

        def counted(store, *args, **kwargs):
            calls.append(kwargs.get('max_files'))
            return original(store, *args, **kwargs)
        with mock.patch.object(self.views.ViewStore, 'fill_view', new=counted):
            second, _ingests, _calls = self.run_vector_pass()
        self.assertEqual(second['vector']['state'], 'published', second)
        hits = len(set(self.eligible(second['generation']).values())) - 1
        self.assertGreaterEqual(hits, 17)
        self.assertLessEqual(len(calls), 2, calls)
        emit('schema3-cache-hit-run', hits=hits, fillCalls=len(calls))

    def test_14_progressing_vector_pass_acks_and_reoffers_its_wake(self):
        """Progress with vectors left: the pass is ACKed (not a counted failure)
        and the same laya-sync wake is offered again in 30 s, with the vector
        diagnostics in its detail. A pass with no store progress offers nothing."""
        self.select_schema3()
        target = 'concepts/slow-card.md'
        self.card(target, 'slowprobe')
        project = str(self.case.project)
        with self.virtual_fill_clock({'slowprobe': 300.0}), self.offers() as seen:
            first, _ingests, _calls = self.run_vector_pass()
            self.assertEqual(first['vector']['state'], 'pending', first)
            self.assertIn(first['state'], ('indexed', 'unchanged'), first)
            self.assertIn(('laya-sync', project, self.worker.SCHEMA3_VECTOR_REOFFER_SECONDS), seen)
            self.assertEqual(self.worker.SCHEMA3_VECTOR_REOFFER_SECONDS, 30)
            detail = first.get('detail') or ''
            self.assertRegex(detail, r'^vec_pending_[a-z_]+:rem[0-9]+:ord[0-9]+:pms[0-9]+')
            self.assertLessEqual(len(detail), 80)
            seen[:] = []
            # Only the slow card is left; even the resume budget does not hold it.
            second, _ingests, _calls = self.run_vector_pass()
        self.assertEqual((second['state'], second['vector']['state']), ('pending', 'pending'), second)
        self.assertNotIn(('laya-sync', project, self.worker.SCHEMA3_VECTOR_REOFFER_SECONDS), seen)
        self.assertEqual(self.embedded(self.head()[0], target), (False, False))
        emit('schema3-vector-reoffer', first=first['state'], second=second['state'], detail=detail)

    def test_15_budget_spent_pass_still_tries_one_cold_target_and_linked_budget_acks(self):
        """A pass whose earlier work left no fill budget still fills the first cold
        target; with main vectors progressing, a linked view held only by the pass
        budget does not turn the result into a counted failure."""
        import os
        import shutil
        import tempfile
        self.select_schema3()
        self.card('concepts/kestrel-perch.md', 'kestrelprobe')
        project = self.case.project
        base = Path(tempfile.mkdtemp(prefix='linked-', dir=str(project.parent))).resolve()
        self.addCleanup(shutil.rmtree, base, True)
        linked = base / 'linked'
        subprocess.run(['git', '-C', str(project), '-c', 'core.hooksPath=' + os.devnull, 'worktree', 'add',
                        '-q', '-b', 'side', str(linked)], check=True, capture_output=True, timeout=20)
        settings = importlib.import_module('memvane.settings.project_dir').settings_path(project)
        relative = settings.relative_to(project)
        (linked / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(settings, linked / relative)
        for item in self.decision['collections']:
            (linked / Path(item['sourceRoot']).relative_to(project)).mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.wiki, linked / self.wiki.relative_to(project), dirs_exist_ok=True)
        time.sleep(1.1)  # Git racy-index window before the linked checkout's capture.
        # Lexical generations of both views first; the provider lane refuses.
        lexical, _ingests, _calls = self.run_vector_pass(Coordinator(refuse='cpu_pressure'))
        self.assertEqual(len(lexical.get('worktrees', ())), 1, lexical)
        reserve = self.worker.SCHEMA3_PUBLISH_RESERVE
        # The unchanged pass's vector window is closed here: the A1 path under test.
        with mock.patch.object(self.worker, 'SCHEMA3_PASS_SECONDS', reserve + 0.001), \
                mock.patch.object(self.worker, 'SCHEMA3_VECTOR_SECONDS', reserve + 0.001), \
                self.offers() as seen:
            result, _ingests, calls = self.run_vector_pass()
        self.assertEqual(result['vector']['state'], 'pending', result)
        self.assertGreaterEqual(len(calls), 1)  # the first cold target was filled
        self.assertEqual([(w['state'], w.get('reason')) for w in result['worktrees']],
                         [('pending', 'schema3_pass_budget')], result)
        self.assertIn(result['state'], ('indexed', 'unchanged'), result)
        self.assertIn(('laya-sync', str(project), self.worker.SCHEMA3_VECTOR_REOFFER_SECONDS), seen)
        emit('schema3-budget-spent-first-cold', state=result['state'], providerCalls=len(calls))

    def test_16_drift_schema3_recall_asks_no_query_vector_but_a_proved_degraded_read_does(self):
        """A bundle-drift read of a schema3 endpoint sealed with a vector spec
        (its proof is read-only: ``productRouteAllowed`` is not true) serves its
        lexical lists without asking for a query vector; a degraded read whose
        bundle proof holds (a linked worktree's) still asks for it."""
        self.select_schema3()
        self.card('concepts/kestrel-perch.md', 'kestrelprobe')
        result, _ingests, _calls = self.run_vector_pass()
        self.assertEqual(result.get('readerResettle'), 'resealed', result)
        host = importlib.import_module('memvane.host.host_response')
        event_log = importlib.import_module('event_log')
        asked, events = [], []

        drift = [True]

        @contextmanager
        def unproved(**kwargs):
            asked.append(kwargs.get('prompt'))
            if drift:
                raise ValueError('reader_vector_caller_unproved')
            yield None  # proved, but no warm provider answered
        real_recheck = self.R._recheck

        def recheck(endpoint):
            record, raw, proof = real_recheck(endpoint)
            if drift:
                # The read-only proof a drifted executing bundle gets.
                proof = dict(proof, productRouteAllowed=False, readOnlyAllowed=True)
            return record, raw, proof

        def recorded(event, **fields):
            events.append(dict(fields, event=event))
            return True
        decision = self.J.select_route(str(self.case.project), action='recall')
        payload = {'cwd': str(self.case.project), 'prompt': 'kestrelprobe', 'session_id': 'drift-probe'}
        provider_module = importlib.import_module('memvane.provider.adapters.local_embed_provider')
        resident = importlib.import_module('memvane.resident.embed_resident')
        def degraded_recall():
            with self.seed.authority(), \
                    mock.patch.object(host, '__file__', str(self.case.plugin / 'core' / 'laya_host_response.py')), \
                    mock.patch.object(provider_module, 'ReviewedLocalQmdProvider', new=self.stub_provider()), \
                    mock.patch.object(resident, 'admit_query', return_value=None), \
                    mock.patch.object(self.dispatch, 'approved_query_vector', new=unproved), \
                    mock.patch.object(self.R, '_recheck', new=recheck), \
                    mock.patch.object(event_log, 'emit', side_effect=recorded), \
                    host._event_scope('recall', 'claude', decision, payload):
                return host.serve_endpoint_recall('claude', payload, cwd=str(self.case.project),
                                                  drift_decision=decision, main_proof=None)
        answer = degraded_recall()
        self.assertEqual(asked, [])
        self.assertIn('kestrel', answer['hookSpecificOutput']['additionalContext'])
        recall = [e for e in events if e['event'] == 'recall']
        self.assertEqual(len(recall), 1, events)
        self.assertEqual(recall[0].get('degraded'), 'reader_endpoint', recall[0])
        self.assertIn('laya_vector_selected_reader_endpoint_degraded_read', recall[0].get('notes') or [])
        # A degraded read whose bundle proof holds keeps asking for it.
        drift.clear()
        events[:] = []
        degraded_recall()
        self.assertGreaterEqual(len(asked), 1)
        recall = [e for e in events if e['event'] == 'recall']
        self.assertNotIn('laya_vector_selected_reader_endpoint_degraded_read', recall[0].get('notes') or [])
        emit('schema3-degraded-recall-lexical', asked=len(asked), notes=recall[0].get('notes'))

    def test_17_empty_recall_reason_is_no_candidates(self):
        """A recall with no candidate rows names that, not its last diagnostic note."""
        self.select_schema3()
        self.card('concepts/kestrel-perch.md', 'kestrelprobe')
        # Lexical only (the provider lane refuses): recall notes a vector fallback.
        self.run_vector_pass(Coordinator(refuse='cpu_pressure'))
        host = importlib.import_module('memvane.host.host_response')
        event_log = importlib.import_module('event_log')
        events = []

        def recorded(event, **fields):
            events.append(dict(fields, event=event))
            return True
        decision = self.J.select_route(str(self.case.project), action='recall')
        payload = {'cwd': str(self.case.project), 'prompt': 'zzqqxv nomatch', 'session_id': 'empty-probe',
                   'hook_event_name': 'UserPromptSubmit'}
        with self.seed.authority(), \
                mock.patch.object(host, '__file__', str(self.case.plugin / 'core' / 'laya_host_response.py')), \
                mock.patch.object(event_log, 'emit', side_effect=recorded):
            host.serve_selected('recall', 'claude', decision, payload)
        recall = [e for e in events if e['event'] == 'recall']
        self.assertEqual(len(recall), 1, events)
        self.assertEqual((recall[0]['outcome'], recall[0]['reason']), ('empty', 'no_candidates'), recall[0])
        # The vector fallback stays a diagnostic note, not the reason.
        self.assertTrue([n for n in recall[0].get('notes') or [] if n.startswith('laya_vector_')], recall[0])
        emit('schema3-empty-recall-reason', reason=recall[0]['reason'], notes=recall[0].get('notes'))

    def test_18_completing_pass_publishes_its_vector_generation_after_an_overflow(self):
        """A fill that completes past the pass deadline still publishes the vector
        generation in the same pass: the publish gets a fresh reserve."""
        self.select_schema3()
        self.card('zzz/last-card.md', 'lastprobe')
        clock = [0.0]
        real = self.worker.time
        fake = mock.Mock(wraps=real)
        fake.monotonic = lambda: real.monotonic() + clock[0]
        real_chunks = self.worker.ReviewedLocalQmdProvider.embed_document_chunks

        def chunks(provider, body, path, timeout=None):
            if 'lastprobe' in body:
                clock[0] += 120.0  # the worker's clock passes the pass deadline
            return real_chunks(provider, body, path, timeout)
        deadlines = []
        original = self.views.ViewStore.prepare_generation

        def prepare(store, *args, **kwargs):
            deadlines.append(kwargs.get('deadline') - fake.monotonic())
            return original(store, *args, **kwargs)
        with mock.patch.object(self.worker, 'time', fake), \
                mock.patch.object(self.worker.ReviewedLocalQmdProvider, 'embed_document_chunks', new=chunks), \
                mock.patch.object(self.views.ViewStore, 'prepare_generation', new=prepare):
            result, _ingests, _calls = self.run_vector_pass()
        self.assertEqual((result['vector']['state'], result.get('readerResettle')),
                         ('published', 'resealed'), result)
        self.assertGreaterEqual(deadlines[-1], self.worker.SCHEMA3_PUBLISH_RESERVE - 1, deadlines)
        self.assertRegex(result.get('detail') or '', r'pub[0-9]+.*res_resealed')
        emit('schema3-overflow-publish', margins=[round(d, 1) for d in deadlines])

    @contextmanager
    def worker_clock(self, jumps):
        """The sync worker's own clock; ``jumps[marker]`` seconds pass inside the
        provider's chunked call for a body holding marker (store clocks stay real).
        Yields the prepare_generation deadline margins on that clock."""
        clock = [0.0]
        real = self.worker.time
        fake = mock.Mock(wraps=real)
        fake.monotonic = lambda: real.monotonic() + clock[0]
        real_chunks = self.worker.ReviewedLocalQmdProvider.embed_document_chunks

        def chunks(provider, body, path, timeout=None):
            clock[0] += sum(seconds for marker, seconds in jumps.items() if marker in body)
            return real_chunks(provider, body, path, timeout)
        margins = []
        original = self.views.ViewStore.prepare_generation

        def prepare(store, *args, **kwargs):
            margins.append(kwargs.get('deadline') - fake.monotonic())
            return original(store, *args, **kwargs)
        with mock.patch.object(self.worker, 'time', fake), \
                mock.patch.object(self.worker.ReviewedLocalQmdProvider, 'embed_document_chunks', new=chunks), \
                mock.patch.object(self.views.ViewStore, 'prepare_generation', new=prepare):
            yield margins

    def test_19_unfinished_fill_past_the_deadline_still_publishes_lexical_and_reoffers(self):
        """The first cold target's call runs past the pass deadline and targets
        remain: this pass's lexical generation gets a fresh reserve and the
        progressing wake is offered again."""
        self.select_schema3()
        self.card('aaa/first-cold.md', 'firstcoldprobe')
        self.card('zzz/later.md', 'laterprobe')
        before = self.head()
        with self.worker_clock({'firstcoldprobe': 120.0}) as margins, self.offers() as seen:
            result, _ingests, _calls = self.run_vector_pass()
        # This pass's lexical generation is the published head.
        self.assertEqual(self.head(), (result['generation'], before[1] + 1), result)
        self.assertIn('aaa/first-cold.md', self.members(result['generation']))
        self.assertEqual(result['vector']['state'], 'pending', result)
        self.assertGreater(result['vector']['progress'], 0, result)
        self.assertGreaterEqual(margins[-1], self.worker.SCHEMA3_PUBLISH_RESERVE - 1, margins)
        self.assertIn(result['state'], ('indexed', 'unchanged'), result)
        self.assertIn(('laya-sync', str(self.case.project), self.worker.SCHEMA3_VECTOR_REOFFER_SECONDS), seen)
        emit('schema3-unfinished-overflow-lexical', margins=[round(m, 1) for m in margins])

    def test_20_overflowed_main_publish_reoffers_for_linked_views(self):
        """A main view whose fill completes past the pass deadline publishes its
        vector generation; a linked view the pass budget held is reported and
        its wake offered again, not dropped with an ACK."""
        import os
        import shutil
        import tempfile
        self.select_schema3()
        self.card('concepts/kestrel-perch.md', 'kestrelprobe')
        project = self.case.project
        base = Path(tempfile.mkdtemp(prefix='linked-', dir=str(project.parent))).resolve()
        self.addCleanup(shutil.rmtree, base, True)
        linked = base / 'linked'
        subprocess.run(['git', '-C', str(project), '-c', 'core.hooksPath=' + os.devnull, 'worktree', 'add',
                        '-q', '-b', 'side', str(linked)], check=True, capture_output=True, timeout=20)
        settings = importlib.import_module('memvane.settings.project_dir').settings_path(project)
        relative = settings.relative_to(project)
        (linked / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(settings, linked / relative)
        for item in self.decision['collections']:
            (linked / Path(item['sourceRoot']).relative_to(project)).mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.wiki, linked / self.wiki.relative_to(project), dirs_exist_ok=True)
        time.sleep(1.1)  # Git racy-index window before the linked checkout's capture.
        lexical, _ingests, _calls = self.run_vector_pass(Coordinator(refuse='cpu_pressure'))
        self.assertEqual(len(lexical.get('worktrees', ())), 1, lexical)
        self.card('zzz/last-card.md', 'lastprobe')
        with self.worker_clock({'lastprobe': 120.0}), self.offers() as seen:
            result, _ingests, _calls = self.run_vector_pass()
        self.assertEqual(result['vector']['state'], 'published', result)
        self.assertEqual([(w['state'], w.get('reason')) for w in result['worktrees']],
                         [('pending', 'schema3_pass_budget')], result)
        self.assertIn(result['state'], ('indexed', 'unchanged'), result)
        self.assertIn(('laya-sync', str(project), self.worker.SCHEMA3_VECTOR_REOFFER_SECONDS), seen)
        emit('schema3-overflow-linked-reoffer', state=result['state'])




    @contextmanager
    def wake_queue(self):
        """The real wake queue in this temp HOME, on a virtual wall clock.

        Yields ``(queue, clock, drain)``: ``drain(execute)`` runs one worker
        drain. The real ``runtime_work.request`` is restored (its fixture guard
        is cleared, and QMD_RUNTIME_DISPATCH keeps it from starting a worker).
        """
        import os
        import tempfile
        from types import SimpleNamespace
        work = importlib.import_module('memvane.runtime.runtime_work')
        root = Path(tempfile.mkdtemp(prefix='wakes-', dir=os.environ.get('HOME'))).resolve()
        clock = {'now': time.time()}
        fake = SimpleNamespace(time=lambda: clock['now'], monotonic=time.monotonic, sleep=lambda _s: None)

        def drain(execute, runs=1):
            return work.drain(queue, execute=execute, ready=lambda: True,
                              wall=lambda: clock['now'], sleep=lambda _s: None, max_runs=runs)
        with mock.patch.dict(os.environ, {'QMD_SANDBOX': '', 'QMD_SETUP_GUARD_FIXTURE': '',
                                          'QMD_RUNTIME_DISPATCH': '1'}), \
                mock.patch.object(work, 'default_root', new=lambda: root), \
                mock.patch.object(work, 'request', new=REAL_WAKE_REQUEST), \
                mock.patch.object(work, 'time', fake):
            queue = work.WakeQueue()
            yield queue, clock, drain

    def sync_execute(self, project, passes):
        """A laya-sync dispatch stand-in: one real pass; non-done results raise
        the dispatcher's ``target_deferred`` (laya_runtime_dispatch)."""
        policy = importlib.import_module('memvane.runtime.runtime_wake_policy')

        def execute(row):
            self.assertEqual((row['kind'], row['target']), ('laya-sync', project))
            result, _ingests, calls = self.run_vector_pass()
            passes.append((result, calls))
            if result['state'] in ('indexed', 'unchanged'):
                return True
            raise policy.DispatchFailure('target_deferred')
        return execute

    def test_21_zero_progress_vector_pass_keeps_its_wake_unacked_without_a_delayed_row(self):
        """Through the real wake queue: a progressing pass ACKs its laya-sync wake
        and leaves one delayed re-offer 30 s out; a following pass with vectors
        left and no store progress is not ACKed (the same row and revision stay,
        with a counted target_deferred attempt) and leaves no delayed row."""
        work = importlib.import_module('memvane.runtime.runtime_work')
        self.select_schema3()
        self.card('concepts/slow-card.md', 'slowprobe')
        project = str(self.case.project)
        passes = []
        execute = self.sync_execute(project, passes)
        with self.wake_queue() as (queue, clock, drain), self.virtual_fill_clock({'slowprobe': 300.0}):
            self.assertEqual(queue.offer('laya-sync', project), 'queued')
            first = drain(execute)
            row, = queue.pending()
            self.assertEqual(first['runs'], 1, first)
            self.assertEqual((passes[0][0]['vector']['state'], passes[0][0].get('vectorReoffer')),
                             ('pending', True), passes[0][0])
            # ACKed; the delayed re-offer survives with a fresh retry history.
            self.assertEqual((row['reason'], row['attempts'], row['status']),
                             (work.DELAYED_REASON, 0, 'pending'), row)
            self.assertGreaterEqual(row['due'], clock['now'] + self.worker.SCHEMA3_VECTOR_REOFFER_SECONDS - 1)
            clock['now'] = row['due']
            second = drain(execute)
            after, = queue.pending()
        last = passes[1][0]
        self.assertEqual(second['runs'], 1, second)
        self.assertEqual((last['state'], last['vector']['state']), ('pending', 'pending'), last)
        self.assertNotIn('vectorReoffer', last)
        # Not ACKed: the same row and revision, one counted attempt, no delayed mark.
        self.assertEqual((after['id'], after['revision']), (row['id'], row['revision']), after)
        self.assertEqual(after['attempts'], 1, after)
        self.assertNotEqual(after['reason'], work.DELAYED_REASON, after)
        self.assertIn(':target_deferred:', str(after['reason']), after)
        emit('schema3-vector-zero-progress-wake', reason=after['reason'], attempts=after['attempts'])

    def test_22_mid_pass_yield_after_progress_acks_reoffers_and_resumes_without_repeats(self):
        """A vector pass that yields (``laya_sync_yield_priority`` before a
        provider call) after it added embedding sets is a progressing pass: the
        wake is ACKed with one delayed re-offer, no provider call follows the
        yield, and the next pass asks the provider only for cards not done."""
        work = importlib.import_module('memvane.runtime.runtime_work')
        self.select_schema3()
        for name in ('alpha', 'bravo', 'charlie'):
            self.card(f'yield/{name}.md', name + 'yieldprobe')
        project = str(self.case.project)
        passes, probes = [], []

        def priority(_controller):
            # A due higher-priority wake appears once one provider call ran.
            probes.append(True)
            return len(probes) > 1
        execute = self.sync_execute(project, passes)
        with self.wake_queue() as (queue, clock, drain):
            self.assertEqual(queue.offer('laya-sync', project), 'queued')
            with mock.patch.object(self.worker, '_higher_priority_wake', side_effect=priority):
                drain(execute)
            row, = queue.pending()
            first, first_calls = passes[0]
            self.assertEqual(len(first_calls), 1, first_calls)  # nothing after the yield
            self.assertEqual(first['vector'].get('progress', 0) > 0, True, first)
            self.assertEqual((row['reason'], row['attempts']), (work.DELAYED_REASON, 0), row)
            clock['now'] = row['due']
            drain(execute)
        second, second_calls = passes[1]
        self.assertTrue(second_calls, second)
        self.assertFalse(set(first_calls) & set(second_calls), (first_calls, second_calls))
        emit('schema3-vector-mid-pass-yield', first=len(first_calls), second=len(second_calls),
             reason=first['vector'].get('reason'))

    def test_23_reoffered_vector_wake_lets_another_due_wake_run_first(self):
        """Queue-level yield after a progressing pass (characterization): the
        delayed re-offer is not due, so another install's due laya-sync wake
        runs next, and the vector wake resumes once its delay passed."""
        import os
        import tempfile
        self.select_schema3()
        self.card('concepts/slow-card.md', 'slowprobe')
        project = str(self.case.project)
        other = Path(tempfile.mkdtemp(prefix='other-install-', dir=os.environ.get('HOME'))).resolve()
        passes, order = [], []
        mine = self.sync_execute(project, passes)

        def execute(row):
            order.append(row['target'])
            return True if row['target'] == str(other) else mine(row)
        with self.wake_queue() as (queue, clock, drain), self.virtual_fill_clock({'slowprobe': 300.0}):
            self.assertEqual(queue.offer('laya-sync', project), 'queued')
            clock['now'] += 1
            self.assertEqual(queue.offer('laya-sync', str(other)), 'queued')
            drain(execute, runs=8)
            self.assertEqual(order, [project, str(other)])
            row, = queue.pending()
            self.assertEqual(row['target'], project)
            self.assertGreater(row['due'], clock['now'])
            clock['now'] = row['due']
            drain(execute, runs=8)
        self.assertEqual(order, [project, str(other), project])
        emit('schema3-vector-queue-yield', order=len(order))

    def test_24_held_provider_lane_defers_other_provider_users_but_not_query_admission(self):
        """Characterization of the lanes a vector fill holds: while one pass
        holds the provider lane, another provider user (a fill of another
        install, learning) is deferred, and recall's query admission
        (``admit_query``) is not: it takes no provider lane."""
        import os
        import tempfile
        rc = importlib.import_module('context_learning.resource_coordination')
        resident = importlib.import_module('memvane.resident.embed_resident')
        from memvane.kernel.resource_deferred import ResourceDeferred
        root = Path(tempfile.mkdtemp(prefix='lanes-', dir=os.environ.get('HOME'))).resolve()
        policy = self.worker.EMBED_POLICY
        with mock.patch.object(rc.Coordinator, '_admit', return_value={'ready': True}):
            with rc.Coordinator(root).activity('provider', policy, {}):
                with self.assertRaises(ResourceDeferred):
                    with rc.Coordinator(root).activity('provider', policy, {}):
                        pass
                self.assertIs(rc.Coordinator(root).check(policy, {})['ready'], True)
                resident.admit_query(coordinator=rc.Coordinator(root), report={})
            # Released with the pass: the next provider user is admitted.
            with rc.Coordinator(root).activity('provider', policy, {}):
                pass
        emit('schema3-vector-lanes', provider='exclusive', query='admitted')

    def test_25_vector_pass_yields_to_a_waiting_initial_build_before_its_next_provider_call(self):
        """Another project's initial build, waiting on a busy owner, is yielded
        to at the next provider-call boundary: no provider call follows, its
        backed-off wake is pulled ahead and runs next, the pass's embedding
        sets stay, and the vector wake resumes on its delayed re-offer."""
        import os
        import tempfile
        work = importlib.import_module('memvane.runtime.runtime_work')
        build = importlib.import_module('memvane.install.initial_build')
        self.select_schema3()
        for name in ('delta', 'echo', 'foxtrot'):
            self.card(f'build/{name}.md', name + 'buildprobe')
        project = str(self.case.project)
        other = Path(tempfile.mkdtemp(prefix='initial-build-', dir=os.environ.get('HOME'))).resolve()
        base = len(self.provider_calls)

        def lookup(target):
            # The other build is seen waiting once this pass made one provider call.
            if str(target) != str(other) or len(self.provider_calls) - base < 1:
                return None
            return {'phase': 'building',
                    'progress': {'state': 'deferred', 'reason': 'owner_busy',
                                 'lastAttemptClass': 'other',
                                 'lastAttemptNs': time.time_ns() - 1_000_000_000}}
        passes, order = [], []
        mine = self.sync_execute(project, passes)

        def execute(row):
            order.append(row['target'])
            return True if row['target'] == str(other) else mine(row)
        with self.wake_queue() as (queue, clock, drain), \
                mock.patch.object(build, 'lookup', side_effect=lookup):
            self.assertEqual(queue.offer('laya-sync', project), 'queued')
            # The build's own wake is backed off after its owner_busy attempt.
            self.assertEqual(queue.offer('laya-sync', str(other), delay=120), 'queued')
            drain(execute, runs=8)
            first, first_calls = passes[0]
            self.assertEqual(len(first_calls), 1, first_calls)
            self.assertEqual(order, [project, str(other)])
            self.assertEqual(first['vector'].get('reason'), 'laya_sync_yield_priority', first)
            self.assertGreater(first['vector'].get('progress', 0), 0, first)
            row, = queue.pending()
            self.assertEqual((row['target'], row['reason'], row['attempts']),
                             (project, work.DELAYED_REASON, 0), row)
            clock['now'] = row['due']
            drain(execute, runs=8)
        second, second_calls = passes[1]
        self.assertEqual(order, [project, str(other), project])
        self.assertTrue(second_calls, second)
        self.assertFalse(set(first_calls) & set(second_calls), (first_calls, second_calls))
        emit('schema3-vector-initial-build-yield', first=len(first_calls), second=len(second_calls))

    # -- 0.44.22: the unchanged pass's vector window (A3) -------------------------
    @contextmanager
    def shared_clock(self, cost):
        """One virtual monotonic clock for the sync worker and both fill layers;
        ``cost[marker]`` seconds pass inside the provider's chunked call for a
        body holding marker. Yields the list of chunked-call markers seen."""
        modules = [self.worker, importlib.import_module('memvane.store.view_fill'),
                   importlib.import_module('memvane.store.blob_fill')]
        offset = [0.0]
        fake = mock.Mock(wraps=time)
        fake.monotonic = lambda: time.monotonic() + offset[0]
        real_chunks = self.worker.ReviewedLocalQmdProvider.embed_document_chunks
        seen = []

        def chunks(provider, body, path, timeout=None):
            for marker, seconds in cost.items():
                if marker in body:
                    seen.append(marker)
                    offset[0] += seconds
            return real_chunks(provider, body, path, timeout)
        with ExitStack() as stack:
            for module in modules:
                stack.enter_context(mock.patch.object(module, 'time', fake))
            stack.enter_context(mock.patch.object(self.worker.ReviewedLocalQmdProvider,
                                                  'embed_document_chunks', new=chunks))
            yield seen

    def cold_cards(self, prefix, count):
        markers = [f'{prefix}{index:02d}probe' for index in range(count)]
        for marker in markers:
            self.card(f'window/{marker}.md', marker)
        return markers

    def embedded_count(self, markers):
        gen = self.head()[0]
        return sum(self.embedded(gen, f'window/{marker}.md')[1] for marker in markers)

    def linked_worktree(self):
        import os
        import shutil
        import tempfile
        project = self.case.project
        base = Path(tempfile.mkdtemp(prefix='linked-', dir=str(project.parent))).resolve()
        self.addCleanup(shutil.rmtree, base, True)
        linked = base / 'linked'
        subprocess.run(['git', '-C', str(project), '-c', 'core.hooksPath=' + os.devnull, 'worktree', 'add',
                        '-q', '-b', 'side', str(linked)], check=True, capture_output=True, timeout=20)
        settings = importlib.import_module('memvane.settings.project_dir').settings_path(project)
        relative = settings.relative_to(project)
        (linked / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(settings, linked / relative)
        for item in self.decision['collections']:
            (linked / Path(item['sourceRoot']).relative_to(project)).mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.wiki, linked / self.wiki.relative_to(project), dirs_exist_ok=True)
        time.sleep(1.1)  # Git racy-index window before the linked checkout's capture.
        return linked

    def test_26_unchanged_pass_fills_its_targets_in_the_vector_window(self):
        """An unchanged pass gets the 240 s vector window: with 12 s cards, one
        pass fills all ten (a fill call that hits its own deadline after adding
        sets does not end the window) and publishes the vector generation; the
        45 s pass budget alone fills two."""
        self.select_schema3()
        markers = self.cold_cards('wnd', 10)
        lexical, _ingests, _calls = self.run_vector_pass(Coordinator(refuse='cpu_pressure'))
        self.assertEqual(self.embedded_count(markers), 0, lexical)
        with self.shared_clock({m: 12.0 for m in markers}) as seen, self.offers():
            result, _ingests, _calls = self.run_vector_pass()
        self.assertEqual(self.embedded_count(markers), 10, (result, seen))
        self.assertEqual(result['vector']['state'], 'published', result)
        self.assertLessEqual(len(seen), 10 + 5, seen)  # a deadline-discarded call is the only repeat
        self.assertEqual(getattr(self.worker, 'SCHEMA3_VECTOR_SECONDS', None), 240)
        emit('schema3-vector-window', calls=len(seen), state=result['vector']['state'])

    def test_27_query_admission_stays_open_through_the_vector_window(self):
        """While an unchanged pass holds the provider lane for its whole window,
        recall's query admission is granted at every provider call and another
        provider user is deferred."""
        import os
        import tempfile
        rc = importlib.import_module('context_learning.resource_coordination')
        resident = importlib.import_module('memvane.resident.embed_resident')
        from memvane.kernel.resource_deferred import ResourceDeferred
        self.select_schema3()
        markers = self.cold_cards('adm', 10)
        self.run_vector_pass(Coordinator(refuse='cpu_pressure'))
        root = Path(tempfile.mkdtemp(prefix='lanes-', dir=os.environ.get('HOME'))).resolve()
        policy = self.worker.EMBED_POLICY
        admitted, deferred = [], []
        real = self.worker.ReviewedLocalQmdProvider.embed_document_chunks

        def probe(provider, body, path, timeout=None):
            resident.admit_query(coordinator=rc.Coordinator(root), report={})
            admitted.append(path)
            try:
                with rc.Coordinator(root).activity('provider', policy, {}):
                    pass
            except ResourceDeferred:
                deferred.append(path)
            return real(provider, body, path, timeout)
        with mock.patch.object(rc.Coordinator, '_admit', return_value={'ready': True}), \
                mock.patch.object(self.worker.ReviewedLocalQmdProvider, 'embed_document_chunks', new=probe), \
                self.shared_clock({m: 12.0 for m in markers}) as seen, self.offers():
            result, _ingests, _calls = self.run_vector_pass(rc.Coordinator(root))
        self.assertGreaterEqual(len(seen), 10, (result, seen))
        # The window went on past a fill call stopped by its own deadline (a
        # card asked again), and admission held through it.
        self.assertGreater(len(seen), len(set(seen)), seen)
        # Every provider call of the pass (seeded cards' included) was admitted.
        self.assertGreaterEqual(len(admitted), len(seen))
        self.assertEqual(deferred, admitted)
        emit('schema3-vector-window-admission', admitted=len(admitted))

    def test_28_initial_build_yield_holds_inside_the_vector_window(self):
        """Inside the 240 s window, another project's initial build seen waiting
        on the owner after five provider calls is yielded to before the sixth:
        its backed-off wake runs next and the filled sets stay."""
        import os
        import tempfile
        work = importlib.import_module('memvane.runtime.runtime_work')
        build = importlib.import_module('memvane.install.initial_build')
        self.select_schema3()
        markers = self.cold_cards('yld', 10)
        self.run_vector_pass(Coordinator(refuse='cpu_pressure'))
        project = str(self.case.project)
        other = Path(tempfile.mkdtemp(prefix='initial-build-', dir=os.environ.get('HOME'))).resolve()
        passes, order = [], []
        mine = self.sync_execute(project, passes)

        def execute(row):
            order.append(row['target'])
            return True if row['target'] == str(other) else mine(row)
        with self.wake_queue() as (queue, clock, drain), \
                self.shared_clock({m: 12.0 for m in markers}) as seen:
            def lookup(target):
                if str(target) != str(other) or len(seen) < 5:
                    return None
                return {'phase': 'building',
                        'progress': {'state': 'deferred', 'reason': 'owner_busy', 'lastAttemptClass': 'other',
                                     'lastAttemptNs': time.time_ns() - 1_000_000_000}}
            with mock.patch.object(build, 'lookup', side_effect=lookup):
                self.assertEqual(queue.offer('laya-sync', project), 'queued')
                self.assertEqual(queue.offer('laya-sync', str(other), delay=120), 'queued')
                drain(execute, runs=8)
        first, _calls = passes[0]
        self.assertEqual(len(seen), 5, (seen, first))
        self.assertEqual(order, [project, str(other)])
        self.assertEqual(first['vector'].get('reason'), 'laya_sync_yield_priority', first)
        # Every card asked for before the yield keeps its sets (a call discarded
        # at its own deadline asks for the same card again: the yield came
        # after the window went on past it).
        self.assertGreater(len(seen), len(set(seen)), seen)
        self.assertEqual(self.embedded_count(markers), len(set(seen)), seen)
        emit('schema3-vector-window-initial-build', calls=len(seen), filled=len(set(seen)))

    def test_29_changed_pass_keeps_the_pass_budget(self):
        """A pass that ingests a card keeps the 45 s pass budget for its fill
        (no vector window): with 12 s cards it fills two."""
        self.select_schema3()
        markers = self.cold_cards('chg', 10)
        with self.shared_clock({m: 12.0 for m in markers}) as seen, self.offers():
            result, ingests, _calls = self.run_vector_pass()
        self.assertTrue(ingests, result)
        self.assertEqual(result['vector']['state'], 'pending', result)
        self.assertLessEqual(self.embedded_count(markers), 2, (result, seen))
        emit('schema3-changed-pass-budget', filled=self.embedded_count(markers))

    def test_30_linked_views_keep_the_pass_budget_after_a_vector_window(self):
        """The window is the main view's: after it, a linked view is held by the
        pass budget (``schema3_pass_budget``) and gets the re-offer, as before."""
        self.select_schema3()
        markers = self.cold_cards('lnk', 4)
        self.linked_worktree()
        lexical, _ingests, _calls = self.run_vector_pass(Coordinator(refuse='cpu_pressure'))
        self.assertEqual(len(lexical.get('worktrees', ())), 1, lexical)
        with self.shared_clock({m: 12.0 for m in markers}), self.offers() as seen:
            result, _ingests, _calls = self.run_vector_pass()
        self.assertEqual(result['vector']['state'], 'published', result)
        self.assertEqual([(w['state'], w.get('reason')) for w in result['worktrees']],
                         [('pending', 'schema3_pass_budget')], result)
        self.assertIn(('laya-sync', str(self.case.project), self.worker.SCHEMA3_VECTOR_REOFFER_SECONDS), seen)
        emit('schema3-linked-after-window', state=result['state'])

    def test_31_window_ends_at_a_deadline_call_that_adds_no_set(self):
        """Inside the vector window a fill call stopped by its own deadline goes
        on only when it added embedding sets: a card whose single provider call
        outlasts every fill call ends the pass ``pending`` after at most one
        repeat, and the cards behind it are not asked for."""
        self.select_schema3()
        self.card('window/aaa-fast.md', 'aaafastprobe')
        self.card('window/bbb-slow.md', 'bbbslowprobe')
        self.card('window/ccc-later.md', 'ccclaterprobe')
        self.run_vector_pass(Coordinator(refuse='cpu_pressure'))
        cost = {'aaafastprobe': 1.0, 'bbbslowprobe': 40.0, 'ccclaterprobe': 1.0}
        with self.shared_clock(cost) as seen, self.offers():
            result, _ingests, _calls = self.run_vector_pass()
        self.assertEqual((result['vector']['state'], result['vector']['reason']),
                         ('pending', 'schema3_vector_deadline'), result)
        self.assertLessEqual(seen.count('bbbslowprobe'), 2, seen)
        self.assertNotIn('ccclaterprobe', seen)
        gen = self.head()[0]
        self.assertEqual(self.embedded(gen, 'window/bbb-slow.md')[1], False)
        self.assertEqual(self.embedded(gen, 'window/ccc-later.md')[1], False)
        emit('schema3-vector-window-ends', slow=seen.count('bbbslowprobe'), calls=len(seen))

    # -- linked views whose project folder name differs from main's -------------
    def linked_other_folder(self):
        """A linked worktree whose branch keeps the other project folder name
        (``.memvane`` vs ``.auto-context``) than the main checkout."""
        import os
        project_dir = importlib.import_module('memvane.settings.project_dir')
        linked = self.linked_worktree()
        main_name = project_dir.settings_path(self.case.project).parent.name
        other = [name for name in project_dir.NAMES if name != main_name][0]
        os.rename(linked / main_name, linked / other)
        self.assertEqual(project_dir.active_name(linked), other)
        return linked, main_name, other

    def unnormalized_linked_decision(self, decision, project):
        """The main decision's roots moved onto ``project`` path by path, with
        no project folder translation (a linked decision its row refuses)."""
        main = Path(decision['projectRoot'])

        def rebased(root):
            return str(project / Path(root).relative_to(main))
        return {**decision, 'projectRoot': str(project),
                'collections': [{**item, 'sourceRoot': rebased(item['sourceRoot'])}
                                for item in decision['collections']],
                'collectionRoots': {name: rebased(root)
                                    for name, root in decision.get('collectionRoots', {}).items()}}

    def test_32_refused_linked_ingest_holds_that_view_and_the_pass_reaches_reseal(self):
        """A linked view whose attested ingest the store refuses
        (``attestation_decision_mismatch``) is held; the pass does not raise,
        the main view publishes its vector generation and the reader reseal
        runs. A refused-provider pass also completes with that view held."""
        self.select_schema3()
        self.card('concepts/kestrel-perch.md', 'kestrelprobe')
        self.linked_other_folder()
        with mock.patch.object(self.worker, '_linked_decision', new=self.unnormalized_linked_decision), \
                self.offers():
            refused, _ingests, _calls = self.run_vector_pass(Coordinator(refuse='cpu_pressure'))
            self.assertEqual([(w['state'], w.get('reason')) for w in refused['worktrees']],
                             [('held', 'attestation_decision_mismatch')], refused)
            result, _ingests, _calls = self.run_vector_pass()
        self.assertEqual((result['vector']['state'], result.get('readerResettle')),
                         ('published', 'resealed'), result)
        self.assertEqual([(w['state'], w.get('reason')) for w in result['worktrees']],
                         [('held', 'attestation_decision_mismatch')], result)
        self.assertIn(result['state'], ('indexed', 'unchanged'), result)
        emit('schema3-linked-ingest-held', state=result['state'], resettle=result.get('readerResettle'))

    def test_33_linked_view_with_the_other_project_folder_name_is_ingested(self):
        """The linked decision's roots name the linked worktree's own project
        folder: a branch that keeps ``.auto-context`` (or ``.memvane``) while
        main uses the other is ingested and published, not refused."""
        self.select_schema3()
        self.card('concepts/kestrel-perch.md', 'kestrelprobe')
        linked, _main_name, other = self.linked_other_folder()
        with self.offers():
            result, _ingests, _calls = self.run_vector_pass(Coordinator(refuse='cpu_pressure'))
        (view,) = result['worktrees']
        self.assertEqual(view['state'], 'indexed', result)
        self.assertGreater(view.get('ingested', 0), 0, result)
        decision = self.worker._linked_decision(self.decision, linked)
        wiki = [item['sourceRoot'] for item in decision['collections'] if item['role'] == 'wiki']
        self.assertEqual([Path(root).relative_to(linked).parts[0] for root in wiki], [other])
        emit('schema3-linked-other-folder', state=view['state'], folder=other)

# Only this unit's checks run here; the seeds' own checks run in their files.
for _name in dir(seed_module.Schema3ConsumerChecks):
    if _name.startswith('test') and _name not in vars(Schema3VectorChecks):
        setattr(Schema3VectorChecks, _name, None)


if __name__ == '__main__':
    unittest.main()

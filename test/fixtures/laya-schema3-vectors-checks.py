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

    @contextmanager
    def activity(self, lane, policy, report):
        from resource_deferred import ResourceDeferred
        self.lanes.append(lane)
        if self.refuse is not None:
            raise ResourceDeferred(self.refuse)
        yield 1

    def check(self, policy, report):
        return {'ready': True}


class Schema3VectorChecks(seed_module.Schema3ConsumerChecks):
    def setUp(self):
        super().setUp()
        self.VV = importlib.import_module('laya_view_vectors')
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
                title = importlib.import_module('laya_blob_values').markdown_title(body, path[6:-3])
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
        provider_module = importlib.import_module('laya_local_embed_provider')
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
            with self.assertRaises(importlib.import_module('laya_blob_values').BlobStoreError) as caught:
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
        import yaml_scalars
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
        scan = importlib.import_module('wiki_dedup_scan')
        learning = importlib.import_module('context_learning.laya_learning')
        config = importlib.import_module('config')
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
        retrieval = importlib.import_module('wiki_compile_retrieval')
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
        selected = importlib.import_module('wiki_selected_laya')
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
        settings = importlib.import_module('project_dir').settings_path(project)
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
        port = importlib.import_module('laya_index_source_port')
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
        journal = importlib.import_module('laya_install_journal')
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
        fill = importlib.import_module('laya_blob_fill')
        values = importlib.import_module('laya_blob_values')
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
            # Lexical publish kept; the vectors wait, the wake stays.
            self.assertEqual((first['state'], first['vector']['state'], first['vector']['reason']),
                             ('pending', 'pending', 'schema3_vector_deadline'), first)
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
        fill = importlib.import_module('laya_blob_fill')
        values = importlib.import_module('laya_blob_values')
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
        work = importlib.import_module('runtime_work')
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
        self.assertEqual(second['vector']['state'], 'published', (first, second))
        self.assertEqual(self.embedded(second['generation'], target), (True, True))
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
        settings = importlib.import_module('project_dir').settings_path(project)
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
        with mock.patch.object(self.worker, 'SCHEMA3_PASS_SECONDS', reserve + 0.001), \
                self.offers() as seen:
            result, _ingests, calls = self.run_vector_pass()
        self.assertEqual(result['vector']['state'], 'pending', result)
        self.assertGreaterEqual(len(calls), 1)  # the first cold target was filled
        self.assertEqual([(w['state'], w.get('reason')) for w in result['worktrees']],
                         [('pending', 'schema3_pass_budget')], result)
        self.assertIn(result['state'], ('indexed', 'unchanged'), result)
        self.assertIn(('laya-sync', str(project), self.worker.SCHEMA3_VECTOR_REOFFER_SECONDS), seen)
        emit('schema3-budget-spent-first-cold', state=result['state'], providerCalls=len(calls))

    def test_16_degraded_schema3_recall_asks_no_query_vector(self):
        """A drift or fence read of a schema3 endpoint sealed with a vector spec
        serves its lexical lists and asks for no query vector."""
        self.select_schema3()
        self.card('concepts/kestrel-perch.md', 'kestrelprobe')
        result, _ingests, _calls = self.run_vector_pass()
        self.assertEqual(result.get('readerResettle'), 'resealed', result)
        host = importlib.import_module('laya_host_response')
        event_log = importlib.import_module('event_log')
        asked, events = [], []

        @contextmanager
        def unproved(**kwargs):
            asked.append(kwargs.get('prompt'))
            raise ValueError('reader_vector_caller_unproved')
            yield  # pragma: no cover

        def recorded(event, **fields):
            events.append(dict(fields, event=event))
            return True
        decision = self.J.select_route(str(self.case.project), action='recall')
        payload = {'cwd': str(self.case.project), 'prompt': 'kestrelprobe', 'session_id': 'drift-probe'}
        provider_module = importlib.import_module('laya_local_embed_provider')
        resident = importlib.import_module('laya_embed_resident')
        with self.seed.authority(), \
                mock.patch.object(host, '__file__', str(self.case.plugin / 'core' / 'laya_host_response.py')), \
                mock.patch.object(provider_module, 'ReviewedLocalQmdProvider', new=self.stub_provider()), \
                mock.patch.object(resident, 'admit_query', return_value=None), \
                mock.patch.object(self.dispatch, 'approved_query_vector', new=unproved), \
                mock.patch.object(event_log, 'emit', side_effect=recorded), \
                host._event_scope('recall', 'claude', decision, payload):
            answer = host.serve_endpoint_recall('claude', payload, cwd=str(self.case.project),
                                                drift_decision=decision, main_proof=None)
        self.assertEqual(asked, [])
        context = answer['hookSpecificOutput']['additionalContext']
        self.assertIn('kestrel', context)
        recall = [e for e in events if e['event'] == 'recall']
        self.assertEqual(len(recall), 1, events)
        self.assertEqual(recall[0].get('degraded'), 'reader_endpoint', recall[0])
        emit('schema3-degraded-recall-lexical', asked=len(asked), notes=recall[0].get('notes'))

    def test_17_empty_recall_reason_is_no_candidates(self):
        """A recall with no candidate rows names that, not its last diagnostic note."""
        self.select_schema3()
        self.card('concepts/kestrel-perch.md', 'kestrelprobe')
        # Lexical only (the provider lane refuses): recall notes a vector fallback.
        self.run_vector_pass(Coordinator(refuse='cpu_pressure'))
        host = importlib.import_module('laya_host_response')
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

    # -- R4: the first vector head after a bundle upgrade -----------------------
    @contextmanager
    def upgraded_bundle(self, new):
        """The upgraded bundle ``new`` executing (U14b upgrade authority over this install)."""
        from types import SimpleNamespace
        spec = importlib.util.spec_from_file_location('vectors_lifecycle_checks',
                                                      ROOT / 'test/fixtures/laya-schema3-lifecycle-checks.py')
        lifecycle = importlib.util.module_from_spec(spec); spec.loader.exec_module(lifecycle)
        owner = SimpleNamespace(case=self.case, reader=self.R, seed=self.seed)
        with lifecycle.Lifecycle.upgrade_authority(owner, new) as (upgrade, target, review, att), \
                mock.patch.object(self.J, '_verified_executing_root', return_value=str(new)), \
                mock.patch.object(self.J, '_provider_file_identity',
                                  return_value=[[str(new / 'p.py'), 1, 2, 3, 4, 5]]), \
                mock.patch.object(self.worker, '_writer_executing_root', return_value=new):
            yield upgrade, target, review

    def test_11_first_vector_head_after_a_bundle_upgrade_reseals_and_recall_is_hybrid(self):
        case = self.case
        self.select_schema3()
        self.assertIsNone(self.selected_record()['vector_profile'])
        new = case.home / 'r4-vector-plugin'
        new.mkdir()
        with self.upgraded_bundle(new) as (upgrade, target, review):
            intent = upgrade._prepare(case.project, case.state, case.journal, case.home, new,
                                      target['sourceCommit'], review['proposalSha256'], '/synthetic/model', 3)
            self.assertEqual(upgrade._switch(intent)['status'], 'active_upgrade_complete')
        # The upgrade leaves the record on its sealed predecessor attestation.
        stored = self.selected_record()
        live = self.J.inspect_journal(case.journal)['providerAttestationSha256']
        self.assertNotEqual(stored['attestation_sha256'], live)
        self.card('concepts/kestrel-perch.md', 'kestrelprobe')
        with self.upgraded_bundle(new):
            result, _ingests, _calls = self.run_vector_pass(authority=ExitStack())
        emit('r4-upgrade-then-first-vector-head', state=result['state'],
             vector=(result.get('vector') or {}).get('state'), resettle=result.get('readerResettle'))
        self.assertEqual((result['state'], result['vector']['state'], result.get('readerResettle')),
                         ('indexed', 'published', 'resealed'), result)
        record = self.selected_record()
        self.assertEqual((record['vector_profile'], record['vector_recipe'], record['attestation_sha256']),
                         (self.spec['profile_id'], self.spec['recipe_id'], live))
        self.assertEqual({k for k in record if record[k] != stored[k]},
                         {'vector_profile', 'vector_recipe', 'attestation_sha256'})
        # Hybrid recall from the upgraded bundle finds the card by vector.
        gen = self.head()
        target_path = 'concepts/kestrel-perch.md'
        with self.upgraded_bundle(new):
            hit = self.hybrid_recall('zzqqxv nomatch', self.card_vector(gen[0], target_path),
                                     authority=ExitStack(), host_root=new)
        self.assertEqual((hit.store_format, hit.data_generation, hit.semantic_status),
                         ('schema3', gen[0], 'complete'))
        self.assertEqual(self.paths(hit)[0], target_path)


# Only this unit's checks run here; the seeds' own checks run in their files.
for _name in dir(seed_module.Schema3ConsumerChecks):
    if _name.startswith('test') and _name not in vars(Schema3VectorChecks):
        setattr(Schema3VectorChecks, _name, None)


if __name__ == '__main__':
    unittest.main()

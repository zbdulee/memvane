#!/usr/bin/env python3
"""Pure synthetic host response checks; no route activation or operating DB."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core"))
import event_log
import laya_host_response as response
import laya_index_vector as vector

SCOPE = "6" * 32
ROOT = "/synthetic/source"
DECISION = {"state": "candidate", "scopeId": SCOPE,
            "sourceRoot": ROOT, "collectionName": "docs"}
VECTOR_PROFILE = {"profileId": "f" * 64, "modelId": "synthetic-model",
                  "embeddingFingerprint": "a" * 64, "dimension": 3,
                  "metric": "cosine", "approvedProviderId": "synthetic-provider",
                  "providerArtifactSha256": "b" * 64,
                  "providerRelativePath": "core/synthetic_provider.py"}


class FakeIndex:
    def __init__(self):
        self.pinned = False
        self.released = False

    def pin(self, scope):
        assert scope == SCOPE
        self.pinned = True
        return "synthetic-token", "generation-7"

    def release(self, token):
        assert token == "synthetic-token"
        self.released = True


class FakeBoundCandidates:
    def require_returned_verified(self): pass
    def finish(self): pass
    def close(self): pass


def sealed_profile(**changes):
    profile = {"profile_id": VECTOR_PROFILE["profileId"],
               "model_id": VECTOR_PROFILE["modelId"],
               "embedding_fingerprint": VECTOR_PROFILE["embeddingFingerprint"],
               "dimension": VECTOR_PROFILE["dimension"], "metric": "cosine",
               "scope": SCOPE, "generation": "generation-7",
               "rows_digest": "c" * 64, "rows_count": 1}
    profile.update(changes)
    return profile


def row(path="docs/a.md", body="alpha\n# quoted heading", **changes):
    result = {"scope": SCOPE, "generation": "generation-7", "path": path,
              "hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
              "body": body, "score": 0.5}
    result.update(changes)
    return result


V2_PROOF = {"productRouteAllowed": True, "selectionEpoch": 3,
            "vectorProfile": VECTOR_PROFILE,
            "providerManifest": {"approvedProviderId": "synthetic-provider"}}
V2_BUILT = {"generalTerms": ["alpha", "beta", "gamma"], "epTerms": [], "identifiers": []}


def v2_decision(project="/synthetic/project"):
    settings = {"topN": 3, "lexicalPatterns": [], "recallStrategy": "hierarchical",
                "minScore": 0, "rawFallbackMinScore": 0, "skipPaths": []}
    roots = {"raw": project + "/docs", "wiki": project + "/wiki"}
    return {**DECISION,
            "pluginRoot": str(Path(response.__file__).resolve().parents[1]),
            "projectRoot": project, "dbPath": "/synthetic/index.sqlite",
            "queuePath": "/synthetic/queue.sqlite", "dbIncarnation": "a" * 32,
            "vectorProfile": VECTOR_PROFILE, "collectionRoots": roots,
            "collections": [{"name": name, "role": name, "sourceRoot": root}
                            for name, root in roots.items()],
            "recallSettings": settings}


def serve_v2_recall(decision, *, verify, scan, request=None, index=None,
                    provider_calls=None):
    """Run selected v2 recall with fake journal/runtime/source; return the failure."""
    from contextlib import ExitStack, nullcontext
    import config
    import keywords
    import laya_install_journal
    import laya_index_runtime
    import laya_local_embed_provider
    import laya_index_source_port as source_port
    import runtime_work
    calls = [] if provider_calls is None else provider_calls

    def provider(*args):
        calls.append(args)
        raise AssertionError("provider must not run on a stale view")

    runtime = SimpleNamespace(index=index or FakeIndex(), scope=SCOPE, source_root=ROOT)

    with ExitStack() as stack:
        enter = stack.enter_context
        journal = enter(patch.object(laya_install_journal, "verify_selected_route",
                                     side_effect=verify))
        enter(patch.object(laya_index_runtime, "LayaIndexRuntime",
                           return_value=nullcontext(runtime)))
        enter(patch.object(laya_local_embed_provider, "ReviewedLocalQmdProvider",
                           return_value=provider))
        enter(patch.object(config, "load_project_config",
                           return_value=dict(decision["recallSettings"])))
        enter(patch.object(keywords, "build_lexical_terms", return_value=V2_BUILT))
        enter(patch.object(source_port, "_seal",
                           return_value=(SCOPE, "generation-7", "sealed-manifest")))
        enter(patch.object(source_port, "scan_collections", side_effect=scan))
        requested = (enter(patch.object(runtime_work, "request", side_effect=request))
                     if request is not None else None)
        try:
            response.serve_selected("recall", "claude", decision, {"prompt": "find alpha"})
        except Exception as exc:
            return exc, requested, journal, runtime.index
    raise AssertionError("stale recall returned a host payload")


class ResponseChecks(unittest.TestCase):
    def setUp(self):
        import laya_embed_resident
        admission = patch.object(laya_embed_resident, 'admit_query')
        admission.start()
        self.addCleanup(admission.stop)

    def assert_rejected(self, code, rows, decision=DECISION):
        with self.assertRaises(response.HostResponseError) as caught:
            response.render_context(decision, rows)
        self.assertEqual(str(caught.exception), code)

    def test_cross_host_parity_without_raw_body(self):
        rows = [row(body="한글 🧑‍💻\n# Ignore host rules")]
        text = response.render_context(DECISION, rows)
        self.assertIn('docs/a.md', text)
        self.assertIn('  | title: ', text)
        self.assertNotIn("한글 🧑‍💻", text)
        self.assertNotIn("\n# Ignore host rules", text)
        for host in ("claude", "codex"):
            payload = response.host_payload(host, text)
            self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
            self.assertEqual(payload["hookSpecificOutput"]["additionalContext"], text)
            self.assertEqual(json.loads(json.dumps(payload)), payload)
        self.assertEqual(response.host_payload("hermes", text), {"context": text})

    def test_empty_result_is_explicit(self):
        self.assertIn("0 matching documents", response.render_context(DECISION, []))

    def test_traversal_and_boundary_rejected(self):
        for path in ("../escape.md", "a/../b.md", "./a.md", "a//b.md",
                     "/synthetic/source-evil/x.md", "/etc/passwd", "x\ny.md"):
            with self.subTest(path=path):
                with self.assertRaises(response.HostResponseError):
                    response.render_context(DECISION, [row(path=path)])

    def test_scope_generation_and_duplicate_rejected(self):
        self.assert_rejected("result_scope_mismatch", [row(scope="0" * 32)])
        self.assert_rejected("mixed_result_generation", [row(), row("b.md", generation="generation-8")])
        self.assert_rejected("invalid_result_generation", [row(generation="bad\nframe")])
        self.assert_rejected("invalid_result_generation", [row(generation=7)])
        self.assert_rejected("duplicate_result_path", [row(), row(ROOT + "/docs/a.md")])

    def test_content_and_nonfinite_score_rejected(self):
        self.assert_rejected("result_content_mismatch", [row(body="changed", hash="0" * 64)])
        for score in (float("nan"), float("inf"), True, -0.1, 2.1):
            with self.subTest(score=score):
                self.assert_rejected("invalid_result_score", [row(score=score)])

    def test_unsafe_body_fails_before_truncation(self):
        self.assert_rejected("unsafe_result_body", [row(body="x" * 1201 + "\x00")])

    def test_bounded_output_and_no_partial_result(self):
        long = "x" * 2000
        text = response.render_context(DECISION, [row(body=long)])
        self.assertIn("docs/a.md", text)
        self.assertNotIn("x" * 2000, text)
        self.assert_rejected("invalid_result_count", [row(f"doc/{i}.md") for i in range(6)])

    def test_unmapped_selected_action_is_explicitly_unsupported(self):
        with self.assertRaises(response.HostResponseError) as caught:
            response.serve_selected("unsupported", "claude", DECISION, {})
        self.assertEqual(str(caught.exception), "selected_action_unsupported")

    def test_companion_root_requires_verifier_proof(self):
        import laya_install_journal
        local = str(Path(response.__file__).resolve().parents[1])
        decision = {**DECISION, 'pluginRoot': '/synthetic/owner'}
        accepted = {'productRouteAllowed': True, 'executingPluginRoot': local}
        with patch.object(laya_install_journal, 'verify_selected_route',
                          return_value=accepted) as verify:
            self.assertEqual(response.serve_selected('file-change-event', 'claude',
                                                     decision, {}), {})
        self.assertEqual(verify.call_count, 2)
        with patch.object(laya_install_journal, 'verify_selected_route',
                          return_value={'productRouteAllowed': True}):
            with self.assertRaisesRegex(response.HostResponseError,
                                        'selected_plugin_root_mismatch'):
                response.serve_selected('file-change-event', 'claude', decision, {})

    def test_selected_recall_uses_journal_provider_and_warm_query(self):
        from contextlib import nullcontext
        import laya_install_journal
        import laya_index_runtime
        import laya_local_embed_provider
        import laya_embed_resident
        import config
        from resource_deferred import ResourceDeferred
        decision = {**DECISION,
                    "pluginRoot": str(Path(response.__file__).resolve().parents[1]),
                    "projectRoot": "/synthetic/project",
                    "dbPath": "/synthetic/index.sqlite",
                    "queuePath": "/synthetic/queue.sqlite",
                    "dbIncarnation": "a" * 32,
                    "vectorProfile": VECTOR_PROFILE}
        proof = {"productRouteAllowed": True, "selectionEpoch": 1,
                 "vectorProfile": VECTOR_PROFILE,
                 "providerManifest": {"approvedProviderId": "synthetic-provider"}}
        provider = object()
        runtime = SimpleNamespace(index=FakeIndex(), scope=SCOPE, source_root=ROOT)
        with patch.object(laya_install_journal, "verify_selected_route", return_value=proof), \
             patch.object(laya_index_runtime, "LayaIndexRuntime", return_value=nullcontext(runtime)), \
             patch.object(laya_local_embed_provider, "ReviewedLocalQmdProvider", return_value=provider) as built, \
             patch.object(config, "load_project_config", return_value={"topN": 3,
                     "lexicalPatterns": []}), \
             patch.object(response, "query_selected_hybrid", return_value=[row()]) as queried:
            payload = response.serve_selected("recall", "codex", decision,
                                             {"prompt": "find alpha"})
            # Deferred query admission no longer refuses recall by itself; the
            # legacy (non-wiki) route still serves no context.
            with patch.object(laya_embed_resident, 'admit_query',
                              side_effect=ResourceDeferred('pressure')):
                deferred = response.serve_selected('recall', 'codex', decision,
                                                   {'prompt': 'find alpha'})
            self.assertEqual(deferred["hookSpecificOutput"]["additionalContext"], "")
        self.assertEqual(built.call_count, 2)
        self.assertTrue(all(call.args == (proof['providerManifest'],)
                            for call in built.call_args_list))
        queried.assert_not_called()
        self.assertEqual(payload["hookSpecificOutput"]["additionalContext"], "")

    def test_selected_update_only_enqueues_and_accepts_worker_epoch_advance(self):
        import laya_install_journal
        import runtime_work
        decision = {**DECISION,
                    "pluginRoot": str(Path(response.__file__).resolve().parents[1]),
                    "projectRoot": "/synthetic/project"}
        proof = {"productRouteAllowed": True, "selectionEpoch": 4,
                 "providerManifest": {"approvedProviderId": "synthetic-provider"}}
        advanced = {**proof, "selectionEpoch": 5}
        with patch.object(laya_install_journal, "verify_selected_route",
                          side_effect=[proof, proof, advanced]), \
             patch.object(runtime_work, "request", return_value={"status": "queued"}) as request:
            payload = response.serve_selected("update", "claude", decision,
                                             {"hook_event_name": "SessionStart"})
        request.assert_called_once_with("laya-sync", decision["projectRoot"])
        self.assertIn("update queued", payload["hookSpecificOutput"]["additionalContext"])

    def test_selected_successful_bash_wakes_full_scan_owner_only(self):
        import laya_install_journal
        import runtime_work
        decision = {**DECISION,
                    "pluginRoot": str(Path(response.__file__).resolve().parents[1]),
                    "projectRoot": "/synthetic/project", "collections": []}
        proof = {"productRouteAllowed": True, "selectionEpoch": 2}
        bash = {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                "tool_input": {"command": "git switch qa/feature"}}
        cases = {"ok": ({"exit_code": 0}, ["laya-sync"]),
                 "official": ({"stdout": "", "stderr": "", "interrupted": False}, ["laya-sync"]),
                 "failed": ({"exit_code": 1}, []),
                 "interrupted": ({"interrupted": True, "stdout": "", "stderr": ""}, []),
                 "background": ({"exit_code": 0, "backgroundTaskId": "b1"}, [])}
        for name, (tool_response, expected) in cases.items():
            with self.subTest(case=name), \
                 patch.object(laya_install_journal, "verify_selected_route", return_value=proof), \
                 patch.object(runtime_work, "request",
                              return_value={"status": "queued"}) as request:
                self.assertEqual(response.serve_selected(
                    "file-change-event", "claude", decision,
                    {**bash, "tool_response": tool_response}), {})
                self.assertEqual([call.args for call in request.call_args_list],
                                 [(kind, decision["projectRoot"]) for kind in expected])

    def test_selected_compile_passes_proved_edit_to_durable_enqueue(self):
        import laya_install_journal
        import wiki_compile_enqueue
        with tempfile.TemporaryDirectory(prefix='laya-host-compile-') as temp:
            root = str(Path(temp).resolve())
            decision = {**DECISION,
                        'pluginRoot': str(Path(response.__file__).resolve().parents[1]),
                        'projectRoot': root}
            event = {'hook_event_name': 'PostToolUse', 'tool_name': 'Write',
                     'tool_input': {'file_path': 'docs/a.md'}, 'cwd': root}
            proof = {'productRouteAllowed': True, 'selectionEpoch': 1}
            with patch.object(laya_install_journal, 'verify_selected_route',
                              return_value=proof), patch.object(
                              wiki_compile_enqueue, 'enqueue_payload',
                              return_value={'queued': 1, 'reason': 'queued'}) as enqueue:
                self.assertEqual(response.serve_selected('compile', 'codex',
                                                         decision, event), {})
            enqueue.assert_called_once_with({**event, 'engine': 'codex'})

    def test_selected_session_start_wakes_pending_compile_queue(self):
        import compile_paths
        import laya_install_journal
        import runtime_work
        import wiki_freshness
        with tempfile.TemporaryDirectory(prefix='laya-host-wake-') as temp:
            root = Path(temp).resolve()
            decision = {**DECISION,
                        'pluginRoot': str(Path(response.__file__).resolve().parents[1]),
                        'projectRoot': str(root), 'collections': []}
            proof = {'productRouteAllowed': True, 'selectionEpoch': 1}
            with patch.object(laya_install_journal, 'verify_selected_route',
                              return_value=proof), patch.object(
                              wiki_freshness, 'unresolved_pending_refreshes_strict',
                              return_value=[{'sourcePath': 'docs/a.md'}]), patch.object(
                              compile_paths, 'ledger', return_value=root / 'queue'), \
                 patch.object(runtime_work, 'request',
                              return_value={'status': 'queued'}) as request:
                response.serve_selected('update', 'codex', decision,
                                        {'hook_event_name': 'SessionStart'})
            self.assertEqual([call.args[0] for call in request.call_args_list],
                             ['laya-sync', 'laya-compile'])

    def test_edit_gate_accepts_only_scoped_edit_paths(self):
        with tempfile.TemporaryDirectory(prefix="laya-host-edit-") as temp:
            project = Path(temp).resolve()
            decision = {"projectRoot": str(project)}
            event = {"hook_event_name": "PreToolUse", "tool_name": "Write",
                     "tool_input": {"file_path": "docs/a.md"}, "cwd": str(project)}
            response._verified_edit(decision, event, "gate")
            event["tool_input"]["file_path"] = "../outside.md"
            with self.assertRaises(response.HostResponseError) as caught:
                response._verified_edit(decision, event, "gate")
            self.assertEqual(str(caught.exception), "edit_outside_project")

    def test_posttool_observer_requires_real_patch_target(self):
        with tempfile.TemporaryDirectory(prefix="laya-host-patch-") as temp:
            project = Path(temp).resolve()
            decision = {"projectRoot": str(project)}
            event = {"hook_event_name": "PostToolUse", "tool_name": "apply_patch",
                     "tool_input": {"patch": "*** Begin Patch\n*** Update File: docs/a.md\n*** End Patch"},
                     "cwd": str(project)}
            response._verified_edit(decision, event, "posttool")
            event["tool_input"]["patch"] = "*** Begin Patch\n*** End Patch"
            with self.assertRaises(response.HostResponseError) as caught:
                response._verified_edit(decision, event, "posttool")
            self.assertEqual(str(caught.exception), "edit_path_missing")

    def test_vector_recall_uses_exact_sealed_profile_and_releases_pin(self):
        index = FakeIndex()
        runtime = SimpleNamespace(index=index, scope=SCOPE, source_root=ROOT)
        result_rows = [row()]
        provider_calls = []

        def provider(prompt, profile):
            provider_calls.append((prompt, profile))
            return {"profileId": profile["profileId"],
                    "approvedProviderId": profile["approvedProviderId"],
                    "providerArtifactSha256": profile["providerArtifactSha256"],
                    "vector": [1.0, 0.0, 0.0]}

        with patch.object(vector.LayaVectorIndex, "profile_for_pin",
                          return_value=sealed_profile()), patch.object(
                              vector, "search_vector_verified", return_value=result_rows) as search:
            found = response.query_selected_vector(runtime, "find alpha", VECTOR_PROFILE,
                                                   VECTOR_PROFILE.copy(), provider)
        self.assertEqual(found, result_rows)
        self.assertEqual(provider_calls, [("find alpha", VECTOR_PROFILE)])
        self.assertTrue(index.released)
        self.assertEqual(search.call_args.kwargs["model_id"], "synthetic-model")
        self.assertEqual(search.call_args.kwargs["embedding_fingerprint"], "a" * 64)
        self.assertEqual(search.call_args.args[3], [1.0, 0.0, 0.0])

    def test_vector_profile_mismatch_never_calls_provider_or_search(self):
        index = FakeIndex()
        runtime = SimpleNamespace(index=index, scope=SCOPE, source_root=ROOT)
        calls = []
        with patch.object(vector.LayaVectorIndex, "profile_for_pin",
                          return_value=sealed_profile(profile_id="0" * 64)), patch.object(
                              vector, "search_vector_verified") as search:
            with self.assertRaises(response.HostResponseError) as caught:
                response.query_selected_vector(runtime, "find alpha", VECTOR_PROFILE,
                                               VECTOR_PROFILE, lambda *_: calls.append(1))
        self.assertEqual(str(caught.exception), "vector_profile_mismatch")
        self.assertEqual(calls, [])
        search.assert_not_called()
        self.assertTrue(index.released)

    def test_vector_provider_missing_or_forged_identity_blocks(self):
        index = FakeIndex()
        runtime = SimpleNamespace(index=index, scope=SCOPE, source_root=ROOT)
        with self.assertRaises(response.HostResponseError) as caught:
            response.query_selected_vector(runtime, "find alpha", VECTOR_PROFILE,
                                           VECTOR_PROFILE, None)
        self.assertEqual(str(caught.exception), "vector_provider_missing")
        self.assertFalse(index.pinned)
        with patch.object(vector.LayaVectorIndex, "profile_for_pin",
                          return_value=sealed_profile()), patch.object(
                              vector, "search_vector_verified") as search:
            with self.assertRaises(response.HostResponseError) as caught:
                response.query_selected_vector(runtime, "find alpha", VECTOR_PROFILE,
                                               VECTOR_PROFILE, lambda *_: {
                                                   "profileId": VECTOR_PROFILE["profileId"],
                                                   "approvedProviderId": "unapproved",
                                                   "providerArtifactSha256": "b" * 64,
                                                   "vector": [1.0, 0.0, 0.0]})
        self.assertEqual(str(caught.exception), "vector_provider_mismatch")
        search.assert_not_called()
        self.assertTrue(index.released)

    def test_same_pin_hybrid_promotes_exact_identifier_lexical_hit(self):
        import laya_index_source_port as source_port
        index = FakeIndex()
        runtime = SimpleNamespace(index=index, scope=SCOPE, source_root=ROOT)
        seen = []

        def lexical(_index, _root, token, query, limit):
            seen.append((token, query, limit))
            return [row(path="settings.md", body="settings details", score=-1.0)]

        with patch.object(vector.LayaVectorIndex, "profile_for_pin",
                          return_value=sealed_profile()), patch.object(
                              vector, "search_vector_verified",
                              return_value=[row(path="unrelated.md", body="other")]
                          ), patch.object(source_port, "search_verified", side_effect=lexical):
            found = response.query_selected_hybrid(
                runtime, "Where is `settings.md`?", VECTOR_PROFILE, VECTOR_PROFILE,
                lambda *_: {"profileId": VECTOR_PROFILE["profileId"],
                            "approvedProviderId": VECTOR_PROFILE["approvedProviderId"],
                            "providerArtifactSha256": VECTOR_PROFILE["providerArtifactSha256"],
                            "vector": [1.0, 0.0, 0.0]})
        self.assertEqual(found[0]["path"], "settings.md")
        self.assertTrue(any('"settings"*' in query for _, query, _ in seen))
        self.assertTrue(all(token == "synthetic-token" and limit == 20
                            for token, _, limit in seen))
        self.assertTrue(index.released)

    def test_same_pin_ep_variants_are_independent_and_fused(self):
        import laya_index_source_port as source_port
        index = FakeIndex()
        runtime = SimpleNamespace(index=index, scope=SCOPE, source_root=ROOT)
        seen = []

        def lexical(_index, _root, token, query, _limit):
            seen.append((token, query))
            if query in ('"ep012"*', '"ep12"*'):
                return [row(path="EP12.md", body="chapter 12", score=-1.0)]
            return []

        with patch.object(vector.LayaVectorIndex, "profile_for_pin",
                          return_value=sealed_profile()), patch.object(
                              vector, "search_vector_verified",
                              return_value=[row(path="unrelated.md", body="other")]
                          ), patch.object(source_port, "search_verified", side_effect=lexical):
            found = response.query_selected_hybrid(
                runtime, "EP12 summary", VECTOR_PROFILE, VECTOR_PROFILE,
                lambda *_: {"profileId": VECTOR_PROFILE["profileId"],
                            "approvedProviderId": VECTOR_PROFILE["approvedProviderId"],
                            "providerArtifactSha256": VECTOR_PROFILE["providerArtifactSha256"],
                            "vector": [1.0, 0.0, 0.0]}, lexical_patterns=("ep",))
        self.assertEqual(found[0]["path"], "EP12.md")
        self.assertIn(("synthetic-token", '"ep012"*'), seen)
        self.assertIn(("synthetic-token", '"ep12"*'), seen)
        self.assertTrue(index.released)
        text = response.render_context(DECISION, found, ranking="hybrid_rrf")
        self.assertIn("higher hybrid rank score ranks first", text)

    def test_qmd_rrf_weights_first_nonempty_list_and_uses_position_score(self):
        # The general query can be empty. QMD pushes only nonempty lists, so
        # the EP list then receives the first-list 2x contribution.
        found = response._fuse_verified_lists([
            [], [row(path="z-ep.md", body="ep")],
            [row(path="a-vector.md", body="semantic")]])
        self.assertEqual([item["path"] for item in found],
                         ["z-ep.md", "a-vector.md"])
        self.assertEqual([item["score"] for item in found], [1.0, 0.5])

    def test_qmd_rrf_top_rank_bonus_changes_winner(self):
        first = [row(path=p, body=p) for p in
                 ("b-first.md", "x.md", "y.md", "a-both-fourth.md")]
        second = [row(path=p, body=p) for p in
                  ("z.md", "x.md", "y.md", "a-both-fourth.md")]
        found = response._fuse_verified_lists([first, second])
        self.assertEqual(found[0]["path"], "b-first.md")
        self.assertEqual(found[0]["score"], 1.0)

    def test_qmd_rrf_equal_scores_retain_first_seen_list_order(self):
        found = response._fuse_verified_lists([
            [row(path="x-double.md", body="x")],
            [row(path="z-seen-first.md", body="z")],
            [row(path="a-seen-later.md", body="a")]])
        self.assertEqual([item["path"] for item in found],
                         ["x-double.md", "z-seen-first.md", "a-seen-later.md"])

    def test_v2_wiki_provenance_and_source_revision_are_required(self):
        import yaml_scalars
        with tempfile.TemporaryDirectory(prefix='laya-wiki-policy-') as temp:
            project = Path(temp).resolve()
            raw = project / 'docs'
            wiki = project / 'wiki'
            raw.mkdir()
            wiki.mkdir()
            source = raw / 'a.md'
            source.write_text('original source', encoding='utf-8')
            revision = {'kind': 'file', 'path': 'docs/a.md', 'collection': 'raw',
                        'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                        'size': source.stat().st_size,
                        'mtimeNs': source.stat().st_mtime_ns}
            decision = {'projectRoot': str(project),
                        'collectionRoots': {'raw': str(raw), 'wiki': str(wiki)},
                        'collections': [{'name': 'raw', 'role': 'raw',
                                         'sourceRoot': str(raw)},
                                        {'name': 'wiki', 'role': 'wiki',
                                         'sourceRoot': str(wiki)}]}
            frontmatter = ('---\ntitle: Synthetic card\nstatus: verified\n'
                           'createdBy: qmd-auto-context\nsourceRevisions:\n  - '
                           + json.dumps(revision) + '\nverifiedBodyHash: {digest}\n'
                           '---\n## Summary\nSource fact.\n')
            digest = yaml_scalars.card_body_hash(frontmatter.replace('{digest}', '0' * 64))
            body = frontmatter.replace('{digest}', digest)
            card = row(path='wiki/card.md', body=body)
            self.assertTrue(response._verified_wiki_row(card, decision))
            before = source.stat()
            os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
            self.assertTrue(response._verified_wiki_row(card, decision))
            source.write_text('new source not compiled yet', encoding='utf-8')
            self.assertFalse(response._verified_wiki_row(card, decision))
            self.assertTrue(response._verified_wiki_row(
                card, decision, current_source=False))
            self.assertFalse(response._verified_wiki_row(
                row(path='wiki/card.md', body=body.replace('Source fact.', 'Forged fact.')),
                decision, current_source=False))
            retained = source.stat()
            source.write_text('modified source', encoding='utf-8')
            os.utime(source, ns=(retained.st_atime_ns, retained.st_mtime_ns))
            self.assertFalse(response._verified_wiki_row(card, decision))

    def test_v2_hierarchy_never_backfills_raw_when_wiki_unverified(self):
        import laya_index_capacity
        index = FakeIndex()
        runtime = SimpleNamespace(index=index, scope=SCOPE, source_root=ROOT,
                                  binding_store=object())
        decision = {'projectRoot': '/synthetic/project',
                    'collectionRoots': {'raw': ROOT, 'wiki': '/synthetic/wiki'},
                    'collections': [{'name': 'raw', 'role': 'raw', 'sourceRoot': ROOT},
                                    {'name': 'wiki', 'role': 'wiki',
                                     'sourceRoot': '/synthetic/wiki'}],
                    'recallSettings': {'recallStrategy': 'hierarchical',
                                       'lexicalPatterns': [], 'minScore': 0,
                                       'rawFallbackMinScore': 0,
                                       'skipPaths': []}}
        import wiki_freshness
        with patch.object(response, '_v2_phase', side_effect=[
                ([row(path='wiki/card.md')], True),
                ([row(path='raw/chapter.md')], True)]) as phase, \
             patch.object(response, '_verified_wiki_row', return_value=False), \
             patch.object(laya_index_capacity, 'open_pinned_candidate_query',
                          return_value=FakeBoundCandidates()), \
             patch.object(wiki_freshness, 'unresolved_pending_refreshes_strict',
                          return_value=[]):
            found, title_only = response.query_selected_v2(
                runtime, 'find chapter', VECTOR_PROFILE, VECTOR_PROFILE,
                lambda *_: None, decision)
        self.assertEqual(found, [])
        self.assertEqual(title_only, frozenset())
        self.assertEqual([call.args[6] for call in phase.call_args_list],
                         [('wiki',)])
        self.assertTrue(index.released)

    def test_v2_revision_label_outside_route_resolves_by_raw_path(self):
        # A backfilled card may carry the collection label of a renamed or
        # copied project. Like the QMD reader, the path decides, not the label.
        with tempfile.TemporaryDirectory(prefix='laya-revision-label-') as temp:
            project, revision, decision = self.span_project(temp)

            def card(collection, path='docs/a.md'):
                return self.span_card({**revision, 'collection': collection,
                                       'path': path})
            for current in (False, True):
                self.assertTrue(response._verified_wiki_row(
                    card('raw'), decision, current_source=current))
                self.assertTrue(response._verified_wiki_row(
                    card('other-project'), decision, current_source=current))
            why = {}
            self.assertFalse(response._verified_wiki_row(
                card('wiki'), decision, current_source=False, reasons=why))
            self.assertEqual(why, {'source_outside_raw_roots': 1})
            (project / 'outside.md').write_bytes(self.SPAN_SOURCE)
            why = {}
            self.assertFalse(response._verified_wiki_row(
                card('other-project', 'outside.md'), decision,
                current_source=False, reasons=why))
            self.assertEqual(why, {'source_outside_raw_roots': 1})
            why = {}
            self.assertFalse(response._verified_wiki_row(
                row(path='wiki/card.md', body='---\ntitle: Draft\nstatus: generated\n'
                    'createdBy: qmd-auto-context\n---\nbody\n'),
                decision, current_source=False, reasons=why))
            self.assertEqual(why, {'status_not_verified': 1})

    def test_v2_all_matches_filtered_is_reported_not_silent(self):
        import io
        import laya_index_capacity
        index = FakeIndex()
        runtime = SimpleNamespace(index=index, scope=SCOPE, source_root=ROOT,
                                  binding_store=object())
        decision = v2_decision()
        decision['recallSettings'] = {**decision['recallSettings'],
                                      'recallStrategy': 'wikiOnly'}
        draft = row(path='wiki/draft.md', body='---\ntitle: Draft\nstatus: generated\n'
                    'createdBy: qmd-auto-context\n---\nbody\n')
        legacy = row(path='wiki/legacy.md', body='---\ntitle: Old\nstatus: verified\n'
                     'createdBy: qmd-auto-context\n---\nbody\n')

        def phase(*args, **kwargs):
            source_filter = args[11]
            kept = [item for item in (draft, legacy, draft) if source_filter(item)]
            return kept, False
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp).resolve()
            os.chmod(log, 0o700)
            with patch.object(response, '_v2_phase', side_effect=phase), \
                 patch.object(laya_index_capacity, 'open_pinned_candidate_query',
                              return_value=FakeBoundCandidates()), \
                 patch('event_log.state_root_for_decision', return_value=log), \
                 response._event_scope('recall', 'codex', decision, {}), \
                 patch('sys.stderr', new_callable=io.StringIO) as stderr:
                found, _ = response.query_selected_v2(
                    runtime, 'find card', VECTOR_PROFILE, VECTOR_PROFILE,
                    lambda *_: None, decision)
            self.assertEqual(found, [])
            lines = [row for row in event_log.read(log) if row['event'] == 'recall']
            self.assertEqual([line['notes'] for line in lines], [['laya_wiki_rows_filtered']])
            self.assertEqual([line['reason'] for line in lines], ['laya_wiki_rows_filtered'])
            self.assertEqual(lines[0]['filtered_count'], 2)
            self.assertEqual(lines[0]['filtered_reasons'],
                             {'source_revisions_missing': 1, 'status_not_verified': 1})
            self.assertIn('2 matched wiki cards, none servable', stderr.getvalue())

    def test_v2_filtered_count_excludes_probe_only_rows(self):
        import io
        import laya_index_capacity
        runtime = SimpleNamespace(index=FakeIndex(), scope=SCOPE, source_root=ROOT,
                                  binding_store=object())
        decision = v2_decision()
        decision['recallSettings'] = {**decision['recallSettings'],
                                      'recallStrategy': 'wikiOnly'}
        draft = row(path='wiki/draft.md', body='---\ntitle: Draft\nstatus: generated\n'
                    'createdBy: qmd-auto-context\n---\nbody\n')
        legacy = row(path='wiki/legacy.md', body='---\ntitle: Old\nstatus: verified\n'
                     'createdBy: qmd-auto-context\n---\nbody\n')

        def phase(*args, **kwargs):
            source_filter = args[11]
            source_filter(draft)  # a DF probe row only
            kwargs['diagnostics']['probeOnlyPaths'] = frozenset({draft['path']})
            return [item for item in (legacy,) if source_filter(item)], False
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp).resolve()
            os.chmod(log, 0o700)
            with patch.object(response, '_v2_phase', side_effect=phase), \
                 patch.object(laya_index_capacity, 'open_pinned_candidate_query',
                              return_value=FakeBoundCandidates()), \
                 patch('event_log.state_root_for_decision', return_value=log), \
                 response._event_scope('recall', 'codex', decision, {}), \
                 patch('sys.stderr', new_callable=io.StringIO) as stderr:
                found, _ = response.query_selected_v2(
                    runtime, 'find card', VECTOR_PROFILE, VECTOR_PROFILE,
                    lambda *_: None, decision)
            self.assertEqual(found, [])
            lines = [row for row in event_log.read(log) if row['event'] == 'recall']
            self.assertEqual(lines[0]['filtered_count'], 1)
            self.assertEqual(lines[0]['filtered_reasons'], {'source_revisions_missing': 1})
            self.assertIn('1 matched wiki cards, none servable', stderr.getvalue())

    def test_v2_scaffold_only_wiki_reports_no_match_not_unservable(self):
        # wiki_init scaffold pages stay indexed but are never cards: an empty
        # wiki answers "0 matching documents", not "none servable".
        import io
        import laya_index_capacity
        index = FakeIndex()
        runtime = SimpleNamespace(index=index, scope=SCOPE, source_root=ROOT,
                                  binding_store=object())
        decision = v2_decision()
        decision['recallSettings'] = {**decision['recallSettings'],
                                      'recallStrategy': 'wikiOnly'}
        scaffold = (
            row(path='wiki/SCHEMA.md', body='# Auto-context Wiki Schema\n\n'
                'This wiki stores promoted, durable project knowledge.\n'),
            row(path='wiki/index.md', body='# Auto-context Wiki Index\n\n- decisions/\n'),
            row(path='wiki/log.md', body='# Auto-context Wiki Log\n\n'
                'Append notable wiki maintenance events here.\n'))

        def phase(*args, **kwargs):
            source_filter = args[11]
            return [item for item in scaffold if source_filter(item)], False
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp).resolve()
            os.chmod(log, 0o700)
            with patch.object(response, '_v2_phase', side_effect=phase), \
                 patch.object(laya_index_capacity, 'open_pinned_candidate_query',
                              return_value=FakeBoundCandidates()), \
                 patch('event_log.state_root_for_decision', return_value=log), \
                 response._event_scope('recall', 'codex', decision, {}), \
                 patch('sys.stderr', new_callable=io.StringIO) as stderr:
                found, _ = response.query_selected_v2(
                    runtime, 'find card', VECTOR_PROFILE, VECTOR_PROFILE,
                    lambda *_: None, decision)
            self.assertEqual(found, [])
            self.assertNotIn('none servable', stderr.getvalue())
            self.assertNotIn('laya_wiki_rows_filtered',
                             [note for row in event_log.read(log)
                              for note in row.get('notes') or []])
        self.assertIn('search completed; 0 matching documents.',
                      response.render_context(decision, found))

    def test_v2_existing_install_leftover_root_files_still_serve_cards(self):
        # An install published before the wiki became cards-only keeps its
        # root SCHEMA.md/index.md/log.md rows (customised schema, an index that
        # names cards, a log with events). Recall serves the cards beside them,
        # including a nested card that shares a meta-page name.
        import io
        import laya_index_capacity
        import yaml_scalars
        index = FakeIndex()
        runtime = SimpleNamespace(index=index, scope=SCOPE, source_root=ROOT,
                                  binding_store=object())
        decision = v2_decision()
        revision = {'kind': 'file', 'path': 'docs/a.md', 'collection': 'raw',
                    'sha256': 'e' * 64, 'size': 5, 'mtimeNs': 1}

        def card(path, title):
            text = ('---\ntitle: ' + title + '\nstatus: verified\n'
                    'createdBy: qmd-auto-context\nsourceRevisions:\n  - '
                    + json.dumps(revision) + '\nverifiedBodyHash: {digest}\n'
                    '---\n## Summary\nstarship archive fact.\n')
            digest = yaml_scalars.card_body_hash(text.replace('{digest}', '0' * 64))
            return row(path=path, body=text.replace('{digest}', digest))
        leftovers = (
            row(path='wiki/SCHEMA.md', body='# Auto-context Wiki Schema\n\nThis wiki '
                'stores generated auxiliary knowledge. Generated cards must stay '
                '`status: generated` and include qmd provenance.\n'),
            row(path='wiki/index.md', body='# Auto-context Wiki Index\n\n'
                '- concepts/keep.md - Keep\n- concepts/index.md - Nested\n'),
            row(path='wiki/log.md', body='# Auto-context Wiki Log\n\n'
                '- 2026-10-01T00:00:00Z created concepts/keep.md - Keep\n'))
        cards = (card('wiki/concepts/keep.md', 'Keep'),
                 card('wiki/concepts/index.md', 'Nested'))

        def phase(*args, **kwargs):
            source_filter = args[11]
            return [item for item in leftovers + cards if source_filter(item)], True
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp).resolve()
            os.chmod(log, 0o700)
            with patch.object(response, '_v2_phase', side_effect=phase), \
                 patch.object(laya_index_capacity, 'open_pinned_candidate_query',
                              return_value=FakeBoundCandidates()), \
                 patch('event_log.state_root_for_decision', return_value=log), \
                 response._event_scope('recall', 'codex', decision, {}), \
                 patch('sys.stderr', new_callable=io.StringIO) as stderr:
                found, _ = response.query_selected_v2(
                    runtime, 'starship archive', VECTOR_PROFILE, VECTOR_PROFILE,
                    lambda *_: None, decision)
            self.assertEqual([item['path'] for item in found],
                             ['wiki/concepts/keep.md', 'wiki/concepts/index.md'])
            self.assertNotIn('none servable', stderr.getvalue())

    def test_v2_wiki_only_setting_uses_only_wiki_and_checks_sources(self):
        import laya_index_capacity
        import wiki_freshness
        index = FakeIndex()
        runtime = SimpleNamespace(index=index, scope=SCOPE, source_root=ROOT,
                                  binding_store=object())
        decision = v2_decision()
        decision['recallSettings'] = {**decision['recallSettings'],
                                      'recallStrategy': 'wikiOnly',
                                      'skipPaths': ['build']}
        with patch.object(response, '_v2_phase', return_value=([], False)) as phase, \
             patch.object(laya_index_capacity, 'open_pinned_candidate_query',
                          return_value=FakeBoundCandidates()), \
             patch.object(wiki_freshness, 'unresolved_pending_refreshes_strict',
                          return_value=[]):
            found, title_only = response.query_selected_v2(
                runtime, 'find card', VECTOR_PROFILE, VECTOR_PROFILE,
                lambda *_: None, decision)
        self.assertEqual((found, title_only), ([], frozenset()))
        self.assertEqual(phase.call_args.args[5], {'wiki': '/synthetic/project/wiki'})
        self.assertEqual(phase.call_args.args[6], ('wiki',))
        self.assertEqual(phase.call_args.args[9], ())
        self.assertTrue(index.released)

    def test_v2_excludes_source_directories_without_matching_wiki_names(self):
        import yaml_scalars
        from laya_source_exclusion import excluded_source
        self.assertTrue(excluded_source('docs/build/a.md', ['build']))
        self.assertTrue(excluded_source('docs/team-workspace/a.md', ['-workspace']))
        self.assertFalse(excluded_source('docs/build-notes.md', ['build']))
        self.assertFalse(excluded_source('docs/a.md', []))
        with tempfile.TemporaryDirectory(prefix='laya-source-exclusion-') as temp:
            project = Path(temp).resolve()
            (project / 'docs' / 'build').mkdir(parents=True)
            (project / 'wiki').mkdir()
            source = project / 'docs' / 'build' / 'a.md'
            source.write_text('original', encoding='utf-8')
            revision = {'kind': 'file', 'path': 'docs/build/a.md', 'collection': 'raw',
                        'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                        'size': source.stat().st_size,
                        'mtimeNs': source.stat().st_mtime_ns}
            decision = v2_decision(str(project))
            frontmatter = ('---\ntitle: Synthetic\nstatus: verified\n'
                           'createdBy: qmd-auto-context\nsourceRevisions:\n  - '
                           + json.dumps(revision) + '\nverifiedBodyHash: {digest}\n'
                           '---\n## Summary\nSource fact.\n')
            digest = yaml_scalars.card_body_hash(frontmatter.replace('{digest}', '0' * 64))
            card = row(path='wiki/keep.md', body=frontmatter.replace('{digest}', digest))
            self.assertTrue(response._verified_wiki_row(card, decision))
            self.assertFalse(response._verified_wiki_row(
                card, decision, source_skip_paths=('build',)))

    def test_v2_refills_before_fusion_and_reports_exclusion_cap(self):
        import keywords
        import laya_index_source_port as source_port
        runtime = SimpleNamespace(index=FakeIndex())
        seal = (SCOPE, 'generation-7', 'manifest')
        excluded = [row(path=f'wiki/excluded-{n}.md') for n in range(20)]
        keep = row(path='wiki/keep.md')
        with patch.object(source_port, '_seal', return_value=seal), \
             patch.object(source_port, 'scan_collections',
                          return_value=SimpleNamespace(manifest='manifest')), \
             patch.object(keywords, 'build_lexical_terms',
                          return_value={'generalTerms': [], 'epTerms': [],
                                        'identifiers': []}), \
             patch.object(response, '_pinned_vector', return_value=excluded + [keep]) as vector:
            found, _ = response._v2_phase(
                runtime, 'token', 'find card', VECTOR_PROFILE,
                lambda *_: None, {'wiki': '/synthetic/wiki'},
                ('wiki',), (), 0, (), source_filter=lambda item: item['path'] == keep['path'])
            self.assertEqual([item['path'] for item in found], ['wiki/keep.md'])
            self.assertEqual(vector.call_args.args[5], response.EXCLUSION_FETCH_LIMIT)
        # The internal RRF list may request 20 rows, while the host renders
        # only three. A full top-100 with twelve proved cards is usable.
        keepers = [row(path=f'wiki/keep-{n}.md') for n in range(12)]
        full = [row(path=f'wiki/excluded-{n}.md') for n in range(88)] + keepers
        with patch.object(source_port, '_seal', return_value=seal), \
             patch.object(source_port, 'scan_collections',
                          return_value=SimpleNamespace(manifest='manifest')), \
             patch.object(keywords, 'build_lexical_terms',
                          return_value={'generalTerms': [], 'epTerms': [],
                                        'identifiers': []}), \
             patch.object(response, '_pinned_vector', return_value=full):
            found, _ = response._v2_phase(
                runtime, 'token', 'find card', VECTOR_PROFILE,
                lambda *_: None, {'wiki': '/synthetic/wiki'},
                ('wiki',), (), 0, (),
                source_filter=lambda item: item['path'].startswith('wiki/keep-'),
                needed_eligible=3)
            self.assertEqual(len(found), 12)
        with patch.object(source_port, '_seal', return_value=seal), \
             patch.object(source_port, 'scan_collections',
                          return_value=SimpleNamespace(manifest='manifest')), \
             patch.object(keywords, 'build_lexical_terms',
                          return_value={'generalTerms': [], 'epTerms': [],
                                        'identifiers': []}), \
             patch.object(response, '_pinned_vector', return_value=[
                 row(path=f'wiki/excluded-{n}.md') for n in range(100)]):
            # A full top-100 with no proved card is capped, never refused:
            # search answers with what it proved (here nothing).
            capped = []
            found, _ = response._v2_phase(
                runtime, 'token', 'find card', VECTOR_PROFILE,
                lambda *_: None, {'wiki': '/synthetic/wiki'},
                ('wiki',), (), 0, (), source_filter=lambda _item: False,
                on_capped=capped.append)
            self.assertEqual(found, [])
            self.assertEqual(capped, ['vector'])
        # Full top-100 with two proved cards and topN 5: both are served.
        sparse = ([row(path=f'wiki/excluded-{n}.md') for n in range(98)]
                  + [row(path='wiki/keep-a.md'), row(path='wiki/keep-b.md')])
        with patch.object(source_port, '_seal', return_value=seal), \
             patch.object(source_port, 'scan_collections',
                          return_value=SimpleNamespace(manifest='manifest')), \
             patch.object(keywords, 'build_lexical_terms',
                          return_value={'generalTerms': [], 'epTerms': [],
                                        'identifiers': []}), \
             patch.object(response, '_pinned_vector', return_value=sparse):
            found, _ = response._v2_phase(
                runtime, 'token', 'find card', VECTOR_PROFILE,
                lambda *_: None, {'wiki': '/synthetic/wiki'},
                ('wiki',), (), 0, (),
                source_filter=lambda item: item['path'].startswith('wiki/keep-'))
            self.assertEqual(sorted(item['path'] for item in found),
                             ['wiki/keep-a.md', 'wiki/keep-b.md'])

    def _lexical_only_phase(self, index, source_filter, diagnostics=None,
                            general=('alpha', 'beta', 'gamma')):
        """Real ``_pinned_vector`` with a provider that is unavailable."""
        import keywords
        import laya_index_source_port as source_port
        import laya_index_vector

        class Pinned:
            def __init__(self, _index):
                pass

            def profile_for_pin(self, _token):
                return sealed_profile()

        def unavailable(_prompt, _profile):
            raise response._VectorUnavailable('selected_bundle_drift_provider_unavailable')
        runtime = SimpleNamespace(index=index, scope=SCOPE)
        with patch.object(source_port, '_seal', return_value=(SCOPE, 'generation-7', 'manifest')), \
             patch.object(source_port, 'scan_collections',
                          return_value=SimpleNamespace(manifest='manifest')), \
             patch.object(source_port, '_verify_row', side_effect=lambda r, *_a: r), \
             patch.object(keywords, 'build_lexical_terms',
                          return_value={'generalTerms': list(general), 'epTerms': [],
                                        'identifiers': []}), \
             patch.object(laya_index_vector, 'LayaVectorIndex', Pinned):
            return response._v2_phase(
                runtime, 'token', 'alpha beta gamma', VECTOR_PROFILE, unavailable,
                {'wiki': '/synthetic/wiki'}, ('wiki',), (), 0, (),
                source_filter=source_filter,
                **({} if diagnostics is None else {'diagnostics': diagnostics}))

    def test_v2_lexical_only_floor_serves_verified_term_matches(self):
        # Bundle drift / cold provider: the strict AND query of a natural
        # prompt matches nothing, while each term matches verified cards.
        by_term = {'"alpha"*': [row(path='wiki/a.md'), row(path='wiki/draft.md')],
                   '"beta"*': [row(path='wiki/a.md'), row(path='wiki/b.md')],
                   '"gamma"*': []}

        class TermIndex(FakeIndex):
            calls = []

            def search_many(self, token, queries, limit, **_kw):
                self.calls.append(queries)
                return [[] if ' AND ' in query else list(by_term.get(query, []))
                        for query in queries]
        index = TermIndex()
        found, evidence = self._lexical_only_phase(
            index, lambda item: item['path'] != 'wiki/draft.md')
        self.assertEqual([item['path'] for item in found], ['wiki/a.md', 'wiki/b.md'])
        self.assertFalse(evidence)  # the floor is never body evidence
        self.assertEqual(len(index.calls), 2)  # probe lists reused, no extra SQL
        # Fused probe rows are candidates: a rejected one counts as matched.
        diagnostics = {}
        self._lexical_only_phase(TermIndex(), lambda item: item['path'] != 'wiki/draft.md',
                                 diagnostics)
        self.assertEqual(diagnostics['probeOnlyPaths'], frozenset())

    def test_v2_lexical_only_floor_uses_one_or_query_after_probe_deadline(self):
        from laya_index_a import IndexContractError

        class TimedIndex(FakeIndex):
            calls = []

            def search_many(self, token, queries, limit, **kwargs):
                self.calls.append((queries, kwargs['deadline_seconds']))
                if len(self.calls) == 1:
                    raise IndexContractError('fts_deadline_exceeded')
                return [[row(path='wiki/a.md')] if ' OR ' in query else []
                        for query in queries]
        index = TimedIndex()
        found, evidence = self._lexical_only_phase(index, lambda _item: True)
        self.assertEqual([item['path'] for item in found], ['wiki/a.md'])
        self.assertFalse(evidence)
        self.assertEqual(index.calls[-1], (('"alpha"* OR "beta"* OR "gamma"*',), 2.0))

    def test_v2_probe_only_rejections_are_not_counted_as_matches(self):
        import keywords
        import laya_index_source_port as source_port

        class ProbeIndex(FakeIndex):
            def search_many(self, token, queries, limit, **_kw):
                return [[] if ' AND ' in query else [row(path='wiki/index.md')]
                        for query in queries]
        diagnostics = {}
        with patch.object(source_port, '_seal', return_value=(SCOPE, 'generation-7', 'manifest')), \
             patch.object(source_port, 'scan_collections',
                          return_value=SimpleNamespace(manifest='manifest')), \
             patch.object(source_port, '_verify_row', side_effect=lambda r, *_a: r), \
             patch.object(keywords, 'build_lexical_terms',
                          return_value={'generalTerms': ['alpha', 'beta'], 'epTerms': [],
                                        'identifiers': []}), \
             patch.object(response, '_pinned_vector', return_value=[]):
            found, _ = response._v2_phase(
                SimpleNamespace(index=ProbeIndex()), 'token', 'alpha beta',
                VECTOR_PROFILE, lambda *_: None, {'wiki': '/synthetic/wiki'},
                ('wiki',), (), 0, (), source_filter=lambda _item: False,
                diagnostics=diagnostics)
        self.assertEqual(found, [])
        self.assertEqual(diagnostics['probeOnlyPaths'], frozenset({'wiki/index.md'}))

    def test_v2_lexical_full_page_with_few_proved_rows_is_capped(self):
        import keywords
        import laya_index_source_port as source_port
        seal = (SCOPE, 'generation-7', 'manifest')
        page = ([row(path=f'wiki/excluded-{n}.md') for n in range(99)]
                + [row(path='wiki/keep.md')])

        class FullPageIndex(FakeIndex):
            def search_many(self, token, queries, limit, **_kw):
                return [page[:limit] for _query in queries]
        runtime = SimpleNamespace(index=FullPageIndex())
        capped = []
        with patch.object(source_port, '_seal', return_value=seal), \
             patch.object(source_port, 'scan_collections',
                          return_value=SimpleNamespace(manifest='manifest')), \
             patch.object(source_port, '_verify_row', side_effect=lambda r, *_a: r), \
             patch.object(keywords, 'build_lexical_terms',
                          return_value={'generalTerms': ['card'], 'epTerms': [],
                                        'identifiers': []}), \
             patch.object(response, '_pinned_vector', return_value=[]):
            found, _ = response._v2_phase(
                runtime, 'token', 'find card', VECTOR_PROFILE,
                lambda *_: None, {'wiki': '/synthetic/wiki'},
                ('wiki',), (), 0, (),
                source_filter=lambda item: item['path'] == 'wiki/keep.md',
                on_capped=capped.append)
        self.assertEqual([item['path'] for item in found], ['wiki/keep.md'])
        self.assertIn('lexical', capped)

    def test_v2_card_without_body_digest_is_served_like_qmd_reader(self):
        import yaml_scalars
        with tempfile.TemporaryDirectory(prefix='laya-body-digest-') as temp:
            project, revision, decision = self.span_project(temp)
            base = ('---\ntitle: Synthetic card\nstatus: verified\n'
                    'createdBy: qmd-auto-context\nsourceRevisions:\n  - '
                    + json.dumps(revision) + '\n{digest_line}---\n## Summary\nSource fact.\n')
            absent = base.replace('{digest_line}', '')
            self.assertTrue(response._verified_wiki_row(
                row(path='wiki/card.md', body=absent), decision, current_source=False))
            empty = base.replace('{digest_line}', 'verifiedBodyHash: ""\n')
            self.assertTrue(response._verified_wiki_row(
                row(path='wiki/card.md', body=empty), decision, current_source=False))
            bound = base.replace('{digest_line}', 'verifiedBodyHash: {digest}\n')
            digest = yaml_scalars.card_body_hash(bound.replace('{digest}', '0' * 64))
            good = bound.replace('{digest}', digest)
            self.assertTrue(response._verified_wiki_row(
                row(path='wiki/card.md', body=good), decision, current_source=False))
            # A present digest still binds the bytes.
            for forged in (good.replace('Source fact.', 'Forged fact.'),
                           bound.replace('{digest}', '0' * 64),
                           bound.replace('{digest}', 'not-a-digest')):
                self.assertFalse(response._verified_wiki_row(
                    row(path='wiki/card.md', body=forged), decision, current_source=False))
            # Provenance is still required: no sourceRevisions, no card.
            legacy = ('---\ntitle: Legacy\nstatus: verified\n'
                      'createdBy: qmd-auto-context\n---\n## Summary\nOld fact.\n')
            self.assertFalse(response._verified_wiki_row(
                row(path='wiki/card.md', body=legacy), decision, current_source=False))

    # ── verifier-cited source spans on selected v2 wiki cards ──────────────────
    # Expected slices are literal bytes of SPAN_SOURCE, not recomputed by the
    # module under test.
    SPAN_SOURCE = b'alpha\nport is 8080\nomega\n'

    def span_project(self, temp):
        project = Path(temp).resolve()
        (project / 'docs').mkdir()
        (project / 'wiki').mkdir()
        source = project / 'docs' / 'a.md'
        source.write_bytes(self.SPAN_SOURCE)
        revision = {'kind': 'file', 'path': 'docs/a.md', 'collection': 'raw',
                    'sha256': hashlib.sha256(self.SPAN_SOURCE).hexdigest(),
                    'size': source.stat().st_size, 'mtimeNs': source.stat().st_mtime_ns}
        return project, revision, v2_decision(str(project))

    @staticmethod
    def span_entry(path, source_sha, start, end, slice_bytes, start_token=None):
        return ('  - {path: "%s", sourceSha256: "%s", startLine: %s, endLine: %d, '
                'spanSha256: "%s"}\n' % (path, source_sha, start_token or start, end,
                                         hashlib.sha256(slice_bytes).hexdigest()))

    @staticmethod
    def span_card(revision, spans_text=''):
        import yaml_scalars
        frontmatter = ('---\ntitle: Synthetic card\nstatus: verified\n'
                       'createdBy: qmd-auto-context\nsourceRevisions:\n  - '
                       + json.dumps(revision) + '\n' + spans_text
                       + 'verifiedBodyHash: {digest}\n---\n## Summary\nSource fact.\n')
        digest = yaml_scalars.card_body_hash(frontmatter.replace('{digest}', '0' * 64))
        return row(path='wiki/card.md', body=frontmatter.replace('{digest}', digest))

    def test_v2_wiki_source_spans_are_checked_against_current_bytes(self):
        with tempfile.TemporaryDirectory(prefix='laya-span-ok-') as temp:
            project, revision, decision = self.span_project(temp)
            sha = revision['sha256']
            single = 'sourceSpans:\n' + self.span_entry('docs/a.md', sha, 2, 2, b'port is 8080\n')
            multi = 'sourceSpans:\n' + self.span_entry('docs/a.md', sha, 2, 3,
                                                       b'port is 8080\nomega\n')
            for spans_text, expected in ((single, ('docs/a.md#L2-L2',)),
                                         (multi, ('docs/a.md#L2-L3',))):
                located = {}
                self.assertTrue(response._verified_wiki_row(
                    self.span_card(revision, spans_text), decision, frozenset(), located))
                self.assertEqual(located, {'wiki/card.md': expected})
            # Legacy card without the key: accepted, file-level only.
            located = {}
            self.assertTrue(response._verified_wiki_row(
                self.span_card(revision), decision, frozenset(), located))
            self.assertEqual(located, {})
            # Same line bytes, but the revision moved: the whole card is stale.
            (project / 'docs' / 'a.md').write_bytes(b'new\n' + self.SPAN_SOURCE)
            self.assertFalse(response._verified_wiki_row(
                self.span_card(revision, single), decision))

    def test_v2_wiki_bad_or_stale_source_spans_reject_the_card(self):
        with tempfile.TemporaryDirectory(prefix='laya-span-bad-') as temp:
            _project, revision, decision = self.span_project(temp)
            sha = revision['sha256']
            good = self.span_entry('docs/a.md', sha, 2, 2, b'port is 8080\n')
            cases = {
                'wrong_slice_hash': self.span_entry('docs/a.md', sha, 2, 2, b'alpha\n'),
                'past_last_line': self.span_entry('docs/a.md', sha, 4, 4, b''),
                'path_not_a_revision': self.span_entry('docs/b.md', sha, 2, 2, b'port is 8080\n'),
                'other_revision_sha': self.span_entry('docs/a.md', 'c' * 64, 2, 2,
                                                      b'port is 8080\n'),
                'quoted_line_number': self.span_entry('docs/a.md', sha, 2, 2, b'port is 8080\n',
                                                      start_token='"2"'),
                'duplicate_entry': good + good,
            }
            for name, entries in cases.items():
                with self.subTest(name=name):
                    located = {}
                    self.assertFalse(response._verified_wiki_row(
                        self.span_card(revision, 'sourceSpans:\n' + entries),
                        decision, frozenset(), located))
                    self.assertEqual(located, {})
            for name, text in {'duplicate_header': 'sourceSpans:\n' + good + 'sourceSpans:\n' + good,
                               'inline_value': 'sourceSpans: []\n',
                               'empty_list': 'sourceSpans:\n'}.items():
                with self.subTest(name=name):
                    self.assertFalse(response._verified_wiki_row(
                        self.span_card(revision, text), decision))

    def test_v2_source_locations_follow_returned_wiki_rows_only(self):
        import laya_index_capacity
        import wiki_freshness
        with tempfile.TemporaryDirectory(prefix='laya-span-query-') as temp:
            _project, revision, decision = self.span_project(temp)
            good = self.span_card(revision, 'sourceSpans:\n' + self.span_entry(
                'docs/a.md', revision['sha256'], 2, 2, b'port is 8080\n'))
            tampered = self.span_card(revision, 'sourceSpans: []\n')
            for card, expect_path, expect_locations in (
                    (good, 'wiki/card.md', {'wiki/card.md': ('docs/a.md#L2-L2',)}),
                    (tampered, None, {})):
                with self.subTest(expect=expect_path):
                    runtime = SimpleNamespace(index=FakeIndex(), scope=SCOPE,
                                              source_root=ROOT, binding_store=object())
                    located = {}
                    with patch.object(response, '_v2_phase', side_effect=[
                            ([card], True), ([row(path='docs/a.md')], True)]), \
                         patch.object(laya_index_capacity, 'open_pinned_candidate_query',
                                      return_value=FakeBoundCandidates()), \
                         patch.object(wiki_freshness, 'unresolved_pending_refreshes_strict',
                                      return_value=[]):
                        found, _title_only = response.query_selected_v2(
                            runtime, 'find port', VECTOR_PROFILE, VECTOR_PROFILE,
                            lambda *_: None, decision, source_locations=located)
                    self.assertEqual([item['path'] for item in found],
                                     [expect_path] if expect_path else [])
                    self.assertEqual(located, expect_locations)

    def test_render_source_locations_are_metadata_not_quoted_body(self):
        with tempfile.TemporaryDirectory(prefix='laya-span-render-') as temp:
            _project, revision, decision = self.span_project(temp)
            card = self.span_card(revision)
            raw_row = row(path='docs/a.md', body='port is 8080\n')
            text = response.render_context(
                decision, [card, raw_row], ranking='hybrid_rrf',
                source_locations={'wiki/card.md': ('docs/a.md#L2-L2',)})
            self.assertIn('  | title: ', text)
            self.assertNotIn('  | port is 8080', text)
            lines = text.split('\n')
            meta = ('  source lines cited by verifier (excerpt locations, not per-claim proof): '
                    '"docs/a.md#L2-L2"')
            self.assertIn(meta, lines)
            self.assertEqual(lines[lines.index(meta) - 1], '  | title: Synthetic card')
            self.assertFalse(any(line.startswith('  | ') and '#L2' in line for line in lines))
            many = tuple(f'docs/a.md#L{n}-L{n}' for n in range(1, 6))
            self.assertIn('"docs/a.md#L3-L3" (+2 more)', response.render_context(
                decision, [card], source_locations={'wiki/card.md': many}))
            self.assertEqual(response.render_context(decision, [card]),
                             response.render_context(decision, [card], source_locations={}))
            bad = {
                'raw_row': ([raw_row], {'docs/a.md': ('docs/a.md#L2-L2',)}),
                'absent_row': ([card], {'wiki/other.md': ('docs/a.md#L2-L2',)}),
                'no_rows': ([], {'wiki/card.md': ('docs/a.md#L2-L2',)}),
                'quote_in_path': ([card], {'wiki/card.md': ('docs/a"b.md#L1-L1',)}),
                'newline_in_path': ([card], {'wiki/card.md': ('docs/a\nb.md#L1-L1',)}),
                'reversed': ([card], {'wiki/card.md': ('docs/a.md#L3-L2',)}),
                'absolute': ([card], {'wiki/card.md': ('/etc/a.md#L1-L1',)}),
                'list_value': ([card], {'wiki/card.md': ['docs/a.md#L2-L2']}),
                'empty_value': ([card], {'wiki/card.md': ()}),
            }
            for name, (rows, locations) in bad.items():
                with self.subTest(name=name):
                    with self.assertRaises(response.HostResponseError) as caught:
                        response.render_context(decision, rows, source_locations=locations)
                    self.assertEqual(str(caught.exception), 'invalid_source_locations')

    def test_raw_fallback_keeps_locator_and_title_but_never_quotes_body(self):
        with tempfile.TemporaryDirectory(prefix='laya-raw-locator-') as temp:
            _project, _revision, decision = self.span_project(temp)
            raw_only = row(path='docs/a.md', body='ALPHA_RAW_ONLY secret fact\n')
            rendered = response.render_context(decision, [raw_only])
            self.assertIn('docs/a.md', rendered)
            self.assertIn('  | title: ', rendered)
            self.assertNotIn('ALPHA_RAW_ONLY', rendered)

    def test_gated_title_only_card_gets_no_source_locator(self):
        with tempfile.TemporaryDirectory(prefix='laya-span-gated-') as temp:
            _project, revision, decision = self.span_project(temp)
            card = self.span_card(revision)
            text = response.render_context(
                decision, [card], ranking='hybrid_rrf',
                title_only_paths=frozenset({'wiki/card.md'}),
                source_locations={'wiki/card.md': ('docs/a.md#L2-L2',)})
            self.assertIn('  | title: Synthetic card', text.split('\n'))
            self.assertNotIn('docs/a.md#L2', text)
            self.assertNotIn('source lines cited by verifier', text)

    def test_render_drops_every_location_rather_than_exceed_context_budget(self):
        with tempfile.TemporaryDirectory(prefix='laya-span-budget-') as temp:
            _project, revision, decision = self.span_project(temp)
            card = self.span_card(revision)
            plain = response.render_context(decision, [card])
            budget = len(plain.encode('utf-8'))
            with patch.object(response, 'MAX_CONTEXT_BYTES', budget):
                self.assertEqual(response.render_context(
                    decision, [card], source_locations={'wiki/card.md': ('docs/a.md#L2-L2',)}),
                    plain)
            with patch.object(response, 'MAX_CONTEXT_BYTES', budget - 1):
                with self.assertRaises(response.HostResponseError) as caught:
                    response.render_context(
                        decision, [card], source_locations={'wiki/card.md': ('docs/a.md#L2-L2',)})
                self.assertEqual(str(caught.exception), 'result_context_too_large')

    def test_fts_atom_matches_qmd_compound_and_cjk_policy(self):
        self.assertEqual(response._fts_atom('PIO-1384'), '"pio 1384"')
        self.assertEqual(response._fts_atom('한국어'), '"한 국 어"')
        self.assertEqual(response._fts_atom('target'), '"target"*')

    def test_v2_df_probes_share_one_batch_and_source_scan(self):
        import keywords
        import laya_index_source_port as source_port

        class BatchedIndex:
            calls = []

            def search_many(self, token, queries, limit, **kwargs):
                self.calls.append((queries, limit, kwargs))
                return [[] for _ in queries]

        index = BatchedIndex()
        runtime = SimpleNamespace(index=index)
        seal = (SCOPE, 'generation-7', 'manifest')
        built = {'generalTerms': ['alpha', 'beta', 'gamma', 'delta', 'epsilon', 'zeta'],
                 'epTerms': [], 'identifiers': []}
        with patch.object(source_port, '_seal', return_value=seal), \
                patch.object(source_port, 'scan_collections',
                             return_value=SimpleNamespace(manifest='manifest')) as scan, \
                patch.object(keywords, 'build_lexical_terms', return_value=built), \
                patch.object(response, '_pinned_vector', return_value=[]):
            rows, evidence = response._v2_phase(
                runtime, 'token', 'alpha beta gamma', VECTOR_PROFILE,
                lambda *_: None, {'raw': ROOT}, ('raw',), (), 0, ())
        self.assertEqual((rows, evidence), ([], False))
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(len(index.calls), 2)
        self.assertEqual(len(index.calls[0][0]), 6)
        self.assertEqual(index.calls[0][1], 1)
        self.assertEqual(index.calls[0][2]['path_prefixes'], ('raw/',))
        self.assertLessEqual(index.calls[0][2]['deadline_seconds'], 1)
        self.assertEqual(index.calls[1][2]['deadline_seconds'], 2.0)

    def test_v2_df_timeout_discards_probe_batch_and_primary_timeout_fails(self):
        import keywords
        import laya_index_source_port as source_port
        from laya_index_a import IndexContractError

        class TimedIndex:
            calls = []
            fail_primary = False

            def search_many(self, token, queries, limit, **kwargs):
                self.calls.append(queries)
                if len(self.calls) == 1 or self.fail_primary:
                    raise IndexContractError('fts_deadline_exceeded')
                return [[] for _ in queries]

        index = TimedIndex()
        runtime = SimpleNamespace(index=index)
        built = {'generalTerms': ['alpha', 'beta', 'gamma', 'delta'],
                 'epTerms': [], 'identifiers': []}
        with patch.object(source_port, '_seal', return_value=(SCOPE, 'generation-7', 'manifest')), \
                patch.object(source_port, 'scan_collections',
                             return_value=SimpleNamespace(manifest='manifest')), \
                patch.object(keywords, 'build_lexical_terms', return_value=built), \
                patch.object(response, '_pinned_vector', return_value=[]):
            response._v2_phase(runtime, 'token', 'alpha beta gamma', VECTOR_PROFILE,
                               lambda *_: None, {'raw': ROOT}, ('raw',), (), 0, ())
            # keywords.select_general_terms_explained(None) is the same
            # positional fallback used by legacy QMD recall on DF failure.
            self.assertEqual(index.calls[1], ('"alpha"* AND "beta"* AND "gamma"*',))
            index.calls.clear()
            index.fail_primary = True
            with self.assertRaisesRegex(IndexContractError, '^fts_deadline_exceeded$'):
                response._v2_phase(runtime, 'token', 'alpha beta gamma', VECTOR_PROFILE,
                                   lambda *_: None, {'raw': ROOT}, ('raw',), (), 0, ())

    def test_only_completed_scan_vs_seal_mismatch_is_wakeable_stale(self):
        import laya_index_source_port as source_port
        from laya_index_a import IndexContractError
        runtime = SimpleNamespace(index=FakeIndex())
        with patch.object(source_port, '_seal',
                          return_value=(SCOPE, 'generation-7', 'sealed-manifest')), \
             patch.object(source_port, 'scan_collections',
                          return_value=SimpleNamespace(manifest='live-manifest')):
            with self.assertRaises(IndexContractError) as caught:
                response._v2_phase(runtime, 'token', 'alpha', VECTOR_PROFILE,
                                   lambda *_: None, {'raw': ROOT}, ('raw',), (), 0, ())
        # The outward contract stays index_stale; only the cause is marked.
        self.assertEqual(str(caught.exception), 'index_stale')
        self.assertTrue(response._is_sealed_manifest_stale(caught.exception))
        others = []
        for detail in ('too many files', 'oversize: raw/a.md',
                       'changed during read: raw/a.md', 'malformed view seal',
                       'search row does not match source',
                       'collection manifest differs from view seal'):
            try:
                source_port._raise_stale(detail)
            except IndexContractError as exc:
                others.append((detail, exc))
        others += [('no cause', IndexContractError('index_stale')),
                   ('fts', IndexContractError('fts_deadline_exceeded')),
                   ('host', response.HostResponseError('index_stale'))]
        for name, exc in others:
            with self.subTest(case=name):
                self.assertFalse(response._is_sealed_manifest_stale(exc))

    def test_v2_stale_recall_queues_sync_without_rows_or_provider(self):
        decision = v2_decision()
        provider_calls = []
        exc, requested, journal, index = serve_v2_recall(
            decision, verify=lambda _d: V2_PROOF,
            scan=lambda *_a, **_k: SimpleNamespace(manifest='live-manifest'),
            request=lambda *_a: {'status': 'queued'}, provider_calls=provider_calls)
        self.assertIsInstance(exc, response.HostResponseError)
        self.assertEqual(str(exc), 'index_stale_sync_queued')
        requested.assert_called_once_with('laya-sync', '/synthetic/project')
        # Initial proof, recheck before the wake, recheck after it.
        self.assertEqual(journal.call_count, 3)
        self.assertEqual(provider_calls, [])
        self.assertTrue(index.released)
        # A running owner may advance the journal epoch before request returns.
        advanced = {**V2_PROOF, 'selectionEpoch': 4}
        exc, requested, _journal, _index = serve_v2_recall(
            decision, verify=[V2_PROOF, V2_PROOF, advanced],
            scan=lambda *_a, **_k: SimpleNamespace(manifest='live-manifest'),
            request=lambda *_a: {'status': 'busy'})
        self.assertEqual(str(exc), 'index_stale_sync_queued')

    def test_v2_stale_recall_reports_unavailable_wake_and_rechecks_proof(self):
        decision = v2_decision()
        live = lambda *_a, **_k: SimpleNamespace(manifest='live-manifest')
        # None is runtime_work.request's fixture/sandbox no-op return.
        for name, request in (('fixture', lambda *_a: None),
                              ('raised', OSError('synthetic queue failure')),
                              ('capacity', lambda *_a: {'status': 'deferred_queue_capacity'})):
            with self.subTest(case=name):
                exc, requested, _journal, _index = serve_v2_recall(
                    decision, verify=lambda _d: V2_PROOF, scan=live, request=request)
                self.assertIsInstance(exc, response.HostResponseError)
                self.assertEqual(str(exc), 'index_stale_sync_unavailable')
                requested.assert_called_once_with('laya-sync', '/synthetic/project')
        exc, requested, _journal, _index = serve_v2_recall(
            decision, verify=lambda _d: V2_PROOF, scan=live,
            request=lambda *_a: {'status': 'deferred_queue_capacity_retained'})
        self.assertEqual(str(exc), 'index_stale_sync_queued')
        requested.assert_called_once_with('laya-sync', '/synthetic/project')
        changed = {**V2_PROOF, 'productRouteAllowed': False}
        exc, requested, _journal, _index = serve_v2_recall(
            decision, verify=[V2_PROOF, changed], scan=live,
            request=lambda *_a: {'status': 'queued'})
        self.assertEqual(str(exc), 'selected_proof_changed')
        requested.assert_not_called()
        regressed = {**V2_PROOF, 'selectionEpoch': 2}
        exc, _requested, _journal, _index = serve_v2_recall(
            decision, verify=[V2_PROOF, V2_PROOF, regressed], scan=live,
            request=lambda *_a: {'status': 'queued'})
        # A journal restore below the recall's highest epoch is never retried.
        self.assertEqual(str(exc), 'selected_proof_rolled_back')

    def test_v2_budget_row_corruption_and_legacy_stale_do_not_wake(self):
        import laya_index_source_port as source_port
        from laya_index_a import IndexContractError
        decision = v2_decision()

        def budget(*_a, **_k):
            source_port._raise_stale('too many files')

        class CorruptRowIndex(FakeIndex):
            def search_many(self, token, queries, limit, **kwargs):
                return [[{'scope': SCOPE, 'generation': 'generation-7',
                          'path': 'wiki/a.md', 'hash': '0' * 64, 'score': 0.1}]
                        for _ in queries]

        cases = {'budget': (budget, None),
                 'row': (lambda *_a, **_k: SimpleNamespace(manifest='sealed-manifest',
                                                           by_path={}),
                         CorruptRowIndex())}
        for name, (scan, index) in cases.items():
            with self.subTest(case=name):
                exc, requested, journal, used = serve_v2_recall(
                    decision, verify=lambda _d: V2_PROOF, scan=scan, index=index,
                    request=lambda *_a: {'status': 'queued'})
                # Unchanged generic failure: main maps it to
                # selected_proof_or_query_failed, never to a sync diagnostic.
                self.assertIs(type(exc), IndexContractError)
                self.assertEqual(str(exc), 'index_stale')
                requested.assert_not_called()
                self.assertEqual(journal.call_count, 1)
                self.assertTrue(used.released)
        # A legacy v1 candidate cannot bypass the required published-wiki selector.
        import laya_install_journal
        import laya_index_runtime
        import laya_local_embed_provider
        import config
        import runtime_work
        from contextlib import nullcontext
        try:
            raise IndexContractError('index_stale') from response._SealedManifestMismatch('x')
        except IndexContractError as marked:
            legacy_stale = marked
        legacy = {key: value for key, value in decision.items()
                  if key not in ('collections', 'collectionRoots', 'recallSettings')}
        runtime = SimpleNamespace(index=FakeIndex(), scope=SCOPE, source_root=ROOT)
        with patch.object(laya_install_journal, 'verify_selected_route',
                          return_value=V2_PROOF), \
             patch.object(laya_index_runtime, 'LayaIndexRuntime',
                          return_value=nullcontext(runtime)), \
             patch.object(laya_local_embed_provider, 'ReviewedLocalQmdProvider'), \
             patch.object(config, 'load_project_config',
                          return_value={'topN': 3, 'lexicalPatterns': []}), \
             patch.object(response, 'query_selected_hybrid', side_effect=legacy_stale), \
             patch.object(runtime_work, 'request') as requested:
            self.assertEqual(response.serve_selected(
                'recall', 'claude', legacy, {'prompt': 'find alpha'}
            )['hookSpecificOutput']['additionalContext'], '')
        requested.assert_not_called()

    def test_repeated_stale_recall_coalesces_in_existing_wake_queue(self):
        import os
        import runtime_work
        decision = v2_decision()
        real_queue = runtime_work.WakeQueue
        with tempfile.TemporaryDirectory(prefix='laya-stale-wake-') as temp:
            state = Path(temp).resolve()
            env = {key: value for key, value in os.environ.items()
                   if key not in ('QMD_SETUP_GUARD_FIXTURE', 'QMD_SANDBOX')}
            env['QMD_RUNTIME_DISPATCH'] = '1'
            with patch.dict(os.environ, env, clear=True), \
                 patch.object(runtime_work, 'WakeQueue',
                              lambda root=None: real_queue(state)), \
                 patch.object(runtime_work, 'wake') as spawned:
                for _ in range(5):
                    exc, _requested, _journal, index = serve_v2_recall(
                        decision, verify=lambda _d: V2_PROOF,
                        scan=lambda *_a, **_k: SimpleNamespace(manifest='live-manifest'))
                    self.assertEqual(str(exc), 'index_stale_sync_queued')
                    self.assertTrue(index.released)
                spawned.assert_not_called()
            wakes = real_queue(state).pending()
        self.assertEqual([(item['kind'], item['target'], item['status'])
                          for item in wakes],
                         [('laya-sync', '/synthetic/project', 'pending')])
        self.assertEqual(wakes[0]['revision'], 5)

    def test_cold_provider_wakes_owner_without_qmd_fallback(self):
        import laya_install_journal
        import laya_embed_resident
        import runtime_work
        decision = v2_decision()
        with patch.object(laya_install_journal, 'verify_selected_route',
                          return_value=V2_PROOF), \
             patch.object(laya_embed_resident, 'read_owner_state', return_value=None), \
             patch.object(runtime_work, 'request', return_value={'status': 'queued'}) as wake:
            with self.assertRaisesRegex(response.HostResponseError,
                                        '^selected_provider_preparing$'):
                response._request_provider_start(decision, V2_PROOF)
        wake.assert_called_once_with('laya-sync', decision['projectRoot'], pull_due=True)

    def test_provider_failure_and_identity_transition_have_distinct_codes(self):
        import laya_install_journal
        import laya_embed_resident
        import runtime_work
        decision = v2_decision()
        state = {'terminal': True, 'reason': 'attestation_invalid'}
        with patch.object(laya_install_journal, 'verify_selected_route',
                          return_value=V2_PROOF), \
             patch.object(laya_embed_resident, 'read_owner_state', return_value=state), \
             patch.object(runtime_work, 'request') as wake:
            with self.assertRaisesRegex(response.HostResponseError,
                                        '^selected_provider_start_failed$'):
                response._request_provider_start(decision, V2_PROOF)
        wake.assert_not_called()
        with patch.object(laya_install_journal, 'verify_selected_route',
                          return_value=V2_PROOF), \
             patch.object(laya_embed_resident, 'read_owner_state', return_value=None), \
             patch.object(runtime_work, 'request', return_value={'status': 'queued'}) as wake:
            with self.assertRaisesRegex(response.HostResponseError,
                                        '^selected_provider_identity_transition$'):
                response._request_provider_start(decision, V2_PROOF,
                                                 transition=True)
        wake.assert_called_once_with('laya-sync', decision['projectRoot'], pull_due=True)

    def test_withdrawn_owner_restarts_from_the_query_path(self):
        # After a bundle upgrade (starting) or lane contention
        # (resource_deferred) the owner has no socket. The query path starts
        # it directly instead of waiting for a backed-off sync wake.
        import time as clock
        import laya_install_journal
        import laya_embed_resident
        import runtime_work
        from laya_index_a import IndexContractError
        from resource_deferred import ResourceDeferred
        decision = v2_decision()
        with tempfile.TemporaryDirectory(prefix='laya-host-owner-') as temp:
            sock = Path(temp) / 'laya-embed.sock'
            proof = {**V2_PROOF, 'providerManifest': {
                **V2_PROOF['providerManifest'], 'serviceSocketPath': str(sock)}}

            def wake(state, *, root=decision['pluginRoot'], transition=False,
                     started=ResourceDeferred('laya_service_preparing'),
                     queued={'status': 'queued'}):
                row = None if state is None else {
                    'fingerprint': 'a' * 64, 'reason': state, 'terminal': False,
                    'at': clock.time() - 60, 'attempts': 0}
                with patch.object(laya_install_journal, 'verify_selected_route',
                                  return_value=proof), \
                     patch.object(laya_embed_resident, 'read_owner_state', return_value=row), \
                     patch.object(laya_embed_resident, 'ensure_running',
                                  side_effect=started) as ensure, \
                     patch.object(runtime_work, 'request', return_value=queued):
                    try:
                        response._request_provider_start({**decision, 'pluginRoot': root},
                                                         proof, transition=transition)
                    except response.HostResponseError as exc:
                        return str(exc), ensure.call_args_list
                self.fail('provider start must raise its code')

            code, calls = wake('starting')
            self.assertEqual(code, 'selected_provider_preparing')
            self.assertEqual(calls, [((decision['projectRoot'], proof['providerManifest']),)])
            code, calls = wake('resource_deferred')
            self.assertEqual((code, len(calls)), ('selected_provider_resource_deferred', 1))
            # A failed wake queue still gets the direct start.
            code, calls = wake('starting', queued=None)
            self.assertEqual((code, len(calls)), ('selected_provider_wake_failed', 1))
            code, calls = wake('starting', started=[None])
            self.assertEqual((code, len(calls)), ('selected_provider_preparing', 1))
            code, calls = wake('starting', started=IndexContractError('provider_start_failed'))
            self.assertEqual(code, 'selected_provider_start_failed')
            code, calls = wake('starting', started=IndexContractError('provider_socket_invalid'))
            self.assertEqual(code, 'selected_provider_state_invalid')
            # Never from another plugin copy: the owner would refuse that root
            # as a terminal identity failure.
            self.assertEqual(wake('starting', root='/other/plugin/root')[1], [])
            # A real failure, an unknown state and a transition keep the sync path.
            self.assertEqual(wake('startup_failed'),
                             ('selected_provider_retry_wait', []))
            self.assertEqual(wake(None), ('selected_provider_preparing', []))
            self.assertEqual(wake('starting', transition=True),
                             ('selected_provider_identity_transition', []))
            sock.write_text('')  # a live or stale socket is the sync path's
            self.assertEqual(wake('starting')[1], [])

    def test_main_reports_stale_sync_codes_with_exit_3_and_no_fallback(self):
        import contextlib
        import io
        from laya_index_a import IndexContractError
        selection = {'state': 'candidate'}
        with tempfile.TemporaryDirectory(prefix='laya-host-main-') as temp:
            payload = Path(temp) / 'payload.json'
            payload.write_text(json.dumps({'prompt': 'find alpha', 'cwd': temp}),
                               encoding='utf-8')
            for raised, code in (
                    (response.HostResponseError('index_stale_sync_queued'),
                     'index_stale_sync_queued'),
                    (response.HostResponseError('index_stale_sync_unavailable'),
                     'index_stale_sync_unavailable'),
                    (response.HostResponseError('selected_provider_preparing'),
                     'selected_provider_preparing'),
                    (IndexContractError('vector_deadline_exceeded'),
                     'vector_deadline_exceeded'),
                    (IndexContractError('vector_budget_exceeded'),
                     'vector_budget_exceeded'),
                    (IndexContractError('index_stale'), 'selected_proof_or_query_failed')):
                with self.subTest(code=code):
                    out, err = io.StringIO(), io.StringIO()
                    with patch.object(response, 'select_hook_route', return_value=selection), \
                         patch.object(response, 'select_route', return_value=selection), \
                         patch.object(response, 'serve_selected', side_effect=raised), \
                         contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        status = response.main(['serve-hook', 'recall', 'claude',
                                                str(payload), temp])
                    self.assertEqual(status, 3)
                    self.assertEqual(out.getvalue(), '')
                    self.assertEqual(err.getvalue(),
                                     f'[memvane] {code}; request refused\n')


class BundleDriftChecks(unittest.TestCase):
    """Host plugin update: read-only published generation, every write fenced."""

    def setUp(self):
        import laya_embed_resident
        admission = patch.object(laya_embed_resident, 'admit_query')
        admission.start()
        self.addCleanup(admission.stop)
        self.decision = {**DECISION,
                         'pluginRoot': '/synthetic/old-claude-cache/0.36.25',
                         'projectRoot': '/synthetic/project',
                         'journalPath': '/synthetic/state/install-journal.sqlite',
                         'dbPath': '/synthetic/index.sqlite',
                         'queuePath': '/synthetic/queue.sqlite',
                         'dbIncarnation': 'a' * 32, 'vectorProfile': VECTOR_PROFILE}
        self.read_only = {'productRouteAllowed': False, 'readOnlyAllowed': True,
                          'selectionEpoch': 9, 'vectorProfile': VECTOR_PROFILE,
                          'providerManifest': {'approvedProviderId': 'synthetic-provider'}}

    def drifted(self, stack, code='executing_code_outside_bundle'):
        import laya_install_journal
        import runtime_work
        full = stack.enter_context(patch.object(
            laya_install_journal, 'verify_selected_route',
            side_effect=laya_install_journal.JournalEvidenceError(code)))
        ro = stack.enter_context(patch.object(
            laya_install_journal, 'verify_selected_route_read_only',
            return_value=self.read_only))
        wake = stack.enter_context(patch.object(runtime_work, 'request'))
        return full, ro, wake

    def test_bundle_mismatch_legacy_route_never_bypasses_required_selection(self):
        from contextlib import ExitStack, nullcontext
        import config
        import laya_index_runtime
        import laya_local_embed_provider
        from laya_index_a import IndexContractError

        def cold_provider(_text, _profile):
            raise IndexContractError('provider_unavailable')

        def hybrid(runtime, prompt, route_profile, approved, provider, **_kw):
            # The published generation's verified lexical list; the vector
            # list is empty because the owner of the old bundle is cold.
            vector = None
            try:
                provider(prompt, route_profile)
            except response._VectorUnavailable:
                vector = []
            self.assertEqual(vector, [])
            return [row()]

        runtime = SimpleNamespace(index=FakeIndex(), scope=SCOPE, source_root=ROOT)
        with ExitStack() as stack:
            _full, ro, wake = self.drifted(stack)
            stack.enter_context(patch.object(laya_index_runtime, 'LayaIndexRuntime',
                                             return_value=nullcontext(runtime)))
            stack.enter_context(patch.object(laya_local_embed_provider,
                                             'ReviewedLocalQmdProvider',
                                             return_value=cold_provider))
            stack.enter_context(patch.object(config, 'load_project_config',
                                             return_value={'topN': 3, 'lexicalPatterns': []}))
            queried = stack.enter_context(patch.object(response, 'query_selected_hybrid',
                                                       side_effect=hybrid))
            payload = response.serve_selected('recall', 'claude', self.decision,
                                              {'prompt': 'find alpha'})
        context = payload['hookSpecificOutput']['additionalContext']
        self.assertEqual(context, '')
        queried.assert_not_called()
        wake.assert_not_called()
        self.assertEqual(ro.call_count, 1)  # read-only proof; no content injected

    def test_degraded_vector_unavailable_yields_empty_vector_list_only(self):
        import laya_index_vector

        class Pinned:
            def __init__(self, _index):
                pass

            def profile_for_pin(self, _token):
                return sealed_profile()

        def unavailable(_prompt, _profile):
            raise response._VectorUnavailable('provider_unavailable')
        runtime = SimpleNamespace(index=FakeIndex(), scope=SCOPE, source_root=ROOT)
        with patch.object(laya_index_vector, 'LayaVectorIndex', Pinned):
            self.assertEqual(response._pinned_vector(runtime, 't', 'q', VECTOR_PROFILE,
                                                     unavailable, 5), [])
            from laya_index_a import IndexContractError

            def broken(_prompt, _profile):
                raise IndexContractError('provider_unavailable')
            with self.assertRaisesRegex(IndexContractError, 'provider_unavailable'):
                response._pinned_vector(runtime, 't', 'q', VECTOR_PROFILE, broken, 5)

    def test_bundle_mismatch_session_start_reports_reason_and_queues_upgrade_only(self):
        from contextlib import ExitStack
        import laya_bundle_upgrade
        with ExitStack() as stack:
            _full, _ro, wake = self.drifted(stack)
            upgrade = stack.enter_context(patch.object(
                laya_bundle_upgrade, 'request', return_value={'status': 'queued'}))
            payload = response.serve_selected('update-queued', 'claude', self.decision,
                                              {'hook_event_name': 'SessionStart'})
        notice = payload['hookSpecificOutput']['additionalContext']
        self.assertEqual(notice.count('\n'), 0)
        self.assertIn('executing_code_outside_bundle', notice)
        self.assertIn('read-only', notice)
        upgrade.assert_called_once_with('/synthetic/project',
                                        '/synthetic/state/install-journal.sqlite',
                                        'executing_code_outside_bundle')
        wake.assert_not_called()
        with ExitStack() as stack:
            self.drifted(stack)
            stack.enter_context(patch.object(
                laya_bundle_upgrade, 'request',
                return_value={'status': 'refused',
                              'reason': 'provider_changed_rebuild_required'}))
            payload = response.serve_selected('update', 'codex', self.decision,
                                              {'hook_event_name': 'SessionStart'})
        self.assertIn('provider_changed_rebuild_required',
                      payload['hookSpecificOutput']['additionalContext'])

    def test_bundle_mismatch_defers_index_compile_and_change_events(self):
        from contextlib import ExitStack
        for action, payload in (('index', {'tool_name': 'Write'}),
                                ('file-change-event', {'tool_name': 'Bash'}),
                                ('posttool', {})):
            with self.subTest(action=action), ExitStack() as stack:
                _full, _ro, wake = self.drifted(stack)
                self.assertEqual(response.serve_selected(action, 'claude', self.decision,
                                                         payload), {})
                wake.assert_not_called()

    def test_non_drift_or_unprovable_read_only_keeps_the_real_refusal(self):
        import laya_install_journal
        from contextlib import ExitStack
        with ExitStack() as stack:
            self.drifted(stack, code='journal_unavailable')
            with self.assertRaisesRegex(laya_install_journal.JournalEvidenceError,
                                        '^journal_unavailable$'):
                response.serve_selected('recall', 'claude', self.decision,
                                        {'prompt': 'find alpha'})
        with ExitStack() as stack:
            self.drifted(stack)
            stack.enter_context(patch.object(
                laya_install_journal, 'verify_selected_route_read_only',
                side_effect=laya_install_journal.JournalEvidenceError(
                    'production_attestation_changed')))
            with self.assertRaisesRegex(laya_install_journal.JournalEvidenceError,
                                        '^executing_code_outside_bundle$'):
                response.serve_selected('recall', 'claude', self.decision,
                                        {'prompt': 'find alpha'})

    def test_main_reports_journal_evidence_code_instead_of_generic_failure(self):
        import contextlib
        import io
        import laya_install_journal
        selection = {'state': 'candidate'}
        with tempfile.TemporaryDirectory(prefix='laya-host-main-') as temp:
            payload = Path(temp) / 'payload.json'
            payload.write_text(json.dumps({'prompt': 'find alpha', 'cwd': temp}),
                               encoding='utf-8')
            out, err = io.StringIO(), io.StringIO()
            raised = laya_install_journal.JournalEvidenceError('executing_bundle_not_selected')
            with patch.object(response, 'select_hook_route', return_value=selection), \
                 patch.object(response, 'select_route', return_value=selection), \
                 patch.object(response, 'serve_selected', side_effect=raised), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                status = response.main(['serve-hook', 'recall', 'claude', str(payload), temp])
        self.assertEqual(status, 3)
        self.assertEqual(err.getvalue(),
                         '[memvane] executing_bundle_not_selected; request refused\n')


class ProofRetryChecks(unittest.TestCase):
    """A proof or view change during one recall (sync publish, upgrade)."""

    LOCAL = str(Path(response.__file__).resolve().parents[1])
    BODY = '---\ntitle: Alpha card\n---\n## Summary\nalpha fact.\n'

    def setUp(self):
        import sqlite3
        import hook_budget
        import laya_embed_resident
        for patcher in (patch.object(laya_embed_resident, 'admit_query'),
                        patch.object(hook_budget, '_deadline', None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        temp = tempfile.TemporaryDirectory(prefix='laya-proof-retry-',
                                           dir=str(Path(tempfile.gettempdir()).resolve()))
        self.addCleanup(temp.cleanup)
        db_path = Path(temp.name).resolve() / 'index.sqlite'
        # The real published-view check reads this head/membership store.
        from contextlib import closing
        with closing(sqlite3.connect(db_path)) as db, db:
            db.executescript(
                'CREATE TABLE head(scope TEXT, gen TEXT);'
                'CREATE TABLE exclusions(scope TEXT, kind TEXT, value TEXT);'
                'CREATE TABLE membership(gen TEXT, scope TEXT, path TEXT, hash TEXT,'
                ' eligible INTEGER);')
            db.execute('INSERT INTO head VALUES (?,?)', (SCOPE, 'generation-7'))
            digest = hashlib.sha256(self.BODY.encode('utf-8')).hexdigest()
            for gen in ('generation-7', 'generation-8', 'generation-9'):
                db.execute('INSERT INTO membership VALUES (?,?,?,?,1)',
                           (gen, SCOPE, 'wiki/a.md', digest))
        db_path.chmod(0o600)
        self.db_path = db_path
        self.decision = {**v2_decision(), 'installId': 'c' * 32,
                         'journalPath': '/synthetic/state/install-journal.sqlite',
                         'generation': 'g-old', 'dbPath': str(db_path)}

    def head(self):
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.db_path)) as db:
            return db.execute('SELECT gen FROM head WHERE scope=?', (SCOPE,)).fetchone()[0]

    def publish(self):
        """A sync owner publishes the next generation."""
        import sqlite3
        nxt = {'generation-7': 'generation-8', 'generation-8': 'generation-9'}[self.head()]
        from contextlib import closing
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute('UPDATE head SET gen=? WHERE scope=?', (nxt, SCOPE))

    def proof(self, epoch, **changes):
        return {'productRouteAllowed': True, 'selectionEpoch': epoch,
                'executingPluginRoot': self.LOCAL, 'installId': 'c' * 32,
                'projectRoot': '/synthetic/project', 'scopeId': SCOPE,
                'dbIncarnation': 'a' * 32, 'bundleManifestSha256': 'd' * 64,
                'vectorProfile': VECTOR_PROFILE,
                'providerManifest': {'approvedProviderId': 'synthetic-provider'},
                **changes}

    def recall(self, *, verify, reselect=None, read_only=None, query=None,
               provider=None, request=None):
        """Run selected recall through the real proof, view and retry path.

        ``query(n)`` runs after the n-th query pinned the current head; a
        ``self.publish()`` there lands between the pin and the final check.
        """
        from contextlib import ExitStack, nullcontext
        import config
        import laya_embed_resident
        import laya_incremental_docs_query
        import laya_install_journal
        import laya_index_runtime
        import laya_local_embed_provider
        import runtime_work
        calls = {'query': []}
        digest = hashlib.sha256(self.BODY.encode('utf-8')).hexdigest()

        def query_v2(runtime, prompt, route_profile, _approved, query_provider, *args,
                     pinned_generation=None, **kwargs):
            head = self.head()
            pinned_generation.append(head)
            calls['query'].append(head)
            try:
                query_provider(prompt, route_profile)
            except response._VectorUnavailable:
                pass
            if query is not None:
                query(len(calls['query']))
            return [row(path='wiki/a.md', generation=head, body=self.BODY,
                        hash=digest)], frozenset()

        def no_read_only(_decision):
            raise AssertionError('read-only proof was not expected')

        runtime = SimpleNamespace(index=FakeIndex(), scope=SCOPE, source_root=ROOT)
        with ExitStack() as stack:
            enter = stack.enter_context
            enter(patch.object(laya_install_journal, 'verify_selected_route',
                               side_effect=verify))
            ro = enter(patch.object(laya_install_journal, 'verify_selected_route_read_only',
                                    side_effect=read_only or no_read_only))
            selected = enter(patch.object(response, 'select_route',
                                          side_effect=reselect or
                                          (lambda *_a, **_k: dict(self.decision))))
            wake = enter(patch.object(runtime_work, 'request',
                                      side_effect=request or
                                      (lambda *_a, **_k: {'status': 'queued'})))
            enter(patch.object(laya_embed_resident, '_failure_decision', return_value=None))
            enter(patch.object(laya_embed_resident, 'read_owner_state', return_value=None))
            enter(patch.object(laya_index_runtime, 'LayaIndexRuntime',
                               return_value=nullcontext(runtime)))
            enter(patch.object(laya_index_runtime, 'capacity_store_paths', return_value=None))
            enter(patch.object(laya_local_embed_provider, 'ReviewedLocalQmdProvider',
                               return_value=provider or (lambda *_: [0.1, 0.2, 0.3])))
            enter(patch.object(config, 'load_project_config',
                               return_value=dict(self.decision['recallSettings'])))
            enter(patch.object(laya_incremental_docs_query, 'query_incremental',
                               return_value=([], frozenset(), {}, (), None)))
            enter(patch.object(response, 'query_selected_v2', side_effect=query_v2))
            enter(patch.object(response, '_select_verified_cards',
                               side_effect=lambda _d, _s, _p, rows: (rows, 'ranked', None)))
            enter(patch('sys.stderr', new_callable=__import__('io').StringIO))
            try:
                result = response.serve_selected('recall', 'codex', dict(self.decision),
                                                 {'prompt': 'find alpha',
                                                  'cwd': '/synthetic/project'})
            except Exception as exc:
                result = exc
        calls['wake'] = wake
        return result, calls, selected, ro

    def served(self, result, head='generation-7'):
        self.assertIsInstance(result, dict, repr(result))
        context = result['hookSpecificOutput']['additionalContext']
        self.assertIn('wiki/a.md', context)
        return context

    def publish_on_first(self, n):
        if n == 1:
            self.publish()

    def test_epoch_advance_by_a_sync_publish_still_serves_verified_cards(self):
        # cas_mark_new_write moves only the journal epoch; the pinned head is
        # still the published one, so this recall serves without a retry.
        proofs = iter([self.proof(3)] + [self.proof(4)] * 10)
        result, calls, selected, _ro = self.recall(verify=lambda _d: next(proofs))
        self.served(result)
        self.assertEqual(calls['query'], ['generation-7'])
        selected.assert_not_called()

    def test_publish_during_recall_retries_once_from_a_fresh_proof(self):
        # The sync wake advanced the epoch and published a new head between
        # the pin and the final view check: one retry re-queries that head.
        proofs = iter([self.proof(3)] * 2 + [self.proof(4)] * 10)
        result, calls, selected, _ro = self.recall(
            verify=lambda _d: next(proofs), query=self.publish_on_first)
        self.served(result)
        self.assertEqual(calls['query'], ['generation-7', 'generation-8'])
        self.assertEqual(selected.call_count, 1)
        self.assertEqual(selected.call_args.args, ('/synthetic/project',))

    def test_upgrade_activation_during_recall_serves_the_new_route(self):
        # The upgrade rewrote the route mid-recall: re-selection yields the
        # same install with a new bundle, proved in full before serving.
        import laya_install_journal
        upgraded = {**self.decision, 'generation': 'g-new', 'pluginRoot': self.LOCAL}
        state = {'calls': 0}

        def verify(decision):
            state['calls'] += 1
            if decision.get('generation') == 'g-new':
                return self.proof(5, bundleManifestSha256='e' * 64)
            if state['calls'] == 1:
                return self.proof(3)
            raise laya_install_journal.JournalEvidenceError('host_route_unverified')
        result, calls, selected, _ro = self.recall(
            verify=verify, reselect=lambda *_a, **_k: dict(upgraded))
        self.served(result)
        self.assertEqual(len(calls['query']), 2)
        self.assertEqual(selected.call_count, 1)

    def test_drifted_bundle_on_retry_serves_the_read_only_published_view(self):
        import laya_install_journal
        state = {'calls': 0}

        def verify(_decision):
            state['calls'] += 1
            if state['calls'] == 1:
                return self.proof(3)
            if state['calls'] == 2:
                return self.proof(4, bundleManifestSha256='e' * 64)
            raise laya_install_journal.JournalEvidenceError('executing_bundle_not_selected')
        read_only = {**self.proof(4, bundleManifestSha256='e' * 64),
                     'productRouteAllowed': False, 'readOnlyAllowed': True}
        result, calls, selected, ro = self.recall(
            verify=verify, read_only=lambda _d: dict(read_only))
        self.served(result)
        self.assertEqual(len(calls['query']), 2)
        self.assertGreaterEqual(ro.call_count, 2)
        self.assertEqual(selected.call_count, 1)

    def test_identity_change_or_repeated_change_still_refuses(self):
        import laya_install_journal
        for name, changes in (('install', {'installId': 'f' * 32}),
                              ('project', {'projectRoot': '/synthetic/other'}),
                              ('store', {'dbIncarnation': 'b' * 32}),
                              ('journal', {'journalPath': '/synthetic/x/install-journal.sqlite'}),
                              ('state', {'state': 'blocked'})):
            with self.subTest(identity=name):
                self.setUp()
                proofs = iter([self.proof(3)] * 2 + [self.proof(4)] * 10)
                moved = {**self.decision, **changes}
                result, calls, selected, _ro = self.recall(
                    verify=lambda _d: next(proofs), query=self.publish_on_first,
                    reselect=lambda *_a, **_k: dict(moved))
                self.assertIsInstance(result, response.HostResponseError)
                self.assertEqual(str(result), 'selected_proof_changed')
                self.assertEqual(calls['query'], ['generation-7'])
                self.assertEqual(selected.call_count, 1)
        with self.subTest('second change'):
            self.setUp()
            proofs = iter([self.proof(3)] * 2 + [self.proof(4)] * 10)
            result, calls, selected, _ro = self.recall(
                verify=lambda _d: next(proofs), query=lambda _n: self.publish())
            self.assertIsInstance(result, response.HostResponseError)
            self.assertEqual(str(result), 'selected_proof_changed')
            self.assertEqual(calls['query'], ['generation-7', 'generation-8'])
            self.assertEqual(selected.call_count, 1)
        with self.subTest('bundle change at the same epoch'):
            # Not an epoch advance: the final recheck refuses, and the
            # retried proof (unchanged route, still the other bundle) is
            # what serves; the first attempt's rows are never served.
            self.setUp()
            proofs = iter([self.proof(3)] + [self.proof(3, bundleManifestSha256='e' * 64)] * 10)
            result, calls, selected, _ro = self.recall(verify=lambda _d: next(proofs))
            self.served(result)
            self.assertEqual(calls['query'], ['generation-7', 'generation-7'])
        with self.subTest('journal refusal mid-recall'):
            state = {'calls': 0}

            def verify(_decision):
                state['calls'] += 1
                if state['calls'] <= 1:
                    return self.proof(3)
                raise laya_install_journal.JournalEvidenceError('inactive_or_mismatched_journal')
            result, calls, selected, _ro = self.recall(verify=verify)
            self.assertIsInstance(result, laya_install_journal.JournalEvidenceError)
            self.assertEqual(str(result), 'inactive_or_mismatched_journal')
            selected.assert_not_called()

    def test_journal_epoch_rollback_is_refused_in_and_across_attempts(self):
        cases = (
            # A restore 5 -> 4 seen by the first attempt's recheck.
            ('restore', [5, 4, 4, 4, 4, 4], None, 0),
            # 5 -> 6 -> 5 inside one attempt: both rechecks sit above 5.
            ('bounce', [5, 6, 5, 5, 5, 5], None, 0),
            # The first attempt saw 6, a publish forces the retry, and the
            # retried proof is back at 5.
            ('across retry', [5, 6, 6, 5, 5, 5], self.publish_on_first, 1))
        for name, epochs, query, reselected in cases:
            with self.subTest(case=name):
                self.setUp()
                proofs = iter([self.proof(epoch) for epoch in epochs])
                result, calls, selected, _ro = self.recall(
                    verify=lambda _d: next(proofs), query=query)
                self.assertIsInstance(result, response.HostResponseError, repr(result))
                self.assertEqual(str(result), 'selected_proof_rolled_back')
                # The retried proof is refused before it queries anything.
                self.assertEqual(calls['query'], ['generation-7'])
                self.assertEqual(selected.call_count, reselected)

    def test_retried_recall_records_one_sync_wake(self):
        # A cold provider wakes the owner once; the retried attempt reuses
        # that wake instead of bumping the wake revision again.
        from laya_index_a import IndexContractError

        def cold(*_args):
            raise IndexContractError('provider_unavailable')
        result, calls, selected, _ro = self.recall(
            verify=lambda _d: self.proof(3), query=self.publish_on_first, provider=cold)
        self.served(result)
        self.assertEqual(calls['query'], ['generation-7', 'generation-8'])
        self.assertEqual(selected.call_count, 1)
        self.assertEqual(calls['wake'].call_count, 1)
        self.assertEqual(calls['wake'].call_args.kwargs, {'pull_due': True})

    def test_retry_uses_the_armed_hook_budget(self):
        import hook_budget
        with self.subTest('budget nearly spent'):
            self.setUp()
            hook_budget._deadline = time.monotonic() + 0.5
            proofs = iter([self.proof(3)] * 2 + [self.proof(4)] * 10)
            result, calls, selected, _ro = self.recall(
                verify=lambda _d: next(proofs), query=self.publish_on_first)
            self.assertEqual(str(result), 'selected_proof_changed')
            self.assertEqual(calls['query'], ['generation-7'])
            selected.assert_not_called()
        with self.subTest('re-selection spends the budget'):
            self.setUp()
            hook_budget._deadline = time.monotonic() + 10

            def slow(*_a, **_k):
                hook_budget._deadline = time.monotonic() + 0.5
                return dict(self.decision)
            proofs = iter([self.proof(3)] * 2 + [self.proof(4)] * 10)
            result, calls, selected, _ro = self.recall(
                verify=lambda _d: next(proofs), query=self.publish_on_first, reselect=slow)
            self.assertEqual(str(result), 'selected_proof_changed')
            self.assertEqual(calls['query'], ['generation-7'])
            self.assertEqual(selected.call_count, 1)
        with self.subTest('enough budget'):
            self.setUp()
            hook_budget._deadline = time.monotonic() + 10
            proofs = iter([self.proof(3)] * 2 + [self.proof(4)] * 10)
            result, calls, _selected, _ro = self.recall(
                verify=lambda _d: next(proofs), query=self.publish_on_first)
            self.served(result)
            self.assertEqual(len(calls['query']), 2)

    def test_main_arms_the_hook_budget_before_route_selection(self):
        import contextlib
        import io
        import hook_budget
        seen = {}

        def select(*_args):
            seen['select'] = hook_budget.deadline()
            return {'state': 'candidate'}
        with tempfile.TemporaryDirectory(prefix='laya-host-main-') as temp:
            payload = Path(temp) / 'payload.json'
            payload.write_text(json.dumps({'prompt': 'find alpha', 'cwd': temp}),
                               encoding='utf-8')
            before = time.monotonic()
            with patch.object(response, 'select_hook_route', side_effect=select), \
                 patch.object(response, 'select_route', return_value={'state': 'candidate'}), \
                 patch.object(response, 'serve_selected', return_value={}), \
                 patch.object(response, '_capture_learning'), \
                 patch.dict(os.environ, {'QMD_HOOK_DEADLINE_MONO': ''}), \
                 contextlib.redirect_stdout(io.StringIO()):
                status = response.main(['serve-hook', 'recall', 'claude', str(payload), temp])
                exported = os.environ.get('QMD_HOOK_DEADLINE_MONO')
        self.assertEqual(status, 0)
        self.assertGreater(seen['select'], before)
        self.assertLessEqual(seen['select'], before + hook_budget.MAX_SECONDS)
        self.assertEqual(exported, '')

    def test_overlapping_recalls_keep_their_own_floor_and_wakes(self):
        # Hermes serves recalls in-process: a concurrent or nested recall
        # must neither reset another one's epoch floor nor share its wakes.
        import threading
        import runtime_work
        a_ready, b_done = threading.Event(), threading.Event()
        outcome = {}

        def epoch(value):
            response._recall_epoch({'selectionEpoch': value})

        def once(_action, _host, decision, _payload):
            root = decision['projectRoot']
            if root == '/p/a':
                epoch(5)
                epoch(6)
                response._recall_sync_wake('/p/a')
                a_ready.set()
                b_done.wait(5)
                # Nested recall of another project inside this one.
                response.serve_selected('recall', 'hermes', {'projectRoot': '/p/n'},
                                        {'prompt': 'q'})
                response._recall_sync_wake('/p/a')
                response._recall_sync_wake('/p/a2')
                try:
                    epoch(5)
                except response.HostResponseError as exc:
                    outcome['a'] = str(exc)
                return {}
            if root == '/p/b':
                a_ready.wait(5)
                epoch(1)
                response._recall_sync_wake('/p/b')
                response._recall_sync_wake('/p/b')
                return {}
            epoch(0)
            response._recall_sync_wake(root)
            return {}

        def run(root):
            try:
                response.serve_selected('recall', 'claude', {'projectRoot': root},
                                        {'prompt': 'q'})
            finally:
                if root == '/p/b':
                    b_done.set()
        with patch.object(response, '_serve_selected_once', side_effect=once), \
             patch.object(runtime_work, 'request',
                          side_effect=lambda *_a, **_k: {'status': 'queued'}) as wake:
            threads = [threading.Thread(target=run, args=(root,))
                       for root in ('/p/a', '/p/b')]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
            # Outside any recall the wake is recorded as before, every time.
            response._recall_sync_wake('/p/free')
            response._recall_sync_wake('/p/free')
        self.assertEqual(outcome.get('a'), 'selected_proof_rolled_back')
        self.assertEqual(sorted(call.args[1] for call in wake.call_args_list),
                         ['/p/a', '/p/a2', '/p/b', '/p/free', '/p/free', '/p/n'])
        self.assertIsNone(response._RECALL_STATE.get())

    def test_non_proof_refusals_are_not_retried(self):
        def stale(_n):
            raise response.HostResponseError('index_stale_sync_queued')
        result, calls, selected, _ro = self.recall(
            verify=lambda _d: self.proof(3), query=stale)
        self.assertEqual(str(result), 'index_stale_sync_queued')
        self.assertEqual(len(calls['query']), 1)
        selected.assert_not_called()


if __name__ == "__main__":
    unittest.main()

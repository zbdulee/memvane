"""Synthetic backend pass -> isolated real QMD -> existing recall hook."""
import hashlib
import json
import os

if os.environ.get('QMD_REAL_MODEL_TESTS') != '1':
    print(json.dumps({'skipped': 'real model tests require explicit QMD_REAL_MODEL_TESTS=1'}))
    raise SystemExit(0)
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, "core")
import wiki_topical as topical
import wiki_topical_backend as backend
import wiki_topical_publish as publisher

SNAP = Path(os.environ.get("QMD_PUBLISH_E2E_MODEL_SNAPSHOT",
    "/Users/dulee/work/laya-search-experiments/snapshot-20261003-165529"))
QMD = Path(os.environ.get("QMD_PUBLISH_E2E_QMD_BIN",
    shutil.which("qmd") or "/Users/dulee/work/.qmd-tools/bin/qmd"))
if not QMD.is_file() or not (SNAP / "models").is_dir() or not (SNAP / "offline.cjs").is_file():
    print(json.dumps({"skipped": "isolated offline QMD runtime unavailable"}))
    raise SystemExit(0)

with tempfile.TemporaryDirectory(prefix="qmd-topical-publish-") as temporary:
    root = Path(temporary).resolve()
    (root / topical.MARKER).write_text("synthetic only\n")
    (root / "sources").mkdir()
    quote = "The cobalt beacon opens only at dawn."
    (root / "sources/story.md").write_text(quote + "\n")
    (root / ".auto-context/wiki").mkdir(parents=True)
    collection = "fixture-topical-wiki"
    (root / ".auto-context/settings.json").write_text(json.dumps({
        "indexing": True, "collections": [collection],
        "collectionPaths": {collection: ".auto-context/wiki"},
        "collectionRoles": {collection: "wiki"}, "recallStrategy": "wikiOnly",
        "topN": 3, "minScore": 0, "events": ["sessionStart", "userPromptSubmit"]}))
    revision = topical.source_snapshot(root, "sources/story.md", {})[0]
    span = {"sourcePath": "sources/story.md", "sourceRevisionSha256": revision["sha256"],
            "startLine": 1, "endLine": 1, "quoteAnchor": quote,
            "quoteSha256": topical.digest(quote.encode())}
    card = {"cardId": "cobalt-beacon", "title": "Cobalt beacon", "category": "world-rule",
            "details": "", "claims": [{"claimId": "dawn-rule", "statement": quote,
                "state": "rule", "timeScope": "chapter-1", "condition": "at dawn", "evidence": [span]}]}
    generated = backend.stage_generation_response(root, {"schema": topical.SCHEMA, "cards": [card]})
    gid = generated["generationId"]
    response = {"verdict": "pass", "checks": [{"claimId": "dawn-rule", "sourcePath": "sources/story.md",
        "quoteSha256": span["quoteSha256"], "quoteAnchor": quote, "supported": True}], "reasons": []}
    cfg = {"extractor": {"builtins": ["codex"]},
           "verify": {"builtins": ["codex"], "crossEngine": "off"}}
    calls = []
    def verifier(argv, payload, timeout, cwd):
        calls.append(payload["task"])
        return response, None, 0
    attested = backend.run_verification_backend(root, gid, "cobalt-beacon", cfg, "codex",
                                                allow_backend_execution=True, runner=verifier)
    assert attested["status"] == "backend_pass" and len(calls) == 1
    published = publisher.publish(root, gid, "cobalt-beacon")
    assert publisher.publish(root, gid, "cobalt-beacon") == published
    (root / "qmd-config").mkdir()
    (root / "qmd-cache/qmd").mkdir(parents=True)
    (root / "qmd-cache/qmd/models").symlink_to(SNAP / "models", target_is_directory=True)
    (root / "qmd-config/index.yml").write_text(
        "collections: {}\nmodels:\n"
        "  embed: hf:ggml-org/embeddinggemma-300M-GGUF/embeddinggemma-300M-Q8_0.gguf\n"
        "  generate: hf:tobil/qmd-query-expansion-1.7B-gguf/qmd-query-expansion-1.7B-q4_k_m.gguf\n"
        "  rerank: hf:ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF/qwen3-reranker-0.6b-q8_0.gguf\n")
    (root / "qmd-db").mkdir()
    env = {**os.environ, "QMD_TOPICAL_SYNTHETIC_RUNTIME": "1",
           "QMD_BIN": str(QMD), "INDEX_PATH": str(root / "qmd-db/index.sqlite"),
           "QMD_CONFIG_DIR": str(root / "qmd-config"), "XDG_CACHE_HOME": str(root / "qmd-cache"),
           "NODE_OPTIONS": "--require=" + str(SNAP / "offline.cjs"),
           "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "QMD_RECALL_LOG": ""}
    publication = subprocess.run([sys.executable, "core/wiki_topical_publish.py", "publish",
                                  "--root", str(root), "--generation-id", gid,
                                  "--card-id", "cobalt-beacon"], env=env, cwd=Path.cwd(),
                                 capture_output=True, text=True, timeout=120, check=True)
    ready = json.loads(publication.stdout)
    assert ready["status"] == "ready" and ready["verifiedCards"] == 1
    forged = root / ".auto-context/wiki/topical-v2" / gid / "forged.md"
    forged.write_text("# unverified synthetic page\n")
    refused = subprocess.run([sys.executable, "core/wiki_topical_publish.py", "sync",
                              "--root", str(root)], env=env, cwd=Path.cwd(),
                             capture_output=True, text=True, timeout=30)
    assert refused.returncode == 1 and json.loads(refused.stdout)["status"] == "failed"
    forged.unlink()
    search = subprocess.run([str(QMD), "search", "cobalt beacon dawn", "-c", collection,
                             "--format", "json"], env=env, cwd=root, capture_output=True,
                            text=True, timeout=30, check=True)
    hits = json.loads(search.stdout)
    assert any("cobalt-beacon.md" in hit.get("file", "") for hit in hits)
    fixture = root / "qmd-query-fixture.json"
    fixture.write_text(json.dumps({"results": hits}))
    hook_env = {**env, "CLAUDE_PLUGIN_ROOT": str(Path.cwd()),
                "QMD_QUERY_FIXTURE": str(fixture), "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(["bash", "hooks/run-hook", "recall", "codex"],
                            input=json.dumps({"hook_event_name": "UserPromptSubmit", "cwd": str(root),
                                              "prompt": "cobalt beacon dawn rule"}),
                            env=hook_env, capture_output=True, text=True, timeout=30, check=True)
    assert quote in result.stdout and "wiki:verified" in result.stdout
    # A source change removes trust even while the old QMD vector remains.
    (root / "sources/story.md").write_text("Changed synthetic source.\n")
    stale = subprocess.run(["bash", "hooks/run-hook", "recall", "codex"],
                           input=json.dumps({"hook_event_name": "UserPromptSubmit", "cwd": str(root),
                                             "prompt": "cobalt beacon dawn rule"}),
                           env=hook_env, capture_output=True, text=True, timeout=30, check=True)
    assert quote not in stale.stdout
    stale_sync = subprocess.run([sys.executable, "core/wiki_topical_publish.py", "sync",
                                 "--root", str(root)], env=env, cwd=Path.cwd(),
                                capture_output=True, text=True, timeout=30)
    assert stale_sync.returncode == 1 and json.loads(stale_sync.stdout)["status"] == "failed"
    print(json.dumps({"backendCalls": len(calls), "published": True,
                      "qmdLexicalAndVectorReady": True, "actualQmdHits": len(hits),
                      "hookInjectedVerifiedBody": True, "staleSourceExcluded": True}))

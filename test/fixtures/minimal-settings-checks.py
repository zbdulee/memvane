#!/usr/bin/env python3
"""Minimal settings model, legacy invariance, migration refusals, generation skips.

Synthetic temp projects only; no QMD, Laya runtime or host CLI is launched.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

CORE = Path(__file__).resolve().parents[2] / "core"
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import laya_index_host_route as route  # noqa: E402
import settings_migration  # noqa: E402
import wiki_compile_enqueue  # noqa: E402
import wiki_init  # noqa: E402

# The live service-engineering shape (copied, never read from disk).
LEGACY_EQUIVALENT = {
    "indexing": True,
    "name": "project",
    "collections": ["project", "project-wiki"],
    "collectionPaths": {"project": ".", "project-wiki": ".auto-context/wiki"},
    "collectionRoles": {"project": "raw", "project-wiki": "wiki"},
    "skipPaths": ["node_modules", ".git", "build", "with_skill"],
    "recallStrategy": "wikiOnly",
    "compile": {"mode": "auto-wiki", "triggers": ["post_tool_source", "manual"],
                "extractor": {"backends": {"claude": ["scripts/x.sh", "claude"]}},
                "budget": {"verifyPerRun": 15}},
    "contextLearning": {"capture": False, "autoCycle": False, "liveSelection": True},
}
# The dogfood qmd-auto-context shape: a narrower raw root and different names.
LEGACY_NARROW = {
    "indexing": True,
    "name": "project",
    "collections": ["project-plans", "project-wiki"],
    "collectionPaths": {"project-plans": "docs/plans", "project-wiki": ".auto-context/wiki"},
    "collectionRoles": {"project-plans": "raw", "project-wiki": "wiki"},
    "queryTimeout": 3,
    "prefixStyle": "tag",
}


def vector_profile():
    model, fingerprint, dimension = "synthetic-model", "a" * 64, 3
    identity = json.dumps([model, fingerprint, dimension, "cosine"], sort_keys=True,
                          ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return {"profileId": hashlib.sha256(identity).hexdigest(), "modelId": model,
            "embeddingFingerprint": fingerprint, "dimension": dimension,
            "metric": "cosine", "approvedProviderId": "synthetic-provider",
            "providerArtifactSha256": "b" * 64,
            "providerRelativePath": "core/synthetic_provider.py"}


class Project(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="minimal-settings-")
        self.addCleanup(self.temp.cleanup)
        original = os.environ.get("HOME")
        self.addCleanup(lambda: os.environ.__setitem__("HOME", original)
                        if original is not None else os.environ.pop("HOME", None))
        self.home = Path(self.temp.name).resolve()
        os.environ["HOME"] = str(self.home)
        self.root = self.home / "project"
        self.auto = self.root / ".auto-context"
        (self.auto / "wiki").mkdir(parents=True)
        (self.root / "docs" / "plans").mkdir(parents=True)
        self.settings = self.auto / "settings.json"

    def write(self, value):
        self.settings.write_text(json.dumps(value, indent=2) + "\n")
        return self.settings.read_bytes()

    def write_route(self, collections, *, settings_bytes=None):
        private = self.home / "private-state"
        private.mkdir(mode=0o700, exist_ok=True)
        data = settings_bytes if settings_bytes is not None else self.settings.read_bytes()
        record = {"schema": route.SCHEMA_V2, "status": "active",
                  "projectRoot": str(self.root),
                  "settingsSha256": hashlib.sha256(data).hexdigest(),
                  "dbPath": str(self.home / "private-index.sqlite"),
                  "queuePath": str(self.home / "private-queue.sqlite"),
                  "dbIncarnation": "1" * 32, "scopeId": "6" * 32,
                  "collections": collections,
                  "pluginRoot": str(self.home / "installed-laya"),
                  "journalPath": str(private / "install-journal.sqlite"),
                  "installId": "3" * 32, "generation": "g-" + "4" * 32,
                  "bundleManifestSha256": "2" * 64,
                  "ownershipManifestSha256": "5" * 64,
                  "vectorProfile": vector_profile()}
        (self.auto / route.RECORD).write_text(json.dumps(record))
        return record

    def derived_collections(self):
        return [{"name": "project", "role": "raw", "sourceRoot": str(self.root)},
                {"name": "project-wiki", "role": "wiki",
                 "sourceRoot": str(self.auto / "wiki")}]


class MinimalModelChecks(Project):
    def test_minimal_settings_derive_effective_config(self):
        cfg = config.normalize_config({"name": "My Project", "skipPaths": ["build"]})
        self.assertEqual(cfg["settingsModel"], "minimal")
        self.assertIs(cfg["indexing"], True)
        self.assertEqual(cfg["collections"], ["my-project", "my-project-wiki"])
        self.assertEqual(cfg["collectionPaths"],
                         {"my-project": ".", "my-project-wiki": ".auto-context/wiki"})
        self.assertEqual(cfg["collectionRoles"],
                         {"my-project": "raw", "my-project-wiki": "wiki"})
        self.assertEqual(cfg["recallStrategy"], "wikiOnly")
        self.assertEqual(cfg["skipPaths"], ["build"])
        self.assertEqual(cfg["wikiPath"], ".auto-context/wiki")
        self.assertEqual(cfg["compile"]["mode"], "auto-wiki")
        self.assertEqual(cfg["compile"]["triggers"],
                         ["post_tool_source", "post_sync_source", "manual"])
        self.assertEqual(cfg["compile"]["extractor"]["builtins"], ["claude", "codex", "hermes"])
        self.assertEqual({k: cfg["contextLearning"][k] for k in
                          ("capture", "autoCycle", "liveSelection")},
                         {"capture": True, "autoCycle": True, "liveSelection": True})
        # Deterministic: the same input always yields the same effective config.
        self.assertEqual(cfg, config.normalize_config({"skipPaths": ["build"],
                                                       "name": "My Project"}))
        self.assertEqual(config.normalize_config({"name": "x"})["skipPaths"], [])

    def test_invalid_or_mixed_minimal_input_is_not_derived(self):
        for raw in ({}, {"skipPaths": ["a"]}, {"name": ""}, {"name": "한글"},
                    {"name": "x", "skipPaths": ["a", ""]}, {"name": "x", "skipPaths": "a"},
                    {"name": "x", "topN": 3}, {"name": "x", "indexing": True}):
            with self.subTest(raw=raw):
                self.assertFalse(config.is_minimal_settings(raw))
                cfg = config.normalize_config(raw)
                self.assertNotIn("settingsModel", cfg)
                self.assertEqual(config.expand_minimal_settings(raw), raw)

    def test_legacy_settings_normalize_exactly_as_before(self):
        for raw in (LEGACY_EQUIVALENT, LEGACY_NARROW,
                    {"name": "x", "collections": ["x"], "indexing": True}):
            with self.subTest(raw=raw):
                self.assertFalse(config.is_minimal_settings(raw))
                self.assertIs(config.expand_minimal_settings(raw), raw)
                self.assertEqual(config.normalize_config(raw),
                                 config._normalize_expanded_config(raw))
                self.assertNotIn("settingsModel", config.normalize_config(raw))
        self.write(LEGACY_NARROW)
        self.assertEqual(config.load_project_config_raw(str(self.root)), LEGACY_NARROW)
        found = config.find_project_config(str(self.root))
        self.assertEqual(found["config"]["collections"], ["project-plans", "project-wiki"])
        self.assertEqual(found["config"]["recallStrategy"], "hierarchical")

    def test_project_lookup_raw_view_and_local_optout(self):
        self.write({"name": "project", "skipPaths": ["build"]})
        found = config.find_project_config(str(self.root / "docs"))
        self.assertEqual(found["configFormat"], "auto-context-dir")
        self.assertEqual(found["config"]["collections"], ["project", "project-wiki"])
        raw = config.load_project_config_raw(str(self.root))
        self.assertEqual(raw["collectionPaths"],
                         {"project": ".", "project-wiki": ".auto-context/wiki"})
        config.write_local_optout(str(self.root))
        off = config.find_project_config(str(self.root))
        self.assertEqual(off["configFormat"], "local-optout")
        self.assertEqual(off["config"]["collections"], [])
        self.assertIs(off["config"]["indexing"], False)

    def test_legacy_file_formats_never_derive(self):
        (self.root / ".auto-context.json").write_text(json.dumps({"name": "project"}))
        found = config.find_project_config(str(self.root))
        self.assertEqual(found["configFormat"], "auto-context-json")
        self.assertEqual(found["config"]["collections"], [])
        self.assertNotIn("settingsModel", found["config"])
        self.assertEqual(config.load_project_config_raw(str(self.root)), {"name": "project"})

    def test_minimal_settings_select_a_v2_route(self):
        data = self.write({"name": "project", "skipPaths": ["build"]})
        self.write_route(self.derived_collections(), settings_bytes=data)
        decision = route.select_route(str(self.root))
        self.assertEqual(decision["state"], "candidate", decision)
        self.assertEqual(decision["recallSettings"]["recallStrategy"], "wikiOnly")
        self.assertEqual(decision["recallSettings"]["skipPaths"], ["build"])
        self.assertEqual(route.wiki_index_roots(decision["collections"]),
                         {"project-wiki": str(self.auto / "wiki")})

    def test_legacy_route_is_unchanged_and_byte_bound(self):
        data = self.write({**LEGACY_EQUIVALENT,
                           "compile": {"mode": "auto-wiki",
                                       "triggers": ["post_tool_source", "manual"],
                                       "extractor": {"builtins": ["claude"]}}})
        self.write_route(self.derived_collections(), settings_bytes=data)
        self.assertEqual(route.select_route(str(self.root))["state"], "candidate")
        # The minimal file with the same identity is a different byte string.
        self.write({"name": "project", "skipPaths": LEGACY_EQUIVALENT["skipPaths"]})
        self.assertEqual(route.select_route(str(self.root))["reason"],
                         "route_settings_changed")

    def test_init_wiki_keeps_minimal_settings_bytes(self):
        data = self.write({"name": "project", "skipPaths": []})
        with contextlib.redirect_stdout(io.StringIO()):
            wiki_init.init_wiki(str(self.root), quiet=True)
        self.assertEqual(self.settings.read_bytes(), data)
        self.assertTrue((self.auto / "wiki" / "SCHEMA.md").is_file())


class MigrationChecks(Project):
    def plan(self):
        return settings_migration.plan(str(self.root))

    def test_equivalent_unbound_settings_migrate_after_acceptance(self):
        before = self.write(LEGACY_EQUIVALENT)
        proposal = self.plan()
        self.assertEqual(proposal["status"], "review_required", proposal)
        self.assertEqual(proposal["minimalSettings"],
                         {"name": "project", "skipPaths": LEGACY_EQUIVALENT["skipPaths"]})
        self.assertIn("compile", proposal["changedFields"])
        self.assertIn("contextLearning", proposal["changedFields"])
        refused = settings_migration.apply(str(self.root), "0" * 64)
        self.assertEqual(refused["reason"], "proposal_acceptance_required")
        self.assertEqual(self.settings.read_bytes(), before)
        done = settings_migration.apply(str(self.root), proposal["proposalSha256"])
        self.assertEqual(done["status"], "migrated", done)
        self.assertEqual(json.loads(self.settings.read_text()), proposal["minimalSettings"])
        self.assertEqual(Path(done["backupPath"]).read_bytes(), before)
        after = config.find_project_config(str(self.root))["config"]
        self.assertEqual(after["collections"], ["project", "project-wiki"])
        self.assertEqual(self.plan()["status"], "already_minimal")

    def test_changed_collection_identity_is_refused(self):
        before = self.write(LEGACY_NARROW)
        result = self.plan()
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["reason"], "collection_identity_changed")
        self.assertNotEqual(result["currentIdentity"], result["derivedIdentity"])
        self.assertEqual(settings_migration.apply(str(self.root), None)["reason"],
                         "collection_identity_changed")
        self.assertEqual(self.settings.read_bytes(), before)

    def test_bound_route_with_other_identity_is_refused(self):
        before = self.write(LEGACY_EQUIVALENT)
        self.write_route([
            {"name": "project-plans", "role": "raw",
             "sourceRoot": str(self.root / "docs" / "plans")},
            {"name": "project-wiki", "role": "wiki", "sourceRoot": str(self.auto / "wiki")}])
        result = self.plan()
        self.assertEqual(result["reason"], "bound_collection_identity_changed", result)
        self.assertEqual(result["boundCollections"][0]["name"], "project-plans")
        self.assertEqual(self.settings.read_bytes(), before)

    def test_bound_route_with_same_identity_is_refused_for_its_digest(self):
        before = self.write(LEGACY_EQUIVALENT)
        self.write_route(self.derived_collections())
        result = self.plan()
        self.assertEqual(result["reason"], "settings_bytes_bound_by_laya_route", result)
        self.assertEqual(settings_migration.apply(str(self.root), None)["reason"],
                         "settings_bytes_bound_by_laya_route")
        self.assertEqual(self.settings.read_bytes(), before)

    def test_selection_marker_and_setup_journal_are_bindings(self):
        self.write(LEGACY_EQUIVALENT)
        marker = self.auto / route.QMD_SELECTION_MARKER
        marker.write_text("{}")
        self.assertEqual(self.plan()["reason"], "settings_bound_by_laya_route")
        marker.unlink()
        (self.auto / "install-update-journal.json").write_text("{}")
        self.assertEqual(self.plan()["reason"], "settings_bound_by_setup_journal")

    def test_disabled_indexing_is_refused(self):
        self.write({**LEGACY_EQUIVALENT, "indexing": False})
        self.assertEqual(self.plan()["reason"], "legacy_indexing_not_enabled")


class GenerationSkipChecks(Project):
    def record_for(self, rel, cfg):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# note\n")
        return wiki_compile_enqueue._source_record(str(path), str(self.root), str(self.root),
                                                   cfg, "claude")

    def test_minimal_settings_skip_dot_folders_and_skip_paths(self):
        self.write({"name": "project", "skipPaths": ["build"]})
        cfg = config.find_project_config(str(self.root))["config"]
        self.assertEqual(self.record_for("docs/a.md", cfg)["source"],
                         {"kind": "file", "path": "docs/a.md", "collection": "project"})
        for rel in ("docs/.draft/a.md", ".claude/a.md", ".auto-context/wiki/concepts/a.md",
                    "build/a.md", "docs/build/a.md"):
            with self.subTest(rel=rel):
                self.assertIsNone(self.record_for(rel, cfg))

    def test_legacy_skip_paths_stay_recall_only(self):
        self.write({**LEGACY_EQUIVALENT, "skipPaths": ["build"]})
        cfg = config.find_project_config(str(self.root))["config"]
        self.assertIsNone(config.generation_skip_paths(cfg))
        self.assertEqual(self.record_for("build/a.md", cfg)["source"]["path"], "build/a.md")


if __name__ == "__main__":
    unittest.main()

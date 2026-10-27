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
from unittest import mock

CORE = Path(__file__).resolve().parents[2] / "core"
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import laya_index_host_route as route  # noqa: E402
import resolve_paths  # noqa: E402
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
        digest = (hashlib.sha256(settings_bytes).hexdigest() if settings_bytes is not None
                  else route.settings_identity_digest(self.root, collections))
        record = {"schema": route.SCHEMA_V2, "status": "active",
                  "projectRoot": str(self.root),
                  "settingsSha256": digest,
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
                          ("capture", "autoCycle")},
                         {"capture": True, "autoCycle": True})
        self.assertNotIn("liveSelection", cfg["contextLearning"])
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
        self.write({"name": "project", "skipPaths": ["build"]})
        self.write_route(self.derived_collections())
        decision = route.select_route(str(self.root))
        self.assertEqual(decision["state"], "candidate", decision)
        self.assertEqual(decision["recallSettings"]["recallStrategy"], "wikiOnly")
        self.assertEqual(decision["recallSettings"]["skipPaths"], ["build"])
        self.assertEqual(route.wiki_index_roots(decision["collections"]),
                         {"project-wiki": str(self.auto / "wiki")})

    def test_route_binds_identity_not_settings_bytes(self):
        data = self.write({**LEGACY_EQUIVALENT,
                           "compile": {"mode": "auto-wiki",
                                       "triggers": ["post_tool_source", "manual"],
                                       "extractor": {"builtins": ["claude"]}}})
        self.write_route(self.derived_collections())
        self.assertEqual(route.select_route(str(self.root))["state"], "candidate")
        # The minimal file with the same identity is a different byte string.
        self.write({"name": "project", "skipPaths": LEGACY_EQUIVALENT["skipPaths"]})
        self.assertEqual(route.select_route(str(self.root))["state"], "candidate")
        # A route bound to the settings file bytes is no longer accepted.
        self.write_route(self.derived_collections(), settings_bytes=data)
        self.write({**LEGACY_EQUIVALENT,
                    "compile": {"mode": "auto-wiki",
                                "triggers": ["post_tool_source", "manual"],
                                "extractor": {"builtins": ["claude"]}}})
        self.assertEqual(route.select_route(str(self.root))["reason"],
                         "route_settings_changed")

    def test_init_wiki_keeps_minimal_settings_bytes(self):
        data = self.write({"name": "project", "skipPaths": []})
        with contextlib.redirect_stdout(io.StringIO()):
            wiki_init.init_wiki(str(self.root), quiet=True)
        self.assertEqual(self.settings.read_bytes(), data)
        self.assertTrue((self.auto / "wiki" / "SCHEMA.md").is_file())


class GenerationSkipChecks(Project):
    def record_for(self, rel, cfg):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# note\n")
        return wiki_compile_enqueue._source_record(str(path), str(self.root), str(self.root),
                                                   cfg, "claude")

    def test_minimal_settings_skip_dot_folders_and_skip_paths(self):
        self.write({"name": "project", "skipPaths": ["drafts"]})
        cfg = config.find_project_config(str(self.root))["config"]
        self.assertEqual(self.record_for("docs/a.md", cfg)["source"],
                         {"kind": "file", "path": "docs/a.md", "collection": "project"})
        for rel in ("docs/.draft/a.md", ".claude/a.md", ".auto-context/wiki/concepts/a.md",
                    "drafts/a.md", "docs/drafts/a.md"):
            with self.subTest(rel=rel):
                self.assertIsNone(self.record_for(rel, cfg))

    def test_legacy_skip_paths_stay_recall_only(self):
        self.write({**LEGACY_EQUIVALENT, "skipPaths": ["drafts"]})
        cfg = config.find_project_config(str(self.root))["config"]
        self.assertIsNone(config.generation_skip_paths(cfg))
        self.assertEqual(self.record_for("drafts/a.md", cfg)["source"]["path"], "drafts/a.md")


def legacy_settings(raw_name, wiki_name, raw_path='.', name='project'):
    """The live legacy shape whose effective identity minimal settings derive."""
    return {
        'indexing': True, 'name': name,
        'collections': [raw_name, wiki_name],
        'collectionPaths': {raw_name: raw_path, wiki_name: '.auto-context/wiki'},
        'collectionRoles': {raw_name: 'raw', wiki_name: 'wiki'},
        'skipPaths': ['build'], 'recallStrategy': 'hierarchical',
        'compile': {'mode': 'auto-wiki', 'triggers': ['post_tool_source', 'manual'],
                    'extractor': {'builtins': ['claude']}},
    }


class MinimalQmdIndexChecks(unittest.TestCase):
    def setUp(self):
        # Temp roots live under /private, which resolve_paths treats as risky.
        patcher = mock.patch.object(resolve_paths, 'is_risky_path', lambda _path: False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_minimal_raw_root_is_never_a_qmd_index_entry(self):
        with tempfile.TemporaryDirectory(prefix='minimal-qmd-') as temp:
            root = Path(temp).resolve()
            (root / '.auto-context' / 'wiki').mkdir(parents=True)
            minimal = config.normalize_config({'name': 'project', 'skipPaths': []})
            resolved = resolve_paths.resolve_paths(str(root), json.dumps(minimal))
            self.assertEqual([row['name'] for row in resolved['indexEntries']],
                             ['project-wiki'])
            self.assertEqual([row['name'] for row in resolved['sourceEntries']], ['project'])
            self.assertEqual(len(resolved['entries']), 2)
            legacy = config.normalize_config(legacy_settings('project', 'project-wiki'))
            resolved = resolve_paths.resolve_paths(str(root), json.dumps(legacy))
            self.assertEqual([row['name'] for row in resolved['indexEntries']],
                             ['project', 'project-wiki'])
            (root / '.auto-context' / 'settings.json').write_text(
                json.dumps({'name': 'project', 'skipPaths': []}))
            with mock.patch.dict(os.environ, {'HOME': str(root.parent)}):
                raw = config.load_project_config_raw(str(root))
            self.assertEqual(raw['settingsModel'], 'minimal')
            self.assertEqual(raw['collectionRoles'], {'project': 'raw', 'project-wiki': 'wiki'})
            resolved = resolve_paths.resolve_paths(str(root), json.dumps(raw))
            self.assertEqual([row['name'] for row in resolved['indexEntries']],
                             ['project-wiki'])

    def test_shared_registry_drops_index_flag_only_for_minimal_raw(self):
        import qmd_shared_store as store
        with tempfile.TemporaryDirectory(prefix='minimal-registry-') as temp:
            base = Path(temp).resolve() / 'store'
            base.mkdir(mode=0o700)
            row = {'scope': '/p', 'path': '/p', 'indexed': True}
            registry = base / 'collections.json'
            registry.write_text(json.dumps({'project': row}))
            registry.chmod(0o600)
            demoted = {'project': {**row, 'indexed': False}}
            # An explicit role edit (no minimal settings) still needs reconciliation.
            with self.assertRaisesRegex(ValueError, 'path_change_requires_migration'):
                store._reserve_owners(base, demoted)
            minimal = config.normalize_config({'name': 'project', 'skipPaths': []})
            self.assertEqual(store.minimal_raw_names(minimal), {'project'})
            self.assertEqual(store.minimal_raw_names(
                config.normalize_config(legacy_settings('project', 'project-wiki'))), set())
            store._reserve_owners(base, demoted, demoted=store.minimal_raw_names(minimal))
            self.assertIs(json.loads(registry.read_text())['project']['indexed'], False)
            moved = {'project': {**row, 'path': '/q', 'indexed': False}}
            with self.assertRaisesRegex(ValueError, 'path_change_requires_migration'):
                store._reserve_owners(base, moved, demoted={'project'})


if __name__ == "__main__":
    unittest.main()

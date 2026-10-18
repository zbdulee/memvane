#!/usr/bin/env python3
"""Synthetic route selection checks; no QMD or Laya runtime is launched."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

CORE = Path(__file__).resolve().parents[2] / "core"
sys.path.insert(0, str(CORE))
import laya_index_host_route as route


def vector_profile():
    model, fingerprint, dimension = "synthetic-model", "a" * 64, 3
    identity = json.dumps([model, fingerprint, dimension, "cosine"],
                          sort_keys=True, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
    return {"profileId": hashlib.sha256(identity).hexdigest(),
            "modelId": model, "embeddingFingerprint": fingerprint,
            "dimension": dimension, "metric": "cosine",
            "approvedProviderId": "synthetic-provider",
            "providerArtifactSha256": "b" * 64,
            "providerRelativePath": "core/synthetic_provider.py"}


class HostRouteChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="laya-route-")
        self.addCleanup(self.temp.cleanup)
        self.original_home = os.environ.get("HOME")
        self.addCleanup(self.restore_home)
        os.environ["HOME"] = self.temp.name
        self.root = Path(self.temp.name).resolve() / "project"
        self.auto = self.root / ".auto-context"
        self.auto.mkdir(parents=True)
        self.settings = self.auto / "settings.json"
        self.settings.write_text(json.dumps({"collections": ["docs"], "indexing": True,
                                             "recallStrategy": "flat",
                                             "collectionPaths": {"docs": "docs"}}))
        (self.root / "docs").mkdir()
        self.path = self.auto / route.RECORD

    def restore_home(self):
        if self.original_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.original_home

    def record(self, **changes):
        row = {"schema": route.SCHEMA, "status": "active",
               "projectRoot": str(self.root),
               "settingsSha256": hashlib.sha256(self.settings.read_bytes()).hexdigest(),
               "dbPath": str(self.root / "private-index.sqlite"),
               "queuePath": str(self.root / "private-queue.sqlite"),
               "dbIncarnation": "1" * 32,
               "scopeId": "6" * 32, "collectionName": "docs",
               "sourceRoot": str(self.root / "docs"),
               "pluginRoot": str(Path(self.temp.name).resolve() / "installed-laya"),
               "journalPath": str(Path(self.temp.name).resolve() / "install-journal.sqlite"),
               "installId": "3" * 32, "generation": "g-" + "4" * 32,
               "bundleManifestSha256": "2" * 64,
               "ownershipManifestSha256": "5" * 64,
               "vectorProfile": vector_profile()}
        row.update(changes)
        self.path.write_text(json.dumps(row))
        return row

    def state(self, cwd=None, action="recall"):
        return route.select_route(str(cwd or self.root), action=action)["state"]

    def test_absent_marker_keeps_legacy(self):
        self.assertEqual(self.state(), "legacy")
        self.assertFalse((self.root / "private-index.sqlite").exists())

    def test_exact_record_is_candidate_for_each_data_action(self):
        self.record()
        nested = self.root / "docs"
        for action in route.ROUTED_ACTIONS:
            with self.subTest(action=action):
                self.assertEqual(self.state(nested, action), "candidate")
        self.assertFalse((self.root / "private-index.sqlite").exists())

    def test_changed_settings_are_blocked_on_next_selection(self):
        self.record()
        self.assertEqual(self.state(), "candidate")
        self.settings.write_text(self.settings.read_text() + "\n")
        self.assertEqual(self.state(), "blocked")

    def test_automatic_compile_setting_blocks_selection(self):
        self.record()
        cfg = json.loads(self.settings.read_text())
        cfg["compile"] = {"mode": "candidates"}
        self.settings.write_text(json.dumps(cfg))
        self.record()
        self.assertEqual(self.state(), "blocked")

    def test_unsupported_recall_settings_block_selected_route(self):
        original = json.loads(self.settings.read_text())
        for change in ({'recallStrategy': 'hierarchical'},
                       {'collectionRoles': {'docs': 'wiki'}},
                       {'minScore': 0.1}, {'rawFallbackMinScore': 0.1},
                       {'topN': 6}, {'topN': True},
                       {'skipPaths': ['private.md']},
                       {'lexicalPatterns': ['other']},
                       {'allowRoots': ['/external']}):
            with self.subTest(change=change):
                self.settings.write_text(json.dumps({**original, **change}))
                self.record()
                self.assertEqual(self.state(), 'blocked')
        for supported in ({'topN': 1}, {'topN': 5},
                          {'lexicalPatterns': ['ep']},
                          {'collectionRoles': {'docs': 'raw'}}):
            with self.subTest(supported=supported):
                self.settings.write_text(json.dumps({**original, **supported}))
                self.record()
                self.assertEqual(self.state(), 'candidate')

    def test_topical_pilot_marker_blocks_selection(self):
        self.record()
        (self.root / ".qmd-topical-v2-project").write_text("pilot")
        self.assertEqual(self.state(), "blocked")

    def test_invalid_record_never_returns_legacy(self):
        self.record(status="preparing")
        self.assertEqual(self.state(), "blocked")
        self.path.write_text("{" + '"schema":"a","schema":"b"}')
        self.assertEqual(self.state(), "blocked")
        self.path.write_text("{}")
        self.assertEqual(self.state(), "blocked")

    def test_vector_profile_is_mandatory_and_exact(self):
        for bad in (None, {}, {**vector_profile(), "dimension": 4},
                    {**vector_profile(), "modelId": "synthetic\nmodel"},
                    {**vector_profile(), "embeddingFingerprint": "Z" * 64},
                    {**vector_profile(), "approvedProviderId": "../provider"},
                    {**vector_profile(), "providerRelativePath": "core/../provider.py"},
                    {**vector_profile(), "providerRelativePath": "core/provider.sh"}):
            with self.subTest(profile=bad):
                self.record(vectorProfile=bad)
                self.assertEqual(self.state(), "blocked")
        self.record()
        record = json.loads(self.path.read_text())
        del record["vectorProfile"]
        self.path.write_text(json.dumps(record))
        self.assertEqual(self.state(), "blocked")

    def test_bad_project_or_package_binding_is_blocked(self):
        self.record(projectRoot=str(Path(self.temp.name) / "other"))
        self.assertEqual(self.state(), "blocked")
        self.record(bundleManifestSha256="not-a-digest")
        self.assertEqual(self.state(), "blocked")
        self.record(ownershipManifestSha256="not-a-digest")
        self.assertEqual(self.state(), "blocked")
        self.record(installId="not-an-id")
        self.assertEqual(self.state(), "blocked")
        self.record(generation="not-a-generation")
        self.assertEqual(self.state(), "blocked")
        self.record(pluginRoot="relative")
        self.assertEqual(self.state(), "blocked")
        self.record(journalPath="relative")
        self.assertEqual(self.state(), "blocked")
        self.record(journalPath=str(self.root / "other.sqlite"))
        self.assertEqual(self.state(), "blocked")
        self.record(dbIncarnation="unknown")
        self.assertEqual(self.state(), "blocked")
        self.record(dbPath="relative.sqlite")
        self.assertEqual(self.state(), "blocked")
        self.record(queuePath="relative.sqlite")
        self.assertEqual(self.state(), "blocked")
        self.record(queuePath=str(self.root / "private-index.sqlite"))
        self.assertEqual(self.state(), "blocked")
        self.record(scopeId="branch-main")
        self.assertEqual(self.state(), "blocked")
        self.record(collectionName="other")
        self.assertEqual(self.state(), "blocked")
        self.record(sourceRoot=str(self.root))
        self.assertEqual(self.state(), "blocked")

    def test_symlink_marker_and_settings_are_blocked(self):
        outside = Path(self.temp.name) / "marker.json"
        self.record()
        self.path.rename(outside)
        self.path.symlink_to(outside)
        self.assertEqual(self.state(), "blocked")
        self.path.unlink()
        self.record()
        moved = Path(self.temp.name) / "settings.json"
        self.settings.rename(moved)
        self.settings.symlink_to(moved)
        self.assertEqual(self.state(), "blocked")

    def test_nested_marker_or_two_markers_block(self):
        nested = self.root / "docs" / ".auto-context"
        nested.mkdir(parents=True)
        (nested / route.RECORD).write_text("{}")
        self.assertEqual(self.state(nested.parent), "blocked")
        self.record()
        self.assertEqual(self.state(nested.parent), "blocked")

    def test_outside_home_nested_cwd_cannot_miss_parent_route(self):
        self.record()
        unrelated_home = Path(self.temp.name) / "other-home"
        unrelated_home.mkdir()
        os.environ["HOME"] = str(unrelated_home)
        self.assertEqual(self.state(self.root / "docs"), "blocked")

    def test_disabled_indexing_and_unsupported_action_block(self):
        self.settings.write_text('{"collections":["docs"],"indexing":false}')
        self.record()
        self.assertEqual(self.state(), "blocked")
        self.assertEqual(self.state(action="unknown"), "blocked")

    def test_multiple_collections_or_wildcard_mapping_block(self):
        self.record()
        self.settings.write_text('{"collections":["docs","more"],"indexing":true,"collectionPaths":{"docs":"docs","more":"more"}}')
        self.record()
        self.assertEqual(self.state(), "blocked")
        self.settings.write_text('{"collections":["docs"],"indexing":true,"collectionPaths":{"*":"docs"}}')
        self.record()
        self.assertEqual(self.state(), "blocked")


if __name__ == "__main__":
    unittest.main()

"""Exercise the real hook entry and receipt with no backend or model calls."""
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch

from context_learning.synthetic_launcher import _dispatch_edit_hook, _indexed_revision
from context_learning.synthetic_scope import COLLECTION_PATHS, WIKI_COLLECTION, PROJECT_WIKI_COLLECTION, project_collection_paths
from context_learning.synthetic_experiment import shared_for, private_for


def denied(call, reason):
    try:
        call()
    except ValueError as exc:
        assert str(exc) == reason, (exc, reason)
    else:
        raise AssertionError("hook unexpectedly succeeded")


with tempfile.TemporaryDirectory(prefix="qmd-synthetic-hook-test-", dir="/tmp") as name:
    base = Path(name).resolve()
    os.environ["HOME"] = str(base)
    (base / ".auto-context").mkdir()
    project = base
    story = project / "마지막 마법의 도서관"
    shared_for(project).mkdir(mode=0o700)
    private_for(project).mkdir(mode=0o700)
    (story / ".auto-context").mkdir(parents=True)
    (story / "04_Manuscript").mkdir()
    (story / "04_Manuscript/ep001.md").write_text("가상 원고\n")
    sibling = project / "또 다른 합성 이야기"
    (sibling / "04_Manuscript").mkdir(parents=True)
    sibling_source = sibling / "04_Manuscript/ep001.md"
    sibling_source.write_text("다른 합성 원고\n")
    paths = project_collection_paths(project)
    settings = {"indexing": True, "collections": list(paths),
                "collectionPaths": paths,
                "collectionRoles": {name: "wiki" if name in (WIKI_COLLECTION, PROJECT_WIKI_COLLECTION) else "raw"
                                    for name in paths},
                "allowRoots": [], "injectSourcePathsPerCard": 0,
                "recallStrategy": "wikiOnly", "wikiPath": paths[WIKI_COLLECTION],
                "contextLearning": {"capture": True, "stateRoot": str(private_for(project))},
                "sharedLearning": {"enabled": True, "root": str(shared_for(project)),
                                   "trialSelection": True},
                "compile": {"mode": "off"}}
    (project / ".auto-context/settings.json").write_text(json.dumps(settings))
    import config as qmd_config
    from collection_match import select_collections
    from posttool import is_story_path
    assert qmd_config.find_project_config(str(story / "04_Manuscript"))["projectRoot"] == str(project)
    assert qmd_config.find_project_config(str(sibling / "04_Manuscript"))["projectRoot"] == str(project)
    assert qmd_config.find_project_config(str(base))["config"]["indexing"] is True
    assert select_collections([str(sibling_source)], str(project),
                              qmd_config.find_project_config(str(project))["config"]) == {}
    assert is_story_path(str(story / "04_Manuscript/ep001.md"), str(project), settings)
    assert not is_story_path(str(sibling_source), str(project), settings)
    # A modern settings file alone is not selected. The ordinary dispatcher
    # would return zero after setup_guard exits 3, so the launcher must reject.
    denied(lambda: _dispatch_edit_hook(story, "04_Manuscript/ep001.md"),
           "synthetic_project_index_not_selected")
    manager = base / "manager.sh"
    manager.write_text("#!/bin/sh\nexit 0\n")
    manager.chmod(0o700)
    queue = base / "dirty-queue"
    environment = {"QMD_SETUP_GUARD_FIXTURE": "1", "QMD_BACKEND_MANAGER": str(manager),
                   "QMD_DIRTY_QUEUE": str(queue), "QMD_CACHE_DIR": str(base / "cache"),
                   "QMD_RECALL_LOG": ""}
    with patch.dict(os.environ, environment), patch(
            "context_learning.synthetic_scope._experiment_config", return_value=None):
        with patch.dict(os.environ, {"QMD_CORE_INDEX_SCRIPT": "/bin/false"}):
            denied(lambda: _dispatch_edit_hook(story, "04_Manuscript/ep001.md"),
                   "synthetic_index_not_enqueued")
        assert not queue.exists()
        _dispatch_edit_hook(story, "04_Manuscript/ep001.md")
        lines = queue.read_text().splitlines()
        assert lines == ["fantasy-20261008-manuscript\t" +
                         str(story / "04_Manuscript")]
        # A sibling novel path cannot be selected by this exact collection map.
        denied(lambda: _dispatch_edit_hook(story, "../other-novel/ep001.md"),
               "synthetic_edit_outside_collection")
    wiki = story / ".auto-context/wiki"
    wiki.mkdir()
    card = wiki / "ep001-main.md"
    card.write_text("---\nstatus: verified\n---\nInvented indexed card.\n")
    revision = hashlib.sha256(card.read_bytes()).hexdigest()
    index_path = base / "selected.sqlite"
    with sqlite3.connect(index_path) as index:
        index.execute("CREATE TABLE documents (collection TEXT,path TEXT,hash TEXT,active INTEGER)")
    selected = {"selected": True, "INDEX_PATH": str(index_path)}
    with patch("qmd_route.project_paths", return_value=selected):
        source_id = WIKI_COLLECTION + "/ep001-main.md"
        assert not _indexed_revision(story, source_id, revision)
        with sqlite3.connect(index_path) as index:
            index.execute("INSERT INTO documents VALUES (?,?,?,1)",
                          (WIKI_COLLECTION, "ep001-main.md", "0" * 64))
        assert not _indexed_revision(story, source_id, revision)
        with sqlite3.connect(index_path) as index:
            index.execute("UPDATE documents SET hash=?", (revision,))
        assert _indexed_revision(story, source_id, revision)

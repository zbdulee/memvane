#!/usr/bin/env python3
import sys
import os
import json
import re
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))
import change_owner
import config as qmd_config
import dirty_queue
import posttool
from collection_match import select_collections


def queue_path():
    return dirty_queue.queue_path()


def enqueue(selected, *, project_root=None):
    if project_root is None:
        dirty_queue.enqueue_collections(selected)
    else:
        dirty_queue.enqueue_project_collections(project_root, selected)


def _synthetic_receipt(project_root, selected):
    """Private proof for the bounded launcher that enqueue actually happened."""
    raw = os.environ.get("QMD_SYNTHETIC_INDEX_RECEIPT")
    if not raw:
        return
    path = Path(raw)
    parent = path.parent
    if (not path.is_absolute() or not re.fullmatch(r"receipt-[0-9a-f]{32}\.json", path.name)
            or not parent.name.startswith("qmd-synthetic-hook-")
            or parent.parent.resolve() != Path(tempfile.gettempdir()).resolve()
            or parent.is_symlink() or not parent.is_dir()
            or parent.stat().st_uid != os.getuid() or parent.stat().st_mode & 0o077
            or path.exists() or path.is_symlink()):
        raise ValueError("invalid_synthetic_index_receipt")
    # An exclusive file in the launcher's owner-only temporary directory is
    # never an ordinary hook output or a success indicator for unrelated edits.
    with path.open("x", encoding="utf8") as output:
        os.chmod(path, 0o600)
        json.dump({"projectRoot": str(project_root), "selected": selected}, output,
                  sort_keys=True, separators=(",", ":"))


def main():
    if os.environ.get("QMD_SANDBOX") or "--sandbox" in sys.argv:
        return 0
    raw = sys.stdin.read().strip()
    if not raw:
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0
    if payload.get("hook_event_name") not in (None, "PostToolUse", "AfterTool"):
        return 0
    cwd = payload.get("cwd") or os.getcwd()
    found = qmd_config.find_project_config(cwd)
    config = found["config"]
    project_root = found["projectRoot"]
    paths = posttool.edited_paths(payload)
    from context_learning.synthetic_scope import invocation_in_generated_story
    if not invocation_in_generated_story(found, payload.get("cwd"), paths):
        return 0
    # Host edit paths are relative to the tool's cwd, while collectionPaths
    # belong to the discovered project root. Resolve edits first and keep the
    # canonical root for collection ownership and the durable queue key.
    try:
        edit_cwd = Path(cwd).resolve(strict=True)
        absolute_paths = [str((edit_cwd / path).resolve()) for path in paths]
    except (OSError, ValueError, TypeError):
        return 0
    owned, raw_of = {}, {}
    if config.get("collections"):
        for raw, absolute in zip(paths, absolute_paths):
            picked = select_collections([absolute], project_root, config)
            if picked:
                owned[absolute] = picked
                raw_of.setdefault(absolute, raw)
    _enqueue_cwd_owned(owned, project_root, config, raw_of, edit_cwd)
    # A path outside this cwd's own collections may belong to another, already
    # approved project. That routes its indexing only; recall scope is unchanged.
    # A missing path (deletion / old side of a move) routes only on a prior
    # observation still bound to the same registration (stage B2a).
    foreign = [raw for raw, absolute in zip(paths, absolute_paths) if absolute not in owned]
    if foreign:
        try:
            routed, _refused = change_owner.route_changes(foreign, edit_cwd)
            from laya_index_host_route import select_route
            legacy, laya = {}, []
            for scope, owners in routed.items():
                state = select_route(scope, action='index').get('state')
                if state == 'legacy':
                    legacy[scope] = owners
                elif state == 'candidate':
                    laya.extend(owners)
                # A blocked route cannot fall back to the old QMD dirty queue.
            if legacy:
                change_owner.enqueue_routed(legacy, edit_cwd)
            if laya:
                import laya_owner_projection
                laya_owner_projection.route_external(
                    laya, str(edit_cwd), selected_root=project_root)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            pass
    return 0


def _enqueue_cwd_owned(owned, project_root, config, raw_of=None, edit_cwd=None):
    if not owned or not qmd_config.event_enabled(config, "postToolUse"):
        return
    # Keep cwd behavior, but fail closed for a path that another approved
    # project also registers. Without a scoped store there is no such claim.
    # A missing cwd path keeps this legacy route; stage B2a does not cover it.
    snapshot, _reason = change_owner.registry_snapshot()
    selected, accepted = {}, []
    for absolute, picked in owned.items():
        if snapshot and change_owner.conflicting_owner_claims(absolute, project_root, snapshot):
            continue
        selected.update(picked)
        accepted.append((raw_of or {}).get(absolute, absolute))
    if not selected:
        return
    # dirty 큐는 **인덱싱 큐**다. role `source`는 qmd에 등록되지 않으므로 넣으면
    # index_worker가 `collection add`로 도로 등록해 role을 무력화한다 — 즉 여기서
    # 거르지 않으면 update.sh의 unregister와 매 편집마다 싸운다.
    # collection_match 자체는 필터하지 않는다(compile enqueue가 같은 매핑을 쓰고
    # 거기서는 source가 정상 입력이다). 필터는 소비자별로 여기 한 줄이다.
    roles = qmd_config.role_map(config)
    selected = {
        name: path for name, path in selected.items()
        if qmd_config.is_indexed_collection(roles, name)
    }
    if not selected:
        return
    try:
        enqueue(selected, project_root=project_root)
        _synthetic_receipt(project_root, selected)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        # Do not emit a two-column queue item when this project's selected
        # pointer is damaged; that could route its external collection globally.
        return
    # Evidence for a later same-session delete/move of these files. Only fully
    # proved present regular files are recorded; failure records nothing.
    if snapshot and edit_cwd is not None:
        try:
            change_owner.observe_present(accepted, edit_cwd, snapshot=snapshot)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            pass


if __name__ == "__main__":
    import hook_main
    sys.exit(hook_main.run(main))

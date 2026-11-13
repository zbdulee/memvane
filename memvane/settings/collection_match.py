#!/usr/bin/env python3
import argparse
import fnmatch
import json
import sys
from pathlib import Path
from memvane.settings import resolve_paths as qmd_resolve_paths


def _resolve_rel(cwd_path, rel):
    # safe_collection_path와 동일한 해석: '~' 홈 확장, 절대경로는 그대로,
    # 상대경로는 cwd 기준. (버그 B: expanduser 누락 불일치 수정)
    candidate = Path(rel).expanduser()
    if not candidate.is_absolute():
        candidate = cwd_path / candidate
    return candidate.resolve()


def select_collections(edited_paths, cwd, config):
    cwd_path = Path(cwd).resolve()
    roots = qmd_resolve_paths.allowed_roots(config)
    collection_paths = config.get("collectionPaths") or {}
    if not isinstance(collection_paths, dict):
        collection_paths = {}
    collections = config.get("collections")
    if not isinstance(collections, list):
        collections = []
    coll_dirs = {}
    # resolve_paths와 동일하게 collections(실제 이름)를 순회하고, 각 이름을
    # collectionPaths에서 fnmatch(첫 매칭)로 매핑한다. dirty-queue에는 wildcard
    # 패턴 키가 아니라 실제 collection 이름이 들어간다. (버그 A 수정)
    for name in collections:
        if not isinstance(name, str):
            continue
        rel = None
        for pat, val in collection_paths.items():
            if isinstance(pat, str) and isinstance(val, str) and fnmatch.fnmatch(name, pat):
                rel = val
                break
        if rel is None:
            continue
        # recall/update와 동일한 안전 경계(cwd 내부 또는 allowRoots 내부)를 적용한다.
        if not qmd_resolve_paths.safe_collection_path(cwd_path, rel, roots):
            print(f"skip unsafe collectionPath: {name} -> {rel}", file=sys.stderr)
            continue
        coll_dirs[name] = _resolve_rel(cwd_path, rel)
    selected = {}
    for p in edited_paths:
        ep = Path(p)
        ep = (cwd_path / ep).resolve() if not ep.is_absolute() else ep.resolve()
        best, best_depth = None, -1
        for name, cdir in coll_dirs.items():
            try:
                ep.relative_to(cdir)
            except ValueError:
                continue
            depth = len(cdir.parts)
            if depth > best_depth:
                best, best_depth = name, depth
        if best is not None:
            selected[best] = str(coll_dirs[best])
    return selected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--paths", required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    paths = json.loads(args.paths)
    config = json.loads(args.config)
    print(json.dumps(select_collections(paths, args.cwd, config), ensure_ascii=False))


if __name__ == "__main__":
    main()

"""Current raw collection of a card's source entry, by label or by path.

A card's ``sources``/``sourceRevisions`` entry names its raw collection. After
a raw collection is renamed (for example from ``rccar-docs`` to ``rccar``),
existing cards still carry the old label. The
selected recall reader already resolves such a label by path
(``laya_host_response._revision_raw_root``); the compile side uses the same
rule here, so a card citing a legacy label is the same source as a job for
the current collection:

- a label that is a compile-source collection of the current settings is
  itself;
- a label the settings do not define resolves to the compile-source
  collection whose root holds the project-relative path;
- anything else (a wiki label, a path outside every raw root) keeps its label
  and matches only an equal label.
"""
from __future__ import annotations

from pathlib import Path

import config as qmd_config
from collection_match import select_collections


def resolver(root, config):
    """``label(collection, path)`` for one project and settings snapshot."""
    root = Path(root)
    names = config.get("collections") if isinstance(config.get("collections"), list) else []
    roles = qmd_config.role_map(config)
    cache = {}

    def label(collection, path):
        if not isinstance(path, str) or not path:
            return collection
        if collection in names:
            return collection
        key = (collection, path)
        if key not in cache:
            found = collection
            try:
                selected = select_collections([str(root / path)], str(root), config)
            except (OSError, ValueError):
                selected = {}
            for name in selected:
                if qmd_config.is_compile_source_collection(roles, name):
                    found = name
            cache[key] = found
        return cache[key]
    return label


def decision_resolver(decision):
    """The same rule over a selected route decision (its collections and roots)."""
    project = Path(decision["projectRoot"])
    roots = decision["collectionRoots"]
    roles = {item["name"]: item["role"] for item in decision["collections"]}

    def label(collection, path):
        if collection in roles or not isinstance(path, str) or not path:
            return collection
        candidate = project / path
        for name in sorted(roots):
            if roles.get(name) != "raw":
                continue
            try:
                candidate.relative_to(roots[name])
            except ValueError:
                continue
            return name
        return collection
    return label

"""The wiki collection a project's settings declare."""
from __future__ import annotations

from memvane.settings import api as qmd_config, types as settings_types


def find_wiki_collection(config: dict) -> tuple[str | None, str | None]:
    roles = qmd_config.role_map(config)
    paths = config.get("collectionPaths") if isinstance(config.get("collectionPaths"), dict) else {}
    for name in config.get("collections", []):
        if qmd_config.collection_role(roles, name) == settings_types.COLLECTION_ROLE_WIKI:
            return name, paths.get(name, config.get("wikiPath", settings_types.WIKI_REL))
    return None, None

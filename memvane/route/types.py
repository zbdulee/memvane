"""Value types for route records."""
from __future__ import annotations

import re

SCHEMA = "laya-index-route-v1"
SCHEMA_V2 = "laya-index-route-v2"
SCHEMA_V3 = "memvane-route-v3"
SCHEMA_V4 = "memvane-route-v4"
SELECTION_SCHEMA_V1 = "memvane-selection-v1"
RECORD = "route.json"
QMD_SELECTION_MARKER = "selection.json"
MAX_RECORD_BYTES = 8192
MAX_SETTINGS_BYTES = 2 * 1024 * 1024
MAX_HOOK_BYTES = 2 * 1024 * 1024
SETTINGS_IDENTITY_SCHEMA = "laya-settings-identity-v1"
FIELDS = frozenset({"schema", "status", "projectRoot", "settingsSha256",
                    "dbPath", "queuePath", "dbIncarnation", "scopeId", "collectionName",
                    "sourceRoot", "pluginRoot", "journalPath", "installId",
                    "generation", "bundleManifestSha256",
                    "ownershipManifestSha256", "vectorProfile"})
FIELDS_V2 = (FIELDS - {"collectionName", "sourceRoot"}) | {"collections"}
FIELDS_V3 = FIELDS_V2
FIELDS_V4 = frozenset({"schema", "status", "projectRoot", "settingsSha256",
                       "stateRoot", "stateNode", "storePath", "storeNode",
                       "storeIncarnation", "installId", "scopeId", "stateFormat",
                       "collections", "vectorProfile"})


SHA256 = re.compile(r"[0-9a-f]{64}\Z")

INCARNATION = re.compile(r"[0-9a-f]{32}\Z")

PACKAGE_GENERATION = re.compile(r"g-[0-9a-f]{32}\Z")

ROUTED_ACTIONS = frozenset({"recall", "update", "update-queued", "gate", "posttool",
                            "index", "compile", "file-change-event"})


COLLECTION_FIELDS = frozenset({"name", "role", "sourceRoot"})
COLLECTION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")

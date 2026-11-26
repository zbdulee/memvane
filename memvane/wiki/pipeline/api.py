"""Public application entrypoints for ``memvane.wiki.pipeline``."""
from __future__ import annotations as _annotations


def pipeline_db_path(project_root):
    from memvane.wiki.pipeline.store import pipeline_db_path as implementation
    return implementation(project_root)


def open_pipeline(project_root, *, readonly=False, timeout_ms=None):
    from memvane.wiki.pipeline.store import open_pipeline as implementation
    return implementation(project_root, readonly=readonly, timeout_ms=timeout_ms)


def pipeline_db_path_in(compile_dir):
    from pathlib import Path
    from memvane.wiki.pipeline.types import DB_NAME
    return Path(compile_dir) / DB_NAME


def open_pipeline_dir(compile_dir, *, readonly=False, timeout_ms=None):
    from memvane.wiki.pipeline.store import open_pipeline_dir as implementation
    return implementation(compile_dir, readonly=readonly, timeout_ms=timeout_ms)


def publish_card(project_root, wiki_root, target_path, content, *, expected_target_sha=None,
                 audit_kind=None, audit_payload=None, tx_id=None, lock_context=None,
                 generation=None):
    from memvane.wiki.pipeline.publish import publish_card as implementation
    return implementation(
        project_root, wiki_root, target_path, content,
        expected_target_sha=expected_target_sha,
        audit_kind=audit_kind, audit_payload=audit_payload, tx_id=tx_id, lock_context=lock_context,
        generation=generation,
    )


def recover_publishes(project_root, wiki_root, *, lock_context=None):
    from memvane.wiki.pipeline.publish import recover_publishes as implementation
    return implementation(project_root, wiki_root, lock_context=lock_context)


def needs_v1_migration(project_root, *, wiki_root=None):
    from memvane.wiki.pipeline.migrate import needs_v1_migration as implementation
    return implementation(project_root, wiki_root=wiki_root)


def migrate_from_v1(project_root, *, wiki_root=None):
    from memvane.wiki.pipeline.migrate import migrate_from_v1 as implementation
    return implementation(project_root, wiki_root=wiki_root)


def audit_log(project_root, kind):
    from memvane.wiki.pipeline.records import AuditLog
    return AuditLog(project_root, kind)


def log_rows(project_root, kind):
    from memvane.wiki.pipeline.records import log_rows as implementation
    return implementation(project_root, kind)


def suppressions(project_root, kind):
    from memvane.wiki.pipeline.records import suppressions as implementation
    return implementation(project_root, kind)


def set_suppression(project_root, kind, key, body_hash, payload):
    from memvane.wiki.pipeline.records import set_suppression as implementation
    return implementation(project_root, kind, key, body_hash, payload)


def cooling_keys(project_root, scope):
    from memvane.wiki.pipeline.records import cooling_keys as implementation
    return implementation(project_root, scope)


def cooldown_active(project_root, scope, key='global'):
    from memvane.wiki.pipeline.records import cooldown_active as implementation
    return implementation(project_root, scope, key)


def set_cooldown(project_root, scope, seconds, key='global'):
    from memvane.wiki.pipeline.records import set_cooldown as implementation
    return implementation(project_root, scope, seconds, key)

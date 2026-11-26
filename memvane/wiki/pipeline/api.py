"""Public application entrypoints for ``memvane.wiki.pipeline``."""
from __future__ import annotations as _annotations


def pipeline_db_path(project_root):
    from memvane.wiki.pipeline.store import pipeline_db_path as implementation
    return implementation(project_root)


def open_pipeline(project_root, *, readonly=False, timeout_ms=None):
    from memvane.wiki.pipeline.store import open_pipeline as implementation
    return implementation(project_root, readonly=readonly, timeout_ms=timeout_ms)


def open_pipeline_dir(compile_dir, *, readonly=False, timeout_ms=None):
    from memvane.wiki.pipeline.store import open_pipeline_dir as implementation
    return implementation(compile_dir, readonly=readonly, timeout_ms=timeout_ms)


def publish_card(project_root, wiki_root, target_path, content, *, expected_target_sha=None,
                 stage_id=None, audit_kind=None, audit_payload=None, tx_id=None, lock_context=None):
    from memvane.wiki.pipeline.publish import publish_card as implementation
    return implementation(
        project_root, wiki_root, target_path, content,
        expected_target_sha=expected_target_sha, stage_id=stage_id,
        audit_kind=audit_kind, audit_payload=audit_payload, tx_id=tx_id, lock_context=lock_context,
    )


def recover_publishes(project_root, wiki_root, *, lock_context=None):
    from memvane.wiki.pipeline.publish import recover_publishes as implementation
    return implementation(project_root, wiki_root, lock_context=lock_context)


def needs_v1_migration(project_root, *, wiki_root=None):
    from memvane.wiki.pipeline.migrate import needs_v1_migration as implementation
    return implementation(project_root, wiki_root=wiki_root)


def migrate_from_v1(project_root, *, wiki_root=None, remove_legacy=True):
    from memvane.wiki.pipeline.migrate import migrate_from_v1 as implementation
    return implementation(project_root, wiki_root=wiki_root, remove_legacy=remove_legacy)

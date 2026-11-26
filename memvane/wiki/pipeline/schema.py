"""DDL statements for ``wiki-pipeline.sqlite3`` (zero I/O)."""
from __future__ import annotations

DDL_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS pipeline_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS refresh_events (
        event_id TEXT PRIMARY KEY,
        source_path TEXT NOT NULL,
        ts TEXT NOT NULL,
        engine TEXT NOT NULL,
        state TEXT NOT NULL,
        settled_sha TEXT,
        settled_reason TEXT,
        settled_at REAL,
        resolved_at REAL
    )
    """,
    'CREATE INDEX IF NOT EXISTS idx_refresh_state_path ON refresh_events(state, source_path)',
    """
    CREATE TABLE IF NOT EXISTS compile_jobs (
        job_id TEXT PRIMARY KEY,
        source_collection TEXT NOT NULL,
        source_path TEXT NOT NULL,
        trigger TEXT NOT NULL,
        engine TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        state TEXT NOT NULL,
        claim_pid INTEGER,
        claim_token TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        created_ns INTEGER NOT NULL,
        updated_ns INTEGER NOT NULL
    )
    """,
    'CREATE INDEX IF NOT EXISTS idx_compile_state_source ON compile_jobs(state, source_collection, source_path)',
    """
    CREATE TABLE IF NOT EXISTS verify_jobs (
        job_id TEXT PRIMARY KEY,
        target_path TEXT NOT NULL,
        source_hash TEXT,
        payload_json TEXT NOT NULL,
        state TEXT NOT NULL,
        claim_pid INTEGER,
        claim_token TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        created_ns INTEGER NOT NULL,
        updated_ns INTEGER NOT NULL
    )
    """,
    'CREATE INDEX IF NOT EXISTS idx_verify_state_target ON verify_jobs(state, target_path)',
    """
    CREATE TABLE IF NOT EXISTS staged_cards (
        stage_id TEXT PRIMARY KEY,
        queue TEXT NOT NULL,
        kind TEXT NOT NULL,
        phase TEXT NOT NULL,
        closed INTEGER NOT NULL DEFAULT 0,
        target_path TEXT,
        control_json TEXT NOT NULL,
        raw_blob BLOB,
        image_blob BLOB,
        charged_bytes INTEGER NOT NULL DEFAULT 0,
        created_ns INTEGER NOT NULL,
        updated_ns INTEGER NOT NULL
    )
    """,
    'CREATE INDEX IF NOT EXISTS idx_staged_scan ON staged_cards(queue, kind, closed, created_ns)',
    'CREATE INDEX IF NOT EXISTS idx_staged_target ON staged_cards(target_path, closed)',
    """
    CREATE TABLE IF NOT EXISTS audit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        target_path TEXT,
        source_path TEXT,
        body_hash TEXT,
        payload_json TEXT NOT NULL,
        created_ns INTEGER NOT NULL
    )
    """,
    'CREATE INDEX IF NOT EXISTS idx_audit_kind_target ON audit_events(kind, target_path)',
    'CREATE INDEX IF NOT EXISTS idx_audit_kind_source_hash ON audit_events(kind, source_path, body_hash)',
    """
    CREATE TABLE IF NOT EXISTS cooldowns (
        scope TEXT NOT NULL,
        key TEXT NOT NULL,
        until_ts REAL NOT NULL,
        meta_json TEXT,
        PRIMARY KEY (scope, key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pipeline_fences (
        scope TEXT PRIMARY KEY,
        reason TEXT NOT NULL,
        meta_json TEXT,
        created_ns INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS publish_txns (
        tx_id TEXT PRIMARY KEY,
        target_path TEXT NOT NULL,
        stage_id TEXT,
        temp_name TEXT NOT NULL,
        temp_ino INTEGER NOT NULL,
        temp_sha TEXT NOT NULL,
        expected_target_sha TEXT,
        state TEXT NOT NULL,
        created_ns INTEGER NOT NULL,
        updated_ns INTEGER NOT NULL
    )
    """,
    'CREATE INDEX IF NOT EXISTS idx_publish_state_target ON publish_txns(state, target_path)',
)

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
        reason TEXT,
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
        pending_blob BLOB,
        state TEXT NOT NULL,
        reason TEXT,
        claim_pid INTEGER,
        claim_token TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        created_ns INTEGER NOT NULL,
        updated_ns INTEGER NOT NULL
    )
    """,
    'CREATE INDEX IF NOT EXISTS idx_verify_state_target ON verify_jobs(state, target_path)',
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
        temp_name TEXT NOT NULL,
        temp_ino INTEGER NOT NULL,
        temp_sha TEXT NOT NULL,
        expected_target_sha TEXT,
        generation_json TEXT,
        state TEXT NOT NULL,
        created_ns INTEGER NOT NULL,
        updated_ns INTEGER NOT NULL
    )
    """,
    'CREATE INDEX IF NOT EXISTS idx_publish_state_target ON publish_txns(state, target_path)',
    """
    CREATE TABLE IF NOT EXISTS card_generations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_path TEXT,
        source_hash TEXT,
        canonical_key TEXT,
        explicit INTEGER NOT NULL DEFAULT 0,
        action TEXT,
        row_json TEXT NOT NULL,
        origin_key TEXT UNIQUE,
        created_ns INTEGER NOT NULL
    )
    """,
    'CREATE INDEX IF NOT EXISTS idx_generation_target ON card_generations(target_path, id)',
    'CREATE INDEX IF NOT EXISTS idx_generation_hash ON card_generations(source_hash, id)',
    'CREATE INDEX IF NOT EXISTS idx_generation_key ON card_generations(canonical_key, id)',
    """
    CREATE TABLE IF NOT EXISTS suppressions (
        kind TEXT NOT NULL,
        key TEXT NOT NULL,
        body_hash TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        updated_ns INTEGER NOT NULL,
        PRIMARY KEY (kind, key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dedup_pairs (
        pair_key TEXT PRIMARY KEY,
        page_a TEXT NOT NULL,
        page_b TEXT NOT NULL,
        score REAL,
        state TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_ns INTEGER NOT NULL,
        updated_ns INTEGER NOT NULL
    )
    """,
    'CREATE INDEX IF NOT EXISTS idx_dedup_state ON dedup_pairs(state, created_ns)',
    """
    CREATE TABLE IF NOT EXISTS backfill_checkpoints (
        binding_digest TEXT PRIMARY KEY,
        state_json TEXT NOT NULL,
        raw_blob BLOB NOT NULL,
        image_blob BLOB,
        updated_ns INTEGER NOT NULL
    )
    """,
    'CREATE INDEX IF NOT EXISTS idx_audit_kind_created ON audit_events(kind, created_ns)',
    'CREATE INDEX IF NOT EXISTS idx_compile_state_updated ON compile_jobs(state, updated_ns)',
    'CREATE INDEX IF NOT EXISTS idx_verify_state_updated ON verify_jobs(state, updated_ns)',
)

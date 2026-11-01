"""Structural schema for a separate, inactive wiki-blob candidate file."""
from functools import lru_cache
import sqlite3

from laya_blob_values import canonical, hash_bytes

APP_ID = 0x4D564231
SCHEMA_VERSION = 1
FORMAT = 'memvane-git-blob-candidate-v1'


def _text(name):
    return f"{name} TEXT NOT NULL CHECK(typeof({name})='text')"


def _sha(name):
    return (f"{name} TEXT NOT NULL CHECK(typeof({name})='text' AND length({name})=64 "
            f"AND {name} NOT GLOB '*[^0-9a-f]*')")


def _uuid(name):
    return (f"{name} TEXT NOT NULL CHECK(typeof({name})='text' AND length({name})=32 "
            f"AND {name} NOT GLOB '*[^0-9a-f]*')")


def _integer(name, lower=0):
    return f"{name} INTEGER NOT NULL CHECK(typeof({name})='integer' AND {name}>={lower})"


TABLES = {
    'store_meta': f'''CREATE TABLE store_meta(
      singleton INTEGER PRIMARY KEY CHECK(singleton=1),
      {_text('format')}, schema_version INTEGER NOT NULL CHECK(schema_version=1),
      {_uuid('repo_id')}, {_uuid('scope_id')}, {_uuid('incarnation')},
      common_git_dir TEXT, common_dev INTEGER, common_ino INTEGER,
      {_text('project_rel')}, git_object_format TEXT,
      {_text('repository_json')}, {_sha('settings_policy_digest')},
      {_sha('schema_digest')}, {_integer('created_ns')})''',
    'worktrees': f'''CREATE TABLE worktrees(
      {_uuid('worktree_id')} PRIMARY KEY,
      canonical_root TEXT NOT NULL UNIQUE, git_admin_dir TEXT UNIQUE,
      {_integer('root_dev')}, {_integer('root_ino', 1)}, admin_dev INTEGER, admin_ino INTEGER,
      {_sha('binding_digest')} UNIQUE, {_text('binding_json')}, {_text('project_root')}, {_text('record_json')},
      {_text('settings_path')}, {_sha('settings_sha')}, {_sha('policy_digest')}, {_text('wiki_root')},
      {_integer('wiki_dev')}, {_integer('wiki_ino', 1)}, {_integer('registered_ns')})''',
    'wiki_blobs': f'''CREATE TABLE wiki_blobs(
      {_sha('blob_sha')} PRIMARY KEY, {_text('body')},
      {_integer('byte_length')}, {_integer('created_ns')},
      CHECK(byte_length=length(CAST(body AS BLOB))))''',
    'blob_admissions': f'''CREATE TABLE blob_admissions(
      {_sha('blob_sha')} PRIMARY KEY REFERENCES wiki_blobs(blob_sha),
      {_uuid('worktree_id')} REFERENCES worktrees(worktree_id),
      {_sha('proof_digest')}, {_text('proof_json')}, {_integer('created_ns')})''',
    'chunk_recipes': f'''CREATE TABLE chunk_recipes(
      {_sha('recipe_id')} PRIMARY KEY, {_text('canonical_spec')} CHECK(json_valid(canonical_spec)))''',
    'vector_profiles': f'''CREATE TABLE vector_profiles(
      {_sha('profile_id')} PRIMARY KEY, {_text('model_id')}, {_sha('model_sha')},
      {_sha('embedding_fingerprint')}, {_text('input_recipe')} CHECK(json_valid(input_recipe)),
      {_integer('dimension', 1)} CHECK(dimension<=2048), {_text('metric')} CHECK(metric='cosine'),
      {_text('canonical_spec')} CHECK(json_valid(canonical_spec)))''',
    'chunk_sets': f'''CREATE TABLE chunk_sets(
      {_sha('blob_sha')} REFERENCES wiki_blobs(blob_sha),
      {_sha('recipe_id')} REFERENCES chunk_recipes(recipe_id),
      {_integer('chunk_count', 1)} CHECK(chunk_count<=1024), {_sha('set_digest')},
      PRIMARY KEY(blob_sha,recipe_id))''',
    'chunks': f'''CREATE TABLE chunks(
      {_sha('blob_sha')}, {_sha('recipe_id')}, {_integer('seq')}, {_integer('position')},
      {_text('text')}, {_sha('text_sha')}, PRIMARY KEY(blob_sha,recipe_id,seq),
      FOREIGN KEY(blob_sha,recipe_id) REFERENCES chunk_sets(blob_sha,recipe_id))''',
    'embedding_sets': f'''CREATE TABLE embedding_sets(
      {_sha('blob_sha')}, {_sha('recipe_id')},
      {_sha('profile_id')} REFERENCES vector_profiles(profile_id),
      {_integer('chunk_count', 1)} CHECK(chunk_count<=1024), {_sha('set_digest')},
      PRIMARY KEY(blob_sha,recipe_id,profile_id),
      FOREIGN KEY(blob_sha,recipe_id) REFERENCES chunk_sets(blob_sha,recipe_id))''',
    'vectors': f'''CREATE TABLE vectors(
      {_sha('blob_sha')}, {_sha('recipe_id')}, {_sha('profile_id')}, {_integer('seq')},
      {_sha('input_sha')}, embedding BLOB NOT NULL
        CHECK(typeof(embedding)='blob' AND length(embedding) BETWEEN 4 AND 8192 AND length(embedding)%4=0),
      {_sha('provenance_digest')}, PRIMARY KEY(blob_sha,recipe_id,profile_id,seq),
      FOREIGN KEY(blob_sha,recipe_id,seq) REFERENCES chunks(blob_sha,recipe_id,seq),
      FOREIGN KEY(blob_sha,recipe_id,profile_id) REFERENCES embedding_sets(blob_sha,recipe_id,profile_id))''',
    'source_fingerprints': f'''CREATE TABLE source_fingerprints(
      {_sha('fingerprint_id')} PRIMARY KEY,
      {_uuid('worktree_id')} REFERENCES worktrees(worktree_id),
      {_text('branch_key')}, {_text('path')}, {_sha('source_sha')}, {_integer('byte_length')},
      head_oid TEXT CHECK(head_oid IS NULL OR typeof(head_oid)='text'),
      {_text('tracked_state')} CHECK(tracked_state IN ('tracked','untracked','unknown')),
      {_text('binding_state')} CHECK(binding_state IN ('observed','legacy_unbound')),
      {_uuid('scan_id')}, {_integer('observed_ns')})''',
    'card_attestations': f'''CREATE TABLE card_attestations(
      {_sha('trust_digest')} PRIMARY KEY,
      {_sha('blob_sha')} REFERENCES wiki_blobs(blob_sha),
      {_text('verification_kind')} CHECK(verification_kind IN ('compiled','rebound','legacy_verified')),
      {_text('engine_identity')}, {_sha('policy_digest')}, {_sha('provenance_digest')},
      {_text('receipt_json')} CHECK(json_valid(receipt_json)),
      {_integer('source_count')} CHECK(source_count<=64), {_integer('created_ns')})''',
    'attestation_sources': f'''CREATE TABLE attestation_sources(
      {_sha('trust_digest')} REFERENCES card_attestations(trust_digest),
      {_integer('ordinal')} CHECK(ordinal<64),
      {_sha('fingerprint_id')} REFERENCES source_fingerprints(fingerprint_id),
      {_text('spans_json')} CHECK(json_valid(spans_json)),
      PRIMARY KEY(trust_digest,ordinal), UNIQUE(trust_digest,fingerprint_id))''',
}
KEYS = {'store_meta': (('singleton',),),
        'worktrees': (('worktree_id',), ('canonical_root',), ('git_admin_dir',), ('binding_digest',)),
        'wiki_blobs': (('blob_sha',),), 'blob_admissions': (('blob_sha',),),
        'chunk_recipes': (('recipe_id',),), 'vector_profiles': (('profile_id',),),
        'chunk_sets': (('blob_sha', 'recipe_id'),),
        'chunks': (('blob_sha', 'recipe_id', 'seq'),),
        'embedding_sets': (('blob_sha', 'recipe_id', 'profile_id'),),
        'vectors': (('blob_sha', 'recipe_id', 'profile_id', 'seq'),),
        'source_fingerprints': (('fingerprint_id',),),
        'card_attestations': (('trust_digest',),),
        'attestation_sources': (('trust_digest', 'ordinal'), ('trust_digest', 'fingerprint_id'))}


def statements():
    result = list(TABLES.values())
    for table, keys in KEYS.items():
        tests = []
        for columns in keys:
            condition = ' AND '.join(f'{c}=NEW.{c}' for c in columns)
            tests.append(f'EXISTS(SELECT 1 FROM {table} WHERE {condition})')
        result.append(f'''CREATE TRIGGER {table}_duplicate BEFORE INSERT ON {table}
          WHEN {' OR '.join(tests)} BEGIN SELECT RAISE(ABORT,'immutable_row'); END''')
        for action in ('UPDATE', 'DELETE'):
            result.append(f'''CREATE TRIGGER {table}_{action.lower()} BEFORE {action} ON {table}
              BEGIN SELECT RAISE(ABORT,'immutable_row'); END''')
    result.append('''CREATE TRIGGER attestation_source_ordinal BEFORE INSERT ON attestation_sources
      WHEN NEW.ordinal >= (SELECT source_count FROM card_attestations WHERE trust_digest=NEW.trust_digest)
      BEGIN SELECT RAISE(ABORT,'source_ordinal_invalid'); END''')
    result.extend((
        'CREATE INDEX source_by_checkout_path ON source_fingerprints(worktree_id,branch_key,path,observed_ns)',
        'CREATE INDEX source_by_sha ON source_fingerprints(source_sha)',
        'CREATE INDEX attestation_by_blob ON card_attestations(blob_sha)',
        'CREATE INDEX attestation_by_source ON attestation_sources(fingerprint_id,trust_digest)'))
    return result


def manifest(db):
    rows = sorted(tuple(row) for row in db.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"))
    return hash_bytes(canonical(rows).encode('utf8'))


@lru_cache(maxsize=1)
def expected_manifest():
    db = sqlite3.connect(':memory:')
    try:
        for ddl in statements():
            db.execute(ddl)
        return manifest(db)
    finally:
        db.close()


def create(db):
    for ddl in statements():
        db.execute(ddl)
    db.execute(f'PRAGMA application_id={APP_ID}')
    db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')

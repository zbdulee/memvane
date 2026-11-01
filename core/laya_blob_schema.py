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
      {_text('repository_json')}, {_sha('schema_digest')}, {_integer('created_ns')})''',
    'worktrees': f'''CREATE TABLE worktrees(
      {_uuid('worktree_id')} PRIMARY KEY,
      canonical_root TEXT NOT NULL UNIQUE, git_admin_dir TEXT UNIQUE,
      {_integer('root_dev')}, {_integer('root_ino', 1)}, admin_dev INTEGER, admin_ino INTEGER,
      {_sha('binding_digest')} UNIQUE, {_text('project_root')}, {_text('record_json')},
      {_text('settings_path')}, {_sha('settings_sha')}, {_text('wiki_root')},
      {_integer('wiki_dev')}, {_integer('wiki_ino', 1)}, {_integer('registered_ns')})''',
    'wiki_blobs': f'''CREATE TABLE wiki_blobs(
      {_sha('blob_sha')} PRIMARY KEY, {_text('body')},
      {_integer('byte_length')}, {_integer('created_ns')},
      CHECK(byte_length=length(CAST(body AS BLOB))))''',
    'blob_admissions': f'''CREATE TABLE blob_admissions(
      {_sha('blob_sha')} PRIMARY KEY REFERENCES wiki_blobs(blob_sha),
      {_uuid('worktree_id')} REFERENCES worktrees(worktree_id),
      {_sha('proof_digest')}, {_text('proof_json')}, {_integer('created_ns')})''',
}
KEYS = {'store_meta': (('singleton',),),
        'worktrees': (('worktree_id',), ('canonical_root',), ('git_admin_dir',), ('binding_digest',)),
        'wiki_blobs': (('blob_sha',),), 'blob_admissions': (('blob_sha',),)}


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

"""Separate-DB claim queue for explicit Laya Index A-int bindings.

Only explicit caller-provided paths. A selected host must prove installation
and hold the queue-adjacent sync owner lock before recovering a prior lease.
A receipt in the index DB precedes a conditional ACK here. Process death and
redelivery can be tested; cross-DB exactly-once or power-loss safety is not
claimed. A recovered lease requires external proof that its old owner ended.
Unbound v1/v2 queues remain synthetic/legacy inputs; a runtime compares a
v2 queue binding to the selected index DB incarnation before using any job.
"""
from __future__ import annotations

import os
import hashlib
import json
from contextlib import closing
from pathlib import Path
import re
import sqlite3
import uuid

from laya_index_a import IndexContractError

ACK, REPROCESS, DEFER, LEASE_LOST = 'ack', 'reprocess', 'defer', 'lease_lost'
FIELDS = ('claim', 'scope', 'gen', 'source_manifest', 'claim_digest',
          'claim_epoch', 'base_gen')
SHA256 = re.compile(r'[0-9a-f]{64}\Z')
APP_ID = 0x4c595131  # LYQ1
SCHEMA_VERSION = 2
LEASE_TOKEN = re.compile(r'[0-9a-f]{32}\Z')
INCARNATION = LEASE_TOKEN

_JOBS_SQL = '''CREATE TABLE jobs(
                  claim TEXT PRIMARY KEY, scope TEXT NOT NULL, gen TEXT NOT NULL,
                  source_manifest TEXT NOT NULL, claim_digest TEXT NOT NULL,
                  claim_epoch INTEGER NOT NULL, base_gen TEXT,
                  state TEXT NOT NULL CHECK(state IN ('pending','leased','acked','replan')),
                  lease_token TEXT, outcome TEXT)'''
_IDENTITY_SEAL_SQL = '''CREATE TRIGGER identity_seal BEFORE UPDATE ON jobs
                WHEN NEW.claim IS NOT OLD.claim OR NEW.scope IS NOT OLD.scope
                  OR NEW.gen IS NOT OLD.gen OR NEW.source_manifest IS NOT OLD.source_manifest
                  OR NEW.claim_digest IS NOT OLD.claim_digest
                  OR NEW.claim_epoch IS NOT OLD.claim_epoch OR NEW.base_gen IS NOT OLD.base_gen
                BEGIN SELECT RAISE(ABORT,'claim_immutable'); END'''
_DELETE_SEAL_SQL = '''CREATE TRIGGER delete_seal BEFORE DELETE ON jobs
                BEGIN SELECT RAISE(ABORT,'claim_immutable'); END'''
_INSERT_SEAL_SQL = '''CREATE TRIGGER insert_seal BEFORE INSERT ON jobs
                WHEN EXISTS (SELECT 1 FROM jobs WHERE claim=NEW.claim)
                BEGIN SELECT RAISE(ABORT,'claim_immutable'); END'''
_QUEUE_META_SQL = '''CREATE TABLE queue_meta(
                  key TEXT PRIMARY KEY, value TEXT)'''
_QUEUE_META_INSERT_SEAL_SQL = '''CREATE TRIGGER queue_meta_insert_seal BEFORE INSERT ON queue_meta
                BEGIN SELECT RAISE(ABORT,'queue_binding_immutable'); END'''
_QUEUE_META_UPDATE_SEAL_SQL = '''CREATE TRIGGER queue_meta_update_seal BEFORE UPDATE ON queue_meta
                BEGIN SELECT RAISE(ABORT,'queue_binding_immutable'); END'''
_QUEUE_META_DELETE_SEAL_SQL = '''CREATE TRIGGER queue_meta_delete_seal BEFORE DELETE ON queue_meta
                BEGIN SELECT RAISE(ABORT,'queue_binding_immutable'); END'''
_SCHEMA_V1_SQL = (_JOBS_SQL, _IDENTITY_SEAL_SQL, _DELETE_SEAL_SQL,
                  _INSERT_SEAL_SQL)
_SCHEMA_V2_SQL = (_JOBS_SQL, _QUEUE_META_SQL, _IDENTITY_SEAL_SQL,
                  _DELETE_SEAL_SQL, _INSERT_SEAL_SQL,
                  _QUEUE_META_INSERT_SEAL_SQL, _QUEUE_META_UPDATE_SEAL_SQL,
                  _QUEUE_META_DELETE_SEAL_SQL)
_EXPECTED_V1_OBJECTS = {
    ('table', 'jobs', 'jobs'): _JOBS_SQL,
    ('index', 'sqlite_autoindex_jobs_1', 'jobs'): None,
    ('trigger', 'identity_seal', 'jobs'): _IDENTITY_SEAL_SQL,
    ('trigger', 'delete_seal', 'jobs'): _DELETE_SEAL_SQL,
    ('trigger', 'insert_seal', 'jobs'): _INSERT_SEAL_SQL,
}
_EXPECTED_V2_OBJECTS = {
    **_EXPECTED_V1_OBJECTS,
    ('table', 'queue_meta', 'queue_meta'): _QUEUE_META_SQL,
    ('index', 'sqlite_autoindex_queue_meta_1', 'queue_meta'): None,
    ('trigger', 'queue_meta_insert_seal', 'queue_meta'): _QUEUE_META_INSERT_SEAL_SQL,
    ('trigger', 'queue_meta_update_seal', 'queue_meta'): _QUEUE_META_UPDATE_SEAL_SQL,
    ('trigger', 'queue_meta_delete_seal', 'queue_meta'): _QUEUE_META_DELETE_SEAL_SQL,
}


class ClaimConflict(ValueError):
    pass


class UnknownDatabase(ValueError):
    pass


def payload_digest(scope, gen, source_manifest, claim_epoch, base_gen):
    """Digest the complete index effect identity that the queue persists."""
    data = json.dumps([scope, gen, source_manifest, claim_epoch, base_gen],
                      ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def _identity(job):
    if not isinstance(job, dict) or any(key not in job for key in FIELDS):
        raise ValueError('invalid_claim_job')
    values = tuple(job[key] for key in FIELDS)
    claim, scope, gen, manifest, digest, epoch, base = values
    if (not all(isinstance(v, str) and v for v in (claim, scope, gen))
            or not all(isinstance(v, str) and SHA256.fullmatch(v) for v in (manifest, digest))
            or type(epoch) is not int or epoch < 0
            or (base is not None and (not isinstance(base, str) or not base))
            or digest != payload_digest(scope, gen, manifest, epoch, base)):
        raise ValueError('invalid_claim_job')
    return values


class ClaimQueue:
    def __init__(self, path, db):
        self.path = Path(path)
        self.db = db

    @staticmethod
    def _path(path):
        path = Path(path)
        if not path.is_absolute() or not path.parent.is_dir() or path.is_symlink():
            raise UnknownDatabase('invalid_explicit_path')
        return path

    @classmethod
    def create(cls, path, *, index_incarnation=None):
        if (index_incarnation is not None and
                (not isinstance(index_incarnation, str)
                 or INCARNATION.fullmatch(index_incarnation) is None)):
            raise ValueError('invalid_index_incarnation')
        path = cls._path(path)
        if any(Path(str(path) + s).exists() or Path(str(path) + s).is_symlink()
               for s in ('-wal', '-shm', '-journal')):
            raise UnknownDatabase('stale_sidecar')
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        db = sqlite3.connect(path, timeout=2, isolation_level=None)
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            for statement in (_JOBS_SQL, _QUEUE_META_SQL):
                db.execute(statement)
            db.execute("INSERT INTO queue_meta VALUES('index_incarnation',?)",
                       (index_incarnation,))
            for statement in _SCHEMA_V2_SQL[2:]:
                db.execute(statement)
            db.execute(f'PRAGMA application_id={APP_ID}')
            db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
            db.commit()
            cls._check_database(db)
            return cls(path, db)
        except BaseException:
            db.rollback()
            db.close()
            # The exclusive path was created by this call and no job was stored.
            path.unlink(missing_ok=True)
            raise

    @classmethod
    def open(cls, path):
        path = cls._path(path)
        if not path.is_file():
            raise UnknownDatabase('queue_missing')
        uri = path.resolve(strict=True).as_uri()
        try:
            # Classify a single read snapshot before requesting RW/WAL access.
            with closing(sqlite3.connect(uri + '?mode=ro', uri=True, timeout=2,
                                         isolation_level=None)) as check:
                check.execute('BEGIN')
                try:
                    cls._check_database(check)
                    check.commit()
                except BaseException:
                    check.rollback()
                    raise
            db = sqlite3.connect(uri + '?mode=rw', uri=True, timeout=2,
                                 isolation_level=None)
            db.execute('BEGIN')
            try:
                cls._check_database(db)  # classify the RW file again after path race
                db.commit()
            except BaseException:
                db.rollback()
                raise
            db.execute('PRAGMA foreign_keys=ON')
            if str(db.execute('PRAGMA journal_mode=WAL').fetchone()[0]).lower() != 'wal':
                raise UnknownDatabase('queue_wal_unavailable')
            db.execute('PRAGMA synchronous=FULL')
            return cls(path, db)
        except BaseException as exc:
            if 'db' in locals():
                db.close()
            if isinstance(exc, UnknownDatabase):
                raise
            if isinstance(exc, sqlite3.Error):
                raise UnknownDatabase('queue_unreadable') from exc
            raise

    @staticmethod
    def _check_database(db):
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if (db.execute('PRAGMA application_id').fetchone()[0] != APP_ID
                or version not in (1, SCHEMA_VERSION)):
            raise UnknownDatabase('queue_schema_unknown')
        rows = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master").fetchall()
        actual = {(kind, name, table): sql for kind, name, table, sql in rows}
        expected = _EXPECTED_V1_OBJECTS if version == 1 else _EXPECTED_V2_OBJECTS
        if len(rows) != len(expected) or actual != expected:
            raise UnknownDatabase('queue_schema_unknown')
        if version == 1:
            binding = None
        else:
            meta = db.execute('SELECT key,value FROM queue_meta').fetchall()
            if (len(meta) != 1 or meta[0][0] != 'index_incarnation'
                    or (meta[0][1] is not None and
                        (not isinstance(meta[0][1], str)
                         or INCARNATION.fullmatch(meta[0][1]) is None))):
                raise UnknownDatabase('queue_binding_invalid')
            binding = meta[0][1]
        seen_tokens = set()
        for row in db.execute('''SELECT claim,scope,gen,source_manifest,claim_digest,
            claim_epoch,base_gen,state,lease_token,outcome FROM jobs'''):
            job = dict(zip((*FIELDS, 'state', 'lease_token', 'outcome'), row))
            try:
                _identity(job)
            except ValueError as exc:
                raise UnknownDatabase('queue_identity_invalid') from exc
            state, token, outcome = job['state'], job['lease_token'], job['outcome']
            if state == 'pending':
                valid = token is None and outcome is None
            else:
                valid = (isinstance(token, str) and LEASE_TOKEN.fullmatch(token)
                         and token not in seen_tokens
                         and ((state == 'leased' and outcome is None)
                              or (state == 'acked' and outcome == 'published')
                              or (state == 'replan' and outcome in
                                  ('head_moved', 'index_stale'))))
                if valid:
                    seen_tokens.add(token)
            if not valid:
                raise UnknownDatabase('queue_state_invalid')
        return binding

    def binding(self):
        """Read the immutable index DB identity; None means an unbound queue."""
        self.db.execute('BEGIN')
        try:
            binding = self._check_database(self.db)
            self.db.commit()
            return binding
        except BaseException:
            self.db.rollback()
            raise

    def close(self):
        self.db.close()

    def _begin(self):
        self.db.execute('BEGIN IMMEDIATE')

    def _row(self, claim):
        row = self.db.execute('''SELECT claim,scope,gen,source_manifest,claim_digest,
            claim_epoch,base_gen,state,lease_token,outcome FROM jobs WHERE claim=?''',
            (claim,)).fetchone()
        return None if row is None else dict(zip((*FIELDS, 'state', 'lease_token', 'outcome'), row))

    def get(self, claim):
        return self._row(claim)

    def active_for_scope(self, scope):
        """Read retained work in epoch order; terminal jobs remain as evidence."""
        if not isinstance(scope, str) or not scope:
            raise ValueError('invalid_scope')
        self.db.execute('BEGIN')
        try:
            self._check_database(self.db)
            rows = self.db.execute('''SELECT claim,scope,gen,source_manifest,claim_digest,
                claim_epoch,base_gen,state,lease_token,outcome FROM jobs
                WHERE scope=? AND state IN ('pending','leased')
                ORDER BY claim_epoch,claim''', (scope,)).fetchall()
            self.db.commit()
            return [dict(zip((*FIELDS, 'state', 'lease_token', 'outcome'), row))
                    for row in rows]
        except BaseException:
            self.db.rollback()
            raise

    def enqueue_next(self, scope, gen, source_manifest, base_gen):
        """Allocate an epoch and persist a fresh full-scan claim atomically."""
        if (not isinstance(scope, str) or not scope or not isinstance(gen, str)
                or not gen or not isinstance(source_manifest, str)
                or SHA256.fullmatch(source_manifest) is None
                or (base_gen is not None and
                    (not isinstance(base_gen, str) or not base_gen))):
            raise ValueError('invalid_claim_job')
        self._begin()
        try:
            epoch = self.db.execute(
                'SELECT COALESCE(MAX(claim_epoch),0)+1 FROM jobs WHERE scope=?',
                (scope,)).fetchone()[0]
            job = dict(claim=uuid.uuid4().hex, scope=scope, gen=gen,
                       source_manifest=source_manifest, claim_epoch=epoch,
                       base_gen=base_gen,
                       claim_digest=payload_digest(scope, gen, source_manifest,
                                                   epoch, base_gen))
            values = _identity(job)
            self.db.execute('''INSERT INTO jobs
                (claim,scope,gen,source_manifest,claim_digest,claim_epoch,base_gen,state)
                VALUES(?,?,?,?,?,?,?,'pending')''', values)
            self.db.commit()
            return job
        except BaseException:
            self.db.rollback()
            raise

    def enqueue(self, job):
        values = _identity(job)
        self._begin()
        try:
            current = self._row(values[0])
            if current is not None:
                if _identity(current) != values:
                    raise ClaimConflict('claim_identity_changed')
                self.db.rollback()
                return current['state']
            self.db.execute('''INSERT INTO jobs
                (claim,scope,gen,source_manifest,claim_digest,claim_epoch,base_gen,state)
                VALUES(?,?,?,?,?,?,?,'pending')''', values)
            self.db.commit()
            return 'pending'
        except BaseException:
            self.db.rollback()
            raise

    def lease(self, claim, *, recover=False):
        self._begin()
        try:
            row = self._row(claim)
            if row is None or not (row['state'] == 'pending' or
                                   (row['state'] == 'leased' and recover is True)):
                self.db.rollback()
                return None
            token = uuid.uuid4().hex
            self.db.execute("UPDATE jobs SET state='leased',lease_token=? WHERE claim=?",
                            (token, claim))
            self.db.commit()
            return {**row, 'state': 'leased', 'lease_token': token}
        except BaseException:
            self.db.rollback()
            raise

    @staticmethod
    def decide(index, job):
        claim, scope, gen, manifest, digest, epoch, _base = _identity(job)
        try:
            receipt = index.receipt_detail(claim)
        except (sqlite3.Error, OSError, IndexContractError):
            return DEFER
        if receipt is None:
            return REPROCESS
        return ACK if receipt == (scope, gen, manifest, digest, epoch) else DEFER

    def conditional_ack(self, job, token, index):
        verdict = self.decide(index, job)
        if verdict != ACK:
            return verdict
        identity = _identity(job)
        self._begin()
        try:
            current = self._row(identity[0])
            if (current is None or _identity(current) != identity
                    or current['lease_token'] != token
                    or current['state'] not in ('leased', 'acked')):
                self.db.rollback()
                return LEASE_LOST
            if current['state'] == 'leased':
                self.db.execute("UPDATE jobs SET state='acked',outcome='published' WHERE claim=?",
                                (identity[0],))
            self.db.commit()
            return ACK
        except BaseException:
            self.db.rollback()
            raise

    def mark_replan(self, job, token, reason):
        if reason not in ('head_moved', 'index_stale'):
            raise ValueError('invalid_replan_reason')
        identity = _identity(job)
        self._begin()
        try:
            current = self._row(identity[0])
            if (current is None or _identity(current) != identity
                    or current['lease_token'] != token or current['state'] != 'leased'):
                self.db.rollback()
                return False
            self.db.execute("UPDATE jobs SET state='replan',outcome=? WHERE claim=?",
                            (reason, identity[0]))
            self.db.commit()
            return True
        except BaseException:
            self.db.rollback()
            raise

    def release_for_retry(self, job, token):
        """Only the current lease may return a claim to pending after refusal."""
        identity = _identity(job)
        self._begin()
        try:
            current = self._row(identity[0])
            if (current is None or _identity(current) != identity
                    or current['lease_token'] != token or current['state'] != 'leased'):
                self.db.rollback()
                return False
            self.db.execute("UPDATE jobs SET state='pending',lease_token=NULL WHERE claim=?",
                            (identity[0],))
            self.db.commit()
            return True
        except BaseException:
            self.db.rollback()
            raise

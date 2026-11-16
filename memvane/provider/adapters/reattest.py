"""Owner-only proof that execution artifacts preserve embedding identity."""
import json
import os
import re
import stat
import time
from pathlib import Path

from memvane.kernel.atomic_rename import atomic_json, private_state, fsync_dir

SCHEMA = 'memvane-provider-reattest-v1'
FILE = 'provider-reattest.json'
PAIRS_SCHEMA = 'memvane-provider-reattest-pairs-v1'
PAIRS_FILE = 'provider-reattest-pairs.json'
PAIRS_LIMIT = 16
_FIELDS = frozenset(('modelSha256', 'tokenizer', 'chunkerSha256', 'chunkRules',
                     'dimension', 'metric', 'pooling', 'normalization', 'maxTokens'))
_SHA = re.compile(r'^[0-9a-f]{64}$')


def _pair(value):
    return (type(value) is dict and set(value) == {'worker', 'provider'} and
            all(type(v) is str and _SHA.fullmatch(v) for v in value.values()))


def _identity(value):
    return (type(value) is dict and set(value) == _FIELDS and
            all(type(value[k]) is str and value[k] for k in _FIELDS - {'dimension', 'maxTokens'}) and
            _SHA.fullmatch(value['modelSha256']) and _SHA.fullmatch(value['chunkerSha256']) and
            type(value['dimension']) is int and 1 <= value['dimension'] <= 2048 and
            type(value['maxTokens']) is int and 1 <= value['maxTokens'] <= 65536)


def _read(state):
    state = private_state(state)
    fd = os.open(state / FILE, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or info.st_mode & 0o077 or info.st_size > 16384):
            raise ValueError('reattest_file_unsafe')
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            return json.load(stream)
    finally:
        if fd is not None:
            os.close(fd)


def _record_valid(record):
    return (type(record) is dict and set(record) == {'attested', 'current', 'attestedIdentity',
                'currentIdentity', 'outcome', 'timeNs'} and record['outcome'] == 'accepted'
            and _pair(record['attested']) and _pair(record['current'])
            and _identity(record['attestedIdentity'])
            and record['attestedIdentity'] == record['currentIdentity']
            and type(record['timeNs']) is int and record['timeNs'] > 0)


def _pairs(state):
    state = private_state(state)
    try:
        fd = os.open(state / PAIRS_FILE, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or info.st_mode & 0o077 or info.st_size > 128 * 1024):
            raise ValueError('reattest_pairs_unsafe')
        def unique(items):
            value = {}
            for key, item in items:
                if key in value:
                    raise ValueError('reattest_pairs_invalid')
                value[key] = item
            return value
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            row = json.load(stream, object_pairs_hook=unique)
            after = os.fstat(stream.fileno())
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError('reattest_pairs_changed')
        if (type(row) is not dict or set(row) != {'schema', 'records'}
                or row['schema'] != PAIRS_SCHEMA or type(row['records']) is not list
                or len(row['records']) > PAIRS_LIMIT or not all(_record_valid(r) for r in row['records'])):
            raise ValueError('reattest_pairs_invalid')
        keys = [(r['attested']['worker'], r['attested']['provider'],
                 r['current']['worker'], r['current']['provider']) for r in row['records']]
        if len(keys) != len(set(keys)):
            raise ValueError('reattest_pairs_invalid')
        identities = {}
        for record in row['records']:
            key = (record['attested']['worker'], record['attested']['provider'])
            if key in identities and identities[key] != record['attestedIdentity']:
                raise ValueError('reattest_pairs_invalid')
            identities[key] = record['attestedIdentity']
        return row['records']
    finally:
        if fd is not None:
            os.close(fd)


def _legacy_valid(state, original, current, identity):
    record = _read(state)
    return (type(record) is dict and record.get('schema') == SCHEMA
            and _record_valid({key: value for key, value in record.items() if key != 'schema'})
            and record['attested'] == original and record['current'] == current
            and record['attestedIdentity'] == identity)


def valid(state, original, current, identity, *, require_legacy=False):
    try:
        if not _pair(original) or not _pair(current) or not _identity(identity):
            return False
        records = _pairs(state)
        if records is None:
            return _legacy_valid(state, original, current, identity)
        found = any(r['attested'] == original and r['current'] == current
                    and r['attestedIdentity'] == identity for r in records)
        return found and (not require_legacy or _legacy_valid(state, original, current, identity))
    except (OSError, ValueError, TypeError, UnicodeError):
        return False


def write(state, original, current, old_identity, new_identity):
    state = private_state(state)
    if (not _pair(original) or not _pair(current) or not _identity(old_identity)
            or not _identity(new_identity) or old_identity != new_identity):
        raise ValueError('reattest_identity_changed')
    record = {'schema': SCHEMA, 'attested': original, 'current': current,
              'attestedIdentity': old_identity, 'currentIdentity': new_identity,
              'outcome': 'accepted', 'timeNs': time.time_ns()}
    target = state / FILE
    prior = None
    if target.is_symlink():
        raise ValueError('reattest_file_unsafe')
    if target.exists():
        prior = _read(state)
        if (type(prior) is not dict or prior.get('schema') != SCHEMA
                or not _record_valid({key: value for key, value in prior.items() if key != 'schema'})):
            raise ValueError('reattest_file_invalid')
        if prior['attested'] != original or prior['attestedIdentity'] != old_identity:
            raise ValueError('reattest_identity_changed')
    records = _pairs(state)
    if records is None:
        records = ([{key: value for key, value in prior.items() if key != 'schema'}]
                   if prior is not None else [])
    if any(r['attested'] == original and r['attestedIdentity'] != old_identity for r in records):
        raise ValueError('reattest_identity_changed')
    records = [r for r in records if (r['attested'], r['current']) != (original, current)]
    records.append({key: value for key, value in record.items() if key != 'schema'})
    records.sort(key=lambda r: r['timeNs'], reverse=True)
    atomic_json(state / PAIRS_FILE, {'schema': PAIRS_SCHEMA, 'records': records[:PAIRS_LIMIT]})
    atomic_json(target, record)
    fsync_dir(state)
    return record

# v1.0.0, v1.0.1 and v1.0.2 contain this exact pair. Their worker asks
# node-llama-cpp for the model-default pooling and returns raw vectors.
LEGACY_WORKER = 'f71bfb3e323f4a4de72df33a699fb4c57b8de69004598bb78819055e50e36c06'
LEGACY_PROVIDER = '87cdbd2b1144c816eaf2ddd71bac3f68c6876c8f1548f29368da05882732e8ee'
LEGACY_CHUNKER = '6e8b4f2fb2604128180590f6c15eb9c7c5b7868c34fbbdd3216a217818e76305'
LEGACY_PAIR = {'worker': LEGACY_WORKER, 'provider': LEGACY_PROVIDER}
# Previous release (1.0.7): same provider and chunker, worker identity above.
PREVIOUS_PAIR = {'worker': '8d6fdd22432d22c3256015da501ff6ef063a899f8c10e198831f1db99509a43c',
                 'provider': LEGACY_PROVIDER}
LEGACY_SEMANTICS = {'pooling': 'model-default', 'normalization': 'none', 'maxTokens': 2048}


def _current(manifest):
    from memvane.kernel.bundle_paths import PROVIDER_RELATIVE_PATH, WORKER_RELATIVE_PATH, bundle_file
    from memvane.provider.api import hash_file
    worker = bundle_file(WORKER_RELATIVE_PATH)
    provider = bundle_file(PROVIDER_RELATIVE_PATH)
    chunker = worker.parent / 'laya_qmd_chunking.mjs'
    raw = worker.read_text(encoding='utf-8')
    found = re.findall(r'^export const EMBEDDING_IDENTITY = (\{[^\n]+\});$', raw, re.M)
    if len(found) != 1:
        raise ValueError('reattest_manifest_missing')
    semantics = json.loads(found[0])
    if type(semantics) is not dict or set(semantics) != set(LEGACY_SEMANTICS):
        raise ValueError('reattest_manifest_invalid')
    identity = {'modelSha256': manifest['modelSha256'], 'tokenizer': 'model-embedded',
                'chunkerSha256': hash_file(str(chunker)), 'chunkRules': '900/135/200',
                'dimension': manifest['dimension'], 'metric': 'cosine', **semantics}
    if not _identity(identity):
        raise ValueError('reattest_manifest_invalid')
    return {'worker': hash_file(str(worker)), 'provider': hash_file(str(provider))}, identity


def _attested_identity(state, original, manifest, current, identity):
    if original in (LEGACY_PAIR, PREVIOUS_PAIR):
        return {**identity, 'chunkerSha256': LEGACY_CHUNKER, **LEGACY_SEMANTICS}
    try:
        old = _read(state)
        old_identity = old['attestedIdentity']
        if (valid(state, original, old['current'], old_identity)
                and _identity(old_identity)):
            return old_identity
    except (OSError, ValueError, KeyError, TypeError):
        pass
    records = _pairs(state)
    if records is not None:
        for record in records:
            if record['attested'] == original:
                return record['attestedIdentity']
    if original == current:
        return identity
    raise ValueError('reattest_legacy_unknown')


def authorize(state, manifest):
    from memvane.provider.api import hash_file
    original = {'worker': manifest['workerSha256'], 'provider': manifest['providerArtifactSha256']}
    actual = {'worker': hash_file(manifest['workerPath']), 'provider': hash_file(manifest['providerPath'])}
    if original == actual:
        return actual
    current, identity = _current(manifest)
    if actual != current:
        raise ValueError('reattest_not_authorized')
    old_identity = _attested_identity(state, original, manifest, current, identity)
    if old_identity != identity or not valid(state, original, current, identity):
        raise ValueError('reattest_not_authorized')
    return current


def check(state, manifest):
    try:
        authorize(state, manifest)
        return True
    except (OSError, KeyError, TypeError):
        return False
    except ValueError as exc:
        if str(exc) == 'reattest_not_authorized':
            return False
        raise


def update(state, manifest):
    original = {'worker': manifest['workerSha256'], 'provider': manifest['providerArtifactSha256']}
    current, identity = _current(manifest)
    old_identity = _attested_identity(state, original, manifest, current, identity)
    return write(state, original, current, old_identity, identity)


def differences(state, manifest):
    original = {'worker': manifest['workerSha256'], 'provider': manifest['providerArtifactSha256']}
    current, identity = _current(manifest)
    old = _attested_identity(state, original, manifest, current, identity)
    return sorted(key for key in _FIELDS if old[key] != identity[key])

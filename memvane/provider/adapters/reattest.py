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
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 16384):
            raise ValueError('reattest_file_unsafe')
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            return json.load(stream)
    finally:
        if fd is not None:
            os.close(fd)


def valid(state, original, current, identity):
    try:
        record = _read(state)
        return (type(record) is dict and set(record) == {'schema', 'attested', 'current',
                    'attestedIdentity', 'currentIdentity', 'outcome', 'timeNs'} and
                record['schema'] == SCHEMA and record['outcome'] == 'accepted' and
                _pair(original) and _pair(current) and _identity(identity) and
                record['attested'] == original and record['current'] == current and
                record['attestedIdentity'] == identity == record['currentIdentity'] and
                type(record['timeNs']) is int and record['timeNs'] > 0)
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
    if target.is_symlink():
        raise ValueError('reattest_file_unsafe')
    atomic_json(target, record)
    fsync_dir(state)
    return record

# v1.0.0, v1.0.1 and v1.0.2 contain this exact pair. Their worker asks
# node-llama-cpp for the model-default pooling and returns raw vectors.
LEGACY_WORKER = 'f71bfb3e323f4a4de72df33a699fb4c57b8de69004598bb78819055e50e36c06'
LEGACY_PROVIDER = '87cdbd2b1144c816eaf2ddd71bac3f68c6876c8f1548f29368da05882732e8ee'
LEGACY_CHUNKER = '6e8b4f2fb2604128180590f6c15eb9c7c5b7868c34fbbdd3216a217818e76305'
LEGACY_PAIR = {'worker': LEGACY_WORKER, 'provider': LEGACY_PROVIDER}
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
    if original == LEGACY_PAIR:
        return {**identity, 'chunkerSha256': LEGACY_CHUNKER, **LEGACY_SEMANTICS}
    try:
        old = _read(state)
        old_identity = old['attestedIdentity']
        if (valid(state, original, old['current'], old_identity)
                and _identity(old_identity)):
            return old_identity
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if original == current:
        return identity
    raise ValueError('reattest_legacy_unknown')


def authorize(state, manifest):
    original = {'worker': manifest['workerSha256'], 'provider': manifest['providerArtifactSha256']}
    current, identity = _current(manifest)
    if original != current:
        old_identity = _attested_identity(state, original, manifest, current, identity)
        if old_identity != identity or not valid(state, original, current, identity):
            raise ValueError('reattest_not_authorized')
    return current


def check(state, manifest):
    try:
        authorize(state, manifest)
        return True
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

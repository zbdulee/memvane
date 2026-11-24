"""Private source and sibling checkpoints for a frozen, native-owned backfill."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile

from memvane.kernel.atomic_rename import canonical_json_bytes, fsync_dir, write_new_file

LIMIT = 8 * 1024 * 1024


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _read(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                or info.st_nlink != 1 or info.st_size > LIMIT):
            raise OSError('unsafe backfill checkpoint')
        blocks = []
        total = 0
        while total < info.st_size:
            block = source.read(min(65536, info.st_size - total))
            if not block:
                break
            blocks.append(block)
            total += len(block)
        data = b''.join(blocks)
        if len(data) != info.st_size or not os.path.samestat(info, path.lstat()):
            raise OSError('backfill checkpoint changed')
        return data


def _write(path, state):
    data = canonical_json_bytes(state)
    if len(data) > LIMIT:
        raise OSError('backfill checkpoint exceeds pass budget')
    fd, name = tempfile.mkstemp(prefix='.w-', dir=path.parent)
    tmp = path.with_name(os.path.basename(name))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if _read(tmp) != data:
            raise OSError('incomplete backfill checkpoint')
        os.replace(tmp, path)
        fsync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def _directory(base, identity):
    for name in ('.handoff', 'v1', 'backfill', identity):
        child = base / name
        try:
            child.mkdir(mode=0o700)
        except FileExistsError:
            pass
        else:
            fsync_dir(base)
        info = child.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise OSError('unsafe private backfill directory')
        base = child
    return base


class FrozenWork:
    """The compile run lock serializes this frozen item's source and candidate evidence."""

    def __init__(self, queue, text, job, frozen_item):
        self.source = (text + '\n').encode()
        binding = {key: value for key, value in job.items() if key not in (
            'partialCompile', '_processedContentRevision', '_fallbackAttempts', '_requeue_count', '_backfillSkipped')}
        self.revision = frozen_item[2]
        self.binding = {'sourceJob': binding, 'cohort': list(frozen_item)}
        identity = digest(canonical_json_bytes(self.binding))
        self.folder = _directory(queue.parent, identity)
        self.path = self.folder / 'work'
        self.state = None
        if os.path.lexists(self.path):
            self.state = json.loads(_read(self.path))
            raw = _read(self.folder / 'raw')
            if (not isinstance(self.state, dict) or self.state.get('version') != 1
                    or self.state.get('binding') != self.binding
                    or self.state.get('rawDigest') != digest(raw)
                    or self.state.get('rawLength') != len(raw)
                    or self.state.get('stage') not in ('claimed', 'saving', 'siblings', 'settled')
                    or not isinstance(self.state.get('results'), dict)
                    or not isinstance(self.state.get('engine'), str)):
                raise OSError('unrecognized frozen backfill work')
        else:
            raw = self.folder / 'raw'
            if not os.path.lexists(raw):
                write_new_file(raw, self.source)
            if _read(raw) != self.source:
                raise OSError('frozen backfill occurrence changed')
            self.state = {'version': 1, 'binding': self.binding, 'stage': 'claimed', 'engine': job.get('engine'),
                          'rawLength': len(self.source), 'rawDigest': digest(self.source),
                          'results': {}, 'next': 'native_extractor'}
            _write(self.path, self.state)
        if json.loads(_read(self.path)) != self.state:
            raise OSError('incomplete frozen backfill record')
        if self.state['stage'] == 'saving' and os.path.lexists(self.folder / 'image'):
            self.answer()
            self.state.update(stage='siblings', next='native_writer')
            _write(self.path, self.state)
        if self.has_answer:
            self.answer()

    @property
    def has_answer(self):
        return self.state.get('stage') == 'siblings'

    def prepare(self, extracted, engine):
        if not self.has_answer:
            image = canonical_json_bytes(extracted)
            if len(image) > LIMIT:
                raise OSError('frozen answer exceeds pass budget')
            image_path = self.folder / 'image'
            self.state.update(stage='saving', engine=engine, next='save_image', imageDigest=digest(image))
            _write(self.path, self.state)
            if not os.path.lexists(image_path):
                write_new_file(image_path, image)
            if _read(image_path) != image:
                raise OSError('frozen answer changed')
            self.state.update(stage='siblings', next='native_writer')
            _write(self.path, self.state)
        if json.loads(_read(self.path)) != self.state:
            raise OSError('incomplete frozen backfill work')
        return self.answer()

    def answer(self):
        data = _read(self.folder / 'image')
        if digest(data) != self.state['imageDigest']:
            raise OSError('frozen answer checksum mismatch')
        return json.loads(data)

    def settled(self):
        self.state.update(stage='settled', next=None)
        _write(self.path, self.state)

    def write_sibling(self, key, writer):
        if key in self.state['results']:
            return self.state['results'][key]
        result = writer()
        if isinstance(result, dict) and result.get('action') in ('created', 'updated', 'candidate',
                                                                 'merge-needed', 'suppressed', 'tombstoned'):
            self.state['results'][key] = result
            _write(self.path, self.state)
        return result

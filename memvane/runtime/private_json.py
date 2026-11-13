"""Bounded owner-private JSON input for process coordination state."""
import json
import os
import stat

MAX_BYTES = 16 * 1024 * 1024


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_json_key')
        result[key] = value
    return result


def _json(raw):
    return json.loads(raw, object_pairs_hook=_object_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid_json_number')))


def read_private_json(path):
    """Read one private regular file without following links or accepting races."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise ValueError('unsafe_input_file')
        if info.st_size > MAX_BYTES:
            raise ValueError('input_budget_exceeded')
        chunks = []
        size = 0
        while True:
            block = os.read(fd, min(65536, MAX_BYTES + 1 - size))
            if not block:
                break
            chunks.append(block)
            size += len(block)
            if size > MAX_BYTES:
                raise ValueError('input_budget_exceeded')
        final = os.fstat(fd)
        if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                final.st_size, final.st_mtime_ns, final.st_ctime_ns):
            raise ValueError('input_changed')
        return _json(b''.join(chunks).decode('utf8'))
    finally:
        os.close(fd)

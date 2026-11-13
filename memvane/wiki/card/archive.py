"""Retain a verified wiki card before replacement or retirement."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from memvane.settings.api import compile_dir


def archive_card(root: Path, card: Path, text: str, proof_hash: str) -> bool:
    """Durably retain old canonical bytes before retirement or replacement.

    The caller holds the card and wiki mutation locks and has compared the
    canonical bytes. A same-content replay reuses the archived hard link.
    """
    archive = compile_dir(root, create=True)
    if archive is None:
        return False
    archive = archive / 'retired-wiki'
    archive.mkdir(mode=0o700, exist_ok=True)
    info = archive.lstat()
    if (archive.is_symlink() or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        return False
    digest = hashlib.sha256((str(card.relative_to(root)) + '\0' +
                             proof_hash + '\0' + text).encode()).hexdigest()
    saved = archive / (digest + '.md')
    try:
        if saved.exists():
            if saved.is_symlink() or saved.read_text(encoding='utf-8') != text:
                return False
        else:
            os.link(card, saved, follow_symlinks=False)
        # The archive is useful only when an operator can locate the original
        # card. Keep that identity beside the immutable bytes on every replay.
        sidecar = archive / (digest + '.json')
        metadata = {'path': card.relative_to(root).as_posix(), 'proofHash': proof_hash}
        if sidecar.exists():
            if sidecar.is_symlink() or json.loads(sidecar.read_text(encoding='utf-8')) != metadata:
                return False
        else:
            fd, temp = tempfile.mkstemp(prefix='.' + digest + '-', dir=archive)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    json.dump(metadata, stream, sort_keys=True)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, sidecar)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
        directory = os.open(archive, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return True

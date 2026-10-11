"""Preserve ownerless legacy rows while unblocking owner-bound queue work."""
import hashlib
from pathlib import Path
import tempfile
from unittest.mock import patch

import dirty_queue_claim as claim


with tempfile.TemporaryDirectory(prefix="qmd-legacy-queue-") as temp:
    root = Path(temp)
    queue = root / "dirty-queue"
    old = b"legacy-a\t/tmp/old-a\nlegacy-b\t/tmp/old-b\n"
    owned = b"owned\t/tmp/current\t/tmp/project\n"
    tail = b"later\t/tmp/later\t/tmp/project\n"
    queue.write_bytes(old + owned)
    snapshot = root / "snapshot"
    assert claim.claim(queue, snapshot)
    with queue.open("ab") as output:
        output.write(tail)
    assert claim.quarantine_ownerless(queue)
    archive = root / "dirty-queue.quarantine" / (hashlib.sha256(old).hexdigest() + ".queue")
    assert archive.read_bytes() == old
    assert archive.stat().st_mode & 0o077 == 0
    assert queue.read_bytes() == owned + tail
    assert not Path(str(queue) + ".claim.json").exists()
    assert claim.claim(queue, snapshot) and snapshot.read_bytes() == owned + tail
    assert not claim.quarantine_ownerless(queue)
    claim.ack(queue)
    assert queue.read_bytes() == b"" and archive.read_bytes() == old

with tempfile.TemporaryDirectory(prefix="qmd-legacy-crash-") as temp:
    root = Path(temp)
    queue = root / "dirty-queue"
    old = b"legacy\t/tmp/old\n"
    owned = b"owned\t/tmp/current\t/tmp/project\n"
    queue.write_bytes(old + owned)
    snapshot = root / "snapshot"
    assert claim.claim(queue, snapshot)
    with patch.object(claim, "_ack_locked", side_effect=RuntimeError("after_done_claim")):
        try:
            claim.quarantine_ownerless(queue)
        except RuntimeError as exc:
            assert str(exc) == "after_done_claim"
        else:
            raise AssertionError("missing crash failpoint")
    assert queue.read_bytes() == old + owned
    assert claim.claim(queue, snapshot)
    assert snapshot.read_bytes() == owned
    assert queue.read_bytes() == owned
    assert (root / "dirty-queue.quarantine" /
            (hashlib.sha256(old).hexdigest() + ".queue")).read_bytes() == old
    claim.ack(queue)
    assert queue.read_bytes() == b""

with tempfile.TemporaryDirectory(prefix="qmd-legacy-publish-crash-") as temp:
    root = Path(temp)
    queue = root / "dirty-queue"
    old = b"legacy\t/tmp/old\n"
    owned = b"owned\t/tmp/current\t/tmp/project\n"
    queue.write_bytes(old + owned)
    snapshot = root / "snapshot"
    assert claim.claim(queue, snapshot)
    with patch.object(claim.os, "link", side_effect=OSError("before_archive_publish")):
        try:
            claim.quarantine_ownerless(queue)
        except OSError as exc:
            assert str(exc) == "before_archive_publish"
        else:
            raise AssertionError("missing archive publish failpoint")
    archive_dir = root / "dirty-queue.quarantine"
    assert list(archive_dir.iterdir()) == []
    assert queue.read_bytes() == old + owned
    assert claim.claim(queue, snapshot) and snapshot.read_bytes() == old + owned
    assert claim.quarantine_ownerless(queue)
    assert queue.read_bytes() == owned
    assert (archive_dir / (hashlib.sha256(old).hexdigest() + ".queue")).read_bytes() == old

with tempfile.TemporaryDirectory(prefix="qmd-legacy-reload-") as temp:
    root = Path(temp)
    queue = root / "dirty-queue"
    old = b"legacy\t/tmp/old\n"
    owned = b"owned\t/tmp/current\t/tmp/project\n"
    queue.write_bytes(old + owned)
    assert claim.claim(queue, root / "snapshot")
    assert claim.reload_state(queue, True)
    assert not claim.quarantine_ownerless(queue)
    assert queue.read_bytes() == old + owned
    assert claim.reload_state(queue) is True
    assert claim.reload_state(queue, False) is False
    assert claim.quarantine_ownerless(queue)
    assert queue.read_bytes() == owned

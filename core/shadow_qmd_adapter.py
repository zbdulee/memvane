"""Disposable candidate configuration gate around the private publish flow.

This composes source-level adapters only. It neither chooses an operating
store nor launches QMD. A caller supplies private paths and a candidate-only
worker callback; no product entry point calls this module yet.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import qmd_candidate_config as config_policy
import shadow_index
import shadow_publish_flow as flow
import sqlite_read


def _physical_collections(index):
    """Return exactly the candidate DB's collection names and paths."""
    with sqlite_read.connect(index) as db:
        rows = db.execute('SELECT name,path,pattern FROM store_collections').fetchall()
    if (not rows or any(not isinstance(name, str) or not isinstance(path, str)
                        or pattern != config_policy.PATTERN
                        for name, path, pattern in rows)):
        raise ValueError('candidate_collections_unproved')
    names = {name for name, _, _ in rows}
    if len(names) != len(rows):
        raise ValueError('candidate_collections_unproved')
    return {name: path for name, path, _ in rows}


def _write_private_config(folder, raw):
    config_dir = folder / 'config'
    cache_dir = folder / 'cache'
    for path in (config_dir, cache_dir):
        path.mkdir(mode=0o700)
        shadow_index._private_dir(path)
    target = config_dir / 'index.yml'
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())
    directory = os.open(config_dir, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return target, cache_dir


def run(root, source, expected, content_revision, *, config_bytes,
        baseline_sha256, approved, work_candidate, acknowledge,
        claim=None, retain_receipt=None):
    """Prove config and physical scope, prepare a private DB, CAS, then ACK.

    ``work_candidate(index_path, env)`` is injected and is never invoked for
    unproved config. It must only change the candidate index, and may return
    False to defer. The adapter rechecks exact config bytes after that callback
    and before sealing; the callback's own QMD execution is outside this
    source-only boundary.
    """
    if not callable(work_candidate) or not callable(acknowledge):
        raise ValueError('invalid_shadow_qmd_callback')
    proof = config_policy.prove(config_bytes, baseline_sha256=baseline_sha256,
                                approved=approved)
    if not proof.ok:
        return {'outcome': flow.REJECTED, 'reason': 'candidate_config_' + proof.reason,
                'ack': 'not_called', 'candidate': None}
    root = shadow_index._private_dir(root)

    def edit(index_path):
        index = shadow_index._private_file(index_path)
        folder = shadow_index._private_dir(index.parent)
        physical = _physical_collections(index)
        if physical != dict(proof.collections):
            raise ValueError('candidate_collections_unproved')
        config_dir, cache_dir = folder / 'config', folder / 'cache'
        environment = config_policy.candidate_environment(
            config_bytes, baseline_sha256=baseline_sha256, approved=approved,
            required_names=physical.keys(), index_path=str(index),
            config_dir=str(config_dir), cache_home=str(cache_dir))
        if not environment.ok:
            raise ValueError('candidate_environment_' + environment.reason)
        target, _ = _write_private_config(folder, bytes(config_bytes))
        if work_candidate(str(index), environment.env) is False:
            return False
        shadow_index._private_file(target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != proof.config_sha256:
            raise ValueError('candidate_config_changed')
        if _physical_collections(index) != physical:
            raise ValueError('candidate_collections_changed')

    return flow.run(root, source, expected, content_revision, edit, acknowledge,
                    claim=claim, retain_receipt=retain_receipt)

"""Guarded atomic writes for workspace paths."""
from __future__ import annotations

from memvane.kernel.atomic_rename import write_text_atomic as _write_text_atomic
from memvane.wiki.workspace.wiki_mutation_lock import guard_path


@guard_path
def write_text_atomic(path, text):
    return _write_text_atomic(path, text)

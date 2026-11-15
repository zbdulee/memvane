"""Read-only journal access for classification and proofs."""
from __future__ import annotations

from contextlib import contextmanager


@contextmanager
def read_only_journals(deadline=None):
    """Journal reads that never repair a hot journal image; busy waits end at ``deadline``."""
    from memvane.journal import install_journal as journal
    with journal.read_only_journals():
        if deadline is None:
            yield
        else:
            with journal.reader_deadline(deadline):
                yield

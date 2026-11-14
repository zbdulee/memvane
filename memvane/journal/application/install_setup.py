"""Install journal creation for an initial build."""
from __future__ import annotations


def prepare_v1_journal(journal, reviewed_provider, route_raw):
    """Stage the reviewed worker attestation, then create the v1 journal or read the existing one.

    Returns ``(staged, row)``.
    """
    from memvane.journal.install_journal import create_v1_journal, inspect_journal, stage_v1_attestation
    staged = stage_v1_attestation(journal, reviewed_provider)
    if journal.exists() or journal.is_symlink():
        row = inspect_journal(journal)
    else:
        row = create_v1_journal(journal, route_raw, staged['attestationSha256'])
    return staged, row

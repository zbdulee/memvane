"""Independent, non-destructive cutover for a newly embedded wiki generation.

This ledger never names a QMD source database or claims provenance for old
vectors. It owns only the private Laya candidate and the exact project route.
The older QMD installation and project settings are left in place.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat

import project_dir
from laya_ledger_common import _read_json

SCHEMA = 'laya-fresh-wiki-cutover-v1'
MARKER_SCHEMA = 'laya-fresh-wiki-selection-marker-v1'
LEDGER = 'fresh-wiki-cutover.json'
MARKER = 'selection.json'
ROUTE = 'route.json'
PHASES = ('prepared', 'switching', 'active', 'upgrading', 'restoring', 'restored', 'retired')
HEX64 = re.compile(r'[0-9a-f]{64}\Z')
HEX32 = re.compile(r'[0-9a-f]{32}\Z')
FILES = (('candidateDb', 'candidateIdentity'), ('queueDb', 'queueIdentity'),
         ('catalogDb', 'catalogIdentity'), ('bindingStore', 'bindingStoreIdentity'))
FIELDS = frozenset(('schema', 'epoch', 'phase', 'projectRoot', 'journalPath',
                    'routeSha256', 'collections', 'candidateDb', 'queueDb',
                    'catalogDb', 'bindingStore', 'scope', 'indexGeneration',
                    'sourceManifest', 'bindingDigest', 'catalogScanId',
                    'catalogRawDigest', 'vectorProfileId', 'vectorRows',
                    'candidateIncarnation', 'attestationSha256',
                    'providerFileIdentitySha256', *(field for _, field in FILES)))


class FreshWikiBlocked(RuntimeError):
    pass


def _fail(reason):
    raise FreshWikiBlocked(reason)


def _paths(project_root, journal_path):
    project, journal = Path(project_root), Path(journal_path)
    if (not project.is_absolute() or project.resolve(strict=True) != project or
            not journal.is_absolute() or journal.name != 'journal.sqlite' or
            journal.is_symlink() or journal.parent.resolve(strict=True) != journal.parent):
        _fail('fresh_wiki_paths_invalid')
    marker, route = (project_dir.runtime_file(project, MARKER),
                     project_dir.runtime_file(project, ROUTE))
    auto = marker.parent
    if auto.is_symlink() or not auto.is_dir():
        _fail('fresh_wiki_project_unavailable')
    info = journal.parent.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        _fail('fresh_wiki_owner_unavailable')
    return journal.parent / LEDGER, marker, auto / ROUTE


def project_wiki_roots(project_root):
    """The wiki roots a selected project may bind: ``.memvane/wiki`` or the
    not-yet-moved ``.auto-context/wiki``."""
    return tuple(str(Path(project_root) / name / project_dir.WIKI)
                 for name in project_dir.NAMES)


def _load(path):
    return validate_row(_read_json(path), path)


def validate_row(row, path):
    """The ledger shape ``_load`` accepts for a row stored at ``path``."""
    from laya_index_capacity import generation_digest
    from laya_index_host_route import wiki_index_roots, _canonical_absolute
    if (not isinstance(row, dict) or set(row) != FIELDS or
            row['schema'] != SCHEMA or type(row['epoch']) is not int or
            row['epoch'] < 1 or row['phase'] not in PHASES or
            type(row['vectorRows']) is not int or row['vectorRows'] < 0 or
            any(not isinstance(row[key], str) or not row[key] for key in
                FIELDS - {'epoch', 'phase', 'collections', 'vectorRows',
                          *(field for _, field in FILES)}) or
            any(not HEX64.fullmatch(row[key]) for key in
                ('routeSha256', 'sourceManifest', 'bindingDigest',
                 'catalogRawDigest', 'vectorProfileId', 'attestationSha256',
                 'providerFileIdentitySha256')) or
            any(not HEX32.fullmatch(row[key]) for key in
                ('scope', 'catalogScanId', 'candidateIncarnation')) or
            generation_digest(row['indexGeneration']) != row['bindingDigest'] or
            Path(row['journalPath']).parent / LEDGER != path or
            any(not isinstance(row[field], list) or len(row[field]) != 2 or
                any(type(value) is not int or value < 0 for value in row[field])
                for _, field in FILES)):
        _fail('fresh_wiki_ledger_invalid')
    try:
        wiki = wiki_index_roots(row['collections'])
        raw = next(item['sourceRoot'] for item in row['collections']
                   if item['role'] == 'raw')
        wiki_root = next(iter(wiki.values()))
        if (len({item['name'] for item in row['collections']}) != 2 or
                not _canonical_absolute(raw) or
                raw != row['projectRoot'] or
                wiki_root not in project_wiki_roots(raw)):
            _fail('fresh_wiki_ledger_invalid')
    except (ValueError, KeyError, TypeError, StopIteration) as exc:
        raise FreshWikiBlocked('fresh_wiki_ledger_invalid') from exc
    if len({row[key] for key, _ in FILES}) != len(FILES):
        _fail('fresh_wiki_ledger_invalid')
    return row


def _marker(row):
    return {'schema': MARKER_SCHEMA, 'projectRoot': row['projectRoot'],
            'journalPath': row['journalPath'],
            'ledgerPath': str(Path(row['journalPath']).parent / LEDGER),
            'routeSha256': row['routeSha256']}


def _check_marker(row, marker_path):
    if _read_json(marker_path) != _marker(row):
        _fail('fresh_wiki_marker_changed')

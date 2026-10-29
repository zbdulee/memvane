"""Finite first-pass wiki compilation for an already selected installation.

An accepted source event authorizes one frozen path/revision cohort. Continuation
never refreshes that authority to newer bytes. Queue admission is not completion:
the compile worker acknowledges a terminal attempt before dropping its job.
All state shares discovery's private lock, binding and atomic JSON primitives.
"""
from pathlib import Path
import hashlib
import os
import stat
import uuid

SCHEMA = 'laya-wiki-initial-backfill-v1'
STAGES = frozenset(('pending', 'dispatched', 'settled', 'skipped'))
CONTROL_NAMES = frozenset(('AGENTS.md', 'CLAUDE.md', 'GEMINI.md'))


def _path(decision):
    import laya_wiki_discovery as d
    path = d._paths(decision)[0]
    return path.with_name(path.stem + '.backfill.json')


def _load(decision):
    import laya_wiki_discovery as d
    row = d._read(_path(decision))
    if row is None:
        return None
    scope, incarnation, _queue, roots, raw = d._binding(decision)
    if (not isinstance(row, dict) or set(row) !=
            {'schema', 'scopeId', 'dbIncarnation', 'collectionRoots', 'installId', 'projectRoot',
             'id', 'kind', 'engine', 'items'}
            or row['schema'] != SCHEMA or row['scopeId'] != scope
            or row['dbIncarnation'] != incarnation or row['collectionRoots'] != roots
            or row['installId'] != decision.get('installId') or row['projectRoot'] != decision['projectRoot']
            or not isinstance(row['id'], str) or not d._HEX32.fullmatch(row['id'])
            or row['kind'] not in d.GRANT_KINDS or not isinstance(row['items'], dict)
            or not isinstance(row['engine'], str) or row['engine'] not in d.SOURCE_ENGINES
            or len(row['items']) > d.RAW_SCAN_MAX_FILES):
        raise d.DiscoveryBlocked('backfill_state_invalid')
    for path, item in row['items'].items():
        if (not d._raw_path(path, raw) or not isinstance(item, dict)
                or set(item) != {'sha256', 'stage'} or not isinstance(item['stage'], str)
                or item['stage'] not in STAGES
                or not isinstance(item['sha256'], str) or not d._HEX64.fullmatch(item['sha256'])):
            raise d.DiscoveryBlocked('backfill_state_invalid')
    return row


def _eligible(decision, snapshot, config):
    """Mirror source admission's cheap path filters; enqueue re-proves each file."""
    import source_text
    from laya_source_exclusion import excluded_source
    import laya_wiki_discovery as d
    _scope, _inc, _queue, roots, raw = d._binding(decision)
    project = Path(decision['projectRoot'])
    skips = config.get('skipPaths', [])
    out = {}
    for path, sha in snapshot.items():
        name, rel = path.split('/', 1)
        if name not in raw or Path(rel).suffix.lower() not in ('.md', '.markdown'):
            continue
        if Path(rel).name in CONTROL_NAMES:
            continue
        if any(part.startswith('.') for part in Path(rel).parts):
            continue
        try:
            project_rel = (Path(roots[name]) / rel).relative_to(project).as_posix()
        except ValueError:
            continue
        if source_text.is_denied_source_path(project_rel) or excluded_source(project_rel, skips):
            continue
        out[path] = sha
    return out


def _authority(decision, row, job, *, markerless=False):
    """Frozen source authority survives only a bundle-generation change."""
    import wiki_selected_laya
    old = job.get('selectedRoute')
    current = wiki_selected_laya.route_identity((decision, None))
    if (not isinstance(old, dict) or set(old) != set(current)
            or not isinstance(old.get('generation'), str) or not old['generation']
            or any(old.get(key) != current[key] for key in
                   ('installId', 'scopeId', 'dbIncarnation'))):
        return None
    cwd = job.get('cwd')
    if not isinstance(cwd, str) or not Path(cwd).is_absolute() or str(Path(cwd).resolve()) != row['projectRoot']:
        return None
    source = job.get('source')
    if not isinstance(source, dict) or source.get('kind') != 'file':
        return None
    name, rel = source.get('collection'), source.get('path')
    if not isinstance(name, str) or not isinstance(rel, str):
        return None
    raw = {entry['name'] for entry in decision['collections'] if entry.get('role') == 'raw'}
    if (name not in raw or Path(rel).is_absolute()
            or any(part in ('', '.', '..') for part in rel.split('/'))):
        return None
    source_path = Path(row['projectRoot']) / rel
    try:
        if source_path.resolve() != source_path:
            return None
        inner = source_path.relative_to(Path(row['collectionRoots'][name])).as_posix()
    except (OSError, ValueError):
        return None
    key = name + '/' + inner
    item = row['items'].get(key)
    if item is None:
        return None
    marker = job.get('initialBackfill')
    if marker != row['id']:
        if not markerless or marker is not None or item['stage'] != 'dispatched':
            return None
    revision = job.get('contentRevision')
    if revision is not None and revision != item['sha256']:
        return None
    return key, item, source_path


def rebind_job(bound, job):
    """Rebind exact frozen work to a newer bundle proof, never another scope."""
    if bound is None:
        return None
    import laya_wiki_discovery as d
    import wiki_freshness
    import wiki_selected_laya
    decision = bound[0]
    old = job.get('selectedRoute')
    current = wiki_selected_laya.route_identity(bound)
    if (not isinstance(old, dict) or any(old.get(key) != current[key]
            for key in ('installId', 'scopeId', 'dbIncarnation'))):
        return None
    if not _path(decision).exists():
        return None
    with d._locked(d._paths(decision)[2], d.WORKER_LOCK_SECONDS):
        wiki_selected_laya.recheck(bound)
        row = _load(decision)
        found = _authority(decision, row, job, markerless=True) if row is not None else None
        if found is None:
            return None
        _key, item, source = found
        captured = wiki_freshness.snapshot_bytes(source)
        if captured is None:
            return None
        actual = captured[0]['sha256']
        wiki_selected_laya.recheck(bound)
        old = dict(job['selectedRoute'])
        current = wiki_selected_laya.route_identity(bound)
        receipt = job.get('_fallbackAttempts')
        if receipt is not None:
            expected = {'path': job['source']['path'], 'collection': job['source']['collection']}
            if (not isinstance(receipt, dict) or receipt.get('selectedRoute') != old
                    or receipt.get('source') != expected or receipt.get('revision') != item['sha256']):
                return None
            job['_fallbackAttempts'] = {**receipt, 'selectedRoute': current}
        job['selectedRoute'] = current
        job['initialBackfill'] = row['id']
        job['contentRevision'] = item['sha256']
        job['_backfillRebound'] = True
        return 'rebound' if actual == item['sha256'] else 'source_changed'


def discard_receipt(job):
    """Bounded metadata copied only into a durable terminal-discard record."""
    import laya_wiki_discovery as d
    marker, revision, source, route, cwd = (job.get('initialBackfill'), job.get('contentRevision'),
        job.get('source'), job.get('selectedRoute'), job.get('cwd'))
    if ((marker is not None and (not isinstance(marker, str) or not d._HEX32.fullmatch(marker)))
            or not isinstance(revision, str) or not d._HEX64.fullmatch(revision)
            or not isinstance(cwd, str) or len(cwd) > 4096
            or not isinstance(source, dict) or source.get('kind') != 'file'
            or not isinstance(source.get('path'), str) or len(source['path']) > 4096
            or not isinstance(source.get('collection'), str) or len(source['collection']) > 64
            or not isinstance(route, dict) or set(route) != {'installId', 'generation', 'scopeId', 'dbIncarnation'}
            or not isinstance(route['generation'], str) or len(route['generation']) > 128
            or any(not isinstance(route[key], str) or not d._HEX32.fullmatch(route[key])
                   for key in ('installId', 'scopeId', 'dbIncarnation'))):
        return None
    return {'initialBackfill': marker, 'contentRevision': revision, 'cwd': cwd,
            'source': {key: source[key] for key in ('kind', 'path', 'collection')},
            'selectedRoute': dict(route)}


def _receipt_path(decision, row, key, *, create=False):
    """Private deterministic file; abandoned temp files are never enumerated."""
    import laya_wiki_discovery as d
    parent = _path(decision).with_suffix('.receipts')
    if create:
        try:
            parent.mkdir(mode=0o700)
        except FileExistsError:
            pass
    try:
        info = os.lstat(parent)
    except FileNotFoundError:
        return None
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or parent.resolve() != parent):
        raise d.DiscoveryBlocked('backfill_receipt_directory_unsafe')
    if create:
        directory = os.open(parent.parent, os.O_RDONLY | d._NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    name = row['id'] + '-' + hashlib.sha256(key.encode()).hexdigest() + '-' + row['items'][key]['sha256']
    return parent / (name + '.json')


def write_discard_receipt(job):
    """Read atomic cohort state without its lock while source queue is locked.

    Frozen identity never changes in place. The current selected proof and the
    exact token/hash are checked again by reconciliation outside the queue lock.
    """
    import laya_wiki_discovery as d
    import wiki_selected_laya
    receipt = discard_receipt(job)
    if receipt is None:
        if job.get('initialBackfill') is not None:
            raise d.DiscoveryBlocked('backfill_discard_authority_unavailable')
        return None
    bound = wiki_selected_laya.selected(Path(receipt['cwd']))
    if bound is None:
        if receipt['initialBackfill'] is not None:
            raise d.DiscoveryBlocked('backfill_discard_authority_unavailable')
        return None
    wiki_selected_laya.recheck(bound)
    decision = bound[0]
    row = _load(decision)
    found = _authority(decision, row, receipt, markerless=True) if row is not None else None
    if found is None or receipt['contentRevision'] != found[1]['sha256']:
        if receipt['initialBackfill'] is not None:
            raise d.DiscoveryBlocked('backfill_discard_authority_unavailable')
        return None
    # Resolve a superseding ordinary job to the existing dispatched cohort now,
    # so canonical evidence always carries an exact cohort token.
    receipt['initialBackfill'] = row['id']
    path = _receipt_path(decision, row, found[0], create=True)
    existing = d._read(path)  # fail closed for symlinks/unsafe existing files
    if existing is not None:
        prior = _authority(decision, row, existing)
        if (prior is None or prior[0] != found[0]
                or existing.get('contentRevision') != receipt['contentRevision']
                or discard_receipt(existing) != existing):
            raise d.DiscoveryBlocked('backfill_receipt_identity_changed')
        receipt = existing
    wiki_selected_laya.recheck(bound)
    d._write(path, receipt)  # temp + fsync + atomic replace + directory fsync
    return receipt


def _sync_receipt(path):
    """A crash may have happened between replace and directory fsync."""
    import laya_wiki_discovery as d
    for target in (path, path.parent, path.parent.parent):
        fd = os.open(target, os.O_RDONLY | d._NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _reconcile_discard_locked(decision, row):
    """Only deterministic paths for this bounded cohort, never an audit scan."""
    import laya_wiki_discovery as d
    changed = False
    for key, item in row['items'].items():
        if item['stage'] not in ('pending', 'dispatched'):
            continue
        path = _receipt_path(decision, row, key)
        receipt = d._read(path) if path is not None else None
        if receipt is None:
            continue
        found = _authority(decision, row, receipt)
        if (found is None or found[0] != key or receipt.get('contentRevision') != item['sha256']
                or discard_receipt(receipt) != receipt):
            raise d.DiscoveryBlocked('backfill_receipt_invalid')
        _sync_receipt(path)
        item['stage'] = 'skipped'
        changed = True
    if changed:
        d._write(_path(decision), row)
        _emit(decision, row)


def reconcile_discards(bound):
    """Outside the source-queue lock; receipt ACK survives claim unlink/crash."""
    if bound is None:
        return
    import laya_wiki_discovery as d
    import wiki_selected_laya
    decision = bound[0]
    try:
        path = _path(decision)
    except (d.DiscoveryBlocked, KeyError, ValueError, OSError):
        return
    if not path.exists():
        return
    with d._locked(d._paths(decision)[2], d.WORKER_LOCK_SECONDS):
        wiki_selected_laya.recheck(bound)
        row = _load(decision)
        if row is None:
            return
        _reconcile_discard_locked(decision, row)
        progress = summary(row)
    if progress['pending'] and not progress['dispatched']:
        wiki_selected_laya.request_sync(Path(decision['projectRoot']), bound)


def summary(row):
    counts = {stage: sum(item['stage'] == stage for item in row['items'].values())
              for stage in STAGES}
    complete = not counts['pending'] and not counts['dispatched']
    return {'total': len(row['items']), **counts,
            'state': ('complete_with_skips' if counts['skipped'] else 'complete') if complete else 'pending'}


def _emit(decision, row):
    import event_log
    progress = summary(row)
    event_log.emit('wiki.backfill', decision=decision, outcome=progress['state'],
                   engine=row['engine'], **{key: value for key, value in progress.items() if key != 'state'})


def discover(decision, snapshot, config, live, engines):
    """Called under discovery's lock; only a proved installation may initialize."""
    import laya_wiki_discovery as d
    import wiki_compile_enqueue as enqueue
    import wiki_source_intent as intent
    if not Path(decision['journalPath']).is_file():
        return None
    row = _load(decision)
    if row is None:
        kind = next((kind for kind in live if enqueue.compile_gate(
            config, intent.trigger_policy(kind == 'session_start')[0]) is not None
            and engines.get(kind) in d.SOURCE_ENGINES), None)
        if kind is None:
            return None
        scope, incarnation, _queue, roots, _raw = d._binding(decision)
        row = {'schema': SCHEMA, 'scopeId': scope, 'dbIncarnation': incarnation,
               'installId': decision['installId'], 'projectRoot': decision['projectRoot'],
               'collectionRoots': roots, 'id': uuid.uuid4().hex, 'kind': kind, 'engine': engines[kind],
               'items': {path: {'sha256': sha, 'stage': 'pending'}
                         for path, sha in _eligible(decision, snapshot, config).items()}}
        d._write(_path(decision), row)
    eligible = _eligible(decision, snapshot, config)
    changed = False
    for path, item in row['items'].items():
        if item['stage'] in ('pending', 'dispatched') and eligible.get(path) != item['sha256']:
            item['stage'] = 'skipped'
            changed = True
    if changed:
        d._write(_path(decision), row)
    return row


def dispatch(decision, row, snapshot, config, *, recheck):
    """One existing admission cap; wait for its jobs to settle before next batch."""
    import laya_wiki_discovery as d
    import wiki_compile_enqueue as enqueue
    if row is None:
        return None
    _reconcile_discard_locked(decision, row)
    if any(item['stage'] == 'dispatched' for item in row['items'].values()):
        return summary(row)
    pending = {path: item['sha256'] for path, item in row['items'].items()
               if item['stage'] == 'pending'}
    if not pending:
        return summary(row)
    recheck()
    result = enqueue.enqueue_changed_sources(decision['projectRoot'], config,
        d._changed_files(pending, decision['collectionRoots'], snapshot),
        session_start=row['kind'] == 'session_start', initial_backfill=row['id'], engine=row['engine'])
    if result.get('reason') not in ('queued', 'capped', 'no_sources', 'compile_disabled'):
        raise d.DiscoveryBlocked('backfill_enqueue_' + str(result.get('reason')))
    for name, rel in result.get('backfillAccepted', []):
        key = name + '/' + rel
        if key in pending:
            row['items'][key]['stage'] = 'dispatched'
    d._write(_path(decision), row)
    _emit(decision, row)
    return summary(row)


def settle_job(bound, job):
    """ACK after a terminal worker attempt; never claim a card was verified."""
    marker = job.get('initialBackfill')
    revision = job.get('_processedContentRevision')
    if bound is None or (not isinstance(marker, str) and not isinstance(revision, str)):
        return
    import laya_wiki_discovery as d
    import wiki_selected_laya
    decision = bound[0]
    try:
        path = _path(decision)
    except (d.DiscoveryBlocked, KeyError, OSError, ValueError):
        if marker is None:
            return  # Ordinary legacy/synthetic jobs have no backfill contract.
        raise
    if marker is None and not path.exists():
        return
    continuation = False
    with d._locked(d._paths(decision)[2], d.WORKER_LOCK_SECONDS):
        wiki_selected_laya.recheck(bound)
        row = _load(decision)
        if row is None:
            return
        if marker is not None and row['id'] != marker:
            raise d.DiscoveryBlocked('backfill_job_identity_changed')
        # A marked job must carry its exact frozen hash. Ordinary work may have
        # read a later revision, so only its worker-owned actual-byte receipt can
        # acknowledge the dispatched frozen revision.
        source = job.get('source') or {}
        rel = source.get('path')
        name = source.get('collection')
        if isinstance(rel, str) and isinstance(name, str) and name in row['collectionRoots']:
            try:
                inner = (Path(row['projectRoot']) / rel).relative_to(Path(row['collectionRoots'][name]))
                item = row['items'].get(name + '/' + inner.as_posix())
            except ValueError:
                item = None
            if marker is not None and item is not None and item['sha256'] != job.get('contentRevision'):
                raise d.DiscoveryBlocked('backfill_job_revision_changed')
        authority = job if marker is not None else {**job, 'contentRevision': revision}
        found = _authority(decision, row, authority, markerless=True)
        if found is None:
            return
        item = found[1]
        if item['stage'] in ('pending', 'dispatched'):
            item['stage'] = ('settled' if item['sha256'] == revision
                             and job.get('_backfillSkipped') is not True else 'skipped')
        d._write(_path(decision), row)
        progress = summary(row)
        _emit(decision, row)
        continuation = progress['pending'] > 0 and progress['dispatched'] == 0
    if continuation:
        wiki_selected_laya.request_sync(Path(decision['projectRoot']), bound)


def settled_job(bound, job):
    """A terminal frozen attempt need not pay again after ACK/wake failure."""
    marker = job.get('initialBackfill')
    if bound is None or not isinstance(marker, str):
        return False
    import laya_wiki_discovery as d
    decision = bound[0]
    with d._locked(d._paths(decision)[2], d.WORKER_LOCK_SECONDS):
        row = _load(decision)
        found = _authority(decision, row, job) if row is not None else None
        return found is not None and found[1]['stage'] in ('settled', 'skipped')

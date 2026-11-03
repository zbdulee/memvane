"""Writer-owned preparation of retained move tuples, without publication.

Scopes are cooperative issued API state, not a same-UID introspection boundary.
No lifetime EX is acquired here; physical publication is a separate operation.
"""
from contextlib import closing
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from types import SimpleNamespace
import weakref

import laya_journal_reader as R

_active = ContextVar('reader_move_writer', default=None)
_scopes = weakref.WeakSet()
_records = weakref.WeakKeyDictionary()
_seals = weakref.WeakKeyDictionary()


@dataclass(frozen=True, eq=False, init=False)
class MoveReadSeal:
    move_id: str
    retained_endpoint: R.VerifiedEndpoint
    old_generation: str
    exclusion_epoch: int
    old_tuple_digest: str
    new_tuple_digest: str
    path_aliases: tuple
    prefix_digest: str

    def __init__(self):
        raise ValueError('reader_move_seal_not_issued')


class _Scope:
    pass


def _register_writer(stack, project, state, *, kind, recheck=None):
    """Called only after the owning mover successfully acquired every lock."""
    scope = _Scope()
    scope.project, scope.state = Path(project), R._state(state)
    scope.project_node, scope.state_node = R._node(scope.project), R._node(scope.state)
    scope.pid, scope.kind, scope.recheck = os.getpid(), kind, recheck
    _scopes.add(scope)
    previous = _active.set(scope)
    def revoke():
        _scopes.discard(scope)
        _active.reset(previous)
    # Registered last: unissue before the same ExitStack releases its locks.
    stack.callback(revoke)


def _owner():
    scope = _active.get()
    R._need(scope is not None and scope in _scopes and scope.pid == os.getpid(),
            'reader_move_owner_required')
    R._need(R._node(scope.project) == scope.project_node and
            R._node(scope.state) == scope.state_node, 'reader_move_owner_changed')
    if scope.recheck is not None:
        scope.recheck()
    return scope


def _logical(path, parent, *, skip_meta=False):
    digest = hashlib.sha256()
    with R._readonly(path, parent) as db:
        R._need(db.execute('PRAGMA integrity_check').fetchall() == [('ok',)],
                'reader_move_store_corrupt')
        digest.update(b'memvane-move-sqlite-v3\0')
        digest.update(R._raw([db.execute('PRAGMA application_id').fetchone()[0],
                              db.execute('PRAGMA user_version').fetchone()[0]]))
        # Document rowids join the ordinary rows to FTS; SQL dumps omit them.
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='docs'").fetchone():
            for path_, rowid in db.execute('SELECT path,rowid FROM docs ORDER BY path'):
                data = R._raw(['docs-rowid', path_, rowid])
                digest.update(len(data).to_bytes(8, 'big')); digest.update(data)
        for line in db.iterdump():
            # Only replay comparisons omit these rows; both complete meta
            # rowsets are checked separately against the proved transition.
            if skip_meta and line.startswith('INSERT INTO "meta" VALUES('):
                continue
            data = line.encode('utf8')
            digest.update(len(data).to_bytes(8, 'big')); digest.update(data)
        # SQLite quote(), used by iterdump(), omits TEXT after its first NUL.
        # Commit typed, length-framed cells as well as the schema/SQL stream.
        def frame(raw):
            digest.update(len(raw).to_bytes(8, 'big')); digest.update(raw)
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            if skip_meta and table == 'meta':
                continue
            digest.update(b'T'); frame(table.encode('utf8'))
            quoted = '"' + table.replace('"', '""') + '"'
            for row in db.execute('SELECT * FROM ' + quoted):
                digest.update(b'R'); digest.update(len(row).to_bytes(4, 'big'))
                for value in row:
                    if value is None: raw = b'n'
                    elif type(value) is str: raw = b't' + value.encode('utf8')
                    elif type(value) is bytes: raw = b'b' + value
                    elif type(value) is int: raw = b'i' + str(value).encode('ascii')
                    elif type(value) is float: raw = b'f' + value.hex().encode('ascii')
                    else: raise ValueError('reader_move_cell_invalid')
                    frame(raw)
            digest.update(b'E')
    return digest.hexdigest()


def _stage_directory(state, move_id):
    stage = state / ('.reader-move-' + move_id)
    try:
        os.mkdir(stage, 0o700)
        R._sync(state, True)
    except FileExistsError:
        pass
    info = stage.lstat()
    R._need(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & 0o077 and stage.resolve(strict=True) == stage,
            'reader_move_stage_changed')
    return stage


def _pending_node(path, stage):
    node = R._file(path, stage)
    info = path.lstat()
    R._need([info.st_dev, info.st_ino] == node and stat.S_IMODE(info.st_mode) == 0o600,
            'reader_move_copy_changed')
    return node


def _copy(source, state, stage, name, *, meta_variants=None):
    destination = stage / name
    pending = stage / ('.' + name + '.copying')
    R._need(not any(os.path.lexists(str(destination) + suffix)
                    for suffix in ('-journal', '-wal', '-shm')), 'reader_move_copy_changed')
    if os.path.lexists(destination):
        R._need(not any(os.path.lexists(str(pending) + suffix)
                       for suffix in ('', '-journal', '-wal', '-shm')), 'reader_move_copy_changed')
        if meta_variants is not None:
            with R._readonly(source, state) as src, R._readonly(destination, stage) as dst:
                R._need(src.execute('SELECT * FROM meta').fetchall() == meta_variants[0]
                        and dst.execute('SELECT * FROM meta').fetchall() in meta_variants,
                        'reader_move_copy_changed')
        R._need(_logical(source, state, skip_meta=meta_variants is not None)
                == _logical(destination, stage, skip_meta=meta_variants is not None),
                'reader_move_copy_changed')
        return destination
    # Only this reserved, unpublished copy name is recoverable scratch. Check
    # every inode before removing any leftover; completed tuple names stay put.
    leftovers = [(path, _pending_node(path, stage)) for suffix in ('-journal', '-wal', '-shm', '')
                 for path in (Path(str(pending) + suffix),) if os.path.lexists(path)]
    for path, node in leftovers:
        R._need(_pending_node(path, stage) == node, 'reader_move_copy_changed')
        path.unlink()
    if leftovers:
        R._sync(stage, True)
    fd = os.open(pending, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    pending_node = R._file(pending, stage)
    with R._readonly(source, state) as src, closing(sqlite3.connect(
            pending.as_uri() + '?mode=rw', uri=True, isolation_level=None)) as dst:
        src.backup(dst)
        # A private closed backup is a standalone image, including on cold RO
        # open. This changes no source journal mode and uses no immutable URI.
        R._need(dst.execute('PRAGMA journal_mode=DELETE').fetchone() == ('delete',),
                'reader_move_copy_changed')
    R._sync(pending)
    R._need(_logical(source, state) == _logical(pending, stage)
            and R._file(pending, stage) == pending_node
            and not os.path.lexists(destination)
            and not any(os.path.lexists(str(destination) + suffix)
                        for suffix in ('-journal', '-wal', '-shm'))
            and not any(os.path.lexists(str(pending) + suffix)
                        for suffix in ('-journal', '-wal', '-shm')), 'reader_move_copy_changed')
    os.replace(pending, destination)
    R._sync(stage, True)
    return destination


def _catalog_variants(path, state, changes):
    with R._readonly(path, state) as db:
        before = db.execute('SELECT * FROM meta').fetchall()
    values = dict(before)
    for key, pair in changes.items():
        if key == 'path':
            R._need(pair == str(path), 'reader_move_catalog_changed')
            continue
        R._need(key in ('binding', 'roots') and len(pair) == 2 and values.get(key) == pair[0],
                'reader_move_catalog_changed')
        values[key] = pair[1]
    return before, [(key, values[key]) for key, _value in before]


def _generation(db_path, state, old, new, decision):
    from laya_index_a import LayaIndexA
    from laya_index_vector import verify_generation_seal
    with R._readonly(db_path, state) as db:
        for generation in (old, new):
            R._need(LayaIndexA._generation_fts_matches(SimpleNamespace(db=db), generation),
                    'reader_move_fts_changed')
        def members(gen):
            return db.execute('SELECT m.path,m.hash,m.eligible,c.body FROM membership m '
                'JOIN content c ON c.hash=m.hash WHERE m.gen=? ORDER BY m.path', (gen,)).fetchall()
        a, b = members(old), members(new)
        R._need(a == b and a, 'reader_move_body_changed')
        seals = [verify_generation_seal(SimpleNamespace(db=db), gen) for gen in (old, new)]
        profile = decision['vectorProfile']
        for seal in seals:
            R._need(seal['profile_id'] == profile['profileId']
                    and seal['model_id'] == profile['modelId']
                    and seal['embedding_fingerprint'] == profile['embeddingFingerprint']
                    and seal['dimension'] == profile['dimension'], 'reader_move_profile_changed')
        exclusions = tuple(db.execute('SELECT kind,value FROM exclusions WHERE scope=? ORDER BY kind,value',
                                      (decision['scopeId'],)))
        epochs = [db.execute('SELECT rev_epoch FROM generations WHERE gen=?', (gen,)).fetchone()
                  for gen in (old, new)]
        R._need(epochs == [(len(exclusions),)] * 2, 'reader_move_exclusions_changed')
        return R._sha(R._raw(a)), len(exclusions)


def _sidecar(path, parent, identity, db_path, state, decision):
    import laya_incremental_docs as docs
    from laya_index_vector import _read_chunk_set, qmd_markdown_title
    with R._readonly(path, parent) as side, R._readonly(db_path, state) as index:
        R._need(side.execute('PRAGMA user_version').fetchone() == (1,) and
                side.execute('SELECT * FROM meta').fetchall() == [docs._expected_meta(identity)],
                'reader_move_sidecar_changed')
        rows = side.execute('SELECT rowid,path,hash,title,body,profile_id,chunk_count FROM docs ORDER BY path').fetchall()
        fts = side.execute('SELECT rowid,path,title,body FROM docs_fts ORDER BY path').fetchall()
        R._need(fts == [(row[0], row[1], row[3], row[4]) for row in rows], 'reader_move_sidecar_changed')
        for _rowid, path_, hash_, title, body, profile, count in rows:
            R._need(type(body) is str and R._sha(body.encode('utf8')) == hash_
                    and profile == identity['vectorProfileId'] and type(count) is int and count > 0
                    and qmd_markdown_title(body, path_) == title
                    and docs._verified(identity, path_, body), 'reader_move_sidecar_changed')
            cached = _read_chunk_set(index, hash_, title, profile, decision['vectorProfile']['dimension'])
            R._need(cached is not None and len(cached) == count, 'reader_move_sidecar_changed')
    return _logical(path, parent)


def _binding(path, parent, roots, decision, generation):
    import laya_index_capacity as capacity
    import laya_source_catalog as source
    roots = source._check_roots(roots)
    with R._readonly(path, parent) as db:
        store = object.__new__(capacity.GenerationBindingStore)
        store.path, store.db = path, db
        store.scope, store.db_incarnation, store.roots = decision['scopeId'], decision['dbIncarnation'], roots
        store._roots_value = capacity._canonical(list(roots.items())).decode('ascii')
        store._node, store.store_id = tuple(R._node(path)), None
        store._check_file_header(); store._check_store()
        row = db.execute(capacity._LOOKUP_SQL, (store.scope, generation, store.db_incarnation)).fetchone()
        R._need(row is not None, 'reader_move_binding_missing')
        return store._decode(generation, row)


def _catalog_scan(db, binding):
    import laya_source_catalog as source
    row = db.execute('SELECT seq,phase,raw_digest,files,bytes FROM scans WHERE scan_id=?',
                     (binding.catalog_scan_id,)).fetchone()
    R._need(row is not None and row[1:3] == ('complete', binding.catalog_raw_digest),
            'reader_move_catalog_changed')
    digest, files, size = hashlib.sha256(), 0, 0
    for path, sha, length in db.execute(
            'SELECT path,sha256,size FROM scan_files WHERE seq=? ORDER BY path', (row[0],)):
        R._need(type(path) is str and type(sha) is str and source._HEX64.fullmatch(sha)
                and type(length) is int and length >= 0, 'reader_move_catalog_changed')
        digest.update(source._raw_line(path, sha, length))
        files += 1
        size += length
    R._need(digest.hexdigest() == binding.catalog_raw_digest and row[3:] == (files, size),
            'reader_move_catalog_changed')


def stage_move_reader(intent, *, installer):
    """Prepare a tuple before physical movement; publication is separate."""
    scope = _owner()
    if scope.kind == 'state':
        return _stage_state(intent, installer, scope)
    R._need(scope.kind == 'project', 'reader_move_stage_unsupported')
    from laya_product_install import _read_private
    import laya_project_dir_move as mover
    import laya_initial_build as build
    import laya_incremental_docs as docs
    import laya_source_catalog as source
    from laya_index_host_route import wiki_index_roots
    original = R._installer(installer)
    state, project, root = R._issued[original][:3]
    R._need((state, project) == (scope.state, scope.project), 'reader_install_changed')
    selected = R.read_selection(state / R._JOURNAL)
    # This preparer stages the live mover's generation only. An independently
    # selected reader needs its own tuple plan; never silently replace it.
    R._need(selected.preferred_endpoint == original.endpoint_id, 'reader_move_selection_unsupported')
    old_record, attestation, proof = R._recheck(original)
    raw_intent = _read_private(state / mover.INTENT, mover.LIMIT)
    R._need(type(intent) is dict and R._raw(intent) == raw_intent and
            mover._canonical(mover._load_intent(state, project)) == raw_intent,
            'reader_move_intent_changed')
    R._need(intent['installId'] == proof['installId'] and intent['mode'] in ('rename', 'adopt')
            and intent['oldRoute'].encode('utf8') == _read_private(state / 'reviewed-route.json', 8192),
            'reader_move_intent_changed')
    decision, _ = R.journal._route(intent['oldRoute'].encode('utf8'), state / R._JOURNAL)
    new_decision, _ = R.journal._route(intent['newRoute'].encode('utf8'), state / R._JOURNAL)
    R._need(new_decision == R.journal.rebound_project_dir_route(decision, project),
            'reader_move_alias_changed')
    old_generation, new_generation = intent['generation']['base'], intent['generation']['gen']
    published = R._published(state, Path(old_record['db_path']), decision)
    R._need(published[0] == old_generation, 'reader_move_generation_changed')
    body_digest, epoch = _generation(Path(old_record['db_path']), state, old_generation, new_generation, decision)
    old_roots, new_roots = wiki_index_roots(decision['collections']), wiki_index_roots(new_decision['collections'])
    old_binding = _binding(Path(old_record['binding_path']), state, old_roots, decision, old_generation)
    with R._readonly(Path(old_record['catalog_path']), state) as db:
        _catalog_scan(db, old_binding)
    binding = _binding(Path(intent['store']['temp']), state, new_roots, decision, new_generation)
    R._need(R._raw(binding.as_record()) == R._raw(intent['generation']['binding'])
            and list(R._node(Path(intent['store']['temp']))) == intent['store']['identity'],
            'reader_move_binding_changed')
    aliases = []
    for (name, old_path, dev, ino), (new_name, new_path, new_dev, new_ino) in zip(
            old_binding.roots,
            binding.roots):
        R._need(name == new_name and old_roots[name] == old_path and new_roots[name] == new_path,
                'reader_move_alias_changed')
        materialized = Path(old_path if intent['mode'] == 'rename' else new_path)
        R._need(materialized.resolve(strict=True) == materialized
                and R._node(materialized) == [new_dev, new_ino], 'reader_move_alias_changed')
        aliases.append((old_path, new_path, dev, ino, new_dev, new_ino))
    identity = build.read(state)['identity']
    side = Path(old_record['sidecar_path'])
    has_side = os.path.lexists(side)
    if has_side:
        _sidecar(side, state, identity, Path(old_record['db_path']), state, decision)
    move_id = R._sha(raw_intent)
    stage = _stage_directory(state, move_id)
    stage_node = R._node(stage)
    old_paths = {}
    for key, filename in (('catalog_path', 'old-catalog.sqlite'), ('binding_path', 'old-binding.sqlite'),
                          ('sidecar_path', 'old-sidecar.sqlite')):
        if key == 'sidecar_path' and not has_side:
            continue
        old_paths[key] = _copy(Path(old_record[key]), state, stage, filename)
    if has_side:
        _sidecar(old_paths['sidecar_path'], stage, identity, Path(old_record['db_path']), state, decision)
    new_catalog = _copy(Path(old_record['catalog_path']), state, stage, 'new-catalog.sqlite',
                        meta_variants=_catalog_variants(Path(old_record['catalog_path']), state, intent['catalog']))
    with closing(sqlite3.connect(new_catalog.as_uri() + '?mode=rw', uri=True)) as db:
        for key, values in intent['catalog'].items():
            if key == 'path':
                R._need(values == old_record['catalog_path'], 'reader_move_catalog_changed'); continue
            current = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
            R._need(key in ('binding', 'roots') and len(values) == 2 and
                    current in ((values[0],), (values[1],)),
                    'reader_move_catalog_changed')
            if current != (values[1],):
                db.execute('UPDATE meta SET value=? WHERE key=?', (values[1], key))
        db.commit()
    new_binding = _copy(Path(intent['store']['temp']), state, stage, 'new-binding.sqlite')
    _binding(new_binding, stage, new_roots, decision, new_generation)
    with R._readonly(new_catalog, stage) as db:
        source.LayaSourceCatalog._check_identity(SimpleNamespace(db=db,binding=source._binding(decision['scopeId'], new_roots)))
        meta = dict(db.execute('SELECT key,value FROM meta'))
        R._need(json.loads(meta['roots']) == new_roots and meta['scope'] == decision['scopeId']
                and db.execute('SELECT phase,raw_digest FROM scans WHERE scan_id=?',
                    (binding.catalog_scan_id,)).fetchone() == ('complete', binding.catalog_raw_digest),
                'reader_move_catalog_changed')
        _catalog_scan(db, binding)
    new_parts = [new_catalog, new_binding]
    if has_side:
        new_side = _copy(side, state, stage, 'new-sidecar.sqlite')
        _sidecar(new_side, stage, {**identity, 'collections':new_decision['collections']},
                 Path(old_record['db_path']), state, new_decision)
        new_parts.append(new_side)
    return _finish(original, selected, old_record, attestation, proof, raw_intent,
                   stage, stage_node, old_paths, new_parts, body_digest,
                   old_generation, new_generation, epoch, aliases,
                   absent_sidecar=None if has_side else side)


def _finish(original, selected, old_record, attestation, proof, raw_intent,
            stage, stage_node, old_paths, new_parts, body_digest,
            old_generation, new_generation, epoch, aliases, *, absent_sidecar=None):
    state, project, root = R._issued[original][:3]
    move_id = R._sha(raw_intent)
    for path in new_parts: R._sync(path)
    R._sync(stage, True)
    _owner(); R._recheck(original)
    R._need(R.read_selection(state / R._JOURNAL) == selected, 'reader_selection_changed')
    R._need(absent_sidecar is None or not os.path.lexists(absent_sidecar), 'reader_move_sidecar_changed')
    R._need(R._node(stage) == stage_node and stage.resolve(strict=True) == stage
            and stage.stat().st_uid == os.getuid() and stage.stat().st_mode & 0o077 == 0,
            'reader_move_stage_changed')
    old_digest = R._sha(R._raw([body_digest, old_generation, epoch,
        [(key, _logical(path, stage)) for key, path in old_paths.items()]
        + ([('sidecar_path', None)] if absent_sidecar is not None else [])]))
    new_digest = R._sha(R._raw([body_digest, new_generation, epoch, aliases,
        [_logical(path, stage) for path in new_parts]
        + ([None] if absent_sidecar is not None else [])]))
    record = dict(old_record)
    if absent_sidecar is not None:
        record.update(sidecar_path=None, sidecar_node=None, sidecar_absent=True)
    for key, path in old_paths.items():
        record[key] = str(path)
        record[{'catalog_path':'catalog_node','binding_path':'binding_node'}.get(key, 'sidecar_node')] = R._node(path)
    record['capability'] = 'v6-retained-ro-v1'
    record.update(move_id=move_id, pinned_generation=old_generation,
                  exclusion_epoch=epoch, tuple_commitment=old_digest)
    endpoint = object.__new__(R.VerifiedEndpoint)
    packed = R._raw(record)
    object.__setattr__(endpoint, 'endpoint_id', R._sha(b'memvane-reader-endpoint-v1\0' + packed))
    object.__setattr__(endpoint, 'record', R._freeze(record))
    R._issued[endpoint] = (state, project, root, {}, packed, attestation)
    R._read_only.add(endpoint); R._retained.add(endpoint)
    _records[endpoint] = (original, record, attestation, proof, stage, stage_node,
        tuple((path, R._node(path), _logical(path, stage)) for path in old_paths.values()))
    from laya_move_reader_prefix import capture_layout, bundle_row
    scope = _owner()
    layout = capture_layout(project) if scope.kind == 'project' else None
    bundle = bundle_row(state)
    seal = object.__new__(MoveReadSeal)
    for key, value in dict(move_id=move_id, retained_endpoint=endpoint, old_generation=old_generation,
            exclusion_epoch=epoch, old_tuple_digest=old_digest, new_tuple_digest=new_digest,
            path_aliases=tuple(aliases), prefix_digest=R._sha(R._raw([raw_intent.decode('utf8'), aliases,
                old_digest, new_digest, 2, layout, bundle]))).items():
        object.__setattr__(seal, key, value)
    scope = _owner()
    with R._readonly(Path(old_record['db_path']), state) as db:
        exclusions = db.execute('SELECT kind,value FROM exclusions WHERE scope=? ORDER BY kind,value',
                                (old_record['scope'],)).fetchall()
    data = dict(version=2, layout=layout, bundle_row=bundle,
        move_id=move_id, endpoint_id=endpoint.endpoint_id, record=record,
        source_record=old_record, state=str(state), project=str(project), root=str(root),
        stage=str(stage), stage_node=stage_node, kind=scope.kind,
        intent=raw_intent.decode('utf8'), old_generation=old_generation, new_generation=new_generation,
        exclusion_epoch=epoch, exclusions=exclusions, body_digest=body_digest,
        old_tuple_digest=old_digest, new_tuple_digest=new_digest, path_aliases=aliases,
        prefix_digest=seal.prefix_digest, base_epoch=selected.epoch,
        previous_endpoint=selected.preferred_endpoint,
        transition_id=R._sha(b'memvane-move-reader-transition-v1\0'+move_id.encode('ascii'))[:32],
        materials=[[str(path),R._node(path),_logical(path,stage)]
                   for path in dict.fromkeys([*old_paths.values(),*new_parts])])
    _seals[seal] = (original, R._raw(data))
    return seal


def _state_pointer(intent, project, home):
    import laya_state_move as mover
    expected = dict(schema=mover.POINTER_SCHEMA, projectRoot=str(project),
        installId=intent['installId'], fromRoot=intent['fromRoot'], toRoot=intent['toRoot'],
        oldRouteSha256=R._sha(intent['oldRoute'].encode('utf8')),
        newRouteSha256=R._sha(intent['newRoute'].encode('utf8')))
    R._need(mover.read_pointer(project, home) == expected, 'reader_move_pointer_changed')


def _stage_state(intent, installer, scope):
    import laya_fresh_wiki_origin as fresh
    import laya_inactive_upgrade as inactive
    import laya_initial_build as build
    import laya_incremental_docs as docs
    import laya_state_move as mover
    from laya_index_host_route import wiki_index_roots
    from laya_product_install import _read_private
    import project_dir

    original = R._installer(installer)
    state, project, _root = R._issued[original][:3]
    R._need((state, project) == (scope.state, scope.project), 'reader_install_changed')
    selected = R.read_selection(state / R._JOURNAL)
    R._need(selected.preferred_endpoint == original.endpoint_id, 'reader_move_selection_unsupported')
    old_record, attestation, proof = R._recheck(original)
    raw_intent = _read_private(state / mover.INTENT, mover.LIMIT)
    R._need(type(intent) is dict and R._raw(intent) == raw_intent and
            mover._canonical(mover._load_intent(state, project)) == raw_intent,
            'reader_move_intent_changed')
    home = state.parents[3]
    mover._ordered_intent(intent, home)
    _state_pointer(intent, project, home)
    target = mover.target_root(state, home)
    R._need(mover.legacy_root(state, home) and intent['fromRoot'] == str(state)
            and not os.path.lexists(target), 'reader_move_alias_changed')
    mover._private_root(target.parent)
    target_parent = R._node(target.parent)
    R._need(target.parent.resolve(strict=True) == target.parent
            and target_parent[0] == R._node(state)[0], 'reader_move_alias_changed')
    decision, _ = R.journal._route(intent['oldRoute'].encode('utf8'), state / R._JOURNAL)
    new_decision, _ = R.journal._route(intent['newRoute'].encode('utf8'), target / R._JOURNAL)
    old_build = build.read(state)
    old_identity = old_build['identity']
    new_identity = {**old_identity, **{key: mover._rebase(old_identity[key], state, target)
                                      for key in mover.BUILD_KEYS}}
    new_build = {**old_build, 'epoch':old_build['epoch'] + 1,
                 'updatedNs':intent['newBuild']['updatedNs'], 'identity':new_identity}
    build._validate(new_build)
    old_att = _read_private(state / R.journal.PRODUCTION_ATTESTATION, mover.LIMIT).decode('utf8')
    new_att = R.journal._canon(R.journal.relocated_attestation(json.loads(old_att), state, target))
    old_ledger = fresh._load(state / fresh.LEDGER)
    new_route = {**decision, **{key:mover._rebase(decision[key], state, target)
                              for key in mover.ROUTE_KEYS}}
    new_ledger = {**old_ledger, **{key:mover._rebase(old_ledger[key], state, target)
                                  for key in mover.LEDGER_KEYS},
                  'epoch':old_ledger['epoch'] + 1, 'routeSha256':R._sha(R._raw(new_route)),
                  'attestationSha256':R._sha(new_att.encode('utf8'))}
    fresh.validate_row(new_ledger, target / fresh.LEDGER)
    old_pointer = dict(schema=build.POINTER_SCHEMA, projectRoot=str(project),
                       stateRoot=str(state), installId=proof['installId'])
    side = Path(old_record['sidecar_path'])
    has_side = os.path.lexists(side)
    expected = dict(schema=mover.SCHEMA, projectRoot=str(project), installId=proof['installId'],
        fromRoot=str(state), toRoot=str(target), oldRoute=R._raw(decision).decode('utf8'),
        newRoute=R._raw(new_route).decode('utf8'), oldAttestation=old_att, newAttestation=new_att,
        oldBuild=old_build, newBuild=new_build, oldLedger=old_ledger, newLedger=new_ledger,
        oldMarker=R._raw(fresh._marker(old_ledger)).decode('utf8'),
        newMarker=R._raw(fresh._marker(new_ledger)).decode('utf8'),
        oldPointer=old_pointer, newPointer={**old_pointer, 'stateRoot':str(target)},
        docs=[old_record['db_path'], new_identity['candidateDb']] if has_side else None,
        counts=inactive._store_counts(old_identity))
    R._need(intent == expected and new_decision == new_route
            and _read_private(state / 'reviewed-route.json', 8192) == intent['oldRoute'].encode('utf8')
            and _read_private(project_dir.folder(project) / fresh.MARKER, 8192)
                == intent['oldMarker'].encode('utf8'), 'reader_move_intent_changed')
    generation = R._published(state, Path(old_record['db_path']), decision)[0]
    body_digest, epoch = _generation(Path(old_record['db_path']), state, generation, generation, decision)
    roots = wiki_index_roots(decision['collections'])
    bound = _binding(Path(old_record['binding_path']), state, roots, decision, generation)
    with R._readonly(Path(old_record['catalog_path']), state) as db:
        _catalog_scan(db, bound)
    if has_side:
        _sidecar(side, state, old_identity, Path(old_record['db_path']), state, decision)
    # The target node is the expected successor of this same-volume rename,
    # not a claim that the absent destination has already been stat'ed.
    node = R._node(state)
    aliases = [(str(state), str(target), *node, *node)]
    stage = _stage_directory(state, R._sha(raw_intent))
    stage_node = R._node(stage)
    old_paths = {key:_copy(Path(old_record[key]), state, stage, 'old-' + name)
                 for key, name in (('catalog_path','catalog.sqlite'),
                                   ('binding_path','binding.sqlite'), ('sidecar_path','sidecar.sqlite'))
                 if key != 'sidecar_path' or has_side}
    if has_side:
        _sidecar(old_paths['sidecar_path'], stage, old_identity, Path(old_record['db_path']), state, decision)
    new_catalog = _copy(Path(old_record['catalog_path']), state, stage, 'new-catalog.sqlite')
    new_binding = _copy(Path(old_record['binding_path']), state, stage, 'new-binding.sqlite')
    _binding(new_binding, stage, roots, decision, generation)
    with R._readonly(new_catalog, stage) as db:
        _catalog_scan(db, bound)
    new_parts = [new_catalog, new_binding]
    if has_side:
        new_side = _copy(side, state, stage, 'new-sidecar.sqlite', meta_variants=(
            [docs._expected_meta(old_identity)], [docs._expected_meta(new_identity)]))
        with closing(sqlite3.connect(new_side.as_uri() + '?mode=rw', uri=True)) as db:
            current = db.execute('SELECT candidate_db FROM meta').fetchall()
            R._need(current in ([(old_record['db_path'],)], [(new_identity['candidateDb'],)]),
                    'reader_move_sidecar_changed')
            if current != [(new_identity['candidateDb'],)]:
                db.execute('UPDATE meta SET candidate_db=?', (new_identity['candidateDb'],))
            db.commit()
        _sidecar(new_side, stage, new_identity, Path(old_record['db_path']), state, new_decision)
        new_parts.append(new_side)
    R._need(not os.path.lexists(target) and target.parent.resolve(strict=True) == target.parent
            and R._node(target.parent) == target_parent, 'reader_move_alias_changed')
    _state_pointer(intent, project, home)
    return _finish(original, selected, old_record, attestation, proof, raw_intent,
                   stage, stage_node, old_paths, new_parts,
                   body_digest, generation, generation, epoch, aliases,
                   absent_sidecar=None if has_side else side)


def _recheck_retained(endpoint):
    R._need(endpoint in _records, 'reader_endpoint_not_issued')
    original, record, attestation, _proof, stage, stage_node, copies = _records[endpoint]
    R._need(stage.resolve(strict=True) == stage and R._node(stage) == stage_node
            and stage.stat().st_uid == os.getuid() and stage.stat().st_mode & 0o077 == 0,
            'reader_endpoint_changed')
    _live_record, raw, proof = R._recheck(original)
    R._need(raw == attestation, 'reader_endpoint_changed')
    for path, node, digest in copies:
        R._need(R._file(path, stage) == node and _logical(path, stage) == digest,
                'reader_endpoint_changed')
    R._need(R._raw(record) == R._issued[endpoint][4], 'reader_endpoint_changed')
    return record, raw, proof

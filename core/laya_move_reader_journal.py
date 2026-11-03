"""Atomic prephysical retained-reader publication and persisted read issuance."""
import json
import os
import stat
from pathlib import Path

import laya_journal_reader as R
import laya_move_read_seal as M

_FIELDS = frozenset(('move_id','endpoint_id','record','source_record','state','project','root',
    'stage','stage_node','kind','intent','old_generation','new_generation','exclusion_epoch',
    'exclusions','body_digest','old_tuple_digest','new_tuple_digest','path_aliases','prefix_digest',
    'base_epoch','previous_endpoint','transition_id','materials'))


def seal_prefix(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='move_read_seals'").fetchone():
        return {}
    return dict(db.execute('SELECT move_id,record FROM move_read_seals'))


def _decode(move_id, packed):
    R._need(type(packed) is str and len(packed.encode('utf8')) <= 262144, 'reader_move_seal_invalid')
    data = json.loads(packed, object_pairs_hook=R.journal._pairs,
                      parse_constant=R.journal._nonfinite)
    R._need(type(data) is dict and set(data) == _FIELDS and R._raw(data).decode('utf8') == packed,
            'reader_move_seal_invalid')
    R._need(data['move_id'] == move_id and type(move_id) is str and R.journal.SHA.fullmatch(move_id)
            and type(data['base_epoch']) is int and data['base_epoch'] >= 1
            and type(data['transition_id']) is str and R.journal.ID.fullmatch(data['transition_id'])
            and data['kind'] in ('project','state'), 'reader_move_seal_invalid')
    record = data['record']
    R._need(type(record) is dict and record.get('move_id') == move_id
            and record.get('pinned_generation') == data['old_generation']
            and record.get('exclusion_epoch') == data['exclusion_epoch']
            and type(data['exclusion_epoch']) is int and data['exclusion_epoch'] >= 0
            and record.get('tuple_commitment') == data['old_tuple_digest']
            and record.get('capability') == 'v6-retained-ro-v1'
            and data['endpoint_id'] == R._sha(b'memvane-reader-endpoint-v1\0'+R._raw(record)),
            'reader_move_seal_invalid')
    R._need(data['move_id'] == R._sha(data['intent'].encode('utf8')) and
            data['prefix_digest'] == R._sha(R._raw([data['intent'], data['path_aliases'],
                data['old_tuple_digest'], data['new_tuple_digest']])), 'reader_move_seal_invalid')
    return data


def validate_endpoint(db, endpoint_id, record, attestation):
    move_id = record.get('move_id')
    rows = seal_prefix(db)
    R._need(type(move_id) is str and move_id in rows, 'reader_move_seal_missing')
    data = _decode(move_id, rows[move_id])
    R._need(data['endpoint_id'] == endpoint_id and data['record'] == record
            and R._sha(attestation) == record['attestation_sha256'], 'reader_move_seal_changed')
    transition = db.execute('SELECT epoch,preferred_endpoint,previous_endpoint FROM reader_transitions '
                            'WHERE transition_id=?', (data['transition_id'],)).fetchone()
    R._need(transition == (data['base_epoch']+1, endpoint_id, data['previous_endpoint']),
            'reader_move_transition_changed')
    previous = db.execute('SELECT record FROM read_endpoints WHERE endpoint_id=?',
                          (data['previous_endpoint'],)).fetchone()
    R._need(previous is not None and json.loads(previous[0]) == data['source_record'],
            'reader_move_previous_changed')
    return data


def validate_selection(db, selected, record):
    rows = seal_prefix(db)
    extended = db.execute("SELECT 1 FROM sqlite_master WHERE name='move_read_seals'").fetchone()
    if not extended:
        R._need(record.get('capability') != 'v6-retained-ro-v1', 'reader_move_seal_missing')
        return
    # Successor/unpin publication is a separate protocol, not generic CAS.
    R._need(len(rows) == 1 and record.get('capability') == 'v6-retained-ro-v1',
            'reader_move_selection_changed')
    data = _decode(*next(iter(rows.items())))
    R._need((selected.epoch,selected.preferred_endpoint,selected.previous_endpoint,selected.transition_id)
            == (data['base_epoch']+1,data['endpoint_id'],data['previous_endpoint'],data['transition_id']),
            'reader_move_selection_changed')


def _verify(data, attestation, *, root):
    state, project, stage = Path(data['state']), Path(data['project']), Path(data['stage'])
    R._need(str(root) == data['root'] and stage.parent == state and
            stage.resolve(strict=True) == stage and R._node(stage) == data['stage_node'],
            'reader_move_material_changed')
    stage_info = stage.lstat()
    R._need(stat.S_ISDIR(stage_info.st_mode) and stage_info.st_uid == os.getuid()
            and stat.S_IMODE(stage_info.st_mode) == 0o700, 'reader_move_material_changed')
    live, raw, proof = R._evidence(state,project,root)
    historical = data['source_record']
    R._need(proof['selectionEpoch'] >= historical['creation_epoch'], 'reader_endpoint_changed')
    live['creation_epoch'] = historical['creation_epoch']
    R._need(live == historical and raw == attestation, 'reader_move_source_changed')
    mover = __import__('laya_state_move' if data['kind']=='state' else 'laya_project_dir_move')
    R._need(mover._read(state/mover.INTENT,mover.LIMIT) == data['intent'].encode('utf8'),
            'reader_move_intent_changed')
    intent = mover._load_intent(state,project)
    R._need(intent is not None, 'reader_move_intent_changed')
    if data['kind']=='state':
        mover._ordered_intent(intent, Path.home())
        M._state_pointer(intent,project,Path.home())
    else:
        expected = dict(schema=mover.POINTER_SCHEMA,projectRoot=str(project),stateRoot=str(state),mode=intent['mode'])
        R._need(mover.read_pointer(project) == expected, 'reader_move_pointer_changed')
    materials = {}
    for path_,node,digest in data['materials']:
        path = Path(path_)
        R._need(path_ not in materials, 'reader_move_material_changed')
        R._need(R._file(path,stage) == node and M._logical(path,stage) == digest,
                'reader_move_material_changed')
        materials[path_] = (node,digest)
    record = data['record']
    expected = dict(historical)
    old_parts = []
    old_paths = []
    for key,node_key in (('catalog_path','catalog_node'),('binding_path','binding_node'),('sidecar_path','sidecar_node')):
        if key == 'sidecar_path' and record.get('sidecar_absent') is True:
            expected.update(sidecar_path=None,sidecar_node=None,sidecar_absent=True)
            old_parts.append((key,None)); continue
        path_ = record.get(key)
        R._need(type(path_) is str and path_ in materials, 'reader_move_material_changed')
        node,digest = materials[path_]
        expected[key], expected[node_key] = path_,node
        old_paths.append(path_); old_parts.append((key,digest))
    expected.update(capability='v6-retained-ro-v1',move_id=data['move_id'],
        pinned_generation=data['old_generation'],exclusion_epoch=data['exclusion_epoch'],
        tuple_commitment=data['old_tuple_digest'])
    R._need(record == expected and len(materials) == 2*len(old_paths), 'reader_move_material_changed')
    new_parts = [digest for path_,(_node,digest) in materials.items() if path_ not in old_paths]
    if record.get('sidecar_absent') is True:new_parts.append(None)
    R._need(data['old_tuple_digest'] == R._sha(R._raw([data['body_digest'],data['old_generation'],
                data['exclusion_epoch'],old_parts]))
            and data['new_tuple_digest'] == R._sha(R._raw([data['body_digest'],data['new_generation'],
                data['exclusion_epoch'],data['path_aliases'],new_parts])), 'reader_move_material_changed')
    decision = R.journal.select_route(str(project),action='recall')
    body, epoch = M._generation(Path(historical['db_path']),state,data['old_generation'],data['old_generation'],decision)
    with R._readonly(Path(historical['db_path']),state) as db:
        exclusions = db.execute('SELECT kind,value FROM exclusions WHERE scope=? ORDER BY kind,value',
                                (historical['scope'],)).fetchall()
    R._need(body == data['body_digest'] and epoch == data['exclusion_epoch']
            and R._raw(exclusions) == R._raw(data['exclusions']), 'reader_move_pin_changed')
    from laya_index_host_route import wiki_index_roots
    roots = wiki_index_roots(decision['collections'])
    bound = M._binding(Path(record['binding_path']),stage,roots,decision,data['old_generation'])
    with R._readonly(Path(record['catalog_path']),stage) as db:
        import laya_source_catalog as source
        from types import SimpleNamespace
        source.LayaSourceCatalog._check_identity(SimpleNamespace(db=db,binding=source._binding(historical['scope'],roots)))
        M._catalog_scan(db,bound)
    if record.get('sidecar_absent') is True:
        R._need(record.get('sidecar_path') is None and not os.path.lexists(historical['sidecar_path']),
                'reader_move_sidecar_changed')
    else:
        import laya_initial_build as build
        M._sidecar(Path(record['sidecar_path']),stage,build.read(state)['identity'],
                   Path(historical['db_path']),state,decision)
    return record, raw, proof


def _issued_seal(seal):
    R._need(type(seal) is M.MoveReadSeal and seal in M._seals, 'reader_move_seal_not_issued')
    scope = M._owner()
    original, packed = M._seals[seal]
    data = _decode(seal.move_id, packed.decode('utf8'))
    R._need((str(scope.state),str(scope.project),scope.kind) == (data['state'],data['project'],data['kind'])
            and seal.retained_endpoint.endpoint_id == data['endpoint_id'], 'reader_move_owner_changed')
    for name in ('old_generation','exclusion_epoch','old_tuple_digest','new_tuple_digest','prefix_digest'):
        R._need(getattr(seal,name) == data[name], 'reader_move_seal_changed')
    R._need(R._raw(seal.path_aliases) == R._raw(data['path_aliases']), 'reader_move_seal_changed')
    _record, raw, _proof = R._recheck(original)
    _verify(data,raw,root=Path(data['root']))
    decision = R.journal.select_route(data['project'],action='recall')
    body, epoch = M._generation(Path(data['source_record']['db_path']),Path(data['state']),
        data['old_generation'],data['new_generation'],decision)
    R._need(body == data['body_digest'] and epoch == data['exclusion_epoch'], 'reader_move_pin_changed')
    return data, raw


def publish_move_reader(journal, *, seal):
    data, attestation = _issued_seal(seal)
    path = R._journal(journal)
    R._need(str(path.parent) == data['state'], 'reader_install_changed')
    result = []
    def publish(db):
        current, record = R._selection(db)
        checked, raw = _issued_seal(seal)
        R._need(checked == data and raw == attestation, 'reader_move_seal_changed')
        existing = seal_prefix(db)
        if data['move_id'] in existing:
            R._need(existing[data['move_id']] == R._raw(data).decode('utf8')
                    and current.preferred_endpoint == data['endpoint_id'], 'reader_move_seal_changed')
            result.append(current); return False
        R._need(not existing and current.epoch == data['base_epoch']
                and current.preferred_endpoint == data['previous_endpoint']
                and record == data['source_record'], 'stale_reader_cas')
        for sql in R.journal._MOVE_READER_DDL.values():db.execute(sql)
        R._insert_endpoint(db,seal.retained_endpoint,data['record'],attestation)
        db.execute('INSERT INTO move_read_seals VALUES(?,?)',(data['move_id'],R._raw(data).decode('utf8')))
        db.execute('INSERT INTO reader_transitions VALUES(?,?,?,?)',
            (data['transition_id'],current.epoch+1,data['endpoint_id'],current.preferred_endpoint))
        db.execute('UPDATE reader_selection SET epoch=?,preferred_endpoint=?,previous_endpoint=?,transition_id=? WHERE id=1',
            (current.epoch+1,data['endpoint_id'],current.preferred_endpoint,data['transition_id']))
        result.append(R._selection(db)[0]); return True
    R.mutate_image(path,publish)
    return result[0]


def resolve_retained(state, project, root, endpoint_id, record, attestation):
    path = R._journal(Path(state)/R._JOURNAL)
    with R._readonly(path,path.parent) as db:
        data = validate_endpoint(db,endpoint_id,record,attestation)
        R._need(data['state'] == str(state) and data['project'] == str(project), 'reader_install_changed')
        _verify(data,attestation,root=root)
    token = object.__new__(R.VerifiedEndpoint)
    packed = R._raw(record)
    object.__setattr__(token,'endpoint_id',endpoint_id)
    object.__setattr__(token,'record',R._freeze(record))
    R._issued[token] = (Path(state),Path(project),Path(root),{},packed,attestation)
    R._read_only.add(token)
    _persisted[token] = data
    return token


import weakref
_persisted = weakref.WeakKeyDictionary()


def recheck_persisted(endpoint):
    R._need(endpoint in _persisted, 'reader_endpoint_not_issued')
    data = _persisted[endpoint]
    state,project,root,_options,packed,raw = R._issued[endpoint]
    R._need(packed == R._raw(data['record']) and endpoint.endpoint_id == data['endpoint_id'],
            'reader_endpoint_changed')
    with R._readonly(state/R._JOURNAL,state) as db:
        current = validate_endpoint(db,endpoint.endpoint_id,data['record'],raw)
        R._need(current == data, 'reader_move_seal_changed')
    return _verify(data,raw,root=root)

"""Read proof for a fenced project rename before catalog/store publication."""
import os
from pathlib import Path

import laya_journal_reader as R


def capture_layout(project):
    import laya_project_dir_move as mover
    import laya_index_host_route as host
    import project_dir
    folders = {}
    for path in (mover.legacy_folder(project), mover.current_folder(project)):
        if os.path.lexists(path):
            R._need(path.resolve(strict=True) == path and path.is_dir(), 'reader_move_layout_changed')
            folders[str(path)] = R._node(path)
    settings = project_dir.settings_path(project)
    R._need(settings is not None, 'reader_move_layout_changed')
    return dict(folders=folders, settings_sha256=R._sha(host._read_regular(settings, host.MAX_SETTINGS_BYTES)),
                decision=R.journal.select_route(str(project), action='recall'))


def bundle_row(state):
    with R._readonly(state / R._JOURNAL, state) as db:
        return R.journal._row(db)


def fenced_project(data, attestation, root):
    """Return historical source evidence only for the exact sealed prefix.

    No old pathname is restated as a live directory. The sealed wiki alias
    is checked against the actual retained folder and wiki nodes instead.
    """
    import laya_project_dir_move as mover
    import laya_fresh_wiki_origin as fresh
    import laya_initial_build as build
    import laya_index_host_route as host
    from laya_index_a import LayaIndexA
    from laya_index_claim_queue import ClaimQueue
    from laya_ledger_common import refuse_qmd_origin
    import project_dir

    R._need(data.get('version') == 2 and data['kind'] == 'project', 'reader_move_prefix_unproved')
    state, project = R._state(data['state']), Path(data['project'])
    historical, layout = data['source_record'], data['layout']
    refuse_qmd_origin(state)
    R._need(project.resolve(strict=True) == project and project.is_dir()
            and R._node(project) == historical['project_node']
            and R._node(state) == historical['state_node'], 'reader_move_layout_changed')
    intent = mover._load_intent(state, project)
    R._need(intent is not None and intent['mode'] == 'rename'
            and mover._read(state / mover.INTENT, mover.LIMIT) == data['intent'].encode('utf8')
            and mover._read(state / fresh.LEDGER) == R._raw(intent['fencedLedger']),
            'reader_move_prefix_unproved')
    R._need(mover.read_pointer(project) == dict(schema=mover.POINTER_SCHEMA,
        projectRoot=str(project), stateRoot=str(state), mode='rename'), 'reader_move_pointer_changed')
    old, new = mover.legacy_folder(project), mover.current_folder(project)
    R._need(type(layout) is dict and set(layout) == {'folders', 'settings_sha256', 'decision'}
            and set(layout['folders']) == {str(old)}, 'reader_move_layout_changed')
    R._need(os.path.lexists(old) != os.path.lexists(new), 'reader_move_layout_changed')
    folder = old if os.path.lexists(old) else new
    R._need(folder.resolve(strict=True) == folder and folder.is_dir()
            and R._node(folder) == layout['folders'][str(old)], 'reader_move_layout_changed')
    decision = layout['decision']
    route, route_sha = R.journal._route(intent['oldRoute'].encode('utf8'), state / R._JOURNAL)
    new_route, _ = R.journal._route(intent['newRoute'].encode('utf8'), state / R._JOURNAL)
    R._need(decision.get('state') == 'candidate'
            and all(decision.get(k) == v for k, v in route.items() if k in decision)
            and decision['collections'] == historical['roots']
            and R._sha(R._raw(decision['recallSettings'])) == historical['settings']
            and new_route == R.journal.rebound_project_dir_route(route, project), 'reader_move_source_changed')
    settings = host._read_regular(folder / project_dir.SETTINGS, host.MAX_SETTINGS_BYTES)
    expected_roots = route['collections'] if folder == old else new_route['collections']
    R._need(R._sha(settings) == layout['settings_sha256']
            and host._derived_collections(route, project, settings) == expected_roots,
            'reader_move_layout_changed')
    for filename, value in ((fresh.ROUTE, intent['oldRoute']), (fresh.MARKER, intent['oldMarker'])):
        R._need(mover._read(folder / filename, 8192) == value.encode('utf8'), 'reader_move_prefix_unproved')
    R._need(mover._read(state / 'reviewed-route.json', 8192) == intent['oldRoute'].encode('utf8')
            and build.read(state) == intent['oldBuild'], 'reader_move_prefix_unproved')
    aliases = data['path_aliases']
    old_roots, new_roots = host.wiki_index_roots(route['collections']), host.wiki_index_roots(new_route['collections'])
    R._need(len(aliases) == len(old_roots) == 1, 'reader_move_alias_changed')
    for old_path, new_path, dev, ino, new_dev, new_ino in aliases:
        R._need([old_path] == list(old_roots.values()) and [new_path] == list(new_roots.values())
                and [dev, ino] == [new_dev, new_ino], 'reader_move_alias_changed')
        wiki = Path(old_path if folder == old else new_path)
        R._need(wiki.resolve(strict=True) == wiki and wiki.is_dir()
                and list(mover._wiki_node(wiki)) == [dev, ino], 'reader_move_alias_changed')
    row = bundle_row(state)
    R._need(row == data['bundle_row'] and row['state'] in ('active', 'restored_old')
            and row['pending'] is None and row['selected_route_sha'] == route_sha
            and row['scope_id'] == historical['scope']
            and (row['project_root'], row['project_dev'], row['project_ino'])
                == (str(project), *historical['project_node'])
            and row['selected']['installId'] == historical['install_id']
            and row['selected']['bundleKind'] == 'full_plugin', 'reader_move_bundle_changed')
    owner = R.journal._inspect_identity(row['selected'])
    executing = R.journal._verified_executing_root(row['selected'], owner)
    observed, sha = R.journal._attestation(state / R._JOURNAL)
    R._need(executing == str(root) and R._raw(observed) == attestation
            and sha == row['provider_attestation_sha'] == historical['attestation_sha256']
            and row['provider_file_identity'] is not None
            and R.journal._provider_file_identity(observed) == row['provider_file_identity']
            and all(observed.get(k) == owner.get(k) for k in
                ('sourceCommit', 'bundleManifestSha256', 'ownershipManifestSha256', 'vectorProfile')),
            'reader_move_bundle_changed')
    ledger = intent['oldLedger']
    for key, ledger_key, node_key in (('db_path', 'candidateDb', 'candidateIdentity'),
            ('catalog_path', 'catalogDb', 'catalogIdentity'),
            ('binding_path', 'bindingStore', 'bindingStoreIdentity')):
        path = Path(historical[key])
        R._need(str(path) == ledger[ledger_key] and R._file(path, state) == ledger[node_key]
                == historical[{'db_path': 'db_node', 'catalog_path': 'catalog_node',
                               'binding_path': 'binding_node'}[key]], 'reader_move_prefix_unproved')
    with R._readonly(Path(historical['catalog_path']), state) as db:
        meta = dict(db.execute('SELECT key,value FROM meta'))
        R._need(all(meta.get(k) == intent['catalog'][k][0] for k in ('binding', 'roots')),
                'reader_move_prefix_unproved')
    R._need(route['dbPath'] == historical['db_path'] and route['scopeId'] == historical['scope']
            and route['dbIncarnation'] == historical['incarnation'], 'reader_move_source_changed')
    with R._readonly(Path(route['dbPath']), state) as db:
        LayaIndexA._check_header(db)
        R._need(db.execute("SELECT value FROM schema_meta WHERE key='incarnation'").fetchone()
                == (historical['incarnation'],), 'reader_incarnation_changed')
    with R._readonly(Path(route['queuePath']), state) as db:
        R._need(ClaimQueue._check_database(db) == historical['incarnation'], 'queue_incarnation_mismatch')
    # The unchanged source descriptor is historical; every live path above is
    # proved separately. Retained tuple/body/exclusion checks remain in _verify.
    return historical, attestation, dict(productRouteAllowed=True, executingPluginRoot=executing,
        selectionEpoch=row['epoch'], projectRoot=str(project), journalPath=str(state / R._JOURNAL),
        installId=historical['install_id'], scopeId=historical['scope'],
        dbIncarnation=historical['incarnation'], vectorProfile=owner['vectorProfile']), decision

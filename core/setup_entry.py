#!/usr/bin/env python3
"""Deterministic, explicit front door for preserving setup and update.

No host installation event calls this file.  Inventory and planning are read
only; prepare writes a private generated request and inactive generations.
Activation is a separate operator action handled by install_update.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile

# Direct skill/CLI setup calls do not pass through the hook shell environment.
sys.dont_write_bytecode = True

import config as project_config
import install_update as coordinator
import qmd_route
import qmd_runtime
import runtime_update
import wiki_topical
import setup_readiness


def _file(value):
    if not value: return None
    path = Path(value)
    return str(path.resolve()) if path.is_absolute() and not path.is_symlink() and path.is_file() else None


def _directory(value):
    if not value: return None
    path = Path(value)
    return str(path.resolve()) if path.is_absolute() and not path.is_symlink() and path.is_dir() else None


def _sha_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def sqlite_extension_loading_available(connection_type=None):
    """Inspect this interpreter without loading a library or touching a DB."""
    connection_type = sqlite3.Connection if connection_type is None else connection_type
    return (callable(getattr(connection_type, 'enable_load_extension', None)) and
            callable(getattr(connection_type, 'load_extension', None)))


def discover(project, *, probe_laya=True):
    root = coordinator.project_root(project)
    inventory = coordinator.inspect(root, probe_laya=probe_laya)
    store_status, store_reason = 'selected', None
    try:
        paths = qmd_route.project_paths(root)
    except (OSError, ValueError) as exc:
        # Inventory is not a runtime fallback. Preparing the shared store is
        # an explicit phase; production hooks still fail closed in qmd_route.
        store_reason = str(exc)
        store_status = 'not_prepared' if store_reason == 'plugin_qmd_store_not_ready' else 'invalid'
        cache = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache')
        paths = {'INDEX_PATH': str(Path(os.environ.get('INDEX_PATH') or cache / 'qmd/index.sqlite')),
                 'QMD_CONFIG_DIR': str(Path(os.environ.get('QMD_CONFIG_DIR') or
                     Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'qmd')),
                 'XDG_CACHE_HOME': str(cache), 'selected': False}
    settings_source = inventory['config']
    settings = None
    if settings_source['kind'] != 'missing':
        settings = project_config.normalize_config(json.loads(Path(settings_source['path']).read_text()))
    wiki = root / '.auto-context/wiki'
    names = ([name for name in settings['collections']
              if settings['collectionPaths'].get(name) == '.auto-context/wiki'
              and settings['collectionRoles'].get(name) == 'wiki'] if settings else [])
    published = sorted((wiki / 'topical-v2').glob('*/*.md')) if wiki.is_dir() else []
    qmd_config = None
    for candidate in (root / 'qmd-config/index.yml', root / '.auto-context/qmd/index.yml'):
        if qmd_config is None: qmd_config = _file(candidate)
    routed_config = _file(Path(paths['QMD_CONFIG_DIR']) / 'index.yml')
    if qmd_config is None and routed_config and Path(routed_config).is_relative_to(root):
        qmd_config = routed_config
    result = {'schema': 'qmd-setup-entry-v1', 'inventory': inventory,
              'discovered': {'wikiDir': _directory(wiki), 'v2OptedIn': wiki_topical.opted_in(root),
                             'wikiCollections': names, 'publishedV2Cards': len(published),
                             'qmdConfig': qmd_config,
                             'externalQmdConfig': routed_config if routed_config != qmd_config else None,
                             'modelCache': _directory(paths['XDG_CACHE_HOME']),
                             'sourceIndex': _file(paths['INDEX_PATH']),
                             'indexRoute': paths['INDEX_PATH'],
                             'uv': _file(shutil.which('uv')),
                             'layaAdapter': _file(Path(__file__).parent / 'context_learning/laya_adapter.py'),
                             'layaLock': _file(Path(__file__).parent / 'context_learning/locks/laya-macos-arm64-py312.lock'),
                             'routeSelected': paths['selected'],
                             'commonStoreSelected': bool(paths.get('shared')),
                             'sharedStoreStatus': store_status,
                             'sharedStoreReason': store_reason,
                             'pythonExecutable': sys.executable,
                             'sqliteExtensionLoading': sqlite_extension_loading_available()},
              'changesApplied': 0}
    return result


def _qmd_choice(args, inventory):
    mode = args.qmd_mode
    if mode == 'auto':
        mode = ('active' if inventory['managedQmd'] and
                inventory['managedQmd'].get('status') == 'selected_managed' else 'reuse')
    if mode == 'active':
        if not inventory['managedQmd'] or inventory['managedQmd'].get('status') != 'selected_managed':
            raise ValueError('active_qmd_required')
        return {'mode': 'active'}
    node = _file(args.node or inventory['candidateExecutables']['node'])
    if not node: raise ValueError('node_executable_required')
    if mode == 'install':
        npm = _file(args.npm or inventory['candidateExecutables']['npm'])
        if not npm: raise ValueError('npm_executable_required')
        return {'mode': 'install', 'node': node, 'npm': npm,
                'allowExecution': args.approve_package_execution,
                'allowLifecycleScripts': args.approve_lifecycle_scripts}
    if mode == 'reuse':
        candidate = args.qmd_entry or shutil.which('qmd')
        entry = _file(candidate)
        if not entry: raise ValueError('qmd_entry_required')
        try: qmd_runtime._probe(entry, node)
        except (OSError, ValueError) as exc: raise ValueError('qmd_reuse_incompatible') from exc
        return {'mode': 'reuse', 'qmd': entry, 'node': node}
    raise ValueError('invalid_qmd_mode')


def _laya_choice(args, discovered, inventory):
    mode = args.laya_mode
    if mode == 'defer': return {'mode': 'defer'}
    if mode == 'active':
        if not inventory['managedLaya'] or inventory['managedLaya'].get('status') != 'selected':
            raise ValueError('active_laya_required')
        return {'mode': 'active'}
    adapter = _file(args.laya_adapter or discovered['layaAdapter'])
    model = _directory(args.laya_model_dir)
    if not adapter: raise ValueError('laya_adapter_required')
    if not model: raise ValueError('laya_model_dir_required')
    if mode == 'reuse':
        # Python virtual environments normally expose their interpreter as a
        # symlink. The downstream runtime probe attests its resolved target and
        # repeats that attestation at activation.
        candidate = Path(args.laya_executable) if args.laya_executable else None
        if (candidate is None or not candidate.is_absolute() or not candidate.is_file()
                or not os.access(candidate, os.X_OK)):
            raise ValueError('laya_executable_required')
        return {'mode': 'reuse', 'executable': str(candidate),
                'adapter': adapter, 'modelDir': model}
    if mode == 'install':
        uv = _file(args.uv or discovered['uv'])
        lock = _file(args.laya_lock or discovered['layaLock'])
        if not uv or not os.access(uv, os.X_OK): raise ValueError('uv_executable_required')
        if not lock: raise ValueError('laya_hash_lock_required')
        return {'mode': 'install', 'uv': uv, 'uvSha256': _sha_file(uv),
                'lock': lock, 'lockSha256': _sha_file(lock),
                'adapter': adapter, 'modelDir': model,
                'allowExecution': args.approve_laya_install}
    raise ValueError('invalid_laya_mode')


def _wiki_mapping(path):
    if not path: return {'mode': 'preserve'}
    source = Path(path)
    if not source.is_absolute() or source.is_symlink() or not source.is_file() or source.stat().st_size > 65536:
        raise ValueError('reviewed_v2_mappings_file_required')
    if source.stat().st_uid != os.getuid() or source.stat().st_mode & 0o077:
        raise ValueError('unsafe_reviewed_v2_mappings_file')
    rows = json.loads(source.read_text())
    if not isinstance(rows, list): raise ValueError('invalid_reviewed_v2_mappings')
    return {'mode': 'reviewed_v2', 'mappings': rows}


def _configured_model(path):
    """Read only the simple QMD embed scalar; complex YAML needs an explicit flag."""
    if not path: return None
    body = Path(path).read_text(encoding='utf8')
    if len(body) > 65536: raise ValueError('qmd_config_too_large')
    if body.lstrip().startswith('{'):
        value = json.loads(body)
        model = value.get('models', {}).get('embed') if isinstance(value, dict) else None
        return model if isinstance(model, str) and model else None
    found = re.findall(r'(?m)^models:\s*\n(?:[ \t]+[^\n]*\n)*?[ \t]+embed:[ \t]*([^\n#]+)', body)
    if len(found) != 1: return None
    scalar = found[0].strip()
    if scalar.startswith('"') and scalar.endswith('"'):
        try: scalar = json.loads(scalar)
        except ValueError: return None
    elif scalar.startswith("'") and scalar.endswith("'"):
        scalar = scalar[1:-1].replace("''", "'")
    return scalar if scalar and '\n' not in scalar else None


def check_update(project):
    found = discover(project, probe_laya=False)
    inv, detected = found['inventory'], found['discovered']
    reasons = []
    if detected['sharedStoreStatus'] != 'selected':
        reasons.append(detected['sharedStoreReason'])
    if inv['status'] != 'ready_to_plan': reasons.append(inv['status'])
    if inv['config']['kind'] != 'modern': reasons.append('modern_settings_not_selected')
    selected = inv['managedQmd'] or {}
    if selected.get('status') != 'selected_managed':
        reasons.append('managed_qmd_not_selected')
    elif (selected['proof'].get('version') != qmd_runtime.VERSION or
          not set(qmd_runtime.CAPABILITIES) <= set(selected['proof'].get('capabilities', []))):
        reasons.append('managed_qmd_incompatible')
    if not detected['routeSelected']: reasons.append('project_index_not_selected')
    if not detected['commonStoreSelected'] and (not detected['v2OptedIn'] or not detected['publishedV2Cards']):
        reasons.append('published_v2_wiki_required')
    if inv['legacyWiki'] and not detected['commonStoreSelected']:
        journal = coordinator.status(inv['project'])
        reviewed = (journal.get('phase') == 'activated' and
                    journal.get('frozen', {}).get('legacyWiki') == inv['legacyWiki'] and
                    journal.get('wikiReview', {}).get('status') == 'reviewed_v2_preserving_legacy')
        if not reviewed: reasons.append('legacy_wiki_review_required')
    if detected['commonStoreSelected']:
        model_reason = setup_readiness.qmd_model_reason()
        if model_reason: reasons.append(model_reason)
    readiness = setup_readiness.components(not reasons)
    laya_reason = readiness['componentReadiness']['laya']['reason']
    if laya_reason: reasons.append(laya_reason)
    return {**readiness, 'schema': 'qmd-setup-update-check-v1',
            'status': 'up_to_date' if not reasons else 'update_review_required',
            'reasons': reasons, 'project': inv['project'],
            'targetQmdVersion': qmd_runtime.VERSION,
            'targetLayaVersion': setup_readiness.runtime.LAYA_PIN, 'changesApplied': 0}


def plan(args):
    found = discover(args.project)
    inv, detected = found['inventory'], found['discovered']
    blockers = []
    if detected['sharedStoreStatus'] != 'selected':
        blockers.append(detected['sharedStoreReason'])
    if inv['status'] != 'ready_to_plan': blockers.append(inv['status'])
    if inv['config']['kind'] == 'missing': blockers.append('project_settings_required')
    try: qmd = _qmd_choice(args, inv)
    except ValueError as exc:
        blockers.append(str(exc)); qmd = None
    try: laya = _laya_choice(args, detected, inv)
    except ValueError as exc:
        blockers.append(str(exc)); laya = None
    wiki_request = _wiki_mapping(args.reviewed_v2_mappings)
    if inv['legacyWiki'] and wiki_request['mode'] != 'reviewed_v2':
        blockers.append('legacy_wiki_review_required')
    bootstrap = args.bootstrap_empty_wiki
    if bootstrap:
        root = Path(inv['project'])
        wiki = Path(detected['wikiDir']) if detected['wikiDir'] else None
        old_paths = (root / '.auto-context.json', root / '.agents/qmd-recall.json',
                     root / 'qmd-db/index.sqlite', root / '.auto-context/qmd/index.sqlite')
        if (args.index_mode != 'shadow' or inv['config']['kind'] != 'modern'
                or inv['legacyWiki'] or detected['routeSelected'] or args.source_index
                or any(path.exists() or path.is_symlink() for path in old_paths)
                or wiki is None or any(wiki.rglob('*.md'))
                or detected['publishedV2Cards']):
            blockers.append('empty_bootstrap_requires_new_project')
        try: coordinator._bootstrap_index_route_guard(root, detected['indexRoute'])
        except ValueError as exc: blockers.append(str(exc))
    index = {'mode': 'none'}
    if args.index_mode == 'shadow':
        if not detected['v2OptedIn'] or not detected['wikiDir'] or not detected['wikiCollections']:
            blockers.append('v2_wiki_required')
        if detected['publishedV2Cards'] == 0 and not bootstrap:
            blockers.append('published_v2_wiki_required')
        config = _file(args.qmd_config or detected['qmdConfig'])
        wiki = detected['wikiDir']
        cache = _directory(args.model_cache or detected['modelCache'])
        if not config or not Path(config).is_relative_to(Path(inv['project'])):
            blockers.append('project_qmd_config_required')
        if not cache: blockers.append('model_cache_required')
        source = None if bootstrap else args.source_index or detected['sourceIndex']
        if source and not _file(source): blockers.append('source_index_unavailable')
        configured_model = _configured_model(config)
        model = args.expected_model or configured_model
        if args.expected_model and configured_model and args.expected_model != configured_model:
            blockers.append('expected_model_config_mismatch')
        dimension = args.expected_dimension
        if source and _file(source) and dimension is None:
            inspected = runtime_update.inspect_index(source, include_file_hash=False)
            if inspected['status'] == 'readable':
                models = {row['model'] for row in inspected['modelFingerprints']}
                if model in models and len(models) == 1: dimension = inspected['dimension']
        if not model: blockers.append('expected_embedding_model_required')
        if not isinstance(dimension, int) or dimension < 1:
            blockers.append('expected_embedding_dimension_required')
        if config and cache and wiki and model and isinstance(dimension, int) and dimension > 0:
            index = {'mode': 'shadow', 'config': config, 'wikiDir': wiki,
                     'modelCache': cache, 'sourceIndex': _file(source) if source else None,
                     'expectedModel': model, 'expectedDimension': dimension,
                     'allowExecution': args.approve_index_execution}
            if bootstrap:
                index['bootstrapEmptyWiki'] = True
                index['bootstrapIndexRoute'] = detected['indexRoute']
    if qmd and qmd['mode'] == 'install' and not (qmd['allowExecution'] and qmd['allowLifecycleScripts']):
        blockers.append('package_execution_approval_required')
    if index['mode'] == 'shadow' and not index['allowExecution']:
        blockers.append('index_execution_approval_required')
    if laya and laya['mode'] == 'install' and not laya['allowExecution']:
        blockers.append('laya_install_execution_approval_required')
    request = None
    if qmd is not None:
        request = {'schema': coordinator.REQUEST_SCHEMA, 'project': inv['project'],
                   'runtimeRoots': inv['defaultRuntimeRoots'], 'qmd': qmd,
                   'laya': laya, 'index': index,
                   'config': 'copy_legacy' if inv['config']['kind'].startswith('legacy_') else 'preserve',
                   'wiki': wiki_request}
    return {'schema': 'qmd-setup-plan-v1', 'status': 'ready_to_prepare' if not blockers else 'blocked',
            'blockers': sorted(set(blockers)), 'request': request,
            'requestSha256': coordinator.digest(request) if request else None,
            'migrationScope': ('runtime_only_no_v2_index' if args.index_mode == 'none' else
                               'v2_empty_bootstrap' if bootstrap else 'v2_shadow_index'),
            'discovered': detected, 'changesApplied': 0}


def prepare(args):
    proposal = plan(args)
    if proposal['status'] != 'ready_to_prepare': return proposal
    root = coordinator.project_root(args.project)
    auto = coordinator._private_auto(root, create=True)
    request_path = auto / ('generated-install-request-' + proposal['requestSha256'] + '.json')
    encoded = (json.dumps(proposal['request'], sort_keys=True, separators=(',', ':')) + '\n').encode()
    if request_path.exists() or request_path.is_symlink():
        if (request_path.is_symlink() or not request_path.is_file() or
            request_path.stat().st_uid != os.getuid() or
            request_path.stat().st_nlink != 1 or request_path.stat().st_mode & 0o077 or
            request_path.read_bytes() != encoded):
            raise ValueError('generated_request_changed')
    else:
        fd, name = tempfile.mkstemp(prefix='.generated-install-request-', dir=auto)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'wb') as output:
                output.write(encoded); output.flush(); os.fsync(output.fileno())
            os.link(name, request_path, follow_symlinks=False)
            dirfd = os.open(auto, os.O_RDONLY)
            try: os.fsync(dirfd)
            finally: os.close(dirfd)
        except FileExistsError:
            if request_path.is_symlink() or request_path.read_bytes() != encoded:
                raise ValueError('generated_request_changed')
        finally: Path(name).unlink(missing_ok=True)
    result = coordinator.prepare(root, str(request_path))
    result['generatedRequest'] = str(request_path)
    result['requestSha256'] = proposal['requestSha256']
    result['migrationScope'] = proposal['migrationScope']
    return result


def learning_plan(args):
    from context_learning import learning_profile as profile
    if args.learning_choice=='preserve' or not args.learning_namespace:
        raise ValueError('explicit_learning_profile_and_namespace_required')
    root=Path(args.learning_namespace)
    # Exact accepted retry uses its original frozen proposal, never retimes it.
    old=root/profile.INSTALL
    row=None
    if args.accepted_learning_profile and (old.exists() or old.is_symlink()):
        from context_learning.dataset import read_private_json
        previous=read_private_json(old)
        if previous['acceptedSha256']==args.accepted_learning_profile:
            row=previous['proposal']
    if row is None:
        row=profile.proposal(Path(args.project),root,choice=args.learning_choice,
            mode=args.learning_migration,expires_at_unix_ms=args.learning_expires_at_unix_ms)
    return {'status':'learning_profile_proposed','proposal':row,'proposalSha256':profile.digest(row),'changesApplied':0}


def select_learning(args):
    from context_learning import learning_profile as profile
    if not args.accepted_learning_profile:raise ValueError('profile_acceptance_required')
    return profile.activate(Path(args.project),Path(args.learning_namespace),
        accepted_sha256=args.accepted_learning_profile,choice=args.learning_choice,
        mode=args.learning_migration,expires_at_unix_ms=args.learning_expires_at_unix_ms)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('inspect', 'check-update', 'plan', 'prepare', 'status', 'activate', 'rollback',
                                          'plan-store', 'prepare-store', 'store-status', 'activate-store', 'enroll-store',
                                          'plan-replacement-store', 'prepare-replacement-store', 'replacement-store-status',
                                          'plan-legacy-enroll', 'prepare-legacy-enroll', 'activate-legacy-enroll',
                                          'plan-install','install',
                                          'plan-learning-profile','activate-learning-profile',
                                          'plan-index-origin','review-index-provider',
                                          'prepare-index-origin','index-origin-status',
                                          'index-build-status','index-build-rescan',
                                          'finalize-index-origin',
                                          'activate-index-origin','recover-index-origin',
                                          'restore-index-origin','retire-index-origin',
                                          'abandon-index-build','abandon-inactive-index-build',
                                          'seal-host-copy'))
    parser.add_argument('--project', required=True)
    parser.add_argument('--qmd-mode', choices=('auto', 'active', 'reuse', 'install'), default='auto')
    parser.add_argument('--qmd-entry'); parser.add_argument('--node'); parser.add_argument('--npm')
    parser.add_argument('--approve-package-execution', action='store_true')
    parser.add_argument('--approve-lifecycle-scripts', action='store_true')
    parser.add_argument('--index-mode', choices=('shadow', 'none'), default='shadow')
    parser.add_argument('--qmd-config'); parser.add_argument('--model-cache')
    parser.add_argument('--source-index'); parser.add_argument('--expected-model')
    parser.add_argument('--expected-dimension', type=int)
    parser.add_argument('--approve-index-execution', action='store_true')
    parser.add_argument('--bootstrap-empty-wiki', action='store_true')
    parser.add_argument('--embedding-model-file')
    parser.add_argument('--expected-embedding-model-sha256')
    parser.add_argument('--expected-embedding-model-bytes', type=int)
    parser.add_argument('--accepted-replacement-request-sha256')
    parser.add_argument('--accepted-legacy-enrollment-sha256')
    parser.add_argument('--qmd-cpu', action='store_true')
    parser.add_argument('--qmd-backend', choices=('auto','cpu'))
    parser.add_argument('--replace-inactive-store-preparation', action='store_true')
    parser.add_argument('--store-scope', action='append')
    parser.add_argument('--allow-live-host-sessions', action='store_true')
    parser.add_argument('--writer-wait-seconds', type=float, default=30)
    parser.add_argument('--deadline-seconds',type=float,default=600)
    parser.add_argument('--approve-store-activation', action='store_true')
    parser.add_argument('--approve-project-enrollment', action='store_true')
    parser.add_argument('--old-host-sessions-restarted', action='store_true')
    parser.add_argument('--reviewed-v2-mappings')
    parser.add_argument('--laya-mode', choices=('defer', 'active', 'reuse', 'install'), default='defer')
    parser.add_argument('--laya-executable'); parser.add_argument('--laya-adapter')
    parser.add_argument('--laya-model-dir'); parser.add_argument('--uv'); parser.add_argument('--laya-lock')
    parser.add_argument('--approve-laya-install', action='store_true')
    parser.add_argument('--learning-choice',choices=('preserve','local-auto-apply','local-staged','off'),default='preserve')
    parser.add_argument('--learning-namespace')
    parser.add_argument('--learning-migration',choices=('new','preserve'),default='preserve')
    parser.add_argument('--learning-expires-at-unix-ms',type=int)
    parser.add_argument('--accepted-learning-profile')
    parser.add_argument('--source-root')
    parser.add_argument('--commit')
    parser.add_argument('--state-root')
    parser.add_argument('--install-id')
    parser.add_argument('--accepted-origin-sha256')
    parser.add_argument('--accepted-provider-sha256')
    parser.add_argument('--provider-model-file')
    parser.add_argument('--provider-dimension', type=int)
    parser.add_argument('--user-qmd-root', action='append')
    parser.add_argument('--approve-index-origin-activation', action='store_true')
    parser.add_argument('--approve-index-origin-restore', action='store_true')
    parser.add_argument('--approve-index-origin-retire', action='store_true')
    parser.add_argument('--approve-index-build-abandon', action='store_true')
    parser.add_argument('--approve-inactive-index-build-abandon', action='store_true')
    parser.add_argument('--wake-revision', type=int)
    args = parser.parse_args(argv)
    try:
        if args.action in ('plan-index-origin','review-index-provider',
                           'prepare-index-origin','index-origin-status',
                           'index-build-status','index-build-rescan',
                           'finalize-index-origin',
                           'activate-index-origin','recover-index-origin',
                           'restore-index-origin','retire-index-origin',
                           'abandon-index-build','abandon-inactive-index-build',
                           'seal-host-copy'):
            import laya_product_install as product
            installed = Path(__file__).resolve(strict=True).parent.parent
            if args.action == 'plan-index-origin':
                result = product.plan_qmd_origin(args.project, background=True)
                pending = product.retired_build_pointer(Path(args.project).resolve())
                if pending is not None:
                    # Finish the interrupted retirement before preparing.
                    result['pendingRetirement'] = pending
            elif args.action == 'review-index-provider':
                result = product.review_local_provider(
                    source_root=args.source_root, plugin_root=installed,
                    state_root=args.state_root, commit_id=args.commit,
                    model_path=args.provider_model_file,
                    dimension=args.provider_dimension)
            elif args.action == 'prepare-index-origin':
                result = product.prepare_owned_qmd_origin(
                    project_root=args.project, source_root=args.source_root,
                    plugin_root=installed, state_root=args.state_root,
                    commit_id=args.commit,
                    expected_proposal_sha256=args.accepted_origin_sha256,
                    accepted_provider_sha256=args.accepted_provider_sha256,
                    install_id=args.install_id,
                    user_qmd_roots=args.user_qmd_root,
                    model_path=args.provider_model_file,
                    dimension=args.provider_dimension,
                    background=True)
            else:
                state = product._private_parent(args.state_root)
                journal = state / 'install-journal.sqlite'
                if args.action == 'index-origin-status':
                    from laya_install_journal import inspect_journal
                    result = {'status': 'inspected', 'journal': inspect_journal(journal),
                              'routeFile': str(state / 'reviewed-route.json')}
                    from laya_initial_build import read as read_build
                    if read_build(state) is not None:
                        result['initialBuild'] = product.initial_build_status(state)
                elif args.action in ('index-build-status','index-build-rescan'):
                    from laya_initial_build import read as read_build
                    build = read_build(state)
                    if build is None or build['identity']['projectRoot'] != str(Path(args.project).resolve()):
                        raise ValueError('initial_build_project_mismatch')
                    result = (product.initial_build_status(state)
                              if args.action == 'index-build-status' else
                              product.request_initial_build_rescan(state))
                elif args.action == 'finalize-index-origin':
                    result = product.finalize_initial_build(
                        project_root=args.project, state_root=state)
                elif args.action == 'restore-index-origin':
                    # User-visible withdrawal to the preserved QMD; deletes no DB.
                    if not args.approve_index_origin_restore:
                        raise ValueError('index_origin_restore_approval_required')
                    result = product.restore_origin_selection(
                        project_root=args.project, state_root=state)
                elif args.action == 'retire-index-origin':
                    # A QMD change stranded the bound build; deletes no DB or store.
                    if not args.approve_index_origin_retire:
                        raise ValueError('index_origin_retire_approval_required')
                    result = product.retire_drifted_origin(
                        project_root=args.project, state_root=state)
                elif args.action == 'abandon-index-build':
                    # An unused registered build (no source read, nothing written);
                    # the pending laya-sync wake is cancelled by revision. Deletes nothing.
                    if not args.approve_index_build_abandon:
                        raise ValueError('index_build_abandon_approval_required')
                    result = product.abandon_unused_initial_build(
                        project_root=args.project, state_root=state,
                        install_id=args.install_id, commit_id=args.commit,
                        wake_revision=args.wake_revision)
                elif args.action == 'abandon-inactive-index-build':
                    # QA-only recovery of a partly built, still unselected
                    # candidate. All private stores are backed up and kept.
                    if not args.approve_inactive_index_build_abandon:
                        raise ValueError('inactive_index_build_abandon_approval_required')
                    result = product.abandon_inactive_initial_build(
                        project_root=args.project, state_root=state,
                        install_id=args.install_id, commit_id=args.commit,
                        wake_revision=args.wake_revision)
                elif args.action == 'seal-host-copy':
                    # Run from the other host's installed copy: seal it in place
                    # against the journal-bound owner; it never selects a route.
                    from laya_install_journal import inspect_journal
                    from laya_product_bundle import (inspect_full_plugin_candidate,
                                                     seal_companion_host_copy)
                    recorded = inspect_journal(journal)
                    identity = recorded.get('selected') or recorded.get('pending')
                    route = json.loads(product._read_private(state / 'reviewed-route.json', 8192))
                    if not isinstance(identity, dict) or not isinstance(route, dict):
                        raise ValueError('host_copy_owner_unselected')
                    if route.get('projectRoot') != str(Path(args.project).resolve()):
                        raise ValueError('host_copy_project_mismatch')
                    owner = inspect_full_plugin_candidate(identity.get('pluginRoot'))
                    if any(owner[key] != identity.get(key) for key in
                           ('sourceCommit', 'bundleManifestSha256', 'vectorProfile')):
                        raise ValueError('host_copy_owner_changed')
                    proof = seal_companion_host_copy(owner['pluginRoot'], installed)
                    result = {'status': 'host_copy_sealed', 'ownerRoot': owner['pluginRoot'],
                              **{key: proof[key] for key in
                                 ('pluginRoot', 'sourceCommit', 'bundleManifestSha256')}}
                else:
                    route = product._read_private(state / 'reviewed-route.json', 8192)
                    with product._install_lock(state):
                        if args.action == 'activate-index-origin':
                            if not args.approve_index_origin_activation:
                                raise ValueError('index_origin_activation_approval_required')
                            result = product.activate_origin_selection(
                                Path(args.project), journal, route)
                        else:
                            result = product.recover_origin_selection(
                                Path(args.project), journal, route)
        elif args.action=='plan-learning-profile':result=learning_plan(args)
        elif args.action=='activate-learning-profile':result=select_learning(args)
        elif args.action in ('install','plan-install'):
            selected=learning_plan(args) if args.learning_choice!='preserve' else None
            if args.action=='install' and selected and args.accepted_learning_profile!=selected['proposalSha256']:
                raise ValueError('profile_acceptance_required')
            import shared_install
            result=shared_install.install(args) if args.action=='install' else shared_install.plan(args)
            from context_learning.learning_profile import recommendation
            source=coordinator._config_source(coordinator.project_root(args.project))
            raw=json.loads(Path(source['path']).read_text()) if source['kind']!='missing' else {}
            result['learningProfileOffer']=recommendation(raw)
            if selected:result['learningProfileProposal']=selected
            if args.action=='install' and selected and result.get('qmdReady') is True:
                # Outside shared_install's settings lock; independent exact CAS.
                try:result['learningProfile']=select_learning(args)
                except (OSError,ValueError,KeyError,TypeError) as exc:
                    result['learningProfile']={'status':'profile_review_required','reason':str(exc),'changesApplied':0}
                    result['status']='qmd_ready_learning_profile_review_required'
        elif args.action.endswith('-store') or args.action in ('store-status', 'replacement-store-status',
                'plan-legacy-enroll', 'prepare-legacy-enroll', 'activate-legacy-enroll'):
            import shared_qmd_setup
            result = shared_qmd_setup.run(args)
        else:
            result = {'inspect': lambda: discover(args.project), 'check-update': lambda: check_update(args.project),
                  'plan': lambda: plan(args),
                  'prepare': lambda: prepare(args), 'status': lambda: coordinator.status(args.project),
                  'activate': lambda: coordinator.activate(args.project),
                      'rollback': lambda: coordinator.rollback(args.project)}[args.action]()
    except Exception as exc:
        from resource_deferred import ResourceDeferred
        if isinstance(exc, ResourceDeferred):
            result = {'status': 'deferred', 'reason': str(exc),
                      'changesApplied': 0}
        else:
            result = {'status': 'rejected', 'reason': str(exc), 'changesApplied': 0}
            if (str(exc) == 'cutover_source_drift_requires_fresh_generation' and
                    args.action in ('finalize-index-origin', 'activate-index-origin')):
                # The bound generation is stale and never rebound: the way on.
                result['nextAction'] = 'retire-index-origin'
            if (str(exc) == 'initial_build_already_registered' and
                    args.action == 'prepare-index-origin'):
                import laya_product_install as product
                pending = product.retired_build_pointer(Path(args.project).resolve())
                if pending is not None:
                    # Only the exact retired build's own pointer is named.
                    result['nextAction'] = pending['nextAction']
                    result['retireStateRoot'] = pending['stateRoot']
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    if result.get('status')=='deferred':return 75
    return 1 if result.get('status') in ('rejected', 'blocked', 'failed_preparing',
        'recovery_required', 'rolled_back_after_activation_failure') else 0


if __name__ == '__main__': raise SystemExit(main())

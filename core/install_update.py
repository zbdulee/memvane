#!/usr/bin/env python3
"""Explicit, preserving managed setup coordinator; no hook invokes this module.

The default command is read-only. Package installation, local model probes and
pointer changes require a reviewed request and separate CLI phases. A private
project journal records the exact inputs and each independently committed step.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import uuid

import config as project_config
import qmd_installer
import qmd_runtime
import qmd_route
import sqlite_read
import runtime_update
import wiki_topical
import wiki_compile
import wiki_mutation_lock
import yaml_scalars
from context_learning import runtime_installer as laya_installer
from context_learning import runtime_setup as laya_setup

SCHEMA = 'qmd-install-update-v1'
REQUEST_SCHEMA = 'qmd-install-request-v1'
JOURNAL = 'install-update-journal.json'
PATHS = ('qmd', 'index', 'laya')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def supported_platform():
    return sys.platform == 'darwin' and platform.machine().lower() == 'arm64'


def project_root(value):
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ValueError('invalid_project_root')
    info = path.stat()
    if info.st_uid != os.getuid() or not stat.S_ISDIR(info.st_mode):
        raise ValueError('unsafe_project_root')
    return path.resolve()


def _private_auto(root, *, create=False):
    path = root / '.auto-context'
    if create and not path.exists(): path.mkdir(mode=0o700)
    if path.is_symlink() or (path.exists() and (not path.is_dir() or path.stat().st_uid != os.getuid()
                                                  or path.stat().st_mode & 0o022)):
        raise ValueError('unsafe_project_settings_dir')
    if not path.is_dir(): raise ValueError('missing_project_settings_dir')
    return path


def _atomic(path, value):
    fd, name = tempfile.mkstemp(prefix='.install-update-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf8') as output:
            json.dump(value, output, sort_keys=True, separators=(',', ':'))
            output.write('\n'); output.flush(); os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def _locked(root):
    auto = _private_auto(root, create=True)
    fd = os.open(auto / '.install-update.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('unsafe_install_update_lock')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield auto
    finally:
        os.close(fd)


def _journal(auto):
    path = auto / JOURNAL
    if not path.exists(): return None
    if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError('unsafe_install_journal')
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get('schema') != SCHEMA:
        raise ValueError('invalid_install_journal')
    return value


def _save(auto, journal):
    _atomic(auto / JOURNAL, journal)


def _legacy_wiki(root):
    wiki = root / '.auto-context/wiki'
    if wiki.is_symlink(): raise ValueError('unsafe_wiki_directory')
    if not wiki.is_dir(): return []
    cards = []
    for path in sorted(wiki.rglob('*.md')):
        if path.is_symlink(): raise ValueError('unsafe_wiki_card')
        relative = path.relative_to(root).as_posix()
        if path.name in ('SCHEMA.md', 'index.md', 'log.md') or 'topical-v2' in path.parts:
            continue
        meta, parsed = wiki_compile.parse_frontmatter(path.read_text(encoding='utf8'))
        version = meta.get('schemaVersion') if parsed else None
        cards.append({'path': relative, 'sha256': file_sha(path),
                      'reason': 'legacy_or_missing_schema_version' if version in (None, '1')
                                else 'v2_without_publication_proof' if version == '2'
                                else 'unsupported_schema_version'})
    return cards


def _config_source(root):
    modern = root / '.auto-context/settings.json'
    if modern.exists() or modern.is_symlink():
        if modern.is_symlink() or not modern.is_file(): raise ValueError('unsafe_settings_file')
        return {'kind': 'modern', 'path': str(modern), 'sha256': file_sha(modern)}
    for kind, path in (('legacy_root', root / '.auto-context.json'),
                       ('legacy_agents', root / '.agents/qmd-recall.json')):
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file(): raise ValueError('unsafe_legacy_settings')
            return {'kind': kind, 'path': str(path), 'sha256': file_sha(path)}
    return {'kind': 'missing'}


def inspect(root):
    root = project_root(root)
    result = {'schema': SCHEMA, 'status': 'ready_to_plan' if supported_platform() else 'unsupported_managed_platform',
              'project': str(root), 'managedPlatform': 'macos-arm64',
              'host': {'os': sys.platform, 'architecture': platform.machine()},
              'config': _config_source(root), 'legacyWiki': _legacy_wiki(root),
              'globalQmd': None, 'managedQmd': None, 'managedLaya': None,
              'defaultRuntimeRoots': {'qmd': str(qmd_runtime.managed_root()),
                                      'laya': str(laya_setup.default_managed_root())},
              'candidateExecutables': {'node': shutil.which('node'),
                                       'npm': shutil.which('npm')},
              'projectIndex': None, 'changesApplied': 0}
    binary = shutil.which('qmd')
    if binary:
        try: result['globalQmd'] = qmd_runtime.probe_cli(str(Path(binary).resolve()))
        except (OSError, ValueError): result['globalQmd'] = {'status': 'not_managed_compatible', 'path': binary}
    qmd_root = qmd_runtime.managed_root()
    if (qmd_root / 'active.json').is_file():
        try: result['managedQmd'] = qmd_runtime.select(qmd_root)
        except (OSError, ValueError, KeyError, TypeError): result['managedQmd'] = {'status': 'invalid_pointer'}
    laya_root = laya_setup.default_managed_root()
    if (laya_root / 'active.json').is_file():
        result['managedLaya'] = laya_setup.choose_runtime(managed_root=laya_root)
    try:
        selected = runtime_update.select_runtime(root)
        if selected: result['projectIndex'] = {'generation': selected['generation'], 'index': selected['INDEX_PATH']}
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        result['projectIndex'] = {'status': 'invalid_pointer'}
    if not supported_platform():
        result['guidance'] = 'Managed setup is limited to macOS arm64; use the documented global QMD plugin path on this host.'
    return result


def _absolute(value, label, *, directory=False):
    if not isinstance(value, str) or not Path(value).is_absolute() or Path(value).is_symlink():
        raise ValueError('invalid_' + label)
    path = Path(value)
    if not (path.is_dir() if directory else path.is_file()):
        raise ValueError('missing_' + label)
    return path.resolve()


def load_request(path, root):
    path = _absolute(path, 'request')
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o022 or path.stat().st_size > 65536:
        raise ValueError('unsafe_request')
    request = json.loads(path.read_text())
    if not isinstance(request, dict) or set(request) != {'schema', 'project', 'runtimeRoots', 'qmd', 'laya', 'index', 'config', 'wiki'}:
        raise ValueError('invalid_install_request')
    if request['schema'] != REQUEST_SCHEMA or request['project'] != str(root):
        raise ValueError('request_project_mismatch')
    roots = request['runtimeRoots']
    if not isinstance(roots, dict) or set(roots) != {'qmd', 'laya'} or any(
            not isinstance(value, str) or not Path(value).is_absolute() for value in roots.values()):
        raise ValueError('invalid_runtime_roots')
    if Path(roots['qmd']) != qmd_runtime.managed_root():
        raise ValueError('qmd_runtime_root_must_be_default')
    qmd, laya, index = request['qmd'], request['laya'], request['index']
    if not all(isinstance(item, dict) for item in (qmd, laya, index)):
        raise ValueError('invalid_install_request')
    allowed_qmd = {'active': {'mode'}, 'reuse': {'mode', 'qmd', 'node'},
                   'install': {'mode', 'node', 'npm', 'allowExecution', 'allowLifecycleScripts'}}
    allowed_laya = {'defer': {'mode'}, 'active': {'mode'},
                    'reuse': {'mode', 'executable', 'adapter', 'modelDir'},
                    'install': {'mode', 'uv', 'uvSha256', 'lock', 'lockSha256',
                                'adapter', 'modelDir', 'allowExecution'}}
    allowed_index = {'none': {'mode'}, 'shadow': {'mode', 'config', 'wikiDir', 'modelCache',
                                                'expectedModel', 'expectedDimension', 'sourceIndex', 'allowExecution'}}
    if index.get('mode') == 'shadow' and index.get('bootstrapEmptyWiki') is True:
        allowed_index['shadow'] = allowed_index['shadow'] | {'bootstrapEmptyWiki', 'bootstrapIndexRoute'}
    for item, allowed in ((qmd, allowed_qmd), (laya, allowed_laya), (index, allowed_index)):
        mode = item.get('mode')
        if mode not in allowed or set(item) != allowed[mode]: raise ValueError('invalid_install_request')
    wiki = request['wiki']
    valid_wiki = wiki == {'mode': 'preserve'} or (isinstance(wiki, dict) and
        set(wiki) == {'mode', 'mappings'} and wiki['mode'] == 'reviewed_v2' and
        isinstance(wiki['mappings'], list))
    if request['config'] not in ('preserve', 'copy_legacy') or not valid_wiki:
        raise ValueError('invalid_install_request')
    for item in (qmd, laya, index):
        if 'allowExecution' in item and type(item['allowExecution']) is not bool:
            raise ValueError('invalid_install_request')
    if 'allowLifecycleScripts' in qmd and type(qmd['allowLifecycleScripts']) is not bool:
        raise ValueError('invalid_install_request')
    if index['mode'] == 'shadow' and (type(index['expectedDimension']) is not int or
                                      index['expectedDimension'] < 1 or not index['expectedModel']):
        raise ValueError('invalid_install_request')
    if index.get('bootstrapEmptyWiki') and (index['sourceIndex'] is not None or
            request['config'] != 'preserve' or request['wiki'] != {'mode': 'preserve'} or
            not isinstance(index.get('bootstrapIndexRoute'), str) or
            not Path(index['bootstrapIndexRoute']).is_absolute()):
        raise ValueError('empty_bootstrap_requires_new_project')
    return request


def _reviewed_wiki(root, legacy, wiki_request):
    if not legacy:
        return {'status': 'no_legacy_cards', 'cards': 0}
    if wiki_request['mode'] != 'reviewed_v2':
        return {'status': 'review_required', 'cards': len(legacy)}
    mappings = wiki_request['mappings']
    if len(mappings) != len(legacy): raise ValueError('legacy_review_incomplete')
    by_path = {row['path']: row for row in legacy}
    seen = set()
    import wiki_topical_publish as publisher
    for row in mappings:
        if (not isinstance(row, dict) or set(row) != {'legacyPath', 'legacySha256', 'generationId', 'cardId'}
                or row['legacyPath'] not in by_path or row['legacyPath'] in seen
                or row['legacySha256'] != by_path[row['legacyPath']]['sha256']):
            raise ValueError('legacy_review_changed')
        seen.add(row['legacyPath'])
        record = publisher._attested(root, row['generationId'], row['cardId'])
        page = root / '.auto-context/wiki/topical-v2' / row['generationId'] / (row['cardId'] + '.md')
        generated = wiki_topical.read_generation_bytes(root, row['generationId'],
                                                        'cards/' + row['cardId'] + '.md')
        if page.is_symlink() or not page.is_file() or publisher._publication_proof(
                root, row['generationId'], row['cardId'], hashlib.sha256(generated).hexdigest(),
                record) is None:
            raise ValueError('reviewed_v2_publication_missing')
        fd, temporary = tempfile.mkstemp(prefix='qmd-reviewed-v2-', suffix='.md')
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'wb') as output: output.write(generated)
            body_hash = yaml_scalars.card_body_hash(generated.decode('utf8'))
            if body_hash is None or not wiki_compile.stamp_verification(Path(temporary), 'verified',
                    record['engine'], project_config.VERIFIED_MODE_UNKNOWN, body_hash):
                raise ValueError('reviewed_v2_body_invalid')
            if not publisher._same_published_card(page.read_bytes(), Path(temporary).read_bytes()):
                raise ValueError('reviewed_v2_page_changed')
        finally: Path(temporary).unlink(missing_ok=True)
    return {'status': 'reviewed_v2_preserving_legacy', 'cards': len(legacy)}


def _frozen_sources(root, request):
    source = _config_source(root)
    legacy = _legacy_wiki(root)
    original = request['index'].get('sourceIndex')
    if original is not None:
        index = _absolute(original, 'source_index')
        original = {'path': str(index), 'fingerprint': sqlite_read.snapshot_fingerprint(index)}
    return {'config': source, 'legacyWiki': legacy, 'sourceIndex': original}


def _check_sources(root, frozen, *, created_settings_sha=None):
    current = _config_source(root)
    original = frozen['config']
    if original['kind'] == 'modern':
        config_ok = current == original
    elif original['kind'] in ('legacy_root', 'legacy_agents'):
        config_ok = (Path(original['path']).is_file() and file_sha(original['path']) == original['sha256']
                     and (current == original or (created_settings_sha is not None and
                          current == {'kind': 'modern', 'path': str(root / '.auto-context/settings.json'),
                                      'sha256': created_settings_sha})))
    else:
        config_ok = current == original
    if not config_ok or _legacy_wiki(root) != frozen['legacyWiki']:
        raise ValueError('project_sources_changed_during_setup')
    original = frozen['sourceIndex']
    if original and (not isinstance(original.get('fingerprint'), dict) or
                     sqlite_read.snapshot_fingerprint(original['path']) != original['fingerprint']):
        raise ValueError('source_index_changed_during_setup')


def _stage_qmd(request):
    qmd = request['qmd'];root = request['runtimeRoots']['qmd']
    if qmd['mode'] == 'active':
        selected = qmd_runtime.select(root)
        return {'mode': 'active', 'generation': selected['generation'], 'wrapper': selected['wrapper']}
    if qmd['mode'] == 'reuse':
        stage = qmd_runtime.prepare_existing(root, qmd['qmd'], qmd['node'])
    else:
        stage = qmd_installer.install_new(root, node=qmd['node'], npm=qmd['npm'],
            allow_execution=qmd['allowExecution'],
            allow_lifecycle_scripts=qmd['allowLifecycleScripts'])
    return {'mode': qmd['mode'], 'generation': stage['generation'], 'wrapper': stage['wrapper']}


def _check_qmd(request, staged):
    selected = qmd_runtime._prepared(staged['generation'])
    if selected['wrapper'] != staged['wrapper']:
        raise ValueError('prepared_qmd_changed')
    if staged['mode'] == 'active' and qmd_runtime.select(request['runtimeRoots']['qmd'])['generation'] != staged['generation']:
        raise ValueError('active_qmd_changed')
    return staged


def _check_qmd_route(staged):
    selected = qmd_route.binary_info()
    if (not selected['managed'] or
            Path(selected['QMD_BIN']) != Path(staged['wrapper'])):
        raise ValueError('managed_qmd_route_mismatch')
    return selected


def _stage_laya(request):
    laya = request['laya'];root = request['runtimeRoots']['laya']
    if laya['mode'] == 'defer': return {'mode': 'defer'}
    if laya['mode'] == 'active':
        choice = laya_setup.choose_runtime(managed_root=root)
        if choice['status'] != 'selected': raise ValueError('active_laya_unavailable')
        return {'mode': 'active', 'executable': choice['executable'],
                'runtimeIdentitySha256': choice['runtime_identity_sha256']}
    if laya['mode'] == 'reuse':
        proof = laya_setup.attest_runtime(laya['executable'], laya['adapter'], laya['modelDir'])
        choice = laya_setup.choose_runtime(mode='reuse', reuse_executable=laya['executable'],
            attestation=proof['attestation'], base_model_sha256=proof['attestation']['base_model_sha256'])
        if choice['status'] != 'selected': raise ValueError('laya_reuse_incompatible')
        return {'mode': 'reuse', 'executable': choice['executable'],
                'runtimeIdentitySha256': choice['runtime_identity_sha256']}
    if not laya['allowExecution']: raise ValueError('laya_install_execution_not_enabled')
    stage = laya_installer.stage_generation(root, laya['uv'], laya['uvSha256'],
                                             laya['lock'], laya['lockSha256'])
    return {'mode': 'install', 'generation': stage['generation'],
            'executable': stage['executable'],
            'runtimeIdentitySha256': stage['runtime_identity_sha256']}


def _check_laya(request, staged):
    mode = staged['mode']
    if mode == 'defer': return staged
    if mode == 'install':
        laya_installer._generation_executable(request['runtimeRoots']['laya'], staged['executable'])
        prepared = json.loads((Path(staged['generation']) / 'prepared.json').read_text())
        if prepared['runtime_identity_sha256'] != staged['runtimeIdentitySha256']:
            raise ValueError('prepared_laya_changed')
    elif mode == 'active':
        choice = laya_setup.choose_runtime(managed_root=request['runtimeRoots']['laya'])
        if choice['status'] != 'selected' or choice['executable'] != staged['executable']:
            raise ValueError('active_laya_changed')
    else:
        proof = laya_setup.attest_runtime(staged['executable'], request['laya']['adapter'],
                                          request['laya']['modelDir'])
        if proof['probe']['runtime_identity_sha256'] != staged['runtimeIdentitySha256']:
            raise ValueError('reused_laya_changed')
    return staged


def _bootstrap_raw_index_route():
    cache = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache')
    route = Path(os.environ.get('INDEX_PATH') or cache / 'qmd/index.sqlite')
    if not route.is_absolute(): raise ValueError('invalid_qmd_runtime_path')
    return route


def _bootstrap_index_route_guard(root, recorded_route):
    """Recheck the plan's route and the current route before every cutover step."""
    if not isinstance(recorded_route, str) or not Path(recorded_route).is_absolute():
        raise ValueError('empty_bootstrap_requires_new_project')
    for route in {Path(recorded_route), _bootstrap_raw_index_route()}:
        if not route.exists() and not route.is_symlink(): continue
        resolved = route.resolve()
        if (route.is_symlink() or not route.is_file() or root == route or root in route.parents
                or root == resolved or root in resolved.parents):
            raise ValueError('empty_bootstrap_requires_new_project')
        # A shared global DB may serve other projects. It is exempt only when
        # its QMD collection paths prove that it does not cover this project.
        try:
            with sqlite_read.connect(route) as db:
                tables = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if not {'store_collections', 'documents'} <= tables:
                    raise ValueError('bootstrap_shared_index_unreviewed')
                paths = [row[0] for row in db.execute('SELECT path FROM store_collections')]
                orphan = db.execute('SELECT 1 FROM documents WHERE active=1 AND '
                    'collection NOT IN (SELECT name FROM store_collections) LIMIT 1').fetchone()
                if orphan: raise ValueError('bootstrap_shared_index_unreviewed')
        except (OSError, sqlite3.Error) as exc:
            raise ValueError('bootstrap_shared_index_unreviewed') from exc
        for value in paths:
            path = Path(value) if isinstance(value, str) else Path('')
            if not path.is_absolute(): raise ValueError('bootstrap_shared_index_unreviewed')
            path = path.resolve()
            if path == root or path in root.parents or root in path.parents:
                raise ValueError('empty_bootstrap_requires_new_project')


def _wiki_corpus(root, request):
    index = request['index']
    wiki = Path(index['wikiDir'])
    if wiki != root / '.auto-context/wiki' or wiki.is_symlink() or not wiki.is_dir():
        raise ValueError('shadow_wiki_root_mismatch')
    source = _config_source(root)
    if source['kind'] == 'missing': raise ValueError('shadow_project_settings_missing')
    settings = project_config.normalize_config(json.loads(Path(source['path']).read_text()))
    names = [name for name in settings['collections']
             if settings['collectionPaths'].get(name) == '.auto-context/wiki'
             and settings['collectionRoles'].get(name) == 'wiki']
    if settings['indexing'] is not True or len(names) != 1:
        raise ValueError('shadow_wiki_collection_required')
    config = Path(index['config'])
    if config.is_symlink() or not config.is_file() or config.stat().st_size > 65536:
        raise ValueError('shadow_qmd_config_invalid')
    config_body = config.read_text(encoding='utf8')
    if names[0] not in config_body or str(wiki) not in config_body:
        raise ValueError('shadow_qmd_config_wiki_missing')
    documents = []
    sources = {}
    import wiki_topical_publish as publisher
    for page in sorted(wiki.rglob('*.md')):
        if page.is_symlink() or not page.is_file(): raise ValueError('unsafe_shadow_wiki_page')
        relative = page.relative_to(wiki).as_posix()
        page_sha = file_sha(page)
        documents.append([relative, page_sha])
        if relative.startswith('topical-v2/'):
            parts = Path(relative).parts
            if len(parts) != 3 or not parts[2].endswith('.md'):
                raise ValueError('invalid_published_v2_path')
            gid, cid = parts[1], Path(parts[2]).stem
            record = publisher._attested(root, gid, cid)
            generated = wiki_topical.read_generation_bytes(root, gid, 'cards/' + cid + '.md')
            if publisher._publication_proof(root, gid, cid,
                    hashlib.sha256(generated).hexdigest(), record) is None:
                raise ValueError('published_v2_proof_missing')
            sidecar = json.loads(wiki_topical.read_generation_bytes(root, gid,
                'cards/' + cid + '.evidence.json'))
            for revision in sidecar['sourceRevisions']:
                source_path = root / revision['path']
                if (source_path.is_symlink() or not source_path.is_file() or
                        file_sha(source_path) != revision['sha256']):
                    raise ValueError('shadow_v2_source_changed')
                sources[revision['path']] = revision['sha256']
    if not documents:
        if not index.get('bootstrapEmptyWiki'):
            raise ValueError('shadow_wiki_empty')
        _bootstrap_index_route_guard(root, index.get('bootstrapIndexRoute'))
        old_paths = (root / '.auto-context.json', root / '.agents/qmd-recall.json',
                     root / 'qmd-db/index.sqlite', root / '.auto-context/qmd/index.sqlite')
        if (source['kind'] != 'modern' or index['sourceIndex'] is not None or
                _legacy_wiki(root) or any(path.exists() or path.is_symlink()
                                          for path in old_paths)):
            raise ValueError('empty_bootstrap_requires_new_project')
    return {'collection': names[0], 'wikiDir': str(wiki),
            'qmdConfigSha256': file_sha(config), 'documents': documents,
            'sourceRevisions': sources}


def _check_shadow_documents(index_path, corpus, expected_model):
    with sqlite_read.connect(index_path) as db:
        found = db.execute('SELECT path,hash FROM documents WHERE collection=? AND active=1 '
                           'ORDER BY path,hash', (corpus['collection'],)).fetchall()
        if [list(row) for row in found] != corpus['documents']:
            raise ValueError('shadow_wiki_documents_mismatch')
        if not corpus['documents'] and (db.execute(
                'SELECT 1 FROM documents WHERE active=1 LIMIT 1').fetchone() or
                db.execute('SELECT 1 FROM content_vectors LIMIT 1').fetchone()):
            raise ValueError('shadow_bootstrap_probe_not_removed')
        for _, content_sha in corpus['documents']:
            if not db.execute('SELECT 1 FROM content_vectors WHERE hash=? AND model=? LIMIT 1',
                              (content_sha, expected_model)).fetchone():
                raise ValueError('shadow_wiki_vectors_missing')


def _stage_index(root, request, qmd):
    index = request['index']
    if index['mode'] == 'none': return {'mode': 'none'}
    if not index['allowExecution']: raise ValueError('shadow_index_execution_not_enabled')
    if index.get('bootstrapEmptyWiki'):
        pointer = root / '.auto-context/qmd-index-active.json'
        if pointer.exists() or pointer.is_symlink():
            raise ValueError('empty_bootstrap_requires_new_project')
    corpus = _wiki_corpus(root, request)
    prior = os.environ.get('INDEX_PATH')
    try:
        if index['sourceIndex'] is None: os.environ.pop('INDEX_PATH', None)
        else: os.environ['INDEX_PATH'] = index['sourceIndex']
        stage = runtime_update.stage_shadow_index(root, qmd_bin=qmd['wrapper'],
            config_file=index['config'], wiki_dir=index['wikiDir'],
            model_cache=index['modelCache'], expected_model=index['expectedModel'],
            expected_dimension=index['expectedDimension'], allow_execution=True,
            bootstrap_empty=index.get('bootstrapEmptyWiki', False))
    finally:
        if prior is None: os.environ.pop('INDEX_PATH', None)
        else: os.environ['INDEX_PATH'] = prior
    _check_shadow_documents(stage['index'], corpus, index['expectedModel'])
    return {'mode': 'shadow', 'generation': stage['generation'],
            'index': stage['index'], 'corpus': corpus}


def _check_index(root, request, staged):
    if staged['mode'] == 'shadow':
        proof = runtime_update._prepared_shadow(root, staged['generation'])
        if proof['index'] != staged['index']: raise ValueError('prepared_index_changed')
        if _wiki_corpus(root, request) != staged['corpus']:
            raise ValueError('shadow_wiki_changed_after_prepare')
        staged_config = Path(staged['generation']) / 'config/index.yml'
        if file_sha(staged_config) != staged['corpus']['qmdConfigSha256']:
            raise ValueError('shadow_qmd_config_changed')
        _check_shadow_documents(staged['index'], staged['corpus'], request['index']['expectedModel'])
    return staged


def _archive_for_next_request(root, auto, journal):
    if journal['phase'] not in ('activated', 'rolled_back'):
        raise ValueError('different_install_request_requires_review')
    if journal['phase'] == 'activated':
        _check_activated(root, journal)
    history = auto / 'install-update-history'
    if history.is_symlink(): raise ValueError('unsafe_install_history')
    history.mkdir(mode=0o700, exist_ok=True)
    if history.stat().st_uid != os.getuid() or history.stat().st_mode & 0o077:
        raise ValueError('unsafe_install_history')
    _atomic(history / (uuid.uuid4().hex + '.json'), journal)


def _fresh_journal(root, request):
    return {'schema': SCHEMA, 'project': str(root), 'request': request,
            'requestSha256': digest(request), 'phase': 'preparing',
            'frozen': _frozen_sources(root, request), 'staged': {},
            'activation': {}, 'lastError': None}


def prepare(root, request_path):
    root = project_root(root)
    request = load_request(request_path, root)
    if not supported_platform():
        return {'status': 'unsupported_managed_platform', 'changesApplied': 0,
                'guidance': 'The bundled managed install is macOS arm64 only; use documented global QMD on this host.'}
    if not wiki_topical.opted_in(root) and request['index']['mode'] == 'shadow':
        return {'status': 'project_v2_optin_required', 'changesApplied': 0}
    with _locked(root) as auto:
        journal = _journal(auto)
        request_hash = digest(request)
        if journal is not None and journal['project'] != str(root):
            raise ValueError('install_journal_project_mismatch')
        if journal is not None and journal['requestSha256'] != request_hash:
            old = dict(journal['request']);old.pop('wiki', None)
            new = dict(request);new.pop('wiki', None)
            if journal['phase'] == 'awaiting_legacy_review' and old == new:
                journal['request'] = request; journal['requestSha256'] = request_hash
                # A reviewed replacement changes the corpus: rebuild the shadow.
                journal['staged'].pop('index', None)
                _save(auto, journal)
            else:
                _archive_for_next_request(root, auto, journal)
                journal = None
        if journal is None:
            journal = _fresh_journal(root, request)
            _save(auto, journal)
        if journal['phase'] == 'activated':
            _check_activated(root, journal)
            return {'status': 'activated', 'journal': str(auto / JOURNAL)}
        if journal['phase'] == 'rolled_back':
            journal['activation'] = {}
            journal['phase'] = 'preparing'
            _save(auto, journal)
        _check_sources(root, journal['frozen'])
        journal['phase'] = 'preparing';journal['lastError'] = None;_save(auto, journal)
        try:
            staged = journal['staged']
            if 'qmd' in staged: _check_qmd(request, staged['qmd'])
            else:
                staged['qmd'] = _stage_qmd(request);_save(auto, journal)
            if 'laya' in staged: _check_laya(request, staged['laya'])
            else:
                staged['laya'] = _stage_laya(request);_save(auto, journal)
            if 'index' in staged: _check_index(root, request, staged['index'])
            else:
                staged['index'] = _stage_index(root, request, staged['qmd']);_save(auto, journal)
            wiki = _reviewed_wiki(root, journal['frozen']['legacyWiki'], request['wiki'])
            journal['wikiReview'] = wiki
            journal['phase'] = 'awaiting_legacy_review' if wiki['status'] == 'review_required' else 'prepared'
            _save(auto, journal)
            return {'status': journal['phase'], 'journal': str(auto / JOURNAL),
                    'staged': staged, 'wikiReview': wiki, 'changesApplied': 0}
        except Exception as exc:
            journal['phase'] = 'failed_preparing';journal['lastError'] = str(exc);_save(auto, journal)
            return {'status': 'failed_preparing', 'reason': str(exc),
                    'journal': str(auto / JOURNAL), 'changesApplied': 0}


def status(root):
    root = project_root(root)
    auto = root / '.auto-context'
    if not auto.is_dir(): return {'status': 'not_prepared'}
    journal = _journal(_private_auto(root))
    return journal if journal is not None else {'status': 'not_prepared'}


def _manager_command(action):
    manager = Path(os.environ.get('QMD_BACKEND_MANAGER') or
                   Path(__file__).resolve().parent / 'backend_manager.sh')
    if not manager.is_absolute() or manager.is_symlink() or not manager.is_file():
        raise ValueError('backend_manager_unavailable')
    return subprocess.run(['bash', str(manager), action], capture_output=True,
                          text=True, timeout=120 if action == 'reload' else 12,
                          check=False)


def _daemon_identity():
    result = _manager_command('identity')
    if result.returncode == 1: return None
    if result.returncode not in (0, 3) or len(result.stdout) > 8192:
        raise ValueError('daemon_identity_unavailable')
    parts = result.stdout.strip().split('\t', 1)
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1]:
        raise ValueError('daemon_identity_invalid')
    return {'pid': int(parts[0]), 'command': parts[1],
            'healthy': result.returncode == 0}


def _reload_and_check_daemon(*, before=None):
    result = _manager_command('reload')
    if result.returncode:
        raise ValueError('managed_daemon_reload_failed')
    current = _daemon_identity()
    route = qmd_route.binary_info()
    if (current is None or not current['healthy'] or route['QMD_ENTRY'] not in current['command'] or
            (route['QMD_NODE_BIN'] and route['QMD_NODE_BIN'] not in current['command']) or
            (before and current['pid'] == before['pid'])):
        raise ValueError('managed_daemon_identity_mismatch')
    return current


def _pointer_path(root, request, kind):
    if kind == 'index': return root / '.auto-context/qmd-index-active.json'
    return Path(request['runtimeRoots'][kind]) / 'active.json'


def _snapshot(path):
    path = Path(path)
    if not path.exists():
        if path.is_symlink(): raise ValueError('unsafe_setup_pointer')
        return {'sha256': None, 'bytes': None}
    if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError('unsafe_setup_pointer')
    body = path.read_bytes()
    if len(body) > 65536: raise ValueError('oversized_setup_pointer')
    return {'sha256': hashlib.sha256(body).hexdigest(),
            'bytes': base64.b64encode(body).decode('ascii')}


def _same_snapshot(path, expected):
    return _snapshot(path)['sha256'] == expected['sha256']


def _target_matches(root, request, journal, kind):
    path = _pointer_path(root, request, kind)
    if not path.is_file() or path.is_symlink(): return False
    value = json.loads(path.read_text())
    staged = journal['staged'][kind]
    if kind == 'qmd': return value.get('generation') == staged['generation']
    if kind == 'index': return value.get('generation') == staged['generation']
    return value.get('executable') == staged['executable']


def _pointer_lock(path, kind):
    if kind == 'index': return path.parent / '.qmd-index-pointer.lock'
    return path.parent / 'activation.lock'


def _restore_pointer(root, request, journal, kind):
    path = _pointer_path(root, request, kind)
    baseline = journal['activation']['baseline'][kind]
    if _same_snapshot(path, baseline): return
    if not _target_matches(root, request, journal, kind):
        raise ValueError('setup_pointer_changed_after_activation')
    recorded = journal['activation'].get('applied', {}).get(kind)
    if recorded and _snapshot(path)['sha256'] != recorded:
        raise ValueError('setup_pointer_changed_after_activation')
    lock = _pointer_lock(path, kind)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('unsafe_setup_pointer_lock')
        fcntl.flock(fd, fcntl.LOCK_EX)
        if not _target_matches(root, request, journal, kind) or (recorded and
                _snapshot(path)['sha256'] != recorded):
            raise ValueError('setup_pointer_changed_after_activation')
        old = baseline['bytes']
        if old is None:
            path.unlink()
        else:
            body = base64.b64decode(old, validate=True)
            if hashlib.sha256(body).hexdigest() != baseline['sha256']:
                raise ValueError('setup_pointer_baseline_changed')
            temp_fd, temp = tempfile.mkstemp(prefix='.setup-restore-', dir=path.parent)
            try:
                os.fchmod(temp_fd, 0o600)
                with os.fdopen(temp_fd, 'wb') as output:
                    output.write(body);output.flush();os.fsync(output.fileno())
                os.replace(temp, path)
            finally: Path(temp).unlink(missing_ok=True)
    finally: os.close(fd)
    if not _same_snapshot(path, baseline): raise ValueError('setup_pointer_restore_failed')


def _copy_legacy_settings(root, journal):
    source = journal['frozen']['config']
    if source['kind'] not in ('legacy_root', 'legacy_agents'):
        return None
    path = root / '.auto-context/settings.json'
    body = Path(source['path']).read_bytes()
    if hashlib.sha256(body).hexdigest() != source['sha256']:
        raise ValueError('legacy_settings_changed')
    parsed = json.loads(body)
    if not isinstance(parsed, dict): raise ValueError('invalid_legacy_settings')
    project_config.normalize_config(parsed)
    expected = hashlib.sha256(body).hexdigest()
    intent = journal['activation'].get('settingsIntent')
    if not isinstance(intent, dict) or intent.get('sha256') != expected or intent.get('sourceSha256') != source['sha256']:
        raise ValueError('legacy_settings_copy_intent_missing')
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or file_sha(path) != expected:
            raise ValueError('modern_settings_conflict')
        return expected
    fd, temporary = tempfile.mkstemp(prefix='.setup-settings-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(body);output.flush();os.fsync(output.fileno())
        # A hard link publishes complete bytes only if no other writer won.
        os.link(temporary, path, follow_symlinks=False)
    finally:
        Path(temporary).unlink(missing_ok=True)
    if project_config.normalize_config(json.loads(path.read_text())) != project_config.normalize_config(parsed):
        raise ValueError('settings_copy_verification_failed')
    return expected


def _activate_one(root, auto, journal, kind):
    request = journal['request']
    staged = journal['staged'][kind]
    if kind == 'qmd' and staged['mode'] == 'active': return
    if kind == 'index' and staged['mode'] == 'none': return
    if kind == 'laya' and staged['mode'] != 'install': return
    path = _pointer_path(root, request, kind)
    if kind in journal['activation']['applied']:
        if not _target_matches(root, request, journal, kind):
            raise ValueError('setup_pointer_changed_after_activation')
        return
    if _target_matches(root, request, journal, kind):
        if _same_snapshot(path, journal['activation']['baseline'][kind]):
            raise ValueError('setup_target_same_as_baseline')
    else:
        if not _same_snapshot(path, journal['activation']['baseline'][kind]):
            raise ValueError('setup_pointer_changed_before_activation')
        journal['activation']['intent'] = kind;_save(auto, journal)
        if kind == 'qmd':
            qmd_runtime.activate(request['runtimeRoots']['qmd'], staged['generation'])
        elif kind == 'index':
            runtime_update.activate_shadow_index(root, staged['generation'])
        else:
            item = request['laya']
            laya_installer.activate_generation(request['runtimeRoots']['laya'],
                staged['executable'], item['adapter'], item['modelDir'])
    if not _target_matches(root, request, journal, kind):
        raise ValueError('setup_activation_not_selected')
    journal['activation']['applied'][kind] = _snapshot(path)['sha256']
    journal['activation']['intent'] = None;_save(auto, journal)


def _rollback_under_lock(root, auto, journal):
    request = journal['request']
    activation = journal.get('activation') or {}
    baseline = activation.get('baseline')
    if not baseline:
        journal['phase'] = 'rolled_back';_save(auto, journal)
        return {'status': 'rolled_back', 'changesApplied': 0}
    for kind in ('laya', 'index', 'qmd'):
        if kind not in baseline: continue
        path = _pointer_path(root, request, kind)
        if _same_snapshot(path, baseline[kind]): continue
        recorded = activation.get('applied', {}).get(kind)
        if recorded and _snapshot(path)['sha256'] != recorded:
            raise ValueError('setup_pointer_changed_after_activation')
        if kind == 'index':
            if not _target_matches(root, request, journal, kind):
                raise ValueError('setup_pointer_changed_after_activation')
            runtime_update.rollback_shadow_index(root)
            if not _same_snapshot(path, baseline[kind]):
                raise ValueError('setup_index_rollback_mismatch')
        else:
            _restore_pointer(root, request, journal, kind)
        activation['applied'].pop(kind, None)
        _save(auto, journal)
    settings_sha = activation.get('settingsSha256') or (activation.get('settingsIntent') or {}).get('sha256')
    if settings_sha:
        settings = root / '.auto-context/settings.json'
        if settings.exists() or settings.is_symlink():
            if settings.is_symlink() or not settings.is_file() or file_sha(settings) != settings_sha:
                raise ValueError('setup_settings_changed_after_activation')
            settings.unlink()
        activation['settingsSha256'] = None
        activation['settingsIntent'] = None
        _save(auto, journal)
    if journal['staged']['qmd']['mode'] != 'active':
        running = _daemon_identity()
        if running: _reload_and_check_daemon(before=running)
    activation['daemonIntent'] = False
    journal['phase'] = 'rolled_back';journal['lastError'] = None;_save(auto, journal)
    return {'status': 'rolled_back', 'changesApplied': 0}


def _check_activated(root, journal):
    if os.environ.get('QMD_SETUP_GUARD_FIXTURE')!='1':
        qmd_route.project_paths(root,isolated=True,reject_conflicts=True)
    request = journal['request']
    activation = journal['activation']
    for kind, recorded_sha in activation.get('applied', {}).items():
        path = _pointer_path(root, request, kind)
        if (_snapshot(path)['sha256'] != recorded_sha or
                not _target_matches(root, request, journal, kind)):
            raise ValueError('setup_pointer_changed_after_activation')
    settings_sha = activation.get('settingsSha256')
    if settings_sha:
        settings = root / '.auto-context/settings.json'
        if settings.is_symlink() or not settings.is_file() or file_sha(settings) != settings_sha:
            raise ValueError('setup_settings_changed_after_activation')
    _check_qmd(request, journal['staged']['qmd'])
    _check_qmd_route(journal['staged']['qmd'])
    if activation.get('daemonAfter'):
        current = _daemon_identity()
        route = qmd_route.binary_info()
        if current is None or not current['healthy'] or route['QMD_ENTRY'] not in current['command']:
            raise ValueError('managed_daemon_identity_mismatch')
    if journal['staged']['laya']['mode'] in ('active', 'install'):
        _check_laya(request, journal['staged']['laya'])


def activate(root):
    root = project_root(root)
    if not supported_platform(): return {'status': 'unsupported_managed_platform', 'changesApplied': 0}
    with _locked(root) as auto, wiki_mutation_lock.lock(root):
        journal = _journal(auto)
        if journal is None: return {'status': 'not_prepared', 'changesApplied': 0}
        if journal['phase'] == 'activated':
            _check_activated(root, journal)
            return {'status': 'activated_unchanged', 'changesApplied': 0}
        if journal['phase'] not in ('prepared', 'activating', 'recovery_required'):
            return {'status': journal['phase'], 'changesApplied': 0}
        request = journal['request']
        activation = journal['activation']
        try:
            _check_sources(root, journal['frozen'],
                           created_settings_sha=activation.get('settingsSha256') or
                           (activation.get('settingsIntent') or {}).get('sha256'))
            wiki = _reviewed_wiki(root, journal['frozen']['legacyWiki'], request['wiki'])
            if wiki['status'] == 'review_required':
                if activation.get('baseline'):
                    raise ValueError('legacy_wiki_review_regressed_during_activation')
                return {'status': 'awaiting_legacy_review', 'changesApplied': 0}
            _check_qmd(request, journal['staged']['qmd'])
            _check_laya(request, journal['staged']['laya'])
            _check_index(root, request, journal['staged']['index'])
            if 'baseline' not in activation:
                activation['baseline'] = {kind: _snapshot(_pointer_path(root, request, kind)) for kind in PATHS}
                activation['applied'] = {};activation['intent'] = None
                activation['daemonBefore'] = _daemon_identity()
                source = journal['frozen']['config']
                if request['config'] == 'copy_legacy' and source['kind'] in ('legacy_root', 'legacy_agents'):
                    activation['settingsIntent'] = {'sourceSha256': source['sha256'],
                                                    'sha256': source['sha256']}
                journal['phase'] = 'activating';_save(auto, journal)
            _activate_one(root, auto, journal, 'qmd')
            _check_qmd_route(journal['staged']['qmd'])
            if request['config'] == 'copy_legacy':
                activation['settingsSha256'] = _copy_legacy_settings(root, journal)
                _save(auto, journal)
            _check_index(root, request, journal['staged']['index'])
            _activate_one(root, auto, journal, 'index')
            _activate_one(root, auto, journal, 'laya')
            if journal['staged']['qmd']['mode'] != 'active' and activation.get('daemonBefore'):
                activation['daemonIntent'] = True;_save(auto, journal)
                activation['daemonAfter'] = _reload_and_check_daemon(before=activation['daemonBefore'])
                activation['daemonIntent'] = False;_save(auto, journal)
            _check_index(root, request, journal['staged']['index'])
            if os.environ.get('QMD_SETUP_GUARD_FIXTURE')!='1':
                # Production hooks require the approved common tuple. An old
                # shadow pointer alone cannot attest a completed install.
                qmd_route.project_paths(root,isolated=True,reject_conflicts=True)
            journal['phase'] = 'activated';journal['lastError'] = None;_save(auto, journal)
            return {'status': 'activated', 'journal': str(auto / JOURNAL),
                    'laya': journal['staged']['laya']['mode'],
                    'index': journal['staged']['index']['mode']}
        except Exception as exc:
            journal['lastError'] = str(exc)
            if not activation.get('baseline'):
                if journal['phase'] in ('activating', 'recovery_required') or activation:
                    journal['phase'] = 'recovery_required'
                    journal['rollbackError'] = 'missing_activation_baseline'
                    _save(auto, journal)
                    return {'status': 'recovery_required', 'reason': str(exc),
                            'rollbackReason': 'missing_activation_baseline'}
                return {'status': 'rejected', 'reason': str(exc), 'changesApplied': 0}
            try:
                result = _rollback_under_lock(root, auto, journal)
                return {'status': 'rolled_back_after_activation_failure',
                        'reason': str(exc), 'rollback': result['status']}
            except Exception as rollback_error:
                journal['phase'] = 'recovery_required'
                journal['rollbackError'] = str(rollback_error);_save(auto, journal)
                return {'status': 'recovery_required', 'reason': str(exc),
                        'rollbackReason': str(rollback_error)}


def rollback(root):
    root = project_root(root)
    with _locked(root) as auto, wiki_mutation_lock.lock(root):
        journal = _journal(auto)
        if journal is None: return {'status': 'not_prepared', 'changesApplied': 0}
        if journal['phase'] == 'rolled_back': return {'status': 'rolled_back_unchanged', 'changesApplied': 0}
        try: return _rollback_under_lock(root, auto, journal)
        except Exception as exc:
            journal['phase'] = 'recovery_required';journal['rollbackError'] = str(exc);_save(auto, journal)
            return {'status': 'recovery_required', 'reason': str(exc)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('inspect', 'prepare', 'activate', 'status', 'rollback'))
    parser.add_argument('--project', required=True)
    parser.add_argument('--request')
    args = parser.parse_args(argv)
    if (args.action == 'prepare') != bool(args.request): parser.error('--request is required only for prepare')
    try:
        actions = {'inspect': inspect, 'prepare': prepare, 'activate': activate,
                   'status': status, 'rollback': rollback}
        result = actions[args.action](args.project, args.request) if args.action == 'prepare' else actions[args.action](args.project)
    except Exception as exc:
        result = {'status': 'rejected', 'reason': str(exc), 'changesApplied': 0}
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    state = result.get('status', result.get('phase'))
    return 0 if state not in ('rejected', 'failed_preparing', 'recovery_required',
                              'rolled_back_after_activation_failure') else 1


if __name__ == '__main__':
    raise SystemExit(main())

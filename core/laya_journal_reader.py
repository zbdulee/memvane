"""Installer-owned reader selection in a v5 journal. No index writer authority."""
from __future__ import annotations

from contextlib import closing, contextmanager, ExitStack
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import re
from types import MappingProxyType, SimpleNamespace
import weakref

import laya_install_journal as journal
from laya_product_install import _install_lock, _read_private

_JOURNAL = 'install-journal.sqlite'
_MAX_IMAGE = 16 * 1024 * 1024
_issued = weakref.WeakKeyDictionary()
_read_only = weakref.WeakSet()
_retained = weakref.WeakSet()
_upgrade_readers = weakref.WeakSet()
_bootstrap_endpoints = weakref.WeakSet()
_bootstrap_installers = weakref.WeakSet()


@dataclass(frozen=True, eq=False, init=False)
class VerifiedEndpoint:
    endpoint_id: str
    record: MappingProxyType

    def __init__(self):
        raise ValueError('reader_endpoint_not_issued')


@dataclass(frozen=True, eq=False, init=False)
class InstallerSeal:
    endpoint: VerifiedEndpoint

    def __init__(self):
        raise ValueError('reader_installer_not_issued')


@dataclass(frozen=True, init=False)
class ReaderSelection:
    epoch: int
    preferred_endpoint: str
    previous_endpoint: str | None
    transition_id: str

    def __init__(self):
        raise ValueError('reader_selection_not_issued')


def _need(value, reason):
    if not value:
        raise journal.JournalEvidenceError(reason)


def _raw(obj):
    return journal._canon(obj).encode('utf8')


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _node(path):
    info = path.lstat()
    return [info.st_dev, info.st_ino]


def _file(path, state):
    _need(path.parent == state and path.resolve(strict=True) == path, 'reader_store_path_invalid')
    info = path.lstat()
    _need(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
          and not info.st_mode & 0o077, 'reader_store_unsafe')
    return [info.st_dev, info.st_ino]


def _state(state):
    from laya_journal_paths import check_journal_namespace
    state = Path(state)
    check_journal_namespace(state)
    _need(state.is_absolute() and state.resolve(strict=True) == state and
          journal._private_parent(state / _JOURNAL).parent == state, 'reader_state_unsafe')
    return state


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


@contextmanager
def _readonly(path, state):
    before = _file(path, state)
    for suffix in ('-wal', '-shm', '-journal'):
        side = Path(str(path) + suffix)
        if os.path.lexists(side):
            _file(side, state)
    try:
        from laya_index_a import _query_only_uri
        with closing(sqlite3.connect(_query_only_uri(path), uri=True,
                                     timeout=2, isolation_level=None)) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('BEGIN')
            yield db
            _need(before == _file(path, state), 'reader_store_changed')
    except sqlite3.Error as exc:
        raise journal.JournalEvidenceError('reader_store_invalid') from exc


def _published_snapshot(db, decision, *, deadline=None, allow_exclusion_advance=False,
                        _proof=None):
    from laya_index_a import LayaIndexA, _StreamDigest, _valid_relative_path
    from laya_index_vector import verify_generation_seal
    import time
    _need(db.in_transaction and db.execute('PRAGMA query_only').fetchone() == (1,),
          'reader_snapshot_unpinned')
    scope = decision['scopeId']
    LayaIndexA._check_header(db)
    incarnation = db.execute("SELECT value FROM schema_meta WHERE key='incarnation'").fetchone()
    _need(incarnation == (decision['dbIncarnation'],), 'reader_incarnation_changed')
    row = db.execute("""SELECT h.gen,s.source_manifest,s.entries_digest,g.rev_epoch
            FROM head h JOIN generations g ON g.scope=h.scope AND g.gen=h.gen
            JOIN candidate_seals s ON s.scope=h.scope AND s.gen=h.gen
            WHERE h.scope=? AND g.state='published' AND s.source_manifest=g.digest""",
                         (scope,)).fetchone()
    _need(row, 'reader_generation_unpublished')
    gen, manifest, entries, epoch = row
    exclusions = tuple(db.execute('SELECT kind,value FROM exclusions WHERE scope=? ORDER BY kind,value',
                                      (scope,)))
    _need(type(allow_exclusion_advance) is bool and type(epoch) is int and epoch>=0
          and (epoch == len(exclusions) or (allow_exclusion_advance and epoch < len(exclusions)))
          and all((kind=='path' and isinstance(value,str) and _valid_relative_path(value))
                  or (kind=='hash' and isinstance(value,str) and len(value)==64
                      and all(ch in '0123456789abcdef' for ch in value))
                  for kind,value in exclusions), 'reader_exclusions_changed')
    digest = _StreamDigest(scope)
    for item, hashed, eligible, body in db.execute("""SELECT m.path,m.hash,m.eligible,c.body
                FROM membership m LEFT JOIN content c ON c.hash=m.hash
                WHERE m.gen=? AND m.scope=? ORDER BY m.path COLLATE BINARY""", (gen, scope)):
        _need(deadline is None or time.monotonic() < deadline, 'reader_snapshot_deadline')
        _need(isinstance(body, str) and _sha(body.encode('utf8')) == hashed,
                  'reader_member_changed')
        digest.add(item, hashed, eligible)
    _need(digest.hexdigest() == entries, 'reader_manifest_changed')
    index = SimpleNamespace(db=db)
    _need(LayaIndexA._generation_fts_matches(index, gen, deadline=deadline), 'reader_fts_changed')
    seal = verify_generation_seal(index, gen, deadline=deadline)
    profile = decision['vectorProfile']
    for left, right in (('profile_id', 'profileId'), ('model_id', 'modelId'),
                            ('embedding_fingerprint', 'embeddingFingerprint'),
                            ('dimension', 'dimension'), ('metric', 'metric')):
        _need(seal[left] == profile[right], 'reader_profile_changed')
    if _proof is not None:
        _need(type(_proof) is dict and not _proof, 'reader_snapshot_input_invalid')
        _proof['vector_seal'] = MappingProxyType(dict(seal))
    return gen, manifest, entries, seal['rows_digest'], exclusions


def _published(state, path, decision):
    with _readonly(path, state) as db:
        return _published_snapshot(db, decision)


def _binding_snapshot(db, binding, decision, published):
    import laya_index_capacity as capacity
    import laya_source_catalog as source
    from laya_index_host_route import wiki_index_roots
    gen, manifest = published[:2]
    scope, incarnation = decision['scopeId'], decision['dbIncarnation']
    roots = source._check_roots(wiki_index_roots(decision['collections']))
    _need(db.in_transaction and db.execute('PRAGMA query_only').fetchone() == (1,),
          'reader_snapshot_unpinned')
    store = object.__new__(capacity.GenerationBindingStore)
    store.path, store.db = binding, db
    store.scope, store.db_incarnation, store.roots = scope, incarnation, roots
    store._roots_value = capacity._canonical(list(roots.items())).decode('ascii')
    store._node, store.store_id = tuple(_node(binding)), None
    store._check_file_node()
    store._check_store()
    row = db.execute(capacity._LOOKUP_SQL, (scope, gen, incarnation)).fetchone()
    _need(row, 'reader_binding_missing')
    bound = store._decode(gen, row)
    _need(bound.source_manifest == manifest, 'reader_binding_changed')
    for _name, root, dev, ino in bound.roots:
        base = Path(root)
        _need(base.resolve(strict=True) == base and base.is_dir()
              and _node(base) == [dev, ino], 'reader_root_changed')
    return bound,roots


def _catalog_snapshot(db, decision, bound, roots, *, deadline=None):
    import laya_source_catalog as source
    import time
    _need(db.in_transaction and db.execute('PRAGMA query_only').fetchone() == (1,),
          'reader_snapshot_unpinned')
    scope=decision['scopeId']
    identity = source._binding(scope, roots)
    source.LayaSourceCatalog._check_identity(SimpleNamespace(db=db, binding=identity))
    meta = dict(db.execute('SELECT key,value FROM meta'))
    _need(meta['scope'] == scope and json.loads(meta['roots']) == roots,
              'reader_catalog_changed')
    scan = db.execute('SELECT seq,phase,raw_digest,files,bytes FROM scans WHERE scan_id=?',
                          (bound.catalog_scan_id,)).fetchone()
    if scan is None:
        # Older catalog writers retain only the current completed scan. A
        # content-identical replacement still proves the bound raw snapshot;
        # verify its rows and counts below instead of trusting its metadata.
        scan = db.execute('SELECT s.seq,s.phase,s.raw_digest,s.files,s.bytes '
                          'FROM current c JOIN scans s ON s.seq=c.seq WHERE c.id=1').fetchone()
    _need(scan is not None and scan[1:3] == ('complete', bound.catalog_raw_digest),
          'reader_catalog_scan_changed')
    digest=hashlib.sha256();files=size=0
    for path,sha,length in db.execute('SELECT path,sha256,size FROM scan_files '
                                      'WHERE seq=? ORDER BY path', (scan[0],)):
        _need(deadline is None or time.monotonic() < deadline, 'reader_snapshot_deadline')
        _need(type(path) is str and type(sha) is str and source._HEX64.fullmatch(sha)
              and type(length) is int and length >= 0, 'reader_catalog_scan_changed')
        digest.update(source._raw_line(path,sha,length));files+=1;size+=length
    _need(deadline is None or time.monotonic() < deadline, 'reader_snapshot_deadline')
    _need(digest.hexdigest()==bound.catalog_raw_digest and scan[3:]==(files,size),
          'reader_catalog_scan_changed')


def _capacity_snapshot(binding_db, catalog_db, binding, decision, published, *, deadline=None):
    bound,roots = _binding_snapshot(binding_db,binding,decision,published)
    _catalog_snapshot(catalog_db,decision,bound,roots,deadline=deadline)


def _base_revision_snapshot(db, decision, published):
    """Observe the latest base receipt in the already pinned transaction.

    This is not a writer-cohort or complete receipt-chain proof.
    """
    import laya_publication_revision as revisions
    _need(db.in_transaction and db.execute('PRAGMA query_only').fetchone() == (1,),
          'reader_snapshot_unpinned')
    prefix=revisions._PREFIX
    baseline=db.execute('SELECT value FROM schema_meta WHERE key=?',
                        (prefix+'0'*20,)).fetchone()
    latest=db.execute('SELECT key,value FROM schema_meta WHERE key>=? AND key<? '
                      'ORDER BY key DESC LIMIT 1',(prefix,prefix+'\uffff')).fetchone()
    if baseline is None:
        _need(latest is None,'reader_base_revision_unproved')
        return None
    identity=revisions._identity('base',(decision['installId'],decision['scopeId'],
                                          decision['dbIncarnation']))
    def hex_digest(value):
        return (isinstance(value,str) and len(value)==64
                and all(ch in '0123456789abcdef' for ch in value))
    try:
        initial=json.loads(baseline[0])
        suffix=latest[0][len(prefix):]
        valid=(isinstance(initial,dict) and set(initial)=={'component','kind','identity',
               'head','headExclusionEpoch','exclusionCount','exclusionDigest','profile','vectorDigest'}
               and initial['component']=='base' and initial['kind']=='baseline'
               and initial['identity']==identity and isinstance(initial['head'],str)
               and bool(initial['head']) and type(initial['headExclusionEpoch']) is int
               and initial['headExclusionEpoch']>=0 and type(initial['exclusionCount']) is int
               and initial['exclusionCount']>=initial['headExclusionEpoch']
               and hex_digest(initial['exclusionDigest']) and hex_digest(initial['vectorDigest'])
               and initial['profile']==decision['vectorProfile']['profileId']
               and json.dumps(initial,sort_keys=True,separators=(',',':'),ensure_ascii=True)==baseline[0]
               and latest[0].startswith(prefix) and len(suffix)==20 and suffix.isascii() and suffix.isdigit())
        revision=int(suffix) if valid else -1
        if revision==0:
            exclusions=hashlib.sha256(b'memvane-base-exclusions-v1\0')
            for entry in published[4]:
                raw=json.dumps(entry,ensure_ascii=True,separators=(',',':')).encode('ascii')
                exclusions.update(len(raw).to_bytes(8,'big'));exclusions.update(raw)
            epoch=db.execute('SELECT rev_epoch FROM generations WHERE scope=? AND gen=?',
                             (decision['scopeId'],published[0])).fetchone()
            valid=(valid and latest[1]==baseline[0] and initial['head']==published[0]
                   and initial['vectorDigest']==published[3]
                   and initial['exclusionCount']==len(published[4])
                   and initial['exclusionDigest']==exclusions.hexdigest()
                   and epoch==(initial['headExclusionEpoch'],))
        elif revision>0:
            receipt=json.loads(latest[1])
            valid=(valid and isinstance(receipt,dict)
                   and set(receipt)=={'component','identity','before','after','changeDigest','exclusionCount'}
                   and receipt['component']=='base' and receipt['identity']==identity
                   and (receipt['before'] is None or (isinstance(receipt['before'],str) and bool(receipt['before'])))
                   and receipt['after']==published[0] and hex_digest(receipt['changeDigest'])
                   and type(receipt['exclusionCount']) is int
                   and receipt['exclusionCount']==len(published[4])
                   and json.dumps(receipt,sort_keys=True,separators=(',',':'),ensure_ascii=True)==latest[1])
    except (TypeError,ValueError,KeyError,IndexError):
        valid=False
    _need(valid,'reader_base_revision_unproved')
    return revision


def _sidecar_identity(db, decision):
    """Check sidecar identity on an already-pinned read-only connection."""
    import laya_incremental_docs as incremental
    _need(db.in_transaction and db.execute('PRAGMA query_only').fetchone() == (1,),
          'reader_snapshot_unpinned')
    identity=(incremental.SCHEMA,decision['installId'],decision['scopeId'],
              decision['dbIncarnation'],decision['vectorProfile']['profileId'],
              decision['dbPath'],decision['projectRoot'])
    _need(db.execute('PRAGMA user_version').fetchone() == (1,)
          and db.execute('SELECT * FROM meta').fetchall() == [identity]
          and incremental._schema_rows(db) in incremental._schema_variants(),
          'reader_sidecar_changed')
    return identity


def _sidecar_snapshot(db, decision, *, deadline=None):
    """Observe identity and revision state in a caller-pinned sidecar.

    An unsealed legacy sidecar is reported explicitly; this does not grant
    a reader capability or validate returned document/vector evidence.
    """
    import time
    import laya_publication_revision as revisions
    identity=_sidecar_identity(db,decision)
    count=db.execute('SELECT count(*) FROM docs').fetchone()[0]
    _need(type(count) is int and 0 <= count <= 20000,'reader_sidecar_changed')
    present=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='publication_revisions'").fetchone()
    if not present:
        return {'rows':count,'baseline':False,'revision':None,
                'legacyCommitment':_legacy_sidecar_commitment(db,identity,deadline=deadline)}
    initial=db.execute('SELECT value FROM publication_revisions WHERE revision=0').fetchone()
    latest=db.execute('SELECT revision,value FROM publication_revisions ORDER BY revision DESC LIMIT 1').fetchone()
    if initial is None:
        _need(latest is None,'reader_sidecar_revision_unproved')
        return {'rows':count,'baseline':False,'revision':None,
                'legacyCommitment':_legacy_sidecar_commitment(db,identity,deadline=deadline)}
    try:
        baseline=json.loads(initial[0])
        valid=(isinstance(baseline,dict)
               and set(baseline)=={'component','kind','identity','rows','digest','stateDigest'}
               and baseline['component']=='sidecar' and baseline['kind']=='baseline'
               and baseline['identity']==revisions._identity('sidecar',identity)
               and type(baseline['rows']) is int and 0 <= baseline['rows'] <= 20000
               and isinstance(baseline['digest'],str) and len(baseline['digest'])==64
               and all(ch in '0123456789abcdef' for ch in baseline['digest'])
               and isinstance(baseline['stateDigest'],str) and len(baseline['stateDigest'])==64
               and all(ch in '0123456789abcdef' for ch in baseline['stateDigest'])
               and json.dumps(baseline,sort_keys=True,separators=(',',':'),ensure_ascii=True)==initial[0])
    except (ValueError,TypeError):
        valid=False
    _need(valid and latest is not None and type(latest[0]) is int and latest[0]>=0,
          'reader_sidecar_revision_unproved')
    def digest(value):
        return (isinstance(value,str) and len(value)==64
                and all(ch in '0123456789abcdef' for ch in value))
    expected=0;state=baseline['stateDigest'];row_count=baseline['rows']
    for number,raw in db.execute('SELECT revision,value FROM publication_revisions ORDER BY revision'):
        _need(deadline is None or time.monotonic() < deadline, 'reader_snapshot_deadline')
        _need(type(number) is int and number==expected, 'reader_sidecar_revision_unproved')
        expected+=1
        if number==0:
            _need(raw==initial[0], 'reader_sidecar_revision_unproved')
            continue
        try:
            item=json.loads(raw)
            valid=(isinstance(item,dict)
                   and set(item)=={'component','identity','before','after','changeDigest',
                                  'stateBefore','stateAfter','rowsBefore','rowsAfter'}
                   and item['component']=='sidecar' and item['identity']==baseline['identity']
                   and (item['before'] is None or digest(item['before']))
                   and (item['after'] is None or digest(item['after']))
                   and item['before'] != item['after'] and digest(item['changeDigest'])
                   and item['stateBefore']==state and digest(item['stateAfter'])
                   and type(item['rowsBefore']) is int and item['rowsBefore']==row_count
                   and type(item['rowsAfter']) is int and 0<=item['rowsAfter']<=20000
                   and item['rowsAfter']==row_count+int(item['after'] is not None)-int(item['before'] is not None)
                   and item['stateAfter']==revisions._sidecar_state(state,item['before'],item['after'])
                   and json.dumps(item,sort_keys=True,separators=(',',':'),ensure_ascii=True)==raw)
        except (ValueError,TypeError):
            valid=False
        _need(valid,'reader_sidecar_revision_unproved')
        state=item['stateAfter'];row_count=item['rowsAfter']
    _need(deadline is None or time.monotonic() < deadline, 'reader_snapshot_deadline')
    _need(expected==latest[0]+1, 'reader_sidecar_revision_unproved')
    actual='0'*64;seen=0
    try:
        for document in db.execute('SELECT path,hash,title,body,profile_id,chunk_count,scan_id FROM docs ORDER BY path'):
            _need(deadline is None or time.monotonic() < deadline, 'reader_snapshot_deadline')
            actual=revisions._sidecar_state(actual,None,revisions._sidecar_row_digest(document));seen+=1
    except (TypeError,UnicodeError,OverflowError) as exc:
        raise journal.JournalEvidenceError('reader_sidecar_revision_unproved') from exc
    _need(deadline is None or time.monotonic() < deadline, 'reader_snapshot_deadline')
    _need(seen==count==row_count and actual==state, 'reader_sidecar_revision_unproved')
    return {'rows':count,'baseline':True,'revision':latest[0]}


def _legacy_sidecar_commitment(db, identity, *, deadline=None, _writer_transaction=False):
    """Bind complete legacy rows and the opaque FTS storage in one read transaction."""
    import time
    from itertools import zip_longest
    _need(db.in_transaction and (_writer_transaction or db.execute('PRAGMA query_only').fetchone()==(1,)),
          'reader_snapshot_unpinned')
    digest=hashlib.sha256(b'memvane-legacy-sidecar-v1\0')
    def add(value):
        raw=_raw(value);digest.update(len(raw).to_bytes(8,'big'));digest.update(raw)
    def check_time():
        _need(deadline is None or time.monotonic()<deadline,'reader_snapshot_deadline')
    count=0
    docs=db.execute('SELECT rowid,path,hash,title,body,profile_id,chunk_count,scan_id FROM docs ORDER BY rowid')
    fts=db.execute('SELECT rowid,path,title,body FROM docs_fts ORDER BY rowid')
    for document,search in zip_longest(docs,fts):
        check_time()
        _need(document is not None and search is not None,'reader_sidecar_revision_unproved')
        rowid,path,hashed,title,body,profile,chunks,scan=document
        _need(all(isinstance(v,str) and v for v in (path,hashed,profile))
              and all(isinstance(v,str) for v in (title,body,scan))
              and profile==identity[4] and type(chunks) is int and chunks>=1
              and _sha(body.encode('utf8'))==hashed
              and search==(rowid,path,title,body),'reader_sidecar_revision_unproved')
        add(document);count+=1
        _need(count<=20000,'reader_sidecar_revision_unproved')
    names=('docs_fts_config','docs_fts_content','docs_fts_data','docs_fts_docsize','docs_fts_idx')
    for name in names:
        check_time()
        columns=[row[1] for row in db.execute('PRAGMA table_info('+name+')')]
        _need(bool(columns) and all(re.fullmatch('[A-Za-z0-9_]+',c) for c in columns),
              'reader_sidecar_revision_unproved')
        add([name,columns])
        order=','.join('"'+col+'"' for col in columns)
        for row in db.execute('SELECT * FROM '+name+' ORDER BY '+order):
            check_time()
            values=[]
            for value in row:
                if value is None: values.append(['null'])
                elif type(value) is int: values.append(['integer',str(value)])
                elif type(value) is str: values.append(['text',value])
                elif type(value) is bytes: values.append(['blob',value.hex()])
                else: _need(False,'reader_sidecar_revision_unproved')
            add(values)
    check_time()
    return {'rows':count,'digest':digest.hexdigest()}


def _sidecar_document_snapshot(side, base, decision, path, *, deadline, require_vectors=True):
    """Check one pinned row; the caller must separately validate the snapshot."""
    import time
    from laya_index_a import LayaIndexA, _valid_relative_path
    from laya_index_host_route import wiki_index_roots
    from laya_index_vector import _read_chunk_set
    _need(type(require_vectors) is bool, 'reader_snapshot_input_invalid')
    _need(side.in_transaction and base.in_transaction
          and side.execute('PRAGMA query_only').fetchone() == (1,)
          and base.execute('PRAGMA query_only').fetchone() == (1,),
          'reader_snapshot_unpinned')
    _need(time.monotonic() < deadline, 'reader_snapshot_deadline')
    roots=wiki_index_roots(decision['collections'])
    _need(type(path) is str and _valid_relative_path(path)
          and any(path.startswith(name+'/') and _valid_relative_path(path[len(name)+1:])
                  for name in roots), 'reader_sidecar_document_changed')
    _sidecar_identity(side,decision)
    LayaIndexA._check_header(base)
    _need(base.execute("SELECT value FROM schema_meta WHERE key='incarnation'").fetchone()
          == (decision['dbIncarnation'],), 'reader_incarnation_changed')
    profile=decision['vectorProfile']
    _need(base.execute('SELECT model_id,embedding_fingerprint,dimension,metric '
                       'FROM vector_profiles WHERE profile_id=?', (profile['profileId'],)).fetchone()
          == (profile['modelId'],profile['embeddingFingerprint'],profile['dimension'],profile['metric']),
          'reader_profile_changed')
    row=side.execute('SELECT rowid,hash,title,body,profile_id,chunk_count FROM docs WHERE path=?',
                     (path,)).fetchone()
    _need(row is not None, 'reader_sidecar_document_changed')
    rowid,digest,title,body,profile_id,count=row
    _need(type(body) is str and type(title) is str and type(digest) is str
          and _sha(body.encode('utf8')) == digest and profile_id == profile['profileId']
          and type(count) is int and count > 0
          and side.execute('SELECT path,title,body FROM docs_fts WHERE rowid=?',(rowid,)).fetchone()
          == (path,title,body), 'reader_sidecar_document_changed')
    _need(base.execute('SELECT 1 FROM exclusions WHERE scope=? AND '
                       '((kind=\'path\' AND value=?) OR (kind=\'hash\' AND value=?)) LIMIT 1',
                       (decision['scopeId'],path,digest)).fetchone() is None,
          'reader_sidecar_document_excluded')
    chunks=None
    if require_vectors:
        chunks=_read_chunk_set(base,digest,title,profile_id,profile['dimension'],deadline=deadline)
        _need(chunks is not None and len(chunks)==count, 'reader_sidecar_document_changed')
    _need(time.monotonic() < deadline, 'reader_snapshot_deadline')
    return {'path':path,'hash':digest,'title':title,'body':body,'chunks':chunks}


def _capacity(state, decision, catalog, binding, published):
    with _readonly(binding, state) as db:
        bound,roots = _binding_snapshot(db,binding,decision,published)
    with _readonly(catalog, state) as db:
        _catalog_snapshot(db,decision,bound,roots)


def _evidence(state, project, executing_root, *, target=None, catalog=None, binding=None, side=None,
              allow_drift=False):
    import laya_fresh_wiki_origin as fresh
    from laya_ledger_common import refuse_qmd_origin
    state = _state(state)
    project, executing_root = Path(project), Path(executing_root)
    refuse_qmd_origin(state)
    decision = journal.select_route(str(project), action='recall')
    try:
        proof = journal.verify_selected_route(decision)
    except Exception as exc:
        if not allow_drift or not journal.is_bundle_drift(exc):
            raise
        proof = journal.verify_selected_route_read_only(decision)
        _need(proof.get('readOnlyAllowed') is True and proof.get('productRouteAllowed') is False,
              'reader_installer_unproved')
    _need((proof.get('productRouteAllowed') is True or
           (allow_drift and proof.get('readOnlyAllowed') is True))
          and proof.get('executingPluginRoot') == str(executing_root)
          and proof['projectRoot'] == str(project) and proof['journalPath'] == str(state / _JOURNAL),
          'reader_installer_unproved')
    if allow_drift:
        proof = {**proof, 'readerDecision': decision}
    ledger = fresh._load(state / fresh.LEDGER)
    info = journal.inspect_journal(state / _JOURNAL)
    return _evidence_stores(state, project, decision, proof, ledger, info,
                            target=target, catalog=catalog, binding=binding, side=side)


def _evidence_stores(state, project, decision, proof, ledger, info, *,
                     target=None, catalog=None, binding=None, side=None, sealed_attestation=None):
    """Check the same store bindings after the caller proves the selection."""
    import laya_incremental_docs as incremental
    import laya_initial_build as build
    _need(ledger['phase'] == 'active' and info['pending'] is None
          and info['state'] in ('active', 'restored_old')
          and ledger['attestationSha256'] == info['providerAttestationSha256']
          and ledger['providerFileIdentitySha256'] == info['providerFileIdentitySha256']
          and ledger['routeSha256'] == info['selectedRouteSha256']
          and ledger['scope'] == proof['scopeId'] and ledger['candidateIncarnation'] == proof['dbIncarnation'],
          'reader_selection_unproved')
    build_record = build.read(state)
    _need(build_record is not None, 'reader_build_missing')
    identity = build_record['identity']
    for k, expected in (('projectRoot', str(project)), ('stateRoot', str(state)),
                        ('scopeId', proof['scopeId']), ('installId', proof['installId']),
                        ('candidateDb', decision['dbPath']), ('dbIncarnation', proof['dbIncarnation']),
                        ('vectorProfileId', proof['vectorProfile']['profileId']),
                        ('catalogDb', ledger['catalogDb']), ('bindingStore', ledger['bindingStore'])):
        _need(identity[k] == expected, 'reader_build_changed')
    live = Path(decision['dbPath'])
    target = live if target is None else Path(target)
    catalog = Path(catalog or ledger['catalogDb'])
    binding = Path(binding or ledger['bindingStore'])
    # Staging in T1 changes only the read-only base image. Neither root files
    # nor arbitrary other capacity/sidecar data may confer a new identity.
    _need(str(catalog) == ledger['catalogDb'] and str(binding) == ledger['bindingStore']
          and _file(catalog, state) == ledger['catalogIdentity']
          and _file(binding, state) == ledger['bindingStoreIdentity']
          and _file(live, state) == ledger['candidateIdentity'], 'reader_capacity_changed')
    sidecar = state / incremental.FILE
    _need(side is None or Path(side) == sidecar, 'reader_sidecar_changed')
    expected = _published(state, live, decision)
    if target != live:
        _need(_published(state, target, decision) == expected, 'reader_staged_store_changed')
    _capacity(state, decision, catalog, binding, expected)
    if os.path.lexists(sidecar):
        with _readonly(sidecar, state) as db:
            _need(db.execute('PRAGMA user_version').fetchone() == (1,)
                  and db.execute('SELECT * FROM meta').fetchall() == [incremental._expected_meta(identity)],
                  'reader_sidecar_changed')
    raw = (_read_private(state / journal.PRODUCTION_ATTESTATION, 1024 * 1024)
           if sealed_attestation is None else sealed_attestation)
    _need(type(raw) is bytes, 'reader_attestation_changed')
    _need(_sha(raw) == ledger['attestationSha256'], 'reader_attestation_changed')
    record = dict(install_id=proof['installId'], project=str(project), project_node=_node(project),
                  state=str(state), state_node=_node(state), scope=proof['scopeId'], store_format='v6',
                  capability='v6-ro-v1', db_path=str(target), db_node=_file(target, state),
                  catalog_path=str(catalog), catalog_node=_file(catalog, state),
                  binding_path=str(binding), binding_node=_file(binding, state),
                  sidecar_path=str(sidecar), incarnation=proof['dbIncarnation'],
                  profile=proof['vectorProfile']['profileId'], attestation_sha256=_sha(raw),
                  checkout_policy='selected-main-v6', settings=_sha(_raw(decision['recallSettings'])),
                  roots=decision['collections'], creation_epoch=proof['selectionEpoch'])
    return record, raw, proof


def _endpoint(state, project, root, options):
    record, raw, proof = _evidence(state, project, root, **options)
    return _issue_endpoint(state, project, root, options, record, raw, proof)


def _issue_endpoint(state, project, root, options, record, raw, proof):
    path=Path(state)/_JOURNAL
    if journal.journal_version(path)==5:
        with _readonly(path,Path(state)) as db:
            _,selected=_selection(db)
            if 'legacy_sidecar_baseline' in selected:
                record['legacy_sidecar_baseline']=selected['legacy_sidecar_baseline']
    elif os.path.lexists(record['sidecar_path']):
        decision=journal.select_route(str(project),action='recall')
        with _readonly(Path(record['sidecar_path']),Path(state)) as db:
            side=_sidecar_snapshot(db,decision)
        if not side['baseline']:
            record['legacy_sidecar_baseline']=side['legacyCommitment']
    token = object.__new__(VerifiedEndpoint)
    packed = _raw(record)
    object.__setattr__(token, 'endpoint_id', _sha(b'memvane-reader-endpoint-v1\0' + packed))
    object.__setattr__(token, 'record', _freeze(record))
    _issued[token] = (Path(state), Path(project), Path(root), dict(options), packed, raw)
    return token, proof


def _bootstrap_evidence(state, project, root, *, target=None, catalog=None, binding=None, side=None):
    """Prove old read evidence and independently review the executing upgrade target."""
    import laya_active_upgrade as upgrade
    import laya_bundle_upgrade as bundle
    from laya_ledger_common import refuse_qmd_origin
    state, project, root = _state(state), Path(project), Path(root)
    refuse_qmd_origin(state)
    intent = upgrade._load_intent(state, project)
    _need(intent is not None and intent['schema'] == upgrade.V1 and
          intent['newProof']['pluginRoot'] == str(root), 'reader_installer_unproved')
    _need(upgrade._read(upgrade._intent_path(state)) == upgrade._bytes(intent), 'reader_installer_unproved')
    old, info, _route, old_att, _marker = upgrade._old_state(project, state, state / _JOURNAL)
    _need(upgrade._journal_snapshot(info) == intent['journalBefore'] and
          upgrade.build.read(state) == intent['oldRecord'], 'reader_selection_unproved')
    upgrade._backup_proof(intent)
    source, commit = bundle.select_source(root)
    _need(commit == intent['newProof']['sourceCommit'], 'reader_installer_unproved')
    journal._executing_from_candidate(root)
    candidate = upgrade.inspect_full_plugin_candidate(str(root))
    _need(all(candidate.get(k) == v for k,v in intent['newProof'].items()), 'reader_installer_unproved')
    proposed = json.loads(intent['newAttestation'])
    manifest = proposed['providerManifest']
    from laya_embed_runtime import generation_of
    generation = generation_of(manifest.get('modelPath'))
    review = upgrade.review_local_provider(source_root=source, plugin_root=root, state_root=state,
        commit_id=commit, model_path=manifest.get('modelPath'), dimension=candidate['vectorProfile']['dimension'],
        **({'runtime_generation':str(generation)} if generation is not None else {}))
    _need(review['proposalSha256'] == intent['reviewSha256'], 'reader_installer_unproved')
    approved = upgrade.reattest(json.loads(old_att), review['reviewedProvider'])
    _need(approved is not None, 'reader_installer_unproved')
    reproduced = upgrade.attestation_record(proof=candidate, journal_path=state / _JOURNAL,
        reattested=approved, schema=upgrade.PRODUCTION_ATTESTATION_SCHEMA)
    _need(journal._canon(reproduced) == intent['newAttestation'], 'reader_installer_unproved')
    decision = journal.select_route(str(project), action='recall')
    old_proof = journal.verify_selected_route_read_only(decision)
    _need(old_proof.get('readOnlyAllowed') is True and old_proof.get('productRouteAllowed') is False and
          old_proof['projectRoot'] == str(project) and old_proof['journalPath'] == str(state / _JOURNAL),
          'reader_installer_unproved')
    record, raw, _ = _evidence_stores(state, project, decision, old_proof, old, info,
                                       target=target, catalog=catalog, binding=binding, side=side)
    readiness = {**old_proof, 'executingPluginRoot':str(root), 'sourceCommit':commit,
                 'bundleManifestSha256':candidate['bundleManifestSha256']}
    return record, raw, readiness


def _bootstrap_endpoint(state, project, root, options):
    record, raw, proof = _bootstrap_evidence(state, project, root, **options)
    endpoint, proof = _issue_endpoint(state, project, root, options, record, raw, proof)
    _bootstrap_endpoints.add(endpoint)
    _read_only.add(endpoint)
    return endpoint, proof


def _successor_chain(db, endpoint_id, stored, target_sha, target_identity, *, max_epoch):
    """Validate immutable, activation-committed provider successors for one endpoint."""
    _need(db.execute("SELECT 1 FROM sqlite_master WHERE name='upgrade_read_successors'").fetchone(),
          'reader_successor_missing')
    current_sha, identity, last_epoch = stored['attestation_sha256'], None, 0
    previous_item = None

    def restored_predecessor(item, before_epoch):
        # Restore seals are committed before a supported rollback changes the
        # journal. They bind the exact activation, predecessor and epoch.
        for seal_endpoint, packed in db.execute(
                'SELECT endpoint_id,record FROM upgrade_read_seals WHERE endpoint_id=?', (endpoint_id,)):
            seal = json.loads(packed, object_pairs_hook=journal._pairs,
                              parse_constant=journal._nonfinite)
            if type(seal) is not dict or seal.get('schema') != 'memvane-upgrade-restore-v1':
                continue
            source = seal.get('journalBefore')
            if (seal_endpoint == endpoint_id and _raw(seal).decode('utf8') == packed
                    and seal.get('endpointId') == endpoint_id
                    and seal.get('intentSha256') == item['intentSha256']
                    and type(source) is dict and source.get('state') == 'active'
                    and source.get('newWriteSeen') is False
                    and type(source.get('epoch')) is int
                    and item['epoch'] <= source['epoch'] < before_epoch
                    and source.get('selected') == item['toIdentity']
                    and source.get('previous') == item['fromIdentity']
                    and source.get('providerAttestationSha256') == item['toAttestation']
                    and source.get('previousProviderAttestationSha256') == item['fromAttestation']):
                return True
        return False

    rows = db.execute('SELECT epoch,intent_sha256,record FROM upgrade_read_successors '
                      'WHERE endpoint_id=? ORDER BY epoch', (endpoint_id,)).fetchall()
    for epoch, intent_sha, packed in rows:
        item = json.loads(packed, object_pairs_hook=journal._pairs, parse_constant=journal._nonfinite)
        if (type(item) is dict and previous_item is not None
                and (item.get('fromAttestation') != current_sha or item.get('fromIdentity') != identity)):
            _need(item.get('fromAttestation') == previous_item['fromAttestation']
                  and item.get('fromIdentity') == previous_item['fromIdentity']
                  and restored_predecessor(previous_item, epoch), 'reader_successor_invalid')
            current_sha, identity = item['fromAttestation'], item['fromIdentity']
        _need(type(item) is dict and _raw(item).decode('utf8') == packed and
              set(item) == {'schema','intentSha256','endpointId','epoch','fromAttestation',
                            'toAttestation','fromIdentity','toIdentity'} and
              item['schema'] == 'memvane-reader-successor-v1' and item['intentSha256'] == intent_sha and
              item['endpointId'] == endpoint_id and item['epoch'] == epoch and
              type(item['fromIdentity']) is dict and type(item['toIdentity']) is dict and
              type(epoch) is int and last_epoch < epoch <= max_epoch and
              item['fromAttestation'] == current_sha and
              (identity is None or item['fromIdentity'] == identity), 'reader_successor_invalid')
        seal_row = db.execute('SELECT endpoint_id,record FROM upgrade_read_seals WHERE intent_sha256=?',
                              (intent_sha,)).fetchone()
        _need(seal_row is not None and seal_row[0] == endpoint_id, 'reader_successor_invalid')
        from laya_active_upgrade import _reader_seal
        seal = _reader_seal(db, intent_sha, endpoint_id)
        source = (seal.get('recovery',{}).get('journalBefore')
                  if type(seal) is dict and seal.get('schema') == 'memvane-upgrade-reader-v2'
                  else seal.get('journalBefore') if type(seal) is dict else None)
        _need(type(seal) is dict and type(source) is dict and
              type(source.get('epoch')) is int and
              _raw(seal).decode('utf8') == seal_row[1] and
              seal.get('intentSha256') == intent_sha and seal.get('endpointId') == endpoint_id and
              source.get('selected') == item['fromIdentity'] and
              source.get('providerAttestationSha256') == current_sha and
              source.get('epoch') + 2 == epoch and
              item['toIdentity'].get('installId') == stored['install_id'] and
              item['toIdentity'].get('vectorProfile',{}).get('profileId') == stored['profile'] and
              type(item['toAttestation']) is str and journal.SHA.fullmatch(item['toAttestation']),
              'reader_successor_invalid')
        if seal.get('schema') in ('memvane-upgrade-reader-v2','memvane-upgrade-reader-retarget-v1'):
            _need(seal['targetIdentity'] == item['toIdentity'] and
                  seal['targetAttestationSha256'] == item['toAttestation'] and
                  (seal['schema'] != 'memvane-upgrade-reader-v2' or
                   seal['recovery'].get('mode') == 'switch'), 'reader_successor_invalid')
        current_sha, identity, last_epoch = item['toAttestation'], item['toIdentity'], epoch
        previous_item = item
    if rows and (current_sha != target_sha or identity != target_identity):
        # A supported rollback restores the immediately preceding bundle while
        # the immutable receipt for the reverted activation remains present.
        # Validate the complete forward chain above before accepting that exact
        # predecessor and only when the live journal proves the restoration.
        row = journal._row(db)
        selected_restored = (row['state'] in ('restored_old', 'pending')
                             and row['selected'] == row['previous'] == target_identity
                             and row['provider_attestation_sha'] == target_sha)
        activating = (row['state'] == 'active' and row['epoch'] == max_epoch + 1
                      and row['previous'] == target_identity
                      and row['previous_provider_attestation_sha'] == target_sha)
        _need((selected_restored or activating) and row['epoch'] > last_epoch
              and item['fromAttestation'] == target_sha
              and item['fromIdentity'] == target_identity
              and restored_predecessor(item, max_epoch),
              'reader_successor_unproved')
        return
    _need(rows and current_sha == target_sha and identity == target_identity, 'reader_successor_unproved')


def _match_persisted(state, endpoint_id, stored, attestation, current, raw, proof):
    if 'legacy_sidecar_baseline' in stored:
        current['legacy_sidecar_baseline'] = stored['legacy_sidecar_baseline']
    _need(proof['selectionEpoch'] >= stored['creation_epoch'], 'reader_endpoint_changed')
    current['creation_epoch'] = stored['creation_epoch']
    if raw != attestation:
        current['attestation_sha256'] = stored['attestation_sha256']
        with _readonly(Path(state) / _JOURNAL, Path(state)) as db:
            row = journal._row(db)
            _need(row['state'] in ('active','restored_old') and row['pending'] is None and
                  row['provider_attestation_sha'] == _sha(raw), 'reader_successor_unproved')
            _successor_chain(db, endpoint_id, stored, _sha(raw), row['selected'], max_epoch=row['epoch'])
    packed = _raw(current)
    _need(packed == _raw(stored) and _sha(attestation) == stored['attestation_sha256'] and
          endpoint_id == _sha(b'memvane-reader-endpoint-v1\0' + packed), 'reader_endpoint_changed')
    return packed


def _persisted_endpoint(state, project, root, endpoint_id, stored, attestation):
    """Reissue a persisted reader after current full proof, without CAS authority."""
    from laya_reader_lease import _require_reader_scope, _bind_reader_scope
    scope = _require_reader_scope(stored.get('install_id'))
    if stored.get('capability') == 'v6-retained-ro-v1':
        from laya_move_reader_journal import resolve_retained
        return resolve_retained(state, project, root, endpoint_id, stored, attestation)
    _need(isinstance(stored, dict) and type(stored.get('creation_epoch')) is int
          and stored['creation_epoch'] >= 1, 'reader_endpoint_invalid')
    options = dict(target=stored.get('db_path'), catalog=stored.get('catalog_path'),
                   binding=stored.get('binding_path'), side=stored.get('sidecar_path'),
                   allow_drift=True)
    current, raw, proof = _evidence(state, project, root, **options)
    packed = _match_persisted(state, endpoint_id, stored, attestation, current, raw, proof)
    token = object.__new__(VerifiedEndpoint)
    object.__setattr__(token, 'endpoint_id', endpoint_id)
    object.__setattr__(token, 'record', _freeze(current))
    _issued[token] = (Path(state), Path(project), Path(root), options, packed, attestation)
    _read_only.add(token)
    _bind_reader_scope(token, scope)
    return token


def _revoke_reader(endpoint):
    _issued.pop(endpoint, None)
    _read_only.discard(endpoint)
    _retained.discard(endpoint)
    _upgrade_readers.discard(endpoint)
    _bootstrap_endpoints.discard(endpoint)


def _persisted_upgrade_endpoint(state, project, root, endpoint_id, stored, attestation):
    from laya_reader_lease import _require_reader_scope, _bind_reader_scope
    from laya_active_upgrade import _fenced_reader_evidence
    scope = _require_reader_scope(stored.get('install_id'))
    record, raw, _proof = _fenced_reader_evidence(state, project, root, endpoint_id, stored, attestation)
    token = object.__new__(VerifiedEndpoint)
    packed = _raw(record)
    object.__setattr__(token, 'endpoint_id', endpoint_id)
    object.__setattr__(token, 'record', _freeze(record))
    _issued[token] = (Path(state), Path(project), Path(root), {}, packed, raw)
    _read_only.add(token)
    _upgrade_readers.add(token)
    _bind_reader_scope(token, scope)
    return token


def _recheck(endpoint):
    _need(type(endpoint) is VerifiedEndpoint and endpoint in _issued, 'reader_endpoint_not_issued')
    from laya_reader_lease import _check_reader_scope
    _check_reader_scope(endpoint)
    from laya_move_reader_journal import _persisted, recheck_persisted
    if endpoint in _persisted:
        return recheck_persisted(endpoint)
    if endpoint in _retained:
        from laya_move_read_seal import _recheck_retained
        return _recheck_retained(endpoint)
    if endpoint in _upgrade_readers:
        from laya_active_upgrade import _fenced_reader_evidence
        state, project, root, _options, packed, raw = _issued[endpoint]
        return _fenced_reader_evidence(state, project, root, endpoint.endpoint_id,
                                       json.loads(packed), raw)
    if endpoint in _bootstrap_endpoints:
        state, project, root, options, packed, raw = _issued[endpoint]
        record, attestation, proof = _bootstrap_evidence(state, project, root, **options)
        saved = json.loads(packed)
        if 'legacy_sidecar_baseline' in saved:
            record['legacy_sidecar_baseline'] = saved['legacy_sidecar_baseline']
        _need(proof['selectionEpoch'] >= saved['creation_epoch'], 'reader_endpoint_changed')
        record['creation_epoch'] = saved['creation_epoch']
        _need(_raw(record) == packed and attestation == raw and
              endpoint.endpoint_id == _sha(b'memvane-reader-endpoint-v1\0' + packed),
              'reader_endpoint_changed')
        return record, raw, proof
    state, project, root, options, packed, raw = _issued[endpoint]
    record, attestation, proof = _evidence(state, project, root, **options)
    saved=json.loads(packed)
    _need(_match_persisted(state, endpoint.endpoint_id, saved, raw, record, attestation, proof) == packed,
          'reader_endpoint_changed')
    return saved, raw, proof


def seal_current_reader(state: Path, project: Path, *, executing_root: Path) -> InstallerSeal:
    from laya_journal_recovery import recover_initial
    recover_initial(_state(state), Path(project), Path(executing_root))
    endpoint, _proof = _endpoint(state, project, executing_root, {})
    token = object.__new__(InstallerSeal)
    object.__setattr__(token, 'endpoint', endpoint)
    _issued[token] = endpoint
    return token


def seal_upgrade_reader(project: Path, state: Path, *, executing_root: Path) -> InstallerSeal:
    state, project, root = _state(state), Path(project), Path(executing_root)
    from laya_journal_recovery import recover_initial
    recover_initial(state, project, root, bootstrap=True)
    endpoint, _proof = _bootstrap_endpoint(state, project, root, {})
    token = object.__new__(InstallerSeal)
    object.__setattr__(token, 'endpoint', endpoint)
    _issued[token] = endpoint
    _bootstrap_installers.add(token)
    return token


def _installer(token, *, conversion=False):
    _need(type(token) is InstallerSeal and token in _issued and _issued[token] is token.endpoint,
          'reader_installer_not_issued')
    _need(conversion or token not in _bootstrap_installers, 'reader_installer_read_only')
    return token.endpoint


def seal_reader_endpoint(installer: InstallerSeal, *, db_path: Path, catalog_path=None,
                         binding_path=None, sidecar_path=None) -> VerifiedEndpoint:
    endpoint = _installer(installer)
    _recheck(endpoint)
    state, project, root = _issued[endpoint][:3]
    staged, _proof = _endpoint(state, project, root, dict(target=db_path, catalog=catalog_path,
                                                         binding=binding_path, side=sidecar_path))
    return staged

def _sync(path, directory=False):
    flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if directory else 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _lock_dir(install_id, *, create):
    from laya_journal_paths import journal_directory
    _need(type(install_id) is str and journal.ID.fullmatch(install_id), 'reader_install_invalid')
    return journal_directory('reader-locks', install_id, create=create)


@contextmanager
def _transition(install_id, *, create=False):
    path = _lock_dir(install_id, create=create) / 'journal-transition.lock'
    fd = os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW | (os.O_CREAT if create else 0), 0o600)
    try:
        st = os.fstat(fd)
        _need(stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and st.st_nlink == 1
              and stat.S_IMODE(st.st_mode) == 0o600, 'reader_lock_unsafe')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('reader_transition_busy') from exc
        _need(os.path.samestat(st, path.lstat()), 'reader_lock_changed')
        if create:
            _sync(path.parent, True)
        yield fd
    finally:
        os.close(fd)


def _stored_endpoint(db, endpoint_id):
    _need(type(endpoint_id) is str and journal.SHA.fullmatch(endpoint_id), 'reader_endpoint_invalid')
    row = db.execute('SELECT record,attestation FROM read_endpoints WHERE endpoint_id=?',
                     (endpoint_id,)).fetchone()
    _need(row and isinstance(row[0], str) and isinstance(row[1], bytes) and len(row[0]) <= 65536
          and len(row[1]) <= 1024 * 1024, 'reader_endpoint_invalid')
    packed = row[0].encode('utf8')
    record = json.loads(packed)
    _need(isinstance(record, dict) and _raw(record) == packed and
          _sha(b'memvane-reader-endpoint-v1\0' + packed) == endpoint_id
          and record.get('store_format') == 'v6' and record.get('capability') in ('v6-ro-v1','v6-retained-ro-v1')
          and record.get('attestation_sha256') == _sha(row[1]), 'reader_endpoint_invalid')
    if record.get('capability') == 'v6-retained-ro-v1':
        from laya_move_reader_journal import validate_endpoint
        validate_endpoint(db, endpoint_id, record, row[1])
    return record


def _selection(db):
    _need(journal._schema_version(db) == 5, 'reader_schema_unknown')
    rows = db.execute('SELECT epoch,preferred_endpoint,previous_endpoint,transition_id FROM reader_selection').fetchall()
    _need(len(rows) == 1, 'reader_selection_invalid')
    selected = object.__new__(ReaderSelection)
    for name, value in zip(('epoch', 'preferred_endpoint', 'previous_endpoint', 'transition_id'), rows[0]):
        object.__setattr__(selected, name, value)
    last = db.execute('SELECT epoch,preferred_endpoint,previous_endpoint,transition_id '
                      'FROM reader_transitions ORDER BY epoch DESC LIMIT 1').fetchone()
    _need(last == rows[0] and type(selected.epoch) is int and selected.epoch >= 1 and
          (selected.epoch == 1) == (selected.previous_endpoint is None)
          and type(selected.transition_id) is str and journal.ID.fullmatch(selected.transition_id),
          'reader_selection_invalid')
    record = _stored_endpoint(db, selected.preferred_endpoint)
    if selected.previous_endpoint is not None:
        previous = _stored_endpoint(db, selected.previous_endpoint)
        _need(record['install_id'] == previous['install_id'], 'reader_install_changed')
    from laya_move_reader_journal import validate_selection
    validate_selection(db, selected, record)
    return selected, record


def _journal(path):
    path = Path(path)
    _need(path.name == _JOURNAL, 'invalid_journal_path')
    _state(path.parent)
    _file(path, path.parent)
    return path


def read_selection(journal: Path) -> ReaderSelection:
    from laya_install_journal import JournalEvidenceError
    for attempt in range(3):
        path = _journal(journal)
        try:
            with _readonly(path, path.parent) as db:
                return _selection(db)[0]
        except JournalEvidenceError as exc:
            if str(exc) != 'reader_store_changed' or attempt == 2:
                raise


def _insert_endpoint(db, endpoint, record, raw):
    row = (_raw(record).decode('utf8'), raw)
    existing = db.execute('SELECT record,attestation FROM read_endpoints WHERE endpoint_id=?',
                          (endpoint.endpoint_id,)).fetchone()
    if existing is None:
        db.execute('INSERT INTO read_endpoints VALUES(?,?,?)', (endpoint.endpoint_id, *row))
    else:
        _need(existing == row, 'reader_endpoint_conflict')


def convert_reader_v5(state: Path, project: Path, *, installer: InstallerSeal, readiness) -> ReaderSelection:
    import laya_journal_recovery as recovery
    import laya_bridge_readiness as bridge
    import laya_initial_build as build
    import laya_fresh_wiki_origin as fresh
    import project_dir
    from context_learning.resource_coordination import Coordinator
    from laya_index_sync import _owner_lock
    from laya_ledger_common import _operation_lock
    endpoint = _installer(installer, conversion=True)
    state, project = _state(state), Path(project)
    _need(_issued[endpoint][:2] == (state, project), 'reader_install_changed')
    recovery.recover_initial(state, project, _issued[endpoint][2],
                             bootstrap=installer in _bootstrap_installers)
    _record, _att, proof = _recheck(endpoint)
    bridge.require_bridge_preflight(readiness, state, proof)
    path = _journal(state / _JOURNAL)
    with ExitStack() as locks:
        locks.enter_context(_install_lock(state))
        _need(locks.enter_context(build.owner(state)), 'initial_build_busy')
        locks.enter_context(_operation_lock(project_dir.runtime_file(project, fresh.MARKER)))
        # Older capacity workers can publish a sidecar between queue leases.
        # Their existing provider lane covers the whole worker lifetime.
        locks.enter_context(Coordinator().quiesce_provider())
        queue = Path(journal.select_route(str(project), action='recall')['queuePath'])
        _need(locks.enter_context(_owner_lock(queue)), 'sync_owner_busy')
        version = journal.journal_version(path)
        _need(version in (3, 4, 5), 'reader_schema_unknown')
        locks.enter_context(_transition(proof['installId'], create=version != 5))
        _need(journal.journal_version(path) == version, 'reader_selection_changed')
        from laya_reader_lease import _prepare_lifetime
        _prepare_lifetime(proof['installId'], create=version != 5)
        if version != 5:
            # A legacy capacity worker can publish after initial seal issuance.
            # Bind the final sidecar only after the existing owners are quiescent.
            old_record, old_raw, _ = _recheck(endpoint)
            issue = _bootstrap_endpoint if endpoint in _bootstrap_endpoints else _endpoint
            refreshed, _ = issue(state,project,_issued[endpoint][2],_issued[endpoint][3])
            new_record, new_raw, _ = _recheck(refreshed)
            old_common=dict(old_record);new_common=dict(new_record)
            old_common.pop('legacy_sidecar_baseline',None)
            new_common.pop('legacy_sidecar_baseline',None)
            old_epoch=old_common.pop('creation_epoch')
            new_epoch=new_common.pop('creation_epoch')
            _need(new_epoch>=old_epoch and old_common==new_common and old_raw==new_raw,
                  'reader_endpoint_changed')
            if refreshed.endpoint_id != endpoint.endpoint_id:
                endpoint=refreshed
                object.__setattr__(installer,'endpoint',endpoint)
                _issued[installer]=endpoint
        from laya_reader_windows import _prepare_conversion_windows
        _prepare_conversion_windows(installer)
        if journal.journal_version(path) == 3:
            journal.convert_to_v4(path)
        if journal.journal_version(path) == 5:
            selected = read_selection(path)
            _need(selected.preferred_endpoint == endpoint.endpoint_id, 'reader_selection_changed')
            return selected
        record, _, proof = _recheck(endpoint)
        bridge.require_bridge_preflight(readiness, state, proof)
        if os.path.lexists(record['sidecar_path']):
            decision=journal.select_route(str(project),action='recall')
            with _readonly(Path(record['sidecar_path']),state) as db:
                observed=_sidecar_snapshot(db,decision)
            if not observed['baseline']:
                _need(observed['legacyCommitment']==record.get('legacy_sidecar_baseline'),
                      'reader_sidecar_revision_unproved')
            else:
                _need('legacy_sidecar_baseline' not in record,'reader_sidecar_revision_unproved')
        else:
            _need('legacy_sidecar_baseline' not in record,'reader_sidecar_revision_unproved')
        initial = recovery.prepare(state, project, endpoint, record, proof)
        with closing(journal._open(path, write=True)[1]) as db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            try:
                row = journal._row(db)
                _need(row == initial.row, 'reader_selection_changed')
                record, raw, proof = _recheck(endpoint)
                bridge.require_bridge_preflight(readiness, state, proof)
                _need(row['epoch'] == proof['selectionEpoch'] and
                      row['provider_attestation_sha'] == _sha(raw), 'reader_selection_changed')
                _need(row['_version'] == 4, 'reader_schema_unknown')
                journal._step('v5-locked')
                for sql in journal._READER_DDL.values():
                    db.execute(sql)
                _insert_endpoint(db, endpoint, record, raw)
                transition = initial.data['transition']
                db.execute('INSERT INTO reader_transitions VALUES(?,1,?,NULL)',
                           (transition, endpoint.endpoint_id))
                db.execute('INSERT INTO reader_selection VALUES(1,1,?,NULL,?)',
                           (endpoint.endpoint_id, transition))
                journal._step('v5-sealed')
                db.execute('UPDATE selection SET schema=? WHERE id=1', (journal.SCHEMA_V5,))
                db.execute('PRAGMA user_version=5')
                journal._step('v5-versioned')
                selected, _ = _selection(db)
                db.commit()
            except BaseException:
                db.rollback()
                raise
        journal._step('v5-committed')
        _sync(path)
        _sync(state, True)
        recovery.finish(initial)
        return selected


def _discard_staging(lockfd, path):
    """Only unlink a previous writer's private staging inode under this same lock."""
    note = os.pread(lockfd, 4097, 0)
    if not note:
        return
    _need(len(note) <= 4096, 'reader_image_unsafe')
    saved = json.loads(note)
    _need(isinstance(saved, list) and len(saved) == 3 and saved[0] == str(path),
          'reader_image_unsafe')
    if os.path.lexists(path):
        _need(_file(path, path.parent) == saved[1:], 'reader_image_changed')
        for suffix in ('-journal', '-wal', '-shm'):
            side = Path(str(path) + suffix)
            if os.path.lexists(side):
                _need(suffix == '-journal', 'reader_image_unsafe')
                _file(side, path.parent)
                side.unlink()
        path.unlink()
    _sync(path.parent, True)
    os.ftruncate(lockfd, 0)
    os.fsync(lockfd)


def mutate_image(journal_path, operation, *, before_replace=None):
    """Mutate an owned sibling; the named v5 journal is never opened for write."""
    _need(before_replace is None or callable(before_replace), 'reader_publication_check_invalid')
    path = _journal(journal_path)
    with _readonly(path, path.parent) as before:
        _, record = _selection(before)
    with _transition(record['install_id']) as lockfd:
        _need(not os.path.lexists(path.parent / journal._INITIAL_INTENT), 'initial_conversion_unsettled')
        image = path.with_name('.reader-next.sqlite')
        _discard_staging(lockfd, image)
        for suffix in ('', '-journal', '-wal', '-shm'):
            _need(not os.path.lexists(str(image) + suffix), 'reader_image_unsafe')
        current_node = _file(path, path.parent)
        _need(path.stat().st_size <= _MAX_IMAGE, 'reader_image_too_large')
        fd = os.open(image, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            note = _raw([str(image), info.st_dev, info.st_ino])
            _need(os.pwrite(lockfd, note, 0) == len(note), 'reader_image_unowned')
            os.fsync(lockfd)
        finally:
            os.close(fd)
        try:
            with closing(sqlite3.connect(image.as_uri() + '?mode=rw', uri=True,
                                         isolation_level=None, timeout=2)) as db:
                with _readonly(path, path.parent) as source:
                    _, current = _selection(source)
                    _need(current['install_id'] == record['install_id'], 'reader_install_changed')
                    source.backup(db)
                journal._step('v5-image-copied')
                db.execute('PRAGMA synchronous=FULL')
                db.execute('BEGIN IMMEDIATE')
                try:
                    changed = operation(db)
                    if not changed:
                        db.rollback()
                        return
                    _selection(db)
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
                journal._step('v5-image-committed')
            _need(image.stat().st_size <= _MAX_IMAGE and
                  _file(image, path.parent) == [info.st_dev, info.st_ino], 'reader_image_changed')
            _sync(image)
            journal._step('v5-image-fsynced')
            if before_replace is not None:
                before_replace()
            _need(_file(path, path.parent) == current_node, 'reader_journal_changed')
            os.replace(image, path)
            journal._step('v5-image-replaced')
            _sync(path.parent, True)
            journal._step('v5-directory-fsynced')
        finally:
            _discard_staging(lockfd, image)


def cas_reader(journal: Path, *, expected_epoch: int, expected_endpoint: str,
               target: VerifiedEndpoint, transition_id: str) -> ReaderSelection:
    path = _journal(journal)
    _need(not (type(target) is VerifiedEndpoint and
               getattr(target,'record',{}).get('store_format') == 'schema3'),
          'reader_format_not_selectable')
    _need(type(target) is not VerifiedEndpoint or target not in _read_only,
          'reader_endpoint_read_only')
    _need(type(target) is VerifiedEndpoint and target in _issued and
          _issued[target][0] == path.parent, 'reader_endpoint_not_issued')
    _need(type(expected_epoch) is int and expected_epoch >= 1 and type(expected_endpoint) is str
          and re.fullmatch(r'[0-9a-f]{64}', expected_endpoint) and type(transition_id) is str
          and re.fullmatch(r'[0-9a-f]{32}', transition_id),
          'reader_transition_invalid')
    outcome = []

    def select(db):
        current, old = _selection(db)
        _need(old.get('capability') != 'v6-retained-ro-v1', 'reader_move_pin_active')
        record, raw, proof = _recheck(target)
        _need(all(record[k] == old[k] for k in ('scope', 'install_id', 'project', 'state',
              'incarnation', 'project_node', 'state_node', 'attestation_sha256', 'profile',
              'checkout_policy', 'settings', 'roots')), 'reader_install_changed')
        if (current.epoch == expected_epoch + 1 and current.previous_endpoint == expected_endpoint
                and current.transition_id == transition_id and current.preferred_endpoint == target.endpoint_id):
            outcome.append(current)
            return False
        _need(current.epoch == expected_epoch and current.preferred_endpoint == expected_endpoint
              and target.endpoint_id != expected_endpoint, 'stale_reader_cas')
        _need(not db.execute('SELECT 1 FROM reader_transitions WHERE transition_id=?',
                             (transition_id,)).fetchone(), 'reader_transition_reused')
        _insert_endpoint(db, target, record, raw)
        next_epoch = current.epoch + 1
        db.execute('INSERT INTO reader_transitions VALUES(?,?,?,?)',
                   (transition_id, next_epoch, target.endpoint_id, expected_endpoint))
        db.execute('UPDATE reader_selection SET epoch=?,preferred_endpoint=?,previous_endpoint=?,transition_id=? WHERE id=1',
                   (next_epoch, target.endpoint_id, expected_endpoint, transition_id))
        outcome.append(_selection(db)[0])
        return True
    mutate_image(path, select)
    return outcome[0]


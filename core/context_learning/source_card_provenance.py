"""Hook-observed edit provenance for synthetic raw documents and wiki cards.

The episode generator keeps its own ledger. This additional route requires two
consumed PreToolUse/PostToolUse edit tickets, immutable raw/card snapshots, exact
compiler-style sourceRevisions, and active QMD index hashes. It never creates
an episode-generator entry and never authorizes a different novel.

Local hook receipts establish what these hook functions observed; a same-user
caller can invoke hooks with invented host IDs, so they are not host signatures.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .compact_input import derive
from .safe_source import snapshot_beneath
from .store import canonical
from .synthetic_experiment import shared_for, story_for
from .synthetic_scope import (COLLECTION_PATHS, NOVEL_ROOT, WIKI_COLLECTION,
                              generated_path)

METHOD = 'host_wiki_edit_v1'
RAW_COLLECTIONS = {name: base for name, base in COLLECTION_PATHS.items()
                   if base in ('01_Settings', '02_Characters', '03_Plot')}
TICKET_MARKER = 'source_registration'
SHA_RE = re.compile(r'[0-9a-f]{64}\Z')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _collection_path(relative, *, wiki=False):
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ValueError('source_card_path_invalid')
    parts = Path(relative).parts
    if (not parts or any(part in ('', '.', '..') for part in parts)
            or any(part.startswith('.') for part in parts[1:])
            or (parts[0].startswith('.') and
                not (wiki and parts[0] == '.auto-context'))
            or not relative.endswith('.md')):
        raise ValueError('source_card_path_invalid')
    choices = ({WIKI_COLLECTION: '.auto-context/wiki'} if wiki else RAW_COLLECTIONS)
    matches = [(name, base) for name, base in choices.items()
               if relative.startswith(base + '/')]
    if len(matches) != 1:
        raise ValueError('source_card_collection_invalid')
    name, base = matches[0]
    suffix = relative[len(base) + 1:]
    if not suffix or any(part.startswith('.') for part in Path(suffix).parts):
        raise ValueError('source_card_path_invalid')
    return name, suffix


def _story_scope(root, project):
    from .synthetic_scope import _synthetic_bundle, policy_for
    from edit_context import _approved
    root, project = _synthetic_bundle(root, project)
    if project != NOVEL_ROOT.resolve(strict=True):
        raise ValueError('source_card_project_invalid')
    # The source and card are not yet in the collection census. All other
    # marker, owner approval, policy, model, receiver, and config pins still run.
    policy_for(root, project, for_inflight_verification=True)
    story = story_for(project).resolve(strict=True)
    _approved(project, story)
    return root, project, story


def capture_host_edits(project, event):
    """Mint registration tickets from the actual before/after hook event.

    Ordinary edit-recall tickets are separate. A registration ticket can be
    consumed only by register_card(), and an unsupported path is ignored.
    """
    from .edit_ticket import reserve
    if not isinstance(event, dict) or not isinstance(event.get('changes'), list):
        raise ValueError('source_card_edit_event_invalid')
    from edit_context import consume_completed_edit
    consume_completed_edit(project, event)
    recorded = []
    for index, change in enumerate(event['changes']):
        path = change.get('path')
        if change.get('after_sha256') is None:
            continue
        try:
            _collection_path(path, wiki=path.startswith('.auto-context/wiki/'))
        except ValueError:
            continue
        text = change['text']
        if not isinstance(text, str) or not text:
            continue
        start = change['source_start']
        metadata = {'parent_sha256': change['document_sha256'],
                    'path_sha256': _sha(path.encode()),
                    'change_index': index, 'change_count': len(event['changes']),
                    'segment_sha256': _sha(text.encode()),
                    'start': start, 'end': start + len(text),
                    'index': 0, 'count': 1,
                    'session_id': event['edit_session_id'],
                    'turn_id': event['edit_turn_id'],
                    'tool_use_id': event['edit_tool_use_id'],
                    'operation': change['operation'], TICKET_MARKER: True}
        recorded.append(reserve(project, change, event, metadata, text))
    return recorded


def _ticket(db, project, story, relative, raw, prior_revision,
            approval_sha, parent_sha):
    revision = _sha(raw)
    rows = db.execute(
        'SELECT ticket_id,project_root,story_root,relative_path,approval_sha256,'
        'parent_approval_sha256,before_sha256,after_sha256,document_sha256,source_side,'
        'source_start,segment_start,segment_end,segment_sha256,event_json,'
        'session_id,turn_id,tool_use_id,operation,created_at,expires_at,request_id '
        'FROM edit_tickets WHERE relative_path=? AND after_sha256=? '
        'AND request_id IS NULL', (relative, revision)).fetchall()
    valid = []
    for row in rows:
        (ticket_id, project_root, story_root, path, approval, parent,
         before, after, document, side, source_start, start, end, segment_sha,
         event_json, session, turn, tool, operation, created, expires,
         request_id) = row
        try:
            event = json.loads(event_json)
        except (TypeError, ValueError):
            continue
        if (project_root == str(project) and story_root == str(story)
                and path == relative and approval == approval_sha
                and parent == parent_sha and before == prior_revision
                and after == document == revision and side == 'after'
                and operation == ('write' if prior_revision is None else 'edit')
                and isinstance(start, int) and isinstance(end, int)
                and isinstance(source_start, int) and source_start <= start < end
                and (prior_revision is not None or
                     (source_start == start == 0 and
                      end == len(raw.decode('utf8')) and segment_sha == revision))
                and isinstance(segment_sha, str) and SHA_RE.fullmatch(segment_sha)
                and isinstance(session, str) and session
                and isinstance(turn, str) and turn
                and isinstance(tool, str) and tool
                and isinstance(created, (int, float)) and created <= time.time() < expires
                and request_id is None and event.get(TICKET_MARKER) is True
                and event.get('session_id') == session
                and event.get('turn_id') == turn
                and event.get('tool_use_id') == tool
                and event.get('operation') == operation
                and event.get('parent_sha256') == revision
                and event.get('segment_sha256') == segment_sha
                and event.get('start') == start and event.get('end') == end):
            valid.append((ticket_id, _sha(canonical(row[:-1]).encode())))
    if len(valid) != 1:
        raise ValueError('source_card_host_ticket_missing_or_ambiguous')
    return valid[0]


def _card_source_ref(story, source_relative, source_collection, source_raw,
                     card_raw):
    import wiki_card
    import wiki_compile
    try:
        text = card_raw.decode('utf8')
        source_raw.decode('utf8')
        meta, okay = wiki_compile.parse_frontmatter(text)
        import yaml_scalars
        source_entries = []
        for item in meta.get('sources', []):
            if not isinstance(item, str):
                raise ValueError('source_card_sources_invalid')
            fields, issue = yaml_scalars.load_flow_mapping(item)
            if issue:
                raise ValueError('source_card_sources_invalid')
            source_entries.append(fields)
        marker = text.find('\n---', 3)
        refs = wiki_card.frontmatter_source_revisions(text[3:marker]) if marker >= 0 else []
        source_stat = (story / source_relative).stat()
        expected = {'kind': 'file',
                    'path': story.name + '/' + source_relative,
                    'collection': source_collection,
                    'sha256': _sha(source_raw),
                    'size': len(source_raw),
                    'mtimeNs': source_stat.st_mtime_ns}
        source_pointer = {key: expected[key] for key in ('kind', 'path', 'collection')}
        if (not okay or meta.get('status') != 'generated'
                or refs != [expected]
                or source_entries != [source_pointer]
                or not text.startswith('---\n')
                or not meta.get('title')
                or not derive(text, _sha(card_raw))['body_text'].strip()):
            raise ValueError('source_card_provenance_mismatch')
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        raise ValueError('source_card_provenance_mismatch') from exc
    return expected


def _index_ack(project, story, source_collection, source_suffix, source_sha,
               card_suffix, card_sha):
    from .laya_learning import selected as laya_selected
    bound = laya_selected(project)
    if bound is not None:
        return _laya_index_ack(bound, project, story, source_collection, source_suffix,
                               source_sha, card_suffix, card_sha)
    raise ValueError('source_card_index_unselected')


def _laya_index_ack(bound, project, story, source_collection, source_suffix,
                    source_sha, card_suffix, card_sha):
    """Selected Laya route: the card is a sealed head member; the raw source,
    which a v2 route never indexes, is acknowledged by its current bytes."""
    from .laya_learning import file_revision, membership_ack
    from .synthetic_scope import generated_path
    if file_revision(Path(story) / generated_path(
            source_collection + '/' + source_suffix)) != source_sha:
        raise ValueError('source_card_index_unacknowledged')
    try:
        membership_ack(project, [[WIKI_COLLECTION, card_suffix, card_sha]], bound=bound)
    except ValueError as exc:
        raise ValueError('source_card_index_unacknowledged') from exc
    return [[source_collection, source_suffix, source_sha],
            [WIKI_COLLECTION, card_suffix, card_sha]]


def _archived_predecessor(db, root, story, source_relative, old_source_sha,
                          new_source_sha, card_relative, old_card_sha):
    """Allow a full host create after the reconciler archived this exact card."""
    if old_source_sha is None:
        return False
    row = db.execute(
        'SELECT r.archive_relative,r.source_refs FROM synthetic_retired_cards r '
        'JOIN synthetic_source_events e ON e.event_id=r.source_event_id '
        'WHERE r.relative_path=? AND r.sha256=? AND r.state=? '
        'AND e.relative_path=? AND e.operation=? AND e.before_sha256=? '
        'AND e.after_sha256=? AND e.state=?',
        (card_relative, old_card_sha, 'queued', source_relative, 'update',
         old_source_sha, new_source_sha, 'needs_origin')).fetchone()
    if row is None:
        return False
    archive, refs_json = row
    expected_archive = ('source-retired/' + _sha(card_relative.encode())[:24]
                        + '-' + old_card_sha + '.md')
    if archive != expected_archive:
        return False
    refs = json.loads(refs_json)
    if (not isinstance(refs, list) or not any(
            isinstance(ref, dict)
            and ref.get('path') == story.name + '/' + source_relative
            and ref.get('sha256') == old_source_sha for ref in refs)):
        return False
    try:
        return _sha(snapshot_beneath(root, archive)) == old_card_sha
    except (OSError, ValueError):
        return False


def register_card(root, project, source_relative, card_relative,
                  *, method=METHOD):
    """Bind two unspent host tickets after both exact documents are indexed."""
    if method != METHOD:
        raise ValueError('source_card_generation_method_unsupported')
    root, project, story = _story_scope(root, project)
    source_collection, source_suffix = _collection_path(source_relative)
    card_collection, card_suffix = _collection_path(card_relative, wiki=True)
    if card_collection != WIKI_COLLECTION:
        raise ValueError('source_card_collection_invalid')
    source_raw = snapshot_beneath(story, source_relative)
    card_raw = snapshot_beneath(story, card_relative)
    source_sha, card_sha = _sha(source_raw), _sha(card_raw)
    _card_source_ref(story, source_relative, source_collection, source_raw, card_raw)
    ack = _index_ack(project, story, source_collection, source_suffix, source_sha,
                     card_suffix, card_sha)
    from .edit_ticket import _approval
    parent_sha, approval_sha = _approval(project, story)
    from .shared import _db
    with _db(root) as db:
        prior_source = db.execute('SELECT source_sha256 FROM source_card_versions '
                                  'WHERE source_path=? ORDER BY rowid DESC LIMIT 1',
                                  (source_relative,)).fetchone()
        prior_card = db.execute('SELECT card_sha256 FROM source_card_versions '
                                'WHERE card_path=? ORDER BY rowid DESC LIMIT 1',
                                (card_relative,)).fetchone()
        source_ticket, source_ticket_proof = _ticket(
            db, project, story, source_relative, source_raw,
            prior_source[0] if prior_source else None, approval_sha, parent_sha)
        card_prior = prior_card[0] if prior_card else None
        archived_transition = card_prior is not None and _archived_predecessor(
            db, root, story, source_relative,
            prior_source[0] if prior_source else None, source_sha,
            card_relative, card_prior)
        if archived_transition:
            card_prior = None
        card_ticket, card_ticket_proof = _ticket(
            db, project, story, card_relative, card_raw,
            card_prior, approval_sha, parent_sha)
        if source_ticket == card_ticket:
            raise ValueError('source_card_ticket_reused')
        if (db.execute('SELECT 1 FROM source_card_versions WHERE card_path=? '
                       'AND card_sha256=?', (card_relative, card_sha)).fetchone()
                or db.execute('SELECT 1 FROM experiment_generated WHERE relative_path=? '
                              'LIMIT 1', (card_relative,)).fetchone()
                or db.execute('SELECT 1 FROM experiment_generated WHERE relative_path=? '
                              'LIMIT 1', (source_relative,)).fetchone()):
            raise ValueError('source_card_duplicate_or_generated')
        if (snapshot_beneath(story, source_relative) != source_raw
                or snapshot_beneath(story, card_relative) != card_raw):
            raise ValueError('source_card_changed_before_registration')
        _card_source_ref(story, source_relative, source_collection, source_raw, card_raw)
        if archived_transition and not _archived_predecessor(
                db, root, story, source_relative, prior_source[0], source_sha,
                card_relative, prior_card[0]):
            raise ValueError('source_card_archive_changed_before_registration')
        if _index_ack(project, story, source_collection, source_suffix, source_sha,
                      card_suffix, card_sha) != ack:
            raise ValueError('source_card_index_changed_before_registration')
        registration = uuid.uuid4().hex
        db.execute('INSERT INTO source_card_versions '
                   '(registration_id,project_root,story_root,source_path,source_sha256,'
                   'source_bytes,source_ticket_id,source_ticket_sha256,card_path,'
                   'card_sha256,card_bytes,card_ticket_id,card_ticket_sha256,'
                   'generation_method,index_ack,registered_at) '
                   'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (registration, str(project), str(story), source_relative, source_sha,
                    source_raw, source_ticket, source_ticket_proof, card_relative,
                    card_sha, card_raw, card_ticket, card_ticket_proof, method,
                    canonical(ack), time.time()))
        for ticket, role in ((source_ticket, 'raw'), (card_ticket, 'card')):
            bound = db.execute('UPDATE edit_tickets SET request_id=? WHERE ticket_id=? '
                               'AND request_id IS NULL',
                               ('source-card:' + registration + ':' + role, ticket)).rowcount
            if bound != 1:
                raise ValueError('source_card_ticket_reused')
    return {'registration_id': registration, 'source_sha256': source_sha,
            'card_sha256': card_sha, 'index_ack': ack, 'generation_method': method}


def reconcile_pending(root, project, *, card_filter=None):
    """Retry genuine unspent card tickets after index ACK or a later session.

    The edit ticket is the durable queue entry. Registration and both ticket
    claims commit atomically in register_card(); a crash before commit leaves
    the tickets retryable, and a crash afterward leaves no unspent card ticket.
    The bounded 24-hour authority cannot be silently renewed by this worker.
    """
    root, project, story = _story_scope(root, project)
    if card_filter is not None:
        _collection_path(card_filter, wiki=True)
    # A SessionStart hook often has read access to the owner ledger but no
    # permission to create its history writer lock. Do not take that lock to
    # discover that there is no registration work. A positive result still
    # enters the normal locked writer path below; a concurrent new ticket is
    # picked up by the next post-index/session wake.
    from .history_activation import selected_database
    from .shared import DATABASE_NAME
    selected = selected_database(Path(root) / DATABASE_NAME)
    if selected.is_file() and not selected.is_symlink():
        info = selected.stat()
        if info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
            raise ValueError('unsafe_shared_database')
        with closing(sqlite3.connect(selected.as_uri() + '?mode=ro', uri=True,
                                     timeout=.02)) as observer:
            observer.execute('PRAGMA query_only=ON')
            query = ('SELECT 1 FROM edit_tickets WHERE request_id IS NULL '
                     "AND relative_path LIKE '.auto-context/wiki/%' "
                     'AND expires_at>?')
            args = [time.time()]
            if card_filter is not None:
                query += ' AND relative_path=?'
                args.append(card_filter)
            if observer.execute(query + ' LIMIT 1', args).fetchone() is None:
                return {'registered': [], 'deferred': [], 'eligible': 0,
                        'skipped': 0}
    from .shared import _db
    with _db(root) as db:
        query = ('SELECT relative_path,after_sha256,event_json FROM edit_tickets '
                 'WHERE request_id IS NULL AND relative_path LIKE ? '
                 'AND expires_at>?')
        params = ['.auto-context/wiki/%', time.time()]
        if card_filter is not None:
            query += ' AND relative_path=?'
            params.append(card_filter)
        # reserve() caps genuine unspent tickets at 128. Inspect beyond that
        # cap to tolerate legacy rows, but fail closed on a damaged/forged
        # backlog rather than doing unbounded file work in a hook wake.
        rows = db.execute(query + ' ORDER BY created_at,ticket_id LIMIT 257',
                          params).fetchall()
    if len(rows) > 256:
        raise ValueError('source_card_ticket_capacity_exceeded')
    pending = []
    skipped = 0
    for card_relative, card_sha, raw_event in rows:
        try:
            if json.loads(raw_event).get(TICKET_MARKER) is not True:
                continue
            _collection_path(card_relative, wiki=True)
            card_raw = snapshot_beneath(story, card_relative)
            if _sha(card_raw) != card_sha:
                skipped += 1
                continue
            import wiki_compile
            import yaml_scalars
            meta, okay = wiki_compile.parse_frontmatter(card_raw.decode('utf8'))
            if not okay or len(meta.get('sources', [])) != 1:
                skipped += 1
                continue
            fields, issue = yaml_scalars.load_flow_mapping(meta['sources'][0])
            prefix = story.name + '/'
            if (issue or not isinstance(fields, dict)
                    or fields.get('kind') != 'file'
                    or not isinstance(fields.get('path'), str)
                    or not fields['path'].startswith(prefix)):
                skipped += 1
                continue
            source_relative = fields['path'][len(prefix):]
            source_collection, _ = _collection_path(source_relative)
            if fields.get('collection') != source_collection:
                skipped += 1
                continue
            source_raw = snapshot_beneath(story, source_relative)
            _card_source_ref(story, source_relative, source_collection,
                             source_raw, card_raw)
            pending.append((source_relative, card_relative))
        except (OSError, ValueError, TypeError, KeyError, UnicodeError):
            skipped += 1
            continue
    registered = []
    deferred = []
    for source_relative, card_relative in pending:
        try:
            result = register_card(root, project, source_relative, card_relative)
            registered.append(result['registration_id'])
        except (OSError, ValueError, TypeError, KeyError, UnicodeError) as exc:
            deferred.append({'card': card_relative, 'reason': str(exc)[:80]})
    return {'registered': registered, 'deferred': deferred,
            'eligible': len(pending), 'skipped': skipped}


def _latest(db, card_relative):
    return db.execute('SELECT registration_id,project_root,story_root,source_path,'
                      'source_sha256,source_bytes,source_ticket_id,source_ticket_sha256,'
                      'card_path,card_sha256,card_bytes,card_ticket_id,'
                      'card_ticket_sha256,generation_method,index_ack '
                      'FROM source_card_versions WHERE card_path=? '
                      'ORDER BY rowid DESC LIMIT 1', (card_relative,)).fetchone()


def registered_revision(db, root, project, candidate_id, revision,
                        *, require_index=True):
    """Recheck immutable registration, live source bytes, and index ACK."""
    if not isinstance(revision, str) or not SHA_RE.fullmatch(revision):
        return False
    try:
        root = Path(root).resolve(strict=True)
        project = Path(project).resolve(strict=True)
        if root != shared_for(project) or project != NOVEL_ROOT.resolve(strict=True):
            return False
        story = story_for(project).resolve(strict=True)
        card_relative = generated_path(candidate_id).as_posix()
        _, card_suffix = _collection_path(card_relative, wiki=True)
        row = _latest(db, card_relative)
        if row is None:
            from .topical_worker_provenance import registered_revision as worker_revision
            return worker_revision(db, root, project, candidate_id, revision,
                                   require_index=require_index)
        (registration, project_root, story_root, source_relative, source_sha,
         source_raw, source_ticket, source_ticket_proof, card_path, card_sha,
         card_raw, card_ticket, card_ticket_proof, method, ack_json) = row
        source_collection, source_suffix = _collection_path(source_relative)
        if db.execute('SELECT 1 FROM experiment_generated WHERE relative_path IN '
                      '(?,?) LIMIT 1', (source_relative, card_relative)).fetchone():
            return False
        if (project_root != str(project) or story_root != str(story)
                or card_path != card_relative or card_sha != revision
                or method != METHOD or _sha(source_raw) != source_sha
                or _sha(card_raw) != card_sha
                or snapshot_beneath(story, source_relative) != source_raw
                or snapshot_beneath(story, card_relative) != card_raw):
            return False
        _card_source_ref(story, source_relative, source_collection, source_raw, card_raw)
        from .edit_ticket import _approval
        parent_sha, approval_sha = _approval(project, story)
        for ticket, role, path, sha, frozen in (
                (source_ticket, 'raw', source_relative, source_sha, source_ticket_proof),
                (card_ticket, 'card', card_relative, card_sha, card_ticket_proof)):
            proof = db.execute(
                'SELECT ticket_id,project_root,story_root,relative_path,'
                'approval_sha256,parent_approval_sha256,before_sha256,after_sha256,'
                'document_sha256,source_side,source_start,segment_start,'
                'segment_end,segment_sha256,event_json,session_id,turn_id,'
                'tool_use_id,operation,created_at,expires_at,request_id '
                'FROM edit_tickets WHERE ticket_id=?', (ticket,)).fetchone()
            if (proof is None or proof[1:4] != (str(project), str(story), path)
                    or proof[4:6] != (approval_sha, parent_sha)
                    or proof[7] != sha or proof[-1] !=
                    'source-card:' + registration + ':' + role
                    or _sha(canonical(proof[:-1]).encode()) != frozen):
                return False
        expected_ack = [[source_collection, source_suffix, source_sha],
                        [WIKI_COLLECTION, card_suffix, card_sha]]
        if json.loads(ack_json) != expected_ack:
            return False
        if require_index and _index_ack(project, story, source_collection,
                                        source_suffix, source_sha, card_suffix,
                                        card_sha) != expected_ack:
            return False
        return True
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, UnicodeError):
        return False


def latest_paths(db):
    """Current registered raw/card revisions for source and index census."""
    rows = db.execute('SELECT source_path,source_sha256,card_path,card_sha256 '
                      'FROM source_card_versions ORDER BY rowid').fetchall()
    latest = {}
    for source, source_sha, card, card_sha in rows:
        if db.execute('SELECT 1 FROM experiment_generated WHERE relative_path IN '
                      '(?,?) LIMIT 1', (source, card)).fetchone():
            raise ValueError('source_card_generator_path_collision')
        latest[source] = ('host_source', source_sha)
        latest[card] = ('host_wiki_card', card_sha)
    from .topical_worker_provenance import latest_paths as worker_paths
    for path, proof in worker_paths(db).items():
        previous = latest.get(path)
        if previous is not None and previous[1] != proof[1]:
            raise ValueError('source_card_worker_path_collision')
        if previous is None:
            latest[path] = proof
    return latest

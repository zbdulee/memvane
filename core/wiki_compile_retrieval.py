"""Compile-time retrieval, including cards not yet in the published generation.

The installer-bound index supplies bounded lexical/vector RRF candidates. A
private, disposable FTS overlay fills publication lag: each call inventories
metadata, but only new/changed card bodies are indexed. Cold bootstrap has a
per-call read budget, not a corpus-size limit. Half the budget follows newest
changes and half a persistent path cursor, so older cold cards cannot starve.
Neither index is a source of model-visible bytes; candidates are read live
through no-follow directory descriptors before returning their snippets.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import sys
import time

import compile_paths as cp
import source_text
import wiki_card
from laya_source_exclusion import card_sources_allowed

REFRESH_FILES = 512
REFRESH_BYTES = 8 * 1024 * 1024
MAX_CARD_BYTES = 1024 * 1024
CANDIDATES = 20
RETRIEVAL_FLOOR = 0.3  # eligibility for a judge, never a duplicate verdict
# Compile-only relevance heuristic for hits without meaningful word overlap.
# This is neither a duplicate probability nor a calibrated semantic guarantee.
VECTOR_RELEVANCE_MIN_COSINE = 0.55
QUERY_SECONDS = 10.0
# The local embedder has a 2,048-token context, including its query prefix.
# UTF-8 bytes conservatively bound even byte-fallback Korean/emoji tokens.
# Lexical retrieval and evidence scoring still use the complete source query.
QUERY_VECTOR_BYTES = 1500
OVERLAY_SCHEMA = 1  # disposable porter unicode61 FTS, independent of route identity
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def visible(rel: str) -> bool:
    path = PurePosixPath(rel)
    return (bool(path.parts) and not path.is_absolute() and path.suffix == '.md'
            and all(part not in ('', '.', '..') and not part.startswith('.')
                    for part in rel.split('/'))
            and not source_text.is_wiki_meta_page(path))


def fingerprint(info) -> str:
    return json.dumps([info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                       info.st_ctime_ns], separators=(',', ':'))


@contextmanager
def opened_directory(path: Path):
    """Open every absolute component without following a symlink."""
    if not path.is_absolute():
        raise ValueError('selected_wiki_source_unsafe')
    fd = os.open('/', _DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, _DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def inventory(wiki_fd: int, deadline: float, cursor=''):
    """Metadata only; hidden and symlink directories are never traversed."""
    found = {}
    complete = True
    last = cursor
    seeking = bool(cursor)

    def visit(fd, prefix):
        nonlocal complete, last, seeking
        with os.scandir(fd) as entries:
            for entry in entries:
                if time.monotonic() >= deadline:
                    complete = False
                    return
                if entry.name.startswith('.'):
                    continue
                rel = prefix + entry.name
                # Resume a partial inventory in scandir order. Skipped names
                # need no file stat/read, so a pass advances beyond its old
                # expensive metadata window instead of sorting it again.
                if seeking and not entry.is_dir(follow_symlinks=False):
                    if rel == cursor:
                        seeking = False
                    continue
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(entry.name, _DIRECTORY, dir_fd=fd)
                    try:
                        visit(child, rel + '/')
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode) and visible(rel):
                    found[rel] = fingerprint(info)
                    last = rel
    visit(wiki_fd, '')
    # A removed cursor starts a fresh cycle next time; never infer deletion
    # from a traversal that did not establish where the prior pass ended.
    return found, complete and not seeking, '' if complete else last


def read_live(wiki_fd: int, rel: str):
    """Stable bounded read; symlink ancestors and mutation races reject a hit."""
    if not visible(rel):
        return None
    fd = os.dup(wiki_fd)
    try:
        parts = rel.split('/')
        for part in parts[:-1]:
            child = os.open(part, _DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        card = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                       dir_fd=fd)
        try:
            before = os.fstat(card)
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_CARD_BYTES:
                return None
            with os.fdopen(os.dup(card), 'rb') as stream:
                data = stream.read(MAX_CARD_BYTES + 1)
            if (len(data) > MAX_CARD_BYTES or fingerprint(before) !=
                    fingerprint(os.fstat(card)) or fingerprint(before) !=
                    fingerprint(os.stat(parts[-1], dir_fd=fd, follow_symlinks=False))):
                return None
            # The parent may have been renamed while the descriptor was held.
            # Walk the current path again; an old detached directory is not live.
            current = os.dup(wiki_fd)
            try:
                for part in parts[:-1]:
                    child = os.open(part, _DIRECTORY, dir_fd=current)
                    os.close(current)
                    current = child
                if fingerprint(os.stat(parts[-1], dir_fd=current,
                                       follow_symlinks=False)) != fingerprint(before):
                    return None
            finally:
                os.close(current)
            return data.decode('utf-8'), hashlib.sha256(data).hexdigest(), fingerprint(before)
        finally:
            os.close(card)
    except (OSError, UnicodeError):
        return None
    finally:
        os.close(fd)


def meaningful(text: str) -> str:
    text = wiki_card.normalize_newlines(text)
    end = text.find('\n---\n', 4) if text.startswith('---\n') else -1
    return wiki_card.extract_card_body(text, end + 5 if end >= 0 else 0)[0]


def terms(text: str) -> set[str]:
    return set(re.findall(r'\w+', text.casefold()))


def lexical_atoms(text: str) -> list[str]:
    """Bounded query atoms shared by overlay retrieval and live-body evidence."""
    from keywords import EN_STOPWORDS, KO_STOPWORDS, strip_ko_suffix
    from laya_host_response import _fts_atom
    words = list(dict.fromkeys(strip_ko_suffix(word) for word in
                               re.findall(r'\w+', text.casefold())))
    words = [word for word in words if 2 <= len(word) <= 128
             and word not in EN_STOPWORDS and word not in KO_STOPWORDS]
    # Keep the index's early terms and spread the rest across a long source,
    # including its tail, rather than using only alphabetically early terms.
    if len(words) > 24:
        words = list(dict.fromkeys(words[:6] +
                     [words[i * (len(words) - 1) // 17] for i in range(18)]))
    return [atom for word in words if (atom := _fts_atom(word))]


@contextmanager
def lexical_probe(text: str, deadline: float):
    """Use the index tokenizer on meaningful current bytes, never metadata.

    One in-memory connection is reused for at most forty candidate bodies.
    The exact-overlap fast path needs no SQL; this probe preserves Korean
    suffix normalization and English Porter/prefix hits when vectors are cold.
    """
    from laya_index_a import IndexContractError, _qmd_fts_text
    db = sqlite3.connect(':memory:')
    try:
        db.execute("CREATE VIRTUAL TABLE probe USING fts5(body, tokenize='porter unicode61')")
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 500)
        query = ' OR '.join(lexical_atoms(text))

        def supported(body):
            if not query:
                return False
            if time.monotonic() >= deadline:
                return None
            try:
                db.execute('DELETE FROM probe')
                db.execute('INSERT INTO probe VALUES (?)',
                           (_qmd_fts_text(body, deadline=deadline),))
                return db.execute('SELECT 1 FROM probe WHERE probe MATCH ? LIMIT 1',
                                  (query,)).fetchone() is not None
            except sqlite3.OperationalError as exc:
                if str(exc) != 'interrupted':
                    raise
                return None
            except IndexContractError as exc:
                if str(exc) != 'fts_deadline_exceeded':
                    raise
                return None
        yield supported
    finally:
        db.close()


@contextmanager
def overlay(root: Path, bound):
    """Private SQLite transaction; failed refreshes never publish partial FTS."""
    import wiki_selected_laya
    wiki_selected_laya.recheck(bound)
    base = cp.compile_dir(root, create=True)
    if base is None:
        raise ValueError('selected_wiki_overlay_unsafe')
    private = base / 'wiki-retrieval'
    with opened_directory(base) as parent:
        try:
            os.mkdir(private.name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        fd = os.open(private.name, _DIRECTORY, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('selected_wiki_overlay_unsafe')
            for name in ('cards.sqlite', 'cards.sqlite-journal', 'cards.sqlite-wal',
                         'cards.sqlite-shm'):
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_nlink != 1 or info.st_mode & 0o077):
                    raise ValueError('selected_wiki_overlay_unsafe')
            created = os.open('cards.sqlite', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                              0o600, dir_fd=fd)
            os.close(created)
            db = sqlite3.connect(str(private / 'cards.sqlite'), timeout=2)
            try:
                db.execute('PRAGMA journal_mode=DELETE')
                db.execute('PRAGMA synchronous=FULL')
                db.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)')
                db.execute('CREATE TABLE IF NOT EXISTS cards (path TEXT PRIMARY KEY, stamp TEXT, sha TEXT)')
                db.execute('CREATE TABLE IF NOT EXISTS seen (path TEXT PRIMARY KEY)')
                db.commit()
                db.execute('BEGIN IMMEDIATE')
                version = db.execute('PRAGMA user_version').fetchone()[0]
                if version == 0:
                    # Only disposable retrieval data changes. DDL and cached
                    # stamps reset in this transaction so a crash cannot leave
                    # an empty new FTS paired with apparently indexed cards.
                    db.execute('DROP TABLE IF EXISTS search')
                    db.execute("CREATE VIRTUAL TABLE search USING fts5(path UNINDEXED, body, tokenize='porter unicode61')")
                    for table in ('cards', 'seen', 'state'):
                        db.execute('DELETE FROM ' + table)
                    db.execute(f'PRAGMA user_version={OVERLAY_SCHEMA}')
                elif version != OVERLAY_SCHEMA:
                    raise ValueError('selected_wiki_overlay_schema_unsupported')
                yield db
                wiki_selected_laya.recheck(bound)
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()
        finally:
            os.close(fd)


def refresh(db, wiki_fd, bound, wiki, deadline):
    decision = bound[0]
    identity = json.dumps([decision.get(key) for key in
                          ('installId', 'scopeId', 'dbIncarnation', 'generation')]
                         + [str(wiki),
                            os.fstat(wiki_fd).st_dev, os.fstat(wiki_fd).st_ino,
                            decision['recallSettings']['skipPaths']], sort_keys=True)
    previous = db.execute("SELECT value FROM state WHERE key='identity'").fetchone()
    if previous is None or previous[0] != identity:
        db.execute('DELETE FROM cards')
        db.execute('DELETE FROM search')
        db.execute('DELETE FROM seen')
        db.execute('DELETE FROM state')
        db.execute("INSERT INTO state VALUES ('identity', ?)", (identity,))
    cursor = db.execute("SELECT value FROM state WHERE key='scan_cursor'").fetchone()
    cursor = cursor[0] if cursor else ''
    if not cursor:
        db.execute('DELETE FROM seen')
    # Metadata and body refresh have separate windows. A partial inventory
    # must leave time to index the bodies it found before advancing its cursor;
    # otherwise cold/unpublished cards can be skipped on every scan cycle.
    metadata_deadline = min(deadline - 1.0, time.monotonic() + 1.0)
    files, complete, last = inventory(wiki_fd, metadata_deadline, cursor)
    db.executemany('INSERT OR IGNORE INTO seen VALUES (?)', ((rel,) for rel in files))
    db.execute("INSERT OR REPLACE INTO state VALUES ('scan_cursor', ?)",
               ('' if complete else last,))
    stored = dict(db.execute('SELECT path, stamp FROM cards'))
    seen = set(row[0] for row in db.execute('SELECT path FROM seen')) if complete else set()
    for rel in (stored.keys() - seen) if complete else ():
        db.execute('DELETE FROM cards WHERE path=?', (rel,))
        db.execute('DELETE FROM search WHERE path=?', (rel,))
    changed = [rel for rel, stamp in files.items() if stored.get(rel) != stamp]
    newest = sorted(changed, key=lambda rel: (-json.loads(files[rel])[3], rel))
    recent = newest[:REFRESH_FILES // 2]
    cursor = db.execute("SELECT value FROM state WHERE key='cursor'").fetchone()
    cursor = cursor[0] if cursor else ''
    fair = sorted(set(changed) - set(recent))
    fair = [rel for rel in fair if rel > cursor] + [rel for rel in fair if rel <= cursor]
    fair = fair[:REFRESH_FILES - len(recent)]
    # Interleave both cohorts so neither the byte nor the time budget can be
    # exhausted by recent writes before older cold cards get a turn.
    chosen = []
    for i in range(max(len(recent), len(fair))):
        if i < len(recent):
            chosen.append(recent[i])
        if i < len(fair):
            chosen.append(fair[i])
    total = 0
    refreshed = 0
    oversize = 0
    for rel in chosen:
        if time.monotonic() >= deadline:
            break
        size = json.loads(files[rel])[2]
        if size <= MAX_CARD_BYTES and total + size > REFRESH_BYTES:
            continue
        total += size if size <= MAX_CARD_BYTES else 0
        oversize += size > MAX_CARD_BYTES
        live = read_live(wiki_fd, rel)
        # Invalid/oversize/excluded files are metadata-cached too: do not keep
        # rereading them each query. A policy or file change invalidates that.
        db.execute('DELETE FROM search WHERE path=?', (rel,))
        db.execute('INSERT OR REPLACE INTO cards VALUES (?, ?, ?)',
                   (rel, files[rel], live[1] if live else ''))
        if live and live[2] == files[rel] and card_sources_allowed(
                live[0], decision['recallSettings']['skipPaths']):
            from laya_index_a import _qmd_fts_text
            db.execute('INSERT INTO search VALUES (?, ?)',
                       (rel, _qmd_fts_text(meaningful(live[0]))))
        refreshed += 1
        if rel in fair:
            db.execute("INSERT OR REPLACE INTO state VALUES ('cursor', ?)", (rel,))
    remaining = len(changed) - refreshed
    status = {'state': 'deferred' if remaining or not complete else 'ready',
              'remaining': remaining, 'inventoryComplete': complete,
              'refreshed': refreshed, 'oversize': oversize}
    return status


def overlay_paths(db, text, deadline, exclusions, name):
    atoms = lexical_atoms(text)
    if not atoms:
        return []
    db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 500)
    db.create_function('compile_allowed', 2,
                       lambda path, sha: int(name + '/' + path not in exclusions[0]
                                             and sha not in exclusions[1]))
    try:
        return list(db.execute('SELECT search.path, cards.sha FROM search JOIN cards '
                               'ON cards.path=search.path WHERE search MATCH ? '
                               'AND compile_allowed(search.path, cards.sha) '
                               'ORDER BY bm25(search), search.path LIMIT ?',
                               (' OR '.join(atoms), CANDIDATES)))
    except sqlite3.OperationalError as exc:
        if str(exc) != 'interrupted':
            raise
        return None
    finally:
        db.set_progress_handler(None, 0)


def query_provider(bound):
    """Only ask an already warm reviewed provider; no model is started here."""
    import laya_host_response as response
    from laya_index_a import IndexContractError
    from laya_local_embed_provider import ReviewedLocalQmdProvider
    from laya_embed_resident import admit_query
    provider = ReviewedLocalQmdProvider(bound[1]['providerManifest'])
    try:
        admit_query()
    except Exception:
        # As in host recall, refused query admission means lexical only.
        def unavailable(*_args):
            raise response._VectorUnavailable('compile_provider_unavailable')
        return unavailable

    def query(text, profile):
        encoded = text.encode('utf-8')
        vector_text = (encoded[:QUERY_VECTOR_BYTES].decode('utf-8', errors='ignore')
                       if len(encoded) > QUERY_VECTOR_BYTES else text)
        try:
            return provider(vector_text, profile)
        except IndexContractError as exc:
            if str(exc) != 'provider_unavailable':
                raise
            raise response._VectorUnavailable('provider_unavailable') from exc
    return query


@contextmanager
def selected_search(bound, text):
    """Open only the capacity path; never fall back to a whole-root scan."""
    from laya_index_runtime import LayaIndexRuntime, capacity_store_paths
    from laya_index_capacity import open_pinned_candidate_query
    from laya_index_host_route import wiki_index_roots
    from laya_source_catalog import load_exclusions
    import laya_host_response as response
    decision, proof = bound
    capacity = capacity_store_paths(decision['dbPath'])
    roots = wiki_index_roots(decision['collections'])
    names = tuple(sorted(roots))
    stores = ({'catalog_path': capacity[0], 'binding_store_path': capacity[1]}
              if capacity is not None else {})
    with LayaIndexRuntime(source_root=roots, scope=decision['scopeId'],
                          db_path=decision['dbPath'], queue_path=decision['queuePath'],
                          db_incarnation=decision['dbIncarnation'], wiki_only=True,
                          **stores) as runtime:
        token, _generation = runtime.index.pin(runtime.scope)
        candidates = None
        try:
            exclusions = load_exclusions(runtime.index, runtime.scope)
            rows = []
            diagnostics = {}
            if capacity is not None:
                provider = query_provider(bound)
                response._provider_ready(text, decision['vectorProfile'], proof['vectorProfile'], provider)
                candidates = open_pinned_candidate_query(runtime.index, token, runtime.binding_store)

                def eligible(row):
                    name, sep, rel = row['path'].partition('/')
                    return (bool(sep) and name in names and visible(rel)
                            and card_sources_allowed(row['body'], decision['recallSettings']['skipPaths']))
                rows, _body_evidence = response._v2_phase(
                    runtime, token, text, decision['vectorProfile'], provider, roots, names,
                    tuple(decision['recallSettings'].get('lexicalPatterns', ())), 0, (),
                    candidates, eligible, diagnostics=diagnostics)
            yield ([(row['path'].split('/', 1)[1], row['hash'],
                     diagnostics.get('vectorCosines', {}).get((row['path'], row['hash'])))
                    for row in rows],
                   exclusions, names[0], 'bound' if capacity else 'overlay_only')
            if load_exclusions(runtime.index, runtime.scope) != exclusions:
                raise ValueError('selected_wiki_exclusions_changed')
            if candidates is not None:
                candidates.require_returned_verified()
                candidates.finish()
        finally:
            if candidates is not None:
                candidates.close()
            runtime.index.release(token)


def matches(root, wiki, text, top_k, cap_chars, bound):
    if not isinstance(text, str) or not text.strip() or len(text) > 100_000:
        return []
    query_terms = terms(text)
    if not query_terms:
        return []
    deadline = time.monotonic() + QUERY_SECONDS
    from keywords import EN_STOPWORDS, KO_STOPWORDS
    with selected_search(bound, text) as (published, exclusions, name, mode), \
            opened_directory(wiki) as wiki_fd, lexical_probe(text, deadline) as supported:
        with overlay(root, bound) as db:
            # Leave time to serve known indexed/overlay candidates even when
            # a very large inventory or cold refresh has more work remaining.
            refresh_deadline = min(deadline - 1.0, time.monotonic() + 3.0)
            status = refresh(db, wiki_fd, bound, wiki, refresh_deadline)
            local = (overlay_paths(db, text, deadline, exclusions, name)
                     if time.monotonic() < deadline else None)
            if local is None:
                status['state'] = 'deferred'
                status['deadlineReached'] = True
            status['indexedCandidates'] = len(published or ())
            status['index'] = mode
        # RRF orders candidates. It is deliberately never returned as confidence:
        # the top hit's rank says nothing about whether two cards are duplicates.
        ranks = {}
        evidence = {}
        vector_evidence = {}
        for rows in (published, local or ()):
            for rank, candidate in enumerate(rows, 1):
                rel, sha, *cosine = candidate
                ranks[rel] = ranks.get(rel, 0) + 1 / (60 + rank)
                evidence.setdefault(rel, set()).add(sha)
                if cosine and cosine[0] is not None:
                    vector_evidence[(rel, sha)] = cosine[0]
        result = []
        for rel in sorted(ranks, key=lambda rel: (-ranks[rel], rel))[:2 * CANDIDATES]:
            if time.monotonic() >= deadline:
                status['state'] = 'deferred'
                status['deadlineReached'] = True
                break
            if name + '/' + rel in exclusions[0]:
                continue
            live = read_live(wiki_fd, rel)
            if (live is None or live[1] in exclusions[1] or live[1] not in evidence[rel]
                    or not card_sources_allowed(live[0], bound[0]['recallSettings']['skipPaths'])):
                continue
            body = meaningful(live[0])
            shared = query_terms & terms(body)
            # A long source can legitimately overlap one small existing card:
            # word coverage is a score, not a requirement to fill a quota.
            lexical_support = any(len(term) >= 2 and term not in EN_STOPWORDS
                                  and term not in KO_STOPWORDS for term in shared)
            if not lexical_support:
                lexical_support = supported(body)
                if lexical_support is None:
                    status['state'] = 'deferred'
                    status['deadlineReached'] = True
                    break
            if (not lexical_support and vector_evidence.get((rel, live[1]), -1.0)
                    < VECTOR_RELEVANCE_MIN_COSINE):
                continue
            coverage = len(shared) / len(query_terms)
            result.append((wiki / rel, max(coverage, RETRIEVAL_FLOOR), live[0][:cap_chars]))
            if len(result) == top_k:
                break
        with overlay(root, bound) as db:
            db.execute("INSERT OR REPLACE INTO state VALUES ('status', ?)",
                       (json.dumps(status, sort_keys=True),))
        if status['state'] == 'deferred' or mode == 'overlay_only' or status['oversize']:
            print(json.dumps({'wikiCompileRetrieval': status}, sort_keys=True), file=sys.stderr)
        with opened_directory(wiki) as current:
            if ((os.fstat(wiki_fd).st_dev, os.fstat(wiki_fd).st_ino) !=
                    (os.fstat(current).st_dev, os.fstat(current).st_ino)):
                raise ValueError('selected_wiki_root_changed')
        return result

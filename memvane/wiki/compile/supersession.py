"""Cards a newer extraction of their one source no longer produces.

A machine card cites one source revision (``sourceRevisions``). When the source
changes, one extraction of the new bytes is the compiler's account of it: the
cards it returns are rewritten, and a card it no longer returns describes bytes
that no longer exist. ``citation`` sorts the cards that cite an older revision:

- ``SUPERSEDED``: may be retired. The plugin owns its bytes
  (``auto_writable_text``), an automatic source trigger made it (``triggers``
  within ``AUTO_SOURCE_TRIGGERS``), it cites exactly this one source, it is
  ``verified`` with a ``verifiedBodyHash`` that matches its body, and it holds no
  text outside the managed block other than the disclaimer and HTML comments.
- ``REFRESHABLE``: the same, but without a matching body digest (a ``generated``
  card, or a verified card older than the digest). An edit inside its managed
  block cannot be told from the compiler's own text, so it is refreshed when the
  answer returns it and is never retired.
- ``PROTECTED``: everything else (hand-written text, a foreign ``createdBy``, a
  manual trigger, several sources, unreadable provenance). It neither asks for
  an extraction nor retires.

The compile worker uses the module in three places.

1. Before a paid call (``wiki_compile_worker._reuse_source_revision``), a
   verified card proving the current revision skips the source unless ``scan``
   finds a ``SUPERSEDED`` or ``REFRESHABLE`` card and no attempt is recorded
   for these exact source bytes (``recorded``).
2. After the extraction, ``record_after`` writes the attempt's outcome for
   ``(source path, source sha256)`` in the ledger's ``suppressions`` table
   (kind ``supersession``). Outcomes: ``pending`` (retirement waits for the
   rewritten cards' review), ``settled`` (nothing to retire), ``incomplete``
   (an empty, capped or partly unknown answer), ``held_truncated`` (the
   extractor saw a cut source, so a missing card is no evidence),
   ``held_ratio`` (more than ``RATIO_LIMIT`` of the superseded cards would go
   and at least ``RATIO_MIN`` of them), ``failed`` (the extractor's answer was
   unusable for good). ``record_failure`` writes ``failed``.
3. ``settle`` runs after each verify pass. A ``pending`` attempt retires its
   superseded cards only once every card the answer rewrote is ``verified`` at
   the new revision; a rewritten card whose review ended otherwise ends the
   attempt as ``held_review_failed`` and retires nothing. Retirement uses the
   lifecycle writer (``wiki_source_retire.retire_card``: ``retired-wiki/``
   archive, unlink under the card and wiki locks, ``retired`` activity event,
   reason ``superseded_by_revision``), at most ``MAX_RETIRE_PER_RUN`` cards per
   pass; the rest waits for the next pass. A finished attempt becomes
   ``retired``.

A recorded attempt counts only for its source bytes: a new edit (new sha256)
is attempted again. The judgement of a card reads the card bytes only.
"""
from __future__ import annotations

from contextvars import ContextVar
import itertools
from pathlib import Path
import sqlite3

from memvane.settings import api as settings_api
from memvane.wiki.card import api as card_api
from memvane.wiki.card.types import (AUTO_BLOCK_RE, AUTO_DISCLAIMER_PREFIXES, COMMENT_CLOSE, COMMENT_OPEN,
                                     FRONTMATTER_RE)

RETIRE_REASON = 'superseded_by_revision'
RECORD_KIND = 'supersession'
AUTO_SOURCE_TRIGGERS = frozenset(('post_tool_source', 'post_sync_source', 'session_start_source'))
CURRENT = 'current'
SUPERSEDED = 'superseded'
REFRESHABLE = 'refreshable'
PROTECTED = 'protected'
# Ratio guard of one source: an answer that drops more than half of the cards it
# could retire, and at least three, is held whole. A complete answer returns up
# to ``cardsPerSource`` (10) cards; one or two cards of a short answer are
# ordinary topic merges, while dropping most of a large source is the signature
# of a bad or narrow answer, and retirement cannot be undone without an operator.
RATIO_LIMIT = 0.5
RATIO_MIN = 3
# Cards retired per settle pass; the wiki lifecycle's MAX_RETIRE_PER_RUN.
MAX_RETIRE_PER_RUN = 25
# Pending attempts looked at per settle pass.
MAX_SETTLE_ATTEMPTS = 64
_EXPECTED = (OSError, ValueError, RuntimeError, sqlite3.Error)
# The (path, sha256) of the source this job extracts because of superseded cards.
_ATTEMPT: ContextVar = ContextVar('supersession_attempt', default=None)


def _strip_comments(text: str) -> str:
    """Drop complete ``<!-- ... -->`` comments in linear time; an unclosed one stays."""
    out = []
    pos = 0
    while True:
        start = text.find(COMMENT_OPEN, pos)
        end = -1 if start < 0 else text.find(COMMENT_CLOSE, start + len(COMMENT_OPEN))
        if end < 0:
            out.append(text[pos:])
            return ''.join(out)
        out.append(text[pos:start])
        pos = end + len(COMMENT_CLOSE)


def _hand_written(body: str) -> bool:
    """True when the card body holds text outside its single managed block."""
    blocks = list(AUTO_BLOCK_RE.finditer(body))
    if len(blocks) != 1:
        return True
    outside = body[:blocks[0].start()] + '\n' + body[blocks[0].end():]
    lines = [line for line in outside.split('\n') if not line.lstrip().startswith(AUTO_DISCLAIMER_PREFIXES)]
    return bool(_strip_comments('\n'.join(lines)).strip())


def _verified_body(meta: dict, text: str) -> bool | None:
    """True: verified with a matching body digest; False: a digest that no longer matches; None: no digest."""
    digest = meta.get('verifiedBodyHash')
    if digest in (None, '', []):
        return None
    return digest == card_api.card_body_hash(text) and meta.get('status') == 'verified'


def citation(text: str, same_source, current: dict) -> str | None:
    """How one card (LF text) cites the source whose bytes are now ``current``.

    ``same_source(revision)`` says whether a normalized revision names the
    source. None: the card does not cite it.
    """
    match = FRONTMATTER_RE.match(text)
    if match is None:
        return None
    block = match.group(1)
    normalized = [card_api.normalize_source_revision(item)
                  for item in card_api.frontmatter_source_revisions(block)]
    cites = [item for item in normalized if item is not None and same_source(item)]
    if not cites:
        unreadable = any(item is None for item in normalized)
        return PROTECTED if unreadable and str(current.get('path')) in block else None
    if any(item['sha256'] == current.get('sha256') and item['size'] == current.get('size') for item in cites):
        return CURRENT
    if len(normalized) != 1 or not card_api.auto_writable_text(text)[0]:
        return PROTECTED
    meta, ok = card_api.parse_frontmatter(text)
    triggers = meta.get('triggers') if ok else None
    if (not isinstance(triggers, list) or not triggers
            or any(trigger not in AUTO_SOURCE_TRIGGERS for trigger in triggers)
            or _hand_written(text[match.end():])):
        return PROTECTED
    verified = _verified_body(meta, text)
    if verified is False:
        return PROTECTED
    return SUPERSEDED if verified else REFRESHABLE


def _current_verified(text: str, same_source, current: dict) -> bool:
    """A card at the source's current bytes that passed review (verified, matching digest)."""
    if citation(text, same_source, current) != CURRENT:
        return False
    meta, ok = card_api.parse_frontmatter(text)
    return ok and _verified_body(meta, text) is True


def _diagnostic_row(action: str, job: dict, reason: str) -> dict:
    """A compile log row that leaves the job's settling reason alone."""
    from memvane.wiki.compile import wiki_compile_worker as worker
    previous = worker._LAST_REASON.get()
    row = worker.bounded_failure(action, job, reason)
    worker._LAST_REASON.set(previous)
    return row


def _source_judge(decision: dict, revision: dict):
    label = settings_api.decision_resolver(decision)
    wanted = label(revision.get('collection'), revision.get('path'))

    def same_source(item):
        # A renamed raw collection's old label resolves by path (recall rule).
        return item.get('path') == revision.get('path') and label(item.get('collection'), item.get('path')) == wanted
    return same_source


def scan(root, bound, source_revision: dict):
    """``{'superseded', 'refreshable', 'wikiRoot'}`` for the source's current bytes, else None.

    ``superseded`` lists ``(card, text)`` of every ``SUPERSEDED`` card and
    ``refreshable`` counts the ``REFRESHABLE`` ones. The bounds and failures
    are those of the worker's ``selected_compiled_revision_card``: any unsafe,
    oversized or unreadable card returns None, which the worker reads as "an
    old card may exist" before skipping a paid call and as "retire nothing"
    after one.
    """
    from memvane.wiki.compile import wiki_compile_worker as worker
    if bound is None:
        return None
    try:
        decision = bound[0]
        wiki_root = Path(next(item['sourceRoot'] for item in decision['collections'] if item['role'] == 'wiki'))
        if wiki_root.is_symlink() or wiki_root.resolve(strict=True) != wiki_root:
            return None
        same_source = _source_judge(decision, source_revision)
        files = sorted(itertools.islice(wiki_root.rglob('*.md'), worker.SELECTED_PROOF_MAX_CARDS + 1))
        if len(files) > worker.SELECTED_PROOF_MAX_CARDS:
            return None
        total = refreshable = 0
        superseded = []
        for path in files:
            rel_card = path.relative_to(wiki_root)
            if any(part.startswith('.') for part in rel_card.parts) or settings_api.is_wiki_meta_page(rel_card):
                continue
            if path.is_symlink() or path.resolve(strict=True) != path or not path.is_file():
                return None
            data = worker._read_proof_card_bounded(
                path, min(worker.SELECTED_PROOF_MAX_CARD_BYTES, worker.SELECTED_PROOF_MAX_BYTES - total))
            if data is None:
                return None
            total += len(data)
            # read_text's universal newlines: the retirement compares these exact bytes.
            text = data.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
            state = citation(text, same_source, source_revision)
            if state == SUPERSEDED:
                superseded.append((path, text))
            refreshable += state == REFRESHABLE
        return {'superseded': superseded, 'refreshable': refreshable, 'wikiRoot': wiki_root}
    except Exception:
        return None


def _attempt(root, path: str) -> tuple[str, dict] | None:
    """``(sha256, payload)`` recorded for one source path, or None (none, or no readable ledger)."""
    from memvane.wiki.pipeline import api as pipeline
    try:
        if not pipeline.pipeline_db_path(Path(root)).exists():
            return None
        with pipeline.open_pipeline(Path(root), readonly=True) as store:
            row = store.get_suppression(RECORD_KIND, path)
    except Exception:
        return None
    if not isinstance(row, dict) or not isinstance(row.get('payload'), dict):
        return None
    return row.get('bodyHash'), row['payload']


def recorded(root, source_revision: dict) -> bool:
    """An attempt is already recorded for these exact source bytes."""
    found = _attempt(root, source_revision.get('path'))
    return found is not None and found[0] == source_revision.get('sha256')


def mark_attempt(source_revision: dict) -> None:
    """This job extracts because of old cards: a failure is recorded for these bytes."""
    _ATTEMPT.set({key: source_revision.get(key) for key in ('path', 'collection', 'sha256', 'size')})


def _record(root, revision: dict, outcome: str, cpath, job, **fields) -> bool:
    from memvane.wiki.pipeline import api as pipeline
    payload = {'outcome': outcome, 'collection': revision.get('collection'),
               'size': revision.get('size'), **fields}
    # Checked (rule 1): without the row the next refresh of these bytes pays for
    # one more extraction; the failure is logged and nothing is retired.
    written = pipeline.set_suppression(Path(root), RECORD_KIND, revision.get('path'), revision.get('sha256'), payload)
    if not written and cpath is not None:
        cpath.append(_diagnostic_row('deferred', job, 'supersession_record_failed'))
    return written


def record_failure(root, reason: str, cpath, job) -> None:
    """A marked attempt whose answer was unusable for good: these bytes are not extracted again."""
    attempt = _ATTEMPT.get()
    if attempt is not None:
        _record(root, attempt, 'failed', cpath, job, reason=reason)


class Outcomes:
    """The cards one extraction's writer outcomes took part with, and whether that is all of it.

    ``complete`` holds while the answer had candidates (all objects) within
    ``cardsPerSource`` and every outcome named its card: ``created``/``updated``
    with this revision name the card written (``written``), ``merge-needed`` the
    cards the writer would not choose between or write over (``named``), and
    ``split-required`` waits for a later round. Anything else (rejection,
    failure, a missing path, another revision) leaves the account incomplete.
    """

    def __init__(self, root, source_revision: dict, candidates, max_cards: int):
        self.root = Path(root)
        self.revision = source_revision
        self.written: set = set()
        self.named: set = set()
        self.complete = (isinstance(candidates, list) and 0 < len(candidates) <= max_cards
                         and all(isinstance(item, dict) for item in candidates))

    def note(self, result):
        """Record one writer result and return it unchanged."""
        action = result.get('action') if isinstance(result, dict) else None
        if action == 'split-required':
            return result
        if action in ('created', 'updated'):
            revisions = result.get('sourceRevisions')
            name = result.get('targetPath')
            if (isinstance(revisions, list) and len(revisions) == 1
                    and card_api.same_revision(revisions[0], self.revision) and isinstance(name, str) and name):
                self.written.add(self._rel(name))
                return result
        elif action == 'merge-needed':
            matches = result.get('targetMatches')
            names = [name for name in [result.get('targetPath'), result.get('matchedPath'),
                                       *(matches if isinstance(matches, list) else [])]
                     if isinstance(name, str) and name]
            if names:
                self.named.update(self._rel(name) for name in names)
                return result
        self.complete = False
        return result

    def _rel(self, name: str) -> str:
        path = (self.root / name).resolve()
        try:
            return path.relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return path.as_posix()


def _unreturned(root: Path, superseded, keep) -> list:
    root = Path(root).resolve()
    return [(card, text) for card, text in superseded if card.resolve().relative_to(root).as_posix() not in keep]


def ratio_held(unreturned: int, superseded: int) -> bool:
    return unreturned >= RATIO_MIN and unreturned > RATIO_LIMIT * superseded


def record_after(root, job: dict, source_revision: dict, selected, cpath, outcomes: Outcomes,
                 truncated: bool) -> None:
    """Record what a finished extraction means for the source's old cards; retire nothing yet."""
    if selected is None:
        return
    if not outcomes.complete or not outcomes.written | outcomes.named:
        _record(root, source_revision, 'incomplete', cpath, job)
        return
    if truncated:
        # The extractor saw only the first maxSourceChars: a card it did not
        # return may describe the rest, so nothing here is evidence of absence.
        cpath.append(_diagnostic_row('deferred', job, 'superseded_held_truncated'))
        _record(root, source_revision, 'held_truncated', cpath, job)
        return
    found = scan(root, selected, source_revision)
    if found is None:
        cpath.append(_diagnostic_row('deferred', job, 'superseded_scan_unavailable'))
        return
    keep = outcomes.written | outcomes.named
    unreturned = _unreturned(root, found['superseded'], keep)
    fields = {'written': sorted(outcomes.written)[:20], 'named': sorted(outcomes.named)[:20]}
    if not unreturned:
        _record(root, source_revision, 'settled', cpath, job, **fields)
    elif ratio_held(len(unreturned), len(found['superseded'])):
        cpath.append({**_diagnostic_row('deferred', job, 'superseded_retire_guarded'),
                      'wouldRetire': len(unreturned), 'supersededCards': len(found['superseded'])})
        _record(root, source_revision, 'held_ratio', cpath, job, wouldRetire=len(unreturned),
                superseded=len(found['superseded']), **fields)
    else:
        _record(root, source_revision, 'pending', cpath, job, wouldRetire=len(unreturned),
                superseded=len(found['superseded']), **fields)


def _open_targets(root) -> set:
    from memvane.wiki.pipeline import api as pipeline
    with pipeline.open_pipeline(Path(root), readonly=True) as store:
        return {row['payload'].get('targetPath') for row in store.open_verify_jobs()}


def _pending(root) -> list:
    from memvane.wiki.pipeline import api as pipeline
    if not pipeline.pipeline_db_path(Path(root)).exists():
        return []
    with pipeline.open_pipeline(Path(root), readonly=True) as store:
        rows = []
        for path, sha in sorted(store.suppressions(RECORD_KIND).items()):
            row = store.get_suppression(RECORD_KIND, path)
            payload = row.get('payload') if isinstance(row, dict) else None
            if isinstance(payload, dict) and payload.get('outcome') == 'pending':
                rows.append((path, sha, payload))
            if len(rows) >= MAX_SETTLE_ATTEMPTS:
                break
        return rows


def _settle_one(root: Path, selected, path: str, sha: str, payload: dict, open_targets: set, budget: list,
                log) -> None:
    from memvane.wiki.lifecycle import api as wiki_source_retire
    job = {'trigger': 'supersession', 'source': {'kind': 'file', 'path': path,
                                                  'collection': payload.get('collection', '')}}
    revision = {'kind': 'file', 'path': path, 'collection': payload.get('collection', ''),
                'sha256': sha, 'size': payload.get('size')}
    snapshot = card_api.snapshot_bytes(root / path)
    if snapshot is None or snapshot[0]['sha256'] != sha:
        _record(root, revision, 'source_changed', log, job, **{k: payload.get(k) for k in ('written', 'named')})
        return
    same_source = _source_judge(selected[0], revision)
    states = []
    for name in payload.get('written') or []:
        card = root / name
        try:
            text = card.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            text = ''
        states.append('pass' if text and _current_verified(text, same_source, revision)
                      else 'wait' if name in open_targets else 'fail')
    if 'wait' in states:
        return
    fields = {k: payload.get(k) for k in ('written', 'named')}
    if 'fail' in states:
        # A rewritten card did not pass review: its old account stays, and so do the others.
        log.append(_diagnostic_row('deferred', job, 'superseded_review_failed'))
        _record(root, revision, 'held_review_failed', log, job, **fields)
        return
    found = scan(root, selected, revision)
    if found is None:
        log.append(_diagnostic_row('deferred', job, 'superseded_scan_unavailable'))
        return
    unreturned = _unreturned(root, found['superseded'], set(payload.get('written') or [])
                             | set(payload.get('named') or []))
    # The recorded denominator: the rewritten cards no longer cite the old revision.
    superseded = max(len(found['superseded']), int(payload.get('superseded') or 0))
    if ratio_held(len(unreturned), superseded):
        _record(root, revision, 'held_ratio', log, job, wouldRetire=len(unreturned), superseded=superseded, **fields)
        return
    retired = []
    for card, text in unreturned:
        if budget[0] <= 0:
            break
        budget[0] -= 1
        try:
            outcome = wiki_source_retire.retire_card(root, found['wikiRoot'], selected, card, text, RETIRE_REASON)
        except _EXPECTED:
            outcome = 'error'
            log.append(_diagnostic_row('deferred', job, 'superseded_retire_error'))
        # Counted only (rule 3): a card left in place is still superseded, so the
        # next settle pass retires it; no paid call is involved.
        if outcome == 'retired':
            budget[1] += 1
            retired.append(card.relative_to(found['wikiRoot']).as_posix())
    if retired:
        log.append({**_diagnostic_row('retired', job, RETIRE_REASON), 'retiredCards': retired[:20],
                    'retiredCount': len(retired)})
    if len(retired) == len(unreturned):
        _record(root, revision, 'retired', log, job, retired=len(retired), **fields)


def settle(root, selected=None) -> int:
    """Retire the cards of pending attempts whose rewritten cards passed review; returns the count.

    Runs after the verify pass. Every expected failure is logged and the pass
    goes on with the next attempt; the pending row stays for the next pass.
    """
    from memvane.wiki.compile import wiki_compile_worker as worker
    from memvane.wiki.workspace import api as workspace
    root = Path(root).resolve()
    log = worker.candidate_log(root)
    try:
        if selected is None:
            selected = workspace.selected(root)
        if selected is None:
            return 0
        rows = _pending(root)
        open_targets = _open_targets(root) if rows else set()
    except _EXPECTED:
        return 0
    budget = [MAX_RETIRE_PER_RUN, 0]  # [cards this pass may still retire, cards retired]
    for path, sha, payload in rows:
        try:
            _settle_one(root, selected, path, sha, payload, open_targets, budget, log)
        except _EXPECTED:
            log.append(_diagnostic_row('deferred', {'source': {'path': path}}, 'superseded_settle_error'))
    retired = budget[1]
    if retired:
        try:
            workspace.request_sync(root, selected)
        except ValueError:
            pass  # Rule 3: the next full scan drops the absent cards.
    return retired

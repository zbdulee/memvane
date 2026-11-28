"""Which cards a newer extraction of their one source supersedes.

A machine card cites one source revision (``sourceRevisions``). When the source
changes, one extraction of the new bytes is the compiler's whole account of it:
the cards it returns are rewritten, and a card it no longer returns describes
bytes that no longer exist. Such a card is *superseded* only when nobody else
owns any of it:

- the plugin owns its bytes (``auto_writable_text``: plugin ``createdBy``,
  managed block, not ``superseded``);
- an automatic source trigger made it (``triggers`` within
  ``AUTO_SOURCE_TRIGGERS``), so a manual compile's card stays;
- it cites exactly this one source, so a card that is still the account of
  another source stays;
- no hand-written text: a ``verifiedBodyHash`` that no longer matches the body,
  or any text outside the managed block other than the disclaimer and HTML
  comments, keeps the card.

Every other card citing an older revision of the source is ``PROTECTED``: it
neither asks for an extraction nor retires. The judgement reads the card bytes
only; no ledger row decides it.

The compile worker uses it twice. Before a paid extraction, a verified card that
proves the current revision skips the source only while ``scan`` finds no
superseded card. After an extraction whose every candidate reached the writer
with a known card (``Outcomes``), ``retire_after`` retires the superseded cards
the answer did not return through the lifecycle writer
(``wiki_source_retire.retire_card``). Retirement is held for the whole source
when it would break the lifecycle mass-deletion rule.
"""
from __future__ import annotations

import itertools
from pathlib import Path

from memvane.settings import api as settings_api
from memvane.wiki.card import api as card_api
from memvane.wiki.card.types import (AUTO_BLOCK_RE, AUTO_DISCLAIMER_PREFIXES, COMMENT_CLOSE, COMMENT_OPEN,
                                     FRESH, FRONTMATTER_RE, MASS_DELETE_FRACTION, MASS_DELETE_MIN)

RETIRE_REASON = 'superseded_by_revision'
AUTO_SOURCE_TRIGGERS = frozenset(('post_tool_source', 'post_sync_source', 'session_start_source'))
CURRENT = 'current'
SUPERSEDED = 'superseded'
PROTECTED = 'protected'


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


def machine_card(text: str) -> bool:
    """A plugin-owned card with compiler provenance: the mass guard's denominator."""
    match = FRONTMATTER_RE.match(text)
    return (match is not None and bool(card_api.frontmatter_source_revisions(match.group(1)))
            and card_api.auto_writable_text(text)[0])


def citation(text: str, same_source, current: dict) -> str | None:
    """How one card (LF text) cites the source whose bytes are now ``current``.

    ``same_source(revision)`` says whether a normalized revision names the
    source. None: the card does not cite it. ``CURRENT``: it cites these bytes.
    ``SUPERSEDED``: it cites only an older revision and may be retired by an
    extraction that does not return it. ``PROTECTED``: it cites an older
    revision (or names the source with unreadable provenance) and stays.
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
            or any(trigger not in AUTO_SOURCE_TRIGGERS for trigger in triggers)):
        return PROTECTED
    digest = meta.get('verifiedBodyHash')
    if digest not in (None, '', []) and digest != card_api.card_body_hash(text):
        return PROTECTED
    if _hand_written(text[match.end():]):
        return PROTECTED
    return SUPERSEDED


def guard_tripped(retiring: int, decided: int) -> bool:
    """The wiki lifecycle's mass-deletion rule applied to one source's retirement."""
    return retiring > MASS_DELETE_MIN and retiring > MASS_DELETE_FRACTION * decided


def _diagnostic_row(action: str, job: dict, reason: str) -> dict:
    """A compile log row that leaves the job's settling reason alone."""
    from memvane.wiki.compile import wiki_compile_worker as worker
    previous = worker._LAST_REASON.get()
    row = worker.bounded_failure(action, job, reason)
    worker._LAST_REASON.set(previous)
    return row


def scan(root, bound, source_revision: dict):
    """``(superseded, decided, wiki_root)`` for the source's current bytes, else None.

    ``superseded`` lists ``(card, text)`` of every ``SUPERSEDED`` card;
    ``decided`` counts the machine cards of the wiki (the mass guard's
    denominator). The bounds and failures are those of the worker's
    ``selected_compiled_revision_card``: any unsafe, oversized or unreadable card
    returns None, which the worker reads as "a superseded card may exist" before
    skipping a paid call and as "retire nothing" after one.
    """
    from memvane.wiki.compile import wiki_compile_worker as worker
    if bound is None:
        return None
    try:
        decision = bound[0]
        wiki_root = Path(next(item['sourceRoot'] for item in decision['collections'] if item['role'] == 'wiki'))
        if wiki_root.is_symlink() or wiki_root.resolve(strict=True) != wiki_root:
            return None
        label = settings_api.decision_resolver(decision)
        wanted = label(source_revision.get('collection'), source_revision.get('path'))

        def same_source(item):
            # A renamed raw collection's old label resolves by path (recall rule).
            return (item.get('path') == source_revision.get('path')
                    and label(item.get('collection'), item.get('path')) == wanted)
        files = sorted(itertools.islice(wiki_root.rglob('*.md'), worker.SELECTED_PROOF_MAX_CARDS + 1))
        if len(files) > worker.SELECTED_PROOF_MAX_CARDS:
            return None
        total = decided = 0
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
            decided += machine_card(text)
            if citation(text, same_source, source_revision) == SUPERSEDED:
                superseded.append((path, text))
        return superseded, decided, wiki_root
    except Exception:
        return None


class Outcomes:
    """The cards one extraction's writer outcomes took part with, and whether that is all of it.

    ``complete`` holds while the answer had candidates within ``cardsPerSource``
    and every outcome named its card: ``created``/``updated`` with this revision
    name the card written, ``merge-needed`` the cards the writer would not choose
    between or write over, and ``split-required`` waits for a later round.
    Anything else (rejection, failure, a missing path, another revision) leaves
    the account incomplete, and nothing retires.
    """

    def __init__(self, root, source_revision: dict, candidates, max_cards: int):
        self.root = Path(root)
        self.revision = source_revision
        self.touched: set = set()
        self.complete = isinstance(candidates, list) and 0 < len(candidates) <= max_cards

    def note(self, result):
        """Record one writer result and return it unchanged."""
        action = result.get('action') if isinstance(result, dict) else None
        names = None
        if action == 'split-required':
            names = []
        elif action in ('created', 'updated'):
            revisions = result.get('sourceRevisions')
            if (isinstance(revisions, list) and len(revisions) == 1
                    and card_api.same_revision(revisions[0], self.revision)
                    and isinstance(result.get('targetPath'), str) and result['targetPath']):
                names = [result['targetPath']]
        elif action == 'merge-needed':
            matches = result.get('targetMatches')
            names = [name for name in [result.get('targetPath'), result.get('matchedPath'),
                                       *(matches if isinstance(matches, list) else [])]
                     if isinstance(name, str) and name] or None
        if names is None:
            self.complete = False
        else:
            self.touched.update((self.root / name).resolve() for name in names)
        return result


def retire_after(root, job: dict, src: Path, source_revision: dict, selected, cpath, outcomes: Outcomes) -> None:
    """Retire the source's superseded cards a finished, complete extraction did not return."""
    if selected is None or not outcomes.complete or not outcomes.touched:
        return  # Fail closed: an empty, capped or partly unknown answer retires nothing.
    from memvane.wiki.lifecycle import api as wiki_source_retire
    from memvane.wiki.workspace import api as workspace
    found = scan(root, selected, source_revision)
    if found is None:
        cpath.append(_diagnostic_row('deferred', job, 'superseded_scan_unavailable'))
        return
    superseded, decided, wiki_root = found
    retiring = [(card, text) for card, text in superseded if card.resolve() not in outcomes.touched]
    if not retiring:
        return
    names = [card.relative_to(wiki_root).as_posix() for card, _ in retiring]
    if guard_tripped(len(retiring), decided):
        cpath.append({**_diagnostic_row('deferred', job, 'superseded_retire_guarded'),
                      'wouldRetire': len(retiring), 'decidedCards': decided, 'cards': names[:20]})
        return
    if card_api.compare_revision(src, source_revision)[0] != FRESH:
        cpath.append(_diagnostic_row('deferred', job, 'superseded_source_changed'))
        return
    retired = []
    for (card, text), name in zip(retiring, names):
        try:
            outcome = wiki_source_retire.retire_card(root, wiki_root, selected, card, text, RETIRE_REASON)
        except OSError:
            outcome = 'blocked'
        # Counted only (rule 3): a card left in place still cites the old revision,
        # so the source's next refresh extracts once more and retires it then.
        if outcome == 'retired':
            retired.append(name)
    if retired:
        try:
            workspace.request_sync(root, selected)
        except ValueError:
            pass  # Rule 3: the next full scan drops the absent cards.
    cpath.append({**_diagnostic_row('retired', job, RETIRE_REASON), 'retiredCards': retired[:20],
                  'retiredCount': len(retired), 'keptCount': len(retiring) - len(retired)})

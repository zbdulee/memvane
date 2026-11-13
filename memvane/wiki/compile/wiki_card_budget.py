#!/usr/bin/env python3
"""Wiki card body budget for the local selection model.

The live selector scores a (user request, card) pair with the legacy input
view of its checkpoint: ``max_len`` 1024 tokens. It reads a card through
``context_learning.compact_input``: the whole file after the frontmatter
(banner, auto-block markers, summary and any hand-written section). A pair
longer than ``max_len`` does not fit the model whole. This module measures the
complete, untruncated body that ``derive`` returns, so a card the compiler
writes is scored whole; measuring a cut prefix would hide the overflow.

Budget arithmetic (mmBERT tokenizer of the live selector, measured):

- ``PAIR_OVERHEAD`` 38: ``[CLS]`` + question head + ``[SEP]`` + two option
  spans + ``[SEP]`` + ``User request:\\n`` + ``\\nWiki card body:\\n`` +
  final ``[SEP]``. Tokenizing request and body jointly inside the state string
  is one token shorter than counting them apart, so the separate count here is
  an upper bound.
- ``BODY_BUDGET`` 700: the derived body. The rest, ``QUERY_ROOM`` 286 tokens,
  is left for the user request. Card titles live in the frontmatter and never
  enter the pair.

Measurement is exact. The selector runtime's own ``tokenizers`` library counts
the body with a selector checkpoint's ``tokenizer/tokenizer.json``
(``method: tokenizer``). Candidates are the project's own live selector first,
then every other live selector of this user's learning state (same checkpoint
family), so counting works wherever the learning runtime and a selector model
exist. A candidate whose runtime fails (import error, corrupt file, timeout)
yields to the next; when every candidate fails the result is
``measure_failed`` and the card is written without a size check (best effort).

Each failed candidate is one attempt with a fixed reason code from
``MEASURE_REASONS`` (and the exit status for ``runtime_exit``). A failed
measurement carries an opaque ``measureId``, the first ``MAX_ATTEMPTS_LOGGED``
attempts, ``attemptsTotal`` and the last code as ``failure``; the caller logs
them (``failure_fields``). No runtime path, stderr, exception text or card text
leaves this module in a diagnostic.

Only a machine with no selector tokenizer at all writes cards unmeasured
(``method: unavailable``, ``fits`` true), and the caller logs it: no local
selector exists there to protect, a provable tokenizer-free bound (UTF-8 bytes
per non-ASCII character) overcounts real Korean text about threefold and would
force a paid split on most cards, and a selector added later scores an
over-length card by its leading part.

``estimate`` is only prompt guidance (the summary room the extractor is told),
never a gate: per character class, ASCII digit 1, ASCII letter run
ceil(len / 4), newline 1, space or tab 0.5, other ASCII 1, Hangul syllable 1,
other BMP character 2, character outside the BMP 4.

A body ``derive`` refuses as oversized (over 16 KiB) is over the budget without
counting (``method: derive_limit``).
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys

MODEL_MAX_LEN = 1024
PAIR_OVERHEAD = 38
BODY_BUDGET = 700
QUERY_ROOM = MODEL_MAX_LEN - PAIR_OVERHEAD - BODY_BUDGET

METHOD_TOKENIZER = 'tokenizer'
METHOD_UNAVAILABLE = 'unavailable'
METHOD_FAILED = 'measure_failed'
METHOD_DERIVE_LIMIT = 'derive_limit'
MAX_SELECTOR_STATES = 64
EXACT_TIMEOUT_SECONDS = 20
MAX_ATTEMPTS_LOGGED = 8

# Why one selector runtime could not count (one code per failed attempt).
REASON_SPAWN_FAILED = 'runtime_spawn_failed'      # the interpreter did not start
REASON_TIMEOUT = 'runtime_timeout'                # no answer within EXACT_TIMEOUT_SECONDS
REASON_RUNTIME_ERROR = 'runtime_error'            # any other subprocess failure
REASON_EXIT = 'runtime_exit'                      # non-zero exit (negative: signal)
REASON_INPUT_UNENCODABLE = 'input_unencodable'    # the body could not be sent
REASON_OUTPUT_UNDECODABLE = 'output_undecodable'  # stdout was not text
REASON_OUTPUT_NOT_JSON = 'output_not_json'
REASON_OUTPUT_SHAPE = 'output_shape_invalid'      # not a list of non-negative integers
REASON_COUNT_MISMATCH = 'count_mismatch'          # one count per text expected
MEASURE_REASONS = (REASON_SPAWN_FAILED, REASON_TIMEOUT, REASON_RUNTIME_ERROR, REASON_EXIT,
                   REASON_INPUT_UNENCODABLE, REASON_OUTPUT_UNDECODABLE,
                   REASON_OUTPUT_NOT_JSON, REASON_OUTPUT_SHAPE, REASON_COUNT_MISMATCH)

_CLASS_RE = re.compile(
    r'[0-9]|[A-Za-z]+|\n|[ \t]+|[\x00-\x7f]|[가-힣]|[\u0080-￿]|[\U00010000-\U0010ffff]')

_EXACT_SCRIPT = (
    'import json, sys\n'
    'from tokenizers import Tokenizer\n'
    'tok = Tokenizer.from_file(sys.argv[1])\n'
    'texts = json.loads(sys.stdin.read())\n'
    'print(json.dumps([len(tok.encode(t, add_special_tokens=False).ids) for t in texts]))\n'
)


def estimate(text: str) -> int:
    """Tokenizer-free guidance estimate of ``text``; never used as a gate."""
    total = 0.0
    for match in _CLASS_RE.finditer(text):
        group = match.group(0)
        char = group[0]
        if '0' <= char <= '9':
            total += 1
        elif char.isascii() and char.isalpha():
            total += math.ceil(len(group) / 4)
        elif char == '\n':
            total += 1
        elif char in ' \t':
            total += 0.5 * len(group)
        elif char.isascii():
            total += 1
        elif '가' <= char <= '힣':
            total += 1
        elif ord(char) > 0xFFFF:
            total += 4
        else:
            total += 2
    return math.ceil(total)


def derived_body(card_text: str) -> str | None:
    """The body the selector reads, from the selector's own ``derive``.

    None when ``derive`` refuses the card (no frontmatter, empty or oversized
    body); such a card cannot be scored at all.
    """
    from memvane.learning.context_learning.compact_input import derive
    from memvane.learning.context_learning.contracts import digest
    try:
        return derive(card_text, digest(card_text))['body_text']
    except ValueError:
        return None


def _selection_state(config: dict, project_root: Path):
    """The live selector state directory the recall path would consult, or None."""
    from memvane.learning.context_learning.seam import _private_state
    shared = config.get('sharedLearning', {}) if isinstance(config, dict) else {}
    if isinstance(shared, dict) and shared.get('enabled') is True:
        if shared.get('profile') == 'qmd-local-learning-profile-v1':
            from memvane.learning.context_learning.learning_profile import selection_root
            return selection_root(config, project_root)
        if shared.get('trialSelection') is True:
            from memvane.learning.context_learning.shared import _safe_root
            return _safe_root(config, project_root)
    return _private_state(config, Path(project_root), create=False)


def _policy_tokenizer(state) -> tuple[str, str] | None:
    from memvane.learning.context_learning.dataset import read_private_json
    policy = read_private_json(Path(state) / 'live-selector.json')
    argv = policy.get('trainerArgv') if isinstance(policy, dict) else None
    if (policy.get('schema') != 'qmd-live-selector-v1' or not isinstance(argv, list)
            or len(argv) != 3 or not all(isinstance(item, str) and Path(item).is_absolute()
                                         for item in argv)):
        return None
    python = Path(argv[0])
    tokenizer = Path(argv[2]) / 'tokenizer' / 'tokenizer.json'
    if (not os.access(python, os.X_OK) or tokenizer.is_symlink()
            or not tokenizer.is_file()):
        return None
    return str(python), str(tokenizer)


def selector_tokenizers(config: dict, project_root) -> list[tuple[str, str]]:
    """Every usable (runtime python, tokenizer.json) of a live selector, in order.

    Read only. A live selector policy names the runtime interpreter and the
    checkpoint directory (``trainerArgv`` = python, adapter, model dir). The
    project's own selector comes first, then the user's other selectors under
    ``live-selection`` (sorted, at most ``MAX_SELECTOR_STATES``). Usable means the
    files exist; whether the runtime works is decided by counting.
    """
    found: list[tuple[str, str]] = []

    def add(state):
        try:
            if Path(state).is_symlink() or not Path(state).is_dir():
                return
            pair = _policy_tokenizer(state)
        except Exception:  # noqa: BLE001
            return
        if pair is not None and pair not in found:
            found.append(pair)

    try:
        state = _selection_state(config, Path(project_root).resolve())
        if state is not None:
            add(state)
    except Exception:  # noqa: BLE001 - the user's other selectors still count
        pass
    try:
        from memvane.learning.context_learning.runtime_setup import learning_state_base
        parent = learning_state_base() / 'live-selection'
        if not parent.is_symlink() and parent.is_dir():
            for state in sorted(parent.iterdir())[:MAX_SELECTOR_STATES]:
                add(state)
    except Exception:  # noqa: BLE001
        pass
    return found


def selector_tokenizer(config: dict, project_root) -> tuple[str, str] | None:
    """The first usable selector tokenizer, or None."""
    found = selector_tokenizers(config, project_root)
    return found[0] if found else None


def count_exact_detailed(texts: list[str], tokenizer: tuple[str, str] | None
                         ) -> tuple[list[int] | None, dict | None]:
    """``(counts, None)``, or ``(None, failure)`` with a fixed ``reason`` code.

    ``failure`` is ``{"reason": code}`` plus ``"exit"`` for ``runtime_exit``.
    Nothing from the runtime's output or the exception goes into it.
    """
    if tokenizer is None or not texts:
        return None, None
    python, tokenizer_json = tokenizer
    try:
        proc = subprocess.run(
            [python, '-I', '-c', _EXACT_SCRIPT, tokenizer_json],
            input=json.dumps(texts, ensure_ascii=False), text=True, capture_output=True,
            timeout=EXACT_TIMEOUT_SECONDS, shell=False,
            env={'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LANG': 'C.UTF-8'})
    except subprocess.TimeoutExpired:
        return None, {'reason': REASON_TIMEOUT}
    except subprocess.SubprocessError:
        return None, {'reason': REASON_RUNTIME_ERROR}
    except OSError:
        return None, {'reason': REASON_SPAWN_FAILED}
    except UnicodeEncodeError:
        return None, {'reason': REASON_INPUT_UNENCODABLE}
    except UnicodeDecodeError:
        return None, {'reason': REASON_OUTPUT_UNDECODABLE}
    except ValueError:  # an argument Popen refuses (embedded NUL)
        return None, {'reason': REASON_SPAWN_FAILED}
    if proc.returncode != 0:
        return None, {'reason': REASON_EXIT, 'exit': proc.returncode}
    try:
        counts = json.loads(proc.stdout)
    except ValueError:
        return None, {'reason': REASON_OUTPUT_NOT_JSON}
    if not isinstance(counts, list) or not all(type(item) is int and item >= 0
                                               for item in counts):
        return None, {'reason': REASON_OUTPUT_SHAPE}
    if len(counts) != len(texts):
        return None, {'reason': REASON_COUNT_MISMATCH}
    return counts, None


def count_exact(texts: list[str], tokenizer: tuple[str, str] | None) -> list[int] | None:
    """Selector token counts of ``texts`` through the selector runtime, or None."""
    return count_exact_detailed(texts, tokenizer)[0]


def _candidates(tokenizers) -> list[tuple[str, str]]:
    if tokenizers is None:
        return []
    if isinstance(tokenizers, tuple):
        return [tokenizers]
    return list(tokenizers)


def measure_bodies_detailed(bodies: list[str], tokenizers=None
                            ) -> tuple[list[int] | None, str, dict]:
    """``measure_bodies`` plus the failed attempts before the answer.

    Returns ``(counts, method, diagnostic)``. ``diagnostic`` is empty when no
    candidate failed, else ``attempts`` (the first ``MAX_ATTEMPTS_LOGGED``
    failures as ``{"attempt": n, "reason": code[, "exit": status]}``, ``n`` the
    candidate's 1-based position), ``attemptsTotal`` and ``failure`` (the last
    failure's code, logged or not).
    """
    candidates = _candidates(tokenizers)
    attempts: list[dict] = []
    total = 0
    last = None
    for position, tokenizer in enumerate(candidates, 1):
        counts, failure = count_exact_detailed(bodies, tokenizer)
        if counts is None and failure is not None:
            total += 1
            last = failure['reason']
            if len(attempts) < MAX_ATTEMPTS_LOGGED:
                attempts.append({'attempt': position, **failure})
        diagnostic = ({'attempts': attempts, 'attemptsTotal': total, 'failure': last}
                      if total else {})
        if counts is not None:
            return counts, METHOD_TOKENIZER, diagnostic
    diagnostic = {'attempts': attempts, 'attemptsTotal': total, 'failure': last} if total else {}
    return None, (METHOD_FAILED if candidates else METHOD_UNAVAILABLE), diagnostic


def measure_bodies(bodies: list[str], tokenizers=None) -> tuple[list[int] | None, str]:
    """Exact counts from the first selector runtime that answers.

    ``tokenizers`` is one ``(python, tokenizer.json)`` pair or a list of them.
    Returns ``(None, 'unavailable')`` with no candidate and
    ``(None, 'measure_failed')`` when every candidate failed.
    """
    counts, method, _ = measure_bodies_detailed(bodies, tokenizers)
    return counts, method


def measure_card(card_text: str, tokenizer=None) -> dict:
    """``{fits, size, budget, method, reason}`` for a complete card file text.

    ``size`` counts the derived body only. ``reason`` is ``over_budget`` (also
    for a body ``derive`` refuses as oversized), ``selector_input_invalid`` (no
    frontmatter or an empty body; not a card the selector reads),
    ``measure_failed`` (every selector runtime failed) or, with ``fits`` true,
    ``tokenizer_unavailable`` (no selector tokenizer on the machine). When a
    runtime failed, the result also carries ``measureId``, ``attempts``,
    ``attemptsTotal`` and ``failure`` (the last attempt's code).
    """
    body = derived_body(card_text)
    if body is None:
        from memvane.wiki.card import api as yaml_scalars
        match = yaml_scalars.FRONTMATTER_RE.match(card_text) if isinstance(card_text, str) else None
        rest = card_text[match.end():] if match else ''
        if rest.strip() and '\x00' not in rest:
            return {'fits': False, 'size': None, 'budget': BODY_BUDGET,
                    'method': METHOD_DERIVE_LIMIT, 'reason': 'over_budget'}
        return {'fits': False, 'size': None, 'budget': BODY_BUDGET,
                'method': METHOD_UNAVAILABLE, 'reason': 'selector_input_invalid'}
    counts, method, diagnostic = measure_bodies_detailed([body], tokenizer)
    if diagnostic:
        diagnostic = {'measureId': os.urandom(8).hex(), **diagnostic}
    if counts is None:
        unavailable = method == METHOD_UNAVAILABLE
        return {'fits': unavailable, 'size': None, 'budget': BODY_BUDGET, 'method': method,
                'reason': 'tokenizer_unavailable' if unavailable else 'measure_failed',
                **diagnostic}
    fits = counts[0] <= BODY_BUDGET
    return {'fits': fits, 'size': counts[0], 'budget': BODY_BUDGET, 'method': method,
            'reason': None if fits else 'over_budget', **diagnostic}


def failure_fields(measured: dict) -> dict:
    """The diagnostic fields of a ``measure_card`` result, for an event or ledger row.

    Only the opaque ``measureId``, attempt numbers, reason codes and exit
    statuses; empty when no runtime failed.
    """
    if not isinstance(measured, dict) or 'measureId' not in measured:
        return {}
    return {key: measured[key] for key in ('measureId', 'attempts', 'attemptsTotal', 'failure')}



def summary_budget() -> int:
    """Estimated room for the summary once the compiler's card scaffold is in the body.

    The scaffold is what ``wiki_compile.markdown_page`` puts after the
    frontmatter: banner, auto-block markers (16-hex ``sourceHash``) and the
    ``## Summary`` heading.
    """
    from memvane.wiki.card import wiki_markers
    scaffold = '\n'.join(['', wiki_markers.AUTO_DISCLAIMER, '',
                          wiki_markers.AUTO_START_TEMPLATE.format(source_hash='0' * 16),
                          '## Summary', '', wiki_markers.AUTO_END, ''])
    return max(0, BODY_BUDGET - estimate(scaffold))


def prompt_budget() -> dict:
    """The budget the extractor prompt states (``payload.cardBudget``)."""
    return {'bodyTokens': BODY_BUDGET, 'summaryTokens': summary_budget(),
            'modelMaxTokens': MODEL_MAX_LEN}


def emit(root, outcome: str, *, bound, path: str = '', title: str = '',
         reason: str | None = None, **fields) -> bool:
    """One ``wiki.card`` event in the install log. Never raises.

    Field names avoid the word "token": the event log drops such keys.
    """
    try:
        from memvane.diagnostics import api as event_log
        state_root = event_log.state_root_of(bound[0].get('journalPath')) if bound else None
        if state_root is None:
            return False
        install = event_log.install_of(bound[0]) if bound else None
        return event_log.emit('wiki.card', outcome=outcome, reason=reason,
                              state_root=state_root, install=install,
                              path=str(path)[:256], title=' '.join(str(title).split())[:200],
                              **fields)
    except Exception:  # noqa: BLE001
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='Measure wiki cards against the local selection body budget.')
    parser.add_argument('--cwd', default=os.getcwd())
    parser.add_argument('cards', nargs='+',
                        help='card files; with two or more, "combined" measures the first '
                             "card's body followed by every other card's body. Without a "
                             'selector tokenizer, sizes and fits are null (method unavailable '
                             'or measure_failed).')
    args = parser.parse_args(argv)
    tokenizer = None
    try:
        from memvane.settings import config as qmd_config
        found = qmd_config.find_project_config(args.cwd)
        tokenizer = selector_tokenizers(found['config'], Path(found['projectRoot']))
    except Exception:  # noqa: BLE001
        tokenizer = None
    texts = [Path(card).read_text(encoding='utf-8') for card in args.cards]
    bodies = [derived_body(text) for text in texts]
    measurable = [body for body in bodies if body is not None]
    combined_body = '\n'.join(measurable) if len(bodies) > 1 and None not in bodies else None
    counts, method = measure_bodies(measurable + ([combined_body] if combined_body else []),
                                    tokenizer)
    cards = []
    position = 0
    for card, text, body in zip(args.cards, texts, bodies):
        if body is None:
            measured = measure_card(text)
            cards.append({'path': card, 'size': None, 'fits': measured['fits'],
                          'reason': measured['reason']})
            continue
        size = counts[position] if counts is not None else None
        position += 1
        cards.append({'path': card, 'size': size,
                      'fits': None if size is None else size <= BODY_BUDGET})
    out = {'budget': BODY_BUDGET, 'method': method, 'cards': cards}
    if combined_body:
        size = counts[-1] if counts is not None else None
        out['combined'] = {'size': size, 'fits': None if size is None else size <= BODY_BUDGET}
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
